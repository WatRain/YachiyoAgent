"""自绘的窗口标题栏。

为什么需要它：
  Windows 原生的那条标题栏（小图标 +「月见八千代」+ 最小化/最大化/关闭）
  跟这套深色界面放在一起很出戏，所以 app/main.py 里关掉了它：

      page.window.title_bar_hidden = True

  关掉之后有两件事**必须自己补上**，否则用户会觉得这个窗口"用不了"：
    · 拖动 —— 整条栏包在 ft.WindowDragArea 里，按住空白处就能挪窗口
    · 关闭 —— 右上角自己画一个按钮，走 page.window.close()；
      这样才会触发 page.on_close，把对话记录存下来
      （用 destroy() 的话是硬销毁，存不下来）

关于最小化：
  Flet 1.0 的 ft.Window 上**没有** minimize() 这个方法，但有 `minimized` 属性 ——
  文档原文 "Set to True to minimize programmatically"。
  （我一开始只查了方法名，漏了这个属性，白说了一句"Flet 做不了最小化"。）
  改完属性要 page.update() 才会推到客户端。
  也没有 maximize()/restore()，不过有 `maximized` 属性，要的话同理。
"""

from __future__ import annotations

import flet as ft

from app import theme as T

BAR_HEIGHT = 34        # 比原生标题栏略窄一点，视觉上更轻


def _window_button(icon, tooltip: str, handler) -> ft.IconButton:
    """标题栏上的一个小圆角按钮（最小化 / 关闭共用）。

    handler 是**无参**函数：这样调用方不会写成 on_click=lambda e: handler(e)，
    也就不会踩"签名对不上"那个老坑。
    """
    return ft.IconButton(
        icon=icon,
        icon_size=15,
        icon_color=T.TEXT_MUTED,
        tooltip=tooltip,
        width=30,
        height=30,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=T.radius_all(T.GAP_SM)),
        ),
        on_click=lambda e: handler(),
    )


def build_title_bar(*, on_minimize, on_close) -> ft.Control:
    """造顶部标题栏。

    两个回调都是**无参**函数，由 app/main.py 提供。
    ★ 故意做成关键字参数：这两个要是接反了，用户点"最小化"会直接把程序关掉，
      这种错不该有机会发生。
    """
    # 左边那个小短条是"抓手"：暗示这块区域可以拖。
    # 没有它的话，一条空白很难让人想到能拖窗口。
    grip = ft.Container(
        width=26,
        height=4,
        border_radius=T.radius_all(2),
        bgcolor=T.OUTLINE,
        tooltip="按住这里拖动窗口",
    )

    return ft.WindowDragArea(
        content=ft.Container(
            content=ft.Row(
                controls=[
                    grip,
                    ft.Container(expand=True),      # 中间全部留白 = 可拖动区
                    _window_button(ft.Icons.REMOVE_ROUNDED, "最小化", on_minimize),
                    _window_button(ft.Icons.CLOSE_ROUNDED, "关闭", on_close),
                ],
                spacing=T.GAP_XS,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            height=BAR_HEIGHT,
            padding=T.pad_only(left=T.GAP_XS, top=T.GAP_XS),
        ),
        # 双击标题栏最大化对一个小挂件窗口没意义，而且最大化之后
        # 440 宽的聊天界面会被拉得很怪，所以关掉。
        maximizable=False,
        # ★ 高度写死，而且【绝对不能加 expand】。
        #   父级是 ft.Column([标题栏, body])，body 是 expand=True 的。
        #   这一条如果也 expand，Column 会把可用高度按比例分给两个子项 ——
        #   标题栏会吃掉半屏，界面上就是"关闭按钮上面一大块空白"。
        #   （和之前 Row(wrap=True) + expand 那个坑是同一类：Python 侧看着都对。）
        height=BAR_HEIGHT,
    )
