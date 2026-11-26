#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desktop_features.py —— v3.0 桌面窗口新增功能的逻辑层（不依赖 tkinter，可单独测试）

  1. 谱图曲线预览：取最新快照（服务运行中 = OpcUaApp.current.latest_records；服务没运行 =
     直接读数据目录），计算坐标轴刻度、抽点后的折线坐标（窗口层只负责在 Canvas 上画）。
  2. 通道实时数据表：每个通道的最新周期、点数、采样间隔、时长、状态/告警、最后更新。
  3. 常用配置：表单值 ↔ 配置项、范围校验、保存（config_tools.update_settings + normalize_config），
     区分“自动生效 / 需重启服务 / 桌面窗口设置”。
  4. 客户端连接列表：从 asyncua 服务端内部取会话（防御性取值，取不到显示“无法获取”）。
  5. 告警桌面提醒：判断哪些告警是新出现的、要不要提醒（开关 / 级别 / 静默时段），
     Windows 任务栏闪烁（FlashWindowEx）、托盘气泡（Shell_NotifyIcon）、PowerShell 通知（备用）。
  6. 登录后自动打开窗口：当前用户“启动”文件夹（shell:startup）里的快捷方式路径、创建与删除。
     这只是登录 Windows 后打开可见的桌面窗口，不是后台服务，也不是计划任务。

