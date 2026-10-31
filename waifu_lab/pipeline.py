from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from waifu_lab.blocklist import blocked_reason
from waifu_lab.caption import build_caption, build_cover_caption, caption_parts
from waifu_lab.delivery import caption_format, channel_split, decide
from waifu_lab.enrich import enrich_posts
from waifu_lab.images import has_valid_urls
from waifu_lab.pixiv import adapt_detail_pages, fetch_illust_detail
from waifu_lab.rules import get_rule
from waifu_lab.state import save_posted, save_runtime
from waifu_lab.tags import classify_tags, record_tag_stats
from waifu_lab.title import clean_title
from waifu_lab.userbot_zip import zip_name_base

log = logging.getLogger("waifu_lab")


ENRICH_KEYS = (
    "character",
    "character_tags",
    "franchise_tags",
    "studio_tags",
    "source_tags",
    "general_tags",
    "generalTags",
    "inferred_franchise",
    "inferred_studio",
    "franchiseList",
    "studioList",
    "characterList",
    "all_tags",
)

MAX_FAILURES = 3
DRY_BOT_LINK = "https://t.me/littlemask02_bot?start=wlart_dry"  # placeholder for captions in a dry run


class ConfigError(RuntimeError):
    """Missing secrets / setup problem. Never counts toward MAX_FAILURES."""


def _enrich_album(pages: list[dict], dict_rows: list[dict]) -> list[dict]:
    if not pages:
        return []
    head = enrich_posts([pages[0]], dict_rows)[0]
    out = []
    for page in pages:
        item = {**page}
        for key in ENRICH_KEYS:
            item[key] = head[key]
        out.append(item)
    return out


def _mark_skipped(posted: dict, runtime: dict, item_id: str, reason: str) -> None:
    posted[item_id] = "skipped"
    runtime.get("failures", {}).pop(item_id, None)
    log.warning("Marked %s as skipped: %s", item_id, reason)


def _failure_n(value) -> int:
    """failures[item_id] may be a legacy int or the new {'n': ...} record."""
    if isinstance(value, dict):
        return int(value.get("n") or 0)
    return int(value or 0)


