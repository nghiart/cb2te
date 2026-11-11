from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from core.recorder import StreamInfo
from core.task_manager import JobStatus, TaskManager


class TestStatusChangeNotification(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.rec_dir = Path(self.tmp.name)
        self.tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)

    def tearDown(self):
        self.tm.stop_all()
        self.tmp.cleanup()

    @patch("core.task_manager.probe_room")
    def test_status_change_callback_triggered(self, mock_probe):
        callback_events = []

        def on_change(room, old_st, new_st, info):
            callback_events.append((room, old_st, new_st))

        self.tm.set_on_status_change_callback(on_change)
        self.tm.start_tracking("cam_test")

        # 1st probe: initial state (offline)
        mock_probe.return_value = StreamInfo(room_id="cam_test", is_live=False, room_status="offline")
        res1 = self.tm.check_all_room_statuses()
        self.assertEqual(len(callback_events), 0)  # No change from initial unknown

        # 2nd probe: status changes to online (1080p)
        mock_probe.return_value = StreamInfo(
            room_id="cam_test",
            is_live=True,
            hls_url="https://edge.example.com/live.m3u8",
            width=1920,
            height=1080,
            resolution_label="1080p",
            room_status="public",
        )
        res2 = self.tm.check_all_room_statuses()
        self.assertEqual(len(callback_events), 1)
        self.assertEqual(callback_events[0], ("cam_test", "offline", "online (1080p)"))

        # 3rd probe: status changes to private
        mock_probe.return_value = StreamInfo(room_id="cam_test", is_live=False, room_status="private")
        res3 = self.tm.check_all_room_statuses()
        self.assertEqual(len(callback_events), 2)
        self.assertEqual(callback_events[1], ("cam_test", "online (1080p)", "private"))


if __name__ == "__main__":
    unittest.main()

