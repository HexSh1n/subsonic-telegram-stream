"""SQLite-библиотека: треки, каналы, история прослушиваний, избранное."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import aiosqlite

from ..metadata.enricher import (
    _normalize,
    album_id_for,
    build_search_text,
    clean_album_title,
    extract_primary_artist,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
  chat_id      INTEGER PRIMARY KEY,
  title        TEXT NOT NULL,
  username     TEXT,
  access_hash  INTEGER
);
CREATE TABLE IF NOT EXISTS tracks(
  id              TEXT PRIMARY KEY,   -- "tr-{chat_id}-{msg_id}"
  chat_id         INTEGER NOT NULL,
  msg_id          INTEGER NOT NULL,
  title           TEXT NOT NULL,
  artist          TEXT NOT NULL,
  album_artist    TEXT NOT NULL DEFAULT '',
  artist_id       TEXT NOT NULL,
  album           TEXT NOT NULL,
  album_id        TEXT NOT NULL,
  duration        INTEGER NOT NULL DEFAULT 0,   -- секунды
  size            INTEGER NOT NULL DEFAULT 0,   -- байты
  mime            TEXT,
  ext             TEXT,
  has_thumb       INTEGER NOT NULL DEFAULT 0,
  posted_at       INTEGER NOT NULL DEFAULT 0,
  cached_path     TEXT,
  cached_size     INTEGER NOT NULL DEFAULT 0,
  last_access     INTEGER NOT NULL DEFAULT 0,
  track_number    INTEGER NOT NULL DEFAULT 0,
  disc_number     INTEGER NOT NULL DEFAULT 1,
  year            INTEGER NOT NULL DEFAULT 0,
  genre           TEXT,
  cover_url       TEXT,
  raw_artist      TEXT NOT NULL DEFAULT '',
  raw_title       TEXT NOT NULL DEFAULT '',
  deezer_track_id INTEGER NOT NULL DEFAULT 0,
  deezer_album_id INTEGER NOT NULL DEFAULT 0,
  enrich_status   TEXT NOT NULL DEFAULT 'pending',
  enriched_at     INTEGER NOT NULL DEFAULT 0,
  search_text     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist_id);
CREATE INDEX IF NOT EXISTS idx_tracks_album  ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_chat   ON tracks(chat_id);
CREATE INDEX IF NOT EXISTS idx_tracks_posted ON tracks(posted_at);
CREATE TABLE IF NOT EXISTS meta(
  chat_id      INTEGER PRIMARY KEY,
  last_msg_id  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plays(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  track_id     TEXT NOT NULL,
  played_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plays_track ON plays(track_id);
CREATE TABLE IF NOT EXISTS starred(
  item_id      TEXT PRIMARY KEY,
  item_type    TEXT NOT NULL,   -- 'song' | 'album' | 'artist'
  starred_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artists(
  artist_id        TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  image_url        TEXT,
  deezer_artist_id INTEGER NOT NULL DEFAULT 0,
  enrich_status    TEXT NOT NULL DEFAULT 'pending',
  enriched_at      INTEGER NOT NULL DEFAULT 0,
  biography        TEXT NOT NULL DEFAULT '',
  similar_json     TEXT NOT NULL DEFAULT '[]',
  info_fetched_at  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS play_queue(
  username   TEXT PRIMARY KEY,
  track_ids  TEXT NOT NULL DEFAULT '[]',
  current_id TEXT NOT NULL DEFAULT '',
  position   INTEGER NOT NULL DEFAULT 0,
  changed_by TEXT NOT NULL DEFAULT '',
  changed_at INTEGER NOT NULL DEFAULT 0
);
"""

_TRACK_COLS = """t.id, t.chat_id, t.msg_id, t.title, t.artist, t.album_artist, t.artist_id,
t.album, t.album_id, t.duration, t.size, t.mime, t.ext, t.has_thumb, t.posted_at,
t.cached_path, t.cached_size, t.last_access, t.track_number, t.disc_number,
t.year, t.genre, t.cover_url, t.enrich_status, c.title AS channel_title,
COALESCE(pl.play_count, 0) AS play_count,
COALESCE(pl.last_played, 0) AS last_played,
st.starred_at AS starred_at"""

_PLAYS_JOIN = """
LEFT JOIN (
    SELECT track_id, COUNT(*) AS play_count, MAX(played_at) AS last_played
    FROM plays GROUP BY track_id
) pl ON pl.track_id = t.id
LEFT JOIN starred st ON st.item_id = t.id
"""

_ALBUM_SELECT_BASE = """
SELECT t.album_id,
       t.album,
       COALESCE(NULLIF(MAX(t.album_artist), ''), MAX(t.artist)) AS artist,
       t.artist_id,
       COUNT(*) AS song_count,
       SUM(t.duration) AS dur,
       MIN(t.posted_at) AS created,
       MAX(t.posted_at) AS newest,
       MAX(CASE WHEN t.year > 0 THEN t.year ELSE 0 END) AS year,
       MAX(NULLIF(t.genre, '')) AS genre,
       COALESCE(SUM(pl.play_count), 0) AS play_count,
       MAX(pl.last_played) AS last_played,
       st_alb.starred_at AS starred_at
FROM tracks t
LEFT JOIN (
    SELECT track_id, COUNT(*) AS play_count, MAX(played_at) AS last_played
    FROM plays GROUP BY track_id
) pl ON pl.track_id = t.id
LEFT JOIN starred st_alb ON st_alb.item_id = t.album_id
"""


