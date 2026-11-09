#!/usr/bin/env python3
"""Unified Entry Point CLI & Daemon for Termux Telegram Recorder.

Usage:
    python rec2_app.py bot              # Run full 24/7 daemon with Bot, Auto-Record, Upload & Retention
    python rec2_app.py status           # Check system health, storage, and favorites in terminal
    python rec2_app.py rec <room_id>    # Standalone single-room recording CLI
"""

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
from core.recorder import StreamInfo, probe_room, record_stream_to_disk
from core.task_manager import JobStatus, TaskManager
from storage.cleaner import RetentionCleaner
from storage.history import HistoryStore

logger = logging.getLogger("rec2_app")


def build_parser() -> argparse.ArgumentParser:
    """Build command-line interface arguments parser."""
    parser = argparse.ArgumentParser(
        description="Chaturbate Termux Telegram Recorder & Manager"
    )
    parser.add_argument(
        "--settings",
        default="settings.txt",
        help="Path to settings.txt configuration file (default: settings.txt)",
    )
    parser.add_argument(
        "--favorites",
        default="favorites.txt",
        help="Path to favorites.txt list (default: favorites.txt)",
    )
    parser.add_argument(
        "--recordings-dir",
        default=None,
        help="Directory to save MP4 recordings (default: recordings/)",
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

    return parser


def run_status_cmd(cfg: Config, recordings_dir: Path, fav_path: Path, db_path: Path) -> int:
    """Print terminal status overview."""
    disk_free = get_disk_free(recordings_dir)
    favs = load_favorites(fav_path) if fav_path.exists() else []
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

    print(f"🔍 Probing streamer '{room}'...")
    info = probe_room(room, proxy=cfg.proxy)
    if not info.is_live or not info.hls_url:
        print(f"ℹ️ Streamer '{room}' is currently OFFLINE or private.")
        return 0

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_mp4 = recordings_dir / f"chaturbate_{room}_{now_str}.mp4"
    stop_event = asyncio.Event() if False else None

    import threading
    th_stop = threading.Event()

    def handle_sig(sig, frame):
        print("\nStopping recording gracefully...")
        th_stop.set()

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    print(f"🔴 Stream is LIVE! Starting recording to {out_mp4} (Press Ctrl+C to stop)...")
    res = record_stream_to_disk(
        info=info,
        out_mp4=out_mp4,
        stop_event=th_stop,
        proxy=cfg.proxy,
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


async def _run_bot_daemon(cfg: Config, recordings_dir: Path, fav_path: Path, db_path: Path) -> None:
    """Run full async daemon with Telegram Bot, TaskManager, UploaderQueue, and RetentionCleaner."""
    from bot.telegram_bot import init_telegram_bot
    from bot.uploader import UploadItem, UploadQueue

    logger.info("Starting Termux Telegram Recorder daemon...")
    acquire_wake_lock()

    store = HistoryStore(db_path)
    cleaner = RetentionCleaner(recordings_dir=recordings_dir)
    upload_queue = UploadQueue()
    task_manager = TaskManager(
        max_concurrent=cfg.max_concurrent,
        recordings_dir=recordings_dir,
        proxy=cfg.proxy,
    )

    # Wire recording finalization to upload queue
    def on_file_ready(output_mp4: Path, room_id: str, duration_sec: float):
        logger.info("File ready: %s (%s, %.1fs)", output_mp4, room_id, duration_sec)
        item = UploadItem(
            file_path=output_mp4,
            room_id=room_id,
            duration_sec=int(duration_sec),
        )
        upload_queue.enqueue(item)

    task_manager.set_on_file_ready_callback(on_file_ready)

    # Load initial favorites into TaskManager
    if fav_path.exists():
        for fav in load_favorites(fav_path):
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

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

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
            await client.start(bot_token=cfg.bot_token)
            logger.info("Telegram Bot connected successfully.")
            await upload_queue.start_worker(client, cfg.chat_id, store)
        except Exception as e:
            logger.error("Failed to connect Telegram Bot: %s", e)

    # Main operational loop
    poll_timer = 0
    clean_timer = 0
    try:
        while not stop_event.is_set():
            # 1. Staggered probe poll every 30 seconds
            if poll_timer <= 0:
                try:
                    await asyncio.to_thread(task_manager.poll_staggered_batch, 0.3)
                except Exception as e:
                    logger.warning("Polling batch error: %s", e)
                poll_timer = 30

            # 2. 48-hour retention cleaner every 30 minutes (1800s)
            if clean_timer <= 0:
                try:
                    cleaner.run_cleanup_cycle(
                        store,
                        retention_hours=cfg.retention_hours,
                        min_free_mb=cfg.min_free_mb,
                    )
                except Exception as e:
                    logger.warning("Retention cleaner error: %s", e)
                clean_timer = 1800

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                poll_timer -= 1
                clean_timer -= 1
    finally:
        logger.info("Cleaning up daemon resources...")
        task_manager.stop_all(wait=True, timeout=3.0)
        await upload_queue.stop_worker()
        if client and hasattr(client, "disconnect"):
            try:
                await client.disconnect()
            except Exception:
                pass
        release_wake_lock()
        logger.info("Daemon stopped gracefully.")


def main(argv: Optional[list[str]] = None) -> int:
    """Main entrypoint routing."""
    parser = build_parser()
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings_path = Path(args.settings)
    cfg = load_config(settings_path) if settings_path.exists() else Config()

    recordings_dir = (
        Path(args.recordings_dir)
        if args.recordings_dir
        else get_recordings_dir()
    )
    fav_path = Path(args.favorites)
    db_path = Path(args.db)

    if args.cmd == "status":
        return run_status_cmd(cfg, recordings_dir, fav_path, db_path)

    if args.cmd == "rec":
        return run_rec_cmd(cfg, recordings_dir, args.target)

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

