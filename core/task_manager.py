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

from core.android import get_disk_free, get_recordings_dir, is_disk_low, release_os_memory
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
    fail_count: int = 0
    cooldown_until: float = 0.0

    def mark_error(self, error: str) -> None:
        """Increment fail count and compute backoff cooldown."""
        self.fail_count += 1
        self.error_message = error
        now = time.time()
        if self.fail_count >= 5:
            self.status = JobStatus.STANDBY  # Remain in standby but un-dispatchable
            # Exponential backoff: 30s, 2m, 5m, 15m, 1h
            backoffs = [30.0, 120.0, 300.0, 900.0, 3600.0]
            wait = backoffs[min(self.fail_count - 5, len(backoffs) - 1)]
            self.cooldown_until = now + wait
        else:
            self.cooldown_until = now + 60.0

    def mark_success(self) -> None:
        """Reset fail count and cooldown on successful operations."""
        self.fail_count = 0
        self.cooldown_until = 0.0
        self.error_message = ""

    def can_dispatch(self) -> bool:
        """Check if job is ready to be dispatched for recording."""
        if self.status != JobStatus.STANDBY:
            return False
        if self.cooldown_until and time.time() < self.cooldown_until:
            return False
        return True


def _normalize_stream_status(status_str: str, is_live: bool = False, is_recording: bool = False) -> str:
    """Normalize raw stream status into fundamental states: online, private, offline, error."""
    if is_live:
        return "online"
    if not status_str:
        return "offline"
    s = status_str.strip().lower()
    if "online" in s or "public" in s:
        return "online"
    if "private" in s or "away" in s or "hidden" in s:
        return "private"
    if "error" in s or "timeout" in s or "fail" in s:
        return "error"
    return "offline"


