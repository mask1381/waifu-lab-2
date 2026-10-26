from __future__ import annotations

from waifu_lab.rules import get_rule, zip_enabled

# Defaults if no law present (kept for backward-compat with tests / manual).
CHANNEL_MAX = 10   # never more than this in the channel
BOT_MIN = 7        # the bot part is worth it only with at least this many pages
SHARE = 1 / 3      # SFW with more than CHANNEL_MAX pages: channel gets about a third
SHARE_MIN = 2
SHARE_MAX = 6


def _law(rules: dict) -> dict:
    return get_rule(rules, "law", default={}) or {}


def zip_after_sfw(rules: dict, n: int) -> bool:
    """True when this SFW post should become a zip (legal zip threshold)."""
    a = int(get_rule(_law(rules), "zip", "sfw_after", default=0) or 0)
    return a > 0 and n >= a


def zip_after_nsfw(rules: dict, n: int) -> bool:
    a = int(get_rule(_law(rules), "zip", "nsfw_after", default=5) or 0)
    return a > 0 and n >= a


def channel_split(n: int, rules: dict | None = None) -> tuple[int, int]:
    """SFW pages -> (in channel, in bot), driven by the channel law (لایحه)."""
    law = _law(rules) if rules else {}
    full = int(get_rule(law, "split", "full_to_channel", default=CHANNEL_MAX) or 0)
    if full <= 0:
        full = CHANNEL_MAX
    if n <= full:
        return n, 0
    div = int(get_rule(law, "split", "share_div", default=0) or 0) or 3
    mn = int(get_rule(law, "split", "min_channel", default=SHARE_MIN) or SHARE_MIN)
    mx = int(get_rule(law, "split", "max_channel", default=SHARE_MAX) or SHARE_MAX)
    ch = max(mn, min(mx, round(n / div)))
    botsafe = max(1, n - BOT_MIN)
    ch = min(ch, botsafe)
    return ch, n - ch


def decide(n: int, nsfw: bool, rules: dict) -> str:
    """'zip' when the (sfw/nsfw) law threshold is reached, or the artist has a zip rule."""
    if nsfw:
        if zip_after_nsfw(rules, n) or zip_enabled(rules):
            return "zip"
        return "post"
    if zip_enabled(rules) or zip_after_sfw(rules, n):
        return "zip"
    return "post"


def nsfw_channel_mode(rules: dict) -> str:
    """cover_only | single_to_channel | all_to_channel — the heart of the NSFW law."""
    return str(get_rule(_law(rules), "nsfw", "channel_mode", default="cover_only") or "cover_only")


def caption_format(method: str, nsfw: bool, n: int, rules: dict) -> str:
    if method == "zip":
        return "zip"
    if nsfw:
        mode = nsfw_channel_mode(rules)
        if mode == "all_to_channel":
            return "album"            # complete NSFW album in the channel
        if mode == "single_to_channel":
            return "nsfw_single"      # one page to channel, rest to bot
        return "bot"                  # cover_only (default)
    if n == 1 and get_rule(rules, "hd_link", default=True):
        return "hd"
    return "bot" if channel_split(n, rules)[1] else "album"
