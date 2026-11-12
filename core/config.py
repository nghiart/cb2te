from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Any, Optional

_IGNORE_COMMENT_PREFIXES = (
    "favorites",
    "one room",
    "skip for",
    "remove:",
    "start/",
    "what is",
    "stop one",
    "stop all",
    "you may",
    "example",
    "interactive menu",
    "start all",
    "leading '#'",
    "chaturbate",
)


@dataclass
class Config:
    api_id: int = 0
    api_hash: str = ""
    bot_token: str = ""
    chat_id: int = 0
    admin_ids: list[int] = field(default_factory=list)
    retention_hours: int = 48
    max_concurrent: int = 2
    min_free_mb: int = 400
    proxy: str = ""
    monitor_interval_sec: int = 300  # Default 5 minutes periodic check
    notify_on_status_change: bool = True
    session_max_duration_sec: int = 900  # 15 mins rotation per session
    use_ffmpeg_recorder: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_room_id(text: str) -> str:
    raw = (text or "").strip().strip("/")
    if not raw:
        return ""
    m = re.search(r"chaturbate\.com/([A-Za-z0-9_-]+)", raw, re.I)
    if m:
        name = m.group(1).lower()
        if name not in ("api", "in", "accounts", "tags", "apps", "tipping", "auth", "billing"):
            return name
    t = re.sub(r"^(?:cb|chaturbate)\s*[:/]?\s*", "", raw, flags=re.I).lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_-]{3,60}", t):
        return t.lower()
    return t.strip().lower()


def load_config(path: Path) -> Config:
    cfg = Config()
    target_path = Path(path)

    # Fallback to .txt if .json does not exist
    if not target_path.exists():
        if target_path.suffix == ".json":
            txt_alt = target_path.with_suffix(".txt")
            if txt_alt.exists():
                return load_config(txt_alt)
        elif target_path.suffix == ".txt":
            json_alt = target_path.with_suffix(".json")
            if json_alt.exists():
                return load_config(json_alt)
        return cfg

    try:
        content = target_path.read_text(encoding="utf-8").strip()
    except OSError:
        return cfg

    if not content:
        return cfg

    # 1. Try parsing JSON format
    if target_path.suffix == ".json" or content.startswith("{"):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                if "api_id" in data:
                    cfg.api_id = int(data["api_id"])
                if "api_hash" in data:
                    cfg.api_hash = str(data["api_hash"])
                if "bot_token" in data:
                    cfg.bot_token = str(data["bot_token"])
                if "chat_id" in data:
                    cfg.chat_id = int(data["chat_id"])
                if "admin_ids" in data:
                    raw_admins = data["admin_ids"]
                    if isinstance(raw_admins, list):
                        cfg.admin_ids = [int(x) for x in raw_admins if str(x).isdigit() or isinstance(x, int)]
                    elif isinstance(raw_admins, (int, str)):
                        cfg.admin_ids = [int(x) for x in str(raw_admins).replace(",", " ").split() if x.isdigit()]
                if "retention_hours" in data:
                    cfg.retention_hours = int(data["retention_hours"])
                if "max_concurrent" in data:
                    cfg.max_concurrent = int(data["max_concurrent"])
                if "min_free_mb" in data:
                    cfg.min_free_mb = int(data["min_free_mb"])
                if "proxy" in data:
                    cfg.proxy = str(data["proxy"] or "").strip()
                if "monitor_interval_sec" in data:
                    cfg.monitor_interval_sec = int(data["monitor_interval_sec"])
                if "notify_on_status_change" in data:
                    cfg.notify_on_status_change = bool(data["notify_on_status_change"])
                if "session_max_duration_sec" in data:
                    cfg.session_max_duration_sec = int(data["session_max_duration_sec"])
                if "use_ffmpeg_recorder" in data:
                    cfg.use_ffmpeg_recorder = bool(data["use_ffmpeg_recorder"])
                return cfg
        except (ValueError, TypeError):
            pass

    # 2. Key-value line based parsing (.txt format fallback)
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        k = key.strip().lower()
        v = val.strip()

        if k.startswith("telegram_"):
            k = k[9:]

        if k == "api_id":
            try:
                cfg.api_id = int(v.split("#")[0].strip())
            except ValueError:
                pass
        elif k == "api_hash":
            cfg.api_hash = v.strip('"\'')
        elif k == "bot_token":
            cfg.bot_token = v.strip('"\'')
        elif k == "chat_id":
            try:
                cfg.chat_id = int(v.split("#")[0].strip())
            except ValueError:
                pass
        elif k in ("admin_ids", "admin_id", "admins"):
            v_clean = v.split("#")[0].strip()
            ids: list[int] = []
            for item in v_clean.replace(",", " ").split():
                try:
                    ids.append(int(item))
                except ValueError:
                    pass
            cfg.admin_ids = ids
        elif k == "retention_hours":
            try:
                cfg.retention_hours = int(v.split("#")[0].strip())
            except ValueError:
                pass
        elif k in ("max_concurrent", "max_concurrent_recordings"):
            try:
                cfg.max_concurrent = int(v.split("#")[0].strip())
            except ValueError:
                pass
        elif k in ("min_free_mb", "min_mb"):
            try:
                cfg.min_free_mb = int(v.split("#")[0].strip())
            except ValueError:
                pass
        elif k == "proxy":
            cfg.proxy = v.strip('"\'')
        elif k in ("monitor_interval_sec", "monitor_interval"):
            try:
                cfg.monitor_interval_sec = int(v.split("#")[0].strip())
            except ValueError:
                pass
        elif k == "notify_on_status_change":
            cfg.notify_on_status_change = v.split("#")[0].strip().lower() in ("1", "true", "yes")
        elif k == "session_max_duration_sec":
            try:
                cfg.session_max_duration_sec = int(v.split("#")[0].strip())
            except ValueError:
                pass
        elif k == "use_ffmpeg_recorder":
            cfg.use_ffmpeg_recorder = v.split("#")[0].strip().lower() in ("1", "true", "yes")

    return cfg


