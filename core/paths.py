"""统一的路径解析。

规则：**永远不要用 os.getcwd() 或相对路径决定数据位置。**
打包成 exe 后安装目录可能不可写，工作目录也不可预期。

- 持久数据（数据库、配置）→ data_dir()
  打包后是 %APPDATA%\\YachiyoAgent\\data（旧版 Flet 前端用的是 FLET_APP_STORAGE_DATA）
  开发期是 <项目>/.devdata
- 随程序分发的只读资源（prompt.md、图标）→ resource_path()
  打包后要从 PyInstaller 的解包目录取，所以必须走这个函数
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """持久数据目录。数据库、配置、日志都放这里。

    优先级（第一个有值的说了算）：
      1. YACHIYO_DATA_DIR       —— Electron 前端启动后端时指定的用户数据目录
      2. FLET_APP_STORAGE_DATA  —— 旧版 Flet 前端的数据目录（**兼容用，别删**）
      3. 打包运行时的 %APPDATA%\\YachiyoAgent\\data
         （安装目录在 Program Files 下不可写，绝不能往那里写数据）
      4. <项目>/.devdata         —— 开发期兜底

    为什么要有第 1 条：新前端不该被迫沿用带 FLET_ 前缀的环境变量，
    但直接把变量名换掉又会让老数据（配置、对话记录）失联 ——
    所以是"加一层优先"，不是"替换"。
    """
    base = os.environ.get("YACHIYO_DATA_DIR") or os.environ.get("FLET_APP_STORAGE_DATA")
    if base:
        p = Path(base)
    elif is_frozen():
        appdata = os.environ.get("APPDATA")
        p = Path(appdata) / "YachiyoAgent" / "data" if appdata else PROJECT_ROOT / ".devdata"
    else:
        p = PROJECT_ROOT / ".devdata"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "yachiyo.db"


def config_path() -> Path:
    return data_dir() / "config.json"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打出的产物里。"""
    return getattr(sys, "frozen", False)


def resource_path(relative: str) -> Path:
    """只读资源。开发期从项目根取，打包后从解包目录取。"""
    base = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
    return base / relative


def assets_dir() -> Path:
    """Live2D 页面 + 引擎资源（随程序分发的只读资源）。

    打包后位置换了，所以别在别处写死 `Path(__file__)...assets/live2d`。
    """
    override = os.environ.get("YACHIYO_ASSETS_DIR")
    return Path(override) if override else resource_path("app/assets/live2d")


def renderer_dir() -> Path:
    """Electron 界面文件。后端起 HTTP 服务时把它挂在 / 上（同源方案）。"""
    override = os.environ.get("YACHIYO_RENDERER_DIR")
    return Path(override) if override else resource_path("desktop/renderer")


def model_roots() -> list[Path]:
    """Live2D 模型的搜索目录。

    现在只有**内置模型**一档：`app/assets/live2d/models/`，随程序分发，装完打开就有角色。
    故意**不给用户留自定义模型的口子** —— 数据目录下的 `models/`、项目根的 `models/`
    都不再参与搜索（"放个文件夹进去就换模型"那种隐式接口不要有）。

    真需要换一只模型时（开发、排查、以后做内置模型替换）用 `YACHIYO_MODEL_DIR`
    显式指一个目录；它排在内置模型前面。

    返回全部候选（不是"第一个存在的"）：调用方要接着往下找。
    """
    roots: list[Path] = []
    override = os.environ.get("YACHIYO_MODEL_DIR")
    if override:
        roots.append(Path(override))
    builtin = assets_dir() / "models"
    if all(str(builtin) != str(r) for r in roots):
        roots.append(builtin)
    return roots
