from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["HistoryRecord", "HistoryStore"]


@dataclass
class HistoryRecord:
    file_path: str
    room_id: str
    uploaded_at: float
    size: int = 0
    status: str = "uploaded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "room_id": self.room_id,
            "uploaded_at": self.uploaded_at,
            "size": self.size,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HistoryRecord:
        file_path = str(data.get("file_path") or data.get("file") or "")
        room_id = str(data.get("room_id") or data.get("room") or "")
        raw_uploaded = data.get("uploaded_at")
        if isinstance(raw_uploaded, (int, float)):
            uploaded_at = float(raw_uploaded)
        elif isinstance(raw_uploaded, str):
            try:
                uploaded_at = datetime.fromisoformat(raw_uploaded.replace("Z", "+00:00")).timestamp()
            except Exception:
                try:
                    uploaded_at = float(raw_uploaded)
                except Exception:
                    uploaded_at = 0.0
        else:
            uploaded_at = 0.0

        size = int(data.get("size") or 0)
        status = str(data.get("status") or "uploaded")
        return cls(
            file_path=file_path,
            room_id=room_id,
            uploaded_at=uploaded_at,
            size=size,
            status=status,
        )


class HistoryStore:
    """Thread-safe JSON-backed store for recorded and uploaded file history."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self._lock = threading.RLock()
        self._records: list[HistoryRecord] = []
        self.load()

    def load(self) -> None:
        """Load records from JSON file. Resilient to missing or corrupted files."""
        with self._lock:
            if not self.db_path.exists():
                self._records = []
                return

            try:
                content = self.db_path.read_text(encoding="utf-8").strip()
                if not content:
                    self._records = []
                    return
                data = json.loads(content)
                if isinstance(data, list):
                    raw_items = data
                elif isinstance(data, dict):
                    raw_items = data.get("records", [])
                else:
                    raw_items = []

                self._records = [
                    HistoryRecord.from_dict(item)
                    for item in raw_items
                    if isinstance(item, dict)
                ]
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(f"Failed to parse history database at {self.db_path}: {exc}")
                self._records = []

    def save(self) -> None:
        """Persist records atomically to disk using a temporary file."""
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            data = [r.to_dict() for r in self._records]
            tmp_path = self.db_path.with_suffix(f".tmp.{time.time_ns()}")
            try:
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp_path.replace(self.db_path)
            except OSError:
                # Fallback to direct write if atomic replacement fails
                self.db_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

    def record_upload(
        self,
        file_path: str | Path,
        room_id: str,
        size: int = 0,
        uploaded_at: float | None = None,
        status: str = "uploaded",
    ) -> HistoryRecord:
        """Record or update an uploaded file entry and persist."""
        with self._lock:
            if uploaded_at is None:
                uploaded_at = time.time()
            path_str = str(file_path)

            existing = self.get_record(path_str)
            if existing is not None:
                existing.room_id = room_id
                existing.size = size
                existing.uploaded_at = float(uploaded_at)
                existing.status = status
                rec = existing
            else:
                rec = HistoryRecord(
                    file_path=path_str,
                    room_id=room_id,
                    uploaded_at=float(uploaded_at),
                    size=size,
                    status=status,
                )
                self._records.append(rec)

            self.save()
            return rec

    def list_records(self, active_only: bool = True) -> list[HistoryRecord]:
        """Return list of records.

        If active_only=True, exclude purged records.
        Returns shallow list copy allowing item inspection/mutation.
        """
        with self._lock:
            if active_only:
                return [r for r in self._records if r.status != "purged"]
            return list(self._records)

    def sync_with_disk(self, base_dir: Path | str | None = None) -> list[HistoryRecord]:
        """Prune records whose files no longer exist on disk.

        Checks file existence across:
        1. Exact path (absolute or relative to current working directory).
        2. Relative to base_dir (if provided).
        3. By filename under base_dir (flat storage).
        4. By filename under base_dir/chaturbate/<room_id>/ (hierarchical storage).

        Marks missing records as 'purged' and persists.
        Returns:
            list[HistoryRecord]: The active records whose files still exist on disk.
        """
        with self._lock:
            base = Path(base_dir).resolve() if base_dir else None
            changed = False
            active: list[HistoryRecord] = []

            for r in self._records:
                if r.status == "purged":
                    continue

                p = Path(r.file_path)
                exists = False

                try:
                    if p.is_file():
                        exists = True
                except OSError:
                    pass

                if not exists and base:
                    candidates = [
                        base / p,
                        base / p.name,
                        base / "chaturbate" / r.room_id / p.name,
                    ]
                    for cand in candidates:
                        try:
                            if cand.is_file():
                                exists = True
                                break
                        except OSError:
                            pass

                if not exists:
                    r.status = "purged"
                    changed = True
                else:
                    active.append(r)

            if changed:
                self.save()

            return active

    def get_record(self, file_path: str | Path) -> HistoryRecord | None:
        """Find record by file path (string or resolved path match)."""
        with self._lock:
            target_str = str(file_path)
            target_resolved: Path | None = None
            try:
                target_resolved = Path(file_path).resolve()
            except Exception:
                target_resolved = None

            for r in self._records:
                if r.file_path == target_str:
                    return r
                if target_resolved is not None:
                    try:
                        if Path(r.file_path).resolve() == target_resolved:
                            return r
                    except Exception:
                        pass
            return None

    def mark_purged(self, file_path: str | Path) -> bool:
        """Mark record matching file_path as purged and persist."""
        with self._lock:
            rec = self.get_record(file_path)
            if rec is not None:
                rec.status = "purged"
                self.save()
                return True
            return False

    def delete_record(self, file_path: str | Path) -> bool:
        """Remove a record entirely from store and persist."""
        with self._lock:
            rec = self.get_record(file_path)
            if rec is not None:
                self._records.remove(rec)
                self.save()
                return True
            return False

    def __len__(self) -> int:
        with self._lock:
            return len([r for r in self._records if r.status != "purged"])
