import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bot.uploader import UploadItem, UploadQueue
from core.config import Config
from core.recorder import StreamInfo
from core.task_manager import JobStatus, TaskManager
from storage.cleaner import RetentionCleaner
from storage.history import HistoryStore


class TestEndToEndPipeline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.rec_dir = self.base_dir / "recordings"
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / "history.json"
        self.store = HistoryStore(self.db_path)
        self.cleaner = RetentionCleaner(recordings_dir=self.rec_dir)
        self.uq = UploadQueue()
        self.tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)

    def tearDown(self):
        self.tm.stop_all(wait=True, timeout=2.0)
        self.tmp.cleanup()

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    async def test_full_e2e_recording_to_upload_and_retention(self, mock_record, mock_probe):
        rec_done = asyncio.Event()

        # 1. Wire TaskManager completion callback to UploadQueue
        def on_file_ready(out_path: Path, room_id: str, duration: float):
            item = UploadItem(
                file_path=out_path,
                room_id=room_id,
                duration_sec=int(duration),
            )
            self.uq.enqueue(item)
            try:
                loop.call_soon_threadsafe(rec_done.set)
            except Exception:
                pass

        self.tm.set_on_file_ready_callback(on_file_ready)

        # 2. Track room and mock live stream
        self.assertTrue(self.tm.start_tracking("streamer_vip"))
        mock_probe.return_value = StreamInfo(
            room_id="streamer_vip",
            is_live=True,
            hls_url="https://live.cb.com/hls.m3u8",
        )

        def fake_record(info, out_mp4, stop_event, **kwargs):
            p = Path(out_mp4)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"Simulated MP4 video bytes" * 1000)
            return p

        mock_record.side_effect = fake_record

        # 3. Setup mock Telegram client
        mock_client = MagicMock()
        mock_progress_msg = MagicMock()
        mock_progress_msg.edit = AsyncMock()
        mock_progress_msg.delete = AsyncMock()
        mock_client.send_message = AsyncMock(return_value=mock_progress_msg)
        mock_client.send_file = AsyncMock()
        mock_client.delete_messages = AsyncMock()

        # Start upload queue worker
        await self.uq.start_worker(client=mock_client, chat_id=-100999, store=self.store)

        # 4. Trigger poll batch
        loop = asyncio.get_running_loop()
        self.tm.poll_staggered_batch(stagger_seconds=0.0)

        # Wait until recording finished and item enqueued
        await asyncio.wait_for(rec_done.wait(), timeout=3.0)

        # 5. Wait for queue to process item
        await asyncio.wait_for(self.uq.join(), timeout=4.0)
        await self.uq.stop_worker()

        # Verify upload happened
        mock_client.send_file.assert_called_once()
        sent_kwargs = mock_client.send_file.call_args.kwargs
        self.assertIn("#streamer_vip", sent_kwargs.get("caption", ""))

        # Verify HistoryStore recorded upload
        records = self.store.list_records(active_only=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].room_id, "streamer_vip")
        uploaded_video_path = Path(records[0].file_path)
        self.assertTrue(uploaded_video_path.exists())

        # 6. Retention check: Not expired yet (< 48h)
        deleted = self.cleaner.run_cleanup_cycle(self.store, retention_hours=48, min_free_mb=0)
        self.assertEqual(len(deleted), 0)
        self.assertTrue(uploaded_video_path.exists())

        # 7. Retention check: Expired (> 48h)
        records[0].uploaded_at = time.time() - 50 * 3600
        self.store.save()

        deleted = self.cleaner.run_cleanup_cycle(self.store, retention_hours=48, min_free_mb=0)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].name, uploaded_video_path.name)
        self.assertFalse(uploaded_video_path.exists())


if __name__ == "__main__":
    unittest.main()
