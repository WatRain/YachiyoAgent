"""设置页：填 provider、填 API Key、测试连接。

界面的规矩：
  · 密钥走 core/secrets.py（系统凭据库），界面只负责收集和显示掩码
  · 配置走 core/config.py（JSON 文件）
  · 地址校验和参数翻译走 core/providers.py
  · 界面自己不做任何网络判断，只显示 core/llm.py 返回的那句人话
"""

from __future__ import annotations

import flet as ft

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

PROTOCOLS = [
    ("openai", "openai（OpenAI 兼容，最常用）"),
    ("anthropic", "anthropic（Claude 官方）"),
    ("gemini", "gemini（Google 官方）"),
    ("custom", "custom（自己写完整模型名）"),
]


def build_settings_view(page: ft.Page, secrets, on_saved) -> ft.Control:
    """造设置页。

    secrets: core.secrets.SecretStore 实例
    on_saved: 保存成功后调用，通知外面"配置变了，重建对话"
    """

    cfg = load_config()

    # ── 控件 ──
    # 注意：Flet 1.0 里 Dropdown 的事件叫 on_select，不是 on_change
    provider_dd = ft.Dropdown(label="Provider", width=360,
                              on_select=lambda e: page.run_task(on_pick))
    name_tf = ft.TextField(label="显示名", width=360)
    protocol_dd = ft.Dropdown(
        label="协议", width=360, value="openai",
        options=[ft.dropdown.Option(key=k, text=v) for k, v in PROTOCOLS],
    )
    base_tf = ft.TextField(label="Base URL", width=360, hint_text="https://api.deepseek.com/v1")
    model_tf = ft.TextField(label="模型 ID", width=360, hint_text="deepseek-chat")
    key_tf = ft.TextField(
        label="API Key", width=360, password=True,
        can_reveal_password=True,            # 右边的"眼睛"按钮
        hint_text="粘贴你的 key；不填 = 用已保存的",
    )
    persist_sw = ft.Switch(label="保存到系统凭据库", value=True)
    result = ft.Text("", size=12, selectable=True)

    # 用一个锁防止用户狂点按钮
    busy = {"v": False}

    def refresh_options() -> None:
        """把已知的 provider 填进下拉框。"""
        provider_dd.options = [
            ft.dropdown.Option(key=p.id, text=f"{p.display_name}{'（预设）' if p.is_builtin else ''}")
            for p in cfg.providers
        ]

    def fill_form(p: ProviderConfig) -> None:
        name_tf.value = p.display_name
        protocol_dd.value = p.protocol
        base_tf.value = p.base_url
        model_tf.value = p.model_id

    def collect(pid: str) -> ProviderConfig:
        """把表单读成一个 ProviderConfig。"""
        return ProviderConfig(
            id=pid,
            display_name=(name_tf.value or "").strip() or pid,
            protocol=protocol_dd.value or "openai",
            base_url=(base_tf.value or "").strip(),
            model_id=(model_tf.value or "").strip(),
        )

    def show(text: str, color=ft.Colors.ON_SURFACE_VARIANT) -> None:
        result.value = text
        result.color = color
        page.update()

    # ── 事件 ──

    async def on_pick(e) -> None:
        """下拉框选中某个 provider：填表单 + 报告密钥状态。"""
        pid = provider_dd.value
        p = get_provider(cfg, pid)
        if p is None:
            return
        fill_form(p)
        if await secrets.exists(pid):
            show("这个 provider 已经有保存的密钥。留空则继续用它。")
        else:
            show("还没有这个 provider 的密钥，请填写。", ft.Colors.ORANGE)
        page.update()

    # 注意：这几个函数【不接事件参数】。
    # 因为我们是用 lambda 转给 page.run_task 的，lambda 里没把事件传进来，
    # 所以函数签名里也不能有 e，否则会 TypeError: missing 1 required positional argument

    async def on_save() -> None:
        pid = provider_dd.value
        if not pid:
            show("请先选择一个 provider。", ft.Colors.ORANGE)
            return

        p = collect(pid)
        problems = validate_provider(p)
        if problems:
            show("；".join(problems), ft.Colors.ORANGE)
            return

        # ① 存配置（里面有 provider 列表和地址，但没有密钥）
        upsert_provider(cfg, p)
        cfg.active_provider = pid
        save_config(cfg)

        # ② 存密钥（如果用户填了）
        raw_key = (key_tf.value or "").strip()
        if raw_key:
            try:
                await secrets.save(pid, raw_key, persist=persist_sw.value)
                key_tf.value = ""
                show("已保存配置和密钥。" + ("（本次运行有效）" if not persist_sw.value else ""),
                     ft.Colors.GREEN)
            except Exception as exc:
                show(f"配置已保存，但密钥保存失败：{type(exc).__name__}", ft.Colors.RED)
        else:
            show("已保存配置（密钥沿用之前保存的）。", ft.Colors.GREEN)

        refresh_options()
        page.update()
        on_saved()          # 告诉外面：配置变了

    async def on_test() -> None:
        pid = provider_dd.value
        if not pid:
            show("请先选择一个 provider。", ft.Colors.ORANGE)
            return
        if busy["v"]:
            return

        p = collect(pid)
        problems = validate_provider(p)
        if problems:
            show("；".join(problems), ft.Colors.ORANGE)
            return

        # 密钥可能刚填、可能已保存
        api_key = (key_tf.value or "").strip()
        if not api_key:
            api_key = await secrets.load(pid) or ""
        if not api_key:
            show("请先填写 API Key。", ft.Colors.ORANGE)
            return

        busy["v"] = True
        show("正在测试…", ft.Colors.PRIMARY)
        try:
            ok, message = await test_connection(p, api_key)
            show(message, ft.Colors.GREEN if ok else ft.Colors.RED)
        finally:
            busy["v"] = False

    async def on_delete() -> None:
        pid = provider_dd.value
        if not pid:
            show("请先选择一个 provider。", ft.Colors.ORANGE)
            return
        remove_provider(cfg, pid)
        save_config(cfg)
        await secrets.delete(pid)        # 配置和密钥一起删，别留孤儿
        refresh_options()
        provider_dd.value = cfg.active_provider or ""
        show("已删除这个 provider（连同它的密钥）。", ft.Colors.GREEN)
        page.update()
        on_saved()

    def on_add(e) -> None:
        """新建一个自定义 provider。"""
        pid = make_provider_id("我的服务", [p.id for p in cfg.providers])
        new = ProviderConfig(id=pid, display_name="我的服务", protocol="openai",
                             base_url="", model_id="")
        upsert_provider(cfg, new)
        save_config(cfg)
        refresh_options()
        provider_dd.value = pid
        cfg.active_provider = pid
        save_config(cfg)
        fill_form(new)
        show("已新建。请填 Base URL、模型 ID 和 API Key。", ft.Colors.PRIMARY)
        page.update()

    def on_reset(e) -> None:
        """把预设灌回来（用户把 provider 删光了时用）。"""
        for preset in BUILTIN_PRESETS:
            if not any(p.id == preset.id for p in cfg.providers):
                upsert_provider(cfg, preset.model_copy(deep=True))
        refresh_options()
        page.update()
        show("预设已恢复。", ft.Colors.GREEN)

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
            ft.Text("模型设置", size=18, weight=ft.FontWeight.BOLD),
            ft.Text(
                "API Key 只保存在你自己的电脑上（系统凭据管理器），"
                "对话内容只发往你填的这个地址。",
                size=11, color=ft.Colors.ON_SURFACE_VARIANT,
            ),
            ft.Divider(),
            provider_dd,
            ft.Row([ft.Button(content="新建", icon=ft.Icons.ADD, on_click=on_add),
                    ft.Button(content="恢复预设", on_click=on_reset)], spacing=8),
            name_tf,
            protocol_dd,
            base_tf,
            model_tf,
            key_tf,
            persist_sw,
            ft.Row([
                ft.Button(content="测试连接", on_click=lambda e: page.run_task(on_test)),
                ft.Button(content="保存", icon=ft.Icons.SAVE, on_click=lambda e: page.run_task(on_save)),
                ft.Button(content="删除", icon=ft.Icons.DELETE, on_click=lambda e: page.run_task(on_delete)),
            ], spacing=8),
            result,
        ],
        scroll=ft.ScrollMode.AUTO,
        spacing=12,
    )
