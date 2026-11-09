"""Asynchronous Telegram MTProto Uploader Queue.

Uploads finalized recordings up to 2GB to Telegram channels/groups via MTProto streaming,
enforcing upload progress throttling and recording metadata into HistoryStore.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from storage.history import HistoryStore

logger = logging.getLogger(__name__)

__all__ = [
    "UploadItem",
    "UploadQueue",
    "format_duration",
]


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

    def __post_init__(self):
        self.file_path = Path(self.file_path)
        if self.size_bytes == 0 and self.file_path.exists():
            try:
                self.size_bytes = self.file_path.stat().st_size
            except OSError:
                self.size_bytes = 0


class UploadQueue:
    """Asynchronous sequential upload worker queue for Telegram MTProto."""

    def __init__(self, edit_throttle_sec: float = 5.0) -> None:
        self._queue: asyncio.Queue[UploadItem] = asyncio.Queue()
        self._items: list[UploadItem] = []
        self._current_upload: Optional[dict[str, Any]] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._edit_throttle_sec = edit_throttle_sec

    def enqueue(self, item: UploadItem) -> None:
        """Enqueue an item for sequential background upload."""
        self._items.append(item)
        self._queue.put_nowait(item)
        logger.info(
            "Enqueued upload for %s (%s, %d bytes)",
            item.room_id,
            item.file_path.name,
            item.size_bytes,
        )

    async def join(self) -> None:
        """Wait until all queued items have been processed."""
        await self._queue.join()

    def get_queue_status(self) -> dict[str, Any]:
        """Return the current queue status and list of pending items."""
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
        }

    async def start_worker(
        self, client: Any, chat_id: int | str, store: HistoryStore
    ) -> None:
        """Start background worker task."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_event.clear()
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
        """Continuously process upload items sequentially."""
        while not self._stop_event.is_set():
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._process_upload(client, chat_id, store, item)
            except asyncio.CancelledError:
                self._queue.task_done()
                break
            except Exception as e:
                logger.exception("Upload failed for %s: %s", item.room_id, e)
            finally:
                if item in self._items:
                    self._items.remove(item)
                self._current_upload = None
                self._queue.task_done()

    async def _process_upload(
        self, client: Any, chat_id: int | str, store: HistoryStore, item: UploadItem
    ) -> None:
        """Upload a single video file to Telegram and record history."""
        if not item.file_path.exists():
            logger.error("File does not exist for upload: %s", item.file_path)
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

        # 1. Send initial progress message
        init_text = f"⏳ Đang tải lên: {item.file_path.name} ({size_mb:.1f} MB) [0%]"
        progress_msg: Any = None
        try:
            progress_msg = await client.send_message(chat_id, init_text)
        except Exception as e:
            logger.warning("Failed to send initial progress message: %s", e)

        # 2. Setup throttled progress callback
        last_edit = 0.0
        last_percent = -1

        def progress_callback(current: int, total: int):
            nonlocal last_edit, last_percent
            if total <= 0:
                return
            now = time.monotonic()
            pct = int((current / total) * 100)
            if self._current_upload:
                self._current_upload["progress_percent"] = pct

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
                    if progress_msg:
                        loop = asyncio.get_running_loop()
                        if hasattr(progress_msg, "edit"):
                            res = progress_msg.edit(edit_text)
                            if asyncio.iscoroutine(res):
                                loop.create_task(res)
                except Exception:
                    pass

        # 3. Caption for final video
        caption = (
            f"📹 [RECORDED] #{item.room_id}\n"
            f"🕒 Thời lượng: {duration_str}\n"
            f"📦 Dung lượng: {size_mb:.1f} MB\n"
            f"⏳ Lưu trữ máy: 48 giờ trước khi xóa tự động."
        )

        # 4. Stream upload via send_file
        await client.send_file(
            chat_id,
            file=item.file_path,
            caption=caption,
            progress_callback=progress_callback,
        )

        # 5. Delete temporary progress message
        if progress_msg:
            try:
                if hasattr(progress_msg, "delete"):
                    await progress_msg.delete()
                elif hasattr(client, "delete_messages"):
                    await client.delete_messages(chat_id, progress_msg)
            except Exception as e:
                logger.debug("Could not delete progress message: %s", e)

        # 6. Record upload in HistoryStore
        store.record_upload(
            file_path=str(item.file_path),
            room_id=item.room_id,
            size=size_bytes,
        )
        logger.info("Successfully uploaded and recorded %s", item.file_path.name)
