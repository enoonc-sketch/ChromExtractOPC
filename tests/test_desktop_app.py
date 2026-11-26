# -*- coding: utf-8 -*-
"""v2.9 桌面软件化：窗口内启停服务（ServiceController）、数据目录 / 数据源 / CSV 导出、
单实例、旧后台参数被拒绝、旧版后台残留检测、窗口版 EXE 的参数判断。

不需要真窗口：窗口相关逻辑都在可单独测试的函数/类里；窗口冒烟见 test_desktop_ui.WindowSmoke。
"""
import ast
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_tools as ct        # noqa: E402
import demo_data                 # noqa: E402
import desktop_ui as du          # noqa: E402
import drivercheck               # noqa: E402
import instance_lock             # noqa: E402
import server_app as sa          # noqa: E402


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PureHelpers(unittest.TestCase):
    def test_resolve_dirs_relative_dedupe(self):
        base = os.path.abspath(os.sep + "base")
        got = du.resolve_dirs(["data", " ", '"%s"' % os.path.join(base, "data"), None], base)
        self.assertEqual(got, [os.path.join(base, "data")])

    def test_merge_replace_and_add(self):
        base = os.path.abspath(os.sep + "base")
        a, b = os.path.join(base, "a"), os.path.join(base, "b")
        self.assertEqual(du.merge_watch_dirs([a], b, base, replace=True), [b])
        self.assertEqual(du.merge_watch_dirs([a], b, base, replace=False), [a, b])
        self.assertEqual(du.merge_watch_dirs([a], a, base, replace=False), [a])
        with self.assertRaises(ValueError):
            du.merge_watch_dirs([a], "", base)

    def test_parse_last_n(self):
        self.assertIsNone(du.parse_last_n(""))
        self.assertIsNone(du.parse_last_n("  "))
        self.assertEqual(du.parse_last_n(" 20 "), 20)
        for bad in ("0", "-1", "abc", "1.5"):
            with self.assertRaises(ValueError):
                du.parse_last_n(bad)

    def test_export_dirs(self):
        base = os.path.abspath(os.sep + "base")
        self.assertEqual(du.default_export_dir({"csv_export_dir": "谱图导出"}, base), os.path.join(base, "谱图导出"))
        absd = os.path.abspath(os.sep + "x")
        self.assertEqual(du.default_export_dir({"csv_export_dir": absd}, base), absd)
        self.assertEqual(du.make_export_dir("R", datetime(2026, 1, 2, 3, 4, 5)),
                         os.path.join("R", "桌面导出_20260102_030405"))

    def test_service_status_text(self):
        t, _ = du.service_status_text(True, True, "", "opc.tcp://0.0.0.0:4840/x")
        self.assertIn("运行中", t)
        self.assertIn("关闭窗口会停止服务", t)
        self.assertIn("启动中", du.service_status_text(True, False, "", "e")[0])
        self.assertIn("启动失败", du.service_status_text(False, False, "端口被占用", "e")[0])
        self.assertIn("已停止", du.service_status_text(False, False, "", "e")[0])


