"""config.py 的测试：读写、原子性、损坏兜底、迁移。

不依赖 pytest 的 tmp_path 夹具 —— 直接用一个工作区内的临时目录，
这样路径完全可控，也方便你手动检查生成出来的 config.json。
"""

import json
import shutil
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import (
    SCHEMA_VERSION,
    AppConfig,
    ConsentRecord,
    ProviderConfig,
    get_provider,
    load_config,
    remove_provider,
    save_config,
    upsert_provider,
)

# 工作区内的测试数据根目录（已在 .gitignore 里）
_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "config"


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    """每个测试用独立的临时数据目录，测完删掉。"""
    root = _TMP_ROOT / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(root))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_missing_file_returns_defaults(isolated_data_dir):
    cfg = load_config()
    assert cfg.providers == []
    assert cfg.schema_version == SCHEMA_VERSION


def test_roundtrip(isolated_data_dir):
    cfg = AppConfig(active_provider="deepseek")
    upsert_provider(cfg, ProviderConfig(id="deepseek", display_name="DeepSeek",
                                        model_id="deepseek-chat"))
    save_config(cfg)

    again = load_config()
    assert again.active_provider == "deepseek"
    assert again.providers[0].model_id == "deepseek-chat"


def test_saved_file_has_no_api_key(isolated_data_dir):
    """最重要的一条：配置文件里绝不能出现密钥。"""
    cfg = AppConfig()
    upsert_provider(cfg, ProviderConfig(id="p", display_name="P", model_id="m"))
    save_config(cfg)

    raw = (isolated_data_dir / "config.json").read_text(encoding="utf-8")
    assert "api_key" not in raw
    assert "apikey" not in raw
    assert "sk-" not in raw


def test_broken_json_falls_back_to_defaults(isolated_data_dir):
    (isolated_data_dir / "config.json").write_text("{ this is not json", encoding="utf-8")
    cfg = load_config()
    assert cfg.providers == []          # 没崩，给了默认值


def test_wrong_shape_falls_back(isolated_data_dir):
    (isolated_data_dir / "config.json").write_text('["a", "b"]', encoding="utf-8")
    assert load_config().providers == []


def test_invalid_field_types_fall_back(isolated_data_dir):
    (isolated_data_dir / "config.json").write_text(
        json.dumps({"schema_version": 1, "history_token_budget": "not-a-number"}),
        encoding="utf-8",
    )
    assert load_config().history_token_budget == 8000     # 默认值


def test_migration_stamps_current_version(isolated_data_dir):
    (isolated_data_dir / "config.json").write_text(
        json.dumps({"providers": [{"id": "p", "display_name": "P", "model_id": "m"}]}),
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.schema_version == SCHEMA_VERSION
    assert cfg.providers[0].id == "p"


def test_save_is_atomic_no_tmp_left_behind(isolated_data_dir):
    save_config(AppConfig())
    assert list(isolated_data_dir.glob("*.tmp")) == []
    assert (isolated_data_dir / "config.json").exists()


def test_save_overwrites_existing(isolated_data_dir):
    save_config(AppConfig(active_provider="a"))
    save_config(AppConfig(active_provider="b"))
    assert load_config().active_provider == "b"


def test_get_provider_by_active_and_by_id():
    cfg = AppConfig(active_provider="a")
    upsert_provider(cfg, ProviderConfig(id="a", display_name="A", model_id="m"))
    upsert_provider(cfg, ProviderConfig(id="b", display_name="B", model_id="m"))

    assert get_provider(cfg).id == "a"
    assert get_provider(cfg, "b").id == "b"
    assert get_provider(cfg, "nope") is None


def test_upsert_replaces_not_duplicates():
    cfg = AppConfig()
    upsert_provider(cfg, ProviderConfig(id="a", display_name="A", model_id="m1"))
    upsert_provider(cfg, ProviderConfig(id="a", display_name="A2", model_id="m2"))

    assert len(cfg.providers) == 1
    assert cfg.providers[0].model_id == "m2"


def test_remove_provider_reassigns_active():
    cfg = AppConfig(active_provider="a")
    upsert_provider(cfg, ProviderConfig(id="a", display_name="A", model_id="m"))
    upsert_provider(cfg, ProviderConfig(id="b", display_name="B", model_id="m"))

    remove_provider(cfg, "a")
    assert cfg.active_provider == "b"


def test_remove_last_provider_clears_active():
    cfg = AppConfig(active_provider="a")
    upsert_provider(cfg, ProviderConfig(id="a", display_name="A", model_id="m"))
    remove_provider(cfg, "a")
    assert cfg.active_provider == ""


def test_unknown_fields_are_ignored():
    """用户手改文件加错字，不该让整个配置失效。"""
    cfg = AppConfig.model_validate({"schema_version": 1, "som_typo": 123})
    assert cfg.schema_version == 1


# ─────────────────────────── 同意记录 ───────────────────────────


def test_consent_defaults_to_never_agreed():
    """没写过就是「从没同意过」：version=0、时间为空。"""
    cfg = AppConfig()
    assert cfg.consent.version == 0
    assert cfg.consent.at == ""


def test_consent_roundtrips(isolated_data_dir):
    cfg = AppConfig()
    cfg.consent = ConsentRecord(version=3, at="2026-09-25T12:30:00+08:00")

    save_config(cfg)
    again = load_config()

    assert again.consent.version == 3
    assert again.consent.at == "2026-09-25T12:30:00+08:00"


def test_old_config_without_consent_loads_as_never_agreed(isolated_data_dir):
    """老版本的 config.json 里没有 consent，升上来要当成「没同意过」。"""
    (isolated_data_dir / "config.json").write_text(
        json.dumps({"schema_version": 1, "active_provider": "deepseek"}),
        encoding="utf-8",
    )

    cfg = load_config()
    assert cfg.consent.version == 0
    assert cfg.active_provider == "deepseek"        # 老字段照常读出来
    assert cfg.schema_version == SCHEMA_VERSION     # 迁移后写的是新版本号


def test_consent_version_must_be_a_number():
    """手改文件把版本号写成文字，要报错而不是悄悄吞掉。"""
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"consent": {"version": "第一版"}})
