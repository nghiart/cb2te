from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

from core.android import get_recordings_dir, is_disk_low
from storage.history import HistoryStore

logger = logging.getLogger(__name__)

__all__ = ["RetentionCleaner"]


class RetentionCleaner:
    """Manages the 48-hour video retention policy and emergency disk space eviction."""

    def __init__(
        self,
        recordings_dir: Path | str | None = None,
        on_emergency_eviction: Optional[Callable[[list[Path]], None]] = None,
    ) -> None:
        if recordings_dir is not None:
            self.recordings_dir = Path(recordings_dir).resolve()
        else:
            self.recordings_dir = get_recordings_dir()

        self.on_emergency_eviction = on_emergency_eviction
        self.last_expired_evictions: list[Path] = []
        self.last_emergency_evictions: list[Path] = []

    def _resolve_file_path(self, file_path: str | Path) -> Path:
        """Resolve a recorded file path against recordings_dir if relative."""
        p = Path(file_path)
        if not p.is_absolute():
            candidate = self.recordings_dir / p
            if candidate.exists() or not p.exists():
                return candidate
        return p

    def run_cleanup_cycle(
        self,
        store: HistoryStore,
        retention_hours: int = 48,
        min_free_mb: int = 400,
        dry_run: bool = False,
    ) -> list[Path]:
        """Execute a cleanup cycle.

        1. 48-hour retention: purges files where (now - uploaded_at) >= retention_hours * 3600.
        2. Emergency eviction: if disk free space < min_free_mb, purges oldest uploaded files
           until disk space is above threshold.

        Returns:
            list[Path]: List of file paths purged (or that would be purged if dry_run=True).
        """
        now = time.time()
        deleted: list[Path] = []
        expired_paths: list[Path] = []
        emergency_paths: list[Path] = []

        # 1. 48-Hour Retention Cleanup
        if retention_hours >= 0:
            cutoff = now - (retention_hours * 3600)
            active_records = store.list_records(active_only=True)
            for record in active_records:
                if record.uploaded_at <= cutoff:
                    p = self._resolve_file_path(record.file_path)
                    logger.info(f"Retention policy expired for {p} (uploaded_at={record.uploaded_at})")
                    if not dry_run:
                        try:
                            if p.exists():
                                p.unlink()
                        except OSError as exc:
                            logger.error(f"Failed to unlink expired file {p}: {exc}")
                        store.mark_purged(record.file_path)

                    if p not in deleted:
                        deleted.append(p)
                        expired_paths.append(p)

        # 2. Emergency Low-Disk Eviction
        if min_free_mb > 0:
            active_records = [
                r for r in store.list_records(active_only=True)
                if self._resolve_file_path(r.file_path) not in deleted
            ]
            # Sort oldest-first by uploaded_at
            active_records.sort(key=lambda r: r.uploaded_at)

            for record in active_records:
                if not is_disk_low(self.recordings_dir, min_free_mb):
                    break

                p = self._resolve_file_path(record.file_path)
                logger.warning(
                    f"Emergency disk eviction: free space low (<{min_free_mb} MB). "
                    f"Unlinking oldest uploaded file: {p}"
                )
                if not dry_run:
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError as exc:
                        logger.error(f"Failed to unlink evicted file {p}: {exc}")
                    store.mark_purged(record.file_path)

                if p not in deleted:
                    deleted.append(p)
                    emergency_paths.append(p)

        self.last_expired_evictions = expired_paths
        self.last_emergency_evictions = emergency_paths

        if emergency_paths and self.on_emergency_eviction is not None and not dry_run:
            try:
                self.on_emergency_eviction(emergency_paths)
            except Exception as exc:
                logger.error(f"Error in on_emergency_eviction callback: {exc}")

        return deleted
