"""统一的路径解析。

规则：**永远不要用 os.getcwd() 或相对路径决定数据位置。**
打包成 exe 后安装目录可能不可写，工作目录也不可预期。

- 持久数据（数据库、配置）→ FLET_APP_STORAGE_DATA
  打包后是 %APPDATA%\\<company>\\<product>\\data
  开发期（flet run）是 <项目>/.flet/storage/data，单独跑脚本时回退到 <项目>/.devdata
- 随程序分发的只读资源（prompt.md、图标）→ resource_path()
  打包后要从 PyInstaller 的解包目录取，所以必须走这个函数
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """持久数据目录。数据库、配置、日志都放这里。"""
    base = os.environ.get("FLET_APP_STORAGE_DATA")
    p = Path(base) if base else PROJECT_ROOT / ".devdata"
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
