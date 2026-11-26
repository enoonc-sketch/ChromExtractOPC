#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
theme.py —— 桌面窗口的现代主题（v3.3 新增；不依赖 tkinter，可单独测试）

不引入任何第三方主题库（ttkbootstrap / sv-ttk 等），只用 Python 自带的 ttk.Style：
以内置的 'clam' 主题为底，按下面的调色板重新配置各个 ttk 样式；经典 tk 控件
（Label / Frame / Canvas / Text / Listbox …）用 Tk 选项数据库 + 控件树着色。

本模块内容：
  - 调色板 PALETTES：浅色 light / 深色 dark 两套，键完全一致（单元测试检查）；
  - 字体（Windows 优先 "Microsoft YaHei UI" / "Segoe UI"，没有就依次回退）、间距常量、DPI 缩放；
  - 主题设置 desktop_theme："light" / "dark" / "auto"（auto = 跟随 Windows 的“应用模式”，
    读注册表 AppsUseLightTheme，读不到按浅色）；
  - WCAG 2.x 对比度计算与必须满足的颜色组合 CONTRAST_PAIRS（正文 ≥ 4.5）；
  - ttk 样式表 ttk_specs()：纯数据（样式名 → configure / map / layout），apply_ttk() 才真正调用 Style；
  - 经典控件着色 paint_tree()：按“角色”给控件上色，切换主题时按角色换成新颜色（即时生效）；
  - v3.2 之前代码里写死的颜色（#777、#1f4e79、#cf222e …）对应到角色 LEGACY_COLORS。

