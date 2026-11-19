"""Загрузка config.yaml и вычисляемые пути проекта."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class TelegramConfig:
    api_id: int
    api_hash: str
    session: str = "tg-stream"
    channels: List[str] = field(default_factory=list)
    # опциональный прокси: {"type": "mtproto"|"socks5"|"socks4"|"http",
    #                       "server": ..., "port": ..., "secret": ...}
    proxy: Optional[dict] = None


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class UserConfig:
    username: str
    password: str


@dataclass
class CacheConfig:
    dir: str = "./cache"
    max_bytes: int = 10 * 1024 * 1024 * 1024


@dataclass
class AppConfig:
    telegram: TelegramConfig
    server: ServerConfig
    users: List[UserConfig]
    cache: CacheConfig

    @property
    def session_path(self) -> Path:
        return PROJECT_ROOT / f"{self.telegram.session}.session"

    @property
    def db_path(self) -> Path:
        return PROJECT_ROOT / "data" / "library.db"

    @property
    def cache_dir(self) -> Path:
        p = Path(self.cache.dir)
        return p if p.is_absolute() else PROJECT_ROOT / p


def load_config(path: Optional[Path] = None) -> AppConfig:
    path = path or (PROJECT_ROOT / "config.yaml")
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден {path}. Скопируй config.example.yaml в config.yaml и заполни (api_id/api_hash, каналы, пользователи)."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    tg = raw.get("telegram") or {}
    api_id = int(tg.get("api_id") or 0)
    api_hash = str(tg.get("api_hash") or "")
    if not api_id or not api_hash:
        raise ValueError(
            "telegram.api_id и telegram.api_hash обязательны — получи их на https://my.telegram.org (API development tools)."
        )

    users_raw = raw.get("users") or []
    users = [UserConfig(str(u["username"]), str(u["password"])) for u in users_raw]
    if not users:
        raise ValueError("В config.yaml нужен хотя бы один пользователь (users:).")

    srv = raw.get("server") or {}
    cch = raw.get("cache") or {}

    return AppConfig(
        telegram=TelegramConfig(
            api_id=api_id,
            api_hash=api_hash,
            session=str(tg.get("session", "tg-stream")),
            channels=[str(c) for c in (tg.get("channels") or [])],
            proxy=(dict(tg["proxy"]) if tg.get("proxy") else None),
        ),
        server=ServerConfig(str(srv.get("host", "0.0.0.0")), int(srv.get("port", 8080))),
        users=users,
        cache=CacheConfig(
            dir=str(cch.get("dir", "./cache")),
            max_bytes=int(cch.get("max_bytes", 10 * 1024 * 1024 * 1024)),
        ),
    )
