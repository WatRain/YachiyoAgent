"""本地 Live2D 服务的测试。

这个服务是"渲染页 ↔ Python"之间唯一的通道，它出错的表现是
"页面一片空白、控制台还没报错"，很难查 —— 所以路由和穿越防护都要盯死。

测试用一个假的模型目录（不碰你真实的模型，几十 MB 读起来太慢）。
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

from app.live2d import server as live2d_server

# 用项目内的 .ptmp/ 而不是 pytest 的 tmp_path：
# tmp_path 会落到 %TEMP%，在受限环境里不可写（WinError 5）。项目里别的测试也这么做。
_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "live2d-server"


@pytest.fixture
def workdir():
    d = _TMP_ROOT / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_model(workdir: Path) -> Path:
    """一个最小可用形状的模型目录。"""
    d = workdir / "models" / "假模型"
    (d / "假模型.8192").mkdir(parents=True)
    (d / "假模型.model3.json").write_text(
        json.dumps({"Version": 3, "FileReferences": {"Moc": "假模型.moc3"}}),
        encoding="utf-8",
    )
    (d / "假模型.moc3").write_bytes(b"\x00" * 64)
    (d / "假模型.8192" / "texture_00.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return d


@pytest.fixture
def running_server(fake_model: Path):
    srv = live2d_server.Live2DServer(fake_model)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


# ─────────────────────────────────────────────
#  找模型 / 拼 URL
# ─────────────────────────────────────────────

def test_find_model_locates_the_real_one():
    """项目里的 models/ 下面应该能找到那个模型（用真实目录验证一次）。"""
    found = live2d_server.find_model()
    assert found is not None, "models/ 下面没找到带 *.model3.json 的目录"
    assert live2d_server.model_json_in(found) is not None


def test_find_model_ignores_dirs_without_definition(workdir: Path):
    (workdir / "models" / "空目录").mkdir(parents=True)
    assert live2d_server.find_model(workdir) is None


def test_model_url_is_percent_encoded(fake_model: Path):
    url = live2d_server.model_url(fake_model)
    assert url.startswith("/model/")
    # 中文文件名必须编码，否则 fetch 会挂
    assert "%" in url
    assert urllib.parse.unquote(url[len("/model/"):]) == "假模型.model3.json"


# ─────────────────────────────────────────────
#  路由
# ─────────────────────────────────────────────

def test_page_is_served_at_root(running_server):
    status, headers, body = get(running_server.page_url)
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    text = body.decode("utf-8")
    assert "window.yachiyo" in text, "主页不像我们的渲染页"
    # 页面本身不该被缓存，否则改完刷新看不到效果
    assert "no-store" in headers["Cache-Control"]


def test_vendored_scripts_are_served(running_server):
    for name in ("pixi.min.mjs", "pixi-live2d-engine.cubism.es.js"):
        status, headers, body = get(running_server.page_url + f"static/vendor/{name}")
        assert status == 200, f"{name} 没发出去"
        assert len(body) > 10_000, f"{name} 内容太短，像是发错了文件"
        assert "javascript" in headers["Content-Type"]


def test_esm_files_get_a_javascript_mime(running_server):
    """★ .mjs 的 MIME 必须是 javascript。

    ES module 的 MIME 不对，浏览器会**直接拒绝执行**，而它报的错
    （"Expected a JavaScript module script but the server responded with a
    MIME type of ..."）看起来很像"文件没发出去"，很容易误诊。
    """
    status, headers, _body = get(running_server.page_url + "static/vendor/pixi.min.mjs")
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")


def test_page_references_the_vendored_files_it_needs(running_server):
    """页面里引用的 /static/ 路径必须真的取得到 —— 少一个文件就是一片空白。"""
    import re

    _status, _headers, body = get(running_server.page_url)
    html = body.decode("utf-8")
    referenced = set(re.findall(r'"(/static/[^"]+)"', html))
    assert referenced, "页面里没有任何 /static/ 引用？"
    for url in referenced:
        status, _h, _b = get(running_server.page_url.rstrip("/") + url)
        assert status == 200, f"页面引用的 {url} 取不到"


def test_model_files_are_served_with_chinese_names(running_server, fake_model: Path):
    """★ 模型文件名是中文的（八千代辉夜姬.model3.json），编码必须对。"""
    quoted = urllib.parse.quote("假模型.model3.json")
    status, _headers, body = get(running_server.page_url + f"model/{quoted}")
    assert status == 200
    assert json.loads(body)["Version"] == 3

    quoted_png = urllib.parse.quote("假模型.8192/texture_00.png")
    status, headers, body = get(running_server.page_url + f"model/{quoted_png}")
    assert status == 200
    assert headers["Content-Type"] == "image/png"
    assert body.startswith(b"\x89PNG")


def test_unknown_path_is_404(running_server):
    status, _headers, _body = get(running_server.page_url + "nope")
    assert status == 404


# ─────────────────────────────────────────────
#  安全
# ─────────────────────────────────────────────

@pytest.mark.parametrize("attack", [
    "static/../pet.html",
    "static/../../app/main.py",
    "static/..%2f..%2fapp%2fmain.py",
    "model/../../../app/main.py",
    "model/..%5c..%5capp%5cmain.py",
])
def test_path_traversal_is_blocked(running_server, attack: str):
    """★ 这个服务只该发它那两个根目录里的东西。

    模型是用户自己电脑上的文件，一个能穿越的本地服务等于把整个磁盘
    暴露给任何能访问这个端口的东西。
    """
    status, _headers, body = get(running_server.page_url + attack)
    assert status == 404, f"{attack} 居然通过了"
    assert b"import" not in body[:200], "好像真的把源码发出去了"


def test_only_listens_on_loopback(running_server):
    """必须只绑 127.0.0.1，不能是 0.0.0.0（那会暴露到局域网）。"""
    host = running_server._httpd.server_address[0]
    assert host == "127.0.0.1"
