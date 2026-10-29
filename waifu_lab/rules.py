from __future__ import annotations

import copy
from typing import Any

# Baseline. An artist entry in sources.json overrides only the keys it sets (deep merge).
DEFAULT_RULES: dict[str, Any] = {
    "nsfw_cover": "own",        # own | sfw
    "cover_page": "auto",       # auto | first | last | int
    "merge_with": [],           # user_id هنرمندهایی که NSFW کم‌صفحه‌شان با هم ترکیب می‌شود
    "sfw_fallback": [],         # user_id هنرمندهایی که اگر SFW نداشت از آن‌ها کاور SFW می‌آید
    "zip": None,                # {"pages": "all"} = zip فعال
    "document": False,          # True = فایل‌های مخزن به صورت سند اصلی
    "hd_link": True,            # لینک HD برای SFW تک‌عکس
    "pages": {"drop_last": False, "merge_singles": None},
    "schedule": {"preferred_slots": None},
    "spoiler": False,
    "hashtags": {
        "max_category": 4,
        "format_tag": True,
        "passthrough": {"sfw": True, "nsfw": True, "max": 2, "max_nsfw": 1},
    },
    "law": {   # لایحه‌ی کانال — استثنای هنرمند (rules.law) همین keys را override می‌کند
        "split": {"full_to_channel": 10, "share_div": 3, "min_channel": 2, "max_channel": 6},
        "zip": {"sfw_after": 0, "nsfw_after": 5},
        "nsfw": {"channel_mode": "cover_only", "single_in_channel": 1, "cover": "sfw_or_text", "spoiler": True},
        "single": {"sfw_hd": True, "nsfw_hd": True},
        "merge": {"min": 4, "max": 10, "max_wait_days": 3},
    },
}
ALLOWED_KEYS = set(DEFAULT_RULES) | {"caption", "image_edit", "law_exception", "law"}

def _artist_law_override(artist_rules: dict) -> dict:
    """۱.۱: استثنای هنرمند = rules.law_exception (legacy: rules.law) — صرف‌اً استثنا، نه لایحه‌ی کانال."""
    exc = artist_rules.get("law_exception", artist_rules.get("law")) or {}
    return copy.deepcopy(exc) if isinstance(exc, dict) else {}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (dicts merge, other values replace)."""
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def resolve_rules(artist: dict, channel_law: dict | None = None) -> dict:
    """Unknown/legacy keys (delivery.*, zip.enabled ...) are dropped on purpose.
    channel_law (لایحه‌ی کانال از بات) به‌عنوان پایه‌ی rules.law می‌نشیند و
    استثنای هنرمند (۱.۱: rules.law_exception، legacy: rules.law) روی آن اعمال می‌شود —
    پس در خروجی `rules["law"]` همیشه قانون نهایی است.

    DEFAULT_RULES is never mutated: each call starts from a fresh copy so successive
    artists / batches cannot leak a previous channel_law into the next one.
    """
    own = {k: v for k, v in (artist.get("rules") or {}).items() if k in ALLOWED_KEYS}
    exc = _artist_law_override(own)
    base = deep_merge(DEFAULT_RULES, {})
    if channel_law:
        base["law"] = deep_merge(base["law"], copy.deepcopy(channel_law))
    rules = deep_merge(base, own)
    if "law_exception" in rules:
        del rules["law_exception"]
    if exc:
        rules["law"] = deep_merge(rules["law"], exc)
    return rules


def zip_enabled(rules: dict | None) -> bool:
    return isinstance((rules or {}).get("zip"), dict)


zip_allowed = zip_enabled  # alias for old imports


def get_rule(rules: dict, *path: str, default: Any = None) -> Any:
    """Safe nested access: get_rule(rules, 'image_edit', 'max_edge', default=None)."""
    cur: Any = rules
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _page_index(spec: Any, n: int) -> int | None:
    """Resolve a page spec ('first' | 'last' | 1-based int) to a 0-based index."""
    if n <= 0:
        return None
    if spec == "last":
        return n - 1
    if spec in ("first", None):
        return 0
    if isinstance(spec, int):
        idx = spec - 1
        return idx if 0 <= idx < n else None
    if isinstance(spec, str) and str(spec).strip().isdigit():
        idx = int(spec) - 1
        return idx if 0 <= idx < n else None
    return 0


def select_page(items: list, spec: Any) -> Any:
    """Pick one item by page spec. Falls back to the first item."""
    if not items:
        return None
    idx = _page_index(spec, len(items))
    if idx is None:
        idx = 0
    return items[idx]


def select_pages(items: list, spec: Any) -> list:
    """Select/order pages for the zip: 'all' keeps everything; an int list picks those pages."""
    if not items:
        return []
    if spec == "all" or spec is None:
        return list(items)
    if isinstance(spec, int):
        spec = [spec]
    if isinstance(spec, (list, tuple)):
        out = []
        for s in spec:
            idx = _page_index(s, len(items))
            if idx is not None and items[idx] not in out:
                out.append(items[idx])
        return out
    return list(items)