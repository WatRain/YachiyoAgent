"""OOBE（Out-Of-Box Experience）：首次启动引导。

为什么值得单独做一个界面：
  用户第一次打开时，面对一个空的聊天窗口完全不知道要干什么。
  引导的价值不只是"教他填"，而是**把"你需要自己准备 API Key"这件事说清楚** ——
  这是产品的核心设定，不能靠用户猜。

交互上刻意做的事（都是为了避免"点了没反应"的困惑）：
  · 服务选择做成两列卡片，**选中的卡片有明确的高亮边框**
  · 没选服务时"下一步"按钮是**禁用的**，不是点了给个看不见的警告
  · 提示行放在**固定位置**（所有步骤都看得到），用颜色区分成功/警告/错误
  · 内置"测试连接"，失败不放行、也不保存
  · 允许"以后再说"，不强迫用户
"""

from __future__ import annotations

import asyncio
import logging

import flet as ft

from app import theme as T
from core.config import ProviderConfig, load_config, save_config, upsert_provider
from core.llm import test_connection
from core.providers import validate_base_url
from core.secrets import backend_label_of

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  推荐的服务（只放主流四个，不要给用户一堆选择）
# ─────────────────────────────────────────────

CHOICES = [
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "desc": "国内直连，便宜，中文好",
        "url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "where": "platform.deepseek.com",
        "icon": ft.Icons.AUTO_AWESOME_ROUNDED,
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "desc": "官方服务，模型最全",
        "url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "where": "platform.openai.com",
        "icon": ft.Icons.PUBLIC_ROUNDED,
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "desc": "一个 key 用很多家模型",
        "url": "https://openrouter.ai/api/v1",
        "model": "",
        "where": "openrouter.ai",
        "icon": ft.Icons.CLOUD_ROUNDED,
    },
    {
        "id": "ollama",
        "name": "本地模型（Ollama）",
        "desc": "完全离线，不用花钱",
        "url": "http://localhost:11434/v1",
        "model": "llama3.1",
        "where": "不需要 key（随便填一个非空值）",
        "icon": ft.Icons.COMPUTER_ROUNDED,
    },
]

TOTAL_STEPS = 3


# ─────────────────────────────────────────────
#  小部件
# ─────────────────────────────────────────────

def _step_dots(current: int) -> ft.Row:
    """顶部进度点：当前步是长条，其他是小圆点。"""
    dots = [
        ft.Container(
            width=22 if i == current else 8,
            height=8,
            border_radius=4,
            bgcolor=T.ACCENT if i == current else T.OUTLINE,
            animate=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
        )
        for i in range(TOTAL_STEPS)
    ]
    return ft.Row(dots, spacing=T.GAP_SM, alignment=ft.MainAxisAlignment.CENTER)


def _headline(title: str, subtitle: str = "") -> ft.Column:
    controls = [ft.Text(title, size=21, weight=ft.FontWeight.W_600, color=T.TEXT)]
    if subtitle:
        controls.append(ft.Text(subtitle, size=12, color=T.TEXT_MUTED))
    return ft.Column(controls, spacing=T.GAP_SM,
                     horizontal_alignment=ft.CrossAxisAlignment.CENTER)


def _keys_where(secrets) -> str:
    """密钥实际存在哪 —— 界面上要如实说，不能替用户假设。"""
    return backend_label_of(secrets)