所有 Windows 专用调用都包在 try 里：非 Windows 或调用失败时返回 False，不会抛异常拖垮窗口。
"""
import base64
import math
import os
import re
import sys
import time
from datetime import datetime

import config_tools
from i18n import tr


class DesktopFeatureError(Exception):
    """给人看的错误信息（直接显示即可）。"""


# ---------------------------------------------------------------------------
# 1. 谱图数据来源与曲线坐标
# ---------------------------------------------------------------------------

SERIES = (("raw", "原始值"), ("uv", "微伏"), ("value", "值"))
SERIES_KEYS = tuple(k for k, _t in SERIES)


def channel_key(rec):
    """通道键，与告警模块（alerts.AlertMonitor）的通道键完全一致：来源|应用|流路|检测器。"""
    return "%s|%s|%s|%s" % (rec.get("source"), rec.get("app"), rec.get("stream"), rec.get("det"))


def _named(prefix, num, name):
    text = "%s%s" % (prefix, "-" if num is None else num)
    return text + (" %s" % name if name else "")


def channel_label(rec, lang=None):
    """下拉框 / 表格里显示的通道名：站点 / 应用 / 流路 / 检测器。lang 指定语言（历史库里固定存中文）。"""
    station = rec.get("analyzer") or os.path.splitext(str(rec.get("source") or ""))[0] or "?"
    return " / ".join((
        tr("站点 %s", lang) % station,
        _named(tr("应用", lang), rec.get("app"), rec.get("app_name")),
        _named(tr("流路", lang), rec.get("stream"), rec.get("stream_name")),
        _named(tr("检测器", lang), rec.get("det"), rec.get("det_name")),
    ))


_LABEL_WORDS = ("站点 ", "应用", "流路", "检测器")


def translate_label(label):
    """把以中文保存的通道显示名（历史库里的“站点 X / 应用1 名称 / 流路2 名称 / 检测器1 名称”）换成当前界面语言。
    只替换每一段开头的固定词，不动用户自己起的应用 / 流路 / 检测器名称。"""
    parts = str(label or "").split(" / ")
    if len(parts) != 4:
        return str(label or "")
    out = []
    for part, w in zip(parts, _LABEL_WORDS):
        if part.startswith(w):
            word = w.strip()
            part = tr(word) + (" " if w.endswith(" ") else "") + part[len(w):]
        out.append(part)
    return " / ".join(out)


def channel_options(records):
    """[(通道键, 显示名)]，按显示名排序；同一通道只保留一条（周期号最大的）。"""
    best = {}
    for r in records or []:
        k = channel_key(r)
        if k not in best or (r.get("cycle_id") or 0) > (best[k].get("cycle_id") or 0):
            best[k] = r
    return sorted(((k, channel_label(r)) for k, r in best.items()), key=lambda kv: kv[1])


def find_record(records, key):
    for r in records or []:
        if channel_key(r) == key:
            return r
    return None


def series_title(rec, kind):
    if kind == "raw":
        return tr("原始值（计数）")
    if kind == "uv":
        return tr("微伏（µV）")
    yt = (rec or {}).get("y_title") or ""
    return tr("值（%s）") % yt if yt else tr("值")


def series_of(rec, kind):
    """(时间列表, 数值列表)，两者等长；kind 不认识时按原始值。"""
    kind = kind if kind in SERIES_KEYS else "raw"
    xs = list((rec or {}).get("time") or [])
    ys = list((rec or {}).get(kind) or [])
    n = min(len(xs), len(ys))
    return xs[:n], ys[:n]


def _finite(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def nice_step(span, count):
    """把 span 分成约 count 份的“好看”步长（1 / 2 / 5 × 10^n）。"""
    if span <= 0 or count < 1:
        return 1.0
    raw = span / float(count)
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def nice_ticks(lo, hi, count=5):
    """落在 [lo, hi] 内的刻度值列表。"""
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        return [lo]
    step = nice_step(hi - lo, max(1, int(count)))
    start = math.ceil(lo / step - 1e-9) * step
    out = []
    v = start
    while v <= hi + step * 1e-9 and len(out) < 100:
        out.append(round(v, 12))
        v += step
    return out


def fmt_tick(v, step=None):
    """刻度文字：按步长决定小数位；大数用千分位较难读，直接用 g 格式。"""
    if v == 0:
        return "0"
    if step is not None and step > 0:
        dec = max(0, -int(math.floor(math.log10(step))))
        if abs(v) >= 1e6 or dec > 6:
            return "%.4g" % v
        return "%.*f" % (dec, v)
    return "%.6g" % v


def fmt_num(v, unit="", digits=4):
    f = _finite(v)
    if f is None:
        return "-"
    text = "%.*g" % (digits, f)
    return text + (" " + unit if unit else "")


def fmt_duration(sec):
    f = _finite(sec)
    if f is None:
        return "-"
    if f >= 120:
        return tr("%.1f s（%.1f 分钟）") % (f, f / 60.0)
    return "%.1f s" % f


def downsample_minmax(xs, ys, buckets):
    """按像素列抽点：每列保留最小值和最大值（保持原顺序），峰形不会被抽没。
    点数不超过 2×buckets 时原样返回。返回 [(x, y)]。"""
    n = min(len(xs), len(ys))
    pts = [(xs[i], ys[i]) for i in range(n)]
    buckets = max(1, int(buckets))
    if n <= 2 * buckets:
        return pts
    out = []
    size = n / float(buckets)
    for b in range(buckets):
        a, z = int(b * size), int((b + 1) * size)
        if z <= a:
            continue
        seg = range(a, min(z, n))
        lo = min(seg, key=lambda i: ys[i])
        hi = max(seg, key=lambda i: ys[i])
        for i in sorted({lo, hi}):
            out.append(pts[i])
    return out


PLOT_MARGINS = (70, 20, 34, 46)    # 左 右 上 下（像素）：给刻度文字留位置


def plot_layout(rec, kind, width, height, margins=PLOT_MARGINS):
    """计算一条谱图在 width×height 画布上的全部绘制内容（纯函数，窗口层照着画即可）。

    返回 dict：
      empty   True 时只显示 message
      box     (x0, y0, x1, y1) 绘图区
      line    折线坐标 [x1, y1, x2, y2, ...]（已按像素抽点）
      xticks / yticks  [(像素位置, 文字)]
      title / info / x_title / y_title / points / drawn
    """
    width, height = int(width), int(height)
    left, right, top, bottom = margins
    x0, y0, x1, y1 = left, top, width - right, height - bottom
    base = {"box": (x0, y0, x1, y1), "line": [], "xticks": [], "yticks": [], "points": 0, "drawn": 0,
            "title": channel_label(rec) if rec else "", "info": "", "x_title": "", "y_title": ""}
    if rec is None:
        return dict(base, empty=True, message=tr("请选择通道（没有数据时请先启动服务或设置数据目录）"))
    if x1 - x0 < 20 or y1 - y0 < 20:
        return dict(base, empty=True, message=tr("窗口太小"))
    xs, ys = series_of(rec, kind)
    pairs = [(fx, fy) for fx, fy in ((_finite(a), _finite(b)) for a, b in zip(xs, ys))
             if fx is not None and fy is not None]
    base["info"] = record_info(rec, len(pairs))
    base["x_title"] = tr("时间（%s）") % (rec.get("x_title") or "s")
    base["y_title"] = series_title(rec, kind)
    if not pairs:
        return dict(base, empty=True, message=tr("这条谱图没有可显示的数据点"))
    xmin = min(p[0] for p in pairs)
    xmax = max(p[0] for p in pairs)
    ymin = min(p[1] for p in pairs)
    ymax = max(p[1] for p in pairs)
    if xmax == xmin:
        xmin, xmax = xmin - 1, xmax + 1
    if ymax == ymin:
        ymin, ymax = ymin - 1, ymax + 1
    pad = (ymax - ymin) * 0.05
    ymin, ymax = ymin - pad, ymax + pad
    pw, ph = float(x1 - x0), float(y1 - y0)

    def px(v):
        return x0 + (v - xmin) / (xmax - xmin) * pw

    def py(v):
        return y1 - (v - ymin) / (ymax - ymin) * ph

    pts = downsample_minmax([p[0] for p in pairs], [p[1] for p in pairs], int(pw))
    line = []
    for fx, fy in pts:
        line += [round(px(fx), 1), round(py(fy), 1)]
    if len(line) == 2:                       # 只有一个点：画一小段横线，Canvas 才看得见
        line += [line[0] + 1, line[1]]
    xc = max(2, int(pw // 90))
    yc = max(2, int(ph // 45))
    xstep = nice_step(xmax - xmin, xc)
    ystep = nice_step(ymax - ymin, yc)
    xt = [(round(px(v), 1), fmt_tick(v, xstep)) for v in nice_ticks(xmin, xmax, xc)]
    yt = [(round(py(v), 1), fmt_tick(v, ystep)) for v in nice_ticks(ymin, ymax, yc)]
    return dict(base, empty=False, line=line, xticks=xt, yticks=yt, points=len(pairs), drawn=len(pts),
                xrange=(xmin, xmax), yrange=(ymin, ymax))


def record_info(rec, points=None):
    """曲线下方的说明：周期时间、周期号、点数、采样间隔、时长、原始值范围。"""
    n = points if points is not None else rec.get("num_points")
    parts = [tr("周期时间 %s") % (rec.get("cycle_time") or "-"),
             tr("周期号 %s") % (rec.get("cycle_id") if rec.get("cycle_id") is not None else "-"),
             tr("点数 %s") % n,
             tr("采样间隔 %s") % fmt_num(rec.get("sample_period"), "s"),
             tr("时长 %s") % fmt_duration(rec.get("duration"))]
    if rec.get("min_raw") is not None and rec.get("max_raw") is not None:
        parts.append(tr("原始值 %s～%s") % (fmt_num(rec.get("min_raw"), digits=6), fmt_num(rec.get("max_raw"), digits=6)))
    if rec.get("is_validation"):
        parts.append(tr("验证周期"))
    return " · ".join(parts)


def record_signature(rec):
    """判断曲线是否需要重画：通道 + 周期号 + 点数 + 周期时间。"""
    if not rec:
        return None
    return (channel_key(rec), rec.get("cycle_id"), rec.get("num_points"), rec.get("cycle_time"))


def service_app(sa):
    """服务运行中的 OpcUaApp 实例（取不到返回 None）。"""
    cls = getattr(sa, "OpcUaApp", None)
    return getattr(cls, "current", None) if cls is not None else None


class RecordSource:
    """谱图快照来源。

    服务运行中：直接用服务刚发布的那份快照（OpcUaApp.current.latest_records），不额外读文件；
    服务未运行：直接读数据目录（server_app.gather_snapshot，与服务同一套解析代码），
    文件没变化时复用缓存；为免反复拷贝大数据库，目录读取至少间隔 dir_interval 秒。"""

    def __init__(self, sa, dir_interval=30, clock=time.monotonic):
        self.sa = sa
        self.dir_interval = dir_interval
        self.clock = clock
        self._cache = {}
        self._dir_key = None
        self._dir_time = None
        self._dir_recs = None
        self._dir_note = ""

    def get(self, service_up, dirs=None, last_n=1, max_points=20000, force=False):
        """返回 (records, 来源 'service'/'dir'/'none', 说明文字)。"""
        if service_up:
            app = service_app(self.sa)
            recs = getattr(app, "latest_records", None) if app is not None else None
            if recs is not None:
                rev = getattr(app, "revision", 0)
                note = (tr("服务运行中：显示服务正在发布的最新快照（已更新 %s 次）") % rev) if recs else \
                    tr("服务运行中：等待第一轮扫描完成……")
                return list(recs), "service", note
        dirs = [d for d in (dirs or []) if d]
        if not dirs:
            return [], "none", tr("服务未运行，也还没有设置数据目录（“数据 › 导出 CSV / 数据源”页里选择）")
        key = (tuple(dirs), int(last_n), int(max_points))
        now = self.clock()
        if (not force and self._dir_recs is not None and key == self._dir_key
                and now - self._dir_time < self.dir_interval):
            return list(self._dir_recs), "dir", self._dir_note
        missing = [d for d in dirs if not os.path.exists(d)]
        if missing and len(missing) == len(dirs):
            return [], "none", tr("找不到数据目录：%s") % tr("、").join(missing)
        recs = self.sa.gather_snapshot(dirs, last_n, max_points, cache=self._cache)
        self._dir_key, self._dir_time, self._dir_recs = key, now, list(recs)
        self._dir_note = tr("服务未运行：直接从数据目录读取（%s，%d 个通道）") % (
            datetime.now().strftime("%H:%M:%S"), len(recs))
        return list(recs), "dir", self._dir_note


# ---------------------------------------------------------------------------
# 2. 通道实时数据表
# ---------------------------------------------------------------------------

CHANNEL_COLUMNS = (("channel", "通道（站点 / 应用 / 流路 / 检测器）", 330), ("cycle_time", "最新周期时间", 140),
                   ("cycle_id", "周期号", 60), ("points", "点数", 60), ("period", "采样间隔", 70),
                   ("duration", "时长", 120), ("status", "状态 / 告警", 260), ("updated", "最后更新", 140))


class ChannelTracker:
    """记录每个通道“本窗口看到它出新周期”的时刻（服务没运行时也能显示最后更新）。"""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._seen = {}     # key -> (cycle_id, 时间戳)

    def update(self, records):
        now = self.clock()
        keys = set()
        for r in records or []:
            k = channel_key(r)
            keys.add(k)
            old = self._seen.get(k)
            if old is None or old[0] != r.get("cycle_id"):
                self._seen[k] = (r.get("cycle_id"), now)
        return {k: v[1] for k, v in self._seen.items() if k in keys}


def _alert_index(snap):
    """{通道键: [告警]}：告警键形如 stale:通道键 / missing:通道键。"""
    out = {}
    for a in (snap or {}).get("active") or []:
        key = str(a.get("key") or "")
        if ":" in key:
            out.setdefault(key.split(":", 1)[1], []).append(a)
    return out


def channel_rows(records, alert_snap=None, updated=None, origin="service", now=None):
    """通道表的行：[{key, values(8 列文字), tag('alert'/'ok'/'offline'/'muted')}]。"""
    now = time.time() if now is None else now
    alerts_by = _alert_index(alert_snap)
    chans = {c.get("key"): c for c in (alert_snap or {}).get("channel_list") or []}
    rows = []
    for key, label in channel_options(records):
        r = find_record(records, key)
        ch = chans.get(key)
        tag = "ok"
        if origin != "service":
            status, tag = tr("服务未运行（直接读自数据目录，未发布）"), "offline"
        elif alerts_by.get(key):
            a = alerts_by[key][0]
            status, tag = tr("⚠ %s：%s") % (a.get("level"), str(a.get("text") or "")[:80]), "alert"
        elif ch and ch.get("muted"):
            status, tag = tr("已屏蔽告警"), "muted"
        elif ch and ch.get("present") is False:
            status, tag = tr("数据源消失或读不了"), "alert"
        else:
            status = tr("正常")
        if origin == "service" and ch is not None and ch.get("idle_min") is not None:
            idle = int(ch.get("idle_min") or 0)
            ts = now - idle * 60
            last = tr("%s（%s）") % (datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                                tr("刚刚") if idle < 1 else tr("%d 分钟前") % idle)
        elif updated and key in updated:
            last = datetime.fromtimestamp(updated[key]).strftime("%m-%d %H:%M:%S")
        else:
            last = "-"
        values = (label, r.get("cycle_time") or "-", "-" if r.get("cycle_id") is None else r.get("cycle_id"),
                  r.get("num_points") if r.get("num_points") is not None else "-",
                  fmt_num(r.get("sample_period"), "s"), fmt_duration(r.get("duration")), status, last)
        rows.append({"key": key, "values": values, "tag": tag})
    return rows


# ---------------------------------------------------------------------------
# 3. 常用配置
# ---------------------------------------------------------------------------

class QuickSettingError(DesktopFeatureError):
    pass


# (键, 标签, 类型, 选项/范围, 提示)。opc_port 是 endpoint 里的端口（保存时改写 endpoint）。
QUICK_FIELDS = (
    ("opc_port", "OPC UA 端口", "int", (1, 65535), "endpoint 里的端口，DCS/SCADA 用它连接，默认 4840"),
    ("poll_seconds", "扫描周期（秒）", "int", (5, 3600), "多久扫描一次数据目录"),
    ("last_n", "每流路读取最近周期数", "int", (1, 100), ""),
    ("max_points", "单曲线最大发布点数", "int", (100, 200000), "超过就抽点后再发布"),
    ("alert_stale_minutes", "无新周期告警（分钟）", "int", (0, 100000), "0 = 不检查"),
    ("alert_scan_errors", "连续扫描出错告警（次）", "int", (0, 1000), "0 = 不检查"),
    ("csv_export_dir", "CSV 导出目录", "str", None, "相对路径按程序目录解析"),
    ("web_port", "设置网页端口", "int", (1, 65535), ""),
    ("web_ui", "启用设置网页", "bool", None, "桌面窗口是本机默认入口；网页供浏览器/远程查看"),
)
NOTIFY_LEVELS = (("all", "严重和警告都提醒"), ("critical", "只提醒严重告警"))
DESKTOP_FIELDS = (
    ("desktop_notify", "新告警时桌面提醒", "bool", None, "任务栏闪烁 + Windows 通知气泡；失败时在窗口里弹出小提示框"),
    ("desktop_notify_level", "提醒哪些级别", "select", NOTIFY_LEVELS, ""),
    ("desktop_notify_quiet", "静默时段内不提醒", "bool", None, "与群机器人 / 邮件使用同一份静默时段（告警与通知页设置）"),
    ("desktop_auto_start_service", "打开窗口后自动启动服务", "bool", None,
     "关闭后打开窗口时不自动启动服务，需手动点“启动服务”（--settings 方式打开一律不自动启动）"),
    ("desktop_start_minimized", "启动时最小化窗口", "bool", None, "适合配合“登录后自动打开窗口”使用"),
)
DESKTOP_KEYS = tuple(f[0] for f in DESKTOP_FIELDS)
ALL_QUICK_FIELDS = QUICK_FIELDS + DESKTOP_FIELDS
QUICK_LABELS = dict((f[0], f[1]) for f in ALL_QUICK_FIELDS)
QUICK_LABELS["endpoint"] = "OPC UA 端口（endpoint）"

_EP_RE = re.compile(r"^(opc\.tcp://)(\[[^\]]*\]|[^:/\[\]]*)(?::(\d*))?(/.*)?$", re.I)


def endpoint_port(endpoint):
    """opc.tcp://host:port/path 里的端口；没写端口按 OPC UA 默认 4840；格式不对返回 None。"""
    m = _EP_RE.match(str(endpoint or "").strip())
    if not m:
        return None
    if m.group(3):
        return int(m.group(3))
    return 4840


