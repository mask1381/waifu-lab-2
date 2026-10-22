from __future__ import annotations

import json
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from waifu_lab.blocklist import blocked_reason
from waifu_lab.config import CONFIG_DIR
from waifu_lab.delivery import decide_delivery
from waifu_lab.enrich import load_dictionary_rows
from waifu_lab.images import download_image, edit_image, fit_telegram
from waifu_lab.pipeline import (
    ConfigError,
    _mark_done,
    _persist_stats,
    _record_failure,
    _save_state,
    prepare_post,
)
from waifu_lab.pixiv import adapt_list_item, fetch_user_illusts, refresh_access_token
from waifu_lab.rules import get_rule, resolve_rules, select_page
from waifu_lab.state import load_posted, load_runtime
from waifu_lab.tags import load_adult_set, load_content_map, load_drop_set
from waifu_lab.telegram import split_balanced
from waifu_lab.userbot_schedule import list_scheduled, schedule_album
from waifu_lab.userbot_zip import (
    gallery_part_size,
    prepare_zip,
    send_gallery_to_storage,
    send_zip_to_storage,
    storage_configured,
    userbot_configured,
)

log = logging.getLogger("waifu_lab.plan")

MAX_TRIES = 6
RECENT_KEEP = 40

DEFAULT_SCHEDULE = {
    "timezone": "Asia/Tehran",
    "lead_minutes": 10,
    "window_hours": 24,
    "max_scheduled": 95,
    "pool_pages": 2,
    "slot_tolerance_minutes": 5,
    "fallback_types": {"zip": ["album"], "telegraph": ["album"], "album": []},
    "slots": [
      {"time": "00:00", "type": "album"},
      {"time": "02:00", "type": "album"},
      {"time": "04:00", "type": "album"},
      {"time": "06:00", "type": "telegraph"},
      {"time": "08:00", "type": "album"},
      {"time": "10:00", "type": "zip"},
      {"time": "12:00", "type": "album"},
      {"time": "14:00", "type": "album"},
      {"time": "16:00", "type": "album"},
      {"time": "18:00", "type": "telegraph"},
      {"time": "20:00", "type": "album"},
      {"time": "22:00", "type": "zip"}
    ],
}


@dataclass(frozen=True)
class Slot:
    when: datetime
    type: str

    def label(self) -> str:
        return f"{self.when:%m-%d %H:%M} {self.type}"


def load_schedule() -> dict:
    path = CONFIG_DIR / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {**DEFAULT_SCHEDULE, **data}


def upcoming_slots(conf: dict, now: datetime) -> list[Slot]:
    tz = ZoneInfo(conf["timezone"])
    now = now.astimezone(tz)
    start = now + timedelta(minutes=int(conf["lead_minutes"]))
    end = now + timedelta(hours=int(conf["window_hours"]))
    out: list[Slot] = []
    for day in range(0, 3):
        d = (now + timedelta(days=day)).date()
        for s in conf["slots"]:
            hh, mm = (int(x) for x in str(s["time"]).split(":"))
            dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            if start <= dt < end:
                out.append(Slot(dt, str(s["type"])))
    out.sort(key=lambda s: s.when)
    return out


def _occupied(slot: Slot, existing: list[dict], tol: timedelta) -> bool:
    return any(abs(m["date"] - slot.when) <= tol for m in existing)


def _score(item: dict) -> int:
    return int(item.get("total_view") or item.get("score") or 0)


def _cost_estimate(item: dict, method: str) -> int:
    """How many messages of the channel's 100-slot scheduled queue the post will use.

    zip = 1: only the cover photo is scheduled, the zip goes to the storage channel right away.
    """
    if method == "album":
        return max(1, int(item.get("_pages") or 1))
    return 1


# ---- merging single-page posts into one album (rules.pages.merge_singles) ----

def _age(it: dict, now: datetime) -> timedelta:
    try:
        d = datetime.fromisoformat(str(it.get("create_date")))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return now - d
    except Exception:
        return timedelta(days=999)  # unknown date -> don't hold it back


