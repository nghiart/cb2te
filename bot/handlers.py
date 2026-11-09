"""Telegram Bot Command Handlers and Formatting Utilities."""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional

from core.android import get_disk_free
from core.config import (
    Config,
    add_favorite,
    load_favorites,
    normalize_room_id,
    remove_favorite,
)
from core.task_manager import JobStatus, TaskManager
from storage.history import HistoryRecord, HistoryStore

logger = logging.getLogger(__name__)

__all__ = [
    "is_authorized",
    "format_status_message",
    "format_help_message",
    "format_files_message",
    "register_handlers",
]


def is_authorized(user_id: Optional[int], admin_ids: list[int]) -> bool:
    """Check if the sender is an authorized administrator."""
    if user_id is None or not admin_ids:
        return False
    return int(user_id) in admin_ids


def format_status_message(
    active_jobs: list[dict[str, Any]],
    queue_status: dict[str, Any],
    free_disk_mb: int | float,
    retention_files_count: int,
) -> str:
    """Format operational status overview for Telegram."""
    lines = [
        "📊 **HỆ THỐNG GHI HÌNH CHATURBATE - TERMUX**",
        "──────────────────────────────",
    ]

    # Active jobs
    lines.append(f"🔴 **Đang ghi hình ({len(active_jobs)}):**")
    if active_jobs:
        for job in active_jobs:
            room = job.get("room_id", "unknown")
            st = job.get("status", "RECORDING")
            size = job.get("size_mb", 0)
            lines.append(f"  • `{room}` - {st} ({size} MB)")
    else:
        lines.append("  *(Không có phòng nào đang ghi)*")

    # Upload queue
    pending = queue_status.get("pending_count", 0)
    current = queue_status.get("current_upload")
    lines.append(f"\n📤 **Hàng đợi Upload:** {pending} file chờ")
    if current:
        cur_file = current.get("file", "")
        cur_pct = current.get("progress_percent", 0)
        lines.append(f"  ⏳ Đang tải: `{cur_file}` ({cur_pct}%)")

    # Disk & Retention
    lines.append(f"\n💾 **Dung lượng trống:** `{int(free_disk_mb)} MB`")
    lines.append(f"📁 **File lưu trữ 48h:** `{retention_files_count} video`")

    return "\n".join(lines)


def format_help_message() -> str:
    """Return command usage manual."""
    return (
        "🤖 **DANH SÁCH LỆNH ĐIỀU KHIỂN BOT**\n"
        "──────────────────────────────\n"
        "• `/status` - Xem trạng thái hệ thống, RAM, ổ đĩa & upload\n"
        "• `/rec <user>` - Bắt đầu theo dõi và ghi hình một streamer\n"
        "• `/stop <user|all>` - Dừng ghi hình một hoặc tất cả streamer\n"
        "• `/fav` - Xem danh sách streamer trong `favorites.txt`\n"
        "• `/add <user>` - Thêm streamer vào danh sách theo dõi\n"
        "• `/del <user>` - Xóa streamer khỏi danh sách theo dõi\n"
        "• `/files` - Xem danh sách video đã ghi trong 48h qua\n"
        "• `/help` - Hiển thị hướng dẫn này"
    )


