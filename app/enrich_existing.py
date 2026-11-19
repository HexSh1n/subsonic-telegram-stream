"""Скрипт полного обновления и обогащения метаданных для существующих треков в базе."""
from __future__ import annotations

import asyncio
import sys
import time

import httpx

from app.config import load_config
from app.library.db import Library
from app.metadata.enricher import (
    _artist_id,
    album_id_for,
    build_search_text,
    clean_display_artist,
    clean_display_title,
    enrich_track,
    extract_primary_artist,
    fallback_album_for,
)


async def main():
    full_recheck = "--full" in sys.argv
    cfg = load_config()
    lib = Library(cfg.db_path)
    await lib.init()

    assert lib._db is not None

    # 1. Сначала мгновенно пересчитываем канонические artist_id, album_artist, album_id,
    #    исправляем кракозябры CP1251, восстанавливаем '30 Seconds to Mars' и заполняем search_text
    #    для ВСЕХ уже обогащённых треков без повторных сетевых запросов (если не --full),
    #    а для треков без жанра или необогащённых — дотягиваем метаданные из Deezer/iTunes.
    all_rows = await lib._fetchall(
        """SELECT t.id, t.chat_id, t.msg_id, t.title, t.artist, t.album, t.album_id,
                  t.year, t.genre, t.cover_url, t.track_number, t.disc_number, t.duration,
                  t.enrich_status, c.title AS channel_title
           FROM tracks t
           LEFT JOIN channels c ON c.chat_id = t.chat_id
           ORDER BY t.chat_id, t.msg_id ASC"""
    )

    print(f"Всего треков в базе: {len(all_rows)}", flush=True)
    reindexed = 0
    to_enrich = []

    for r in all_rows:
        chan_title = r.get("channel_title") or "Telegram"
        disp_artist = clean_display_artist(r["artist"]) or r["artist"]
        disp_title = clean_display_title(r["title"]) or r["title"]
        album_artist = extract_primary_artist(disp_artist)
        if disp_artist.lower() == album_artist.lower():
            disp_artist = album_artist
        art_id = _artist_id(album_artist)

        has_real_album = (
            bool(r.get("cover_url"))
            and r.get("album")
            and r["album"] != chan_title
            and not r["album"].endswith(" — Синглы")
        )

        # Если название изменилось после исправления кракозябр (например Jane Air) — отправляем на обогащение
        mojibake_fixed = (disp_title != r["title"]) or (disp_artist != r["artist"])

        if has_real_album and not full_recheck and not mojibake_fixed:
            alb_name = r["album"]
            alb_id = album_id_for(album_artist, alb_name)
            stext = build_search_text(disp_artist, album_artist, disp_title, alb_name)
            await lib._db.execute(
                """UPDATE tracks SET
                     title = ?, artist = ?, album_artist = ?, artist_id = ?,
                     album = ?, album_id = ?, enrich_status = 'ok', search_text = ?
                   WHERE id = ?""",
                (disp_title, disp_artist, album_artist, art_id, alb_name, alb_id, stext, r["id"]),
            )
            reindexed += 1
        else:
            # Сразу обновим отображаемые artist/title/artist_id/fallback-альбом и поставим в очередь на API
            fb_alb, fb_alb_id = fallback_album_for(album_artist, chan_title)
            stext = build_search_text(disp_artist, album_artist, disp_title, fb_alb)
            await lib._db.execute(
                """UPDATE tracks SET
                     title = ?, artist = ?, album_artist = ?, artist_id = ?,
                     album = ?, album_id = ?, enrich_status = 'pending', search_text = ?
                   WHERE id = ?""",
                (disp_title, disp_artist, album_artist, art_id, fb_alb, fb_alb_id, stext, r["id"]),
            )
            r["artist"] = disp_artist
            r["title"] = disp_title
            to_enrich.append(r)

    await lib._db.commit()
    print(f"Мгновенно нормализовано ID/имён для уже обогащённых треков: {reindexed}", flush=True)
    print(f"Треков для API-поиска (необогащённые + исправленные кракозябры): {len(to_enrich)}", flush=True)

    enriched_count = 0
    skipped_count = 0

    async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "tg-stream/1.0"}) as client:
        try:
            total = len(to_enrich)
            for idx, tr in enumerate(to_enrich, start=1):
                t_id = tr["id"]
                chat_id = tr["chat_id"]
                artist = tr["artist"]
                title = tr["title"]
                channel_title = tr.get("channel_title") or "Telegram"
                dur = int(tr.get("duration") or 0)

                res = await enrich_track(artist, title, chat_id, channel_title, client=client, duration=dur)
                await lib.save_enrichment(t_id, res)

                if res["enrich_status"] == "ok":
                    enriched_count += 1
                    print(
                        f"[{idx}/{total}] OK: {res['artist']} — {res['title']} -> "
                        f"«{res['album']}» (#{res['track_number']}, {res['year']}, {res['genre'] or '-'})",
                        flush=True,
                    )
                else:
                    skipped_count += 1
                    if idx % 20 == 0 or idx == total:
                        print(f"[{idx}/{total}] -- {res['artist']} — {res['title']} -> «{res['album']}»", flush=True)

                await asyncio.sleep(0.12)

            # Дополнительно подтянем жанр и год альбома из Deezer для альбомов, где ещё нет жанра
            albums_missing_genre = await lib._fetchall(
                """SELECT album_id, MAX(album_artist) AS artist, MAX(album) AS album, MAX(title) AS sample_title
                   FROM tracks
                   WHERE enrich_status = 'ok' AND (genre IS NULL OR genre = '')
                   GROUP BY album_id"""
            )
            if albums_missing_genre:
                print(f"\nДокачиваем жанры и точные годы для {len(albums_missing_genre)} альбомов...", flush=True)
                for alb in albums_missing_genre:
                    res = await enrich_track(
                        alb["artist"], alb["sample_title"], 0, "Telegram", client=client
                    )
                    if res.get("enrich_status") == "ok" and (res.get("genre") or res.get("year")):
                        await lib._db.execute(
                            """UPDATE tracks SET
                                 genre = CASE WHEN (genre IS NULL OR genre = '') AND ? != '' THEN ? ELSE genre END,
                                 year = CASE WHEN ? > 0 THEN ? ELSE year END
                               WHERE album_id = ?""",
                            (res["genre"], res["genre"], res["year"], res["year"], alb["album_id"]),
                        )
                    await asyncio.sleep(0.08)
                await lib._db.commit()

            stats = await lib.harmonize_albums_and_neighbors()
            print(
                f"\nГармонизация альбомов завершена: подтянуто по соседям +{stats['inferred_neighbors']}, "
                f"выровнено годов/жанров: {stats['harmonized_tracks']}.",
                flush=True,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("\nПроцесс остановлен.", flush=True)
        finally:
            await lib.close()

    print(
        f"\nГотово! Дообогащено через API: {enriched_count}, в альбомах «Синглы»: {skipped_count} из {len(to_enrich)}.",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
