#!/usr/bin/env python3
"""Unified Entry Point CLI & Daemon for Termux Telegram Recorder (rec2_app / 1st.py)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.android import (
    acquire_wake_lock,
    get_disk_free,
    get_recordings_dir,
    release_wake_lock,
)
from core.config import (
    Config,
    load_config,
    load_favorites,
    normalize_room_id,
)
from core.recorder import (
    MIN_VALID_STREAM_BYTES,
    StreamInfo,
    probe_room,
    record_stream_to_disk,
)
from core.task_manager import JobStatus, TaskManager
from storage.cleaner import RetentionCleaner
from storage.history import HistoryStore

logger = logging.getLogger("1st_app")


def build_parser() -> argparse.ArgumentParser:
    """Build command-line interface arguments parser."""
    parser = argparse.ArgumentParser(
        description="Chaturbate Termux Telegram Recorder & Manager (1st.py)"
    )
    parser.add_argument(
        "--settings",
        default="settings.json",
        help="Path to settings.json configuration file (default: settings.json, fallback: settings.txt)",
    )
    parser.add_argument(
        "--favorites",
        default="favorites.json",
        help="Path to favorites.json list (default: favorites.json, fallback: favorites.txt)",
    )
    parser.add_argument(
        "--recordings-dir",
        default=None,
        help="Directory to save MP4 recordings (default: ~/storage/downloads/CB2TE or recordings/)",
    )
    parser.add_argument(
        "--db",
        default="history.json",
        help="Path to history database JSON file (default: history.json)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging output",
    )

    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # Subcommand: bot
    subparsers.add_parser("bot", help="Run full 24/7 daemon with Telegram Bot & Uploader")

    # Subcommand: status
    subparsers.add_parser("status", help="Print local storage and favorites status")

    # Subcommand: rec <room_id>
    rec_parser = subparsers.add_parser("rec", help="Record a single room directly from CLI")
    rec_parser.add_argument(
        "target",
        nargs="+",
        help="Room username or URL to record",
    )

    # Subcommand: doctor
    doctor_parser = subparsers.add_parser(
        "doctor", help="Diagnose why streams are not being saved (ffmpeg/probe/HLS/disk)"
    )
    doctor_parser.add_argument(
        "--probe",
        type=int,
        default=1,
        help="Number of favorites to probe end-to-end (default: 1, 0 = no network)",
    )
    doctor_parser.add_argument(
        "rooms",
        nargs="*",
        help="Extra room usernames to probe (optional)",
    )

    # Subcommand: remux
    remux_parser = subparsers.add_parser(
        "remux", help="Remux leftover .ts/.m4s spools into .mp4 (after fixing ffmpeg)"
    )
    remux_parser.add_argument("paths", nargs="*", help="Files or directories (default: recordings dir)")

    return parser


def run_status_cmd(cfg: Config, recordings_dir: Path, fav_path: Path, db_path: Path) -> int:
    """Print terminal status overview."""
    _gdf = sys.modules.get("rec2_app", sys.modules[__name__]).__dict__.get("get_disk_free", get_disk_free)
    disk_free = _gdf(recordings_dir)
    favs = load_favorites(fav_path) if (fav_path.exists() or fav_path.with_suffix(".txt").exists()) else []
    store = HistoryStore(db_path)
    records = store.list_records(active_only=True)

    print("\n" + "=" * 50)
    print(" 📹 CHATURBATE RECORDER - SYSTEM STATUS")
    print("=" * 50)
    print(f"📁 Recordings Directory : {recordings_dir.resolve()}")
    print(f"💾 Free Disk Space      : {int(disk_free)} MB")
    print(f"⭐ Favorites Tracked    : {len(favs)} rooms")
    for f in favs[:10]:
        print(f"   • {f.get('room_id')}")
    if len(favs) > 10:
        print(f"   ... and {len(favs) - 10} more")

    print(f"\n📦 History Records (48h): {len(records)} files uploaded")
    for r in records[-5:]:
        size_mb = r.size / (1024 * 1024)
        print(f"   • {Path(r.file_path).name} ({size_mb:.1f} MB) -> #{r.room_id}")

    print("=" * 50 + "\n")
    return 0


def run_rec_cmd(cfg: Config, recordings_dir: Path, target_rooms: list[str]) -> int:
    """Run single-room recording from command-line."""
    recordings_dir.mkdir(parents=True, exist_ok=True)
    room = normalize_room_id(target_rooms[0])
    if not room:
        print(f"Error: Invalid room identifier '{target_rooms[0]}'")
        return 1

    # Check caller module dynamically (rec2_app vs 1st) for mocks
    mod = sys.modules.get("rec2_app", sys.modules[__name__])
    _probe = getattr(mod, "probe_room", probe_room)
    _record = getattr(mod, "record_stream_to_disk", record_stream_to_disk)

    print(f"🔍 Probing streamer '{room}'...")
    info = _probe(room, proxy=cfg.proxy)
    if not info.is_live or not info.hls_url:
        detail = info.error or "offline"
        print(f"ℹ️ Streamer '{room}' không ghi được — status={info.room_status or 'offline'} ({detail})")
        if info.room_status in ("geo_blocked", "error"):
            print("   ↳ Chạy 'python3 1st.py doctor' và kiểm tra PROXY trong settings.json")
        return 0

    res_str = f" [{info.resolution_label}]" if info.resolution_label else ""
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_mp4 = recordings_dir / f"chaturbate_{room}_{now_str}.mp4"

    import threading
    th_stop = threading.Event()

    def handle_sig(sig, frame):
        print("\nStopping recording gracefully...")
        th_stop.set()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    print(f"🔴 Stream is LIVE{res_str}! Starting recording to {out_mp4} (Press Ctrl+C to stop)...")
    res = _record(
        info=info,
        out_mp4=out_mp4,
        stop_event=th_stop,
        proxy=cfg.proxy,
        reprobe_fn=lambda: _probe(room, proxy=cfg.proxy),
        min_valid_bytes=MIN_VALID_STREAM_BYTES,
        on_progress=lambda b, el: sys.stdout.write(
            f"\rRecorded: {b / (1024 * 1024):.1f} MB | Elapsed: {int(el)}s"
        ),
    )
    print()
    if res and res.exists() and res.stat().st_size > 0:
        print(f"✅ Recording finished successfully: {res} ({res.stat().st_size / (1024 * 1024):.1f} MB)")
        return 0
    else:
        print("⚠️ No video data saved.")
        return 1


def run_doctor_cmd(
    cfg: Config,
    settings_path: Path,
    recordings_dir: Path,
    fav_path: Path,
    probe_limit: int,
    rooms: list[str],
    db_path: Optional[Path] = None,
) -> int:
    """Run the full diagnostic suite."""
    from core.doctor import FAIL, format_report, run_doctor

    checks = run_doctor(
        cfg,
        settings_path,
        recordings_dir,
        fav_path,
        probe_limit=probe_limit,
        extra_rooms=rooms,
        db_path=db_path,
    )
    print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def run_remux_cmd(recordings_dir: Path, targets: list[str]) -> int:
    """Remux leftover raw spools (.ts / .m4s) into .mp4."""
    from core.muxer import mux_stream_copy

    roots = [Path(t) for t in targets] if targets else [recordings_dir]
    spools: list[Path] = []
    for root in roots:
        if root.is_dir():
            spools.extend(sorted(root.glob("*.ts")))
            spools.extend(sorted(root.glob("*.m4s")))
        elif root.is_file():
            spools.append(root)

    if not spools:
        print(f"Không tìm thấy file .ts/.m4s nào trong {[str(r) for r in roots]}")
        return 0

    failures = 0
    for spool in spools:
        out = spool.with_suffix(".mp4")
        if out.exists() and out.stat().st_size > 0:
            print(f"⏭  Bỏ qua (đã có MP4): {out.name}")
            continue
        is_fmp4 = spool.suffix.lower() == ".m4s"
        ok, err = mux_stream_copy(
            spool,
            out,
            input_format="mov" if is_fmp4 else "mpegts",
        )
        if ok:
            size_mb = out.stat().st_size / (1024 * 1024)
            print(f"✅ {spool.name} -> {out.name} ({size_mb:.1f} MB)")
            try:
                spool.unlink()
            except OSError:
                pass
        else:
            failures += 1
            print(f"❌ {spool.name}: {err}")
    return 1 if failures else 0


async def _run_bot_daemon(cfg: Config, recordings_dir: Path, fav_path: Path, db_path: Path) -> None:
    """Run full async daemon with Telegram Bot, TaskManager, UploaderQueue, and RetentionCleaner."""
    from bot.telegram_bot import init_telegram_bot
    from bot.uploader import UploadItem, UploadQueue
    from bot.upload_store import UploadStore
    from bot.postprocessor import Postprocessor, enqueue_remux
    from bot.monitor import ProcessWatchdog

    logger.info("Starting Termux Telegram Recorder daemon (1st.py)...")
    acquire_wake_lock()

    store = HistoryStore(db_path)
    cleaner = RetentionCleaner(recordings_dir=recordings_dir)
    upload_store = UploadStore(db_path=recordings_dir / "upload_queue.db")
    upload_queue = UploadQueue(upload_store=upload_store)
    watchdog = ProcessWatchdog()

    postprocessor = Postprocessor(
        on_upload_ready=lambda mp4, room, dur: upload_queue.enqueue(
            UploadItem(file_path=mp4, room_id=room, duration_sec=dur)
        )
    )

    task_manager = TaskManager(
        max_concurrent=cfg.max_concurrent,
        recordings_dir=recordings_dir,
        proxy=cfg.proxy,
        max_duration_sec=getattr(cfg, "session_max_duration_sec", 7200),
        use_ffmpeg_recorder=getattr(cfg, "use_ffmpeg_recorder", True),
        max_recording_height=getattr(cfg, "max_recording_height", 1080),
        min_free_mb=getattr(cfg, "min_free_mb", 400),
    )

    # Wire recording finalization to upload queue or postprocessor
    def on_file_ready(output_file: Path, room_id: str, duration_sec: float):
        logger.info("File ready: %s (%s, %.1fs)", output_file, room_id, duration_sec)
        if output_file.suffix.lower() == ".ts":
            enqueue_remux(output_file, room_id, int(duration_sec))
        else:
            item = UploadItem(
                file_path=output_file,
                room_id=room_id,
                duration_sec=int(duration_sec),
            )
            upload_queue.enqueue(item)

    task_manager.set_on_file_ready_callback(on_file_ready)

    # Load initial favorites into TaskManager
    fav_items = load_favorites(fav_path)
    for fav in fav_items:
        rid = fav.get("room_id")
        if rid:
            task_manager.start_tracking(rid)

    # Init Telegram Bot client
    client = init_telegram_bot(
        config=cfg,
        task_manager=task_manager,
        upload_queue=upload_queue,
        history_store=store,
        favorites_path=fav_path,
    )

    loop = asyncio.get_running_loop()

    async def _send_safe_message(target: int | str, text: str) -> None:
        try:
            # Enforce that text notifications are ONLY sent to positive user IDs (private admin chat).
            # Never send text messages to groups/channels (negative IDs or cfg.chat_id).
            try:
                t_int = int(target)
                if t_int <= 0 or (cfg.chat_id and t_int == int(cfg.chat_id)):
                    logger.warning("Blocked attempt to send text notification to non-admin target %s", target)
                    return
            except (ValueError, TypeError):
                logger.warning("Blocked attempt to send text notification to invalid target %s", target)
                return

            await client.send_message(target, text)
            logger.info("Sent Telegram notification to %s", target)
        except Exception as exc:
            logger.warning("Failed to send Telegram message to %s: %s", target, exc)

    def dispatch_tg_notification(text: str) -> None:
        if not client:
            return
        # Status notifications and bot controls are strictly routed to admin IDs via private chat.
        # The Telegram group (cfg.chat_id) only receives uploaded media files, NEVER text status notifications.
        targets = []
        for a in cfg.admin_ids:
            if not a:
                continue
            try:
                tid = int(a)
                if tid > 0 and (not cfg.chat_id or tid != int(cfg.chat_id)):
                    targets.append(tid)
            except (ValueError, TypeError):
                continue

        if not targets:
            logger.debug("No valid admin_ids configured to receive private status notifications")
            return

        for target in targets:
            try:
                asyncio.run_coroutine_threadsafe(_send_safe_message(target, text), loop)
            except Exception as e:
                logger.warning("Failed to schedule telegram message to %s: %s", target, e)

    def on_session_started(room_id: str, info: StreamInfo, out_path: Path):
        res = f" [{info.resolution_label}]" if info.resolution_label else ""
        now_str = datetime.now().strftime("%H:%M:%S")
        msg = (
            f"🔴 **BẮT ĐẦU GHI HÌNH: `#{room_id}`**{res}\n"
            f"⏱️ Bắt đầu lúc: `{now_str}`\n"
            f"📁 File: `{out_path.name}`"
        )
        dispatch_tg_notification(msg)

    task_manager.set_on_session_started_callback(on_session_started)

    def on_session_finished(room_id: str, duration_sec: float, bytes_recorded: int, result_path: Optional[Path]):
        m, s = divmod(int(duration_sec), 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}h{m:02d}m{s:02d}s" if h > 0 else f"{m}m{s:02d}s"
        size_mb = bytes_recorded / (1024 * 1024)
        now_str = datetime.now().strftime("%H:%M:%S")

        if result_path and result_path.exists() and result_path.stat().st_size > 0:
            msg = (
                f"⏹️ **KẾT THÚC PHIÊN LIVE: `#{room_id}`**\n"
                f"⏱️ Thời lượng: `{dur_str}` | Dung lượng: `{size_mb:.1f} MB`\n"
                f"🕒 Kết thúc lúc: `{now_str}`\n"
                f"📤 Video đang được đưa vào hàng đợi tải lên nhóm..."
            )
        else:
            msg = (
                f"⚠️ **KẾT THÚC PHIÊN LIVE: `#{room_id}` (Không có file)**\n"
                f"⏱️ Thời lượng: `{dur_str}` | Dung lượng: `{size_mb:.1f} MB`\n"
                f"🕒 Kết thúc lúc: `{now_str}`"
            )
        dispatch_tg_notification(msg)

    task_manager.set_on_session_finished_callback(on_session_finished)

    def on_batch_status_change(
        all_statuses: dict[str, tuple[str, StreamInfo]],
        changes: list[tuple[str, str, str, StreamInfo]],
    ):
        if not cfg.notify_on_status_change or not client:
            return

        now_str = datetime.now().strftime("%H:%M:%S")

        header_changes = []
        for room, _old_st, new_st, info in changes:
            st_lower = new_st.lower()
            if "online" in st_lower:
                res = f" [{info.resolution_label}]" if info.resolution_label else ""
                header_changes.append(f"🔔 `#{room}` vừa online{res}")
            elif "private" in st_lower:
                header_changes.append(f"🔔 `#{room}` vừa private")
            else:
                header_changes.append(f"🔔 `#{room}` vừa offline")

        lines = [
            "\n".join(header_changes),
            f"⏱️ Thời gian: `{now_str}`",
            f"📋 Trạng thái tất cả các phòng ({len(all_statuses)}):",
        ]

        active_jobs = {j.room_id for j in task_manager.get_active_jobs()}

        def _sort_key(item):
            r, (lbl, _) = item
            if "online" in lbl.lower():
                return (0, r)
            if "private" in lbl.lower():
                return (1, r)
            return (2, r)

        for room, (status_label, info) in sorted(all_statuses.items(), key=_sort_key):
            st_lower = status_label.lower()
            rec_tag = " *(Đang ghi)*" if room in active_jobs else ""
            if "online" in st_lower:
                res = f" [{info.resolution_label}]" if info.resolution_label else ""
                icon = "🟢"
                label = f"Online{res}"
            elif "private" in st_lower:
                icon = "🔒"
                label = "Private"
            else:
                icon = "⚪"
                label = "Offline"
            lines.append(f"• `#{room}`: {icon} {label}{rec_tag}")

        msg = "\n".join(lines)
        dispatch_tg_notification(msg)

    task_manager.set_on_batch_status_change_callback(on_batch_status_change)

    stop_event = asyncio.Event()

    def _shutdown_handler():
        logger.info("Shutdown signal received. Stopping daemon...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown_handler)
        except (NotImplementedError, RuntimeError):
            pass

    # Start bot and uploader worker
    if client:
        try:
            await upload_store.init()
            await upload_queue.recover_pending()
            await postprocessor.start()
            await watchdog.start()
            await client.start(bot_token=cfg.bot_token)
            logger.info("Telegram Bot connected successfully.")
            await upload_queue.start_worker(client, cfg.chat_id, store)
        except Exception as e:
            logger.error("Failed to connect Telegram Bot: %s", e)

    # Main operational loop: check all rooms every monitor_interval_sec (default 60s)
    clean_timer = 0
    cycle_interval = max(10, cfg.monitor_interval_sec)

    try:
        while not stop_event.is_set():
            # Dynamic backpressure governance
            backlog_sec, speed_mbps = upload_queue.estimate_backlog()
            if backlog_sec > 900:
                logger.warning("High upload backlog (%.0fs > 900s). Lowering recording ceiling to 720p.", backlog_sec)
                task_manager.max_recording_height = 720
            elif backlog_sec < 300:
                task_manager.max_recording_height = getattr(cfg, "max_recording_height", 1080)

            # 1. 60-second periodic status check for all rooms & live recording dispatch
            try:
                logger.info("Running room status inspection check (cycle %ds, backlog %.0fs)...", cycle_interval, backlog_sec)
                # Pause new recordings dispatch if backlog exceeds 1800s
                dispatch = backlog_sec <= 1800
                if not dispatch:
                    logger.warning("Severe upload backlog (%.0fs > 1800s). Pausing recording dispatch.", backlog_sec)
                await asyncio.to_thread(task_manager.check_all_room_statuses, dispatch, 0.3)
            except Exception as e:
                logger.warning("Room status check error: %s", e)

            # 2. 48-hour retention cleaner every 30 minutes (1800s)
            if clean_timer <= 0:
                try:
                    store.sync_with_disk(task_manager.recordings_dir)
                    cleaner.run_cleanup_cycle(
                        store,
                        retention_hours=cfg.retention_hours,
                        min_free_mb=cfg.min_free_mb,
                    )
                except Exception as e:
                    logger.warning("Retention cleaner error: %s", e)
                clean_timer = 1800

            # Wait for next cycle or shutdown signal
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=float(cycle_interval))
            except asyncio.TimeoutError:
                clean_timer -= cycle_interval
    finally:
        logger.info("Cleaning up daemon resources...")
        task_manager.stop_all(wait=True, timeout=120.0)
        await postprocessor.stop()
        await watchdog.stop()
        await upload_queue.stop_worker()
        await upload_store.close()
        if client and hasattr(client, "disconnect"):
            try:
                await client.disconnect()
            except Exception:
                pass
        release_wake_lock()
        logger.info("Daemon stopped gracefully.")


def resolve_config_path(arg_path: str | Path, default_name: str) -> Path:
    """Resolve configuration or data file paths canonically.

    If relative, prefers cwd if existing, otherwise script directory to prevent
    fragmented configs when daemon is invoked from arbitrary working directories.
    """
    p = Path(arg_path) if arg_path else Path(default_name)
    if p.is_absolute():
        return p
    cwd_path = Path.cwd() / p
    if cwd_path.exists():
        return cwd_path.resolve()
    script_dir = Path(__file__).resolve().parent
    script_path = script_dir / p
    if script_path.exists():
        return script_path.resolve()
    return script_path.resolve()


def main(argv: Optional[list[str]] = None) -> int:
    """Main entrypoint routing."""
    parser = build_parser()
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings_path = resolve_config_path(args.settings, "settings.json")
    cfg = load_config(settings_path)

    recordings_dir = (
        Path(args.recordings_dir).resolve()
        if args.recordings_dir
        else get_recordings_dir()
    )
    fav_path = resolve_config_path(args.favorites, "favorites.json")
    db_path = resolve_config_path(args.db, "history.json")

    if args.cmd == "status":
        return run_status_cmd(cfg, recordings_dir, fav_path, db_path)

    if args.cmd == "rec":
        return run_rec_cmd(cfg, recordings_dir, args.target)

    if args.cmd == "doctor":
        return run_doctor_cmd(
            cfg,
            settings_path,
            recordings_dir,
            fav_path,
            probe_limit=args.probe,
            rooms=list(args.rooms or []),
            db_path=db_path,
        )

    if args.cmd == "remux":
        return run_remux_cmd(recordings_dir, list(args.paths or []))

    if args.cmd == "bot":
        try:
            asyncio.run(_run_bot_daemon(cfg, recordings_dir, fav_path, db_path))
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            logger.exception("Fatal daemon error: %s", e)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
