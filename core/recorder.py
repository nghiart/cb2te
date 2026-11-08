"""Low-RAM Progressive Recorder Engine.

Streams HLS media directly to disk in small progressive chunks (<= 64KB),
enforcing a 1.8GB Telegram upload rotation cap and minimal RAM footprint (<16MB).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core.android import get_safe_tmp_dir, release_os_memory
from core.config import normalize_room_id
from core.muxer import mux_stream_copy

logger = logging.getLogger(__name__)

__all__ = [
    "StreamInfo",
    "probe_room",
    "parse_hls_segments",
    "parse_target_duration",
    "record_stream_to_disk",
]

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


@dataclass
class StreamInfo:
    room_id: str
    is_live: bool
    hls_url: str = ""
    width: int = 0
    height: int = 0


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": _DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
    }


def _get_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def probe_room(room_id: str, proxy: str = "") -> StreamInfo:
    """Probe Chaturbate room status and extract HLS live stream info.

    Returns:
        StreamInfo: Populated with is_live=True and hls_url if public and live,
                    or is_live=False on offline, private, or network failure.
    """
    user = normalize_room_id(room_id)
    if not user:
        return StreamInfo(room_id="", is_live=False)

    url = f"https://chaturbate.com/api/chatvideocontext/{user}/"
    headers = _default_headers()

    try:
        opener = _get_opener(proxy)
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=12) as resp:
            raw_bytes = resp.read()
            data = json.loads(raw_bytes.decode("utf-8", errors="replace"))

        status = str(data.get("room_status", "")).strip().lower()
        hls_url = str(data.get("hls_source") or data.get("hls_url") or "").strip()

        if status == "public" and hls_url:
            w = int(data.get("width") or 0)
            h = int(data.get("height") or 0)
            return StreamInfo(
                room_id=user,
                is_live=True,
                hls_url=hls_url,
                width=w,
                height=h,
            )
        return StreamInfo(room_id=user, is_live=False)
    except Exception as e:
        logger.debug(f"probe_room failed for {user}: {e}")
        return StreamInfo(room_id=user, is_live=False)


def parse_hls_segments(text: str, base_url: str) -> list[str]:
    """Parse HLS m3u8 playlist text and extract segment URLs resolved against base_url."""
    segments: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        full_url = urllib.parse.urljoin(base_url, line)
        segments.append(full_url)
    return segments


def parse_target_duration(text: str, default: float = 2.0) -> float:
    """Extract EXT-X-TARGETDURATION tag value from m3u8 playlist, bounded [1.0, 6.0]."""
    m = re.search(r"#EXT-X-TARGETDURATION:(\d+(?:\.\d+)?)", text)
    if m:
        try:
            val = float(m.group(1))
            return max(1.0, min(val, 6.0))
        except ValueError:
            pass
    return default


def record_stream_to_disk(
    info: StreamInfo,
    out_mp4: Path | str,
    stop_event: threading.Event,
    max_bytes: int = 1_800 * 1024 * 1024,
    on_progress: Optional[Callable[[int, float], None]] = None,
    proxy: str = "",
) -> Path | None:
    """Record live HLS stream directly to disk with progressive spooling.

    Downloads TS segments in sequence, streaming chunks of <= 64KB directly
    to a temporary raw TS file. Remuxes to MP4 via stream copy when stop_event
    is set, max_bytes cap (1.8GB) is reached, or playlist ends / 4 consecutive errors occur.

    Args:
        info: StreamInfo with live status and HLS URL.
        out_mp4: Destination path for final MP4 file.
        stop_event: Threading event to stop recording gracefully.
        max_bytes: Maximum raw bytes before rotating/stopping (default: 1.8GB).
        on_progress: Optional callback on_progress(bytes_written, elapsed_seconds).
        proxy: Optional HTTP/SOCKS proxy URL.

    Returns:
        Path to output MP4 if recording succeeded and non-empty, else None.
    """
    if not info.is_live or not info.hls_url:
        logger.warning(f"record_stream_to_disk: Stream is not live or missing HLS URL ({info})")
        return None

    out_path = Path(out_mp4)
    tmp_dir = get_safe_tmp_dir()
    clean_room = normalize_room_id(info.room_id) or "stream"
    unique_id = uuid.uuid4().hex[:8]
    raw_ts_path = tmp_dir / f"{clean_room}_{unique_id}.raw.ts"

    bytes_written = 0
    start_time = time.monotonic()
    seen_segments: dict[str, None] = {}
    consecutive_errors = 0
    current_playlist_url = info.hls_url
    opener = _get_opener(proxy)
    poll_interval = 2.0

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(raw_ts_path, "wb") as f_out:
            while not stop_event.is_set():
                if bytes_written >= max_bytes:
                    logger.info(
                        f"record_stream_to_disk: Reached max_bytes cap ({max_bytes} bytes). "
                        "Stopping recording for rotation."
                    )
                    break

                # Fetch playlist
                try:
                    req = urllib.request.Request(current_playlist_url, headers=_default_headers())
                    with opener.open(req, timeout=10) as resp:
                        raw_playlist = resp.read()
                        playlist_text = raw_playlist.decode("utf-8", errors="replace")
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    logger.debug(
                        f"record_stream_to_disk: Playlist refresh error ({consecutive_errors}/4): {e}"
                    )
                    if consecutive_errors >= 4:
                        logger.info(
                            "record_stream_to_disk: 4 consecutive playlist fetch errors. Stream likely ended."
                        )
                        break
                    if stop_event.wait(timeout=1.5):
                        break
                    continue

                # Handle master playlist variant resolution if present
                if "#EXT-X-STREAM-INF:" in playlist_text:
                    variants = parse_hls_segments(playlist_text, current_playlist_url)
                    if variants:
                        current_playlist_url = variants[0]
                        continue

                poll_interval = parse_target_duration(playlist_text, default=2.0)
                segments = parse_hls_segments(playlist_text, current_playlist_url)

                # Sequentially download newly discovered segments
                for seg_url in segments:
                    if stop_event.is_set() or bytes_written >= max_bytes:
                        break
                    if seg_url in seen_segments:
                        continue
                    seen_segments[seg_url] = None

                    # Limit seen_segments memory growth during multi-hour streams
                    if len(seen_segments) > 2000:
                        # Truly discard the oldest 1000 items in FIFO order
                        for old_key in list(seen_segments.keys())[:1000]:
                            del seen_segments[old_key]

                    try:
                        seg_req = urllib.request.Request(seg_url, headers=_default_headers())
                        with opener.open(seg_req, timeout=12) as seg_resp:
                            while not stop_event.is_set():
                                chunk = seg_resp.read(64 * 1024)
                                if not chunk:
                                    break
                                f_out.write(chunk)
                                bytes_written += len(chunk)

                                if on_progress:
                                    try:
                                        on_progress(bytes_written, time.monotonic() - start_time)
                                    except Exception:
                                        pass

                                if bytes_written >= max_bytes:
                                    break
                        f_out.flush()
                    except Exception as e:
                        logger.warning(f"record_stream_to_disk: Segment download failed ({seg_url}): {e}")

                if "#EXT-X-ENDLIST" in playlist_text:
                    logger.info("record_stream_to_disk: Playlist marked #EXT-X-ENDLIST. Finishing.")
                    break

                # Sleep until next poll interval, interruptible by stop_event
                sleep_sec = min(poll_interval, 3.0)
                if stop_event.wait(timeout=sleep_sec):
                    break

        # Check if any data was written
        if bytes_written == 0 or not raw_ts_path.exists() or raw_ts_path.stat().st_size == 0:
            logger.info(f"record_stream_to_disk: Zero bytes spooled for {info.room_id}.")
            return None

        # Remux to MP4
        logger.info(f"record_stream_to_disk: Remuxing {raw_ts_path} ({bytes_written} bytes) -> {out_path}")
        ok, err = mux_stream_copy(raw_ts_path, out_path)
        if ok and out_path.exists() and out_path.stat().st_size > 0:
            logger.info(f"record_stream_to_disk: Successfully recorded and muxed: {out_path}")
            return out_path
        else:
            logger.error(f"record_stream_to_disk: Muxing failed: {err}")
            return None

    finally:
        if raw_ts_path.exists():
            try:
                raw_ts_path.unlink()
            except OSError as e:
                logger.warning(f"record_stream_to_disk: Failed to delete temp file {raw_ts_path}: {e}")
        release_os_memory()
