"""长期记忆：抽取 + 存储 + 注入。

三层记忆里，这是"长期"那一层：
  · 短期 = 当前会话的 messages（在 core/chat.py 里）
  · 中期 = 旧对话的压缩摘要（以后再做）
  · 长期 = 关于用户的事实与偏好（本文件）

设计要点：
  1. **结构化存储**（key/value），用户能看懂、能改、能删 —— 这是隐私要求
  2. **抽取只在一个回合结束后做**，流式过程中做会拿到半截内容
  3. **解析失败必须丢弃**，绝不把模型写的自由文本塞进记忆库
  4. **明确禁止记录敏感信息**（证件号、密码、精确住址、健康隐私）
"""

from __future__ import annotations

import json
import logging

from core import store
from core.llm import get_litellm_acompletion

log = logging.getLogger(__name__)

MAX_MEMORIES_PER_TURN = 3

# 这是"抽取"用的提示词 —— 它和八千代的人格卡无关，
# 是一个纯粹的数据提取任务，所以要写得像给程序员看的规格说明，不能带角色语气。
EXTRACT_PROMPT = """以下是一轮对话。请提取"值得长期记住的关于用户的信息"。

只输出 JSON 数组，格式：
[{{"key": "短英文标识", "value": "中文描述", "confidence": 0.0到1.0}}]

没有可记的就输出 []。

规则：
- 只记偏好、约定、称呼、长期事实（例如：喜欢的称呼、作息、职业、正在做的事）
- 不记临时情绪、不记一次性的闲聊内容
- 绝对不记敏感信息：证件号、密码、银行卡、精确住址、健康或医疗隐私
- 最多 3 条
- key 用简短的英文小写下划线，例如 prefers_nickname、work_schedule

对话：
用户：{user}
八千代：{assistant}
"""


def format_memories(memories: list[dict] | None) -> str:
    """把记忆格式化成一段文字，塞进 system prompt。

    返回空字符串表示"没有记忆可注入"。
    """
    if not memories:
        return ""

    lines = []
    for item in memories:
        key = str(item.get("key", "")).strip()
        value = str(item.get("value", "")).strip()
        if key and value:
            lines.append(f"- {key}：{value}")

    if not lines:
        return ""

    return (
        "\n\n---\n\n"
        "# 你记得的关于用户的事\n"
        + "\n".join(lines)
        + "\n\n（这些只在相关时自然使用，不要逐条背诵，也不要主动说"
        + "\"我记住了\"之类的话。）\n"
    )


def parse_extracted(raw: str) -> list[dict]:
    """把模型吐出来的文本解析成记忆条目列表。

    模型经常在外面裹一层 ```json 围栏，或者加几句废话，
    所以这里要先"抠"出 JSON 再解析。解析不了就返回空列表 —— 宁可丢，不可存错。
    """
    text = (raw or "").strip()

    # 剥掉 ```json ... ``` 围栏
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()

    # 如果前后有废话，尝试只取第一段方括号内容
    if not text.startswith("["):
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end < start:
            return []
        text = text[start:end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.debug("记忆抽取结果不是合法 JSON，丢弃")
        return []

    if not isinstance(data, list):
        return []

    cleaned: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        value = str(item.get("value", "")).strip()
        if not key or not value:
            continue
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        cleaned.append({
            "key": key[:40],           # 别让模型写出超长 key
            "value": value[:200],      # 也别让单条记忆太长
            "confidence": max(0.0, min(1.0, confidence)),
        })

    return cleaned[:MAX_MEMORIES_PER_TURN]


async def extract_memories(chat, user_text: str, assistant_text: str) -> list[dict]:
    """用一次额外的模型调用，从这一轮对话里抽取值得长期记住的信息。

    参数 chat 就是 core.chat.Chat 的实例 —— 借它的模型配置用一下。
    这里**不碰 chat.messages**（不能污染真实对话历史），
    而是临时发一次独立请求。
    """
    if not user_text.strip() or not assistant_text.strip():
        return []

    acompletion = await get_litellm_acompletion()
    prompt = EXTRACT_PROMPT.format(user=user_text[:1500], assistant=assistant_text[:1500])

    try:
        response = await acompletion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,        # 抽取任务要稳定，不要发挥
            max_tokens=500,
            timeout=30,
            **chat.kwargs,
        )
        raw = response.choices[0].message.content or ""
    except Exception as exc:
        # 抽取失败不该影响正常聊天，记一笔就完事
        log.warning("记忆抽取失败: %s", type(exc).__name__)
        return []

    return parse_extracted(raw)


def save_extracted(items: list[dict]) -> int:
    """把抽取到的条目写进记忆库。返回写入条数。"""
    for item in items:
        store.upsert_memory(item["key"], item["value"], item["confidence"])
    return len(items)


def all_memories() -> list[dict]:
    return store.load_memories()


def forget(key: str) -> None:
    store.delete_memory(key)


def forget_all() -> None:
    store.clear_memories()
