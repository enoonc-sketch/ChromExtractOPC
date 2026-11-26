# -*- coding: utf-8 -*-
"""v3.1 服务侧接线测试（不需要 asyncua：只构造 OpcUaApp 对象，不启动 OPC UA 服务器）：

  - 新配置键都在 DEFAULT_CONFIG / EDITABLE / config.json，并按热加载 / 需重启分好类；
  - OpcUaApp.update_modbus：快照 → 峰识别 → 寄存器表 → 真实 socket 读回；
  - OpcUaApp.after_alarms：告警出现 / 恢复和客户端连接数写入历史库；
  - watcher_thread 每次有新快照时调用 history_cb；
  - 命令行 --backup / --restore（run_backup_cli / run_restore_cli）往返。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import contextlib
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_tools as ct      # noqa: E402
import demo_data               # noqa: E402
import history                 # noqa: E402
import modbus_server as ms     # noqa: E402
import server_app as sa        # noqa: E402

V31_KEYS = ("desktop_language", "backup_keep", "history_enabled", "history_file", "history_retention_days",
            "history_max_mb", "peak_min_height", "peak_min_width", "peak_noise_factor", "peak_smooth",
            "peak_start", "peak_end", "peak_series", "peaks_publish", "peaks_publish_max",
            "modbus_server_enabled", "modbus_server_host", "modbus_server_port", "modbus_server_allow",
            "modbus_server_unit", "modbus_server_order", "modbus_server_map", "desktop_report_auto",
            "desktop_report_shifts", "desktop_report_dir", "desktop_auth", "desktop_idle_lock_minutes",
            "user_roles")


class ConfigKeys(unittest.TestCase):
    def test_defaults_editable_published(self):
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8-sig") as f:
            pub = json.load(f)
        for k in V31_KEYS:
            self.assertIn(k, sa.DEFAULT_CONFIG, k)
            self.assertIn(k, pub, k)
            self.assertTrue(k in ct.EDITABLE or k in ct.STRUCTURED, k)
        self.assertEqual(sa.DEFAULT_CONFIG["modbus_server_host"], "127.0.0.1")     # 默认不对外
        self.assertFalse(sa.DEFAULT_CONFIG["modbus_server_enabled"])

    def test_hot_restart_classification(self):
        for k in ("modbus_server_enabled", "modbus_server_host", "modbus_server_port", "history_enabled",
                  "peaks_publish"):
            self.assertIn(k, sa.RESTART_KEYS)
        for k in ("modbus_server_map", "peak_min_height", "user_roles"):
            self.assertIn(k, sa.HOT_KEYS)
        for k in ("desktop_language", "desktop_auth", "desktop_report_auto"):
            self.assertIn(k, sa.DESKTOP_KEYS)

    def test_normalize_v31(self):
        cfg = sa.load_config(None)
        cfg.update(modbus_server_allow="10.0.0.0/8, 192.168.1.5", desktop_report_shifts=None, peak_smooth=4,
                   modbus_server_order="cdab", history_retention_days=99999)
        sa.normalize_config(cfg, ".")
        self.assertEqual(cfg["modbus_server_allow"], ["10.0.0.0/8", "192.168.1.5"])
        self.assertEqual(cfg["modbus_server_order"], "CDAB")
        self.assertEqual(cfg["history_retention_days"], 3650)
        self.assertEqual(len(cfg["desktop_report_shifts"]), 2)
        for bad in ({"modbus_server_map": [{"address": 0, "channel": "x", "field": "nope"}]},
                    {"peak_end": 5, "peak_start": 10}, {"desktop_report_shifts": [{"name": "a", "start": "9"}]},
                    {"history_file": "a.txt"}, {"peaks_publish": "maybe"}):
            c = sa.load_config(None)
            c.update(bad)
            with self.assertRaises(sa.ConfigError, msg=str(bad)):
                sa.normalize_config(c, ".")


def _records():
    tmp = tempfile.mkdtemp()
    try:
        demo_data.make_demo_db(os.path.join(tmp, "demo_V3.db"))
        return sa.gather_snapshot([tmp], 5, 20000)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class ServiceWiring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.recs = sorted(_records(), key=lambda r: -r["num_points"])     # 1400 点的那条有 50 / 150 / 260 s 三个峰
        cfg = sa.load_config(None)
        cfg.update(_base_dir=self.tmp, peak_min_height=2000, peak_series="raw")
        sa.normalize_config(cfg, self.tmp)
        key = sa._channel_key(self.recs[0])
        cfg["modbus_server_map"] = ms.auto_map([(key, "demo")], peaks=3)
        self.cfg, self.key = cfg, key
        self.app = sa.OpcUaApp(cfg)

    def tearDown(self):
        if self.app.modbus is not None:
            self.app.modbus.stop()
        if self.app.history is not None:
            self.app.history.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_peaks_and_modbus_registers(self):
        pk = self.app.compute_peaks(self.recs[0])
        self.assertEqual([round(p["rt"]) for p in pk], [50, 150, 260])
        self.assertIs(self.app.compute_peaks(self.recs[0]), pk)              # 同一周期用缓存
        self.app.modbus = ms.ModbusForwarder("127.0.0.1", 0).start()
        self.app.latest_records = self.recs
        self.app.update_modbus({"active": [{"key": "stale:" + self.key, "level": "警告", "text": "x"}]})
        m = {(it["channel"], it["field"]): it for it in self.cfg["modbus_server_map"]}

        def read(ch, field):
            it = m[(ch, field)]
            n = ms.TYPES[it["type"]]
            regs = ms.client_read("127.0.0.1", self.app.modbus.port, it["address"], n)
            return ms.decode_value(regs, it["type"], "ABCD")
        self.assertEqual(read(self.key, "cycle_id"), self.recs[0]["cycle_id"])
        self.assertEqual(read(self.key, "peak_count"), 3)
        self.assertAlmostEqual(read(self.key, "peak_rt_2"), 150, delta=0.1)
        self.assertEqual(read(self.key, "alerts"), 1)
        self.assertEqual(read("*", "alert_severity"), 1)
        self.assertEqual(read("*", "service_heartbeat"), 1)
        self.assertEqual(ms.client_read("127.0.0.1", self.app.modbus.port, 0, 1, func=6), ("exception", 1))

    def test_after_alarms_writes_history(self):
        self.app.history = history.HistoryStore(os.path.join(self.tmp, "h.db"))
        self.app.history.service_started()
        self.app.after_alarms({"active": [{"key": "scan_error", "level": "严重", "text": "扫描出错"}]})
        self.app.after_alarms({"active": []})
        kinds = [e["kind"] for e in self.app.history.events()]
        self.assertEqual(kinds, ["service_start", "alert_on", "alert_off"])
        self.assertIsNone(self.app.client_count())          # 没有 OPC UA 服务器：不记成 0

    def test_watcher_calls_history_cb(self):
        data = tempfile.mkdtemp(dir=self.tmp)
        demo_data.make_demo_db(os.path.join(data, "demo_V3.db"))
        self.cfg["watch_dirs"] = [data]
        got, stop = [], threading.Event()

        def hist(recs):
            got.append(len(recs))
            stop.set()
        t = threading.Thread(target=sa.watcher_thread, args=(lambda r: None, stop, self.cfg),
                             kwargs={"history_cb": hist}, daemon=True)
        t.start()
        t.join(30)
        self.assertEqual(got, [len(self.recs)])


class BackupCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "app")
        os.makedirs(os.path.join(self.base, "certs"))
        self.cfgp = os.path.join(self.base, "config.json")
        ct.write_atomic(self.cfgp, {"poll_seconds": 40})
        with open(os.path.join(self.base, "certs", "server_key.pem"), "wb") as f:
            f.write(b"-----BEGIN PRIVATE KEY-----\nX\n-----END PRIVATE KEY-----\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cli_round_trip(self):
        out = os.path.join(self.tmp, "b.zip")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(sa.run_backup_cli(out, self.base, self.cfgp), 0)
        self.assertIn("私钥", buf.getvalue())                 # 未加密含私钥：警告
        out2 = os.path.join(self.tmp, "b2.zip")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sa.run_backup_cli(out2, self.base, self.cfgp, encrypt=True, password="Secret#123"), 0)
        ct.write_atomic(self.cfgp, {"poll_seconds": 77})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(sa.run_restore_cli(out2, self.base, self.cfgp, yes=True, password="Secret#123"), 0)
        self.assertEqual(ct.read_raw(self.cfgp)["poll_seconds"], 40)
        self.assertIn("重启服务", buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sa.run_restore_cli(out2, self.base, self.cfgp, yes=True, password="wrong-pass"), 2)

    def test_help_and_console_only(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            sa.main(["--help"])
        self.assertIn("--backup", buf.getvalue())
        self.assertIn("--restore", buf.getvalue())
        self.assertIn("--restore", sa.CONSOLE_ONLY_ARGS)


if __name__ == "__main__":
    unittest.main()
