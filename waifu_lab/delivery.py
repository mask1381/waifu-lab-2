from __future__ import annotations

import logging

from waifu_lab.rules import get_rule, zip_allowed

log = logging.getLogger(__name__)

MODES = {"auto", "zip", "album", "telegraph"}
METHODS = ("telegraph", "zip", "album")


def _allowed(rules: dict) -> list[str]:
    allowed = get_rule(rules, "delivery", "allowed", default=list(METHODS)) or list(METHODS)
    return [m for m in allowed if m in METHODS]


def decide_delivery(chosen: dict, page_count: int, nsfw: bool, rules: dict) -> tuple[str, str]:
    """Returns (telegraph | zip | album, short reason for the log)."""
    mode = get_rule(rules, "delivery", "mode", default="auto")
    if mode not in MODES:
        log.warning("Unknown delivery.mode %r, using auto", mode)
        mode = "auto"
    if mode != "auto":
        # a forced mode never overrides the artist's zip rules
        if mode == "zip" and not zip_allowed(rules):
            log.warning("delivery.mode=zip ignored: zip is not allowed for this artist, using auto")
            mode = "auto"
        elif mode == "zip" and not nsfw:
            return "album", "zip is NSFW-only, using album"
        else:
            return mode, f"forced by rules (mode={mode})"

    if not nsfw:
        sfw_min = get_rule(rules, "delivery", "sfw_telegraph_min_pages", default=None)
        n = int(page_count or 1)
        if sfw_min and "telegraph" in _allowed(rules) and n >= int(sfw_min):
            return "telegraph", f"sfw pages {n} >= {sfw_min}"
        return "album", "sfw is always album"

    pages = int(page_count or 1)
    allowed = _allowed(rules)

    # zip forbidden for this artist: Telegraph for big posts, album otherwise
    if not zip_allowed(rules):
        tg_min = int(get_rule(rules, "delivery", "telegraph", "min_pages_over_album", default=11))
        if "telegraph" in allowed and pages >= tg_min:
            return "telegraph", f"nsfw, zip not allowed, pages {pages} >= {tg_min}"
        return "album", f"nsfw, zip not allowed, pages {pages}"

    zip_min = int(get_rule(rules, "delivery", "nsfw_zip_min_pages", default=5))
    pref = "zip" if pages >= zip_min else "telegraph"
    if pref in allowed:
        return pref, f"nsfw pages {pages} ({'>=' if pref == 'zip' else '<'} {zip_min})"
    return ("album" if "album" in allowed else "zip"), f"{pref} not allowed for this artist"


def fallback_chain(method: str, nsfw: bool, rules: dict) -> list[str]:
    """Methods to try, in order, after `method` fails."""
    table = get_rule(rules, "delivery", "fallback", default={}) or {}
    if not isinstance(table, dict):
        return []
    allowed = _allowed(rules)
    out: list[str] = []
    for m in table.get(method) or []:
        if m == "zip" and (not nsfw or not zip_allowed(rules)):
            continue
        if m in allowed and m != method and m not in out:
            out.append(m)
    return out
