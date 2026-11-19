"""Обогащение метаданных аудиотреков: Deezer (основной) + iTunes (резервный)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple

import httpx

log = logging.getLogger("tgstream.metadata")

# Регулярные выражения для очистки названий
_RE_CHANNEL_TAG = re.compile(r"@[a-zA-Z0-9_]{3,}", re.IGNORECASE)
_RE_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_RE_SITE_WATERMARK = re.compile(
    r"[\(\[\{][^\)\]\}]*\b(?:muzma|zaycev|muzmo|mp3|promodj|sefon|hitmo|zvukoff|drivemusic)\.[a-z]{2,5}[^\)\]\}]*[\)\]\}]",
    re.IGNORECASE,
)
_RE_HYPHENS = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_RE_LEADING_TRACK_NUM = re.compile(r"^\s*\d{1,2}\s*(?:\.\s+|\s+-\s+|\s*_\s+)(?=[A-Za-zА-Яа-яЁё])", re.UNICODE)
_RE_SPACES = re.compile(r"\s+", re.UNICODE)

# Скобки и суффиксы для поискового запроса (не удаляются из отображаемого названия)
_RE_SEARCH_BRACKETS = re.compile(
    r"\[[^\]]*\]|\([^\)]*(?:official|audio|video|remaster|prod|feat|ft\.|lyrics|320|kbps|"
    r"album\s*version|bonus\s*track|original\s*version|slowed|reverb|ost\b|soundtrack|"
    r"live\s+in|session|demo|rough\s*mix|superstar\s*remix|nfsmw|unshatter)[^\)]*\)",
    re.IGNORECASE,
)
# Подзаголовки вида "There There. (The Boney King Of Nowhere.)"
_RE_DOT_SUBTITLE = re.compile(r"\.\s*\([^\)]+\)\s*$")
_RE_INLINE_FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$", re.IGNORECASE)
_RE_ARTIST_FEAT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|vs\.?)\s+.+$", re.IGNORECASE)
_RE_SOUNDTRACK_ALBUM = re.compile(
    r"\b(?:original\s+(?:series|motion\s+picture|game|television)\s+soundtrack|ost)\b",
    re.IGNORECASE,
)
_RE_ALBUM_EDITION = re.compile(
    r"\s*[\(\[]"
    r"[^\)\]]*\b(?:"
    r"deluxe(?:\s+edition|\s+version)?|"
    r"expanded(?:\s+edition|\s+version)?|"
    r"bonus(?:\s+track|\s+edition|\s+version)?|"
    r"special\s+edition|"
    r"collector'?s\s+edition|"
    r"anniversary(?:\s+edition|\s+deluxe|\s+remaster(?:ed)?)?|"
    r"\d+(?:st|nd|rd|th)\s+anniversary[^\)\]]*|"
    r"\d+\s+year\s+anniversary[^\)\]]*|"
    r"remaster(?:ed)?(?:\s+\d{4})?|"
    r"\d{4}\s+remaster(?:ed)?|"
    r"explicit(?:\s+version)?|"
    r"clean(?:\s+version)?"
    r")\b[^\)\]]*[\)\]]"
    r"|\s+-\s+(?:single|ep)\s*$",
    re.IGNORECASE,
)

_TRIBUTE_KEYWORDS = (
    "vitamin string quartet",
    "piano tribute",
    "the backing tracks",
    "backing track",
    "karaoke",
    "8-bit arcade",
    "lullaby baby",
    "rockabye baby",
    "made famous by",
    "tribute to",
    "string quartet",
    "the pop heroes",
)

# Канонические имена артистов для единообразного отображения (без дублей по регистру)
_CANONICAL_ARTISTS: Dict[str, str] = {
    "30 seconds to mars": "30 Seconds to Mars",
    "thirty seconds to mars": "30 Seconds to Mars",
    "seconds to mars": "30 Seconds to Mars",
    "amotti": "Amotti",
    "c418": "C418",
    "d4vd": "d4vd",
    "domino": "domiNo",
    "dom no": "domiNo",
    "fontaines d c": "Fontaines D.C.",
    "glare": "Glare",
    "jane air": "Jane Air",
    "jay z": "Jay-Z",
    "linkin park": "Linkin Park",
    "mf doom": "MF DOOM",
    "my chemical romance": "My Chemical Romance",
    "oxxxymiron": "Oxxxymiron",
    "pearl jam": "Pearl Jam",
    "queens of the stone age": "Queens of the Stone Age",
    "†††": "††† (Crosses)",
    "crosses": "††† (Crosses)",
}

# Группы, где "&" или "," является частью неделимого дуэта/группы
_KEEP_DUO_ARTISTS = {
    "jay-z & kanye west",
    "simon & garfunkel",
    "hall & oates",
    "of mice & men",
    "coheed and cambria",
    "angels & airwaves",
    "she & him",
}

CACHE_MAX_SIZE = 5000
_CACHE: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_ALBUM_CACHE: OrderedDict[int, Dict[str, Any]] = OrderedDict()


def _cache_put(key: str, val: Dict[str, Any]) -> None:
    _CACHE[key] = val
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX_SIZE:
        _CACHE.popitem(last=False)


def fix_mojibake(text: str) -> str:
    """Исправить кракозябры CP1251, прочитанные как Latin-1 (например 'Ïóëÿ' -> 'Пуля'),
    не затрагивая валидные западноевропейские диакритики ('début', 'Rivière', 'Lø').
    """
    if not text:
        return ""
    # В CP1251-кракозябрах русские слова состоят из подряд идущих символов 0xA8, 0xB8, 0xC0..0xFF
    runs = re.findall(r"[\u00a8\u00b8\u00c0-\u00ff]{2,}", text)
    if not runs or sum(len(r) for r in runs) < 2:
        return text
    # Если это одиночное слово с 2 диакритиками внутри латиницы — не трогаем
    if len(runs) == 1 and len(runs[0]) == 2 and re.search(r"[A-Za-z][\u00c0-\u00ff]{2}|[\u00c0-\u00ff]{2}[A-Za-z]", text):
        return text

    def _decode_run(m: re.Match) -> str:
        chunk = m.group(0)
        try:
            dec = chunk.encode("latin1").decode("cp1251")
            if any("а" <= c.lower() <= "я" or c.lower() == "ё" for c in dec):
                return dec
        except Exception:
            pass
        return chunk

    return re.sub(r"[\u00a8\u00b8\u00c0-\u00ff]+", _decode_run, text)


def clean_display_artist(text: str) -> str:
    """Очистить имя артиста для отображения (не срезая ведущие цифры вроде '30 Seconds to Mars')."""
    if not text:
        return ""
    t = fix_mojibake(text)
    t = _RE_HYPHENS.sub("-", t)
    t = _RE_URL.sub(" ", t)
    t = _RE_CHANNEL_TAG.sub(" ", t)
    t = _RE_SITE_WATERMARK.sub(" ", t)
    t = _RE_SPACES.sub(" ", t).strip()
    if t.lower() == "seconds to mars":
        return "30 Seconds to Mars"
    return t


def clean_display_title(text: str) -> str:
    """Очистить название трека для отображения (сохраняя скобки типа feat/remix, но убирая мусор)."""
    if not text:
        return ""
    t = fix_mojibake(text)
    t = _RE_HYPHENS.sub("-", t)
    t = _RE_URL.sub(" ", t)
    t = _RE_CHANNEL_TAG.sub(" ", t)
    t = _RE_SITE_WATERMARK.sub(" ", t)
    t = _RE_LEADING_TRACK_NUM.sub("", t)
    t = _RE_SPACES.sub(" ", t).strip()
    return t


def clean_text(text: str) -> str:
    """Очистка строки для поискового запроса (убирает скобки с версиями, feat и т.д.)."""
    if not text:
        return ""
    t = clean_display_title(text)
    t = _RE_DOT_SUBTITLE.sub("", t)
    t = _RE_SEARCH_BRACKETS.sub(" ", t)
    t = _RE_INLINE_FEAT.sub("", t)
    # Если всё название было в квадратных скобках (например '[JFK]'), вернём содержимое без скобок
    t = _RE_SPACES.sub(" ", t).strip(" .-–—")
    if not t:
        t = clean_display_title(text).strip("[]() ")
    return t


def _strip_diacritics_keep_cyrillic(s: str) -> str:
    """Убрать диакритику с латиницы (JAŸ-Z -> JAY-Z, Dödens -> Dodens, Lø -> Lo), сохранив 'й'."""
    s = s.replace("ё", "е").replace("Ё", "Е")
    s = s.replace("ø", "o").replace("Ø", "O").replace("ł", "l").replace("Ł", "L")
    out = []
    for ch in s:
        if "а" <= ch.lower() <= "я":
            out.append(ch)
        else:
            decomp = unicodedata.normalize("NFKD", ch)
            out.extend(c for c in decomp if not unicodedata.combining(c))
    return "".join(out)


def _normalize(s: str) -> str:
    if not s:
        return ""
    s = fix_mojibake(s).lower()
    s = _strip_diacritics_keep_cyrillic(s)
    # Заменяем символ креста † внутри слов на 't' (Bi†ches -> bitches, †elepa†hy -> telepathy)
    s = re.sub(r"(?<=[a-z])†|†(?=[a-z])", "t", s)
    s = _RE_HYPHENS.sub(" ", s)
    for ch in ".,/\\-_'\"`!?()[]{}<>:@#*%—–‐•…&+":
        s = s.replace(ch, " ")
    norm = " ".join(s.split())
    if norm in ("thirty seconds to mars", "seconds to mars"):
        return "30 seconds to mars"
    return norm


def extract_primary_artist(artist: str) -> str:
    """Выделить основного исполнителя (без feat., ft., второго артиста через запятую)
    и привести к каноническому регистру.
    """
    disp = clean_display_artist(artist)
    if not disp:
        return "Unknown Artist"

    low = disp.lower()
    if low in _CANONICAL_ARTISTS:
        return _CANONICAL_ARTISTS[low]

    # Убираем feat. / ft. / featuring / vs.
    base = _RE_ARTIST_FEAT.sub("", disp).strip(" ,;-")

    # Проверяем, не входит ли в список неделимых дуэтов
    if base.lower() not in _KEEP_DUO_ARTISTS:
        # Разделяем по запятой или слэшу, если перечислено несколько артистов
        for sep in (", ", " / ", "/"):
            if sep in base:
                first = base.split(sep, 1)[0].strip()
                if first and len(first) >= 2:
                    base = first
                    break

    norm_key = _normalize(base)
    if norm_key in _CANONICAL_ARTISTS:
        return _CANONICAL_ARTISTS[norm_key]
    return base or disp


def parse_artist_title(artist: str, title: str, channel_title: str) -> Tuple[str, str]:
    """Извлечь артиста и название, если они были склеены в одной строке."""
    artist = clean_display_artist(artist)
    title = clean_display_title(title)

    clean_chan = clean_display_artist(channel_title).lower()
    if not artist or artist.lower() == clean_chan or artist == "Telegram":
        for sep in (" - ", " – ", " — ", " -"):
            if sep in title:
                parts = title.split(sep, 1)
                p_artist, p_title = clean_display_artist(parts[0]), clean_display_title(parts[1])
                if p_artist and p_title:
                    return p_artist, p_title

    return artist, title


def _artist_id(artist: str) -> str:
    primary = extract_primary_artist(artist)
    norm = _normalize(primary) or primary.lower().strip()
    return "ar-" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def _album_id(chat_id: int, album: str, album_artist: str = "") -> str:
    if album_artist:
        return album_id_for(album_artist, album)
    return "al-" + hashlib.sha1(f"{chat_id}:{album.lower().strip()}".encode("utf-8")).hexdigest()[:12]


def clean_album_title(album: str) -> str:
    """Убрать из названия альбома пометки (Deluxe Edition), (20th Anniversary), (Remastered), - Single и т.п."""
    if not album:
        return ""
    t = fix_mojibake(album)
    t = _RE_HYPHENS.sub("-", t)
    cleaned = _RE_ALBUM_EDITION.sub("", t).strip()
    cleaned = _RE_SPACES.sub(" ", cleaned).strip()
    return cleaned if len(cleaned) >= 2 else t.strip()


def album_id_for(album_artist: str, album: str) -> str:
    base_alb = clean_album_title(album)
    norm_alb = _normalize(base_alb) or base_alb.lower().strip()
    if _RE_SOUNDTRACK_ALBUM.search(album):
        key = f"ost:{norm_alb}"
    else:
        primary = extract_primary_artist(album_artist)
        norm_art = _normalize(primary) or primary.lower().strip()
        key = f"{norm_art}:{norm_alb}"
    return "al-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def fallback_album_for(artist: str, channel_title: str = "") -> Tuple[str, str]:
    """Индивидуальный альбом «Синглы» для каждого артиста вместо свалки в один альбом канала."""
    primary = extract_primary_artist(artist)
    if not primary or primary.lower() == (channel_title or "").lower() or primary == "Telegram":
        alb_name = channel_title or "Telegram"
        alb_id = "al-" + hashlib.sha1(f"chan:{alb_name.lower()}".encode("utf-8")).hexdigest()[:12]
        return alb_name, alb_id
    alb_name = f"{primary} — Синглы"
    norm_art = _normalize(primary) or primary.lower()
    alb_id = "al-" + hashlib.sha1(f"singles:{norm_art}".encode("utf-8")).hexdigest()[:12]
    return alb_name, alb_id


def _is_tribute(cand_artist: str, cand_title: str, cand_album: str, target_artist_norm: str) -> bool:
    combined = f"{cand_artist} {cand_title} {cand_album}".lower()
    for kw in _TRIBUTE_KEYWORDS:
        if kw in combined and kw not in target_artist_norm:
            return True
    return False


def is_match(artist: str, title: str, cand_artist: str, cand_title: str) -> bool:
    """Строгая проверка совпадения артиста и названия."""
    prim = extract_primary_artist(artist)
    na = _normalize(prim)
    nt = _normalize(clean_text(title))
    ca = _normalize(cand_artist)
    ct = _normalize(clean_text(cand_title))

    # Спец-случай для ††† (Crosses)
    if prim in ("†††", "††† (Crosses)") and ("crosses" in ca or "†††" in cand_artist):
        art_ok = True
    else:
        if not na or not ca:
            return False
        if _is_tribute(cand_artist, cand_title, "", na):
            return False
        art_tokens = set(na.split())
        cand_art_tokens = set(ca.split())
        if len(na) <= 3:
            art_ok = (na == ca) or (na in cand_art_tokens)
        else:
            art_ok = (na == ca) or (art_tokens and art_tokens.issubset(cand_art_tokens)) or (ca in na and len(ca) >= 4)

    if not art_ok:
        return False

    if not nt or not ct:
        return False

    if nt == ct:
        return True

    # Для коротких названий (например "Intro", "Home", "Stay") требуем точного совпадения после очистки
    if len(nt) <= 5 or len(ct) <= 5:
        return nt == ct

    nt_tokens = set(nt.split())
    ct_tokens = set(ct.split())
    return nt_tokens.issubset(ct_tokens) or ct_tokens.issubset(nt_tokens)


def _score_candidate(
    orig_title: str,
    orig_duration: int,
    cand_artist: str,
    cand_title: str,
    cand_album: str,
    cand_duration: int = 0,
    record_type: str = "",
) -> int:
    """Оценка кандидата: чем выше, тем точнее совпадение (студийный альбом > сингл > сборник/live)."""
    score = 100
    orig_low = orig_title.lower()
    ct_low = cand_title.lower()
    alb_low = cand_album.lower()

    # Штраф за live/karaoke/instrumental/acappella/remix, если этого не было в исходном треке
    for marker in ("live", "karaoke", "instrumental", "acoustic", "demo", "commentary", "acappella", "a cappella"):
        if marker in ct_low and marker not in orig_low:
            score -= 40
        if marker in alb_low and marker not in orig_low:
            score -= 40

    if "remix" in ct_low and "remix" not in orig_low and "reanimation" not in alb_low:
        score -= 35

    # Штраф за сборники "Greatest Hits" / "Best Of" / "Essential"
    for comp in ("greatest hits", "best of", "the essential", "anthology", "top hits"):
        if comp in alb_low:
            score -= 20

    rt = (record_type or "").lower()
    if rt == "album":
        score += 15
    elif rt == "ep":
        score += 8
    elif rt == "compile" or rt == "compilation":
        score -= 15

    if orig_duration > 0 and cand_duration > 0:
        diff = abs(orig_duration - cand_duration)
        if diff <= 3:
            score += 20
        elif diff <= 8:
            score += 10
        elif diff > 25 and "slowed" not in orig_low:
            score -= 25

    if _normalize(clean_text(orig_title)) == _normalize(clean_text(cand_title)):
        score += 15

    return score


async def _fetch_deezer_album(client: httpx.AsyncClient, deezer_album_id: int) -> Dict[str, Any]:
    if deezer_album_id in _ALBUM_CACHE:
        _ALBUM_CACHE.move_to_end(deezer_album_id)
        return _ALBUM_CACHE[deezer_album_id]

    info: Dict[str, Any] = {"year": 0, "genre": "", "record_type": "", "artist": ""}
    try:
        resp = await client.get(f"https://api.deezer.com/album/{deezer_album_id}")
        if resp.status_code == 200:
            data = resp.json()
            r_date = data.get("release_date") or ""
            if len(r_date) >= 4 and r_date[:4].isdigit():
                info["year"] = int(r_date[:4])
            genres = (data.get("genres") or {}).get("data") or []
            if genres and isinstance(genres[0], dict):
                info["genre"] = genres[0].get("name") or ""
            info["record_type"] = data.get("record_type") or ""
            info["artist"] = (data.get("artist") or {}).get("name") or ""
            _ALBUM_CACHE[deezer_album_id] = info
            while len(_ALBUM_CACHE) > 2000:
                _ALBUM_CACHE.popitem(last=False)
    except Exception as e:
        log.debug("Deezer album lookup error (%s): %s", deezer_album_id, e)
    return info


def _search_queries(artist: str, title: str) -> List[str]:
    prim = extract_primary_artist(artist)
    s_title = clean_text(title)
    # Если для ††† есть символ † в названии, заменяем его на t для поиска
    if "†" in prim or "†" in s_title:
        t_clean = re.sub(r"(?<=[A-Za-z])†|†(?=[A-Za-z])", "t", s_title).replace("†", "").strip()
        queries = []
        if t_clean:
            queries.append(f"Crosses {t_clean}".strip())
            queries.append(f"††† {t_clean}".strip())
        return queries or [f"Crosses {s_title}".strip()]

    q1 = f"{prim} {s_title}".strip()
    queries = [q1]
    if prim == "30 Seconds to Mars":
        queries.append(f"Thirty Seconds to Mars {s_title}".strip())
    return queries


async def lookup_deezer(
    client: httpx.AsyncClient,
    artist: str,
    title: str,
    duration: int = 0,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Поиск через Deezer API. Возвращает (meta, had_network_error)."""
    had_error = False
    candidates = []
    prim_norm = _normalize(extract_primary_artist(artist))

    for term in _search_queries(artist, title):
        try:
            resp = await client.get("https://api.deezer.com/search", params={"q": term, "limit": 10})
            if resp.status_code == 429 or resp.status_code >= 500:
                had_error = True
                continue
            if resp.status_code != 200:
                continue
            body = resp.json()
            if "error" in body:
                had_error = True
                continue
            data = body.get("data") or []
        except Exception as e:
            log.debug("Deezer search error: %s", e)
            had_error = True
            continue

        for it in data:
            cand_art = (it.get("artist") or {}).get("name") or ""
            cand_tit = it.get("title") or ""
            alb_obj = it.get("album") or {}
            cand_alb = alb_obj.get("title") or ""
            if not cand_alb:
                continue
            if _is_tribute(cand_art, cand_tit, cand_alb, prim_norm):
                continue
            if is_match(artist, title, cand_art, cand_tit):
                sc = _score_candidate(
                    title,
                    duration,
                    cand_art,
                    cand_tit,
                    cand_alb,
                    int(it.get("duration") or 0),
                    alb_obj.get("type") or "",
                )
                candidates.append((sc, it))
        if candidates:
            break

    if not candidates:
        return None, had_error

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]

    alb_obj = best.get("album") or {}
    collection = alb_obj.get("title")
    if not collection:
        return None, had_error

    cover_url = alb_obj.get("cover_xl") or alb_obj.get("cover_big") or alb_obj.get("cover")
    track_id = int(best.get("id") or 0)
    deezer_alb_id = int(alb_obj.get("id") or 0)

    track_no = 0
    disc_no = 1
    year = 0
    genre = ""

    if deezer_alb_id:
        alb_info = await _fetch_deezer_album(client, deezer_alb_id)
        year = alb_info.get("year") or 0
        genre = alb_info.get("genre") or ""

    if track_id:
        try:
            tr_resp = await client.get(f"https://api.deezer.com/track/{track_id}")
            if tr_resp.status_code == 200:
                tr_data = tr_resp.json()
                track_no = int(tr_data.get("track_position") or 0)
                disc_no = int(tr_data.get("disk_number") or 1)
                if not year:
                    r_date = tr_data.get("release_date") or ""
                    if len(r_date) >= 4 and r_date[:4].isdigit():
                        year = int(r_date[:4])
        except Exception:
            pass

    return {
        "album": collection,
        "track_number": track_no,
        "disc_number": disc_no,
        "year": year,
        "genre": genre,
        "cover_url": cover_url,
        "deezer_track_id": track_id,
        "deezer_album_id": deezer_alb_id,
    }, False


