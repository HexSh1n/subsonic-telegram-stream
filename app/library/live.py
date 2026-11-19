"""Живые обновления: новые посты, редактирования и удаления попадают в базу в реальном времени."""
from __future__ import annotations

import logging

from telethon import events, utils

from ..metadata.enricher import enrich_track
from .indexer import message_to_row

log = logging.getLogger("tgstream.live")


def _raw_chat_id(marked: int) -> int:
    return utils.resolve_id(marked)[0] if marked < 0 else marked


def start_live_updates(rt) -> None:
    lib, tg = rt.lib, rt.tg

    async def _handle_upsert(event, is_edit: bool = False):
        if event.chat_id is None:
            return
        chat_id = _raw_chat_id(event.chat_id)
        title = await lib.channel_title(chat_id)
        if title is None:
            return  # канал не из нашего списка
        row = message_to_row(event.message, chat_id, title)
        if row is not None:
            enr = await enrich_track(
                row["artist"],
                row["title"],
                chat_id,
                title,
                client=rt.http,
                duration=int(row.get("duration") or 0),
            )
            row.update(enr)
            await lib.upsert_tracks([row])
            if is_edit:
                await lib.save_enrichment(row["id"], enr)
            action = "~трек (изменён)" if is_edit else "+трек"
            log.info(
                "Живое обновление: %s «%s» (артист: %s, альбом: «%s», трек №%d)",
                action, row["title"], row["artist"], row["album"], row["track_number"],
            )
        if not is_edit:
            await lib.bump_last_msg_id(chat_id, event.message.id)

    @tg.client.on(events.NewMessage())
    async def on_new(event):
        try:
            await _handle_upsert(event, is_edit=False)
        except Exception:
            log.exception("NewMessage: не удалось обработать")

    @tg.client.on(events.MessageEdited())
    async def on_edited(event):
        try:
            await _handle_upsert(event, is_edit=True)
        except Exception:
            log.exception("MessageEdited: не удалось обработать")

    @tg.client.on(events.MessageDeleted())
    async def on_deleted(event):
        try:
            if event.chat_id is None:
                return
            chat_id = _raw_chat_id(event.chat_id)
            for mid in event.deleted_ids or []:
                tid = await lib.track_id_by_msg(chat_id, mid)
                if tid:
                    await lib.delete_track(tid)
                    rt.cache.remove_track(tid)
                    log.info("Живое обновление: −трек %s", tid)
        except Exception:
            log.exception("MessageDeleted: не удалось обработать")

    log.info("Живые обновления включены (новые посты, правки и удаления с авто-обогащением метаданных)")
