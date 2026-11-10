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


def mux_stream_copy(
    input_ts: Path | str,
    output_mp4: Path | str,
    *,
    input_format: str = "",
    timeout: float = 1800.0,
) -> tuple[bool, str]:
    """Fast remux a recorded spool (MPEG-TS or fMP4) into MP4.

    Stream copy only (``-c copy``), zero re-encoding CPU load - suitable for
    low-spec Android/Termux devices.  A ``+faststart`` remux needs to rewrite
    the whole file, so a second attempt without it is made for full disks.

    Args:
        input_ts: Path to the raw spool file (``.ts`` or ``.m4s``).
        output_mp4: Target path for the ``.mp4`` file.
        input_format: Optional ffmpeg demuxer hint (``mpegts`` / ``mov``).
        timeout: Hard limit in seconds; a stuck ffmpeg must not wedge Termux.

    Returns:
        tuple[bool, str]: ``(True, "")`` on success, or ``(False, reason)``.
    """
    in_path = Path(input_ts)
    out_path = Path(output_mp4)

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg binary not found (install it with: pkg install ffmpeg)"

    if not in_path.is_file():
        return False, f"Input file does not exist: {in_path}"

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"Failed to create output directory {out_path.parent}: {e}"

    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    in_flags = [
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-analyzeduration", "30M",
        "-probesize", "30M",
    ]
    if input_format:
        in_flags = ["-f", input_format, *in_flags]
    out_flags = [
        "-avoid_negative_ts", "make_zero",
        "-max_muxing_queue_size", "4096",
    ]
    inputs = [*in_flags, "-i", str(in_path)]

    attempts: list[list[str]] = [
        # 1) Stream copy with faststart (best for Telegram/streaming playback).
        [*base, *inputs, "-map", "0:v:0?", "-map", "0:a:0?",
         "-c", "copy", "-movflags", "+faststart", *out_flags, str(out_path)],
        # 2) Stream copy without faststart (needs no second disk pass).
        [*base, *inputs, "-map", "0:v:0?", "-map", "0:a:0?",
         "-c", "copy", *out_flags, str(out_path)],
        # 3) Last resort: keep the video track even if audio cannot be copied.
        [*base, *inputs, "-map", "0:v:0", "-c:v", "copy", "-an",
         *out_flags, str(out_path)],
    ]

    last_reason = "unknown ffmpeg failure"
    for index, cmd in enumerate(attempts, 1):
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass

        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            last_reason = f"ffmpeg timed out after {int(timeout)}s (attempt {index})"
            logger.error(last_reason)
            _safe_unlink(out_path)
            continue
        except Exception as e:
            last_reason = f"Failed to run ffmpeg: {e}"
            logger.error(last_reason)
            _safe_unlink(out_path)
            continue

        if proc.returncode == 0:
            if out_path.exists() and out_path.stat().st_size > 0:
                return True, ""
            last_reason = f"ffmpeg succeeded but output file {out_path} is missing or empty"
            logger.warning("%s (attempt %d)", last_reason, index)
            _safe_unlink(out_path)
            continue

        stderr_tail = (proc.stderr or "").strip().splitlines()
        brief = stderr_tail[-1][:300] if stderr_tail else f"exit code {proc.returncode}"
        last_reason = f"ffmpeg exited with code {proc.returncode}: {brief}"
        logger.warning("mux attempt %d failed: %s", index, brief)
        _safe_unlink(out_path)

    return False, last_reason


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