def _virtual(chunk: list[dict]) -> dict:
    """One pool item that stands for several single-page posts."""
    v = dict(chunk[0])
    v["_merge"] = list(chunk)
    v["_merge_ids"] = [c["id"] for c in chunk]
    v["_pages"] = v["page_count"] = len(chunk)
    v["total_view"] = sum(_score(c) for c in chunk)
    return v


def merge_singles(items: list[dict], conf: dict) -> list[dict]:
    """Single-page album posts -> albums of min..max pages. NSFW/SFW never mixed.

    Leftovers (fewer than min) wait for more singles; after max_wait_days they go out alone.
    """
    mn = int(conf.get("min", 4))
    mx = min(int(conf.get("max", 10)), 10)
    wait = timedelta(days=float(conf.get("max_wait_days", 3)))
    now = datetime.now(timezone.utc)
    keep: list[dict] = []
    groups: dict[bool, list[dict]] = {True: [], False: []}
    for it in items:
        if it["_method"] == "album" and it["_pages"] == 1:
            groups[bool(it.get("is_nsfw"))].append(it)
        else:
            keep.append(it)
    for singles in groups.values():
        singles.sort(key=_score, reverse=True)
        if len(singles) >= mn:
            keep += [_virtual(c) for c in split_balanced(singles, mx)]
        else:
            keep += [it for it in singles if _age(it, now) >= wait]
    return keep


def build_pools(client: httpx.Client, token: str, artists: list[dict], posted_ids: set[str], pages: int) -> dict[str, dict]:
    """Fresh candidates of every artist, each tagged with the delivery method its artist rules give it.

    Posts matching data/blocked_content.csv never enter a pool (checked before merging).
    """
    pools: dict[str, dict] = {}
    for artist in artists:
        uid = str(artist["user_id"])
        rules = resolve_rules(artist)
        try:
            illusts = fetch_user_illusts(client, token, uid, posted_ids, max_pages=pages, early_break=False)
        except Exception as exc:
            log.warning("Skipping artist %s: %s", artist.get("name") or uid, exc)
            continue
        drop_last = bool(get_rule(rules, "pages", "drop_last", default=False))
        items: list[dict] = []
        blocked_n = 0
        for raw in illusts:
            if not raw:
                continue
            it = adapt_list_item(raw)
            if not it.get("id") or it["id"] in posted_ids:
                continue
            hit = blocked_reason(it)
            if hit:
                blocked_n += 1
                log.info("Blocked content %s (%s) from %s", it["id"], hit, artist.get("name") or uid)
                continue
            nsfw = bool(it.get("is_nsfw"))
            n = int(it.get("page_count") or 1)
            if drop_last and n > 1:
                n -= 1
            method, _ = decide_delivery(it, n, nsfw, rules)
            it["_method"], it["_pages"] = method, n
            items.append(it)

        ms = get_rule(rules, "pages", "merge_singles", default=None)
        if ms:
            before = len(items)
            items = merge_singles(items, ms)
            log.info("Merge singles %s: %s -> %s item(s)", artist.get("name") or uid, before, len(items))

        pools[uid] = {"artist": artist, "rules": rules, "items": items}
        kinds: dict[str, int] = {}
        for it in items:
            kinds[it["_method"]] = kinds.get(it["_method"], 0) + 1
        log.info("Pool %s: %s fresh %s (blocked %s)", artist.get("name") or uid, len(items), kinds, blocked_n)
        time.sleep(0.3)
    return pools