def save_config(cfg: Config, path: Path) -> None:
    """Save configuration to JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        path.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        # Save as traditional key-value
        lines = [
            f"API_ID = {cfg.api_id}",
            f'API_HASH = "{cfg.api_hash}"',
            f'BOT_TOKEN = "{cfg.bot_token}"',
            f"CHAT_ID = {cfg.chat_id}",
            f"ADMIN_IDS = {', '.join(str(x) for x in cfg.admin_ids)}",
            f"RETENTION_HOURS = {cfg.retention_hours}",
            f"MAX_CONCURRENT = {cfg.max_concurrent}",
            f"MIN_FREE_MB = {cfg.min_free_mb}",
            f'PROXY = "{cfg.proxy}"',
            f"MONITOR_INTERVAL_SEC = {cfg.monitor_interval_sec}",
            f"NOTIFY_ON_STATUS_CHANGE = {1 if cfg.notify_on_status_change else 0}",
            f"SESSION_MAX_DURATION_SEC = {cfg.session_max_duration_sec}",
            f"USE_FFMPEG_RECORDER = {1 if cfg.use_ffmpeg_recorder else 0}",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_favorites(path: Path, include_disabled: bool = False) -> list[dict]:
    target_path = Path(path)

    # Fallback between json and txt
    if not target_path.exists():
        if target_path.suffix == ".json":
            txt_alt = target_path.with_suffix(".txt")
            if txt_alt.exists():
                return load_favorites(txt_alt, include_disabled=include_disabled)
        elif target_path.suffix == ".txt":
            json_alt = target_path.with_suffix(".json")
            if json_alt.exists():
                return load_favorites(json_alt, include_disabled=include_disabled)
        return []

    try:
        content = target_path.read_text(encoding="utf-8").strip()
    except OSError:
        return []

    if not content:
        return []

    # 1. JSON parsing
    if target_path.suffix == ".json" or content.startswith("[") or content.startswith("{"):
        try:
            data = json.loads(content)
            raw_list = data.get("favorites", []) if isinstance(data, dict) else data
            if isinstance(raw_list, list):
                out: list[dict] = []
                seen: set[str] = set()
                for it in raw_list:
                    if isinstance(it, str):
                        rid = normalize_room_id(it)
                        if rid and rid not in seen:
                            seen.add(rid)
                            out.append({
                                "platform": "chaturbate",
                                "room_id": rid,
                                "note": "",
                                "nick": "",
                                "enabled": True,
                            })
                    elif isinstance(it, dict):
                        rid = normalize_room_id(it.get("room_id") or it.get("user") or "")
                        if not rid or rid in seen:
                            continue
                        en = bool(it.get("enabled", True))
                        if not en and not include_disabled:
                            continue
                        seen.add(rid)
                        note = str(it.get("note") or it.get("nick") or "").strip()
                        out.append({
                            "platform": it.get("platform", "chaturbate"),
                            "room_id": rid,
                            "note": note,
                            "nick": note,
                            "enabled": en,
                        })
                return out
        except (ValueError, TypeError):
            pass

    # 2. Text parsing (.txt fallback)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in content.lstrip("\ufeff").splitlines():
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
            if any(uncommented.lower().startswith(w) for w in _IGNORE_COMMENT_PREFIXES):
                continue
            line = uncommented
            enabled = False

        parts = line.split(None, 1)
        if not parts:
            continue
        token = parts[0]
        note = parts[1].strip() if len(parts) > 1 else ""

        rid = normalize_room_id(token)
        if not rid:
            continue
        if rid in seen:
            continue
        seen.add(rid)
        out.append({
            "platform": "chaturbate",
            "room_id": rid,
            "note": note,
            "nick": note,
            "enabled": enabled,
        })
    return out


def save_favorites(items: list[dict], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.suffix == ".json":
        clean_items = []
        seen: set[str] = set()
        for it in items:
            rid = normalize_room_id(it.get("room_id") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            clean_items.append({
                "platform": it.get("platform", "chaturbate"),
                "room_id": rid,
                "note": str(it.get("note") or it.get("nick") or "").strip(),
                "enabled": bool(it.get("enabled", True)),
            })
        path.write_text(json.dumps({"favorites": clean_items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return

    # Text format
    lines = [
        "# Chaturbate favorites",
        "# One username per line. An optional note (streamer name) may follow.",
        "# Leading '#' disables a streamer without deleting it.",
        "",
    ]
    seen: set[str] = set()
    for it in items:
        rid = normalize_room_id(it.get("room_id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        note = str(it.get("note") or it.get("nick") or "").strip()
        en = bool(it.get("enabled", True))
        prefix = "" if en else "# "
        suffix = f"  {note}" if note else ""
        lines.append(f"{prefix}{rid}{suffix}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def add_favorite(room_id: str, note: str, path: Path) -> bool:
    rid = normalize_room_id(room_id)
    if not rid:
        return False
    path = Path(path)
    items = load_favorites(path, include_disabled=True) if (path.exists() or path.with_suffix(".txt").exists() or path.with_suffix(".json").exists()) else []
    for it in items:
        if it["room_id"] == rid:
            it["note"] = note.strip()
            it["nick"] = note.strip()
            it["enabled"] = True
            save_favorites(items, path)
            return True
    items.append({
        "platform": "chaturbate",
        "room_id": rid,
        "note": note.strip(),
        "nick": note.strip(),
        "enabled": True,
    })
    save_favorites(items, path)
    return True


def remove_favorite(room_id: str, path: Path) -> bool:
    rid = normalize_room_id(room_id)
    path = Path(path)
    if not rid:
        return False
    items = load_favorites(path, include_disabled=True)
    orig_len = len(items)
    items = [it for it in items if it["room_id"] != rid]
    if len(items) == orig_len:
        return False
    save_favorites(items, path)
    return True
