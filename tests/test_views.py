"""界面接线的测试：每个按钮点一下，确认不会因为"参数对不上"而崩。

背景：这个 bug 真实发生过 ——
    on_click=lambda e: page.run_task(on_save)
而 on_save 的签名是 async def on_save(e)。
lambda 收到了事件 e，但调 run_task 时【没把它传进去】，
于是 on_save 被零参数调用 → TypeError: missing 1 required positional argument: 'e'。

这类 bug 的特点是：**界面能建出来、按钮能显示，只有点下去才炸**。
所以光"能启动"不够，必须真的点一遍。

这个测试不需要 Flet 运行时 —— 用一个假的 Page 顶替。
"""

import asyncio
import os
import shutil
import uuid
from pathlib import Path

import flet as ft
import pytest

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "views"


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    root = _TMP_ROOT / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(root))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class FakePage:
    """够用的假 Page：只实现界面真正会调的那几个方法。"""

    def __init__(self) -> None:
        self.updates = 0
        self.tasks: list = []

    def update(self) -> None:
        self.updates += 1

    def run_task(self, fn, *args, **kwargs):
        """和 Flet 一样：接协程【函数】和它的参数，然后调度执行。"""
        coro = fn(*args, **kwargs)
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def scroll_to(self, **kwargs) -> None:
        return None


class FakeSecrets:
    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    async def exists(self, provider_id: str) -> bool:
        return provider_id in self.saved

    async def load(self, provider_id: str):
        return self.saved.get(provider_id)

    async def save(self, provider_id: str, api_key: str, *, persist: bool = True) -> None:
        self.saved[provider_id] = api_key

    async def delete(self, provider_id: str) -> None:
        self.saved.pop(provider_id, None)


