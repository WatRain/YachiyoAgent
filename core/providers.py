"""Provider 层：把"用户填的表单"翻译成"LiteLLM 的调用参数"。

**唯一的翻译点。** UI 层永远不该出现 api_base / model= 这类字段。

三层结构：
    预设 Provider（本文件，只读模板）
        → 用户 Provider 实例（config.json，可编辑）
            → LiteLLM 调用参数（to_litellm_kwargs，运行时）

安全职责：
    base_url 由用户自由填写 = 攻击面。
    只允许 http/https，且拒绝把用户名密码写进地址里。
"""

from __future__ import annotations

import re
import secrets as _secrets
from urllib.parse import urlparse

from core.config import AppConfig, ProviderConfig

ALLOWED_SCHEMES = {"http", "https"}

# 预设只是"少打字"，不代表任何承诺；key 和模型名永远由用户填。
# 这些 base_url 我只写了我有把握的；其余留空 = 用该协议的官方默认端点。
BUILTIN_PRESETS: list[ProviderConfig] = [
    ProviderConfig(
        id="deepseek", display_name="DeepSeek", protocol="openai",
        base_url="https://api.deepseek.com/v1", model_id="deepseek-chat",
        is_builtin=True,
    ),
    ProviderConfig(
        id="openai", display_name="OpenAI", protocol="openai",
        base_url="https://api.openai.com/v1", model_id="gpt-4o-mini",
        is_builtin=True,
    ),
    ProviderConfig(
        id="anthropic", display_name="Anthropic", protocol="anthropic",
        base_url="", model_id="claude-3-5-haiku-latest",
        is_builtin=True,
    ),
    ProviderConfig(
        id="gemini", display_name="Google Gemini", protocol="gemini",
        base_url="", model_id="gemini-2.0-flash",
        is_builtin=True,
    ),
    ProviderConfig(
        id="openrouter", display_name="OpenRouter", protocol="openai",
        base_url="https://openrouter.ai/api/v1", model_id="",
        is_builtin=True,
    ),
    ProviderConfig(
        id="ollama", display_name="本地 Ollama", protocol="openai",
        base_url="http://localhost:11434/v1", model_id="llama3.1",
        is_builtin=True,
    ),
]


# ---------------------------------------------------------------- 校验

def validate_base_url(raw: str) -> str:
    """规范化并校验端点地址。空字符串是合法的（表示用协议默认端点）。"""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""

    p = urlparse(url)
    if p.scheme not in ALLOWED_SCHEMES:
        raise ValueError("端点只支持 http:// 或 https://")
    if not p.netloc:
        raise ValueError("端点地址不完整")
    if p.username or p.password:
        raise ValueError("请不要把用户名密码写在地址里（会随请求发出去）")
    return url


def host_of(base_url: str) -> str:
    return (urlparse(base_url).hostname or "").lower()


def looks_like_local(base_url: str) -> bool:
    """本机 / 局域网地址。合法（Ollama、LM Studio、one-api 都这样），但要提示用户。"""
    host = host_of(base_url)
    if not host:
        return False
    return (
        host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
        or host.startswith("192.168.")
        or host.startswith("10.")
        or host.startswith("172.16.")
        or host.endswith(".local")
    )


def validate_provider(p: ProviderConfig) -> list[str]:
    """返回问题列表；空列表 = 没问题。用于 UI 的行内错误提示。"""
    problems: list[str] = []

    if not p.id.strip():
        problems.append("Provider id 不能为空")
    if not p.display_name.strip():
        problems.append("显示名不能为空")
    if not p.model_id.strip():
        problems.append("模型 ID 不能为空")

    try:
        validate_base_url(p.base_url)
    except ValueError as exc:
        problems.append(str(exc))

    return problems


def make_provider_id(display_name: str, existing: list[str]) -> str:
    """给"新建 Provider"生成一个稳定、ascii、不重复的 id（它是密钥的存储键）。"""
    slug = re.sub(r"[^a-z0-9]+", "_", (display_name or "").strip().lower()).strip("_")
    if len(slug) < 2:
        slug = "provider"
    pid = slug
    while pid in existing:
        pid = f"{slug}_{_secrets.token_hex(2)}"
    return pid


# ---------------------------------------------------------------- 翻译

def resolve_model_string(p: ProviderConfig) -> str:
    """模型字符串。

    用户怎么填都行：
      "deepseek-chat"        → "openai/deepseek-chat"   （自动补协议前缀）
      "openai/deepseek-chat" → 原样                     （自带前缀就尊重用户）
    LiteLLM 靠这个前缀识别协议；这就是 main.py 里注释过的那个坑，现在被固化在这里。
    """
    model = (p.model_id or "").strip()
    if p.protocol == "custom" or "/" in model:
        return model
    return f"{p.protocol}/{model}"


def to_litellm_kwargs(p: ProviderConfig, api_key: str) -> dict:
    """Provider 配置 + 密钥 → LiteLLM 调用参数。"""
    kwargs: dict = {"model": resolve_model_string(p), "api_key": api_key}

    base = validate_base_url(p.base_url)
    if base:
        kwargs["api_base"] = base
    if p.extra_headers:
        kwargs["extra_headers"] = dict(p.extra_headers)

    return kwargs


# ---------------------------------------------------------------- 预设

def default_config() -> AppConfig:
    """首启动：把预设灌进去，并把第一个设为激活。"""
    cfg = AppConfig()
    cfg.providers = [p.model_copy(deep=True) for p in BUILTIN_PRESETS]
    cfg.active_provider = cfg.providers[0].id
    return cfg


def seed_builtins_if_empty(cfg: AppConfig) -> bool:
    """如果用户在设置页里把 provider 删光了，这里可以帮他重新灌预设。
    返回是否做了修改（调用方据此决定要不要 save_config）。
    """
    if cfg.providers:
        return False
    cfg.providers = [p.model_copy(deep=True) for p in BUILTIN_PRESETS]
    cfg.active_provider = cfg.providers[0].id
    return True
