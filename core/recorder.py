"""Low-RAM Progressive Recorder Engine for Chaturbate live HLS streams.

The recorder streams HLS media directly to disk in small progressive chunks
(``<= 64 KB``) while supporting both container flavours Chaturbate may hand out:

* ``playlist.m3u8``  -> MPEG-TS segments (``.ts``)
* ``playlist_sfm4s.m3u8`` / CMAF -> fragmented MP4 (``#EXT-X-MAP`` init +
  ``.m4s`` fragments).  The init segment **must** be written once at the head of
  the spool, otherwise the result is headerless garbage that ffmpeg refuses to
  mux and the whole session is lost.

The engine also:

* sends browser-like headers (``Referer``/``Origin``/UA) for playlists and
  segments, which Chaturbate's edge requires;
* re-probes the room when the playlist token expires / returns 403-404-410;
* never throws away recorded data when muxing fails - the raw spool is kept as
  ``.ts``/``.m4s`` so an upload can still happen.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from core.android import get_safe_tmp_dir, release_os_memory
from core.config import normalize_room_id
from core.muxer import find_ffmpeg, mux_stream_copy
from core.net import (
    NetError,
    chaturbate_headers,
    build_opener,
    http_request,
)

logger = logging.getLogger(__name__)

__all__ = [
    "StreamInfo",
    "HlsVariant",
    "HlsPlaylist",
    "MIN_VALID_STREAM_BYTES",
    "probe_room",
    "probe_hls",
    "normalize_hls_url",
    "parse_hls_segments",
    "parse_hls_playlist",
    "parse_target_duration",
    "select_variant",
    "record_stream_to_disk",
    "record_stream_ffmpeg",
]

#: A session shorter than this is considered noise and is discarded.
MIN_VALID_STREAM_BYTES = 64 * 1024

#: Room states that mean "online but not recordable".
_BLOCKED_STATUSES = {
    "private",
    "hidden",
    "away",
    "password protected",
    "password_protected",
    "password",
    "group",
    "group show",
    "ticket",
    "ticket show",
    "spy",
    "spy show",
}

_ERROR_STATUSES = {"offline", "unknown", "error"}

_API_TEMPLATE = "https://chaturbate.com/api/chatvideocontext/{room}/"
_EDGE_URL_ENDPOINT = "https://chaturbate.com/get_edge_hls_url_ajax/"
_ROOM_PAGE_TEMPLATE = "https://chaturbate.com/{room}/"

_M3U8_RE = re.compile(r'(https?://[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*)')
_DOSSIER_RES = (
    r'window\.initialRoomDossier\s*=\s*"((?:\\.|[^"\\])*)"',
    r"window\.initialRoomDossier\s*=\s*'((?:\\.|[^'\\])*)'",
)


@dataclass
class StreamInfo:
    """Result of probing a room."""

    room_id: str
    is_live: bool
    hls_url: str = ""
    width: int = 0
    height: int = 0
    room_status: str = ""
    error: str = ""
    resolution_label: str = ""

    def __post_init__(self):
        if not self.resolution_label and self.height > 0:
            self.resolution_label = f"{self.height}p"

    def __str__(self) -> str:  # pragma: no cover - debug helper
        res = f", res={self.resolution_label!r}" if self.resolution_label else ""
        return (
            f"StreamInfo(room={self.room_id!r}, live={self.is_live}, "
            f"status={self.room_status!r}, hls={self.hls_url!r}, err={self.error!r}{res})"
        )


@dataclass
class HlsVariant:
    url: str
    bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""
    audio_group: str = ""


@dataclass
class HlsPlaylist:
    is_master: bool = False
    variants: list[HlsVariant] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    init_url: str = ""
    target_duration: float = 2.0
    ended: bool = False
    vod: bool = False
    encrypted: bool = False
    media_sequence: int = 0
    audio_playlists: dict[str, str] = field(default_factory=dict)


class _HttpStatusError(Exception):
    """Playlist/segment request returned an HTTP error status."""

    def __init__(self, code: int, url: str = "") -> None:
        super().__init__(f"HTTP {code} for {url}" if url else f"HTTP {code}")
        self.code = int(code)
        self.url = url


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_hls_url(raw: str) -> str:
    """Clean a ``hls_source`` value coming from the API or page HTML.

    Handles JSON escaping (``\\/``, ``\\u002D``), protocol-relative URLs and
    stray whitespace/quotes.
    """
    value = str(raw or "").strip().strip("\"'")
    if not value:
        return ""
    value = (
        value.replace(r"\/", "/")
        .replace("\\u002D", "-")
        .replace("\\u002d", "-")
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
    )
    if value.startswith("//"):
        return f"https:{value}"
    return value


def _headers_for(room_id: str, *, html: bool = False) -> dict[str, str]:
    return chaturbate_headers(room_id, html=html)


def _media_headers(room_id: str) -> dict[str, str]:
    """Headers used for playlist + segment fetches (edge is strict on Referer)."""
    headers = chaturbate_headers(room_id)
    headers["Accept"] = "*/*"
    headers["Accept-Encoding"] = "identity"
    headers.pop("X-Requested-With", None)
    return headers


# ---------------------------------------------------------------------------
# HLS parsing
# ---------------------------------------------------------------------------

def parse_target_duration(text: str, default: float = 2.0) -> float:
    """Extract EXT-X-TARGETDURATION tag value from m3u8 playlist, bounded [1.0, 6.0]."""
    m = re.search(r"#EXT-X-TARGETDURATION:\s*(\d+(?:\.\d+)?)", text or "")
    if m:
        try:
            val = float(m.group(1))
            return max(1.0, min(val, 6.0))
        except ValueError:
            pass
    return default


def parse_hls_segments(text: str, base_url: str) -> list[str]:
    """Return every non-tag playlist line resolved against ``base_url``.

    For a media playlist these are the segment URIs; for a master playlist the
    caller must use :func:`parse_hls_playlist` (which distinguishes variants).
    """
    segments: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        segments.append(urllib.parse.urljoin(base_url, line))
    return segments


def _parse_attribute(text: str, name: str) -> str:
    m = re.search(rf'{re.escape(name)}\s*=\s*"([^"]*)"', text or "", re.I)
    return m.group(1) if m else ""


def parse_hls_playlist(text: str, base_url: str) -> HlsPlaylist:
    """Parse a master or media playlist, including ``#EXT-X-MAP`` init data."""
    playlist = HlsPlaylist(target_duration=parse_target_duration(text, default=2.0))
    pending_variant = False
    pending_bandwidth = 0
    pending_width = 0
    pending_height = 0
    pending_codecs = ""
    pending_audio_group = ""

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXT-X-STREAM-INF"):
            pending_variant = True
            m_bw = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.I)
            pending_bandwidth = int(m_bw.group(1)) if m_bw else 0
            m_res = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
            if m_res:
                pending_width, pending_height = int(m_res.group(1)), int(m_res.group(2))
            else:
                pending_width = pending_height = 0
            pending_codecs = _parse_attribute(line, "CODECS").lower()
            pending_audio_group = _parse_attribute(line, "AUDIO")
            continue

        if line.startswith("#EXT-X-MEDIA"):
            upper = line.upper()
            if "TYPE=AUDIO" in upper:
                group_id = _parse_attribute(line, "GROUP-ID")
                uri = _parse_attribute(line, "URI")
                if group_id and uri:
                    playlist.audio_playlists[group_id] = urllib.parse.urljoin(base_url, uri)
            continue

        if line.startswith("#EXT-X-MAP"):
            uri = _parse_attribute(line, "URI")
            if uri:
                playlist.init_url = urllib.parse.urljoin(base_url, uri)
            continue

        if line.startswith("#EXT-X-KEY"):
            if "METHOD=NONE" not in line.upper():
                playlist.encrypted = True
            continue

        if line.startswith("#EXT-X-PLAYLIST-TYPE"):
            if "VOD" in line.upper():
                playlist.vod = True
            continue

        if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            try:
                playlist.media_sequence = int(line.split(":", 1)[1].strip())
            except (IndexError, ValueError):
                pass
            continue

        if line.startswith("#EXT-X-ENDLIST"):
            playlist.ended = True
            continue

        if line.startswith("#"):
            continue

        url = urllib.parse.urljoin(base_url, line)
        if pending_variant:
            playlist.variants.append(
                HlsVariant(
                    url=url,
                    bandwidth=pending_bandwidth,
                    width=pending_width,
                    height=pending_height,
                    codecs=pending_codecs,
                    audio_group=pending_audio_group,
                )
            )
            pending_variant = False
            continue
        playlist.segments.append(url)

    playlist.is_master = bool(playlist.variants)
    return playlist


