"""把 Live2D 渲染页挂到后端自己的服务器上。

## 为什么不再用 app/live2d/server.py 那个独立的小 HTTP 服务

Flet 版是「Python 起一个只读服务 → 无头 Edge 打开它 → CDP 抓帧 → 传到界面」。
Electron 版不需要传帧了，但**还需要一个 http 源**：渲染页要 fetch 模型文件，
`file://` 下会被跨源策略拦掉；而且页面和模型分属两个目录，需要统一映射。

干脆把它挂进后端同一个 FastAPI 上，好处是**同源**：
Electron 渲染进程里 `<iframe>` 打开的渲染页，可以直接
`iframe.contentWindow.yachiyo.lookAt(x, y)` 驱动，不用另发明一套 postMessage 协议。

## URL 映射（和 app/live2d/server.py 保持一致）

    /pet/            → app/assets/live2d/pet.html
    /static/<路径>    → app/assets/live2d/<路径>        （vendor/*.mjs、map 文件）
    /model/<路径>     → 贴图缓存优先，其次模型目录        （moc3、贴图、动作）

pet.html 里的依赖路径全是绝对路径（`/static/vendor/...`），模型 URL 也是 `/model/...`，
所以这些路由必须挂在**根**上，不能挂在 /pet 下面。
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.live2d.server import ASSETS_DIR, _safe_join, find_model, model_url
from app.live2d.textures import cache_overlay, ensure_texture_cache
from core.paths import data_dir

log = logging.getLogger(__name__)

# 8192 的原始贴图会把渲染进程撑死，先缩到这个尺寸（和 RenderConfig 的默认值一致）
TEXTURE_MAX_SIZE = 2048
CACHE_HEADERS = {"Cache-Control": "max-age=3600"}
CUBISM_CORE_CDN_URL = "https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js"
CUBISM_CORE_FILENAME = "live2dcubismcore.min.js"
CUBISM_CORE_MAX_BYTES = 20 * 1024 * 1024
_cubism_core_download_lock = asyncio.Lock()

# 这几个后缀给错 MIME 会直接坏掉：
#   .mjs 给错 → 浏览器拒绝执行 ES module（报 "Expected a JavaScript module script…"）
#   .moc3 当文本 → 二进制被按 UTF-8 解一遍就废了
KNOWN_TYPES = {
    ".moc3": "application/octet-stream",
    ".mjs": "text/javascript",
    ".js": "text/javascript",
    ".json": "application/json",
    ".html": "text/html",
    ".css": "text/css",
    ".png": "image/png",
}


def _media_type(path: Path) -> str:
    import mimetypes

    mime = KNOWN_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] \
        or "application/octet-stream"
    if mime.startswith("text/") or mime in ("application/json", "text/javascript"):
        return mime + "; charset=utf-8"
    return mime


def _cubism_core_cache_path() -> Path:
    configured_dir = os.environ.get("YACHIYO_LIVE2D_CACHE_DIR")
    cache_dir = Path(configured_dir) if configured_dir else data_dir() / "live2d" / "cache"
    return cache_dir / CUBISM_CORE_FILENAME


def _valid_cubism_core_cache(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


async def _ensure_cubism_core_cache() -> Path:
    path = _cubism_core_cache_path()
    if _valid_cubism_core_cache(path):
        return path

    async with _cubism_core_download_lock:
        if _valid_cubism_core_cache(path):
            return path

        temp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix=f".{CUBISM_CORE_FILENAME}.", suffix=".tmp",
                dir=path.parent, delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)

            total_bytes = 0
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                async with client.stream("GET", CUBISM_CORE_CDN_URL) as response:
                    response.raise_for_status()
                    media_type = response.headers.get("content-type", "").split(";", 1)[0]
                    media_type = media_type.strip().lower()
                    supported_types = {
                        "text/javascript", "application/javascript", "application/octet-stream",
                    }
                    if media_type not in supported_types:
                        raise ValueError(f"意外的 Content-Type：{media_type or 'missing'}")
                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > CUBISM_CORE_MAX_BYTES:
                        raise ValueError("Cubism Core 响应超过大小限制")
                    with temp_path.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            total_bytes += len(chunk)
                            if total_bytes > CUBISM_CORE_MAX_BYTES:
                                raise ValueError("Cubism Core 响应超过大小限制")
                            output.write(chunk)

            if total_bytes == 0:
                raise ValueError("Cubism Core 响应为空")
            os.replace(temp_path, path)
            temp_path = None
            log.info("Cubism Core 已从官方 CDN 下载并缓存到本机")
            return path
        except Exception as exc:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            log.warning("Cubism Core 首次下载失败：%s", exc)
            raise HTTPException(
                status_code=502,
                detail="首次加载 Live2D 需要连接官方 CDN；下载 Cubism Core 失败，请检查网络后重试。",
            ) from exc


@dataclass(frozen=True)
class Live2DInfo:
    ready: bool
    page: str = ""
    model: str = ""
    model_dir: str = ""
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "page": self.page,
            "model": self.model,
            # 只给模型名，不给完整路径：界面要拿它显示"现在是哪个角色"
            # （也是模型版权声明该出现的位置），路径没必要往外传。
            "name": Path(self.model_dir).name if self.model_dir else "",
            "message": self.message,
        }


def find_render_model() -> Path | None:
    """要渲染的模型目录。将来公开版会改成优先用内置模型。"""
    return find_model()


def mount(app: FastAPI, *, assets_dir: Path = ASSETS_DIR,
          model_dir: Path | None = None) -> Live2DInfo:
    """给 app 装上渲染页的路由。找不到模型就什么都不挂（前端显示一句提示）。"""
    assets = assets_dir
    if not (assets / "pet.html").is_file():
        return Live2DInfo(ready=False, message=f"渲染页不存在：{assets / 'pet.html'}")

    found = model_dir or find_render_model()
    if found is None:
        return Live2DInfo(ready=False, message="没找到 Live2D 模型（models/ 下面没有 *.model3.json）")

    # 第一次启动要缩贴图（两张 8192 的大图），之后走缓存是零成本
    ensure_texture_cache(found, max_size=TEXTURE_MAX_SIZE)
    cache = cache_overlay(found, max_size=TEXTURE_MAX_SIZE)
    if cache is None:
        log.warning("贴图缓存没建起来，直接发原图（内存占用会高不少）")

    param = model_url(found)
    if param is None:  # 理论上不会发生：find_model 就是按这份定义文件找的
        return Live2DInfo(ready=False, message="模型目录里没有 .model3.json")

    @app.get("/pet/", include_in_schema=False)
    @app.get("/pet/index.html", include_in_schema=False)
    def pet_page() -> FileResponse:  # noqa: ANN202
        # 页面不缓存：改完刷新就生效，调试期省事
        return FileResponse(assets / "pet.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"})

    @app.get("/live2d/cubism-core.js", include_in_schema=False)
    async def cubism_core_file() -> FileResponse:
        core = await _ensure_cubism_core_cache()
        return FileResponse(core, media_type="text/javascript; charset=utf-8",
                            headers=CACHE_HEADERS)

    @app.get("/static/{rel:path}", include_in_schema=False)
    def static_file(rel: str) -> FileResponse:  # noqa: ANN202
        target = _safe_join(assets, rel)
        if target is None or not target.is_file():
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(target, media_type=_media_type(target), headers=CACHE_HEADERS)

    @app.get("/model/{rel:path}", include_in_schema=False)
    def model_file(rel: str) -> FileResponse:  # noqa: ANN202
        for root in (cache, found):
            if root is None:
                continue
            target = _safe_join(root, rel)
            if target is not None and target.is_file():
                return FileResponse(target, media_type=_media_type(target),
                                    headers=CACHE_HEADERS)
        raise HTTPException(status_code=404, detail="Not Found")

    log.info("渲染页已挂载：模型=%s 贴图缓存=%s", found.name, cache or "无")
    return Live2DInfo(ready=True, page="/pet/", model=param, model_dir=str(found))