def endpoint_with_port(endpoint, port):
    m = _EP_RE.match(str(endpoint or "").strip())
    if not m:
        raise QuickSettingError(tr("当前 endpoint 格式不对（%r），请在“系统 › 常用设置 → OPC UA 服务”里直接改 endpoint。") % endpoint)
    return "%s%s:%d%s" % (m.group(1), m.group(2) or "0.0.0.0", int(port), m.group(4) or "")


def quick_form_values(cfg):
    """配置（state()['config'] 或合并默认值后的 cfg）→ 表单初值。"""
    cfg = cfg or {}
    out = {}
    for key, _label, kind, _opt, _hint in ALL_QUICK_FIELDS:
        if key == "opc_port":
            p = endpoint_port(cfg.get("endpoint"))
            out[key] = "" if p is None else str(p)
        elif kind == "bool":
            out[key] = bool(cfg.get(key))
        else:
            v = cfg.get(key)
            out[key] = "" if v is None else str(v)
    return out


def quick_updates(values, orig):
    """表单值与原配置对比，返回要保存的改动 {配置键: 值}；有错一次性列出所有原因（QuickSettingError）。"""
    orig = orig or {}
    cur = quick_form_values(orig)
    errors, upd = [], {}
    for key, label, kind, opt, _hint in ALL_QUICK_FIELDS:
        if key not in values:
            continue
        v = values[key]
        label = tr(label)
        if kind == "bool":
            if bool(v) != bool(cur.get(key)):
                upd[key] = bool(v)
            continue
        text = str(v if v is not None else "").strip()
        if key == "opc_port" and not text and not cur.get(key):
            continue                         # endpoint 本身格式不对时端口栏为空：不动它
        if kind == "int":
            try:
                n = int(text)
            except ValueError:
                errors.append(tr("%s 必须是整数，当前填的是：%r") % (label, text))
                continue
            lo, hi = opt
            if not lo <= n <= hi:
                errors.append(tr("%s 必须在 %d～%d 之间，当前：%d") % (label, lo, hi, n))
                continue
            if str(n) != str(cur.get(key)):
                if key == "opc_port":
                    try:
                        upd["endpoint"] = endpoint_with_port(orig.get("endpoint"), n)
                    except QuickSettingError as e:
                        errors.append(str(e))
                else:
                    upd[key] = n
        elif kind == "select":
            allowed = [a for a, _t in opt]
            if text not in allowed:
                errors.append(tr("%s 只能选：%s") % (label, " / ".join(tr(t) for _a, t in opt)))
            elif text != str(cur.get(key)):
                upd[key] = text
        else:
            if key == "csv_export_dir" and not text:
                errors.append(tr("%s 不能为空") % label)
            elif any(c in text for c in "\r\n\t"):
                errors.append(tr("%s 不能包含换行或制表符") % label)
            elif text != str(cur.get(key)):
                upd[key] = text
    if errors:
        raise QuickSettingError(tr("没有保存，请改正：\n") + "\n".join("· " + e for e in errors))
    return upd


