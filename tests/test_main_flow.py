"""从"引导完成"切到"主界面"这条链的测试。

这条链以前完全没有测试覆盖 —— 而它恰好是用户报"点完没进主界面"的地方。

链路是：
    OOBE 的 on_done
        → main.py 的 on_oobe_done
            → page.run_task(go)
                → state.rebuild_chat()
                → show_main()          ← 把 root.content 换成 Tabs
                    → page.update()

这里用一个假的 Page 把每一环都跑一遍，断言界面真的被换掉了。
"""

import asyncio
import shutil
import uuid
from pathlib import Path

import flet as ft
import pytest

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "main"


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    root = _TMP_ROOT / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(root))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class FakeWindow:
    """够用的假窗口。真实 ft.Window 上 close()/destroy() 都是 async。"""

    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self.closed = False
        self.destroyed = False

    async def close(self) -> None:
        self.closed = True

    async def destroy(self) -> None:
        self.destroyed = True


class FakePage:
    """够用的假 Page。"""

    def __init__(self) -> None:
        self.controls: list = []
        self.updates = 0
        self.tasks: list = []
        self.window = FakeWindow()

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def update(self) -> None:
        self.updates += 1

    def run_task(self, fn, *args, **kwargs):
        # 立刻执行（和 Flet 一样：调度到事件循环）
        task = asyncio.ensure_future(fn(*args, **kwargs))
        self.tasks.append(task)
        return task


class FakeSecrets:
    def __init__(self, keys: dict | None = None) -> None:
        self.saved = dict(keys or {})

    async def exists(self, pid: str) -> bool:
        return pid in self.saved

    async def load(self, pid: str):
        return self.saved.get(pid)

    async def save(self, pid: str, key: str, *, persist: bool = True) -> None:
        self.saved[pid] = key

    async def delete(self, pid: str) -> None:
        self.saved.pop(pid, None)


async def _drain(page: FakePage) -> None:
    """等所有调度出去的任务跑完。"""
    while page.tasks:
        pending = list(page.tasks)
        page.tasks.clear()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)


def _find(control, wanted):
    found = []
    if isinstance(control, wanted):
        found.append(control)
    for attr in ("controls", "content", "tabs"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            for item in value:
                found += _find(item, wanted)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            found += _find(value, wanted)
    return found


def _type_names(node) -> list[str]:
    """把一个（已解码的）msgpack 结构里所有控件的类型名收集出来。

    Flet 的协议里，每个控件带一个 `_c` 字段，值就是 Dart 端用来找控件的类型名。
    """
    names: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("_c"), str):
            names.append(node["_c"])
        for key, value in node.items():
            if key not in ("_c", "_i", "_p"):
                names += _type_names(value)
    elif isinstance(node, list):
        for item in node:
            names += _type_names(item)
    return names


# ─────────────────────────────────────────────
#  序列化：Python 侧"对象都在" ≠ 客户端"画得出来"
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_main_view_serializes_to_something_the_client_can_build():
    """★ 把主界面真的走一遍 Flet 的 msgpack 编码，检查发出去的到底是什么。

    为什么值得测：
      "Python 里的控件树是对的"和"用户屏幕上出现了东西"之间隔着两层 ——
      Flet 的编码，以及 Flutter 端按 `_c` 类型名去找控件。
      这里至少把编码那一层钉死：Tabs / TabBar / TabBarView 和两个页面的
      控件都必须真的在报文里。没有这一步，界面全白也只会在用户那边发现。
    """
    import msgpack

    from flet.messaging.protocol import configure_encode_object_for_msgpack

    from app import main as main_mod
    from core.config import ProviderConfig, load_config, save_config, upsert_provider

    cfg = load_config()
    upsert_provider(cfg, ProviderConfig(
        id="deepseek", display_name="DeepSeek",
        base_url="https://api.deepseek.com/v1", model_id="deepseek-chat",
    ))
    cfg.active_provider = "deepseek"
    save_config(cfg)

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    root = page.controls[0]
    tabs = _find(root.content, ft.Tabs)
    assert tabs, "主界面里没有 Tabs"

    encoder = configure_encode_object_for_msgpack(ft.BaseControl)
    packed = msgpack.packb(tabs[0], default=encoder)
    assert packed, "序列化出来是空的"
    decoded = msgpack.unpackb(packed, raw=False)

    names = _type_names(decoded)
    for needed in ("Tabs", "TabBar", "TabBarView", "Column"):
        assert needed in names, f"发出去的报文里没有 {needed}：{sorted(set(names))}"

    # 两个页面都得在里面：对话页有输入框，设置页有 provider 下拉
    assert names.count("TextField") >= 2, "对话页/设置页的输入框没进报文"
    assert "Dropdown" in names, "设置页的 provider 下拉没进报文"
    assert "FilledButton" in names, "设置页的按钮没进报文"


