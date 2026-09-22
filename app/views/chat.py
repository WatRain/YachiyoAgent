"""聊天界面（HyperOS 风格）。

分层的规矩没变：
  · 界面只管"画"和"收集输入"
  · 对话逻辑全在 core/chat.py
  · 界面永远不 import litellm，也永远不碰 api_key

流式的两个动作别搞混：
  · acc += piece            数据层在【累加】
  · bubble.value = acc      界面层在【覆盖】
"""

from __future__ import annotations

import asyncio
import time

import flet as ft

from app import theme as T

# 节流：攒够这么多毫秒才刷一次界面。
# 人眼看不出 40 毫秒的间隔（看起来仍是连续的逐字），
# 但刷新次数从"每块一次"降到最多每秒 25 次。
THROTTLE_MS = 40

GAP_V = 11          # 气泡/输入框内垂直内边距


def _bubble(text: str, *, is_user: bool) -> ft.Container:
    """造一个气泡。

    HyperOS 的味道来自三点：大圆角、中性色分层、克制的间距。
      用户：强调色底 + 白字，靠右
      八千代：亮一层的表面色，靠左
    """
    if is_user:
        bg = T.ACCENT
        # 靠内那一侧用很小的圆角（气泡"指向"说话的人）
        radius = T.radius_only(
            tl=T.RADIUS_BUBBLE, tr=T.RADIUS_BUBBLE,
            bl=T.RADIUS_BUBBLE, br=T.GAP_XS,
        )
    else:
        bg = T.SURFACE_HIGH
        radius = T.radius_only(
            tl=T.RADIUS_BUBBLE, tr=T.RADIUS_BUBBLE,
            bl=T.GAP_XS, br=T.RADIUS_BUBBLE,
        )

    return ft.Container(
        content=ft.Markdown(
            text or " ",                       # Markdown 不能是空串，否则不显示
            selectable=True,
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
        ),
        bgcolor=bg,
        padding=T.pad(h=T.GAP_LG, v=GAP_V),
        border_radius=radius,
        alignment=ft.Alignment.CENTER_RIGHT if is_user else ft.Alignment.CENTER_LEFT,
        # 入场动画：从"略低一点"滑到正常位置。
        # 只动 offset 不动 opacity —— 万一动画没触发，文字也看得见（安全）。
        offset=ft.Offset(0, 0.06),
        animate_offset=ft.Animation(220, ft.AnimationCurve.EASE_OUT_CUBIC),
    )