def classify_keys(changed, sa):
    """把改动的配置键分成：自动生效 / 需重启服务 / 桌面窗口设置。"""
    hot, restart, desk = [], [], []
    hot_keys = set(getattr(sa, "HOT_KEYS", ()) or ())
    restart_keys = set(getattr(sa, "RESTART_KEYS", ()) or ())
    for k in changed:
        if k in DESKTOP_KEYS or k.startswith("desktop_"):
            desk.append(k)
        elif k in restart_keys or k.startswith("web_"):
            restart.append(k)
        elif k in hot_keys:
            hot.append(k)
        else:                       # 例如 csv_export_dir：远程 ExportCSV 用的是启动时的配置
            restart.append(k)
    return hot, restart, desk


def _labels(keys):
    return tr("、").join(tr(QUICK_LABELS.get(k, k)) for k in keys)


def save_message(changed, sa):
    if not changed:
        return tr("没有改动。")
    hot, restart, desk = classify_keys(changed, sa)
    lines = [tr("已保存 %d 项。") % len(changed)]
    if hot:
        lines.append(tr("自动生效（服务运行中，下一轮扫描内自动加载）：%s。") % _labels(hot))
    if restart:
        extra = tr("（桌面“数据 › 导出 CSV / 数据源”页立即使用新目录）") if "csv_export_dir" in restart else ""
        lines.append(tr("需要重启服务才生效：%s%s。可点“保存并重启服务”或窗口上方“重启服务”。") % (_labels(restart), extra))
    if desk:
        lines.append(tr("桌面窗口设置，已立即生效：%s。") % _labels(desk))
    return "\n".join(lines)


