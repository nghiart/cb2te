import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from rec2_app import build_parser, main


class TestApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name)
        self.settings_file = self.base_dir / "settings.txt"
        self.settings_file.write_text("API_ID=123\nBOT_TOKEN=dummy\nCHAT_ID=-1001\n")
        self.fav_file = self.base_dir / "favorites.txt"
        self.fav_file.write_text("room1\nroom2\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_parser(self):
        p = build_parser()
        args = p.parse_args(["bot"])
        self.assertEqual(args.cmd, "bot")

        args_rec = p.parse_args(["rec", "streamer_x"])
        self.assertEqual(args_rec.cmd, "rec")
        self.assertEqual(args_rec.target, ["streamer_x"])

        args_status = p.parse_args(["status"])
        self.assertEqual(args_status.cmd, "status")

    @patch("rec2_app.get_disk_free")
    def test_status_command_output(self, mock_disk):
        mock_disk.return_value = 5000
        # Running status command prints to stdout and returns 0
        code = main([
            "--settings", str(self.settings_file),
            "--favorites", str(self.fav_file),
            "status",
        ])
        self.assertEqual(code, 0)

    @patch("rec2_app.probe_room")
    @patch("rec2_app.record_stream_to_disk")
    def test_rec_command_offline(self, mock_record, mock_probe):
        from core.recorder import StreamInfo
        mock_probe.return_value = StreamInfo(room_id="cam_off", is_live=False)

        code = main([
            "--settings", str(self.settings_file),
            "--recordings-dir", str(self.base_dir),
            "rec", "cam_off",
        ])
        self.assertEqual(code, 0)
        mock_record.assert_not_called()

    @patch("rec2_app.probe_room")
    @patch("rec2_app.record_stream_to_disk")
    def test_rec_command_live_success(self, mock_record, mock_probe):
        from core.recorder import StreamInfo
        mock_probe.return_value = StreamInfo(room_id="cam_live", is_live=True, hls_url="http://live.cb.com/hls.m3u8")

        fake_out = self.base_dir / "chaturbate_cam_live_test.mp4"
        fake_out.write_bytes(b"dummy video")
        mock_record.return_value = fake_out

        code = main([
            "--settings", str(self.settings_file),
            "--recordings-dir", str(self.base_dir),
            "rec", "cam_live",
        ])
        self.assertEqual(code, 0)
        mock_record.assert_called_once()


if __name__ == "__main__":
    unittest.main()
