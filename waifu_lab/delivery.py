from __future__ import annotations

from waifu_lab.rules import get_rule, zip_enabled

CHANNEL_MAX = 10   # never more than this in the channel
BOT_MIN = 7        # the bot part is worth it only with at least this many pages
SHARE = 1 / 3      # SFW with more than CHANNEL_MAX pages: channel gets about a third
SHARE_MIN = 2
SHARE_MAX = 6


def channel_split(n: int) -> tuple[int, int]:
    """SFW pages -> (in channel, in bot)."""
    if n <= CHANNEL_MAX:
        return n, 0
    ch = max(SHARE_MIN, min(SHARE_MAX, round(n * SHARE)))
    ch = min(ch, n - BOT_MIN)
    return ch, n - ch


def decide(n: int, nsfw: bool, rules: dict) -> str:
    """'zip' only when the artist has a zip rule, otherwise always 'post'."""
    return "zip" if zip_enabled(rules) else "post"


def caption_format(method: str, nsfw: bool, n: int, rules: dict) -> str:
    if method == "zip":
        return "zip"
    if nsfw:
        return "bot"
    if n == 1 and get_rule(rules, "hd_link", default=True):
        return "hd"
    return "bot" if channel_split(n)[1] else "album"