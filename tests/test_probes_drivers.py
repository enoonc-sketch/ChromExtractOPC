# -*- coding: utf-8 -*-
"""设备检测接入告警、串口锁、驱动与软件环境检测（用模拟数据，不需要真实硬件和 Windows）。"""
import os, shutil, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import alerts        # noqa: E402
import drivercheck   # noqa: E402
import nettest       # noqa: E402
import probes        # noqa: E402
import envwatch      # noqa: E402


class Probes(unittest.TestCase):
    def setUp(self):
        self.P = probes.normalize_probes([
            {"label": "DCS", "kind": "ping", "params": {"host": "192.168.1.10"}, "interval": 30,
             "fail_threshold": 3, "level": "严重"},
            {"label": "GC-1", "kind": "modbus_rtu", "params": {"port": "COM3", "unit": "1"}, "interval": 60}])
        self.t = [0.0]
        self.cfg = {"poll_seconds": 30, "alert_stale_minutes": 0, "alert_scan_errors": 0, "alert_probes": self.P,
                    "alert_webhook_level": "all", "alert_email_level": "critical", "alert_quiet": []}
        self.h, self.m = [], []
        rec = lambda box: (lambda r, c: box.append(([a.key for a in r], [a.key for a in c])))
        n = alerts.make_notifier(self.cfg, hook=rec(self.h), mail=rec(self.m), clock=lambda: 1e9)
        self.mon = alerts.AlertMonitor(self.cfg, clock=lambda: 1e9, notifier=n)
        self.mon.last_scan = self.mon.last_ok_scan = 1e9
        self.mon.channel_count = 1
        self.res = {"ping": False, "modbus_rtu": True}
        self.pm = probes.ProbeMonitor(self.cfg, self.mon, clock=lambda: self.t[0], first_delay=0, stagger=0,
                                      runner=lambda k, p: {"ok": self.res[k], "summary": "x", "latency_ms": 3})
        self.key = "probe:" + self.P[0]["id"]

    def tick(self, n=1):
        for _ in range(n):
            self.pm.run_due(sync=True)
            self.t[0] += 30

    def test_threshold_alert_and_recover(self):
        self.tick(2)
        self.assertNotIn(self.key, self.mon.active)          # 2 次失败还不告警
        self.tick()
        self.assertEqual(self.mon.active[self.key].level, "严重")
        self.assertEqual(self.m[-1], ([self.key], []))       # 严重 -> 邮件也收到
        self.res["ping"] = True
        self.tick()
        self.assertNotIn(self.key, self.mon.active)
        self.assertEqual(self.h[-1], ([], [self.key]))

    def test_disable_removes_without_recovery(self):
        self.tick(3)
        sent = len(self.h)
        self.cfg["alert_probes"] = probes.normalize_probes([dict(self.P[0], enabled=False), self.P[1]])
        self.tick()
        self.assertNotIn(self.key, self.mon.active)
        self.assertEqual(len(self.h), sent)

    def test_uptime(self):
        self.tick(3)
        self.res["ping"] = True
        self.tick()
        row = [x for x in self.pm.status() if x["label"] == "DCS"][0]
        self.assertEqual((row["checks"], row["failed"]), (4, 3))
        self.assertEqual(row["history"], [0, 0, 0, 1])

    def test_busy_port_not_counted(self):
        pm = probes.ProbeMonitor(self.cfg, self.mon, clock=lambda: self.t[0], first_delay=0, stagger=0,
                                 runner=lambda k, p: {"ok": False, "busy": True, "summary": "占用"})
        for _ in range(5):
            pm.run_due(sync=True)
            self.t[0] += 100
        self.assertTrue(all(x["fails"] == 0 and x["checks"] == 0 for x in pm.status()))

    def test_validation(self):
        for bad in ([{"label": "", "kind": "ping", "params": {"host": "x"}}], [{"label": "a", "kind": "telnet"}],
                    [{"label": "a", "kind": "ping", "params": {"host": "-f x"}}],
                    [{"label": "a", "kind": "ping", "params": {"host": "x"}, "interval": 5}],
                    [{"label": "a", "kind": "ping", "params": {"host": "x"}}] * 2,
                    [{"label": "a", "kind": "opcua", "params": {"endpoint": "http://x"}}]):
            with self.assertRaises(ValueError):
                probes.normalize_probes(bad)

    def test_password_never_stored_plain(self):
        p = probes.normalize_probes([{"label": "a", "kind": "opcua",
                                      "params": {"endpoint": "opc.tcp://x:4840", "password": "secret"}}])
        self.assertNotIn("password", p[0]["params"])


class PortLock(unittest.TestCase):
    def test_busy_port(self):
        lk = nettest.port_lock("com7")
        lk.acquire()
        try:
            r = nettest.run_test("modbus_rtu", {"port": "COM7", "_wait": 0.1})
            self.assertTrue(r.get("busy"))
            self.assertFalse(r["ok"])
        finally:
            lk.release()


