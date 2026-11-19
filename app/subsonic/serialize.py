"""Преобразование строк библиотеки в Subsonic-структуры."""
from __future__ import annotations

from typing import Dict, List

from .common import iso_ts, mime_for


def song_dict(r: Dict) -> Dict:
    dur = int(r.get("duration") or 0)
    size = int(r.get("size") or 0)
    bitrate = int(size * 8 / dur / 1000) if dur > 0 else 0
    ext = r.get("ext") or ""
    year = max(0, int(r.get("year") or 0))
    play_count = int(r.get("play_count") or 0)
    last_played = int(r.get("last_played") or 0)
    starred_at = int(r.get("starred_at") or 0)

    # Если есть внешняя обложка альбома (cover_url), отдаём album_id — одна обложка на весь альбом
    if r.get("cover_url"):
        cover_art = r["album_id"]
    elif r.get("has_thumb"):
        cover_art = r["id"]
    else:
        cover_art = r["album_id"]

    d = {
        "id": r["id"],
        "parent": r["album_id"],
        "isDir": False,
        "title": r["title"],
        "artist": r["artist"],
        "albumArtist": r.get("album_artist") or r["artist"],
        "artistId": r["artist_id"],
        "album": r["album"],
        "albumId": r["album_id"],
        "duration": dur,
        "bitRate": bitrate,
        "track": int(r.get("track_number") or 0),
        "discNumber": int(r.get("disc_number") or 1),
        "year": year,
        "genre": r.get("genre") or "",
        "contentType": mime_for(ext, r.get("mime")),
        "suffix": ext,
        "size": size,
        "playCount": play_count,
        "path": f"{r.get('channel_title') or 'Telegram'}/{r['album']}/{r['title']}",
        "coverArt": cover_art,
        "created": iso_ts(int(r.get("posted_at") or 0)),
        "type": "music",
        "isVideo": False,
    }
    if last_played > 0:
        d["played"] = iso_ts(last_played)
    if starred_at > 0:
        d["starred"] = iso_ts(starred_at)
    return d


def album_dict(r: Dict) -> Dict:
    play_count = int(r.get("play_count") or 0)
    last_played = int(r.get("last_played") or 0)
    starred_at = int(r.get("starred_at") or 0)
    d = {
        "id": r["album_id"],
        "name": r["album"],
        "title": r["album"],
        "artist": r["artist"],
        "artistId": r["artist_id"],
        "coverArt": r["album_id"],
        "songCount": int(r.get("song_count") or 0),
        "duration": int(r.get("dur") or 0),
        "playCount": play_count,
        "created": iso_ts(int(r.get("created") or 0)),
    }
    if r.get("year") and int(r["year"]) > 0:
        d["year"] = int(r["year"])
    if r.get("genre"):
        d["genre"] = r["genre"]
    if last_played > 0:
        d["played"] = iso_ts(last_played)
    if starred_at > 0:
        d["starred"] = iso_ts(starred_at)
    return d


def artist_dict(r: Dict) -> Dict:
    starred_at = int(r.get("starred_at") or 0)
    d = {
        "id": r["artist_id"],
        "name": r["artist"],
        "albumCount": int(r.get("album_count") or 0),
    }
    if r.get("image_url"):
        d["coverArt"] = r["artist_id"]
        d["artistImageUrl"] = r["image_url"]
    if starred_at > 0:
        d["starred"] = iso_ts(starred_at)
    return d


def index_buckets(artists: List[Dict]) -> List[Dict]:
    """Исполнители, сгруппированные по первой букве (поддерживает латиницу A-Z и кириллицу А-Я)."""
    buckets: Dict[str, List[Dict]] = {}
    for a in artists:
        ch = (a["artist"] or "?").strip()[:1].upper()
        if ch == "Ё":
            ch = "Е"
        letter = ch if ch.isalpha() else "#"
        buckets.setdefault(letter, []).append(artist_dict(a))

    def _sort_key(letter: str):
        if letter == "#":
            return (2, letter)
        if "A" <= letter <= "Z":
            return (0, letter)
        return (1, letter)

    return [
        {"name": letter, "artist": buckets[letter]}
        for letter in sorted(buckets, key=_sort_key)
    ]
