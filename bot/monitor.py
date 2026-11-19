"""Process Watchdog for FFmpeg streams.

Monitors output file growth and kills stalled FFmpeg processes if no bytes
are written within the timeout threshold (e.g. 60 seconds).
Avoids checking CPU time which is unreliable on Android/Termux.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["ProcessWatch", "ProcessWatchdog"]


@dataclass
class ProcessWatch:
    pid: int
    room: str
    target_path: Path
    last_size: int = 0
    last_check: float = field(default_factory=time.monotonic)
    stall_count: int = 0


class ProcessWatchdog:
    """Thread-safe watchdog monitoring active recording processes."""

    def __init__(self, check_interval: float = 15.0, stall_timeout: float = 60.0):
        self.check_interval = check_interval
        self.stall_timeout = stall_timeout
        self._watches: dict[str, ProcessWatch] = {}
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def register(self, room: str, pid: int, target_path: Path | str) -> None:
        """Register or update a process to watch."""
        async with self._lock:
            p = Path(target_path)
            init_size = self._get_size(p)
            self._watches[room] = ProcessWatch(
                pid=pid,
                room=room,
                target_path=p,
                last_size=init_size,
                last_check=time.monotonic(),
                stall_count=0,
            )
            logger.debug("Watchdog registered %s (pid=%d, path=%s, size=%d)", room, pid, p, init_size)

    async def unregister(self, room: str) -> None:
        """Unregister a room process from watchdog."""
        async with self._lock:
            if room in self._watches:
                del self._watches[room]
                logger.debug("Watchdog unregistered %s", room)

    def _get_size(self, path: Path) -> int:
        """Get file size or total directory file size."""
        try:
            if path.is_file():
                return path.stat().st_size
            if path.is_dir():
                total = 0
                for item in path.iterdir():
                    if item.is_file():
                        total += item.stat().st_size
                return total
        except OSError:
            pass
        return 0

    async def start(self) -> None:
        """Start background monitoring loop."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._watch_loop())

    async def stop(self) -> None:
        """Stop background monitoring loop."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _watch_loop(self) -> None:
        """Periodic loop checking file size growth."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.check_interval)
                async with self._lock:
                    rooms = list(self._watches.keys())
                    now = time.monotonic()

                    for room in rooms:
                        watch = self._watches.get(room)
                        if not watch:
                            continue

                        current_size = self._get_size(watch.target_path)
                        delta = current_size - watch.last_size
                        elapsed = now - watch.last_check

                        if delta <= 0:
                            if elapsed >= self.stall_timeout:
                                watch.stall_count += 1
                                if watch.stall_count >= 2:
                                    logger.warning(
                                        "Watchdog: %s (PID %d) stalled for %.1fs (size=%d). Terminating process.",
                                        room, watch.pid, elapsed, current_size,
                                    )
                                    self._kill_pid(watch.pid)
                                    del self._watches[room]
                                    continue
                        else:
                            watch.stall_count = 0
                            watch.last_check = now

                        watch.last_size = current_size
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Error in watchdog loop: %s", e)

    def _kill_pid(self, pid: int) -> None:
        """Send SIGTERM followed by SIGKILL if process still exists."""
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as e:
            logger.debug("Watchdog kill error for pid %d: %s", pid, e)