def select_variant(variants: list[HlsVariant]) -> Optional[HlsVariant]:
    """Pick the best recordable variant: highest resolution, browser codecs first.

    ``_fast``/low-latency variants are deprioritised because they drop segments
    for non-ffmpeg consumers.
    """
    if not variants:
        return None

    def score(v: HlsVariant) -> tuple:
        codecs = (v.codecs or "").lower()
        browser_av = 1 if ("avc1" in codecs and ("mp4a" in codecs or "opus" in codecs)) else 0
        slow = 0 if "fast" in v.url.lower() else 1
        return (slow, browser_av, v.height, v.bandwidth, v.width)

    return max(variants, key=score)


# ---------------------------------------------------------------------------
# Room probing
# ---------------------------------------------------------------------------

def _json_get(data: object, *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_hls_from_html(html_text: str) -> str:
    """Extract ``hls_source`` from the room page (dossier JSON or raw m3u8)."""
    if not html_text:
        return ""
    for pattern in _DOSSIER_RES:
        m = re.search(pattern, html_text)
        if not m:
            continue
        raw = m.group(1)
        # JS string literal -> JSON text -> dict
        try:
            inner = json.loads(f'"{raw}"')
        except ValueError:
            inner = raw.replace(r"\/", "/")
        try:
            payload = json.loads(inner)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            url = normalize_hls_url(_json_get(payload, "hls_source", "hls_url"))
            if url:
                return url
    m = _M3U8_RE.search(html_text)
    if m:
        return normalize_hls_url(m.group(1))
    return ""


def _fetch_edge_url(room: str, proxy: str, *, timeout: float = 10.0) -> tuple[str, str, str]:
    """Fallback endpoint: POST /get_edge_hls_url_ajax/.

    Returns ``(hls_url, room_status, error)``.
    """
    csrf = uuid.uuid4().hex.upper()[:32]
    headers = chaturbate_headers(room, csrf_token=csrf)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = urllib.parse.urlencode({"room_slug": room, "bandwidth": "high"}).encode()
    try:
        res = http_request(
            _EDGE_URL_ENDPOINT,
            headers=headers,
            proxy=proxy,
            timeout=timeout,
            method="POST",
            data=body,
        )
    except NetError as exc:
        return "", "", f"edge-ajax transport error: {exc}"
    if not res.ok:
        return "", "", f"edge-ajax HTTP {res.status}"
    payload = res.json()
    if not isinstance(payload, dict):
        return "", "", "edge-ajax returned non-JSON"
    url = normalize_hls_url(_json_get(payload, "url", "hls_source", "hls_url"))
    status = _json_get(payload, "room_status", "status").lower()
    return url, status, ""


def probe_room(room_id: str, proxy: str = "", *, prefer_ts: bool = True) -> StreamInfo:
    """Probe a Chaturbate room and resolve a recordable HLS URL.

    Strategy (each step only runs if the previous one did not produce a URL):

    1. ``GET /api/chatvideocontext/<room>/`` (browser headers).
    2. ``POST /get_edge_hls_url_ajax/`` (CSRF + Referer).
    3. ``GET /<room>/`` and extract ``window.initialRoomDossier`` / raw ``.m3u8``.

    A ``public`` room with **no** ``hls_source`` is reported as
    ``room_status="geo_blocked"`` (typical for non-US residential IPs) instead of
    being silently treated as offline.
    """
    user = normalize_room_id(room_id)
    if not user:
        return StreamInfo(room_id="", is_live=False, room_status="invalid",
                          error="empty room identifier")

    errors: list[str] = []
    payload: dict = {}
    status = ""

    # --- 1. JSON API -------------------------------------------------------
    try:
        res = http_request(
            _API_TEMPLATE.format(room=urllib.parse.quote(user)),
            headers=_headers_for(user),
            proxy=proxy,
            timeout=12,
        )
        if res.ok:
            parsed = res.json()
            if isinstance(parsed, dict):
                payload = parsed
            else:
                errors.append(f"api non-JSON (HTTP {res.status})")
        else:
            errors.append(f"api HTTP {res.status}")
    except NetError as exc:
        errors.append(f"api transport: {exc}")

    status = _json_get(payload, "room_status", "status").lower()
    hls_url = normalize_hls_url(_json_get(payload, "hls_source", "hls_url", "hlsSource"))

    # --- 2. Legacy AJAX endpoint ------------------------------------------
    # Only worth calling when the API failed or reported "public" without a URL
    # (geo/WAF case). Avoids doubling requests for genuinely offline rooms.
    if not hls_url and status in ("", "public", "unknown"):
        edge_url, edge_status, edge_err = _fetch_edge_url(user, proxy)
        if edge_url:
            hls_url = edge_url
            status = status or edge_status
        elif edge_err:
            errors.append(edge_err)

    # --- 3. Room page (dossier / raw m3u8) --------------------------------
    if not hls_url and status in ("", "public"):
        try:
            page = http_request(
                _ROOM_PAGE_TEMPLATE.format(room=urllib.parse.quote(user)),
                headers=_headers_for(user, html=True),
                proxy=proxy,
                timeout=15,
            )
            if page.ok:
                hls_url = _extract_hls_from_html(page.text)
                if not hls_url:
                    errors.append("page has no hls_source")
            else:
                errors.append(f"page HTTP {page.status}")
        except NetError as exc:
            errors.append(f"page transport: {exc}")

    # --- 4. Interpret -----------------------------------------------------
    if hls_url and status not in _BLOCKED_STATUSES:
        if prefer_ts and "playlist_sfm4s.m3u8" in hls_url:
            hls_url = _prefer_ts_playlist(hls_url, user, proxy)
        width = _int_or_zero(payload.get("width") if isinstance(payload, dict) else 0)
        height = _int_or_zero(payload.get("height") if isinstance(payload, dict) else 0)

        res_label = f"{height}p" if height > 0 else ""
        if not res_label and hls_url:
            # Check URL path only (never query string token) for standard resolution patterns
            url_path = urllib.parse.urlparse(hls_url).path
            m_res = re.search(r"(?:chunklist_|playlist_|\b)(240|360|480|540|720|1080|1440|2160)p", url_path, re.I)
            if m_res:
                res_label = f"{m_res.group(1).lower()}p"

        return StreamInfo(
            room_id=user,
            is_live=True,
            hls_url=hls_url,
            width=width,
            height=height,
            room_status=status or "public",
            resolution_label=res_label,
        )

    if status in _BLOCKED_STATUSES:
        detail = f"room is {status} (online but not recordable)"
        logger.info("probe %s: %s", user, detail)
        return StreamInfo(room_id=user, is_live=False, room_status=status, error=detail)

    if status == "public" and not hls_url:
        detail = (
            "room_status=public but no hls_source "
            "(CDN/geo restriction or WAF block - configure PROXY in settings.txt)"
        )
        logger.warning("probe %s: %s | %s", user, detail, " ; ".join(errors))
        return StreamInfo(room_id=user, is_live=False, room_status="geo_blocked", error=detail)

    if errors:
        detail = " ; ".join(errors)
        logger.warning("probe %s failed: %s", user, detail)
        return StreamInfo(room_id=user, is_live=False, room_status="error", error=detail)

    return StreamInfo(room_id=user, is_live=False, room_status=status or "offline",
                      error="offline")


def _int_or_zero(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _prefer_ts_playlist(hls_url: str, room: str, proxy: str) -> str:
    """Prefer the MPEG-TS playlist when Chaturbate advertises CMAF/fMP4.

    TS is far more forgiving to remux on low-end Android; fMP4 remains supported
    when no TS variant exists.
    """
    alt = hls_url.replace("playlist_sfm4s.m3u8", "playlist.m3u8")
    if alt == hls_url:
        return hls_url
    try:
        res = http_request(alt, headers=_media_headers(room), proxy=proxy, timeout=8)
        if res.ok and "#EXTM3U" in res.text:
            logger.debug("probe %s: using TS playlist variant", room)
            return alt
        logger.debug("probe %s: TS playlist unavailable (HTTP %s)", room, res.status)
    except NetError as exc:
        logger.debug("probe %s: TS playlist check failed (%s)", room, exc)
    return hls_url


def probe_hls(
    hls_url: str,
    room_id: str = "",
    proxy: str = "",
    *,
    fetch_init: bool = True,
    fetch_first_segment: bool = True,
) -> dict:
    """Diagnostic helper: inspect a live playlist end-to-end.

    Returns a dict with the chosen variant, container flavour (TS vs fMP4), a
    sample segment size, and any error.  Used by the ``doctor`` command.
    """
    report: dict = {
        "ok": False,
        "master_url": hls_url,
        "playlist_url": "",
        "status": 0,
        "is_master": False,
        "variants": [],
        "init_url": "",
        "init_bytes": 0,
        "fmp4": False,
        "encrypted": False,
        "segment_count": 0,
        "first_segment_bytes": 0,
        "target_duration": 0.0,
        "error": "",
    }
    if not hls_url:
        report["error"] = "no HLS URL"
        return report

    opener = build_opener(proxy)
    headers = _media_headers(room_id)
    playlist_url = hls_url
    try:
        for _ in range(3):
            text, final_url = _fetch_text(opener, playlist_url, headers, timeout=10)
            report["status"] = 200
            pl = parse_hls_playlist(text, final_url or playlist_url)
            if pl.is_master:
                report["is_master"] = True
                report["variants"] = [
                    {"url": v.url, "width": v.width, "height": v.height,
                     "bandwidth": v.bandwidth}
                    for v in pl.variants
                ]
                chosen = select_variant(pl.variants)
                if chosen is None:
                    report["error"] = "master playlist without variants"
                    return report
                playlist_url = chosen.url
                continue

            report["playlist_url"] = playlist_url
            report["segment_count"] = len(pl.segments)
            report["target_duration"] = pl.target_duration
            report["init_url"] = pl.init_url
            first_seg = (pl.segments[0].split("?")[0].lower() if pl.segments else "")
            report["fmp4"] = bool(pl.init_url) or first_seg.endswith((".m4s", ".mp4"))
            report["encrypted"] = pl.encrypted
            if pl.encrypted:
                report["error"] = "HLS is encrypted (AES-128) - not supported"
                return report
            if fetch_init and pl.init_url:
                blob = _fetch_bytes(opener, pl.init_url, headers, timeout=10)
                report["init_bytes"] = len(blob)
                if not blob:
                    report["error"] = "init segment is empty"
                    return report
            if fetch_first_segment and pl.segments:
                blob = _fetch_bytes(opener, pl.segments[-1], headers, timeout=15)
                report["first_segment_bytes"] = len(blob)
                if not blob:
                    report["error"] = "segment download returned 0 bytes"
                    return report
            report["ok"] = True
            return report
        report["error"] = "too many playlist hops"
        return report
    except _HttpStatusError as exc:
        report["status"] = exc.code
        report["error"] = f"HTTP {exc.code} (expired token / blocked Referer / geo)"
        return report
    except NetError as exc:
        report["error"] = f"transport: {exc}"
        return report
    except Exception as exc:  # pragma: no cover - defensive
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report


# ---------------------------------------------------------------------------
# Low-level fetch helpers
# ---------------------------------------------------------------------------

def _fetch_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: dict[str, str],
    timeout: float = 10.0,
) -> tuple[str, str]:
    try:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            final_url = _safe_geturl(resp, url)
        return body.decode("utf-8", errors="replace"), final_url
    except urllib.error.HTTPError as exc:
        raise _HttpStatusError(int(getattr(exc, "code", 0) or 0), url) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NetError(str(exc)) from exc


def _safe_geturl(resp: object, fallback: str) -> str:
    try:
        value = resp.geturl()  # type: ignore[attr-defined]
        if isinstance(value, str) and value:
            return value
    except Exception:  # pragma: no cover - defensive
        pass
    return fallback


def _fetch_bytes(
    opener: urllib.request.OpenerDirector,
    url: str,
    headers: dict[str, str],
    timeout: float = 15.0,
) -> bytes:
    try:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise _HttpStatusError(int(getattr(exc, "code", 0) or 0), url) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NetError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem}_{index:02d}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}_{uuid.uuid4().hex[:6]}{suffix}")


