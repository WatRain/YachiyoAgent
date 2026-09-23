"""core/paths.py 的路径规则测试。

为什么值得单独测：这些规则决定"数据写到哪里、模型从哪里找"，
打包后（sys.frozen）路径全变了 —— 这类问题在开发机上永远复现不出来，
只能靠测试把这层约定钉住。改错的表现是"安装后配置存不住 / 模型找不到"。

用项目内的 .ptmp/ 而不是 pytest 的 tmp_path：%TEMP% 在受限环境里不可写。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from app.live2d import server as live2d_server
from core import paths as core_paths

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "paths"


@pytest.fixture
def workdir():
    d = _TMP_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_data_dir_honours_the_env_var(monkeypatch, workdir: Path):
    monkeypatch.setenv("YACHIYO_DATA_DIR", str(workdir))
    assert core_paths.data_dir() == workdir


def test_packaged_build_writes_next_to_appdata_not_the_install_dir(monkeypatch, workdir: Path):
    """打包后必须写 %APPDATA%：安装目录在 Program Files 下不可写。"""
    monkeypatch.delenv("YACHIYO_DATA_DIR", raising=False)
    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)
    monkeypatch.setenv("APPDATA", str(workdir))
    monkeypatch.setattr(core_paths, "is_frozen", lambda: True)

    got = core_paths.data_dir()

    assert got == workdir / "YachiyoAgent" / "data"
    assert got.is_dir(), "目录应该被建出来，不然第一次写文件就失败"


def test_packaged_build_without_appdata_falls_back_to_devdata(monkeypatch, workdir: Path):
    monkeypatch.delenv("YACHIYO_DATA_DIR", raising=False)
    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(core_paths, "is_frozen", lambda: True)
    assert core_paths.data_dir() == core_paths.PROJECT_ROOT / ".devdata"


def test_model_roots_is_only_the_builtin_model(monkeypatch, workdir: Path):
    """没有显式指定时，候选里只有内置模型 —— 不给用户留放模型的口子。"""
    monkeypatch.delenv("YACHIYO_MODEL_DIR", raising=False)
    monkeypatch.delenv("YACHIYO_ASSETS_DIR", raising=False)
    monkeypatch.setenv("YACHIYO_DATA_DIR", str(workdir / "data"))
    monkeypatch.setattr(core_paths, "resource_path", lambda rel: workdir / "res" / rel)

    assert core_paths.model_roots() == [workdir / "res" / "app" / "assets" / "live2d" / "models"]


def test_model_roots_puts_the_override_first(monkeypatch, workdir: Path):
    monkeypatch.setenv("YACHIYO_MODEL_DIR", str(workdir / "mine"))
    monkeypatch.delenv("YACHIYO_ASSETS_DIR", raising=False)
    monkeypatch.setattr(core_paths, "resource_path", lambda rel: workdir / "res" / rel)

    roots = core_paths.model_roots()

    assert roots[0] == workdir / "mine"
    assert roots[1] == workdir / "res" / "app" / "assets" / "live2d" / "models"


def test_model_roots_has_no_duplicates_when_the_override_is_the_builtin(monkeypatch, workdir: Path):
    monkeypatch.setenv("YACHIYO_ASSETS_DIR", str(workdir / "assets"))
    monkeypatch.setenv("YACHIYO_MODEL_DIR", str(workdir / "assets" / "models"))
    assert core_paths.model_roots() == [workdir / "assets" / "models"]


def test_a_model_dropped_in_the_data_dir_is_ignored(monkeypatch, workdir: Path):
    """以前允许用户把模型放进 <数据目录>/models —— 现在故意不看那里。

    这条测试是"不要给用户留自定义模型接口"的守门人：哪天有人把那一档加回来，它会红。
    """
    data = workdir / "data"
    model = data / "models" / "someone" / "someone.model3.json"
    model.parent.mkdir(parents=True)
    model.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("YACHIYO_DATA_DIR", str(data))
    monkeypatch.delenv("YACHIYO_MODEL_DIR", raising=False)
    monkeypatch.delenv("YACHIYO_ASSETS_DIR", raising=False)
    monkeypatch.setattr(core_paths, "resource_path", lambda rel: workdir / "res" / rel)

    assert live2d_server.find_model() is None


def test_the_builtin_model_ships_inside_assets(monkeypatch, workdir: Path):
    """内置模型随程序走：`app/assets/live2d/models/`，所以打包后开箱就有角色。

    这条测试在真实仓库上跑（不打桩路径）：用户的数据目录是空的，也不设
    `YACHIYO_MODEL_DIR`，此时 `find_model()` 必须落到内置模型上。
    """
    monkeypatch.delenv("YACHIYO_MODEL_DIR", raising=False)
    monkeypatch.delenv("YACHIYO_ASSETS_DIR", raising=False)
    monkeypatch.delenv("FLET_APP_STORAGE_DATA", raising=False)
    monkeypatch.setenv("YACHIYO_DATA_DIR", str(workdir))

    builtin = core_paths.assets_dir() / "models"
    found = live2d_server.find_model()

    assert builtin.is_dir(), "内置模型应该放在 app/assets/live2d/models/ 下"
    assert found is not None, "有内置模型却找不到，说明 model_roots() 漏了 assets 那一档"
    assert builtin in found.parents, f"找到的是 {found}，不在内置模型目录里"
    assert list(found.glob("*.model3.json")), "内置模型目录里必须有 *.model3.json"


def test_find_model_skips_an_empty_override_root(monkeypatch, workdir: Path):
    """第一个目录是空的时不能停在那里，要继续往下找到内置模型。"""
    (workdir / "empty").mkdir()
    builtin = workdir / "res" / "app" / "assets" / "live2d" / "models" / "builtin"
    builtin.mkdir(parents=True)
    (builtin / "builtin.model3.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("YACHIYO_MODEL_DIR", str(workdir / "empty"))
    monkeypatch.delenv("YACHIYO_ASSETS_DIR", raising=False)
    monkeypatch.setattr(core_paths, "resource_path", lambda rel: workdir / "res" / rel)

    assert live2d_server.find_model() == builtin

