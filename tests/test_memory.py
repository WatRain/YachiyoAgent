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
    """system 不能存进去 —— 它每次重建时会重新拼（因为带记忆）。

    空 content 的 assistant 也丢掉；孤零零的 tool 条目（没有 tool_call_id、
    前面也找不到那次调用）同样留不下，理由见下面的孤儿测试。
    """
    store.save_conversation([
        {"role": "system", "content": "人格设定"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": ""},        # 空内容丢掉
        {"role": "tool", "content": "工具结果"},      # 没有 tool_call_id，也丢掉
    ])
    saved = store.load_conversation()
    assert [m["role"] for m in saved] == ["user"]


def test_conversation_keeps_the_whole_tool_round(isolated_data_dir):
    """工具轮要整段留下来：调用的那句 + tool_calls + 结果 + 真回答。

    ★ 以前只存 user / assistant，工具结果和 tool_calls 一起丢掉，历史里就剩一句
      「我查一下。」。两个后果都实测过：重启后界面上的工具卡片全部蒸发
      （用户的原话是「重新开启应用后，之前调用过的工具看不到」）；
      模型看到一整段「说了要查、后面什么都没有」的样板，下次就照着演。
    """
    round_trip = [
        {"role": "user", "content": "苹果折叠屏发售了吗"},
        {"role": "assistant", "content": "我查一下。",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "web_search", "content": "搜索结果"},
        {"role": "assistant", "content": "还没发售。"},
    ]
    store.save_conversation(round_trip)
    assert store.load_conversation() == round_trip


def test_conversation_drops_orphan_tool_calls(isolated_data_dir):
    """带 tool_calls 但没有对应 tool 结果的，把 tool_calls 摘掉、那句话当普通发言。

    老版本只存半句、工具跑到一半被取消，都会留下这种孤儿。留着有害：
    下一次请求可能直接报错（接口要求严格成对）；模型看到的只有「说了不查」。
    """
    store.save_conversation([
        {"role": "user", "content": "看看b站热点"},
        {"role": "assistant", "content": "我去查一下 B 站现在的热点。",
         "tool_calls": [{"id": "c9", "type": "function",
                         "function": {"name": "web_search", "arguments": "{}"}}]},
        {"role": "assistant", "content": "两个页面都打不开，我按搜到的整理："},
        {"role": "user", "content": "谢谢"},
        {"role": "assistant", "content": "不客气～"},
    ])
    saved = store.load_conversation()
    assert [m["content"] for m in saved] == [
        "看看b站热点",
        "我去查一下 B 站现在的热点。",
        "两个页面都打不开，我按搜到的整理：",
        "谢谢",
        "不客气～",
    ]
    assert "tool_calls" not in saved[1]


def test_conversation_drops_tool_results_without_a_call(isolated_data_dir):
    """找不到那次调用的 tool 结果整条丢掉 —— 它没有上下文，留着没意义。"""
    store.save_conversation([
        {"role": "user", "content": "你好"},
        {"role": "tool", "tool_call_id": "c999", "name": "web_search", "content": "野结果"},
        {"role": "assistant", "content": "在的～"},
    ])
    assert [m["role"] for m in store.load_conversation()] == ["user", "assistant"]


def test_reminders(isolated_data_dir):
    store.add_reminder("开会", "明天九点")
    assert len(store.list_reminders()) == 1
    assert store.list_reminders()[0]["content"] == "开会"
    store.clear_reminders()
    assert store.list_reminders() == []
