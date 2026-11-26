# -*- coding: utf-8 -*-
"""v3.1 峰识别（peaks.py）测试：用合成高斯峰校验保留时间 / 峰高 / 面积 / 半峰宽精度，
基线漂移、噪声、积分区间、最小峰高 / 峰宽、参数校验、CSV 导出、OPC UA 数组。"""
import csv
import math
import os
import random
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import peaks  # noqa: E402

XS = [i * 0.1 for i in range(3000)]          # 0～300 秒，10 Hz
SQ2PI = math.sqrt(2 * math.pi)


def _signal(spec, drift=0.0, offset=0.0, noise=0.0, seed=1):
    ys = [offset + drift * x for x in XS]
    for rt, h, s in spec:
        ys = [a + b for a, b in zip(ys, peaks.gaussian(XS, rt, h, s))]
    if noise:
        rnd = random.Random(seed)
        ys = [y + rnd.gauss(0, noise) for y in ys]
    return ys


SPEC = [(50, 100, 2), (120, 40, 3), (200, 10, 1.5)]


class Accuracy(unittest.TestCase):
    def _check(self, res, tol_area, tol_rt=0.05, tol_h=0.02, tol_w=0.03):
        pk = res["peaks"]
        self.assertEqual(len(pk), 3)
        for p, (rt, h, s) in zip(pk, SPEC):
            self.assertAlmostEqual(p["rt"], rt, delta=tol_rt)
            self.assertAlmostEqual(p["height"], h, delta=h * tol_h)
            self.assertAlmostEqual(p["area"], h * s * SQ2PI, delta=h * s * SQ2PI * tol_area)
            self.assertAlmostEqual(p["fwhm"], 2.3548 * s, delta=2.3548 * s * tol_w)
        self.assertAlmostEqual(sum(p["area_pct"] for p in pk), 100.0, places=6)

    def test_clean_gaussians(self):
        self._check(peaks.find_peaks(XS, _signal(SPEC)), tol_area=0.01)

    def test_baseline_drift_and_offset(self):
        self._check(peaks.find_peaks(XS, _signal(SPEC, drift=0.02, offset=5)), tol_area=0.01)

    def test_noise(self):
        for seed in range(1, 8):            # 多组随机噪声：不拆峰、不丢峰
            res = peaks.find_peaks(XS, _signal(SPEC, drift=0.02, offset=5, noise=0.2, seed=seed))
            self.assertAlmostEqual(res["noise"], 0.2, delta=0.06)
            self._check(res, tol_area=0.06, tol_rt=0.1, tol_h=0.04, tol_w=0.08)

    def test_narrow_peaks_keep_height(self):
        xs = [i * 0.25 for i in range(1600)]           # 采样 0.25 s，σ=0.4 s 的窄峰：平滑不应压低峰高
        ys = [a + b for a, b in zip(peaks.gaussian(xs, 50, 8000, 0.4), peaks.gaussian(xs, 150, 12000, 0.6))]
        pk = peaks.find_peaks(xs, ys)["peaks"]
        self.assertEqual(len(pk), 2)
        self.assertAlmostEqual(pk[0]["height"], 8000, delta=8000 * 0.03)
        self.assertAlmostEqual(pk[1]["area"], 12000 * 0.6 * SQ2PI, delta=12000 * 0.6 * SQ2PI * 0.02)

    def test_area_percent(self):
        pk = peaks.find_peaks(XS, _signal(SPEC))["peaks"]
        total = sum(h * s for _r, h, s in SPEC)
        for p, (_rt, h, s) in zip(pk, SPEC):
            self.assertAlmostEqual(p["area_pct"], h * s / total * 100, delta=0.3)

    def test_overlapping_peaks_split_at_valley(self):
        ys = _signal([(100, 50, 2), (108, 30, 2)])
        pk = peaks.find_peaks(XS, ys)["peaks"]
        self.assertEqual(len(pk), 2)
        self.assertAlmostEqual(pk[0]["end"], pk[1]["start"], delta=0.11)
        self.assertLess(pk[0]["rt"], pk[1]["rt"])


