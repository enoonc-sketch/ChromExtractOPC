# -*- coding: utf-8 -*-
"""稳定性测试：统计正确（丢包率、百分位、最长连续失败、抖动）、错误归类、CSV 明细、参数校验。用模拟时钟，瞬间跑完。"""
import os, shutil, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import stability as st   # noqa: E402


class Sim:
    def __init__(self, fail_at=(), crc_at=(), spike_at=()):
        self.t, self.i = 1_000_000.0, 0
        self.fail_at, self.crc_at, self.spike_at = set(fail_at), set(crc_at), set(spike_at)

    def runner(self, kind, params):
        i, self.i = self.i, self.i + 1
        self.t += 0.02
        if i in self.fail_at:
            return {"ok": False, "summary": "站号 1 在 1.0 秒内没有应答"}
        if i in self.crc_at:
            return {"ok": False, "summary": "CRC 校验错误：线路干扰，收到：01 03 0%d" % i}
        return {"ok": True, "summary": "ok", "latency_ms": 20 + i % 10 + (500 if i in self.spike_at else 0)}

    def sleep(self, w):
        self.t += w

    def make(self, kind="modbus_rtu", interval=1, minutes=10, out=None):
        return st.StabilityRun(kind, {"port": "COM3", "unit": 1, "host": "10.0.0.1"}, interval, minutes,
                               out_dir=out, runner=self.runner, clock=lambda: self.t, sleeper=self.sleep)


class Stats(unittest.TestCase):
    def test_all_ok(self):
        sim = Sim()
        r = sim.make()
        r.run_blocking()
        s = r.stats()
        self.assertEqual((s["n"], s["fail"], s["state"]), (600, 0, "finished"))
        self.assertEqual(s["loss_pct"], 0.0)
        self.assertTrue(s["verdict"].startswith("稳定"))
        self.assertEqual((s["min"], s["max"]), (20.0, 29.0))

    def test_streak_and_errors(self):
        sim = Sim(fail_at=range(100, 107), crc_at=(300, 450))
        r = sim.make()
        r.run_blocking()
        s = r.stats()
        self.assertEqual(s["fail"], 9)
        self.assertEqual(s["max_streak"], 7)
        self.assertEqual(s["loss_pct"], 1.5)
        self.assertIn("连续 7 次", s["verdict"])
        self.assertEqual(s["errors"][0]["count"], 7)
        self.assertEqual(s["errors"][1]["count"], 2)            # 不同字节的 CRC 错误归成一类
        self.assertTrue(any("终端电阻" in h for h in s["hints"]))

    def test_spike_detected(self):
        sim = Sim(spike_at=range(0, 600, 50))
        r = sim.make()
        r.run_blocking()
        self.assertIn("尖峰", r.stats()["verdict"])

    def test_ping_forces_single_packet(self):
        self.assertEqual(st.StabilityRun("ping", {"host": "x", "count": 4}, 1, 1).params["count"], 1)

    def test_stop_early(self):
        sim = Sim()
        r = sim.make(minutes=60)
        orig = sim.runner

        def runner(k, p):
            if sim.i == 50:
                r.stop()
            return orig(k, p)
        r.runner = runner
        r.run_blocking()
        self.assertEqual(r.stats()["state"], "stopped")
        self.assertLess(r.stats()["n"], 60)


class Csv(unittest.TestCase):
    def test_csv_written_and_rotated(self):
        out = tempfile.mkdtemp()
        try:
            for i in range(st.KEEP_FILES + 3):
                sim = Sim(fail_at=(5,))
                sim.t += i * 10
                r = sim.make(minutes=1, out=out)
                r.run_blocking()
            files = [f for f in os.listdir(out) if f.endswith(".csv")]
            self.assertLessEqual(len(files), st.KEEP_FILES)
            text = open(os.path.join(out, r.stats()["csv"]), encoding="utf-8-sig").read()
            self.assertIn("失败", text)
            self.assertIn("#,结论", text)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_formula_injection_escaped(self):
        self.assertEqual(st._cell("=cmd|' /C calc'!A0"), "'=cmd|' /C calc'!A0")


