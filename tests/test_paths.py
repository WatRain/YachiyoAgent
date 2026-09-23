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


def test_model_roots_puts_the_user_overrides_first(monkeypatch, workdir: Path):
    """寻找顺序：显式指定的 → 数据目录 → 内置。顺序错了用户换不了模型。"""
    monkeypatch.setenv("YACHIYO_MODEL_DIR", str(workdir / "mine"))
    monkeypatch.setenv("YACHIYO_DATA_DIR", str(workdir / "data"))
    monkeypatch.setattr(core_paths, "resource_path", lambda rel: workdir / "res" / rel)

    roots = core_paths.model_roots()

    assert roots[0] == workdir / "mine"
    assert roots[1] == workdir / "data" / "models"
    assert roots[2] == workdir / "res" / "models"


def test_model_roots_has_no_duplicates_without_an_override(monkeypatch, workdir: Path):
    monkeypatch.delenv("YACHIYO_MODEL_DIR", raising=False)
    monkeypatch.setenv("YACHIYO_DATA_DIR", str(workdir / "data"))
    monkeypatch.setattr(core_paths, "resource_path", lambda rel: workdir / "data" / rel)
    assert core_paths.model_roots() == [workdir / "data" / "models"]


def test_find_model_looks_into_the_data_dir(monkeypatch, workdir: Path):
    """打包后用户把模型丢进 <数据目录>/models —— 这条路径必须真的能找到模型。"""
    data = workdir / "data"
    model = data / "models" / "someone" / "someone.model3.json"
    model.parent.mkdir(parents=True)
    model.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("YACHIYO_DATA_DIR", str(data))
    monkeypatch.delenv("YACHIYO_MODEL_DIR", raising=False)
    monkeypatch.setattr(core_paths, "resource_path", lambda rel: workdir / "res" / rel)

    assert live2d_server.find_model() == model.parent


def test_find_model_skips_roots_without_a_model(monkeypatch, workdir: Path):
    """第一个目录是空的时不能停在那里，要继续往下找。"""
    (workdir / "empty").mkdir()
    bundled = workdir / "res" / "models" / "builtin"
    bundled.mkdir(parents=True)
    (bundled / "builtin.model3.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("YACHIYO_MODEL_DIR", str(workdir / "empty"))
    monkeypatch.setenv("YACHIYO_DATA_DIR", str(workdir / "data"))
    monkeypatch.setattr(core_paths, "resource_path", lambda rel: workdir / "res" / rel)

    assert live2d_server.find_model() == bundled
