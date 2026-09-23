"""跑后端：python -m backend

前端（Electron 主进程）会这么用它：
  1. 用 YACHIYO_DATA_DIR 指定用户数据目录（不指定就用 core.paths 的默认规则）
  2. 从 stdout 读一行 `YACHIYO_BACKEND_READY {...}`，里面有端口和 token
  3. 把窗口指向 url

端口默认随机（避免和用户机器上别的东西撞），token 默认随机生成。
两者都可以用环境变量固定下来，方便调试。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys

from backend.app import create_app
from backend.legacy import migrate_legacy_data
from core.logging_setup import setup_logging
from core.paths import data_dir, logs_dir, resource_path

log = logging.getLogger(__name__)


def _use_bundled_tiktoken_cache() -> None:
    """打包版带着 tiktoken 的 BPE 词表时，指过去用 —— 免得第一次聊天要联网下载。

    tiktoken 是在**用的时候**读 TIKTOKEN_CACHE_DIR 的，所以在这设就来得及；
    找不到那份词表就不设（开发期就是这种情况，它会用系统临时目录）。
    """
    cache = resource_path("tiktoken_cache")
    if cache.is_dir() and any(cache.iterdir()):
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache))


def _free_port() -> int:
    """问系统要一个空闲端口。绑 0 再关掉，从拿到的那一刻到 uvicorn 真正绑上
    之间有一个极小的时间窗，对本机应用来说够用了。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _announce(port: int, token: str) -> None:
    payload = {
        "port": port,
        "token": token,
        "url": f"http://127.0.0.1:{port}/",
        "api": 1,
    }
    print("YACHIYO_BACKEND_READY " + json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    level = os.environ.get("YACHIYO_LOG_LEVEL", "INFO").upper()
    setup_logging(getattr(logging, level, logging.INFO))

    moved = migrate_legacy_data()
    log.info("后端启动中：数据目录=%s 日志目录=%s 搬家=%s", data_dir(), logs_dir(), moved or "无")
    _use_bundled_tiktoken_cache()

    # 注意顺序：这一行是在 uvicorn 真正开始监听之后才打出去的（见 on_ready），
    # 否则前端拿到端口就去连会吃到 connection refused。

    token = os.environ.get("YACHIYO_BACKEND_TOKEN") or secrets.token_urlsafe(32)
    port_env = os.environ.get("YACHIYO_BACKEND_PORT", "").strip()
    port = int(port_env) if port_env.isdigit() else _free_port()

    app = create_app(token=token, on_ready=lambda: _announce(port, token))

    import uvicorn

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level=level.lower(), access_log=False)
    except KeyboardInterrupt:
        log.info("收到中断，后端退出")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