窗口代码（desktop_ui.py）只负责把真实的 tkinter 对象传进来；这里的函数都按“鸭子类型”调用
（cget / configure / winfo_children / option_add / Style.configure …），测试用假对象即可跑通。
"""
import os
import re
import sys

# ---------------------------------------------------------------------------
# 主题选项（配置键 desktop_theme）
# ---------------------------------------------------------------------------
THEME_CHOICES = ("light", "dark", "auto")
DEFAULT_THEME = "auto"
THEME_OPTIONS = (("light", "浅色"), ("dark", "深色"), ("auto", "跟随系统"))
MENU_TITLE = "主题"
SECTION_LABEL = "界面主题"
HINT = ("切换后立即生效并保存（desktop_theme）。“跟随系统”按 Windows 设置 → 个性化 → 颜色 → "
        "“选择默认应用模式”，读不到时按浅色。")
_ALIASES = {"light": "light", "浅色": "light", "亮色": "light", "day": "light",
            "dark": "dark", "深色": "dark", "暗色": "dark", "night": "dark",
            "auto": "auto", "system": "auto", "跟随系统": "auto", "follow": "auto", "": "auto"}


def normalize_theme(value):
    """配置校验（宽松）：认识的写法规范成 light / dark / auto，其它（乱写、数字、None）一律回退 auto，
    不报错——主题只影响外观，不能因为它让服务启动失败。"""
    if not isinstance(value, str):
        return DEFAULT_THEME
    return _ALIASES.get(value.strip().lower(), DEFAULT_THEME)


_PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def windows_prefers_light(reg=None):
    """读 Windows 注册表 HKCU\\...\\Personalize\\AppsUseLightTheme：1 = 浅色，0 = 深色。
    读不到（不是 Windows、键不存在、权限问题）返回 None。reg 默认是 winreg 模块，测试时传假对象。"""
    if reg is None:
        try:
            import winreg as reg          # 只在 Windows 上存在
        except ImportError:
            return None
    key = None
    try:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, _PERSONALIZE)
        value, _typ = reg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(int(value))
    except Exception:  # noqa  任何读取失败都当作“不知道”
        return None
    finally:
        if key is not None:
            try:
                reg.CloseKey(key)
            except Exception:  # noqa
                pass


def resolve_theme(setting, reg=None, platform=None):
    """设置值 → 实际使用的调色板名（"light" / "dark"）。auto：Windows 上读注册表，其它系统或读失败 → 浅色。"""
    mode = normalize_theme(setting)
    if mode != "auto":
        return mode
    plat = platform if platform is not None else sys.platform
    if not str(plat).startswith("win") and reg is None:
        return "light"
    light = windows_prefers_light(reg)
    if light is None:
        return "light"
    return "light" if light else "dark"


# ---------------------------------------------------------------------------
# 调色板：两套主题的键必须完全一致（test_theme 检查），颜色一律 #rrggbb 小写
# ---------------------------------------------------------------------------
PALETTES = {
    "light": {
        # 主色
        "primary": "#0b5cc6", "primary_hover": "#0a4fa8", "primary_pressed": "#083f87", "on_primary": "#ffffff",
        "accent": "#0b4a8c",                     # 区域标题、卡片大号数字
        # 背景与卡片
        "bg": "#f4f6f9", "surface": "#ffffff", "surface_alt": "#f3f6fa", "card": "#ffffff",
        "card_hover": "#eef5ff", "border": "#d5dbe3", "border_strong": "#a9b3bf",
        # 文字
        "text": "#1f2328", "text_muted": "#59636e", "text_disabled": "#7b848e",
        # 左侧导航（浅灰侧栏）
        "nav_bg": "#eceff4", "nav_text": "#1f2328", "nav_muted": "#57606a", "nav_hover": "#e0e5ec",
        "nav_sel_bg": "#d9e7fb", "nav_sel_text": "#0a3069", "nav_bar": "#0b5cc6", "nav_locked": "#757e88",
        # 顶部工具栏 / 底部状态栏
        "toolbar_bg": "#ffffff", "status_bg": "#eceff4", "status_text": "#4f5963",
        # 输入框、选中、表头
        "input_bg": "#ffffff", "input_border": "#c3ccd6", "select_bg": "#cfe2fc", "select_text": "#0a3069",
        "heading_bg": "#eef1f5", "heading_text": "#1f2328",
        # 状态色（文字色，可直接写在背景 / 卡片上）+ 柔和底色（告警行、提示条）
        "success": "#1a7f37", "warning": "#8a5a00", "danger": "#c4232c", "info": "#0b5cc6",
        "success_bg": "#e3f6e8", "warning_bg": "#fff5d6", "danger_bg": "#ffe9e8", "info_bg": "#e3f0ff",
        # 按钮
        "btn_bg": "#f3f5f8", "btn_hover": "#e7ebf0", "btn_pressed": "#dbe1e8", "btn_text": "#1f2328",
        "btn_border": "#c9d1da", "danger_btn": "#c4232c", "danger_hover": "#a51d25", "on_danger": "#ffffff",
        # 图表（谱图 / 历史叠加 / 稳定性）
        "chart_bg": "#ffffff", "chart_grid": "#e9edf2", "chart_axis": "#59636e", "chart_text": "#1f2328",
        "chart_frame": "#a9b3bf", "chart_peak": "#c4232c",
        # 日志级别
        "log_debug": "#5f6b77", "log_info": "#1f2328", "log_warning": "#8a5a00", "log_error": "#c4232c",
        # 滚动条 / 进度条
        "scroll_trough": "#eef1f5", "scroll_thumb": "#c2cad3", "scroll_thumb_hover": "#9ea8b3",
    },
    "dark": {
        "primary": "#2f6fd6", "primary_hover": "#2864c7", "primary_pressed": "#265cb4", "on_primary": "#ffffff",
        "accent": "#79b8ff",
        "bg": "#16191e", "surface": "#1e2228", "surface_alt": "#23282f", "card": "#21262d",
        "card_hover": "#28303a", "border": "#353c45", "border_strong": "#525b66",
        "text": "#e6edf3", "text_muted": "#a2acb7", "text_disabled": "#737d88",
        # 深色侧栏
        "nav_bg": "#101317", "nav_text": "#d3dae2", "nav_muted": "#8f99a4", "nav_hover": "#1a1f25",
        "nav_sel_bg": "#1d2b3d", "nav_sel_text": "#ffffff", "nav_bar": "#4c94f8", "nav_locked": "#6d7782",
        "toolbar_bg": "#1e2228", "status_bg": "#101317", "status_text": "#a2acb7",
        "input_bg": "#12161b", "input_border": "#3f4751", "select_bg": "#1f3d66", "select_text": "#f0f6fc",
        "heading_bg": "#262c34", "heading_text": "#e6edf3",
        "success": "#4ac26b", "warning": "#e0b030", "danger": "#ff7b72", "info": "#6cb6ff",
        "success_bg": "#16301f", "warning_bg": "#33280f", "danger_bg": "#3d1f22", "info_bg": "#13294a",
        "btn_bg": "#2a3038", "btn_hover": "#343b45", "btn_pressed": "#3e4651", "btn_text": "#e6edf3",
        "btn_border": "#434b56", "danger_btn": "#b8282d", "danger_hover": "#cf3238", "on_danger": "#ffffff",
        "chart_bg": "#12161b", "chart_grid": "#242a32", "chart_axis": "#9aa4af", "chart_text": "#d3dae2",
        "chart_frame": "#4b545f", "chart_peak": "#ff7b72",
        "log_debug": "#8f99a4", "log_info": "#e6edf3", "log_warning": "#e0b030", "log_error": "#ff7b72",
        "scroll_trough": "#171b20", "scroll_thumb": "#3b434d", "scroll_thumb_hover": "#525c68",
    },
}

# 曲线调色板（历史叠加对比等多条曲线）：浅色用较深的颜色，深色用较亮的颜色，保证在图表底色上看得清
SERIES = {
    "light": ("#0b5cc6", "#c4232c", "#1a7f37", "#7c3aed", "#b45309", "#0e7490", "#be185d", "#4d5562"),
    "dark": ("#6cb6ff", "#ff7b72", "#4ac26b", "#c4a5ff", "#f0a04b", "#3fc8d4", "#f778ba", "#c9d1d9"),
}

# 每个角色的中文说明（报告配色表用）
ROLE_NOTES = {
    "primary": "主色（主按钮、选中色条、进度条）", "accent": "区域标题、卡片大号数字",
    "bg": "页面背景", "surface": "区域 / 表格 / 卡片底色", "card": "总览卡片", "card_hover": "卡片悬停",
    "border": "边框、分隔线", "text": "正文文字", "text_muted": "次要文字（说明、提示）",
    "text_disabled": "禁用文字", "nav_bg": "左侧导航背景", "nav_text": "导航文字", "nav_muted": "导航分组标题",
    "nav_sel_bg": "导航选中底色", "nav_bar": "导航选中色条", "nav_locked": "无权限（锁）灰显",
    "toolbar_bg": "顶部工具栏", "status_bg": "底部状态栏", "input_bg": "输入框 / 日志底色",
    "select_bg": "表格选中行", "heading_bg": "表头", "success": "成功（运行中）", "warning": "警告",
    "danger": "严重 / 错误", "info": "信息", "danger_bg": "告警行底色（浅红）", "warning_bg": "屏蔽 / 提醒行底色",
    "surface_alt": "斑马纹", "chart_bg": "图表底色", "chart_grid": "图表网格线",
}

# 必须满足的对比度（WCAG 2.x）：(前景角色, 背景角色, 最低对比度)。
# 正文 / 告警 / 按钮文字 ≥ 4.5；无权限灰显、禁用文字属“非活动控件”，按 ≥ 3.0 要求（仍明显可读）。
CONTRAST_PAIRS = (
    ("text", "bg", 4.5), ("text", "surface", 4.5), ("text", "card", 4.5), ("text", "surface_alt", 4.5),
    ("text_muted", "bg", 4.5), ("text_muted", "surface", 4.5), ("text_muted", "card", 4.5),
    ("accent", "surface", 4.5), ("accent", "card", 4.5), ("accent", "bg", 4.5),
    ("nav_text", "nav_bg", 4.5), ("nav_text", "nav_hover", 4.5), ("nav_muted", "nav_bg", 4.5),
    ("nav_sel_text", "nav_sel_bg", 4.5), ("nav_locked", "nav_bg", 3.0), ("nav_locked", "nav_hover", 3.0),
    ("status_text", "status_bg", 4.5), ("text", "toolbar_bg", 4.5), ("text_muted", "toolbar_bg", 4.5),
    ("on_primary", "primary", 4.5), ("on_primary", "primary_hover", 4.5), ("on_primary", "primary_pressed", 4.5),
    ("on_danger", "danger_btn", 4.5), ("on_danger", "danger_hover", 4.5),
    ("btn_text", "btn_bg", 4.5), ("btn_text", "btn_hover", 4.5), ("btn_text", "btn_pressed", 4.5),
    ("heading_text", "heading_bg", 4.5), ("select_text", "select_bg", 4.5),
    ("text", "input_bg", 4.5), ("text_disabled", "bg", 3.0), ("text_disabled", "surface", 3.0),
    # 告警色：直接写在背景 / 区域 / 卡片上的文字
    ("success", "bg", 4.5), ("success", "surface", 4.5), ("success", "card", 4.5),
    ("warning", "bg", 4.5), ("warning", "surface", 4.5), ("warning", "card", 4.5),
    ("danger", "bg", 4.5), ("danger", "surface", 4.5), ("danger", "card", 4.5),
    ("info", "bg", 4.5), ("info", "surface", 4.5), ("info", "card", 4.5),
    ("danger", "status_bg", 4.5),
    # 告警行：浅红 / 浅黄底上的正文
    ("text", "danger_bg", 4.5), ("text", "warning_bg", 4.5), ("text", "success_bg", 4.5), ("text", "info_bg", 4.5),
    ("danger", "danger_bg", 4.5), ("warning", "warning_bg", 4.5),
    # 日志与图表
    ("log_debug", "input_bg", 4.5), ("log_info", "input_bg", 4.5), ("log_warning", "input_bg", 4.5),
    ("log_error", "input_bg", 4.5), ("chart_text", "chart_bg", 4.5), ("chart_axis", "chart_bg", 4.5),
    ("chart_peak", "chart_bg", 4.5),
)
SERIES_MIN_CONTRAST = 3.0          # 曲线是“图形元素”（WCAG 1.4.11 要求 ≥ 3:1）


_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_NAMED = {"white": "#ffffff", "black": "#000000", "red": "#ff0000", "green": "#008000", "blue": "#0000ff",
          "gray": "#808080", "grey": "#808080", "orange": "#ffa500"}


def norm_color(value):
    """'#777' / '#777777' / 'White' → '#777777' / '#ffffff'；不是颜色返回 None（例如 'SystemButtonFace'）。"""
    v = str(value if value is not None else "").strip().lower()
    v = _NAMED.get(v, v)
    if not _HEX_RE.match(v):
        return None
    if len(v) == 4:
        v = "#" + "".join(c * 2 for c in v[1:])
    return v


def relative_luminance(color):
    """WCAG 2.x 相对亮度（sRGB）。"""
    c = norm_color(color)
    if c is None:
        raise ValueError("不是 #rrggbb 颜色：%r" % (color,))
    out = []
    for i in (1, 3, 5):
        v = int(c[i:i + 2], 16) / 255.0
        out.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    r, g, b = out
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg, bg):
    """WCAG 对比度 (L1 + 0.05) / (L2 + 0.05)，范围 1～21。"""
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def contrast_report(name):
    """[(前景, 背景, 对比度, 要求, 是否满足)]，报告和测试共用。"""
    pal = PALETTES[name]
    rows = []
    for fg, bg, need in CONTRAST_PAIRS:
        r = contrast_ratio(pal[fg], pal[bg])
        rows.append((fg, bg, round(r, 2), need, r >= need))
    return rows


def palette(name):
    """调色板名 → 调色板副本（名不对回退浅色）。"""
    return dict(PALETTES.get(name if name in PALETTES else "light"))


def series_colors(name):
    return SERIES.get(name if name in SERIES else "light")


def series_color(name, i):
    s = series_colors(name)
    return s[int(i) % len(s)]


# ---------------------------------------------------------------------------
# v3.2 及以前代码里写死的颜色 → 角色（窗口着色时自动换成当前主题的颜色）
# ---------------------------------------------------------------------------
LEGACY_COLORS = {
    "#777777": "text_muted", "#555555": "text_muted", "#666666": "text_muted", "#888888": "text_muted",
    "#57606a": "text_muted", "#6e7781": "text_muted",
    "#333333": "text", "#24292f": "text", "#000000": "text",
    "#1f4e79": "accent",
    "#cf222e": "danger", "#d1242f": "danger", "#dc3545": "danger",
    "#9a6700": "warning", "#856404": "warning",
    "#1a7f37": "success",
    "#0969da": "info",
    "#8c959f": "text_disabled",
    "#c0c6cc": "border", "#cccccc": "border", "#d0d7de": "border", "#eeeeee": "border",
    "#ffffff": "surface",
    "#fafbfc": "chart_bg", "#eef1f4": "chart_grid",
    "#f3f5f7": "nav_bg", "#dbe9f7": "nav_sel_bg",
}


def legacy_role(value):
    c = norm_color(value)
    return LEGACY_COLORS.get(c) if c else None


def color(pal, value):
    """角色名或旧颜色 → 当前调色板里的颜色；都不是就原样返回（如 '' 或系统颜色名）。"""
    if isinstance(value, str) and value in pal:
        return pal[value]
    role = legacy_role(value)
    if role:
        return pal[role]
    return value


# ---------------------------------------------------------------------------
# 字体、间距、DPI
# ---------------------------------------------------------------------------
FONT_CANDIDATES = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC", "Noto Sans CJK SC",
                   "Source Han Sans SC", "WenQuanYi Micro Hei", "DejaVu Sans")
MONO_CANDIDATES = ("Cascadia Mono", "Consolas", "Sarasa Mono SC", "Courier New", "DejaVu Sans Mono")
BASE_PT = 10            # 正文字号（点）。Tk 的“点”随 tk scaling 缩放，高 DPI 下自动变大
FONT_SIZES = {"small": 9, "body": 10, "nav": 10, "head": 9, "section": 11, "crumb": 13, "card_value": 18,
              "card_icon": 15, "card_title": 9, "status": 9, "mono": 10, "dot": 12}
_fonts = {"family": "Microsoft YaHei UI", "mono": "Consolas"}

# Tk 自带的命名字体 → (字号, 粗细, 是否等宽)；改它们等于改所有没指定字体的控件
NAMED_FONTS = {
    "TkDefaultFont": (BASE_PT, "normal", False), "TkTextFont": (BASE_PT, "normal", False),
    "TkMenuFont": (BASE_PT, "normal", False), "TkHeadingFont": (BASE_PT, "bold", False),
    "TkCaptionFont": (BASE_PT + 1, "bold", False), "TkSmallCaptionFont": (BASE_PT - 1, "normal", False),
    "TkTooltipFont": (BASE_PT - 1, "normal", False), "TkIconFont": (BASE_PT, "normal", False),
    "TkFixedFont": (BASE_PT, "normal", True),
}

SPACE = {"xs": 2, "s": 4, "m": 8, "l": 12, "xl": 16, "xxl": 24}


def pick_font(available, candidates=FONT_CANDIDATES):
    """从系统已有字体里按优先级挑一个；都没有返回 None（调用方保留 Tk 默认字体）。"""
    have = {str(f).lower(): str(f) for f in (available or ())}
    for c in candidates:
        if c.lower() in have:
            return have[c.lower()]
    return None


def set_fonts(family=None, mono=None):
    if family:
        _fonts["family"] = family
    if mono:
        _fonts["mono"] = mono
    return dict(_fonts)


def font_family():
    return _fonts["family"]


def font(size="body", weight="normal", mono=False):
    """字体元组 (字族, 字号[, "bold"])。size 可以是 FONT_SIZES 的名字或点数。"""
    pt = FONT_SIZES.get(size, size) if isinstance(size, str) else size
    fam = _fonts["mono"] if mono else _fonts["family"]
    if weight and weight != "normal":
        return (fam, int(pt), weight)
    return (fam, int(pt))


def clamp_dpi(dpi):
    try:
        d = float(dpi)
    except (TypeError, ValueError):
        return 96.0
    if d != d or d < 72:
        return 96.0
    return min(d, 480.0)


def ui_scale(dpi):
    """界面缩放倍数 = DPI / 96（100% = 1.0，150% = 1.5），用来放大像素类尺寸（行高、内边距、窗口默认大小）。"""
    return round(clamp_dpi(dpi) / 96.0, 3)


def tk_scaling(dpi):
    """Tk 的 scaling = 每点多少像素 = DPI / 72（字号用“点”，所以字体随之放大且清晰）。"""
    return round(clamp_dpi(dpi) / 72.0, 4)


def px(value, scale):
    """把 100% 缩放下的像素数换算到当前缩放（至少 1）。"""
    return max(1, int(round(float(value) * float(scale or 1.0))))


def scale_size(size, scale, screen=None):
    """(宽, 高) 按缩放放大，并且不超过屏幕（留出任务栏）。"""
    w, h = px(size[0], scale), px(size[1], scale)
    if screen and screen[0] and screen[1]:
        w = min(w, max(320, int(screen[0]) - 40))
        h = min(h, max(240, int(screen[1]) - 80))
    return w, h


def enable_dpi_awareness(ctypes_mod=None, platform=None):
    """Windows：在创建 Tk 窗口之前声明“系统 DPI 感知”，避免 125% / 150% 缩放下被系统拉伸而模糊。
    返回实际生效的方式："shcore" / "user32" / ""（非 Windows 或失败）。只调用一次，失败不影响启动。
    用“系统 DPI 感知”而不是“每显示器感知”：Tk 8.6 不处理 WM_DPICHANGED，每显示器模式拖到别的屏会错位。"""
    plat = platform if platform is not None else sys.platform
    if not str(plat).startswith("win"):
        return ""
    try:
        if ctypes_mod is None:
            import ctypes as ctypes_mod
        windll = ctypes_mod.windll
    except Exception:  # noqa
        return ""
    try:
        windll.shcore.SetProcessDpiAwareness(1)        # PROCESS_SYSTEM_DPI_AWARE（Win 8.1+）
        return "shcore"
    except Exception:  # noqa
        pass
    try:
        windll.user32.SetProcessDPIAware()             # Vista / Win7
        return "user32"
    except Exception:  # noqa
        return ""


def system_dpi(ctypes_mod=None, platform=None):
    """Windows 系统 DPI（96 = 100%）；拿不到返回 None（调用方改用 Tk 的 winfo_fpixels('1i')）。"""
    plat = platform if platform is not None else sys.platform
    if not str(plat).startswith("win"):
        return None
    try:
        if ctypes_mod is None:
            import ctypes as ctypes_mod
        user32 = ctypes_mod.windll.user32
    except Exception:  # noqa
        return None
    try:
        d = int(user32.GetDpiForSystem())               # Win10 1607+
        if d > 0:
            return d
    except Exception:  # noqa
        pass
    try:
        hdc = user32.GetDC(0)
        try:
            d = int(ctypes_mod.windll.gdi32.GetDeviceCaps(hdc, 88))     # LOGPIXELSX
        finally:
            user32.ReleaseDC(0, hdc)
        return d if d > 0 else None
    except Exception:  # noqa
        return None


def metrics(scale=1.0, linespace=None):
    """像素类尺寸（已按 DPI 缩放）。linespace = 正文字体行高（像素，窗口里量出来），用来算表格行高。"""
    s = scale or 1.0
    row = px(28, s)
    if linespace:
        row = max(row, int(linespace * 1.75))
    return {"rowheight": row, "btn_pad": (px(10, s), px(4, s)), "tool_pad": (px(8, s), px(4, s)),
            "entry_pad": px(5, s), "tab_pad": (px(14, s), px(6, s)), "head_pad": (px(8, s), px(6, s)),
            "scroll": px(12, s), "progress": px(8, s), "nav_bar": px(3, s), "nav_padx": px(12, s),
            "nav_pady": px(6, s), "card_pad": (px(14, s), px(10, s)), "border": 1, "indicator": px(14, s)}


# ---------------------------------------------------------------------------
# 按钮三种样式 + 其它样式名
# ---------------------------------------------------------------------------
STYLE = {
    "primary": "Primary.TButton", "secondary": "TButton", "danger": "Danger.TButton",
    "toolbar": "Toolbar.TButton", "status": "Status.TButton", "status_alert": "StatusAlert.TButton",
    "toggle": "Toggle.TButton", "toggle_on": "ToggleOn.TButton",
    "subtabs": "Sub.TNotebook", "progress": "Accent.Horizontal.TProgressbar",
    "tree": "Treeview", "vscroll": "Vertical.TScrollbar", "hscroll": "Horizontal.TScrollbar",
    "entry": "TEntry", "combo": "TCombobox", "check": "TCheckbutton", "radio": "TRadiobutton",
}

# 按钮文字（中文原文）→ 种类。只把少数“主要动作”做成主按钮，危险操作做成红色，其余都是次按钮。
_PRIMARY_PREFIX = ("▶", "保存", "开始", "登录", "生成报表", "导出 CSV", "查询", "一键加固", "导出备份", "启动从站")
_DANGER_PREFIX = ("■", "停止", "删除", "还原", "清除", "清空列表", "写入（确认后执行）")


def button_kind(text):
    """按钮文字（中文原文；英文界面先反查成中文再传进来）→ "primary" / "danger" / "secondary"。"""
    t = str(text or "").strip()
    if not t:
        return "secondary"
    if t.startswith(_DANGER_PREFIX):
        return "danger"
    if t.startswith(_PRIMARY_PREFIX):
        return "primary"
    return "secondary"


def button_style(kind):
    return STYLE.get(kind, STYLE["secondary"])


# 复选框 / 单选框放在不同底色的容器里时用的样式（ttk 控件的底色来自样式，不会继承父控件）
CONTAINER_VARIANTS = {"surface": "", "bg": "Page.", "card": "Card.", "toolbar_bg": "Toolbar.",
                      "status_bg": "Status.", "nav_bg": "Nav."}


def variant_style(base, bg_role):
    """('TCheckbutton', 'bg') → 'Page.TCheckbutton'；不认识的底色用区域底色版本。"""
    return CONTAINER_VARIANTS.get(bg_role, "") + base


# 树形表格的行标签：斑马纹 + 状态行（告警行用浅红底而不是刺眼的红字）
TREE_TAGS = (("odd", "surface_alt", None), ("even", "surface", None),
             ("alert", "danger_bg", "text"), ("offline", None, "text_muted"), ("muted", "warning_bg", "text"),
             ("ok", "success_bg", "text"), ("info", "info_bg", "text"))
STATUS_TAGS = ("alert", "offline", "muted", "ok", "info")


def tree_tags(pal):
    """{标签: {"background": …, "foreground": …}}，按配置顺序（斑马纹在前）。"""
    out = {}
    for tag, bg, fg in TREE_TAGS:
        kw = {}
        if bg:
            kw["background"] = pal[bg]
        if fg:
            kw["foreground"] = pal[fg]
        out[tag] = kw
    return out


def zebra_tags(index, tags=()):
    """第 index 行（0 起）应带的标签：已有状态标签（告警 / 离线 / 屏蔽）的行不加斑马纹，避免两种底色打架。"""
    tags = tuple(tags or ())
    if any(t in STATUS_TAGS for t in tags):
        return tags
    return tags + ("odd" if index % 2 else "even",)


# 日志级别着色
LOG_LEVELS = ("debug", "info", "warning", "error")
_LOG_RE = re.compile(r"\[(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\]")


def log_level(line):
    m = _LOG_RE.search(str(line or ""))
    if not m:
        return "info"
    lv = m.group(1)
    if lv in ("ERROR", "CRITICAL", "FATAL"):
        return "error"
    if lv in ("WARNING", "WARN"):
        return "warning"
    return lv.lower()


def log_tags(pal):
    return {"log_" + lv: {"foreground": pal["log_" + lv]} for lv in LOG_LEVELS}


# 总览卡片左上角的图标字符（基本多文种平面字符，不用 emoji，Windows 自带字体都能显示）
CARD_ICONS = {"service": "◉", "endpoint": "⇄", "clients": "◎", "channels": "≋", "alarms": "⚠",
              "cycle": "◷", "modbus": "⇌"}


# ---------------------------------------------------------------------------
# ttk 样式表（纯数据）
# ---------------------------------------------------------------------------
def _btn(bg, fg, border, hover, pressed, disabled_bg, disabled_fg, pad):
    conf = {"background": bg, "foreground": fg, "bordercolor": border, "lightcolor": bg, "darkcolor": bg,
            "focuscolor": fg, "focusthickness": 0, "relief": "flat", "padding": pad, "anchor": "center"}
    mp = {"background": [("disabled", disabled_bg), ("pressed", pressed), ("active", hover)],
          "foreground": [("disabled", disabled_fg)],
          "lightcolor": [("disabled", disabled_bg), ("pressed", pressed), ("active", hover)],
          "darkcolor": [("disabled", disabled_bg), ("pressed", pressed), ("active", hover)],
          "bordercolor": [("disabled", disabled_bg), ("focus", border)]}
    return conf, mp


def ttk_specs(pal, m=None, fonts=None):
    """返回 {样式名: {"configure": {...}, "map": {...}, "layout": [...]?}}。
    m = metrics()；fonts = {"body": 字体, "bold": 字体, "small": 字体}（不给就用 font()）。"""
    m = m or metrics(1.0)
    f = fonts or {"body": font("body"), "bold": font("body", "bold"), "small": font("small"),
                  "tab": font("body")}
    p = pal
    specs = {}
    specs["."] = {"configure": {
        "background": p["bg"], "foreground": p["text"], "fieldbackground": p["input_bg"],
        "bordercolor": p["border"], "lightcolor": p["bg"], "darkcolor": p["bg"], "troughcolor": p["scroll_trough"],
        "selectbackground": p["select_bg"], "selectforeground": p["select_text"], "insertcolor": p["text"],
        "focuscolor": p["primary"], "font": f["body"]},
        "map": {"foreground": [("disabled", p["text_disabled"])]}}
    dis_bg = p["surface_alt"]
    for name, args in (
            ("TButton", (p["btn_bg"], p["btn_text"], p["btn_border"], p["btn_hover"], p["btn_pressed"])),
            ("Primary.TButton", (p["primary"], p["on_primary"], p["primary"], p["primary_hover"],
                                 p["primary_pressed"])),
            ("Danger.TButton", (p["danger_btn"], p["on_danger"], p["danger_btn"], p["danger_hover"],
                                p["danger_hover"])),
            ("Toggle.TButton", (p["btn_bg"], p["btn_text"], p["btn_border"], p["btn_hover"], p["btn_pressed"])),
            ("ToggleOn.TButton", (p["select_bg"], p["select_text"], p["primary"], p["select_bg"],
                                  p["select_bg"]))):
        pad = m["tool_pad"] if name.startswith("Toggle") else m["btn_pad"]
        conf, mp = _btn(*args, disabled_bg=dis_bg, disabled_fg=p["text_disabled"], pad=pad)
        specs[name] = {"configure": conf, "map": mp}
    # 扁平按钮：工具栏、状态栏（平时没有边框，悬停才显示底色）
    for name, bg, fg in (("Toolbar.TButton", p["toolbar_bg"], p["text"]),
                         ("Status.TButton", p["status_bg"], p["status_text"]),
                         ("StatusAlert.TButton", p["status_bg"], p["danger"])):
        conf, mp = _btn(bg, fg, bg, p["btn_hover"], p["btn_pressed"], disabled_bg=bg,
                        disabled_fg=p["text_disabled"], pad=m["tool_pad"])
        if name.startswith("Status"):
            conf["padding"] = (m["tool_pad"][0], max(1, m["tool_pad"][1] - 2))
        if name == "StatusAlert.TButton":
            conf["font"] = f["bold"]
        specs[name] = {"configure": conf, "map": mp}
    specs["TEntry"] = {"configure": {
        "fieldbackground": p["input_bg"], "foreground": p["text"], "bordercolor": p["input_border"],
        "lightcolor": p["input_bg"], "darkcolor": p["input_bg"], "insertcolor": p["text"],
        "padding": m["entry_pad"], "selectbackground": p["select_bg"], "selectforeground": p["select_text"]},
        "map": {"bordercolor": [("focus", p["primary"]), ("disabled", p["border"])],
                "lightcolor": [("focus", p["primary"])],
                "fieldbackground": [("disabled", p["surface_alt"]), ("readonly", p["surface_alt"])],
                "foreground": [("disabled", p["text_disabled"])]}}
    specs["TCombobox"] = {"configure": {
        "fieldbackground": p["input_bg"], "foreground": p["text"], "background": p["btn_bg"],
        "bordercolor": p["input_border"], "lightcolor": p["input_bg"], "darkcolor": p["input_bg"],
        "arrowcolor": p["text_muted"], "insertcolor": p["text"], "padding": m["entry_pad"],
        "selectbackground": p["select_bg"], "selectforeground": p["select_text"], "arrowsize": m["indicator"]},
        "map": {"bordercolor": [("focus", p["primary"]), ("disabled", p["border"])],
                "lightcolor": [("focus", p["primary"])],
                "fieldbackground": [("readonly", p["input_bg"]), ("disabled", p["surface_alt"])],
                "foreground": [("disabled", p["text_disabled"])],
                "selectbackground": [("readonly", p["input_bg"])],
                "selectforeground": [("readonly", p["text"])],
                "background": [("active", p["btn_hover"]), ("pressed", p["btn_pressed"])],
                "arrowcolor": [("disabled", p["text_disabled"])]}}
    for base in ("TCheckbutton", "TRadiobutton"):
        for role, prefix in CONTAINER_VARIANTS.items():
            bg = p[role]
            fg = p["nav_text"] if role == "nav_bg" else (p["status_text"] if role == "status_bg" else p["text"])
            specs[prefix + base] = {"configure": {
                "background": bg, "foreground": fg, "indicatorbackground": p["input_bg"],
                "indicatorforeground": p["on_primary"], "upperbordercolor": p["input_border"],
                "lowerbordercolor": p["input_border"], "focuscolor": bg, "focusthickness": 0,
                "indicatormargin": (0, 0, m["entry_pad"], 0), "indicatorsize": m["indicator"],
                "padding": (2, 2)},
                "map": {"background": [("active", bg)],
                        "indicatorbackground": [("disabled", p["surface_alt"]), ("selected", p["primary"]),
                                                ("pressed", p["select_bg"])],
                        "indicatorforeground": [("selected", p["on_primary"])],
                        "upperbordercolor": [("selected", p["primary"]), ("active", p["primary"])],
                        "lowerbordercolor": [("selected", p["primary"]), ("active", p["primary"])],
                        "foreground": [("disabled", p["text_disabled"])]}}
    specs["Treeview"] = {"configure": {
        "background": p["surface"], "fieldbackground": p["surface"], "foreground": p["text"],
        "rowheight": m["rowheight"], "bordercolor": p["border"], "lightcolor": p["surface"],
        "darkcolor": p["surface"], "font": f["body"]},
        "map": {"background": [("selected", p["select_bg"])], "foreground": [("selected", p["select_text"])]}}
    specs["Treeview.Heading"] = {"configure": {
        "background": p["heading_bg"], "foreground": p["heading_text"], "relief": "flat",
        "bordercolor": p["border"], "lightcolor": p["heading_bg"], "darkcolor": p["heading_bg"],
        "padding": m["head_pad"], "font": f["bold"]},
        "map": {"background": [("active", p["btn_hover"])], "relief": [("pressed", "flat")]}}
    for name in ("TNotebook", "Sub.TNotebook"):
        specs[name] = {"configure": {"background": p["bg"], "bordercolor": p["border"], "lightcolor": p["bg"],
                                     "darkcolor": p["bg"], "tabmargins": (2, 4, 2, 0), "padding": 0}}
        # 子标签：未选中是浅灰底、次要文字；选中的与内容区同底色、主色文字（扁平“文件夹标签”）
        specs[name + ".Tab"] = {"configure": {
            "background": p["heading_bg"], "foreground": p["text_muted"], "bordercolor": p["border"],
            "lightcolor": p["heading_bg"], "darkcolor": p["heading_bg"], "padding": m["tab_pad"], "font": f["tab"],
            "focuscolor": p["heading_bg"]},
            "map": {"background": [("selected", p["bg"]), ("active", p["btn_hover"])],
                    "foreground": [("selected", p["accent"]), ("active", p["text"])],
                    "bordercolor": [("selected", p["border"])],
                    "lightcolor": [("selected", p["primary"])],
                    "focuscolor": [("selected", p["bg"])],
                    "expand": [("selected", (1, 1, 1, 0))]}}
    for name in ("Horizontal.TProgressbar", STYLE["progress"]):
        specs[name] = {"configure": {"background": p["primary"], "troughcolor": p["scroll_trough"],
                                     "bordercolor": p["scroll_trough"], "lightcolor": p["primary"],
                                     "darkcolor": p["primary"], "thickness": m["progress"]}}
    for orient in ("Vertical", "Horizontal"):
        name = orient + ".TScrollbar"
        specs[name] = {"configure": {
            "background": p["scroll_thumb"], "troughcolor": p["scroll_trough"], "bordercolor": p["scroll_trough"],
            "lightcolor": p["scroll_thumb"], "darkcolor": p["scroll_thumb"], "arrowcolor": p["text_muted"],
            "gripcount": 0, "arrowsize": m["scroll"], "relief": "flat"},
            "map": {"background": [("active", p["scroll_thumb_hover"]), ("pressed", p["scroll_thumb_hover"])],
                    "lightcolor": [("active", p["scroll_thumb_hover"])],
                    "darkcolor": [("active", p["scroll_thumb_hover"])]},
            # 去掉两端箭头，只留轨道和滑块（现代扁平滚动条）
            "layout": [(orient + ".Scrollbar.trough", {"sticky": "ns" if orient == "Vertical" else "ew",
                                                        "children": [(orient + ".Scrollbar.thumb",
                                                                      {"expand": "1", "sticky": "nswe"})]})]}
    specs["TFrame"] = {"configure": {"background": p["bg"]}}
    specs["TLabel"] = {"configure": {"background": p["bg"], "foreground": p["text"]}}
    specs["TSeparator"] = {"configure": {"background": p["border"]}}
    return specs


def style_names():
    """所有定义了的样式名（desktop_ui 里 style=… 引用的名字必须在这里）。"""
    return set(ttk_specs(palette("light")))


def apply_ttk(style, pal, m=None, fonts=None, base="clam"):
    """把样式表应用到 ttk.Style（以 clam 为底）。返回配置了几个样式。切换主题时再调一次即可，已有控件即时生效。"""
    try:
        if base and style.theme_use() != base:
            style.theme_use(base)
    except Exception:  # noqa  个别环境没有 clam：保留当前主题，只改颜色
        pass
    n = 0
    for name, spec in ttk_specs(pal, m, fonts).items():
        if spec.get("layout") is not None:
            try:
                style.layout(name, spec["layout"])
            except Exception:  # noqa
                pass
        try:
            style.configure(name, **spec.get("configure", {}))
        except Exception:  # noqa
            continue
        if spec.get("map"):
            try:
                style.map(name, **spec["map"])
            except Exception:  # noqa
                pass
        n += 1
    return n


def option_db(pal, fonts=None):
    """Tk 选项数据库：之后新建的经典 tk 控件默认就用主题颜色（懒加载页面、弹窗、simpledialog 对话框）。"""
    p = pal
    f = fonts or {"body": font("body"), "mono": font("mono", mono=True)}
    out = [
        ("*Frame.background", p["bg"]), ("*Toplevel.background", p["bg"]),
        ("*Label.background", p["bg"]), ("*Label.foreground", p["text"]),
        ("*Message.background", p["bg"]), ("*Message.foreground", p["text"]),
        ("*LabelFrame.background", p["bg"]), ("*LabelFrame.foreground", p["accent"]),
        ("*Canvas.background", p["bg"]), ("*Canvas.highlightBackground", p["border"]),
        ("*Text.background", p["input_bg"]), ("*Text.foreground", p["text"]),
        ("*Text.insertBackground", p["text"]), ("*Text.selectBackground", p["select_bg"]),
        ("*Text.selectForeground", p["select_text"]), ("*Text.highlightBackground", p["border"]),
        ("*Text.highlightColor", p["primary"]), ("*Text.relief", "flat"),
        ("*Listbox.background", p["input_bg"]), ("*Listbox.foreground", p["text"]),
        ("*Listbox.selectBackground", p["select_bg"]), ("*Listbox.selectForeground", p["select_text"]),
        ("*Listbox.highlightBackground", p["border"]), ("*Listbox.highlightColor", p["primary"]),
        ("*Listbox.relief", "flat"),
        ("*TCombobox*Listbox.background", p["input_bg"]), ("*TCombobox*Listbox.foreground", p["text"]),
        ("*TCombobox*Listbox.selectBackground", p["select_bg"]),
        ("*TCombobox*Listbox.selectForeground", p["select_text"]),
        ("*Menu.background", p["surface"]), ("*Menu.foreground", p["text"]),
        ("*Menu.activeBackground", p["select_bg"]), ("*Menu.activeForeground", p["select_text"]),
        # 万一还有经典按钮 / 输入框（第三方对话框）也别是一块灰
        ("*Button.background", p["btn_bg"]), ("*Button.foreground", p["btn_text"]),
        ("*Button.activeBackground", p["btn_hover"]), ("*Button.activeForeground", p["btn_text"]),
        ("*Entry.background", p["input_bg"]), ("*Entry.foreground", p["text"]),
        ("*Entry.insertBackground", p["text"]),
        ("*Checkbutton.background", p["bg"]), ("*Checkbutton.foreground", p["text"]),
        ("*Checkbutton.selectColor", p["input_bg"]), ("*Checkbutton.activeBackground", p["bg"]),
        ("*Radiobutton.background", p["bg"]), ("*Radiobutton.foreground", p["text"]),
        ("*Radiobutton.selectColor", p["input_bg"]), ("*Radiobutton.activeBackground", p["bg"]),
    ]
    if f.get("body"):
        out.append(("*TCombobox*Listbox.font", f["body"]))
    return out


def apply_option_db(root, pal, fonts=None):
    n = 0
    for pattern, value in option_db(pal, fonts):
        try:
            root.option_add(pattern, value)
            n += 1
        except Exception:  # noqa
            pass
    return n


# ---------------------------------------------------------------------------
# 经典 tk 控件着色（按“角色”）
# ---------------------------------------------------------------------------
INHERIT = "@inherit"          # 背景跟父控件一样
AUTO_TEXT = "@text"           # 文字色按所在背景挑（导航上用导航文字色，其它用正文色）

# 控件类（winfo_class）→ [(选项, 默认角色)]
CLASS_OPTIONS = {
    "Tk": (("background", "bg"),),
    "Toplevel": (("background", "bg"),),
    "Frame": (("background", INHERIT), ("highlightbackground", "border"), ("highlightcolor", "border")),
    "Label": (("background", INHERIT), ("foreground", AUTO_TEXT), ("activebackground", INHERIT),
              ("disabledforeground", "text_disabled")),
    "Message": (("background", INHERIT), ("foreground", AUTO_TEXT)),
    "LabelFrame": (("background", INHERIT), ("foreground", "accent"), ("highlightbackground", "border"),
                   ("highlightcolor", "border")),
    "Canvas": (("background", INHERIT), ("highlightbackground", "border"), ("highlightcolor", "border")),
    "Text": (("background", "input_bg"), ("foreground", "text"), ("insertbackground", "text"),
             ("selectbackground", "select_bg"), ("selectforeground", "select_text"),
             ("inactiveselectbackground", "select_bg"), ("highlightbackground", "border"),
             ("highlightcolor", "primary")),
    "Listbox": (("background", "input_bg"), ("foreground", "text"), ("selectbackground", "select_bg"),
                ("selectforeground", "select_text"), ("highlightbackground", "border"),
                ("highlightcolor", "primary"), ("disabledforeground", "text_disabled")),
    "Button": (("background", "btn_bg"), ("foreground", "btn_text"), ("activebackground", "btn_hover"),
               ("activeforeground", "btn_text"), ("highlightbackground", INHERIT)),
    "Entry": (("background", "input_bg"), ("foreground", "text"), ("insertbackground", "text"),
              ("readonlybackground", "surface_alt"), ("highlightbackground", "border"),
              ("highlightcolor", "primary")),
    "Checkbutton": (("background", INHERIT), ("foreground", AUTO_TEXT), ("activebackground", INHERIT),
                    ("selectcolor", "input_bg")),
    "Radiobutton": (("background", INHERIT), ("foreground", AUTO_TEXT), ("activebackground", INHERIT),
                    ("selectcolor", "input_bg")),
}
BG_OPTION = "background"
# ttk 控件的颜色来自样式（apply_ttk），不按选项着色
TTK_CLASSES = frozenset(("TButton", "TEntry", "TCombobox", "TCheckbutton", "TRadiobutton", "Treeview", "TNotebook",
                         "TProgressbar", "TScrollbar", "TLabel", "TFrame", "TLabelframe", "TSeparator", "TScale",
                         "TSpinbox", "TMenubutton", "TPanedwindow", "TSizegrip"))
TEXT_ON = {"nav_bg": "nav_text", "nav_hover": "nav_text", "nav_sel_bg": "nav_sel_text",
           "status_bg": "status_text", "primary": "on_primary", "danger_btn": "on_danger"}
# 反查颜色时“背景类”选项优先认背景角色，“文字类”优先认文字角色（两种角色碰巧同色时不会认错）
_BG_ROLES = ("bg", "surface", "card", "surface_alt", "nav_bg", "toolbar_bg", "status_bg", "input_bg",
             "chart_bg", "nav_sel_bg", "card_hover", "danger_bg", "warning_bg", "success_bg", "info_bg",
             "heading_bg", "select_bg", "border", "chart_grid")
_TEXT_ROLES = ("text", "text_muted", "accent", "danger", "warning", "success", "info", "text_disabled",
               "nav_text", "nav_muted", "nav_locked", "status_text", "primary", "border", "chart_text")


def text_role_on(bg_role):
    return TEXT_ON.get(bg_role, "text")


def reverse_role(pal, value, option):
    c = norm_color(value)
    if c is None:
        return None
    order = _BG_ROLES if option in ("background", "activebackground", "readonlybackground", "selectcolor",
                                    "highlightbackground", "highlightcolor") else _TEXT_ROLES
    for r in order:
        if pal.get(r) == c:
            return r
    for r, v in pal.items():
        if v == c:
            return r
    return None


def classify(option, value, default_role, pal, parent_role="bg"):
    """控件第一次着色时，按它当前的颜色值判断属于哪个角色：
    - 跟默认值一样（选项数据库给的 / 系统默认色）→ 默认角色（INHERIT / AUTO_TEXT / 固定角色）；
    - v3.2 写死的旧颜色 → LEGACY_COLORS 里的角色；
    - 恰好是当前调色板里的颜色 → 对应角色；
    - 其它少见的自定义颜色：固定角色的选项（输入框、列表）用默认角色；背景 / 文字类 → None（保持原样）。"""
    c = norm_color(value)
    if default_role == INHERIT:
        if c is None or c == pal.get(parent_role) or c == pal.get("bg"):
            return INHERIT
    elif default_role == AUTO_TEXT:
        if c is None or c == pal.get(text_role_on(parent_role)) or c == pal.get("text"):
            return AUTO_TEXT
    else:
        if c is None or c == pal.get(default_role):
            return default_role
    role = LEGACY_COLORS.get(c) or reverse_role(pal, c, option)
    if role is not None:
        return role
    if default_role not in (INHERIT, AUTO_TEXT):
        return default_role          # 输入框 / 列表的系统默认色（如 #c3c3c3 选中色）一律换成主题色
    return None


def resolve_role(role, parent_role):
    if role == INHERIT:
        return parent_role
    if role == AUTO_TEXT:
        return text_role_on(parent_role)
    return role


ROLE_ATTR = "_theme_roles"


def set_roles(widget, **roles):
    """窗口代码显式指定控件颜色角色（例如 set_roles(lbl, background="nav_bg", foreground="nav_text")）。
    着色时优先用它，不再按颜色值猜。"""
    cur = getattr(widget, ROLE_ATTR, None)
    cur = dict(cur) if isinstance(cur, dict) else {}
    cur.update(roles)
    try:
        setattr(widget, ROLE_ATTR, cur)
    except Exception:  # noqa
        pass
    return cur


def widget_class(w):
    try:
        c = w.winfo_class()
    except Exception:  # noqa
        return None
    return c if isinstance(c, str) else None


def _paint_ttk(w, cls, pal, parent_role):
    """ttk 控件：复选 / 单选框按所在容器底色换样式变体；树形表格重设行标签颜色；下拉列表弹出框换色。"""
    if cls in ("TCheckbutton", "TRadiobutton"):
        try:
            cur = str(w.cget("style") or "")
        except Exception:  # noqa
            cur = ""
        base = cls
        managed = {variant_style(base, r) for r in CONTAINER_VARIANTS}
        if cur in ("", base) or cur in managed:
            want = variant_style(base, parent_role if parent_role in CONTAINER_VARIANTS else "surface")
            if want != (cur or base):
                try:
                    w.configure(style=want)
                except Exception:  # noqa
                    pass
    elif cls == "Treeview":
        for tag, kw in tree_tags(pal).items():
            try:
                w.tag_configure(tag, **kw)
            except Exception:  # noqa
                pass
    elif cls == "TCombobox":
        try:                       # 弹出列表是单独的 Listbox：已经弹出过（存在）的才换色，不为此新建弹出窗口
            lb = "%s.popdown.f.l" % w
            if int(w.tk.call("winfo", "exists", lb)):
                w.tk.call(lb, "configure", "-background", pal["input_bg"], "-foreground", pal["text"],
                          "-selectbackground", pal["select_bg"], "-selectforeground", pal["select_text"])
        except Exception:  # noqa
            pass


def paint_widget(w, pal, parent_role="bg"):
    """给一个控件上色，返回它自己的背景角色（给子控件继承）。"""
    cls = widget_class(w)
    if cls is None:
        return parent_role
    if cls in TTK_CLASSES:
        _paint_ttk(w, cls, pal, parent_role)
        return parent_role
    opts = CLASS_OPTIONS.get(cls)
    if not opts:
        return parent_role
    roles = getattr(w, ROLE_ATTR, None)
    if not isinstance(roles, dict):
        roles = {}
    learned = dict(roles)
    for opt, default in opts:
        if opt in learned:
            continue
        try:
            val = w.cget(opt)
        except Exception:  # noqa  这个控件没有这个选项
            learned[opt] = None
            continue
        learned[opt] = classify(opt, val, default, pal, parent_role)
    if learned != roles:
        try:
            setattr(w, ROLE_ATTR, learned)
        except Exception:  # noqa
            pass
    own_bg = parent_role
    if learned.get(BG_OPTION) is not None:
        own_bg = resolve_role(learned[BG_OPTION], parent_role)
    try:
        setattr(w, "_theme_bg", own_bg)          # 之后单独给子树着色时，从这里知道父控件的底色角色
    except Exception:  # noqa
        pass
    kw = {}
    for opt, role in learned.items():
        if role is None:
            continue
        base = own_bg if opt != BG_OPTION else parent_role
        r = resolve_role(role, base if role != AUTO_TEXT else own_bg)
        if r in pal:
            kw[opt] = pal[r]
    if kw:
        try:
            w.configure(**kw)
        except Exception:  # noqa
            for k, v in kw.items():          # 个别选项不支持：逐个设
                try:
                    w.configure(**{k: v})
                except Exception:  # noqa
                    pass
    return own_bg


def paint_tree(w, pal, parent_role="bg", skip=None):
    """从 w 开始递归着色（包括它自己），返回着色的控件数。skip(w) 为真时跳过该控件及其子控件。"""
    if skip is not None:
        try:
            if skip(w):
                return 0
        except Exception:  # noqa
            pass
    own = paint_widget(w, pal, parent_role)
    n = 1
    try:
        kids = w.winfo_children()
    except Exception:  # noqa
        kids = []
    for k in kids:
        n += paint_tree(k, pal, own, skip)
    return n


def texts():
    """本模块需要翻译的中文原文（i18n 测试用）。"""
    return {t for _c, t in THEME_OPTIONS} | {MENU_TITLE, SECTION_LABEL, HINT, "界面语言与主题"}


def describe(name):
    """一行说明：'浅色（light）：主色 #0b5cc6 …'（日志用）。"""
    p = palette(name)
    return "%s：主色 %s，背景 %s，文字 %s" % (name, p["primary"], p["bg"], p["text"])
