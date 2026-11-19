"""Индексация канала: обход истории, вычистка удалённых.

Индексация инкрементальная — по meta.last_msg_id.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from telethon.tl.types import DocumentAttributeAudio, DocumentAttributeFilename

from ..metadata.enricher import (
    _artist_id,
    build_search_text,
    extract_primary_artist,
    fallback_album_for,
    parse_artist_title,
)

log = logging.getLogger("tgstream.index")

AUDIO_EXTS = {
    "mp3", "flac", "m4a", "ogg", "opus", "wav", "aac", "wma", "aiff", "ape",
}
MAX_FILE_SIZE = 4_000_000_000
BATCH_SIZE = 200
EXIST_CHECK_BATCH = 200


def _ext_of(filename: Optional[str]) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def message_to_row(msg, chat_id: int, channel_title: str) -> Optional[dict]:
    """Сообщение -> строка tracks, либо None если это не аудио."""
    doc = getattr(msg, "document", None)
    if doc is None:
        return None

    audio = next(
        (a for a in doc.attributes
         if isinstance(a, DocumentAttributeAudio) and not getattr(a, "voice", False)),
        None,
    )
    filename = next(
        (a.file_name for a in doc.attributes
         if isinstance(a, DocumentAttributeFilename) and a.file_name),
        None,
    )
    ext = _ext_of(filename)
    if audio is None and ext not in AUDIO_EXTS:
        return None
    if not doc.size or doc.size > MAX_FILE_SIZE:
        return None

    stem = (filename or "").rsplit(".", 1)[0] if filename else ""
    raw_artist = (audio and audio.performer) or channel_title
    raw_title = (audio and audio.title) or stem or f"Track {msg.id}"
    artist, title = parse_artist_title(raw_artist, raw_title, channel_title)
    album_artist = extract_primary_artist(artist)
    if artist.lower() == album_artist.lower():
        artist = album_artist
    fb_album, fb_album_id = fallback_album_for(album_artist, channel_title)

    return {
        "id": f"tr-{chat_id}-{msg.id}",
        "chat_id": chat_id,
        "msg_id": msg.id,
        "title": title,
        "artist": artist,
        "album_artist": album_artist,
        "artist_id": _artist_id(album_artist),
        "album": fb_album,
        "album_id": fb_album_id,
        "duration": int(audio.duration) if audio and audio.duration else 0,
        "size": int(doc.size),
        "mime": doc.mime_type,
        "ext": ext,
        "has_thumb": 1 if getattr(doc, "thumbs", None) else 0,
        "posted_at": int(msg.date.timestamp()) if msg.date else 0,
        "track_number": 0,
        "disc_number": 1,
        "year": 0,
        "genre": "",
        "cover_url": None,
        "raw_artist": raw_artist,
        "raw_title": raw_title,
        "enrich_status": "pending",
        "search_text": build_search_text(artist, album_artist, title, fb_album),
    }


async def index_channel(tg, lib, chat_ref: str, full: bool = False) -> tuple:
    """Проиндексировать канал; возвращает (chat_id, число новых треков)."""
    entity = await tg.client.get_entity(chat_ref)
    chat_id = entity.id
    channel_title = getattr(entity, "title", None) or chat_ref
    username = getattr(entity, "username", None)
    access_hash = getattr(entity, "access_hash", None)
    await lib.upsert_channel(chat_id, channel_title, username, access_hash)

    last = 0 if full else await lib.last_msg_id(chat_id)
    log.info("Индексация %s (chat_id=%s, от msg_id > %s)", channel_title, chat_id, last)

    max_id = last
    added = 0
    batch: List[dict] = []

    async for msg in tg.client.iter_messages(entity, reverse=True, min_id=last):
        if msg.id > max_id:
            max_id = msg.id
        row = message_to_row(msg, chat_id, channel_title)
        if row is None:
            continue
        batch.append(row)
        added += 1
        if len(batch) >= BATCH_SIZE:
            await lib.upsert_tracks(batch)
            log.info("  %s: +%d треков (msg_id=%s)", channel_title, added, msg.id)
            batch = []

    if batch:
        await lib.upsert_tracks(batch)
    if max_id > last:
        await lib.set_last_msg_id(chat_id, max_id)
    log.info("Канал %s проиндексирован: +%d треков", channel_title, added)
    return chat_id, added


async def cleanup_deleted(tg, lib, cache, chat_id: int) -> int:
    """Удалить из базы треки, сообщений которых в канале больше нет."""
    rows = await lib.tracks_msg_ids(chat_id)
    by_msg = dict(rows)
    all_ids = list(by_msg.keys())
    removed = 0

    for i in range(0, len(all_ids), EXIST_CHECK_BATCH):
        part = all_ids[i:i + EXIST_CHECK_BATCH]
        try:
            msgs = await tg.client.get_messages(chat_id, ids=part)
        except Exception:
            log.exception("Вычистка chat_id=%s: проверка партии не удалась, прерываю", chat_id)
            return removed
        for mid, m in zip(part, msgs or []):
            if m is None and mid in by_msg:
                tid = by_msg[mid]
                await lib.delete_track(tid)
                cache.remove_track(tid)
                removed += 1
        await asyncio.sleep(2.0)

    if removed:
        log.info("Вычистка chat_id=%s: удалено %d треков, отсутствующих в канале", chat_id, removed)
    return removed
