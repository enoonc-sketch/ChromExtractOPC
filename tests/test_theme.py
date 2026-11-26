# -*- coding: utf-8 -*-
"""v3.3 现代主题（theme.py）：不需要 tkinter，全部用假对象测试。

覆盖：两套调色板键一致、WCAG 对比度（正文 ≥ 4.5）、跟随系统（假注册表）、配置宽松校验回退、
ttk 样式表与 desktop_ui.py 里 style= 引用的样式名都已定义、按钮三种样式、经典控件按角色着色
（切换主题后颜色变化）、斑马纹 / 告警行、日志级别、DPI 缩放、字体回退、i18n。
"""
import ast
import json
import os
import re
import sys
import tempfile
import shutil
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import i18n                      # noqa: E402
import theme                     # noqa: E402
import ui_layout as ul           # noqa: E402

HEX = re.compile(r"^#[0-9a-f]{6}$")
CJK = re.compile(r"[\u4e00-\u9fff]")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class Palettes(unittest.TestCase):
    def test_two_palettes_same_keys(self):
        self.assertEqual(set(theme.PALETTES), {"light", "dark"})
        self.assertEqual(set(theme.PALETTES["light"]), set(theme.PALETTES["dark"]))
        for name, pal in theme.PALETTES.items():
            for k, v in pal.items():
                self.assertRegex(v, HEX, "%s.%s" % (name, k))
        self.assertEqual(len(theme.SERIES["light"]), len(theme.SERIES["dark"]))
        for name, s in theme.SERIES.items():
            for c in s:
                self.assertRegex(c, HEX, name)

    def test_required_roles_present(self):
        need = {"primary", "bg", "surface", "card", "border", "text", "text_muted", "success", "warning",
                "danger", "info", "nav_bg", "nav_sel_bg", "nav_bar", "nav_locked", "toolbar_bg", "status_bg",
                "input_bg", "select_bg", "heading_bg", "danger_bg", "chart_bg", "chart_grid", "log_error"}
        self.assertFalse(need - set(theme.PALETTES["light"]))

    def test_legacy_and_pairs_reference_existing_roles(self):
        roles = set(theme.PALETTES["light"])
        self.assertFalse(set(theme.LEGACY_COLORS.values()) - roles)
        for fg, bg, _n in theme.CONTRAST_PAIRS:
            self.assertIn(fg, roles)
            self.assertIn(bg, roles)
        self.assertFalse(set(theme.ROLE_NOTES) - roles)
        for tag, bg, fg in theme.TREE_TAGS:
            for r in (bg, fg):
                if r:
                    self.assertIn(r, roles, tag)
        # ui_layout 的状态色（指示灯 / 卡片）都能对应到角色
        for c in ul.COLORS.values():
            self.assertIn(theme.legacy_role(c), roles, c)

    def test_palette_fallback(self):
        self.assertEqual(theme.palette("nope"), theme.PALETTES["light"])
        self.assertIsNot(theme.palette("dark"), theme.PALETTES["dark"])      # 副本，改它不影响模板


