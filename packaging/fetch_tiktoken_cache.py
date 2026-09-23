"""把 tiktoken 的编码数据先下载到 packaging/tiktoken_cache/，供打包时一起带走。

为什么需要这一步：tiktoken 的 BPE 词表不在 wheel 里，第一次用的时候才从网上下，
默认落到系统临时目录。打包版有两处不放心：
  1. 用户第一次聊天要是网不好，就白等（甚至会报错）
  2. 临时目录会被清理，下次又得重下

所以构建时先下好、打进包里，运行时用 TIKTOKEN_CACHE_DIR 指过去
（见 backend/__main__.py）。文件很小（每个约 1.7MB）。

用法：.venv\\Scripts\\python.exe packaging\\fetch_tiktoken_cache.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "tiktoken_cache"
# litellm 数 token 时最常用的两个；按需再加
ENCODINGS = ("cl100k_base", "o200k_base")


def main() -> int:
    os.environ["TIKTOKEN_CACHE_DIR"] = str(CACHE)
    CACHE.mkdir(parents=True, exist_ok=True)

    try:
        import tiktoken
    except ImportError:
        print("没装 tiktoken（先在项目环境里 pip install tiktoken）", file=sys.stderr)
        return 1

    for name in ENCODINGS:
        try:
            tiktoken.get_encoding(name)
        except Exception as exc:                      # noqa: BLE001
            # 下不下来不算致命：打包时只是不带缓存，运行时仍会自己下载
            print(f"× {name}：{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        print(f"√ {name}")

    files = sorted(p for p in CACHE.iterdir() if p.is_file())
    total = sum(p.stat().st_size for p in files)
    print(f"缓存目录：{CACHE}（{len(files)} 个文件，{total / 1024 / 1024:.1f} MB）")
    return 0 if files else 1


if __name__ == "__main__":
    sys.exit(main())
