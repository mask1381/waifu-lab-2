from __future__ import annotations

import copy
from typing import Any

# Baseline. An artist entry in sources.json overrides only the keys it sets (deep merge).
DEFAULT_RULES: dict[str, Any] = {
    "hashtags": {
        "max_category": 4,
        "format_tag": True,  # #ZIP / #Gallery
        "passthrough": {"sfw": True, "nsfw": True, "max": 2, "max_nsfw": 1},
    },
    "pages": {"drop_last": False, "merge_singles": None},  # merge_singles: {"min":4,"max":10,"max_wait_days":3}
    "schedule": {"preferred_slots": None},                 # ["12:30", "00:30"] = فقط در این اسلات‌ها
    "spoiler": False,  # spoiler on the zip / gallery cover photo
    "delivery": {
        "mode": "auto",  # auto | zip | album | telegraph  (forced for every post)
        "allowed": ["telegraph", "zip", "album"],  # methods the auto logic may pick
        "sfw_telegraph_min_pages": None,  # SFW با این تعداد صفحه یا بیشتر -> telegraph
        "nsfw_zip_min_pages": 5,  # NSFW: pages >= this -> zip, fewer -> telegraph
        "telegraph": {
            # "telegraph" is the gallery method: a cover photo plus one storage-bot link per part.
            # Each part goes to storage as a zip, or as a media group when zip is not allowed.
            "min_pages_over_album": 11,  # only when zip is not allowed: >= this -> gallery, else album
            "cover_page": "first",  # which page is the cover photo (first | last | 1-based int)
            "part_size": 10,  # pages per gallery part / zip (1..10, Telegram media-group limit)
        },
        # what to try when a method fails (zip is never used for SFW)
        "fallback": {"telegraph": ["zip", "album"], "zip": [], "album": []},
        # check that Telegram really built Instant View before sending a Telegraph post
        "verify": {
            "telegraph": {
                "enabled": True,
                "enforce": False,        # False = send the best page even if none passed; True = failed -> next method
                "min_photo_ratio": 0.0,  # raise to e.g. 1.0 once the logs show real numbers
                "retries": 3,
                "wait": 3.0,
            },
        },
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (dicts merge, other values replace)."""
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


def resolve_rules(artist: dict) -> dict:
    """Effective rules = DEFAULT_RULES deep-merged with artist['rules']."""
    return deep_merge(DEFAULT_RULES, artist.get("rules") or {})


def get_rule(rules: dict, *path: str, default: Any = None) -> Any:
    """Safe nested access: get_rule(rules, 'image_edit', 'max_edge', default=None)."""
    cur: Any = rules
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def zip_allowed(rules: dict | None) -> bool:
    """Whether a zip may be built for this artist at all.

    Single source of truth for zip permission: both conditions must hold.
      - "zip" is in delivery.allowed (a missing/empty list means every method is allowed,
        which is how delivery._allowed() reads it)
      - zip.enabled is not False
    """
    rules = rules or {}
    if get_rule(rules, "zip", "enabled", default=True) is False:
        return False
    allowed = get_rule(rules, "delivery", "allowed", default=None)
    if not allowed:
        return True
    if isinstance(allowed, str):
        return allowed == "zip"
    try:
        return "zip" in allowed
    except TypeError:
        return False


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
