"""用无头 Edge 渲染 Live2D，并通过 CDP 取帧、驱动模型。

## 这条链是怎么定下来的（每一步都有实测依据）

  1. Flet 1.0 里嵌不进网页：没有 WebView 控件，预编译客户端也没有 webview 插件，
     本机没有 Flutter/Dart 工具链。所以只能"浏览器渲染 → Python 取帧"。

  2. **帧必须由 Python 驱动**：无头 Edge 里 `requestAnimationFrame` 基本不触发，
     PIXI 的 ticker 整个生命周期只跑 1 次 —— 引擎从没画过一帧，截图永远全透明。
     所以每取一帧前先调 `window.yachiyo.tick()`（页面里那个函数做的就是
     "更新模型 + 渲染"）。这样帧率也可控，不白烧 CPU。

  3. **绝对不要传 `--default-background-color=00000000`**：实测它会让渲染进程
     直接消失（HTML 侧没有任何报错）。透明背景只用 CDP 的
     `Emulation.setDefaultBackgroundColorOverride`，那个是好的。

  4. 页面加载顺序：Cubism Core（普通脚本，官方 CDN）**先于**引擎（动态 import）。
     引擎在模块求值时就检查 Core 在不在。

  5. 模型是 Cubism **5**（moc3 头 `MOC3\x05`），所以用支持 2/3/4/5 的
     `untitled-pixi-live2d-engine`（MIT），而不是只带 Cubism 4 框架的
     `pixi-live2d-display@0.4.0`（那个会让渲染进程硬崩）。

## 线程约定

  这个类是**同步**的，而且不是线程安全的：请在同一个工作线程里用。
  （界面那边用队列把帧交给 Flet 的事件循环，不要跨线程直接碰它。）
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.live2d.server import Live2DServer, find_model, model_url
from app.live2d.textures import cache_overlay, ensure_texture_cache

log = logging.getLogger(__name__)


class RendererError(RuntimeError):
    """渲染器起不来或中途坏了。"""


class RendererCrashed(RendererError):
    """浏览器进程没了 —— 调用方可以重建一个。"""


class RendererNotReady(RendererError):
    """模型还没就绪。"""


@dataclass
class RenderConfig:
    width: int = 420
    height: int = 560
    edge_path: Path | None = None
    ready_timeout: float = 60.0     # 等模型加载好
    call_timeout: float = 30.0      # 单条 CDP 命令
    ticks_per_frame: int = 2        # 每取一帧推进几次模型更新
    tick_ms: int = 33               # 每次推进多少毫秒（33 ≈ 30fps）
    texture_max_size: int = 2048    # 贴图压缩上限（8192 会撑爆/极慢）
    extra_edge_flags: list[str] = field(default_factory=list)


def find_edge() -> Path | None:
    """找 Edge 的可执行文件。找不到就返回 None（调用方负责给用户一个说法）。"""
    override = os.environ.get("YACHIYO_EDGE")
    if override and Path(override).is_file():
        return Path(override)

    try:
        import winreg  # 只在 Windows 上有
    except ImportError:
        winreg = None

    if winreg is not None:
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    path, _ = winreg.QueryValueEx(handle, "")
                    if path and Path(path).is_file():
                        return Path(path)
            except OSError:
                pass

    for candidate in (
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Google/Chrome/Application/chrome.exe",
    ):
        if candidate.is_file():
            return candidate
    return None


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CdpSession:
    """最小可用的 CDP 客户端：发命令、等自己的回包，把事件丢掉。

    抽成独立类是为了**可测**：测试里塞一个假 websocket 进来就能验协议部分，
    不需要真的起浏览器。
    """

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next_id = 0
        self.events: list[dict] = []

    def call(self, method: str, params: dict | None = None, *, timeout: float = 30.0):
        self._next_id += 1
        want = self._next_id
        try:
            self._ws.send(json.dumps({"id": want, "method": method, "params": params or {}}))
        except Exception as exc:                     # 连接没了 = 浏览器没了
            raise RendererCrashed(f"发送 {method} 时连接已断开：{type(exc).__name__}") from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv()
            except Exception as exc:
                raise RendererCrashed(f"等 {method} 回包时连接断开：{type(exc).__name__}") from exc
            message = json.loads(raw)
            if message.get("id") == want:
                if "error" in message:
                    raise RendererError(f"{method} 失败：{message['error']}")
                return message.get("result", {})
            self.events.append(message)              # 不是我们的回包，先记下
        raise RendererError(f"{method} 超时（{timeout}s）")

    def js(self, expression: str, *, timeout: float = 30.0):
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        }, timeout=timeout)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text")
            raise RendererError(f"页面里执行出错：{text}")
        return result.get("result", {}).get("value")

    def screenshot(self, *, timeout: float = 30.0) -> bytes:
        result = self.call("Page.captureScreenshot",
                           {"format": "png", "captureBeyondViewport": False},
                           timeout=timeout)
        return base64.b64decode(result["data"])


class Live2DRenderer:
    """起服务 → 起无头 Edge → 连 CDP → 取帧 / 驱动模型。"""

    def __init__(self, model_dir: Path | None = None,
                 config: RenderConfig | None = None) -> None:
        self.config = config or RenderConfig()
        self.model_dir = model_dir or find_model()
        self._server: Live2DServer | None = None
        self._proc: subprocess.Popen | None = None
        self._profile: Path | None = None
        self._session: CdpSession | None = None

    # ---------- 生命周期 ----------

    @property
    def running(self) -> bool:
        return self._session is not None and self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        if self.model_dir is None:
            raise RendererError("没找到 Live2D 模型（models/ 下面没有 *.model3.json）")
        edge = self.config.edge_path or find_edge()
        if edge is None:
            raise RendererError("没找到 Edge/Chrome，无法渲染角色")

        # 贴图先压好（8192×8192 会撑爆渲染进程，而且解码要好几秒）
        made = ensure_texture_cache(self.model_dir,
                                    max_size=self.config.texture_max_size)
        if made:
            log.info("压缩了 %d 张贴图缓存", made)
        overlay = cache_overlay(self.model_dir, max_size=self.config.texture_max_size)

        self._server = Live2DServer(self.model_dir, cache_dir=overlay)
        self._server.start()
        page = f"{self._server.page_url}?model={model_url(self.model_dir)}"

        # 每次都用全新 profile：上一次崩溃留下的脏状态会让 Edge 启动即退，
        # 而且日志是空白的，非常难查。
        self._profile = Path(tempfile.mkdtemp(prefix="yachiyo-live2d-"))
        port = free_port()
        flags = [
            str(edge), "--headless=new",
            f"--remote-debugging-port={port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self._profile}",
            "--no-first-run", "--no-default-browser-check", "--hide-scrollbars",
            # ★ 这里**故意没有** --default-background-color=00000000：
            #   实测那个开关会让渲染进程静默消失。透明靠下面的 Emulation 覆盖。
            f"--window-size={self.config.width},{self.config.height}",
            *self.config.extra_edge_flags,
            page,
        ]
        log.info("启动无头渲染器：%s", edge.name)
        self._proc = subprocess.Popen(flags, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        try:
            self._session = self._connect(port)
            self._session.call("Page.enable", timeout=self.config.call_timeout)
            self._session.call("Runtime.enable", timeout=self.config.call_timeout)
            self._session.call("Emulation.setDefaultBackgroundColorOverride",
                               {"color": {"r": 0, "g": 0, "b": 0, "a": 0}},
                               timeout=self.config.call_timeout)
            self._wait_ready()
        except Exception:
            self.close()
            raise

    def _connect(self, port: int) -> CdpSession:
        """等 CDP 端点起来并连上。"""
        import urllib.request

        from websockets.sync.client import connect

        deadline = time.monotonic() + 30
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RendererError(
                    f"渲染器启动即退出（退出码 {self._proc.returncode}）")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/list", timeout=1) as resp:
                    targets = json.load(resp)
                pages = [t for t in targets
                         if t.get("type") == "page" and "127.0.0.1" in t.get("url", "")]
                if pages:
                    # legacy=True：我们要长期持有这条连接（不能写成 with 块），
                    # 这也是 websockets 官方给的直接连接方式。
                    ws = connect(pages[0]["webSocketDebuggerUrl"],
                                 max_size=None, open_timeout=20, legacy=True)
                    return CdpSession(ws)
            except Exception as exc:
                last = exc
            time.sleep(0.25)
        raise RendererError(f"连不上 CDP 端点：{type(last).__name__ if last else '超时'}")

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.config.ready_timeout
        while time.monotonic() < deadline:
            try:
                if self._session.js("!!(window.yachiyo && window.yachiyo.ready)"):
                    log.info("角色就绪")
                    return
                error = self._session.js("window.yachiyo ? window.yachiyo.error : ''")
                if error:
                    raise RendererError(f"页面报告加载失败：{error}")
            except RendererCrashed:
                raise
            except RendererError:
                pass                    # 页面还没挂上接口，继续等
            time.sleep(0.5)
        raise RendererNotReady(f"{self.config.ready_timeout}s 内模型没就绪")

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.call("Emulation.setDefaultBackgroundColorOverride", {})
            except Exception:
                pass
            try:
                self._session._ws.close()
            except Exception:
                pass
            self._session = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._server is not None:
            self._server.stop()
            self._server = None
        if self._profile is not None:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None

    def __enter__(self) -> "Live2DRenderer":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------- 取帧 / 驱动 ----------

    def call(self, expression: str, *, timeout: float | None = None):
        self._require_session()
        return self._session.js(expression, timeout=timeout or self.config.call_timeout)

    def state(self) -> dict:
        raw = self.call("JSON.stringify(window.yachiyo.state())")
        return json.loads(raw) if raw else {}

    def tick(self, times: int = 1) -> int:
        """推进模型若干帧（更新 + 渲染）。返回页面的帧计数。"""
        frames = 0
        for _ in range(max(1, times)):
            frames = self.call(f"window.yachiyo.tick({self.config.tick_ms})")
        return int(frames or 0)

    def frame(self, *, ticks: int | None = None) -> bytes:
        """取一帧 PNG（**带透明通道**：模型以外的像素 alpha=0）。"""
        self._require_session()
        self.tick(ticks if ticks is not None else self.config.ticks_per_frame)
        return self._session.screenshot(timeout=self.config.call_timeout)

    def set_mouth(self, value: float) -> dict:
        """写口型参数，并把模型里的值读回来（确认真的写进去了）。"""
        return json.loads(self.call(f"JSON.stringify(window.yachiyo.setMouth({float(value)}))"))

    def play_motion(self, group: str, index: int = 0):
        return self.call(f"window.yachiyo.playMotion({json.dumps(group)}, {int(index)})")

    def set_expression(self, name: str):
        return self.call(f"window.yachiyo.setExpression({json.dumps(name)})")

    def list_expressions(self) -> list[str]:
        return json.loads(self.call("JSON.stringify(window.yachiyo.listExpressions())"))

    def list_motions(self) -> dict:
        return json.loads(self.call("JSON.stringify(window.yachiyo.listMotions())"))

    def _require_session(self) -> None:
        if self._session is None:
            raise RendererNotReady("渲染器还没启动")
