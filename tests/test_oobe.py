"""OOBE（首次启动引导）的测试。

引导的价值不只是"教用户填"，而是把"你需要自己准备 API Key"
这件事说清楚。所以这里除了测流程，也测**关键文案有没有出现**。

关于交互的一条重要约定（也是这个文件的重点）：
  **没选服务时，主按钮是"禁用"状态。**
  这比"点了之后给个警告"好 —— 用户不会遇到"点了没反应"的困惑。
"""

import asyncio
import shutil
import uuid
from pathlib import Path

import flet as ft
import pytest

_TMP_ROOT = Path(__file__).resolve().parent.parent / ".ptmp" / "oobe"


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
    def __init__(self) -> None:
        self.tasks: list = []

    def update(self) -> None:
        pass

    def run_task(self, fn, *args, **kwargs):
        task = asyncio.ensure_future(fn(*args, **kwargs))
        self.tasks.append(task)
        return task


class FakeSecrets:
    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    async def exists(self, pid: str) -> bool:
        return pid in self.saved

    async def load(self, pid: str):
        return self.saved.get(pid)

    async def save(self, pid: str, key: str, *, persist: bool = True) -> None:
        self.saved[pid] = key

    async def delete(self, pid: str) -> None:
        self.saved.pop(pid, None)

    # 引导页会问"密钥存在哪"，用来在界面上如实说明
    persistent = True

    def backend_label(self) -> str:
        return "Windows 凭据管理器"


class SessionOnlySecrets(FakeSecrets):
    """模拟系统凭据库不可用：持久化直接失败，只能存内存。"""

    persistent = False

    def backend_label(self) -> str:
        return "仅本次运行（内存）"

    async def save(self, pid: str, key: str, *, persist: bool = True) -> None:
        if persist:
            raise OSError(1312, "CredWriteW 失败：没有可用的登录会话")
        await super().save(pid, key, persist=persist)


# ─────────────────────────────────────────────
#  工具
# ─────────────────────────────────────────────

def collect(control, wanted):
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


_BUTTON_CLASSES = (ft.FilledButton, ft.TextButton, ft.Button, ft.IconButton)


def buttons(view) -> list:
    out = []
    for cls in _BUTTON_CLASSES:
        out += collect(view, cls)
    return out