async def lookup_itunes(
    client: httpx.AsyncClient,
    artist: str,
    title: str,
    duration: int = 0,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Поиск через iTunes API (резервный). Возвращает (meta, had_network_error)."""
    had_error = False
    candidates = []
    prim_norm = _normalize(extract_primary_artist(artist))

    for term in _search_queries(artist, title):
        try:
            resp = await client.get(
                "https://itunes.apple.com/search",
                params={"term": term, "media": "music", "entity": "song", "limit": 10},
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                had_error = True
                continue
            if resp.status_code != 200:
                continue
            results = resp.json().get("results") or []
        except Exception:
            had_error = True
            continue

        for it in results:
            it_art = it.get("artistName") or ""
            it_tit = it.get("trackName") or ""
            it_alb = it.get("collectionName") or ""
            if not it_alb:
                continue
            if _is_tribute(it_art, it_tit, it_alb, prim_norm):
                continue
            if is_match(artist, title, it_art, it_tit):
                cand_dur = int((it.get("trackTimeMillis") or 0) / 1000)
                sc = _score_candidate(title, duration, it_art, it_tit, it_alb, cand_dur)
                candidates.append((sc, it))
        if candidates:
            break

    if not candidates:
        return None, had_error

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0][1]

    collection = best.get("collectionName")
    if not collection:
        return None, had_error

    # Убираем суффикс " - Single" / " - EP" только если в названии больше 3 символов
    year = 0
    rel_date = best.get("releaseDate") or ""
    if len(rel_date) >= 4 and rel_date[:4].isdigit():
        year = int(rel_date[:4])

    artwork = best.get("artworkUrl100") or ""
    if artwork:
        artwork = artwork.replace("100x100bb.jpg", "600x600bb.jpg")

    return {
        "album": collection,
        "track_number": int(best.get("trackNumber") or 0),
        "disc_number": int(best.get("discNumber") or 1),
        "year": year,
        "genre": best.get("primaryGenreName") or "",
        "cover_url": artwork or None,
        "deezer_track_id": 0,
        "deezer_album_id": int(best.get("collectionId") or 0),
    }, False


def build_search_text(artist: str, album_artist: str, title: str, album: str) -> str:
    """Строка в нижнем регистре для быстрого регистронезависимого поиска (включая кириллицу)."""
    parts = [
        artist or "",
        album_artist or "",
        title or "",
        album or "",
        _normalize(artist or ""),
        _normalize(album_artist or ""),
        _normalize(title or ""),
        _normalize(album or ""),
    ]
    return " ".join(p.lower() for p in parts if p)


async def enrich_track(
    artist: str,
    title: str,
    chat_id: int,
    channel_title: str,
    client: Optional[httpx.AsyncClient] = None,
    duration: int = 0,
) -> Dict[str, Any]:
    """Обогащение трека:
    - Исправляет кракозябры CP1251 и восстанавливает '30 Seconds to Mars'.
    - Группирует artist_id и album_id по основному исполнителю (без размножения из-за feat. и регистра).
    - Ищет в Deezer (с получением года и жанра альбома), затем в iTunes.
    - Необогащённые треки складывает в «{Артист} — Синглы» вместо общей свалки канала.
    """
    disp_artist = clean_display_artist(artist) or artist
    disp_title = clean_display_title(title) or title
    album_artist = extract_primary_artist(disp_artist)
    # Если сам артист отличается от канонического только регистром — используем канонический
    if disp_artist.lower() == album_artist.lower():
        disp_artist = album_artist

    s_art = _normalize(album_artist)
    s_tit = _normalize(clean_text(disp_title))
    term_key = f"{s_art}:{s_tit}"

    meta: Optional[Dict[str, Any]] = None
    status = "not_found"

    if term_key in _CACHE:
        cached_entry = _CACHE[term_key]
        meta = cached_entry.get("meta")
        status = cached_entry.get("status", "not_found")
    elif s_art and s_tit and disp_artist.lower() != clean_display_artist(channel_title).lower():
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "tg-stream/1.0"})
            should_close = True
        try:
            meta, dz_err = await lookup_deezer(client, disp_artist, disp_title, duration=duration)
            it_err = False
            if meta is None:
                meta, it_err = await lookup_itunes(client, disp_artist, disp_title, duration=duration)
            if meta is not None:
                status = "ok"
                _cache_put(term_key, {"status": "ok", "meta": meta})
            elif dz_err and it_err:
                status = "error"
            else:
                status = "not_found"
                _cache_put(term_key, {"status": "not_found", "meta": None})
        finally:
            if should_close:
                await client.aclose()

    art_id = _artist_id(album_artist)

    if meta and meta.get("album"):
        alb_name = clean_album_title(meta["album"])
        alb_id = album_id_for(album_artist, alb_name)
        return {
            "artist": disp_artist,
            "album_artist": album_artist,
            "artist_id": art_id,
            "title": disp_title,
            "album": alb_name,
            "album_id": alb_id,
            "track_number": int(meta.get("track_number") or 0),
            "disc_number": int(meta.get("disc_number") or 1),
            "year": int(meta.get("year") or 0),
            "genre": meta.get("genre") or "",
            "cover_url": meta.get("cover_url"),
            "deezer_track_id": int(meta.get("deezer_track_id") or 0),
            "deezer_album_id": int(meta.get("deezer_album_id") or 0),
            "enrich_status": "ok",
            "search_text": build_search_text(disp_artist, album_artist, disp_title, alb_name),
        }

    fb_album, fb_album_id = fallback_album_for(album_artist, channel_title)
    return {
        "artist": disp_artist,
        "album_artist": album_artist,
        "artist_id": art_id,
        "title": disp_title,
        "album": fb_album,
        "album_id": fb_album_id,
        "track_number": 0,
        "disc_number": 1,
        "year": 0,
        "genre": "",
        "cover_url": None,
        "deezer_track_id": 0,
        "deezer_album_id": 0,
        "enrich_status": status,
        "search_text": build_search_text(disp_artist, album_artist, disp_title, fb_album),
    }


_RE_DEEZER_ARTIST_PIC = re.compile(r"/images/artist/([0-9a-f]{32})/", re.IGNORECASE)


@asynccontextmanager
async def _http_client(client: Optional[httpx.AsyncClient]):
    if client is not None:
        yield client
        return
    async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": "tg-stream/1.0"}) as c:
        yield c


async def lookup_deezer_artist(
    client: Optional[httpx.AsyncClient],
    artist_name: str,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    prim = extract_primary_artist(clean_display_artist(artist_name) or artist_name)
    prim_norm = _normalize(prim)
    if not prim_norm:
        return None, False

    queries = [prim]
    if "†" in prim:
        queries.insert(0, "Crosses")
    elif prim == "30 Seconds to Mars":
        queries.append("Thirty Seconds to Mars")

    had_error = False
    best: Optional[Tuple[int, int, Dict[str, Any]]] = None
    async with _http_client(client) as http:
        for q in queries:
            try:
                resp = await http.get("https://api.deezer.com/search/artist", params={"q": q, "limit": 5})
                if resp.status_code == 429 or resp.status_code >= 500:
                    had_error = True
                    continue
                if resp.status_code != 200:
                    continue
                body = resp.json()
                if "error" in body:
                    had_error = True
                    continue
                for item in body.get("data") or []:
                    cand_norm = _normalize(item.get("name") or "")
                    if not cand_norm:
                        continue
                    exact = 1 if cand_norm == prim_norm else 0
                    if not exact and prim_norm not in cand_norm and cand_norm not in prim_norm:
                        continue
                    pic = item.get("picture_xl") or item.get("picture_big") or item.get("picture_medium") or ""
                    if not _RE_DEEZER_ARTIST_PIC.search(pic):
                        continue
                    score = (exact, int(item.get("nb_fan") or 0))
                    if best is None or score > (best[0], best[1]):
                        best = (
                            score[0],
                            score[1],
                            {"deezer_artist_id": int(item.get("id") or 0), "image_url": pic},
                        )
            except Exception as e:
                log.debug("Deezer artist lookup error (%s): %s", q, e)
                had_error = True
    return (best[2], False) if best is not None else (None, had_error)


_WIKI_MUSIC_HINTS = (
    "band", "musician", "singer", "rapper", "hip hop", "hip-hop", "rock", "metal",
    "music", "album", "dj", "producer", "composer", "songwriter", "vocalist",
    "группа", "музыкант", "певец", "певица", "рэпер", "рэп", "хип-хоп",
    "рок", "исполнитель", "альбом", "диджей", "композитор", "вокалист",
)


def _is_music_wiki(data: Dict[str, Any]) -> bool:
    if not isinstance(data, dict) or data.get("type") == "disambiguation":
        return False
    text = f"{data.get('description') or ''} {data.get('extract') or ''}".lower()
    return any(h in text for h in _WIKI_MUSIC_HINTS)


async def _fetch_wiki_bio(client: httpx.AsyncClient, artist_name: str) -> str:
    from urllib.parse import quote

    prim = extract_primary_artist(clean_display_artist(artist_name) or artist_name)
    if not prim:
        return ""
    search_name = "Crosses" if "†" in prim else prim
    candidates = [
        ("ru", search_name),
        ("ru", f"{search_name} (группа)"),
        ("ru", f"{search_name} (музыкант)"),
        ("en", search_name),
        ("en", f"{search_name} (band)"),
        ("en", f"{search_name} (musician)"),
    ]
    headers = {"User-Agent": "tg-stream/1.0 (https://music.pixelsolo.ru)"}
    for lang, title in candidates:
        try:
            url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}"
            resp = await client.get(url, headers=headers, follow_redirects=True)
            if resp.status_code != 200:
                continue
            data = resp.json()
            if _is_music_wiki(data):
                extract = (data.get("extract") or "").strip()
                if extract:
                    return extract
        except Exception:
            continue
    return ""


async def lookup_artist_extra(
    client: Optional[httpx.AsyncClient],
    artist_name: str,
    deezer_artist_id: int = 0,
) -> Tuple[str, List[Dict[str, Any]], int]:
    async with _http_client(client) as http:
        dz_id = int(deezer_artist_id or 0)
        if dz_id <= 0:
            res, _ = await lookup_deezer_artist(http, artist_name)
            if res:
                dz_id = int(res.get("deezer_artist_id") or 0)

        async def _fetch_related() -> List[Dict[str, Any]]:
            if dz_id <= 0:
                return []
            try:
                r = await http.get(f"https://api.deezer.com/artist/{dz_id}/related", params={"limit": 15})
                if r.status_code != 200:
                    return []
                out = []
                for item in r.json().get("data") or []:
                    name = (item.get("name") or "").strip()
                    if not name:
                        continue
                    pic = item.get("picture_xl") or item.get("picture_big") or item.get("picture_medium") or ""
                    if not _RE_DEEZER_ARTIST_PIC.search(pic):
                        pic = ""
                    out.append({"name": name, "deezer_id": int(item.get("id") or 0), "image_url": pic})
                return out
            except Exception:
                return []

        bio, related = await asyncio.gather(_fetch_wiki_bio(http, artist_name), _fetch_related())
        return bio, related, dz_id
