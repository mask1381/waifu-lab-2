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
from waifu_lab.delivery import BOT_MIN, CHANNEL_MAX, channel_split, nsfw_channel_mode, decide
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
from waifu_lab.pixiv import adapt_list_item, fetch_illust_detail, fetch_user_illusts, refresh_access_token
from waifu_lab.rules import deep_merge, get_rule, resolve_rules, select_page, zip_enabled
from waifu_lab.state import load_posted, load_runtime
from waifu_lab.tags import load_adult_set, load_content_map, load_drop_set
from waifu_lab.telegram import split_balanced
from waifu_lab.userbot_schedule import list_scheduled, schedule_album, schedule_text
from waifu_lab.userbot_zip import (
    prepare_zip,
    send_gallery_to_storage,
    send_hd_to_storage,
    send_zip_to_storage,
    storage_configured,
    userbot_configured,
)

log = logging.getLogger("waifu_lab.plan")

MAX_TRIES = 6
RECENT_KEEP = 40
NSFW_MAX_WAIT_DAYS = 3

DEFAULT_SCHEDULE = {
    "timezone": "Asia/Tehran", "lead_minutes": 10, "window_hours": 24, "max_scheduled": 95,
    "pool_pages": 2, "slot_tolerance_minutes": 5,
    "fallback_types": {"zip": ["post"], "post": []},
    "slots": [{"time": f"{h:02d}:00", "type": "post"} for h in range(0, 24, 2)],
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


def _age(it: dict, now: datetime) -> timedelta:
    try:
        d = datetime.fromisoformat(str(it.get("create_date")))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return now - d
    except Exception:
        return timedelta(days=999)


# ---------- pool items ----------

def _cost(it: dict, rules: dict) -> int:
    """Estimated scheduled messages in the channel queue."""
    if it["_method"] == "zip":
        return 1
    if it["_nsfw"]:
        own = get_rule(rules, "nsfw_cover", default="own") == "own" and not it.get("_merge")
        return 1 if own else 6
    return channel_split(it["_pages"], rules)[0]


def _annotate(it: dict, rules: dict) -> dict:
    n = int(it.get("page_count") or 1)
    if get_rule(rules, "pages", "drop_last", default=False) and n > 1:
        n -= 1
    it["_nsfw"] = bool(it.get("is_nsfw"))
    it["_pages"] = n
    it["_method"] = decide(n, it["_nsfw"], rules)
    it["_cost"] = _cost(it, rules)
    return it


def _virtual(chunk: list[dict], rules: dict) -> dict:
    v = dict(chunk[0])
    v["_merge"] = list(chunk)
    v["_merge_ids"] = [c["id"] for c in chunk]
    v["_pages"] = sum(c["_pages"] for c in chunk)
    v["page_count"] = v["_pages"]
    v["total_view"] = sum(_score(c) for c in chunk)
    v["_cost"] = _cost(v, rules)
    return v


def merge_singles(items: list[dict], conf: dict, rules: dict) -> list[dict]:
    """SFW single-page posts -> albums of min..max pages."""
    mn = int(conf.get("min", 4))
    mx = min(int(conf.get("max", 10)), CHANNEL_MAX)
    wait = timedelta(days=float(conf.get("max_wait_days", 3)))
    now = datetime.now(timezone.utc)
    singles = [it for it in items if it["_method"] == "post" and not it["_nsfw"] and it["_pages"] == 1]
    keep = [it for it in items if it not in singles]
    singles.sort(key=_score, reverse=True)
    if len(singles) >= mn:
        keep += [_virtual(c, rules) for c in split_balanced(singles, mx)]
    else:
        keep += [it for it in singles if _age(it, now) >= wait]
    return keep


def merge_small_nsfw(pools: dict[str, dict]) -> None:
    """NSFW posts under BOT_MIN pages are combined (same artist + artists in rules.merge_with)
    until the bundle has >= BOT_MIN pages. Leftovers wait NSFW_MAX_WAIT_DAYS, then go alone."""
    now = datetime.now(timezone.utc)
    taken: set[str] = set()
    for uid, pool in pools.items():
        partners = [uid] + [str(x) for x in get_rule(pool["rules"], "merge_with", default=[]) if str(x) in pools]
        small = [
            it for p in dict.fromkeys(partners) for it in pools[p]["items"]
            if it["_nsfw"] and it["_method"] == "post" and it["_pages"] < BOT_MIN
            and not it.get("_merge") and it["id"] not in taken
        ]
        small.sort(key=_score, reverse=True)
        groups, cur, total = [], [], 0
        for it in small:
            cur.append(it)
            total += it["_pages"]
            if total >= BOT_MIN:
                groups.append(cur)
                cur, total = [], 0
        used = {it["id"] for g in groups for it in g}
        for it in cur:
            if _age(it, now) < timedelta(days=NSFW_MAX_WAIT_DAYS):
                it["_hold"] = True
        taken |= used
        for p in dict.fromkeys(partners):
            pools[p]["items"] = [it for it in pools[p]["items"] if it["id"] not in used]
        pool["items"] += [_virtual(g, pool["rules"]) for g in groups]
        if groups or cur:
            log.info("NSFW merge %s: %s bundle(s), %s waiting", pool["artist"].get("name") or uid, len(groups), len(cur))


def build_pools(client, token, artists, posted_ids: set[str], pages: int, channel_law: dict | None = None) -> dict[str, dict]:
    pools: dict[str, dict] = {}
    for artist in artists:
        uid = str(artist["user_id"])
        rules = resolve_rules(artist, channel_law)
        try:
            illusts = fetch_user_illusts(client, token, uid, posted_ids, max_pages=pages, early_break=False)
        except Exception as exc:
            log.warning("Skipping artist %s: %s", artist.get("name") or uid, exc)
            continue
        items, blocked_n = [], 0
        for raw in illusts:
            if not raw:
                continue
            it = adapt_list_item(raw)
            if not it.get("id") or it["id"] in posted_ids:
                continue
            hit = blocked_reason(it)
            if hit:
                blocked_n += 1
                continue
            items.append(_annotate(it, rules))
        ms = get_rule(rules, "pages", "merge_singles", default=None)
        if ms:
            items = merge_singles(items, ms, rules)
        pools[uid] = {"artist": artist, "rules": rules, "items": items}
        log.info("Pool %s: %s fresh (blocked %s)", artist.get("name") or uid, len(items), blocked_n)
        time.sleep(0.3)
    merge_small_nsfw(pools)
    return pools


def _next_candidate(pools, slot_type: str, blocked: set[str], used_counts: dict, recent: list[str], slot_hhmm: str = ""):
    rows = []
    for uid, pool in pools.items():
        preferred = get_rule(pool["rules"], "schedule", "preferred_slots", default=None) or []
        if preferred and slot_hhmm not in preferred:
            continue
        cands = [
            c for c in pool["items"]
            if c["_method"] == slot_type and c["id"] not in blocked and not c.get("_hold")
        ]
        if not cands:
            continue
        top = max(cands, key=_score)
        last = max((i for i, r in enumerate(recent) if r == uid), default=-1)
        rows.append(((0 if preferred else 1, used_counts.get(uid, 0), last, -_score(top)), uid, top))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    return rows[0][1], rows[0][2]


# ---------- delivery ----------

def _need_storage() -> None:
    if not userbot_configured():
        raise ConfigError("Userbot secrets missing or invalid (TG_API_ID / TG_API_HASH / TG_SESSION_STRING)")
    if not storage_configured():
        raise ConfigError("STORAGE_CHAT_ID missing or invalid")


def _blobs(client, items: list[dict], rules: dict | None = None) -> list[bytes]:
    out = []
    for it in items:
        try:
            out.append(fit_telegram(download_image(client, it)))
        except Exception as exc:
            log.warning("Skipping page %s: %s", it.get("id"), exc)
    if not out:
        raise RuntimeError("No images could be downloaded")
    return out


def _cover_spec(rules: dict):
    spec = get_rule(rules, "cover_page", default="auto")
    return "first" if spec == "auto" else spec


def _own_cover(prep) -> dict:
    spec = get_rule(prep.rules, "cover_page", default="auto")
    pages = prep.enriched
    if spec == "auto":
        return pages[min(1, len(pages) - 1)]
    return select_page(pages, spec)


def _cover_blob(client, item, rules) -> bytes:
    blob = download_image(client, item)
    if get_rule(rules, "image_edit", "enabled", default=False):
        blob = edit_image(blob, rules)
    return fit_telegram(blob)


def deliver_post(prep, when, client, chat_id: str, cover_prep=None) -> int:
    """Send one prepared post. when=None sends now. Returns the number of channel messages."""
    rules, pages, n = prep.rules, prep.enriched, len(prep.enriched)

    if prep.method == "zip":
        _need_storage()
        cover = select_page(pages, _cover_spec(rules))
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = prepare_zip(client, pages, prep.chosen["id01"], Path(tmp), rules, name_hint=prep.zip_name)
            blob = _cover_blob(client, cover, rules)
            link = send_zip_to_storage(zip_path).link if zip_path else None
            ids = schedule_album(chat_id, [blob], prep.make_caption("zip", link), when)
        return len(ids) or 1

    if not prep.nsfw:
        cover = select_page(pages, _cover_spec(rules))
        ordered = [cover] + [it for it in pages if it is not cover]
        ch, bot = channel_split(n, rules)
        if n == 1 and get_rule(rules, "hd_link", default=True):
            _need_storage()
            with tempfile.TemporaryDirectory() as tmp:
                link = send_hd_to_storage(client, pages[0], Path(tmp), prep.zip_name)
            blob = _cover_blob(client, pages[0], rules)
            return len(schedule_album(chat_id, [blob], prep.make_caption("hd", link), when)) or 1
        if not bot:
            blobs = _blobs(client, ordered)
            return len(schedule_album(chat_id, blobs, prep.make_caption("album"), when)) or len(blobs)
        _need_storage()
        with tempfile.TemporaryDirectory() as tmp:
            links = send_gallery_to_storage(client, ordered[ch:], Path(tmp), prep.zip_name, rules=rules)
            blobs = _blobs(client, ordered[:ch])
            ids = schedule_album(chat_id, blobs, prep.make_caption("bot", links), when)
        return len(ids) or len(blobs)

    # NSFW: the channel law decides the channel mode:
    #   cover_only        -> everything to bot, channel gets a cover / SFW album / text (default)
    #   single_to_channel -> single_in_channel page(s) to channel, rest to bot (cover only, full album in bot)
    #   all_to_channel    -> the whole NSFW album goes to the channel
    mode = nsfw_channel_mode(rules)
    if mode == "all_to_channel":
        return len(_blobs(client, pages)) and len(
            schedule_album(chat_id, _blobs(client, pages), prep.make_caption("album"), when)
        ) or len(pages)

    _need_storage()
    with tempfile.TemporaryDirectory() as tmp:
        links = send_gallery_to_storage(client, pages, Path(tmp), prep.zip_name, rules=rules)

    if mode == "single_to_channel":
        single = int(get_rule(rules, "law", "nsfw", "single_in_channel", default=1) or 1)
        single = max(1, min(single, n))
        _need_storage()
        with tempfile.TemporaryDirectory() as tmp:
            sub_links = send_gallery_to_storage(client, pages[:single], Path(tmp), prep.zip_name + "-c", rules=rules)
        return len(schedule_album(chat_id, _blobs(client, pages[:single]), prep.make_caption("bot", sub_links), when)) or single

    if cover_prep is not None:
        caption = prep.make_caption("cover", links, cover=cover_prep)
        blobs = _blobs(client, cover_prep.enriched[:CHANNEL_MAX])
        return len(schedule_album(chat_id, blobs, caption, when)) or len(blobs)
    caption = prep.make_caption("bot", links)
    if get_rule(rules, "nsfw_cover", default="own") == "own" and not prep.chosen.get("_merge"):
        blob = _cover_blob(client, _own_cover(prep), rules)
        return len(schedule_album(chat_id, [blob], caption, when)) or 1
    log.warning("No SFW cover for %s: sending a text post", prep.item_id)
    return len(schedule_text(chat_id, caption, when)) or 1


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

    def _prep(self, cand, nsfw, rules):
        return prepare_post(
            self.client, self.token, cand, self.dict_rows, self.content_map, self.adult_set,
            self.drop_set, self.posted, self.runtime, rules, nsfw, self.dry,
        )

    def _find_cover(self, uid: str):
        """SFW cover album for an NSFW post: same artist first, then rules.sfw_fallback artists."""
        order = [uid] + [str(x) for x in get_rule(self.pools[uid]["rules"], "sfw_fallback", default=[])]
        blocked = self.rejected | self.taken | set(self.posted)
        for u in dict.fromkeys(order):
            pool = self.pools.get(u)
            if not pool:
                continue
            cands = [
                c for c in pool["items"]
                if not c["_nsfw"] and c["_method"] == "post" and not c.get("_merge")
                and not c.get("_hold") and c["id"] not in blocked
            ]
            for c in sorted(cands, key=_score, reverse=True)[:3]:
                try:
                    p = self._prep(c, False, pool["rules"])
                except Exception as exc:
                    log.warning("SFW cover %s failed: %s", c["id"], exc)
                    self.rejected.add(c["id"])
                    continue
                if p is None:
                    self.rejected.add(c["id"])
                    continue
                return c, p
        return None, None

    def deliver(self, uid: str, cand: dict, when, label: str) -> bool | None:
        """True = delivered/dry-ok, None = skipped (nothing to send). Raises on failure."""
        pool = self.pools[uid]
        rules, nsfw = pool["rules"], cand["_nsfw"]
        prep = self._prep(cand, nsfw, rules)
        if prep is None:
            return None
        cover_c = cover_p = None
        if nsfw and prep.method != "zip" and (
            cand.get("_merge") or get_rule(rules, "nsfw_cover", default="own") == "sfw"
        ):
            cover_c, cover_p = self._find_cover(uid)
        ids = cand.get("_merge_ids") or [cand["id"]]
        name = pool["artist"].get("name") or uid

        if self.dry:
            cap = (prep.make_caption("cover", cover=cover_p) if cover_p else prep.make_caption()).splitlines()
            log.info("DRY %s -> %s | %s %s | %s pages | cover=%s | %s", label, ids, "NSFW" if nsfw else "SFW",
                     prep.method, len(prep.enriched), cover_c["id"] if cover_c else "own/text", " / ".join(cap[:3]))
            self.rows.append(f"{label} {prep.method} {name} | {ids} | {len(prep.enriched)} pages")
            return True

        cost = deliver_post(prep, when, self.client, self.cfg["telegram_chat_id"], cover_p)
        stamp = f"scheduled:{prep.method}:{when:%Y-%m-%dT%H:%M}" if when else f"sent:{prep.method}"
        for mid in ids:
            _mark_done(self.posted, self.runtime, mid, stamp)
        if cover_c:
            for mid in cover_c.get("_merge_ids") or [cover_c["id"]]:
                _mark_done(self.posted, self.runtime, mid, f"cover_of:{ids[0]}")
            self.taken.add(cover_c["id"])
        _persist_stats(self.cfg, prep.classified, nsfw=nsfw)
        self.budget_used += cost
        self.rows.append(f"{label} {prep.method} {name} | {ids} | {len(prep.enriched)} pages | {cost} msg")
        return True

    def fill(self, slot: Slot) -> bool:
        for slot_type in [slot.type, *self.conf["fallback_types"].get(slot.type, [])]:
            if self._try(slot, slot_type):
                return True
            if self.full:
                return False
        return False

    def _try(self, slot: Slot, slot_type: str) -> bool:
        hhmm = f"{slot.when:%H:%M}"
        for _ in range(MAX_TRIES):
            blocked = self.rejected | self.taken | set(self.posted)
            pick = _next_candidate(self.pools, slot_type, blocked, self.used_counts, self.recent, hhmm)
            if pick is None:
                return False
            uid, cand = pick
            if self.budget_used + cand["_cost"] > self.max_scheduled:
                log.info("Skipping %s: would pass the scheduled limit", cand["id"])
                self.rejected.add(cand["id"])
                continue
            ids = cand.get("_merge_ids") or [cand["id"]]
            try:
                res = self.deliver(uid, cand, slot.when, slot.label())
            except ConfigError:
                raise
            except Exception as exc:
                self.errors.append(exc)
                log.warning("Delivery failed for %s: %s", cand["id"], exc)
                if "SCHEDULE_TOO_MUCH" in str(exc):
                    self.full = True
                    return False
                for mid in ids:
                    _record_failure(self.posted, self.runtime, mid)
                self.rejected.add(cand["id"])
                self._save()
                continue
            if res is None:
                self.rejected.add(cand["id"])
                self._save()
                continue
            self.taken.add(cand["id"])
            self.used_counts[uid] = self.used_counts.get(uid, 0) + 1
            self.recent.append(uid)
            self._save()
            if not self.dry:
                time.sleep(2)
            return True
        return False


def _state(cfg):
    st = cfg.get("state")
    if st:
        return st.runtime(), st.posted()
    runtime = (
        load_runtime(cfg["runtime_path"])
        if "runtime_path" in cfg
        else {"recentlyUsed": [], "recentCaptions": {}, "failures": {}}
    )
    posted = load_posted(cfg["posted_path"]) if "posted_path" in cfg else {}
    return runtime, posted


def _data(cfg):
    return (
        load_dictionary_rows(cfg["dictionary_path"]),
        load_content_map(cfg["content_tags_path"]),
        load_adult_set(cfg["adult_tags_path"]),
        load_drop_set(cfg["drop_tags_path"]),
    )


def run_plan(cfg: dict, dry: bool) -> int:
    conf = load_schedule()
    tz = ZoneInfo(conf["timezone"])
    now = datetime.now(tz)
    slots = upcoming_slots(conf, now)
    log.info("Plan at %s: %s slot(s)", now.strftime("%Y-%m-%d %H:%M"), len(slots))
    if not slots:
        return 0
    chat_id = cfg["telegram_chat_id"]
    have_userbot = userbot_configured()
    if not dry and not have_userbot:
        raise ConfigError("Userbot secrets missing or invalid")

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
    log.info("Queue %s message(s); open slots: %s", len(existing), [s.label() for s in open_slots])
    if not open_slots:
        return 0

    runtime, posted = _state(cfg)
    dict_rows, content_map, adult_set, drop_set = _data(cfg)

    with httpx.Client(follow_redirects=True, timeout=20.0) as client:
        token = refresh_access_token(client, cfg["pixiv_refresh_token"])
        pools = build_pools(client, token, cfg["artists"], set(posted), int(conf["pool_pages"]), cfg.get("channel_law"))
        planner = Planner(cfg, conf, client, token, pools, posted, runtime, dict_rows, content_map, adult_set, drop_set, dry, len(existing))
        empty = []
        for slot in open_slots:
            if planner.full or not planner.fill(slot):
                empty.append(slot.label())
    log.info("Plan summary:\n%s", "\n".join(planner.rows) or "(nothing)")
    if empty:
        log.warning("Empty slots: %s", empty)
    if planner.errors and not planner.rows:
        raise planner.errors[0]
    return 0


# ---------- manual post by illust id ----------

def _overrides(ov: dict) -> dict:
    """One-time choices from the bot wizard -> rules patch."""
    out: dict = {}
    if ov.get("nsfw_cover") in ("own", "sfw"):
        out["nsfw_cover"] = ov["nsfw_cover"]
    if "zip" in ov:
        out["zip"] = {"pages": "all"} if ov["zip"] else None
    if "document" in ov:
        out["document"] = bool(ov["document"])
    if "hd_link" in ov:
        out["hd_link"] = bool(ov["hd_link"])
    return out


def run_manual(cfg: dict, batch: dict) -> int:
    dry = cfg["dry_run"]
    iid = str(batch.get("illust_id") or "").strip()
    if not iid.isdigit():
        raise RuntimeError("manual batch needs a numeric illust_id")
    when = None
    if batch.get("when"):
        when = datetime.fromisoformat(str(batch["when"]).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    conf = load_schedule()
    runtime, posted = _state(cfg)
    dict_rows, content_map, adult_set, drop_set = _data(cfg)

    with httpx.Client(follow_redirects=True, timeout=20.0) as client:
        token = refresh_access_token(client, cfg["pixiv_refresh_token"])
        detail = fetch_illust_detail(client, token, iid)
        uid = str((detail.get("user") or {}).get("id") or "")
        base = next((a for a in cfg["artists"] if str(a["user_id"]) == uid),
                    {"user_id": uid, "name": (detail.get("user") or {}).get("name", ""), "rules": {}})
        artist = {**base, "rules": deep_merge(base.get("rules") or {}, _overrides(batch.get("overrides") or {}))}
        rules = resolve_rules(artist, cfg.get("channel_law"))

        item = _annotate(adapt_list_item(detail), rules)
        pools = {uid: {"artist": artist, "rules": rules, "items": []}}
        if item["_nsfw"] and item["_method"] == "post" and get_rule(rules, "nsfw_cover", default="own") == "sfw":
            wanted = [uid] + [str(x) for x in get_rule(rules, "sfw_fallback", default=[])]
            arts = [artist] + [a for a in cfg["artists"] if str(a["user_id"]) in wanted and str(a["user_id"]) != uid]
            pools = build_pools(client, token, arts, set(posted) | {item["id"]}, int(conf["pool_pages"]), cfg.get("channel_law"))
            pools.setdefault(uid, {"artist": artist, "rules": rules, "items": []})
        pools[uid]["rules"] = rules

        planner = Planner(cfg, conf, client, token, pools, posted, runtime, dict_rows, content_map, adult_set, drop_set, dry, 0)
        pools[uid]["items"] = [it for it in pools[uid]["items"] if it["id"] != item["id"]] + [item]
        try:
            res = planner.deliver(uid, item, when, f"manual {iid}")
        except Exception:
            if not dry:
                _record_failure(posted, runtime, item["id"])
                planner._save()
            raise
        if res is None:
            log.warning("Manual post %s was skipped (blocked content or no valid images)", iid)
        planner._save()
    log.info("Manual summary: %s", planner.rows or "(nothing)")
    return 0