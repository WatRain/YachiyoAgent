"""设置页（HyperOS 风格）：填 provider、填 API Key、测试连接。

界面的规矩：
  · 密钥走 core/secrets.py（系统凭据库），界面只负责收集和显示掩码
  · 配置走 core/config.py（JSON 文件）
  · 地址校验和参数翻译走 core/providers.py
  · 界面自己不做任何网络判断，只显示 core/llm.py 返回的那句人话
"""

from __future__ import annotations

import logging

import flet as ft

from app import theme as T
from core.config import (
    ProviderConfig,
    get_provider,
    load_config,
    remove_provider,
    save_config,
    upsert_provider,
)
from core.llm import test_connection
from core.providers import BUILTIN_PRESETS, make_provider_id, validate_provider
from core.secrets import backend_label_of

log = logging.getLogger(__name__)

PROTOCOLS = [
    ("openai", "OpenAI 兼容（最常用）"),
    ("anthropic", "Anthropic（Claude）"),
    ("gemini", "Google Gemini"),
    ("custom", "自定义模型名"),
]

# 复制自 README 的对照表，省得用户到处查
PRESET_HINTS = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "platform.deepseek.com"),
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "platform.openai.com"),
    "openrouter": ("https://openrouter.ai/api/v1", "（视模型而定）", "openrouter.ai"),
    "anthropic": ("（留空用官方默认）", "claude-3-5-haiku-latest", "console.anthropic.com"),
    "gemini": ("（留空用官方默认）", "gemini-2.0-flash", "aistudio.google.com"),
    "ollama": ("http://localhost:11434/v1", "llama3.1", "不需要 key"),
}

NOTICE_V = 10       # 提示条内垂直内边距


def _section(title: str, *controls: ft.Control) -> ft.Container:
    """一张卡片：小标题 + 若干行。"""
    return T.card(
        ft.Column(
            controls=[
                ft.Text(title, size=12, color=T.TEXT_MUTED, weight=ft.FontWeight.W_500),
                ft.Column(list(controls), spacing=T.GAP_MD),
            ],
            spacing=T.GAP_MD,
        )
    )


def _input_border(radius: int = T.RADIUS_CHIP, color: str = T.OUTLINE) -> ft.OutlineInputBorder:
    """输入控件的圆角边框。

    Flet 1.0 起，直接给 TextField / Dropdown 设 border_radius 已弃用，
    要把圆角包在 OutlineInputBorder 里。
    """
    return ft.OutlineInputBorder(border_radius=radius, side=ft.BorderSide(1, color))


def _field(label: str, hint: str = "", **kwargs) -> ft.TextField:
    """统一样式的输入框。"""
    return ft.TextField(
        label=label,
        hint_text=hint,
        border=_input_border(),
        text_size=14,
        **kwargs,
    )


