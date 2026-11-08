# Chaturbate Recorder on Android Termux with Telegram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the monolithic Linux-only `rec2-depseek.py` into a modular, memory-safe recording daemon optimized for Android Termux with Telegram remote control and 2 GB MTProto video uploads with a 48-hour retention policy.

**Architecture:** A unified `asyncio` Python daemon running inside Termux. It manages: (1) an in-memory `TaskManager` that monitors streamers with staggered polling and a max concurrency limit of 2; (2) a low-RAM progressive recorder spooling 8–16 MB chunks to temporary `.raw.ts` files and muxing via FFmpeg stream-copy (`-c copy`) up to a 1.8 GB Telegram safety ceiling; (3) an asynchronous `UploadQueue` uploading files up to 2 GB via Telethon MTProto with progress callbacks; (4) a `RetentionCleaner` that purges uploaded files after 48 hours or under emergency disk-space constraints; and (5) a `TelegramBot` controller handling remote commands (`/status`, `/rec`, `/stop`, `/fav`, `/add`, `/del`, `/files`).

**Tech Stack:** Python 3.10+ (standard library `asyncio`, `urllib`, `ctypes`, `subprocess`), `telethon` (MTProto Bot Client), `ffmpeg` / `ffprobe` (via Termux `pkg install ffmpeg`), `unittest` (standard library test runner).