class Contrast(unittest.TestCase):
    def test_known_ratios(self):
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#ffffff"), 21.0, places=2)
        self.assertAlmostEqual(theme.contrast_ratio("#777", "#777777"), 1.0, places=5)
        self.assertAlmostEqual(theme.contrast_ratio("white", "#767676"), 4.54, places=2)
        with self.assertRaises(ValueError):
            theme.relative_luminance("SystemButtonFace")

    def test_all_pairs_meet_wcag(self):
        for name in ("light", "dark"):
            for fg, bg, ratio, need, ok in theme.contrast_report(name):
                self.assertTrue(ok, "%s：%s / %s 对比度 %.2f < %.1f" % (name, fg, bg, ratio, need))

    def test_body_text_at_least_45(self):
        for name, pal in theme.PALETTES.items():
            for fg in ("text", "text_muted", "danger", "warning", "success", "info"):
                for bg in ("bg", "surface", "card"):
                    self.assertGreaterEqual(theme.contrast_ratio(pal[fg], pal[bg]), 4.5, (name, fg, bg))
            # 告警行：浅红底上的正文
            self.assertGreaterEqual(theme.contrast_ratio(pal["text"], pal["danger_bg"]), 4.5, name)
            # 无权限灰显仍可读（非活动控件 ≥ 3）
            self.assertGreaterEqual(theme.contrast_ratio(pal["nav_locked"], pal["nav_bg"]), 3.0, name)
            # 灰显要和正常导航文字区分得开
            self.assertGreater(theme.contrast_ratio(pal["nav_text"], pal["nav_bg"]),
                               theme.contrast_ratio(pal["nav_locked"], pal["nav_bg"]) + 1.5, name)

    def test_series_visible_on_chart(self):
        for name in ("light", "dark"):
            bg = theme.PALETTES[name]["chart_bg"]
            for c in theme.SERIES[name]:
                self.assertGreaterEqual(theme.contrast_ratio(c, bg), theme.SERIES_MIN_CONTRAST, (name, c))
            # 网格线是“浅”的：与底色差别小于坐标轴
            p = theme.PALETTES[name]
            self.assertLess(theme.contrast_ratio(p["chart_grid"], bg), theme.contrast_ratio(p["chart_axis"], bg))


