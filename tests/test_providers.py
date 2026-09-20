"""providers.py 的测试：校验与翻译。

这些不依赖 Flet 运行时，也不发网络请求，所以永远是绿的。
"""

import pytest

from core.config import ProviderConfig
from core.providers import (
    default_config,
    looks_like_local,
    make_provider_id,
    resolve_model_string,
    seed_builtins_if_empty,
    to_litellm_kwargs,
    validate_base_url,
    validate_provider,
)


def make(**kw) -> ProviderConfig:
    base = dict(id="p", display_name="P", protocol="openai",
                base_url="https://api.example.com/v1", model_id="some-model")
    base.update(kw)
    return ProviderConfig(**base)


# ------------------------------------------------------------ base_url 校验

def test_empty_base_url_is_allowed():
    assert validate_base_url("") == ""
    assert validate_base_url("   ") == ""


def test_trailing_slash_removed():
    assert validate_base_url("https://x.com/v1/") == "https://x.com/v1"


@pytest.mark.parametrize("bad", [
    "file:///etc/passwd",
    "ftp://x.com",
    "data:text/plain,hi",
    "javascript:alert(1)",
])
def test_rejects_non_http_schemes(bad):
    with pytest.raises(ValueError):
        validate_base_url(bad)


def test_rejects_credentials_in_url():
    with pytest.raises(ValueError):
        validate_base_url("https://user:pass@api.example.com/v1")


def test_rejects_url_without_host():
    with pytest.raises(ValueError):
        validate_base_url("https://")


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434/v1", True),
    ("http://127.0.0.1:8000/v1", True),
    ("http://192.168.1.5:8080/v1", True),
    ("http://10.0.0.7/v1", True),
    ("https://api.deepseek.com/v1", False),
])
def test_local_detection(url, expected):
    assert looks_like_local(url) is expected


# ------------------------------------------------------------ 模型字符串

def test_model_gets_protocol_prefix():
    assert resolve_model_string(make(model_id="deepseek-chat")) == "openai/deepseek-chat"


def test_explicit_prefix_is_respected():
    assert resolve_model_string(make(model_id="openai/gpt-4o-mini")) == "openai/gpt-4o-mini"


def test_custom_protocol_keeps_raw_string():
    p = make(protocol="custom", model_id="openrouter/x/y", base_url="")
    assert resolve_model_string(p) == "openrouter/x/y"


def test_anthropic_prefix():
    p = make(protocol="anthropic", base_url="", model_id="claude-3-5-haiku-latest")
    assert resolve_model_string(p) == "anthropic/claude-3-5-haiku-latest"


# ------------------------------------------------------------ LiteLLM 参数

def test_kwargs_basic():
    kw = to_litellm_kwargs(make(model_id="deepseek-chat"), "sk-test")
    assert kw["model"] == "openai/deepseek-chat"
    assert kw["api_key"] == "sk-test"
    assert kw["api_base"] == "https://api.example.com/v1"


def test_kwargs_omits_empty_base_url():
    kw = to_litellm_kwargs(make(base_url=""), "sk-test")
    assert "api_base" not in kw


def test_kwargs_includes_extra_headers():
    p = make(extra_headers={"X-Title": "Yachiyo"})
    assert to_litellm_kwargs(p, "k")["extra_headers"] == {"X-Title": "Yachiyo"}


def test_kwargs_propagates_bad_base_url():
    with pytest.raises(ValueError):
        to_litellm_kwargs(make(base_url="file:///tmp"), "k")


# ------------------------------------------------------------ validate_provider

def test_validate_provider_clean():
    assert validate_provider(make()) == []


def test_validate_provider_collects_all_problems():
    problems = validate_provider(make(model_id="", display_name="", base_url="ftp://x"))
    joined = " / ".join(problems)
    assert "模型 ID" in joined
    assert "显示名" in joined
    assert "http" in joined


# ------------------------------------------------------------ id 生成

def test_make_provider_id_slugifies():
    assert make_provider_id("My Relay Station", []) == "my_relay_station"


def test_make_provider_id_handles_non_ascii():
    # 中文名 slug 化后为空 → 回退到 provider
    assert make_provider_id("我的中转站", []) == "provider"


def test_make_provider_id_avoids_collision():
    pid = make_provider_id("test", ["test"])
    assert pid != "test"
    assert pid.startswith("test_")


# ------------------------------------------------------------ 预设

def test_default_config_seeds_builtins():
    cfg = default_config()
    ids = [p.id for p in cfg.providers]
    assert "deepseek" in ids
    assert cfg.active_provider == ids[0]


def test_default_config_is_deep_copied():
    a = default_config()
    b = default_config()
    a.providers[0].display_name = "改过了"
    assert b.providers[0].display_name != "改过了"


def test_seed_builtins_only_when_empty():
    cfg = default_config()
    assert seed_builtins_if_empty(cfg) is False

    cfg.providers = []
    assert seed_builtins_if_empty(cfg) is True
    assert len(cfg.providers) > 0
    assert cfg.active_provider == cfg.providers[0].id
