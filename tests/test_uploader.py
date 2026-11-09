import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bot.uploader import UploadItem, UploadQueue
from storage.history import HistoryStore


class TestUploader(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.db_path = self.base_dir / "history.json"
        self.store = HistoryStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_enqueue_and_queue_length(self):
        uq = UploadQueue()
        item = UploadItem(
            file_path=self.base_dir / "rec.mp4",
            room_id="room_x",
            duration_sec=120,
        )
        uq.enqueue(item)
        status = uq.get_queue_status()
        self.assertEqual(status["pending_count"], 1)
        self.assertEqual(status["items"][0]["room_id"], "room_x")

    async def test_worker_upload_success_and_history_recorded(self):
        uq = UploadQueue()
        video_file = self.base_dir / "chaturbate_model_20261109_120000.mp4"
        video_file.write_bytes(b"A" * 1024 * 1024)  # 1 MB

        item = UploadItem(
            file_path=video_file,
            room_id="model",
            duration_sec=3665,
        )

        mock_client = MagicMock()
        progress_msg = MagicMock()
        progress_msg.id = 123
        progress_msg.edit = AsyncMock()
        progress_msg.delete = AsyncMock()

        mock_client.send_message = AsyncMock(return_value=progress_msg)
        mock_client.delete_messages = AsyncMock()

        async def fake_send_file(chat_id, file, progress_callback=None, caption="", **kwargs):
            if progress_callback:
                progress_callback(512 * 1024, 1024 * 1024)
                progress_callback(1024 * 1024, 1024 * 1024)
            return MagicMock()

        mock_client.send_file = AsyncMock(side_effect=fake_send_file)

        uq.enqueue(item)
        await uq.start_worker(client=mock_client, chat_id=-10012345, store=self.store)

        # Wait until item is processed
        await asyncio.wait_for(uq.join(), timeout=3.0)
        await uq.stop_worker()

        # Check send_message called for initial status
        mock_client.send_message.assert_called_once()
        # Check send_file called
        mock_client.send_file.assert_called_once()
        call_kwargs = mock_client.send_file.call_args.kwargs
        self.assertIn("#model", call_kwargs.get("caption", ""))

        # Check delete progress message was called
        self.assertTrue(progress_msg.delete.called or mock_client.delete_messages.called)

        # Check HistoryStore recorded the upload
        records = self.store.list_records(active_only=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].room_id, "model")
        self.assertEqual(records[0].status, "uploaded")

    async def test_worker_handles_upload_error(self):
        uq = UploadQueue()
        bad_file = self.base_dir / "error.mp4"
        bad_file.write_bytes(b"sample")

        item = UploadItem(file_path=bad_file, room_id="err_room", duration_sec=60)

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock(return_value=MagicMock())
        mock_client.send_file = AsyncMock(side_effect=RuntimeError("Network drop"))
        mock_client.delete_messages = AsyncMock()

        uq.enqueue(item)
        await uq.start_worker(client=mock_client, chat_id=-10012345, store=self.store)

        await asyncio.wait_for(uq.join(), timeout=3.0)
        await uq.stop_worker()

        # No successful upload in store
        records = self.store.list_records(active_only=True)
        self.assertEqual(len(records), 0)

    def test_format_duration(self):
        from bot.uploader import format_duration
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(125), "2m05s")
        self.assertEqual(format_duration(3665), "1h01m")
        self.assertEqual(format_duration(7200), "2h00m")

    async def test_nonexistent_file_skipped(self):
        uq = UploadQueue()
        missing = self.base_dir / "does_not_exist.mp4"
        item = UploadItem(file_path=missing, room_id="ghost", duration_sec=10)

        mock_client = MagicMock()
        mock_client.send_message = AsyncMock()
        mock_client.send_file = AsyncMock()

        uq.enqueue(item)
        await uq.start_worker(client=mock_client, chat_id=-10012345, store=self.store)

        await asyncio.wait_for(uq.join(), timeout=3.0)
        await uq.stop_worker()

        mock_client.send_message.assert_not_called()
        mock_client.send_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