FAKE = [
    {"name": "USB-SERIAL CH340 (COM3)", "cls": "Ports", "id": "USB\\VID_1A86&PID_7523\\5&1", "err": 0, "present": True,
     "dprov": "wch.cn", "dver": "3.8.2023.2", "ddate": "2023-02-01", "signed": True},
    {"name": "Prolific USB-to-Serial Comm Port (COM5)", "cls": "Ports", "id": "USB\\VID_067B&PID_2303\\6&2", "err": 10,
     "present": True},
    {"name": "USB Serial Port (COM4)", "cls": "Ports", "id": "FTDIBUS\\VID_0403+PID_6001+A1\\0000", "err": 0,
     "present": True, "dprov": "FTDI", "dver": "2.12.36.4", "signed": True},
    {"name": "USB2.0-Serial", "cls": None, "id": "USB\\VID_1A86&PID_7523\\7&3", "err": 28, "present": True},
    {"name": "CP2102 USB to UART", "cls": "Ports", "id": "USB\\VID_10C4&PID_EA60\\0001", "err": 45, "present": False},
    {"name": "Intel Ethernet", "cls": "Net", "id": "PCI\\VEN_8086", "err": 0, "present": True},
]


class Drivers(unittest.TestCase):
    def test_classify(self):
        items = {i["name"]: i for i in drivercheck.check_serial_drivers(runner=lambda s: FAKE)}
        self.assertEqual(items["串口：USB-SERIAL CH340 (COM3)"]["level"], "正常")
        self.assertIn("FTDI", items["串口：USB Serial Port (COM4)"]["text"])
        self.assertIn("假芯片", items["设备：Prolific USB-to-Serial Comm Port (COM5)"]["fix"])
        self.assertIn("CH341SER", items["设备：USB2.0-Serial"]["fix"])
        self.assertFalse(any("CP2102" in k for k in items))          # 已拔出的设备不报
        self.assertFalse(any("Ethernet" in k for k in items))        # 非串口设备不报

    def test_empty_and_errors(self):
        self.assertEqual(drivercheck.check_serial_drivers(runner=lambda s: [])[0]["level"], "注意")

        def boom(s):
            raise ValueError("bad")
        self.assertEqual(drivercheck.check_serial_drivers(runner=boom)[0]["level"], "注意")

    def test_data_dirs(self):
        tmp = tempfile.mkdtemp()
        try:
            open(os.path.join(tmp, "a_V3.db"), "w").close()
            lv = [i["level"] for i in drivercheck.check_data_dirs({"watch_dirs": [tmp, os.path.join(tmp, "no")]})]
            self.assertEqual(lv, ["正常", "问题"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_all_never_raises(self):
        r = drivercheck.run_all({"watch_dirs": []}, runner=lambda s: FAKE)
        self.assertIn("summary", r)
        self.assertFalse(r["ok"])


class EnvWatchTest(unittest.TestCase):
    """启动时环境检测：问题 -> 告警（只通知一次），修好 -> 恢复；注意只记日志一次；数据目录不重复告警；出错不崩。"""

    def setUp(self):
        self.h = []
        cfg = {"poll_seconds": 30, "alert_stale_minutes": 0, "alert_scan_errors": 0,
               "alert_webhook_level": "all", "alert_email_level": "critical", "alert_quiet": []}
        n = alerts.make_notifier(cfg, hook=lambda r, c: self.h.append((len(r), len(c))), mail=lambda r, c: None,
                                 clock=lambda: 1e9)
        self.mon = alerts.AlertMonitor(cfg, clock=lambda: 1e9, notifier=n)
        self.mon.last_scan = self.mon.last_ok_scan = 1e9
        self.mon.channel_count = 1
        self.cfg = cfg
        self.res = [{"summary": "x", "elapsed_ms": 1, "items": [
            {"name": "设备：USB2.0-Serial", "level": "问题", "text": "没有安装驱动程序。", "fix": "CH341SER"},
            {"name": "数据目录", "level": "问题", "text": "找不到", "fix": ""},
            {"name": "开机自启任务", "level": "注意", "text": "没有安装", "fix": ""}]}]

    def test_problem_alert_then_recover(self):
        e = envwatch.EnvWatch(self.cfg, self.mon, check=lambda c: self.res[0])
        e.run_once()
        keys = [k for k in self.mon.active if k.startswith("env:")]
        self.assertEqual(len(keys), 1)                        # 数据目录不重复告警，"注意"不告警
        e.run_once()
        self.assertEqual(self.h, [(1, 0)])                    # 复查不重复通知
        self.res[0] = {"summary": "ok", "elapsed_ms": 1, "items": []}
        e.run_once()
        self.assertFalse([k for k in self.mon.active if k.startswith("env:")])
        self.assertEqual(self.h[-1], (0, 1))

    def test_check_error_safe(self):
        def boom(c):
            raise RuntimeError("WMI")
        r = envwatch.EnvWatch(self.cfg, self.mon, check=boom).run_once()
        self.assertIn("出错", r["summary"])


if __name__ == "__main__":
    unittest.main()