def _move_file(src: Path, dst: Path) -> Path:
    """Move a file, falling back to copy+unlink across filesystems.

    ``os.replace`` fails with ``EXDEV`` when the spool lives in Termux's
    ``$TMPDIR`` (internal storage) and ``RECORDINGS_DIR`` points at ``/sdcard``
    (FUSE/SD card).  Losing the session at that point is unacceptable.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.replace(dst)
        return dst
    except OSError:
        pass
    shutil.move(str(src), str(dst))
    return dst


def record_stream_to_disk(
    info: StreamInfo,
    out_mp4: Path | str,
    stop_event: threading.Event,
    max_bytes: int = 1_800 * 1024 * 1024,
    on_progress: Optional[Callable[[int, float], None]] = None,
    proxy: str = "",
    reprobe_fn: Optional[Callable[[], Optional[StreamInfo]]] = None,
    keep_raw_on_mux_failure: bool = True,
    min_valid_bytes: int = 0,
) -> Path | None:
    """Record a live HLS stream to disk.

    Args:
        info: Live :class:`StreamInfo` with ``hls_url``.
        out_mp4: Destination ``.mp4`` path.
        stop_event: Set to stop recording gracefully (data is still muxed).
        max_bytes: Rotate/finish after this many raw bytes (Telegram 2 GB cap).
        on_progress: ``callback(bytes_written, elapsed_seconds)``.
        proxy: HTTP/SOCKS proxy URL.
        reprobe_fn: Callable returning a fresh :class:`StreamInfo`; used to
            refresh expired playlist URLs (token/403) without ending the file.
        keep_raw_on_mux_failure: Keep the raw ``.ts``/``.m4s`` spool when ffmpeg
            cannot remux it (prevents losing hours of footage).
        min_valid_bytes: Sessions smaller than this are discarded as noise.

    Returns:
        Path of the saved file: the ``.mp4`` on success, the raw ``.ts``/``.m4s``
        when muxing failed but data was preserved, or ``None``.
    """
    if not info.is_live or not info.hls_url:
        logger.warning("record_stream_to_disk: stream not live or missing HLS URL (%s)", info)
        return None

    out_path = Path(out_mp4)
    clean_room = normalize_room_id(info.room_id) or "stream"
    tmp_dir = get_safe_tmp_dir()
    unique_id = uuid.uuid4().hex[:8]
    spool_path = tmp_dir / f"{clean_room}_{unique_id}.spool"
    headers = _media_headers(clean_room)
    opener = build_opener(proxy)

    master_url = info.hls_url
    playlist_url = info.hls_url

    seen_segments: dict[str, None] = {}
    bytes_written = 0
    start_time = time.monotonic()
    consecutive_errors = 0
    seg_fail_streak = 0
    reprobe_count = 0
    variant_resets = 0
    poll_interval = 2.0
    init_hash = ""
    is_fmp4 = False
    cap_reached = False

    logger.info("Recording %s from %s", clean_room, playlist_url)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(spool_path, "wb") as f_out:
            while not stop_event.is_set():
                if bytes_written >= max_bytes:
                    logger.info(
                        "record_stream_to_disk: reached max_bytes cap (%d). Rotating.",
                        max_bytes,
                    )
                    cap_reached = True
                    break

                # -- fetch playlist ------------------------------------
                try:
                    playlist_text, final_url = _fetch_text(
                        opener, playlist_url, headers, timeout=10
                    )
                    consecutive_errors = 0
                    seg_fail_streak = 0
                except _HttpStatusError as exc:
                    consecutive_errors += 1
                    logger.warning(
                        "record_stream_to_disk: playlist HTTP %s (%d/4) url=%s",
                        exc.code, consecutive_errors, playlist_url,
                    )
                    # A variant URL can expire while the master URL still works.
                    # Retry the master at most twice, otherwise a dead variant
                    # would ping-pong forever.
                    if playlist_url != master_url and variant_resets < 2:
                        variant_resets += 1
                        logger.info(
                            "record_stream_to_disk: variant playlist failed, "
                            "falling back to master (attempt %d)", variant_resets,
                        )
                        playlist_url = master_url
                        if stop_event.wait(timeout=1.0):
                            break
                        continue
                    if _try_reprobe(
                        reprobe_fn, exc.code, consecutive_errors
                    ) and reprobe_count < 5:
                        reprobe_count += 1
                        new_info = _call_reprobe(reprobe_fn)
                        if new_info and new_info.is_live and new_info.hls_url:
                            master_url = playlist_url = new_info.hls_url
                            consecutive_errors = 0
                            logger.info("Refreshed HLS URL (reprobe %d)", reprobe_count)
                            continue
                    if consecutive_errors >= 4:
                        logger.info("record_stream_to_disk: playlist gone, finishing")
                        break
                    if stop_event.wait(timeout=min(1.5 * consecutive_errors, 8.0)):
                        break
                    continue
                except (urllib.error.URLError, NetError, OSError) as exc:
                    consecutive_errors += 1
                    logger.warning(
                        "record_stream_to_disk: playlist error (%d/4): %s",
                        consecutive_errors, exc,
                    )
                    if consecutive_errors >= 4:
                        logger.info("record_stream_to_disk: stream unreachable, finishing")
                        break
                    if stop_event.wait(timeout=min(1.5 * consecutive_errors, 8.0)):
                        break
                    continue

                playlist = parse_hls_playlist(playlist_text, final_url or playlist_url)

                # -- master playlist: pick a variant -------------------
                if playlist.is_master:
                    chosen = select_variant(playlist.variants)
                    if chosen is not None:
                        # If the chosen variant relies on a separated audio group,
                        # the single-stream Python recorder cannot record both audio & video.
                        # Delegate to ffmpeg recorder if ffmpeg is available.
                        if chosen.audio_group and playlist.audio_playlists.get(chosen.audio_group):
                            if find_ffmpeg():
                                logger.info(
                                    "record_stream_to_disk: master playlist has separated audio group (%r) "
                                    "- delegating to ffmpeg recorder to preserve audio",
                                    chosen.audio_group,
                                )
                                return record_stream_ffmpeg(
                                    info=info,
                                    out_mp4=out_path,
                                    stop_event=stop_event,
                                    max_duration_sec=0,
                                    max_bytes=max_bytes,
                                    on_progress=on_progress,
                                    proxy=proxy,
                                    min_valid_bytes=min_valid_bytes,
                                )
                            logger.warning(
                                "record_stream_to_disk: stream has separated audio group but ffmpeg "
                                "is not available - recording VIDEO-ONLY"
                            )

                        if chosen.url and chosen.url != playlist_url:
                            logger.debug("record_stream_to_disk: variant %s (%dp)",
                                         chosen.url, chosen.height)
                            playlist_url = chosen.url
                            continue
                    logger.warning("record_stream_to_disk: unusable master playlist")
                    break

                # A working media playlist means the variant URL is healthy again.
                variant_resets = 0
                if not is_fmp4 and playlist.segments:
                    first_seg = playlist.segments[0].split("?")[0].lower()
                    if first_seg.endswith((".m4s", ".mp4")):
                        is_fmp4 = True

                if playlist.encrypted:
                    logger.error(
                        "record_stream_to_disk: playlist is AES-128 encrypted - "
                        "decryption is not implemented"
                    )
                    break

                if playlist.vod and bytes_written == 0:
                    logger.error("record_stream_to_disk: got a VOD/replay, not a live stream")
                    break

                poll_interval = playlist.target_duration

                # -- init segment (fMP4 / CMAF) ------------------------
                if playlist.init_url:
                    try:
                        init_bytes = _fetch_bytes(opener, playlist.init_url, headers, timeout=12)
                    except _HttpStatusError as exc:
                        logger.warning("init segment HTTP %s", exc.code)
                        init_bytes = b""
                    except (urllib.error.URLError, NetError, OSError) as exc:
                        logger.warning("init segment error: %s", exc)
                        init_bytes = b""
                    if init_bytes:
                        new_hash = hashlib.sha256(init_bytes).hexdigest()[:16]
                        if not init_hash:
                            f_out.write(init_bytes)
                            bytes_written += len(init_bytes)
                            init_hash = new_hash
                            is_fmp4 = True
                            logger.info("fMP4/CMAF stream: wrote init segment (%d bytes)",
                                        len(init_bytes))
                        elif new_hash != init_hash:
                            logger.info("Init segment changed (codec/resolution switch); rotating")
                            cap_reached = True
                            break

                # -- media segments ------------------------------------
                for seg_url in playlist.segments:
                    if stop_event.is_set() or bytes_written >= max_bytes:
                        break
                    if seg_url in seen_segments:
                        continue

                    try:
                        seg_req = urllib.request.Request(seg_url, headers=headers)
                        with opener.open(seg_req, timeout=15) as seg_resp:
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
                        seg_fail_streak = 0
                    except urllib.error.HTTPError as exc:
                        seg_fail_streak += 1
                        logger.warning("segment HTTP %s (%d/4): %s",
                                       getattr(exc, "code", "?"), seg_fail_streak, seg_url)
                        if seg_fail_streak >= 4:
                            break
                        continue
                    except (urllib.error.URLError, NetError, OSError) as exc:
                        seg_fail_streak += 1
                        logger.warning("segment error (%d/4): %s", seg_fail_streak, exc)
                        if seg_fail_streak >= 4:
                            break
                        continue

                    seen_segments[seg_url] = None
                    # Bound memory on multi-hour streams (FIFO, keeps sliding window).
                    if len(seen_segments) > 2000:
                        for old_key in list(seen_segments.keys())[:1000]:
                            del seen_segments[old_key]

                if bytes_written >= max_bytes:
                    cap_reached = True
                    break

                if playlist.ended:
                    logger.info("record_stream_to_disk: #EXT-X-ENDLIST - stream ended")
                    break

                if seg_fail_streak >= 4:
                    if reprobe_fn and reprobe_count < 5:
                        reprobe_count += 1
                        new_info = _call_reprobe(reprobe_fn)
                        if new_info and new_info.is_live and new_info.hls_url:
                            master_url = playlist_url = new_info.hls_url
                            seg_fail_streak = 0
                            logger.info("Refreshed HLS URL after segment failures (reprobe %d)",
                                        reprobe_count)
                            continue
                    logger.info("record_stream_to_disk: too many segment failures, finishing")
                    break

                if stop_event.wait(timeout=min(poll_interval, 3.0)):
                    break

        # -- finalize ------------------------------------------------
        raw_size = bytes_written
        if raw_size == 0:
            logger.info("record_stream_to_disk: zero bytes spooled for %s", info.room_id)
            return None
        if raw_size < min_valid_bytes:
            logger.info(
                "record_stream_to_disk: session too short (%d bytes < %d), discarded",
                raw_size, min_valid_bytes,
            )
            return None

        spool_suffix = ".m4s" if is_fmp4 else ".ts"
        spool_file = spool_path.with_suffix(spool_suffix)
        _move_file(spool_path, spool_file)
        logger.info(
            "record_stream_to_disk: spooled %d bytes (%s) -> muxing to %s",
            raw_size, spool_suffix, out_path,
        )

        try:
            ok, err = mux_stream_copy(
                spool_file,
                out_path,
                input_format="mov" if is_fmp4 else "mpegts",
            )
        except Exception as exc:  # muxer must never cost us the recording
            logger.exception("record_stream_to_disk: muxer crashed: %s", exc)
            ok, err = False, f"muxer crash: {exc}"

        if ok and out_path.exists() and out_path.stat().st_size > 0:
            logger.info("record_stream_to_disk: saved %s", out_path)
            return out_path

        logger.error("record_stream_to_disk: muxing failed: %s", err)

        fallback_floor = max(min_valid_bytes, MIN_VALID_STREAM_BYTES)
        if keep_raw_on_mux_failure and raw_size >= fallback_floor:
            fallback = _unique_path(out_path.with_suffix(spool_suffix))
            try:
                _move_file(spool_file, fallback)
            except OSError as exc:
                logger.error("record_stream_to_disk: could not keep raw spool: %s", exc)
                return None
            logger.warning(
                "record_stream_to_disk: kept RAW %s (%d bytes) instead of losing the session. "
                "Install/repair ffmpeg (pkg install ffmpeg) and remux it later.",
                fallback, raw_size,
            )
            return fallback
        return None

    finally:
        for leftover in (spool_path, spool_path.with_suffix(".ts"), spool_path.with_suffix(".m4s")):
            try:
                if leftover.exists():
                    leftover.unlink()
            except OSError as exc:
                logger.warning("record_stream_to_disk: failed to remove temp %s: %s", leftover, exc)
        if cap_reached:
            logger.info("record_stream_to_disk: rotation requested (cap reached)")
        release_os_memory()


def _try_reprobe(
    reprobe_fn: Optional[Callable[[], Optional[StreamInfo]]],
    code: int,
    consecutive_errors: int,
) -> bool:
    """Only refresh the playlist URL when it plausibly expired/was blocked."""
    if reprobe_fn is None or consecutive_errors < 2:
        return False
    return code in (401, 403, 404, 410, 429) or consecutive_errors >= 3


def _call_reprobe(
    reprobe_fn: Optional[Callable[[], Optional[StreamInfo]]],
) -> Optional[StreamInfo]:
    if reprobe_fn is None:
        return None
    try:
        result = reprobe_fn()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("reprobe failed: %s", exc)
        return None
    return result if isinstance(result, StreamInfo) else None


def record_stream_ffmpeg(
    info: StreamInfo,
    out_mp4: Path | str,
    stop_event: threading.Event,
    max_duration_sec: int = 1800,
    max_bytes: int = 1_800 * 1024 * 1024,
    on_progress: Optional[Callable[[int, float], None]] = None,
    proxy: str = "",
    min_valid_bytes: int = 0,
) -> Path | None:
    """Record an HLS live stream directly to MP4 via ffmpeg subprocess.

    Uses stream copy (-c copy) to achieve near-zero CPU and low memory footprint,
    enabling 4-10 concurrent recordings on Android Termux.

    Args:
        info: Live StreamInfo with hls_url.
        out_mp4: Target MP4 path.
        stop_event: Event to signal graceful stopping.
        max_duration_sec: Maximum duration in seconds before rotation.
        max_bytes: Maximum file size in bytes before rotation.
        on_progress: Progress callback(bytes_written, elapsed_seconds).
        proxy: Optional HTTP/HTTPS proxy.
        min_valid_bytes: Minimum bytes required to consider the recording valid.

    Returns:
        Path to the recorded MP4 file, or None if failed.
    """
    if not info.is_live or not info.hls_url:
        logger.warning("record_stream_ffmpeg: stream not live or missing HLS URL")
        return None

    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        logger.warning("record_stream_ffmpeg: ffmpeg binary not found, falling back to python recorder")
        return record_stream_to_disk(
            info=info,
            out_mp4=out_mp4,
            stop_event=stop_event,
            max_bytes=max_bytes,
            on_progress=on_progress,
            proxy=proxy,
            min_valid_bytes=min_valid_bytes,
        )

    out_path = Path(out_mp4)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_target = out_path.with_name(f"{out_path.stem}.part{out_path.suffix}")

    clean_room = normalize_room_id(info.room_id) or "stream"
    user_agent = chaturbate_headers(clean_room).get(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    )
    headers_str = (
        f"User-Agent: {user_agent}\r\n"
        f"Referer: https://chaturbate.com/{clean_room}/\r\n"
        f"Origin: https://chaturbate.com\r\n"
    )

    cmd = [
        ffmpeg_bin,
        "-y",
        "-headers", headers_str,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]

    if proxy:
        cmd.extend(["-http_proxy", proxy])

    cmd.extend([
        "-i", info.hls_url,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c", "copy",
    ])

    if max_duration_sec and max_duration_sec > 0:
        cmd.extend(["-t", str(max_duration_sec)])

    cmd.extend([
        "-movflags", "+faststart",
        str(temp_target),
    ])

    logger.info("record_stream_ffmpeg: starting recording for %s -> %s", clean_room, temp_target.name)
    start_mono = time.monotonic()

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        while proc.poll() is None:
            if stop_event.is_set():
                logger.info("record_stream_ffmpeg: stop_event set, stopping ffmpeg gracefully for %s", clean_room)
                try:
                    proc.communicate(input="q", timeout=8)
                except (subprocess.TimeoutExpired, Exception):
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                break

            now = time.monotonic()
            elapsed = now - start_mono

            cur_bytes = 0
            if temp_target.exists():
                try:
                    cur_bytes = temp_target.stat().st_size
                except OSError:
                    pass

            if on_progress:
                on_progress(cur_bytes, elapsed)

            if cur_bytes >= max_bytes:
                logger.info("record_stream_ffmpeg: max_bytes reached (%d bytes), stopping %s", cur_bytes, clean_room)
                try:
                    proc.communicate(input="q", timeout=8)
                except (subprocess.TimeoutExpired, Exception):
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                break

            time.sleep(1.0)

        # Ensure process has fully exited and flushed output to disk
        if proc.poll() is None:
            try:
                proc.communicate(input="q", timeout=5)
            except (subprocess.TimeoutExpired, Exception):
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        else:
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    except Exception as exc:
        logger.error("record_stream_ffmpeg error for %s: %s", clean_room, exc)
        if proc and proc.poll() is None:
            proc.kill()
    finally:
        release_os_memory()

    if temp_target.exists():
        size = temp_target.stat().st_size
        valid_floor = max(min_valid_bytes, MIN_VALID_STREAM_BYTES)
        if size >= valid_floor:
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass
            temp_target.rename(out_path)
            logger.info("record_stream_ffmpeg: completed %s (%d bytes)", out_path.name, size)
            return out_path
        else:
            logger.warning(
                "record_stream_ffmpeg: discarded small recording %s (%d bytes < %d floor)",
                temp_target.name, size, valid_floor,
            )
            try:
                temp_target.unlink()
            except OSError:
                pass

    return None
