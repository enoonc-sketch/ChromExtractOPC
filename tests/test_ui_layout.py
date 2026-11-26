# -*- coding: utf-8 -*-
"""v3.2 主窗口导航结构（ui_layout.py）测试——不需要 tkinter：

  - 分组 / 页面数量符合要求（一级 ≤5 组、每组 3～5 项），结构自检无问题；
  - 不丢功能：v3.1 的 14 个标签页及其每个区域都恰好映射到一个新页面，构建方法 / 区域方法在 DesktopWindow 上都存在，
    区域方法里画的区域标题与 v3.1 一致；菜单、卡片、刷新表引用的方法都存在；
  - i18n：导航、子标签、卡片、菜单、状态栏的文字都有英文，英文下导航文字确实变化；
  - 角色：每个页面所需角色正确，操作员 / 工程师 / 管理员 / 关闭登录 四种身份下锁住的页面正确；
  - 懒加载：PageManager 每页只构建一次；不可见时渲染只记参数、显示时按顺序补调；构建出错不反复重试；
  - 记住页面与窗口大小：last_page 无效或无权限时回退总览；geometry 解析 / 校验 / 夹到屏幕；
    新配置键在 DEFAULT_CONFIG / EDITABLE / normalize_config / config.json 里；
  - 导航宽度按最长文字自适应；总览卡片与状态栏的数值。
"""
import ast
import json
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import audit                    # noqa: E402
import config_tools as ct       # noqa: E402
import desktop_ui as du         # noqa: E402
import i18n                     # noqa: E402
import server_app as sa         # noqa: E402
import ui_layout as ul          # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")


def _window_methods():
    """DesktopWindow（含 V31Mixin / V32Mixin）上定义的方法名。"""
    names = set()
    for cls in du.DesktopWindow.mro():
        names |= {k for k, v in vars(cls).items() if callable(v)}
    return names


def _section_titles():
    """desktop_ui.py 里每个 _sec_* 方法画的第一个区域标题（self._section(inner, tr("…"))）。"""
    with open(os.path.join(ROOT, "desktop_ui.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_sec_"):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "_section"
                        and len(sub.args) >= 2 and isinstance(sub.args[1], ast.Call) and sub.args[1].args
                        and isinstance(sub.args[1].args[0], ast.Constant)):
                    out[node.name] = sub.args[1].args[0].value
                    break
    return out


