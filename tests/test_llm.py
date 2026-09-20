"""llm.py 的测试：错误归一化 + 参数校验（不发真实网络请求）。"""

import pytest

from core.config import ProviderConfig
from core.llm import humanize_error
from core.llm import test_connection as check_connection   # 改别名：名字以 test_ 开头会被 pytest 误收集


def make(**kw) -> ProviderConfig:
    base = dict(id="p", display_name="P", protocol="openai",
                base_url="https://api.example.com/v1", model_id="m")
    base.update(kw)
    return ProviderConfig(**base)


class HttpError(Exception):
    def __init__(self, status, msg="boom"):
        super().__init__(msg)
        self.status_code = status


@pytest.mark.parametrize("status,keyword", [
    (400, "400"),
    (401, "API Key"),
    (402, "余额"),
    (403, "权限"),
    (404, "404"),
    (429, "429"),
    (500, "服务端"),
    (503, "服务端"),
])
def test_status_codes_translated(status, keyword):
    assert keyword in humanize_error(HttpError(status))


def test_timeout_detected_from_text():
    assert "超时" in humanize_error(Exception("Request timed out after 30s"))


def test_connection_error_detected():
    assert "无法连接" in humanize_error(Exception("Connection refused"))


def test_ssl_error_detected():
    assert "证书" in humanize_error(Exception("SSL certificate verify failed"))


def test_fallback_never_leaks_message():
    """兜底分支不能把原始报文吐出来（可能含请求头）。"""
    secret = "sk-SHOULD-NOT-APPEAR-123456"
    msg = humanize_error(Exception(f"provider rejected header Authorization: Bearer {secret}"))
    assert secret not in msg
    assert "Exception" in msg


@pytest.mark.asyncio
async def test_empty_key_short_circuits():
    ok, msg = await check_connection(make(), "   ")
    assert ok is False
    assert "API Key" in msg


@pytest.mark.asyncio
async def test_bad_base_url_reported_without_network():
    ok, msg = await check_connection(make(base_url="ftp://evil"), "sk-x")
    assert ok is False
    assert "http" in msg