class Validation(unittest.TestCase):
    def test_bad_args(self):
        for args in (("telnet", {}, 1, 1), ("ping", {}, 0.1, 1), ("tcp", {}, 1, 0), ("tcp", {}, 1, 5000),
                     ("tcp", {}, 120, 1), ("tcp", {}, "x", 1)):
            with self.assertRaises(st.StabilityError):
                st.StabilityRun(*args)


class Multi(unittest.TestCase):
    """多目标：关联分析（单个 / 同时 / 零散失败）、同一串口互斥轮询、参数一致性校验、汇总 CSV。"""
    T = [{"label": "GC-1", "kind": "modbus_rtu", "params": {"port": "COM3", "unit": 1}},
         {"label": "GC-2", "kind": "modbus_rtu", "params": {"port": "com3", "unit": 2}},
         {"label": "DCS", "kind": "tcp", "params": {"host": "10.0.0.5", "port": 4840}}]

    @staticmethod
    def factory(fails):
        def f(i, kind, params, wrap):
            t, k = [1_000_000.0], [0]

            def runner(kd, p):
                j = k[0]
                k[0] += 1
                t[0] += 0.02
                if j in fails.get(i, ()):
                    return {"ok": False, "summary": "站号 %s 在 1.0 秒内没有应答" % p.get("unit", 1)}
                return {"ok": True, "summary": "ok", "latency_ms": 20 + j % 5}
            return st.StabilityRun(kind, params, 1, 10, runner=wrap(runner), clock=lambda: t[0],
                                   sleeper=lambda w: t.__setitem__(0, t[0] + w), tag="_t%d" % i)
        return f

    def _run(self, fails):
        m = st.MultiRun(self.T, 1, 10, bus_gap=0, run_factory=self.factory(fails))
        m.run_blocking()
        return m.stats()

    def test_all_ok(self):
        s = self._run({})
        self.assertIn("全部 3 个目标稳定", s["verdict"])
        self.assertTrue(any("COM3" in n for n in s["notes"]))        # 提示同一串口在轮询

    def test_single_device(self):
        s = self._run({2: range(200, 230)})
        self.assertIn("只有「GC-2」", s["verdict"])
        self.assertEqual(s["worst"], "GC-2")

    def test_shared_failure(self):
        s = self._run({1: range(200, 215), 2: range(201, 214)})
        self.assertIn("相同时段", s["verdict"])
        self.assertTrue(any("共用" in n for n in s["notes"]))

    def test_scattered(self):
        self.assertIn("不重合", self._run({1: range(50, 55), 2: range(400, 405)})["verdict"])

    def test_same_port_never_overlaps(self):
        import threading
        import time as _t
        active, overlap, lk = [0], [0], threading.Lock()

        def r(kind, p):
            with lk:
                active[0] += 1
                if active[0] > 1:
                    overlap[0] += 1
            _t.sleep(0.01)
            with lk:
                active[0] -= 1
            return {"ok": True, "summary": "ok", "latency_ms": 10}
        out = tempfile.mkdtemp()
        try:
            m = st.MultiRun([{"label": "A", "kind": "modbus_rtu", "params": {"port": "COM3", "unit": 1}},
                             {"label": "B", "kind": "modbus_rtu", "params": {"port": "COM3", "unit": 2}}],
                            interval=0.2, duration_min=1, out_dir=out, runner=r, bus_gap=0)
            for x in m.runs:
                x.duration = 1.5
            m.duration = 1.5
            m.run_blocking()
            self.assertEqual(overlap[0], 0)
            self.assertEqual(m.stats()["state"], "finished")
            self.assertTrue(m.stats()["summary_csv"])
            self.assertIn("总体结论", open(os.path.join(out, m.stats()["summary_csv"]), encoding="utf-8-sig").read())
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_validation(self):
        bad_lists = ([], [{"kind": "modbus_rtu", "params": {"port": "COM3", "baud": 9600}},
                          {"kind": "modbus_rtu", "params": {"port": "COM3", "baud": 19200}}],
                     [{"kind": "telnet"}], [{"kind": "ping", "params": {"host": "x"}}] * 17, "x")
        for bad in bad_lists:
            with self.assertRaises(st.StabilityError):
                st.MultiRun(bad, 1, 10)


if __name__ == "__main__":
    unittest.main()
