"""Telethon Telegram Bot Client Initialization and Lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from bot.handlers import register_handlers
from bot.uploader import UploadQueue
from core.config import Config
from core.task_manager import TaskManager
from storage.history import HistoryStore

logger = logging.getLogger(__name__)

__all__ = ["init_telegram_bot"]


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
            "Telethon is not installed. Telegram Bot cannot be started natively without telethon."
        )
        return None

    proxy = None
    if config.proxy:
        # Simple proxy parser if configured
        proxy = config.proxy

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

