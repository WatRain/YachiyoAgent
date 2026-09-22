"""记忆层与存储层的测试。不联网、不需要 API Key。

用 FLET_APP_STORAGE_DATA 指到临时目录，不碰你真实的 %APPDATA% 数据。
"""

import json
import shutil
import uuid
from pathlib import Path

import pytest

from core import store
from core.memory import format_memories, parse_extracted, save_extracted

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "memory"


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    """每个测试一个独立的数据目录，测完删掉。"""
    root = _TMP_ROOT / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(root))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ─────────────────────────────────────────────
#  parse_extracted：模型输出的清洗
# ─────────────────────────────────────────────

def test_parse_clean_json():
    items = parse_extracted('[{"key": "a", "value": "b", "confidence": 0.9}]')
    assert items == [{"key": "a", "value": "b", "confidence": 0.9}]


def test_parse_strips_markdown_fence():
    """模型经常裹一层 ```json 围栏。"""
    raw = '```json\n[{"key": "c", "value": "d"}]\n```'
    assert parse_extracted(raw) == [{"key": "c", "value": "d", "confidence": 0.8}]


def test_parse_survives_surrounding_chatter():
    """模型有时候会加几句废话。"""
    raw = '好的，结果如下：[{"key": "e", "value": "f"}] 完毕'
    assert parse_extracted(raw)[0]["key"] == "e"


@pytest.mark.parametrize("bad", [
    "", "这不是 JSON", "{}", '{"key": "x"}', "null", "123",
])
def test_parse_returns_empty_on_garbage(bad):
    """解析不了必须丢弃，绝不能把自由文本塞进记忆库。"""
    assert parse_extracted(bad) == []


def test_parse_drops_items_missing_fields():
    raw = '[{"key": "ok", "value": "v"}, {"key": ""}, {"value": "no key"}, "字符串"]'
    items = parse_extracted(raw)
    assert [i["key"] for i in items] == ["ok"]


def test_parse_clamps_confidence():
    raw = '[{"key": "a", "value": "b", "confidence": 5}, {"key": "c", "value": "d", "confidence": -2}]'
    items = parse_extracted(raw)
    assert items[0]["confidence"] == 1.0
    assert items[1]["confidence"] == 0.0


def test_parse_limits_to_three():
    raw = json.dumps([{"key": f"k{i}", "value": "v"} for i in range(10)])
    assert len(parse_extracted(raw)) == 3


def test_parse_truncates_long_values():
    raw = json.dumps([{"key": "a" * 200, "value": "b" * 1000}])
    item = parse_extracted(raw)[0]
    assert len(item["key"]) <= 40
    assert len(item["value"]) <= 200


# ─────────────────────────────────────────────
#  format_memories：注入格式
# ─────────────────────────────────────────────

def test_format_empty_returns_blank():
    assert format_memories([]) == ""
    assert format_memories(None) == ""


def test_format_contains_key_and_value():
    text = format_memories([{"key": "nickname", "value": "喜欢被叫小彩"}])
    assert "nickname" in text
    assert "喜欢被叫小彩" in text
    assert "你记得的关于用户的事" in text


def test_format_skips_incomplete_items():
    assert format_memories([{"key": "", "value": "x"}, {"key": "y"}]) == ""


# ─────────────────────────────────────────────
#  store：记忆与对话的读写
# ─────────────────────────────────────────────

def test_memory_upsert_updates_not_duplicates():
    store.upsert_memory("k", "第一次")
    store.upsert_memory("k", "第二次")
    memories = store.load_memories()
    assert len(memories) == 1
    assert memories[0]["value"] == "第二次"


def test_memory_delete_and_clear():
    save_extracted([{"key": "a", "value": "1", "confidence": 0.9},
                    {"key": "b", "value": "2", "confidence": 0.9}])
    store.delete_memory("a")
    assert [m["key"] for m in store.load_memories()] == ["b"]
    store.clear_memories()
    assert store.load_memories() == []


def test_memory_file_is_readable_json(isolated_data_dir):
    """记忆是结构化文件，用户应该能打开看懂（隐私透明）。"""
    store.upsert_memory("nickname", "喜欢被叫小彩")
    raw = (isolated_data_dir / "memories.json").read_text(encoding="utf-8")
    assert "nickname" in raw
    assert "小彩" in raw       # ensure_ascii=False，中文不被转义


def test_broken_memory_file_does_not_crash(isolated_data_dir):
    (isolated_data_dir / "memories.json").write_text("{坏掉的", encoding="utf-8")
    assert store.load_memories() == []


def test_conversation_roundtrip(isolated_data_dir):
    history = [{"role": "user", "content": "你好"},
               {"role": "assistant", "content": "呀吼～"}]
    store.save_conversation(history)
    assert store.load_conversation() == history


def test_conversation_drops_system_and_empty(isolated_data_dir):
    """system 不能存进去 —— 它每次重建时会重新拼（因为带记忆）。"""
    store.save_conversation([
        {"role": "system", "content": "人格设定"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": ""},        # 空内容丢掉
        {"role": "tool", "content": "工具结果"},      # tool 也丢掉
    ])
    saved = store.load_conversation()
    assert [m["role"] for m in saved] == ["user"]


def test_reminders(isolated_data_dir):
    store.add_reminder("开会", "明天九点")
    assert len(store.list_reminders()) == 1
    assert store.list_reminders()[0]["content"] == "开会"
    store.clear_reminders()
    assert store.list_reminders() == []
