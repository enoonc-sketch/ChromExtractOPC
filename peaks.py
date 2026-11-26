#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
peaks.py —— 峰识别与峰表（v3.1 新增；纯 Python，不依赖 numpy / tkinter）

步骤：
  1. 只看积分区间 [start, end]（秒；end=0 表示到最后）；
  2. 移动平均平滑（smooth 个点，奇数）；
  3. 噪声估计：一阶差分的中位数绝对偏差（MAD）换算成标准差，对基线漂移不敏感；
  4. 基线估计：滑动最小值（单调队列，O(n)）再平滑，用来找候选峰；
  5. 找峰：扣基线后的局部极大值，且高于阈值 = max(最小峰高, 噪声 × noise_factor)；
     向两边走到谷点（导数变号）或回落到基线附近为止，作为峰起点 / 终点；
  6. 每个峰用起点—终点连线作基线（谷到谷），在原始数据上梯形积分得到面积，
     顶点用三点抛物线插值求保留时间，半峰宽用线性插值求半高处的两个交点；
  7. 半峰宽小于最小峰宽的峰丢弃；面积% = 峰面积 / 所有峰面积之和 × 100。

输出每个峰：保留时间 rt（秒）、峰高 height、峰面积 area、面积% area_pct、半峰宽 fwhm（秒）、
起止 start / end（秒）以及起止点的基线值（画图用）。
"""
import csv
import math

DEFAULT_PARAMS = {
    "min_height": 0.0,      # 最小峰高（与所选数据同单位）；0 = 只按噪声阈值
    "min_width": 0.0,       # 最小半峰宽（秒）
    "noise_factor": 5.0,    # 阈值 = 噪声标准差 × 这个倍数
    "smooth": 5,            # 平滑点数（1 = 不平滑）
    "start": 0.0,           # 积分起点（秒）
    "end": 0.0,             # 积分终点（秒）；0 = 到最后
    "baseline_window": 0.0, # 基线滑动窗口（秒）；0 = 自动（数据时长的 1/10）
}
PARAM_RANGES = {"min_height": (0.0, 1e12), "min_width": (0.0, 3600.0), "noise_factor": (0.5, 1000.0),
                "smooth": (1, 501), "start": (0.0, 1e6), "end": (0.0, 1e6), "baseline_window": (0.0, 1e6)}
CONFIG_KEYS = {"peak_min_height": "min_height", "peak_min_width": "min_width",
               "peak_noise_factor": "noise_factor", "peak_smooth": "smooth",
               "peak_start": "start", "peak_end": "end"}
MAX_PEAKS = 500
CSV_COLUMNS = ("序号", "保留时间(s)", "峰高", "峰面积", "面积%", "半峰宽(s)", "起点(s)", "终点(s)")


class PeakError(ValueError):
    pass


def normalize_params(params=None):
    """参数校验：缺省用默认值，类型/范围不对抛 PeakError。"""
    out = dict(DEFAULT_PARAMS)
    for k, v in (params or {}).items():
        if k not in DEFAULT_PARAMS:
            continue
        try:
            v = int(float(v)) if k == "smooth" else float(v)
        except (TypeError, ValueError):
            raise PeakError("峰识别参数 %s 必须是数字，当前为：%r" % (k, v))
        if isinstance(v, float) and not math.isfinite(v):
            raise PeakError("峰识别参数 %s 不能是无穷大或 NaN" % k)
        lo, hi = PARAM_RANGES[k]
        if not lo <= v <= hi:
            raise PeakError("峰识别参数 %s 必须在 %g～%g 之间，当前为：%g" % (k, lo, hi, v))
        out[k] = v
    if out["smooth"] % 2 == 0:
        out["smooth"] += 1
    if out["end"] and out["end"] <= out["start"]:
        raise PeakError("积分终点必须大于起点（终点 0 表示到最后）")
    return out


def params_from_config(cfg):
    return normalize_params({p: (cfg or {}).get(k) for k, p in CONFIG_KEYS.items()
                             if (cfg or {}).get(k) is not None})


def moving_average(ys, w):
    n = len(ys)
    if w <= 1 or n < 3:
        return list(ys)
    h = w // 2
    pre = [0.0]
    for v in ys:
        pre.append(pre[-1] + v)
    out = []
    for i in range(n):
        a, b = max(0, i - h), min(n, i + h + 1)
        out.append((pre[b] - pre[a]) / (b - a))
    return out


def rolling_min(ys, w):
    """滑动最小值（窗口 w 点，居中），单调队列 O(n)。"""
    from collections import deque
    n = len(ys)
    if n == 0:
        return []
    h = max(1, w // 2)
    out = [0.0] * n
    dq = deque()
    j = 0
    for i in range(n):
        hi = min(n - 1, i + h)
        while j <= hi:
            while dq and ys[dq[-1]] >= ys[j]:
                dq.pop()
            dq.append(j)
            j += 1
        while dq[0] < i - h:
            dq.popleft()
        out[i] = ys[dq[0]]
    return out


def estimate_noise(ys):
    """噪声标准差估计：一阶差分的 MAD × 1.4826 / √2。"""
    if len(ys) < 3:
        return 0.0
    d = sorted(abs(ys[i + 1] - ys[i]) for i in range(len(ys) - 1))
    med = d[len(d) // 2]
    return med * 1.4826 / math.sqrt(2.0)


def _clean(xs, ys):
    pts = []
    for x, y in zip(xs or [], ys or []):
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            pts.append((x, y))
    pts.sort(key=lambda p: p[0])
    return [p[0] for p in pts], [p[1] for p in pts]


def _quad_fit(xs, ys):
    """最小二乘拟合 y = c0 + c1·x + c2·x²，返回 (c2, c1, c0)；点数不足或奇异返回 None。"""
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    u = [x - m for x in xs]
    s = [sum(v ** p for v in u) for p in range(5)]
    t = [sum(y * v ** p for v, y in zip(u, ys)) for p in range(3)]
    A = [[s[0], s[1], s[2]], [s[1], s[2], s[3]], [s[2], s[3], s[4]]]

    def det3(M):
        return (M[0][0] * (M[1][1] * M[2][2] - M[1][2] * M[2][1]) - M[0][1] * (M[1][0] * M[2][2] - M[1][2] * M[2][0])
                + M[0][2] * (M[1][0] * M[2][1] - M[1][1] * M[2][0]))
    D = det3(A)
    if abs(D) < 1e-300:
        return None
    sol = []
    for col in range(3):
        Mc = [row[:] for row in A]
        for r in range(3):
            Mc[r][col] = t[r]
        sol.append(det3(Mc) / D)
    b0, b1, b2 = sol                       # 以 m 为中心：y = b0 + b1·u + b2·u²
    return b2, b1 - 2 * b2 * m, b0 - b1 * m + b2 * m * m


def _interp_x(x0, y0, x1, y1, y):
    if y1 == y0:
        return (x0 + x1) / 2.0
    return x0 + (x1 - x0) * (y - y0) / (y1 - y0)


def _extend(sig, xs, i, step, limit, noise):
    """从峰脚 i 继续向外（step=-1 左 / +1 右）走到 limit，返回途中的最低点；
    信号比最低点高出 3 倍噪声（遇到相邻峰）就停。"""
    n = len(sig)
    best, m, j = i, sig[i], i
    while 0 <= j + step < n and (xs[j + step] >= limit if step < 0 else xs[j + step] <= limit):
        j += step
        if sig[j] < m:
            m, best = sig[j], j
        elif sig[j] > m + 3.0 * noise:
            break
    return best


def find_peaks(xs, ys, params=None):
    """返回 {"peaks": [峰...], "noise": 噪声, "threshold": 阈值, "params": 参数}。"""
    p = normalize_params(params)
    xs, ys = _clean(xs, ys)
    res = {"peaks": [], "noise": 0.0, "threshold": 0.0, "params": p}
    if len(xs) < 5:
        return res
    lo_t, hi_t = p["start"], (p["end"] or float("inf"))
    idx = [i for i, x in enumerate(xs) if lo_t <= x <= hi_t]
    if len(idx) < 5:
        return res
    xs = [xs[i] for i in idx]
    ys = [ys[i] for i in idx]
    n = len(xs)
    dt = (xs[-1] - xs[0]) / max(1, n - 1) or 1.0
    sm = moving_average(ys, int(p["smooth"]))
    noise = estimate_noise(ys)
    span = (max(sm) - min(sm)) or 1.0
    bw = p["baseline_window"] or (xs[-1] - xs[0]) / 10.0
    wpts = max(3, int(bw / dt))
    base = moving_average(rolling_min(sm, wpts), wpts)
    base = [min(b, s) for b, s in zip(base, sm)]
    sig = [s - b for s, b in zip(sm, base)]
    thr = max(p["min_height"], noise * p["noise_factor"], span * 1e-6)
    res["noise"], res["threshold"] = noise, thr
    cands = []
    i = 1
    while i < n - 1:
        if sig[i] >= thr and sig[i] >= sig[i - 1] and sig[i] > sig[i + 1]:
            cands.append(i)
        i += 1
    seen = {}
    taken = []                              # 已接受的峰的 [起点, 终点] 下标
    for c in sorted(cands, key=lambda k: -sig[k]):     # 从最高的候选开始
        if any(a0 <= c <= b0 for a0, b0 in taken):
            continue                        # 噪声把同一个峰拆成了多个极大值：并进已有的峰
        low = max(2.0 * noise, 0.002 * sig[c])
        a = c
        while a > 0 and sig[a - 1] <= sig[a] and sig[a] > low:
            a -= 1
        b = c
        while b < n - 1 and sig[b + 1] <= sig[b] and sig[b] > low:
            b += 1
        apex = c
        # 噪声让峰脚提前停在小凹坑上：按估计的半峰宽继续向外找最低点（遇到相邻峰就停）
        half = sig[apex] / 2.0
        L, R = apex, apex
        while L > 0 and sig[L] > half:
            L -= 1
        while R < n - 1 and sig[R] > half:
            R += 1
        fw = max(xs[R] - xs[L], 2 * dt)
        a = _extend(sig, xs, a, -1, xs[apex] - 1.6 * fw, noise)
        b = _extend(sig, xs, b, +1, xs[apex] + 1.6 * fw, noise)
        for a0, b0 in taken:                # 不与已接受的峰重叠（相邻峰在谷点分开）
            if a0 <= a <= b0:
                a = b0
            if a0 <= b <= b0:
                b = a0
        if b - a < 2:
            continue
        taken.append((a, b))
        seen[(a, b)] = apex
    peaks = []
    for (a, b), c in sorted(seen.items(), key=lambda kv: kv[1]):
        if b - a < 2:
            continue
        xa, xb = xs[a], xs[b]
        ya, yb = sm[a], sm[b]

        def line(x, xa=xa, xb=xb, ya=ya, yb=yb):
            return ya + (yb - ya) * (x - xa) / (xb - xa) if xb != xa else ya
        # 顶点：在原始数据上取最高点附近（约半峰宽的一半）做最小二乘抛物线拟合，
        # 得到保留时间和峰高——比三点插值抗噪，也不像平滑那样压低窄峰的峰高
        corr = [ys[k] - line(xs[k]) for k in range(a, b + 1)]
        csm = [sm[k] - line(xs[k]) for k in range(a, b + 1)]
        ci = max(range(len(csm)), key=lambda k: csm[k])
        k = a + ci
        rt, h = xs[k], corr[ci]
        half0 = csm[ci] / 2.0
        L0, R0 = ci, ci
        while L0 > 0 and csm[L0] > half0:
            L0 -= 1
        while R0 < len(csm) - 1 and csm[R0] > half0:
            R0 += 1
        span_pts = max(1, (R0 - L0) // 4)
        lo_i, hi_i = max(0, ci - span_pts), min(len(corr) - 1, ci + span_pts)
        fit = _quad_fit([xs[a + j] for j in range(lo_i, hi_i + 1)], corr[lo_i:hi_i + 1])
        if fit is not None:
            c2, c1, c0 = fit
            if c2 < 0:
                xv = -c1 / (2 * c2)
                if xs[a + lo_i] <= xv <= xs[a + hi_i]:
                    rt, h = xv, c0 + c1 * xv + c2 * xv * xv
        if h < thr:
            continue
        area = 0.0
        for j in range(a, b):
            f0, f1 = ys[j] - line(xs[j]), ys[j + 1] - line(xs[j + 1])
            area += (f0 + f1) * 0.5 * (xs[j + 1] - xs[j])
        half = h / 2.0
        L = ci
        while L > 0 and corr[L] > half:
            L -= 1
        R = ci
        while R < len(corr) - 1 and corr[R] > half:
            R += 1
        xl = _interp_x(xs[a + L], corr[L], xs[a + L + 1], corr[L + 1], half) if L < ci else xs[a]
        xr = _interp_x(xs[a + R - 1], corr[R - 1], xs[a + R], corr[R], half) if R > ci else xs[b]
        fwhm = max(0.0, xr - xl)
        if fwhm < p["min_width"]:
            continue
        peaks.append({"rt": rt, "height": h, "area": area, "fwhm": fwhm, "start": xa, "end": xb,
                      "start_y": ya, "end_y": yb, "apex_y": line(rt) + h, "index": k})
        if len(peaks) >= MAX_PEAKS:
            break
    total = sum(max(0.0, pk["area"]) for pk in peaks)
    for i, pk in enumerate(peaks, 1):
        pk["no"] = i
        pk["area_pct"] = (max(0.0, pk["area"]) / total * 100.0) if total > 0 else 0.0
    res["peaks"] = peaks
    return res


def peaks_for_record(rec, params=None, series="value"):
    """对一条谱图记录（server_app 快照格式）找峰。series 不认识时用 value。"""
    series = series if series in ("raw", "uv", "value") else "value"
    rec = rec or {}
    return find_peaks(rec.get("time") or [], rec.get(series) or [], params)


def _fmt(v, digits=6):
    try:
        return "%.*g" % (digits, float(v))
    except (TypeError, ValueError):
        return ""


def table_rows(peaks):
    """峰表的显示行（字符串元组），列与 CSV_COLUMNS 一致。"""
    return [(str(pk["no"]), "%.3f" % pk["rt"], _fmt(pk["height"]), _fmt(pk["area"]),
             "%.2f" % pk["area_pct"], "%.3f" % pk["fwhm"], "%.3f" % pk["start"], "%.3f" % pk["end"])
            for pk in peaks]


def _safe_cell(v):
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") and not _is_number(s) else s


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def export_csv(peaks, path, meta=None, headers=None):
    """导出峰表 CSV（UTF-8 BOM，Excel 直接打开不乱码；防公式注入）。meta 为 [(名称, 值)] 写在表头前。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for k, v in meta or []:
            w.writerow([_safe_cell(k), _safe_cell(v)])
        if meta:
            w.writerow([])
        w.writerow(list(headers or CSV_COLUMNS))
        for row in table_rows(peaks):
            w.writerow([_safe_cell(c) for c in row])
    return path


def opcua_arrays(peaks, limit=50):
    """发布到 OPC UA 的数组（节点名固定英文，不随界面语言变化）。"""
    pk = list(peaks or [])[:max(0, int(limit))]
    return {"Count": len(pk),
            "RetentionTimes": [float(p["rt"]) for p in pk],
            "Heights": [float(p["height"]) for p in pk],
            "Areas": [float(p["area"]) for p in pk],
            "AreaPercents": [float(p["area_pct"]) for p in pk],
            "Widths": [float(p["fwhm"]) for p in pk]}


def gaussian(xs, rt, height, sigma):
    """合成高斯峰（测试与演示用）。"""
    return [height * math.exp(-((x - rt) ** 2) / (2.0 * sigma * sigma)) for x in xs]


class PeakCache:
    """按 (通道键, 周期号, 参数) 缓存找峰结果，同一周期不重复计算。"""

    def __init__(self, maxsize=256):
        self.maxsize = maxsize
        self._d = {}

    def get(self, key, cycle_id, params, series, compute):
        sig = (key, cycle_id, series, tuple(sorted((params or {}).items())))
        if sig in self._d:
            return self._d[sig]
        if len(self._d) >= self.maxsize:
            self._d.clear()
        v = self._d[sig] = compute()
        return v