def _service_card(c: dict, selected: bool) -> ft.Container:
    """一个服务选项卡片。

    选中态和高亮靠**边框颜色 + 底色**表达，而不是靠打勾 ——
    卡片一多，颜色比小图标更容易一眼扫到。
    """
    return ft.Container(
        content=ft.Row([
            ft.Container(
                content=ft.Icon(c["icon"], size=19,
                                color=T.ON_ACCENT if selected else T.ACCENT),
                width=36, height=36, border_radius=18,
                bgcolor=T.ACCENT if selected else T.SURFACE_HIGHEST,
                alignment=ft.Alignment.CENTER,
            ),
            ft.Column([
                ft.Text(c["name"], size=13, weight=ft.FontWeight.W_500, color=T.TEXT),
                ft.Text(c["desc"], size=10, color=T.TEXT_MUTED),
            ], spacing=1, expand=True),
        ], spacing=T.GAP_MD, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=T.GAP_MD,
        bgcolor=T.SURFACE_HIGH if selected else T.SURFACE,
        border=ft.Border.all(2 if selected else 1, T.ACCENT if selected else T.OUTLINE),
        border_radius=T.RADIUS_CARD,
        ink=True,
        animate=ft.Animation(150, ft.AnimationCurve.EASE_OUT),
    )


def build_oobe(page: ft.Page, secrets, on_done) -> ft.Control:
    """造引导界面。

    secrets: SecretStore 实例（用来存 key）
    on_done: 引导完成（或用户跳过）后调用，关键字参数：
               test_connected: bool —— 有没有真的测通
               persisted: bool      —— key 有没有存进系统凭据库
                                     （False = 只在内存里，关掉程序就没了）
    """

    state: dict = {"step": 0, "choice": None, "testing": False, "done": False, "busy": False}

    # ── 固定区域 ──
    dots_row = ft.Row([], alignment=ft.MainAxisAlignment.CENTER)
    content = ft.Column(
        controls=[],
        spacing=T.GAP_MD,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )

    # 提示行：固定位置，所有步骤都看得到
    notice_text = ft.Text("", size=12, text_align=ft.TextAlign.CENTER, color=T.TEXT_MUTED)
    notice_box = ft.Container(
        content=ft.Row([notice_text], alignment=ft.MainAxisAlignment.CENTER),
        padding=ft.Padding.all(T.GAP_SM),
        visible=False,
    )

    spinner = ft.ProgressRing(width=16, height=16, visible=False,
                              stroke_width=2, color=T.ACCENT)

    next_btn = ft.FilledButton(content="继续", icon=ft.Icons.ARROW_FORWARD_ROUNDED)
    back_btn = ft.TextButton(content="上一步", icon=ft.Icons.ARROW_BACK_ROUNDED)
    skip_btn = ft.TextButton(content="以后再说")

    # ── 第 3 步的控件 ──
    key_field = ft.TextField(
        label="API Key",
        hint_text="粘贴你的 key",
        password=True,
        can_reveal_password=True,
        border=ft.OutlineInputBorder(border_radius=T.RADIUS_CHIP,
                                    side=ft.BorderSide(1, T.OUTLINE)),
        text_size=14,
        width=340,
    )

    def notify(text: str, kind: str = "info") -> None:
        """在固定位置显示一条提示。"""
        color = {"info": T.TEXT_MUTED, "ok": T.SUCCESS,
                 "warn": T.WARNING, "error": T.DANGER}[kind]
        notice_text.value = text
        notice_text.color = color
        notice_box.visible = bool(text)
        page.update()

    # ─────────────────────────────────────────
    #  每一步的渲染
    # ─────────────────────────────────────────

    def render() -> None:
        step = state["step"]
        dots_row.controls = _step_dots(step).controls
        content.controls.clear()

        back_btn.visible = step > 0
        skip_btn.visible = step < TOTAL_STEPS - 1
        spinner.visible = False
        next_btn.disabled = False
        notice_text.value = ""
        notice_box.visible = False

        if step == 0:
            render_welcome()
        elif step == 1:
            render_choose()
        else:
            render_key()

        page.update()

    def render_welcome() -> None:
        content.controls += [
            ft.Container(height=T.GAP_LG),
            ft.Container(
                content=ft.Text("八", size=30, color=T.ON_ACCENT, weight=ft.FontWeight.BOLD),
                width=72, height=72, border_radius=36, bgcolor=T.ACCENT,
                alignment=ft.Alignment.CENTER,
            ),
            _headline("你好，我是月见八千代", "开始之前，有两件事想先和你说清楚"),
            T.card(ft.Row([
                ft.Icon(ft.Icons.KEY_ROUNDED, size=18, color=T.ACCENT),
                ft.Column([
                    ft.Text("你需要自己准备一个 API Key", size=13,
                            weight=ft.FontWeight.W_500, color=T.TEXT),
                    ft.Text("八千代的“大脑”由大模型提供。应用本身不提供 Key，也不经手。",
                            size=11, color=T.TEXT_MUTED),
                ], spacing=2, expand=True),
            ], spacing=T.GAP_MD, vertical_alignment=ft.CrossAxisAlignment.START)),
            T.card(ft.Row([
                ft.Icon(ft.Icons.LOCK_ROUNDED, size=18, color=T.SUCCESS),
                ft.Column([
                    ft.Text("Key 只留在你自己的电脑上", size=13,
                            weight=ft.FontWeight.W_500, color=T.TEXT),
                    ft.Text(f"它被存进{_keys_where(secrets)}；对话内容只发往你填的那个地址，"
                            "不经过任何第三方服务器。",
                            size=11, color=T.TEXT_MUTED),
                ], spacing=2, expand=True),
            ], spacing=T.GAP_MD, vertical_alignment=ft.CrossAxisAlignment.START)),
            ft.Text("大约需要 2 分钟", size=11, color=T.TEXT_MUTED),
        ]
        next_btn.content = "继续"
        next_btn.icon = ft.Icons.ARROW_FORWARD_ROUNDED

    def render_choose() -> None:
        """两列卡片，点一张 = 选中它（不是直接跳下一步）。

        ⚠️ 这里必须用 ResponsiveRow + col，不能用 wrap=True 的 Row。
          因为 wrap=True 在 Flutter 里是 Wrap，而 Wrap 的子项不支持 expand ——
          卡片会被算成宽度 0，渲染出来一片空白（这个 bug 真的发生过）。
        """
        cards_row = ft.ResponsiveRow(controls=[], spacing=T.GAP_MD, run_spacing=T.GAP_MD)

        def pick(c: dict):
            def _handler(e) -> None:
                state["choice"] = c
                render()                 # 重画，让选中态显示出来
            return _handler

        for c in CHOICES:
            selected = state["choice"] is not None and state["choice"]["id"] == c["id"]
            card = _service_card(c, selected)
            card.on_click = pick(c)
            # 窄屏一行一个，够宽时一行两个
            card.col = {"xs": 12, "sm": 6}
            cards_row.controls.append(card)

        ready = state["choice"] is not None
        next_btn.content = "继续"
        next_btn.icon = ft.Icons.ARROW_FORWARD_ROUNDED
        # ★ 没选服务时按钮禁用 —— 用户不可能"点了没反应"
        next_btn.disabled = not ready

        content.controls += [
            _headline("你想用哪个服务？", "选一个你已经有 Key 的；不确定就选 DeepSeek"),
            ft.Container(height=T.GAP_SM),
            cards_row,
            ft.Text("都不是？先随便选一个，进去后可以在「设置」页改地址和模型名。",
                    size=11, color=T.TEXT_MUTED),
        ]

    def render_key() -> None:
        c = state["choice"] or CHOICES[0]
        where = _keys_where(secrets)
        next_btn.content = "测试并开始"
        next_btn.icon = ft.Icons.ROCKET_LAUNCH_ROUNDED

        content.controls += [
            _headline(f"粘贴 {c['name']} 的 API Key", "它只会存在这台电脑上"),
            ft.Container(height=T.GAP_SM),
            key_field,
            ft.Text(f"Key 申请地址：{c['where']}", size=11, color=T.TEXT_MUTED),
            ft.Text(f"Key 存进：{where}", size=11, color=T.TEXT_MUTED),
        ]

        # 存不住就提前说清，别等用户下次开机发现 key 没了才奇怪
        if not getattr(secrets, "persistent", True):
            content.controls.append(
                ft.Text(f"注意：{where}，Key 只能保留在本次运行中，关掉程序后要重新填。",
                        size=11, color=T.WARNING, text_align=ft.TextAlign.CENTER)
            )

        content.controls += [
            T.card(ft.Column([
                ft.Text(f"地址：{c['url']}", size=11, color=T.TEXT_MUTED),
                ft.Text(f"模型：{c['model'] or '（稍后到设置页填）'}",
                        size=11, color=T.TEXT_MUTED),
            ], spacing=2), padding=T.GAP_MD),
            spinner,
        ]

    # ─────────────────────────────────────────
    #  动作
    # ─────────────────────────────────────────

    async def go_next() -> None:
        step = state["step"]
        log.debug("OOBE 下一步：step=%s", step)
        if state["done"] or state["busy"]:
            # 正在测试中重复点，直接忽略（避免重复发请求）
            return

        if step < TOTAL_STEPS - 1:
            state["step"] += 1
            render()
            return

        # ── 最后一步：测试 + 保存 ──
        c = state["choice"] or CHOICES[0]
        api_key = (key_field.value or "").strip()
        if not api_key:
            notify("请先粘贴 API Key（用本地模型的话，随便填个非空值也行）。", "warn")
            return

        provider = ProviderConfig(
            id=c["id"], display_name=c["name"], protocol="openai",
            base_url=c["url"], model_id=c["model"],
        )

        try:
            validate_base_url(provider.base_url)
        except ValueError as exc:
            notify(str(exc), "error")
            return

        if not provider.model_id:
            # 这个服务需要用户自己填模型名
            notify("这个服务需要你自己填模型名，先带你进去，到「设置」页补上。", "warn")
            await finish(provider, api_key, tested=False)
            return

        state["testing"] = True
        state["busy"] = True
        # 按钮本身也进入忙碌态 —— 光"禁用"不够，用户看不出是在干活还是坏了
        next_btn.content = "测试中…"
        next_btn.icon = None
        next_btn.disabled = True
        spinner.visible = True
        notify("正在测试连接…", "info")

        # 让"正在测试"先画到屏幕上，再去调网络。
        # 不加这个 await，界面可能来不及刷新就被网络调用占住了。
        await asyncio.sleep(0)

        try:
            ok, message = await test_connection(provider, api_key)
        except Exception as exc:
            # ★ 必须有这个兜底。
            #   没有它的话，一个没预料到的异常会让按钮永远停在"测试中…"，
            #   而且用户界面上完全看不出发生了什么（异常只在日志里）。
            #   这个 bug 真的发生过。
            log.warning("OOBE 测试连接抛出异常: %s", type(exc).__name__)
            ok, message = False, f"测试连接时出错（{type(exc).__name__}），请稍后重试。"
        finally:
            # 不管是成功、失败还是抛异常，按钮一定要恢复可点
            state["testing"] = False
            state["busy"] = False
            spinner.visible = False
            next_btn.content = "测试并开始"
            next_btn.icon = ft.Icons.ROCKET_LAUNCH_ROUNDED
            next_btn.disabled = False

        if ok:
            notify(message, "ok")
            await finish(provider, api_key, tested=True)
        else:
            notify(message, "error")

    async def finish(provider: ProviderConfig, api_key: str, *, tested: bool) -> None:
        """把配置和密钥存下来，然后交给主界面。只执行一次。

        ★ 一条重要规矩：**连接已经测通了，就不能因为"存不住 key"把用户扣在这里。**
          存不进系统凭据库时自动降级成"仅本次运行有效"，让他先进去用，
          并且如实告诉他下次要重新填。
          这条规矩是踩坑之后加的：以前 save 一失败就直接 return，
          用户看到的是引导页停着不动 —— 而连接明明是通的。
        """
        if state["done"]:
            log.debug("OOBE 已经完成过，忽略重复的 finish")
            return
        state["done"] = True

        cfg = load_config()
        upsert_provider(cfg, provider)
        cfg.active_provider = provider.id
        save_config(cfg)

        persisted = True
        try:
            await secrets.save(provider.id, api_key, persist=True)
        except Exception as exc:
            log.warning("密钥持久化失败，降级为仅本次运行：%s: %s", type(exc).__name__, exc)
            persisted = False
            try:
                await secrets.save(provider.id, api_key, persist=False)
            except Exception as exc2:
                # 连内存都存不进去（key 是空的之类），那才是真的没法继续
                state["done"] = False          # 允许重试
                notify(f"密钥保存失败（{type(exc2).__name__}），请重试。", "error")
                return

        log.info("OOBE 完成：provider=%s，已测试=%s，已持久化=%s",
                 provider.id, tested, persisted)
        on_done(test_connected=tested, persisted=persisted)

    def go_back(e=None) -> None:
        if state["step"] > 0:
            state["step"] -= 1
            render()

    def skip(e=None) -> None:
        if state["done"]:
            return
        state["done"] = True
        log.info("用户跳过了引导")
        on_done(test_connected=False)

    next_btn.on_click = lambda e: page.run_task(go_next)
    back_btn.on_click = go_back
    skip_btn.on_click = skip

    # ── 组装 ──
    view = ft.Column(
        controls=[
            ft.Container(height=T.GAP_MD),
            dots_row,
            content,
            notice_box,
            ft.Row([back_btn, next_btn], alignment=ft.MainAxisAlignment.CENTER,
                   spacing=T.GAP_SM),
            ft.Row([skip_btn], alignment=ft.MainAxisAlignment.CENTER),
            ft.Container(height=T.GAP_SM),
        ],
        spacing=T.GAP_MD,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )

    render()
    return view
