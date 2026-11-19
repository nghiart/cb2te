"""Persistent upload queue store backed by SQLite WAL mode.

Ensures single-writer serialization with asyncio.Lock, PRAGMA journal_mode=WAL,
and PRAGMA busy_timeout=5000. Provides instant persistence and restart recovery.
Deduplication is based on (path, size, mtime).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["UploadStore", "UploadTask"]


@dataclass
class UploadTask:
    id: int
    path: str
    room_id: str
    duration_sec: int
    size: int
    mtime: float
    status: str
    retries: int
    next_retry_at: Optional[float]
    tg_msg_id: Optional[int]
    created_at: float
    updated_at: float

    @property
    def file_path(self) -> Path:
        return Path(self.path)


class UploadStore:
    """Thread-safe and async-safe persistent upload queue repository."""

    def __init__(self, db_path: Path | str = "upload_queue.db") -> None:
        self.db_path = Path(db_path).resolve()
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = asyncio.Lock()

    async def init(self) -> None:
        """Initialize SQLite database connection and table schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode
        )
        self._conn.row_factory = sqlite3.Row

        # Optimize for concurrency and durability
        await self._exec("PRAGMA journal_mode=WAL")
        await self._exec("PRAGMA busy_timeout=5000")
        await self._exec("PRAGMA synchronous=NORMAL")

        await self._exec(
            """
            CREATE TABLE IF NOT EXISTS upload_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                room_id TEXT NOT NULL,
                duration_sec INTEGER NOT NULL DEFAULT 0,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                retries INTEGER NOT NULL DEFAULT 0,
                next_retry_at REAL,
                tg_msg_id INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(path, size, mtime)
            )
            """
        )
        await self._exec(
            "CREATE INDEX IF NOT EXISTS idx_upload_queue_status ON upload_queue(status, next_retry_at)"
        )
        logger.info("UploadStore initialized at %s (WAL mode)", self.db_path)

    async def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        async with self._write_lock:
            if self._conn is None:
                raise RuntimeError("UploadStore is not initialized. Call init() first.")
            return await asyncio.to_thread(self._conn.execute, sql, params)

    async def enqueue(self, filepath: Path, room_id: str, duration_sec: int = 0) -> Optional[int]:
        """Enqueue file for upload with deduplication by (path, size, mtime)."""
        p = Path(filepath).resolve()
        if not p.exists():
            return None

        stat = p.stat()
        now = time.time()
        cursor = await self._exec(
            """
            INSERT OR IGNORE INTO upload_queue
                (path, room_id, duration_sec, size, mtime, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(p), room_id, duration_sec, stat.st_size, stat.st_mtime, now, now),
        )
        if cursor.rowcount > 0 and cursor.lastrowid:
            row_id = cursor.lastrowid
            logger.info("Enqueued file to UploadStore: %s (id=%d, size=%d)", p.name, row_id, stat.st_size)
            return row_id
        return None

    async def recover(self) -> list[UploadTask]:
        """Fetch all incomplete items (PENDING, UPLOADING) ordered by next_retry_at."""
        if self._conn is None:
            return []
        rows = await asyncio.to_thread(
            self._conn.execute,
            """
            SELECT * FROM upload_queue
            WHERE status != 'DONE' AND status != 'FAILED'
            ORDER BY next_retry_at ASC NULLS FIRST, id ASC
            """,
        )
        return [self._row_to_task(r) for r in rows.fetchall()]

    async def mark_uploading(self, task_id: int) -> None:
        """Mark task as currently in-flight."""
        await self._exec(
            "UPDATE upload_queue SET status='UPLOADING', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )

    async def mark_done(self, task_id: int, tg_msg_id: Optional[int] = None) -> None:
        """Mark task as successfully uploaded."""
        await self._exec(
            "UPDATE upload_queue SET status='DONE', tg_msg_id=?, updated_at=? WHERE id=?",
            (tg_msg_id, time.time(), task_id),
        )

    async def mark_retry(self, task_id: int, retries: int, next_retry_at: float) -> None:
        """Schedule task retry with exponential/backoff delay."""
        await self._exec(
            """
            UPDATE upload_queue
            SET status='PENDING', retries=?, next_retry_at=?, updated_at=?
            WHERE id=?
            """,
            (retries, next_retry_at, time.time(), task_id),
        )

    async def mark_failed(self, task_id: int, reason: str = "") -> None:
        """Mark task permanently failed after exceeding max retries."""
        await self._exec(
            "UPDATE upload_queue SET status='FAILED', updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        logger.warning("Upload task %d marked permanently FAILED: %s", task_id, reason)

    async def count_backlog(self) -> tuple[int, int]:
        """Return (pending_count, total_pending_bytes)."""
        if self._conn is None:
            return 0, 0
        rows = await asyncio.to_thread(
            self._conn.execute,
            """
            SELECT COUNT(*), COALESCE(SUM(size), 0)
            FROM upload_queue
            WHERE status != 'DONE' AND status != 'FAILED'
            """,
        )
        res = rows.fetchone()
        return (int(res[0]), int(res[1])) if res else (0, 0)

    def _row_to_task(self, row: sqlite3.Row) -> UploadTask:
        return UploadTask(
            id=row["id"],
            path=row["path"],
            room_id=row["room_id"],
            duration_sec=row["duration_sec"],
            size=row["size"],
            mtime=row["mtime"],
            status=row["status"],
            retries=row["retries"],
            next_retry_at=row["next_retry_at"],
            tg_msg_id=row["tg_msg_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def close(self) -> None:
        """Close database connection."""
        async with self._write_lock:
            if self._conn is not None:
                await asyncio.to_thread(self._conn.close)
                self._conn = None
