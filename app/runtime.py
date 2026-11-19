"""Глобальное состояние приложения (инициализируется в lifespan main.py)."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple

import httpx


class Runtime:
    def __init__(self):
        self.cfg = None          # AppConfig
        self.lib = None          # library.db.Library
        self.tg = None           # telegram.client.TgGateway
        self.cache = None        # cache.lru.TrackCache
        self.http: Optional[httpx.AsyncClient] = None
        self.scanning = False    # идёт ли индексация (для getScanStatus)
        self.scan_task = None    # фоновая задача переиндексации (startScan)
        self.bg_task = None      # стартовая задача (connect + первичный индекс)
        self.cache_task = None   # фоновая периодическая очистка кэша
        self.enrich_task = None  # фоновый воркер обогащения метаданных
        self.enrich_event: Optional[asyncio.Event] = None
        self.now_playing: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def init(self, cfg, lib, cache, tg, http: Optional[httpx.AsyncClient] = None):
        self.cfg = cfg
        self.lib = lib
        self.cache = cache
        self.tg = tg
        self.http = http
        self.scanning = False
        self.scan_task = None
        self.bg_task = None
        self.cache_task = None
        self.enrich_task = None
        self.enrich_event = asyncio.Event()
        self.now_playing = {}

    def trigger_enrich(self) -> None:
        if self.enrich_event is not None:
            self.enrich_event.set()


runtime = Runtime()
