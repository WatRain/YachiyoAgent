"""一次性数据搬家：把 Flet 时代的数据目录里的 JSON 挪到新目录。

为什么需要：密钥在 Windows 凭据管理器里（和目录无关，搬家不用管），
但 provider 配置、对话记录、记忆都是 JSON 文件，换目录就会"人还在、记忆没了"。
开发期这台机器上正好有需要保下来的数据，所以做一次幂等的复制 —— 只补缺失的
文件，绝不覆盖新目录里已有的东西。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from core.paths import PROJECT_ROOT, data_dir

log = logging.getLogger(__name__)

MOVE_FILES = ("config.json", "conversation.json", "memories.json", "reminders.json")

# 老位置：flet run 会写 <项目>/app/.flet/storage/data，早期脚本写 <项目>/.devdata
LEGACY_DIRS = (
    PROJECT_ROOT / "app" / ".flet" / "storage" / "data",
    PROJECT_ROOT / ".flet" / "storage" / "data",
    PROJECT_ROOT / ".devdata",
)


def migrate_legacy_data() -> list[str]:
    """返回这次实际搬过来的文件名（空列表 = 没什么可搬的）。"""
    target = data_dir()
    moved: list[str] = []

    for folder in LEGACY_DIRS:
        if not folder.exists():
            continue
        try:
            if folder.resolve() == target.resolve():
                continue  # 目标就是它自己，别自搬自
        except OSError:
            pass

        for name in MOVE_FILES:
            src = folder / name
            dst = target / name
            if not src.is_file() or dst.exists():
                continue
            try:
                shutil.copy2(src, dst)
            except OSError as exc:
                log.warning("搬运 %s 失败（%s）", src, type(exc).__name__)
                continue
            moved.append(name)
            log.info("已从旧目录搬来 %s：%s", name, folder)

    if moved:
        log.info("数据搬家完成，共 %d 个文件", len(moved))
    return moved
