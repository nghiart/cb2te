import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.recorder import StreamInfo
from core.task_manager import JobStatus, RecordingJob, TaskManager


class TestTaskManager(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.rec_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_job_lifecycle_and_concurrency(self):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        self.assertTrue(tm.start_tracking("room1"))
        self.assertTrue(tm.start_tracking("room2"))
        self.assertTrue(tm.start_tracking("room3"))

        # Duplicate start returns False
        self.assertFalse(tm.start_tracking("room1"))
        self.assertFalse(tm.start_tracking(""))

        jobs = tm.get_jobs()
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(j.status == JobStatus.STANDBY for j in jobs))

        # Test stop
        self.assertTrue(tm.stop_tracking("room1"))
        self.assertEqual(len(tm.get_jobs()), 2)
        # Non-existent stop returns False
        self.assertFalse(tm.stop_tracking("room1"))
        self.assertFalse(tm.stop_tracking(""))

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_concurrency_limit_governance(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("cam1")
        tm.start_tracking("cam2")
        tm.start_tracking("cam3")

        mock_probe.side_effect = lambda room, proxy="": StreamInfo(
            room_id=room, is_live=True, hls_url=f"https://live.cb.com/{room}.m3u8"
        )

        started_events = {
            "cam1": threading.Event(),
            "cam2": threading.Event(),
            "cam3": threading.Event(),
        }

        def fake_record(info, out_mp4, stop_event, **kwargs):
            started_events[info.room_id].set()
            # Wait for stop event or short duration
            stop_event.wait(timeout=5.0)
            return Path(out_mp4)

        mock_record.side_effect = fake_record

        # Poll batch without stagger delay for fast tests
        tm.poll_staggered_batch(stagger_seconds=0.0)

        # Wait for cam1 and cam2 threads to spin up
        self.assertTrue(started_events["cam1"].wait(timeout=2.0))
        self.assertTrue(started_events["cam2"].wait(timeout=2.0))

        active_jobs = tm.get_active_jobs()
        self.assertEqual(len(active_jobs), 2)
        active_ids = {j.room_id for j in active_jobs}
        self.assertIn("cam1", active_ids)
        self.assertIn("cam2", active_ids)

        # cam3 must remain in STANDBY because max_concurrent is 2
        cam3_job = next(j for j in tm.get_jobs() if j.room_id == "cam3")
        self.assertEqual(cam3_job.status, JobStatus.STANDBY)
        self.assertFalse(started_events["cam3"].is_set())

        # Cleanup
        tm.stop_all(wait=True, timeout=2.0)

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_slot_frees_up_and_next_job_starts(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=1, recordings_dir=self.rec_dir)
        tm.start_tracking("cam1")
        tm.start_tracking("cam2")

        mock_probe.side_effect = lambda room, proxy="": StreamInfo(
            room_id=room, is_live=True, hls_url=f"https://live.cb.com/{room}.m3u8"
        )

        cam1_done = threading.Event()

        def fake_record_cam1(info, out_mp4, stop_event, **kwargs):
            p = Path(out_mp4)
            p.write_bytes(b"dummy video")
            cam1_done.set()
            return p

        mock_record.side_effect = fake_record_cam1

        # Poll first batch: cam1 starts and completes immediately
        tm.poll_staggered_batch(stagger_seconds=0.0)
        self.assertTrue(cam1_done.wait(timeout=2.0))

        # Give a moment for the cam1 worker thread to exit and transition to STANDBY
        time.sleep(0.05)
        self.assertEqual(len(tm.get_active_jobs()), 0)

        # Now available slots is 1. Next poll starts cam2!
        cam2_started = threading.Event()

        def fake_record_cam2(info, out_mp4, stop_event, **kwargs):
            cam2_started.set()
            stop_event.wait(timeout=5.0)
            return Path(out_mp4)

        mock_record.side_effect = fake_record_cam2
        tm.poll_staggered_batch(stagger_seconds=0.0)

        self.assertTrue(cam2_started.wait(timeout=2.0))
        active_jobs = tm.get_active_jobs()
        self.assertEqual(len(active_jobs), 1)
        self.assertEqual(active_jobs[0].room_id, "cam2")

        tm.stop_all(wait=True, timeout=2.0)

    @patch("core.task_manager.probe_room")
    def test_probe_offline_room_remains_standby(self, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("offline_user")

        mock_probe.return_value = StreamInfo(room_id="offline_user", is_live=False)

        tm.poll_staggered_batch(stagger_seconds=0.0)

        jobs = tm.get_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, JobStatus.STANDBY)
        self.assertGreater(jobs[0].last_poll, 0.0)
        self.assertEqual(len(tm.get_active_jobs()), 0)

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_on_file_ready_callback_invoked(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("live_user")

        mock_probe.return_value = StreamInfo(
            room_id="live_user", is_live=True, hls_url="https://live.cb.com/live_user.m3u8"
        )

        ready_event = threading.Event()
        callback_args = {}

        def on_ready(path: Path, room_id: str, duration: float):
            callback_args["path"] = path
            callback_args["room_id"] = room_id
            callback_args["duration"] = duration
            ready_event.set()

        tm.set_on_file_ready_callback(on_ready)

        def fake_record(info, out_mp4, stop_event, **kwargs):
            p = Path(out_mp4)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"dummy mp4 data")
            return p

        mock_record.side_effect = fake_record

        tm.poll_staggered_batch(stagger_seconds=0.0)

        self.assertTrue(ready_event.wait(timeout=2.0))
        self.assertEqual(callback_args["room_id"], "live_user")
        self.assertTrue(isinstance(callback_args["path"], Path))
        self.assertTrue(callback_args["path"].exists())
        self.assertGreaterEqual(callback_args["duration"], 0.0)

        time.sleep(0.05)
        job = tm.get_jobs()[0]
        self.assertEqual(job.status, JobStatus.STANDBY)
        self.assertIsNone(job.current_file)

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_stop_tracking_active_recording(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("active_user")

        mock_probe.return_value = StreamInfo(
            room_id="active_user", is_live=True, hls_url="https://live.cb.com/stream.m3u8"
        )

        rec_started = threading.Event()

        def fake_record(info, out_mp4, stop_event, **kwargs):
            rec_started.set()
            stop_event.wait(timeout=5.0)
            p = Path(out_mp4)
            p.write_bytes(b"partial video")
            return p

        mock_record.side_effect = fake_record

        tm.poll_staggered_batch(stagger_seconds=0.0)
        self.assertTrue(rec_started.wait(timeout=2.0))

        # Room is active
        self.assertEqual(len(tm.get_active_jobs()), 1)

        # Stop tracking
        self.assertTrue(tm.stop_tracking("active_user", wait=True, timeout=2.0))
        self.assertEqual(len(tm.get_jobs()), 0)
        self.assertEqual(len(tm.get_active_jobs()), 0)

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_stop_all(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("cam_a")
        tm.start_tracking("cam_b")

        mock_probe.side_effect = lambda room, proxy="": StreamInfo(
            room_id=room, is_live=True, hls_url=f"https://live.cb.com/{room}.m3u8"
        )

        started = threading.Event()

        def fake_record(info, out_mp4, stop_event, **kwargs):
            started.set()
            stop_event.wait(timeout=5.0)
            return Path(out_mp4)

        mock_record.side_effect = fake_record

        tm.poll_staggered_batch(stagger_seconds=0.0)
        self.assertTrue(started.wait(timeout=2.0))

        tm.stop_all(wait=True, timeout=2.0)

        self.assertEqual(len(tm.get_active_jobs()), 0)
        for j in tm.get_jobs():
            self.assertEqual(j.status, JobStatus.STOPPED)
            self.assertTrue(j.stop_event.is_set())

    @patch("core.task_manager.time.sleep")
    @patch("core.task_manager.probe_room")
    def test_stagger_polling_timing(self, mock_probe, mock_sleep):
        tm = TaskManager(max_concurrent=5, recordings_dir=self.rec_dir)
        tm.start_tracking("room1")
        tm.start_tracking("room2")
        tm.start_tracking("room3")

        mock_probe.return_value = StreamInfo(room_id="dummy", is_live=False)

        tm.poll_staggered_batch(stagger_seconds=0.3)

        # 3 rooms -> sleep 0.3s after 1st and 2nd rooms (2 sleeps)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_called_with(0.3)

    @patch("core.task_manager.probe_room")
    def test_probe_exception_handled_gracefully(self, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("buggy_user")

        mock_probe.side_effect = RuntimeError("Network timeout")

        tm.poll_staggered_batch(stagger_seconds=0.0)

        job = tm.get_jobs()[0]
        self.assertEqual(job.status, JobStatus.STANDBY)
        self.assertIn("Network timeout", job.error_message)

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_progress_updates_bytes_recorded(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("prog_user")

        mock_probe.return_value = StreamInfo(
            room_id="prog_user", is_live=True, hls_url="https://live.cb.com/prog.m3u8"
        )

        progress_applied = threading.Event()

        def fake_record(info, out_mp4, stop_event, on_progress=None, **kwargs):
            if on_progress:
                on_progress(1024 * 1024, 2.5)
            progress_applied.set()
            stop_event.wait(timeout=5.0)
            return Path(out_mp4)

        mock_record.side_effect = fake_record

        tm.poll_staggered_batch(stagger_seconds=0.0)
        self.assertTrue(progress_applied.wait(timeout=2.0))

        active_jobs = tm.get_active_jobs()
        self.assertEqual(len(active_jobs), 1)
        self.assertEqual(active_jobs[0].bytes_recorded, 1024 * 1024)

        tm.stop_all(wait=True, timeout=2.0)

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_record_exception_sets_error_and_can_reactivate(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("error_model")

        mock_probe.return_value = StreamInfo(
            room_id="error_model", is_live=True, hls_url="https://live.cb.com/stream.m3u8"
        )
        mock_record.side_effect = RuntimeError("Disk full or stream corruption")

        tm.poll_staggered_batch(stagger_seconds=0.0)

        # Allow worker thread to run and handle exception
        time.sleep(0.05)
        job = tm.get_job("error_model")
        self.assertIsNotNone(job)
        self.assertEqual(job.status, JobStatus.ERROR)
        self.assertIn("Disk full", job.error_message)

        # Reactivate via start_tracking
        self.assertTrue(tm.start_tracking("error_model"))
        self.assertEqual(job.status, JobStatus.STANDBY)
        self.assertEqual(job.error_message, "")

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_empty_recording_does_not_invoke_callback(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("empty_model")

        mock_probe.return_value = StreamInfo(
            room_id="empty_model", is_live=True, hls_url="https://live.cb.com/stream.m3u8"
        )
        # record_stream_to_disk returns None if 0 bytes or mux failure
        mock_record.return_value = None

        callback_mock = MagicMock()
        tm.set_on_file_ready_callback(callback_mock)

        tm.poll_staggered_batch(stagger_seconds=0.0)
        time.sleep(0.05)

        callback_mock.assert_not_called()
        job = tm.get_job("empty_model")
        self.assertEqual(job.status, JobStatus.STANDBY)

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_to_disk")
    def test_callback_exception_handled_cleanly(self, mock_record, mock_probe):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        tm.start_tracking("test_err_user")

        mock_probe.return_value = StreamInfo(
            room_id="test_err_user", is_live=True, hls_url="https://live.cb.com/stream.m3u8"
        )

        def fake_record(info, out_mp4, stop_event, **kwargs):
            p = Path(out_mp4)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"content")
            return p

        mock_record.side_effect = fake_record

        def bad_callback(path, room_id, duration):
            raise ValueError("Unexpected callback crash")

        tm.set_on_file_ready_callback(bad_callback)

        tm.poll_staggered_batch(stagger_seconds=0.0)
        time.sleep(0.05)

        job = tm.get_job("test_err_user")
        self.assertEqual(job.status, JobStatus.STANDBY)

    def test_room_normalization_and_get_job(self):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        self.assertTrue(tm.start_tracking("https://chaturbate.com/sweet_girl/"))

        job = tm.get_job("sweet_girl")
        self.assertIsNotNone(job)
        self.assertEqual(job.room_id, "sweet_girl")

        # get_job with url also normalizes
        self.assertIs(tm.get_job("https://chaturbate.com/sweet_girl/"), job)
        self.assertIsNone(tm.get_job("unknown_room"))

    def test_overlapping_poll_batch_locked(self):
        tm = TaskManager(max_concurrent=2, recordings_dir=self.rec_dir)
        # Acquire polling lock manually
        self.assertTrue(tm._polling_lock.acquire(blocking=False))

        # poll_staggered_batch should return immediately without deadlock
        tm.poll_staggered_batch(stagger_seconds=0.0)

        tm._polling_lock.release()

    @patch("core.task_manager.probe_room")
    @patch("core.task_manager.record_stream_ffmpeg")
    def test_use_ffmpeg_recorder_flag(self, mock_ffmpeg_rec, mock_probe):
        tm = TaskManager(
            max_concurrent=4,
            recordings_dir=self.rec_dir,
            use_ffmpeg_recorder=True,
        )
        tm.start_tracking("streamer_ff")
        mock_probe.return_value = StreamInfo(
            room_id="streamer_ff",
            is_live=True,
            hls_url="https://live.example.com/streamer_ff.m3u8",
        )
        fake_file = self.rec_dir / "test.mp4"
        fake_file.write_bytes(b"data" * 20000)
        mock_ffmpeg_rec.return_value = fake_file

        callback_called = []
        tm.set_on_file_ready_callback(lambda p, r, d: callback_called.append((p, r)))

        tm.poll_staggered_batch(stagger_seconds=0.0)
        time.sleep(0.05)

        self.assertTrue(mock_ffmpeg_rec.called)
        self.assertEqual(len(callback_called), 1)
        self.assertEqual(callback_called[0][1], "streamer_ff")

    def test_resolution_and_transient_error_flapping_no_alert(self):
        """Verify that resolution changes and transient probe errors don't trigger false alerts."""
        tm = TaskManager(recordings_dir=self.rec_dir)
        tm.start_tracking("stable_cam")
        batch_mock = MagicMock()
        tm.set_on_batch_status_change_callback(batch_mock)

        with patch("core.task_manager.probe_room") as mock_probe:
            # Baseline: initial status online (720p)
            mock_probe.return_value = StreamInfo(room_id="stable_cam", is_live=True, resolution_label="720p")
            res1 = tm.check_all_room_statuses(dispatch_recording=False)
            self.assertFalse(res1[0][2])
            batch_mock.assert_not_called()

            # Resolution disappears or changes: should NOT alert
            mock_probe.return_value = StreamInfo(room_id="stable_cam", is_live=True, resolution_label="")
            res2 = tm.check_all_room_statuses(dispatch_recording=False)
            self.assertFalse(res2[0][2])
            batch_mock.assert_not_called()

            # Transient error: should NOT alert
            mock_probe.return_value = StreamInfo(room_id="stable_cam", is_live=False, room_status="error")
            res3 = tm.check_all_room_statuses(dispatch_recording=False)
            self.assertFalse(res3[0][2])
            batch_mock.assert_not_called()

            # Room genuinely goes offline: should alert
            mock_probe.return_value = StreamInfo(room_id="stable_cam", is_live=False, room_status="offline")
            res4 = tm.check_all_room_statuses(dispatch_recording=False)
            self.assertTrue(res4[0][2])
            self.assertEqual(batch_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
