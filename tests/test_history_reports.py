# -*- coding: utf-8 -*-
"""v3.1 历史数据（history.py）与运行报表（reports.py）测试：
周期去重写入 / 压缩曲线往返、按日期与通道查询、保留天数与容量清理、事件记录（启停、告警出现/恢复、连接数）、
叠加对比坐标、CSV 导出、班次计算、日报 / 班报统计、xlsx（标准库生成，openpyxl 可读时校验内容）/ html / csv、自动生成判定。"""
import csv
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import history   # noqa: E402
import reports   # noqa: E402


def _rec(cycle_id, ct, stream=1, n=50, k=1.0):
    t = [i * 0.5 for i in range(n)]
    return dict(source="gc1_V3.db", analyzer="SN1", app=1, app_name="A", stream=stream, stream_name="S",
                det=1, det_name="TCD", cycle_id=cycle_id, cycle_time=ct, is_validation=0, cal_level=0,
                num_points=n, sample_period=0.5, duration=t[-1], min_raw=0, max_raw=n, y_title="mV", x_title="s",
                time=t, raw=[float(i) for i in range(n)], uv=[i * 0.1 for i in range(n)],
                value=[k * i * 0.01 for i in range(n)])


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "history", "history.db")
        self.st = history.HistoryStore(self.db, retention_days=30, max_mb=100)

    def tearDown(self):
        self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class Store(_Base):
    def test_record_dedupe_and_roundtrip(self):
        n = self.st.record_records([_rec(1, "2026-11-26 08:00:00"), _rec(1, "2026-11-26 08:00:00", stream=2)])
        self.assertEqual(n, 2)
        self.assertEqual(self.st.record_records([_rec(1, "2026-11-26 08:00:00")]), 0)     # 同一周期不重复
        self.assertEqual(self.st.record_records([_rec(2, "2026-11-26T08:10:00")]), 1)
        rows = self.st.query_cycles()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["cycle_time"], "2026-11-26 08:10:00")
        rec = self.st.load_cycle(rows[0]["id"])
        self.assertEqual(rec["value"], _rec(2, "")["value"])
        self.assertEqual(rec["det_name"], "TCD")
        self.assertEqual(len(rec["time"]), 50)

    def test_curve_is_compressed(self):
        r = _rec(1, "2026-11-26 08:00:00", n=5000)
        r["raw"] = [100.0] * 5000
        blob = history.pack_curve(r)
        self.assertLess(len(blob), 5000 * 8 * 4 / 2)
        self.assertEqual(history.unpack_curve(blob)["raw"], [100.0] * 5000)

    def test_skip_test_station(self):
        r = _rec(1, "2026-11-26 08:00:00")
        r["analyzer"] = "TEST"
        self.assertEqual(self.st.record_records([r]), 0)

    def test_query_filters(self):
        for i in range(5):
            self.st.record_records([_rec(i, "2026-11-2%d 08:00:00" % (i + 1), stream=1 + i % 2)])
        rows = self.st.query_cycles("2026-11-22 00:00:00", "2026-11-24 23:59:59")
        self.assertEqual([r["cycle_id"] for r in rows], [3, 2, 1])
        ch = self.st.channels()
        self.assertEqual(len(ch), 2)
        rows = self.st.query_cycles(channels=[ch[0]["channel"]])
        self.assertTrue(all(r["channel"] == ch[0]["channel"] for r in rows))

    def test_retention_cleanup(self):
        now = datetime(2026, 11, 26, 12, 0, 0)
        self.st.record_records([_rec(1, "2026-09-01 08:00:00"), _rec(2, "2026-11-25 08:00:00")], now=now)
        self.st.add_event("clients", value=1, now=datetime(2026, 9, 1))
        removed = self.st.cleanup(now)
        self.assertEqual(removed, 1)
        self.assertEqual([r["cycle_id"] for r in self.st.query_cycles()], [2])
        self.assertEqual(self.st.events(), [])

    def test_capacity_cleanup(self):
        self.st.max_mb = 0
        for i in range(10):
            self.st.record_records([_rec(i, "2026-11-26 08:%02d:00" % i, n=2000)])
        self.st.cleanup(datetime(2026, 11, 26, 9, 0, 0))
        self.assertEqual(self.st.query_cycles(), [])

    def test_events(self):
        t0 = datetime(2026, 11, 26, 8, 0, 0)
        self.st.service_started(now=t0)
        self.st.track_alerts({"active": [{"key": "stale:k", "level": "警告", "text": "旧"}]}, now=t0 + timedelta(minutes=5))
        self.st.track_alerts({"active": [{"key": "stale:k", "level": "警告", "text": "旧"}]}, now=t0 + timedelta(minutes=6))
        self.st.track_alerts({"active": []}, now=t0 + timedelta(minutes=20))
        self.st.track_clients(2, now=t0)
        self.st.track_clients(2, now=t0)
        self.st.track_clients(3, now=t0)
        self.st.service_stopped(now=t0 + timedelta(hours=2))
        kinds = [e["kind"] for e in self.st.events()]
        self.assertEqual(kinds.count("alert_on"), 1)
        self.assertEqual(kinds.count("alert_off"), 1)
        self.assertEqual(kinds.count("clients"), 2)
        self.assertEqual(kinds[0], "service_start")
        self.assertEqual(kinds[-1], "service_stop")
        self.assertEqual(self.st.uptime_spans("2026-11-26 00:00:00", "2026-11-27 00:00:00"),
                         [("2026-11-26 08:00:00", "2026-11-26 10:00:00")])

    def test_export_and_overlay(self):
        self.st.record_records([_rec(1, "2026-11-26 08:00:00"), _rec(2, "2026-11-26 08:10:00", k=2)])
        ids = [r["id"] for r in self.st.query_cycles()]
        p = os.path.join(self.tmp, "c.csv")
        self.assertEqual(history.export_cycles_csv(self.st, ids, p), 2)
        with open(p, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 1 + 100)
        recs = [self.st.load_cycle(i) for i in ids]
        lay = history.overlay_layout(recs, "value", 900, 400)
        self.assertTrue(lay["ok"])
        self.assertEqual(len(lay["lines"]), 2)
        self.assertNotEqual(lay["lines"][0][0], lay["lines"][1][0])           # 不同颜色
        self.assertIn("#2", lay["lines"][0][2] + lay["lines"][1][2])          # 图例含周期号
        self.assertFalse(history.overlay_layout([], "value", 900, 400)["ok"])
        self.assertFalse(history.overlay_layout(recs, "value", 50, 40)["ok"])

    def test_readonly_missing(self):
        with self.assertRaises(history.HistoryError):
            history.HistoryStore(os.path.join(self.tmp, "none.db"), readonly=True)

    def test_open_from_config(self):
        self.assertIsNone(history.open_from_config({"history_enabled": False}))
        st = history.open_from_config({"history_enabled": True, "_base_dir": self.tmp, "history_file": "h2.db"})
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "h2.db")))
        st.close()


