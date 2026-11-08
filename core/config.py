from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    if not path.exists():
        return cfg

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return cfg

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

    return cfg


def load_favorites(path: Path, include_disabled: bool = False) -> list[dict]:
    if not path.exists():
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

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

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def add_favorite(room_id: str, note: str, path: Path) -> bool:
    rid = normalize_room_id(room_id)
    if not rid:
        return False
    items = load_favorites(path, include_disabled=True) if path.exists() else []
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
    if not rid or not path.exists():
        return False
    items = load_favorites(path, include_disabled=True)
    orig_len = len(items)
    items = [it for it in items if it["room_id"] != rid]
    if len(items) == orig_len:
        return False
    save_favorites(items, path)
    return True