def make_validator(sa, base_dir):
    """与服务启动时完全相同的校验：默认值 + 文件内容 → normalize_config，不合法抛异常。"""
    def validate(data):
        cfg = sa.load_config(None)
        cfg.update(data)
        cfg["_base_dir"] = base_dir
        sa.normalize_config(cfg, base_dir)
        return cfg
    return validate


def save_quick_settings(cfg_path, base_dir, sa, updates):
    """保存改动（config_tools.update_settings：只改给出的项、校验不过不写文件、先备份再原子替换）。
    返回 {"changed", "hot", "restart", "desktop", "message"}。失败抛 config_tools.ConfigToolError（带原因）。"""
    changed = config_tools.update_settings(cfg_path, dict(updates or {}), make_validator(sa, base_dir)) if updates else []
    hot, restart, desk = classify_keys(changed, sa)
    return {"changed": changed, "hot": hot, "restart": restart, "desktop": desk,
            "message": save_message(changed, sa)}


def startup_behavior(cfg, requested_start=True):
    """打开窗口时：(是否自动启动服务, 是否最小化)。--settings（requested_start=False）一律不自动启动。"""
    cfg = cfg or {}
    start = bool(requested_start) and bool(cfg.get("desktop_auto_start_service", True))
    return start, bool(cfg.get("desktop_start_minimized", False))


# ---------------------------------------------------------------------------
# 4. 客户端连接（asyncua 内部会话）
# ---------------------------------------------------------------------------

UNAVAILABLE = "无法获取"


def _peer_text(peer):
    if peer is None:
        return UNAVAILABLE
    if isinstance(peer, (tuple, list)) and len(peer) >= 2:
        host, port = peer[0], peer[1]
        return ("[%s]:%s" if ":" in str(host) else "%s:%s") % (host, port)
    return str(peer)


def _user_text(user):
    if user is None:
        return UNAVAILABLE
    name = getattr(user, "name", None)
    role = getattr(user, "role", None)
    role_name = getattr(role, "name", None) or (str(role) if role is not None else "")
    if name:
        return tr("%s（%s）") % (name, role_name) if role_name and role_name != "User" else str(name)
    # 本服务的 ChromUserManager 对匿名连接返回 User(role=User, name=None)；会话刚建立未激活时为 Anonymous
    if role_name.lower() in ("anonymous", "user", ""):
        return tr("匿名（只读）")
    return role_name


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa  asyncua 版本差异：取不到就算了，绝不让窗口崩
        return default