class Shifts(unittest.TestCase):
    def test_normalize(self):
        s = reports.normalize_shifts([{"name": "夜班", "start": "20:00"}, {"name": "白班", "start": "8:00"}])
        self.assertEqual([x["start"] for x in s], ["08:00", "20:00"])
        self.assertEqual(reports.normalize_shifts(None), reports.DEFAULT_SHIFTS)
        for bad in ([{"name": "", "start": "08:00"}], [{"name": "a", "start": "25:00"}],
                    [{"name": "a", "start": "08:00"}, {"name": "a", "start": "09:00"}], "x"):
            with self.assertRaises(reports.ReportError):
                reports.normalize_shifts(bad)
        self.assertEqual(reports.parse_shifts_text("白班=08:00，夜班=20:00")[1]["name"], "夜班")
        with self.assertRaises(reports.ReportError):
            reports.normalize_mode("weekly")

    def test_periods(self):
        day = datetime(2026, 11, 26).date()
        ps = reports.shift_periods(day, reports.DEFAULT_SHIFTS)
        self.assertEqual((ps[0]["start"].hour, ps[0]["end"].hour), (8, 20))
        self.assertEqual(ps[1]["end"], datetime(2026, 11, 27, 8, 0))
        p = reports.period_for(datetime(2026, 11, 27, 3, 0), "shift", reports.DEFAULT_SHIFTS)
        self.assertEqual(p["name"], "2026-11-26 夜班")
        done = reports.completed_periods(datetime(2026, 11, 27, 9, 0), "shift", reports.DEFAULT_SHIFTS, 0)
        self.assertEqual([x["name"] for x in done], [])
        done = reports.completed_periods(datetime(2026, 11, 27, 9, 0), "shift", reports.DEFAULT_SHIFTS, 1)
        self.assertEqual([x["name"] for x in done], ["2026-11-26 白班", "2026-11-26 夜班"])


