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
from core.recorder import probe_room
from core.task_manager import JobStatus, TaskManager
from storage.history import HistoryRecord, HistoryStore

logger = logging.getLogger(__name__)

__all__ = [
    "is_authorized",
    "is_authorized_private",
    "format_status_message",
    "format_room_status_message",
    "format_help_message",
    "format_files_message",
    "register_handlers",
]


def is_authorized(user_id: Optional[int], admin_ids: list[int]) -> bool:
    """Check if the sender is an authorized administrator."""
    if user_id is None or not admin_ids:
        return False
    return int(user_id) in admin_ids


def is_authorized_private(event: Any, admin_ids: list[int]) -> bool:
    """Check if the event is a private 1-on-1 chat with the bot from an authorized admin.

    All bot controls and responses are restricted to private chat with the bot.
    Commands sent in groups or channels are strictly rejected.
    """
    if hasattr(event, "is_private") and not bool(getattr(event, "is_private", True)):
        return False
    return is_authorized(getattr(event, "sender_id", None), admin_ids)


def format_status_message(
    active_jobs: list[dict[str, Any]],
    queue_status: dict[str, Any],
    free_disk_mb: int | float,
    retention_files_count: int,
    all_rooms_status: Optional[list[dict[str, Any]]] = None,
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
            res = job.get("resolution", "")
            res_str = f" [{res}]" if res else ""
            lines.append(f"  • `#{room}`{res_str} - {st} ({size} MB)")
    else:
        lines.append("  *(Không có phòng nào đang ghi)*")

    # All tracked rooms status
    if all_rooms_status:
        lines.append(f"\n🌐 **Trạng thái các phòng theo dõi ({len(all_rooms_status)}):**")
        for item in all_rooms_status:
            r_id = item.get("room_id", "")
            raw_st = item.get("status", "offline").lower()
            res = item.get("resolution", "")
            is_rec = item.get("is_recording", False)
            res_info = f" [{res}]" if res else ""
            rec_tag = " *(Đang ghi)*" if is_rec else ""

            if "online" in raw_st:
                icon = "🟢"
                label = f"Online{res_info}"
            elif "private" in raw_st:
                icon = "🔒"
                label = "Private"
            else:
                icon = "⚪"
                label = "Offline"
            lines.append(f"  • `#{r_id}`: {icon} {label}{rec_tag}")

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


def format_room_status_message(room: str, job: Any, info: Any, files: list[HistoryRecord]) -> str:
    """Format detailed status for an individual room."""
    lines = [
        f"🔍 **CHI TIẾT ROOM: #{room}**",
        "──────────────────────────────",
    ]
    if info:
        is_live = info.is_live
        status_label = "🟢 ONLINE" if is_live else f"⚪ {info.room_status.upper() or 'OFFLINE'}"
        res_label = info.resolution_label or (f"{info.height}p" if info.height > 0 else "Unknown")
        lines.append(f"• **Trạng thái Stream:** {status_label}")
        if is_live:
            lines.append(f"• **Độ phân giải:** `{res_label}` ({info.width}x{info.height})")
            if info.hls_url:
                lines.append(f"• **HLS URL:** `{info.hls_url[:60]}...`")
        elif info.error:
            lines.append(f"• **Ghi chú lỗi:** `{info.error}`")

    if job:
        lines.append(f"• **Tiến trình hiện tại:** `{job.status.value}`")
        if job.status == JobStatus.RECORDING:
            dur = int(time.time() - job.started_at) if job.started_at else 0
            lines.append(f"• **Đã ghi:** `{job.bytes_recorded / (1024*1024):.1f} MB` ({dur}s)")
            if job.current_file:
                lines.append(f"• **File tạm:** `{job.current_file.name}`")
    else:
        lines.append("• **Theo dõi tự động:** `Chưa kích hoạt`")

    # Files for this room
    room_files = [f for f in files if f.room_id.lower() == room.lower()]
    lines.append(f"\n📦 **Video đã tải lên ({len(room_files)}):**")
    for rf in room_files[-3:]:
        sz = rf.size / (1024 * 1024)
        fn = Path(rf.file_path).name
        lines.append(f"  • `{fn}` ({sz:.1f} MB)")

    return "\n".join(lines)


def format_help_message() -> str:
    """Return command usage manual."""
    return (
        "🤖 **DANH SÁCH LỆNH ĐIỀU KHIỂN BOT**\n"
        "──────────────────────────────\n"
        "• `/status [user]` - Xem trạng thái hệ thống hoặc chi tiết phòng (1080p, 720p, v.v.)\n"
        "• `/rec <user>` - Bắt đầu theo dõi và ghi hình một streamer ngay\n"
        "• `/stop <user|all>` - Dừng ghi hình một hoặc tất cả streamer\n"
        "• `/upload <user|file>` - Tải lên file video còn tồn của phòng\n"
        "• `/fav` - Xem danh sách streamer trong `favorites.json`\n"
        "• `/add <user>` - Thêm streamer vào danh sách theo dõi\n"
        "• `/del <user>` - Xóa streamer khỏi danh sách theo dõi\n"
        "• `/files` - Xem danh sách video đã ghi trong 48h qua\n"
        "• `/clean [user|all]` - Xóa thủ công file video đã ghi/upload để giải phóng dung lượng\n"
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
    fav_file = Path(favorites_path) if favorites_path else Path("favorites.json")

    async def handle_start(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
            return
        await event.reply(format_help_message())

    async def handle_help(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
            return
        await event.reply(format_help_message())

    async def handle_status(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
            return

        parts = (event.raw_text or "").strip().split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            # Detailed status for specific room
            room = normalize_room_id(parts[1])
            job = task_manager.get_job(room)
            try:
                info = probe_room(room, proxy=config.proxy)
            except Exception as e:
                info = None
            records = history_store.sync_with_disk(task_manager.recordings_dir)
            text = format_room_status_message(room, job, info, records)
            await event.reply(text)
            return

        active_jobs = [
            {
                "room_id": j.room_id,
                "status": j.status.value,
                "size_mb": round(j.bytes_recorded / (1024 * 1024), 1),
                "resolution": j.last_detected_resolution,
            }
            for j in task_manager.get_active_jobs()
        ]
        active_room_ids = {j.room_id for j in task_manager.get_active_jobs()}
        all_rooms_status = [
            {
                "room_id": j.room_id,
                "status": j.last_detected_status or "offline",
                "resolution": j.last_detected_resolution,
                "is_recording": j.room_id in active_room_ids,
            }
            for j in task_manager.get_jobs()
        ]
        q_status = upload_queue.get_queue_status()
        disk_free = get_disk_free(task_manager.recordings_dir)
        records = history_store.sync_with_disk(task_manager.recordings_dir)

        text = format_status_message(
            active_jobs=active_jobs,
            queue_status=q_status,
            free_disk_mb=disk_free,
            retention_files_count=len(records),
            all_rooms_status=all_rooms_status,
        )
        await event.reply(text)

    async def handle_rec(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
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
        if not is_authorized_private(event, config.admin_ids):
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

    async def handle_upload(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
            return

        parts = (event.raw_text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            await event.reply("⚠️ Sử dụng: `/upload <username hoặc tên file>`")
            return

        target = parts[1].strip()
        room = normalize_room_id(target)

        # Look for matching mp4 files in recordings_dir (recursive search)
        rec_dir = Path(task_manager.recordings_dir)
        matched = []
        if rec_dir.exists():
            for f in rec_dir.rglob("*.mp4"):
                if room and room in f.name.lower():
                    matched.append(f)
                elif target.lower() in f.name.lower():
                    matched.append(f)

        if not matched:
            await event.reply(f"⚠️ Không tìm thấy file video nào phù hợp với `{target}` trong `{rec_dir}`.")
            return

        from bot.uploader import UploadItem
        enqueued_names = []
        for mf in matched:
            item = UploadItem(file_path=mf, room_id=room or "manual")
            upload_queue.enqueue(item)
            enqueued_names.append(mf.name)

        await event.reply(f"📤 Đã đưa vào hàng đợi upload {len(matched)} file:\n• " + "\n• ".join(enqueued_names))

    async def handle_fav(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
            return

        favs = load_favorites(fav_file)
        if not favs:
            await event.reply(f"📋 Danh sách `{fav_file.name}` đang trống.")
            return

        lines = [f"📋 **DANH SÁCH FAVORITES ({len(favs)} phòng):**"]
        for f in favs:
            note = f" ({f.get('note')})" if f.get("note") else ""
            status = "🟢" if f.get("enabled", True) else "⚪"
            lines.append(f"{status} • `{f.get('room_id', '')}`{note}")
        await event.reply("\n".join(lines))

    async def handle_add(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
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
            await event.reply(f"✅ Đã thêm `{room}` vào `{fav_file.name}` và bắt đầu theo dõi.")
        else:
            await event.reply(f"ℹ️ `{room}` đã có sẵn trong `{fav_file.name}`.")

    async def handle_del(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
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
            await event.reply(f"🗑 Đã xóa `{room}` khỏi `{fav_file.name}`.")
        else:
            await event.reply(f"⚠️ `{room}` không có trong `{fav_file.name}`.")

    async def handle_files(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
            return

        records = history_store.sync_with_disk(task_manager.recordings_dir)
        text = format_files_message(records, retention_hours=config.retention_hours)
        await event.reply(text)

    async def handle_clean(event: Any) -> None:
        if not is_authorized_private(event, config.admin_ids):
            return

        parts = (event.raw_text or "").strip().split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else ""

        records = history_store.sync_with_disk(task_manager.recordings_dir)
        if not records:
            # Also check if there are any lingering mp4 files in recordings_dir
            rec_dir = Path(task_manager.recordings_dir)
            extra_files = list(rec_dir.rglob("*.mp4")) if rec_dir.exists() else []
            if not extra_files:
                await event.reply("ℹ️ Không có file video nào cần dọn dẹp trên ổ đĩa.")
                return

        # Determine which files to delete
        deleted_count = 0
        freed_bytes = 0
        deleted_names = []

        rec_dir = Path(task_manager.recordings_dir).resolve()

        if not target or target.lower() == "all":
            # Delete all uploaded/recorded files
            for r in list(records):
                p = Path(r.file_path)
                candidates = [
                    p,
                    rec_dir / p,
                    rec_dir / p.name,
                    rec_dir / "chaturbate" / r.room_id / p.name,
                ]
                for cand in candidates:
                    try:
                        if cand.is_file():
                            sz = cand.stat().st_size
                            cand.unlink()
                            freed_bytes += sz
                            deleted_count += 1
                            deleted_names.append(cand.name)
                            break
                    except OSError:
                        pass
                history_store.mark_purged(r.file_path)

            # Prune empty directories
            try:
                for sub in rec_dir.glob("chaturbate/*"):
                    if sub.is_dir() and not any(sub.iterdir()):
                        sub.rmdir()
            except OSError:
                pass

            freed_mb = freed_bytes / (1024 * 1024)
            current_free = get_disk_free(rec_dir)
            history_store.sync_with_disk(rec_dir)
            await event.reply(
                f"🧹 **Đã dọn dẹp toàn bộ:** Xóa `{deleted_count}` video.\n"
                f"💾 **Dung lượng giải phóng:** `{freed_mb:.1f} MB`\n"
                f"📊 **Dung lượng trống hiện tại:** `{int(current_free)} MB`"
            )
        else:
            # Delete for specific room or file match
            room = normalize_room_id(target)
            matched_records = [
                r for r in records
                if (room and r.room_id.lower() == room.lower()) or (target.lower() in Path(r.file_path).name.lower())
            ]

            if not matched_records:
                await event.reply(f"⚠️ Không tìm thấy file video nào phù hợp với `{target}` để xóa.")
                return

            for r in matched_records:
                p = Path(r.file_path)
                candidates = [
                    p,
                    rec_dir / p,
                    rec_dir / p.name,
                    rec_dir / "chaturbate" / r.room_id / p.name,
                ]
                for cand in candidates:
                    try:
                        if cand.is_file():
                            sz = cand.stat().st_size
                            cand.unlink()
                            freed_bytes += sz
                            deleted_count += 1
                            deleted_names.append(cand.name)
                            break
                    except OSError:
                        pass
                history_store.mark_purged(r.file_path)

            freed_mb = freed_bytes / (1024 * 1024)
            current_free = get_disk_free(rec_dir)
            history_store.sync_with_disk(rec_dir)
            lines = [
                f"🗑 **Đã xóa thủ công {deleted_count} video cho `{target}`:**",
                "• " + "\n• ".join(deleted_names[:10]),
            ]
            if len(deleted_names) > 10:
                lines.append(f"... và {len(deleted_names) - 10} file khác")
            lines.append(f"\n💾 Giải phóng: `{freed_mb:.1f} MB` | Trống: `{int(current_free)} MB`")
            await event.reply("\n".join(lines))

    dispatch_map: dict[str, Callable] = {
        "/start": handle_start,
        "/help": handle_help,
        "/status": handle_status,
        "/rec": handle_rec,
        "/stop": handle_stop,
        "/upload": handle_upload,
        "/fav": handle_fav,
        "/add": handle_add,
        "/del": handle_del,
        "/files": handle_files,
        "/clean": handle_clean,
    }

    # If Telethon client is active, bind events.NewMessage
    try:
        from telethon import events

        @client.on(events.NewMessage)
        async def on_message(event: Any):
            # Control commands are strictly private via 1-on-1 bot chat.
            # Any messages sent in groups/channels are ignored to keep groups clean.
            if not getattr(event, "is_private", False):
                return
            if not is_authorized(getattr(event, "sender_id", None), config.admin_ids):
                return
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