def _header(state: dict) -> ft.Container:
    """顶栏：名字 + 状态点。"""
    dot = ft.Container(
        width=8, height=8, border_radius=4,
        bgcolor=T.TEXT_MUTED,
    )
    name = ft.Text("月见八千代", size=15, weight=ft.FontWeight.W_600, color=T.TEXT)
    sub = ft.Text("月读管理员", size=11, color=T.TEXT_MUTED)

    state["dot"] = dot
    return ft.Container(
        content=ft.Row(
            controls=[
                # 左边一个圆头像占位（没有图片就用首字）
                ft.Container(
                    content=ft.Text("八", size=15, color=T.ON_ACCENT, weight=ft.FontWeight.BOLD),
                    width=34, height=34, border_radius=17,
                    bgcolor=T.ACCENT,
                    alignment=ft.Alignment.CENTER,
                ),
                ft.Column([name, sub], spacing=1, expand=True),
                dot,
            ],
            spacing=T.GAP_MD,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=T.SURFACE,
        border_radius=T.RADIUS_CARD,
        padding=ft.Padding.symmetric(horizontal=T.GAP_LG, vertical=GAP_V),
    )


def build_chat_view(page: ft.Page, get_chat, on_settings_needed) -> ft.Control:
    """造聊天界面。

    get_chat: 一个函数，返回当前的 Chat 对象（没配置好则返回 None）
    on_settings_needed: 没配好时点提示会调它
    """

    state: dict = {"task": None}

    # ── 消息区 ──
    messages_col = ft.Column(controls=[], scroll=ft.ScrollMode.AUTO, expand=True, spacing=T.GAP_MD)

    # ── 状态行（"正在思考…" / "已保存" 之类）──
    status = ft.Text("", size=11, color=T.TEXT_MUTED)

    # ── 输入框 ──
    input_field = ft.TextField(
        hint_text="和八千代说点什么…",
        expand=True,
        multiline=True,
        shift_enter=True,
        min_lines=1,
        max_lines=4,
        # 不设 border —— 外层的 input_shell 已经画了圆角边框
        text_size=14,
        content_padding=ft.Padding.symmetric(horizontal=T.GAP_LG, vertical=GAP_V),
        on_submit=lambda e: page.run_task(send),
    )

    send_btn = ft.IconButton(
        icon=ft.Icons.ARROW_UPWARD_ROUNDED,
        icon_color=T.ON_ACCENT,
        bgcolor=T.ACCENT,
        icon_size=20,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=T.RADIUS_INPUT)),
        tooltip="发送",
        on_click=lambda e: page.run_task(send),
    )
    stop_btn = ft.IconButton(
        icon=ft.Icons.STOP_ROUNDED,
        icon_color=T.TEXT,
        bgcolor=T.SURFACE_HIGHEST,
        icon_size=20,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=T.RADIUS_INPUT)),
        tooltip="停止",
        visible=False,
        on_click=lambda e: page.run_task(stop),
    )

    # 输入区外壳（圆角胶囊）
    input_shell = ft.Container(
        content=ft.Row([input_field, send_btn, stop_btn], spacing=T.GAP_SM,
                       vertical_alignment=ft.CrossAxisAlignment.END),
        bgcolor=T.SURFACE_HIGHEST,
        border_radius=T.RADIUS_INPUT,
        padding=ft.Padding.only(left=T.GAP_SM, right=T.GAP_SM, top=T.GAP_XS, bottom=T.GAP_XS),
        # 聚焦时描边点亮
        border=ft.Border.all(1, T.OUTLINE),
    )

    # ── 小工具 ──

    def add_bubble(text: str, *, is_user: bool) -> ft.Container:
        bubble = _bubble(text, is_user=is_user)
        messages_col.controls.append(bubble)
        # 触发入场动画（下一帧把 offset 归位）
        async def animate() -> None:
            await asyncio.sleep(0.01)
            bubble.offset = ft.Offset(0, 0)
            try:
                bubble.update()
            except Exception:
                pass
        page.run_task(animate)
        return bubble

    async def scroll_down() -> None:
        """滚到底部。等一帧是为了让新内容先完成布局。"""
        await asyncio.sleep(0.01)
        try:
            await page.scroll_to(offset=-1, duration=180, curve=ft.AnimationCurve.EASE_OUT)
        except Exception:
            pass    # 滚动失败无所谓，不该影响对话

    def set_busy(busy: bool) -> None:
        send_btn.visible = not busy
        stop_btn.visible = busy
        input_shell.border = ft.Border.all(1, T.ACCENT if busy else T.OUTLINE)
        page.update()

    def set_dot(busy: bool) -> None:
        dot = state.get("dot")
        if dot is not None:
            dot.bgcolor = T.ACCENT if busy else T.SUCCESS
            try:
                dot.update()
            except Exception:
                pass

    # ── 主要动作 ──

    async def send() -> None:
        text = (input_field.value or "").strip()
        if not text:
            return

        chat = get_chat()
        if chat is None:
            add_bubble("还没有配置模型。请到「设置」页填一个 provider 和 API Key。", is_user=False)
            page.update()
            await scroll_down()
            return

        input_field.value = ""
        add_bubble(text, is_user=True)
        set_busy(True)

        reply_bubble = add_bubble("", is_user=False)
        reply_md: ft.Markdown = reply_bubble.content
        status.value = "正在思考…"
        page.update()
        await scroll_down()

        async def run() -> None:
            chat.add_message(text)
            acc = ""
            last_flush = time.monotonic()
            set_dot(True)

            try:
                async for piece in chat.reply_stream():
                    acc += piece                       # 数据层：累加
                    now = time.monotonic()
                    if (now - last_flush) * 1000 >= THROTTLE_MS:
                        reply_md.value = acc           # 界面层：覆盖
                        page.update()
                        last_flush = now
                        await scroll_down()

                # 循环结束后一定要再刷一次，否则最后不到 40 毫秒的内容会丢
                reply_md.value = acc or "（没有内容）"
            except asyncio.CancelledError:
                reply_md.value = (acc or "") + "\n\n_（已停止）_"
                raise
            finally:
                status.value = ""
                set_busy(False)
                set_dot(False)
                page.update()
                await scroll_down()

        state["task"] = asyncio.create_task(run())
        try:
            await state["task"]
        except asyncio.CancelledError:
            pass                     # 取消是正常操作，不是错误
        finally:
            state["task"] = None

        # 回合结束，尝试抽取长期记忆（失败也不影响聊天）
        async def extract() -> None:
            try:
                count = await chat.remember_this_turn()
                if count:
                    status.value = f"记住了 {count} 件事"
                    page.update()
                    await asyncio.sleep(2)
                    status.value = ""
                    page.update()
            except Exception:
                pass

        page.run_task(extract)

    async def stop() -> None:
        task = state["task"]
        if task and not task.done():
            task.cancel()

    def add_notice(text: str) -> None:
        """给外面用：比如提示"设置已保存"。"""
        status.value = text
        page.update()

    def render_history(messages, *, animate: bool = False) -> int:
        """把已有的消息补画成气泡，返回画了几条。

        ★ 为什么需要它：
          app/main.py 的 startup() 以前只把历史塞进 chat.messages，
          却没画到界面上 —— 状态栏写着"已恢复上次的 8 条对话"，
          聊天区却是空的。用户看到的就是"对话历史记录没有显示出来"。
          **"数据里有了"和"屏幕上有了"是两件事** ——
          这个项目已经栽在这上面两次了（另一次是 Row(wrap=True)+expand）。

        animate=False（默认）不做入场动画：一次补画十几条会一起闪，
        而且这些消息本来就该"已经在那儿"了。
        """
        shown = 0
        for message in messages or []:
            role = message.get("role")
            content = message.get("content")
            # 只画对话本身：system（人格设定）和 tool（工具调用）不是给用户看的
            if role not in ("user", "assistant") or not content:
                continue
            bubble = _bubble(content, is_user=(role == "user"))
            if not animate:
                # _bubble 默认 offset 是(0, 0.06)，那是"入场动画的起点"。
                # 不归位的话，这些气泡会永远比正常位置低一点点。
                bubble.offset = ft.Offset(0, 0)
            messages_col.controls.append(bubble)
            shown += 1

        if shown:
            page.update()
            page.run_task(scroll_down)      # 直接停在最新一条
        return shown

    # ── 组装 ──
    view = ft.Column(
        controls=[
            _header(state),
            messages_col,
            ft.Row([status], alignment=ft.MainAxisAlignment.CENTER),
            input_shell,
        ],
        expand=True,
        spacing=T.GAP_MD,
    )
    view.add_notice = add_notice          # 挂上去，方便外部调用
    view.render_history = render_history  # 同上：启动时恢复历史要用
    return view