def collect(control, wanted):
    """把一棵控件树里所有某种类型的控件找出来。"""
    found = []
    if isinstance(control, wanted):
        found.append(control)
    for attr in ("controls", "content", "tabs"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            for item in value:
                found += collect(item, wanted)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            found += collect(value, wanted)
    return found


def collect_clickables(control):
    """找出所有"点了会触发回调"的控件。

    ★ 故意不写死控件类型（Button / IconButton / TextButton / ...）。
      否则界面一改样式（比如把 Button 换成 IconButton），测试就会误报失败 ——
      那种失败不是 bug，是我们的测试太脆。
    """
    found = []
    if hasattr(control, "on_click") and control.on_click is not None:
        found.append(control)
    for attr in ("controls", "content", "tabs"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            for item in value:
                found += collect_clickables(item)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            found += collect_clickables(value)
    return found


def label_of(control) -> str:
    """给控件起个能读的名字，用于断言失败时的提示。"""
    for attr in ("content", "text", "label", "icon", "hint_text"):
        value = getattr(control, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(control).__name__


async def _settle(page: FakePage) -> None:
    """等已经调度出去的任务跑完。"""
    if page.tasks:
        await asyncio.gather(*page.tasks, return_exceptions=True)
        page.tasks.clear()
    await asyncio.sleep(0.05)


# ─────────────────────────────────────────────
#  设置页
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_view_builds():
    from app.views.settings import build_settings_view

    page = FakePage()
    view = build_settings_view(page, FakeSecrets(), lambda *a, **k: None)
    assert view is not None
    assert collect_clickables(view), "设置页应该有可点的控件"


@pytest.mark.asyncio
async def test_every_settings_button_is_clickable():
    """★ 就是这条能抓住"参数对不上"的 bug。"""
    from app.views.settings import build_settings_view

    page = FakePage()
    view = build_settings_view(page, FakeSecrets(), lambda *a, **k: None)

    clickables = collect_clickables(view)
    assert clickables, "设置页应该有可点的控件"
    for control in clickables:
        # 用 None 当事件对象：如果函数签名要求事件却没收到，这里就会 TypeError
        try:
            control.on_click(None)
        except TypeError as exc:
            pytest.fail(f"点 {label_of(control)!r} 时报参数错误：{exc}")
        await _settle(page)


@pytest.mark.asyncio
async def test_settings_save_writes_key_and_config():
    """点「保存」应该真的把密钥存下来，并通知外面。"""
    from app.views.settings import build_settings_view

    page = FakePage()
    secrets = FakeSecrets()
    notified = {"n": 0}

    view = build_settings_view(page, secrets, lambda *a, **k: notified.__setitem__("n", notified["n"] + 1))

    # 填好表单
    fields = collect(view, ft.TextField)
    by_label = {f.label: f for f in fields}
    by_label["显示名"].value = "测试用"
    by_label["Base URL"].value = "https://api.example.com/v1"
    by_label["模型 ID"].value = "test-model"
    by_label["API Key"].value = "sk-test-1234567890"

    # 点保存（按名字找，不写死控件类型）
    保存 = [c for c in collect_clickables(view) if "保存" in label_of(c)][0]
    保存.on_click(None)
    await _settle(page)

    assert secrets.saved, "密钥没有被保存"
    assert notified["n"] >= 1, "没有通知外面（配置变了）"


@pytest.mark.asyncio
async def test_settings_rejects_bad_base_url():
    """地址不合法时应该只显示提示，不保存密钥。"""
    from app.views.settings import build_settings_view

    page = FakePage()
    secrets = FakeSecrets()
    view = build_settings_view(page, secrets, lambda *a, **k: None)

    fields = {f.label: f for f in collect(view, ft.TextField)}
    fields["Base URL"].value = "file:///etc/passwd"      # 明确不允许
    fields["模型 ID"].value = "m"
    fields["API Key"].value = "sk-x"

    [c for c in collect_clickables(view) if "保存" in label_of(c)][0].on_click(None)
    await _settle(page)

    assert not secrets.saved, "地址非法时不该保存"


# ─────────────────────────────────────────────
#  聊天页
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_view_builds():
    from app.views.chat import build_chat_view

    page = FakePage()
    view = build_chat_view(page, lambda: None, lambda: None)
    assert view is not None
    # 至少有"发送"和"停止"两个可点控件（不写死控件类型，改样式不会误报）
    clickables = collect_clickables(view)
    assert len(clickables) >= 2, f"聊天页可点控件太少：{[label_of(c) for c in clickables]}"


@pytest.mark.asyncio
async def test_every_chat_button_is_clickable():
    from app.views.chat import build_chat_view

    page = FakePage()
    view = build_chat_view(page, lambda: None, lambda: None)

    clickables = collect_clickables(view)
    assert clickables
    for control in clickables:
        try:
            control.on_click(None)
        except TypeError as exc:
            pytest.fail(f"点 {label_of(control)!r} 时报参数错误：{exc}")
        await _settle(page)


@pytest.mark.asyncio
async def test_chat_without_config_shows_hint():
    """没配置 provider 时，发消息应该提示去设置页，而不是崩。"""
    from app.views.chat import build_chat_view

    page = FakePage()
    view = build_chat_view(page, lambda: None, lambda: None)   # get_chat 永远返回 None

    text_field = collect(view, ft.TextField)[0]
    text_field.value = "你好"
    text_field.on_submit(None)
    await _settle(page)

    # 应该出现一条提示
    texts = [c.value for c in collect(view, ft.Markdown)]
    assert any("设置" in str(t) for t in texts), f"没有出现提示，实际内容：{texts}"


@pytest.mark.asyncio
async def test_chat_empty_input_does_nothing():
    """空输入不该触发任何请求。"""
    from app.views.chat import build_chat_view

    page = FakePage()
    view = build_chat_view(page, lambda: None, lambda: None)

    text_field = collect(view, ft.TextField)[0]
    text_field.value = "   "
    text_field.on_submit(None)
    await _settle(page)

    assert not collect(view, ft.Markdown), "空输入不该产生气泡"


# ─────────────────────────────────────────────
#  恢复历史对话
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_render_history_draws_bubbles():
    """★ 回归测试：历史消息必须真的画成气泡。

    真实故障记录：
      app/main.py 的 startup() 只把历史塞进了 chat.messages，
      没有画到界面上 —— 状态栏写着"已恢复上次的 8 条对话"，
      聊天区一片空白。用户报的就是"对话历史记录没有显示出来"。
    """
    from app.views.chat import build_chat_view

    page = FakePage()
    view = build_chat_view(page, lambda: None, lambda: None)

    shown = view.render_history([
        {"role": "system", "content": "人格设定，不该显示"},
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在的"},
        {"role": "tool", "content": "{}"},
        {"role": "assistant", "content": ""},          # 空内容不画
    ])
    await _settle(page)

    assert shown == 2, f"应该只画 user/assistant 两条，实际 {shown}"

    texts = [m.value for m in collect(view, ft.Markdown)]
    assert "在吗" in texts, f"用户那条没画出来：{texts}"
    assert "在的" in texts, f"八千代那条没画出来：{texts}"
    assert not any("人格设定" in str(t) for t in texts), "system 消息不该显示"
    assert not any("{}" == str(t) for t in texts), "tool 消息不该显示"


@pytest.mark.asyncio
async def test_render_history_bubbles_are_at_normal_position():
    """★ 历史气泡不能带着"入场动画起点"的偏移。

    _bubble 默认 offset=(0, 0.06)（那是给新消息滑入用的）。
    历史气泡如果忘了归位，就会永远比正常位置低一点点。
    """
    from app.views.chat import build_chat_view

    page = FakePage()
    view = build_chat_view(page, lambda: None, lambda: None)
    view.render_history([{"role": "user", "content": "你好"}])
    await _settle(page)

    bubbles = collect(view, ft.Container)
    animated = [b for b in bubbles if getattr(b, "offset", None) == ft.Offset(0, 0.06)]
    assert not animated, "历史气泡还停在入场动画的起点上"


def test_render_history_with_nothing_to_show():
    """没有可画的内容时不该报错、也不该瞎刷界面。"""
    from app.views.chat import build_chat_view

    page = FakePage()
    view = build_chat_view(page, lambda: None, lambda: None)
    before = page.updates

    assert view.render_history([]) == 0
    assert view.render_history(None) == 0
    assert view.render_history([{"role": "system", "content": "x"}]) == 0
    assert page.updates == before, "没东西可画却刷了界面"


# ─────────────────────────────────────────────
#  自绘标题栏
# ─────────────────────────────────────────────

def test_title_bar_is_a_drag_area():
    """整条栏必须包在 WindowDragArea 里，否则窗口拖不动。"""
    from app.views.titlebar import build_title_bar

    bar = build_title_bar(on_minimize=lambda: None, on_close=lambda: None)
    assert isinstance(bar, ft.WindowDragArea)
    # 双击最大化对 440 宽的小挂件窗口没意义
    assert bar.maximizable is False


def test_title_bar_must_not_expand():
    """★ 回归测试：标题栏不能 expand —— 否则会吃掉半屏。

    真实故障记录：
      app/main.py 里 root 是 Column([标题栏, body])，body 是 expand=True。
      标题栏一开始也写了 expand=True，于是 Column 把可用高度按比例分给两者，
      标题栏占了半屏。用户看到的就是"关闭按钮上面一大块空白"。

      和之前 Row(wrap=True) + expand=True 那次是同一类问题：
      Python 侧结构看着完全正常，只有真的画出来才知道错了。
      所以这里盯死两件事：不 expand、高度写死。
    """
    from app.views.titlebar import BAR_HEIGHT, build_title_bar

    bar = build_title_bar(on_minimize=lambda: None, on_close=lambda: None)
    assert not getattr(bar, "expand", False), (
        "标题栏用了 expand：它和 body 会被 Column 平分高度，屏幕上就是一大块空白"
    )
    assert bar.height == BAR_HEIGHT, "标题栏高度没定死，会被父级拉伸"


def test_title_bar_buttons_are_wired_to_the_right_action():
    """★ 两个按钮不能接反 —— 接反了点"最小化"就把程序关了。

    所以 build_title_bar 的两个回调是关键字参数，而且这里逐个点名验证。
    """
    from app.views.titlebar import build_title_bar

    calls: list[str] = []
    bar = build_title_bar(
        on_minimize=lambda: calls.append("minimize"),
        on_close=lambda: calls.append("close"),
    )

    buttons = collect_clickables(bar)
    assert len(buttons) == 2, f"标题栏应该有两个按钮，实际 {len(buttons)}"

    by_tooltip = {str(b.tooltip): b for b in buttons}
    assert set(by_tooltip) == {"最小化", "关闭"}, f"按钮不对：{sorted(by_tooltip)}"

    # 传 None 当事件对象：签名对不上就会 TypeError（这个 bug 真发生过）
    by_tooltip["最小化"].on_click(None)
    by_tooltip["关闭"].on_click(None)
    assert calls == ["minimize", "close"], f"按钮接反了：{calls}"