**Spec:** [docs/superpowers/specs/2026-11-08-termux-telegram-recorder-design.md](file:///Users/ngo/Desktop/blog.nghiart/cb2te/docs/superpowers/specs/2026-11-08-termux-telegram-recorder-design.md)

---

## Global Constraints
- Target platform: Android Termux (F-Droid) with Python 3.10+ and standard Bionic libc (`libc.so`).
- Total process RAM footprint must remain strictly below 150 MB (Android LMK threshold is ~350–400 MB).
- Peak Telegram upload file size: 2,000 MB (rotation at 1,800 MB for safety).
- Storage retention: exactly 48 hours after upload completion, unless emergency eviction triggers when free space < `MIN_FREE_MB`.
- Concurrency limit: maximum 2 simultaneous recording streams by default (`MAX_CONCURRENT_RECORDINGS = 2`).
- Testing framework: `python3 -m unittest` with zero external test runner dependencies.

---

## Review Focus
1. **Network disconnect / HLS playlist stall**: The HLS stream stops delivering segments unexpectedly -> Expect exponential retry (up to 4 streaks) followed by master playlist re-probe, then clean mux of recorded data without crashing.
2. **1.8 GB Size Cap Handling**: A stream runs for 5+ hours -> Expect automatic chunk rotation at 1.8 GB: current file finalized and queued for upload, recording continues immediately on a new chunk file.
3. **Emergency Disk Low Space**: Phone storage drops below `MIN_FREE_MB` (e.g. 400 MB) mid-recording -> Expect `RetentionCleaner` to immediately evict oldest uploaded files even if under 48 hours, notifying the Telegram group.
4. **Telegram FloodWait during Upload Progress**: Rapid chunk transfers trigger Telegram API rate limits -> Expect progress message edits throttled to at least 5 seconds between edits.
5. **Daemon Termination & Wake-Lock**: Termux receives `SIGTERM` / `SIGINT` -> Expect `termux-wake-unlock` to be called, active recordings flushed to MP4, and state cleanly finalized.

---

### Task 1: Configuration & Favorites Manager

**Files:**
- Create: `core/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Config`: dataclass holding `api_id: int`, `api_hash: str`, `bot_token: str`, `chat_id: int`, `admin_ids: list[int]`, `retention_hours: int`, `max_concurrent: int`, `min_free_mb: int`, `proxy: str`.
  - `load_config(path: Path) -> Config`
  - `load_favorites(path: Path) -> list[dict]`
  - `save_favorites(items: list[dict], path: Path) -> None`
  - `add_favorite(room_id: str, note: str, path: Path) -> bool`
  - `remove_favorite(room_id: str, path: Path) -> bool`

- [ ] **Step 1: Write the failing test for configuration and favorites parsing**

```python
# tests/test_config.py
import unittest
import tempfile
from pathlib import Path
from core.config import load_config, load_favorites, save_favorites, add_favorite, remove_favorite

class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.settings_file = self.tmp_dir / "settings.txt"
        self.fav_file = self.tmp_dir / "favorites.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_config_defaults_and_parsing(self):
        self.settings_file.write_text(
            "TELEGRAM_API_ID = 123456\n"
            "TELEGRAM_API_HASH = abcdef123456\n"
            "TELEGRAM_BOT_TOKEN = 1234:xyz\n"
            "TELEGRAM_CHAT_ID = -100123456789\n"
            "TELEGRAM_ADMIN_IDS = 111, 222\n"
            "RETENTION_HOURS = 48\n"
            "MAX_CONCURRENT = 2\n"
            "MIN_FREE_MB = 500\n"
        )
        cfg = load_config(self.settings_file)
        self.assertEqual(cfg.api_id, 123456)
        self.assertEqual(cfg.api_hash, "abcdef123456")
        self.assertEqual(cfg.bot_token, "1234:xyz")
        self.assertEqual(cfg.chat_id, -100123456789)
        self.assertEqual(cfg.admin_ids, [111, 222])
        self.assertEqual(cfg.retention_hours, 48)
        self.assertEqual(cfg.max_concurrent, 2)
        self.assertEqual(cfg.min_free_mb, 500)

    def test_favorites_crud(self):
        self.fav_file.write_text("streamer1  Note 1\n# streamer2  Disabled\n")
        favs = load_favorites(self.fav_file)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["room_id"], "streamer1")

        add_favorite("streamer3", "New note", self.fav_file)
        favs_after_add = load_favorites(self.fav_file)
        self.assertEqual(len(favs_after_add), 2)
        self.assertTrue(any(f["room_id"] == "streamer3" for f in favs_after_add))

        remove_favorite("streamer1", self.fav_file)
        favs_after_del = load_favorites(self.fav_file)
        self.assertEqual(len(favs_after_del), 1)
        self.assertEqual(favs_after_del[0]["room_id"], "streamer3")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core'`

- [ ] **Step 3: Implement `core/config.py`**

Implement `Config` dataclass and helper functions to parse key-value lines safely (ignoring `#` comments, handling integer and list casts, and preserving favorites notes).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/config.py tests/test_config.py
git commit -m "feat(config): add settings and favorites manager"
```

---

### Task 2: Android Termux Environment & Memory Abstraction

**Files:**
- Create: `core/android.py`
- Create: `tests/test_android.py`

**Interfaces:**
- Produces:
  - `acquire_wake_lock() -> bool`
  - `release_wake_lock() -> bool`
  - `get_safe_tmp_dir() -> Path`
  - `get_recordings_dir() -> Path`
  - `release_os_memory() -> None`
  - `get_disk_free(path: Path) -> int`
  - `is_disk_low(path: Path, min_mb: int) -> bool`

- [ ] **Step 1: Write the failing test for Android environment helpers**

```python
# tests/test_android.py
import unittest
import shutil
import tempfile
from pathlib import Path
from core.android import (
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_android.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.android'`

- [ ] **Step 3: Implement `core/android.py`**

Implement:
- `acquire_wake_lock()`: runs `termux-wake-lock` if `which termux-wake-lock` exists, without throwing errors on macOS/Linux.
- `release_wake_lock()`: runs `termux-wake-unlock`.
- `get_safe_tmp_dir()`: checks `$TMPDIR` -> `/data/data/com.termux/files/usr/tmp` -> `tempfile.gettempdir()`.
- `release_os_memory()`: calls `gc.collect()`, then tries `ctypes.CDLL("libc.so").malloc_trim(0)` (Android Bionic) and `ctypes.CDLL("libc.so.6").malloc_trim(0)` (glibc) with silent fallback.
- `get_disk_free(path)` and `is_disk_low(path, min_mb)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_android.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/android.py tests/test_android.py
git commit -m "feat(android): add wake-lock, memory trim, and storage helpers"
```

---

### Task 3: Local Storage History & 48-Hour Retention Cleaner

**Files:**
- Create: `storage/history.py`
- Create: `storage/cleaner.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Consumes: `core/android.py` (`get_disk_free`, `is_disk_low`)
- Produces:
  - `HistoryRecord`: dataclass `file_path: str, room_id: str, uploaded_at: float, size: int, status: str`
  - `HistoryStore`: `record_upload(...)`, `list_records(...)`, `mark_purged(file_path)`
  - `RetentionCleaner`: `run_cleanup_cycle(store, retention_hours, min_free_mb, dry_run=False) -> list[Path]`

- [ ] **Step 1: Write the failing test for history tracking and 48-hour retention cleanup**

```python
# tests/test_storage.py
import unittest
import time
import tempfile
from pathlib import Path
from storage.history import HistoryStore
from storage.cleaner import RetentionCleaner

class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.db_path = self.tmp_dir / "history.json"
        self.store = HistoryStore(self.db_path)
        self.cleaner = RetentionCleaner(recordings_dir=self.tmp_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_list_uploads(self):
        f = self.tmp_dir / "test1.mp4"
        f.write_text("sample content")
        self.store.record_upload(str(f), "room_a", size=14)
        records = self.store.list_records(active_only=True)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].room_id, "room_a")
        self.assertEqual(records[0].status, "uploaded")

    def test_cleanup_48h_expired_files(self):
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'storage'`

- [ ] **Step 3: Implement `storage/history.py` and `storage/cleaner.py`**

- `HistoryStore`: JSON-backed file store with thread-safe writes (`history.json`), persisting uploaded file metadata.
- `RetentionCleaner`: checks `now - uploaded_at >= retention_hours * 3600`. Also implements emergency disk eviction if `is_disk_low()` returns true: sorts uploaded files oldest-first and unlinks until above `min_free_mb`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_storage.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add storage/ tests/test_storage.py
git commit -m "feat(storage): add history tracking and 48-hour retention cleaner"
```

---

### Task 4: Media Muxer & Stream Inspection

**Files:**
- Create: `core/muxer.py`
- Create: `tests/test_muxer.py`

**Interfaces:**
- Produces:
  - `find_ffmpeg() -> str | None`
  - `find_ffprobe() -> str | None`
  - `inspect_media(path: Path) -> dict`
  - `mux_stream_copy(input_ts: Path, output_mp4: Path) -> tuple[bool, str]`

- [ ] **Step 1: Write the failing test for media muxing**

```python
# tests/test_muxer.py
import unittest
import tempfile
from pathlib import Path
from core.muxer import find_ffmpeg, find_ffprobe, mux_stream_copy, inspect_media

class TestMuxer(unittest.TestCase):
    def test_tools_detected(self):
        ff = find_ffmpeg()
        self.assertTrue(ff is not None and len(ff) > 0, "ffmpeg must be found in PATH or system")

    def test_mux_invalid_input_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            bad_input = Path(td) / "empty.ts"
            bad_input.write_bytes(b"not a video")
            out_mp4 = Path(td) / "out.mp4"
            ok, err = mux_stream_copy(bad_input, out_mp4)
            self.assertFalse(ok)
            self.assertGreater(len(err), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_muxer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.muxer'`

- [ ] **Step 3: Implement `core/muxer.py`**

Implement:
- `find_ffmpeg()` and `find_ffprobe()` using `shutil.which` and standard Termux paths (`/data/data/com.termux/files/usr/bin/ffmpeg`).
- `inspect_media(path: Path)`: calls `ffprobe -show_entries stream=codec_type,codec_name -of json`.
- `mux_stream_copy(input_ts: Path, output_mp4: Path) -> tuple[bool, str]`: calls `ffmpeg -y -i input_ts -c copy -movflags +faststart output_mp4`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_muxer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/muxer.py tests/test_muxer.py
git commit -m "feat(media): add faststream copy muxer and probe helpers"
```

---

### Task 5: Low-RAM Progressive Recorder Engine

**Files:**
- Create: `core/recorder.py`
- Create: `tests/test_recorder.py`

**Interfaces:**
- Consumes: `core/android.py` (`get_safe_tmp_dir`), `core/muxer.py` (`mux_stream_copy`)
- Produces:
  - `StreamInfo`: dataclass `room_id: str, is_live: bool, hls_url: str, width: int, height: int`
  - `probe_room(room_id: str, proxy: str = "") -> StreamInfo`
  - `record_stream_to_disk(info: StreamInfo, out_mp4: Path, stop_event: threading.Event, max_bytes: int = 1_800 * 1024 * 1024, on_progress: callable = None) -> Path | None`

- [ ] **Step 1: Write the failing test for recorder probe and progressive spooling**

```python
# tests/test_recorder.py
import unittest
import threading
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from core.recorder import parse_hls_segments, StreamInfo

class TestRecorder(unittest.TestCase):
    def test_parse_hls_segments(self):
        sample_m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:4\n"
            "#EXTINF:4.000,\n"
            "segment1.ts\n"
            "#EXTINF:4.000,\n"
            "https://cdn.example.com/segment2.ts\n"
        )
        segs = parse_hls_segments(sample_m3u8, base_url="https://live.example.com/hls/")
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0], "https://live.example.com/hls/segment1.ts")
        self.assertEqual(segs[1], "https://cdn.example.com/segment2.ts")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_recorder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.recorder'`

