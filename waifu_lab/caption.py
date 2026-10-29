from __future__ import annotations

"""One caption builder for every delivery format.

    <b>title</b>
    <i>teaser / hook</i>

    <blockquote>bot link line(s)</blockquote>   (zip / bot / hd / cover)

    #hashtags

    footer

Formats: zip | album | bot | hd | cover
  bot   : SFW post with the rest in the bot, or NSFW with its own cover / text only
  cover : NSFW bundle whose channel image is an SFW album of ANOTHER post. Title and hashtags
          come from the SFW post, the hook sentence depends on how the two posts relate
          (same character / same franchise / unrelated), so it never lies.
"""

import html
import logging
import re

from waifu_lab.caption_nsfw import nsfw_parts
from waifu_lab.caption_sfw import _pick_fresh, sfw_parts
from waifu_lab.names import relation
from waifu_lab.rules import get_rule
from waifu_lab.tags import BRAND_TAG, FORMAT_TAGS

log = logging.getLogger(__name__)

FORMATS = ("zip", "album", "bot", "hd")
CAPTION_LIMIT = 1024

_DYNAMIC_FOOTER = ""


def set_footer(footer: str) -> None:
    """Set dynamic footer received from batch/bot."""
    global _DYNAMIC_FOOTER
    _DYNAMIC_FOOTER = (footer or "").strip()


TAG_RE = re.compile(r"<[^>]+>")

# ---------- hook sentences for "cover" posts (chosen by the relation of the two posts) ----------
HOOK_CHAR = [  # {n} = the shared character
    "Another side of {n} 😏", "{n} has a secret side 🤫💕", "You haven't seen all of {n} yet 👀",
    "More {n} is waiting for you 😳", "{n} has more to show 🔥", "This is not all of {n} 😈",
    "Want more {n}? Check the bot 💋", "The best {n} pics are in the bot 🔥",
    "There is a hotter {n} behind the link 😳", "{n} is waiting in the bot 💕",
    "See {n} like never before 🙈🔥", "{n} saved the best for the bot 😏",
    "Ready for more {n}? 😍", "{n} has a bold side too 😈", "Tap to meet the other {n} 🤭",
]
HOOK_FRANCHISE = [  # {f} = the shared franchise
    "More {f} girls are waiting 😳", "The rest of {f} is in the bot 🔥", "Love {f}? There is more 😏",
    "{f} has a bolder side 🙈", "More {f} waifus inside the bot 💋", "Not all {f} girls are this shy 😈",
    "Want more {f}? Tap below 👇🔥", "Another {f} girl is waiting 😍", "{f} fans, this one is for you 😳",
    "The bot has more {f} 💕", "Look who else is from {f} 👀", "{f} gets a little hotter in the bot 🔥",
    "More {f} art, just one tap away 🤭", "Your {f} collection is not done yet 😏",
    "Come see the other {f} girls 💖",
]
HOOK_NONE = [  # no name at all: safe when the two posts have nothing in common
    "Want more like this? 😈 Check the bot", "Something hotter is waiting for you 🔥",
    "Tap for more waifus 💋", "Your next waifu is one tap away 😳", "There is a bolder pack in the bot 🙈",
    "More waifus are hiding in the bot 👀", "Feeling curious? Tap below 😏", "The good stuff is in the bot 🔥",
    "Ready for something more? 😈", "Your next favorite is waiting 💕", "This was only the warm up 😳",
    "A secret pack is waiting for you 🤫", "Open the bot for a surprise 🎁🔥", "Not shy anymore, tap below 🙈💋",
    "Bored? Here is something spicy 🌶️😏", "Hot picks are one tap away 🔥", "More waifus, more fun 😍",
    "Don't miss the rest of the pack 👀", "The best pics are not here 😈", "A little more? Just tap 💖",
]
BTN_CHAR = [
    "🔥 More {n} · Tap to open", "💋 {n} is waiting · Tap to get", "😈 Secret {n} pics · Tap here",
    "🙈 See more {n} · Tap to see", "😳 {n} unlocked · Tap to open", "💖 The full {n} set · Tap to get",
    "🤫 Hidden {n} pics · Tap to open", "👀 More of {n} · Tap here",
]
BTN_FRANCHISE = [
    "🔥 More {f} · Tap to open", "💋 More {f} girls · Tap to get", "😈 Secret {f} pics · Tap here",
    "🙈 See more {f} · Tap to see", "😳 {f} unlocked · Tap to open", "💖 The {f} pack · Tap to get",
    "🤫 Hidden {f} pics · Tap to open", "👀 More of {f} · Tap here",
]
BTN_NONE = [
    "🔥 More waifus · Tap to open", "💋 The full pack · Tap to get", "😈 Secret pics · Tap here",
    "🙈 See more · Tap to see", "😳 Unlock the pack · Tap to open", "💖 More for you · Tap to get",
    "🤫 Hidden pics · Tap to open", "👀 Take a look · Tap here",
]
HOOKS = {"char": (HOOK_CHAR, BTN_CHAR), "franchise": (HOOK_FRANCHISE, BTN_FRANCHISE), "none": (HOOK_NONE, BTN_NONE)}


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def _visible_len(text: str) -> int:
    return len(html.unescape(TAG_RE.sub("", text)))