class LogicData(unittest.TestCase):
    """数据目录选择、查看数据源、导出 CSV（与命令行 --list / --extract 同一套代码）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"poll_seconds": 30, "watch_dirs": []}, f)
        self.data = os.path.join(self.tmp, "数据")
        os.makedirs(self.data)
        demo_data.make_demo_db(os.path.join(self.data, "demo_V3.db"))
        self.logic = du.DesktopLogic(self.cfg, self.tmp, sa)

    def tearDown(self):
        self.logic.stop_stability()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_and_read_watch_dirs(self):
        saved = self.logic.set_watch_dirs([self.data])
        self.assertEqual(saved, [os.path.abspath(self.data)])
        self.assertEqual(self.logic.watch_dirs(), saved)
        self.assertEqual(ct.read_raw(self.cfg)["poll_seconds"], 30)        # 其它配置不动

    def test_relative_dir_resolves_against_program_dir(self):
        saved = self.logic.set_watch_dirs(["数据"])
        self.assertEqual(saved, [os.path.join(self.tmp, "数据")])

    def test_missing_dir_rejected(self):
        before = open(self.cfg, "rb").read()
        with self.assertRaises(ct.ConfigToolError):
            self.logic.set_watch_dirs([os.path.join(self.tmp, "没有")])
        self.assertEqual(open(self.cfg, "rb").read(), before)

    def test_list_sources(self):
        with self.assertRaises(ct.ConfigToolError):
            self.logic.list_sources_text()                  # 还没设数据目录
        self.logic.set_watch_dirs([self.data])
        text = self.logic.list_sources_text()
        self.assertIn("demo_V3.db", text)
        self.assertIn("检测器", text)

    def test_export_csv(self):
        self.logic.set_watch_dirs([self.data])
        out = du.make_export_dir(os.path.join(self.tmp, "导出"))
        n, text = self.logic.export_csv(out, last=2)
        self.assertGreater(n, 0)
        self.assertIn("完成", text)
        self.assertTrue(os.path.isfile(os.path.join(out, "index.csv")))
        self.assertEqual(len(os.listdir(os.path.join(out, "csv"))), n)

    def test_export_missing_dir_reports(self):
        with self.assertRaises(ct.ConfigToolError):
            self.logic.export_csv(os.path.join(self.tmp, "o"), dirs=[os.path.join(self.tmp, "nope")])


class ServiceControllerTest(unittest.TestCase):
    """窗口里的“启动服务 / 停止服务 / 重启”：真实 asyncua 服务在本进程工作线程里运行。"""

    def setUp(self):
        try:
            import asyncua  # noqa: F401
        except ImportError:
            self.skipTest("没有安装 asyncua")
        self.tmp = tempfile.mkdtemp()
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        demo_data.make_demo_db(os.path.join(data, "t_V3.db"))
        self.port = _free_port()
        cfg = dict(sa.DEFAULT_CONFIG)
        cfg.update(endpoint="opc.tcp://127.0.0.1:%d/ChromExtract" % self.port, watch_dirs=[data],
                   security="none", log_file="", web_ui=False, env_check_on_start=False)
        cfg["_base_dir"], cfg["_cfg_path"] = self.tmp, None
        sa.normalize_config(cfg, self.tmp)
        self.cfg = cfg
        self.svc = du.ServiceController(sa)

    def tearDown(self):
        if hasattr(self, "svc"):
            self.svc.stop(10)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait(self, cond, sec=40):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.2)
        return False

    def _port_open(self):
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def test_start_stop_restart(self):
        self.assertFalse(self.svc.running())
        self.assertTrue(self.svc.start(self.cfg))
        self.assertFalse(self.svc.start(self.cfg))                  # 已在运行：不重复启动
        self.assertTrue(self._wait(self.svc.is_up), "服务没有启动：%s" % self.svc.error)
        self.assertTrue(self._port_open())
        self.assertTrue(self.svc.stop(15))
        self.assertFalse(self.svc.running())
        self.assertIsNone(sa.ALERTS.get("monitor"))
        self.assertTrue(self._wait(lambda: not self._port_open(), 10), "停止后端口仍在监听")
        # 重启：同一进程里再次启动
        self.assertTrue(self.svc.start(self.cfg))
        self.assertTrue(self._wait(self.svc.is_up), "重启失败：%s" % self.svc.error)
        self.assertTrue(self.svc.stop(15))

    def test_port_in_use_reports_error(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", self.port))
        blocker.listen(1)
        try:
            self.svc.start(self.cfg)
            self.assertTrue(self._wait(lambda: not self.svc.running(), 30), "端口占用时服务线程应退出")
            self.assertTrue(self.svc.error)
            self.assertFalse(self.svc.is_up())
        finally:
            blocker.close()

    def test_stop_when_not_running_is_safe(self):
        self.assertTrue(self.svc.stop(1))


class SingleInstanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_instance_refused(self):
        a, b = instance_lock.SingleInstance(self.tmp), instance_lock.SingleInstance(self.tmp)
        self.assertTrue(a.acquire())
        try:
            self.assertFalse(b.acquire())
        finally:
            a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_run_desktop_app_exits_when_already_running(self):
        held = instance_lock.SingleInstance(self.tmp)
        self.assertTrue(held.acquire())
        shown = []
        old_info, old_gui = sa.show_info_dialog, sa.run_gui
        sa.show_info_dialog = lambda t, m: shown.append(m)
        sa.run_gui = lambda *a, **k: self.fail("已有实例时不能再开窗口")
        try:
            cfg = dict(sa.DEFAULT_CONFIG, log_file="")
            cfg["_base_dir"], cfg["_cfg_path"] = self.tmp, None
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sa.run_desktop_app(cfg, self.tmp, None), 0)
            self.assertTrue(shown and "已经打开" in shown[0])
        finally:
            sa.show_info_dialog, sa.run_gui = old_info, old_gui
            held.release()


class NoBackgroundMode(unittest.TestCase):
    """v2.9：后台静默 / Windows 服务 / 开机自启 已移除，旧参数给出中文说明并返回 2。"""

    def test_legacy_args_rejected(self):
        for arg in ("--background", "--status", "--stop", "--service", "--install-service",
                    "--uninstall-service", "--start-service", "--stop-service", "--service-status"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(sa.main([arg]), 2, arg)
            self.assertIn("桌面软件", buf.getvalue())

    def test_help_has_no_background_options(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            sa.main(["--help"])
        text = buf.getvalue()
        for gone in ("--background", "--install-service", "--service-status", "--stop-service"):
            self.assertNotIn(gone, text)
        for kept in ("--settings", "--selftest", "--extract", "--list", "--diagnose", "--drivercheck"):
            self.assertIn(kept, text)

    def test_removed_functions_and_modules(self):
        for name in ("serve", "service_cli", "run_windows_service"):
            self.assertFalse(hasattr(sa, name), name)
        for mod in ("bgrun", "winservice"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, mod + ".py")), mod)
        for dp, _dirs, files in os.walk(ROOT):
            if "build_env" in dp or ".git" in dp:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(dp, fn), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names = [node.module]
                    for n in names:
                        self.assertNotIn(n.split(".")[0], ("bgrun", "winservice"), os.path.join(dp, fn))

    def test_tool_invocation_detection(self):
        self.assertFalse(sa._is_tool_invocation([]))
        self.assertFalse(sa._is_tool_invocation(["--settings"]))
        self.assertFalse(sa._is_tool_invocation(["--gui", "--config", "x.json"]))
        self.assertFalse(sa._is_tool_invocation(["--config=x.json"]))
        self.assertTrue(sa._is_tool_invocation(["--selftest"]))
        self.assertTrue(sa._is_tool_invocation(["--extract", "d", "-o", "o"]))

    def test_selftest_core_restores_log_level(self):
        try:
            import asyncua  # noqa: F401
        except ImportError:
            self.skipTest("没有安装 asyncua")
        import logging
        root = logging.getLogger()
        old = root.level
        root.setLevel(logging.INFO)
        try:
            ok, msg = sa.selftest_core()
            self.assertTrue(ok, msg)
            self.assertEqual(root.level, logging.INFO)
        finally:
            root.setLevel(old)


class LegacyLeftoverCheck(unittest.TestCase):
    def test_reports_old_task_and_service(self):
        items = drivercheck.check_system({}, runner=lambda s: {"fw": "Running", "task": "Ready",
                                                               "svc": "Stopped", "os": "Windows 10"})
        left = [i for i in items if i["name"] == "旧版后台运行残留"]
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["level"], "注意")
        self.assertIn("卸载.bat", left[0]["fix"])
        self.assertIn("计划任务", left[0]["text"])
        self.assertIn("Windows 服务", left[0]["text"])

    def test_clean_system_has_no_autostart_item(self):
        items = drivercheck.check_system({}, runner=lambda s: {"fw": "Running", "os": "Windows 11"})
        self.assertFalse([i for i in items if "自启" in i["name"] or "残留" in i["name"]])

    def test_bad_runner_output_safe(self):
        items = drivercheck.check_system({}, runner=lambda s: ["not", "a", "dict"])
        self.assertIsInstance(items, list)


if __name__ == "__main__":
    unittest.main()
