from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from waifu_lab.telegram import split_balanced
from waifu_lab.userbot_zip import _resolve_entity

log = logging.getLogger(__name__)

CHUNK_GAP = 60   # seconds between albums of one post
ATTEMPTS = 2


def _utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        raise ValueError("schedule time must be timezone-aware")
    return when.astimezone(timezone.utc)


def _ids(res) -> list[int]:
    items = res if isinstance(res, (list, tuple)) else [res]
    return [int(m.id) for m in items if m is not None and getattr(m, "id", None) is not None]


@asynccontextmanager
async def _session(chat_id: str):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Userbot session is not authorized (TG_SESSION_STRING invalid or revoked)")
        entity = await _resolve_entity(client, int(chat_id))
        yield client, entity
    finally:
        await client.disconnect()


async def _delete(client, entity, ids: list[int]) -> None:
    """Best-effort removal of scheduled messages (rollback). Never raises."""
    from telethon.tl import functions

    if not ids:
        return
    try:
        await client(functions.messages.DeleteScheduledMessagesRequest(peer=entity, id=list(ids)))
        log.info("Rolled back scheduled messages %s", ids)
    except Exception as exc:
        log.error("ORPHAN scheduled messages %s could not be deleted: %s", ids, exc)


def _retry(factory, what: str):
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return asyncio.run(factory())
        except Exception as exc:
            last = exc
            text = str(exc)
            if "SCHEDULE_TOO_MUCH" in text or "SCHEDULE_DATE" in text:
                raise
            log.warning("%s failed (try %s/%s): %s", what, attempt, ATTEMPTS, exc)
            if attempt < ATTEMPTS:
                time.sleep(5 * attempt)
    raise RuntimeError(f"{what} failed after {ATTEMPTS} attempts") from last


# ---- list ----

async def _list(chat_id: str) -> list[dict]:
    from telethon.tl import functions

    async with _session(chat_id) as (client, entity):
        res = await client(functions.messages.GetScheduledHistoryRequest(peer=entity, hash=0))
        out: list[dict] = []
        for m in getattr(res, "messages", None) or []:
            date = getattr(m, "date", None)
            if date is None:
                continue
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            out.append({"id": int(m.id), "date": date})
        return out


def list_scheduled(chat_id: str) -> list[dict]:
    """Messages currently waiting in the channel's scheduled queue: [{"id", "date" (UTC, aware)}]."""
    return asyncio.run(_list(chat_id))


# ---- album ----

async def _album(chat_id: str, blobs: list[bytes], caption: str, when: datetime | None) -> list[int]:
    async with _session(chat_id) as (client, entity):
        ids: list[int] = []
        try:
            for i, idxs in enumerate(split_balanced(list(range(len(blobs))))):
                files = []
                for k in idxs:
                    bio = io.BytesIO(blobs[k])
                    bio.name = f"page_{k + 1}.jpg"
                    files.append(bio)
                at = _utc(when + timedelta(seconds=CHUNK_GAP * i)) if when else None
                if len(files) == 1:
                    res = await client.send_file(entity, files[0], caption=caption, parse_mode="html", schedule=at)
                else:
                    caps = [caption] + [""] * (len(files) - 1)
                    res = await client.send_file(entity, files, caption=caps, parse_mode="html", schedule=at)
                ids.extend(_ids(res))
        except Exception:
            await _delete(client, entity, ids)
            raise
        log.info("Album %s (%s messages)", when.strftime("%m-%d %H:%M") if when else "sent now", len(ids))
        return ids


def schedule_album(chat_id: str, blobs: list[bytes], caption: str, when: datetime | None = None) -> list[int]:
    """Schedule photos as one or more balanced albums, same caption on the first photo of each.

    when=None sends immediately instead of scheduling.
    """
    return _retry(lambda: _album(chat_id, blobs, caption, when), "Scheduling album")


# ---- text ----

async def _text(chat_id: str, text: str, when: datetime | None) -> list[int]:
    async with _session(chat_id) as (client, entity):
        res = await client.send_message(
            entity, text, parse_mode="html", link_preview=False, schedule=_utc(when) if when else None
        )
        return _ids(res)


def schedule_text(chat_id: str, text: str, when: datetime | None) -> list[int]:
    """Text-only post (used when an NSFW post has no SFW cover at all)."""
    return _retry(lambda: _text(chat_id, text, when), "Scheduling text")