class Report(_Base):
    def _fill(self):
        t0 = datetime(2026, 11, 26, 8, 0, 0)
        self.st.service_started(now=t0)
        for i in range(6):
            self.st.record_records([_rec(i, (t0 + timedelta(minutes=30 * i)).strftime(history.TS))], now=t0)
        key = history.channel_key(_rec(0, ""))
        self.st.track_alerts({"active": [{"key": "stale:" + key, "level": "警告", "text": "无新周期"}]},
                             now=t0 + timedelta(hours=1))
        self.st.track_alerts({"active": []}, now=t0 + timedelta(hours=1, minutes=30))
        self.st.track_alerts({"active": [{"key": "scan_error", "level": "严重", "text": "扫描出错"}]},
                             now=t0 + timedelta(hours=3))
        self.st.track_clients(1, now=t0)
        self.st.track_clients(4, now=t0 + timedelta(hours=2))
        self.st.track_clients(2, now=t0 + timedelta(hours=3))
        self.st.alive(now=t0 + timedelta(hours=4), force=True)
        return t0, key

    def test_compute(self):
        t0, key = self._fill()
        rep = reports.compute_report(self.st, datetime(2026, 11, 26), datetime(2026, 11, 27),
                                     now=t0 + timedelta(hours=4))
        self.assertAlmostEqual(rep["uptime_seconds"], 4 * 3600, delta=1)
        self.assertAlmostEqual(rep["uptime_pct"], 4 / 24 * 100, places=3)
        self.assertEqual(rep["service_starts"], 1)
        self.assertEqual(rep["clients_peak"], 4)
        self.assertEqual(rep["alert_count"], 2)
        self.assertAlmostEqual(rep["alert_seconds"], 1800 + 3600, delta=1)     # 未恢复的算到“现在”
        self.assertEqual(rep["cycles_total"], 6)
        ch = rep["channels"][0]
        self.assertEqual((ch["cycles"], ch["alerts"], ch["status"]), (6, 1, "有告警"))
        self.assertTrue(any(a["ongoing"] for a in rep["alerts"]))

    def test_shift_window(self):
        t0, key = self._fill()
        rep = reports.compute_report(self.st, datetime(2026, 11, 26, 9, 15), datetime(2026, 11, 26, 9, 45),
                                     now=t0 + timedelta(hours=4))
        self.assertAlmostEqual(rep["alert_seconds"], 900, delta=1)    # 09:00～09:30 的告警只算落在时段内的 15 分钟
        self.assertEqual(rep["alert_count"], 0)                 # 告警在本时段之前出现
        self.assertEqual(rep["cycles_total"], 1)

    def test_files(self):
        t0, _key = self._fill()
        out = os.path.join(self.tmp, "rep")
        files, rep = reports.generate(self.st, reports.day_period(t0.date()), out, ("xlsx", "html", "csv"),
                                      now=t0 + timedelta(hours=4))
        self.assertEqual([os.path.splitext(f)[1] for f in files], [".xlsx", ".html", ".csv"])
        with zipfile.ZipFile(files[0]) as z:
            self.assertIn("xl/worksheets/sheet3.xml", z.namelist())
            self.assertIn("无新周期", z.read("xl/worksheets/sheet3.xml").decode("utf-8"))
        try:
            import openpyxl
        except ImportError:
            openpyxl = None
        if openpyxl is not None:
            wb = openpyxl.load_workbook(files[0])
            self.assertEqual(wb.sheetnames, ["概要", "通道", "告警"])
            vals = {r[0].value: r[1].value for r in wb["概要"].iter_rows(min_row=2)}
            self.assertEqual(vals["客户端连接数峰值"], 4)
        with open(files[1], encoding="utf-8") as f:
            self.assertIn("<table>", f.read())

    def test_xlsx_escaping(self):
        p = os.path.join(self.tmp, "x.xlsx")
        reports.write_xlsx(p, [("a/b:c", ("h",), [("<&>\x01",), (1.5,)])])
        with zipfile.ZipFile(p) as z:
            xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn("&lt;&amp;&gt;", xml)
            self.assertNotIn("\x01", xml)
            self.assertIn("a_b_c", z.read("xl/workbook.xml").decode("utf-8"))

    def test_auto_reporter(self):
        out = os.path.join(self.tmp, "auto")
        ar = reports.AutoReporter("daily", None, out)
        due = ar.due(datetime(2026, 11, 27, 0, 5))
        self.assertEqual([p["name"] for p in due], ["2026-11-26"])
        reports.generate(self.st, due[-1], out, ("xlsx",))
        self.assertEqual(ar.due(datetime(2026, 11, 27, 0, 6)), [])
        self.assertEqual(reports.AutoReporter("off", None, out).due(), [])


if __name__ == "__main__":
    unittest.main()
