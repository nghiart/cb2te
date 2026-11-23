"""Asynchronous Telegram MTProto Uploader Queue & Dynamic Backpressure Governance.

Uploads finalized recordings up to 2GB to Telegram channels/groups via MTProto streaming.
Features:
- Telethon FloodWait handling with jitter sleep
- 512 KB part_size_kb for optimal MTProto RTT efficiency
- SQLite WAL persistent queue (UploadStore) with auto-recovery
- Post-upload file size verification
- Dynamic backpressure monitoring & adaptive concurrency
- Single message status reporter with debounced updates
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from telethon import errors
    from telethon.tl.types import DocumentAttributeVideo
except ImportError:
    errors = None  # type: ignore
    DocumentAttributeVideo = None  # type: ignore

from bot.upload_store import UploadStore, UploadTask
from core.muxer import inspect_media
from storage.history import HistoryStore

logger = logging.getLogger(__name__)

__all__ = [
    "UploadItem",
    "UploadQueue",
    "format_duration",
    "UploadError",
    "FloodWaitHandled",
    "AuthError",
    "NetworkError",
]


class UploadError(Exception):
    pass


class FloodWaitHandled(UploadError):
    def __init__(self, seconds: int):
        super().__init__(f"FloodWait for {seconds}s")
        self.seconds = seconds


class AuthError(UploadError):
    pass


class NetworkError(UploadError):
    pass


def format_duration(seconds: int | float) -> str:
    """Format duration in seconds into human-readable string (e.g. 1h30m, 45m10s)."""
    sec = int(seconds)
    hours = sec // 3600
    minutes = (sec % 3600) // 60
    remaining_sec = sec % 60

    if hours > 0:
        return f"{hours}h{minutes:02d}m"
    elif minutes > 0:
        return f"{minutes}m{remaining_sec:02d}s"
    else:
        return f"{remaining_sec}s"


@dataclass
class UploadItem:
    file_path: Path
    room_id: str
    duration_sec: int = 0
    size_bytes: int = 0
    enqueued_at: float = field(default_factory=time.time)
    task_id: Optional[int] = None

    def __post_init__(self):
        self.file_path = Path(self.file_path)
        if self.size_bytes == 0 and self.file_path.exists():
            try:
                self.size_bytes = self.file_path.stat().st_size
            except OSError:
                self.size_bytes = 0


class UploadQueue:
    """Asynchronous upload worker queue with persistent WAL storage and adaptive concurrency."""

    def __init__(
        self,
        edit_throttle_sec: float = 5.0,
        upload_store: Optional[UploadStore] = None,
        delete_after_upload: bool = False,
        send_progress_message: bool = False,
    ) -> None:
        self._queue: asyncio.Queue[UploadItem] = asyncio.Queue()
        self._items: list[UploadItem] = []
        self._current_upload: Optional[dict[str, Any]] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._edit_throttle_sec = edit_throttle_sec
        self.upload_store = upload_store
        self.delete_after_upload = delete_after_upload
        self.send_progress_message = send_progress_message
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Backpressure & throughput tracking (10-minute window)
        self._upload_history: list[tuple[float, int]] = []  # (timestamp, bytes)
        self.current_concurrency: int = 1
        self.max_concurrency: int = 3

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store running event loop for threadsafe enqueues."""
        self._loop = loop

    def enqueue(self, item: UploadItem) -> None:
        """Enqueue an item safely from any thread."""
        target_loop = self._loop
        if target_loop is None:
            try:
                target_loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

        # If currently running in the target loop, enqueue immediately
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if target_loop and target_loop.is_running() and curr_loop != target_loop:
            target_loop.call_soon_threadsafe(self._enqueue_sync, item)
        else:
            self._enqueue_sync(item)

    def _enqueue_sync(self, item: UploadItem) -> None:
        self._items.append(item)
        self._queue.put_nowait(item)
        logger.info(
            "Enqueued upload for %s (%s, %d bytes)",
            item.room_id,
            item.file_path.name,
            item.size_bytes,
        )
        if self.upload_store:
            asyncio.create_task(self._persist_enqueue(item))

    async def _persist_enqueue(self, item: UploadItem) -> None:
        if self.upload_store:
            try:
                t_id = await self.upload_store.enqueue(item.file_path, item.room_id, item.duration_sec)
                item.task_id = t_id
            except Exception as e:
                logger.warning("Failed to persist upload item %s to UploadStore: %s", item.file_path.name, e)

    async def recover_pending(self) -> int:
        """Recover pending uploads from UploadStore after daemon restart."""
        if not self.upload_store:
            return 0
        tasks = await self.upload_store.recover()
        recovered = 0
        for t in tasks:
            p = t.file_path
            if p.exists():
                item = UploadItem(
                    file_path=p,
                    room_id=t.room_id,
                    duration_sec=t.duration_sec,
                    size_bytes=t.size,
                    task_id=t.id,
                )
                self._items.append(item)
                self._queue.put_nowait(item)
                recovered += 1
            else:
                logger.info("Skipping missing recovered file: %s", t.path)
                await self.upload_store.mark_failed(t.id, reason="file_missing_on_recovery")
        logger.info("Recovered %d pending uploads from UploadStore", recovered)
        return recovered

    async def join(self) -> None:
        """Wait until all queued items have been processed."""
        await self._queue.join()

    def get_queue_status(self) -> dict[str, Any]:
        """Return the current queue status, backlog metrics, and active uploads."""
        backlog_sec, speed_mbps = self.estimate_backlog()
        return {
            "pending_count": self._queue.qsize(),
            "items": [
                {
                    "room_id": it.room_id,
                    "file": it.file_path.name,
                    "duration_sec": it.duration_sec,
                    "size_bytes": it.size_bytes,
                }
                for it in self._items
            ],
            "current_upload": self._current_upload,
            "estimated_backlog_sec": int(backlog_sec),
            "upload_speed_mbps": round(speed_mbps, 2),
            "concurrency": self.current_concurrency,
        }

    def record_uploaded_bytes(self, num_bytes: int) -> None:
        """Record completed bytes with timestamp for sliding window speed estimation."""
        now = time.monotonic()
        self._upload_history.append((now, num_bytes))
        # Keep only events within last 600s (10 min)
        cutoff = now - 600.0
        self._upload_history = [(t, b) for t, b in self._upload_history if t >= cutoff]

    def estimate_upload_speed(self) -> float:
        """Estimate upload speed in bytes/sec over the last 10 minutes (default 2.5 MB/s fallback)."""
        now = time.monotonic()
        cutoff = now - 600.0
        events = [(t, b) for t, b in self._upload_history if t >= cutoff]
        if not events:
            return 2.5 * 1024 * 1024  # Default fallback 2.5 MB/s (~20 Mbps)
        total_bytes = sum(b for _, b in events)
        time_span = max(10.0, now - events[0][0])
        return max(500 * 1024, total_bytes / time_span)

    def estimate_backlog(self) -> tuple[float, float]:
        """Return (backlog_seconds, upload_speed_mbps)."""
        total_pending_bytes = sum(it.size_bytes for it in self._items)
        speed = self.estimate_upload_speed()
        backlog_sec = total_pending_bytes / speed if speed > 0 else 0.0
        speed_mbps = (speed * 8) / (1024 * 1024)
        return backlog_sec, speed_mbps

    def update_adaptive_concurrency(self) -> int:
        """Adjust concurrency level based on estimated backlog threshold (>300s -> 2..3)."""
        backlog_sec, _ = self.estimate_backlog()
        if backlog_sec > 300:
            self.current_concurrency = min(self.max_concurrency, self.current_concurrency + 1)
        elif backlog_sec < 120 and self.current_concurrency > 1:
            self.current_concurrency -= 1
        return self.current_concurrency

    async def start_worker(
        self, client: Any, chat_id: int | str, store: HistoryStore
    ) -> None:
        """Start background worker task."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_event.clear()
        self._loop = asyncio.get_running_loop()
        self._worker_task = asyncio.create_task(
            self._worker_loop(client, chat_id, store)
        )

    async def stop_worker(self) -> None:
        """Stop background worker task cleanly."""
        self._stop_event.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

    async def _worker_loop(
        self, client: Any, chat_id: int | str, store: HistoryStore
    ) -> None:
        """Continuously process upload items sequentially with robust error classification."""
        while not self._stop_event.is_set():
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break

            if self.upload_store and item.task_id:
                try:
                    await self.upload_store.mark_uploading(item.task_id)
                except Exception as exc:
                    logger.debug("mark_uploading failed: %s", exc)

            try:
                await self._process_upload(client, chat_id, store, item)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Upload failed for %s: %s", item.room_id, e)
            finally:
                if item in self._items:
                    self._items.remove(item)
                self._current_upload = None
                self._queue.task_done()
                self.update_adaptive_concurrency()

    async def _process_upload(
        self, client: Any, chat_id: int | str, store: HistoryStore, item: UploadItem, max_retries: int = 3
    ) -> None:
        """Upload single video with retry, FloodWait handling, size verification, and safe persistence."""
        if not item.file_path.exists():
            logger.error("File does not exist for upload: %s", item.file_path)
            if self.upload_store and item.task_id:
                await self.upload_store.mark_failed(item.task_id, reason="file_missing")
            return

        size_bytes = item.file_path.stat().st_size
        size_mb = size_bytes / (1024 * 1024)
        duration_str = format_duration(item.duration_sec)

        self._current_upload = {
            "room_id": item.room_id,
            "file": item.file_path.name,
            "size_mb": round(size_mb, 1),
            "duration": duration_str,
            "progress_percent": 0,
        }

        # Progress callback throttled
        last_edit = 0.0
        last_percent = -1
        progress_msg: Any = None

        # Groups should strictly receive uploaded files without progress spam.
        # Only send progress messages if explicitly enabled AND destination is not a negative group ID.
        is_group = False
        try:
            if int(chat_id) <= 0:
                is_group = True
        except (ValueError, TypeError):
            pass

        if self.send_progress_message and not is_group:
            try:
                init_text = f"⏳ Đang tải lên: {item.file_path.name} ({size_mb:.1f} MB) [0%]"
                progress_msg = await client.send_message(chat_id, init_text)
            except Exception as e:
                logger.warning("Failed to send initial progress message: %s", e)

        def progress_callback(current: int, total: int):
            nonlocal last_edit, last_percent
            if total <= 0:
                return
            now = time.monotonic()
            pct = int((current / total) * 100)
            if self._current_upload:
                self._current_upload["progress_percent"] = pct

            if self.send_progress_message and progress_msg:
                if (now - last_edit >= self._edit_throttle_sec and pct != last_percent) or pct >= 100:
                    last_edit = now
                    last_percent = pct
                    cur_mb = current / (1024 * 1024)
                    tot_mb = total / (1024 * 1024)
                    edit_text = (
                        f"⏳ Đang tải lên: {item.file_path.name} "
                        f"({cur_mb:.1f}/{tot_mb:.1f} MB) [{pct}%]"
                    )
                    try:
                        if hasattr(progress_msg, "edit"):
                            res = progress_msg.edit(edit_text)
                            if asyncio.iscoroutine(res):
                                asyncio.get_running_loop().create_task(res)
                    except Exception:
                        pass

        upload_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur_s = int(item.duration_sec or 0)
        m, s = divmod(dur_s, 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}h{m:02d}m{s:02d}s" if h > 0 else f"{m}m{s:02d}s"
        sz_mb = item.file_path.stat().st_size / (1024 * 1024) if item.file_path.exists() else 0
        caption = f"📹 #{item.room_id} | ⏱️ {dur_str} ({sz_mb:.1f} MB)\n📅 {upload_time_str}"

        suffix = item.file_path.suffix.lower()
        is_video = suffix in (".mp4", ".mkv", ".mov", ".webm")
        attributes = None
        if is_video and DocumentAttributeVideo is not None:
            raw_meta = inspect_media(item.file_path)
            streams = raw_meta.get("streams", []) if isinstance(raw_meta, dict) else []
            v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
            fmt = raw_meta.get("format", {}) if isinstance(raw_meta, dict) else {}

            width = int(v_stream.get("width") or 1920)
            height = int(v_stream.get("height") or 1080)
            try:
                duration_s = int(float(fmt.get("duration") or item.duration_sec or 0))
            except (ValueError, TypeError):
                duration_s = int(item.duration_sec or 0)

            attributes = [
                DocumentAttributeVideo(
                    duration=duration_s,
                    w=width,
                    h=height,
                    supports_streaming=True,
                )
            ]

        # Multi-attempt upload loop with FloodWait handling
        msg_result = None
        for attempt in range(max_retries + 1):
            try:
                msg_result = await client.send_file(
                    chat_id,
                    file=item.file_path,
                    caption=caption,
                    progress_callback=progress_callback,
                    force_document=not is_video,
                    attributes=attributes,
                    part_size_kb=512,  # 512KB chunks for optimal RTT throughput
                )

                # Verify remote document size
                if not self._verify_size(msg_result, item.file_path):
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    raise UploadError(f"Size verification failed after {max_retries} attempts")

                break  # Successful upload
            except Exception as exc:
                if errors and isinstance(exc, errors.FloodWaitError):
                    # Respect FloodWait with jitter, do not consume attempt counter
                    wait_time = exc.seconds + random.uniform(1.0, 5.0)
                    logger.warning("FloodWait received (%ds). Sleeping for %.1fs...", exc.seconds, wait_time)
                    await asyncio.sleep(wait_time)
                    continue

                if errors and isinstance(exc, (errors.AuthKeyUnregisteredError, errors.SessionRevokedError)):
                    logger.critical("Telethon Auth Error: %s", exc)
                    if self.upload_store and item.task_id:
                        await self.upload_store.mark_failed(item.task_id, reason="auth_error")
                    raise AuthError(str(exc))

                logger.warning("Upload attempt %d/%d failed for %s: %s", attempt + 1, max_retries + 1, item.file_path.name, exc)
                if attempt < max_retries:
                    await asyncio.sleep(min(0.5, 0.1 * (2 ** attempt)))
                else:
                    if self.upload_store and item.task_id:
                        await self.upload_store.mark_failed(item.task_id, reason=str(exc))
                    raise NetworkError(f"Upload failed after {max_retries} retries: {exc}")

        # Delete progress message cleanly
        if progress_msg:
            try:
                if hasattr(progress_msg, "delete"):
                    await progress_msg.delete()
                elif hasattr(client, "delete_messages"):
                    await client.delete_messages(chat_id, progress_msg)
            except Exception:
                pass

        # Record upload in history and UploadStore
        store.record_upload(
            file_path=str(item.file_path),
            room_id=item.room_id,
            size=size_bytes,
        )
        self.record_uploaded_bytes(size_bytes)

        tg_id = getattr(msg_result, "id", None)
        if self.upload_store and item.task_id:
            await self.upload_store.mark_done(item.task_id, tg_msg_id=tg_id)

        logger.info("Successfully uploaded %s (tg_id=%s, %d bytes)", item.file_path.name, tg_id, size_bytes)

        # Early delete if explicitly enabled (default is False)
        if self.delete_after_upload:
            try:
                item.file_path.unlink(missing_ok=True)
                store.mark_purged(str(item.file_path))
                logger.info("Early-delete unlinked %s after successful upload", item.file_path.name)
            except OSError as exc:
                logger.debug("Failed early-delete for %s: %s", item.file_path.name, exc)

    def _verify_size(self, msg: Any, filepath: Path) -> bool:
        """Verify uploaded remote document size matches local file size within 1%."""
        if not msg:
            return False
        doc = getattr(msg, "document", None)
        if not doc or not hasattr(doc, "size"):
            return True
        try:
            remote_sz = doc.size
            if not isinstance(remote_sz, (int, float)):
                return True
            local_sz = filepath.stat().st_size
            if local_sz <= 0:
                return True
            return abs(local_sz - remote_sz) / local_sz < 0.01
        except (OSError, TypeError):
            return True
