"""八千代 Agent —— 程序入口。

启动顺序很重要：
  1. 先设 LITELLM_LOCAL_MODEL_COST_MAP（必须在 import litellm 之前）
  2. 配日志（带密钥脱敏）
  3. 建 Flet 应用

界面只做"画"和"收输入"，对话逻辑全在 core/。
"""

from __future__ import annotations

import os

# ★ 必须在任何 core / litellm 被导入之前设置。
#   让 litellm 用自带的本地价格表，不联网去 GitHub 下载（我们会超时几秒）。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import logging

import flet as ft

from app.views.chat import build_chat_view
from app.views.settings import build_settings_view
from core import store
from core.chat import Chat
from core.config import get_provider, load_config
from core.logging_setup import setup_logging
from core.providers import to_litellm_kwargs, validate_base_url
from core.secrets import SecretStore

log = logging.getLogger(__name__)

APP_TITLE = "月见八千代"
WINDOW_WIDTH = 460
WINDOW_HEIGHT = 820


class AppState:
    """跨界面共享的状态：当前对话 + 密钥存储。"""

    def __init__(self) -> None:
        self.secrets = SecretStore()
        self.chat: Chat | None = None
        self.applied_provider: str = ""     # 当前 chat 用的是哪个 provider
        self.applied_key: str = ""          # 只用于"配置有没有变"的比较，不外传

    async def rebuild_chat(self, *, force: bool = False) -> str:
        """按当前配置准备对话。返回一句给用户看的状态说明（空串 = 一切正常）。

        ★ 关键：provider 和密钥都没变时【不重建】——
          否则每次发消息都会丢上下文。
        """
        cfg = load_config()
        provider = get_provider(cfg)
        if provider is None:
            self.chat = None
            return "还没有配置 provider。请到「设置」页选一个并填 API Key。"

        try:
            validate_base_url(provider.base_url)
        except ValueError as exc:
            self.chat = None
            return f"provider 地址不合法：{exc}"

        key = await self.secrets.load(provider.id)
        if not key:
            self.chat = None
            return f"「{provider.display_name}」还没有填 API Key。"

        try:
            kwargs = to_litellm_kwargs(provider, key)
        except ValueError as exc:
            self.chat = None
            return f"provider 配置有问题：{exc}"

        # 已经就绪而且没变化 → 保持现状（上下文得以保留）
        if (not force) and self.chat is not None and self.applied_provider == provider.id:
            if self.applied_key == key:
                return ""

        self.chat = Chat(kwargs)
        self.applied_provider = provider.id
        self.applied_key = key
        log.info("对话已就绪：provider=%s model=%s", provider.id, provider.model_id)
        return ""

    def history(self) -> list[dict]:
        """取当前对话里可保留的部分（user / assistant），用来在重建时搬过去。"""
        if self.chat is None:
            return []
        return [
            m for m in self.chat.messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]


def main(page: ft.Page) -> None:
    setup_logging()

    page.title = APP_TITLE
    page.window.width = WINDOW_WIDTH
    page.window.height = WINDOW_HEIGHT
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 12

    state = AppState()
    holder: dict = {}          # 放 chat_view，方便在闭包里引用

    def get_chat() -> Chat | None:
        return state.chat

    async def apply_config() -> None:
        """设置页保存后调用：按新配置重建对话，已有的对话内容搬过去。"""
        saved_history = state.history()          # 先留住旧内容
        notice = await state.rebuild_chat(force=True)
        if state.chat is not None and saved_history:
            state.chat.messages.extend(saved_history)

        view = holder.get("view")
        if view is not None:
            view.add_notice(notice or "配置已更新，可以开始聊天了。")

    # ── 建两个界面 ──
    settings_view = build_settings_view(
        page, state.secrets, lambda: page.run_task(apply_config)
    )
    chat_view = build_chat_view(page, get_chat, lambda: None)
    holder["view"] = chat_view

    page.add(
        # Flet 1.0 的 Tabs 需要用 TabBar（标题栏）+ TabBarView（内容区）组合。
        # 注意：TabBarView 里的顺序要和 TabBar 里的 tabs 一一对应。
        ft.Tabs(
            length=2,
            selected_index=0,
            expand=True,
            content=ft.Column(
                expand=True,
                controls=[
                    ft.TabBar(
                        tabs=[
                            ft.Tab(label="对话", icon=ft.Icons.CHAT),
                            ft.Tab(label="设置", icon=ft.Icons.SETTINGS),
                        ]
                    ),
                    ft.TabBarView(
                        expand=True,
                        controls=[
                            ft.Container(content=chat_view, expand=True),
                            ft.Container(content=settings_view, expand=True),
                        ],
                    ),
                ],
            ),
        )
    )

    # ── 启动：加载配置 + 恢复上次的对话 ──
    async def startup() -> None:
        notice = await state.rebuild_chat()
        if state.chat is not None:
            saved = store.load_conversation()
            if saved:
                state.chat.messages.extend(saved)
                chat_view.add_notice(f"已恢复上次的 {len(saved)} 条对话。")
                return
        if notice:
            chat_view.add_notice(notice)
        else:
            chat_view.add_notice("准备好了，说点什么吧。")

    page.run_task(startup)

    # ── 退出：保存对话 ──
    def save_conversation() -> None:
        history = state.history()
        try:
            store.save_conversation(history)
            log.info("已保存对话记录 %d 条", len(history))
        except Exception as exc:
            log.warning("保存对话失败: %s", type(exc).__name__)

    page.on_disconnect = lambda e: save_conversation()
    page.on_close = lambda e: save_conversation()


if __name__ == "__main__":
    ft.run(main)
