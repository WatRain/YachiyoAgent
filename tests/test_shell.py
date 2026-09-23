"""Electron 壳的源码级检查。

**为什么是"读源码断言"而不是真跑界面**：跑一次 Electron 要十来秒、还要窗口，
不适合放进单元测试（真机检查交给 `scratch/ui_check.py` 那种脚本）。但删掉 Flet
版本之后，有几条老测试守着的约定不能就这么没了 —— 标题栏按钮有没有接线、
首次运行会不会拦住用户、密钥文案有没有说清存哪。这些都能在源码里查，而且
一旦有人改坏了，这里立刻会红。

（历史：这些约定原来是 tests/test_views.py、tests/test_oobe.py、
tests/test_main_flow.py 在 Flet 控件树上验证的，那三个文件随 Flet 一起删了。）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.paths import PROJECT_ROOT

DESKTOP = PROJECT_ROOT / "desktop"
MAIN_JS = DESKTOP / "main.js"
PRELOAD_JS = DESKTOP / "preload.js"
APP_JS = DESKTOP / "renderer" / "app.js"
INDEX_HTML = DESKTOP / "renderer" / "index.html"


def _text(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"没有 {path.name}（这个环境不是完整仓库）")
    return path.read_text(encoding="utf-8")


def test_window_is_frameless_and_locked_down() -> None:
    """无边框 + 渲染进程不能直接摸 Node。

    界面自己画标题栏（`frame: false`），所以窗口按钮必须由我们接线；
    而渲染进程只通过 preload 那一个桥跟主进程说话。
    """
    main = _text(MAIN_JS)
    assert "frame: false" in main
    assert "contextIsolation: true" in main
    assert "nodeIntegration: false" in main


def test_title_bar_buttons_are_wired_all_the_way_to_the_main_process() -> None:
    """界面上的「—」「✕」→ preload → 主进程，三段都要在。"""
    app = _text(APP_JS)
    preload = _text(PRELOAD_JS)
    main = _text(MAIN_JS)

    assert 'btn-min' in app and 'btn-close' in app
    assert "yachiyoShell.minimize()" in app
    assert "yachiyoShell.close()" in app

    assert '"win:minimize"' in preload or "'win:minimize'" in preload
    assert '"win:close"' in preload or "'win:close'" in preload

    assert 'ipcMain.on("win:minimize"' in main
    assert 'ipcMain.on("win:close"' in main


def test_api_token_never_travels_in_the_page_url() -> None:
    """真 token 走 IPC，不进 URL（URL 会留在历史/devtools 里）。

    主进程只给页面一个占位 `?token=1`（表示"已经授权过了"），
    真值由 `shell:info` 在进程内交过去。
    """
    main = _text(MAIN_JS)
    assert "shell:info" in main
    assert "backend.info.token" in main
    # 载入地址里不能出现真的 token 变量
    assert "?token=1" in main
    assert "${backend.info.token}" not in main.split("loadURL")[1].split("\n")[0]


def test_first_run_setup_blocks_the_chat_until_a_key_exists() -> None:
    """没配好就别放人进去聊天（老版本 OOBE 的核心约定）。

    判定必须同时看两件事：选中的 provider **存在**、且它**有密钥**。
    只看其中一个，用户就会得到"能发消息但每条都报错"的体验。
    """
    app = _text(APP_JS)
    assert "active_has_key" in app
    assert "openSetup()" in app
    # 判定与"没就绪就弹引导"要挨在一起
    ready_block = app[app.index("active_has_key") :][:400]
    assert "!ready" in ready_block
    assert "openSetup()" in ready_block


def test_setup_sheet_says_where_the_key_goes() -> None:
    """密钥存哪必须写在用户看得见的地方（也是隐私承诺）。

    老测试守的就是这句文案：**系统凭据管理器 + 不经过我们**。
    """
    app = _text(APP_JS)
    assert "凭据管理器" in app
    assert "不经过" in app


def test_physics_switch_is_discoverable_by_assistive_tech() -> None:
    """物理开关是个真按钮，得能被脚本/读屏认出来（当初 UI 自动化就卡在这）。"""
    app = _text(APP_JS)
    assert 'data-act="physics"' in app
    assert 'role="switch"' in app
    assert "aria-checked" in app


def test_live2d_panel_is_an_iframe_of_our_own_origin() -> None:
    """角色画在同源的 iframe 里 —— 这样才能直接调它的 yachiyo API。

    换成 file:// 或另一个端口，就得再发明一套 postMessage 协议。
    """
    html = _text(INDEX_HTML)
    assert "<iframe" in html
    assert 'id="pet"' in html
    app = _text(APP_JS)
    assert "contentWindow.yachiyo" in app or "yachiyo.lookAt" in app
