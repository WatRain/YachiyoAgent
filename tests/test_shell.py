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


def test_physics_switch_is_gone_from_settings() -> None:
    """物理开关已经撤掉：关掉物理角色会僵在那儿，用户反馈太怪。

    它当初是界面上唯一一个 role="switch" 的控件，顺手守住 —— 别再溜回来。
    真要省帧率就走 pet.html 的 ?physics=0（调试/自动化口子），不该出现在界面上。
    """
    app = _text(APP_JS)
    assert 'data-act="physics"' not in app
    assert "live2d_physics" not in app
    assert "角色物理" not in app


def test_tool_calls_are_shown_with_what_they_will_run() -> None:
    """模型动手时要看得见「哪个工具」+「执行的命令」。

    只有一句状态行不够用：用户不知道模型在动什么，也就没法决定要不要安心。
    命令是后端翻好送来的（见 core/tools.py 的 tool_command），界面只管显示。
    """
    app = _text(APP_JS)
    assert "function addToolCall(" in app
    assert "tool-call" in app
    assert "msg.command" in app
    assert '"tool_start"' in app and '"tool_end"' in app


def test_tool_calls_show_the_result_when_they_finish() -> None:
    """干完活还得说一句「拿回来了什么」，卡片不能一直只显示它要干什么。

    结果由后端压成一句话送来（core/tools.py 的 tool_result）—— 界面不做截断，
    也不认识"失败"该怎么判，只认 msg.failed 这个布尔值。
    """
    app = _text(APP_JS)
    assert "msg.result" in app
    assert "msg.failed" in app
    assert "tool-call-result" in app
    # 收尾时得把事件本身传进去，不然拿不到 result —— 卡片会一直停在"正在用…"
    assert "finishToolCall(msg)" in app


def test_tool_call_results_go_in_as_text_not_html() -> None:
    """工具结果里可能有尖括号（读到的代码、网页标题），一律当字面量。

    这里是 textContent 不是 innerHTML —— 走错一步，用户读一次文件就够
    在自己界面上执行一段陌生脚本了。
    """
    app = _text(APP_JS)
    # 结果那一行在 finishCard 里（实时收尾和重启后补画共用它）
    start = app.index("function finishCard(")
    block = app[start : app.index("function addToolCall(", start)]
    assert "line.textContent = text" in block
    assert "innerHTML" not in block


def test_render_history_draws_tool_cards_too() -> None:
    """重启后也要把上次调过的工具补画回来。

    后端给的流里 tool 条目已经翻好了（label / command / result），
    但 renderHistory 以前只认 user / assistant，历史里的工具调用全蒸发了 ——
    用户的原话是「重新开启应用后，之前调用过的工具看不到」。
    """
    app = _text(APP_JS)
    start = app.index("function renderHistory(")
    block = app[start : app.index("/* 工具卡片", start)]
    assert 'role === "tool"' in block
    assert "buildToolCard(message)" in block
    assert "finishCard(card," in block


def test_the_answer_bubble_is_created_lazily() -> None:
    """助手气泡不能在发消息时就占好位。

    模型要是先调工具再回话，工具卡片就只能排在那个空气泡下面 ——
    最后答案反而显示在「我调了什么工具」的上面，顺序整个反掉。
    """
    app = _text(APP_JS)
    assert "function ensureReply()" in app
    assert "setBubbleText(ensureReply(), replyText)" in app
    send_block = app[app.index("function sendMessage()") :]
    assert "reply = addBubble(" not in send_block[:1500]


def test_the_answer_starts_a_new_bubble_under_the_tool_card() -> None:
    """工具卡片之后的答案必须另起一个气泡。

    模型爱先说一句「我查一下」再动手（实测每个用了工具的回合都有这么一句），
    那句话留在卡片上面没问题 —— 时间顺序就是如此。但真正的答案要是还写进同一个
    气泡，就会显示在卡片的上面，读起来像「回复在工具调用的上方」。
    """
    app = _text(APP_JS)
    start = app.index("function addToolCall(")
    block = app[start : app.index("function finishToolCall(", start)]
    assert "reply = null;" in block
    assert 'replyText = "";' in block


def test_live2d_panel_is_an_iframe_of_our_own_origin() -> None:
    """角色画在同源的 iframe 里 —— 这样才能直接调它的 yachiyo API。

    换成 file:// 或另一个端口，就得再发明一套 postMessage 协议。
    """
    html = _text(INDEX_HTML)
    assert "<iframe" in html
    assert 'id="pet"' in html
    app = _text(APP_JS)
    assert "contentWindow.yachiyo" in app or "yachiyo.lookAt" in app
