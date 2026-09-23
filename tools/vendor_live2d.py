"""下载并内置 Live2D 渲染链需要的 MIT 前端依赖。

    pixi.js                       MIT  —— 渲染框架（v8）
    untitled-pixi-live2d-engine   MIT  —— Live2D 插件，支持 Cubism 2/3/4/5
    live2dcubismcore.min.js       不分发 —— 页面从 Live2D 官方 CDN 取

用法：
    python tools/vendor_live2d.py

会把文件写进 app/assets/live2d/vendor/，并生成 README.md（含 sha256）和
vendor.json。**只下载页面真正用到的那两个文件**：

  · pixi.min.mjs            ESM，页面用 import 加载
  · …cubism.es.js           ESM，静态 import "pixi.js"，由页面里的 import map 指到上面那个

不用 UMD 版本，因为 PIXI 8 的 dist/pixi.min.js 实测**不是** UMD
（结尾是 `h}({});`，globalThis.PIXI 出现 0 次），靠全局名会踩空。

Cubism Core 故意不内置：那是 Live2D 自己的东西，页面运行时从官方 CDN 取，
我们不分发它。
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "app" / "assets" / "live2d" / "vendor"

PIXI_VER = "8.13.1"
ENGINE_VER = "1.4.0"
CDN = "https://cdn.jsdelivr.net/npm"

FILES = [
    ("pixi.min.mjs",
     f"{CDN}/pixi.js@{PIXI_VER}/dist/pixi.min.mjs",
     f"PixiJS {PIXI_VER}（ESM 构建）"),
    ("pixi-live2d-engine.cubism.es.js",
     f"{CDN}/untitled-pixi-live2d-engine@{ENGINE_VER}/dist/cubism.es.js",
     f"untitled-pixi-live2d-engine {ENGINE_VER}（Cubism 3/4/5，ESM 构建）"),
    ("LICENSE-pixi.txt",
     f"{CDN}/pixi.js@{PIXI_VER}/LICENSE",
     f"PixiJS {PIXI_VER} 许可证全文"),
    ("LICENSE-untitled-pixi-live2d-engine.txt",
     f"{CDN}/untitled-pixi-live2d-engine@{ENGINE_VER}/LICENSE",
     f"untitled-pixi-live2d-engine {ENGINE_VER} 许可证全文"),
]

README = """# 内置的前端渲染依赖

这里**不是**本项目的代码，是第三方库，按各自许可证随程序分发。
两个都是 MIT，不影响本项目选择什么许可证。

重新生成：`python tools/vendor_live2d.py`

| 文件 | 说明 | 大小 | sha256 |
|---|---|---|---|
{rows}

## 不在这里的依赖

**Cubism Core**（`live2dcubismcore.min.js`）由页面在运行时从 Live2D 官方 CDN 加载，
不由我们分发：

    https://cubism.live2d.com/sdk-web/cubismcore/live2dcubismcore.min.js

## 为什么不用 live2d-widget

模型原先配的是 `stevenjoezhang/live2d-widget`，它是 **GPL-3.0-or-later**。
一旦随程序分发，整个应用就必须是 GPL-3（必须公开源码）。
换成 MIT 的 PIXI + Live2D 引擎，本项目就能自由选择许可证。

## 为什么不用 pixi-live2d-display 0.4.0

它是 2022 年的东西，只带 **Cubism 4** 框架；我们的模型 moc3 是**版本 5**
（文件头 `MOC3\\x05`），加载它会让渲染进程直接崩，而且 HTML 侧没有任何报错。
换成支持 Cubism 2/3/4/5 的 untitled-pixi-live2d-engine。
"""


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def main() -> None:
    VENDOR.mkdir(parents=True, exist_ok=True)
    rows = []
    meta = {}
    for name, url, desc in FILES:
        print(f"下载 {name} …")
        try:
            data = fetch(url)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"  {url} 返回 {exc.code}") from exc
        (VENDOR / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        rows.append(f"| `{name}` | {desc}<br>来源：{url} | {len(data)} B | `{digest}` |")
        meta[name] = {"url": url, "bytes": len(data), "sha256": digest}
        print(f"  {len(data)} 字节  sha256={digest[:16]}…")

    (VENDOR / "README.md").write_text(
        README.format(rows="\n".join(rows)), encoding="utf-8")
    (VENDOR / "vendor.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出 vendor/README.md 与 vendor/vendor.json")


if __name__ == "__main__":
    main()
