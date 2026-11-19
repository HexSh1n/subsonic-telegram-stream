"""Точка входа: FastAPI-приложение + фон Telegram (connect, индексация, авто-обогащение)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import PROJECT_ROOT, load_config
from .runtime import runtime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tgstream")


async def _background_start():
    """Подключаемся к Telegram и индексируем каналы (не блокируя запуск сервера)."""
    try:
        await runtime.tg.start()
        if not runtime.tg.ready:
            log.warning("Telegram-сессия не готова")
            return

        from .library.indexer import index_channel
        from .library.live import start_live_updates

        start_live_updates(runtime)
        runtime.scanning = True
        try:
            for ref in runtime.cfg.telegram.channels:
                try:
                    chat_id, added = await index_channel(runtime.tg, runtime.lib, ref, full=False)
                    if added > 0:
                        runtime.trigger_enrich()
                except Exception:
                    log.exception("Индексация %s не удалась", ref)
                    continue
            asyncio.create_task(runtime.cache.evict(runtime.lib))
            runtime.trigger_enrich()
        finally:
            runtime.scanning = False
        log.info("Стартовая индексация завершена: %d треков", await runtime.lib.total_tracks())
    except Exception:
        log.exception("Критическая ошибка в _background_start")


async def _enrich_tracks(pending):
    from .metadata.enricher import enrich_track

    log.info("Фоновое обогащение: в очереди %d треков", len(pending))
    for tr in pending:
        enr = await enrich_track(
            tr["artist"],
            tr["title"],
            tr["chat_id"],
            tr.get("channel_title") or "Telegram",
            client=runtime.http,
            duration=int(tr.get("duration") or 0),
        )
        await runtime.lib.save_enrichment(tr["id"], enr)
        await asyncio.sleep(0.15)

    stats = await runtime.lib.harmonize_albums_and_neighbors()
    log.info("Обогащение треков завершено (соседи: +%d, выровнено: %d)",
             stats["inferred_neighbors"], stats["harmonized_tracks"])


async def _enrich_artists(pending):
    from .metadata.enricher import lookup_deezer_artist

    log.info("Фоновое обогащение исполнителей: %d", len(pending))
    for ar in pending:
        res, had_err = await lookup_deezer_artist(runtime.http, ar["name"])
        if res:
            await runtime.lib.save_artist_enrichment(
                ar["artist_id"], ar["name"], res["image_url"], res["deezer_artist_id"], "ok"
            )
        else:
            await runtime.lib.save_artist_enrichment(
                ar["artist_id"], ar["name"], None, 0, "error" if had_err else "not_found"
            )
        await asyncio.sleep(0.15)
    log.info("Обогащение исполнителей завершено")


async def _enrichment_worker():
    while True:
        try:
            if runtime.lib is None:
                await asyncio.sleep(5)
                continue

            pending = await runtime.lib.pending_enrichment_tracks(limit=100)
            pending_artists = await runtime.lib.pending_artist_enrichment(limit=100)
            if not pending and not pending_artists:
                if runtime.enrich_event is not None:
                    runtime.enrich_event.clear()
                    try:
                        await asyncio.wait_for(runtime.enrich_event.wait(), timeout=300.0)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(60)
                continue

            if pending:
                await _enrich_tracks(pending)
            if pending_artists:
                await _enrich_artists(pending_artists)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Ошибка в фоновом воркере обогащения")
            await asyncio.sleep(15)


async def _cache_janitor():
    """Фоновая периодическая очистка кэша (каждые 30 минут)."""
    while True:
        try:
            await asyncio.sleep(1800)
            if runtime.lib and runtime.cache:
                await runtime.cache.evict(runtime.lib)
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("Ошибка в фоновой очистке кэша")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .cache.lru import TrackCache
    from .library.db import Library
    from .telegram.client import TgGateway

    cfg = load_config()
    PROJECT_ROOT.joinpath("data").mkdir(exist_ok=True)
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    lib = Library(cfg.db_path)
    await lib.init()
    http_client = httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "tg-stream/1.0"})
    runtime.init(cfg, lib, TrackCache(cfg.cache_dir, cfg.cache.max_bytes), TgGateway(cfg), http=http_client)
    runtime.bg_task = asyncio.create_task(_background_start())
    runtime.cache_task = asyncio.create_task(_cache_janitor())
    runtime.enrich_task = asyncio.create_task(_enrichment_worker())
    log.info("tg-stream запущен: http://%s:%d (Subsonic API: /rest/)",
             cfg.server.host, cfg.server.port)
    yield

    for task in (runtime.bg_task, runtime.scan_task, runtime.cache_task, runtime.enrich_task):
        if task:
            task.cancel()
    if runtime.http:
        await runtime.http.aclose()
    await runtime.tg.stop()
    await lib.close()


app = FastAPI(title="tg-stream", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    return {"service": "tg-stream",
            "subsonic": "/rest/ping?f=json&u=<user>&t=<token>&s=<salt>"}


def _register_router() -> None:
    from .subsonic.router import router
    app.include_router(router)


_register_router()


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")
