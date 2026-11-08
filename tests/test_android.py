from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.android import (
    acquire_wake_lock,
    release_wake_lock,
    get_safe_tmp_dir,
    get_recordings_dir,
    release_os_memory,
    get_disk_free,
    is_disk_low,
)


class TestAndroid(unittest.TestCase):
    def test_get_safe_tmp_dir_exists_and_writable(self):
        tmp = get_safe_tmp_dir()
        self.assertTrue(tmp.is_dir())
        test_file = tmp / "_test_write.tmp"
        test_file.write_text("ok")
        self.assertTrue(test_file.exists())
        test_file.unlink()

    def test_get_recordings_dir(self):
        rec_dir = get_recordings_dir()
        self.assertTrue(rec_dir.is_dir())

    def test_release_os_memory_does_not_crash(self):
        try:
            release_os_memory()
        except Exception as e:
            self.fail(f"release_os_memory crashed: {e}")

    def test_disk_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            free = get_disk_free(p)
            self.assertGreaterEqual(free, 0)
            self.assertFalse(is_disk_low(p, min_mb=0))
            # Test artificially huge threshold
            self.assertTrue(is_disk_low(p, min_mb=999_999_999))

    def test_wake_lock_when_cli_missing(self):
        with patch("shutil.which", return_value=None):
            self.assertFalse(acquire_wake_lock())
            self.assertFalse(release_wake_lock())

    def test_wake_lock_when_cli_succeeds(self):
        with patch("shutil.which", return_value="/bin/termux-wake-lock"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                self.assertTrue(acquire_wake_lock())
                mock_run.assert_called_once()

        with patch("shutil.which", return_value="/bin/termux-wake-unlock"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                self.assertTrue(release_wake_lock())
                mock_run.assert_called_once()

    def test_wake_lock_when_cli_fails_or_errors(self):
        with patch("shutil.which", return_value="/bin/termux-wake-lock"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                self.assertFalse(acquire_wake_lock())

            with patch("subprocess.run", side_effect=OSError("Command failed")):
                self.assertFalse(acquire_wake_lock())

        with patch("shutil.which", return_value="/bin/termux-wake-unlock"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                self.assertFalse(release_wake_lock())

            with patch("subprocess.run", side_effect=OSError("Command failed")):
                self.assertFalse(release_wake_lock())

    def test_get_safe_tmp_dir_fallback_chain(self):
        with tempfile.TemporaryDirectory() as td:
            custom_tmp = Path(td) / "custom_tmp"
            with patch.dict(os.environ, {"TMPDIR": str(custom_tmp)}):
                res = get_safe_tmp_dir()
                self.assertEqual(res, custom_tmp.resolve())
                self.assertTrue(res.is_dir())

    def test_release_os_memory_with_mocked_libc(self):
        mock_libc = MagicMock()
        with patch("ctypes.CDLL", return_value=mock_libc):
            release_os_memory()
            mock_libc.malloc_trim.assert_called_once_with(0)

    def test_disk_helpers_edge_cases(self):
        # min_mb negative
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            self.assertFalse(is_disk_low(p, min_mb=-10))

        # disk free on non-existent path should still resolve to parent
        non_existent = Path("/nonexistent_dir_12345/sub_dir")
        # Should either return free space of nearest parent or -1 without crashing
        free = get_disk_free(non_existent)
        self.assertIsInstance(free, int)


if __name__ == "__main__":
    unittest.main()
