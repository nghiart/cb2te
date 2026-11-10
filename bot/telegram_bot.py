"""Telethon Telegram Bot Client Initialization and Lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from bot.handlers import register_handlers
from bot.uploader import UploadQueue
from core.config import Config
from core.net import ProxyConfigError, normalize_proxy
from core.task_manager import TaskManager
from storage.history import HistoryStore

logger = logging.getLogger(__name__)

__all__ = ["init_telegram_bot", "telethon_proxy_dict"]

#: Map our proxy URL schemes to the ``proxy_type`` values Telethon understands.
_TELETHON_PROXY_TYPES = {
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks4": "socks4",
    "socks4a": "socks4",
    "http": "http",
    "https": "http",
}


def telethon_proxy_dict(proxy_url: str) -> Optional[dict]:
    """Convert ``PROXY = socks5://user:pass@host:1080`` into Telethon's dict form.

    Telethon expects a dict (not a URL string) and needs ``python-socks[asyncio]``
    installed when a proxy is used.  Invalid configuration is logged instead of
    crashing the daemon - the recorder keeps working, only Telegram is skipped.
    """
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    try:
        info = normalize_proxy(raw)
    except ProxyConfigError as exc:
        logger.error("PROXY không hợp lệ (%s): %s", raw, exc)
        return None
    if info is None:
        return None

    proxy_type = _TELETHON_PROXY_TYPES.get(info.scheme)
    if proxy_type is None:
        logger.error("Telethon không hỗ trợ proxy scheme '%s' (%s)", info.scheme, raw)
        return None

    proxy: dict[str, Any] = {
        "proxy_type": proxy_type,
        "addr": info.host,
        "port": info.port,
        "rdns": info.rdns,
    }
    if info.username:
        proxy["username"] = info.username
        proxy["password"] = info.password
    return proxy


def init_telegram_bot(
    config: Config,
    task_manager: TaskManager,
    upload_queue: UploadQueue,
    history_store: HistoryStore,
    favorites_path: Optional[Path | str] = None,
    session_name: str = "cb2te_bot_session",
) -> Any:
    """Initialize Telethon TelegramClient and register command handlers."""
    try:
        from telethon import TelegramClient
    except ImportError:
        logger.warning(
            "Telethon is not installed. Telegram Bot cannot be started natively "
            "without telethon (pip install telethon)."
        )
        return None

    proxy = telethon_proxy_dict(config.proxy)
    if proxy is not None:
        try:
            import python_socks  # noqa: F401
        except ImportError:
            logger.warning(
                "PROXY được cấu hình nhưng thiếu python-socks — bot Telegram sẽ "
                "không kết nối được. Cài bằng: pip install 'python-socks[asyncio]'"
            )

    client = TelegramClient(session_name, config.api_id, config.api_hash, proxy=proxy)
    register_handlers(
        client=client,
        config=config,
        task_manager=task_manager,
        upload_queue=upload_queue,
        history_store=history_store,
        favorites_path=favorites_path,
    )
    return client
