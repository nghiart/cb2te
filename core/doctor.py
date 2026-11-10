"""On-device diagnostics ("doctor") for the Termux recorder.

Answers the question *"why is nothing being saved?"* link by link:

1. environment / ffmpeg / ffprobe
2. recordings + spool directories (writable, enough free space)
3. settings (Telegram, proxy, concurrency)
4. Chaturbate reachability: probe -> playlist -> variant -> init -> segment

Every check is PASS / WARN / FAIL with an actionable hint.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.android import get_disk_free, get_safe_tmp_dir
from core.config import Config, load_favorites
from core.muxer import find_ffmpeg, find_ffprobe
from core.net import ProxyConfigError, normalize_proxy
from core.recorder import probe_hls, probe_room

logger = logging.getLogger(__name__)

__all__ = ["Check", "run_doctor", "format_report", "PASS", "WARN", "FAIL"]

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_ICONS = {PASS: "✅", WARN: "⚠️", FAIL: "❌"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


def _is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix or "com.termux" in str(Path(sys.executable))


def _writable_dir(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".doctor_{os.getpid()}.tmp"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _tool_version(binary: str) -> str:
    try:
        proc = subprocess.run(
            [binary, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
        first = (proc.stdout or "").strip().splitlines()
        return first[0][:120] if first else ""
    except Exception as exc:  # pragma: no cover - device specific
        return f"version check failed: {exc}"


def _mb(value: int) -> int:
    return int(value / (1024 * 1024)) if value and value > 0 else 0


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_environment() -> Check:
    detail = (
        f"Python {platform.python_version()} | {platform.system()} "
        f"{platform.release()} | termux={_is_termux()}"
    )
    return Check("Môi trường Python", PASS, detail)


def _check_ffmpeg() -> Check:
    binary = find_ffmpeg()
    if not binary:
        return Check(
            "FFmpeg",
            FAIL,
            "không tìm thấy ffmpeg trong PATH",
            "pkg install ffmpeg  (bắt buộc: không có ffmpeg thì file .ts không "
            "đóng gói được thành .mp4)",
        )
    version = _tool_version(binary)
    return Check("FFmpeg", PASS, f"{binary} — {version}")


def _check_ffprobe() -> Check:
    binary = find_ffprobe()
    if not binary:
        return Check(
            "FFprobe",
            WARN,
            "không tìm thấy ffprobe",
            "pkg install ffmpeg (ffprobe dùng để kiểm tra video sau khi ghi)",
        )
    return Check("FFprobe", PASS, f"{binary} — {_tool_version(binary)}")


def _check_recordings_dir(recordings_dir: Path, min_free_mb: int) -> Check:
    ok, err = _writable_dir(recordings_dir)
    if not ok:
        return Check(
            "Thư mục recordings",
            FAIL,
            f"{recordings_dir} không ghi được: {err}",
            "termux-setup-storage, rồi export RECORDINGS_DIR=... trỏ tới thư mục "
            "ghi được (vd /data/data/com.termux/files/home/recordings)",
        )
    free_mb = _mb(get_disk_free(recordings_dir))
    status = PASS if free_mb >= max(min_free_mb, 512) else FAIL
    hint = "" if status == PASS else "Dọn bớt file cũ hoặc tăng dung lượng trống"
    return Check(
        "Thư mục recordings",
        status,
        f"{recordings_dir} — trống {free_mb} MB (ngưỡng {min_free_mb} MB)",
        hint,
    )


def _check_spool_dir() -> Check:
    spool = get_safe_tmp_dir()
    ok, err = _writable_dir(spool)
    if not ok:
        return Check(
            "Thư mục tạm (spool)",
            FAIL,
            f"{spool} không ghi được: {err}",
            "export TMPDIR=$HOME/tmp (Android dọn /tmp bất cứ lúc nào)",
        )
    free_mb = _mb(get_disk_free(spool))
    status = PASS if free_mb >= 1024 else WARN
    hint = "" if status == PASS else "Spool cần chỗ trống cho 1 phiên ghi (khuyến nghị > 1 GB)"
    return Check("Thư mục tạm (spool)", status, f"{spool} — trống {free_mb} MB", hint)


def _check_settings(cfg: Config, settings_path: Path) -> list[Check]:
    checks: list[Check] = []
    if not settings_path.exists():
        checks.append(Check(
            "settings.txt",
            FAIL,
            f"không thấy {settings_path}",
            "cp settings.example.txt settings.txt && nano settings.txt",
        ))
    else:
        missing = []
        if not cfg.api_id or not cfg.api_hash:
            missing.append("API_ID/API_HASH")
        if not cfg.bot_token:
            missing.append("BOT_TOKEN")
        if not cfg.chat_id:
            missing.append("CHAT_ID")
        if not cfg.admin_ids:
            missing.append("ADMIN_IDS")
        checks.append(
            Check(
                "settings.txt",
                PASS if not missing else FAIL,
                "đã nạp" if not missing else f"thiếu: {', '.join(missing)}",
                "" if not missing else "Điền đủ thông tin Telegram trong settings.txt",
            )
        )

    try:
        info = normalize_proxy(cfg.proxy)
    except ProxyConfigError as exc:
        checks.append(Check("Proxy", FAIL, str(exc), "Sửa PROXY trong settings.txt"))
        return checks

    if info is None:
        checks.append(Check(
            "Proxy",
            WARN,
            "không cấu hình (kết nối trực tiếp)",
            "Nếu Chaturbate trả 403 hoặc 'public nhưng không có hls_source' "
            "(geo-block), hãy đặt PROXY = http://user:pass@host:port hoặc "
            "socks5://host:port",
        ))
        return checks

    detail = f"{info.scheme}://{info.host}:{info.port}"
    if info.scheme.startswith("socks"):
        try:
            import socks  # noqa: F401
            from sockshandler import SocksiPyHandler  # noqa: F401
        except ImportError:
            checks.append(Check(
                "Proxy",
                FAIL,
                f"{detail} cần PySocks nhưng chưa cài",
                "pip install pysocks",
            ))
            return checks
    checks.append(Check("Proxy", PASS, detail))
    return checks


def _check_termux_helpers() -> Check:
    import shutil

    has_wake = shutil.which("termux-wake-lock") is not None
    if has_wake:
        return Check("Termux helpers", PASS, "termux-wake-lock có sẵn")
    if _is_termux():
        return Check(
            "Termux helpers",
            WARN,
            "thiếu termux-wake-lock",
            "pkg install termux-api (và cài app Termux:API), nếu không Android sẽ "
            "giết tiến trình khi tắt màn hình",
        )
    return Check("Termux helpers", WARN, "không phải Termux — bỏ qua")


def _check_telethon() -> Check:
    try:
        import telethon  # noqa: F401

        return Check("Telethon", PASS, getattr(telethon, "__version__", "installed"))
    except ImportError:
        return Check(
            "Telethon",
            WARN,
            "chưa cài telethon",
            "pip install telethon (chỉ cần cho chế độ bot/upload)",
        )


def _check_history(db_path: Path) -> Check:
    from storage.history import HistoryStore

    try:
        store = HistoryStore(db_path)
        count = len(store)
        return Check("history.json", PASS, f"{db_path} — {count} bản ghi")
    except Exception as exc:  # pragma: no cover - corrupt db
        return Check(
            "history.json",
            WARN,
            f"không đọc được: {exc}",
            "Xoá history.json (hệ thống sẽ tạo lại) hoặc sửa JSON lỗi",
        )


def _check_rooms(rooms: list[str], proxy: str) -> list[Check]:
    checks: list[Check] = []
    if not rooms:
        checks.append(Check(
            "Chaturbate",
            WARN,
            "không có streamer nào để kiểm tra",
            "Thêm username vào favorites.txt để doctor kiểm tra end-to-end",
        ))
        return checks

    for room in rooms:
        info = probe_room(room, proxy=proxy)
        if not info.is_live:
            hint = ""
            status = WARN
            if info.room_status in ("geo_blocked", "error"):
                status = FAIL
                hint = (
                    "Thêm PROXY (settings.txt) hoặc kiểm tra mạng. "
                    "Xem log chi tiết bằng: python3 rec2_app.py -v rec " + room
                )
            checks.append(Check(
                f"Probe {room}",
                status,
                f"status={info.room_status or 'offline'} — {info.error or 'offline'}",
                hint,
            ))
            continue

        hls = probe_hls(info.hls_url, room_id=room, proxy=proxy)
        if not hls["ok"]:
            checks.append(Check(
                f"HLS {room}",
                FAIL,
                f"playlist lỗi: {hls['error']} (HTTP {hls['status']})",
                "Playlist trả 403/404 thường do token hết hạn hoặc IP bị chặn — "
                "thử PROXY; nếu là geo-block hãy dùng proxy ở quốc gia được phép",
            ))
            continue

        flavour = "fMP4/CMAF (#EXT-X-MAP)" if hls["fmp4"] else "MPEG-TS"
        detail = (
            f"{hls['segment_count']} segment | {flavour} | "
            f"variant {len(hls['variants'])} | segment mẫu {hls['first_segment_bytes']} B"
        )
        if hls["first_segment_bytes"] <= 0:
            status, hint = FAIL, "Segment tải về 0 byte — CDN chặn Referer/IP"
        else:
            status, hint = PASS, ""
        checks.append(Check(f"Live {room}", status, detail, hint))

    return checks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_doctor(
    cfg: Config,
    settings_path: Path,
    recordings_dir: Path,
    favorites_path: Path,
    *,
    probe_limit: int = 1,
    extra_rooms: Optional[list[str]] = None,
) -> list[Check]:
    """Run every diagnostic check and return the list of results."""
    checks: list[Check] = [
        _check_environment(),
        _check_ffmpeg(),
        _check_ffprobe(),
        _check_recordings_dir(recordings_dir, cfg.min_free_mb),
        _check_spool_dir(),
    ]
    checks.extend(_check_settings(cfg, settings_path))
    checks.append(_check_termux_helpers())
    checks.append(_check_telethon())
    checks.append(_check_history(Path("history.json")))

    rooms: list[str] = []
    for room in (extra_rooms or []):
        if room and room not in rooms:
            rooms.append(room)
    if favorites_path.exists():
        for fav in load_favorites(favorites_path):
            rid = fav.get("room_id")
            if rid and rid not in rooms:
                rooms.append(rid)
    if probe_limit > 0:
        rooms = rooms[:probe_limit]
    else:
        rooms = []

    if rooms:
        checks.extend(_check_rooms(rooms, cfg.proxy))
    else:
        checks.extend(_check_rooms([], cfg.proxy))

    return checks


def format_report(checks: list[Check], title: str = "CHẨN ĐOÁN HỆ THỐNG CB2TE") -> str:
    """Render checks as a human readable report."""
    lines = ["", "=" * 60, f" 🩺 {title}", "=" * 60]
    for check in checks:
        icon = _ICONS.get(check.status, "•")
        lines.append(f"{icon} [{check.status}] {check.name}")
        if check.detail:
            lines.append(f"      {check.detail}")
        if check.hint:
            lines.append(f"      ↳ {check.hint}")
    failures = sum(1 for c in checks if c.status == FAIL)
    warnings = sum(1 for c in checks if c.status == WARN)
    lines.append("-" * 60)
    lines.append(f"Kết quả: {len(checks) - failures - warnings} PASS, "
                 f"{warnings} WARN, {failures} FAIL")
    if failures:
        lines.append("⚠️  Có lỗi chặn ghi hình — xử lý các mục ❌ ở trên trước.")
    elif warnings:
        lines.append("Hệ thống có thể ghi hình, nhưng nên xử lý các cảnh báo ⚠️.")
    else:
        lines.append("✅ Tất cả kiểm tra đều đạt — pipeline ghi hình sẵn sàng.")
    lines.append("=" * 60 + "\n")
    return "\n".join(lines)
