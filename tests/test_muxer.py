import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.muxer import find_ffmpeg, find_ffprobe, inspect_media, mux_stream_copy


class TestMuxer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tools_detected(self):
        ff = find_ffmpeg()
        fp = find_ffprobe()
        if ff is None or fp is None:
            self.skipTest("ffmpeg/ffprobe not installed on this host")
        self.assertTrue(ff is not None and len(ff) > 0, "ffmpeg must be found in PATH or system")
        self.assertTrue(fp is not None and len(fp) > 0, "ffprobe must be found in PATH or system")

    def test_candidate_paths_fallback(self):
        # When shutil.which returns None, fallback candidate should be checked
        with patch("core.muxer.shutil.which", return_value=None):
            with patch("core.muxer.os.path.isfile") as mock_isfile:
                with patch("core.muxer.os.access") as mock_access:
                    def isfile_side_effect(p):
                        return p == "/data/data/com.termux/files/usr/bin/ffmpeg"

                    def access_side_effect(p, mode):
                        return p == "/data/data/com.termux/files/usr/bin/ffmpeg"

                    mock_isfile.side_effect = isfile_side_effect
                    mock_access.side_effect = access_side_effect

                    found = find_ffmpeg()
                    self.assertEqual(found, "/data/data/com.termux/files/usr/bin/ffmpeg")

    def test_tools_not_found(self):
        with patch("core.muxer.shutil.which", return_value=None):
            with patch("core.muxer.os.path.isfile", return_value=False):
                self.assertIsNone(find_ffmpeg())
                self.assertIsNone(find_ffprobe())

    def test_mux_invalid_input_returns_error(self):
        bad_input = self.tmp_dir / "empty.ts"
        bad_input.write_bytes(b"not a video")
        out_mp4 = self.tmp_dir / "out.mp4"
        ok, err = mux_stream_copy(bad_input, out_mp4)
        self.assertFalse(ok)
        self.assertGreater(len(err), 0)

    def test_mux_missing_ffmpeg(self):
        with patch("core.muxer.find_ffmpeg", return_value=None):
            bad_input = self.tmp_dir / "some.ts"
            bad_input.write_bytes(b"data")
            out_mp4 = self.tmp_dir / "out.mp4"
            ok, err = mux_stream_copy(bad_input, out_mp4)
            self.assertFalse(ok)
            self.assertIn("ffmpeg binary not found", err)

    def test_mux_valid_stream_copy_and_inspect(self):
        # Generate a small valid TS sample using ffmpeg
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            self.skipTest("ffmpeg not available on host")

        sample_ts = self.tmp_dir / "sample.ts"
        gen_cmd = [
            ffmpeg_bin,
            "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
            "-f", "lavfi", "-i", "sine=duration=1",
            "-c:v", "libx264", "-c:a", "aac",
            str(sample_ts)
        ]
        res = subprocess.run(gen_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(res.returncode, 0, f"Failed to generate test TS: {res.stderr.decode()}")

        # Now test mux_stream_copy to a nested destination directory
        nested_out = self.tmp_dir / "sub" / "output.mp4"
        ok, err = mux_stream_copy(sample_ts, nested_out)
        self.assertTrue(ok, f"mux_stream_copy failed: {err}")
        self.assertEqual(err, "")
        self.assertTrue(nested_out.exists())
        self.assertGreater(nested_out.stat().st_size, 0)

        # Inspect media
        meta = inspect_media(nested_out)
        self.assertIn("streams", meta)
        self.assertIn("format", meta)
        video_stream = next((s for s in meta["streams"] if s.get("codec_type") == "video"), None)
        self.assertIsNotNone(video_stream)
        self.assertEqual(video_stream.get("codec_name"), "h264")
        self.assertEqual(video_stream.get("width"), 320)
        self.assertEqual(video_stream.get("height"), 240)

    def test_inspect_media_errors(self):
        # Non-existent file
        self.assertEqual(inspect_media(self.tmp_dir / "missing.mp4"), {})

        # Corrupt file
        bad_file = self.tmp_dir / "bad.mp4"
        bad_file.write_bytes(b"corrupt")
        self.assertEqual(inspect_media(bad_file), {})

        # Missing ffprobe
        with patch("core.muxer.find_ffprobe", return_value=None):
            self.assertEqual(inspect_media(self.tmp_dir / "anything.mp4"), {})

        # Subprocess exception
        with patch("core.muxer.subprocess.run", side_effect=OSError("Process spawn error")):
            dummy_file = self.tmp_dir / "dummy.mp4"
            dummy_file.write_bytes(b"123")
            self.assertEqual(inspect_media(dummy_file), {})

    @patch("core.muxer.find_ffmpeg", return_value="/usr/bin/ffmpeg")
    def test_mux_input_not_exists(self, _mock_ffmpeg):
        nonexistent = self.tmp_dir / "does_not_exist.ts"
        out_mp4 = self.tmp_dir / "out.mp4"
        ok, err = mux_stream_copy(nonexistent, out_mp4)
        self.assertFalse(ok)
        self.assertIn("Input file does not exist", err)

    @patch("core.muxer.find_ffmpeg", return_value="/usr/bin/ffmpeg")
    def test_mux_mkdir_failure(self, _mock_ffmpeg):
        sample = self.tmp_dir / "input.ts"
        sample.write_bytes(b"dummy")
        # Target path inside a file rather than directory to cause OSError in mkdir
        blocking_file = self.tmp_dir / "blocker"
        blocking_file.write_text("not a directory")
        out_mp4 = blocking_file / "sub" / "out.mp4"

        ok, err = mux_stream_copy(sample, out_mp4)
        self.assertFalse(ok)
        self.assertIn("Failed to create output directory", err)

    @patch("core.muxer.find_ffmpeg", return_value="/usr/bin/ffmpeg")
    def test_mux_subprocess_exception(self, _mock_ffmpeg):
        sample = self.tmp_dir / "input.ts"
        sample.write_bytes(b"dummy")
        out_mp4 = self.tmp_dir / "out.mp4"

        with patch("core.muxer.subprocess.run", side_effect=OSError("Command failed")):
            ok, err = mux_stream_copy(sample, out_mp4)
            self.assertFalse(ok)
            self.assertIn("Failed to run ffmpeg", err)

    @patch("core.muxer.find_ffmpeg", return_value="/usr/bin/ffmpeg")
    def test_mux_success_code_but_empty_output(self, _mock_ffmpeg):
        sample = self.tmp_dir / "input.ts"
        sample.write_bytes(b"dummy")
        out_mp4 = self.tmp_dir / "out.mp4"

        mock_proc = unittest.mock.MagicMock()
        mock_proc.returncode = 0
        mock_proc.stderr = ""
        with patch("core.muxer.subprocess.run", return_value=mock_proc):
            # Output file is not created or is empty
            ok, err = mux_stream_copy(sample, out_mp4)
            self.assertFalse(ok)
            self.assertIn("missing or empty", err)


if __name__ == "__main__":
    unittest.main()