class Structure(unittest.TestCase):
    def test_check_layout_clean(self):
        self.assertEqual(ul.check_layout(), [])

    def test_group_and_page_counts(self):
        self.assertLessEqual(len(ul.GROUPS), 5)
        self.assertEqual([g.gid for g in ul.GROUPS], ["monitor", "data", "comm", "system"])
        for g in ul.GROUPS:
            self.assertTrue(3 <= len(g.pages) <= 5, g.gid)
        self.assertEqual(ul.page_ids(), ["overview", "channels", "preview", "alarms",
                                         "history", "export", "reports",
                                         "clients", "modbus", "commdiag", "tools",
                                         "quick", "security", "audit", "backup"])
        self.assertEqual(ul.DEFAULT_PAGE, "overview")
        self.assertIsNotNone(ul.page("overview"))
        self.assertIsNone(ul.page("nope"))
        self.assertEqual(ul.group_of("modbus").gid, "comm")
        # 通讯诊断：IP / 端口 / 串口是页面内子标签
        self.assertEqual(ul.page("commdiag").subtab_titles(), ["IP 扫描", "端口扫描", "串口诊断"])
        # 日志不占导航，放在底部可折叠面板
        self.assertNotIn("log", ul.page_ids())

    def test_every_v31_tab_and_section_has_a_new_home(self):
        lm = ul.legacy_map()
        self.assertEqual(len(ul.LEGACY_TABS), 14)
        for tab, secs in ul.LEGACY_TABS:
            want = set(secs) if secs else {""}
            self.assertEqual(set(lm[tab]), want, tab)
            for sec, pid in lm[tab].items():
                self.assertIn(pid, ul.page_ids(), (tab, sec))
        rows = ul.legacy_rows()
        self.assertEqual(len(rows), sum(max(1, len(s)) for _t, s in ul.LEGACY_TABS))
        self.assertFalse([r for r in rows if "缺失" in r[2]])
        # 每个区域只放在一个页面
        used = [s for p in ul.pages() for s in p.sections()]
        self.assertEqual(len(used), len(set(used)))
        self.assertEqual(set(used), set(ul.SECTION_SOURCES))
        self.assertEqual({p.builder for p in ul.pages() if p.builder} - {"_page_overview"}, set(ul.WHOLE_SOURCES))

    def test_methods_exist_on_window(self):
        have = _window_methods()
        need = set()
        for p in ul.pages():
            need |= set(p.methods())
        need |= {n for n, _p in ul.RENDER_GUARDS}
        need |= {m for ms in ul.ON_SHOW.values() for m in ms}
        need |= {it[2] for _t, items in ul.MENUS for it in items if it is not None}
        self.assertEqual(sorted(need - have), [])
        # v3.1 拆成区域方法后，原来的 8 个多区域构建方法不再存在（避免两套入口）
        for old in ("_build_status_tab", "_build_data_tab", "_build_alerts_tab", "_build_tools_tab",
                    "_build_system_tab", "_build_quick_tab", "_build_comm_tab", "_build_backup_tab"):
            self.assertNotIn(old, have)

    def test_section_titles_match_v31(self):
        titles = _section_titles()
        for meth, (_tab, sec) in ul.SECTION_SOURCES.items():
            if meth == "_sec_quick_lang":           # 界面语言区域由 _build_lang_section 画
                continue
            self.assertEqual(titles.get(meth), sec, meth)

    def test_render_guards_do_not_wrap_permission_checks(self):
        guarded = {n for names in du.GUARDED.values() for n in names}
        self.assertEqual({n for n, _p in ul.RENDER_GUARDS} & guarded, set())

    def test_cards(self):
        self.assertEqual([c[0] for c in ul.CARDS],
                         ["service", "endpoint", "clients", "channels", "alarms", "cycle", "modbus"])


