from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from bot.postprocessor import Postprocessor, enqueue_remux


class TestPostprocessor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.ready_files = []

        def on_ready(mp4_path, room, dur):
            self.ready_files.append((mp4_path, room, dur))

        self.post = Postprocessor(on_upload_ready=on_ready, remux_timeout=5.0)

    async def asyncTearDown(self):
        await self.post.stop()
        self.tmp.cleanup()

    def test_verify_duration(self):
        with patch.object(self.post, "_ffprobe_duration", return_value=850.0):
            res = self.post._verify_duration(self.tmp_path / "dummy.mp4", 900)
            self.assertEqual(res, 850.0)

        # Short final segment > 5s accepted
        with patch.object(self.post, "_ffprobe_duration", return_value=15.0):
            res = self.post._verify_duration(self.tmp_path / "dummy.mp4", 900)
            self.assertEqual(res, 15.0)

        # Tiny segment < 5s warned but returned
        with patch.object(self.post, "_ffprobe_duration", return_value=3.0):
            res = self.post._verify_duration(self.tmp_path / "dummy.mp4", 900)
            self.assertEqual(res, 3.0)


if __name__ == "__main__":
    unittest.main()