def build_settings_view(page: ft.Page, secrets, on_saved) -> ft.Control:
    """造设置页。

    secrets: core.secrets.SecretStore 实例
    on_saved: 保存成功后调用，通知外面"配置变了，重建对话"
    """

    cfg = load_config()
    busy = {"v": False}      # 防止狂点按钮

    # ── 控件 ──
    provider_dd = ft.Dropdown(
        label="Provider", border=_input_border(), text_size=14,
        on_select=lambda e: page.run_task(on_pick),
    )
    name_tf = _field("显示名")
    protocol_dd = ft.Dropdown(
        label="协议", border=_input_border(), text_size=14, value="openai",
        options=[ft.dropdown.Option(key=k, text=v) for k, v in PROTOCOLS],
    )
    base_tf = _field("Base URL", "https://api.deepseek.com/v1")
    model_tf = _field("模型 ID", "deepseek-chat")
    key_tf = _field("API Key", "粘贴你的 key；不填 = 沿用已保存的",
                    password=True, can_reveal_password=True)
    persist_sw = ft.Switch(label="保存到系统凭据库", value=True, label_text_style=ft.TextStyle(size=13))

    hint = ft.Text("", size=11, color=T.TEXT_MUTED, selectable=True)
    result = ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.INFO_OUTLINE_ROUNDED, size=15, color=T.TEXT_MUTED),
            ft.Text("", size=12, color=T.TEXT_MUTED, selectable=True, expand=True),
        ], spacing=T.GAP_SM),
        padding=ft.Padding.symmetric(horizontal=T.GAP_MD, vertical=NOTICE_V),
        border_radius=T.RADIUS_CHIP,
        bgcolor=T.SURFACE_HIGH,
        visible=False,
    )
    result_text = result.content.controls[1]
    result_icon = result.content.controls[0]

    def refresh_options() -> None:
        provider_dd.options = [
            ft.dropdown.Option(key=p.id,
                               text=f"{p.display_name}{'（预设）' if p.is_builtin else ''}")
            for p in cfg.providers
        ]

    def fill_form(p: ProviderConfig) -> None:
        name_tf.value = p.display_name
        protocol_dd.value = p.protocol
        base_tf.value = p.base_url
        model_tf.value = p.model_id
        # 显示这个 provider 的填法提示
        if p.id in PRESET_HINTS:
            url, model, where = PRESET_HINTS[p.id]
            hint.value = f"Base URL：{url}　|　模型 ID：{model}　|　Key 申请：{where}"
        else:
            hint.value = ""

    def collect(pid: str) -> ProviderConfig:
        return ProviderConfig(
            id=pid,
            display_name=(name_tf.value or "").strip() or pid,
            protocol=protocol_dd.value or "openai",
            base_url=(base_tf.value or "").strip(),
            model_id=(model_tf.value or "").strip(),
        )

    def show(text: str, kind: str = "info") -> None:
        """在卡片里显示一条提示。kind: info / ok / warn / error"""
        colors = {
            "info": (T.TEXT_MUTED, ft.Icons.INFO_OUTLINE_ROUNDED),
            "ok": (T.SUCCESS, ft.Icons.CHECK_CIRCLE_ROUNDED),
            "warn": (T.WARNING, ft.Icons.ERROR_OUTLINE_ROUNDED),
            "error": (T.DANGER, ft.Icons.ERROR_OUTLINE_ROUNDED),
        }
        color, icon = colors.get(kind, colors["info"])
        result_text.value = text
        result_text.color = color
        result_icon.name = icon
        result_icon.color = color
        result.visible = bool(text)
        page.update()

    # ── 事件 ──
    # 注意：这几个函数【不接事件参数】。
    # 因为我们是用 lambda 转给 page.run_task 的，lambda 里没把事件传进来，
    # 函数签名里也不能有 e，否则会 TypeError: missing 1 required positional argument

    async def on_pick() -> None:
        pid = provider_dd.value
        p = get_provider(cfg, pid)
        if p is None:
            return
        fill_form(p)
        if await secrets.exists(pid):
            show("这个 provider 已经有保存的密钥。留空则继续用它。", "ok")
        else:
            show("还没有这个 provider 的密钥，请填写。", "warn")
        page.update()

    async def on_save() -> None:
        pid = provider_dd.value
        if not pid:
            show("请先选择一个 provider。", "warn")
            return

        p = collect(pid)
        problems = validate_provider(p)
        if problems:
            show("；".join(problems), "warn")
            return

        # ① 存配置（里面有 provider 列表和地址，但没有密钥）
        upsert_provider(cfg, p)
        cfg.active_provider = pid
        save_config(cfg)

        # ② 存密钥（如果用户填了）
        raw_key = (key_tf.value or "").strip()
        if raw_key:
            want_persist = bool(persist_sw.value)
            try:
                await secrets.save(pid, raw_key, persist=want_persist)
                key_tf.value = ""
                suffix = "" if want_persist else "（仅本次运行有效）"
                show(f"已保存配置和密钥。{suffix}", "ok")
            except Exception as exc:
                # 持久化失败不该让人没法用：退一步存内存，但必须说清后果
                log.warning("密钥持久化失败，降级为仅本次运行：%s: %s",
                            type(exc).__name__, exc)
                try:
                    await secrets.save(pid, raw_key, persist=False)
                except Exception as exc2:
                    show(f"密钥保存失败：{type(exc2).__name__}。请重试。", "error")
                else:
                    key_tf.value = ""
                    show(f"配置已保存。但{backend_label_of(secrets)}不可用"
                         f"（{type(exc).__name__}），Key 只保留在本次运行中。", "warn")
        else:
            show("已保存配置（密钥沿用之前保存的）。", "ok")

        refresh_options()
        page.update()
        on_saved()          # 告诉外面：配置变了

    async def on_test() -> None:
        pid = provider_dd.value
        if not pid:
            show("请先选择一个 provider。", "warn")
            return
        if busy["v"]:
            return

        p = collect(pid)
        problems = validate_provider(p)
        if problems:
            show("；".join(problems), "warn")
            return

        # 密钥可能刚填、可能已保存
        api_key = (key_tf.value or "").strip()
        if not api_key:
            api_key = await secrets.load(pid) or ""
        if not api_key:
            show("请先填写 API Key。", "warn")
            return

        busy["v"] = True
        show("正在测试…", "info")
        try:
            ok, message = await test_connection(p, api_key)
            show(message, "ok" if ok else "error")
        finally:
            busy["v"] = False

    async def on_delete() -> None:
        pid = provider_dd.value
        if not pid:
            show("请先选择一个 provider。", "warn")
            return
        remove_provider(cfg, pid)
        save_config(cfg)
        await secrets.delete(pid)        # 配置和密钥一起删，别留孤儿
        refresh_options()
        provider_dd.value = cfg.active_provider or ""
        show("已删除这个 provider（连同它的密钥）。", "ok")
        page.update()
        on_saved()

    def on_add(e) -> None:
        """新建一个自定义 provider。"""
        pid = make_provider_id("我的服务", [p.id for p in cfg.providers])
        new = ProviderConfig(id=pid, display_name="我的服务", protocol="openai",
                             base_url="", model_id="")
        upsert_provider(cfg, new)
        cfg.active_provider = pid
        save_config(cfg)
        refresh_options()
        provider_dd.value = pid
        fill_form(new)
        show("已新建。请填 Base URL、模型 ID 和 API Key。", "info")
        page.update()

    def on_reset(e) -> None:
        """把预设灌回来（用户把 provider 删光了时用）。"""
        for preset in BUILTIN_PRESETS:
            if not any(p.id == preset.id for p in cfg.providers):
                upsert_provider(cfg, preset.model_copy(deep=True))
        refresh_options()
        page.update()
        show("预设已恢复。", "ok")

    # ── 首次进来：没有 provider 就把预设灌进去 ──
    if not cfg.providers:
        for preset in BUILTIN_PRESETS:
            upsert_provider(cfg, preset.model_copy(deep=True))
        cfg.active_provider = cfg.providers[0].id
        save_config(cfg)

    refresh_options()
    provider_dd.value = cfg.active_provider or cfg.providers[0].id
    p0 = get_provider(cfg, provider_dd.value)
    if p0:
        fill_form(p0)

    # ── 组装 ──
    return ft.Column(
        controls=[
            # 顶部说明
            ft.Container(
                content=ft.Column([
                    ft.Text("模型设置", size=20, weight=ft.FontWeight.W_600, color=T.TEXT),
                    ft.Text("API Key 只存在你自己的电脑上（系统凭据管理器），"
                            "对话内容只发往你填的这个地址。",
                            size=11, color=T.TEXT_MUTED),
                ], spacing=T.GAP_XS),
                padding=ft.Padding.only(left=T.GAP_XS, top=T.GAP_XS),
            ),

            _section(
                "选择服务",
                provider_dd,
                hint,
                ft.Row([
                    ft.TextButton(content="新建", icon=ft.Icons.ADD_ROUNDED, on_click=on_add),
                    ft.TextButton(content="恢复预设", icon=ft.Icons.REFRESH_ROUNDED, on_click=on_reset),
                ], spacing=T.GAP_SM),
            ),

            _section("连接信息", name_tf, protocol_dd, base_tf, model_tf),

            _section(
                "密钥",
                key_tf,
                ft.Row([persist_sw], spacing=T.GAP_SM),
                ft.Text(f"保存位置：{backend_label_of(secrets)}", size=11,
                        color=T.TEXT_MUTED),
            ),

            # 操作按钮
            ft.Row([
                ft.FilledButton(content="测试连接", icon=ft.Icons.WIFI_TETHERING_ROUNDED,
                                on_click=lambda e: page.run_task(on_test), expand=True),
                ft.FilledButton(content="保存", icon=ft.Icons.SAVE_ROUNDED,
                                on_click=lambda e: page.run_task(on_save), expand=True),
            ], spacing=T.GAP_SM),
            ft.TextButton(content="删除这个 provider", icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                          icon_color=T.DANGER,
                          style=ft.ButtonStyle(color=T.DANGER),
                          on_click=lambda e: page.run_task(on_delete)),

            result,
            ft.Container(height=T.GAP_XL),        # 底部留白，方便滚动到底
        ],
        scroll=ft.ScrollMode.AUTO,
        spacing=T.GAP_MD,
    )
