"""贴图压缩 与 缓存覆盖 的测试。

背景：这套模型带 8192×8192 的贴图，实测会把无头 Edge 的渲染进程直接撑死
（没有任何 JS 报错，进程就没了）。所以贴图必须先缩到能跑的大小。
这一步出错的后果是"角色出不来"，所以尺寸、跳过逻辑、覆盖顺序都要盯住。
"""

from __future__ import annotations

import json
import shutil
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest
from PIL import Image

from app.live2d import textures as tex

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "live2d-tex"


@pytest.fixture
def workdir():
    d = _TMP_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make_model(workdir: Path, *, texture_size: int) -> Path:
    """造一个带一张贴图的模型目录。"""
    model = workdir / "models" / "测试模型"
    (model / "贴图").mkdir(parents=True)
    (model / "测试模型.model3.json").write_text(json.dumps({
        "Version": 3,
        "FileReferences": {
            "Moc": "测试模型.moc3",
            "Textures": ["贴图/texture_00.png"],
        },
    }, ensure_ascii=False), encoding="utf-8")
    (model / "测试模型.moc3").write_bytes(b"\x00" * 32)
    Image.new("RGBA", (texture_size, texture_size), (200, 30, 40, 255)).save(
        model / "贴图" / "texture_00.png")
    return model


# ─────────────────────────────────────────────
#  清单 / 计划
# ─────────────────────────────────────────────

def test_textures_come_from_the_model_definition(workdir: Path):
    """贴图清单要读 model3.json，不能"把目录里所有 png 都缩一遍"。

    模型目录里还有头像这种跟渲染无关的图，不该动它。
    """
    model = make_model(workdir, texture_size=64)
    Image.new("RGBA", (32, 32)).save(model / "头像.png")

    assert tex.model_textures(model) == ["贴图/texture_00.png"]


def test_small_textures_are_left_alone(workdir: Path):
    model = make_model(workdir, texture_size=512)
    assert tex.plan(model, max_size=2048, base=workdir) == []


def test_big_textures_are_planned_for_resize(workdir: Path):
    model = make_model(workdir, texture_size=4096)
    jobs = tex.plan(model, max_size=2048, base=workdir)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.width == 4096 and job.height == 4096
    assert job.target == tex.cache_root(model, base=workdir) / "贴图/texture_00.png"


# ─────────────────────────────────────────────
#  真正压缩
# ─────────────────────────────────────────────

def test_resize_actually_shrinks_and_keeps_aspect(workdir: Path):
    model = workdir / "models" / "宽模型"
    (model / "贴图").mkdir(parents=True)
    (model / "宽模型.model3.json").write_text(json.dumps({
        "FileReferences": {"Textures": ["贴图/wide.png"]}}, ensure_ascii=False),
        encoding="utf-8")
    Image.new("RGBA", (8192, 4096), (10, 200, 30, 255)).save(model / "贴图" / "wide.png")

    made = tex.ensure_texture_cache(model, max_size=2048, base=workdir)
    assert made == 1, "该缩的没缩"

    out = tex.cache_root(model, base=workdir) / "贴图/wide.png"
    with Image.open(out) as img:
        assert max(img.size) == 2048, f"长边应该被缩到 2048，实际 {img.size}"
        # 等比：8192x4096 → 2048x1024
        assert img.size == (2048, 1024), f"比例没保持：{img.size}"
    assert out.stat().st_size < (model / "贴图" / "wide.png").stat().st_size


def test_second_run_is_a_no_op(workdir: Path):
    """第二次启动不该再缩一遍 —— 缓存就是为这个存在的。"""
    model = make_model(workdir, texture_size=4096)
    assert tex.ensure_texture_cache(model, max_size=2048, base=workdir) == 1
    assert tex.ensure_texture_cache(model, max_size=2048, base=workdir) == 0


def test_cache_overlay_only_when_cache_exists(workdir: Path):
    model = make_model(workdir, texture_size=4096)
    assert tex.cache_overlay(model, base=workdir) is None
    tex.ensure_texture_cache(model, max_size=2048, base=workdir)
    overlay = tex.cache_overlay(model, base=workdir)
    assert overlay is not None and overlay.is_dir()


def test_source_files_are_never_modified(workdir: Path):
    """★ 用户给的模型是只读的：我们只写缓存目录。"""
    model = make_model(workdir, texture_size=4096)
    src = model / "贴图" / "texture_00.png"
    before = (src.stat().st_size, src.stat().st_mtime, src.read_bytes()[:64])

    tex.ensure_texture_cache(model, max_size=2048, base=workdir)

    assert (src.stat().st_size, src.stat().st_mtime, src.read_bytes()[:64]) == before


# ─────────────────────────────────────────────
#  服务层的覆盖顺序
# ─────────────────────────────────────────────

def test_server_prefers_the_texture_cache(workdir: Path):
    """缓存里有的，服务就发缓存那一份（小图），而不是原始大图。"""
    from app.live2d.server import Live2DServer

    model = make_model(workdir, texture_size=4096)
    tex.ensure_texture_cache(model, max_size=2048, base=workdir)
    overlay = tex.cache_overlay(model, base=workdir)

    srv = Live2DServer(model, cache_dir=overlay)
    srv.start()
    try:
        url = srv.page_url + "model/" + urllib.parse.quote("贴图/texture_00.png")
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = resp.read()
        # 原始那张是 4096 纯色 PNG，缩过的那张明显更小
        original = (model / "贴图" / "texture_00.png").read_bytes()
        assert len(data) < len(original), "发出来的还是原始大图，缓存没生效"
    finally:
        srv.stop()


def test_server_falls_back_to_model_dir_for_other_files(workdir: Path):
    """moc3、动作这些缓存里没有，仍然要能从模型目录取到。"""
    from app.live2d.server import Live2DServer

    model = make_model(workdir, texture_size=512)
    srv = Live2DServer(model, cache_dir=None)
    srv.start()
    try:
        url = srv.page_url + "model/" + urllib.parse.quote("测试模型.moc3")
        with urllib.request.urlopen(url, timeout=10) as resp:
            assert resp.status == 200
            assert resp.read() == b"\x00" * 32
    finally:
        srv.stop()