- [ ] **Step 3: Implement `core/recorder.py`**

Implement:
- `probe_room(room_id, proxy)`: probes `https://chaturbate.com/api/chatvideocontext/<room>/` with fallbacks.
- `parse_hls_segments(text, base_url)`: extracts URLs from m3u8 playlists.
- `record_stream_to_disk`:
  - Spools 8–16 MB RAM buffer progressively to `tmp/<room>_<uuid>.raw.ts`.
  - Checks if cumulative size >= `max_bytes` (1.8 GB) or `stop_event.is_set()`: flushes via `mux_stream_copy` to `out_mp4` and unlinks `.raw.ts`.
  - Re-probes up to 4 times on stream network hiccups before concluding live ended.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_recorder.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/recorder.py tests/test_recorder.py
git commit -m "feat(recorder): add progressive stream downloader with 1.8GB rotation cap"
```

---

### Task 6: In-Memory TaskManager & Concurrency Governance

**Files:**
- Create: `core/task_manager.py`
- Create: `tests/test_task_manager.py`

**Interfaces:**
- Consumes: `core/config.py` (`Config`), `core/recorder.py` (`StreamInfo`, `record_stream_to_disk`, `probe_room`)
- Produces:
  - `JobStatus`: Enum (`STANDBY`, `RECORDING`, `MUXING`, `UPLOADING`, `ERROR`, `STOPPED`)
  - `RecordingJob`: dataclass holding job metadata, `bytes_recorded`, `current_file`, `stop_event`
  - `TaskManager`:
    - `start_tracking(room_id: str) -> bool`
    - `stop_tracking(room_id: str) -> bool`
    - `get_active_jobs() -> list[RecordingJob]`
    - `poll_staggered_batch() -> None`
    - `set_on_file_ready_callback(cb: callable)`

- [ ] **Step 1: Write the failing test for TaskManager state and concurrency limits**

```python
# tests/test_task_manager.py
import unittest
from unittest.mock import MagicMock
from core.task_manager import TaskManager, JobStatus

