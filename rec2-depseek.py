#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recmd_cb v7.2 — Pure-RAM Pipeline to Single Sequential NVMe Write (Fixed flush crash).

Kiến trúc Pure-RAM:
  1. Zero NVMe Write trong Recording & Standby (chunked RamBuffer giữ Heap).
  2. In-RAM FFmpeg mux + faststart trên /dev/shm (0 byte chạm NVMe).
  3. Single Sequential NVMe commit (16 MB block copy) sau khi ffprobe xác nhận.
  4. Tên file: chaturbate_<room>_<YYYYMMDD>_<HHMMSS_start>-<HHMMSS_end>.mp4
"""

from __future__ import annotations

import argparse
import codecs
import ctypes
import gc
import hashlib
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import (
    HTTPCookieProcessor,
    HTTPHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
RECORD_DIR = BASE_DIR / "recordings"
SETTINGS_FILE = BASE_DIR / "settings.txt"
FAVORITES_FILE = BASE_DIR / "favorites.txt"
SCRIPT_NAME = Path(__file__).name

_UID = os.getuid() if hasattr(os, "getuid") else 0
SHM_BASE = (
    Path("/dev/shm")
    if Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK)
    else Path(tempfile.gettempdir())
)
RAM_WORK_DIR = SHM_BASE / f"recmd_cb_ram_{_UID}"
RAM_STATE_DIR = SHM_BASE / f"recmd_cb_state_{_UID}"
RAM_LOG_DIR = RAM_STATE_DIR / "logs"
LOCAL_STATE_LINK = BASE_DIR / "state"

RAM_FLUSH_TARGET_MB = 980
RAM_FLUSH_TARGET_BYTES = RAM_FLUSH_TARGET_MB * 1024 * 1024
RAM_SAFE_RESERVE_BYTES = 2 * RAM_FLUSH_TARGET_BYTES + 512 * 1024 * 1024
MIN_VALID_STREAM_BYTES = 32 * 1024
NVME_SEQ_COPY_CHUNK = 16 * 1024 * 1024

STANDBY_SEC = 35
UA_PC = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CB_CONTEXT = "https://chaturbate.com/api/chatvideocontext"
_PREFIX_RE = r"chaturbate|cb"
_FAV_NAMES = {"favorites", "favorites.txt", "fav", "fav.txt"}
FAV_STAGGER_SEC = 0.3

STOP = threading.Event()
PROXY = ""
MIN_FREE_MB = 400
MAX_MB = 0
MAX_HOURS = 0.0

DISK_CHECK_INTERVAL_SEC = 3.0
LOG_MAX_BYTES = 2 * 1024 * 1024
ONLINE_CACHE_TTL_SEC = 60.0

_SSL_CTX = ssl.create_default_context()
_LOG_CONTEXT = "main"
ONLINE_STATUS_CACHE: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# RAM helpers
# ---------------------------------------------------------------------------

def _release_os_memory() -> None:
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


class RamBuffer:
    """Chunked buffer; progressive write giữ đỉnh RAM ≈ 1x tổng dữ liệu."""
    __slots__ = ("_chunks", "_total")

    def __init__(self) -> None:
        self._chunks: deque[bytes] = deque()
        self._total = 0

    def __len__(self) -> int:
        return self._total

    def append(self, data: bytes) -> None:
        if data:
            self._chunks.append(data)
            self._total += len(data)

    extend = append

    def clear(self) -> None:
        self._chunks.clear()
        self._total = 0

    def write_to(self, path: Path, bufsize: int = 4 * 1024 * 1024) -> int:
        """Ghi cuốn chiếu sang tmpfs. Trả về số byte đã ghi."""
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with path.open("wb", buffering=bufsize) as f:
            while self._chunks:
                chunk = self._chunks.popleft()
                f.write(chunk)
                written += len(chunk)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        self._total = 0
        _release_os_memory()
        return written


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

def init_ram_workspace() -> None:
    RAM_WORK_DIR.mkdir(parents=True, exist_ok=True)
    RAM_STATE_DIR.mkdir(parents=True, exist_ok=True)
    RAM_LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if LOCAL_STATE_LINK.is_symlink():
            if LOCAL_STATE_LINK.resolve() != RAM_STATE_DIR.resolve():
                LOCAL_STATE_LINK.unlink(missing_ok=True)
                LOCAL_STATE_LINK.symlink_to(RAM_STATE_DIR, target_is_directory=True)
        elif LOCAL_STATE_LINK.is_dir():
            shutil.rmtree(LOCAL_STATE_LINK, ignore_errors=True)
            LOCAL_STATE_LINK.symlink_to(RAM_STATE_DIR, target_is_directory=True)
        elif not LOCAL_STATE_LINK.exists():
            LOCAL_STATE_LINK.symlink_to(RAM_STATE_DIR, target_is_directory=True)
    except OSError:
        pass


def recordings_dir() -> Path:
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    return RECORD_DIR


def state_dir() -> Path:
    init_ram_workspace()
    return RAM_STATE_DIR


def ram_work_dir() -> Path:
    RAM_WORK_DIR.mkdir(parents=True, exist_ok=True)
    return RAM_WORK_DIR


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def set_log_context(ctx: str) -> None:
    global _LOG_CONTEXT
    _LOG_CONTEXT = (ctx or "main").strip()


def _rotate_log_if_needed(path: Path, max_bytes: int = LOG_MAX_BYTES) -> None:
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            backup = path.with_suffix(path.suffix + ".old")
            backup.unlink(missing_ok=True)
            path.replace(backup)
    except OSError:
        pass


def room_log_path(room_id: str) -> Path:
    init_ram_workspace()
    return RAM_LOG_DIR / f"{safe_name(room_id).lower()}.log"


def events_log_path() -> Path:
    init_ram_workspace()
    return RAM_STATE_DIR / "events.log"


def log(msg: str, level: str = "INFO", room: str = "", event: bool = True) -> None:
    now_str = datetime.now().strftime("%H:%M:%S")
    ctx = room or _LOG_CONTEXT or "main"
    tag = f"[{ctx}] " if ctx not in ("main", "") else ""
    lvl_tag = f"[{level}] " if level not in ("INFO", "") else ""
    formatted = f"[{now_str}] {lvl_tag}{tag}{msg}"
    print(formatted, flush=True)

    target_room = room or (
        _LOG_CONTEXT if _LOG_CONTEXT not in ("main", "remux", "recover") else ""
    )
    if target_room:
        try:
            r_path = room_log_path(target_room)
            _rotate_log_if_needed(r_path, max_bytes=1024 * 1024)
            with r_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(formatted + "\n")
        except OSError:
            pass

    if event and not msg.startswith("  "):
        try:
            e_path = events_log_path()
            _rotate_log_if_needed(e_path, max_bytes=1024 * 1024)
            with e_path.open("a", encoding="utf-8", errors="replace") as f:
                f.write(formatted + "\n")
        except OSError:
            pass


def tail_file(path: Path, n: int = 8) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\r\n") for line in deque(f, maxlen=n) if line.strip()]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# RAM guard / filename
# ---------------------------------------------------------------------------

def system_ram_available_bytes() -> int:
    mem_avail = -1
    try:
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        mem_avail = int(parts[1]) * 1024
                    break
    except OSError:
        pass
    shm_free = -1
    try:
        shm_free = int(shutil.disk_usage(str(SHM_BASE)).free)
    except OSError:
        pass
    vals = [v for v in (mem_avail, shm_free) if v >= 0]
    return min(vals) if vals else -1


def process_rss_bytes(pid: int) -> int:
    if pid <= 0:
        return 0
    try:
        st_file = Path(f"/proc/{pid}/status")
        if st_file.is_file():
            for line in st_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1]) * 1024
    except OSError:
        pass
    return 0


def ram_low_guard() -> bool:
    avail = system_ram_available_bytes()
    return 0 <= avail < RAM_SAFE_RESERVE_BYTES


def build_final_mp4_path(room_id: str, start_dt: datetime, end_dt: datetime) -> Path:
    rid = safe_name(room_id).lower()
    date_str = start_dt.strftime("%Y%m%d")
    t_start = start_dt.strftime("%H%M%S")
    t_end = end_dt.strftime("%H%M%S")
    root = recordings_dir()
    base = f"chaturbate_{rid}_{date_str}_{t_start}-{t_end}"
    candidate = root / f"{base}.mp4"
    suffix = 1
    while candidate.exists():
        candidate = root / f"{base}_{suffix:02d}.mp4"
        suffix += 1
    return candidate


def purge_trash_files() -> None:
    try:
        if RECORD_DIR.is_dir():
            for p in RECORD_DIR.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() == ".json" or p.name.endswith(".part.mp4"):
                    p.unlink(missing_ok=True)
                elif p.suffix.lower() in (".ts", ".mp4") and p.stat().st_size == 0:
                    p.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        if RAM_WORK_DIR.is_dir():
            active_pids = {int(j.get("pid") or 0) for j in list_jobs(alive_only=True)}
            for p in RAM_WORK_DIR.iterdir():
                m = re.search(r"_(\d+)_", p.name)
                if m and int(m.group(1)) not in active_pids:
                    p.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# In-RAM mux + sequential NVMe commit
# ---------------------------------------------------------------------------

def _inspect_media_streams(media_path: Path) -> dict:
    res = {"has_video": False, "has_valid_audio": False, "audio_codec": "", "sample_rate": 0}
    pb = find_ffprobe()
    if not pb or not media_path.is_file():
        # Optimistic fallback khi không có ffprobe: giả định hợp lệ
        return {"has_video": True, "has_valid_audio": True,
                "audio_codec": "aac", "sample_rate": 48000}
    try:
        r = subprocess.run(
            [pb, "-v", "error", "-analyzeduration", "30M", "-probesize", "30M",
             "-show_entries", "stream=codec_type,codec_name,sample_rate,channels",
             "-of", "json", str(media_path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
        )
        if r.returncode != 0:
            return res
        data = json.loads((r.stdout or b"{}").decode("utf-8", "replace"))
        for s in (data.get("streams") or []):
            if not isinstance(s, dict):
                continue
            ctype = str(s.get("codec_type") or "").lower()
            if ctype == "video":
                res["has_video"] = True
            elif ctype == "audio":
                sr = int(s.get("sample_rate") or 0)
                ch = int(s.get("channels") or 0)
                if sr > 0 and ch > 0:
                    res["has_valid_audio"] = True
                    res["audio_codec"] = str(s.get("codec_name") or "")
                    res["sample_rate"] = sr
        return res
    except Exception:
        return res


def _sequential_copy_ram_to_nvme(ram_src: Path, nvme_dst: Path) -> None:
    nvme_dst.parent.mkdir(parents=True, exist_ok=True)
    with ram_src.open("rb", buffering=NVME_SEQ_COPY_CHUNK) as fsrc, \
         nvme_dst.open("wb", buffering=NVME_SEQ_COPY_CHUNK) as fdst:
        shutil.copyfileobj(fsrc, fdst, length=NVME_SEQ_COPY_CHUNK)
        fdst.flush()
        os.fsync(fdst.fileno())


def _mux_ram_variants(
    ff: str,
    ram_raw_v: Path,
    ram_raw_a: Optional[Path],
    ram_out_mp4: Path,
    source_has_audio: bool,
    ext_audio_ok: bool,
) -> tuple[bool, dict, str]:
    """Thử các cấu hình mux trên /dev/shm; trả (success, out_info, last_reason)."""
    in_flags = [
        "-analyzeduration", "30M", "-probesize", "30M",
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
    ]
    out_flags = ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]

    attempts: list[list[str]] = []

    if ext_audio_ok and ram_raw_a is not None:
        # 2 input: video TS + external audio playlist TS
        attempts.append([
            ff, "-y", "-hide_banner", "-loglevel", "error",
            *in_flags, "-i", str(ram_raw_v),
            *in_flags, "-i", str(ram_raw_a),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-af", "aresample=async=1:first_pts=0",
            *out_flags, str(ram_out_mp4),
        ])
    else:
        # 1) Video copy + AAC encode audio (nếu có)
        attempts.append([
            ff, "-y", "-hide_banner", "-loglevel", "error",
            *in_flags, "-i", str(ram_raw_v),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-af", "aresample=async=1:first_pts=0",
            *out_flags, str(ram_out_mp4),
        ])
        # 2) Video copy + audio copy (bitstream filter AAC)
        attempts.append([
            ff, "-y", "-hide_banner", "-loglevel", "error",
            *in_flags, "-i", str(ram_raw_v),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
            *out_flags, str(ram_out_mp4),
        ])
        # 3) Pure copy (sẽ lỗi nếu audio không phải AAC; chấp nhận video-only)
        attempts.append([
            ff, "-y", "-hide_banner", "-loglevel", "error",
            *in_flags, "-i", str(ram_raw_v),
            "-c", "copy", *out_flags, str(ram_out_mp4),
        ])

    out_info = {"has_video": False, "has_valid_audio": False,
                "audio_codec": "", "sample_rate": 0}
    last_reason = ""

    for idx, cmd in enumerate(attempts, 1):
        ram_out_mp4.unlink(missing_ok=True)
        try:
            r = subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=900, start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            last_reason = f"attempt {idx}: timeout"
            log(f"Mux attempt {idx}: TIMEOUT", level="WARN", event=False)
            continue
        except Exception as e:
            last_reason = f"attempt {idx}: {e}"
            log(f"Mux attempt {idx}: exception {e}", level="WARN", event=False)
            continue

        if r.returncode != 0:
            stderr_lines = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            brief = stderr_lines[-1][:180] if stderr_lines else ""
            last_reason = f"attempt {idx} rc={r.returncode}: {brief}"
            log(f"Mux attempt {idx}: FAILED rc={r.returncode} — {brief}",
                level="WARN", event=False)
            continue

        if not ram_out_mp4.is_file() or ram_out_mp4.stat().st_size < 4096:
            last_reason = f"attempt {idx}: empty output"
            log(f"Mux attempt {idx}: produced no valid file", level="WARN", event=False)
            continue

        out_info = _inspect_media_streams(ram_out_mp4)
        if not out_info["has_video"]:
            last_reason = f"attempt {idx}: no video track"
            log(f"Mux attempt {idx}: no video stream", level="WARN", event=False)
            continue
        if source_has_audio and not out_info["has_valid_audio"]:
            # Không reject cứng — chỉ ưu tiên attempt kế tiếp; cuối cùng vẫn accept
            last_reason = f"attempt {idx}: missing audio (source had audio)"
            log(f"Mux attempt {idx}: audio lost vs source", level="WARN", event=False)
            continue

        log(f"Mux attempt {idx}: OK "
            f"(video={'Y' if out_info['has_video'] else 'N'}, "
            f"audio={'Y ' + out_info['audio_codec'] if out_info['has_valid_audio'] else 'N'}) "
            f"-> {fmt_mb(ram_out_mp4.stat().st_size)}",
            event=False)
        return True, out_info, ""

    # Last chance: chấp nhận bất kỳ output có video
    if ram_out_mp4.is_file() and ram_out_mp4.stat().st_size >= 4096:
        out_info = _inspect_media_streams(ram_out_mp4)
        if out_info["has_video"]:
            log("Mux fallback: accepting last output (video-only may apply)",
                level="WARN", event=False)
            return True, out_info, ""

    return False, out_info, last_reason or "all attempts failed"


def _fallback_raw_ts_to_nvme(
    ram_raw_v: Path, room_id: str, start_dt: datetime, end_dt: datetime,
) -> Optional[Path]:
    """Nếu mux thất bại hoàn toàn, cứu dữ liệu bằng cách copy raw TS (.ts)."""
    if not ram_raw_v.is_file() or ram_raw_v.stat().st_size < MIN_VALID_STREAM_BYTES:
        return None
    rid = safe_name(room_id).lower()
    base = f"chaturbate_{rid}_{start_dt.strftime('%Y%m%d_%H%M%S')}-{end_dt.strftime('%H%M%S')}"
    dst = recordings_dir() / f"{base}.ts"
    n = 1
    while dst.exists():
        dst = recordings_dir() / f"{base}_{n:02d}.ts"
        n += 1
    try:
        _sequential_copy_ram_to_nvme(ram_raw_v, dst)
        log(f"Fallback: saved raw TS to NVMe: {dst.name} ({fmt_mb(dst.stat().st_size)})",
            level="WARN")
        return dst
    except Exception as e:
        log(f"Fallback raw TS copy failed: {e}", level="ERROR")
        return None


def flush_ram_buffer_to_nvme_mp4(
    video: RamBuffer,
    audio: RamBuffer,
    room_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> Optional[Path]:
    total = len(video) + len(audio)
    if total < MIN_VALID_STREAM_BYTES:
        log(f"Skip flush: only {fmt_mb(total)} "
            f"(< {fmt_mb(MIN_VALID_STREAM_BYTES)}, video={fmt_mb(len(video))}, "
            f"audio={fmt_mb(len(audio))})",
            level="WARN", event=False)
        video.clear(); audio.clear(); _release_os_memory()
        return None

    ff = find_ffmpeg()
    if not ff:
        log("ffmpeg not found; cannot mux", level="ERROR")
        video.clear(); audio.clear(); _release_os_memory()
        return None

    rdir = ram_work_dir()
    pid = os.getpid()
    stamp = uuid.uuid4().hex[:8]
    ram_raw_v = rdir / f"raw_v_{pid}_{stamp}.ts"
    ram_raw_a = rdir / f"raw_a_{pid}_{stamp}.ts" if len(audio) > 1024 else None
    ram_out_mp4 = rdir / f"mux_{pid}_{stamp}.mp4"
    final_nvme_mp4 = build_final_mp4_path(room_id, start_dt, end_dt)

    log(f"Flushing RAM buffer: {fmt_mb(total)} "
        f"(video={fmt_mb(len(video))}, audio={fmt_mb(len(audio))})",
        event=False)

    try:
        # -------- Phase 1: progressive write to /dev/shm --------
        try:
            v_bytes = video.write_to(ram_raw_v)
            a_bytes = audio.write_to(ram_raw_a) if ram_raw_a is not None else 0
            log(f"Wrote to /dev/shm: video={fmt_mb(v_bytes)}, audio={fmt_mb(a_bytes)}",
                event=False)
        except Exception as e:
            log(f"Phase 1 (write tmpfs) failed: {e}", level="ERROR")
            return None

        src_info = _inspect_media_streams(ram_raw_v)
        ext_audio_ok = bool(ram_raw_a and ram_raw_a.is_file()
                            and ram_raw_a.stat().st_size > 1024)
        source_has_audio = src_info["has_valid_audio"] or ext_audio_ok

        # -------- Phase 2: mux on /dev/shm --------
        raw_v_size = ram_raw_v.stat().st_size
        log(f"Muxing {fmt_mb(raw_v_size)} on /dev/shm "
            f"(src video={'Y' if src_info['has_video'] else 'N'}, "
            f"src audio={'Y ' + src_info['audio_codec'] if src_info['has_valid_audio'] else 'N'}, "
            f"ext_audio={'Y' if ext_audio_ok else 'N'})",
            event=False)

        try:
            ok, out_info, reason = _mux_ram_variants(
                ff, ram_raw_v, ram_raw_a, ram_out_mp4, source_has_audio, ext_audio_ok,
            )
        except Exception as e:
            log(f"Phase 2 (mux) crashed: {e}", level="ERROR")
            ok, out_info, reason = False, {}, f"crash: {e}"

        if not ok:
            log(f"Mux FAILED: {reason}", level="ERROR")
            _fallback_raw_ts_to_nvme(ram_raw_v, room_id, start_dt, end_dt)
            return None

        # -------- Phase 3: single sequential NVMe write --------
        out_size = ram_out_mp4.stat().st_size
        log(f"Sequential NVMe write: {final_nvme_mp4.name} ({fmt_mb(out_size)}) ...",
            event=False)
        try:
            _sequential_copy_ram_to_nvme(ram_out_mp4, final_nvme_mp4)
        except Exception as e:
            log(f"Phase 3 (NVMe copy) failed: {e}", level="ERROR")
            _fallback_raw_ts_to_nvme(ram_raw_v, room_id, start_dt, end_dt)
            return None

        final_sz = final_nvme_mp4.stat().st_size
        aud_tag = (
            f"Video+Audio ({out_info['audio_codec'].upper()} {out_info['sample_rate']}Hz)"
            if out_info.get("has_valid_audio") else "Video-only"
        )
        log(f"Flushed RAM -> NVMe [{aud_tag}]: {final_nvme_mp4.name} "
            f"({fmt_mb(final_sz)})")
        return final_nvme_mp4
    except Exception as e:
        log(f"Flush pipeline error: {e}", level="ERROR")
        return None
    finally:
        video.clear()
        audio.clear()
        for p in (ram_raw_v, ram_raw_a, ram_out_mp4):
            if p is not None:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        _release_os_memory()


# ---------------------------------------------------------------------------
# Settings & HTTP
# ---------------------------------------------------------------------------

def load_proxy_file() -> str:
    if not SETTINGS_FILE.exists():
        return ""
    try:
        for raw in SETTINGS_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip().lower() == "proxy":
                return v.strip()
    except OSError:
        pass
    return ""


def resolve_proxy(cli: str = "") -> str:
    for v in (
        cli,
        os.environ.get("HTTPS_PROXY"), os.environ.get("https_proxy"),
        os.environ.get("HTTP_PROXY"), os.environ.get("http_proxy"),
        os.environ.get("ALL_PROXY"),
        load_proxy_file(),
    ):
        s = (v or "").strip()
        if s:
            return s
    return ""


class HttpResponse:
    def __init__(self, status: int, url: str, headers, body: bytes):
        self.status_code = int(status or 0)
        self.url = url
        self.headers = headers or {}
        self.content = body or b""
        self._text: Optional[str] = None

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = self.content.decode("utf-8", "replace")
        return self._text

    def json(self):
        t = (self.text or "").strip()
        if not t:
            return None
        for ch in ("}", "]"):
            pos = t.rfind(ch)
            if pos >= 0:
                t = t[: pos + 1]
                break
        return json.loads(t or "null")


class HttpSession:
    def __init__(self):
        self.headers: dict = {}
        self.proxies: dict = {}
        self.cookies = CookieJar()
        self._opener = None
        self._opener_key: tuple = ()

    @staticmethod
    def _timeout(timeout) -> float:
        if isinstance(timeout, (tuple, list)):
            nums = [float(x) for x in timeout if x is not None]
            return max(nums) if nums else 15.0
        return float(timeout if timeout is not None else 15.0)

    def get_cookie(self, name: str) -> str:
        for c in self.cookies:
            if c.name == name and c.value:
                return c.value
        return ""

    def _get_opener(self):
        key = tuple(sorted((k, v) for k, v in (self.proxies or {}).items() if v))
        if self._opener is None or key != self._opener_key:
            handlers = []
            if key:
                handlers.append(ProxyHandler(dict(key)))
            handlers.append(HTTPCookieProcessor(self.cookies))
            handlers.append(HTTPSHandler(context=_SSL_CTX))
            handlers.append(HTTPHandler())
            self._opener = build_opener(*handlers)
            self._opener_key = key
        return self._opener

    def _read_bounded(self, resp, sock_timeout: float, max_bytes: int = -1) -> bytes:
        deadline = time.monotonic() + max(sock_timeout * 2.2, 12.0)
        stop_grace: Optional[float] = None
        chunks: list[bytes] = []
        total = 0
        chunk_sz = 65536
        while True:
            now = time.monotonic()
            if STOP.is_set():
                if stop_grace is None:
                    stop_grace = now + 2.0
                elif now > stop_grace:
                    break
            if now > deadline:
                break
            to_read = chunk_sz if max_bytes < 0 else min(chunk_sz, max_bytes - total)
            if to_read <= 0:
                break
            part = resp.read(to_read)
            if not part:
                break
            chunks.append(part)
            total += len(part)
        return b"".join(chunks)

    def request(self, method, url, headers=None, timeout=15, data=None) -> HttpResponse:
        hdrs = dict(self.headers)
        if headers:
            hdrs.update(headers)
        body = None
        if data is not None:
            if isinstance(data, dict):
                body = urlencode(data).encode()
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            elif isinstance(data, bytes):
                body = data
            else:
                body = str(data).encode()
        req = Request(url, data=body, headers=hdrs, method=method.upper())
        t_val = self._timeout(timeout)
        try:
            with self._get_opener().open(req, timeout=t_val) as resp:
                raw = self._read_bounded(resp, t_val)
                status = int(getattr(resp, "status", None)
                             or getattr(resp, "code", None) or 200)
                return HttpResponse(status, str(resp.geturl() or url), dict(resp.headers), raw)
        except HTTPError as e:
            try:
                raw = self._read_bounded(e, 4.0, max_bytes=8192)
            except Exception:
                raw = b""
            return HttpResponse(int(e.code or 0),
                                str(getattr(e, "url", None) or url),
                                dict(e.headers or {}), raw)
        except URLError as e:
            raise RuntimeError(str(e.reason or e)) from e
        except (OSError, ValueError) as e:
            raise RuntimeError(str(e)) from e

    def get(self, url, headers=None, timeout=15) -> HttpResponse:
        return self.request("GET", url, headers=headers, timeout=timeout)

    def post(self, url, data=None, headers=None, timeout=15) -> HttpResponse:
        return self.request("POST", url, headers=headers, timeout=timeout, data=data)


def session() -> HttpSession:
    s = HttpSession()
    s.headers.update({"User-Agent": UA_PC, "Accept": "*/*"})
    s.proxies = {"http": PROXY, "https": PROXY} if PROXY else {}
    return s


# ---------------------------------------------------------------------------
# Tools / helpers
# ---------------------------------------------------------------------------

def find_ffmpeg() -> Optional[str]:
    w = shutil.which("ffmpeg")
    if w:
        return w
    for c in (BASE_DIR / "ffmpeg", BASE_DIR / "bin" / "ffmpeg"):
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def find_ffprobe() -> Optional[str]:
    w = shutil.which("ffprobe")
    if w:
        return w
    ff = find_ffmpeg()
    if ff:
        cand = Path(ff).with_name("ffprobe")
        if cand.is_file():
            return str(cand)
    return None


def fmt_mb(n: int) -> str:
    b = max(0, int(n or 0))
    if 0 < b < 1024 * 100:
        return f"{b / 1024:.1f} KB"
    return f"{b / 1024 / 1024:.2f} MB"


def disk_free(path: Path) -> int:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return int(shutil.disk_usage(str(path)).free)
    except OSError:
        return -1


def disk_low(path: Path) -> bool:
    if MIN_FREE_MB <= 0:
        return False
    free = disk_free(path)
    return 0 <= free < int(MIN_FREE_MB) * 1024 * 1024


def need_link_msg() -> str:
    return "Need: 'cb <username>', 'chaturbate <username>', or https://chaturbate.com/<username>/"


def elapsed_label(iso: str) -> str:
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.astimezone()
        sec = max(0, int((datetime.now(t.tzinfo) - t).total_seconds()))
    except Exception:
        return ""
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def safe_name(s: str) -> str:
    s = unicodedata.normalize("NFC", str(s or "").strip())
    s = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", "_", s)
    return (s[:60] or "room").strip("._") or "room"


def url_for(room_id: str) -> str:
    return f"https://chaturbate.com/{room_id}/"


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------

def job_key(room_id: str) -> str:
    return f"chaturbate_{safe_name(room_id).lower()}"


def job_path(key: str) -> Path:
    return state_dir() / f"{key}.json"


def write_job(key: str, data: dict) -> None:
    p = job_path(key)
    try:
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_job(path: Path) -> Optional[dict]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def patch_job(key: str, **fields) -> None:
    d = read_job(job_path(key)) or {}
    d.update({k: v for k, v in fields.items() if v is not None})
    write_job(key, d)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        stat_p = Path(f"/proc/{pid}/stat")
        if stat_p.exists():
            content = stat_p.read_text(encoding="utf-8", errors="ignore")
            rparen = content.rfind(")")
            if rparen > 0 and len(content) > rparen + 2 and content[rparen + 2] == "Z":
                return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def list_jobs(alive_only: bool = True) -> list[dict]:
    out = []
    for p in sorted(state_dir().glob("chaturbate_*.json")):
        j = read_job(p)
        if not j:
            p.unlink(missing_ok=True)
            continue
        j["_key"] = p.stem.lower()
        alive = pid_alive(int(j.get("pid") or 0))
        j["_alive"] = alive
        if alive_only and not alive:
            p.unlink(missing_ok=True)
            continue
        out.append(j)
    return out


def drop_job(key: str) -> None:
    try:
        job_path(key).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# classify & favorites
# ---------------------------------------------------------------------------

def _cb_user(text: str) -> str:
    t = re.sub(r"^(?:cb|chaturbate)\s*[:/]?\s*", "", (text or "").strip().strip("/"), flags=re.I)
    m = re.search(r"chaturbate\.com/([A-Za-z0-9_-]+)", t, re.I)
    if m:
        name = m.group(1).lower()
        if name not in ("api", "in", "accounts", "tags", "apps", "tipping", "auth", "billing"):
            return name
    if re.fullmatch(r"[A-Za-z0-9_-]{3,40}", t):
        return t.lower()
    return ""


def classify(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Empty target")
    m = re.match(rf"^({_PREFIX_RE})\s+(.+)$", raw, re.I)
    if m:
        rest = m.group(2).strip()
        rid = _cb_user(rest)
        if not rid:
            raise ValueError(f"No Chaturbate username in: {rest}")
        return {"kind": "chaturbate", "url": rest, "room_id": rid, "platform": "chaturbate"}
    if re.search(r"chaturbate\.com", raw, re.I):
        rid = _cb_user(raw)
        if not rid:
            raise ValueError("No username found in the Chaturbate URL")
        return {"kind": "chaturbate", "url": raw, "room_id": rid, "platform": "chaturbate"}
    if _cb_user(raw):
        return {"kind": "chaturbate", "url": raw, "room_id": _cb_user(raw), "platform": "chaturbate"}
    raise ValueError("Not a Chaturbate username or URL. " + need_link_msg())


def _norm_fav_id(rid: str) -> str:
    return _cb_user(rid) or (rid or "").strip().lstrip("@")


def parse_favorites_all(text: str, include_disabled: bool = False) -> list[dict]:
    out: list[dict] = []
    seen = set()
    for raw in (text or "").lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line or (line.startswith("[") and line.endswith("]")):
            continue
        enabled = True
        if line.startswith("#"):
            if not include_disabled:
                continue
            uncommented = line.lstrip("#").strip()
            if not uncommented:
                continue
            if any(uncommented.lower().startswith(w) for w in (
                "favorites", "one room", "skip for", "remove:", "start/",
                "what is", "stop one", "stop all", "you may", "example",
                "interactive menu", "start all", "leading '#'", "chaturbate",
            )):
                continue
            line = uncommented
            enabled = False

        parts = line.split()
        if not parts:
            continue
        token = parts[0]
        nick = " ".join(parts[1:])

        if re.search(r"chaturbate\.com", token, re.I):
            rid = _cb_user(token)
        elif re.match(rf"^({_PREFIX_RE})$", token, re.I) and len(parts) >= 2:
            rid = _cb_user(parts[1])
            nick = " ".join(parts[2:])
        else:
            rid = _cb_user(token)

        if not rid or not _cb_user(rid):
            continue
        key = rid.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"platform": "chaturbate", "room_id": rid, "nick": nick, "enabled": enabled})
    return out


def load_favorites_file(path: Optional[Path] = None, include_disabled: bool = False) -> list[dict]:
    p = path or FAVORITES_FILE
    if not p.exists():
        return []
    try:
        return parse_favorites_all(p.read_text(encoding="utf-8"), include_disabled=include_disabled)
    except OSError as e:
        log(f"Cannot read {p.name}: {e}", level="WARN")
        return []


def save_favorites_file(items: list[dict], path: Optional[Path] = None) -> None:
    p = path or FAVORITES_FILE
    lines = [
        f"# Chaturbate favorites — managed next to {SCRIPT_NAME}",
        "# One username per line. An optional note (streamer name) may follow.",
        "# Leading '#' disables a streamer without deleting it.",
        "",
    ]
    for it in items:
        rid = str(it.get("room_id") or "").strip()
        if not rid:
            continue
        nick = str(it.get("nick") or "").strip()
        en = bool(it.get("enabled", True))
        prefix = "" if en else "# "
        suffix = f"  {nick}" if nick else ""
        lines.append(f"{prefix}{rid}{suffix}")
    lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")


def write_favorites_template(path: Path) -> None:
    save_favorites_file([], path)


def resolve_favorites_arg(targets: list[str]) -> Optional[Path]:
    if len(targets) != 1:
        return None
    raw = str(targets[0]).strip().strip('"').strip("'")
    if not raw or re.match(r"^https?://", raw, re.I):
        return None
    p = Path(raw)
    try:
        if p.is_file() and p.suffix.lower() in (".txt", ".list"):
            return p.resolve()
    except OSError:
        pass
    low = raw.lower()
    name = raw if low.endswith((".txt", ".list")) else raw + ".txt"
    for b in (BASE_DIR, Path.cwd()):
        cand = b / name
        try:
            if cand.is_file() and (low in _FAV_NAMES or low.endswith((".txt", ".list"))):
                return cand.resolve()
        except OSError:
            pass
    if low in _FAV_NAMES:
        return FAVORITES_FILE
    if low.endswith((".txt", ".list")):
        return p.resolve() if p.is_absolute() else BASE_DIR / Path(raw).name
    return None


# ---------------------------------------------------------------------------
# Chaturbate discovery
# ---------------------------------------------------------------------------

def _hls_init_url(playlist: str, base: str) -> str:
    m = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist or "")
    return urljoin(base, m.group(1)) if m else ""


def _cb_hdrs(user: str, csrf_token: str = "", html_mode: bool = False) -> dict:
    hdrs = {
        "User-Agent": UA_PC,
        "Referer": f"https://chaturbate.com/{user}/",
        "Origin": "https://chaturbate.com",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            if html_mode
            else "application/json, text/javascript, */*;q=0.01"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": f"agreeterms=1; csrftoken={csrf_token}" if csrf_token else "agreeterms=1",
    }
    if not html_mode:
        hdrs["X-Requested-With"] = "XMLHttpRequest"
    if csrf_token:
        hdrs["X-CSRFToken"] = csrf_token
    return hdrs


def _extract_initial_room_dossier(html_text: str) -> Optional[dict]:
    if not html_text:
        return None
    patterns = (
        r'window\.initialRoomDossier\s*=\s*"((?:\\.|[^"\\])*)"',
        r"window\.initialRoomDossier\s*=\s*'((?:\\.|[^'\\])*)'",
    )
    for pat in patterns:
        m = re.search(pat, html_text)
        if not m:
            continue
        raw_escaped = m.group(1)
        for decode_fn in (
            lambda s: codecs.decode(s, "unicode_escape"),
            lambda s: s.encode("utf-8").decode("unicode_escape"),
        ):
            try:
                data = json.loads(decode_fn(raw_escaped))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    m_hls = re.search(r'(https://[^\s"\'<>\\]+?\.m3u8[^\s"\'<>\\]*)', html_text)
    if m_hls:
        url_raw = m_hls.group(1).replace(r"\u002D", "-").replace(r"\/", "/")
        return {"hls_source": url_raw, "room_status": "public"}
    return None


def _normalize_cb_mpegts_url(sess: HttpSession, hls_url: str, hdrs: dict) -> str:
    clean_url = re.sub(r"\?.*$", "", hls_url)
    if "playlist_sfm4s.m3u8" in clean_url:
        ts_candidate = hls_url.replace("playlist_sfm4s.m3u8", "playlist.m3u8")
        try:
            r = sess.get(ts_candidate, headers=hdrs, timeout=6)
            if r.status_code < 400 and "#EXTM3U" in (r.text or ""):
                return ts_candidate
        except Exception:
            pass
    return hls_url


def _cb_inspect_master(sess: HttpSession, master_url: str, hdrs: dict) -> tuple[int, int, str, str]:
    try:
        r = sess.get(master_url, headers=hdrs, timeout=8)
        if r.status_code >= 400 or "#EXTM3U" not in (r.text or ""):
            return 0, 0, master_url, ""
        text = r.text or ""
        _segs, _target, _ended, variants, audio_media_map = _hls_parse_ex(text, r.url or master_url)
        if not variants:
            w, h = _hls_res(text)
            return w, h, master_url, ""
        best = _hls_select_best_variant_dict(variants)
        aud_grp = str(best.get("audio_group") or "")
        ext_aud_url = audio_media_map.get(aud_grp, "") if aud_grp else ""
        return (
            int(best.get("width") or 0),
            int(best.get("height") or 0),
            str(best.get("url") or master_url),
            ext_aud_url,
        )
    except Exception:
        return 0, 0, master_url, ""


def _info(platform, room_id, nick, live, play, headers, error,
          ok=True, width=0, height=0, room_status=""):
    return {
        "ok": ok, "live": live, "platform": platform, "room_id": room_id,
        "nick": nick or "", "play_url": play, "variant_url": play or "",
        "audio_url": "", "fmt": "m3u8" if play else "",
        "headers": headers or {}, "error": error,
        "width": int(width or 0), "height": int(height or 0),
        "room_status": room_status or ("LIVE" if live else "OFFLINE"),
    }


def probe_chaturbate(sess: HttpSession, room_id: str, fast_check: bool = False) -> dict:
    user = _cb_user(room_id) or str(room_id or "").strip().lower()
    hdrs = _cb_hdrs(user or "x")
    if not user:
        return _info("chaturbate", room_id, "", False, None, hdrs,
                     "Empty username", ok=False, room_status="invalid")

    js: Optional[dict] = None
    last_err = ""
    t_out = 7 if fast_check else 12

    try:
        r = sess.get(f"{CB_CONTEXT}/{quote(user)}/", headers=hdrs, timeout=t_out)
        if r.status_code < 400:
            try:
                parsed = r.json()
                if isinstance(parsed, dict):
                    js = parsed
            except Exception:
                pass
        else:
            last_err = f"HTTP {r.status_code}"
    except Exception as e:
        last_err = str(e)

    if not isinstance(js, dict) or (not fast_check and not (js.get("hls_source") or js.get("hls_url"))):
        try:
            html_r = sess.get(
                f"https://chaturbate.com/{quote(user)}/",
                headers=_cb_hdrs(user, html_mode=True),
                timeout=t_out,
            )
            if html_r.status_code < 400 and html_r.text:
                dossier = _extract_initial_room_dossier(html_r.text)
                if isinstance(dossier, dict):
                    if js is None:
                        js = dossier
                    else:
                        for k, v in dossier.items():
                            if v and not js.get(k):
                                js[k] = v
        except Exception as e:
            if not last_err:
                last_err = str(e)

    if not isinstance(js, dict):
        msg = f"Err: {last_err}" if last_err else "Not found / blocked"
        return _info("chaturbate", user, user, False, None, hdrs, msg, ok=False, room_status="error")

    nick = str(js.get("broadcaster_username") or js.get("room_slug") or user)
    status = str(js.get("room_status") or js.get("status") or "").lower().strip()
    hls = str(js.get("hls_source") or js.get("hls_url") or js.get("hlsSource") or "").strip()

    if not hls and status in ("", "public") and not fast_check:
        try:
            csrf = sess.get_cookie("csrftoken")
            pr = sess.post(
                "https://chaturbate.com/get_edge_hls_url_ajax/",
                data={"room_slug": user, "bandwidth": "high"},
                headers=_cb_hdrs(user, csrf_token=csrf),
                timeout=10,
            )
            pj = pr.json() if pr.status_code < 400 else {}
            if isinstance(pj, dict):
                hls = str(pj.get("url") or pj.get("hls_source") or "").strip()
                if pj.get("room_status"):
                    status = str(pj.get("room_status")).lower().strip()
        except Exception:
            pass

    if status in ("private", "hidden", "password protected", "password", "away", "group", "ticket"):
        return _info("chaturbate", user, nick, False, None, hdrs,
                     f"Online ({status})", room_status=status.upper())

    if hls.startswith("http") and (not status or status == "public"):
        hls = hls.replace(r"\u002D", "-").replace(r"\/", "/")
        if not fast_check:
            hls = _normalize_cb_mpegts_url(sess, hls, hdrs)
        w, h, best_var, ext_aud = _cb_inspect_master(sess, hls, hdrs)
        info = _info("chaturbate", user, nick, True, hls, hdrs, None,
                     width=w, height=h, room_status="PUBLIC")
        info["variant_url"] = best_var
        info["audio_url"] = ext_aud
        return info

    if status == "public" and not hls:
        return _info("chaturbate", user, nick, False, None, hdrs,
                     "Public (no HLS)", room_status="PUBLIC*")
    return _info("chaturbate", user, nick, False, None, hdrs, "Offline", room_status="OFFLINE")


def check_online_status_batch(room_ids: list[str], force: bool = False,
                              show_progress: bool = True) -> dict[str, dict]:
    now = time.time()
    todo = []
    for rid in room_ids:
        key = rid.lower().strip()
        if not key:
            continue
        cached = ONLINE_STATUS_CACHE.get(key)
        if not force and cached and (now - cached.get("ts", 0)) < ONLINE_CACHE_TTL_SEC:
            continue
        todo.append(key)

    if not todo:
        return ONLINE_STATUS_CACHE

    if show_progress:
        print(f"  Checking live status for {len(todo)} streamer(s)...", end="\r", flush=True)

    def _worker(u: str) -> tuple[str, dict]:
        try:
            info = probe_chaturbate(session(), u, fast_check=True)
            if info.get("live"):
                h = int(info.get("height") or 0)
                res_tag = f" {h}p" if h > 0 else ""
                label = f"ONLINE{res_tag}"
            else:
                st = str(info.get("room_status") or "OFFLINE").upper()
                label = f"ONLINE ({st})" if st in ("PRIVATE", "AWAY", "GROUP", "TICKET", "PASSWORD") else st
            return u, {
                "live": bool(info.get("live")),
                "label": label[:18],
                "nick": str(info.get("nick") or u),
                "width": int(info.get("width") or 0),
                "height": int(info.get("height") or 0),
                "ts": time.time(),
            }
        except Exception:
            return u, {"live": False, "label": "ERROR", "nick": u,
                       "width": 0, "height": 0, "ts": time.time()}

    max_w = min(6, max(1, len(todo)))
    with ThreadPoolExecutor(max_workers=max_w) as pool:
        futs = [pool.submit(_worker, u) for u in todo]
        for fut in as_completed(futs):
            try:
                u, res = fut.result()
                ONLINE_STATUS_CACHE[u] = res
            except Exception:
                pass
    return ONLINE_STATUS_CACHE


# ---------------------------------------------------------------------------
# HLS parser
# ---------------------------------------------------------------------------

def _hls_parse_ex(playlist: str, base: str):
    segs: list[str] = []
    variants: list[dict] = []
    audio_media_map: dict[str, str] = {}
    target = 4.0
    ended = False
    lines = [ln.strip() for ln in (playlist or "").splitlines()]
    i = 0
    pending_variant = False
    pending_bw = pending_w = pending_h = 0
    pending_fps = 0.0
    pending_codecs = ""
    pending_aud_grp = ""
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = max(1.0, float(line.split(":", 1)[1].strip()))
            except ValueError:
                pass
        elif line.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in line.upper():
            m_grp = re.search(r'GROUP-ID="([^"]+)"', line, re.I)
            m_uri = re.search(r'URI="([^"]+)"', line, re.I)
            if m_grp and m_uri:
                audio_media_map[m_grp.group(1)] = urljoin(base, m_uri.group(1))
        elif line.startswith("#EXT-X-STREAM-INF"):
            pending_variant = True
            m_bw = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", line, re.I)
            pending_bw = int(m_bw.group(1)) if m_bw else 0
            m_res = re.search(r"RESOLUTION=(\d+)x(\d+)", line, re.I)
            if m_res:
                pending_w, pending_h = int(m_res.group(1)), int(m_res.group(2))
            else:
                pending_w = pending_h = 0
            m_fps = re.search(r"FRAME-RATE=([\d.]+)", line, re.I)
            pending_fps = float(m_fps.group(1)) if m_fps else 0.0
            m_cdc = re.search(r'CODECS="([^"]+)"', line, re.I)
            pending_codecs = m_cdc.group(1).lower() if m_cdc else ""
            m_ag = re.search(r'AUDIO="([^"]+)"', line, re.I)
            pending_aud_grp = m_ag.group(1) if m_ag else ""
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line.upper():
            raise RuntimeError("Encrypted HLS stream")
        elif line.startswith("#EXTINF"):
            i += 1
            while i < len(lines) and (not lines[i] or lines[i].startswith("#")):
                i += 1
            if i < len(lines) and not lines[i].startswith("#"):
                segs.append(urljoin(base, lines[i]))
        elif pending_variant and line and not line.startswith("#"):
            variants.append({
                "url": urljoin(base, line),
                "bw": pending_bw,
                "width": pending_w,
                "height": pending_h,
                "fps": pending_fps,
                "codecs": pending_codecs,
                "audio_group": pending_aud_grp,
            })
            pending_variant = False
        i += 1
    return segs, target, ended, variants, audio_media_map


def _hls_parse(playlist: str, base: str):
    segs, target, ended, variants, _aud = _hls_parse_ex(playlist, base)
    return segs, target, ended, variants


def _hls_select_best_variant_dict(variants: list[dict]) -> dict:
    if not variants:
        return {}

    def _score(v: dict) -> tuple:
        cdc = str(v.get("codecs") or "").lower()
        has_both_av = 2 if ("avc1" in cdc and ("mp4a" in cdc or "opus" in cdc)) else (1 if not cdc else 0)
        has_audio = 1 if ("mp4a" in cdc or "opus" in cdc or v.get("audio_group") or not cdc) else 0
        return (has_both_av, has_audio, int(v.get("height") or 0),
                int(round(float(v.get("fps") or 0.0))),
                int(v.get("bw") or 0), int(v.get("width") or 0))

    return max(variants, key=_score)


def _hls_is_vod(text: str) -> bool:
    # Chỉ coi là VOD khi playlist TYPE=VOD (ENDLIST đơn thuần chỉ là live kết thúc)
    return bool(re.search(r"#EXT-X-PLAYLIST-TYPE:\s*VOD", text or "", re.I))


def _hls_res(text: str) -> tuple[int, int]:
    pairs = [(int(m.group(1)), int(m.group(2)))
             for m in re.finditer(r"RESOLUTION=(\d+)x(\d+)", text or "", re.I)]
    if not pairs:
        return (0, 0)
    return max(pairs, key=lambda p: p[0] * p[1])


# ---------------------------------------------------------------------------
# Pure-RAM recording loop
# ---------------------------------------------------------------------------

def effective_ram_flush_limit_bytes() -> int:
    if MAX_MB > 0:
        return max(MIN_VALID_STREAM_BYTES, min(RAM_FLUSH_TARGET_BYTES, int(MAX_MB) * 1024 * 1024))
    return RAM_FLUSH_TARGET_BYTES


def _chunk_time_exceeded(t0: float) -> bool:
    return MAX_HOURS > 0 and (time.monotonic() - t0) >= MAX_HOURS * 3600


def record_hls_pure_ram(
    info: dict,
    room_id: str,
    on_progress: Optional[Callable] = None,
    reprobe_fn: Optional[Callable[[], Optional[dict]]] = None,
) -> None:
    master_url = str(info.get("play_url") or "").strip()
    if not master_url:
        raise RuntimeError("No play URL")
    headers = dict(info.get("headers") or {})
    headers.setdefault("Accept", "*/*")
    headers.setdefault("Accept-Encoding", "identity")

    sess = session()
    seen: deque[str] = deque(maxlen=250)
    seen_set: set[str] = set()
    seen_aud: deque[str] = deque(maxlen=250)
    seen_aud_set: set[str] = set()

    playlist_url = str(info.get("variant_url") or master_url)
    audio_playlist_url = str(info.get("audio_url") or "")

    flush_limit = effective_ram_flush_limit_bytes()
    reprobe_count = 0
    seg_fail_streak = 0

    while not STOP.is_set():
        video_buf = RamBuffer()
        audio_buf = RamBuffer()
        start_dt = datetime.now()
        t0 = time.monotonic()
        last_prog = last_check = last_new = t0
        init_bytes: bytes = b""
        init_aud_bytes: bytes = b""
        current_init_hash = ""
        chunk_rotated = False

        preview_name = (
            f"chaturbate_{safe_name(room_id).lower()}_"
            f"{start_dt.strftime('%Y%m%d_%H%M%S')}-....mp4"
        )
        log(f"Pure-RAM Recording started (Target: {fmt_mb(flush_limit)} in RAM -> NVMe)")
        if on_progress:
            try:
                on_progress(0, 0, preview_name)
            except Exception:
                pass

        try:
            while not STOP.is_set():
                now = time.monotonic()
                ram_wrote = len(video_buf) + len(audio_buf)

                if now - last_check >= DISK_CHECK_INTERVAL_SEC:
                    last_check = now
                    if disk_low(recordings_dir()):
                        log("Low NVMe disk space; flushing RAM buffer early", level="WARN")
                        break
                    if _chunk_time_exceeded(t0):
                        log(f"Chunk reached {MAX_HOURS}h cap; flushing")
                        chunk_rotated = True
                        break
                    if ram_wrote >= MIN_VALID_STREAM_BYTES and ram_low_guard():
                        log(f"RAM reserve reached "
                            f"({fmt_mb(system_ram_available_bytes())} free); early flush",
                            level="WARN")
                        chunk_rotated = True
                        break

                r = None
                last_err = ""
                for _attempt in range(3):
                    try:
                        r = sess.get(playlist_url, headers=headers, timeout=10)
                        last_err = ""
                        if r.status_code < 400:
                            break
                        last_err = f"HTTP {r.status_code}"
                    except Exception as e:
                        last_err = str(e)
                        r = None
                    if STOP.wait(1.0):
                        r = None
                        break

                if r is None or r.status_code >= 400 or ("#EXTM3U" not in (r.text or "")):
                    if STOP.is_set():
                        break
                    if playlist_url != master_url:
                        playlist_url = master_url
                        continue
                    if reprobe_fn and reprobe_count < 5:
                        reprobe_count += 1
                        if STOP.wait(2.0):
                            break
                        new_info = reprobe_fn()
                        if new_info and new_info.get("live") and new_info.get("play_url"):
                            master_url = str(new_info["play_url"])
                            playlist_url = str(new_info.get("variant_url") or master_url)
                            audio_playlist_url = str(new_info.get("audio_url") or "")
                            log(f"Refreshed HLS URL (attempt {reprobe_count})", event=False)
                            continue
                    if len(video_buf) > 0:
                        log(f"Stream interrupted ({last_err or 'playlist gone'}); flushing")
                        break
                    raise RuntimeError(f"Playlist fetch failed: {last_err or 'invalid m3u8'}")

                text = r.text or ""

                segs, target, ended, variants, aud_map = _hls_parse_ex(text, r.url or playlist_url)

                if variants and not segs:
                    best_v = _hls_select_best_variant_dict(variants)
                    playlist_url = str(best_v.get("url") or "")
                    ag = str(best_v.get("audio_group") or "")
                    if ag and ag in aud_map:
                        audio_playlist_url = aud_map[ag]
                    continue

                if _hls_is_vod(text) and len(video_buf) == 0:
                    raise RuntimeError("Got a replay (VOD), not a live stream")

                init_url = _hls_init_url(text, r.url or playlist_url)
                if init_url:
                    try:
                        ir = sess.get(init_url, headers=headers, timeout=12)
                        if ir.status_code < 400 and len(ir.content) > 0:
                            new_hash = hashlib.sha256(ir.content).hexdigest()[:16]
                            if not init_bytes:
                                init_bytes = ir.content
                                video_buf.append(init_bytes)
                                current_init_hash = new_hash
                            elif current_init_hash and new_hash != current_init_hash:
                                log("Stream resolution/codec changed; flushing current RAM buffer")
                                chunk_rotated = True
                                break
                    except Exception as e:
                        log(f"Init segment warning: {e}", level="WARN", event=False)

                for seg in segs:
                    if STOP.is_set():
                        break
                    if seg in seen_set:
                        continue
                    try:
                        sr = sess.get(seg, headers=headers, timeout=15)
                    except Exception as e:
                        seg_fail_streak += 1
                        log(f"Segment error ({seg_fail_streak}): {e}",
                            level="WARN", event=False)
                        if seg_fail_streak >= 4:
                            playlist_url = master_url
                            break
                        continue

                    if sr.status_code >= 400 or len(sr.content) < 64:
                        seg_fail_streak += 1
                        if seg_fail_streak >= 4:
                            playlist_url = master_url
                            break
                        continue

                    prev_wrote = len(video_buf) + len(audio_buf)
                    video_buf.append(sr.content)
                    reprobe_count = 0
                    seg_fail_streak = 0
                    if len(seen) == seen.maxlen:
                        seen_set.discard(seen.popleft())
                    seen.append(seg)
                    seen_set.add(seg)
                    last_new = time.monotonic()

                    ram_wrote = len(video_buf) + len(audio_buf)
                    if on_progress and prev_wrote == 0:
                        try:
                            on_progress(ram_wrote, int(time.monotonic() - t0), preview_name)
                        except Exception:
                            pass

                    if ram_wrote >= flush_limit:
                        log(f"RAM Buffer reached {fmt_mb(ram_wrote)} "
                            f"(>= {fmt_mb(flush_limit)}); flushing")
                        chunk_rotated = True
                        break

                if audio_playlist_url and not STOP.is_set() and not chunk_rotated:
                    try:
                        ar = sess.get(audio_playlist_url, headers=headers, timeout=8)
                        if ar.status_code < 400 and "#EXTM3U" in (ar.text or ""):
                            atext = ar.text or ""
                            a_init_url = _hls_init_url(atext, ar.url or audio_playlist_url)
                            if a_init_url and not init_aud_bytes:
                                air = sess.get(a_init_url, headers=headers, timeout=10)
                                if air.status_code < 400 and len(air.content) > 0:
                                    init_aud_bytes = air.content
                                    audio_buf.append(init_aud_bytes)
                            asegs, _, _, _ = _hls_parse(atext, ar.url or audio_playlist_url)
                            for aseg in asegs:
                                if STOP.is_set() or aseg in seen_aud_set:
                                    continue
                                asr = sess.get(aseg, headers=headers, timeout=10)
                                if asr.status_code < 400 and len(asr.content) >= 32:
                                    audio_buf.append(asr.content)
                                    if len(seen_aud) == seen_aud.maxlen:
                                        seen_aud_set.discard(seen_aud.popleft())
                                    seen_aud.append(aseg)
                                    seen_aud_set.add(aseg)
                    except Exception:
                        pass

                ram_wrote = len(video_buf) + len(audio_buf)
                now = time.monotonic()
                if now - last_prog >= 5:
                    log(f"  [Pure RAM] {fmt_mb(ram_wrote)} / {fmt_mb(flush_limit)}  "
                        f"({int(now - t0)}s)", event=False)
                    if on_progress:
                        try:
                            on_progress(ram_wrote, int(now - t0), preview_name)
                        except Exception:
                            pass
                    last_prog = now

                if chunk_rotated or STOP.is_set():
                    break
                if ended or seg_fail_streak >= 4:
                    seg_fail_streak = 0
                    if reprobe_fn and reprobe_count < 4:
                        reprobe_count += 1
                        if STOP.wait(2.0):
                            break
                        new_info = reprobe_fn()
                        if new_info and new_info.get("live") and new_info.get("play_url"):
                            master_url = str(new_info["play_url"])
                            playlist_url = str(new_info.get("variant_url") or master_url)
                            audio_playlist_url = str(new_info.get("audio_url") or "")
                            continue
                    break

                idle = time.monotonic() - last_new
                if ram_wrote > 0 and idle > max(25.0, target * 6):
                    if reprobe_fn and reprobe_count < 4:
                        reprobe_count += 1
                        new_info = reprobe_fn()
                        if new_info and new_info.get("live") and new_info.get("play_url"):
                            master_url = str(new_info["play_url"])
                            playlist_url = str(new_info.get("variant_url") or master_url)
                            audio_playlist_url = str(new_info.get("audio_url") or "")
                            last_new = time.monotonic()
                            continue
                    log("No new segments; flushing RAM buffer to NVMe")
                    break
                if ram_wrote == 0 and idle > 30:
                    raise RuntimeError("HLS produced no segments")
                if STOP.wait(min(max(target * 0.45, 0.7), 2.5)):
                    break
        except KeyboardInterrupt:
            STOP.set()
        finally:
            end_dt = datetime.now()
            try:
                result = flush_ram_buffer_to_nvme_mp4(
                    video_buf, audio_buf, room_id, start_dt, end_dt,
                )
                if result is not None:
                    log(f"Flush OK -> {result.name} ({fmt_mb(result.stat().st_size)})",
                        event=False)
                else:
                    log("Flush completed without file (buffer too small or mux failed)",
                        level="WARN", event=False)
            except Exception as e:
                log(f"Flush wrapper exception: {e}", level="ERROR")

        if chunk_rotated and not STOP.is_set() and not disk_low(recordings_dir()):
            continue
        break


# ---------------------------------------------------------------------------
# Background spawn
# ---------------------------------------------------------------------------

def _on_signal(_sig, _frm) -> None:
    STOP.set()


def spawn_background(targets: list, quiet: bool = False) -> int:
    argv = [sys.executable, "-u", str(Path(__file__).resolve())]
    if PROXY:
        argv += ["--proxy", PROXY]
    if MIN_FREE_MB != 400:
        argv += ["--min-mb", str(int(MIN_FREE_MB))]
    if MAX_MB:
        argv += ["--max-mb", str(int(MAX_MB))]
    if MAX_HOURS > 0:
        argv += ["--max-hours", str(MAX_HOURS)]
    argv += ["rec"] + [str(x) for x in targets]

    log_path = state_dir() / "bg.log"
    _rotate_log_if_needed(log_path)
    try:
        log_f = open(log_path, "a", encoding="utf-8", errors="replace")
    except OSError as e:
        log(f"Cannot write background log {log_path}: {e}", level="ERROR")
        return 2
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log_f,
            stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
        )
    except OSError as e:
        log(f"Cannot start in background: {e}", level="ERROR")
        return 2
    finally:
        log_f.close()

    if quiet:
        log(f"Spawned pid {proc.pid} for {' '.join(str(x) for x in targets)}")
    else:
        log(f"Started background pid {proc.pid} ({' '.join(str(x) for x in targets)})")
    return 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_rec_favorites(path: Path) -> int:
    if not path.exists():
        if path.name.lower() == "favorites.txt":
            write_favorites_template(path)
            log(f"Created {path}. Run 'python3 {SCRIPT_NAME} menu' to add streamers.")
            return 2
        log(f"Not found: {path}", level="ERROR")
        return 2

    items = load_favorites_file(path, include_disabled=False)
    if not items:
        log(f"{path.name} has no active rooms (use menu to add or enable)")
        return 2

    groups: list[list[str]] = []
    rids: list[str] = []
    for it in items:
        rid = str(it.get("room_id") or "").strip()
        if rid and _cb_user(rid):
            groups.append(["cb", rid])
            rids.append(rid)
    if not groups:
        log(f"{path.name} has no valid rooms")
        return 2
    if not find_ffmpeg():
        log("ffmpeg not found. Install it: sudo apt install ffmpeg", level="ERROR")
        return 2

    out_dir = recordings_dir()
    free = disk_free(out_dir)
    if disk_low(out_dir):
        log(f"Low disk space ({fmt_mb(free)}); keep at least {MIN_FREE_MB} MB free",
            level="ERROR")
        return 2

    log(f"Checking online status for {len(rids)} room(s) in {path.name}...")
    status_map = check_online_status_batch(rids, force=True, show_progress=False)
    for rid in rids:
        st = status_map.get(rid.lower(), {})
        log(f"  {rid:<22} : {st.get('label', 'UNKNOWN')}", event=False)

    running_keys = {j.get("_key") for j in list_jobs()}
    rc = 0
    started = skipped = 0
    for i, g in enumerate(groups):
        spec = classify(" ".join(g))
        key = job_key(spec["room_id"])
        if key in running_keys:
            skipped += 1
            continue
        r = spawn_background(g, quiet=True)
        if r:
            rc = r
        else:
            started += 1
            running_keys.add(key)
        if i + 1 < len(groups):
            time.sleep(FAV_STAGGER_SEC)
    log(f"Favorites summary: {started} started "
        f"(Pure-RAM {fmt_mb(effective_ram_flush_limit_bytes())} -> NVMe), "
        f"{skipped} already running")
    return rc if started == 0 and rc else 0


def cmd_rec(target: str, bg: bool = False, targets: Optional[list] = None) -> int:
    raw_targets = [str(x) for x in (targets or []) if str(x).strip()]
    fav = resolve_favorites_arg(raw_targets if raw_targets else [target])
    if fav is not None:
        return cmd_rec_favorites(fav)

    if raw_targets:
        target = " ".join(raw_targets)

    spec = classify(target)
    rid = spec["room_id"]
    key = job_key(rid)
    jobs_now = list_jobs()
    running = [j for j in jobs_now if j.get("_key") == key]
    if running:
        log(f"Already recording pid {running[0].get('pid')} Chaturbate {rid}")
        return 1
    if not find_ffmpeg():
        log("ffmpeg not found. Install it: sudo apt install ffmpeg", level="ERROR")
        return 2
    out_dir = recordings_dir()
    free = disk_free(out_dir)
    if disk_low(out_dir):
        log(f"Low disk space ({fmt_mb(free)}); keep at least {MIN_FREE_MB} MB free",
            level="ERROR")
        return 2
    if bg:
        return spawn_background(["cb", rid])

    set_log_context(rid)
    sess = session()
    pid = os.getpid()

    write_job(key, {
        "pid": pid, "platform": "chaturbate", "room_id": rid, "nick": rid,
        "url": url_for(rid),
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "standby", "live_label": "CHECKING", "size": 0, "file": "",
    })
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    def _safe_reprobe() -> Optional[dict]:
        try:
            return probe_chaturbate(sess, rid)
        except Exception:
            return None

    try:
        while not STOP.is_set():
            try:
                info = probe_chaturbate(sess, rid)
            except Exception as e:
                log(f"Probe error: {e}", level="WARN")
                patch_job(key, status="standby", live_label="ERROR", error=str(e))
                if STOP.wait(STANDBY_SEC):
                    break
                continue

            nick = info.get("nick") or rid
            if not info.get("live"):
                st_label = str(info.get("room_status") or "OFFLINE").upper()
                patch_job(key, nick=nick, status="standby", live_label=st_label,
                          file="", size=0, error=info.get("error") or "")
                log(f"Standby [{st_label}] ({info.get('error') or 'offline'}) — "
                    f"retry in {STANDBY_SEC}s", event=False)
                if STOP.wait(STANDBY_SEC):
                    break
                continue

            h_val = int(info.get("height") or 0)
            live_lbl = f"ONLINE {h_val}p" if h_val > 0 else "ONLINE"
            q = f" {info['width']}x{info['height']}" if info.get("width") and info.get("height") else ""
            log(f"LIVE m3u8{q} (100% Pure-RAM -> NVMe at "
                f"{fmt_mb(effective_ram_flush_limit_bytes())})")
            patch_job(key, nick=nick, status="recording", live_label=live_lbl, size=0, error="")

            def on_prog(ram_sz, _sec, target_name, _key=key, _lbl=live_lbl):
                patch_job(_key, status="recording", live_label=_lbl,
                          size=int(ram_sz), file=target_name)

            try:
                record_hls_pure_ram(info, rid, on_progress=on_prog, reprobe_fn=_safe_reprobe)
            except KeyboardInterrupt:
                STOP.set()
                break
            except Exception as e:
                log(f"Recording interrupted: {e}", level="WARN")
                patch_job(key, error=str(e))
                if STOP.is_set():
                    break

            if STOP.is_set():
                break

            if disk_low(out_dir):
                log(f"Low disk space ({fmt_mb(disk_free(out_dir))}), standing by",
                    level="WARN")
                patch_job(key, status="standby", error="low disk space")
                if STOP.wait(STANDBY_SEC):
                    break
                continue

            if STOP.wait(2.5):
                break
    finally:
        drop_job(key)
        log("Worker stopped cleanly")
    return 0


def _match_job(token: str, jobs: list[dict]) -> list[dict]:
    t = (token or "").strip()
    if not t:
        return jobs
    parts = t.split()
    rid_token = t
    if len(parts) >= 2 and parts[0].lower() in ("cb", "chaturbate"):
        rid_token = " ".join(parts[1:]).strip()
    tl = rid_token.lower().lstrip("@")
    exact, prefix = [], []
    for j in jobs:
        rid = str(j.get("room_id") or "")
        fields = [str(j.get("pid") or ""), rid.lower(),
                  str(j.get("nick") or "").lower(), str(j.get("_key") or "").lower()]
        if tl in fields:
            exact.append(j)
        elif any(f.startswith(tl) for f in fields if f):
            prefix.append(j)
    return exact or prefix


def _stop_recorder_pid(pid: int, force_immediate: bool = False) -> None:
    if pid <= 0 or not pid_alive(pid):
        return
    if not force_immediate:
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            return
        for _ in range(75):
            if not pid_alive(pid):
                return
            time.sleep(0.2)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        for _ in range(6):
            if not pid_alive(pid):
                return
            time.sleep(0.15)


def cmd_stop(token: str, all_jobs: bool = False) -> int:
    jobs = list_jobs()
    if not jobs:
        log("No running recorder jobs")
        return 1
    if all_jobs:
        picked = jobs
        log(f"Stopping {len(picked)} recorder job(s) & flushing RAM cache to NVMe...")
    elif not (token or "").strip():
        if len(jobs) == 1:
            picked = jobs
        else:
            log("Several jobs are running; give an id or --all:")
            for j in jobs:
                log(_job_line(j))
            return 2
    else:
        picked = _match_job(token, jobs)
        if not picked:
            log(f"No match for {token}")
            for j in jobs:
                log(_job_line(j))
            return 1
        if len(picked) > 1:
            log("Several matches, be more specific:")
            for j in picked:
                log(_job_line(j))
            return 2

    for j in picked:
        pid = int(j.get("pid") or 0)
        log(f"Stopping pid {pid} (Chaturbate {j.get('room_id')}) & saving MP4 to NVMe...")
        _stop_recorder_pid(pid, force_immediate=False)
        drop_job(j.get("_key") or "")
    return 0


def cmd_killall() -> int:
    my_pid = os.getpid()
    killed_pids: set[int] = set()

    for j in list_jobs(alive_only=False):
        pid = int(j.get("pid") or 0)
        if pid > 0 and pid != my_pid and pid_alive(pid):
            try:
                os.kill(pid, signal.SIGINT)
            except OSError:
                pass
            killed_pids.add(pid)
        drop_job(j.get("_key") or "")

    if killed_pids:
        for _ in range(50):
            if not any(pid_alive(p) for p in killed_pids):
                break
            time.sleep(0.2)
        for pid in list(killed_pids):
            if pid_alive(pid):
                _stop_recorder_pid(pid, force_immediate=True)

    proc_root = Path("/proc")
    rec_dir_str = str(RECORD_DIR.resolve())
    ram_dir_str = str(RAM_WORK_DIR.resolve())
    script_str = str(Path(__file__).resolve())
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == my_pid or pid in killed_pids:
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ")
            except OSError:
                continue
            if not cmdline:
                continue
            is_our_python = (script_str in cmdline)
            is_our_ffmpeg = ("ffmpeg" in cmdline
                             and (rec_dir_str in cmdline or ram_dir_str in cmdline))
            if is_our_python or is_our_ffmpeg:
                _stop_recorder_pid(pid, force_immediate=True)
                killed_pids.add(pid)

    purge_trash_files()
    log(f"Terminated {len(killed_pids)} background process(es) & flushed RAM cache to NVMe.")
    return 0


def _job_line(j: dict) -> str:
    nick = j.get("nick") or ""
    rid = j.get("room_id") or ""
    pid = int(j.get("pid") or 0)
    fname = str(j.get("file") or "")
    buf_sz = int(j.get("size") or 0)
    rss_sz = process_rss_bytes(pid)
    bits = [f"pid {pid}", (j.get("status") or "?").upper(), "Chaturbate", str(rid)]
    if nick and str(nick) != str(rid):
        bits.append(str(nick))
    bits.append(f"RAM-Buf:{fmt_mb(buf_sz)} (Proc-RSS:{fmt_mb(rss_sz)})"
                if (buf_sz > 0 or rss_sz > 0) else "-")
    age = elapsed_label(str(j.get("started") or ""))
    if age:
        bits.append(age)
    if fname:
        bits.append(f"-> {fname}")
    return "  " + "  ".join(str(x) for x in bits if x)


def cmd_list() -> int:
    jobs = list_jobs()
    if not jobs:
        log("No running jobs")
        return 0
    log(f"{len(jobs)} recorder job(s) running (100% Pure-RAM mode):")
    for j in jobs:
        log(_job_line(j))
    return 0


def cmd_probe(target: str) -> int:
    spec = classify(target)
    rid = spec["room_id"]
    log(f"Probing Chaturbate {rid}...", event=False)
    try:
        info = probe_chaturbate(session(), rid)
    except Exception as e:
        log(f"Probe error: {e}", level="ERROR", event=False)
        return 2
    log(f"Name   {info.get('nick') or rid}", event=False)
    if info.get("live"):
        q = f"  {info['width']}x{info['height']}" if info.get("width") and info.get("height") else ""
        log(f"Status ONLINE (LIVE)  {info.get('fmt') or ''}{q}", event=False)
        if info.get("play_url"):
            log(f"URL    {str(info['play_url'])[:140]}", event=False)
        return 0
    log(f"Status {info.get('room_status') or 'OFFLINE'} ({info.get('error') or ''})",
        event=False)
    return 1


def cmd_files() -> int:
    purge_trash_files()
    d = recordings_dir()
    free = disk_free(d)
    ram_avail = system_ram_available_bytes()
    log(f"NVMe Save folder: {d}"
        + (f" (NVMe free: {fmt_mb(free)}, RAM avail: {fmt_mb(ram_avail)})"
           if free >= 0 else ""),
        event=False)
    files: list[Path] = []
    try:
        for p in d.iterdir():
            if p.is_file() and p.suffix.lower() in (".mp4", ".ts") and p.stat().st_size > 0:
                files.append(p)
    except OSError as e:
        log(f"Cannot read folder: {e}", level="ERROR", event=False)
        return 2
    if not files:
        log("No saved recordings on NVMe yet", event=False)
        return 0
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    total_bytes = 0
    for n, p in enumerate(files):
        sz = p.stat().st_size
        total_bytes += sz
        if n < 40:
            log(f"  {fmt_mb(sz):>10}  {p.name}", event=False)
    if len(files) > 40:
        log(f"  ... {len(files) - 40} more", event=False)
    log(f"Total on NVMe: {len(files)} file(s) ({fmt_mb(total_bytes)})", event=False)
    return 0


# ---------------------------------------------------------------------------
# Interactive menu
# ---------------------------------------------------------------------------

def _parse_indices(arg_str: str, max_idx: int) -> list[int]:
    out: list[int] = []
    for tok in re.split(r"[\s,]+", (arg_str or "").strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            for k in range(min(a, b), max(a, b) + 1):
                if 1 <= k <= max_idx and k not in out:
                    out.append(k)
        elif tok.isdigit():
            k = int(tok)
            if 1 <= k <= max_idx and k not in out:
                out.append(k)
    return out


def _build_menu_rows(fav_path: Path, check_online: bool = False) -> tuple[list[dict], list[dict]]:
    if not fav_path.exists():
        write_favorites_template(fav_path)
    fav_items = load_favorites_file(fav_path, include_disabled=True)
    jobs = list_jobs(alive_only=True)
    jobs_by_key = {j.get("_key"): j for j in jobs}

    all_rids = [str(it.get("room_id") or "") for it in fav_items]
    for j in jobs:
        r = str(j.get("room_id") or "")
        if r and r not in all_rids:
            all_rids.append(r)

    if check_online and all_rids:
        check_online_status_batch(all_rids, force=True, show_progress=True)
    elif all_rids:
        missing = [r for r in all_rids if r.lower() not in ONLINE_STATUS_CACHE]
        if missing:
            check_online_status_batch(missing, force=False, show_progress=True)

    rows: list[dict] = []
    seen_keys: set[str] = set()

    for idx, it in enumerate(fav_items):
        rid = str(it.get("room_id") or "")
        key = job_key(rid)
        seen_keys.add(key)
        j = jobs_by_key.get(key)
        c_info = ONLINE_STATUS_CACHE.get(rid.lower(), {})
        rows.append({
            "in_fav": True, "fav_idx": idx,
            "enabled": bool(it.get("enabled", True)),
            "room_id": rid,
            "nick": str(it.get("nick") or (j.get("nick") if j else "") or c_info.get("nick") or ""),
            "job": j,
            "online_label": str((j.get("live_label") if j and j.get("live_label") else "")
                                or c_info.get("label") or "UNKNOWN"),
            "key": key,
        })

    for j in jobs:
        key = j.get("_key") or ""
        if key in seen_keys:
            continue
        rid = str(j.get("room_id") or "")
        c_info = ONLINE_STATUS_CACHE.get(rid.lower(), {})
        rows.append({
            "in_fav": False, "fav_idx": -1, "enabled": False, "room_id": rid,
            "nick": str(j.get("nick") or c_info.get("nick") or ""),
            "job": j,
            "online_label": str(j.get("live_label") or c_info.get("label") or "UNKNOWN"),
            "key": key,
        })
    return rows, fav_items


def _clear_screen() -> None:
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def _render_menu_dashboard(rows: list[dict], fav_path: Path, status_msg: str = "") -> None:
    _clear_screen()
    free_disk = disk_free(recordings_dir())
    free_disk_str = fmt_mb(free_disk) if free_disk >= 0 else "?"
    free_ram = system_ram_available_bytes()
    free_ram_str = fmt_mb(free_ram) if free_ram >= 0 else "?"
    running_cnt = sum(1 for r in rows if r.get("job"))
    live_rec_cnt = sum(1 for r in rows if (r.get("job") or {}).get("status") == "recording")

    print("=" * 108)
    print(f"  RECMD CHATURBATE v7.2 (100% Pure-RAM -> NVMe) | NVMe Free: {free_disk_str} "
          f"| RAM Avail: {free_ram_str} | Workers: {running_cnt} (REC: {live_rec_cnt})")
    print(f"  RAM State/Mux: {SHM_BASE} (0 NVMe writes until "
          f"{fmt_mb(effective_ram_flush_limit_bytes())}) | Output: {RECORD_DIR.name}/")
    print("=" * 108)
    print(f" {'#':>2}  {'FAV':<4}  {'USERNAME':<18}  {'LIVE STATUS':<16}  "
          f"{'WORKER / PID':<22}  {'RAM BUF / PROC RSS':>19}  {'NOTE / SESSION'}")
    print("-" * 108)

    if not rows:
        print("   (No streamers in favorites.txt and no running jobs. Press 'a' to add a streamer!)")
    else:
        for i, r in enumerate(rows, 1):
            fav_tag = "ON" if (r["in_fav"] and r["enabled"]) else ("OFF#" if r["in_fav"] else "TEMP")
            rid = str(r["room_id"])[:18]
            live_lbl = str(r.get("online_label") or "UNKNOWN")[:16]
            j = r.get("job")
            if j:
                st = str(j.get("status") or "running").upper()
                pid = int(j.get("pid") or 0)
                age = elapsed_label(str(j.get("started") or ""))
                proc_str = f"{st} (p:{pid}{',' + age if age else ''})"[:22]
                buf_sz = int(j.get("size") or 0)
                rss_sz = process_rss_bytes(pid)
                sz_str = f"{fmt_mb(buf_sz)} / {fmt_mb(rss_sz)}"
                fname = str(j.get("file") or j.get("error") or r.get("nick") or "")[:24]
            else:
                proc_str = "STOPPED"
                sz_str = "-"
                fname = str(r.get("nick") or "")[:24]
            print(f" {i:>2}  {fav_tag:<4}  {rid:<18}  {live_lbl:<16}  "
                  f"{proc_str:<22}  {sz_str:>19}  {fname}")

    recent_events = tail_file(events_log_path(), n=5)
    print("-" * 108)
    print(" Recent Events (RAM Log):")
    if not recent_events:
        print("   (No recent events)")
    else:
        for ev in recent_events:
            print(f"   {ev[:102]}")

    print("-" * 108)
    print(" Commands:")
    print("  s <#> / s all : Start recording (100% Pure RAM) |  x <#> / x all : Stop & flush RAM -> NVMe MP4")
    print("  o             : Check ONLINE status for all     |  k             : Kill all bg workers (stay in menu)")
    print("  z             : EXIT ALL (Stop all + Quit app)  |  q             : Quit menu only (bg keeps running)")
    print("  a [username]  : Add streamer to favorites.txt   |  t <#> / d <#> : Toggle ON/OFF# / Delete streamer")
    print("  l <#>         : View room log                   |  f / r [Enter] : List NVMe MP4s / Refresh table")
    print("=" * 108)
    if status_msg:
        print(f" >> {status_msg}")
        print("-" * 108)


def cmd_menu(fav_path: Optional[Path] = None) -> int:
    fpath = fav_path or FAVORITES_FILE
    status_msg = "Checked online status on startup."
    first_load = True

    while True:
        rows, fav_items = _build_menu_rows(fpath, check_online=first_load)
        first_load = False
        _render_menu_dashboard(rows, fpath, status_msg=status_msg)
        status_msg = ""
        try:
            raw_in = input("Action > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting menu (background jobs continue running. "
                  "Use 'z' or 'exitall' to stop everything).")
            return 0

        if not raw_in or raw_in.lower() in ("r", "refresh"):
            continue
        if raw_in.lower() in ("q", "quit"):
            print("Exiting menu (background jobs continue running).")
            return 0
        if raw_in.lower() in ("z", "exitall", "shutdown"):
            print("Stopping all background processes, flushing RAM buffers to NVMe MP4, and exiting...")
            cmd_killall()
            print("All processes terminated cleanly. Goodbye!")
            return 0

        parts = raw_in.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("o", "online", "check"):
            all_rids = [r["room_id"] for r in rows]
            check_online_status_batch(all_rids, force=True, show_progress=True)
            online_cnt = sum(1 for r in all_rids if ONLINE_STATUS_CACHE.get(r.lower(), {}).get("live"))
            status_msg = f"Refreshed online status: {online_cnt}/{len(all_rids)} streamer(s) ONLINE."

        elif cmd in ("s", "start"):
            if not arg:
                status_msg = "Specify row number(s) or 'all', e.g.: s 1 2  or  s all"
                continue
            if arg.lower() == "all":
                picked = [r for r in rows if (r["in_fav"] and r["enabled"])]
            else:
                idxs = _parse_indices(arg, len(rows))
                picked = [rows[i - 1] for i in idxs]
            if not picked:
                status_msg = "No matching rows to start."
                continue
            started_n = skipped_n = 0
            for r in picked:
                if r.get("job"):
                    skipped_n += 1
                    continue
                rc = spawn_background(["cb", r["room_id"]], quiet=True)
                if rc == 0:
                    started_n += 1
                time.sleep(0.2)
            status_msg = (f"Started {started_n} job(s) in Pure-RAM mode"
                          + (f" ({skipped_n} already running)" if skipped_n else ""))

        elif cmd in ("x", "stop"):
            if not arg:
                status_msg = "Specify row number(s) or 'all', e.g.: x 1  or  x all"
                continue
            if arg.lower() == "all":
                cmd_stop("", all_jobs=True)
                status_msg = "Stopped all running recorders & saved MP4 files to NVMe."
                continue
            idxs = _parse_indices(arg, len(rows))
            picked = [rows[i - 1] for i in idxs if rows[i - 1].get("job")]
            if not picked:
                status_msg = "No running processes matched."
                continue
            for r in picked:
                j = r["job"]
                pid = int(j.get("pid") or 0)
                print(f"  Stopping PID {pid} ({r['room_id']}) & flushing RAM to NVMe MP4...")
                _stop_recorder_pid(pid, force_immediate=False)
                drop_job(r["key"])
            status_msg = f"Stopped {len(picked)} process(es) & saved MP4 to NVMe."

        elif cmd in ("k", "killall"):
            cmd_killall()
            status_msg = "Killed all background processes & flushed RAM buffers to NVMe."

        elif cmd in ("l", "log", "logs"):
            print("-" * 80)
            if not arg:
                lines = tail_file(events_log_path(), n=30)
                print(" === Global Events Log (last 30 lines) ===")
                print("\n".join(lines) if lines else " (Empty)")
            else:
                idxs = _parse_indices(arg, len(rows))
                if not idxs:
                    status_msg = "Specify a valid row number, e.g.: l 1"
                    continue
                for idx in idxs:
                    r = rows[idx - 1]
                    rpath = room_log_path(r["room_id"])
                    lines = tail_file(rpath, n=30)
                    print(f" === Room Log: {r['room_id']} ({rpath.name}) ===")
                    print("\n".join(lines) if lines else " (No log entries yet)")
            try:
                input("\nPress Enter to return to menu...")
            except (EOFError, KeyboardInterrupt):
                pass

        elif cmd in ("a", "add"):
            if not arg:
                try:
                    arg = input("  Enter CB username (or URL): ").strip()
                except (EOFError, KeyboardInterrupt):
                    continue
            if not arg:
                continue
            note = ""
            if " #" in arg:
                arg, note = arg.split(" #", 1)
                arg, note = arg.strip(), note.strip()
            try:
                spec = classify(arg)
            except ValueError as e:
                status_msg = f"Cannot add: {e}"
                continue
            rid = _norm_fav_id(str(spec["room_id"]))
            dup = next((it for it in fav_items if it["room_id"].lower() == rid.lower()), None)
            if dup:
                dup["enabled"] = True
                save_favorites_file(fav_items, fpath)
                status_msg = f"{rid} is already in favorites (ensured ON)."
            else:
                fav_items.append({"platform": "chaturbate", "room_id": rid,
                                  "nick": note, "enabled": True})
                save_favorites_file(fav_items, fpath)
                status_msg = f"Added {rid} to {fpath.name}."
            check_online_status_batch([rid], force=True, show_progress=True)
            st_lbl = ONLINE_STATUS_CACHE.get(rid.lower(), {}).get("label", "UNKNOWN")
            try:
                ans = input(f"  [{rid} is {st_lbl}] Start recording in background now? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans in ("", "y", "yes"):
                spawn_background(["cb", rid], quiet=True)
                time.sleep(0.25)
                status_msg += " Started in background!"

        elif cmd in ("t", "toggle"):
            idxs = _parse_indices(arg, len(rows))
            if not idxs:
                status_msg = "Specify row number(s) to toggle, e.g.: t 1 3"
                continue
            toggled = 0
            for idx in idxs:
                r = rows[idx - 1]
                if r["in_fav"] and 0 <= r["fav_idx"] < len(fav_items):
                    fav_items[r["fav_idx"]]["enabled"] = not fav_items[r["fav_idx"]]["enabled"]
                    toggled += 1
            if toggled:
                save_favorites_file(fav_items, fpath)
                status_msg = f"Toggled ON/OFF# for {toggled} streamer(s)."
            else:
                status_msg = "Selected row is not in favorites.txt."

        elif cmd in ("d", "del", "delete", "rm"):
            idxs = _parse_indices(arg, len(rows))
            if not idxs:
                status_msg = "Specify row number(s) to delete, e.g.: d 2"
                continue
            to_remove: set[int] = set()
            for idx in idxs:
                r = rows[idx - 1]
                if r["in_fav"]:
                    to_remove.add(r["fav_idx"])
                if r.get("job"):
                    try:
                        ans = input(f"  Row #{idx} ({r['room_id']}) is running. "
                                    f"Stop & flush it too? [Y/n]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        ans = "n"
                    if ans in ("", "y", "yes"):
                        j = r["job"]
                        _stop_recorder_pid(int(j.get("pid") or 0), force_immediate=False)
                        drop_job(r["key"])
            if to_remove:
                new_favs = [it for i, it in enumerate(fav_items) if i not in to_remove]
                save_favorites_file(new_favs, fpath)
                status_msg = f"Removed {len(to_remove)} entry(s)."
            else:
                status_msg = "Nothing deleted from favorites.txt."

        elif cmd in ("p", "probe"):
            idxs = _parse_indices(arg, len(rows))
            if not idxs:
                status_msg = "Specify row number(s) to probe, e.g.: p 1"
                continue
            for idx in idxs:
                r = rows[idx - 1]
                print("-" * 60)
                cmd_probe(f"cb {r['room_id']}")
            try:
                input("\nPress Enter to return to menu...")
            except (EOFError, KeyboardInterrupt):
                pass

        elif cmd in ("f", "files"):
            print("-" * 60)
            cmd_files()
            try:
                input("\nPress Enter to return to menu...")
            except (EOFError, KeyboardInterrupt):
                pass

        else:
            status_msg = f"Unknown command '{cmd}'. Use s, x, o, k, z, l, a, t, d, p, f, r, or q."


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description="Chaturbate live-stream recorder v7.2 (100% Pure-RAM to Sequential NVMe).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--proxy", default="", help="HTTP proxy, e.g. http://127.0.0.1:7890")
    p.add_argument("--bg", action="store_true", help="run in background")
    p.add_argument("--all", action="store_true", help="stop: stop everything")
    p.add_argument("--min-mb", type=int, default=400,
                   help="refuse to record below this free disk space (default 400)")
    p.add_argument("--max-mb", type=int, default=0,
                   help="override RAM flush threshold in MiB (0 = default 980 MB)")
    p.add_argument("--max-hours", type=float, default=0.0,
                   help="chunk duration cap in hours (0 = no cap)")
    p.add_argument(
        "cmd", nargs="?",
        choices=("menu", "rec", "stop", "killall", "exitall", "list",
                 "probe", "files", "logs"),
        help="menu (default) / rec / stop / killall / exitall / list / probe / files / logs",
    )
    p.add_argument("target", nargs="*",
                   help="'cb <username>', URL, favorites.txt, or file name")
    return p


def main(argv: Optional[list] = None) -> int:
    global PROXY, MIN_FREE_MB, MAX_MB, MAX_HOURS
    init_ram_workspace()
    p = build_parser()
    args = p.parse_args(argv)
    PROXY = resolve_proxy(args.proxy)
    MIN_FREE_MB = max(0, int(args.min_mb or 0))
    MAX_MB = max(0, int(args.max_mb or 0))
    try:
        MAX_HOURS = float(args.max_hours)
    except (TypeError, ValueError):
        MAX_HOURS = 0.0
    if PROXY:
        os.environ.setdefault("HTTP_PROXY", PROXY)
        os.environ.setdefault("HTTPS_PROXY", PROXY)

    purge_trash_files()

    if not args.cmd or args.cmd == "menu":
        fav_custom = resolve_favorites_arg(args.target) if args.target else None
        return cmd_menu(fav_custom)

    blob = " ".join(args.target).strip()
    try:
        if args.cmd in ("killall", "exitall"):
            return cmd_killall()
        if args.cmd == "list":
            return cmd_list()
        if args.cmd == "files":
            return cmd_files()
        if args.cmd == "logs":
            if blob:
                spec = classify(blob)
                lines = tail_file(room_log_path(spec["room_id"]), n=40)
            else:
                lines = tail_file(events_log_path(), n=40)
            print("\n".join(lines) if lines else "No logs yet.")
            return 0
        if args.cmd == "probe":
            if not blob:
                log(need_link_msg(), level="ERROR", event=False)
                return 2
            return cmd_probe(blob)
        if args.cmd == "rec":
            if not blob:
                log(need_link_msg(), level="ERROR", event=False)
                return 2
            return cmd_rec(blob, bg=args.bg, targets=args.target)
        return cmd_stop(blob, all_jobs=args.all)
    except ValueError as e:
        log(str(e), level="ERROR", event=False)
        return 2
    except RuntimeError as e:
        log(str(e), level="ERROR", event=False)
        return 2
    except KeyboardInterrupt:
        log("Stopped", event=False)
        return 0


if __name__ == "__main__":
    sys.exit(main())
