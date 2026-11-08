"""Media muxing and stream inspection utilities for Termux and Unix systems.

Provides fast stream-copy remuxing (TS -> MP4 with +faststart) and ffprobe
media inspection with automatic binary path discovery (including Termux usr/bin).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess

logger = logging.getLogger(__name__)

_TERMUX_FFMPEG_CANDIDATES = [
    "/data/data/com.termux/files/usr/bin/ffmpeg",
    "/system/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
]

_TERMUX_FFPROBE_CANDIDATES = [
    "/data/data/com.termux/files/usr/bin/ffprobe",
    "/system/bin/ffprobe",
    "/usr/bin/ffprobe",
    "/usr/local/bin/ffprobe",
    "/opt/homebrew/bin/ffprobe",
]


def _is_executable(path: str) -> bool:
    """Check if file exists and has executable permissions."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_ffmpeg() -> str | None:
    """Locate ffmpeg binary in PATH or common Termux / system directories."""
    which_bin = shutil.which("ffmpeg")
    if which_bin and _is_executable(which_bin):
        return which_bin

    for candidate in _TERMUX_FFMPEG_CANDIDATES:
        if _is_executable(candidate):
            return candidate

    return None


def find_ffprobe() -> str | None:
    """Locate ffprobe binary in PATH or common Termux / system directories."""
    which_bin = shutil.which("ffprobe")
    if which_bin and _is_executable(which_bin):
        return which_bin

    for candidate in _TERMUX_FFPROBE_CANDIDATES:
        if _is_executable(candidate):
            return candidate

    return None


def inspect_media(path: Path | str) -> dict:
    """Run ffprobe on a media file to inspect codecs, dimensions, duration, and size.

    Returns:
        dict: Parsed JSON metadata from ffprobe, or empty dict {} if inspection
              fails or ffprobe binary is not available.
    """
    target = Path(path)
    if not target.is_file():
        logger.warning(f"inspect_media: File does not exist or is not a regular file: {path}")
        return {}

    ffprobe = find_ffprobe()
    if not ffprobe:
        logger.warning("inspect_media: ffprobe binary not found")
        return {}

    cmd = [
        ffprobe,
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name,width,height:format=duration,size",
        "-of", "json",
        str(target),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
        if proc.returncode != 0:
            logger.warning(
                f"inspect_media failed for {path} (exit code {proc.returncode}): {proc.stderr.strip()}"
            )
            return {}
        return json.loads(proc.stdout)
    except Exception as e:
        logger.error(f"inspect_media error inspecting {path}: {e}")
        return {}


def mux_stream_copy(input_ts: Path | str, output_mp4: Path | str) -> tuple[bool, str]:
    """Fast remux MPEG-TS into MP4 using stream copy (-c copy -movflags +faststart).

    Zero re-encoding CPU load, ideal for low-spec Android Termux devices.

    Args:
        input_ts: Path to input .ts file.
        output_mp4: Target path for .mp4 file.

    Returns:
        tuple[bool, str]: (True, "") on success, or (False, error_message) on failure.
    """
    in_path = Path(input_ts)
    out_path = Path(output_mp4)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg binary not found"

    if not in_path.is_file():
        return False, f"Input file does not exist: {in_path}"

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Failed to create output directory {out_path.parent}: {e}"

    cmd = [
        ffmpeg,
        "-y",
        "-nostdin",
        "-i", str(in_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            if out_path.exists() and out_path.stat().st_size > 0:
                return True, ""
            return False, f"ffmpeg succeeded but output file {out_path} is missing or empty"

        err_msg = proc.stderr.strip() or f"ffmpeg exited with code {proc.returncode}"
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        return False, err_msg
    except Exception as e:
        if out_path.exists():
            try:
                out_path.unlink()
            except OSError:
                pass
        return False, f"Failed to run ffmpeg: {e}"
