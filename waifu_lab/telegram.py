from __future__ import annotations

import json
import logging
import time

import httpx

from waifu_lab.config import REQUEST_TIMEOUT, TELEGRAM_API_BASE
from waifu_lab.images import download_image, fit_telegram, media_url

log = logging.getLogger(__name__)


def _url(token: str, method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{token}/{method}"


def _clean_caption(caption: str | None) -> str:
    """Never let a missing caption crash sending or error reporting."""
    return (caption or "").strip()


def _raise_with_detail(r: httpx.Response, method: str, extra: str = "") -> None:
    """Log Telegram's own error description before raising (raise_for_status alone hides it)."""
    if r.is_success:
        return
    log.error("Telegram %s HTTP %s %s | %s", method, r.status_code, extra, r.text[:500])
    r.raise_for_status()


def send_photo(
    client: httpx.Client,
    token: str,
    chat_id: str,
    caption: str,
    *,
    photo_url: str | None = None,
    photo_bytes: bytes | None = None,
    has_spoiler: bool = False,
    button_url: str | None = None,
) -> dict:
    caption = _clean_caption(caption)
    data = {
        "chat_id": chat_id,
        "has_spoiler": "true" if has_spoiler else "false",
    }
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    if button_url:
        data["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "♡ ♡ Hen.ART ♡ ♡", "url": button_url}]]}
        )
    files = None
    if photo_bytes is not None:
        files = {"photo": ("photo.jpg", fit_telegram(photo_bytes), "image/jpeg")}
    else:
        data["photo"] = photo_url
    r = client.post(_url(token, "sendPhoto"), data=data, files=files, timeout=REQUEST_TIMEOUT)
    _raise_with_detail(r, "sendPhoto", f"chat_id={chat_id} caption_len={len(caption)}")
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto failed: {payload}")
    log.info("Telegram photo sent")
    return payload


def send_media_group(
    client: httpx.Client,
    token: str,
    chat_id: str,
    caption: str,
    *,
    image_urls: list[str] | None = None,
    images: list[bytes] | None = None,
) -> dict:
    caption = _clean_caption(caption)
    urls = image_urls or []
    blobs = images or []
    if len(blobs) == 1:
        return send_photo(client, token, chat_id, caption, photo_bytes=blobs[0])
    if len(urls) == 1 and not blobs:
        return send_photo(client, token, chat_id, caption, photo_url=urls[0])

    media = []
    files = None
    if blobs:
        files = {}
        for idx, blob in enumerate(blobs):
            field = "data" if idx == 0 else f"data_{idx}"
            media.append(
                {
                    "type": "photo",
                    "media": f"attach://{field}",
                    **({"caption": caption, "parse_mode": "HTML"} if idx == 0 and caption else {}),
                }
            )
            files[field] = (f"{field}.jpg", fit_telegram(blob), "image/jpeg")
    else:
        for idx, src in enumerate(urls):
            media.append(
                {
                    "type": "photo",
                    "media": src,
                    **({"caption": caption, "parse_mode": "HTML"} if idx == 0 and caption else {}),
                }
            )

    data = {"chat_id": chat_id, "media": json.dumps(media)}
    r = client.post(_url(token, "sendMediaGroup"), data=data, files=files, timeout=60.0)
    _raise_with_detail(r, "sendMediaGroup", f"chat_id={chat_id} caption_len={len(caption)} items={len(media)}")
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram sendMediaGroup failed: {payload}")
    log.info("Telegram album sent (%s photos)", len(blobs) or len(urls))
    return payload


def send_item_photo(client, token, chat_id, item: dict, caption: str, **kwargs) -> dict:
    url = media_url(item)
    try:
        return send_photo(client, token, chat_id, caption, photo_url=url, **kwargs)
    except Exception as exc:
        log.warning("URL send failed, downloading: %s", exc)
    blob = download_image(client, item)
    return send_photo(client, token, chat_id, caption, photo_bytes=blob, **kwargs)


def send_item_album(client, token, chat_id, items: list[dict], caption: str) -> dict:
    urls = [media_url(it) for it in items]
    try:
        return send_media_group(client, token, chat_id, caption, image_urls=urls)
    except Exception as exc:
        log.warning("URL album failed, downloading: %s", exc)

    blobs: list[bytes] = []
    for it in items:
        try:
            blobs.append(download_image(client, it))
        except Exception as exc:
            log.warning("Skipping album page %s: %s", it.get("id"), exc)
    if not blobs:
        raise RuntimeError("No album images could be downloaded")
    return send_media_group(client, token, chat_id, caption, images=blobs)


def message_id_of(payload: dict) -> int:
    result = payload["result"]
    if isinstance(result, list):
        result = result[0]
    return int(result["message_id"])


def delete_message(client: httpx.Client, token: str, chat_id: str, message_id: int) -> bool:
    """Best-effort delete (used to roll back a photo when the zip could not be sent). Never raises."""
    try:
        r = client.post(
            _url(token, "deleteMessage"),
            data={"chat_id": chat_id, "message_id": message_id},
            timeout=REQUEST_TIMEOUT,
        )
        ok = bool(r.json().get("ok"))
    except Exception as exc:
        log.error("deleteMessage failed for %s: %s", message_id, exc)
        return False
    if ok:
        log.info("Rolled back photo message %s", message_id)
    else:
        log.error("deleteMessage refused for %s: %s", message_id, r.text)
    return ok


# ---- multi-album sending (SFW posts with more than 10 pages) ----

ALBUM_MAX = 10          # Telegram limit per sendMediaGroup
ALBUM_DELAY = 1.5       # seconds between albums
RATE_LIMIT_RETRIES = 3


def split_balanced(items: list, max_size: int = ALBUM_MAX) -> list[list]:
    """Split into ceil(n/max_size) near-equal chunks, order preserved (11 -> 6+5, 23 -> 8+8+7)."""
    n = len(items)
    if n <= max_size:
        return [items]
    parts = -(-n // max_size)
    base, extra = divmod(n, parts)
    out, i = [], 0
    for j in range(parts):
        size = base + (1 if j < extra else 0)
        out.append(items[i : i + size])
        i += size
    return out


def message_ids_of(payload: dict) -> list[int]:
    result = payload["result"]
    if isinstance(result, dict):
        result = [result]
    return [int(m["message_id"]) for m in result]


def _send_album_with_rate_limit(client, token, chat_id, chunk, caption) -> dict:
    for attempt in range(1, RATE_LIMIT_RETRIES + 1):
        try:
            return send_item_album(client, token, chat_id, chunk, caption)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
                raise
            try:
                wait = int(exc.response.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            wait = min(wait + 1, 120)
            log.warning("Telegram 429, sleeping %ss (try %s/%s)", wait, attempt, RATE_LIMIT_RETRIES)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def send_item_albums(client, token, chat_id, items: list[dict], caption: str) -> list[int]:
    """Send all pages as one or more balanced albums, same caption on the first photo of each.

    All or nothing: if any album fails, the albums already sent for this post are deleted
    and the error is raised. Returns the message ids that were sent.
    """
    chunks = split_balanced(items)
    log.info("Sending %s pages as %s album(s): %s", len(items), len(chunks), [len(c) for c in chunks])
    sent: list[int] = []
    try:
        for idx, chunk in enumerate(chunks):
            if idx:
                time.sleep(ALBUM_DELAY)
            payload = _send_album_with_rate_limit(client, token, chat_id, chunk, caption)
            sent.extend(message_ids_of(payload))
    except Exception:
        if sent:
            log.error("Album %s/%s failed, rolling back %s message(s)", idx + 1, len(chunks), len(sent))
            for mid in sent:
                if not delete_message(client, token, chat_id, mid):
                    log.error("ORPHAN album message left in channel: %s", mid)
        raise
    return sent
