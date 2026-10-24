from __future__ import annotations

"""One caption builder for every delivery format.

Skeleton:

    <b>title</b>
    <i>teaser</i>

    <blockquote>bot link line(s)</blockquote>   (zip / gallery only)

    #hashtags

    footer (full 4-line THE LABS footer for every format)

Album has no CTA and no blockquote. Gallery parts are separated by a blank line.
Every CTA line is one big hyperlink (emoji + text), so it is easy to tap.

Title and teaser are picked once per post (caption_parts) and reused if delivery
falls back to another format, so the caption never changes its wording mid-post.

"bot" is the gallery format: the post is a cover photo and the links are
one "Get the album" line per storage album (bot deep links). "hd" is a single
original-quality file.
"""

import html
import logging
import re

from waifu_lab.caption_nsfw import nsfw_parts
from waifu_lab.caption_sfw import sfw_parts
from waifu_lab.rules import get_rule
from waifu_lab.tags import BRAND_TAG, FORMAT_TAGS

log = logging.getLogger(__name__)

FORMATS = ("zip", "album", "bot", "hd")
CAPTION_LIMIT = 1024  # Telegram limit for photo / album captions (visible characters)

WAIFU_LAB_URL = "https://t.me/+Nt8As_Mw1l0yMGY0"
FULL_FOOTER = "\n".join(
    [
        "𝗧𝗛𝗘 𝗟𝗔𝗕𝗦 ▸",
        '├─ 🎴 <a href="https://t.me/+2Sf09aP2q903YmI8">𝑨𝒏𝒊𝑾𝒂𝒍𝒍 𝑳𝒂𝒃</a>',
        f'├─ 🪽 <a href="{WAIFU_LAB_URL}">𝑾𝒂𝒊𝒇𝒖 𝑳𝒂𝒃</a>',
        '└─ 🌌 <a href="https://t.me/+98kf5lpHI0gzMzNk">𝑨𝒏𝒊𝑵𝒆𝒙𝒖𝒔 𝑳𝒂𝒃</a>',
    ]
)

TAG_RE = re.compile(r"<[^>]+>")


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def _visible_len(text: str) -> int:
    """Length Telegram counts: the text after HTML tags are parsed away."""
    return len(html.unescape(TAG_RE.sub("", text)))


def _link(url: str, text: str) -> str:
    """The whole line is the hyperlink."""
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def caption_parts(items: list[dict], nsfw: bool, runtime: dict, info: dict | None = None) -> dict:
    """Pick title + teaser once per post."""
    if nsfw:
        title, teaser = nsfw_parts(items, info)
    else:
        title, teaser = sfw_parts(items, runtime, info)
    return {"title": title, "teaser": teaser}


def _gallery_cta(count: int, links: list[str]) -> str:
    n = len(links)
    if n > 1:
        # one line per part, a blank line between lines
        return "\n\n".join(
            _link(link, f"📖 Get the album, part {i} of {n}")
            for i, link in enumerate(links, start=1)
        )
    pics = f"{count} pic{'s' if count != 1 else ''}"
    if n == 1:
        return _link(links[0], f"📖 {pics} · Get the album")
    return f"📖 {pics}"


def _cta(fmt: str, count: int, page_url: str | list[str] | None) -> str:
    """Text for the blockquote. Empty for album (no blockquote at all)."""
    if fmt == "zip":
        # page_url = the storage bot's deep link for this zip
        if isinstance(page_url, str) and page_url:
            return _link(page_url, "📦 Full quality ZIP 👉 Tap to get")
        return "📦 Full quality ZIP below 👇"
    if fmt == "hd":
        if isinstance(page_url, str) and page_url:
            return _link(page_url, "🖼 Original quality 👉 Tap to get")
        return "🖼 Original quality below 👇"
    if fmt == "bot":
        # page_url = list of storage bot deep links, one per album part
        if isinstance(page_url, str):
            links = [page_url]
        else:
            links = list(page_url or [])
        return _gallery_cta(count, [x for x in links if x])
    return ""  # album


def _footer(fmt: str, rules: dict | None) -> str:
    if get_rule(rules or {}, "caption", "footer", default=True) is False:
        return ""
    return FULL_FOOTER


def _assemble(parts: dict, cta: str, tags: list[str], footer: str) -> str:
    head = [f"<b>{_esc(parts.get('title'))}</b>"]
    if parts.get("teaser"):
        head.append(f"<i>{_esc(parts['teaser'])}</i>")

    blocks = ["\n".join(head)]
    if cta:
        blocks.append(f"<blockquote>{cta}</blockquote>")
    if tags:
        blocks.append(" ".join(tags))
    if footer:
        blocks.append(footer)
    return "\n\n".join(blocks)


def build_caption(
    parts: dict,
    fmt: str,
    count: int,
    hashtags: list[str] | None,
    rules: dict | None = None,
    info: dict | None = None,
    page_url: str | list[str] | None = None,
) -> str:
    """Caption for one delivery format (zip | album | bot | hd).

    count    - number of pages in the post
    hashtags - classify_tags()["hashtags"] (franchise, characters, category, brand)
    info     - classify_tags() result; used to protect important hashtags when trimming
    page_url - zip / hd: storage bot deep link (str); bot: list of storage bot deep links
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unknown caption format {fmt!r}")

    cta = _cta(fmt, count, page_url)
    footer = _footer(fmt, rules)
    tags = [t for t in (hashtags or []) if t]
    fmt_tag = FORMAT_TAGS.get(fmt)
    if fmt_tag and get_rule(rules or {}, "hashtags", "format_tag", default=True) is not False:
        if fmt_tag.lower() not in {t.lower() for t in tags}:
            tags.insert(0, fmt_tag)
    text = _assemble(parts, cta, tags, footer)

    # photo / album captions are limited to 1024 visible characters:
    # drop the least important hashtags first (category / pass-through), then the teaser
    protected = {BRAND_TAG.lower(), *(h.lower() for h in FORMAT_TAGS.values())}
    if info:
        protected |= {h.lower() for h in info.get("character_hashtags") or []}
        if info.get("franchise_hashtag"):
            protected.add(info["franchise_hashtag"].lower())

    while _visible_len(text) > CAPTION_LIMIT:
        idx = next((i for i in range(len(tags) - 1, -1, -1) if tags[i].lower() not in protected), None)
        if idx is None:
            break
        log.warning("Caption too long (%s), dropping hashtag %s", _visible_len(text), tags[idx])
        tags.pop(idx)
        text = _assemble(parts, cta, tags, footer)

    if _visible_len(text) > CAPTION_LIMIT and parts.get("teaser"):
        log.warning("Caption still too long (%s), dropping teaser", _visible_len(text))
        text = _assemble({**parts, "teaser": ""}, cta, tags, footer)

    if _visible_len(text) > CAPTION_LIMIT:
        log.warning("Caption is still %s chars after trimming", _visible_len(text))
    return text