# ─────────────────────────────────────────────
#  启动时恢复历史对话
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_startup_shows_saved_conversation(monkeypatch):
    """★ 回归测试：上次的对话要**画在屏幕上**，不能只塞进内存。

    用户报的问题：主界面里对话历史记录没有显示出来。
    原因：startup() 只做了 state.chat.messages.extend(saved)，
    没让界面把它画出来 —— 状态栏还写着"已恢复上次的 N 条对话"。
    """
    from app import main as main_mod
    from core import store

    _write_config()

    # 先造一份"上次的对话"
    store.save_conversation([
        {"role": "user", "content": "昨天那个问题"},
        {"role": "assistant", "content": "我想想"},
    ])

    fake_store = FakeSecrets({"deepseek": "sk-history"})

    class PatchedStore:
        def __new__(cls, *a, **kw):
            return fake_store

    monkeypatch.setattr(main_mod, "SecretStore", PatchedStore)

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    root = page.controls[0]
    on_screen = [m.value for m in _find(root, ft.Markdown)]
    assert "昨天那个问题" in on_screen, (
        f"历史里的用户消息没画出来，屏幕上只有：{on_screen}"
    )
    assert "我想想" in on_screen, (
        f"历史里的回复没画出来，屏幕上只有：{on_screen}"
    )


# ─────────────────────────────────────────────
#  自绘标题栏（原生标题栏被隐藏之后，拖动和关闭只能自己提供）
# ─────────────────────────────────────────────

def _write_config() -> None:
    from core.config import ProviderConfig, load_config, save_config, upsert_provider

    cfg = load_config()
    upsert_provider(cfg, ProviderConfig(
        id="deepseek", display_name="DeepSeek",
        base_url="https://api.deepseek.com/v1", model_id="deepseek-chat",
    ))
    cfg.active_provider = "deepseek"
    save_config(cfg)


@pytest.mark.asyncio
async def test_main_hides_native_title_bar_and_ships_its_own():
    """★ 原生 Windows 标题栏要关掉，而且必须自带一条能拖、能关的栏。

    关掉原生标题栏却忘了补自己的，用户就会既不能移动也不能关闭窗口 ——
    这是"能启动 ≠ 能点"的典型。
    """
    from app import main as main_mod

    _write_config()

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    assert page.window.title_bar_hidden is True, "没有隐藏 Windows 原生标题栏"

    root = page.controls[0]
    drag_areas = _find(root, ft.WindowDragArea)
    assert drag_areas, "没有自绘标题栏 —— 窗口将无法拖动"

    close_buttons = [
        b for b in _find(root, ft.IconButton) if "关闭" in str(getattr(b, "tooltip", ""))
    ]
    assert close_buttons, "自绘标题栏上没有关闭按钮 —— 窗口将无法关闭"

    # 点一下：不能报参数错误，而且要真的走到 window.close()
    close_buttons[0].on_click(None)
    await _drain(page)
    assert page.window.closed is True, "点了关闭按钮，窗口却没关"


@pytest.mark.asyncio
async def test_close_button_saves_conversation_before_closing(monkeypatch):
    """关闭前必须把对话存下来。

    原生标题栏没了以后，这个按钮是**唯一**的关闭入口，
    不能出现"关了但聊天记录丢了"。
    """
    from app import main as main_mod
    from core import store

    class FakeChat:
        def __init__(self, kwargs) -> None:
            self.kwargs = kwargs
            self.messages = [
                {"role": "system", "content": "人格设定"},
                {"role": "user", "content": "在吗"},
                {"role": "assistant", "content": "在的"},
            ]

    monkeypatch.setattr(main_mod, "Chat", FakeChat)

    fake_store = FakeSecrets({"deepseek": "sk-close-test"})

    class PatchedStore:
        def __new__(cls, *a, **kw):
            return fake_store

    monkeypatch.setattr(main_mod, "SecretStore", PatchedStore)

    _write_config()

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    root = page.controls[0]
    close_btn = [
        b for b in _find(root, ft.IconButton) if "关闭" in str(getattr(b, "tooltip", ""))
    ][0]

    close_btn.on_click(None)
    await _drain(page)

    saved = store.load_conversation()
    assert [m.get("content") for m in saved] == ["在吗", "在的"], \
        f"关闭前没把对话存好，实际存了：{saved}"
    assert page.window.closed is True


