from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from waifu_lab.images import IMG_EXT, PXIMG_HEADERS
from waifu_lab.rules import get_rule, select_pages, zip_allowed

log = logging.getLogger(__name__)

FALLBACK_ZIP_NAME = "WaifuLab"
MAX_NAME_LEN = 60
MAX_ZIP_BYTES = int(1.9 * 1024**3)  # Telethon limit is 2 GB for normal accounts
DOWNLOAD_TIMEOUT = 90.0
DOWNLOAD_ATTEMPTS = 2

# storage channel + delivery bot
DEFAULT_BOT_USERNAME = "littlemask02_bot"
BOT_REPLY_TIMEOUT = 45.0   # the bot usually answers in ~5s
BOT_POLL_INTERVAL = 2.0
STORAGE_ATTEMPTS = 2
GALLERY_PART_SIZE = 10     # Telegram media-group limit


def userbot_configured() -> bool:
    api_id = os.environ.get("TG_API_ID", "").strip()
    api_hash = os.environ.get("TG_API_HASH", "").strip()
    session = os.environ.get("TG_SESSION_STRING", "").strip()
    return bool(api_id.isdigit() and api_hash and session)


def storage_configured() -> bool:
    return os.environ.get("STORAGE_CHAT_ID", "").strip().lstrip("-").isdigit()


# ---- zip naming ----

def _camel(text) -> str:
    """'Final Fantasy X' -> 'FinalFantasyX' (ASCII only)."""
    ascii_text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", ascii_text)
    return "".join(w[:1].upper() + w[1:] for w in words)


def zip_name_base(info: dict | None, art_title: str | None = None) -> str:
    """Name that matches the content: characters (max 2) + franchise.

    No character -> the artist's Latin title first. Nothing at all -> first theme, then 'WaifuLab'.
    """
    info = info or {}
    chars = [c for c in (_camel(x) for x in (info.get("characters") or [])[:2]) if c]
    parts: list[str] = []
    if chars:
        parts.append("_".join(chars))
    else:
        title = _camel(art_title)
        if title:
            parts.append(title)
    franchise = _camel(info.get("franchise"))
    if franchise:
        parts.append(franchise)
    if not parts:
        theme = next((t for t in (_camel(x) for x in info.get("themes") or []) if t), "")
        if theme:
            parts.append(theme)
    name = "_".join(parts)[:MAX_NAME_LEN].strip("_")
    return name or FALLBACK_ZIP_NAME


# ---- zip building ----

def _original_urls(page: dict) -> list[str]:
    """Original-quality URLs only (no master1200 conversion), direct host first, proxy as fallback."""
    out: list[str] = []
    for raw in (page.get("image_url_hq"), page.get("image_url_std")):
        raw = str(raw or "")
        if not raw.startswith("https://"):
            continue
        direct = raw.replace("i.pixiv.re", "i.pximg.net")
        proxy = direct.replace("i.pximg.net", "i.pixiv.re")
        for url in (direct, proxy):
            if url not in out:
                out.append(url)
    return out


