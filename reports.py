#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reports.py —— 运行报表（v3.1 新增；不依赖 tkinter，不加新依赖）

统计数据来自 history.HistoryStore（服务运行期间记录的周期与事件）：
  - 按日 / 按班（班次开始时间可配置，例如 白班 08:00、夜班 20:00；班次属于它开始的那一天）；
  - 统计：服务在线时长与在线率、启动次数、每个通道的周期数 / 首末周期 / 告警次数与时长 / 状态、
    告警明细（出现、恢复、持续时长）、OPC UA 客户端连接数峰值；
  - 导出：.xlsx（本模块用标准库 zipfile 直接生成 Office Open XML，不依赖 openpyxl / xlsxwriter，
    Excel / WPS 可直接打开）、.html（浏览器打开后“打印 → 另存为 PDF”）、.csv。

自动生成由桌面窗口里的定时器完成（窗口运行期间每分钟检查一次，上一个日 / 班结束后生成一次），
不建 Windows 计划任务。
"""
import csv
import html
import os
import re
import zipfile
from datetime import datetime, timedelta

TS = "%Y-%m-%d %H:%M:%S"
DEFAULT_SHIFTS = [{"name": "白班", "start": "08:00"}, {"name": "夜班", "start": "20:00"}]
MODES = ("off", "daily", "shift")
_HM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ReportError(ValueError):
    pass


def normalize_shifts(items):
    """班次列表校验：1～6 个，名称非空且不重复，开始时间 HH:MM 不重复；按开始时间排序。"""
    if items in (None, "", []):
        return [dict(x) for x in DEFAULT_SHIFTS]
    if not isinstance(items, list):
        raise ReportError("班次必须是列表，例如 [{\"name\": \"白班\", \"start\": \"08:00\"}]")
    out, names, starts = [], set(), set()
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise ReportError("第 %d 个班次格式不对" % i)
        name = str(it.get("name") or "").strip()[:20]
        st = str(it.get("start") or "").strip()
        m = _HM.match(st)
        if not name:
            raise ReportError("第 %d 个班次没有名称" % i)
        if not m:
            raise ReportError("班次「%s」的开始时间要写成 HH:MM，例如 08:00" % name)
        st = "%02d:%s" % (int(m.group(1)), m.group(2))
        if name in names or st in starts:
            raise ReportError("班次名称或开始时间重复：%s %s" % (name, st))
        names.add(name)
        starts.add(st)
        out.append({"name": name, "start": st})
    if len(out) > 6:
        raise ReportError("班次最多 6 个")
    return sorted(out, key=lambda x: x["start"])


def parse_shifts_text(text):
    """“白班=08:00, 夜班=20:00” 这种写法 → 列表。"""
    items = []
    for part in re.split(r"[,，;；\n]+", str(text or "")):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ReportError("班次写法：名称=HH:MM，多个用逗号隔开，例如 白班=08:00, 夜班=20:00")
        n, s = part.split("=", 1)
        items.append({"name": n.strip(), "start": s.strip()})
    return normalize_shifts(items)


def shifts_text(shifts):
    return ", ".join("%s=%s" % (s["name"], s["start"]) for s in shifts)


def normalize_mode(v):
    v = str(v or "off").strip().lower()
    if v not in MODES:
        raise ReportError("自动生成报表只能是 off / daily / shift，当前为：%r" % v)
    return v


def _at(day, hm):
    h, m = hm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m))


def day_period(day):
    s = datetime(day.year, day.month, day.day)
    return {"kind": "day", "name": s.strftime("%Y-%m-%d"), "start": s, "end": s + timedelta(days=1)}


def shift_periods(day, shifts):
    """某一天开始的各个班次：[{kind, name, start, end}]，最后一个班到第二天第一个班开始。"""
    shifts = normalize_shifts(shifts)
    out = []
    for i, sh in enumerate(shifts):
        st = _at(day, sh["start"])
        if i + 1 < len(shifts):
            en = _at(day, shifts[i + 1]["start"])
        else:
            en = _at(day + timedelta(days=1), shifts[0]["start"])
        out.append({"kind": "shift", "name": "%s %s" % (day.strftime("%Y-%m-%d"), sh["name"]),
                    "shift": sh["name"], "start": st, "end": en})
    return out


def period_for(now, mode, shifts=None):
    """当前时刻所在的日 / 班。"""
    if mode == "shift":
        for d in (now - timedelta(days=1), now):
            for p in shift_periods(d.date(), shifts):
                if p["start"] <= now < p["end"]:
                    return p
    return day_period(now.date())


def completed_periods(now, mode, shifts=None, lookback_days=2):
    """最近 lookback_days 天里已经结束的日 / 班（按时间顺序），自动生成时用。"""
    out = []
    for back in range(lookback_days, -1, -1):
        d = (now - timedelta(days=back)).date()
        ps = [day_period(d)] if mode == "daily" else shift_periods(d, shifts) if mode == "shift" else []
        out.extend(p for p in ps if p["end"] <= now)
    return out


def file_stem(period):
    return "运行报表_" + re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", period["name"]).strip("_")


def _ts(dt):
    return dt.strftime(TS)


def _p(s):
    return datetime.strptime(s[:19], TS)


def _overlap(a0, a1, b0, b1):
    s, e = max(a0, b0), min(a1, b1)
    return max(0.0, (e - s).total_seconds())


def compute_report(store, start, end, now=None, labels=None):
    """统计 [start, end) 的运行情况。start/end 为 datetime。labels：{通道键: 显示名}（可选）。"""
    now = now or datetime.now()
    stop = min(end, now) if start < now else end
    span = max(1.0, (end - start).total_seconds())
    s, e = _ts(start), _ts(end)
    # 在线时长
    up = 0.0
    for a, b in store.uptime_spans(s, e):
        up += _overlap(_p(a), _p(b), start, end)
    starts = len(store.events(s, e, ["service_start"]))
    # 告警：往前多看 7 天，找跨入本时段的告警
    evs = store.events(_ts(start - timedelta(days=7)), e, ["alert_on", "alert_off", "service_stop"])
    open_, items = {}, []
    for ev in evs:
        if ev["kind"] == "alert_on":
            open_.setdefault(ev["key"], ev)
        elif ev["kind"] == "alert_off" and ev["key"] in open_:
            on = open_.pop(ev["key"])
            items.append((on, _p(ev["ts"])))
        elif ev["kind"] == "service_stop":
            for k, on in list(open_.items()):
                items.append((on, _p(ev["ts"])))
            open_.clear()
    for on in open_.values():
        items.append((on, None))
    alerts = []
    for on, off in items:
        t0 = _p(on["ts"])
        t1 = off or stop
        if t1 < start or t0 >= end:
            continue
        alerts.append({"key": on["key"], "level": on["level"], "text": on["text"], "start": on["ts"],
                       "end": _ts(off) if off else "", "ongoing": off is None,
                       "seconds": _overlap(t0, t1, start, end), "started_in": t0 >= start})
    alerts.sort(key=lambda a: a["start"])
    # 客户端连接数峰值
    cl = [ev["value"] or 0 for ev in store.events(s, e, ["clients"])]
    prev = store.last_event_before(s, "clients")
    if prev is not None and up > 0:
        cl.append(prev["value"] or 0)
    peak_clients = int(max(cl)) if cl else 0
    # 通道
    labels = dict(labels or {})
    chans = {}
    for c in store.cycle_counts(s, e):
        labels.setdefault(c["channel"], c["label"] or c["channel"])
        chans[c["channel"]] = {"channel": c["channel"], "label": labels[c["channel"]], "cycles": c["count"],
                               "first": c["first"], "last": c["last"], "alerts": 0, "alert_seconds": 0.0}
    for a in alerts:
        ch = a["key"].split(":", 1)[1] if ":" in a["key"] else ""
        if ch:
            row = chans.setdefault(ch, {"channel": ch, "label": labels.get(ch, ch), "cycles": 0, "first": "",
                                        "last": "", "alerts": 0, "alert_seconds": 0.0})
            row["alerts"] += 1 if a["started_in"] else 0
            row["alert_seconds"] += a["seconds"]
    for row in chans.values():
        row["status"] = ("无周期" if not row["cycles"] else "有告警" if row["alert_seconds"] > 0 else "正常")
    return {"start": s, "end": e, "generated": _ts(now), "span_seconds": span,
            "uptime_seconds": up, "uptime_pct": up / span * 100.0, "service_starts": starts,
            "alert_count": sum(1 for a in alerts if a["started_in"]),
            "alert_seconds": sum(a["seconds"] for a in alerts),
            "clients_peak": peak_clients, "cycles_total": sum(r["cycles"] for r in chans.values()),
            "channels": sorted(chans.values(), key=lambda r: r["label"]), "alerts": alerts}


def fmt_dur(sec):
    sec = int(round(sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return "%d:%02d:%02d" % (h, m, s)


def report_tables(rep, tr=None, title=""):
    """报表 → 三张表 [(表名, 表头, 行)]，供 xlsx / csv / html 共用。tr 为翻译函数（界面语言）。"""
    t = tr or (lambda x: x)
    summary = [
        (t("报表"), title), (t("开始"), rep["start"]), (t("结束"), rep["end"]), (t("生成时间"), rep["generated"]),
        (t("服务在线时长"), fmt_dur(rep["uptime_seconds"])), (t("在线率%"), round(rep["uptime_pct"], 2)),
        (t("服务启动次数"), rep["service_starts"]), (t("周期总数"), rep["cycles_total"]),
        (t("告警次数"), rep["alert_count"]), (t("告警累计时长"), fmt_dur(rep["alert_seconds"])),
        (t("客户端连接数峰值"), rep["clients_peak"]),
    ]
    ch_rows = [(r["label"], r["cycles"], r["first"] or "", r["last"] or "", r["alerts"],
                fmt_dur(r["alert_seconds"]), t(r["status"])) for r in rep["channels"]]
    al_rows = [(a["start"], a["end"] or t("（未恢复）"), fmt_dur(a["seconds"]), a["level"], a["text"])
               for a in rep["alerts"]]
    return [(t("概要"), (t("项目"), t("数值")), summary),
            (t("通道"), (t("通道"), t("周期数"), t("首个周期"), t("最后周期"), t("告警次数"), t("告警时长"), t("状态")), ch_rows),
            (t("告警"), (t("出现时间"), t("恢复时间"), t("持续"), t("级别"), t("内容")), al_rows)]


# ---- 最小 xlsx 写入（标准库） ----
_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _x(s):
    return html.escape(_BAD.sub("", str(s)), quote=True)


def _col(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_xml(header, rows):
    widths = [len(str(h)) for h in header]
    for r in rows:
        for i, v in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], min(60, len(str(v))))
    cols = "".join('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i + 1, i + 1, max(8, w * 2 + 2))
                   for i, w in enumerate(widths))
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
           '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
           "<cols>%s</cols><sheetData>" % cols]
    for ri, row in enumerate([header] + list(rows), 1):
        cells = []
        for ci, v in enumerate(row):
            ref = "%s%d" % (_col(ci), ri)
            style = ' s="1"' if ri == 1 else ""
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                cells.append('<c r="%s" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>' % (ref, style, _x(v)))
            else:
                cells.append('<c r="%s"%s><v>%r</v></c>' % (ref, style, v))
        out.append('<row r="%d">%s</row>' % (ri, "".join(cells)))
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(path, sheets):
    """sheets: [(表名, 表头, 行)]。生成 Excel 可打开的 .xlsx（内联字符串，第一行加粗冻结）。"""
    names = []
    for name, _h, _r in sheets:
        n = re.sub(r"[\[\]\*\?/\\:]", "_", str(name))[:31] or "Sheet"
        while n in names:
            n = (n[:28] + "_%d" % len(names))
        names.append(n)
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    ct += ['<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1)
           for i in range(len(sheets))]
    ct.append("</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    wb += ['<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_x(n), i + 1, i + 1) for i, n in enumerate(names)]
    wb.append("</sheets></workbook>")
    wbr = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    wbr += ['<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
            for i in range(len(sheets))]
    wbr.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (len(sheets) + 1))
    wbr.append("</Relationships>")
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font>'
              '<font><b/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
              '</styleSheet>')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "".join(ct))
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", "".join(wb))
        z.writestr("xl/_rels/workbook.xml.rels", "".join(wbr))
        z.writestr("xl/styles.xml", styles)
        for i, (_n, header, rows) in enumerate(sheets):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), _sheet_xml(header, rows))
    os.replace(tmp, path)
    return path


def write_html(path, sheets, title):
    parts = ["<!doctype html><html><head><meta charset='utf-8'><title>%s</title>" % _x(title),
             "<style>body{font-family:'Microsoft YaHei',sans-serif;margin:24px}table{border-collapse:collapse;"
             "margin:8px 0 24px}td,th{border:1px solid #999;padding:3px 8px;font-size:13px}th{background:#eee}"
             "@media print{h2{page-break-before:auto}}</style></head><body><h1>%s</h1>" % _x(title)]
    for name, header, rows in sheets:
        parts.append("<h2>%s</h2><table><tr>%s</tr>" % (_x(name), "".join("<th>%s</th>" % _x(h) for h in header)))
        for r in rows:
            parts.append("<tr>%s</tr>" % "".join("<td>%s</td>" % _x(v) for v in r))
        parts.append("</table>")
    parts.append("</body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return path


def _cell(v):
    s = str(v)
    if s[:1] in ("=", "+", "-", "@") and not re.match(r"^-?\d", s):
        s = "'" + s
    return s


def write_csv(path, sheets):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for name, header, rows in sheets:
            w.writerow(["## " + str(name)])
            w.writerow([_cell(h) for h in header])
            for r in rows:
                w.writerow([_cell(v) for v in r])
            w.writerow([])
    return path


def generate(store, period, out_dir, formats=("xlsx", "html"), tr=None, now=None, labels=None):
    """生成一个时段的报表文件，返回 [文件路径]。"""
    rep = compute_report(store, period["start"], period["end"], now=now, labels=labels)
    title = ("%s %s" % ((tr or (lambda x: x))("运行报表"), period["name"]))
    sheets = report_tables(rep, tr, title)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.join(out_dir, file_stem(period))
    out = []
    for fmt in formats:
        if fmt == "xlsx":
            out.append(write_xlsx(stem + ".xlsx", sheets))
        elif fmt == "html":
            out.append(write_html(stem + ".html", sheets, title))
        elif fmt == "csv":
            out.append(write_csv(stem + ".csv", sheets))
    return out, rep


class AutoReporter:
    """窗口内定时器调用 due()：返回需要补生成的时段（输出目录里已有同名 .xlsx 的跳过）。"""

    def __init__(self, mode, shifts, out_dir):
        self.mode = normalize_mode(mode)
        self.shifts = normalize_shifts(shifts)
        self.out_dir = out_dir
        self.done = set()

    def due(self, now=None):
        if self.mode == "off":
            return []
        now = now or datetime.now()
        out = []
        for p in completed_periods(now, self.mode, self.shifts, lookback_days=1):
            stem = file_stem(p)
            if stem in self.done or os.path.exists(os.path.join(self.out_dir, stem + ".xlsx")):
                self.done.add(stem)
                continue
            out.append(p)
        return out

    def mark(self, period):
        self.done.add(file_stem(period))