class TaskManager:
    """Manages recording jobs, concurrency limits, staggered polling, and status change tracking."""

    def __init__(
        self,
        max_concurrent: int = 4,
        recordings_dir: Optional[Path | str] = None,
        proxy: str = "",
        max_bytes: int = 1_800 * 1024 * 1024,
        max_duration_sec: int = 7200,
        use_ffmpeg_recorder: bool = False,
        max_recording_height: int = 1080,
        min_free_mb: int = 400,
    ) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.proxy = proxy
        self.max_bytes = max_bytes
        self.max_duration_sec = max_duration_sec
        self.use_ffmpeg_recorder = use_ffmpeg_recorder
        self.max_recording_height = max_recording_height
        self.min_free_mb = min_free_mb

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
        self._on_session_started: Optional[Callable[[str, StreamInfo, Path], None]] = None
        self._on_session_finished: Optional[Callable[[str, float, int, Optional[Path]], None]] = None
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

    def set_on_session_started_callback(
        self, cb: Callable[[str, StreamInfo, Path], None]
    ) -> None:
        """Set callback invoked when a room starts recording."""
        with self._lock:
            self._on_session_started = cb

    def set_on_session_finished_callback(
        self, cb: Callable[[str, float, int, Optional[Path]], None]
    ) -> None:
        """Set callback invoked when a recording session finishes."""
        with self._lock:
            self._on_session_finished = cb

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

    def check_all_room_statuses(
        self, dispatch_recording: bool = False, stagger_seconds: float = 0.0
    ) -> list[tuple[str, StreamInfo, bool]]:
        """Probe all tracked rooms and fire callbacks if any status changed.

        If dispatch_recording=True, automatically starts recording live rooms up to max_concurrent.
        Returns: list of (room_id, stream_info, has_changed).
        """
        with self._lock:
            tracked_rooms = list(self._jobs.keys())

        results = []
        all_statuses: dict[str, tuple[str, StreamInfo]] = {}
        changes: list[tuple[str, str, str, StreamInfo]] = []

        for idx, room in enumerate(tracked_rooms):
            try:
                info = probe_room(room, proxy=self.proxy)
            except Exception as e:
                info = StreamInfo(room_id=room, is_live=False, room_status="error", error=str(e))

            changed = False
            old_full = "unknown"
            new_full = "unknown"

            with self._lock:
                job = self._jobs.get(room)
                if job:
                    is_rec = job.status in (JobStatus.RECORDING, JobStatus.MUXING)
                    raw_new = "online" if info.is_live else (info.room_status or "offline")
                    new_norm = _normalize_stream_status(raw_new, is_live=info.is_live, is_recording=is_rec)
                    old_norm = _normalize_stream_status(job.last_detected_status) if job.last_detected_status != "unknown" else "unknown"

                    res_str = info.resolution_label or job.last_detected_resolution

                    # Transient probe errors (network drop / HTTP 429/502) must NEVER flap status
                    if new_norm == "error":
                        new_norm = old_norm if old_norm != "unknown" else "offline"

                    old_full = f"{old_norm} ({job.last_detected_resolution})" if (old_norm == "online" and job.last_detected_resolution) else old_norm
                    new_full = f"{new_norm} ({res_str})" if (new_norm == "online" and res_str) else new_norm

                    if old_norm == "unknown":
                        # Initial baseline setup: record status without triggering alert
                        job.last_detected_status = new_norm
                        job.last_detected_resolution = res_str
                        job.last_status_checked_at = time.time()
                    elif old_norm != new_norm:
                        # Fundamental state transition (offline <-> online <-> private)
                        changed = True
                        job.last_detected_status = new_norm
                        job.last_detected_resolution = res_str
                        job.last_status_checked_at = time.time()
                    else:
                        # State is identical (e.g. online -> online): update resolution if newly found
                        if res_str and not job.last_detected_resolution:
                            job.last_detected_resolution = res_str
                        job.last_status_checked_at = time.time()

                    if dispatch_recording and info.is_live and info.hls_url:
                        if job.can_dispatch():
                            if self.min_free_mb > 0 and is_disk_low(self.recordings_dir, self.min_free_mb):
                                logger.warning(
                                    "Low disk space (<%d MB). Pausing recording dispatch for room %s.",
                                    self.min_free_mb, job.room_id,
                                )
                                job.error_message = f"Paused: low disk space (<{self.min_free_mb} MB)"
                            else:
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
                                    )
                                    job.thread = t
                                    t.start()

                                    if self._on_session_started:
                                        try:
                                            self._on_session_started(job.room_id, info, out_path)
                                        except Exception as e:
                                            logger.warning("Error in on_session_started callback for %s: %s", job.room_id, e)

            all_statuses[room] = (new_full, info)

            if changed:
                changes.append((room, old_full, new_full, info))
                if self._on_status_change:
                    try:
                        self._on_status_change(room, old_full, new_full, info)
                    except Exception as e:
                        logger.warning("Error in on_status_change for %s: %s", room, e)

            results.append((room, info, changed))

            if stagger_seconds > 0 and idx < len(tracked_rooms) - 1:
                time.sleep(stagger_seconds)

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
                            and job.can_dispatch()
                        ):
                            if self.min_free_mb > 0 and is_disk_low(self.recordings_dir, self.min_free_mb):
                                logger.warning(
                                    "Low disk space (<%d MB). Pausing recording dispatch for room %s.",
                                    self.min_free_mb, job.room_id,
                                )
                                job.error_message = f"Paused: low disk space (<{self.min_free_mb} MB)"
                            else:
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
                                    )
                                    job.thread = t
                                    t.start()

                                    if self._on_session_started:
                                        try:
                                            self._on_session_started(job.room_id, info, out_path)
                                        except Exception as e:
                                            logger.warning("Error in on_session_started callback for %s: %s", job.room_id, e)
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
                if self.min_free_mb > 0 and is_disk_low(self.recordings_dir, self.min_free_mb):
                    logger.warning(
                        "Low disk space during recording (<%d MB). Stopping recording for room %s.",
                        self.min_free_mb, job.room_id,
                    )
                    job.stop_event.set()

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
                    max_height=self.max_recording_height,
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
                    job.mark_error(job.error_message)
                else:
                    job.status = JobStatus.STANDBY
                    job.mark_success()
                job.current_file = None
                job.last_detected_status = "offline"

            saved_file = result_mp4 if (result_mp4 and result_mp4.exists() and result_mp4.stat().st_size > 0) else None

            if self._on_session_finished:
                try:
                    self._on_session_finished(job.room_id, duration_sec, job.bytes_recorded, saved_file)
                except Exception as e:
                    logger.warning("Error in on_session_finished callback for %s: %s", job.room_id, e)

            if saved_file:
                if saved_file.suffix.lower() not in (".mp4", ".mkv"):
                    logger.warning(
                        "Room %s: saved RAW %s (mux failed) - it will still be uploaded",
                        job.room_id, saved_file.name,
                    )
                if self._on_file_ready:
                    try:
                        self._on_file_ready(saved_file, job.room_id, duration_sec)
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
