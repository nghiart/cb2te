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

    @patch("core.task_manager.probe_room")
    def test_batch_status_change_callback_triggered(self, mock_probe):
        batch_events = []

        def on_batch(all_statuses, changes):
            batch_events.append((all_statuses, changes))

        self.tm.set_on_batch_status_change_callback(on_batch)
        self.tm.start_tracking("room_a")
        self.tm.start_tracking("room_b")

        # 1st probe: both offline initially
        mock_probe.side_effect = [
            StreamInfo(room_id="room_a", is_live=False, room_status="offline"),
            StreamInfo(room_id="room_b", is_live=False, room_status="offline"),
        ]
        self.tm.check_all_room_statuses()
        self.assertEqual(len(batch_events), 0)

        # 2nd probe: room_a turns online, room_b stays offline
        mock_probe.side_effect = [
            StreamInfo(room_id="room_a", is_live=True, hls_url="http://x", resolution_label="720p"),
            StreamInfo(room_id="room_b", is_live=False, room_status="offline"),
        ]
        self.tm.check_all_room_statuses()
        self.assertEqual(len(batch_events), 1)
        all_st, ch = batch_events[0]
        self.assertIn("room_a", all_st)
        self.assertIn("room_b", all_st)
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0][0], "room_a")
        self.assertEqual(ch[0][1], "offline")
        self.assertEqual(ch[0][2], "online (720p)")

        # 3rd probe: no change, batch callback must NOT be called again
        mock_probe.side_effect = [
            StreamInfo(room_id="room_a", is_live=True, hls_url="http://x", resolution_label="720p"),
            StreamInfo(room_id="room_b", is_live=False, room_status="offline"),
        ]
        self.tm.check_all_room_statuses()
        self.assertEqual(len(batch_events), 1)

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_ffmpeg")
    def test_session_lifecycle_callbacks(self, mock_record, mock_probe):
        started_events = []
        finished_events = []

        def on_started(room_id, info, out_path):
            started_events.append((room_id, info.resolution_label, out_path.name))

        def on_finished(room_id, dur, b_rec, saved_path):
            finished_events.append((room_id, dur, b_rec, saved_path.name if saved_path else None))

        self.tm.use_ffmpeg_recorder = True
        self.tm.set_on_session_started_callback(on_started)
        self.tm.set_on_session_finished_callback(on_finished)
        self.tm.start_tracking("cam_life")

        mock_probe.return_value = StreamInfo(
            room_id="cam_life",
            is_live=True,
            hls_url="https://edge.example.com/live.m3u8",
            resolution_label="1080p",
        )

        fake_out = self.rec_dir / "chaturbate" / "cam_life" / "test.mp4"
        fake_out.parent.mkdir(parents=True, exist_ok=True)
        fake_out.write_bytes(b"data" * 100)
        mock_record.return_value = fake_out

        # Dispatch recording
        self.tm.check_all_room_statuses(dispatch_recording=True)

        # Worker thread executes in background
        job = self.tm.get_job("cam_life")
        if job and job.thread:
            job.thread.join(timeout=2.0)

        self.assertEqual(len(started_events), 1)
        self.assertEqual(started_events[0][0], "cam_life")
        self.assertEqual(started_events[0][1], "1080p")

        self.assertEqual(len(finished_events), 1)
        self.assertEqual(finished_events[0][0], "cam_life")
        self.assertEqual(finished_events[0][3], "test.mp4")


if __name__ == "__main__":
    unittest.main()