def collect_sessions(app):
    """返回 {"ok", "reason", "sessions": [{id, address, name, user, state}], "count", "max"}。

    asyncua 的 BinaryServer.clients 是每个 TCP 连接的协议对象（peer_name；processor.session 是
    InternalSession：name 会话名、user 登录用户）。这些是内部属性，不同版本可能变化，所以逐项防御性取值。"""
    res = {"ok": False, "reason": "", "sessions": [], "count": 0, "max": None}
    if app is None:
        res["reason"] = tr("服务未运行")
        return res
    cfg = _safe(lambda: getattr(app, "cfg", None)) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    server = _safe(lambda: getattr(app, "server", None))
    iserver = _safe(lambda: getattr(server, "iserver", None))
    res["max"] = _safe(lambda: int(getattr(iserver, "max_connections", None) or cfg.get("max_connections") or 0)) or \
        cfg.get("max_connections")
    clients = _safe(lambda: getattr(getattr(server, "bserver", None), "clients", None))
    items = []
    if clients is not None:
        for proto in _safe(lambda: list(clients), []) or []:
            proc = _safe(lambda: getattr(proto, "processor", None))
            sess = _safe(lambda: getattr(proc, "session", None))
            name = _safe(lambda: getattr(sess, "name", None)) if sess is not None else None
            state = _safe(lambda: getattr(sess, "state", None)) if sess is not None else None
            state_name = getattr(state, "name", None) or (str(state) if state is not None else "")
            items.append({
                "id": "%s|%s" % (id(proto), _peer_text(_safe(lambda: getattr(proto, "peer_name", None)))),
                "address": _peer_text(_safe(lambda: getattr(proto, "peer_name", None))),
                "name": str(name) if name else (tr("（尚未建立会话）") if sess is None else UNAVAILABLE),
                "user": _user_text(_safe(lambda: getattr(sess, "user", None))) if sess is not None else "-",
                "state": state_name or (tr("已连接") if sess is None else tr("活动")),
            })
        res["ok"] = True
    else:
        transports = _safe(lambda: getattr(iserver, "asyncio_transports", None))
        if transports is None:
            res["reason"] = tr("无法获取（当前 asyncua 版本不提供会话列表）")
            return res
        for tp in _safe(lambda: list(transports), []) or []:
            peer = _safe(lambda: tp.get_extra_info("peername"))
            items.append({"id": "%s|%s" % (id(tp), _peer_text(peer)), "address": _peer_text(peer),
                          "name": UNAVAILABLE, "user": UNAVAILABLE, "state": tr("已连接")})
        res["ok"] = True
        res["reason"] = tr("只能取到客户端地址（会话名、用户无法获取）")
    res["sessions"] = items
    res["count"] = len(items)
    return res


class ConnTracker:
    """给每个连接补上“连接时间”（asyncua 不记录）：第一次看到它的时刻。"""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._first = {}

    def apply(self, res):
        now = self.clock()
        alive = set()
        for s in res.get("sessions") or []:
            alive.add(s["id"])
            t = self._first.setdefault(s["id"], now)
            s["since"] = datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
        for k in [k for k in self._first if k not in alive]:
            del self._first[k]
        return res


def sessions_summary(res):
    if not res.get("ok"):
        return tr("客户端连接：%s") % (res.get("reason") or UNAVAILABLE)
    text = tr("当前连接数：%d / 上限 %s") % (res.get("count", 0), res.get("max") or UNAVAILABLE)
    if res.get("reason"):
        text += tr("（%s）") % res["reason"]
    return text


# ---------------------------------------------------------------------------
# 5. 告警桌面提醒
# ---------------------------------------------------------------------------

class AlertNotifyTracker:
    """判断哪些告警是“新出现的”、要不要提醒。

    同一条告警只提醒一次；恢复后再出现会再提醒；服务停止（离线快照）时清空记录。
    关闭提醒 / 级别不够 / 处于静默时段时不提醒，但仍记为“已见过”（静默结束后不会集中弹一堆）。"""

    def __init__(self):
        self._seen = set()
        self.suppressed = 0

    def update(self, snap, cfg):
        cfg = cfg or {}
        if not snap or snap.get("offline"):
            self._seen = set()
            return []
        active = list(snap.get("active") or [])
        new = [a for a in active if a.get("key") not in self._seen]
        self._seen = {a.get("key") for a in active}
        if not new or not cfg.get("desktop_notify", True):
            return []
        if str(cfg.get("desktop_notify_level") or "all") == "critical":
            new = [a for a in new if a.get("level") == "严重"]
        if new and cfg.get("desktop_notify_quiet", True) and snap.get("quiet"):
            self.suppressed += len(new)
            return []
        return new


def notification_text(alerts, app_name=None):
    """(标题, 正文, 级别 'error'/'warning')；长度控制在托盘气泡的限制内（标题 63、正文 255 字符）。"""
    crit = [a for a in alerts if a.get("level") == "严重"]
    app_name = app_name or tr("色谱提取 OPC UA 服务")
    title = tr("%s：新告警 %d 项%s") % (app_name, len(alerts), tr("（严重 %d）") % len(crit) if crit else "")
    body = "\n".join("[%s] %s" % (a.get("level"), a.get("text")) for a in alerts[:3])
    if len(alerts) > 3:
        body += tr("\n……另有 %d 项，请看窗口“监控 › 告警”页") % (len(alerts) - 3)
    if len(body) > 250:
        body = body[:247] + "…"
    return title[:63], body, ("error" if crit else "warning")


