# -*- coding: utf-8 -*-
"""桌面窗口（desktop_ui）逻辑层测试：表单拼装、保存往返、坏值拒绝、离线状态拼装。

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


class EntryPoints(unittest.TestCase):
    def test_help_lists_desktop_and_settings(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            sa.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = buf.getvalue()
        self.assertIn("--desktop", text)
        self.assertIn("--settings", text)

    def test_version_is_26(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            sa.main(["--version"])
        self.assertIn("2.6", buf.getvalue())


class WindowSmoke(unittest.TestCase):
    def test_window_constructs_and_closes(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"poll_seconds": 30}, f)
            cfg = sa.load_config(None)
            cfg["_cfg_path"], cfg["_base_dir"] = cfg_path, tmp
            sa.normalize_config(cfg, tmp)
            try:
                win = du.DesktopWindow(cfg, start_service=False)
            except Exception as e:  # 无显示环境（tkinter.TclError 等）：跳过
                self.skipTest("无显示环境，跳过窗口冒烟：%s" % e)
            win.root.update_idletasks()
            win.root.update()
            tabs = [win.nb.tab(i, "text") for i in range(win.nb.index("end"))]
            self.assertEqual(tabs, ["运行状态", "告警与通知", "诊断工具", "系统设置"])
            win._on_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
