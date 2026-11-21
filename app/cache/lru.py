"""LRU-кэш на диске: полностью скачанные треки, недокачанные .part + обложки.

Трек во время стриминга пишется в {id}.part, после полной докачки
переименовывается в {id}.bin и регистрируется в библиотеке. Вытеснение —
сначала брошенные и устаревшие .part фрагменты, затем самые давно слушанные
.bin треки по last_access из библиотеки (с запасом 10%).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Dict

log = logging.getLogger("tgstream.cache")

COVER_CACHE_LIMIT = 500
PART_MAX_AGE_SECONDS = 24 * 3600  # 24 часа для недокачанных .part файлов


class TrackCache:
    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes
        self.tracks_dir = root / "tracks"
        self.covers_dir = root / "covers"
        self.tracks_dir.mkdir(parents=True, exist_ok=True)
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        self._write_locks: Dict[str, asyncio.Lock] = {}

    def write_lock(self, track_id: str) -> asyncio.Lock:
        lock = self._write_locks.get(track_id)
        if lock is None:
            lock = asyncio.Lock()
            self._write_locks[track_id] = lock
        return lock

    def release_lock(self, track_id: str) -> None:
        lock = self._write_locks.get(track_id)
        if lock is not None:
            if lock.locked():
                lock.release()
            self._write_locks.pop(track_id, None)

    def is_locked(self, track_id: str) -> bool:
        lock = self._write_locks.get(track_id)
        return lock is not None and lock.locked()

    def track_path(self, track_id: str) -> Path:
        return self.tracks_dir / f"{track_id}.bin"

    def part_path(self, track_id: str) -> Path:
        return self.tracks_dir / f"{track_id}.part"

    def cover_path(self, key: str) -> Path:
        return self.covers_dir / f"{key}.jpg"

    def finalize(self, track_id: str) -> Path:
        part = self.part_path(track_id)
        final = self.track_path(track_id)
        part.replace(final)
        return final

    def total_size(self) -> int:
        total = 0
        for f in self.tracks_dir.iterdir():
            if f.is_file() and f.suffix in (".bin", ".part"):
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    def trim_stale_parts(self, max_age: int = PART_MAX_AGE_SECONDS) -> int:
        """Удалить .part файлы, к которым давно не обращались и которые не заблокированы записью."""
        now = time.time()
        freed = 0
        for p in self.tracks_dir.glob("*.part"):
            if not p.is_file() or self.is_locked(p.stem):
                continue
            try:
                st = p.stat()
                if now - st.st_mtime > max_age:
                    size = st.st_size
                    p.unlink(missing_ok=True)
                    freed += size
                    log.info("Кэш: удалён устаревший .part %s (-%d байт)", p.name, size)
            except OSError:
                pass
        return freed

    async def evict(self, lib) -> None:
        """Удалить недокачанные .part и старые .bin треки сверх лимита."""
        # 1. Сначала удаляем .part файлы старше 24 часов
        self.trim_stale_parts()

        # 2. Собираем оставшиеся незаблокированные .part файлы (по mtime ASC)
        part_files = []
        for p in self.tracks_dir.glob("*.part"):
            if not p.is_file() or self.is_locked(p.stem):
                continue
            try:
                st = p.stat()
                part_files.append((p, st.st_size, st.st_mtime))
            except OSError:
                pass
        part_files.sort(key=lambda x: x[2])

        # 3. Собираем .bin файлы из библиотеки
        entries = await lib.cached_tracks_lru()
        bin_files = []
        for e in entries:
            p = Path(e["cached_path"])
            try:
                size = p.stat().st_size
                bin_files.append((e["id"], p, size))
            except OSError:
                await lib.clear_cached(e["id"])  # файла нет — почистим запись

        total = sum(item[1] for item in part_files) + sum(item[2] for item in bin_files)
        if total <= self.max_bytes:
            return

        target = int(self.max_bytes * 0.9)
        log.info(
            "Кэш треков (%d байт) превышает лимит (%d байт). Целевой размер: %d байт",
            total, self.max_bytes, target,
        )

        # Вытесняем сначала недокачанные .part файлы (начиная с самых старых)
        for p, size, _ in part_files:
            if total <= target:
                break
            try:
                p.unlink(missing_ok=True)
                total -= size
                log.info("Кэш: вытеснен .part %s (-%d байт)", p.name, size)
            except OSError:
                pass

        # Если лимит всё ещё превышен — вытесняем полные треки .bin (по last_access ASC)
        for tid, path, size in bin_files:
            if total <= target:
                break
            try:
                path.unlink(missing_ok=True)
                await lib.clear_cached(tid)
                total -= size
                log.info("Кэш: вытеснен трек %s (-%d байт)", tid, size)
            except OSError:
                log.warning("Кэш: не удалось удалить %s", path)

    def trim_covers(self) -> None:
        covers = sorted(
            (f for f in self.covers_dir.glob("*.jpg") if f.is_file()),
            key=lambda f: f.stat().st_mtime,
        )
        excess = len(covers) - COVER_CACHE_LIMIT
        for f in covers[: max(excess, 0)]:
            try:
                f.unlink()
            except OSError:
                pass

    def remove_track(self, track_id: str) -> None:
        for p in (self.track_path(track_id), self.part_path(track_id)):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
