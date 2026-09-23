"""打包配置的回归测试。

为什么要有这个文件：打包专有的坑（模块没被 StaticFiles 收进来、路径写死到
只读目录、PyInstaller 漏收插件命名空间包）在开发期一点症状都没有，只有真跑
打包版才炸 —— 而打包一次要几分钟，不适合放进单元测试。这里退而求其次，
把"配置里必须有的那几样"钉住，免得以后清理 spec 时手滑删掉。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.paths import PROJECT_ROOT

SPEC = PROJECT_ROOT / "packaging" / "backend.spec"
SPEC_TEXT = SPEC.read_text(encoding="utf-8") if SPEC.exists() else ""


@pytest.mark.skipif(not SPEC.exists(), reason="没有打包规格（跑测试的环境不是仓库根）")
def test_spec_collects_tiktoken_plugin_namespace_package() -> None:
    """tiktoken_ext 必须显式收。

    它是 tiktoken 的"插件命名空间包"，tiktoken 靠 pkgutil 扫描它注册编码；
    PyInstaller 不会自己发现它，漏了就是打包版一说话就
    `ValueError: Unknown encoding cl100k_base. Plugins found: []`（踩过一次）。
    """
    assert "tiktoken_ext" in SPEC_TEXT
    assert "collect_submodules" in SPEC_TEXT


@pytest.mark.skipif(not SPEC.exists(), reason="没有打包规格")
def test_spec_ships_renderer_and_live2d_assets() -> None:
    assert "desktop/renderer" in SPEC_TEXT
    assert "app/assets/live2d" in SPEC_TEXT or "app\" / \"assets\" / \"live2d" in SPEC_TEXT
    assert "prompt.md" in SPEC_TEXT


@pytest.mark.skipif(not SPEC.exists(), reason="没有打包规格")
def test_spec_carries_the_tiktoken_cache_when_it_exists() -> None:
    cache = PROJECT_ROOT / "packaging" / "tiktoken_cache"
    assert "tiktoken_cache" in SPEC_TEXT
    if cache.is_dir() and any(cache.iterdir()):
        # 有词表就必须打进包里（否则用户第一次聊天得联网下）
        assert "datas.append" in SPEC_TEXT


def test_bundled_tiktoken_cache_points_the_env_var_only_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """backend/__main__ 只在真的带着词表时才设 TIKTOKEN_CACHE_DIR。

    开发期（项目根下没有 tiktoken_cache/）不能设，否则 tiktoken 会去一个不存在的
    目录里找词表、找不到又下不进去。
    """
    from backend import __main__ as backend_main

    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)

    # 1) 有词表：指过去
    cache = tmp_path / "tiktoken_cache"
    cache.mkdir()
    (cache / "deadbeef").write_bytes(b"x")
    monkeypatch.setattr(backend_main, "resource_path", lambda rel: cache)
    backend_main._use_bundled_tiktoken_cache()
    assert os.environ["TIKTOKEN_CACHE_DIR"] == str(cache)

    # 2) 目录在但是空的：当作没有
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)
    empty = tmp_path / "empty_tiktoken_cache"
    empty.mkdir()
    monkeypatch.setattr(backend_main, "resource_path", lambda rel: empty)
    backend_main._use_bundled_tiktoken_cache()
    assert "TIKTOKEN_CACHE_DIR" not in os.environ

    # 3) 目录不存在：当作没有
    monkeypatch.setattr(backend_main, "resource_path", lambda rel: tmp_path / "nope")
    backend_main._use_bundled_tiktoken_cache()
    assert "TIKTOKEN_CACHE_DIR" not in os.environ


def test_log_dir_is_writable_when_packaged() -> None:
    """打包后 __dirname 在只读的 app.asar 里 —— main.js 必须先判断是否打包。

    这条只能读源码断言（真跑打包版太重），但足以拦住"又写回 __dirname"这种回退。
    """
    main_js = PROJECT_ROOT / "desktop" / "main.js"
    if not main_js.exists():
        pytest.skip("没有 Electron 壳")
    text = main_js.read_text(encoding="utf-8")
    assert "app.isPackaged" in text
    assert "getPath(\"userData\")" in text or "getPath('userData')" in text