def is_windows():
    return os.name == "nt"


def _win_api():
    import ctypes
    from ctypes import wintypes
    return ctypes, wintypes


def flash_window(hwnd, count=5):
    """任务栏按钮闪烁（FlashWindowEx：FLASHW_TRAY | FLASHW_TIMERNOFG，直到窗口被切到前台）。"""
    if not is_windows() or not hwnd:
        return False
    try:
        ctypes, wintypes = _win_api()

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), wintypes.HWND(hwnd), 0x2 | 0xC, int(count), 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
        return True
    except Exception:  # noqa
        return False


class TrayBalloon:
    """Windows 托盘图标 + 气泡通知（Shell_NotifyIconW）。图标只在窗口打开期间存在，
    close()（窗口关闭时调用）会删除它。Win10/11 上气泡显示为系统通知。"""

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_ICON, NIF_TIP, NIF_INFO = 0x2, 0x4, 0x10
    NIIF = {"info": 0x1, "warning": 0x2, "error": 0x3}

    def __init__(self, hwnd, icon_path=None, tip="色谱提取 OPC UA 服务", uid=0xC0A1):
        self.hwnd, self.icon_path, self.tip, self.uid = hwnd, icon_path, tip, uid
        self.added = False
        self.error = ""
        self._hicon = None

    def _struct(self):
        ctypes, wintypes = _win_api()

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = wintypes.HWND(self.hwnd)
        nid.uID = self.uid
        return ctypes, wintypes, nid

    def _icon(self, ctypes, wintypes):
        if self._hicon:
            return self._hicon
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        h = None
        if self.icon_path and os.path.exists(self.icon_path):
            h = user32.LoadImageW(None, self.icon_path, 1, 16, 16, 0x10)      # IMAGE_ICON, LR_LOADFROMFILE
        if not h:
            user32.LoadIconW.restype = wintypes.HICON
            user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
            h = user32.LoadIconW(None, ctypes.c_void_p(32515))              # IDI_WARNING
        self._hicon = h
        return h

    def _call(self, ctypes, wintypes, msg, nid):
        fn = ctypes.windll.shell32.Shell_NotifyIconW
        fn.restype = wintypes.BOOL
        return bool(fn(msg, ctypes.byref(nid)))

    def show(self, title, text, level="warning"):
        if not is_windows() or not self.hwnd:
            self.error = tr("不是 Windows")
            return False
        try:
            ctypes, wintypes, nid = self._struct()
            nid.hIcon = self._icon(ctypes, wintypes)
            nid.szTip = self.tip[:127]
            if not self.added:
                nid.uFlags = self.NIF_ICON | self.NIF_TIP
                if not self._call(ctypes, wintypes, self.NIM_ADD, nid):
                    self.error = tr("Shell_NotifyIcon(NIM_ADD) 失败")
                    return False
                self.added = True
            nid.uFlags = self.NIF_ICON | self.NIF_TIP | self.NIF_INFO
            nid.szInfoTitle = str(title)[:63]
            nid.szInfo = str(text)[:255]
            nid.uVersion = 10000                     # 旧系统上的显示时长（毫秒）
            nid.dwInfoFlags = self.NIIF.get(level, 0x2)
            ok = self._call(ctypes, wintypes, self.NIM_MODIFY, nid)
            if not ok:
                self.error = tr("Shell_NotifyIcon(NIM_MODIFY) 失败")
            return ok
        except Exception as e:  # noqa
            self.error = str(e) or type(e).__name__
            return False

    def close(self):
        if not self.added:
            return
        try:
            ctypes, wintypes, nid = self._struct()
            self._call(ctypes, wintypes, self.NIM_DELETE, nid)
        except Exception:  # noqa
            pass
        self.added = False


def ps_quote(text):
    """PowerShell 单引号字符串（内部单引号写两个；去掉控制字符，防止拼出别的命令）。"""
    t = "".join(ch if (ch >= " " or ch == "\n") else " " for ch in str(text or ""))
    t = t.replace("\n", " ")
    return "'" + t.replace("'", "''").replace("\u2018", "''").replace("\u2019", "''") + "'"


