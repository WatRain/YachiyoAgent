"""日志配置：所有输出都必须经过一道脱敏检查。

为什么不用 print：
  print 没有级别、没有时间、没有来源，也**绕不过脱敏**。
  logging 有一条所有人都必须经过的关卡（Filter），
  这样才能保证"密钥不落日志"这件事**不依赖你每次记得别打印**。

用法：程序启动时调用一次 setup_logging()。
"""

from __future__ import annotations

import logging
import re

from core.paths import logs_dir

# 命中这些词（不分大小写）的"字段名"一律替换掉
SENSITIVE_HINTS = (
    "key",
    "token",
    "secret",
    "authorization",
    "password",
    "cookie",
    "apikey",
)

# 兜底：把形似密钥的长串直接抹掉（防止密钥被拼进自由文本）
_PATTERNS = (
    re.compile(r"\b(?:sk|xai|gsk|or)-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
)


def redact_string(text: str) -> str:
    """把字符串里形似密钥的部分抹掉。"""
    for pattern in _PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


def redact(obj, _depth: int = 0):
    """递归脱敏：字典/列表/字符串都过一遍。"""
    if _depth > 6:  # 防止自引用结构无限递归
        return "<too-deep>"

    if isinstance(obj, dict):
        return {
            key: (
                "<redacted>"
                if any(hint in str(key).lower() for hint in SENSITIVE_HINTS)
                else redact(value, _depth + 1)
            )
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(item, _depth + 1) for item in obj]
    if isinstance(obj, str):
        return redact_string(obj)
    return obj


class RedactingFilter(logging.Filter):
    """挂在每个 handler 上的脱敏关卡。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # msg 可能是字符串（带 %s 占位），也可能是任意对象
        if isinstance(record.msg, str):
            record.msg = redact_string(record.msg)
        else:
            record.msg = redact(record.msg)

        # 占位符对应的实参也过一遍
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact(record.args)
            else:
                record.args = tuple(redact(a) for a in record.args)
        return True  # 返回 False 会丢掉这条日志，我们要保留（只是脱敏）


def setup_logging(level: int = logging.INFO) -> None:
    """配置根日志。程序启动时调一次。"""
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ① 写文件（保留完整记录，方便排查用户反馈的问题）
    file_handler = logging.FileHandler(logs_dir() / "app.log", encoding="utf-8")
    file_handler.setFormatter(formatter)

    # ② 也打屏幕（开发时方便看）
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # 防止重复调用时叠加 handler
    for handler in (file_handler, stream_handler):
        handler.addFilter(RedactingFilter())  # ★ 每个出口都要过关卡
        root.addHandler(handler)

    # litellm 自己的日志很吵，而且它不走我们的脱敏，收到 WARNING 就好
    logging.getLogger("litellm").setLevel(logging.WARNING)
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