class FakeReg:
    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, value=None, fail=False):
        self.value, self.fail, self.closed, self.opened = value, fail, 0, []

    def OpenKey(self, root, path):
        self.opened.append((root, path))
        if self.fail:
            raise OSError("no key")
        return "key"

    def QueryValueEx(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        return self.value, 4

    def CloseKey(self, key):
        self.closed += 1


class ThemeSetting(unittest.TestCase):
    def test_normalize_is_lenient(self):
        for v, want in (("light", "light"), ("DARK", "dark"), (" auto ", "auto"), ("system", "auto"),
                        ("浅色", "light"), ("深色", "dark"), ("跟随系统", "auto"), ("", "auto"),
                        (None, "auto"), (1, "auto"), ("purple", "auto"), (["dark"], "auto")):
            self.assertEqual(theme.normalize_theme(v), want, v)

    def test_auto_reads_registry(self):
        r = FakeReg(1)
        self.assertEqual(theme.resolve_theme("auto", reg=r, platform="win32"), "light")
        self.assertEqual(r.opened[0][1], r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        self.assertEqual(r.closed, 1)
        self.assertEqual(theme.resolve_theme("auto", reg=FakeReg(0), platform="win32"), "dark")

    def test_auto_falls_back_to_light(self):
        self.assertEqual(theme.resolve_theme("auto", reg=FakeReg(fail=True), platform="win32"), "light")
        self.assertEqual(theme.resolve_theme("auto", reg=FakeReg(None), platform="win32"), "light")
        self.assertEqual(theme.resolve_theme("auto", reg=FakeReg("garbage"), platform="win32"), "light")
        self.assertEqual(theme.resolve_theme("auto", platform="linux"), "light")
        self.assertIsNone(theme.windows_prefers_light(FakeReg(fail=True)))

    def test_explicit_ignores_registry(self):
        self.assertEqual(theme.resolve_theme("dark", reg=FakeReg(1), platform="win32"), "dark")
        self.assertEqual(theme.resolve_theme("light", reg=FakeReg(0), platform="win32"), "light")
        self.assertEqual(theme.resolve_theme("bogus", reg=FakeReg(0), platform="win32"), "dark")   # 无效 → auto

    def test_config_key_everywhere(self):
        import config_tools
        import server_app as sa
        self.assertEqual(sa.DEFAULT_CONFIG["desktop_theme"], "auto")
        self.assertIn("desktop_theme", sa.DESKTOP_KEYS)
        self.assertIs(config_tools.EDITABLE["desktop_theme"], str)
        self.assertEqual(json.loads(_read("config.json"))["desktop_theme"], "auto")

    def test_normalize_config_falls_back(self):
        import server_app as sa
        tmp = tempfile.mkdtemp()
        try:
            for raw, want in (("dark", "dark"), ("LIGHT", "light"), ("rainbow", "auto"), (42, "auto"), (None, "auto")):
                cfg = dict(sa.DEFAULT_CONFIG)
                cfg["desktop_theme"] = raw
                cfg["watch_dirs"] = [tmp]
                sa.normalize_config(cfg, tmp)          # 不报 ConfigError
                self.assertEqual(cfg["desktop_theme"], want, raw)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FakeStyle:
    def __init__(self, base="default"):
        self.base, self.conf, self.maps, self.layouts, self.used = base, {}, {}, {}, []

    def theme_use(self, name=None):
        if name is None:
            return self.base
        self.used.append(name)
        self.base = name

    def configure(self, name, **kw):
        self.conf.setdefault(name, {}).update(kw)

    def map(self, name, **kw):
        self.maps.setdefault(name, {}).update(kw)

    def layout(self, name, spec=None):
        self.layouts[name] = spec


def _style_refs():
    """desktop_ui.py 里 style=… 用到的样式名：字符串常量或 theme.STYLE["键"]。"""
    tree = ast.parse(_read("desktop_ui.py"))
    names, keys = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "style":
            v = node.value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                names.add(v.value)
            elif isinstance(v, ast.Subscript) and isinstance(v.value, ast.Attribute) and v.value.attr == "STYLE":
                sl = v.slice
                if isinstance(sl, ast.Constant):
                    keys.add(sl.value)
                elif isinstance(sl, ast.IfExp):
                    for c in (sl.body, sl.orelse):
                        if isinstance(c, ast.Constant):
                            keys.add(c.value)
    return names, keys


class Styles(unittest.TestCase):
    def test_every_style_used_is_defined(self):
        defined = theme.style_names()
        names, keys = _style_refs()
        self.assertGreaterEqual(len(keys), 8, "desktop_ui.py 里应有 style=theme.STYLE[...] 引用")
        for k in keys:
            self.assertIn(k, theme.STYLE, k)
            self.assertIn(theme.STYLE[k], defined, k)
        for n in names:
            self.assertIn(n, defined, n)
        for v in theme.STYLE.values():
            self.assertIn(v, defined, v)

    def test_three_button_styles(self):
        defined = theme.style_names()
        for kind in ("primary", "secondary", "danger"):
            self.assertIn(theme.button_style(kind), defined)
        self.assertEqual(len({theme.button_style(k) for k in ("primary", "secondary", "danger")}), 3)
        light = theme.ttk_specs(theme.palette("light"))
        self.assertEqual(light["Primary.TButton"]["configure"]["background"], theme.PALETTES["light"]["primary"])
        self.assertEqual(light["Danger.TButton"]["configure"]["background"], theme.PALETTES["light"]["danger_btn"])
        self.assertIn("active", [s for s, _v in light["TButton"]["map"]["background"]])    # 悬停效果

    def test_variants_defined(self):
        defined = theme.style_names()
        for base in ("TCheckbutton", "TRadiobutton"):
            for role in theme.CONTAINER_VARIANTS:
                self.assertIn(theme.variant_style(base, role), defined)
        self.assertEqual(theme.variant_style("TCheckbutton", "bg"), "Page.TCheckbutton")
        self.assertEqual(theme.variant_style("TCheckbutton", "weird"), "TCheckbutton")

    def test_specs_same_for_both_themes(self):
        a, b = theme.ttk_specs(theme.palette("light")), theme.ttk_specs(theme.palette("dark"))
        self.assertEqual(set(a), set(b))
        for name in a:
            self.assertEqual(set(a[name].get("configure", {})), set(b[name].get("configure", {})), name)

    def test_apply_uses_clam_and_configures(self):
        st = FakeStyle()
        n = theme.apply_ttk(st, theme.palette("dark"), theme.metrics(1.5))
        self.assertEqual(st.used, ["clam"])
        self.assertEqual(n, len(theme.ttk_specs(theme.palette("dark"))))
        self.assertEqual(st.conf["Treeview"]["rowheight"], theme.metrics(1.5)["rowheight"])
        self.assertEqual(st.conf["."]["background"], theme.PALETTES["dark"]["bg"])
        self.assertIn("Vertical.TScrollbar", st.layouts)            # 扁平滚动条（无箭头）
        self.assertEqual(st.conf["Treeview.Heading"]["relief"], "flat")
        st2 = FakeStyle("clam")
        theme.apply_ttk(st2, theme.palette("light"))
        self.assertEqual(st2.used, [])                              # 已经是 clam 就不再切换

    def test_option_db(self):
        class Root:
            def __init__(self):
                self.opts = {}

            def option_add(self, k, v):
                self.opts[k] = v
        r = Root()
        theme.apply_option_db(r, theme.palette("dark"))
        self.assertEqual(r.opts["*Label.background"], theme.PALETTES["dark"]["bg"])
        self.assertEqual(r.opts["*Text.background"], theme.PALETTES["dark"]["input_bg"])
        self.assertIn("*TCombobox*Listbox.background", r.opts)


class Buttons(unittest.TestCase):
    def test_kinds(self):
        for t in ("▶ 启动服务", "保存设置", "保存并重启服务", "登录", "开始测试", "生成报表", "导出 CSV", "查询"):
            self.assertEqual(theme.button_kind(t), "primary", t)
        for t in ("■ 停止服务", "停止", "删除所选", "删除选中账号", "还原", "还原并重启服务", "清除", "清空列表",
                  "写入（确认后执行）"):
            self.assertEqual(theme.button_kind(t), "danger", t)
        for t in ("刷新", "浏览…", "放弃修改", "清空显示", "打开日志文件夹", "", None, "Refresh"):
            self.assertEqual(theme.button_kind(t), "secondary", t)

    def test_english_text_maps_back(self):
        i18n.set_language("en")
        try:
            en = i18n.tr("■ 停止服务")
            self.assertNotEqual(en, "■ 停止服务")
            self.assertEqual(theme.button_kind(i18n.untr(en)), "danger")
        finally:
            i18n.set_language("zh")


class W:
    """假控件：winfo_class / cget / configure / winfo_children / tag_configure。"""

    def __init__(self, cls, master=None, **opts):
        self.cls, self.master, self.opts, self.kids, self.tags = cls, master, dict(opts), [], {}
        if master is not None:
            master.kids.append(self)

    def winfo_class(self):
        return self.cls

    def cget(self, k):
        if k not in self.opts:
            raise KeyError(k)
        return self.opts[k]

    def configure(self, **kw):
        for k in kw:
            if k not in self.opts and k != "style":
                raise KeyError(k)
        self.opts.update(kw)

    def winfo_children(self):
        return list(self.kids)

    def tag_configure(self, tag, **kw):
        self.tags.setdefault(tag, {}).update(kw)


def _frame(master=None, pal=None, **kw):
    p = pal or theme.PALETTES["light"]
    o = {"background": p["bg"], "highlightbackground": "SystemButtonFace", "highlightcolor": "SystemButtonFace"}
    o.update(kw)
    return W("Frame", master, **o)


def _label(master, pal=None, **kw):
    p = pal or theme.PALETTES["light"]
    o = {"background": p["bg"], "foreground": p["text"], "activebackground": "SystemButtonFace",
         "disabledforeground": "SystemDisabledText"}
    o.update(kw)
    return W("Label", master, **o)


class Painting(unittest.TestCase):
    def setUp(self):
        self.L, self.D = theme.palette("light"), theme.palette("dark")
        self.root = W("Tk", None, background="SystemButtonFace")
        self.nav = _frame(self.root)
        theme.set_roles(self.nav, background="nav_bg")
        self.nav_lbl = _label(self.nav)
        self.sec = W("LabelFrame", self.root, background=self.L["bg"], foreground="#1f4e79",
                     highlightbackground="#d0d7de", highlightcolor="#d0d7de")
        theme.set_roles(self.sec, background="surface")
        self.hint = _label(self.sec, foreground="#777")
        self.err = _label(self.sec, foreground="#cf222e")
        self.card_lbl = _label(self.sec, background="white", foreground="#1f4e79")
        self.custom = _label(self.sec, foreground="#123456")
        self.text = W("Text", self.sec, background=self.L["input_bg"], foreground=self.L["text"],
                      insertbackground="black", selectbackground="#c3c3c3", selectforeground="black",
                      inactiveselectbackground="#c3c3c3", highlightbackground="#d9d9d9", highlightcolor="black")
        self.tree = W("Treeview", self.sec)
        self.chk = W("TCheckbutton", self.root, style="")
        self.chk2 = W("TCheckbutton", self.sec, style="")
        self.canvas = W("Canvas", self.sec, background="white", highlightbackground="#d0d7de",
                        highlightcolor="#d0d7de")
        theme.set_roles(self.canvas, background="chart_bg")

    def test_first_paint_light(self):
        n = theme.paint_tree(self.root, self.L)
        self.assertEqual(n, 13)
        L = self.L
        self.assertEqual(self.root.opts["background"], L["bg"])
        self.assertEqual(self.nav.opts["background"], L["nav_bg"])
        self.assertEqual(self.nav_lbl.opts["background"], L["nav_bg"])       # 继承父控件底色
        self.assertEqual(self.nav_lbl.opts["foreground"], L["nav_text"])     # 按底色挑文字色
        self.assertEqual(self.sec.opts["background"], L["surface"])
        self.assertEqual(self.sec.opts["foreground"], L["accent"])
        self.assertEqual(self.hint.opts["background"], L["surface"])
        self.assertEqual(self.hint.opts["foreground"], L["text_muted"])      # 旧的 #777 → 次要文字
        self.assertEqual(self.err.opts["foreground"], L["danger"])
        self.assertEqual(self.custom.opts["foreground"], "#123456")          # 不认识的自定义颜色不动
        self.assertEqual(self.text.opts["background"], L["input_bg"])
        self.assertEqual(self.text.opts["insertbackground"], L["text"])
        self.assertEqual(self.canvas.opts["background"], L["chart_bg"])
        self.assertEqual(self.chk.opts["style"], "Page.TCheckbutton")       # 在页面底色上
        self.assertEqual(self.chk2.opts.get("style"), "")                   # 在区域里：默认样式（区域底色）
        self.assertEqual(self.tree.tags["alert"]["background"], L["danger_bg"])
        self.assertEqual(self.tree.tags["odd"]["background"], L["surface_alt"])

    def test_switch_to_dark_and_back(self):
        theme.paint_tree(self.root, self.L)
        theme.paint_tree(self.root, self.D)
        D = self.D
        self.assertEqual(self.root.opts["background"], D["bg"])
        self.assertEqual(self.nav_lbl.opts["background"], D["nav_bg"])
        self.assertEqual(self.nav_lbl.opts["foreground"], D["nav_text"])
        self.assertEqual(self.sec.opts["background"], D["surface"])
        self.assertEqual(self.hint.opts["foreground"], D["text_muted"])
        self.assertEqual(self.err.opts["foreground"], D["danger"])
        self.assertEqual(self.card_lbl.opts["foreground"], D["accent"])
        self.assertEqual(self.text.opts["background"], D["input_bg"])
        self.assertEqual(self.canvas.opts["background"], D["chart_bg"])
        self.assertEqual(self.tree.tags["alert"]["background"], D["danger_bg"])
        self.assertEqual(self.custom.opts["foreground"], "#123456")
        theme.paint_tree(self.root, self.L)
        self.assertEqual(self.hint.opts["foreground"], self.L["text_muted"])
        self.assertEqual(self.nav.opts["background"], self.L["nav_bg"])

    def test_first_paint_in_dark(self):
        """窗口一开始就是深色：选项数据库给的默认色是深色调色板的值，也要认成“默认”而不是写死。"""
        D = self.D
        root = W("Tk", None, background="SystemButtonFace")
        f = _frame(root, D)
        lb = _label(f, D)
        legacy = _label(f, D, foreground="#57606a")
        theme.paint_tree(root, D)
        theme.paint_tree(root, self.L)
        self.assertEqual(lb.opts["background"], self.L["bg"])
        self.assertEqual(lb.opts["foreground"], self.L["text"])
        self.assertEqual(legacy.opts["foreground"], self.L["text_muted"])

    def test_skip_and_bad_widgets(self):
        class Broken:
            def winfo_class(self):
                raise RuntimeError("destroyed")
        self.root.kids.append(Broken())
        theme.paint_tree(self.root, self.L, skip=lambda w: w is self.sec)
        self.assertEqual(self.hint.opts["foreground"], "#777")              # 跳过的子树不动

    def test_color_helper(self):
        self.assertEqual(theme.color(self.D, "danger"), self.D["danger"])
        self.assertEqual(theme.color(self.D, "#CF222E"), self.D["danger"])
        self.assertEqual(theme.color(self.D, "SystemWindow"), "SystemWindow")
        self.assertEqual(theme.norm_color("#abc"), "#aabbcc")
        self.assertIsNone(theme.norm_color("Microsoft"))


class RowsAndLog(unittest.TestCase):
    def test_zebra(self):
        self.assertEqual(theme.zebra_tags(0), ("even",))
        self.assertEqual(theme.zebra_tags(1), ("odd",))
        self.assertEqual(theme.zebra_tags(3, ("x",)), ("x", "odd"))
        self.assertEqual(theme.zebra_tags(1, ("alert",)), ("alert",))        # 告警行不叠斑马纹
        tags = theme.tree_tags(theme.palette("light"))
        self.assertEqual(list(tags)[:2], ["odd", "even"])
        self.assertEqual(tags["alert"]["foreground"], theme.PALETTES["light"]["text"])   # 不是刺眼的红字
        self.assertNotIn("foreground", tags["odd"])

    def test_log_levels(self):
        self.assertEqual(theme.log_level("2026-11-26 10:00:00 [ERROR] x"), "error")
        self.assertEqual(theme.log_level("[CRITICAL] x"), "error")
        self.assertEqual(theme.log_level("[WARNING] x"), "warning")
        self.assertEqual(theme.log_level("[DEBUG] x"), "debug")
        self.assertEqual(theme.log_level("[INFO] x"), "info")
        self.assertEqual(theme.log_level("plain"), "info")
        self.assertEqual(set(theme.log_tags(theme.palette("dark"))),
                         {"log_debug", "log_info", "log_warning", "log_error"})


class FakeCtypes:
    class _Fn:
        def __init__(self, ret=None, exc=None, calls=None, name=""):
            self.ret, self.exc, self.calls, self.name = ret, exc, calls, name

        def __call__(self, *a):
            self.calls.append((self.name, a))
            if self.exc:
                raise self.exc
            return self.ret

    def __init__(self, shcore_ok=True, user32_ok=True, dpi=144):
        self.calls = []
        mk = self._Fn
        self.windll = type("WinDLL", (), {})()
        self.windll.shcore = type("S", (), {})()
        self.windll.shcore.SetProcessDpiAwareness = mk(0, None if shcore_ok else OSError("x"), self.calls, "shcore")
        self.windll.user32 = type("U", (), {})()
        self.windll.user32.SetProcessDPIAware = mk(1, None if user32_ok else OSError("y"), self.calls, "user32")
        self.windll.user32.GetDpiForSystem = mk(dpi, None, self.calls, "dpi")


class Dpi(unittest.TestCase):
    def test_scale_numbers(self):
        self.assertEqual(theme.ui_scale(96), 1.0)
        self.assertEqual(theme.ui_scale(144), 1.5)
        self.assertAlmostEqual(theme.tk_scaling(96), 1.3333, places=3)
        self.assertAlmostEqual(theme.tk_scaling(144), 2.0, places=3)
        for bad in (None, "x", -5, 0, float("nan")):
            self.assertEqual(theme.clamp_dpi(bad), 96.0)
        self.assertEqual(theme.clamp_dpi(10000), 480.0)
        self.assertEqual(theme.px(3, 1.5), 4)
        self.assertEqual(theme.px(0.1, 1), 1)
        self.assertEqual(theme.scale_size((900, 600), 1.5), (1350, 900))
        self.assertEqual(theme.scale_size((1100, 720), 1.5, (1366, 768)), (1326, 688))   # 不超过屏幕
        m1, m2 = theme.metrics(1.0), theme.metrics(2.0)
        self.assertGreater(m2["rowheight"], m1["rowheight"])
        self.assertEqual(theme.metrics(1.0, linespace=24)["rowheight"], 42)            # 行高随字体

    def test_awareness(self):
        c = FakeCtypes()
        self.assertEqual(theme.enable_dpi_awareness(c, platform="win32"), "shcore")
        self.assertEqual(c.calls[0], ("shcore", (1,)))
        c = FakeCtypes(shcore_ok=False)
        self.assertEqual(theme.enable_dpi_awareness(c, platform="win32"), "user32")
        c = FakeCtypes(shcore_ok=False, user32_ok=False)
        self.assertEqual(theme.enable_dpi_awareness(c, platform="win32"), "")
        self.assertEqual(theme.enable_dpi_awareness(FakeCtypes(), platform="linux"), "")
        self.assertEqual(theme.system_dpi(FakeCtypes(dpi=120), platform="win32"), 120)
        self.assertIsNone(theme.system_dpi(FakeCtypes(), platform="darwin"))

    def test_card_columns(self):
        self.assertEqual(ul.card_columns(900, 190), 4)
        self.assertEqual(ul.card_columns(632, 190), 3)        # 900x600 窗口、英文导航时的内容区
        self.assertEqual(ul.card_columns(300, 190), 2)
        self.assertEqual(ul.card_columns(5000, 190), 4)
        self.assertEqual(ul.card_columns(948, 285), 3)        # 150% 缩放
        self.assertEqual(ul.card_columns("x", 190), 4)

    def test_nav_width_scales(self):
        labels = ul.nav_labels(lambda t: t)
        w1 = ul.nav_width(labels)
        w15 = ul.nav_width(labels, measure=lambda t: ul.estimate_px(t) * 1.5, scale=1.5)
        self.assertGreater(w15, w1)
        lo, hi = ul.NAV_WIDTH_RANGE
        self.assertEqual(ul.nav_width(["x" * 500], scale=2), hi * 2)
        self.assertEqual(ul.nav_width(["a"], scale="bad"), lo)


class Fonts(unittest.TestCase):
    def tearDown(self):
        theme.set_fonts("Microsoft YaHei UI", "Consolas")

    def test_pick(self):
        self.assertEqual(theme.pick_font(["Arial", "Segoe UI", "microsoft yahei"]), "microsoft yahei")
        self.assertEqual(theme.pick_font(["Arial", "Segoe UI"]), "Segoe UI")
        self.assertEqual(theme.pick_font(["Microsoft YaHei UI", "Segoe UI"]), "Microsoft YaHei UI")
        self.assertIsNone(theme.pick_font(["Arial"]))
        self.assertEqual(theme.pick_font(["Consolas", "Courier New"], theme.MONO_CANDIDATES), "Consolas")

    def test_font_tuple(self):
        theme.set_fonts("Segoe UI", "Consolas")
        self.assertEqual(theme.font(10), ("Segoe UI", 10))
        self.assertEqual(theme.font("section", "bold"), ("Segoe UI", 11, "bold"))
        self.assertEqual(theme.font("mono", mono=True), ("Consolas", 10))
        theme.set_fonts(None, None)                     # 没找到字体：保持原来的
        self.assertEqual(theme.font_family(), "Segoe UI")

    def test_desktop_ui_has_no_hardcoded_font_family(self):
        src = _read("desktop_ui.py")
        self.assertNotIn('"Microsoft YaHei"', src)


class Translations(unittest.TestCase):
    def test_texts_have_english(self):
        keys = theme.texts()
        self.assertEqual(i18n.missing_keys(keys), [])
        for k in keys:
            self.assertFalse(CJK.search(i18n.EN.get(k, "")), k)
        self.assertEqual(i18n.EN["浅色"], "Light")
        self.assertEqual(i18n.EN["深色"], "Dark")


if __name__ == "__main__":
    unittest.main()
