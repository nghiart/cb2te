from __future__ import annotations

import ctypes
import gc
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

__all__ = [
    "acquire_wake_lock",
    "release_wake_lock",
    "get_safe_tmp_dir",
    "get_recordings_dir",
    "release_os_memory",
    "get_disk_free",
    "is_disk_low",
]


def acquire_wake_lock() -> bool:
    """Acquire Android wake lock via termux-wake-lock if available."""
    cmd = shutil.which("termux-wake-lock")
    if not cmd:
        return False
    try:
        res = subprocess.run(
            [cmd],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def release_wake_lock() -> bool:
    """Release Android wake lock via termux-wake-unlock if available."""
    cmd = shutil.which("termux-wake-unlock")
    if not cmd:
        return False
    try:
        res = subprocess.run(
            [cmd],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def get_safe_tmp_dir() -> Path:
    """Find and return a safe, writable temporary directory for buffering.

    Priority order:
    1. $TMPDIR environment variable
    2. Termux usr/tmp (/data/data/com.termux/files/usr/tmp)
    3. tempfile.gettempdir()
    """
    candidates: list[Path] = []

    env_tmp = os.environ.get("TMPDIR")
    if env_tmp:
        candidates.append(Path(env_tmp))

    candidates.append(Path("/data/data/com.termux/files/usr/tmp"))
    candidates.append(Path(tempfile.gettempdir()))

    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            if cand.is_dir() and os.access(cand, os.W_OK):
                return cand.resolve()
        except OSError:
            continue

    fallback = Path(tempfile.gettempdir()).resolve()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_recordings_dir() -> Path:
    """Return the output directory for finalized recordings.

    Priority:
    1. RECORDINGS_DIR environment variable if specified.
    2. ~/storage/downloads/CB2TE (Android Termux downloads symlink) if storage is accessible.
    3. ~/storage/shared/Download/CB2TE if shared storage is accessible.
    4. ./recordings (local project workspace fallback).
    """
    env_path = os.environ.get("RECORDINGS_DIR")
    if env_path:
        rec_dir = Path(env_path)
        try:
            rec_dir.mkdir(parents=True, exist_ok=True)
            return rec_dir.resolve()
        except OSError:
            pass

    home = Path.home()
    android_candidates = [
        home / "storage" / "downloads" / "CB2TE",
        home / "storage" / "downloads",
        home / "storage" / "shared" / "Download" / "CB2TE",
    ]

    for cand in android_candidates:
        try:
            # Check if parent or storage symlink exists
            if cand.parent.exists():
                cand.mkdir(parents=True, exist_ok=True)
                if cand.is_dir() and os.access(cand, os.W_OK):
                    return cand.resolve()
        except OSError:
            continue

    rec_dir = Path.cwd() / "recordings"
    try:
        rec_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return rec_dir.resolve()


def release_os_memory() -> None:
    """Trigger Python garbage collection and release free memory back to OS via malloc_trim."""
    gc.collect()
    for libname in ("libc.so", "libc.so.6"):
        try:
            libc = ctypes.CDLL(libname)
            if hasattr(libc, "malloc_trim"):
                libc.malloc_trim.argtypes = [ctypes.c_size_t]
                libc.malloc_trim.restype = ctypes.c_int
                libc.malloc_trim(0)
                break
        except Exception:
            continue


def get_disk_free(path: Path | str) -> int:
    """Return free disk space in bytes for the filesystem containing path."""
    try:
        target = Path(path).resolve()
        while not target.exists() and target != target.parent:
            target = target.parent
        return int(shutil.disk_usage(str(target)).free)
    except OSError:
        return -1


def is_disk_low(path: Path | str, min_mb: int) -> bool:
    """Check if free disk space is below min_mb megabytes threshold."""
    if min_mb <= 0:
        return False
    free = get_disk_free(path)
    if free < 0:
        return False
    return free < int(min_mb) * 1024 * 1024
