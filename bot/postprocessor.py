"""Remux .ts segments into .mp4 with faststart, verify duration, and enqueue for upload.
Single worker with nice priority to avoid competing with ongoing recordings.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from queue import Queue
from typing import Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["Postprocessor", "enqueue_remux"]

_remux_queue: Queue[tuple[Path, str, int]] = Queue()


def enqueue_remux(ts_path: Path | str, room_id: str, segment_duration: int = 900) -> None:
    """Enqueue a .ts segment for asynchronous remuxing and upload."""
    p = Path(ts_path)
    _remux_queue.put((p, room_id, segment_duration))
    logger.info("Enqueued for remux: %s (%s)", p.name, room_id)


class Postprocessor:
    """Background remux worker with lower CPU scheduling priority."""

    def __init__(
        self,
        on_upload_ready: Optional[Callable[[Path, str, int], None]] = None,
        remux_timeout: float = 120.0,
    ) -> None:
        self.on_upload_ready = on_upload_ready
        self.remux_timeout = remux_timeout
        self._stop_event = asyncio.Event()
        self._worker_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start background worker task."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Stop background worker task cleanly."""
        self._stop_event.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

    async def _worker_loop(self) -> None:
        """Continuously process .ts files from queue."""
        while not self._stop_event.is_set():
            try:
                # Non-blocking check with sleep to allow clean cancellation
                if _remux_queue.empty():
                    await asyncio.sleep(0.5)
                    continue

                ts_path, room_id, seg_dur = _remux_queue.get_nowait()
                await asyncio.to_thread(self._remux_one, ts_path, room_id, seg_dur)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in postprocessor loop: %s", e)

    def _remux_one(self, ts_path: Path, room_id: str, seg_dur: int) -> None:
        """Remux a single .ts file to .mp4 using nice priority."""
        if not ts_path.exists():
            logger.warning("Postprocessor: .ts file does not exist: %s", ts_path)
            return

        mp4_path = ts_path.with_suffix(".mp4")
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        nice_bin = shutil.which("nice")

        cmd = []
        if nice_bin and os.name != "nt":
            cmd.extend([nice_bin, "-n", "10"])

        cmd.extend([
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel", "warning",
            "-i", str(ts_path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(mp4_path),
        ])

        logger.debug("Remuxing %s -> %s", ts_path.name, mp4_path.name)
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=self.remux_timeout)
            if res.returncode != 0:
                logger.warning("Remux non-zero exit (%d) for %s: %s", res.returncode, ts_path.name, res.stderr.decode(errors="ignore"))
                if not mp4_path.exists() or mp4_path.stat().st_size == 0:
                    raise RuntimeError(f"Remux failed to produce output file: {mp4_path}")

            # Verify duration
            actual_dur = self._verify_duration(mp4_path, seg_dur)
            final_duration = int(actual_dur) if actual_dur else seg_dur

            # Remove .ts after successful remux to preserve storage
            try:
                ts_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("Could not unlink raw .ts: %s", exc)

            # Trigger upload ready callback
            if self.on_upload_ready:
                self.on_upload_ready(mp4_path, room_id, final_duration)

        except Exception as exc:
            logger.exception("Remux error for %s: %s", ts_path.name, exc)

    def _verify_duration(self, mp4_path: Path, expected_sec: int) -> Optional[float]:
        """Verify segment duration: regular segment >= 0.9 * expected, final > 5s."""
        dur = self._ffprobe_duration(mp4_path)
        if dur is None:
            return None
        min_regular = expected_sec * 0.9
        if dur >= min_regular:
            return dur
        if dur >= 5.0:
            logger.info("Postprocessor: accepted short/final segment duration (%.1fs) for %s", dur, mp4_path.name)
            return dur
        logger.warning("Postprocessor: segment %s duration very low (%.1fs)", mp4_path.name, dur)
        return dur

    def _ffprobe_duration(self, path: Path) -> Optional[float]:
        """Probe media duration via ffprobe."""
        ffprobe_bin = shutil.which("ffprobe") or "ffprobe"
        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0 and res.stdout.strip():
                return float(res.stdout.strip())
        except (ValueError, TypeError, subprocess.SubprocessError):
            pass
        return None

