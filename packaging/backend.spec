# -*- mode: python ; coding: utf-8 -*-
"""把 Python 后端打包成一个独立目录（onedir）。

为什么是 onedir 而不是 onefile：onefile 每次启动都要把几十 MB 解压到临时目录，
冷启动要多等好几秒 —— 用户点开图标等五秒会以为程序坏了。

打包（在项目根目录执行）：
    .venv\\Scripts\\pyinstaller.exe packaging\\backend.spec --noconfirm
产物：
    packaging/dist/yachiyo-backend/          ← 整个目录都要随应用分发
    packaging/dist/yachiyo-backend/yachiyo-backend.exe

资源放在 _internal 里，路径与项目里的相对位置一致，这样 core/paths.py 的
resource_path() 在开发期和打包后指向同一批文件（不用维护两套路径逻辑）。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent          # noqa: F821  (SPECPATH 由 PyInstaller 注入)
sys.path.insert(0, str(ROOT))

# litellm 用字符串动态找 provider 实现，静态分析看不见；uvicorn 的
# loops/protocols 也是运行时按名字挑的。都显式收进来。
#
# tiktoken_ext 必须收：它是 tiktoken 的"插件命名空间包"，tiktoken 靠
# pkgutil 扫描它来注册编码。漏了它的后果是打包版一说话就
# `ValueError: Unknown encoding cl100k_base. Plugins found: []`
# —— 开发期完全正常，只有打包后才炸（踩过一次）。
hiddenimports: list[str] = []
for package in ("uvicorn", "litellm", "websockets", "anyio", "pydantic", "fastapi",
                "starlette", "tiktoken_ext"):
    hiddenimports += collect_submodules(package)

datas = [
    # prompt.md 由 core.chat 通过 resource_path("prompt.md") 读
    (str(ROOT / "prompt.md"), "."),
    # Live2D 页面与引擎资源（后端挂在 /pet/ /static/）
    (str(ROOT / "app" / "assets" / "live2d"), "app/assets/live2d"),
    # Electron 界面文件（后端挂在 /）
    (str(ROOT / "desktop" / "renderer"), "desktop/renderer"),
]
datas += collect_data_files("litellm")
datas += collect_data_files("certifi")     # HTTPS 用的根证书
datas += collect_data_files("tiktoken")

# tiktoken 的 BPE 词表（构建时用 packaging/fetch_tiktoken_cache.py 先下好）。
# 不带也能跑，但用户第一次聊天得等它联网下载，网不好就报错。
_tiktoken_cache = ROOT / "packaging" / "tiktoken_cache"
if _tiktoken_cache.is_dir() and any(_tiktoken_cache.iterdir()):
    datas.append((str(_tiktoken_cache), "tiktoken_cache"))
else:
    print("!! 没有 packaging/tiktoken_cache/：先跑 packaging/fetch_tiktoken_cache.py，"
          "否则打包版第一次聊天要联网下载词表")

a = Analysis(                                # noqa: F821
    [str(ROOT / "backend" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 旧 Flet 前端不参与打包（P5 之后 app/ 只剩 Live2D 部分）
    excludes=["flet", "flet_desktop", "tkinter", "unittest", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)                            # noqa: F821

exe = EXE(                                   # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yachiyo-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 后端没有可见控制台；日志本来就写文件
    disable_windowed_traceback=False,
)
coll = COLLECT(                              # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="yachiyo-backend",
)