def _next_candidate(
    pools, slot_type: str, blocked: set[str], used_counts: dict, recent: list[str], slot_hhmm: str = ""
):
    """Best candidate for a slot type.

    Artists with schedule.preferred_slots compete only in those slots and win them first.
    Everyone else: least used today, then least recently used, then most viewed.
    """
    rows = []
    for uid, pool in pools.items():
        preferred = get_rule(pool["rules"], "schedule", "preferred_slots", default=None) or []
        if preferred and slot_hhmm not in preferred:
            continue
        cands = [c for c in pool["items"] if c["_method"] == slot_type and c["id"] not in blocked]
        if not cands:
            continue
        top = max(cands, key=_score)
        last = max((i for i, r in enumerate(recent) if r == uid), default=-1)
        rows.append(((0 if preferred else 1, used_counts.get(uid, 0), last, -_score(top)), uid, top))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    _, uid, top = rows[0]
    return uid, top


def _schedule_one(method: str, prep, when: datetime, client: httpx.Client, cfg: dict) -> tuple[str, int]:
    """Schedule one post with one method. Returns (method used, scheduled messages used).

    The caption is built inside each branch: the gallery caption needs one storage bot link
    per part, the zip caption needs its own link, and every format has its own CTA / footer.
    """
    chat_id = cfg["telegram_chat_id"]
    rules = prep.rules
    enriched = prep.enriched
    cover_item = select_page(enriched, get_rule(rules, "cover", "page"))

    if method == "album":
        caption = prep.make_caption("album")
        ordered = [cover_item] + [it for it in enriched if it is not cover_item]
        blobs: list[bytes] = []
        for it in ordered:
            try:
                blobs.append(fit_telegram(download_image(client, it)))
            except Exception as exc:
                log.warning("Skipping page %s: %s", it.get("id"), exc)
        if not blobs:
            raise RuntimeError("No album images could be downloaded")
        ids = schedule_album(chat_id, blobs, caption, when)
        return "album", len(ids) or len(blobs)

    if method == "telegraph":
        # gallery: every part goes to the storage channel first, the bot answers with a
        # deep link, and only then the cover photo (with all links) is scheduled
        if not userbot_configured():
            raise ConfigError("Userbot secrets missing or invalid (TG_API_ID / TG_API_HASH / TG_SESSION_STRING)")
        if not storage_configured():
            raise ConfigError("STORAGE_CHAT_ID missing or invalid")
        gallery_cover = select_page(
            enriched, get_rule(rules, "delivery", "telegraph", "cover_page", default="first")
        )
        with tempfile.TemporaryDirectory() as tmp:
            links = send_gallery_to_storage(
                client, enriched, Path(tmp), prep.zip_name, part_size=gallery_part_size(rules), rules=rules
            )
            caption = prep.make_caption("telegraph", links)
            blob = download_image(client, gallery_cover)
            if get_rule(rules, "image_edit", "enabled", default=False):
                blob = edit_image(blob, rules)
            blob = fit_telegram(blob)
            ids = schedule_album(chat_id, [blob], caption, when)
        return "telegraph", len(ids) or 1

    if method == "zip":
        if not userbot_configured():
            raise ConfigError("Userbot secrets missing or invalid (TG_API_ID / TG_API_HASH / TG_SESSION_STRING)")
        if not storage_configured():
            raise ConfigError("STORAGE_CHAT_ID missing or invalid")
        with tempfile.TemporaryDirectory() as tmp:
            # everything that can fail without side effects on the channel first
            zip_path = prepare_zip(client, enriched, prep.chosen["id01"], Path(tmp), rules, name_hint=prep.zip_name)
            blob = download_image(client, cover_item)
            if get_rule(rules, "image_edit", "enabled", default=False):
                blob = edit_image(blob, rules)
            blob = fit_telegram(blob)

            # the zip goes to the storage channel now; the bot answers with a deep link
            link = send_zip_to_storage(zip_path).link if zip_path else None
            caption = prep.make_caption("zip", link)

            # only the cover photo (with the link in its caption) is scheduled
            ids = schedule_album(chat_id, [blob], caption, when)
        return ("zip" if zip_path else "photo"), len(ids) or 1

    raise RuntimeError(f"Unknown delivery method {method!r}")


