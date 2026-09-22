"""八千代 Agent —— 程序入口。

启动顺序很重要：
  1. 先设 LITELLM_LOCAL_MODEL_COST_MAP（必须在 import litellm 之前）
  2. 配日志（带密钥脱敏）
  3. 建 Flet 应用

首次启动 → 走引导（OOBE）；已配置过 → 直接进主界面。
界面只做"画"和"收输入"，对话逻辑全在 core/。
"""

from __future__ import annotations

import os

# ★ 必须在任何 core / litellm 被导入之前设置。
#   让 litellm 用自带的本地价格表，不联网去 GitHub 下载（我们会超时几秒）。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import logging
import threading

import flet as ft

from app import theme
from app.views.chat import build_chat_view
from app.views.oobe import build_oobe
from app.views.settings import build_settings_view
from app.views.titlebar import build_title_bar
from core import store
from core.chat import Chat
from core.config import config_path, get_provider, load_config
from core.logging_setup import setup_logging
from core.providers import to_litellm_kwargs, validate_base_url
from core.secrets import SecretStore, backend_label_of

log = logging.getLogger(__name__)

APP_TITLE = "月见八千代"
WINDOW_WIDTH = 440
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


def warm_up_litellm() -> None:
    """在后台把 litellm 加载好。

    为什么需要这个：
      import litellm 实测要 7 秒多，而且是**同步阻塞**的（会冻住事件循环）。
      如果不预热，用户点"测试连接"时会看到界面卡住 7 秒 —— 感觉像"点了没反应"。
      在启动时后台跑一遍，这 7 秒就被藏到用户还在看欢迎页的时候了。
    """

    def _load() -> None:
        try:
            import litellm  # noqa: F401

            log.info("litellm 预热完成")
        except Exception as exc:
            log.warning("litellm 预热失败: %s", type(exc).__name__)

    threading.Thread(target=_load, daemon=True).start()