def format_files_message(records: list[HistoryRecord], retention_hours: int = 48) -> str:
    """Format the list of managed video files and expiration countdown."""
    if not records:
        return "📁 **Không có video nào trong vòng lưu trữ 48h.**"

    now = time.time()
    lines = [
        f"📁 **DANH SÁCH VIDEO LƯU TRỮ ({len(records)} file):**",
        "──────────────────────────────",
    ]

    for r in records:
        size_mb = r.size / (1024 * 1024)
        elapsed_sec = max(0.0, now - r.uploaded_at)
        rem_sec = max(0.0, (retention_hours * 3600) - elapsed_sec)
        rem_hours = int(rem_sec // 3600)
        rem_min = int((rem_sec % 3600) // 60)

        file_name = Path(r.file_path).name
        lines.append(
            f"• `{file_name}` ({size_mb:.1f} MB)\n"
            f"  Streamer: #{r.room_id} | Còn lại: {rem_hours}h{rem_min:02d}m"
        )

    return "\n".join(lines)


def register_handlers(
    client: Any,
    config: Config,
    task_manager: TaskManager,
    upload_queue: Any,
    history_store: HistoryStore,
    favorites_path: Optional[Path | str] = None,
) -> dict[str, Callable]:
    """Register all bot command handlers and return dispatch mapping."""
    fav_file = Path(favorites_path) if favorites_path else Path("favorites.txt")

    async def handle_start(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return
        await event.reply(format_help_message())

    async def handle_help(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return
        await event.reply(format_help_message())

    async def handle_status(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return

        active_jobs = [
            {
                "room_id": j.room_id,
                "status": j.status.value,
                "size_mb": round(j.bytes_recorded / (1024 * 1024), 1),
            }
            for j in task_manager.get_active_jobs()
        ]
        q_status = upload_queue.get_queue_status()
        disk_free = get_disk_free(task_manager.recordings_dir)
        records = history_store.list_records(active_only=True)

        text = format_status_message(
            active_jobs=active_jobs,
            queue_status=q_status,
            free_disk_mb=disk_free,
            retention_files_count=len(records),
        )
        await event.reply(text)

    async def handle_rec(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return

        parts = (event.raw_text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            await event.reply("⚠️ Sử dụng: `/rec <username hoặc link>`")
            return

        room = normalize_room_id(parts[1])
        if not room:
            await event.reply("⚠️ Tên phòng không hợp lệ.")
            return

        ok = task_manager.start_tracking(room)
        if ok:
            await event.reply(f"✅ Đã thêm `{room}` vào danh sách theo dõi ghi hình.")
        else:
            await event.reply(f"ℹ️ Phòng `{room}` đã nằm trong danh sách theo dõi.")

    async def handle_stop(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return

        parts = (event.raw_text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            await event.reply("⚠️ Sử dụng: `/stop <username|all>`")
            return

        target = parts[1].strip()
        if target.lower() == "all":
            task_manager.stop_all()
            await event.reply("🛑 Đã phát tín hiệu dừng tất cả các phòng đang ghi.")
            return

        room = normalize_room_id(target)
        ok = task_manager.stop_tracking(room)
        if ok:
            await event.reply(f"🛑 Đã dừng theo dõi/ghi hình phòng `{room}`.")
        else:
            await event.reply(f"⚠️ Phòng `{room}` không có trong danh sách đang theo dõi.")

    async def handle_fav(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return

        favs = load_favorites(fav_file)
        if not favs:
            await event.reply("📋 Danh sách `favorites.txt` đang trống.")
            return

        lines = [f"📋 **DANH SÁCH FAVORITES ({len(favs)} phòng):**"]
        for f in favs:
            lines.append(f"• `{f.get('room_id', '')}`")
        await event.reply("\n".join(lines))

    async def handle_add(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return

        parts = (event.raw_text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            await event.reply("⚠️ Sử dụng: `/add <username>`")
            return

        room = normalize_room_id(parts[1])
        if not room:
            await event.reply("⚠️ Tên phòng không hợp lệ.")
            return

        ok = add_favorite(room_id=room, note="", path=fav_file)
        task_manager.start_tracking(room)
        if ok:
            await event.reply(f"✅ Đã thêm `{room}` vào `favorites.txt` và bắt đầu theo dõi.")
        else:
            await event.reply(f"ℹ️ `{room}` đã có sẵn trong `favorites.txt`.")

    async def handle_del(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return

        parts = (event.raw_text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            await event.reply("⚠️ Sử dụng: `/del <username>`")
            return

        room = normalize_room_id(parts[1])
        if not room:
            await event.reply("⚠️ Tên phòng không hợp lệ.")
            return

        ok = remove_favorite(room_id=room, path=fav_file)
        task_manager.stop_tracking(room)
        if ok:
            await event.reply(f"🗑 Đã xóa `{room}` khỏi `favorites.txt`.")
        else:
            await event.reply(f"⚠️ `{room}` không có trong `favorites.txt`.")

    async def handle_files(event: Any) -> None:
        if not is_authorized(event.sender_id, config.admin_ids):
            return

        records = history_store.list_records(active_only=True)
        text = format_files_message(records, retention_hours=config.retention_hours)
        await event.reply(text)

    dispatch_map: dict[str, Callable] = {
        "/start": handle_start,
        "/help": handle_help,
        "/status": handle_status,
        "/rec": handle_rec,
        "/stop": handle_stop,
        "/fav": handle_fav,
        "/add": handle_add,
        "/del": handle_del,
        "/files": handle_files,
    }

    # If Telethon client is active, bind events.NewMessage
    try:
        from telethon import events

        @client.on(events.NewMessage)
        async def on_message(event: Any):
            text = (event.raw_text or "").strip()
            cmd = text.split()[0].lower() if text else ""
            if "@" in cmd:
                cmd = cmd.split("@")[0]
            handler = dispatch_map.get(cmd)
            if handler:
                await handler(event)
    except (ImportError, AttributeError):
        pass

    return dispatch_map