class TestTaskManager(unittest.TestCase):
    def test_job_lifecycle_and_concurrency(self):
        tm = TaskManager(max_concurrent=2)
        tm.start_tracking("room1")
        tm.start_tracking("room2")
        tm.start_tracking("room3")

        jobs = tm.get_jobs()
        self.assertEqual(len(jobs), 3)
        self.assertTrue(all(j.status == JobStatus.STANDBY for j in jobs))

        # Test stop
        tm.stop_tracking("room1")
        self.assertEqual(len(tm.get_jobs()), 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_task_manager.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.task_manager'`

- [ ] **Step 3: Implement `core/task_manager.py`**

Implement:
- `TaskManager`: maintains an in-memory dictionary of `RecordingJob` objects.
- Governs `MAX_CONCURRENT_RECORDINGS` (if 2 rooms are active, 3rd room stays in `STANDBY` until a slot frees).
- Dispatches polling with 0.3s stagger between rooms to minimize battery and radio wakeups.
- On file completion, invokes `on_file_ready(output_mp4_path, room_id, duration_sec)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_task_manager.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/task_manager.py tests/test_task_manager.py
git commit -m "feat(task_manager): add in-memory job scheduler with concurrency limits"
```

---

### Task 7: Asynchronous Telegram Uploader Queue (Telethon MTProto)

**Files:**
- Create: `bot/uploader.py`
- Create: `tests/test_uploader.py`

**Interfaces:**
- Consumes: `storage/history.py` (`HistoryStore`), `telethon.TelegramClient`
- Produces:
  - `UploadItem`: dataclass `file_path: Path, room_id: str, duration_sec: int`
  - `UploadQueue`:
    - `enqueue(item: UploadItem)`
    - `start_worker(client, chat_id, store)`
    - `stop_worker()`
    - `get_queue_status() -> dict`

- [ ] **Step 1: Write the failing test for UploadQueue queueing and throttling**

```python
# tests/test_uploader.py
import unittest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from bot.uploader import UploadQueue, UploadItem

class TestUploader(unittest.TestCase):
    def test_enqueue_and_queue_length(self):
        uq = UploadQueue()
        item = UploadItem(file_path=Path("rec.mp4"), room_id="room_x", duration_sec=120)
        uq.enqueue(item)
        status = uq.get_queue_status()
        self.assertEqual(status["pending_count"], 1)
        self.assertEqual(status["items"][0]["room_id"], "room_x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_uploader.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot'`

- [ ] **Step 3: Implement `bot/uploader.py`**

Implement:
- `UploadQueue`: `asyncio.Queue` consumer.
- For each item:
  1. Sends initial progress message to `chat_id`: `⏳ Đang tải lên: <file.name> (450 MB) [0%]`.
  2. Calls `client.send_file()` via MTProto streaming with progress callback.
  3. Throttles `edit_message` to at least 5 seconds between edits to avoid Telegram FloodWait.
  4. Upon finish: deletes the progress message, sends the final video message with rich caption, and records to `HistoryStore`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_uploader.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/uploader.py tests/test_uploader.py
git commit -m "feat(bot): add async 2GB MTProto upload queue with progress updates"
```

---

### Task 8: Telegram Bot Command Handlers & Controller

**Files:**
- Create: `bot/telegram_bot.py`
- Create: `bot/handlers.py`
- Create: `tests/test_bot_handlers.py`

**Interfaces:**
- Consumes: `core/config.py`, `core/task_manager.py`, `bot/uploader.py`, `storage/history.py`
- Produces:
  - `init_telegram_bot(config, task_manager, upload_queue, history_store) -> TelegramClient`
  - Handlers for:
    - `/start`, `/help`
    - `/status`
    - `/rec <user>`
    - `/stop <user|all>`
    - `/fav`
    - `/add <user>`
    - `/del <user>`
    - `/files`

- [ ] **Step 1: Write the failing test for bot command routing and authorization**

```python
# tests/test_bot_handlers.py
import unittest
from unittest.mock import MagicMock
from bot.handlers import is_authorized, format_status_message

class TestBotHandlers(unittest.TestCase):
    def test_authorization(self):
        admin_ids = [111, 222]
        self.assertTrue(is_authorized(111, admin_ids))
        self.assertTrue(is_authorized(222, admin_ids))
        self.assertFalse(is_authorized(999, admin_ids))

    def test_format_status_message(self):
        msg = format_status_message(
            active_jobs=[{"room_id": "model_a", "status": "RECORDING", "size_mb": 45}],
            queue_status={"pending_count": 0},
            free_disk_mb=12000,
            retention_files_count=3,
        )
        self.assertIn("model_a", msg)
        self.assertIn("RECORDING", msg)
        self.assertIn("12000", msg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_bot_handlers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bot.handlers'`

- [ ] **Step 3: Implement `bot/handlers.py` and `bot/telegram_bot.py`**

Implement:
- `is_authorized(user_id, admin_ids) -> bool`.
- Command dispatching for `/start`, `/status`, `/rec`, `/stop`, `/fav`, `/add`, `/del`, `/files`.
- Telethon client initialization using `client.start(bot_token=cfg.bot_token)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_bot_handlers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bot/telegram_bot.py bot/handlers.py tests/test_bot_handlers.py
git commit -m "feat(bot): add telethon bot client and administrative command handlers"
```

---

### Task 9: Unified Entry Point CLI & Daemon Integration

**Files:**
- Create: `rec2_app.py`
- Create: `tests/test_app.py`

**Interfaces:**
- Consumes: all components (`core/`, `bot/`, `storage/`)
- Produces:
  - CLI commands:
    - `python rec2_app.py bot` -> Runs full daemon with Telegram Bot + wake-lock + upload queue + retention cleaner.
    - `python rec2_app.py status` -> Terminal health-check.
    - `python rec2_app.py rec <room_id>` -> Standalone local recording CLI.

- [ ] **Step 1: Write the failing test for CLI argument parser**

```python
# tests/test_app.py
import unittest
from rec2_app import build_parser

class TestApp(unittest.TestCase):
    def test_cli_parser(self):
        p = build_parser()
        args = p.parse_args(["bot"])
        self.assertEqual(args.cmd, "bot")

        args_rec = p.parse_args(["rec", "streamer_x"])
        self.assertEqual(args_rec.cmd, "rec")
        self.assertEqual(args_rec.target, ["streamer_x"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests/test_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rec2_app'`

- [ ] **Step 3: Implement `rec2_app.py`**

Implement:
- `main()` and `build_parser()`.
- Signal handling for `SIGINT` and `SIGTERM`:
  - Calls `release_wake_lock()`.
  - Sets stop flag on all active jobs to trigger MP4 commit.
  - Flushes and stops `UploadQueue`.
  - Gracefully exits.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add rec2_app.py tests/test_app.py
git commit -m "feat(app): add unified CLI entry point and daemon lifecycle integration"
```

---

### Task 10: Full System Verification & End-to-End Test Suite

**Files:**
- Create: `tests/test_e2e.py`

**Interfaces:**
- Consumes: all modules

- [ ] **Step 1: Write comprehensive end-to-end integration test**

```python
# tests/test_e2e.py
import unittest
import tempfile
import time
from pathlib import Path
from core.config import Config
from core.task_manager import TaskManager, JobStatus
from storage.history import HistoryStore
from storage.cleaner import RetentionCleaner
from bot.uploader import UploadQueue, UploadItem

class TestEndToEndPipeline(unittest.TestCase):
    def test_full_pipeline_flow(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db_path = base / "history.json"
            store = HistoryStore(db_path)
            cleaner = RetentionCleaner(recordings_dir=base)
            uq = UploadQueue()
            tm = TaskManager(max_concurrent=2)

            # 1. Start tracking room
            tm.start_tracking("test_room")
            self.assertEqual(len(tm.get_jobs()), 1)

            # 2. Simulate video file created
            video = base / "chaturbate_test_room_20261108_100000.mp4"
            video.write_bytes(b"fake mp4 header and data")

            # 3. Simulate enqueue and upload completion
            uq.enqueue(UploadItem(video, "test_room", 120))
            self.assertEqual(uq.get_queue_status()["pending_count"], 1)

            # Record upload into history
            store.record_upload(str(video), "test_room", size=len(video.read_bytes()))
            records = store.list_records(active_only=True)
            self.assertEqual(len(records), 1)

            # 4. Retention test: not expired now
            deleted = cleaner.run_cleanup_cycle(store, retention_hours=48, min_free_mb=0)
            self.assertEqual(len(deleted), 0)
            self.assertTrue(video.exists())

            # 5. Fake expiration > 48h
            records[0].uploaded_at = time.time() - 49 * 3600
            store.save()
            deleted = cleaner.run_cleanup_cycle(store, retention_hours=48, min_free_mb=0)
            self.assertEqual(len(deleted), 1)
            self.assertFalse(video.exists())
```

- [ ] **Step 2: Run all tests across the entire test suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS (all tests pass)

- [ ] **Step 3: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test(e2e): add end-to-end integration test suite"
```

