"""stream / download / getCoverArt — отдача аудио и обложек с Range-поддержкой."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
import httpx
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

from ..runtime import runtime
from ..telegram.media import iter_file_chunks
from .common import mime_for, parse_range

log = logging.getLogger("tgstream.subsonic")

CHUNK = 256 * 1024


async def handle_stream(qp, request, download: bool = False):
    track = await runtime.lib.track_by_id(qp.get("id", ""))
    if not track:
        return PlainTextResponse("Track not found", status_code=404)

    size = int(track["size"] or 0)
    if size <= 0:
        return PlainTextResponse("Unknown file size", status_code=502)

    rng = parse_range(request.headers.get("range"), size)
    if rng is None:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    start, end, is_range = rng
    # Обновляем last_access только при начале проигрывания (start == 0), а не на каждый Range-чанк
    if start == 0:
        await runtime.lib.touch(track["id"])

    content_type = mime_for(track.get("ext"), track.get("mime"))
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
    if is_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    # 1) полный кэш — отдаём с диска (Telegram для этого не нужен)
    cached = Path(track["cached_path"]) if track["cached_path"] else None
    if cached and int(track["cached_size"] or 0) == size and cached.exists():
        return await _disk_response(cached, start, end, is_range, content_type, headers)

    # 2) кэша нет — нужен живой Telegram
    if not runtime.tg or not runtime.tg.ready:
        return PlainTextResponse("Telegram session not ready (run: python -m app.login)", status_code=503)

    # 3) частичный кэш .part продолжаем только при стриме с нулевого смещения
    part = runtime.cache.part_path(track["id"])
    part_len = 0
    if start == 0 and part.exists():
        part_len = part.stat().st_size
        if part_len >= size:  # битый остаток — начнём заново
            part.unlink(missing_ok=True)
            part_len = 0

    lock = runtime.cache.write_lock(track["id"])
    can_cache = start == 0 and not lock.locked()
    if can_cache:
        await lock.acquire()

    msg = await runtime.tg.get_message(int(track["chat_id"]), int(track["msg_id"]))
    if msg is None:
        if can_cache:
            runtime.cache.release_lock(track["id"])
        return PlainTextResponse("Could not fetch message from Telegram", status_code=502)

    async def gen():
        try:
            pos = start
            remaining = end - start + 1

            # префикс из уже скачанной части
            if part_len > 0:
                take = min(part_len, remaining)
                async with aiofiles.open(part, "rb") as f:
                    off = 0
                    while off < take:
                        buf = await f.read(min(CHUNK, take - off))
                        if not buf:
                            break
                        yield buf
                        off += len(buf)
                    pos = off
                    remaining -= off

            write_file = None
            if can_cache and remaining > 0:
                write_file = await aiofiles.open(part, "ab" if part_len else "wb")

            try:
                if remaining > 0:
                    async for chunk in iter_file_chunks(runtime.tg.client, msg, pos):
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                        if write_file:
                            await write_file.write(chunk)
                        yield chunk
                        remaining -= len(chunk)
                        if remaining <= 0:
                            break
            finally:
                if write_file:
                    await write_file.close()

            # дочитали до конца файла — регистрируем готовый кэш
            if can_cache and remaining <= 0 and end == size - 1:
                final = runtime.cache.finalize(track["id"])
                await runtime.lib.update_cached(track["id"], str(final), size)
                asyncio.create_task(runtime.cache.evict(runtime.lib))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Стрим %s оборвался", track["id"])
            raise
        finally:
            if can_cache:
                runtime.cache.release_lock(track["id"])

    return StreamingResponse(gen(), status_code=206 if is_range else 200,
                             media_type=content_type, headers=headers)


async def _disk_response(path, start, end, is_range, content_type, headers):
    length = end - start + 1

    async def gen():
        async with aiofiles.open(path, "rb") as f:
            await f.seek(start)
            left = length
            while left > 0:
                buf = await f.read(min(CHUNK, left))
                if not buf:
                    break
                yield buf
                left -= len(buf)

    return StreamingResponse(gen(), status_code=206 if is_range else 200,
                             media_type=content_type, headers=headers)


@asynccontextmanager
async def _http_client():
    if runtime.http is not None:
        yield runtime.http
        return
    async with httpx.AsyncClient(timeout=8.0) as client:
        yield client


def _resize_cover_url(url: str, small: bool) -> str:
    if small:
        return url.replace("/1000x1000-", "/250x250-").replace("600x600bb.jpg", "300x300bb.jpg")
    return url.replace("/1000x1000-", "/500x500-")


async def _download_cover_url(dl_url: str, cover: Path) -> bool:
    tmp_cover = cover.with_suffix(".jpg.tmp")
    async with _http_client() as client:
        for attempt in range(2):
            try:
                resp = await client.get(dl_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200 and len(resp.content) > 500:
                    tmp_cover.write_bytes(resp.content)
                    tmp_cover.replace(cover)
                    return True
                log.warning("Обложка %s вернула статус %s", dl_url, resp.status_code)
            except Exception:
                log.warning("Ошибка загрузки обложки %s", dl_url)
                tmp_cover.unlink(missing_ok=True)
            if attempt == 0:
                await asyncio.sleep(0.25)
    return False


async def _fetch_tg_thumb(track: dict, dest: Path) -> bool:
    if dest.exists():
        return True
    msg = await runtime.tg.get_message(int(track["chat_id"]), int(track["msg_id"]))
    if msg is None:
        return False
    tmp = dest.with_suffix(".jpg.tmp")
    try:
        await runtime.tg.client.download_media(msg, thumb=-1, file=str(tmp))
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            return True
    except Exception:
        log.exception("Не удалось скачать обложку %s из Telegram", track["id"])
        tmp.unlink(missing_ok=True)
    return False


async def handle_cover_art(qp, request):
    cid = qp.get("id", "")
    if not cid:
        return PlainTextResponse("No cover art", status_code=404)

    try:
        req_size = int(qp.get("size") or 0)
    except (TypeError, ValueError):
        req_size = 0
    is_small = 0 < req_size <= 300

    if cid.startswith("ar-"):
        image_url = await runtime.lib.artist_image_url(cid)
        if not image_url:
            return PlainTextResponse("No cover art", status_code=404)
        cover = runtime.cache.cover_path(f"{cid}_sm" if is_small else cid)
        if not cover.exists():
            if not await _download_cover_url(_resize_cover_url(image_url, is_small), cover):
                return PlainTextResponse("No cover art", status_code=404)
            runtime.cache.trim_covers()
        return FileResponse(cover, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})

    track = await runtime.lib.track_by_id(cid) or await runtime.lib.cover_track_for_album(cid)
    if track is None:
        return PlainTextResponse("No cover art", status_code=404)

    cover_url = track.get("cover_url")
    base_key = track["album_id"] if cover_url else cid
    cover = runtime.cache.cover_path(f"{base_key}_sm" if (is_small and cover_url) else base_key)
    served_tg_fallback = False

    if not cover.exists():
        if cover_url:
            await _download_cover_url(_resize_cover_url(cover_url, is_small), cover)

        if not cover.exists() and track.get("has_thumb"):
            if not runtime.tg or not runtime.tg.ready:
                return PlainTextResponse("Telegram session not ready", status_code=503)
            # При сбое CDN не перезаписываем альбомный кэш (al-*) миниатюрой из MP3
            tg_cover = runtime.cache.cover_path(f"{track['id']}_tg") if cover_url else cover
            if await _fetch_tg_thumb(track, tg_cover):
                cover = tg_cover
                served_tg_fallback = bool(cover_url)

        if not cover.exists():
            return PlainTextResponse("No cover art", status_code=404)
        runtime.cache.trim_covers()

    max_age = 300 if served_tg_fallback else 86400
    return FileResponse(
        cover,
        media_type="image/jpeg",
        headers={"Cache-Control": f"public, max-age={max_age}"},
    )
