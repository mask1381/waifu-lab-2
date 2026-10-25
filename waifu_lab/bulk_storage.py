from __future__ import annotations

"""Bulk upload to the storage chat: /bulk_start -> files (no per-album links) -> /bulk_end -> ONE link.

Usage: link = bulk_upload([[album1_paths], [album2_paths], ...])
(for AniWall: same idea, use config.STORAGE_LINK_RE instead of _bot_link_pattern)
"""

import asyncio
import logging
import os
import time
from pathlib import Path

from waifu_lab.userbot_zip import (
    BOT_POLL_INTERVAL,
    BOT_REPLY_TIMEOUT,
    _bot_link_pattern,
    _pick_bot_link,
    _resolve_entity,
)

log = logging.getLogger(__name__)

SETTLE_SECONDS = 6   # let the bot register the last album parts before /bulk_end
BETWEEN_ALBUMS = 2


async def _cmd(client, entity, cmd: str, want_link: bool) -> str:
    sent = await client.send_message(entity, cmd)
    pattern = _bot_link_pattern()
    deadline = time.monotonic() + BOT_REPLY_TIMEOUT
    while time.monotonic() < deadline:
        async for m in client.iter_messages(entity, min_id=sent.id, limit=30):
            if getattr(m, "reply_to_msg_id", None) != sent.id:
                continue
            if not want_link:
                return ""
            link = _pick_bot_link(m, pattern)
            if link:
                return link
            raise RuntimeError(f"bulk end failed: {(getattr(m, 'message', '') or '')[:120]}")
        await asyncio.sleep(BOT_POLL_INTERVAL)
    raise RuntimeError(f"bot did not answer {cmd}")


async def _bulk_all(batches: list[list[Path]]) -> str:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(os.environ["TG_SESSION_STRING"].strip()),
        int(os.environ["TG_API_ID"].strip()),
        os.environ["TG_API_HASH"].strip(),
    )
    await client.connect()
    try:
        entity = await _resolve_entity(client, int(os.environ["STORAGE_CHAT_ID"].strip()))
        await _cmd(client, entity, "/bulk_start", False)   # wait until the bot turned bulk ON
        ids: list[int] = []
        try:
            for paths in batches:
                payload = str(paths[0]) if len(paths) == 1 else [str(p) for p in paths]
                as_doc = len(paths) == 1 and paths[0].suffix.lower() == ".zip"
                sent = await client.send_file(entity, payload, force_document=as_doc)
                ids += [int(m.id) for m in (sent if isinstance(sent, (list, tuple)) else [sent])]
                await asyncio.sleep(BETWEEN_ALBUMS)
            await asyncio.sleep(SETTLE_SECONDS)
            link = await _cmd(client, entity, "/bulk_end", True)
        except Exception:
            try:                                            # never leave bulk mode ON
                await _cmd(client, entity, "/bulk_end", False)
            except Exception as exc:
                log.error("could not close bulk mode: %s", exc)
            if ids:
                try:
                    await client.delete_messages(entity, ids)
                except Exception as exc:
                    log.error("ORPHAN storage messages %s: %s", ids, exc)
            raise
        log.info("Bulk link ready (%s messages)", len(ids))
        return link
    finally:
        await client.disconnect()


def bulk_upload(batches: list[list[Path]]) -> str:
    if not batches:
        raise ValueError("Nothing to upload")
    return asyncio.run(_bulk_all(batches))
