"""Subsonic-эндпоинты: /rest/{name} и легаси /rest/{name}.view."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from ..metadata.enricher import _normalize, lookup_artist_extra
from ..runtime import runtime
from .common import authenticate, envelope_error, envelope_ok, iso_ts
from .serialize import album_dict, artist_dict, index_buckets, song_dict
from .stream import handle_cover_art, handle_stream

log = logging.getLogger("tgstream.subsonic")

router = APIRouter()


def _int(qp, name, default, lo=0, hi=500):
    try:
        return max(lo, min(hi, int(qp.get(name, default))))
    except (TypeError, ValueError):
        return default


async def _param_list(qp, request, key: str):
    vals = [str(v) for v in request.query_params.getlist(key) if v]
    if not vals and request.method == "POST":
        ct = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
            try:
                form = await request.form()
                vals = [str(v) for v in form.getlist(key) if v]
            except Exception:
                pass
    if not vals and qp.get(key):
        vals = [str(qp[key])]
    return vals

async def h_ping(qp, request):
    return {}


async def h_get_license(qp, request):
    return {"license": {"valid": True, "email": "tg-stream@localhost",
                        "licenseExpires": "2999-01-01T00:00:00.000Z"}}


async def h_get_open_subsonic(qp, request):
    return {"openSubsonicExtensions": []}


async def h_get_user(qp, request):
    username = qp.get("username") or qp.get("u") or "admin"
    return {
        "user": {
            "username": username,
            "email": f"{username}@localhost",
            "scrobblingEnabled": True,
            "adminRole": True,
            "settingsRole": True,
            "downloadRole": True,
            "uploadRole": True,
            "playlistRole": True,
            "coverArtRole": True,
            "commentRole": True,
            "podcastRole": True,
            "streamRole": True,
            "jukeboxRole": True,
            "shareRole": True,
            "videoConversionRole": True,
            "avatarLastChanged": "2026-01-01T00:00:00.000Z",
            "folder": [0],
        }
    }


async def h_get_users(qp, request):
    users = runtime.cfg.users if runtime.cfg and runtime.cfg.users else []
    return {
        "users": {
            "user": [
                (await h_get_user({"username": u.username}, request))["user"]
                for u in users
            ]
        }
    }


async def h_get_music_folders(qp, request):
    channels = await runtime.lib.channels()
    return {"musicFolders": {"musicFolder": [
        {"id": str(c["chat_id"]), "name": c["title"]} for c in channels
    ]}}


# ── навигация ───────────────────────────────────────────────────────────

async def h_get_artists(qp, request):
    mf = qp.get("musicFolderId")
    chat_id = int(mf) if mf and mf.isdigit() else None
    artists = await runtime.lib.artists(chat_id=chat_id)
    return {"artists": {"ignoredArticles": "", "index": index_buckets(artists)}}


async def h_get_indexes(qp, request):
    mf = qp.get("musicFolderId")
    if mf and mf.isdigit():
        artists = await runtime.lib.artists(chat_id=int(mf))
        return {"indexes": {"lastModified": 0, "ignoredArticles": "",
                            "index": index_buckets(artists)}}
    channels = await runtime.lib.channels()
    if len(channels) > 1:
        return {
            "indexes": {
                "lastModified": 0,
                "ignoredArticles": "",
                "index": [{
                    "name": "Каналы",
                    "artist": [{"id": str(c["chat_id"]), "name": c["title"]} for c in channels]
                }],
                "child": [
                    {"id": str(c["chat_id"]), "title": c["title"], "name": c["title"], "isDir": True}
                    for c in channels
                ],
            }
        }
    artists = await runtime.lib.artists()
    return {"indexes": {"lastModified": 0, "ignoredArticles": "",
                        "index": index_buckets(artists)}}


async def h_get_artist(qp, request):
    artist = await runtime.lib.artist_by_id(qp.get("id", ""))
    if not artist:
        return envelope_error(70, "Artist not found")
    albums = await runtime.lib.albums_of_artist(artist["artist_id"])
    return {"artist": {**artist_dict(artist), "album": [album_dict(a) for a in albums]}}


async def _artist_info_payload(qp):
    artist = await runtime.lib.artist_by_id(qp.get("id", ""))
    if not artist:
        return None

    bio = artist.get("biography") or ""
    try:
        related = json.loads(artist.get("similar_json") or "[]")
    except Exception:
        related = []

    if int(artist.get("info_fetched_at") or 0) == 0:
        bio, related, dz_id = await lookup_artist_extra(
            runtime.http, artist["name"] if "name" in artist else artist["artist"],
            int(artist.get("deezer_artist_id") or 0),
        )
        await runtime.lib.update_artist_info(
            artist["artist_id"], artist["artist"], bio, related, dz_id
        )

    count = _int(qp, "count", 20, 1, 50)
    include_not_present = str(qp.get("includeNotPresent", "false")).lower() in ("true", "1")
    lib_by_norm = {_normalize(a["artist"]): a for a in await runtime.lib.artists()}

    similar_artists = []
    seen_ids = set()
    for item in related:
        name = item.get("name") or ""
        norm = _normalize(name)
        if not norm:
            continue
        lib_art = lib_by_norm.get(norm)
        if lib_art and lib_art["artist_id"] != artist["artist_id"]:
            if lib_art["artist_id"] not in seen_ids:
                seen_ids.add(lib_art["artist_id"])
                similar_artists.append(artist_dict(lib_art))
        elif include_not_present:
            entry = {"id": "-1", "name": name}
            if item.get("image_url"):
                entry["artistImageUrl"] = item["image_url"]
            similar_artists.append(entry)
        if len(similar_artists) >= count:
            break

    info = {
        "biography": bio,
        "musicBrainzId": "",
        "lastFmUrl": "",
        "similarArtist": similar_artists,
    }
    img = artist.get("image_url") or ""
    if img:
        info["smallImageUrl"] = img.replace("/1000x1000-", "/250x250-")
        info["mediumImageUrl"] = img.replace("/1000x1000-", "/500x500-")
        info["largeImageUrl"] = img
    return info


async def h_get_artist_info(qp, request):
    info = await _artist_info_payload(qp)
    if info is None:
        return envelope_error(70, "Artist not found")
    return {"artistInfo": info}


async def h_get_artist_info2(qp, request):
    info = await _artist_info_payload(qp)
    if info is None:
        return envelope_error(70, "Artist not found")
    return {"artistInfo2": info}


async def h_get_album(qp, request):
    album = await runtime.lib.album_by_id(qp.get("id", ""))
    if not album:
        return envelope_error(70, "Album not found")
    tracks = await runtime.lib.album_tracks(album["album_id"])
    return {"album": {**album_dict(album), "song": [song_dict(t) for t in tracks]}}


async def h_get_song(qp, request):
    track = await runtime.lib.track_by_id(qp.get("id", ""))
    if not track:
        return envelope_error(70, "Song not found")
    return {"song": song_dict(track)}


async def h_get_music_directory(qp, request):
    dir_id = qp.get("id", "")
    if dir_id.isdigit():  # папка = канал -> исполнители канала
        cid = int(dir_id)
        cname = (await runtime.lib.channel_title(cid)) or dir_id
        rows = await runtime.lib.artists(chat_id=cid)
        return {"directory": {"id": dir_id, "name": cname, "parent": "root", "child": [
            {
                "id": r["artist_id"],
                "parent": dir_id,
                "isDir": True,
                "title": r["artist"],
                "artist": r["artist"],
                **({"coverArt": r["artist_id"]} if r.get("image_url") else {}),
            }
            for r in rows
        ]}}
    if dir_id.startswith("ar-"):
        artist = await runtime.lib.artist_by_id(dir_id)
        if not artist:
            return envelope_error(70, "Directory not found")
        albums = await runtime.lib.albums_of_artist(dir_id)
        return {"directory": {"id": dir_id, "child": [
            {"id": a["album_id"], "parent": dir_id, "isDir": True,
             "title": a["album"], "artist": a["artist"], "coverArt": a["album_id"],
             "playCount": int(a.get("play_count") or 0)}
            for a in albums
        ]}}
    if dir_id.startswith("al-"):
        tracks = await runtime.lib.album_tracks(dir_id)
        return {"directory": {"id": dir_id, "child": [song_dict(t) for t in tracks]}}
    return envelope_error(70, "Directory not found")


# ── списки альбомов / случайные треки / жанры / поиск ───────────────────

async def _album_list_payload(qp, key):
    kind = qp.get("type", "newest")
    size = _int(qp, "size", 10, 1, 500)
    offset = _int(qp, "offset", 0, 0, 100000)
    from_year = _int(qp, "fromYear", 0, 0, 9999) if "fromYear" in qp else None
    to_year = _int(qp, "toYear", 9999, 0, 9999) if "toYear" in qp else None
    genre = qp.get("genre")
    rows = await runtime.lib.album_list(
        kind, size, offset, from_year=from_year, to_year=to_year, genre=genre
    )
    return {key: {"album": [album_dict(a) for a in rows]}}


async def h_get_album_list2(qp, request):
    return await _album_list_payload(qp, "albumList2")


async def h_get_album_list(qp, request):
    return await _album_list_payload(qp, "albumList")


async def h_get_random_songs(qp, request):
    size = _int(qp, "size", 10, 1, 500)
    rows = await runtime.lib.random_tracks(size)
    return {"randomSongs": {"song": [song_dict(t) for t in rows]}}


async def h_get_genres(qp, request):
    rows = await runtime.lib.genres()
    return {
        "genres": {
            "genre": [
                {
                    "value": r["name"],
                    "songCount": int(r.get("song_count") or 0),
                    "albumCount": int(r.get("album_count") or 0),
                }
                for r in rows
            ]
        }
    }


async def h_get_songs_by_genre(qp, request):
    genre = qp.get("genre", "")
    count = _int(qp, "count", 10, 1, 500)
    offset = _int(qp, "offset", 0, 0, 100000)
    rows = await runtime.lib.songs_by_genre(genre, count, offset) if genre else []
    return {"songsByGenre": {"song": [song_dict(t) for t in rows]}}


async def _search_payload(qp):
    r = await runtime.lib.search(
        qp.get("query", ""),
        _int(qp, "artistCount", 20, 0, 500), _int(qp, "artistOffset", 0, 0, 10000000),
        _int(qp, "albumCount", 20, 0, 500), _int(qp, "albumOffset", 0, 0, 10000000),
        _int(qp, "songCount", 20, 0, 500), _int(qp, "songOffset", 0, 0, 10000000),
    )
    return {
        "artist": [artist_dict(a) for a in r["artists"]],
        "album": [album_dict(a) for a in r["albums"]],
        "song": [song_dict(s) for s in r["songs"]],
    }


async def h_search3(qp, request):
    return {"searchResult3": await _search_payload(qp)}


async def h_search2(qp, request):
    return {"searchResult2": await _search_payload(qp)}


async def h_search(qp, request):
    return {"searchResult": await _search_payload(qp)}


# ── сканирование ────────────────────────────────────────────────────────

async def h_get_scan_status(qp, request):
    return {"scanStatus": {"scanning": runtime.scanning,
                           "count": await runtime.lib.total_tracks()}}


async def h_start_scan(qp, request):
    if runtime.scan_task is None or runtime.scan_task.done():
        runtime.scan_task = asyncio.create_task(_rescan())
    return {"scanStatus": {"scanning": True, "count": await runtime.lib.total_tracks()}}


async def _rescan():
    from ..library.indexer import cleanup_deleted, index_channel

    runtime.scanning = True
    try:
        for ref in runtime.cfg.telegram.channels:
            try:
                chat_id, added = await index_channel(runtime.tg, runtime.lib, ref, full=False)
                if added > 0:
                    runtime.trigger_enrich()
                asyncio.create_task(cleanup_deleted(runtime.tg, runtime.lib, runtime.cache, chat_id))
            except Exception:
                log.exception("Переиндексация %s не удалась", ref)
        asyncio.create_task(runtime.cache.evict(runtime.lib))
        runtime.trigger_enrich()
    finally:
        runtime.scanning = False


async def h_stream(qp, request):
    return await handle_stream(qp, request)


async def h_download(qp, request):
    return await handle_stream(qp, request, download=True)


async def h_get_cover_art(qp, request):
    return await handle_cover_art(qp, request)


async def h_get_avatar(qp, request):
    return PlainTextResponse("No avatar", status_code=404)


async def h_star(qp, request):
    for sid in await _param_list(qp, request, "id"):
        itype = "artist" if sid.startswith("ar-") else ("album" if sid.startswith("al-") else "song")
        await runtime.lib.star_item(sid, itype)
    for alid in await _param_list(qp, request, "albumId"):
        await runtime.lib.star_item(alid, "album")
    for arid in await _param_list(qp, request, "artistId"):
        await runtime.lib.star_item(arid, "artist")
    return {}


async def h_unstar(qp, request):
    for key in ("id", "albumId", "artistId"):
        for item_id in await _param_list(qp, request, key):
            await runtime.lib.unstar_item(item_id)
    return {}


async def _starred_payload():
    data = await runtime.lib.get_starred_all()
    return {
        "artist": [artist_dict(a) for a in data["artists"]],
        "album": [album_dict(a) for a in data["albums"]],
        "song": [song_dict(s) for s in data["songs"]],
    }


async def h_get_starred(qp, request):
    return {"starred": await _starred_payload()}


async def h_get_starred2(qp, request):
    return {"starred2": await _starred_payload()}


async def h_scrobble(qp, request):
    tid = qp.get("id", "")
    if not tid:
        return {}
    username = qp.get("u") or "admin"
    player = qp.get("c") or "client"
    # По стандарту Subsonic submission=false означает "сейчас играет" (не завершённое прослушивание)
    submission = str(qp.get("submission", "true")).lower() not in ("false", "0")
    if submission:
        cur = runtime.now_playing.get((username, player))
        if cur and cur.get("track_id") == tid:
            runtime.now_playing.pop((username, player), None)
        await runtime.lib.add_play(tid)
    else:
        runtime.now_playing[(username, player)] = {
            "track_id": tid,
            "username": username,
            "player_name": player,
            "updated_at": time.time(),
        }
        await runtime.lib.sync_play_queue_from_track(username, tid, player)
        await runtime.lib.touch(tid)
    return {}


async def h_get_now_playing(qp, request):
    now = time.time()
    entries = []
    stale_keys = []
    for key, item in list(runtime.now_playing.items()):
        age = now - float(item.get("updated_at") or 0)
        if age > 600:
            stale_keys.append(key)
            continue
        track = await runtime.lib.track_by_id(item["track_id"])
        if not track:
            stale_keys.append(key)
            continue
        dur = int(track.get("duration") or 0)
        if age > max(600, dur + 60):
            stale_keys.append(key)
            continue
        entry = song_dict(track)
        entry["username"] = item["username"]
        entry["minutesAgo"] = int(age // 60)
        entry["playerId"] = (abs(hash(item["player_name"])) % 10000) + 1
        entry["playerName"] = item["player_name"]
        entries.append(entry)
    for k in stale_keys:
        runtime.now_playing.pop(k, None)
    return {"nowPlaying": {"entry": entries}}


async def _play_queue_payload(qp, by_index: bool = False):
    username = qp.get("u") or "admin"
    q = await runtime.lib.get_play_queue(username)
    if not q:
        return {}
    tracks = q["tracks"]
    payload = {
        "username": q["username"],
        "position": q["position"],
        "changed": iso_ts(q["changed_at"]),
        "changedBy": q["changed_by"],
        "entry": [song_dict(t) for t in tracks],
    }
    if by_index:
        idx = next((i for i, t in enumerate(tracks) if t["id"] == q["current_id"]), 0)
        payload["currentIndex"] = idx
    else:
        payload["current"] = q["current_id"]
    return {"playQueue": payload}


async def h_get_play_queue(qp, request):
    return await _play_queue_payload(qp, by_index=False)


async def h_get_play_queue_by_index(qp, request):
    return await _play_queue_payload(qp, by_index=True)


async def _save_play_queue_handler(qp, request, by_index: bool = False):
    ids = await _param_list(qp, request, "id")
    username = qp.get("u") or "admin"
    player = qp.get("c") or "client"
    position = _int(qp, "position", 0, 0, 10**9)

    if by_index:
        idx = _int(qp, "currentIndex", 0, 0, max(0, len(ids) - 1))
        current_id = ids[idx] if 0 <= idx < len(ids) else ""
    else:
        current_id = qp.get("current") or (ids[0] if ids else "")

    await runtime.lib.save_play_queue(username, ids, current_id, position, player)
    return {}


async def h_save_play_queue(qp, request):
    return await _save_play_queue_handler(qp, request, by_index=False)


async def h_save_play_queue_by_index(qp, request):
    return await _save_play_queue_handler(qp, request, by_index=True)


async def h_get_playlists(qp, request):
    return {"playlists": {"playlist": []}}


async def h_playlist_nyi(qp, request):
    return envelope_error(0, "Playlist management is not implemented yet")


async def h_ok_noop(qp, request):
    return {}


async def h_empty_radios(qp, request):
    return {"internetRadioStations": {"internetRadioStation": []}}


async def h_empty_podcasts(qp, request):
    return {"podcasts": {"channel": []}}


HANDLERS = {
    "ping": h_ping,
    "getUser": h_get_user,
    "getUsers": h_get_users,
    "getLicense": h_get_license,
    "getOpenSubsonicExtensions": h_get_open_subsonic,
    "getMusicFolders": h_get_music_folders,
    "getArtists": h_get_artists,
    "getIndexes": h_get_indexes,
    "getArtist": h_get_artist,
    "getArtistInfo": h_get_artist_info,
    "getArtistInfo2": h_get_artist_info2,
    "getAlbum": h_get_album,
    "getSong": h_get_song,
    "getMusicDirectory": h_get_music_directory,
    "getAlbumList": h_get_album_list,
    "getAlbumList2": h_get_album_list2,
    "getRandomSongs": h_get_random_songs,
    "getGenres": h_get_genres,
    "getSongsByGenre": h_get_songs_by_genre,
    "search": h_search,
    "search2": h_search2,
    "search3": h_search3,
    "getScanStatus": h_get_scan_status,
    "startScan": h_start_scan,
    "stream": h_stream,
    "download": h_download,
    "getCoverArt": h_get_cover_art,
    "getAvatar": h_get_avatar,
    "getPlaylists": h_get_playlists,
    "getPlaylist": h_playlist_nyi,
    "createPlaylist": h_playlist_nyi,
    "updatePlaylist": h_playlist_nyi,
    "deletePlaylist": h_playlist_nyi,
    "star": h_star,
    "unstar": h_unstar,
    "setRating": h_ok_noop,
    "getStarred": h_get_starred,
    "getStarred2": h_get_starred2,
    "scrobble": h_scrobble,
    "getNowPlaying": h_get_now_playing,
    "getPlayQueue": h_get_play_queue,
    "savePlayQueue": h_save_play_queue,
    "getPlayQueueByIndex": h_get_play_queue_by_index,
    "savePlayQueueByIndex": h_save_play_queue_by_index,
    "getInternetRadioStations": h_empty_radios,
    "getPodcasts": h_empty_podcasts,
}


@router.api_route("/rest/{op}", methods=["GET", "POST", "HEAD"])
async def dispatch(op: str, request: Request):
    name = op[:-5] if op.endswith(".view") else op
    handler = HANDLERS.get(name)
    if handler is None:
        return envelope_error(0, f"Endpoint '{name}' is not implemented")

    qp = dict(request.query_params)
    if not qp.get("u") and request.method == "POST":
        ct = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
            try:
                form = await request.form()
                for k, v in form.items():
                    qp[k] = str(v)
            except Exception:
                pass
        elif "application/json" in ct:
            try:
                data = await request.json()
                if isinstance(data, dict):
                    for k, v in data.items():
                        qp[k] = str(v)
            except Exception:
                pass

    if authenticate(qp, runtime.cfg.users) is None:
        return envelope_error(40, "Wrong username or password")

    try:
        result = await handler(qp, request)
    except Exception:
        log.exception("Ошибка в обработчике %s", name)
        return envelope_error(0, "Internal server error")

    if isinstance(result, Response):
        return result
    return envelope_ok(result or {})
