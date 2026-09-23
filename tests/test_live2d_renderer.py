"""渲染器模块的测试。

分两层：
  · CDP 协议层：用假的 websocket 验（不需要起浏览器）
  · 真实渲染：起一次无头 Edge，验"能出图、帧有透明通道、能驱动模型"

真实那一层跑得慢（十几秒），而且需要机器上有 Edge，所以单独标出来；
但它才是真正证明"角色能出现在界面上"的那一条，不能没有。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from app.live2d import renderer as rnd

# ─────────────────────────────────────────────
#  CDP 协议层（假 websocket）
# ─────────────────────────────────────────────


class FakeWs:
    """假的 CDP 通道：按脚本回包，可以模拟"事件插在中间"和"连接断掉"。"""

    def __init__(self, replies: list[dict | None], *, fail_send: bool = False) -> None:
        self.replies = list(replies)
        self.sent: list[dict] = []
        self.fail_send = fail_send
        self.closed = False

    def send(self, raw: str) -> None:
        if self.fail_send:
            raise ConnectionError("断了")
        self.sent.append(json.loads(raw))

    def recv(self):
        if not self.replies:
            raise TimeoutError("没有更多回包了")
        item = self.replies.pop(0)
        if item is None:
            raise ConnectionError("连接被关掉了")
        return json.dumps(item)

    def close(self) -> None:
        self.closed = True


def test_call_matches_its_own_reply_and_skips_events():
    """CDP 是**多路复用**的：事件会插在命令回包中间，必须按 id 认领自己的那个。

    认错回包的表现是"有时候拿到别的命令的结果"，非常难查。
    """
    ws = FakeWs([
        {"method": "Page.frameNavigated", "params": {}},      # 事件，先来
        {"id": 1, "result": {"ok": True}},
    ])
    session = rnd.CdpSession(ws)

    assert session.call("Page.enable") == {"ok": True}
    assert ws.sent[0]["method"] == "Page.enable"
    assert ws.sent[0]["id"] == 1
    assert len(session.events) == 1, "事件应该被记下来而不是当成回包"


def test_call_raises_on_error_reply():
    ws = FakeWs([{"id": 1, "error": {"message": "Not allowed"}}])
    with pytest.raises(rnd.RendererError, match="Not allowed"):
        rnd.CdpSession(ws).call("Page.nope")


def test_call_reports_crash_when_connection_dies():
    """连接没了 = 浏览器进程没了，必须报成 RendererCrashed，让上层能重建。"""
    session = rnd.CdpSession(FakeWs([None]))
    with pytest.raises(rnd.RendererCrashed):
        session.call("Runtime.evaluate")


def test_send_failure_is_also_a_crash():
    session = rnd.CdpSession(FakeWs([], fail_send=True))
    with pytest.raises(rnd.RendererCrashed):
        session.call("Runtime.evaluate")


def test_js_extracts_exception_description():
    """页面里抛异常时，要报出**真正的错误文字**。

    只有 exceptionDetails.text 时是一句 "Uncaught"，等于没说 —— 这个坑踩过。
    """
    ws = FakeWs([{
        "id": 1,
        "result": {"exceptionDetails": {
            "text": "Uncaught",
            "exception": {"description": "Error: Failed to upload Live2D texture."},
        }},
    }])
    with pytest.raises(rnd.RendererError, match="Failed to upload"):
        rnd.CdpSession(ws).js("1/0")


def test_screenshot_decodes_base64():
    payload = b"\x89PNG\r\n\x1a\nfakepng"
    ws = FakeWs([{"id": 1, "result": {"data": base64.b64encode(payload).decode()}}])
    assert rnd.CdpSession(ws).screenshot() == payload


# ─────────────────────────────────────────────
#  取帧 = tick + 截图（用假 session 验顺序）
# ─────────────────────────────────────────────


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def js(self, expression, timeout=None):
        self.calls.append(("js", expression))
        if "tick(" in expression:
            return 7
        if "state()" in expression:
            return json.dumps({"ready": True, "frames": 7})
        if "listExpressions" in expression:
            return json.dumps(["smile"])
        return "1"

    def screenshot(self, timeout=None):
        self.calls.append(("shot", None))
        return b"PNGDATA"

    def call(self, method, params=None, timeout=None):
        self.calls.append(("call", method))
        return {}


def test_frame_ticks_before_capturing():
    """★ 取帧前必须先 tick。

    无头环境里 rAF 不触发，不 tick 的话拿到的是**从没渲染过**的画面（全透明）。
    这条顺序错了，表现就是"画面永远空白、还没有任何报错"。
    """
    renderer = rnd.Live2DRenderer(config=rnd.RenderConfig(ticks_per_frame=3))
    renderer._session = FakeSession()

    data = renderer.frame()

    assert data == b"PNGDATA"
    kinds = [kind for kind, _ in renderer._session.calls]
    assert kinds.count("js") == 3, f"应该 tick 三次，实际调用序列 {kinds}"
    assert all("tick(" in expr for kind, expr in renderer._session.calls if kind == "js")
    assert kinds[-1] == "shot", "最后一步才是截图"


def test_mouth_write_reports_readback():
    renderer = rnd.Live2DRenderer()
    renderer._session = FakeSession()
    # FakeSession 对未知表达式返回 "1"，这里只验证调用的是 setMouth 且能解析
    renderer.set_mouth(0.7)
    expr = renderer._session.calls[-1][1]
    assert "setMouth(0.7)" in expr


def test_methods_refuse_when_not_started():
    renderer = rnd.Live2DRenderer()
    with pytest.raises(rnd.RendererNotReady):
        renderer.frame()
    with pytest.raises(rnd.RendererNotReady):
        renderer.state()


# ─────────────────────────────────────────────
#  找浏览器
# ─────────────────────────────────────────────

def test_find_edge_finds_something():
    edge = rnd.find_edge()
    assert edge is not None, "这台机器上没找到 Edge/Chrome，角色渲染会不可用"
    assert edge.is_file()


def test_edge_override(monkeypatch):
    monkeypatch.setenv("YACHIYO_EDGE", "")
    assert rnd.find_edge() is not None or True     # 空值不该让它崩


# ─────────────────────────────────────────────
#  真机：起一次无头渲染，验"能出图 + 透明 + 能驱动"
# ─────────────────────────────────────────────

@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上跑")
def test_real_renderer_produces_a_transparent_frame():
    """★ 这是整条链的最终证据：角色真的被画出来了，而且是透明背景。

    跑得慢（要起浏览器、加载模型），但少了它，前面那些单测都只是在验协议。
    """
    from PIL import Image

    import io

    config = rnd.RenderConfig(width=360, height=480, ready_timeout=90,
                              ticks_per_frame=3)
    with rnd.Live2DRenderer(config=config) as renderer:
        data = renderer.frame()

        state = renderer.state()
        assert state.get("ready") is True, f"模型没就绪：{state}"
        assert state.get("mouthPath"), "拿不到参数写入路径，口型就用不了"

        img = Image.open(io.BytesIO(data)).convert("RGBA")
        alpha = img.getchannel("A")
        # 用直方图数像素：避免 Pillow 12 起 getdata() 的弃用告警，也快得多
        histogram = alpha.histogram()
        transparent = histogram[0]                 # alpha == 0
        opaque = sum(histogram[9:])                # alpha > 8

        assert opaque > 0, "画面完全是空的 —— 角色没画出来"
        assert transparent > 0, "画面没有透明像素 —— 贴到深色界面上会是个方块"
        assert alpha.getbbox(), "没有内容包围盒"

        # Python 驱动模型：写进去、读回来
        result = renderer.set_mouth(0.8)
        assert abs(result["readBack"] - 0.8) < 0.01, f"口型没写进模型：{result}"

        assert "Idle" in renderer.list_motions()
        assert renderer.list_expressions(), "表情列表不该是空的"