def _record_failure(posted: dict, runtime: dict, item_id: str, reason: str = "delivery error") -> None:
    failures = runtime.setdefault("failures", {})
    n = _failure_n(failures.get(item_id, 0)) + 1
    reason = str(reason or "").strip() or "delivery error"
    failures[item_id] = {
        "n": n,
        "reason": reason[:300],
        "last": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    log.warning("Failure %s/%s for %s: %s", n, MAX_FAILURES, item_id, reason)
    if n >= MAX_FAILURES:
        _mark_skipped(posted, runtime, item_id, f"failed {MAX_FAILURES} times")


def _mark_done(posted: dict, runtime: dict, item_id: str, value: str = "") -> None:
    posted[item_id] = value
    runtime.get("failures", {}).pop(item_id, None)


def _hashtag_opts(rules: dict, nsfw: bool) -> dict:
    """rules['hashtags'] -> classify_tags options (per artist, per path)."""
    h = get_rule(rules, "hashtags", default={}) or {}
    pt = h.get("passthrough") or {}
    keys = ("max_category", "fallback", "max_len", "pass_max_len", "pass_max_words")
    opts = {k: h[k] for k in keys if k in h}
    opts["passthrough"] = bool(pt.get("nsfw" if nsfw else "sfw", True))
    limit = pt.get("max_nsfw") if nsfw and "max_nsfw" in pt else pt.get("max")
    if limit is not None:
        opts["passthrough_max"] = limit
    return opts


def _classify(enriched, dict_rows, content_map, adult_set, drop_set, rules, nsfw):
    """Decide what every Pixiv tag becomes; returns the full classify result."""
    return classify_tags(enriched, dict_rows, content_map, adult_set, drop_set, _hashtag_opts(rules, nsfw))


def _apply_page_rules(pages: list[dict], rules: dict) -> list[dict]:
    """pages.drop_last removes the last page of the post (ads). Single-page posts are untouched."""
    if get_rule(rules, "pages", "drop_last", default=False) and len(pages) > 1:
        return pages[:-1]
    return pages


def _unify_tags(pages: list[dict]) -> None:
    """Merged post: every page carries the union of tags of all source illusts; titles are dropped."""
    tags: list[str] = []
    translations: dict[str, str] = {}
    pixiv_tags: list[dict] = []
    seen: set[str] = set()
    for p in pages:
        for t in p.get("generalTags") or []:
            if t not in tags:
                tags.append(t)
        translations.update(p.get("tagTranslations") or {})
        for pt in p.get("pixivTags") or []:
            if pt["name"] not in seen:
                seen.add(pt["name"])
                pixiv_tags.append(pt)
    raw = list(dict.fromkeys([*tags, *translations.values()]))
    for p in pages:
        p["generalTags"], p["tagsRaw"] = tags, raw
        p["tagTranslations"], p["pixivTags"] = translations, pixiv_tags
        p["title"] = None


# ---- one prepared post (shared by the immediate "post" mode and the nightly "plan" mode) ----

@dataclass
class PreparedPost:
    chosen: dict
    nsfw: bool
    rules: dict
    valid: list
    enriched: list
    classified: dict
    method: str          # zip | post
    parts: dict
    dry: bool = False
    runtime: dict = field(default_factory=dict)

    @property
    def item_id(self) -> str:
        return self.chosen["id"]

    @property
    def zip_name(self) -> str:
        art_title = next((it.get("art_title") for it in self.enriched if it.get("art_title")), None)
        return zip_name_base(self.classified, art_title)

    @property
    def fmt(self) -> str:
        return caption_format(self.method, self.nsfw, len(self.enriched), self.rules)

    def make_caption(self, m: str | None = None, page_url=None, cover=None) -> str:
        """m: zip | album | bot | hd | cover. 'cover' needs the prepared SFW cover post."""
        m = m or self.fmt
        if m == "cover" and cover is None:
            m = "bot"
        if page_url is None and self.dry:
            if m in ("bot", "cover"):
                page_url = [f"{DRY_BOT_LINK}1"]
            elif m in ("zip", "hd"):
                page_url = DRY_BOT_LINK
        if m == "cover":
            return build_cover_caption(cover.parts, cover.classified, self.classified, page_url, self.rules, self.runtime)
        n = len(self.enriched)
        count = (n if self.nsfw else channel_split(n)[1]) if m == "bot" else n
        return build_caption(
            self.parts, m, count, self.classified["hashtags"], self.rules, self.classified, page_url,
            more=(m == "bot" and not self.nsfw),
        )


def prepare_post(
    client, token, chosen, dict_rows, content_map, adult_set, drop_set, posted, runtime, rules, nsfw: bool, dry: bool = False
) -> PreparedPost | None:
    kind = "NSFW" if nsfw else "SFW"
    item_id = chosen["id"]
    merged = bool(chosen.get("_merge"))
    ids = [c["id"] for c in chosen["_merge"]] if merged else [item_id]

    if merged:
        pages = []
        for comp in chosen["_merge"]:
            detail = fetch_illust_detail(client, token, comp["id01"])
            pages += _apply_page_rules(adapt_detail_pages(detail), rules)
        _unify_tags(pages)
    else:
        detail = fetch_illust_detail(client, token, chosen["id01"])
        pages = _apply_page_rules(adapt_detail_pages(detail), rules)

    hit = next((h for h in (blocked_reason(p) for p in pages) if h), None)
    if hit:
        log.warning("%s: %s skipped, blocked content (%s)", kind, item_id, hit)
        if not dry:
            for mid in ids:
                _mark_skipped(posted, runtime, mid, f"blocked content ({hit})")
        return None

    valid = [p for p in pages if has_valid_urls(p)]
    if not valid:
        log.warning("%s: no valid image URLs for %s", kind, item_id)
        if not dry:
            for mid in ids:
                _mark_skipped(posted, runtime, mid, "no valid image URLs")
        return None

    enriched = _enrich_album(valid, dict_rows)
    art_title = None if merged else clean_title(valid[0].get("title"), adult_set)
    for it in enriched:
        it["art_title"] = art_title
    classified = _classify(enriched, dict_rows, content_map, adult_set, drop_set, rules, nsfw=nsfw)
    method = decide(len(valid), nsfw, rules)
    parts = caption_parts(enriched, nsfw, runtime, classified)
    log.info("Prepared %s %s: %s pages, method=%s", kind, item_id, len(valid), method)
    return PreparedPost(chosen, nsfw, rules, valid, enriched, classified, method, parts, dry, runtime)


def _persist_stats(cfg, classified, nsfw: bool) -> None:
    """Count every Pixiv tag of a posted item into data/tag_stats.csv or State sink."""
    if cfg["dry_run"]:
        return
    if "state" in cfg and cfg["state"]:
        cfg["state"].add_stats(classified["decisions"], nsfw)
        return
    if "tag_stats_path" in cfg:
        record_tag_stats(cfg["tag_stats_path"], classified["decisions"], nsfw)


def _save_state(cfg, posted, runtime) -> None:
    """Persist state files or send to State sink; a dry run never writes state."""
    if cfg["dry_run"]:
        log.info("DRY_RUN: state not saved")
        return
    if "state" in cfg and cfg["state"]:
        cfg["state"].save(posted, runtime)
        return
    if posted is not None and "posted_path" in cfg:
        save_posted(cfg["posted_path"], posted)
    if "runtime_path" in cfg:
        save_runtime(cfg["runtime_path"], runtime)