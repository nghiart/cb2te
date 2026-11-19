from __future__ import annotations

import asyncio
import os
import signal
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from bot.monitor import ProcessWatchdog


class TestProcessWatchdog(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.watchdog = ProcessWatchdog(check_interval=0.05, stall_timeout=0.1)

    async def asyncTearDown(self):
        await self.watchdog.stop()
        self.tmp.cleanup()

    async def test_watchdog_register_and_unregister(self):
        test_file = self.tmp_path / "test.ts"
        test_file.write_bytes(b"hello")

        await self.watchdog.register("room1", 99999, test_file)
        self.assertIn("room1", self.watchdog._watches)
        self.assertEqual(self.watchdog._watches["room1"].last_size, 5)

        await self.watchdog.unregister("room1")
        self.assertNotIn("room1", self.watchdog._watches)

    @patch("os.kill")
    async def test_watchdog_kills_stalled_process(self, mock_kill):
        test_file = self.tmp_path / "stalled.ts"
        test_file.write_bytes(b"12345")

        await self.watchdog.register("stalled_room", 12345, test_file)
        await self.watchdog.start()

        # Let watchdog run for enough cycles to detect stall (stall_timeout=0.1s, stall_count >= 2)
        await asyncio.sleep(0.35)

        # Process should be killed and removed from watches
        self.assertNotIn("stalled_room", self.watchdog._watches)
        mock_kill.assert_any_call(12345, signal.SIGTERM)
        mock_kill.assert_any_call(12345, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