class Params(unittest.TestCase):
    def test_min_height(self):
        pk = peaks.find_peaks(XS, _signal(SPEC), {"min_height": 20})["peaks"]
        self.assertEqual([round(p["rt"]) for p in pk], [50, 120])

    def test_min_width(self):
        pk = peaks.find_peaks(XS, _signal(SPEC), {"min_width": 5.0})["peaks"]
        self.assertEqual([round(p["rt"]) for p in pk], [120])

    def test_integration_window(self):
        pk = peaks.find_peaks(XS, _signal(SPEC), {"start": 80, "end": 250})["peaks"]
        self.assertEqual([round(p["rt"]) for p in pk], [120, 200])

    def test_noise_factor_suppresses_noise_peaks(self):
        ys = _signal([], noise=0.5, seed=3)
        self.assertEqual(peaks.find_peaks(XS, ys)["peaks"], [])

    def test_validation(self):
        with self.assertRaises(peaks.PeakError):
            peaks.normalize_params({"smooth": "x"})
        with self.assertRaises(peaks.PeakError):
            peaks.normalize_params({"noise_factor": 0})
        with self.assertRaises(peaks.PeakError):
            peaks.normalize_params({"start": 100, "end": 50})
        with self.assertRaises(peaks.PeakError):
            peaks.normalize_params({"min_height": float("nan")})
        self.assertEqual(peaks.normalize_params({"smooth": 4})["smooth"], 5)

    def test_params_from_config(self):
        p = peaks.params_from_config({"peak_min_height": 3, "peak_smooth": 7, "peak_end": 100})
        self.assertEqual((p["min_height"], p["smooth"], p["end"]), (3.0, 7, 100.0))

    def test_degenerate_inputs(self):
        self.assertEqual(peaks.find_peaks([], [])["peaks"], [])
        self.assertEqual(peaks.find_peaks([0, 1, 2], [1, 2, 1])["peaks"], [])
        flat = peaks.find_peaks(XS, [1.0] * len(XS))
        self.assertEqual(flat["peaks"], [])
        ys = _signal(SPEC)
        ys[10] = float("nan")
        ys[11] = float("inf")
        self.assertEqual(len(peaks.find_peaks(XS, ys)["peaks"]), 3)


class Output(unittest.TestCase):
    def test_record_csv_and_opcua(self):
        rec = {"time": XS, "value": _signal(SPEC), "raw": [0] * len(XS), "uv": [0] * len(XS)}
        pk = peaks.peaks_for_record(rec)["peaks"]
        self.assertEqual(len(pk), 3)
        self.assertEqual(peaks.peaks_for_record(rec, series="raw")["peaks"], [])
        tmp = tempfile.mkdtemp()
        try:
            p = peaks.export_csv(pk, os.path.join(tmp, "p.csv"), meta=[("通道", "=cmd|' /C calc'!A0")])
            with open(p, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertTrue(rows[0][1].startswith("'"))           # 防公式注入
            self.assertEqual(rows[2], list(peaks.CSV_COLUMNS))
            self.assertEqual(len(rows), 3 + 3)
        finally:
            shutil.rmtree(tmp)
        arr = peaks.opcua_arrays(pk, limit=2)
        self.assertEqual(arr["Count"], 2)
        self.assertEqual(len(arr["RetentionTimes"]), 2)
        self.assertAlmostEqual(arr["RetentionTimes"][0], 50, delta=0.05)
        self.assertEqual(set(arr), {"Count", "RetentionTimes", "Heights", "Areas", "AreaPercents", "Widths"})

    def test_table_rows(self):
        pk = peaks.find_peaks(XS, _signal(SPEC))["peaks"]
        rows = peaks.table_rows(pk)
        self.assertEqual(len(rows[0]), len(peaks.CSV_COLUMNS))
        self.assertEqual(rows[0][0], "1")

    def test_cache(self):
        c = peaks.PeakCache()
        calls = []
        f = lambda: calls.append(1) or [1]  # noqa: E731
        c.get("k", 1, {"a": 1}, "value", f)
        c.get("k", 1, {"a": 1}, "value", f)
        c.get("k", 2, {"a": 1}, "value", f)
        self.assertEqual(len(calls), 2)

    def test_speed_20000_points(self):
        import time
        xs = [i * 0.05 for i in range(20000)]
        ys = [a + b for a, b in zip(peaks.gaussian(xs, 300, 100, 3), peaks.gaussian(xs, 700, 50, 4))]
        t0 = time.perf_counter()
        pk = peaks.find_peaks(xs, ys)["peaks"]
        self.assertEqual(len(pk), 2)
        self.assertLess(time.perf_counter() - t0, 5.0)


if __name__ == "__main__":
    unittest.main()
