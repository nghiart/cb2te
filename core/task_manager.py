"""In-Memory TaskManager & Concurrency Governance Engine.

Governs concurrent Chaturbate stream recording jobs, enforcing concurrency limits,
staggered probe intervals, periodic 5-minute status monitoring, and asynchronous
recording thread dispatch on Android Termux.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from core.android import get_recordings_dir, release_os_memory
from core.config import normalize_room_id
from core.recorder import (
    MIN_VALID_STREAM_BYTES,
    StreamInfo,
    probe_room,
    record_stream_ffmpeg,
    record_stream_to_disk,
)

logger = logging.getLogger(__name__)

__all__ = [
    "JobStatus",
    "RecordingJob",
    "TaskManager",
]


class JobStatus(str, Enum):
    STANDBY = "STANDBY"
    RECORDING = "RECORDING"
    MUXING = "MUXING"
    UPLOADING = "UPLOADING"
    ERROR = "ERROR"
    STOPPED = "STOPPED"


@dataclass
class RecordingJob:
    room_id: str
    status: JobStatus = JobStatus.STANDBY
    bytes_recorded: int = 0
    current_file: Optional[Path] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    started_at: float = 0.0
    last_poll: float = 0.0
    error_message: str = ""
    last_detected_status: str = "unknown"
    last_detected_resolution: str = ""
    last_status_checked_at: float = 0.0


class TaskManager:
    """Manages recording jobs, concurrency limits, staggered polling, and status change tracking."""

    def __init__(
        self,
        max_concurrent: int = 4,
        recordings_dir: Optional[Path | str] = None,
        proxy: str = "",
        max_bytes: int = 1_800 * 1024 * 1024,
        max_duration_sec: int = 900,
        use_ffmpeg_recorder: bool = False,
    ) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.proxy = proxy
        self.max_bytes = max_bytes
        self.max_duration_sec = max_duration_sec
        self.use_ffmpeg_recorder = use_ffmpeg_recorder

        if recordings_dir is not None:
            self.recordings_dir = Path(recordings_dir)
        else:
            self.recordings_dir = get_recordings_dir()

        try:
            self.recordings_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self._jobs: dict[str, RecordingJob] = {}
        self._lock = threading.RLock()
        self._polling_lock = threading.Lock()
        self._on_file_ready: Optional[Callable[[Path, str, float], None]] = None
        self._on_status_change: Optional[Callable[[str, str, str, StreamInfo], None]] = None
        self._on_batch_status_change: Optional[
            Callable[[dict[str, tuple[str, StreamInfo]], list[tuple[str, str, str, StreamInfo]]], None]
        ] = None

    def start_tracking(self, room_id: str) -> bool:
        """Start tracking a room. Returns True if newly added or reactivated, False otherwise."""
        clean = normalize_room_id(room_id)
        if not clean:
            return False

        with self._lock:
            if clean in self._jobs:
                job = self._jobs[clean]
                if job.status in (JobStatus.STOPPED, JobStatus.ERROR):
                    job.status = JobStatus.STANDBY
                    job.stop_event = threading.Event()
                    job.thread = None
                    job.error_message = ""
                    return True
                return False

            self._jobs[clean] = RecordingJob(room_id=clean, status=JobStatus.STANDBY)
            return True

    def stop_tracking(
        self, room_id: str, wait: bool = False, timeout: float = 2.0
    ) -> bool:
        """Stop tracking a room, terminating any running recording. Returns True if removed."""
        clean = normalize_room_id(room_id)
        if not clean:
            return False

        with self._lock:
            job = self._jobs.pop(clean, None)
            if job is None:
                return False

            job.stop_event.set()
            job.status = JobStatus.STOPPED

        if wait and job.thread and job.thread.is_alive():
            job.thread.join(timeout=timeout)

        return True

    def get_jobs(self) -> list[RecordingJob]:
        """Return a shallow copy list of all tracked jobs."""
        with self._lock:
            return list(self._jobs.values())

    def list_jobs(self) -> list[RecordingJob]:
        """Alias for get_jobs() returning all tracked jobs."""
        return self.get_jobs()

    def get_job(self, room_id: str) -> Optional[RecordingJob]:
        """Return the RecordingJob for a room if tracked, else None."""
        clean = normalize_room_id(room_id)
        with self._lock:
            return self._jobs.get(clean)

    def get_active_jobs(self) -> list[RecordingJob]:
        """Return a list of jobs currently recording or muxing."""
        with self._lock:
            return [
                j
                for j in self._jobs.values()
                if j.status in (JobStatus.RECORDING, JobStatus.MUXING)
            ]

    def set_on_file_ready_callback(
        self, cb: Callable[[Path, str, float], None]
    ) -> None:
        """Set callback invoked as cb(output_mp4_path, room_id, duration_sec) on file completion."""
        with self._lock:
            self._on_file_ready = cb

    def set_on_status_change_callback(
        self, cb: Callable[[str, str, str, StreamInfo], None]
    ) -> None:
        """Set callback invoked as cb(room_id, old_status, new_status, info) when a room changes status."""
        with self._lock:
            self._on_status_change = cb

    def set_on_batch_status_change_callback(
        self,
        cb: Callable[[dict[str, tuple[str, StreamInfo]], list[tuple[str, str, str, StreamInfo]]], None],
    ) -> None:
        """Set callback invoked on periodic check when at least one room status changes.

        cb(all_statuses_map, list_of_changes)
        all_statuses_map: {room_id: (display_status_label, info)}
        list_of_changes: [(room_id, old_full, new_full, info)]
        """
        with self._lock:
            self._on_batch_status_change = cb

    def check_all_room_statuses(self) -> list[tuple[str, StreamInfo, bool]]:
        """Probe all tracked rooms and fire callbacks if any status changed.

        Returns: list of (room_id, stream_info, has_changed).
        """
        with self._lock:
            tracked_rooms = list(self._jobs.keys())

        results = []
        all_statuses: dict[str, tuple[str, StreamInfo]] = {}
        changes: list[tuple[str, str, str, StreamInfo]] = []

        for room in tracked_rooms:
            try:
                info = probe_room(room, proxy=self.proxy)
            except Exception as e:
                info = StreamInfo(room_id=room, is_live=False, room_status="error", error=str(e))

            status_str = "online" if info.is_live else (info.room_status or "offline")
            res_str = info.resolution_label

            changed = False
            old_full = "unknown"
            with self._lock:
                job = self._jobs.get(room)
                if job:
                    old_status = job.last_detected_status
                    old_res = job.last_detected_resolution
                    old_full = f"{old_status} ({old_res})" if (old_status == "online" and old_res) else old_status

                    new_full = f"{status_str} ({res_str})" if (status_str == "online" and res_str) else status_str

                    if old_status != "unknown" and old_full != new_full:
                        changed = True

                    job.last_detected_status = status_str
                    job.last_detected_resolution = res_str
                    job.last_status_checked_at = time.time()

            new_full = f"{status_str} ({res_str})" if (status_str == "online" and res_str) else status_str
            all_statuses[room] = (new_full, info)

            if changed:
                changes.append((room, old_full, new_full, info))
                if self._on_status_change:
                    try:
                        self._on_status_change(room, old_full, new_full, info)
                    except Exception as e:
                        logger.warning("Error in on_status_change for %s: %s", room, e)

            results.append((room, info, changed))

        if changes and self._on_batch_status_change:
            try:
                self._on_batch_status_change(all_statuses, changes)
            except Exception as e:
                logger.warning("Error in on_batch_status_change: %s", e)

        return results

    def poll_staggered_batch(self, stagger_seconds: float = 0.3) -> None:
        """Probe standby rooms with staggered timing and dispatch recording if live."""
        if not self._polling_lock.acquire(blocking=False):
            logger.debug("poll_staggered_batch already in progress, skipping.")
            return

        try:
            with self._lock:
                active_count = sum(
                    1
                    for j in self._jobs.values()
                    if j.status in (JobStatus.RECORDING, JobStatus.MUXING)
                )
                available_slots = self.max_concurrent - active_count
                if available_slots <= 0:
                    return

                candidates = sorted(
                    [j for j in self._jobs.values() if j.status == JobStatus.STANDBY],
                    key=lambda j: j.last_poll,
                )

            for i, job in enumerate(candidates):
                with self._lock:
                    if (
                        self._jobs.get(job.room_id) is not job
                        or job.status != JobStatus.STANDBY
                    ):
                        continue
                    active_count = sum(
                        1
                        for j in self._jobs.values()
                        if j.status in (JobStatus.RECORDING, JobStatus.MUXING)
                    )
                    if active_count >= self.max_concurrent:
                        break

                job.last_poll = time.time()
                try:
                    info = probe_room(job.room_id, proxy=self.proxy)
                except Exception as e:
                    logger.warning("Probe failed for room %s: %s", job.room_id, e)
                    job.error_message = str(e)
                    info = None

                if info:
                    # Update status detection & check change
                    status_str = "online" if info.is_live else (info.room_status or "offline")
                    res_str = info.resolution_label
                    old_status = job.last_detected_status
                    old_res = job.last_detected_resolution
                    old_full = f"{old_status} ({old_res})" if (old_status == "online" and old_res) else old_status
                    new_full = f"{status_str} ({res_str})" if (status_str == "online" and res_str) else status_str

                    if old_status != "unknown" and old_full != new_full and self._on_status_change:
                        try:
                            self._on_status_change(job.room_id, old_full, new_full, info)
                        except Exception as e:
                            logger.warning("Error in on_status_change: %s", e)

                    job.last_detected_status = status_str
                    job.last_detected_resolution = res_str
                    job.last_status_checked_at = time.time()

                if info and info.is_live and info.hls_url:
                    with self._lock:
                        if (
                            self._jobs.get(job.room_id) is job
                            and job.status == JobStatus.STANDBY
                        ):
                            active_count = sum(
                                1
                                for j in self._jobs.values()
                                if j.status in (JobStatus.RECORDING, JobStatus.MUXING)
                            )
                            if active_count < self.max_concurrent:
                                now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                                out_name = f"chaturbate_{job.room_id}_{now_str}.mp4"
                                room_dir = self.recordings_dir / "chaturbate" / job.room_id
                                room_dir.mkdir(parents=True, exist_ok=True)
                                out_path = room_dir / out_name

                                job.status = JobStatus.RECORDING
                                job.current_file = out_path
                                job.started_at = time.time()
                                job.bytes_recorded = 0
                                job.error_message = ""
                                job.stop_event.clear()

                                t = threading.Thread(
                                    target=self._record_worker,
                                    args=(job, info, out_path),
                                    name=f"rec-{job.room_id}",
                                    daemon=True,
                                raid=False) if hasattr(threading.Thread, "raid") else threading.Thread(
                                    target=self._record_worker,
                                    args=(job, info, out_path),
                                    name=f"rec-{job.room_id}",
                                    daemon=True,
                                )
                                job.thread = t
                                t.start()
                elif info is not None and not info.is_live:
                    if info.room_status in ("error", "geo_blocked"):
                        job.error_message = info.error or info.room_status
                        logger.warning(
                            "Room %s not recordable [%s]: %s",
                            job.room_id, info.room_status, info.error,
                        )
                    else:
                        job.error_message = ""

                if i < len(candidates) - 1 and stagger_seconds > 0:
                    time.sleep(stagger_seconds)
        finally:
            self._polling_lock.release()

    def _record_worker(
        self, job: RecordingJob, info: StreamInfo, out_path: Path
    ) -> None:
        """Worker thread executing record_stream_to_disk and lifecycle transitions."""
        started_mono = time.monotonic()
        result_mp4: Optional[Path] = None

        def _reprobe() -> Optional[StreamInfo]:
            """Refresh the room's HLS URL when the playlist token expires."""
            fresh = probe_room(job.room_id, proxy=self.proxy)
            if fresh.is_live and fresh.hls_url:
                return fresh
            return None

        try:
            def on_progress(bytes_written: int, _elapsed: float) -> None:
                job.bytes_recorded = bytes_written

            if self.use_ffmpeg_recorder:
                result_mp4 = record_stream_ffmpeg(
                    info=info,
                    out_mp4=out_path,
                    stop_event=job.stop_event,
                    max_duration_sec=self.max_duration_sec,
                    max_bytes=self.max_bytes,
                    on_progress=on_progress,
                    proxy=self.proxy,
                    min_valid_bytes=MIN_VALID_STREAM_BYTES,
                )
            else:
                result_mp4 = record_stream_to_disk(
                    info=info,
                    out_mp4=out_path,
                    stop_event=job.stop_event,
                    max_bytes=self.max_bytes,
                    on_progress=on_progress,
                    proxy=self.proxy,
                    reprobe_fn=_reprobe,
                    min_valid_bytes=MIN_VALID_STREAM_BYTES,
                )
        except Exception as e:
            logger.exception("Recording failed for room %s: %s", job.room_id, e)
            job.error_message = str(e)
        finally:
            duration_sec = max(0.0, time.monotonic() - started_mono)

            with self._lock:
                if job.stop_event.is_set():
                    job.status = JobStatus.STOPPED
                elif job.error_message:
                    job.status = JobStatus.ERROR
                else:
                    job.status = JobStatus.STANDBY
                job.current_file = None

            if result_mp4 and result_mp4.exists() and result_mp4.stat().st_size > 0:
                if result_mp4.suffix.lower() not in (".mp4", ".mkv"):
                    logger.warning(
                        "Room %s: saved RAW %s (mux failed) - it will still be uploaded",
                        job.room_id, result_mp4.name,
                    )
                if self._on_file_ready:
                    try:
                        self._on_file_ready(result_mp4, job.room_id, duration_sec)
                    except Exception as e:
                        logger.exception(
                            "Error in on_file_ready callback for %s: %s",
                            job.room_id,
                            e,
                        )
            else:
                logger.warning(
                    "Room %s: recording finished without a saved file "
                    "(duration=%.0fs, bytes=%d, error=%s)",
                    job.room_id, duration_sec, job.bytes_recorded,
                    job.error_message or "none",
                )

            release_os_memory()

    def stop_all(self, wait: bool = False, timeout: float = 60.0) -> None:
        """Signal all active/tracked jobs to stop, optionally waiting for worker threads."""
        with self._lock:
            jobs = list(self._jobs.values())

        for job in jobs:
            job.stop_event.set()
            job.status = JobStatus.STOPPED

        if wait:
            for job in jobs:
                if job.thread and job.thread.is_alive():
                    job.thread.join(timeout=timeout)
