"""把超大贴图压成"能跑"的大小。

## 为什么需要这一步（实测踩的坑）

  这套模型带的是两张 **8192×8192** 的贴图（33.6 MB + 25.2 MB）。
  实测在无头 Edge 里加载到这一步，**渲染进程直接消失**：
  HTML 侧没有任何 JS 报错，Edge 日志里也没有异常，服务端只看到
  "客户端在下载贴图中途断开"。两张 8192² 的 RGBA 贴图光解码后就是
  2 × 268 MB，渲染进程被撑死了。

## 处理方式

  按 model3.json 里声明的贴图清单，等比缩到 max_size（默认 2048）以内，
  缓存到数据目录，**不修改用户的模型文件**：
      源：  <模型目录>/八千代辉夜姬.8192/texture_00.png   （只读）
      缓存：<数据目录>/live2d/textures/<模型名>/八千代辉夜姬.8192/texture_00.png

  2048 对应用里 420×560 的面板已经是 4 倍以上过采样，肉眼看不出差别，
  内存占用却降到 1/16。

  只有"源文件比 max_size 大"才重做；源文件更新过（mtime 变了）也会重做。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from core.paths import data_dir

log = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 2048


@dataclass
class TextureJob:
    source: Path
    target: Path
    width: int
    height: int

    @property
    def needs_resize(self) -> bool:
        return max(self.width, self.height) > 0


def model_textures(model_dir: Path) -> list[str]:
    """从模型定义里读出贴图清单（相对路径）。

    用 model3.json 里声明的清单，而不是"把目录下所有 png 都缩一遍" ——
    模型目录里还有头像之类跟渲染无关的图片，不该动它们。
    """
    from app.live2d.server import model_json_in

    path = model_json_in(model_dir)
    if path is None:
        return []
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("读模型定义失败 %s：%s", path.name, exc)
        return []
    refs = spec.get("FileReferences") or {}
    textures = refs.get("Textures") or []
    return [str(t) for t in textures if isinstance(t, str)]


def cache_root(model_dir: Path, *, max_size: int = DEFAULT_MAX_SIZE,
               base: Path | None = None) -> Path:
    """缓存目录。**带上尺寸**：不然改了 max_size 会复用上一个尺寸的旧缓存。"""
    root = base or (data_dir() / "live2d" / "textures")
    return root / model_dir.name / str(max_size)


def plan(model_dir: Path, *, max_size: int = DEFAULT_MAX_SIZE,
         base: Path | None = None) -> list[TextureJob]:
    """算出每张贴图要缩到多大、缩到哪。"""
    from PIL import Image

    root = cache_root(model_dir, max_size=max_size, base=base)
    jobs: list[TextureJob] = []
    for rel in model_textures(model_dir):
        source = model_dir / rel
        if not source.is_file():
            continue
        target = root / rel
        try:
            with Image.open(source) as img:
                width, height = img.size
        except Exception as exc:
            log.warning("读贴图失败 %s：%s", source.name, exc)
            continue
        if max(width, height) <= max_size:
            continue                      # 本来就够小，不用管
        jobs.append(TextureJob(source=source, target=target,
                               width=width, height=height))
    return jobs


def ensure_texture_cache(model_dir: Path, *, max_size: int = DEFAULT_MAX_SIZE,
                         base: Path | None = None) -> int:
    """把需要缩的贴图缩好放进缓存，返回**本次真正生成了几张**。

    已经有缓存、而且比源文件新的，直接跳过 —— 所以第二次启动是零成本。
    """
    from PIL import Image

    made = 0
    for job in plan(model_dir, max_size=max_size, base=base):
        if job.target.is_file() and job.target.stat().st_mtime >= job.source.stat().st_mtime:
            continue
        job.target.parent.mkdir(parents=True, exist_ok=True)
        log.info("压缩贴图 %s：%dx%d → 不超过 %dpx",
                 job.source.name, job.width, job.height, max_size)
        with Image.open(job.source) as img:
            img = img.convert("RGBA")
            img.thumbnail((max_size, max_size), Image.LANCZOS)
            img.save(job.target, format="PNG", optimize=True)
        made += 1
    return made


def cache_overlay(model_dir: Path, *, max_size: int = DEFAULT_MAX_SIZE,
                  base: Path | None = None) -> Path | None:
    """返回可以盖在模型目录上的缓存目录；没缓存就返回 None。"""
    root = cache_root(model_dir, max_size=max_size, base=base)
    if root.is_dir() and any(root.rglob("*.png")):
        return root
    return None