def ps_encoded(script):
    """powershell -EncodedCommand 用的参数（UTF-16LE Base64）：中文、引号都不会被命令行拆坏。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def toast_script(title, text):
    return "\n".join((
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null",
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)",
        "$x = $t.GetElementsByTagName('text')",
        "$x.Item(0).AppendChild($t.CreateTextNode(%s)) > $null" % ps_quote(title),
        "$x.Item(1).AppendChild($t.CreateTextNode(%s)) > $null" % ps_quote(text),
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t)",
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier(%s).Show($n)"
        % ps_quote(POWERSHELL_APP_ID),
    ))


def _ps_argv(script):
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-EncodedCommand", ps_encoded(script)]


def powershell_toast(title, text, popen=None):
    """备用：用 PowerShell 弹 Windows 通知（无窗口，不等待）。返回是否成功启动。"""
    if not is_windows():
        return False
    try:
        import subprocess
        popen = popen or subprocess.Popen
        popen(_ps_argv(toast_script(title, text)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:  # noqa
        return False


def deliver_notification(title, text, level, backends):
    """依次尝试提醒方式 [(名称, 函数)]，返回第一个成功的名称；都失败返回 None。"""
    for name, fn in backends:
        try:
            if fn(title, text, level):
                return name
        except Exception:  # noqa
            continue
    return None


# ---------------------------------------------------------------------------
# 6. 登录后自动打开窗口（当前用户“启动”文件夹里的快捷方式）
# ---------------------------------------------------------------------------

SHORTCUT_NAME = "色谱提取 OPC UA 服务.lnk"      # 与安装程序 {userstartup}\{#MyAppName}.lnk 同名
APP_EXE = "ChromExtractOPC.exe"
CLI_EXE = "ChromExtractOPC-cli.exe"


def startup_dir(env=None):
    """当前用户的启动文件夹（shell:startup）：%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup。"""
    env = os.environ if env is None else env
    appdata = env.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def startup_shortcut_path(env=None):
    d = startup_dir(env)
    return os.path.join(d, SHORTCUT_NAME) if d else None


def shortcut_target(frozen, executable, program_dir, exists=os.path.exists):
    """快捷方式要指向谁：(目标, 参数, 起始位置, 图标)。
    打包后指向窗口版 ChromExtractOPC.exe（即使当前运行的是命令行版）；源码运行时用 pythonw 打开 server_app.py。"""
    if frozen:
        d = os.path.dirname(executable)
        exe = executable
        if os.path.basename(executable).lower() != APP_EXE.lower():
            cand = os.path.join(d, APP_EXE)
            if exists(cand):
                exe = cand
        return exe, "", d, exe
    pyw = os.path.join(os.path.dirname(executable), "pythonw.exe")
    target = pyw if exists(pyw) else executable
    script = os.path.join(program_dir, "server_app.py")
    icon = os.path.join(program_dir, "assets", "app.ico")
    return target, '"%s"' % script, program_dir, icon if exists(icon) else target


def current_shortcut_target(sa):
    return shortcut_target(bool(getattr(sys, "frozen", False)), sys.executable, sa.program_dir())


def shortcut_script(lnk, target, args, workdir, icon):
    """创建快捷方式的 PowerShell（WScript.Shell COM）：所有值用单引号字符串，防注入。"""
    return "\n".join((
        "$ErrorActionPreference = 'Stop'",
        "$d = Split-Path -Parent %s" % ps_quote(lnk),
        "if (-not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Path $d -Force > $null }",
        "$w = New-Object -ComObject WScript.Shell",
        "$s = $w.CreateShortcut(%s)" % ps_quote(lnk),
        "$s.TargetPath = %s" % ps_quote(target),
        "$s.Arguments = %s" % ps_quote(args),
        "$s.WorkingDirectory = %s" % ps_quote(workdir),
        "$s.IconLocation = %s" % ps_quote("%s,0" % icon),
        "$s.Description = %s" % ps_quote("登录 Windows 后打开色谱提取 OPC UA 服务窗口（桌面窗口，不是后台服务）"),
        "$s.WindowStyle = 1",
        "$s.Save()",
    ))


def run_powershell(script, timeout=30):
    """同步运行一段 PowerShell（无窗口）。返回 (返回码, 输出)。"""
    import subprocess
    r = subprocess.run(_ps_argv(script), capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or b"") + (r.stderr or b"")
    for enc in ("utf-8", "mbcs", "gbk"):
        try:
            return r.returncode, out.decode(enc).strip()
        except Exception:  # noqa
            continue
    return r.returncode, out.decode("latin-1", "replace").strip()


def login_open_enabled(lnk=None):
    lnk = lnk if lnk is not None else startup_shortcut_path()
    return bool(lnk) and os.path.isfile(lnk)


def set_login_open(enable, target_info, lnk=None, runner=None, windows=None):
    """勾选 = 在启动文件夹创建快捷方式；取消 = 删除。返回给人看的结果文字，失败抛 DesktopFeatureError。"""
    lnk = lnk if lnk is not None else startup_shortcut_path()
    windows = is_windows() if windows is None else windows
    if not lnk:
        raise DesktopFeatureError(tr("找不到当前用户的“启动”文件夹（没有 APPDATA 环境变量）。"))
    if not enable:
        if os.path.isfile(lnk):
            try:
                os.remove(lnk)
            except OSError as e:
                raise DesktopFeatureError(tr("删除快捷方式失败：%s\n%s") % (lnk, e))
            return tr("已取消“登录后自动打开窗口”，已删除：%s") % lnk
        return tr("“登录后自动打开窗口”本来就没有开启。")
    if not windows:
        raise DesktopFeatureError(tr("“登录后自动打开窗口”只能在 Windows 上使用。"))
    target, args, workdir, icon = target_info
    runner = runner or run_powershell
    try:
        rc, out = runner(shortcut_script(lnk, target, args, workdir, icon))
    except Exception as e:  # noqa  例如 PowerShell 被组策略禁用
        raise DesktopFeatureError(tr("创建快捷方式失败：%s") % e)
    if rc != 0 or not os.path.isfile(lnk):
        raise DesktopFeatureError(tr("创建快捷方式失败（返回 %s）：%s\n可以手动操作：Win+R 输入 shell:startup，"
                                  "把桌面上的程序快捷方式复制进去。") % (rc, (out or "")[-300:]))
    return (tr("已开启“登录后自动打开窗口”：登录 Windows 后会自动打开本程序的桌面窗口（不是后台服务）。\n"
            "快捷方式：%s") % lnk)
