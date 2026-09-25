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
#  对话记录（user / assistant / tool 全存，用来跨次启动恢复上下文）
# ─────────────────────────────────────────────

def _conversation_path() -> Path:
    return data_dir() / CONVERSATION_FILE


def _call_ids(item: dict) -> list[str]:
    """一条 assistant 消息里所有 tool_calls 的 id。"""
    return [
        c.get("id") or ""
        for c in item.get("tool_calls") or []
        if isinstance(c, dict)
    ]


def _drop_orphan_tool_calls(items: list[dict]) -> list[dict]:
    """把工具轮里配不上对的部分摘掉。

    接口认的是严格成对的形状：assistant(tool_calls) → 每个 id 一条 tool 结果。
    历史里难免有配不上的 —— 老版本只存了「我查一下…」那半句（工具结果没落盘），
    或者工具跑到一半被取消。留下孤儿有两条害处：下一次请求可能直接报错；
    模型看到「说了要查、后面什么都没有」的样板，只会学成「说了不查」。
    所以：
      · 带 tool_calls 但结果不齐的 assistant → 摘掉 tool_calls，那句话当普通发言；
      · 找不到对应调用的 tool 结果 → 整条丢掉（没有上下文，留着没意义）。
    """
    declared = {i for item in items for i in _call_ids(item) if i}
    answered = {item.get("tool_call_id") for item in items if item.get("role") == "tool"}
    out: list[dict] = []
    for item in items:
        role = item.get("role")
        if role == "tool":
            if item.get("tool_call_id") in declared:
                out.append(item)
            continue
        ids = [i for i in _call_ids(item) if i]
        if item.get("tool_calls") and (not ids or not all(i in answered for i in ids)):
            out.append({"role": role, "content": item.get("content") or ""})
            continue
        out.append(item)
    return out


def load_conversation() -> list[dict]:
    """读上次的对话记录。

    ★ 连工具轮一起读回来（assistant 的 tool_calls + 后面几条 tool 结果）。
      以前只留 user / assistant，重启后历史里就只剩「我查一下…」这种半句：
      序列是残的（接口不认），模型看到一整段「说了不查」的样板也会照抄，
      界面上那些工具卡片更是全都蒸发了（用户反馈过「重启后看不到调过什么工具」）。
      tool 结果是喂回模型的内容，和助手回答一样属于这段对话。

    system 不读 —— 它每次都是现拼的（里面带长期记忆和时间）。
    """
    data = _read_json(_conversation_path(), [])
    if not isinstance(data, list):
        return []
    items: list[dict] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role")
        content = raw.get("content")
        text = content if isinstance(content, str) else ""
        if role == "user":
            if text:
                items.append({"role": "user", "content": text})
        elif role == "assistant":
            entry: dict = {"role": "assistant", "content": text}
            if raw.get("tool_calls"):
                entry["tool_calls"] = raw["tool_calls"]
            if text or entry.get("tool_calls"):
                items.append(entry)
        elif role == "tool" and raw.get("tool_call_id"):
            items.append({
                "role": "tool",
                "tool_call_id": raw["tool_call_id"],
                "name": raw.get("name") or "",
                "content": text,
            })
    return _drop_orphan_tool_calls(items)


def save_conversation(messages: list[dict]) -> None:
    _write_json(_conversation_path(), messages)


def clear_conversation() -> None:
    _write_json(_conversation_path(), [])