def download_original(client: httpx.Client, page: dict) -> tuple[str, bytes]:
    last_error: Exception | None = None
    for url in _original_urls(page):
        headers = PXIMG_HEADERS if "i.pximg.net" in url else None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                r = client.get(url, headers=headers, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
                r.raise_for_status()
                ctype = r.headers.get("content-type", "")
                if not r.content or not ctype.startswith("image/"):
                    raise RuntimeError(f"unexpected response ({ctype or 'no content-type'}, {len(r.content)} bytes)")
                name = Path(urlparse(url).path).name
                if not IMG_EXT.search(name):
                    name = f"{page.get('id') or 'image'}.jpg"
                log.info("Original downloaded %s via %s (%s bytes)", name, url.split("/")[2], len(r.content))
                return name, r.content
            except Exception as exc:
                last_error = exc
                log.warning("Original fetch failed (%s, try %s/%s): %s", url, attempt, DOWNLOAD_ATTEMPTS, exc)
                if attempt < DOWNLOAD_ATTEMPTS:
                    time.sleep(1.5 * attempt)
    raise RuntimeError(f"Could not download original for page {page.get('id')}") from last_error


def _clean_name(text: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", text or "") or FALLBACK_ZIP_NAME


def build_zip(
    client: httpx.Client,
    pages: list[dict],
    workdir: Path,
    *,
    archive_name: str,
    file_prefix: str,
) -> Path:
    """Download exactly these pages into one uncompressed zip. All pages or nothing.

    Files keep the given order and are numbered with zero padding:
    Lulu_FinalFantasyX_01.png, ..._02.png. The archive is <archive_name>.zip.
    One failed download aborts the whole zip, so a broken post never ships half a gallery.
    """
    if not pages:
        raise ValueError("build_zip needs at least one page")
    archive_name = _clean_name(archive_name)
    prefix = _clean_name(file_prefix)
    width = max(2, len(str(len(pages))))

    tmp_path = workdir / f"{archive_name}.building.zip"
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for index, page in enumerate(pages, start=1):
            name, blob = download_original(client, page)
            ext = Path(name).suffix.lower() or ".jpg"
            zf.writestr(f"{prefix}_{index:0{width}d}{ext}", blob)

    final_path = workdir / f"{archive_name}.zip"
    tmp_path.replace(final_path)

    size = final_path.stat().st_size
    if size > MAX_ZIP_BYTES:
        raise RuntimeError(f"Zip too large for upload: {size} bytes")
    log.info("Zip ready: %s (%s files, %.1f MB)", final_path.name, len(pages), size / 1024**2)
    return final_path


def prepare_zip(
    client: httpx.Client,
    pages: list[dict],
    illust_id: str,
    workdir: Path,
    rules: dict | None = None,
    name_hint: str | None = None,
) -> Path | None:
    """Download pages at original quality into one uncompressed zip. All pages or nothing.

    rules may override:
      - delivery.allowed / zip.enabled (see rules.zip_allowed: not allowed -> None)
      - zip.pages ('all' | 1-based int list) which pages to include
    """
    rules = rules or {}
    # Last line of defence: even if a caller (gallery, a future path) ends up here
    # by mistake, no zip is built when the artist's rules do not allow one.
    if not zip_allowed(rules):
        log.info("Zip not allowed by artist rules; skipping zip")
        return None

    chosen_pages = select_pages(pages, get_rule(rules, "zip", "pages", default="all"))
    if not chosen_pages:
        log.warning("Zip rules selected no pages")
        return None
    base = _clean_name(name_hint)
    return build_zip(
        client,
        chosen_pages,
        workdir,
        archive_name=f"{base}_{illust_id}",
        file_prefix=base,
    )


def split_parts(pages: list[dict], size: int = GALLERY_PART_SIZE) -> list[list[dict]]:
    """Cut the post into gallery parts (Telegram shows at most 10 media per album)."""
    size = max(1, int(size))
    return [pages[i:i + size] for i in range(0, len(pages), size)]


# ---- telethon helpers ----

async def _resolve_entity(client, chat_id: int):
    try:
        return await client.get_entity(chat_id)
    except ValueError:
        # fresh session has no cached channels; load dialogs once, then retry
        await client.get_dialogs()
        return await client.get_entity(chat_id)


@dataclass
class StorageUpload:
    """One storage message (a zip, or a media group when zip is not allowed) plus its deep link."""
    link: str
    file_id: int
    reply_id: int | None = None
    message_ids: list[int] = field(default_factory=list)  # every message the upload created

    def all_ids(self) -> list[int]:
        ids = list(self.message_ids) or [self.file_id]
        if self.reply_id:
            ids.append(self.reply_id)
        return ids


# ---- storage channel: userbot uploads, the bot replies with a deep link ----

# The bot's reply ends with one label line + one deep link per lab:
#   🔔 ‹ AniWall Lab ›   -> ...?start=wlart_...
#   🔔 ◇ GACHAVERSE ART ◇ -> ...?start=wlart_...
#   🔔 『 Waifu Lab 』     -> ...?start=wlart_...
# This pipeline posts to Waifu Lab, so that third link is the one to use.
DEFAULT_LINK_LABEL = "Waifu Lab"
LINK_LABEL_WINDOW = 200  # characters of text inspected above each link
URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _bot_link_pattern() -> re.Pattern:
    bot = (os.environ.get("STORAGE_BOT_USERNAME") or DEFAULT_BOT_USERNAME).strip().lstrip("@")
    return re.compile(rf"https?://t\.me/{re.escape(bot)}\?start=[A-Za-z0-9_\-]+", re.I)


def _link_label() -> str:
    return (os.environ.get("STORAGE_BOT_LINK_LABEL") or DEFAULT_LINK_LABEL).strip() or DEFAULT_LINK_LABEL


def _label_key(text: str) -> str:
    """'🔔 『 Waifu Lab 』' -> 'waifu lab' (lowercase words only)."""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _utf16_index(text: str, offset: int) -> int:
    """Telegram entity offsets count UTF-16 units; convert to a Python index."""
    if offset <= 0:
        return 0
    units = 0
    for index, char in enumerate(text):
        if units >= offset:
            return index
        units += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _message_links(m) -> list[tuple[int, str]]:
    """Every link in the message as (position, url), in text order.

    Entity URLs are included because the bot may hide the link behind anchor
    text; plain URLs are matched in the text itself. Positions are Python
    indexes into the message text, so a link keeps its label line.
    """
    text = getattr(m, "message", "") or ""
    found: dict[str, int] = {}
    for ent in getattr(m, "entities", None) or []:
        url = getattr(ent, "url", None)
        if url:
            found.setdefault(str(url), _utf16_index(text, int(getattr(ent, "offset", 0) or 0)))
    for match in URL_RE.finditer(text):
        found.setdefault(match.group(0), match.start())
    return sorted(((pos, url) for url, pos in found.items()), key=lambda item: item[0])


def _link_label_line(text: str, pos: int) -> str:
    """Nearest non-empty line above the link: the footer's label for that link."""
    lines = [line.strip() for line in text[max(0, pos - LINK_LABEL_WINDOW):pos].splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _pick_bot_link(message, pattern: re.Pattern) -> str | None:
    """The one deep link to use from the bot's reply.

    The reply lists one link per lab; the link is chosen by the label line right
    above it, not by position. If no label matches, the last link wins: the
    footer ends with the Waifu Lab line.
    """
    text = getattr(message, "message", "") or ""
    links: list[tuple[int, str]] = []
    seen: set[str] = set()
    for pos, url in _message_links(message):
        hit = pattern.search(url)
        if not hit or hit.group(0) in seen:
            continue
        seen.add(hit.group(0))
        links.append((pos + hit.start(), hit.group(0)))
    if not links:
        return None
    if len(links) == 1:
        return links[0][1]

    wanted = _label_key(_link_label())
    for pos, link in links:
        if wanted and wanted in _label_key(_link_label_line(text, pos)):
            log.info("Bot reply: picked the %r link %s", _link_label(), link)
            return link

    log.warning(
        "Bot reply has %s links and none sits under %r; using the last one: %s (links: %s)",
        len(links), _link_label(), links[-1][1], ", ".join(link for _, link in links),
    )
    return links[-1][1]


async def _wait_for_bot_link(client, entity, sent_ids: int | list[int], pattern: re.Pattern) -> tuple[str, int] | None:
    """Poll for the bot's reply and return (deep link, reply message id).

    A media group is several messages; the bot may reply to any of them, so accept them all.
    """
    wanted = {sent_ids} if isinstance(sent_ids, int) else set(sent_ids)
    first_id = min(wanted)
    deadline = time.monotonic() + BOT_REPLY_TIMEOUT
    while time.monotonic() < deadline:
        async for m in client.iter_messages(entity, min_id=first_id, limit=30):
            if getattr(m, "reply_to_msg_id", None) not in wanted:
                continue
            link = _pick_bot_link(m, pattern)
            if link:
                return link, int(m.id)
        await asyncio.sleep(BOT_POLL_INTERVAL)
    return None


async def _to_storage(
    client, entity, paths: list[Path], label: str = "", force_document: bool = False
) -> StorageUpload:
    """Upload one message (one zip, or a media group of pages) and wait for the bot's link.

    Deletes the message(s) if the bot stays silent.
    """
    last_pct = {"v": -25}

    def progress(sent: int, total: int) -> None:
        pct = int(sent * 100 / total) if total else 100
        if pct - last_pct["v"] >= 25:
            last_pct["v"] = pct
            log.info("Storage upload %s%s%%", f"{label} " if label else "", pct)

    payload = str(paths[0]) if len(paths) == 1 else [str(p) for p in paths]
    as_document = force_document or (len(paths) == 1 and paths[0].suffix.lower() == ".zip")
    sent = await client.send_file(entity, payload, force_document=as_document, progress_callback=progress)
    msgs = sent if isinstance(sent, (list, tuple)) else [sent]
    ids = [int(m.id) for m in msgs]
    primary = ids[0]
    found = await _wait_for_bot_link(client, entity, ids, _bot_link_pattern())
    if not found:
        try:
            await client.delete_messages(entity, ids)
        except Exception as exc:
            log.error("ORPHAN storage message %s could not be deleted: %s", ids, exc)
        raise RuntimeError(f"Storage bot did not reply with a link within {BOT_REPLY_TIMEOUT:.0f}s")
    link, reply_id = found
    log.info("Storage link ready: %s (message %s)", link, primary)
    return StorageUpload(link=link, file_id=primary, reply_id=reply_id, message_ids=ids)


async def _upload_all(batches: list[list[Path]], force_document: bool = False) -> list[StorageUpload]:
    """Upload a whole batch in one session; roll every upload back if any of them fails."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    uploads: list[StorageUpload] = []
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized (TG_SESSION_STRING invalid or revoked)")
        entity = await _resolve_entity(client, int(os.environ["STORAGE_CHAT_ID"].strip()))
        try:
            for index, paths in enumerate(batches, start=1):
                uploads.append(
                    await _to_storage(client, entity, paths, f"{index}/{len(batches)}", force_document)
                )
        except Exception:
            ids = [mid for u in uploads for mid in u.all_ids()]
            if ids:
                try:
                    await client.delete_messages(entity, ids)
                    log.info("Rolled back storage uploads %s", ids)
                except Exception as exc:
                    log.error("ORPHAN storage messages %s could not be deleted: %s", ids, exc)
            raise
        return uploads
    finally:
        await client.disconnect()


def _upload_batch(batches: list[list[Path]], force_document: bool = False) -> list[StorageUpload]:
    """Upload one message per batch (a zip or a media group)."""
    if not batches:
        raise ValueError("Nothing to upload")
    last_error: Exception | None = None
    for attempt in range(1, STORAGE_ATTEMPTS + 1):
        try:
            return asyncio.run(_upload_all(batches, force_document))
        except Exception as exc:
            last_error = exc
            log.warning("Storage upload failed (try %s/%s): %s", attempt, STORAGE_ATTEMPTS, exc)
            if attempt < STORAGE_ATTEMPTS:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Storage upload failed after {STORAGE_ATTEMPTS} attempts") from last_error


def send_zip_to_storage(zip_path: Path) -> StorageUpload:
    """Upload one zip to the storage channel and return its bot deep link."""
    return _upload_batch([[zip_path]])[0]


def _part_files(client: httpx.Client, part: list[dict], workdir: Path, prefix: str) -> list[Path]:
    """Download one gallery part as plain original files (no archive)."""
    paths: list[Path] = []
    for index, page in enumerate(part, start=1):
        name, blob = download_original(client, page)
        ext = Path(name).suffix.lower() or ".jpg"
        path = workdir / f"{prefix}_{index:02d}{ext}"
        path.write_bytes(blob)
        paths.append(path)
    return paths


def send_gallery_to_storage(
    client: httpx.Client,
    pages: list[dict],
    workdir: Path,
    base_name: str,
    rules: dict | None = None,
) -> list[str]:
    """Upload pages to the storage chat as media groups (max 10, balanced). One deep link per part.
    Files are originals-as-documents only when the artist has document=true."""
    from waifu_lab.telegram import split_balanced

    parts = split_balanced(list(pages), GALLERY_PART_SIZE)
    batches = [
        _part_files(client, part, workdir, f"{_clean_name(base_name)}_Part{index}")
        for index, part in enumerate(parts, start=1)
    ]
    links = [
        u.link
        for u in _upload_batch(batches, force_document=bool(get_rule(rules or {}, "document", default=False)))
    ]
    log.info("Storage ready: %s part(s)", len(links))
    return links


def send_hd_to_storage(client: httpx.Client, page: dict, workdir: Path, base_name: str) -> str:
    """One original-quality image as a document -> bot deep link."""
    name, blob = download_original(client, page)
    path = workdir / f"{_clean_name(base_name)}_HD{Path(name).suffix.lower() or '.jpg'}"
    path.write_bytes(blob)
    return _upload_batch([[path]], force_document=True)[0].link