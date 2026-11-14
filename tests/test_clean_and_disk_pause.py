from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from core.config import Config
from core.recorder import StreamInfo
from core.task_manager import JobStatus, TaskManager
from storage.history import HistoryStore
from bot.handlers import register_handlers


class TestCleanAndDiskPause(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.rec_dir = self.tmp_path / "recordings"
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.tmp_path / "history.json"
        self.store = HistoryStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    @patch("core.task_manager.is_disk_low", return_value=True)
    @patch("core.task_manager.probe_room")
    def test_task_manager_pauses_dispatch_when_disk_low(self, mock_probe, mock_disk):
        tm = TaskManager(
            max_concurrent=2,
            recordings_dir=self.rec_dir,
            min_free_mb=500,
        )
        mock_probe.return_value = StreamInfo(
            room_id="cam_live",
            is_live=True,
            hls_url="https://live.example.com/playlist.m3u8",
            resolution_label="1080p",
        )
        tm.start_tracking("cam_live")

        results = tm.check_all_room_statuses(dispatch_recording=True)
        job = tm.get_job("cam_live")

        # Job must stay in STANDBY because disk is low
        self.assertEqual(job.status, JobStatus.STANDBY)
        self.assertIn("Paused: low disk space", job.error_message)

    def test_handle_clean_command(self):
        cfg = Config(admin_ids=[12345])
        tm = TaskManager(recordings_dir=self.rec_dir)
        queue = MagicMock()

        # Create dummy recorded files
        room_dir = self.rec_dir / "chaturbate" / "streamer1"
        room_dir.mkdir(parents=True, exist_ok=True)
        vid1 = room_dir / "streamer1_rec.mp4"
        vid1.write_bytes(b"x" * 1024)

        self.store.record_upload(str(vid1), room_id="streamer1", size=1024)
        self.assertTrue(vid1.exists())
        self.assertEqual(len(self.store.list_records(active_only=True)), 1)

        # Mock Telegram Event
        event = MagicMock()
        event.sender_id = 12345
        event.raw_text = "/clean streamer1"

        replies = []
        async def mock_reply(text):
            replies.append(text)
        event.reply = mock_reply

        dispatch = register_handlers(
            client=MagicMock(),
            config=cfg,
            task_manager=tm,
            upload_queue=queue,
            history_store=self.store,
        )

        import asyncio
        asyncio.run(dispatch["/clean"](event))

        # File must be deleted and purged
        self.assertFalse(vid1.exists())
        self.assertEqual(len(self.store.list_records(active_only=True)), 0)
        self.assertTrue(any("Đã xóa thủ công 1 video" in r for r in replies))


if __name__ == "__main__":
    unittest.main()