@pytest.mark.asyncio
async def test_main_column_stretches_its_children():
    """★ 回归测试：装标题栏的那个 Column 必须 STRETCH。

    Column 默认 horizontal_alignment=START，子项只占"内容宽度"。
    不改成 STRETCH 的话，标题栏会缩成一小撮、关闭按钮挤到左边，
    而不是贴在窗口右上角。
    """
    from app import main as main_mod
    from core.config import config_path

    config_path().unlink(missing_ok=True)

    page = FakePage()
    main_mod.main(page)

    root = page.controls[0]
    column = root.content
    assert isinstance(column, ft.Column)
    assert column.horizontal_alignment == ft.CrossAxisAlignment.STRETCH, (
        "外层 Column 没有 STRETCH，标题栏撑不满宽度"
    )
    assert column.expand is True


@pytest.mark.asyncio
async def test_title_bar_does_not_cover_the_oobe():
    """引导页也要能在自绘标题栏下面正常显示。"""
    from app import main as main_mod
    from core.config import config_path

    config_path().unlink(missing_ok=True)

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    root = page.controls[0]
    assert _find(root, ft.WindowDragArea), "引导阶段没有标题栏（那用户拖不动窗口）"
    texts = " ".join(str(t.value) for t in _find(root, ft.Text) if t.value)
    assert "API Key" in texts, "标题栏把引导页挤没了"


# ─────────────────────────────────────────────
#  有配置时：启动就该进主界面
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_boot_with_config_shows_main():
    """已有配置时，启动流程应该把界面换成 Tabs（对话/设置）。"""
    from app import main as main_mod
    from core.config import ProviderConfig, load_config, save_config, upsert_provider

    # ★ 先造出"已经配置过"的状态。
    #   这个测试以前漏了这一步，于是 boot() 走的是引导分支，
    #   断言就成了在测另一个流程（失败信息还是误导人的）。
    cfg = load_config()
    upsert_provider(cfg, ProviderConfig(
        id="deepseek", display_name="DeepSeek",
        base_url="https://api.deepseek.com/v1", model_id="deepseek-chat",
    ))
    cfg.active_provider = "deepseek"
    save_config(cfg)

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    root = page.controls[0]
    assert isinstance(root, ft.Container), "根容器没加上"
    assert root.content is not None, "启动后界面仍是空的"

    # 应该是 Tabs（主界面），而不是引导界面
    tabs = _find(root.content, ft.Tabs)
    assert tabs, f"应该显示主界面 Tabs，实际是 {type(root.content).__name__}"


# ─────────────────────────────────────────────
#  没有配置时：启动进引导
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_boot_without_config_shows_oobe():
    from app import main as main_mod
    from core.config import config_path

    # 确保没有配置文件
    config_path().unlink(missing_ok=True)

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    root = page.controls[0]
    assert root.content is not None
    # 引导界面里应该有"月见八千代"和"API Key"
    texts = " ".join(str(t.value) for t in _find(root.content, ft.Text) if t.value)
    assert "API Key" in texts, "没有显示引导界面"
    assert not _find(root.content, ft.Tabs), "没有配置却进了主界面"