def main(page: ft.Page) -> None:
    setup_logging()
    warm_up_litellm()               # ★ 越早越好：把 7 秒的导入藏到启动阶段

    # ── 外观：字体 + 主题（都集中在 app/theme.py 里）──
    page.title = APP_TITLE
    page.theme = theme.build_theme()
    page.theme_mode = ft.ThemeMode.DARK
    theme.load_fonts(page)          # 有打包字体就用打包的，否则用系统 MiSans
    page.padding = theme.PAGE_PADDING
    page.bgcolor = theme.BG
    page.window.width = WINDOW_WIDTH
    page.window.height = WINDOW_HEIGHT
    # ★ 去掉 Windows 原生那条标题栏 —— 它跟这套深色界面放一起太出戏。
    #   代价是"拖动窗口"和"关闭窗口"要自己提供：
    #   顶部那条自绘栏见 app/views/titlebar.py。
    page.window.title_bar_hidden = True
    page.window.title_bar_buttons_hidden = True

    state = AppState()
    # 启动就把"key 存到哪"写进日志：出问题时不用猜是凭据库还是别的
    log.info("密钥后端：%s", backend_label_of(state.secrets))

    # ── 退出：保存对话 ─────────────────────────────

    def save_conversation() -> None:
        history = state.history()
        if not history:
            return
        try:
            store.save_conversation(history)
            log.info("已保存对话记录 %d 条", len(history))
        except Exception as exc:
            log.warning("保存对话失败: %s", type(exc).__name__)

    async def close_window() -> None:
        """点自绘标题栏上那个关闭按钮时走这里。

        先自己存一遍对话，再走 close()（它会触发 on_close，那边也会存一次）。
        两道都留着：原生标题栏没了以后，这是我们唯一的关闭入口，
        不能出现"关了但聊天记录没存上"。
        """
        save_conversation()
        try:
            await page.window.close()
        except Exception as exc:
            # close() 走的是 invoke_method，万一客户端不响应也必须有出路
            log.warning("window.close() 失败（%s: %s），改用 destroy()",
                        type(exc).__name__, exc)
            await page.window.destroy()

    # 整个窗口只有这一个容器；里面的 body 在"引导"和"主界面"之间切换。
    # 自绘标题栏放在 body 外面 —— 这样切界面时它不会跟着被换掉。
    body = ft.Container(expand=True)
    root = ft.Container(
        expand=True,
        content=ft.Column(
            controls=[build_title_bar(lambda: page.run_task(close_window)), body],
            spacing=0,
            expand=True,
            # Column 默认 horizontal_alignment=START，子项只占"内容宽度"——
            # 标题栏就缩成一小撮、不管它，还不好看。STRETCH 让两者都撑满宽度。
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
    )
    page.add(root)

    # ── 主界面（配置好之后才有）──────────────────────

    def show_main(*, notice: str = "") -> None:
        log.info("show_main: 开始构建主界面")
        holder: dict = {}

        def get_chat() -> Chat | None:
            return state.chat

        async def apply_config() -> None:
            """设置页保存后调用：按新配置重建对话，已有的对话内容搬过去。"""
            saved_history = state.history()
            try:
                message = await state.rebuild_chat(force=True)
            except Exception as exc:
                # 后面是 page.run_task，异常不接住就会被静默丢掉
                log.error("应用配置失败：%s: %s", type(exc).__name__, exc, exc_info=True)
                view = holder.get("view")
                if view is not None:
                    view.add_notice(f"应用配置时出错（{type(exc).__name__}），详情见日志。")
                return

            if state.chat is not None and saved_history:
                state.chat.messages.extend(saved_history)

            view = holder.get("view")
            if view is not None:
                view.add_notice(message or "配置已更新，可以开始聊天了。")

        log.info("show_main: 构建设置页…")
        settings_view = build_settings_view(
            page, state.secrets, lambda: page.run_task(apply_config)
        )
        log.info("show_main: 构建设置页完成")
        chat_view = build_chat_view(page, get_chat, lambda: None)
        log.info("show_main: 构建聊天页完成")
        holder["view"] = chat_view

        if notice:
            chat_view.add_notice(notice)

        log.info("show_main: 组装 Tabs…")
        body.content = ft.Tabs(
            length=2,
            selected_index=0,
            expand=True,
            content=ft.Column(
                expand=True,
                controls=[
                    ft.TabBar(tabs=[
                        ft.Tab(label="对话", icon=ft.Icons.CHAT_BUBBLE_ROUNDED),
                        ft.Tab(label="设置", icon=ft.Icons.SETTINGS_ROUNDED),
                    ]),
                    ft.TabBarView(expand=True, controls=[
                        ft.Container(content=chat_view, expand=True),
                        ft.Container(content=settings_view, expand=True),
                    ]),
                ],
            ),
        )
        page.update()
        log.info("show_main: 已切换并 update() 完成")

        # 恢复上次的对话记录
        async def startup() -> None:
            if state.chat is None:
                return
            saved = store.load_conversation()
            if not saved:
                return
            state.chat.messages.extend(saved)
            # 如果上面已经给了提示（比如"连接成功"），不要盖掉它
            if not notice:
                chat_view.add_notice(f"已恢复上次的 {len(saved)} 条对话。")

        page.run_task(startup)

    # ── 引导完成 → 切到主界面 ──────────────────────

    def show_error(exc: BaseException, *, what: str = "进入主界面") -> None:
        """兜底界面：出错了也要让用户看见原因，而不是停在一个不动的页面上。

        为什么要这个：
          切换失败以前是直接 raise。异常跑到 Flet 的任务里就没人管了 ——
          界面上什么都看不到，用户看到的只是"点了没反应"。这个 bug 真的发生过。
        """
        log.error("%s失败：%s: %s", what, type(exc).__name__, exc, exc_info=True)
        detail = f"{type(exc).__name__}: {exc}"
        body.content = ft.Column(
            controls=[
                ft.Container(height=theme.GAP_XL),
                ft.Icon(ft.Icons.ERROR_OUTLINE_ROUNDED, size=40, color=theme.DANGER),
                ft.Text(f"{what}出错了", size=18, weight=ft.FontWeight.W_600,
                        color=theme.TEXT),
                ft.Text(detail, size=12, color=theme.TEXT_MUTED, selectable=True,
                        text_align=ft.TextAlign.CENTER),
                ft.Text("这不是你的操作问题。详细信息在数据目录的 logs/app.log 里。",
                        size=11, color=theme.TEXT_MUTED,
                        text_align=ft.TextAlign.CENTER),
                ft.Container(height=theme.GAP_SM),
                ft.FilledButton(content="重试", icon=ft.Icons.REFRESH_ROUNDED,
                                on_click=lambda e: page.run_task(boot)),
            ],
            spacing=theme.GAP_MD,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )
        page.update()

    def on_oobe_done(*, test_connected: bool, persisted: bool = True) -> None:
        if not persisted:
            # 存不住也要放人进去，但必须如实说清后果
            notice = ("配置已保存。但这台机器上系统凭据库不可用，"
                      "Key 只保留在本次运行中，下次启动要重新填。")
        elif test_connected:
            notice = "连接成功，可以开始了。"
        else:
            notice = "配置已保存，开始聊吧。"
        log.info("引导完成（已测试连接=%s，已持久化=%s）", test_connected, persisted)

        async def go() -> None:
            log.info("引导完成后的 go() 开始执行")
            try:
                message = await state.rebuild_chat(force=True)
                log.info("引导完成后的 rebuild_chat 返回：%r", message)
                show_main(notice=message or notice)
            except Exception as exc:
                # 这里必须接住 —— 否则界面停在上一步，而用户看不到任何原因
                show_error(exc, what="切换到主界面")

        log.info("准备调度 go() 任务")
        page.run_task(go)

    # ── 启动分流 ──────────────────────────────────

    async def boot() -> None:
        # 判断是不是第一次：配置文件还不存在 = 全新的机器/全新的安装
        first_run = not config_path().exists()

        if first_run:
            log.info("首次启动，进入引导流程")
            try:
                body.content = build_oobe(page, state.secrets, on_oobe_done)
            except Exception as exc:
                # 连引导页都建不出来，也必须让用户看到一句话，而不是一片空白
                show_error(exc, what="打开引导页")
                return
            page.update()
            return

        log.info("已有配置，直接进入主界面")
        try:
            message = await state.rebuild_chat()
            show_main(notice=message)
        except Exception as exc:
            show_error(exc, what="启动")

    # ── 退出：保存对话 ─────────────────────────────
    # （save_conversation 定义在上面，因为自绘标题栏的关闭按钮要用到它；）

    page.on_disconnect = lambda e: save_conversation()
    page.on_close = lambda e: save_conversation()

    page.run_task(boot)


if __name__ == "__main__":
    ft.run(main)
