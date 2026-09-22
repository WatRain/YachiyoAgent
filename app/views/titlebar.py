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
  Flet 1.0 的 ft.Window 上**没有** minimize() / maximize() / restore()
  （只有 close / destroy / center / to_front / start_dragging / start_resizing）。
  所以这条栏只提供"关闭"。想最小化，点任务栏上的窗口按钮一样可以。
  以后如果要自己做缩放边框，Window.start_resizing(edge) 是现成的入口。
"""

from __future__ import annotations

import flet as ft

from app import theme as T

BAR_HEIGHT = 34        # 比原生标题栏略窄一点，视觉上更轻


def build_title_bar(on_close) -> ft.Control:
    """造顶部标题栏。

    on_close: 点关闭按钮时调用的**无参**函数。
              app/main.py 传进来的是"先存对话、再关窗"那个流程。
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

    close_btn = ft.IconButton(
        icon=ft.Icons.CLOSE_ROUNDED,
        icon_size=15,
        icon_color=T.TEXT_MUTED,
        tooltip="关闭",
        width=30,
        height=30,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=T.radius_all(T.GAP_SM)),
        ),
        on_click=lambda e: on_close(),
    )

    return ft.WindowDragArea(
        content=ft.Container(
            content=ft.Row(
                controls=[
                    grip,
                    ft.Container(expand=True),      # 中间全部留白 = 可拖动区
                    close_btn,
                ],
                spacing=T.GAP_SM,
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
