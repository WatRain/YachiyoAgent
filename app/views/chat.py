"""聊天界面。

分层的规矩：
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


def _bubble(text: str, *, is_user: bool) -> ft.Container:
    """造一个气泡。is_user=True 是用户说的（靠右、带底色）。"""
    return ft.Container(
        content=ft.Markdown(
            text or " ",
            selectable=True,                      # 允许选中复制
            extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
        ),
        bgcolor=ft.Colors.SURFACE_CONTAINER_HIGHEST if is_user else None,
        padding=12,
        border_radius=12,
        # 用户消息靠右，八千代的靠左
        alignment=ft.Alignment.CENTER_RIGHT if is_user else ft.Alignment.CENTER_LEFT,
    )


def build_chat_view(page: ft.Page, get_chat, on_settings_needed) -> ft.Control:
    """造聊天界面。

    get_chat: 一个函数，返回当前的 Chat 对象（没配置好则返回 None）
    on_settings_needed: 没配置时点"去设置"会调它
    """

    # ── 界面上的东西 ──
    messages_col = ft.Column(
        controls=[],
        scroll=ft.ScrollMode.AUTO,     # 内容多了自动出现滚动条
        expand=True,                   # 占满剩余空间
        spacing=10,
    )
    status = ft.Text("", size=12, color=ft.Colors.ON_SURFACE_VARIANT)
    input_field = ft.TextField(
        hint_text="和八千代说点什么…",
        expand=True,                   # 横向占满
        multiline=True,                # 允许多行
        shift_enter=True,              # Shift+回车换行，回车发送
        min_lines=1,
        max_lines=4,
        # 圆角/边框那两个参数在 1.0 里改过写法，先不加 —— 不影响任何功能
        on_submit=lambda e: page.run_task(send),
    )
    send_btn = ft.Button(content="发送", icon=ft.Icons.SEND, on_click=lambda e: page.run_task(send))
    stop_btn = ft.Button(
        content="停止", icon=ft.Icons.STOP,
        on_click=lambda e: page.run_task(stop),
        visible=False,                 # 平时藏着，流式时才出现
    )

    # 正在跑的任务（用来实现"停止"）。放在字典里是为了能在嵌套函数里改它。
    state: dict = {"task": None}

    # ── 小工具 ──

    def add_bubble(text: str, *, is_user: bool) -> ft.Container:
        bubble = _bubble(text, is_user=is_user)
        messages_col.controls.append(bubble)
        return bubble

    async def scroll_down() -> None:
        """滚到底部。等一帧是为了让新内容先完成布局。"""
        await asyncio.sleep(0.01)
        try:
            await page.scroll_to(offset=-1, duration=200)
        except Exception:
            pass    # 滚动失败无所谓，不该影响对话

    def set_busy(busy: bool) -> None:
        """切换"正在生成"状态：禁用输入，露出停止按钮。"""
        send_btn.disabled = busy
        input_field.disabled = busy
        stop_btn.visible = busy
        page.update()

    # ── 主要动作 ──

    async def send() -> None:
        text = (input_field.value or "").strip()
        if not text:
            return

        chat = get_chat()
        if chat is None:
            add_bubble(
                "还没有配置模型。请到「设置」页填一个 provider 和 API Key。", is_user=False
            )
            page.update()
            await scroll_down()
            return

        # ① 清空输入、把用户的话画上去
        input_field.value = ""
        add_bubble(text, is_user=True)
        set_busy(True)

        # ② 八千代的回复气泡，边收边改它的内容
        reply_bubble = add_bubble("", is_user=False)
        reply_md: ft.Markdown = reply_bubble.content
        page.update()
        await scroll_down()

        # ③ 真正开始跑。任务放在 state 里是为了能取消。
        async def run() -> None:
            chat.add_message(text)
            acc = ""
            last_flush = time.monotonic()
            status.value = "正在思考…"
            page.update()

            try:
                async for piece in chat.reply_stream():
                    acc += piece                      # 数据层：累加
                    now = time.monotonic()
                    # 节流：攒够 40 毫秒才刷一次界面
                    # 人眼看不出 40 毫秒的间隔，但刷新次数从"每块一次"降到最多每秒 25 次
                    if (now - last_flush) * 1000 >= 40:
                        reply_md.value = acc          # 界面层：覆盖
                        page.update()
                        last_flush = now
                        await scroll_down()

                # 循环结束后一定要再刷一次，否则最后不到 40 毫秒的内容会丢
                reply_md.value = acc or "（没有内容）"
            except asyncio.CancelledError:
                # 用户点了"停止"。已收到的内容保留，末尾加一句说明。
                reply_md.value = (acc or "") + "\n\n_（已停止）_"
                raise
            finally:
                status.value = ""
                set_busy(False)
                await scroll_down()

        state["task"] = asyncio.create_task(run())
        try:
            await state["task"]
        except asyncio.CancelledError:
            pass                     # 取消是正常操作，不是错误
        finally:
            state["task"] = None

        # ④ 回合结束，尝试抽取长期记忆（失败也不影响聊天）
        async def extract() -> None:
            try:
                count = await chat.remember_this_turn()
                if count:
                    status.value = f"（记住了 {count} 件事）"
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

    # ── 组装 ──
    view = ft.Column(
        controls=[
            messages_col,
            status,
            ft.Row(
                controls=[input_field, send_btn, stop_btn],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.END,
            ),
        ],
        expand=True,
        spacing=8,
    )
    view.add_notice = add_notice      # 挂上去，方便外部调用
    return view
