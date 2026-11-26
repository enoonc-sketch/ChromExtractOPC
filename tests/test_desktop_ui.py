# -*- coding: utf-8 -*-
"""桌面窗口（desktop_ui）逻辑层测试：表单拼装、保存往返、坏值拒绝、离线状态拼装。
（v2.9 新增的服务控制、数据目录、导出 CSV 等见 test_desktop_app.py）

不依赖真窗口；窗口冒烟测试只在有显示（或 xvfb）时跑，没有就跳过。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_tools as ct        # noqa: E402
import desktop_features as df    # noqa: E402
import desktop_ui as du          # noqa: E402
import server_app as sa          # noqa: E402


class CollectSettings(unittest.TestCase):
    ORIG = {"poll_seconds": 30, "endpoint": "opc.tcp://0.0.0.0:4840/ChromExtract",
            "allow_anonymous": True, "watch_dirs": ["/data/a"], "web_port": 8840}

    def test_only_changed_keys(self):
        values = {"poll_seconds": "30", "endpoint": "opc.tcp://0.0.0.0:4840/ChromExtract",
                  "allow_anonymous": True, "web_port": "8840"}
        self.assertEqual(du.collect_settings(values, self.ORIG), {})

    def test_changed_number_and_bool(self):
        values = {"poll_seconds": "60", "allow_anonymous": False}
        self.assertEqual(du.collect_settings(values, self.ORIG),
                         {"poll_seconds": "60", "allow_anonymous": False})

    def test_watch_dirs_multiline_text(self):
        values = {"watch_dirs": "/data/a\n/data/b\n\n"}
        self.assertEqual(du.collect_settings(values, self.ORIG),
                         {"watch_dirs": ["/data/a", "/data/b"]})
        self.assertEqual(du.collect_settings({"watch_dirs": "/data/a"}, self.ORIG), {})


class NettestParams(unittest.TestCase):
    def test_serial_kind_gets_serial_fields(self):
        keys = [k for k, _l, _d, _t in du.nettest_fields("modbus_rtu")]
        self.assertEqual(keys[:5], ["port", "baud", "bytesize", "parity", "stopbits"])
        self.assertIn("unit", keys)

    def test_build_params_drops_unknown_and_casts_chk(self):
        params = du.build_nettest_params("modbus_rtu", {
            "port": "COM3", "baud": "9600", "unit": "1", "rs485_rts": 1, "bogus": "x"})
        self.assertEqual(params, {"port": "COM3", "baud": "9600", "unit": "1", "rs485_rts": True})

    def test_ping_only_declared_keys(self):
        params = du.build_nettest_params("ping", {"host": "1.2.3.4", "port": "COM3"})
        self.assertEqual(params, {"host": "1.2.3.4"})


class BodyShapes(unittest.TestCase):
    def test_quiet_items(self):
        self.assertEqual(du.quiet_once_item(" 2026-01-01 08:00 ", "2026-01-01 18:00", "大修"),
                         {"type": "once", "start": "2026-01-01 08:00",
                          "end": "2026-01-01 18:00", "note": "大修"})
        self.assertEqual(du.quiet_daily_item("22:00", "06:00", [6, 7]),
                         {"type": "daily", "start": "22:00", "end": "06:00",
                          "days": [6, 7], "note": ""})

    def test_channel_rule_and_probe(self):
        self.assertEqual(du.channel_rule_body("GC1|F1", True, "", "", "检修"),
                         {"channel": "GC1|F1", "mute": True, "stale_minutes": "",
                          "until": "", "note": "检修"})
        body = du.probe_body("", "DCS网关", "ping", {"host": "1.2.3.4"}, "60", "3", "警告")
        self.assertEqual(body["kind"], "ping")
        self.assertTrue(body["enabled"])
        self.assertEqual(body["params"], {"host": "1.2.3.4"})


class LogicSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"poll_seconds": 30}, f)
        self.logic = du.DesktopLogic(self.cfg, self.tmp, sa)

    def tearDown(self):
        self.logic.stop_stability()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _values(self, **over):
        values = {k: v for k, v in self.logic.state()["config"].items()
                  if k in du.ALL_FIELDS}
        values.update(over)
        return values

    def test_save_roundtrip(self):
        msg = self.logic.save(self._values(poll_seconds="45"))
        self.assertIn("poll_seconds", msg)
        self.assertEqual(ct.read_raw(self.cfg)["poll_seconds"], 45)

    def test_no_change_no_write(self):
        before = open(self.cfg, "rb").read()
        self.assertEqual(self.logic.save(self._values()), "没有改动。")
        self.assertEqual(open(self.cfg, "rb").read(), before)

    def test_bad_value_rejected_file_untouched(self):
        before = open(self.cfg, "rb").read()
        with self.assertRaises(ct.ConfigToolError):
            self.logic.save(self._values(endpoint="http://x"))
        self.assertEqual(open(self.cfg, "rb").read(), before)

    def test_state_has_no_password_hash(self):
        self.logic.action("add_user", {"name": "dcs", "password": "Str0ng!pass"})
        st = json.dumps(self.logic.state())
        self.assertIn("dcs", st)
        self.assertNotIn("pbkdf2", st)

    def test_offline_alerts_snapshot(self):
        a = self.logic.alerts()
        self.assertTrue(a["offline"])
        self.assertEqual(a["active"], [])
        self.assertIn("quiet_list", a)
        self.assertIn("channel_list", a)

    def test_probe_save_and_remove(self):
        self.logic.action("probe_save", {"probe": du.probe_body(
            "", "测试Ping", "ping", {"host": "127.0.0.1"}, "60", "3", "警告")})
        probes = self.logic.probes()["probes"]
        self.assertEqual([p["label"] for p in probes], ["测试Ping"])
        self.logic.action("probe_remove", {"id": probes[0]["id"]})
        self.assertEqual(self.logic.probes()["probes"], [])


class ModbusParams(unittest.TestCase):
    """v2.8：Modbus Poll/Slave 表单参数拼装（纯函数）。"""

    def test_poll_tcp_params(self):
        p = du.build_modbus_poll_params({"transport": "TCP", "host": "192.168.1.20", "port": "502",
                                         "unit": "7", "func": "03 读保持寄存器", "addr": "100",
                                         "count": "4", "dtype": "float32", "order": "CDAB",
                                         "interval": "0.5"})
        self.assertEqual(p["transport"], "tcp")
        self.assertEqual(p["func"], 3)
        self.assertEqual(p["unit"], 7)
        self.assertEqual(p["addr"], 100)
        self.assertEqual(p["dtype"], "float32")
        self.assertEqual(p["order"], "CDAB")
        self.assertEqual(p["interval"], 0.5)

    def test_poll_rtu_params(self):
        p = du.build_modbus_poll_params({"transport": "RTU", "serial": "COM5", "baud": "19200",
                                         "parity": "E", "stopbits": "2", "func": "16 写多寄存器",
                                         "values": "123.45"})
        self.assertEqual(p["transport"], "rtu")
        self.assertEqual(p["func"], 16)
        self.assertEqual(p["baud"], 19200)
        self.assertEqual(p["parity"], "E")
        self.assertEqual(p["stopbits"], 2.0)

    def test_poll_bad_values_rejected(self):
        with self.assertRaises(ValueError):
            du.build_modbus_poll_params({"transport": "TCP", "host": "", "func": "03 读保持寄存器"})
        with self.assertRaises(ValueError):
            du.build_modbus_poll_params({"transport": "TCP", "host": "1.2.3.4", "unit": "999",
                                         "func": "03 读保持寄存器"})
        with self.assertRaises(ValueError):
            du.build_modbus_poll_params({"transport": "RTU", "serial": "", "func": "03 读保持寄存器"})

    def test_slave_params(self):
        p = du.build_modbus_slave_params({"transport": "TCP", "host": "0.0.0.0", "tcp_port": "1502",
                                          "unit": "3", "size": "5000"})
        self.assertEqual(p["tcp_port"], 1502)
        self.assertEqual(p["size"], 5000)
        p = du.build_modbus_slave_params({"transport": "RTU", "serial": "COM3", "unit": "7"})
        self.assertEqual(p["unit"], 7)
        with self.assertRaises(ValueError):
            du.build_modbus_slave_params({"transport": "RTU", "serial": "COM3", "unit": "0"})
        with self.assertRaises(ValueError):
            du.build_modbus_slave_params({"transport": "TCP", "size": "70000"})


class EntryPoints(unittest.TestCase):
    def test_help_lists_desktop_and_settings(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            sa.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = buf.getvalue()
        self.assertIn("--desktop", text)
        self.assertIn("--settings", text)

    def test_version_is_33(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            sa.main(["--version"])
        self.assertIn("3.3", buf.getvalue())

    def test_help_lists_modbus_tools(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            sa.main(["--help"])
        text = buf.getvalue()
        self.assertIn("--modbus-poll", text)
        self.assertIn("--modbus-slave", text)


class WindowSmoke(unittest.TestCase):
    """真实 tkinter 窗口冒烟（没有显示环境时跳过）：v3.2 左侧导航 + 懒加载页面。"""

    def test_window_constructs_and_closes(self):
        import ui_layout
        tmp = tempfile.mkdtemp()
        try:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"poll_seconds": 30, "desktop_language": "zh", "desktop_auth": False}, f)
            cfg = sa.load_config(cfg_path)
            cfg["_cfg_path"], cfg["_base_dir"] = cfg_path, tmp
            sa.normalize_config(cfg, tmp)
            try:
                win = du.DesktopWindow(cfg, start_service=False)
            except Exception as e:  # 无显示环境（tkinter.TclError 等）：跳过
                self.skipTest("无显示环境，跳过窗口冒烟：%s" % e)
            win.root.update_idletasks()
            win.root.update()
            # 打开时只建了总览页（懒加载）
            self.assertEqual(win.pages.current, "overview")
            self.assertEqual(win.pages.built, {"overview"})
            self.assertEqual([p.pid for p in ui_layout.pages()], list(win._nav_items))
            # 逐页打开：每页只建一次
            for pid in ui_layout.page_ids():
                win._show_page(pid)
                win.root.update()
                self.assertEqual(win.pages.current, pid)
                self.assertNotIn(pid, win.pages.errors, pid)
            for pid in ui_layout.page_ids():
                win._show_page(pid)
            self.assertTrue(all(n == 1 for n in win.pages.build_count.values()))
            # v3.1 / v3.0 / v2.9 / v2.8 的控件都还在（分散到新页面里）
            for attr in ("pk_tree", "hs_tree", "hs_canvas", "mb_tree", "rp_out", "bk_preview", "au_tree", "ur_tree",
                         "user_var", "btn_login", "lang_combo", "tb_lang", "ip_tree", "ps_tree", "sp_tree", "sp_out",
                         "sp_iface", "sp_rts", "chd_tree", "pv_canvas", "pv_combo", "pv_series", "cl_tree", "cl_sum",
                         "qs_msg", "login_var", "btn_start", "btn_stop", "btn_restart", "data_dirs_var", "src_out",
                         "exp_btn", "exp_dir", "exp_last", "box", "mbp_out", "mbp_poll_btn", "mbs_log", "mbs_area_out",
                         "mbs_btn", "status_dot", "sb_vars", "crumb_var"):
                self.assertTrue(hasattr(win, attr), attr)
            self.assertEqual(set(win.cs_prog), {"ip", "ps", "serial"})
            self.assertEqual(set(win._qvars), {f[0] for f in df.ALL_QUICK_FIELDS})
            self.assertFalse(win.svc.running())          # --settings：不自动启动服务
            win._set_language("en")
            self.assertEqual(win._nav_items["overview"].cget("text"), "Overview")
            self.assertEqual(win.btn_start.cget("text"), "▶ Start service")
            win._set_language("zh")
            self.assertEqual(win._nav_items["overview"].cget("text"), "总览")
            # v3.3：主题即时切换（深色 → 浅色），已建页面的经典控件颜色随之改变
            import theme
            win._set_theme("dark")
            win.root.update()
            self.assertEqual(str(win.root.cget("bg")).lower(), theme.PALETTES["dark"]["bg"])
            self.assertEqual(str(win.box.cget("bg")).lower(), theme.PALETTES["dark"]["input_bg"])
            win._set_theme("light")
            win.root.update()
            self.assertEqual(str(win.root.cget("bg")).lower(), theme.PALETTES["light"]["bg"])
            self.assertEqual(str(win.nav.cget("bg")).lower(), theme.PALETTES["light"]["nav_bg"])
            win._set_theme("auto")
            win._show_page("history")
            win._on_close()
            with open(cfg_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f).get("desktop_last_page"), "history")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