def _link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>'


def caption_parts(items: list[dict], nsfw: bool, runtime: dict, info: dict | None = None) -> dict:
    """Pick title + teaser once per post."""
    if nsfw:
        title, teaser = nsfw_parts(items, info)
    else:
        title, teaser = sfw_parts(items, runtime, info)
    return {"title": title, "teaser": teaser}


def _gallery_cta(count: int, links: list[str], more: bool) -> str:
    n = len(links)
    word = f"{count} more pics" if more else f"{count} pics"
    if n > 1:
        return "\n\n".join(_link(link, f"📖 {word} · Get the album, part {i} of {n}") for i, link in enumerate(links, start=1))
    if n == 1:
        return _link(links[0], f"📖 {word} · Get the album")
    return f"📖 {word}"


def _cta(fmt: str, count: int, page_url, more: bool = False) -> str:
    if fmt == "zip":
        if isinstance(page_url, str) and page_url:
            return _link(page_url, "📦 Full quality ZIP 👉 Tap to get")
        return "📦 Full quality ZIP below 👇"
    if fmt == "hd":
        if isinstance(page_url, str) and page_url:
            return _link(page_url, "🖼 Original quality 👉 Tap to get")
        return "🖼 Original quality below 👇"
    if fmt == "bot":
        links = [page_url] if isinstance(page_url, str) else list(page_url or [])
        return _gallery_cta(count, [x for x in links if x], more)
    return ""


def _footer(rules: dict | None) -> str:
    return "" if get_rule(rules or {}, "caption", "footer", default=True) is False else _DYNAMIC_FOOTER


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


def _fit(parts: dict, cta: str, tags: list[str], footer: str, info: dict | None) -> str:
    """Trim to 1024 visible chars: least important hashtags first, then the teaser."""
    protected = {BRAND_TAG.lower(), *(h.lower() for h in FORMAT_TAGS.values())}
    if info:
        protected |= {h.lower() for h in info.get("character_hashtags") or []}
        if info.get("franchise_hashtag"):
            protected.add(info["franchise_hashtag"].lower())
    text = _assemble(parts, cta, tags, footer)
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


def _with_format_tag(hashtags, fmt: str, rules) -> list[str]:
    tags = [t for t in (hashtags or []) if t]
    ft = FORMAT_TAGS.get(fmt)
    if ft and get_rule(rules or {}, "hashtags", "format_tag", default=True) is not False:
        if ft.lower() not in {t.lower() for t in tags}:
            tags.insert(0, ft)
    return tags


def build_caption(
    parts: dict,
    fmt: str,
    count: int,
    hashtags: list[str] | None,
    rules: dict | None = None,
    info: dict | None = None,
    page_url=None,
    more: bool = False,
) -> str:
    """count: pages shown in the link line (for 'bot' = pages that are inside the bot).
    more: True for SFW posts ('N more pics'), False for NSFW ('N pics')."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown caption format {fmt!r}")
    return _fit(parts, _cta(fmt, count, page_url, more), _with_format_tag(hashtags, fmt, rules), _footer(rules), info)


def build_cover_caption(
    cover_parts: dict, cover_info: dict, nsfw_info: dict, links, rules: dict | None, runtime: dict
) -> str:
    """Channel image = SFW album of another post, bot link = the NSFW bundle.
    Title + hashtags belong to the SFW post (they match the image); the hook and the button
    text follow the relation of the two posts, so they never claim something untrue."""
    rel, name = relation(cover_info, nsfw_info)
    hooks, btns = HOOKS[rel]
    mem = runtime.setdefault("recentCaptions", {})
    hook = _pick_fresh([h.format(n=name, f=name) for h in hooks], f"h:{rel}", mem)
    btn = _pick_fresh([b.format(n=name, f=name) for b in btns], f"b:{rel}", mem)

    links = [x for x in ([links] if isinstance(links, str) else list(links or [])) if x]
    if not links:
        cta = btn
    elif len(links) == 1:
        cta = _link(links[0], btn)
    else:
        cta = "\n\n".join(_link(link, f"{btn} · Part {i}") for i, link in enumerate(links, start=1))

    parts = {"title": cover_parts.get("title"), "teaser": hook}
    tags = _with_format_tag(cover_info.get("hashtags"), "bot", rules)
    log.info("Cover caption: relation=%s name=%r", rel, name)
    return _fit(parts, cta, tags, _footer(rules), cover_info)