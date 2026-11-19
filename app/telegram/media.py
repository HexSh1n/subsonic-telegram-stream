"""Загрузка файла из Telegram с произвольного смещения.

MTProto отдаёт данные кусками с выравниванием на 4 КБ — выравниваем смещение
вниз и пропускаем лишние байты первого куска.
"""
from __future__ import annotations

from typing import AsyncIterator

TG_ALIGNMENT = 4096
REQUEST_SIZE = 512 * 1024


async def iter_file_chunks(client, message, offset: int) -> AsyncIterator[bytes]:
    aligned = offset - (offset % TG_ALIGNMENT)
    skip = offset - aligned
    async for chunk in client.iter_download(
        message, offset=aligned, request_size=REQUEST_SIZE
    ):
        if skip:
            if len(chunk) <= skip:
                skip -= len(chunk)
                continue
            chunk = chunk[skip:]
            skip = 0
        yield chunk
