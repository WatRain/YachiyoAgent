"""本地存储：长期记忆 + 提醒。

用 JSON 文件，不用数据库 —— 这几样东西都很小（几十条量级），
JSON 更好读、更好调试、也方便用户自己打开看（隐私透明）。

文件都放在 data_dir() 下，也就是打包后的
%APPDATA%\\<company>\\<product>\\data\\。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.paths import data_dir

log = logging.getLogger(__name__)

MEMORY_FILE = "memories.json"
REMINDER_FILE = "reminders.json"
CONVERSATION_FILE = "conversation.json"


def _now() -> str:
    """当前时间，UTC，ISO 格式。存 UTC、显示时再转本地。"""
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default):
    """读 JSON。文件不存在或损坏都返回默认值，绝不抛异常。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("读取 %s 失败（%s），用空数据继续", path.name, type(exc).__name__)
        return default


def _write_json(path: Path, data) -> None:
    """原子写：先写临时文件再替换，中途崩溃不会毁掉原文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ─────────────────────────────────────────────
#  长期记忆
# ─────────────────────────────────────────────

def _memory_path() -> Path:
    return data_dir() / MEMORY_FILE


def load_memories() -> list[dict]:
    """读全部记忆。"""
    data = _read_json(_memory_path(), [])
    return data if isinstance(data, list) else []


def save_memories(memories: list[dict]) -> None:
    _write_json(_memory_path(), memories)


def upsert_memory(key: str, value: str, confidence: float = 0.8) -> None:
    """按 key upsert：同一个 key 更新，不会存成两条。

    这就是"结构化记忆"的意义 —— 用户能看懂、能改、能删。
    """
    memories = load_memories()
    for item in memories:
        if item.get("key") == key:
            item["value"] = value
            item["confidence"] = confidence
            item["updated_at"] = _now()
            break
    else:
        memories.append({
            "key": key,
            "value": value,
            "confidence": confidence,
            "updated_at": _now(),
        })
    save_memories(memories)


def delete_memory(key: str) -> None:
    save_memories([m for m in load_memories() if m.get("key") != key])


def clear_memories() -> None:
    save_memories([])


# ─────────────────────────────────────────────
#  提醒
# ─────────────────────────────────────────────

def _reminder_path() -> Path:
    return data_dir() / REMINDER_FILE


def load_reminders() -> list[dict]:
    data = _read_json(_reminder_path(), [])
    return data if isinstance(data, list) else []


def add_reminder(content: str, due_at: str = "") -> dict:
    """加一条提醒。due_at 是给用户看的描述（比如"明天九点"），不做精确调度。"""
    reminders = load_reminders()
    item = {"content": content, "due_at": due_at, "created_at": _now()}
    reminders.append(item)
    _write_json(_reminder_path(), reminders)
    return item


def list_reminders() -> list[dict]:
    return load_reminders()


def clear_reminders() -> None:
    _write_json(_reminder_path(), [])


# ─────────────────────────────────────────────
#  对话记录（只保留 user / assistant，用来跨次启动恢复上下文）
# ─────────────────────────────────────────────

def _conversation_path() -> Path:
    return data_dir() / CONVERSATION_FILE


def load_conversation() -> list[dict]:
    """读上次的对话记录。

    只返回 role 是 user / assistant 且 content 非空的消息 ——
    system 每次重建时都会重新拼（因为它里面带记忆和时间）。
    """
    data = _read_json(_conversation_path(), [])
    if not isinstance(data, list):
        return []
    cleaned = []
    for item in data:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content:
            cleaned.append({"role": role, "content": content})
    return cleaned


def save_conversation(messages: list[dict]) -> None:
    _write_json(_conversation_path(), messages)


def clear_conversation() -> None:
    _write_json(_conversation_path(), [])
