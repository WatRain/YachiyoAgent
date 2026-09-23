"""Live2D 渲染页用的本地 HTTP 服务。

## 为什么必须起服务，不能直接 file:// 打开

  · 渲染页要 fetch 模型（`.model3.json` / `.moc3` / 贴图），
    `file://` 下的 fetch 会被浏览器按跨源拦掉；
  · 页面、内置依赖（vendor/）、模型分属两个目录，需要一个统一的 URL 映射；
  · 以后要做"角色面板"，也要有个稳定的地方让页面和 Python 通信。

## 只监听 127.0.0.1，端口随机

  模型是用户自己电脑上的文件，不该暴露到局域网。端口用 0 让系统分配，
  避免和别的程序撞端口。

## URL 映射

    /                → app/assets/live2d/pet.html
    /static/<路径>    → app/assets/live2d/<路径>      （pet.html、vendor/*）
    /model/<路径>     → <模型目录>/<路径>              （moc3、贴图、动作…）

两条根都做了目录穿越防护：解析出来的真实路径必须落在根目录里面。
"""

from __future__ import annotations

import http.server
import logging
import posixpath
import threading
import urllib.parse
from functools import partial
from pathlib import Path

from core import paths as core_paths

log = logging.getLogger(__name__)

ASSETS_DIR = core_paths.assets_dir()
MODEL_JSON_SUFFIXES = (".model3.json", ".model.json")


def find_model(project_root: Path | None = None) -> Path | None:
    """找一个 Live2D 模型目录（含 *.model3.json 或 *.model.json）。

    Cubism 4 是 model3.json，Cubism 2 是 model.json —— 两种都认。
    找到多个时按名字排序取第一个，保证行为稳定（不是随机挑）。

    搜索目录见 core.paths.model_roots()：数据目录优先（打包后用户放模型的地方），
    最后才是随程序分发的内置模型。传了 project_root 就只找它下面的 models/
    （测试用，行为跟以前一样）。
    """
    roots = [project_root / "models"] if project_root else core_paths.model_roots()
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if model_json_in(d) is not None:
                return d
    return None


def model_json_in(model_dir: Path) -> Path | None:
    """模型目录里的那份定义文件。"""
    for suffix in MODEL_JSON_SUFFIXES:
        found = sorted(model_dir.glob(f"*{suffix}"))
        if found:
            return found[0]
    return None


def model_url(model_dir: Path) -> str | None:
    """渲染页要用的 `?model=` 参数值（相对 /model/ 的路径，已做 URL 编码）。"""
    path = model_json_in(model_dir)
    if path is None:
        return None
    return "/model/" + urllib.parse.quote(path.name)


def _safe_join(root: Path, relative: str) -> Path | None:
    """把 URL 路径拼到根目录下，并挡住 ../ 穿越。"""
    # URL 里的中文/空格要先解码（模型文件名是中文的）
    rel = urllib.parse.unquote(relative, encoding="utf-8", errors="replace")
    rel = rel.replace("\\", "/").lstrip("/")
    rel = posixpath.normpath(rel)
    if rel in (".", ""):
        return None
    if rel.startswith("../") or rel == "..":
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None            # 跑到根目录外面去了，拒绝
    return candidate


class _Handler(http.server.BaseHTTPRequestHandler):
    """实现两个根的路由。没有目录列表、没有写操作，只读。"""

    server_version = "YachiyoLive2D/1.0"

    def __init__(self, *args, assets: Path, model_dir: Path,
                 cache_dir: Path | None = None, **kwargs) -> None:
        self._assets = assets
        self._model_dir = model_dir
        self._cache_dir = cache_dir
        super().__init__(*args, **kwargs)

    # 默认实现会把每个请求打到 stderr，太吵；降到 debug
    def log_message(self, fmt, *args) -> None:
        log.debug("live2d http: " + fmt, *args)

    def do_GET(self) -> None:                       # noqa: N802 (BaseHTTPRequestHandler 的约定)
        self._serve(head_only=False)

    def do_HEAD(self) -> None:                      # noqa: N802
        self._serve(head_only=True)

    def _serve(self, *, head_only: bool) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/pet.html", "/index.html"):
            self._send_file(self._assets / "pet.html", head_only=head_only,
                            cache="no-store")       # 页面本身不缓存，改完刷新就生效
            return
        if path.startswith("/static/"):
            target = _safe_join(self._assets, path[len("/static/"):])
            if target is None or not target.is_file():
                self.send_error(404, "Not Found")
                return
            self._send_file(target, head_only=head_only, cache="max-age=3600")
            return
        if path.startswith("/model/"):
            rel = path[len("/model/"):]
            # ★ 先看贴图缓存：8192 的原始贴图会把渲染进程撑死，
            #   缓存里放的是缩好的版本。缓存没有的（moc3、动作…）再回模型目录取。
            for root in (self._cache_dir, self._model_dir):
                if root is None:
                    continue
                target = _safe_join(root, rel)
                if target is not None and target.is_file():
                    self._send_file(target, head_only=head_only, cache="max-age=3600")
                    return
            self.send_error(404, "Not Found")
            return
        self.send_error(404, "Not Found")

    def _send_file(self, path: Path, *, head_only: bool, cache: str) -> None:
        if not path.is_file():
            self.send_error(404, "Not Found")
            return
        size = path.stat().st_size
        content_type = self._content_type(path)
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", cache)
        # 这个服务只给本机页面用，不需要跨源；顺带把嗅探关掉
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if head_only:
            return
        # ★ 分块发送，不要一次 read_bytes()：
        #   贴图单张就 33 MB，全读进内存既浪费又没必要。
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 浏览器中途取消连接是常事（比如渲染进程被系统回收），不是错误。
            # 不接住的话 socketserver 会往 stderr 打一大串 traceback，把真正的日志淹掉。
            log.debug("live2d http: 客户端提前断开 %s", path.name)

    @staticmethod
    def _content_type(path: Path) -> str:
        import mimetypes

        known = {
            ".moc3": "application/octet-stream",
            ".js": "text/javascript",
            # ★ .mjs 必须显式给 MIME：ES module 的 MIME 不对，浏览器会直接拒绝执行
            #   （"Expected a JavaScript module script but the server responded
            #    with a MIME type of ..."），而这个错看起来像文件没发出去。
            ".mjs": "text/javascript",
            ".json": "application/json",
            ".html": "text/html",
            ".css": "text/css",
            ".png": "image/png",
        }
        return known.get(path.suffix.lower()) or \
            mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class Live2DServer:
    """后台线程里跑的只读 HTTP 服务。"""

    def __init__(self, model_dir: Path, assets_dir: Path = ASSETS_DIR,
                 cache_dir: Path | None = None) -> None:
        if not (assets_dir / "pet.html").is_file():
            raise FileNotFoundError(f"渲染页不存在：{assets_dir / 'pet.html'}")
        handler = partial(_Handler, assets=assets_dir, model_dir=model_dir,
                          cache_dir=cache_dir)
        # ThreadingHTTPServer：页面会并发拉一堆贴图/动作文件，单线程会卡住
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="live2d-http", daemon=True)

    @property
    def page_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self) -> None:
        self._thread.start()
        log.info("Live2D 服务已启动：%s", self.page_url)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
