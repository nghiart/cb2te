from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from storage.cleaner import RetentionCleaner
from storage.history import HistoryRecord, HistoryStore


class TestStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.db_path = self.tmp_dir / "history.json"
        self.store = HistoryStore(self.db_path)
        self.cleaner = RetentionCleaner(recordings_dir=self.tmp_dir)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_record_and_list_uploads(self) -> None:
        f = self.tmp_dir / "test1.mp4"
        f.write_text("sample content")
        self.store.record_upload(str(f), "room_a", size=14)
        records = self.store.list_records(active_only=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].room_id, "room_a")
        self.assertEqual(records[0].status, "uploaded")
        self.assertEqual(records[0].size, 14)
        self.assertEqual(records[0].file_path, str(f))

    def test_cleanup_48h_expired_files(self) -> None:
        f_old = self.tmp_dir / "old.mp4"
        f_old.write_text("old video")
        f_new = self.tmp_dir / "new.mp4"
        f_new.write_text("new video")

        # Fake upload times: 50h ago vs 2h ago
        now = time.time()
        self.store.record_upload(str(f_old), "room_old", size=9, uploaded_at=now - 50 * 3600)
        self.store.record_upload(str(f_new), "room_new", size=9, uploaded_at=now - 2 * 3600)

        deleted = self.cleaner.run_cleanup_cycle(self.store, retention_hours=48, min_free_mb=0)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].name, "old.mp4")
        self.assertFalse(f_old.exists())
        self.assertTrue(f_new.exists())

        active = self.store.list_records(active_only=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].file_path, str(f_new))

    def test_cleanup_dry_run(self) -> None:
        f = self.tmp_dir / "expired.mp4"
        f.write_text("expired video")
        now = time.time()
        self.store.record_upload(str(f), "room_exp", size=13, uploaded_at=now - 60 * 3600)

        # In dry_run mode, file is NOT unlinked and status is NOT changed
        deleted = self.cleaner.run_cleanup_cycle(self.store, retention_hours=48, min_free_mb=0, dry_run=True)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].name, "expired.mp4")
        self.assertTrue(f.exists())

        active = self.store.list_records(active_only=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].status, "uploaded")

    def test_emergency_disk_eviction_oldest_first(self) -> None:
        now = time.time()
        f1 = self.tmp_dir / "vid1_oldest.mp4"
        f2 = self.tmp_dir / "vid2_middle.mp4"
        f3 = self.tmp_dir / "vid3_newest.mp4"
        f1.write_text("vid1")
        f2.write_text("vid2")
        f3.write_text("vid3")

        # All uploaded recently (< 48 hours ago)
        self.store.record_upload(str(f1), "room1", size=4, uploaded_at=now - 10 * 3600)
        self.store.record_upload(str(f2), "room2", size=4, uploaded_at=now - 5 * 3600)
        self.store.record_upload(str(f3), "room3", size=4, uploaded_at=now - 1 * 3600)

        # Mock is_disk_low: True for first check (f1 evicted), True for second check (f2 evicted), False for third check (f3 kept)
        with patch("storage.cleaner.is_disk_low", side_effect=[True, True, False]):
            deleted = self.cleaner.run_cleanup_cycle(self.store, retention_hours=48, min_free_mb=400)

        self.assertEqual(len(deleted), 2)
        self.assertEqual(deleted[0].name, "vid1_oldest.mp4")
        self.assertEqual(deleted[1].name, "vid2_middle.mp4")
        self.assertFalse(f1.exists())
        self.assertFalse(f2.exists())
        self.assertTrue(f3.exists())

        active = self.store.list_records(active_only=True)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].file_path, str(f3))

    def test_persistence_across_instances(self) -> None:
        f = self.tmp_dir / "persisted.mp4"
        f.write_text("data")
        self.store.record_upload(str(f), "room_persisted", size=4)

        # Create a new HistoryStore loading from the same JSON file
        store2 = HistoryStore(self.db_path)
        records = store2.list_records(active_only=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].room_id, "room_persisted")
        self.assertEqual(records[0].size, 4)

        # Mark purged in store2 and verify persisted
        store2.mark_purged(str(f))
        self.assertEqual(len(store2.list_records(active_only=True)), 0)
        self.assertEqual(len(store2.list_records(active_only=False)), 1)
        self.assertEqual(store2.list_records(active_only=False)[0].status, "purged")

        # Reload store1 and verify it observes purged status
        self.store.load()
        self.assertEqual(len(self.store.list_records(active_only=True)), 0)

    def test_record_update_existing_path(self) -> None:
        f = self.tmp_dir / "video.mp4"
        f.write_text("data")
        r1 = self.store.record_upload(str(f), "room_1", size=100)
        self.assertEqual(len(self.store.list_records(active_only=False)), 1)

        # Updating the same path should not duplicate records
        r2 = self.store.record_upload(str(f), "room_updated", size=200)
        records = self.store.list_records(active_only=False)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].room_id, "room_updated")
        self.assertEqual(records[0].size, 200)

    def test_missing_file_on_disk_handled_gracefully(self) -> None:
        # File is recorded in history but deleted manually from disk
        now = time.time()
        missing_path = self.tmp_dir / "deleted_manually.mp4"
        self.store.record_upload(str(missing_path), "room_x", size=10, uploaded_at=now - 50 * 3600)

        deleted = self.cleaner.run_cleanup_cycle(self.store, retention_hours=48, min_free_mb=0)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].name, "deleted_manually.mp4")

        # Should still be marked purged
        active = self.store.list_records(active_only=True)
        self.assertEqual(len(active), 0)

    def test_corrupt_or_empty_json_recovery(self) -> None:
        bad_json = self.tmp_dir / "corrupted.json"
        bad_json.write_text("{invalid json content!@#")
        store = HistoryStore(bad_json)
        self.assertEqual(store.list_records(active_only=False), [])

        empty_json = self.tmp_dir / "empty.json"
        empty_json.write_text("")
        store_empty = HistoryStore(empty_json)
        self.assertEqual(store_empty.list_records(active_only=False), [])

    def test_record_from_dict_iso_timestamp(self) -> None:
        rec = HistoryRecord.from_dict({
            "file": "chaturbate_test.mp4",
            "room": "test_streamer",
            "uploaded_at": "2026-11-08T15:30:00Z",
            "size": 12345,
            "status": "uploaded",
        })
        self.assertEqual(rec.file_path, "chaturbate_test.mp4")
        self.assertEqual(rec.room_id, "test_streamer")
        self.assertEqual(rec.size, 12345)
        self.assertGreater(rec.uploaded_at, 0)

    def test_cleaner_emergency_callback(self) -> None:
        callback_called = []

        def on_evict(paths: list[Path]) -> None:
            callback_called.extend(paths)

        cleaner = RetentionCleaner(
            recordings_dir=self.tmp_dir,
            on_emergency_eviction=on_evict,
        )
        f = self.tmp_dir / "evict_me.mp4"
        f.write_text("data")
        self.store.record_upload(str(f), "room_cb", size=4)

        with patch("storage.cleaner.is_disk_low", side_effect=[True, False]):
            cleaner.run_cleanup_cycle(self.store, retention_hours=48, min_free_mb=500)

        self.assertEqual(len(callback_called), 1)
        self.assertEqual(callback_called[0].name, "evict_me.mp4")

    def test_delete_record_and_nested_dir_creation(self) -> None:
        nested_db = self.tmp_dir / "nested" / "sub" / "history.json"
        store = HistoryStore(nested_db)
        store.record_upload("file1.mp4", "room_1", size=10)
        self.assertEqual(len(store.list_records()), 1)

        # Delete record
        deleted = store.delete_record("file1.mp4")
        self.assertTrue(deleted)
        self.assertEqual(len(store.list_records(active_only=False)), 0)

        # Deleting non-existent record returns False
        self.assertFalse(store.delete_record("nonexistent.mp4"))

    def test_multithreaded_concurrent_writes(self) -> None:
        import concurrent.futures

        def worker(idx: int) -> None:
            fpath = self.tmp_dir / f"thread_{idx}.mp4"
            self.store.record_upload(str(fpath), f"room_{idx}", size=idx)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(worker, range(20)))

        records = self.store.list_records(active_only=False)
        self.assertEqual(len(records), 20)

    def test_sync_with_disk_prunes_missing_files(self) -> None:
        # File 1: exists in base_dir
        f1 = self.tmp_dir / "vid1.mp4"
        f1.write_text("v1")
        self.store.record_upload(str(f1), "room_1")

        # File 2: exists in hierarchical folder chaturbate/room_2/vid2.mp4
        h_dir = self.tmp_dir / "chaturbate" / "room_2"
        h_dir.mkdir(parents=True, exist_ok=True)
        f2 = h_dir / "vid2.mp4"
        f2.write_text("v2")
        self.store.record_upload("vid2.mp4", "room_2")

        # File 3: missing (was deleted manually)
        self.store.record_upload(str(self.tmp_dir / "deleted_manually.mp4"), "room_3")

        active = self.store.sync_with_disk(self.tmp_dir)
        self.assertEqual(len(active), 2)
        active_rooms = {r.room_id for r in active}
        self.assertEqual(active_rooms, {"room_1", "room_2"})

        # Record 3 should now be marked as purged
        all_recs = self.store.list_records(active_only=False)
        rec3 = next(r for r in all_recs if r.room_id == "room_3")
        self.assertEqual(rec3.status, "purged")