# ─────────────────────────────────────────────
#  ★ 核心：引导完成 → 主界面
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_oobe_done_switches_to_main(monkeypatch):
    """★ 这是用户报的 bug：走完引导后没进主界面。

    这里把整条链跑一遍，断言 root.content 从引导界面变成主界面 Tabs。
    """
    from app import main as main_mod
    from app.views import oobe as oobe_mod
    from core.config import config_path, get_provider, load_config

    # 从"没有配置"开始
    config_path().unlink(missing_ok=True)

    # 把测试连接换成假的（不联网）
    async def fake_test(provider, api_key, **kwargs):
        return True, "连接成功。"

    monkeypatch.setattr(oobe_mod, "test_connection", fake_test)

    page = FakePage()

    # 关键连接点：oobe_done_handler 会在 build_oobe 之后被调用。
    # 这里用 main() 自己的流程，但把 secrets 换成假的（才能拿到"已保存的 key"）。
    captured: dict = {}
    real_build_oobe = oobe_mod.build_oobe
    real_main_state = None

    # 让 main() 用一个能被我们控制的 secret store：
    # AppState 在 main() 内部创建，所以这里打个补丁替换掉类
    real_secret_store = main_mod.SecretStore
    fake_store = FakeSecrets()

    class PatchedStore:
        def __new__(cls, *a, **kw):
            return fake_store

    monkeypatch.setattr(main_mod, "SecretStore", PatchedStore)

    # 捕获 on_oobe_done，方便我们手动触发
    def capturing_build_oobe(page_, secrets_, on_done):
        captured["on_done"] = on_done
        return real_build_oobe(page_, secrets_, on_done)

    monkeypatch.setattr(main_mod, "build_oobe", capturing_build_oobe)

    main_mod.main(page)
    await _drain(page)

    # 确认现在在引导界面
    root = page.controls[0]
    assert "on_done" in captured, "main() 没有把 on_done 传进引导"
    assert not _find(root.content, ft.Tabs), "一开始不该是主界面"

    # 模拟引导完成（走的是内部 finish()：先存配置、再存密钥、再回调）
    # 这里直接调 on_done 就够 —— 前面那些由 OOBE 自己的测试覆盖
    fake_store.saved["deepseek"] = "sk-test-key"
    # 引导会先写好配置，所以这里也补上（模拟 finish 做过的事）
    from core.config import ProviderConfig, save_config, upsert_provider
    cfg = load_config()
    upsert_provider(cfg, ProviderConfig(
        id="deepseek", display_name="DeepSeek",
        base_url="https://api.deepseek.com/v1", model_id="deepseek-chat",
    ))
    cfg.active_provider = "deepseek"
    save_config(cfg)

    captured["on_done"](test_connected=True)
    await _drain(page)

    # ★ 核心断言：界面应该已经换成主界面
    assert _find(root.content, ft.Tabs), (
        f"引导完成后没有切到主界面，界面还是 {type(root.content).__name__}"
    )


@pytest.mark.asyncio
async def test_oobe_done_without_persistence_still_shows_main(monkeypatch):
    """★ 密钥没存住（persisted=False）时，主界面照样要出来。

    这是用户报的"点完测试并开始还是不进"那条链的最后一道保险：
    存 key 失败绝不能变成"进不去"。
    """
    from app import main as main_mod
    from core.config import config_path

    config_path().unlink(missing_ok=True)

    fake_store = FakeSecrets({"deepseek": "sk-session-only"})

    class PatchedStore:
        def __new__(cls, *a, **kw):
            return fake_store

    monkeypatch.setattr(main_mod, "SecretStore", PatchedStore)

    captured: dict = {}
    real_build_oobe = main_mod.build_oobe

    def capturing(page_, secrets_, on_done):
        captured["on_done"] = on_done
        return real_build_oobe(page_, secrets_, on_done)

    monkeypatch.setattr(main_mod, "build_oobe", capturing)

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    from core.config import ProviderConfig, load_config, save_config, upsert_provider
    cfg = load_config()
    upsert_provider(cfg, ProviderConfig(
        id="deepseek", display_name="DeepSeek",
        base_url="https://api.deepseek.com/v1", model_id="deepseek-chat",
    ))
    cfg.active_provider = "deepseek"
    save_config(cfg)

    captured["on_done"](test_connected=True, persisted=False)
    await _drain(page)

    root = page.controls[0]
    assert _find(root.content, ft.Tabs), (
        "密钥没能持久化，界面就没进主界面 —— 用户会被卡死"
    )


@pytest.mark.asyncio
async def test_show_main_works_without_provider(monkeypatch):
    """没配置 provider 时也要能进主界面（只是提示去设置页）。

    否则用户点"以后再说"会被卡在引导界面 —— 那就等于没有"跳过"这个功能。
    """
    from app import main as main_mod
    from core.config import config_path

    config_path().unlink(missing_ok=True)

    fake_store = FakeSecrets()

    class PatchedStore:
        def __new__(cls, *a, **kw):
            return fake_store

    monkeypatch.setattr(main_mod, "SecretStore", PatchedStore)

    captured: dict = {}
    real_build_oobe = main_mod.build_oobe

    def capturing(page_, secrets_, on_done):
        captured["on_done"] = on_done
        return real_build_oobe(page_, secrets_, on_done)

    monkeypatch.setattr(main_mod, "build_oobe", capturing)

    page = FakePage()
    main_mod.main(page)
    await _drain(page)

    root = page.controls[0]
    assert not _find(root.content, ft.Tabs)

    # 模拟"以后再说"：没有配置、没有密钥
    captured["on_done"](test_connected=False)
    await _drain(page)

    assert _find(root.content, ft.Tabs), (
        "点「以后再说」之后没有进主界面 —— 那用户就被永久卡在引导里了"
    )
