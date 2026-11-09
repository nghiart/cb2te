import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bot.handlers import (
    format_files_message,
    format_help_message,
    format_status_message,
    is_authorized,
    register_handlers,
)
from core.config import Config
from core.task_manager import JobStatus, RecordingJob, TaskManager
from storage.history import HistoryRecord, HistoryStore


class TestBotHandlers(unittest.TestCase):
    def test_authorization(self):
        admin_ids = [111, 222]
        self.assertTrue(is_authorized(111, admin_ids))
        self.assertTrue(is_authorized(222, admin_ids))
        self.assertFalse(is_authorized(999, admin_ids))
        # Empty admin_ids -> allow none or default deny
        self.assertFalse(is_authorized(111, []))

    def test_format_status_message(self):
        msg = format_status_message(
            active_jobs=[{"room_id": "model_a", "status": "RECORDING", "size_mb": 45}],
            queue_status={"pending_count": 0},
            free_disk_mb=12000,
            retention_files_count=3,
        )
        self.assertIn("model_a", msg)
        self.assertIn("RECORDING", msg)
        self.assertIn("12000", msg)

    def test_format_help_message(self):
        msg = format_help_message()
        self.assertIn("/status", msg)
        self.assertIn("/rec", msg)
        self.assertIn("/stop", msg)
        self.assertIn("/fav", msg)
        self.assertIn("/add", msg)
        self.assertIn("/del", msg)
        self.assertIn("/files", msg)

    def test_format_files_message(self):
        records = [
            HistoryRecord(file_path="chaturbate_m1.mp4", room_id="m1", uploaded_at=1000.0, size=50_000_000),
            HistoryRecord(file_path="chaturbate_m2.mp4", room_id="m2", uploaded_at=2000.0, size=150_000_000),
        ]
        msg = format_files_message(records, retention_hours=48)
        self.assertIn("m1", msg)
        self.assertIn("m2", msg)


class TestBotCommandDispatching(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.fav_file = self.base_dir / "favorites.txt"
        self.fav_file.write_text("model_fav1\nmodel_fav2\n")

        self.cfg = Config(
            api_id=123,
            api_hash="abc",
            bot_token="token",
            chat_id=-100123,
            admin_ids=[111],
        )
        self.tm = TaskManager(max_concurrent=2, recordings_dir=self.base_dir)
        self.db_path = self.base_dir / "history.json"
        self.store = HistoryStore(self.db_path)
        self.uq = MagicMock()
        self.uq.get_queue_status.return_value = {"pending_count": 0, "items": []}

    def tearDown(self):
        self.tmp.cleanup()

    async def test_unauthorized_user_ignored_or_rejected(self):
        mock_client = MagicMock()
        handlers = register_handlers(
            mock_client,
            self.cfg,
            self.tm,
            self.uq,
            self.store,
            favorites_path=self.fav_file,
        )

        mock_event = MagicMock()
        mock_event.sender_id = 999  # Not in admin_ids
        mock_event.raw_text = "/status"
        mock_event.reply = AsyncMock()

        handler = handlers.get("/status")
        self.assertIsNotNone(handler)
        await handler(mock_event)

        # Unauthorized: should not return status info
        mock_event.reply.assert_not_called()

    async def test_start_and_help_commands(self):
        mock_client = MagicMock()
        handlers = register_handlers(
            mock_client,
            self.cfg,
            self.tm,
            self.uq,
            self.store,
            favorites_path=self.fav_file,
        )

        mock_event = MagicMock()
        mock_event.sender_id = 111
        mock_event.raw_text = "/help"
        mock_event.reply = AsyncMock()

        handler = handlers.get("/help")
        await handler(mock_event)
        mock_event.reply.assert_called_once()
        reply_text = mock_event.reply.call_args[0][0]
        self.assertIn("/status", reply_text)

    async def test_rec_and_stop_commands(self):
        mock_client = MagicMock()
        handlers = register_handlers(
            mock_client,
            self.cfg,
            self.tm,
            self.uq,
            self.store,
            favorites_path=self.fav_file,
        )

        # /rec new_model
        mock_event = MagicMock()
        mock_event.sender_id = 111
        mock_event.raw_text = "/rec new_model"
        mock_event.reply = AsyncMock()

        await handlers["/rec"](mock_event)
        self.assertIsNotNone(self.tm.get_job("new_model"))
        mock_event.reply.assert_called_once()

        # /stop new_model
        mock_event_stop = MagicMock()
        mock_event_stop.sender_id = 111
        mock_event_stop.raw_text = "/stop new_model"
        mock_event_stop.reply = AsyncMock()

        await handlers["/stop"](mock_event_stop)
        self.assertIsNone(self.tm.get_job("new_model"))
        mock_event_stop.reply.assert_called_once()

    async def test_fav_add_del_commands(self):
        mock_client = MagicMock()
        handlers = register_handlers(
            mock_client,
            self.cfg,
            self.tm,
            self.uq,
            self.store,
            favorites_path=self.fav_file,
        )

        # /fav
        mock_event = MagicMock()
        mock_event.sender_id = 111
        mock_event.raw_text = "/fav"
        mock_event.reply = AsyncMock()
        await handlers["/fav"](mock_event)
        reply = mock_event.reply.call_args[0][0]
        self.assertIn("model_fav1", reply)

        # /add added_model
        mock_add = MagicMock()
        mock_add.sender_id = 111
        mock_add.raw_text = "/add added_model"
        mock_add.reply = AsyncMock()
        await handlers["/add"](mock_add)
        self.assertIn("added_model", self.fav_file.read_text())

        # /del added_model
        mock_del = MagicMock()
        mock_del.sender_id = 111
        mock_del.raw_text = "/del added_model"
        mock_del.reply = AsyncMock()
        await handlers["/del"](mock_del)
        self.assertNotIn("added_model", self.fav_file.read_text())

    def test_init_telegram_bot_fallback(self):
        from bot.telegram_bot import init_telegram_bot
        # When telethon is missing or mocked
        res = init_telegram_bot(self.cfg, self.tm, self.uq, self.store)
        # Returns client if telethon available else None
        self.assertTrue(res is None or hasattr(res, "on"))


if __name__ == "__main__":
    unittest.main()
