"""Общий код Subsonic API: конверт ответов, авторизация, парсинг Range."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from fastapi.responses import JSONResponse

API_VERSION = "1.16.1"


def envelope_ok(payload: Optional[dict] = None) -> JSONResponse:
    body = {"subsonic-response": {"status": "ok", "version": API_VERSION}}
    if payload:
        body["subsonic-response"].update(payload)
    return JSONResponse(body)


def envelope_error(code: int, message: str) -> JSONResponse:
    return JSONResponse({
        "subsonic-response": {
            "status": "failed",
            "version": API_VERSION,
            "error": {"code": code, "message": message},
        }
    })


def authenticate(qp, users) -> Optional[object]:
    """Subsonic-аутентификация: t=md5(password+s)&s=salt либо p=пароль."""
    username = qp.get("u")
    if not username:
        return None
    user = next((u for u in users if u.username == username), None)
    if user is None:
        return None

    token, salt = qp.get("t"), qp.get("s")
    if token and salt:
        expected = hashlib.md5((user.password + salt).encode("utf-8")).hexdigest()
        return user if token.lower() == expected else None

    plain = qp.get("p")
    if plain:
        if plain.startswith("enc:"):
            try:
                plain = bytes.fromhex(plain[4:]).decode("utf-8")
            except ValueError:
                return None
        return user if plain == user.password else None
    return None


def parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int, bool]]:
    """Range-заголовок -> (start, end, is_range). None = невалидный запрос.

    Без заголовка — весь файл. Поддержаны bytes=a-b, bytes=a-, bytes=-n.
    """
    if not header:
        return (0, max(size - 1, 0), False)
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m:
        return None
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return None
    if a == "":  # суффиксный диапазон: последние n байт
        n = int(b)
        if n <= 0 or size <= 0:
            return None
        return (max(0, size - n), size - 1, True)
    start = int(a)
    if start >= size or size <= 0:
        return None
    end = min(int(b), size - 1) if b else size - 1
    if start > end:
        return None
    return (start, end, True)


def iso_ts(ts: int) -> str:
    if not ts:
        ts = 0
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


MIME_BY_EXT = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "m4a": "audio/mp4",
    "aac": "audio/aac", "ogg": "audio/ogg", "opus": "audio/ogg",
    "wav": "audio/x-wav", "wma": "audio/x-ms-wma",
}


def mime_for(ext: Optional[str], fallback: Optional[str] = None) -> str:
    if fallback and "/" in fallback:
        return fallback
    return MIME_BY_EXT.get((ext or "").lower(), "audio/mpeg")