class Planner:
    def __init__(self, cfg, conf, client, token, pools, posted, runtime, dict_rows, content_map, adult_set, drop_set, dry, budget_used):
        self.cfg, self.conf, self.client, self.token = cfg, conf, client, token
        self.pools, self.posted, self.runtime = pools, posted, runtime
        self.dict_rows, self.content_map, self.adult_set, self.drop_set = dict_rows, content_map, adult_set, drop_set
        self.dry = dry
        self.budget_used = budget_used
        self.max_scheduled = int(conf["max_scheduled"])
        self.used_counts: dict[str, int] = {}
        self.recent: list[str] = list(runtime.get("planRecent") or [])
        self.rejected: set[str] = set()
        self.taken: set[str] = set()
        self.rows: list[str] = []
        self.errors: list[Exception] = []
        self.full = False

    def _save(self) -> None:
        if self.dry:
            return
        self.runtime["planRecent"] = self.recent[-RECENT_KEEP:]
        _save_state(self.cfg, self.posted, self.runtime)

    def _schedule(self, prep, slot: Slot) -> tuple[str, int]:
        errors: list[Exception] = []
        for m in prep.chain:
            try:
                return _schedule_one(m, prep, slot.when, self.client, self.cfg)
            except Exception as exc:
                errors.append(exc)
                log.warning("Scheduling %s failed for %s: %s", m, prep.item_id, exc)
                if "SCHEDULE_TOO_MUCH" in str(exc):
                    break
        real = [e for e in errors if not isinstance(e, ConfigError)]
        raise (real[0] if real else errors[0])

    def _record(self, slot: Slot, uid: str, cand: dict, prep, method: str, cost: int) -> None:
        self.used_counts[uid] = self.used_counts.get(uid, 0) + 1
        self.recent.append(uid)
        self.taken.update(cand.get("_merge_ids") or [cand["id"]])
        name = self.pools[uid]["artist"].get("name") or uid
        merged = f" (merged {len(cand['_merge_ids'])})" if cand.get("_merge_ids") else ""
        self.rows.append(
            f"{slot.when:%H:%M}  {method:9} {name} | {cand['id']}{merged} | {len(prep.valid)} pages | {cost} msg"
        )

    def fill(self, slot: Slot) -> bool:
        for slot_type in [slot.type, *self.conf["fallback_types"].get(slot.type, [])]:
            if self._try(slot, slot_type):
                return True
            if self.full:
                return False
        return False

    def _try(self, slot: Slot, slot_type: str) -> bool:
        slot_hhmm = f"{slot.when:%H:%M}"
        for _ in range(MAX_TRIES):
            blocked = self.rejected | self.taken | set(self.posted)
            pick = _next_candidate(self.pools, slot_type, blocked, self.used_counts, self.recent, slot_hhmm)
            if pick is None:
                return False
            uid, cand = pick
            pool = self.pools[uid]
            est = _cost_estimate(cand, slot_type)
            if self.budget_used + est > self.max_scheduled:
                log.info("Skipping %s: %s msg would pass the scheduled limit", cand["id"], est)
                self.rejected.add(cand["id"])
                continue
            nsfw = bool(cand.get("is_nsfw"))
            try:
                prep = prepare_post(
                    self.client, self.token, cand, self.dict_rows, self.content_map, self.adult_set,
                    self.drop_set, self.posted, self.runtime, pool["rules"], nsfw, self.dry,
                )
            except Exception as exc:
                log.warning("Could not prepare %s: %s", cand["id"], exc)
                self.rejected.add(cand["id"])
                continue
            if prep is None:
                self.rejected.add(cand["id"])
                self._save()  # persists the "skipped" mark (blocked content / no valid images)
                continue
            if prep.method != slot_type:
                log.info("Candidate %s resolved to %s but the slot needs %s", cand["id"], prep.method, slot_type)
                self.rejected.add(cand["id"])
                continue

            if self.dry:
                cap = prep.make_caption(prep.method).splitlines()
                log.info(
                    "DRY_RUN plan %s -> %s | %s | zip_name=%s | %s",
                    slot.label(), cand["id"], prep.method, prep.zip_name, " / ".join(cap[:2]),
                )
                self._record(slot, uid, cand, prep, prep.method, est)
                return True

            try:
                used_method, cost = self._schedule(prep, slot)
            except ConfigError:
                raise
            except Exception as exc:
                self.errors.append(exc)
                if "SCHEDULE_TOO_MUCH" in str(exc):
                    log.error("Telegram scheduled-message limit reached; stopping the plan")
                    self.full = True
                    return False
                for mid in cand.get("_merge_ids") or [cand["id"]]:
                    _record_failure(self.posted, self.runtime, mid)
                self.rejected.add(cand["id"])
                self._save()
                continue

            for mid in cand.get("_merge_ids") or [cand["id"]]:
                _mark_done(self.posted, self.runtime, mid, f"scheduled:{used_method}:{slot.when:%Y-%m-%dT%H:%M}")
            _persist_stats(self.cfg, prep.classified, nsfw=nsfw)
            self.budget_used += cost
            self._record(slot, uid, cand, prep, used_method, cost)
            self._save()
            time.sleep(2)  # about one message per second per chat
            return True
        return False


