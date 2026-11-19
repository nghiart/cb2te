from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from bot.upload_store import UploadStore


class TestUploadStore(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "test_queue.db"
        self.store = UploadStore(self.db_path)
        await self.store.init()

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    async def test_enqueue_and_recover(self):
        f1 = self.tmp_path / "vid1.mp4"
        f1.write_bytes(b"12345678")

        task_id = await self.store.enqueue(f1, "room_a", 120)
        self.assertIsNotNone(task_id)

        # Re-enqueue same file should be ignored by UNIQUE(path, size, mtime)
        task_id2 = await self.store.enqueue(f1, "room_a", 120)
        self.assertIsNone(task_id2)

        tasks = await self.store.recover()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].room_id, "room_a")
        self.assertEqual(tasks[0].size, 8)
        self.assertEqual(tasks[0].status, "PENDING")

    async def test_lifecycle_and_backlog(self):
        f = self.tmp_path / "vid.mp4"
        f.write_bytes(b"hello world")

        t_id = await self.store.enqueue(f, "room_b", 60)
        count, total_bytes = await self.store.count_backlog()
        self.assertEqual(count, 1)
        self.assertEqual(total_bytes, 11)

        await self.store.mark_uploading(t_id)
        tasks = await self.store.recover()
        self.assertEqual(tasks[0].status, "UPLOADING")

        await self.store.mark_done(t_id, tg_msg_id=1234)
        tasks_after = await self.store.recover()
        self.assertEqual(len(tasks_after), 0)

        count_after, bytes_after = await self.store.count_backlog()
        self.assertEqual(count_after, 0)
        self.assertEqual(bytes_after, 0)


if __name__ == "__main__":
    unittest.main()