def button_text(control) -> str:
    for attr in ("content", "text"):
        value = getattr(control, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def main_button(view):
    """主按钮：第 1、2 步叫「继续」，第 3 步叫「测试并开始」。"""
    for wanted in ("继续", "测试并开始"):
        for b in buttons(view):
            if wanted in button_text(b):
                return b
    return None


def named_button(view, text):
    for b in buttons(view):
        if text in button_text(b):
            return b
    return None


def labels(view) -> str:
    """把界面里所有文字拼起来，方便断言文案。"""
    parts = []
    for t in collect(view, ft.Text):
        if t.value:
            parts.append(str(t.value))
    for tf in collect(view, ft.TextField):
        for v in (tf.label, tf.hint_text):
            if v:
                parts.append(str(v))
    return " | ".join(parts)


def service_cards(view):
    """服务选择卡片：绑了 on_click 的 Container，且里面有服务名。"""
    names = ("DeepSeek", "OpenAI", "OpenRouter", "Ollama")
    out = []
    for c in collect(view, ft.Container):
        if c.on_click is None:
            continue
        inside = " ".join(str(t.value) for t in collect(c, ft.Text) if t.value)
        if any(n in inside for n in names):
            out.append(c)
    return out


def pick_service(view, name: str) -> bool:
    for c in service_cards(view):
        inside = " ".join(str(t.value) for t in collect(c, ft.Text) if t.value)
        if name in inside:
            c.on_click(None)
            return True
    return False


async def settle(page: FakePage) -> None:
    if page.tasks:
        await asyncio.gather(*page.tasks, return_exceptions=True)
        page.tasks.clear()
    await asyncio.sleep(0.05)


async def click(view, page, text: str) -> None:
    b = named_button(view, text)
    assert b is not None, f"找不到按钮：{text}（现有：{[button_text(x) for x in buttons(view)]}）"
    b.on_click(None)
    await settle(page)


# ─────────────────────────────────────────────
#  第一步：欢迎 + 关键文案
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_oobe_builds():
    from app.views.oobe import build_oobe

    page = FakePage()
    view = build_oobe(page, FakeSecrets(), lambda **k: None)
    assert view is not None


@pytest.mark.asyncio
async def test_first_step_explains_key_requirement():
    """第一屏必须说清"需要你自己准备 Key"和"key 存在你电脑上"。"""
    from app.views.oobe import build_oobe

    page = FakePage()
    view = build_oobe(page, FakeSecrets(), lambda **k: None)
    text = labels(view)

    assert "API Key" in text, "没提到需要 API Key"
    assert "凭据管理器" in text, "没说明 key 存在哪"
    assert "不经过" in text, "没说明请求发往哪"
    assert "月见八千代" in text, "没有介绍自己"


# ─────────────────────────────────────────────
#  第二步：选服务
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_second_step_lists_four_services():
    from app.views.oobe import build_oobe

    page = FakePage()
    view = build_oobe(page, FakeSecrets(), lambda **k: None)
    await click(view, page, "继续")

    text = labels(view)
    for name in ("DeepSeek", "OpenAI", "OpenRouter", "Ollama"):
        assert name in text, f"推荐列表里没有 {name}"

    cards = service_cards(view)
    assert len(cards) == 4, f"应该有 4 张可点卡片，实际 {len(cards)}"


@pytest.mark.asyncio
async def test_service_cards_are_actually_visible():
    """★ 回归测试：服务卡片必须真的能被渲染出来。

    真实 bug 记录：
      最初把卡片放在 ft.Row(wrap=True, expand=True) 里。
      wrap=True 在 Flutter 里是 Wrap，而 Wrap 的子项不支持 expand ——
      卡片被算成宽度 0，界面上一片空白（Python 侧看控件"都在"，所以单测过了）。
      修法：改用 ResponsiveRow + col。

    所以这条测试盯两件事：卡片有 col 断点，而且没有 expand。
    """
    from app.views.oobe import build_oobe

    page = FakePage()
    view = build_oobe(page, FakeSecrets(), lambda **k: None)
    await click(view, page, "继续")

    cards = service_cards(view)
    assert cards, "第 2 步没有服务卡片"

    for card in cards:
        labels_in_card = [t.value for t in collect(card, ft.Text) if t.value]
        # 不能依赖 expand —— 放在 Wrap 里会变成 0 宽
        assert not getattr(card, "expand", False), (
            f"卡片 {labels_in_card} 用了 expand，在 wrap 布局里会变成 0 宽（渲染成空白）"
        )
        # 必须有 col 断点，否则 ResponsiveRow 不知道给它多宽
        assert getattr(card, "col", None), (
            f"卡片 {labels_in_card} 没有 col 断点，ResponsiveRow 里宽度不确定"
        )


@pytest.mark.asyncio
async def test_next_is_disabled_until_service_picked():
    """★ 没选服务时主按钮禁用 —— 用户不可能"点了没反应"。"""
    from app.views.oobe import build_oobe

    page = FakePage()
    view = build_oobe(page, FakeSecrets(), lambda **k: None)
    await click(view, page, "继续")

    btn = main_button(view)
    assert btn is not None
    assert btn.disabled is True, "没选服务时主按钮应该禁用"

    assert pick_service(view, "DeepSeek")
    await settle(page)
    btn = main_button(view)
    assert btn.disabled is False, "选了服务之后主按钮应该可用"


@pytest.mark.asyncio
async def test_selecting_service_highlights_it():
    """选中的卡片要有视觉区别（边框或底色变成强调色）。"""
    from app.views.oobe import build_oobe

    page = FakePage()
    view = build_oobe(page, FakeSecrets(), lambda **k: None)
    await click(view, page, "继续")

    assert pick_service(view, "DeepSeek")
    await settle(page)

    highlighted = [
        c for c in service_cards(view)
        if "3482FF" in str(c.border) or "3482FF" in str(c.bgcolor)
    ]
    assert highlighted, "没有任何卡片显示为选中态"


@pytest.mark.asyncio
async def test_back_button_returns_to_step_one():
    from app.views.oobe import build_oobe

    page = FakePage()
    view = build_oobe(page, FakeSecrets(), lambda **k: None)
    await click(view, page, "继续")
    assert "你已经有 Key" in labels(view)

    await click(view, page, "上一步")
    assert "API Key" in labels(view), "没回到第一步"


# ─────────────────────────────────────────────
#  第三步：填 key + 测试
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_flow_saves_config_and_key(monkeypatch):
    """走完三步（测试连接用假的），应该存下配置和密钥，并回调 on_done。"""
    from app.views import oobe as oobe_mod

    async def fake_test(provider, api_key, **kwargs):
        assert api_key == "sk-oobe-test"
        assert provider.id == "deepseek"
        return True, "连接成功。"

    monkeypatch.setattr(oobe_mod, "test_connection", fake_test)

    page = FakePage()
    secrets = FakeSecrets()
    done = {"called": 0, "tested": None, "persisted": None}

    def on_done(*, test_connected: bool, persisted: bool = True) -> None:
        done["called"] += 1
        done["tested"] = test_connected
        done["persisted"] = persisted

    view = oobe_mod.build_oobe(page, secrets, on_done)

    await click(view, page, "继续")
    assert pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")

    collect(view, ft.TextField)[0].value = "sk-oobe-test"
    await click(view, page, "测试并开始")
    await asyncio.sleep(0.1)

    assert secrets.saved.get("deepseek") == "sk-oobe-test", "密钥没存下来"
    assert done["called"] == 1, f"on_done 应该只被调用一次，实际 {done['called']} 次"
    assert done["tested"] is True
    assert done["persisted"] is True

    from core.config import get_provider, load_config
    provider = get_provider(load_config(), "deepseek")
    assert provider is not None
    assert provider.base_url == "https://api.deepseek.com/v1"
    assert provider.model_id == "deepseek-chat"


@pytest.mark.asyncio
async def test_storage_failure_still_enters_app(monkeypatch):
    """★ 回归测试：连上了、但密钥存不住时，用户仍然要能进主界面。

    真实故障记录：
      系统凭据库调用失败时，finish() 直接 return —— 用户看到的画面是
      "点了「测试并开始」，然后什么都没发生"，因为连接其实是通的、
      配置也写下去了，只有密钥那一步死在半路。
      产品上的正确行为是：降级成"仅本次运行有效"，放人进去，并如实告知。
    """
    from app.views import oobe as oobe_mod

    async def fake_test(provider, api_key, **kwargs):
        return True, "连接成功。"

    monkeypatch.setattr(oobe_mod, "test_connection", fake_test)

    page = FakePage()
    secrets = SessionOnlySecrets()
    done = {"called": 0, "tested": None, "persisted": None}

    def on_done(*, test_connected: bool, persisted: bool = True) -> None:
        done["called"] += 1
        done["tested"] = test_connected
        done["persisted"] = persisted

    view = oobe_mod.build_oobe(page, secrets, on_done)

    await click(view, page, "继续")
    assert pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")
    collect(view, ft.TextField)[0].value = "sk-no-keystore"
    await click(view, page, "测试并开始")
    await asyncio.sleep(0.1)

    assert done["called"] == 1, "存不住密钥就把用户扣在引导里了"
    assert done["tested"] is True
    assert done["persisted"] is False, "没存住却报告存住了"
    # 配置还是要写下来的
    from core.config import get_provider, load_config
    assert get_provider(load_config(), "deepseek") is not None
    # 本次运行内必须可用（否则进了主界面也是坏的）
    assert await secrets.load("deepseek") == "sk-no-keystore"


@pytest.mark.asyncio
async def test_step_three_says_where_key_is_stored():
    """第 3 步要如实写出 key 存到哪，存不住要提前警告。"""
    from app.views.oobe import build_oobe

    page = FakePage()
    view = build_oobe(page, FakeSecrets(), lambda **k: None)
    await click(view, page, "继续")
    pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")

    assert "Windows 凭据管理器" in labels(view), "没告诉用户 key 存在哪"
    assert "只能保留在本次运行中" not in labels(view), "能存住却吓唬用户"

    # 换一个存不住的后端再看
    page2 = FakePage()
    view2 = build_oobe(page2, SessionOnlySecrets(), lambda **k: None)
    await click(view2, page2, "继续")
    pick_service(view2, "DeepSeek")
    await settle(page2)
    await click(view2, page2, "继续")
    assert "只能保留在本次运行中" in labels(view2), "存不住却没提前警告"


@pytest.mark.asyncio
async def test_connection_failure_keeps_user_on_step(monkeypatch):
    """测试连接失败时不能保存，也不能放他进去。"""
    from app.views import oobe as oobe_mod

    async def fake_test(provider, api_key, **kwargs):
        return False, "API Key 无效或已被撤销（401）。"

    monkeypatch.setattr(oobe_mod, "test_connection", fake_test)

    page = FakePage()
    secrets = FakeSecrets()
    done = {"called": 0}
    view = oobe_mod.build_oobe(
        page, secrets, lambda **k: done.__setitem__("called", done["called"] + 1)
    )

    await click(view, page, "继续")
    pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")
    collect(view, ft.TextField)[0].value = "sk-bad"
    await click(view, page, "测试并开始")
    await asyncio.sleep(0.1)

    assert not secrets.saved, "连接失败却把密钥存下来了"
    assert done["called"] == 0, "连接失败却放他进主界面了"
    assert "401" in labels(view), "没有把失败原因显示出来"


@pytest.mark.asyncio
async def test_empty_key_is_rejected():
    """不填 key 就点"测试并开始"，应该给出提示且不保存。"""
    from app.views.oobe import build_oobe

    page = FakePage()
    secrets = FakeSecrets()
    view = build_oobe(page, secrets, lambda **k: None)

    await click(view, page, "继续")
    pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")
    await click(view, page, "测试并开始")      # 故意不填 key

    assert not secrets.saved
    assert "请先粘贴" in labels(view), "没有提示要填 key"


# ─────────────────────────────────────────────
#  跳过 与 幂等
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_skip_enters_app_without_config():
    """"以后再说"应该直接进主界面，不报错。"""
    from app.views.oobe import build_oobe

    page = FakePage()
    done = {"called": 0}
    view = build_oobe(
        page, FakeSecrets(), lambda **k: done.__setitem__("called", done["called"] + 1)
    )

    await click(view, page, "以后再说")
    assert done["called"] == 1, "「以后再说」没有生效"


@pytest.mark.asyncio
async def test_exception_in_test_connection_does_not_hang(monkeypatch):
    """★ 回归测试：测试连接抛异常时，界面不能卡死。

    真实 bug 记录：
      我原来直接 await test_connection(...)，没包 try/except。
      一旦它抛出没预料到的异常，go_next 就中断了 ——
      按钮永远停在"测试中…"且禁用，提示永远停在"正在测试连接…"，
      而用户界面上完全看不出发生了什么（异常只在日志里）。
      表现就是"点完没反应、也没进主界面"。
    """
    from app.views import oobe as oobe_mod

    async def boom(provider, api_key, **kwargs):
        raise RuntimeError("模拟没预料到的错误")

    monkeypatch.setattr(oobe_mod, "test_connection", boom)

    page = FakePage()
    secrets = FakeSecrets()
    view = oobe_mod.build_oobe(page, secrets, lambda **k: None)

    await click(view, page, "继续")
    pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")
    collect(view, ft.TextField)[0].value = "sk-x"

    b = main_button(view)
    b.on_click(None)
    await settle(page)
    await asyncio.sleep(0.1)

    btn = main_button(view)
    assert btn is not None, "按钮不见了"
    assert btn.disabled is False, "按钮卡住了：异常之后没有恢复可点"
    assert "测试中" not in button_text(btn), "按钮一直停在「测试中…」"
    assert "出错" in labels(view) or "失败" in labels(view), "没有告诉用户出错了"
    assert not secrets.saved, "出错时不该保存密钥"


@pytest.mark.asyncio
async def test_can_retry_after_failure(monkeypatch):
    """失败之后应该能重新试（按钮恢复可点，再次点击会重新请求）。"""
    from app.views import oobe as oobe_mod

    calls = {"n": 0}

    async def flaky(provider, api_key, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return False, "第一次失败。"
        return True, "连接成功。"

    monkeypatch.setattr(oobe_mod, "test_connection", flaky)

    page = FakePage()
    secrets = FakeSecrets()
    view = oobe_mod.build_oobe(page, secrets, lambda **k: None)

    await click(view, page, "继续")
    pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")
    collect(view, ft.TextField)[0].value = "sk-x"

    await click(view, page, "测试并开始")          # 第一次：失败
    assert not secrets.saved
    assert main_button(view).disabled is False

    # 再点一次（按钮文字应该已经恢复成"测试并开始"）
    await click(view, page, "测试并开始")          # 第二次：成功
    assert calls["n"] == 2, "重试没有真的再发请求"
    assert secrets.saved.get("deepseek") == "sk-x"


@pytest.mark.asyncio
async def test_on_done_called_only_once(monkeypatch):
    """★ 回归测试：on_done 曾经被反复调用。它必须只触发一次。"""
    from app.views import oobe as oobe_mod

    async def fake_test(provider, api_key, **kwargs):
        return True, "连接成功。"

    monkeypatch.setattr(oobe_mod, "test_connection", fake_test)

    page = FakePage()
    calls = {"n": 0}
    view = oobe_mod.build_oobe(
        page, FakeSecrets(), lambda **k: calls.__setitem__("n", calls["n"] + 1)
    )

    await click(view, page, "继续")
    pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")
    collect(view, ft.TextField)[0].value = "sk-x"

    # 连点主按钮
    for _ in range(3):
        b = main_button(view)
        if b is not None and not b.disabled:
            b.on_click(None)
        await settle(page)
        await asyncio.sleep(0.05)

    assert calls["n"] == 1, f"on_done 被调用了 {calls['n']} 次，应该只有 1 次"


@pytest.mark.asyncio
async def test_busy_state_prevents_duplicate_requests(monkeypatch):
    """★ 测试连接期间重复点按钮，不能重复发请求。

    这是为了修一个真实体验问题：
      首次 import litellm 要 7 秒多，期间用户会以为"点了没反应"而反复点。
      所以按钮要进入忙碌态（文字变"测试中…"），并且忽略重复点击。
    """
    from app.views import oobe as oobe_mod

    calls = {"n": 0}
    release = asyncio.Event()

    async def slow_test(provider, api_key, **kwargs):
        calls["n"] += 1
        await release.wait()          # 卡住，模拟慢网络
        return True, "连接成功。"

    monkeypatch.setattr(oobe_mod, "test_connection", slow_test)

    page = FakePage()
    view = oobe_mod.build_oobe(page, FakeSecrets(), lambda **k: None)

    await click(view, page, "继续")
    pick_service(view, "DeepSeek")
    await settle(page)
    await click(view, page, "继续")
    collect(view, ft.TextField)[0].value = "sk-x"

    # 点一次，让它卡在 slow_test 里
    main_button(view).on_click(None)
    await asyncio.sleep(0.05)

    # 按钮应该已经变成忙碌态
    busy_btn = main_button(view)
    assert busy_btn is None or "测试中" in button_text(busy_btn) or busy_btn.disabled, \
        "测试期间按钮没有进入忙碌态"

    # 这时候再连点三次
    for _ in range(3):
        b = main_button(view)
        if b is not None and not b.disabled:
            b.on_click(None)
        await asyncio.sleep(0.02)

    assert calls["n"] == 1, f"重复点击导致发了 {calls['n']} 次请求，应该只有 1 次"

    release.set()                     # 放行
    await settle(page)
    await asyncio.sleep(0.1)