def _track_select(where: str = "") -> str:
    sql = (
        f"SELECT {_TRACK_COLS} FROM tracks t "
        f"LEFT JOIN channels c ON c.chat_id = t.chat_id "
        f"{_PLAYS_JOIN}"
    )
    return sql + (f" WHERE {where}" if where else "")


_ARTIST_COLS = """t.artist_id,
COALESCE(NULLIF(MAX(t.album_artist), ''), MAX(t.artist)) AS artist,
COUNT(DISTINCT t.album_id) AS album_count,
st.starred_at AS starred_at,
MAX(ar.image_url) AS image_url"""


def _parse_json_list(raw: Optional[str]) -> List[str]:
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else []
    except Exception:
        return []


class Library:
    def __init__(self, path: Path):
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Добавить новые колонки и индексы в существующую базу, если их ещё нет."""
        assert self._db is not None
        async with self._db.execute("PRAGMA table_info(tracks)") as cur:
            existing = {row["name"] for row in await cur.fetchall()}
        columns_to_add = [
            ("track_number", "INTEGER NOT NULL DEFAULT 0"),
            ("disc_number", "INTEGER NOT NULL DEFAULT 1"),
            ("year", "INTEGER NOT NULL DEFAULT 0"),
            ("genre", "TEXT"),
            ("cover_url", "TEXT"),
            ("album_artist", "TEXT NOT NULL DEFAULT ''"),
            ("raw_artist", "TEXT NOT NULL DEFAULT ''"),
            ("raw_title", "TEXT NOT NULL DEFAULT ''"),
            ("deezer_track_id", "INTEGER NOT NULL DEFAULT 0"),
            ("deezer_album_id", "INTEGER NOT NULL DEFAULT 0"),
            ("enrich_status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("enriched_at", "INTEGER NOT NULL DEFAULT 0"),
            ("search_text", "TEXT NOT NULL DEFAULT ''"),
        ]
        for name, col_def in columns_to_add:
            if name not in existing:
                await self._db.execute(f"ALTER TABLE tracks ADD COLUMN {name} {col_def}")

        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_tracks_enrich ON tracks(enrich_status)")

        # Переносим старый маркер year = -1 в enrich_status = 'not_found'
        await self._db.execute(
            "UPDATE tracks SET enrich_status = 'not_found', year = 0 WHERE year = -1"
        )
        await self._db.execute(
            "UPDATE tracks SET enrich_status = 'ok' "
            "WHERE enrich_status = 'pending' AND cover_url IS NOT NULL AND cover_url != ''"
        )

        async with self._db.execute("PRAGMA table_info(artists)") as cur:
            artist_cols = {row["name"] for row in await cur.fetchall()}
        for name, col_def in [
            ("biography", "TEXT NOT NULL DEFAULT ''"),
            ("similar_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("info_fetched_at", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if name not in artist_cols:
                await self._db.execute(f"ALTER TABLE artists ADD COLUMN {name} {col_def}")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # ── низкоуровневые помощники ──────────────────────────────────────────

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    # ── каналы и мета индексации ──────────────────────────────────────────

    async def upsert_channel(self, chat_id: int, title: str, username: Optional[str], access_hash: Optional[int]) -> None:
        await self._db.execute(
            "INSERT INTO channels(chat_id, title, username, access_hash) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, username=excluded.username, access_hash=excluded.access_hash",
            (chat_id, title, username, access_hash),
        )
        await self._db.commit()

    async def channels(self) -> List[Dict[str, Any]]:
        return await self._fetchall("SELECT * FROM channels ORDER BY title COLLATE NOCASE")

    async def channel_access_hash(self, chat_id: int) -> Optional[int]:
        row = await self._fetchone("SELECT access_hash FROM channels WHERE chat_id = ?", (chat_id,))
        return row["access_hash"] if row else None

    async def last_msg_id(self, chat_id: int) -> int:
        row = await self._fetchone("SELECT last_msg_id FROM meta WHERE chat_id = ?", (chat_id,))
        return row["last_msg_id"] if row else 0

    async def set_last_msg_id(self, chat_id: int, msg_id: int) -> None:
        await self._db.execute(
            "INSERT INTO meta(chat_id, last_msg_id) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_msg_id=excluded.last_msg_id",
            (chat_id, msg_id),
        )
        await self._db.commit()

    # ── запись треков (индексер и обогащение) ─────────────────────────────

    async def upsert_tracks(self, rows: List[Dict[str, Any]]) -> None:
        """Вставить новые треки или обновить существующие, НЕ затирая уже обогащённые метаданные."""
        prepared = []
        for r in rows:
            d = dict(r)
            d.setdefault("album_artist", extract_primary_artist(d.get("artist") or ""))
            d.setdefault("raw_artist", d.get("artist") or "")
            d.setdefault("raw_title", d.get("title") or "")
            d.setdefault("track_number", 0)
            d.setdefault("disc_number", 1)
            d.setdefault("year", 0)
            d.setdefault("genre", "")
            d.setdefault("cover_url", None)
            d.setdefault("deezer_track_id", 0)
            d.setdefault("deezer_album_id", 0)
            d.setdefault("enrich_status", "pending")
            d.setdefault("enriched_at", int(time.time()) if d["enrich_status"] != "pending" else 0)
            d.setdefault(
                "search_text",
                build_search_text(d["artist"], d["album_artist"], d["title"], d["album"]),
            )
            prepared.append(d)

        await self._db.executemany(
            """INSERT INTO tracks(
                 id, chat_id, msg_id, title, artist, album_artist, artist_id, album, album_id,
                 duration, size, mime, ext, has_thumb, posted_at,
                 track_number, disc_number, year, genre, cover_url,
                 raw_artist, raw_title, deezer_track_id, deezer_album_id,
                 enrich_status, enriched_at, search_text
               )
               VALUES(
                 :id, :chat_id, :msg_id, :title, :artist, :album_artist, :artist_id, :album, :album_id,
                 :duration, :size, :mime, :ext, :has_thumb, :posted_at,
                 :track_number, :disc_number, :year, :genre, :cover_url,
                 :raw_artist, :raw_title, :deezer_track_id, :deezer_album_id,
                 :enrich_status, :enriched_at, :search_text
               )
               ON CONFLICT(id) DO UPDATE SET
                 duration = excluded.duration,
                 size = excluded.size,
                 mime = excluded.mime,
                 ext = excluded.ext,
                 has_thumb = excluded.has_thumb,
                 posted_at = excluded.posted_at,
                 raw_artist = CASE WHEN excluded.raw_artist != '' THEN excluded.raw_artist ELSE tracks.raw_artist END,
                 raw_title = CASE WHEN excluded.raw_title != '' THEN excluded.raw_title ELSE tracks.raw_title END,
                 title = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.title ELSE excluded.title END,
                 artist = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.artist ELSE excluded.artist END,
                 album_artist = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.album_artist ELSE excluded.album_artist END,
                 artist_id = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.artist_id ELSE excluded.artist_id END,
                 album = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.album ELSE excluded.album END,
                 album_id = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.album_id ELSE excluded.album_id END,
                 track_number = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.track_number ELSE excluded.track_number END,
                 disc_number = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.disc_number ELSE excluded.disc_number END,
                 year = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.year ELSE excluded.year END,
                 genre = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.genre ELSE excluded.genre END,
                 cover_url = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.cover_url ELSE excluded.cover_url END,
                 deezer_track_id = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.deezer_track_id ELSE excluded.deezer_track_id END,
                 deezer_album_id = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.deezer_album_id ELSE excluded.deezer_album_id END,
                 enrich_status = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.enrich_status ELSE excluded.enrich_status END,
                 enriched_at = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.enriched_at ELSE excluded.enriched_at END,
                 search_text = CASE WHEN tracks.enrich_status = 'ok' AND excluded.enrich_status = 'pending' THEN tracks.search_text ELSE excluded.search_text END
            """,
            prepared,
        )
        await self._db.commit()

    async def save_enrichment(self, track_id: str, enr: Dict[str, Any]) -> None:
        """Сохранить результат enrich_track для одного трека."""
        await self._db.execute(
            """UPDATE tracks SET
                 title = ?, artist = ?, album_artist = ?, artist_id = ?,
                 album = ?, album_id = ?, track_number = ?, disc_number = ?,
                 year = ?, genre = ?, cover_url = ?,
                 deezer_track_id = ?, deezer_album_id = ?,
                 enrich_status = ?, enriched_at = ?, search_text = ?
               WHERE id = ?""",
            (
                enr["title"],
                enr["artist"],
                enr["album_artist"],
                enr["artist_id"],
                enr["album"],
                enr["album_id"],
                int(enr.get("track_number") or 0),
                int(enr.get("disc_number") or 1),
                int(enr.get("year") or 0),
                enr.get("genre") or "",
                enr.get("cover_url"),
                int(enr.get("deezer_track_id") or 0),
                int(enr.get("deezer_album_id") or 0),
                enr.get("enrich_status") or "ok",
                int(time.time()),
                enr.get("search_text") or "",
                track_id,
            ),
        )
        await self._db.commit()

    async def pending_enrichment_tracks(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Треки, ожидающие обогащения (pending или старый error)."""
        retry_before = int(time.time()) - 3600
        return await self._fetchall(
            """SELECT t.id, t.chat_id, t.msg_id, t.title, t.artist, t.duration,
                      c.title AS channel_title
               FROM tracks t
               LEFT JOIN channels c ON c.chat_id = t.chat_id
               WHERE t.enrich_status = 'pending'
                  OR (t.enrich_status = 'error' AND t.enriched_at < ?)
               ORDER BY t.posted_at DESC
               LIMIT ?""",
            (retry_before, limit),
        )

    async def pending_artist_enrichment(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Исполнители, у которых ещё не загружена обложка."""
        retry_before = int(time.time()) - 3600
        return await self._fetchall(
            """SELECT t.artist_id,
                      COALESCE(NULLIF(MAX(t.album_artist), ''), MAX(t.artist)) AS name
               FROM tracks t
               LEFT JOIN artists a ON a.artist_id = t.artist_id
               WHERE a.artist_id IS NULL
                  OR a.enrich_status = 'pending'
                  OR (a.enrich_status = 'error' AND a.enriched_at < ?)
               GROUP BY t.artist_id
               LIMIT ?""",
            (retry_before, limit),
        )

    async def save_artist_enrichment(
        self,
        artist_id: str,
        name: str,
        image_url: Optional[str],
        deezer_artist_id: int,
        status: str,
    ) -> None:
        await self._db.execute(
            """INSERT INTO artists(artist_id, name, image_url, deezer_artist_id, enrich_status, enriched_at)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(artist_id) DO UPDATE SET
                 name = excluded.name,
                 image_url = COALESCE(excluded.image_url, artists.image_url),
                 deezer_artist_id = CASE WHEN excluded.deezer_artist_id > 0 THEN excluded.deezer_artist_id ELSE artists.deezer_artist_id END,
                 enrich_status = excluded.enrich_status,
                 enriched_at = excluded.enriched_at""",
            (artist_id, name, image_url, deezer_artist_id, status, int(time.time())),
        )
        await self._db.commit()

    async def artist_image_url(self, artist_id: str) -> Optional[str]:
        row = await self._fetchone(
            "SELECT image_url FROM artists WHERE artist_id = ?", (artist_id,)
        )
        return row["image_url"] if row and row["image_url"] else None

    async def update_artist_info(
        self,
        artist_id: str,
        name: str,
        biography: str,
        similar: List[Dict[str, Any]],
        deezer_artist_id: int = 0,
    ) -> None:
        now = int(time.time())
        sim_json = json.dumps(similar, ensure_ascii=False)
        await self._db.execute(
            """INSERT INTO artists(artist_id, name, deezer_artist_id, biography, similar_json, info_fetched_at)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(artist_id) DO UPDATE SET
                 biography = excluded.biography,
                 similar_json = excluded.similar_json,
                 info_fetched_at = excluded.info_fetched_at,
                 deezer_artist_id = CASE WHEN excluded.deezer_artist_id > 0 THEN excluded.deezer_artist_id ELSE artists.deezer_artist_id END""",
            (artist_id, name, deezer_artist_id, biography, sim_json, now),
        )
        await self._db.commit()

    # ── синхронизация очереди воспроизведения (playQueue) ──────────────────

    async def save_play_queue(
        self,
        username: str,
        track_ids: List[str],
        current_id: str,
        position: int,
        changed_by: str,
    ) -> None:
        await self._db.execute(
            """INSERT INTO play_queue(username, track_ids, current_id, position, changed_by, changed_at)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET
                 track_ids = excluded.track_ids,
                 current_id = excluded.current_id,
                 position = excluded.position,
                 changed_by = excluded.changed_by,
                 changed_at = excluded.changed_at""",
            (username, json.dumps(track_ids), current_id, position, changed_by, int(time.time())),
        )
        await self._db.commit()

    async def get_play_queue(self, username: str) -> Optional[Dict[str, Any]]:
        row = await self._fetchone(
            "SELECT username, track_ids, current_id, position, changed_by, changed_at "
            "FROM play_queue WHERE username = ?",
            (username,),
        )
        if not row:
            return None
        track_ids = _parse_json_list(row["track_ids"])
        if not track_ids:
            return None
        placeholders = ",".join("?" for _ in track_ids)
        rows = await self._fetchall(
            _track_select(f"t.id IN ({placeholders})"),
            tuple(track_ids),
        )
        by_id = {r["id"]: r for r in rows}
        ordered = [by_id[tid] for tid in track_ids if tid in by_id]
        if not ordered:
            return None
        return {
            "username": row["username"],
            "current_id": row["current_id"] or ordered[0]["id"],
            "position": int(row["position"] or 0),
            "changed_by": row["changed_by"] or "",
            "changed_at": int(row["changed_at"] or 0),
            "tracks": ordered,
        }

    async def sync_play_queue_from_track(
        self, username: str, track_id: str, changed_by: str
    ) -> None:
        """Автоматически обновляет play_queue при старте трека на любом клиенте."""
        row = await self._fetchone(
            "SELECT track_ids FROM play_queue WHERE username = ?", (username,)
        )
        existing_ids = _parse_json_list(row["track_ids"]) if row else []

        if track_id in existing_ids:
            await self.save_play_queue(username, existing_ids, track_id, 0, changed_by)
            return

        track = await self.track_by_id(track_id)
        if not track:
            return
        ids = (existing_ids + [track_id])[-100:]
        await self.save_play_queue(username, ids, track_id, 0, changed_by)

    async def harmonize_albums_and_neighbors(self) -> Dict[str, int]:
        """Пост-обработка метаданных:
        1) Склеивает Deluxe / Remastered / Anniversary-версии одного и того же альбома.
        2) Подтягивает треки-синглы (1–2 трека) и необогащённые скиты, залитые внутри пачки одного альбома.
        3) Выравнивает название, год, жанр и обложку внутри каждого album_id.
        """
        assert self._db is not None
        inferred = 0

        all_rows = await self._fetchall(
            """SELECT id, chat_id, msg_id, posted_at, artist, album_artist, artist_id, title,
                      album, album_id, year, genre, cover_url, track_number, disc_number, enrich_status
               FROM tracks ORDER BY chat_id, msg_id ASC"""
        )

        # 0. Приводим названия альбомов к базовым (без Deluxe / Remastered / Anniversary)
        for r in all_rows:
            if r["enrich_status"] == "ok" and not r["album"].endswith(" — Синглы"):
                base_alb = clean_album_title(r["album"])
                new_alb_id = album_id_for(r["album_artist"] or r["artist"], base_alb)
                if base_alb != r["album"] or new_alb_id != r["album_id"]:
                    stext = build_search_text(r["artist"], r["album_artist"], r["title"], base_alb)
                    await self._db.execute(
                        "UPDATE tracks SET album = ?, album_id = ?, search_text = ? WHERE id = ?",
                        (base_alb, new_alb_id, stext, r["id"]),
                    )
                    r["album"] = base_alb
                    r["album_id"] = new_alb_id

        alb_counts = Counter(r["album_id"] for r in all_rows)

        # 1. Контекст соседних сообщений в канале (включая одиночные синглы внутри заливки альбома)
        by_chat: Dict[int, List[Dict[str, Any]]] = {}
        for r in all_rows:
            by_chat.setdefault(r["chat_id"], []).append(r)

        for chat_id, items in by_chat.items():
            n = len(items)
            for i, row in enumerate(items):
                cur_count = alb_counts.get(row["album_id"], 0)
                # Не перезаписываем уже обогащённые треки и не разрушаем многотрековые альбомы
                if row["enrich_status"] == "ok":
                    continue
                if cur_count >= 2 and _normalize(row["album"]) != _normalize(row["title"]):
                    continue

                neighbors_before = [
                    items[j] for j in range(max(0, i - 4), i)
                    if items[j]["artist_id"] == row["artist_id"]
                    and items[j]["enrich_status"] == "ok"
                    and alb_counts.get(items[j]["album_id"], 0) >= 4
                    and abs(items[j]["msg_id"] - row["msg_id"]) <= 5
                ]
                neighbors_after = [
                    items[j] for j in range(i + 1, min(n, i + 5))
                    if items[j]["artist_id"] == row["artist_id"]
                    and items[j]["enrich_status"] == "ok"
                    and alb_counts.get(items[j]["album_id"], 0) >= 4
                    and abs(items[j]["msg_id"] - row["msg_id"]) <= 5
                ]
                cand_albums = [nb["album_id"] for nb in (neighbors_before + neighbors_after)]
                if not cand_albums:
                    continue

                top_alb_id, count = Counter(cand_albums).most_common(1)[0]
                if top_alb_id == row["album_id"]:
                    continue

                before_ids = {nb["album_id"] for nb in neighbors_before}
                after_ids = {nb["album_id"] for nb in neighbors_after}
                is_sandwiched = (top_alb_id in before_ids and top_alb_id in after_ids)

                donor = next(nb for nb in (neighbors_before + neighbors_after) if nb["album_id"] == top_alb_id)
                same_upload_batch = (
                    abs(int(row.get("posted_at") or 0) - int(donor.get("posted_at") or 0)) <= 600
                    and (row["year"] <= 0 or donor["year"] <= 0 or row["year"] <= donor["year"] + 1)
                )

                if is_sandwiched or (count >= 2 and (row["enrich_status"] != "ok" or same_upload_batch)):
                    inferred_track_no = row["track_number"]
                    if (inferred_track_no <= 1 or row["enrich_status"] != "ok") and neighbors_before:
                        prev_nb = neighbors_before[-1]
                        if prev_nb["album_id"] == top_alb_id and prev_nb["track_number"] > 0 and prev_nb["msg_id"] == row["msg_id"] - 1:
                            inferred_track_no = prev_nb["track_number"] + 1
                    stext = build_search_text(row["artist"], row["album_artist"], row["title"], donor["album"])
                    await self._db.execute(
                        """UPDATE tracks SET
                             album = ?, album_id = ?, year = ?, genre = ?, cover_url = ?,
                             track_number = ?, disc_number = ?, enrich_status = 'ok', search_text = ?
                           WHERE id = ?""",
                        (
                            donor["album"],
                            donor["album_id"],
                            donor["year"],
                            donor["genre"],
                            donor["cover_url"],
                            inferred_track_no,
                            donor["disc_number"],
                            stext,
                            row["id"],
                        ),
                    )
                    alb_counts[row["album_id"]] = max(0, alb_counts.get(row["album_id"], 1) - 1)
                    alb_counts[donor["album_id"]] += 1
                    row["album"] = donor["album"]
                    row["album_id"] = donor["album_id"]
                    row["year"] = donor["year"]
                    row["genre"] = donor["genre"]
                    row["cover_url"] = donor["cover_url"]
                    row["track_number"] = inferred_track_no
                    row["enrich_status"] = "ok"
                    inferred += 1

        # 2. Выравнивание года, жанра и обложки внутри каждого album_id
        alb_rows = await self._fetchall(
            "SELECT id, album_id, year, genre, cover_url FROM tracks WHERE enrich_status = 'ok'"
        )
        by_album: Dict[str, List[Dict[str, Any]]] = {}
        for r in alb_rows:
            by_album.setdefault(r["album_id"], []).append(r)

        harmonized = 0
        for alb_id, tracks_in_alb in by_album.items():
            years = [t["year"] for t in tracks_in_alb if (t["year"] or 0) > 0]
            genres = [t["genre"] for t in tracks_in_alb if t.get("genre")]
            covers = [t["cover_url"] for t in tracks_in_alb if t.get("cover_url")]

            # Для года берём наиболее частый (а при равенстве — самый ранний год оригинального релиза)
            best_year = 0
            if years:
                counts = Counter(years)
                max_c = max(counts.values())
                best_year = min(y for y, c in counts.items() if c == max_c)
            best_genre = Counter(genres).most_common(1)[0][0] if genres else ""
            best_cover = Counter(covers).most_common(1)[0][0] if covers else None

            for t in tracks_in_alb:
                need_upd = (
                    (best_year and t["year"] != best_year)
                    or (best_genre and not t.get("genre"))
                    or (best_cover and not t.get("cover_url"))
                )
                if need_upd:
                    await self._db.execute(
                        """UPDATE tracks SET
                             year = CASE WHEN ? > 0 THEN ? ELSE year END,
                             genre = CASE WHEN (genre IS NULL OR genre = '') AND ? != '' THEN ? ELSE genre END,
                             cover_url = COALESCE(NULLIF(cover_url, ''), ?)
                           WHERE id = ?""",
                        (best_year, best_year, best_genre, best_genre, best_cover, t["id"]),
                    )
                    harmonized += 1

        await self._db.commit()
        return {"inferred_neighbors": inferred, "harmonized_tracks": harmonized}

    # ── статистика и треки ────────────────────────────────────────────────

    async def total_tracks(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS n FROM tracks")
        return row["n"] if row else 0

    async def track_by_id(self, track_id: str) -> Optional[Dict[str, Any]]:
        return await self._fetchone(_track_select("t.id = ?"), (track_id,))

    async def touch(self, track_id: str) -> None:
        await self._db.execute(
            "UPDATE tracks SET last_access = ? WHERE id = ?", (int(time.time()), track_id)
        )
        await self._db.commit()

    async def update_cached(self, track_id: str, path: str, size: int) -> None:
        await self._db.execute(
            "UPDATE tracks SET cached_path = ?, cached_size = ? WHERE id = ?", (path, size, track_id)
        )
        await self._db.commit()

    async def clear_cached(self, track_id: str) -> None:
        await self._db.execute(
            "UPDATE tracks SET cached_path = NULL, cached_size = 0 WHERE id = ?", (track_id,)
        )
        await self._db.commit()

    async def cached_tracks_lru(self) -> List[Dict[str, Any]]:
        return await self._fetchall(
            "SELECT id, cached_path, cached_size FROM tracks "
            "WHERE cached_path IS NOT NULL ORDER BY last_access ASC"
        )

    async def add_play(self, track_id: str) -> None:
        await self._db.execute(
            "INSERT INTO plays(track_id, played_at) VALUES(?,?)", (track_id, int(time.time()))
        )
        await self._db.commit()

    # ── избранное (star / unstar / getStarred) ────────────────────────────

    async def star_item(self, item_id: str, item_type: str) -> None:
        await self._db.execute(
            "INSERT INTO starred(item_id, item_type, starred_at) VALUES(?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET starred_at=excluded.starred_at",
            (item_id, item_type, int(time.time())),
        )
        await self._db.commit()

    async def unstar_item(self, item_id: str) -> None:
        await self._db.execute("DELETE FROM starred WHERE item_id = ?", (item_id,))
        await self._db.commit()

    async def get_starred_all(self) -> Dict[str, List[Dict[str, Any]]]:
        artists = await self._fetchall(
            f"""SELECT {_ARTIST_COLS}
                FROM starred st
                JOIN tracks t ON t.artist_id = st.item_id
                LEFT JOIN artists ar ON ar.artist_id = t.artist_id
                GROUP BY t.artist_id
                ORDER BY st.starred_at DESC"""
        )
        albums = await self._fetchall(
            _ALBUM_SELECT_BASE
            + " WHERE st_alb.starred_at IS NOT NULL GROUP BY t.album_id ORDER BY st_alb.starred_at DESC"
        )
        songs = await self._fetchall(
            _track_select("st.starred_at IS NOT NULL") + " ORDER BY st.starred_at DESC"
        )
        return {"artists": artists, "albums": albums, "songs": songs}

    # ── исполнители/альбомы (ID3-навигация) ───────────────────────────────

    async def artists(self, chat_id: Optional[int] = None) -> List[Dict[str, Any]]:
        where, params = ("WHERE t.chat_id = ?", (chat_id,)) if chat_id is not None else ("", ())
        return await self._fetchall(
            f"""SELECT {_ARTIST_COLS}
                FROM tracks t
                LEFT JOIN starred st ON st.item_id = t.artist_id
                LEFT JOIN artists ar ON ar.artist_id = t.artist_id
                {where}
                GROUP BY t.artist_id
                ORDER BY artist COLLATE NOCASE""",
            params,
        )

    async def artist_by_id(self, artist_id: str) -> Optional[Dict[str, Any]]:
        return await self._fetchone(
            f"""SELECT {_ARTIST_COLS},
                       COALESCE(MAX(ar.deezer_artist_id), 0) AS deezer_artist_id,
                       COALESCE(MAX(ar.biography), '') AS biography,
                       COALESCE(MAX(ar.similar_json), '[]') AS similar_json,
                       COALESCE(MAX(ar.info_fetched_at), 0) AS info_fetched_at
                FROM tracks t
                LEFT JOIN starred st ON st.item_id = t.artist_id
                LEFT JOIN artists ar ON ar.artist_id = t.artist_id
                WHERE t.artist_id = ?
                GROUP BY t.artist_id""",
            (artist_id,),
        )

    async def albums_of_artist(self, artist_id: str) -> List[Dict[str, Any]]:
        return await self._fetchall(
            _ALBUM_SELECT_BASE
            + """ WHERE t.artist_id = ?
                  GROUP BY t.album_id
                  ORDER BY (CASE WHEN MAX(t.year) > 0 THEN 0 ELSE 1 END) ASC,
                           MAX(t.year) DESC, MIN(t.posted_at) DESC""",
            (artist_id,),
        )

    async def album_by_id(self, album_id: str) -> Optional[Dict[str, Any]]:
        return await self._fetchone(
            _ALBUM_SELECT_BASE + " WHERE t.album_id = ? GROUP BY t.album_id",
            (album_id,),
        )

    async def album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        rows = await self._fetchall(
            _track_select("t.album_id = ?")
            + " ORDER BY t.disc_number ASC, "
            "(CASE WHEN t.track_number > 0 THEN t.track_number ELSE 9999 END) ASC, "
            "t.posted_at ASC, t.msg_id ASC",
            (album_id,),
        )
        # Скрываем полные дубли треков внутри одного альбома (оставляем лучший размер/кэш и суммируем прослушивания)
        deduped: List[Dict[str, Any]] = []
        seen_idx: Dict[str, int] = {}
        for r in rows:
            norm_key = f"{r.get('disc_number', 1)}:{r.get('track_number', 0)}:{_normalize(r['title'])}"
            if norm_key in seen_idx:
                idx = seen_idx[norm_key]
                prev = deduped[idx]
                if abs(int(prev.get("duration") or 0) - int(r.get("duration") or 0)) <= 5:
                    total_plays = int(prev.get("play_count") or 0) + int(r.get("play_count") or 0)
                    max_played = max(int(prev.get("last_played") or 0), int(r.get("last_played") or 0))
                    # Если новый трек закэширован или больше по размеру — берём его за основу
                    if (r.get("cached_path") and not prev.get("cached_path")) or int(r.get("size") or 0) > int(prev.get("size") or 0):
                        r["play_count"] = total_plays
                        r["last_played"] = max_played
                        deduped[idx] = r
                    else:
                        prev["play_count"] = total_plays
                        prev["last_played"] = max_played
                    continue
            seen_idx[norm_key] = len(deduped)
            deduped.append(r)
        return deduped

    async def album_list(
        self,
        kind: str,
        size: int,
        offset: int,
        from_year: Optional[int] = None,
        to_year: Optional[int] = None,
        genre: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if kind == "frequent":
            return await self._fetchall(
                _ALBUM_SELECT_BASE
                + " GROUP BY t.album_id HAVING COALESCE(SUM(pl.play_count), 0) > 0 "
                "ORDER BY play_count DESC, last_played DESC LIMIT ? OFFSET ?",
                (size, offset),
            )
        if kind == "recent":
            return await self._fetchall(
                _ALBUM_SELECT_BASE
                + " GROUP BY t.album_id HAVING COALESCE(MAX(pl.last_played), 0) > 0 "
                "ORDER BY last_played DESC LIMIT ? OFFSET ?",
                (size, offset),
            )
        if kind == "starred":
            return await self._fetchall(
                _ALBUM_SELECT_BASE
                + " WHERE st_alb.starred_at IS NOT NULL GROUP BY t.album_id "
                "ORDER BY st_alb.starred_at DESC LIMIT ? OFFSET ?",
                (size, offset),
            )
        if kind == "byYear":
            fy = int(from_year if from_year is not None else 0)
            ty = int(to_year if to_year is not None else 9999)
            lo, hi = min(fy, ty), max(fy, ty)
            direction = "ASC" if fy <= ty else "DESC"
            return await self._fetchall(
                _ALBUM_SELECT_BASE
                + f" WHERE t.year BETWEEN ? AND ? AND t.year > 0 GROUP BY t.album_id "
                f"ORDER BY year {direction}, t.album COLLATE NOCASE LIMIT ? OFFSET ?",
                (lo, hi, size, offset),
            )
        if kind == "byGenre" and genre:
            return await self._fetchall(
                _ALBUM_SELECT_BASE
                + " WHERE LOWER(t.genre) = LOWER(?) GROUP BY t.album_id "
                "ORDER BY newest DESC LIMIT ? OFFSET ?",
                (genre.strip(), size, offset),
            )

        orders = {
            "newest": " ORDER BY newest DESC",
            "highest": " ORDER BY play_count DESC, newest DESC",
            "alphabeticalByName": " ORDER BY t.album COLLATE NOCASE",
            "alphabeticalByArtist": " ORDER BY artist COLLATE NOCASE, t.album COLLATE NOCASE",
            "random": " ORDER BY RANDOM()",
        }
        sql = _ALBUM_SELECT_BASE + " GROUP BY t.album_id" + orders.get(kind, orders["newest"]) + " LIMIT ? OFFSET ?"
        return await self._fetchall(sql, (size, offset))

    async def genres(self) -> List[Dict[str, Any]]:
        return await self._fetchall(
            """SELECT genre AS name,
                      COUNT(*) AS song_count,
                      COUNT(DISTINCT album_id) AS album_count
               FROM tracks
               WHERE genre IS NOT NULL AND genre != ''
               GROUP BY genre
               ORDER BY song_count DESC, genre COLLATE NOCASE"""
        )

    async def songs_by_genre(self, genre: str, count: int, offset: int) -> List[Dict[str, Any]]:
        return await self._fetchall(
            _track_select("LOWER(t.genre) = LOWER(?)") + " ORDER BY t.posted_at DESC LIMIT ? OFFSET ?",
            (genre.strip(), count, offset),
        )

    async def random_tracks(self, n: int) -> List[Dict[str, Any]]:
        return await self._fetchall(_track_select() + " ORDER BY RANDOM() LIMIT ?", (n,))

    async def cover_track_for_album(self, album_id: str) -> Optional[Dict[str, Any]]:
        # Предпочитаем трек с HD cover_url, затем с Telegram-миниатюрой
        return await self._fetchone(
            _track_select("t.album_id = ? AND ((t.cover_url IS NOT NULL AND t.cover_url != '') OR t.has_thumb = 1)")
            + " ORDER BY (CASE WHEN t.cover_url IS NOT NULL AND t.cover_url != '' THEN 0 ELSE 1 END) ASC LIMIT 1",
            (album_id,),
        )

    # ── поиск (search / search2 / search3) ────────────────────────────────

    @staticmethod
    def _like(q: str) -> str:
        return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    async def search(
        self,
        q: str,
        artist_count: int,
        artist_offset: int,
        album_count: int,
        album_offset: int,
        song_count: int,
        song_offset: int,
    ) -> Dict[str, List[Dict[str, Any]]]:
        q_clean = (q or "").strip().strip('"\'')
        if not q_clean:
            artists = []
            if artist_count > 0:
                artists = await self._fetchall(
                    f"""SELECT {_ARTIST_COLS}
                        FROM tracks t
                        LEFT JOIN starred st ON st.item_id = t.artist_id
                        LEFT JOIN artists ar ON ar.artist_id = t.artist_id
                        GROUP BY t.artist_id
                        ORDER BY artist COLLATE NOCASE
                        LIMIT ? OFFSET ?""",
                    (artist_count, artist_offset),
                )
            albums = []
            if album_count > 0:
                albums = await self.album_list("newest", album_count, album_offset)
            songs = []
            if song_count > 0:
                songs = await self._fetchall(
                    _track_select()
                    + " ORDER BY t.posted_at DESC, t.id DESC LIMIT ? OFFSET ?",
                    (song_count, song_offset),
                )
            return {
                "artists": artists,
                "albums": albums,
                "songs": songs,
            }

        tokens = [t for t in (_normalize(q_clean) or q_clean.lower()).split() if t]
        if not tokens:
            tokens = [q_clean.lower()]

        where_tokens = " AND ".join(["t.search_text LIKE ? ESCAPE '\\'"] * len(tokens))
        token_params = [self._like(tok) for tok in tokens]
        raw_like = self._like(q_clean.lower())

        artists = await self._fetchall(
            f"""SELECT {_ARTIST_COLS}
                FROM tracks t
                LEFT JOIN starred st ON st.item_id = t.artist_id
                LEFT JOIN artists ar ON ar.artist_id = t.artist_id
                WHERE t.search_text LIKE ? ESCAPE '\\'
                  AND (LOWER(t.artist) LIKE ? ESCAPE '\\' OR LOWER(t.album_artist) LIKE ? ESCAPE '\\' OR t.search_text LIKE ? ESCAPE '\\')
                GROUP BY t.artist_id
                ORDER BY (CASE WHEN LOWER(t.album_artist) LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END),
                         artist COLLATE NOCASE
                LIMIT ? OFFSET ?""",
            (token_params[0], raw_like, raw_like, token_params[0], raw_like, artist_count, artist_offset),
        )
        albums = await self._fetchall(
            _ALBUM_SELECT_BASE
            + f" WHERE ({where_tokens}) GROUP BY t.album_id "
            "ORDER BY (CASE WHEN LOWER(t.album) LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END), "
            "t.album COLLATE NOCASE LIMIT ? OFFSET ?",
            (*token_params, raw_like, album_count, album_offset),
        )
        songs = await self._fetchall(
            _track_select(f"({where_tokens})")
            + " ORDER BY (CASE WHEN LOWER(t.title) LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END), "
            "t.posted_at DESC LIMIT ? OFFSET ?",
            (*token_params, raw_like, song_count, song_offset),
        )
        return {"artists": artists, "albums": albums, "songs": songs}

    # ── живые обновления и вычистка удалённых ─────────────────────────────

    async def channel_title(self, chat_id: int) -> Optional[str]:
        row = await self._fetchone("SELECT title FROM channels WHERE chat_id = ?", (chat_id,))
        return row["title"] if row else None

    async def track_id_by_msg(self, chat_id: int, msg_id: int) -> Optional[str]:
        row = await self._fetchone(
            "SELECT id FROM tracks WHERE chat_id = ? AND msg_id = ?", (chat_id, msg_id)
        )
        return row["id"] if row else None

    async def tracks_msg_ids(self, chat_id: int) -> list:
        rows = await self._fetchall("SELECT msg_id, id FROM tracks WHERE chat_id = ?", (chat_id,))
        return [(r["msg_id"], r["id"]) for r in rows]

    async def delete_track(self, track_id: str) -> None:
        await self._db.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        await self._db.execute("DELETE FROM plays WHERE track_id = ?", (track_id,))
        await self._db.execute("DELETE FROM starred WHERE item_id = ?", (track_id,))
        await self._db.commit()

    async def bump_last_msg_id(self, chat_id: int, msg_id: int) -> None:
        await self._db.execute(
            "INSERT INTO meta(chat_id, last_msg_id) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET last_msg_id = MAX(last_msg_id, excluded.last_msg_id)",
            (chat_id, msg_id),
        )
        await self._db.commit()