class Translations(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("zh")

    def test_all_layout_texts_have_english(self):
        keys = {k for k in ul.texts() if CJK.search(k)}
        self.assertGreater(len(keys), 40)
        self.assertEqual(i18n.missing_keys(keys), [])

    def test_nav_text_changes_with_language(self):
        zh = ul.nav_labels(lambda s: i18n.tr(s, "zh"))
        en = ul.nav_labels(lambda s: i18n.tr(s, "en"))
        self.assertEqual(len(zh), len(en))
        self.assertIn("总览", zh)
        self.assertIn("Overview", en)
        self.assertIn("Monitoring", en)
        self.assertFalse([t for t in en if CJK.search(t)])
        self.assertEqual(ul.breadcrumb("commdiag", lambda s: i18n.tr(s, "en")), "Comms  ›  Comms diagnostics")
        self.assertEqual(ul.breadcrumb("commdiag"), "通讯  ›  通讯诊断")


class Roles(unittest.TestCase):
    EXPECT = {"overview": "operator", "channels": "operator", "preview": "operator", "alarms": "operator",
              "history": "operator", "export": "engineer", "reports": "operator",
              "clients": "operator", "modbus": "engineer", "commdiag": "engineer", "tools": "engineer",
              "quick": "engineer", "security": "admin", "audit": "admin", "backup": "admin"}

    def test_required_roles(self):
        self.assertEqual({pid: ul.required_role(pid) for pid in ul.page_ids()}, self.EXPECT)
        self.assertEqual(ul.required_role("nope"), "admin")
        for p in ul.pages():
            self.assertIn(p.perm, audit.PERMISSIONS, p.pid)

    def _can(self, role):
        return lambda perm: audit.allowed(role, perm)

    def test_locked_pages_per_role(self):
        self.assertEqual(ul.locked_pages(self._can("operator")),
                         ["export", "modbus", "commdiag", "tools", "quick", "security", "audit", "backup"])
        self.assertEqual(ul.locked_pages(self._can("engineer")), ["security", "audit", "backup"])
        self.assertEqual(ul.locked_pages(self._can("admin")), [])
        self.assertEqual(ul.locked_pages(None), [])           # 关闭桌面登录：全部可用

    def test_session_integration_and_hint(self):
        s = audit.Session(lambda: {}, lambda *_a: False, auth_enabled=True)
        self.assertFalse(ul.allowed("backup", s.can))
        self.assertTrue(ul.allowed("overview", s.can))
        self.assertFalse(ul.allowed("nope", s.can))
        s2 = audit.Session(lambda: {}, lambda *_a: False, auth_enabled=False)
        self.assertTrue(ul.allowed("backup", s2.can))
        hint = ul.lock_hint("backup", "操作员")
        self.assertIn("管理员", hint)
        self.assertIn("操作员", hint)
        en = ul.lock_hint("quick", "Operator", tr=lambda t: i18n.tr(t, "en"))
        self.assertIn("Engineer", en)
        self.assertFalse(CJK.search(en))

    def test_broken_can_means_locked(self):
        self.assertFalse(ul.allowed("quick", lambda _p: 1 / 0))


class LazyPages(unittest.TestCase):
    def setUp(self):
        self.built = []
        self.drawn = []
        self.mgr = ul.PageManager(self.built.append, order=[("draw_a", "a"), ("draw_b", "a"), ("draw_c", "b")])
        self.a = self.mgr.guard("draw_a", "a", lambda x: self.drawn.append(("a", x)))
        self.b = self.mgr.guard("draw_b", "a", lambda x=0: self.drawn.append(("b", x)))
        self.c = self.mgr.guard("draw_c", "b", lambda: self.drawn.append(("c",)))

    def test_build_once(self):
        self.assertTrue(self.mgr.show("a"))
        self.assertFalse(self.mgr.show("a"))
        self.mgr.show("b")
        self.mgr.show("a")
        self.assertEqual(self.built, ["a", "b"])
        self.assertEqual(self.mgr.build_count, {"a": 1, "b": 1})

    def test_hidden_pages_defer_and_replay_latest_in_order(self):
        self.mgr.show("b")
        self.b(1)
        self.a(1)
        self.a(2)                   # 只保留最后一次
        self.c()                    # b 可见：立即执行
        self.assertEqual(self.drawn, [("c",)])
        self.mgr.show("a")          # 建页面后按顺序补调：draw_a 在 draw_b 前
        self.assertEqual(self.drawn, [("c",), ("a", 2), ("b", 1)])
        self.a(3)                   # 可见：立即执行
        self.assertEqual(self.drawn[-1], ("a", 3))
        self.assertTrue(self.mgr.is_visible("a"))
        self.assertFalse(self.mgr.is_visible("b"))
        self.assertTrue(self.mgr.any_visible(("x", "a")))

    def test_render_during_build_runs_immediately(self):
        mgr = ul.PageManager(lambda pid: draw(pid))
        out = []
        draw = mgr.guard("_render_overview", "overview", out.append)
        mgr.show("overview")
        self.assertEqual(out, ["overview"])

    def test_build_error_is_recorded_not_retried(self):
        calls = []

        def bad(pid):
            calls.append(pid)
            raise RuntimeError("坏了")
        mgr = ul.PageManager(bad)
        mgr.show("x")
        mgr.show("y")
        mgr.show("x")
        self.assertEqual(calls, ["x", "y"])
        self.assertIn("x", mgr.errors)
        self.assertEqual(mgr.current, "x")

    def test_poll_tables_reference_real_pages(self):
        for pids in ul.POLL_PAGES.values():
            for pid in pids:
                self.assertIn(pid, ul.page_ids())


class Remember(unittest.TestCase):
    def test_last_page_fallback(self):
        self.assertEqual(ul.normalize_last_page("Modbus"), "modbus")
        for bad in (None, "", "nope", "log", "../x", 123, "总览", "a" * 60):
            self.assertEqual(ul.normalize_last_page(bad), "overview", bad)
        operator = lambda perm: audit.allowed("operator", perm)     # noqa: E731
        self.assertEqual(ul.resolve_start_page("history", operator), "history")
        self.assertEqual(ul.resolve_start_page("backup", operator), "overview")     # 没权限：回退总览
        self.assertEqual(ul.resolve_start_page("backup", None), "backup")
        self.assertEqual(ul.resolve_start_page("gone_in_v4", None), "overview")

    def test_geometry(self):
        self.assertEqual(ul.parse_geometry("1100x720+10+-20"), (1100, 720, 10, -20))
        self.assertEqual(ul.parse_geometry("1200x800"), (1200, 800, None, None))
        self.assertIsNone(ul.parse_geometry("big"))
        self.assertEqual(ul.normalize_geometry("300x200+5+5"), "900x600+5+5")     # 夹到最小尺寸
        self.assertEqual(ul.normalize_geometry("junk"), "")
        self.assertEqual(ul.normalize_geometry(""), "")
        self.assertEqual(ul.restore_geometry(""), "1100x720")
        self.assertEqual(ul.restore_geometry("", 1024, 700), "984x620")
        self.assertEqual(ul.restore_geometry("1300x900+100+50", 1920, 1080), "1300x900+100+50")
        self.assertEqual(ul.restore_geometry("1300x900+3000+50", 1920, 1080), "1300x900")   # 跑到屏幕外：丢位置
        self.assertEqual(ul.restore_geometry("5000x4000", 1920, 1080), "1920x1080")
        self.assertEqual(ul.DEFAULT_SIZE, (1100, 720))
        self.assertEqual(ul.MIN_SIZE, (900, 600))

    def test_config_keys(self):
        for k, default in (("desktop_last_page", "overview"), ("desktop_geometry", "")):
            self.assertEqual(sa.DEFAULT_CONFIG[k], default)
            self.assertIs(ct.EDITABLE[k], str)
            self.assertIn(k, sa.DESKTOP_KEYS)         # 窗口自己的设置：不需要重启服务
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8-sig") as f:
            pub = json.load(f)
        self.assertEqual(pub["desktop_last_page"], "overview")
        self.assertEqual(pub["desktop_geometry"], "")
        cfg = sa.load_config(None)
        cfg.update(desktop_last_page="NoSuchPage", desktop_geometry="10x10+1+1")
        sa.normalize_config(cfg, ".")                 # 不合法不报错：回退默认 / 夹到最小
        self.assertEqual(cfg["desktop_last_page"], "overview")
        self.assertEqual(cfg["desktop_geometry"], "900x600+1+1")
        cfg.update(desktop_last_page="tools", desktop_geometry="garbage")
        sa.normalize_config(cfg, ".")
        self.assertEqual((cfg["desktop_last_page"], cfg["desktop_geometry"]), ("tools", ""))


class Display(unittest.TestCase):
    def test_nav_width_fits_longest_label(self):
        labels_en = ul.nav_labels(lambda s: i18n.tr(s, "en"))
        labels_zh = ul.nav_labels(lambda s: i18n.tr(s, "zh"))
        w_en = ul.nav_width(labels_en)
        w_zh = ul.nav_width(labels_zh)
        lo, hi = ul.NAV_WIDTH_RANGE
        for w, labels in ((w_en, labels_en), (w_zh, labels_zh)):
            self.assertTrue(lo <= w <= hi)
            self.assertGreaterEqual(w, max(ul.estimate_px(t) for t in labels) + ul.ICON_PX)
        self.assertEqual(ul.nav_width(["a"]), lo)
        self.assertEqual(ul.nav_width(["x" * 500]), hi)
        self.assertEqual(ul.nav_width(["abcd"], measure=lambda t: 100), 100 + ul.ICON_PX + 44)
        self.assertEqual(ul.nav_width(["ab"], measure=lambda t: 1 / 0), lo)       # 测量失败时估算

    def test_service_state_and_port(self):
        self.assertEqual(ul.service_state(True, True)[0], "running")
        self.assertEqual(ul.service_state(True, False)[0], "starting")
        self.assertEqual(ul.service_state(False, False, "端口被占用")[0], "error")
        self.assertEqual(ul.service_state(False, False)[0], "stopped")
        self.assertEqual(ul.service_state(True, True, busy="自检中")[1], "自检中")
        self.assertEqual(ul.endpoint_port("opc.tcp://0.0.0.0:4841/ChromExtract"), "4841")
        self.assertEqual(ul.endpoint_port("opc.tcp://host/x"), "4840")
        self.assertEqual(ul.endpoint_port("http://x"), "—")

    def test_overview_metrics_running(self):
        svc = {"running": True, "up": True, "endpoint": "opc.tcp://0.0.0.0:4840/ChromExtract"}
        alerts = {"channels": 3, "last_scan": "2026-11-26 10:00:00",
                  "active": [{"level": "严重", "text": "x", "since": "t"}, {"level": "警告", "text": "y", "since": "t"}]}
        recs = [{"cycle_time": "2026-11-26 09:50:00"}, {"cycle_time": "2026-11-26 09:58:00"}]
        m = ul.overview_metrics(svc, recs, alerts, {"ok": True, "count": 2, "max": 10},
                                {"running": ("127.0.0.1", 502, 7), "enabled": True})
        self.assertEqual(m["service"][0], "运行中")
        self.assertEqual(m["endpoint"][0], "端口：4840")
        self.assertEqual(m["clients"][:2], ("2", "上限 10"))
        self.assertEqual(m["channels"][0], "3")
        self.assertEqual(m["alarms"][:2], ("2", "严重 1 / 警告 1"))
        self.assertEqual(m["alarms"][2], ul.COLORS["error"])
        self.assertEqual(m["cycle"][0], "2026-11-26 09:58:00")
        self.assertEqual(m["modbus"][0], "运行中")
        self.assertIn("127.0.0.1:502", m["modbus"][1])
        en = ul.overview_metrics(svc, recs, alerts, None, None, tr=lambda t: i18n.tr(t, "en"))
        self.assertEqual(en["service"][0], "Running")
        self.assertEqual(en["modbus"][0], "Disabled")

    def test_overview_metrics_stopped(self):
        m = ul.overview_metrics({"running": False, "up": False}, [], {"offline": True}, None,
                                {"running": None, "enabled": True}, n_channels=4)
        self.assertEqual(m["service"][0], "已停止")
        self.assertEqual(m["clients"][0], "0")
        self.assertEqual(m["alarms"][0], "—")
        self.assertEqual(m["channels"][0], "4")
        self.assertEqual(m["cycle"][0], "—")
        self.assertEqual(m["modbus"][0], "已启用，未运行")

    def test_status_bar(self):
        items = ul.status_bar_items({"running": True, "up": True, "endpoint": "opc.tcp://h:4900/x"},
                                    {"active": [{}], "last_scan": "10:00"}, {"ok": True, "count": 3})
        self.assertEqual(items, ["服务：运行中", "端口：4900", "连接数：3", "告警：1", "最后扫描：10:00"])
        items = ul.status_bar_items({"running": False, "up": False, "endpoint": "opc.tcp://h:4840/x"},
                                    {"offline": True}, None, tr=lambda t: i18n.tr(t, "en"))
        self.assertEqual(items, ["Service: Stopped", "Port: 4840", "Connections: 0", "Alerts: —", "Last scan: —"])


if __name__ == "__main__":
    unittest.main()
