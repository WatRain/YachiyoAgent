"""模型调用层：测试连接 + 错误归一化。

职责边界（重要）：
    本文件只负责"能不能连上、出错怎么跟用户说"。
    **agent loop（多轮 + 工具调用 + 流式渲染）是你自己写的那部分**，
    不在这里 —— 见 docs/AGENT_LOOP.md。

两条铁律：
    1. 任何网络调用都必须有超时。没有超时保护的调用是事故源头。
    2. 错误信息绝不回显原始报文/请求头 —— 只给状态码翻译和异常类名。
"""

from __future__ import annotations

import asyncio
import logging

from core.config import ProviderConfig
from core.providers import resolve_model_string, to_litellm_kwargs, validate_base_url

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
PING = "ping"


def humanize_error(exc: BaseException) -> str:
    """把 SDK 异常翻译成用户看得懂的一句话。不含任何请求内容。"""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = str(exc).lower()

    if status == 400:
        return "请求被拒绝（400）。多半是模型名不对，或该模型不支持这次请求的格式。"
    if status == 401:
        return "API Key 无效或已被撤销（401）。请确认是否复制完整、有没有多余空格。"
    if status == 402:
        return "账户余额不足（402）。"
    if status == 403:
        return "这个 Key 没有访问该模型的权限（403）。"
    if status == 404:
        return "端点地址或模型名不正确（404）。"
    if status == 408:
        return "服务端超时（408），稍后重试。"
    if status == 422:
        return "参数不被接受（422）。请检查模型名和协议选择是否正确。"
    if status == 429:
        return "请求过于频繁或额度已用尽（429）。"
    if status in (500, 502, 503, 504):
        return f"服务端错误（{status}），这不是你的配置问题，稍后重试。"
    if "timeout" in text or "timed out" in text:
        return "连接超时。请检查网络、代理设置，或端点地址是否正确。"
    if "connection" in text or "connect" in text:
        return "无法连接到该端点。请检查地址拼写，以及服务是否在运行。"
    if "ssl" in text or "certificate" in text:
        return "TLS 证书校验失败。可能是代理拦截，或端点证书有问题。"
    if "model" in text and ("not found" in text or "does not exist" in text):
        return "模型不存在。请检查模型 ID 是否写对。"

    # 兜底：只给类名，绝不 str(exc) 全量返回
    return f"调用失败（{type(exc).__name__}）"


def _short_host(base_url: str) -> str:
    try:
        from urllib.parse import urlparse

        return urlparse(validate_base_url(base_url)).hostname or "默认端点"
    except Exception:
        return "该端点"


async def test_connection(
    provider: ProviderConfig,
    api_key: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[bool, str]:
    """发一条最小请求验证配置是否可用。

    返回 (是否成功, 给用户看的一句话)。**不抛异常**，所有失败都变成返回值。
    成本极低（max_tokens=1），所以可以放心让用户随手点。
    """
    from litellm import acompletion

    if not (api_key or "").strip():
        return False, "请先填写 API Key。"

    try:
        kwargs = to_litellm_kwargs(provider, api_key)
    except ValueError as exc:
        return False, str(exc)

    where = _short_host(provider.base_url)
    target = f"{resolve_model_string(provider)} @ {where}"

    try:
        await asyncio.wait_for(
            acompletion(
                **kwargs,
                messages=[{"role": "user", "content": PING}],
                max_tokens=1,
                stream=False,
                timeout=timeout,
            ),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        return False, f"超时（超过 {timeout:.0f} 秒）。目标：{target}"
    except Exception as exc:
        log.warning("test_connection 失败 provider=%s: %s", provider.id, type(exc).__name__)
        return False, f"{humanize_error(exc)}（目标：{target}）"

    return True, f"连接成功。目标：{target}"


async def list_models(
    provider: ProviderConfig,
    api_key: str,
    *,
    timeout: float = 15.0,
) -> list[str] | None:
    """尝试列出可用模型（仅 OpenAI 兼容端点）。

    很多中转站没实现这个接口，所以**失败是正常情况**，返回 None 就行。
    拿到列表时可以用它做模型下拉框，体验提升很大。
    """
    import httpx

    try:
        base = validate_base_url(provider.base_url) or "https://api.openai.com/v1"
    except ValueError:
        return None

    if provider.protocol != "openai":
        return None

    headers = {"Authorization": f"Bearer {api_key}"}
    headers.update(provider.extra_headers or {})

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{base}/models", headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None

    ids = [str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id")]
    return sorted(ids) or None
