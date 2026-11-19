"""Обёртка над Telethon-клиентом: подключение, прокси, получение сообщений."""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Optional

from telethon import TelegramClient
from telethon.tl.types import InputPeerChannel

log = logging.getLogger("tgstream.tg")


def build_telethon_client(cfg) -> TelegramClient:
    """TelegramClient с учётом опционального прокси из config.yaml.

    Прокси не нужен в финальной версии — просто удали секцию telegram.proxy.
    """
    proxy = getattr(cfg.telegram, "proxy", None)
    kwargs = {}
    if proxy:
        ptype = str(proxy.get("type", "mtproto")).lower()
        server, port = str(proxy["server"]), int(proxy["port"])
        if ptype == "mtproto":
            from telethon.network.connection import ConnectionTcpMTProxyRandomizedIntermediate

            kwargs["connection"] = ConnectionTcpMTProxyRandomizedIntermediate
            kwargs["proxy"] = (server, port, str(proxy["secret"]))
            log.info("Telegram: используется MTProto-прокси %s:%d", server, port)
        else:  # socks5 / socks4 / http — нужен PySocks: pip install "telethon[socks]"
            kwargs["proxy"] = (ptype, server, port)
            log.info("Telegram: используется %s-прокси %s:%d", ptype, server, port)
    return TelegramClient(str(cfg.session_path), cfg.telegram.api_id, cfg.telegram.api_hash, **kwargs)


class TgGateway:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = build_telethon_client(cfg)
        self.ready = False
        self._msg_cache: OrderedDict = OrderedDict()

    async def start(self) -> None:
        try:
            await self.client.connect()
            self.ready = await self.client.is_user_authorized()
            if self.ready:
                me = await self.client.get_me()
                log.info("Telegram: авторизован как %s", getattr(me, "username", None) or getattr(me, "first_name", "?"))
            else:
                log.warning("Telegram: сессия не авторизована — запусти `python -m app.login`")
        except Exception:
            log.exception("Telegram: не удалось подключиться")

    async def stop(self) -> None:
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def get_message(self, chat_id: int, msg_id: int):
        """Сообщение по (chat_id, msg_id) с кэшем в памяти и фолбэком через access_hash."""
        key = (chat_id, msg_id)
        cached = self._msg_cache.get(key)
        if cached is not None:
            msg, ts = cached
            if time.monotonic() - ts < 1800:
                self._msg_cache.move_to_end(key)
                return msg
            self._msg_cache.pop(key, None)

        msg = None
        try:
            msg = await self.client.get_messages(chat_id, ids=msg_id)
        except Exception as e:
            log.debug("get_messages(%s, %s) через сессию не удался: %s", chat_id, msg_id, e)

        if msg is None:
            # file_reference в кэше сессии мог протухнуть — пробуем по явному пиру
            from ..runtime import runtime

            access_hash = await runtime.lib.channel_access_hash(chat_id) if runtime.lib else None
            if access_hash is None:
                return None
            try:
                peer = InputPeerChannel(chat_id, access_hash)
                msg = await self.client.get_messages(peer, ids=msg_id)
            except Exception as e:
                log.warning("get_messages(%s, %s) через access_hash не удался: %s", chat_id, msg_id, e)
                return None

        if msg is not None:
            self._msg_cache[key] = (msg, time.monotonic())
            while len(self._msg_cache) > 500:
                self._msg_cache.popitem(last=False)
        return msg
