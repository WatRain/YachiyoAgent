"""HyperOS 风格的设计令牌（design tokens）。

为什么单独一个文件：
  配色、圆角、间距如果散落在各个界面文件里，改一处就会漏一处。
  集中在这里，改一次全局生效。

注意：Flet 的颜色可以写十六进制字符串，所以这里全用字符串常量，
不依赖 ft.Colors 的具体成员名（那个在不同版本里会变）。
"""

from __future__ import annotations

import flet as ft

# ─────────────────────────────────────────────
#  配色：深色中性底 + 鲜亮蓝强调色
#  HyperOS 的特征是"大面积中性色 + 一点高饱和强调色"，不要花哨
# ─────────────────────────────────────────────

ACCENT = "#3482FF"          # HyperOS 标志性的蓝
ACCENT_DIM = "#2A6AD6"      # 按下/次要状态用的深一点
ON_ACCENT = "#FFFFFF"

BG = "#121215"              # 窗口底色
SURFACE = "#1A1A1E"         # 卡片
SURFACE_HIGH = "#222227"    # 用户气泡 / 更亮一层
SURFACE_HIGHEST = "#2C2C33"  # 输入框等

TEXT = "#ECECF0"            # 主文字
TEXT_MUTED = "#8E8E98"      # 次要文字
OUTLINE = "#33333B"         # 描边

DANGER = "#FF5A5F"
SUCCESS = "#3DD68C"
WARNING = "#FFB340"


# ─────────────────────────────────────────────
#  圆角：HyperOS 用大圆角。这是"像不像"的关键之一
# ─────────────────────────────────────────────

RADIUS_CARD = 20            # 卡片、面板
RADIUS_BUBBLE = 20          # 聊天气泡
RADIUS_INPUT = 22           # 输入框、按钮
RADIUS_CHIP = 14            # 小标签


# ─────────────────────────────────────────────
#  间距：8 的倍数，节奏统一
# ─────────────────────────────────────────────

GAP_XS = 4
GAP_SM = 8
GAP_MD = 12
GAP_LG = 16
GAP_XL = 24

PAGE_PADDING = 14           # 窗口内边距


# ─────────────────────────────────────────────
#  字体
# ─────────────────────────────────────────────

# MiSans 是小米自家字体，HyperOS 默认就用它。
# 分两种情况：
#   ① 用户机器上装了 MiSans → 直接用系统字体名
#   ② 没装 → 从 assets/fonts/ 加载随程序打包的字体文件
# 界面上调用 load_fonts() 处理，两种都能工作。
SYSTEM_FONT_NAME = "MiSans"
BUNDLED_FONT_FILE = "MiSans-Regular.otf"     # 放在 assets/fonts/ 下（可选）
FALLBACK_FONT = "Microsoft YaHei UI"          # 系统都有的中文无衬线


def load_fonts(page: ft.Page) -> None:
    """把字体注册给 Flet。

    有打包字体就用打包的（这样换台没装 MiSans 的电脑也一致）；
    没有就退回系统字体名。
    """
    from core.paths import resource_path

    bundled = resource_path(f"assets/fonts/{BUNDLED_FONT_FILE}")
    if bundled.exists():
        page.fonts = {SYSTEM_FONT_NAME: str(bundled)}
    else:
        # 不注册也行 —— 系统里装了就叫得出名字
        page.fonts = {}


# ─────────────────────────────────────────────
#  主题
# ─────────────────────────────────────────────

def build_theme() -> ft.Theme:
    """造一套 HyperOS 味道的 Material 3 主题。"""
    scheme = ft.ColorScheme(
        primary=ACCENT,
        on_primary=ON_ACCENT,
        primary_container=ACCENT_DIM,
        on_primary_container=ON_ACCENT,
        surface=BG,
        on_surface=TEXT,
        on_surface_variant=TEXT_MUTED,
        surface_container=SURFACE,
        surface_container_high=SURFACE_HIGH,
        surface_container_highest=SURFACE_HIGHEST,
        surface_container_low=SURFACE,
        surface_container_lowest=BG,
        outline=OUTLINE,
        outline_variant=OUTLINE,
        error=DANGER,
        on_error=ON_ACCENT,
    )

    return ft.Theme(
        color_scheme=scheme,
        font_family=SYSTEM_FONT_NAME,
        use_material3=True,
        scaffold_bgcolor=BG,
        card_bgcolor=SURFACE,
        divider_color=OUTLINE,
        splash_color="#1F3482FF",       # 点按时的水波纹（带透明度的蓝）
        highlight_color="#143482FF",
        hover_color="#0F3482FF",
        # 按钮统一大圆角
        filled_button_theme=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=RADIUS_INPUT)),
        button_theme=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=RADIUS_INPUT)),
        outlined_button_theme=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=RADIUS_INPUT)),
        text_button_theme=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=RADIUS_INPUT)),
        # 卡片：不要阴影，靠底色分层（HyperOS 的风格）
        card_theme=ft.CardTheme(color=SURFACE, elevation=0, shape=ft.RoundedRectangleBorder(radius=RADIUS_CARD)),
        # 设置项的列表行
        list_tile_theme=ft.ListTileTheme(
            text_color=TEXT,
            icon_color=TEXT_MUTED,
            shape=ft.RoundedRectangleBorder(radius=RADIUS_CHIP),
            content_padding=ft.Padding.symmetric(horizontal=GAP_LG, vertical=GAP_SM),
        ),
    )


# ─────────────────────────────────────────────
#  常用构造的快捷方式
#
#  注意：Flet 1.0 里 ft.padding / ft.border_radius / ft.border 是【模块】，
#  便捷方法是挂在【类】上的：ft.Padding.symmetric(...)、ft.BorderRadius.only(...)。
#  写 ft.Padding.symmetric 会报 AttributeError。
# ─────────────────────────────────────────────

def pad(h: int = 0, v: int = 0) -> ft.Padding:
    """水平 h、垂直 v 的内边距。"""
    return ft.Padding.symmetric(horizontal=h, vertical=v)


def pad_only(*, left: int = 0, top: int = 0, right: int = 0, bottom: int = 0) -> ft.Padding:
    return ft.Padding.only(left=left, top=top, right=right, bottom=bottom)


def radius_all(r: int) -> ft.BorderRadius:
    return ft.BorderRadius.all(r)


def radius_only(*, tl: int = 0, tr: int = 0, bl: int = 0, br: int = 0) -> ft.BorderRadius:
    """四个角分别设圆角（气泡靠内那侧用小圆角）。"""
    return ft.BorderRadius.only(top_left=tl, top_right=tr, bottom_left=bl, bottom_right=br)


def outline(width: int = 1, color: str = OUTLINE) -> ft.Border:
    return ft.Border.all(width, color)


def card(content: ft.Control, *, padding: int = GAP_LG, bgcolor: str = SURFACE) -> ft.Container:
    """统一风格的卡片/面板。"""
    return ft.Container(
        content=content,
        bgcolor=bgcolor,
        border_radius=RADIUS_CARD,
        padding=padding,
    )