def run_plan(cfg: dict, dry: bool) -> int:
    conf = load_schedule()
    tz = ZoneInfo(conf["timezone"])
    now = datetime.now(tz)
    slots = upcoming_slots(conf, now)
    log.info("Plan at %s (%s): %s slot(s) in the next %sh", now.strftime("%Y-%m-%d %H:%M"), conf["timezone"], len(slots), conf["window_hours"])
    if not slots:
        return 0

    chat_id = cfg["telegram_chat_id"]
    have_userbot = userbot_configured()
    if not dry and not have_userbot:
        raise ConfigError("Userbot secrets missing or invalid (TG_API_ID / TG_API_HASH / TG_SESSION_STRING)")

    existing: list[dict] = []
    if have_userbot:
        try:
            existing = list_scheduled(chat_id)
        except Exception as exc:
            if not dry:
                raise RuntimeError(f"Could not read the scheduled queue: {exc}") from exc
            log.warning("Could not read the scheduled queue (dry run continues): %s", exc)

    tol = timedelta(minutes=int(conf["slot_tolerance_minutes"]))
    open_slots = [s for s in slots if not _occupied(s, existing, tol)]
    log.info("Scheduled queue has %s message(s); open slots: %s", len(existing), [s.label() for s in open_slots])
    if not open_slots:
        log.info("Nothing to plan")
        return 0

    runtime = cfg["state"].runtime() if "state" in cfg and cfg["state"] else load_runtime(cfg["runtime_path"])
    posted = cfg["state"].posted() if "state" in cfg and cfg["state"] else load_posted(cfg["posted_path"])
    dict_rows = load_dictionary_rows(cfg["dictionary_path"])
    content_map = load_content_map(cfg["content_tags_path"])
    adult_set = load_adult_set(cfg["adult_tags_path"])
    drop_set = load_drop_set(cfg["drop_tags_path"])

    with httpx.Client(follow_redirects=True, timeout=20.0) as client:
        token = refresh_access_token(client, cfg["pixiv_refresh_token"])
        pools = build_pools(client, token, cfg["artists"], set(posted), int(conf["pool_pages"]))
        planner = Planner(
            cfg, conf, client, token, pools, posted, runtime, dict_rows, content_map, adult_set, drop_set, dry, len(existing)
        )
        empty: list[str] = []
        for slot in open_slots:
            if planner.full:
                empty.append(slot.label())
                continue
            if not planner.fill(slot):
                empty.append(slot.label())
                log.warning("Slot %s could not be filled", slot.label())

    log.info("Plan summary (%s):\n%s", "dry run" if dry else "scheduled", "\n".join(planner.rows) or "(nothing)")
    if empty:
        log.warning("Empty slots: %s", empty)
    if planner.errors and not planner.rows:
        raise planner.errors[0]
    return 0
