#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chrom_core.py —— 谱图数据解析核心库

从 DataBrowser 日志数据库（*_V3.db）和 .mbd 谱图文件中提取谱图曲线（BinTrc）。
被服务端程序（server_app.py）和命令行提取模式共用。
"""
import array
import csv
import math
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile

__version__ = "2.2"

# ---- 输入防护上限：数据文件来自外部，不能假设它是善意、完整的 ----
MAX_TRACE_POINTS = 2_000_000          # 单条谱图最多采样点数
MAX_BLOB_BYTES = 32 * 1024 * 1024     # 单条谱图二进制最大字节数
MAX_FILE_BYTES = 256 * 1024 * 1024    # .mbd 文件最大字节数（整个读入内存）
MAX_TEXT_LEN = 256                    # 来自数据文件的名称类文本最大长度


def set_limits(max_points=None, max_blob_mb=None, max_file_mb=None):
    """由配置文件覆盖默认上限。"""
    global MAX_TRACE_POINTS, MAX_BLOB_BYTES, MAX_FILE_BYTES
    if max_points:
        MAX_TRACE_POINTS = int(max_points)
    if max_blob_mb:
        MAX_BLOB_BYTES = int(max_blob_mb) * 1024 * 1024
    if max_file_mb:
        MAX_FILE_BYTES = int(max_file_mb) * 1024 * 1024


class TraceError(Exception):
    pass


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_text(s, maxlen=None):
    """去掉控制字符（防日志注入/节点名污染）并限制长度。"""
    return _CTRL_RE.sub(" ", s)[: (maxlen or MAX_TEXT_LEN)]


_CSV_DANGER = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(v):
    """CSV 公式注入防护：文本以 = + - @ 开头时，Excel 会当公式执行，前面补一个单引号。
    只处理字符串，数值原样输出。"""
    if isinstance(v, str) and v.startswith(_CSV_DANGER):
        return "'" + v
    return v


class _Reader:
    def __init__(self, buf):
        self.b = buf
        self.p = 0

    def need(self, n):
        if self.p + n > len(self.b):
            raise TraceError("数据不完整（在位置 %d 处需要 %d 字节）" % (self.p, n))

    def u32(self):
        self.need(4)
        v = struct.unpack_from("<I", self.b, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        self.need(4)
        v = struct.unpack_from("<i", self.b, self.p)[0]
        self.p += 4
        return v

    def f32(self):
        self.need(4)
        v = struct.unpack_from("<f", self.b, self.p)[0]
        self.p += 4
        return v

    def f64(self):
        self.need(8)
        v = struct.unpack_from("<d", self.b, self.p)[0]
        self.p += 8
        return v

    def cstr(self):
        end = self.b.find(b"\x00", self.p)
        if end < 0:
            raise TraceError("字符串没有结束符")
        raw = self.b[self.p:end]
        self.p = end + 1
        return _decode_text(raw)


def _decode_text(raw):
    for enc in ("utf-8", "gbk"):
        try:
            return clean_text(raw.decode(enc))
        except UnicodeDecodeError:
            pass
    return clean_text(raw.decode("latin-1", "replace"))


def parse_trc(blob):
    """把一段 BinTrc 二进制解码成字典。"""
    blob = bytes(blob)
    if len(blob) > MAX_BLOB_BYTES:
        raise TraceError("谱图数据过大：%d 字节（上限 %d）" % (len(blob), MAX_BLOB_BYTES))
    r = _Reader(blob)
    info = {}
    # 1) 头
    info["head_name"] = r.cstr()
    info["head_ver"] = r.u32()
    # 2) 采样数据
    info["chan_name"] = r.cstr()
    info["chan_ver"] = r.u32()
    info["sample_period"] = r.f32()
    info["unit_mult"] = r.f32()
    info["delay"] = r.f32()
    n = r.i32()
    if n < 0 or n > MAX_TRACE_POINTS or r.p + 4 * n > len(blob):
        raise TraceError("采样点数不合理：%d（上限 %d）" % (n, MAX_TRACE_POINTS))
    data = array.array("i")
    data.frombytes(blob[r.p:r.p + 4 * n])
    if sys.byteorder != "little":
        data.byteswap()
    r.p += 4 * n
    info["data"] = data
    info["conversion_factor"] = r.f64()
    info["base_freq"] = r.f32()
    # 3) 说明信息（缺失或不完整时不影响曲线本身）
    info.update(dict(det_id=None, trace_id=None, trace_name="", y_title="", y_mult=1.0,
                     x_title="", x_mult=1.0, expected_runtime=0.0, det_name=""))
    try:
        r.cstr()
        r.u32()
        info["det_id"] = r.i32()
        info["trace_id"] = r.i32()
        r.i32()  # trace type
        info["trace_name"] = r.cstr()
        r.f32()  # sample period (again)
        info["y_title"] = r.cstr()
        info["y_mult"] = r.f32()
        info["x_title"] = r.cstr()
        info["x_mult"] = r.f32()
        info["expected_runtime"] = r.f32()
        r.f32()  # delay time
        r.i32()  # uniform sample
        r.i32()  # attribute mask
        r.f32()  # base freq
        info["det_name"] = r.cstr()
    except TraceError:
        pass
    return info


def _fin(x, default):
    """非有限数（NaN/Inf）或非法值一律退回默认值，避免污染下游。"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def to_series(info, raw_only=False, max_points=None):
    """返回 (时间秒, 时间显示值, 原始值, 微伏, 显示值) 五个列表。
    max_points：超过时在展开成列表之前就等间隔抽点，内存占用与点数上限成正比。"""
    sp = _fin(info["sample_period"], 0.0) or _fin(info["base_freq"], 0.0)
    delay = _fin(info["delay"], 0.0)
    um = _fin(info["unit_mult"], 1.0) or 1.0
    ym = _fin(info["y_mult"], 1.0) or 1.0
    xm = _fin(info["x_mult"], 1.0) or 1.0
    data = info["data"]
    n = len(data)
    if max_points and n > max_points:
        step = n / float(max_points)
        idx = [int(k * step) for k in range(int(max_points))]
    else:
        idx = range(n)
    raw = [data[i] for i in idx]
    t = [delay + i * sp for i in idx] if sp else [float(i) for i in idx]
    tx = [v * xm for v in t]
    uv = [v * um for v in raw]
    val = [v * ym for v in uv]
    return t, tx, raw, uv, val


# ---------------------------------------------------------------------------
# 读取数据来源：日志数据库（*_V3.db）和 .mbd 文件
# ---------------------------------------------------------------------------

class Item:
    """一条谱图 = 一个来源里的一条曲线。"""

    def __init__(self, **kw):
        self.source = ""
        self.analyzer = ""
        self.app = None
        self.stream = None
        self.det = None
        self.cycle_id = None
        self.cycle_time = ""
        self.is_validation = None
        self.cal_level = None
        self.blob = b""
        self.app_name = ""
        self.stream_name = ""
        self.__dict__.update(kw)


def iter_mbd(path):
    """读取 .mbd 谱图文件，返回其中所有的 trc 谱图。"""
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise TraceError("MBD 文件过大：%s" % os.path.basename(path))
    with open(path, "rb") as f:
        buf = f.read()
    if len(buf) < 16 or buf[:4] != b"EZBD":
        raise TraceError("不是 MBD 谱图文件：%s" % os.path.basename(path))
    count = struct.unpack_from("<i", buf, 12)[0]
    if count < 0 or count > 100000 or 16 + 24 * count > len(buf):
        raise TraceError("MBD 文件头损坏")
    for k in range(count):
        o = 16 + 24 * k
        name = buf[o:o + 12].split(b"\x00")[0].decode("latin-1").lower()
        det, size, off = struct.unpack_from("<iii", buf, o + 12)
        # 偏移必须落在文件内且不能是负数（负数会让切片从文件尾倒着取，读到错误数据）
        if name == "trc" and 0 < size <= MAX_BLOB_BYTES and off >= 16 and off + size <= len(buf):
            yield Item(source=os.path.basename(path), det=det, blob=buf[off:off + size])


def _open_db_copy(path, tmpdir):
    """复制一份到临时目录再读取，避免占用或改动 DataBrowser 正在使用的原文件。"""
    base = os.path.basename(path)
    dst = os.path.join(tmpdir, base)
    shutil.copy2(path, dst)
    for ext in ("-wal", "-shm"):
        if os.path.exists(path + ext):
            shutil.copy2(path + ext, dst + ext)
    with open(dst, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise TraceError("不是 SQLite 数据库：%s" % base)
    con = sqlite3.connect(dst)
    con.text_factory = lambda b: _decode_text(b)
    # 数据库文件不可信：只读、不信任库内的视图/触发器、只允许 SELECT
    con.execute("PRAGMA query_only=ON")
    try:
        con.execute("PRAGMA trusted_schema=OFF")
    except sqlite3.Error:
        pass
    con.set_authorizer(_authorizer)   # 必须放在上面两条 PRAGMA 之后
    return con


_SQL_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                getattr(sqlite3, "SQLITE_RECURSIVE", -1)}


def _authorizer(action, arg1, arg2, dbname, source):
    return sqlite3.SQLITE_OK if action in _SQL_ALLOWED else sqlite3.SQLITE_DENY


def db_overview(con):
    """返回数据库概况（仪器、应用、流路、谱图数量）。"""
    cur = con.cursor()
    out = {}
    try:
        out["analyzers"] = cur.execute(
            "SELECT SerialNumber, Name, Version FROM Analyzer").fetchall()
    except sqlite3.Error:
        out["analyzers"] = []
    names_a = dict(cur.execute("SELECT ApplicationId, Name FROM Application").fetchall()) \
        if _has(cur, "Application") else {}
    names_s = {(a, s): n for a, s, n in cur.execute(
        "SELECT ApplicationId, StreamId, Name FROM Stream").fetchall()} if _has(cur, "Stream") else {}
    rows = cur.execute(
        "SELECT ApplicationId, StreamId, DetectorId, COUNT(*), MIN(CycleTime), MAX(CycleTime) "
        "FROM Chromatogram GROUP BY ApplicationId, StreamId, DetectorId "
        "ORDER BY ApplicationId, StreamId, DetectorId").fetchall()
    out["groups"] = [dict(app=a, stream=s, det=d, n=n, first=t0, last=t1,
                          app_name=names_a.get(a, ""), stream_name=names_s.get((a, s), ""))
                     for a, s, d, n, t0, t1 in rows]
    return out


def _has(cur, table):
    return cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone() is not None


def iter_db(path, tmpdir, app=None, stream=None, det=None, since=None, until=None, last=None):
    con = _open_db_copy(path, tmpdir)
    try:
        cur = con.cursor()
        serial = ""
        try:
            r = cur.execute("SELECT SerialNumber FROM Analyzer LIMIT 1").fetchone()
            serial = r[0] if r else ""
        except sqlite3.Error:
            pass
        names_a = dict(cur.execute("SELECT ApplicationId, Name FROM Application").fetchall()) \
            if _has(cur, "Application") else {}
        names_s = {(a, s): n for a, s, n in cur.execute(
            "SELECT ApplicationId, StreamId, Name FROM Stream").fetchall()} if _has(cur, "Stream") else {}
        cyc = {}
        if _has(cur, "Cycle"):
            for cid, isv, cal in cur.execute("SELECT CycleRowId, IsValidation, CalibrationLevel FROM Cycle"):
                cyc[cid] = (isv, cal)

        # 条件片段全是写死的列名，取值一律走参数绑定（无 SQL 注入面）
        conds, args = ["BinTrc IS NOT NULL", "length(BinTrc) <= ?"], [MAX_BLOB_BYTES]
        for clause, val in (("ApplicationId=?", app), ("StreamId=?", stream), ("DetectorId=?", det)):
            if val is not None:
                conds.append(clause)
                args.append(val)
        if since:
            conds.append("CycleTime>=?")
            args.append(since)
        if until:
            conds.append("CycleTime<=?")
            args.append(until + ("z" if len(until) <= 10 else ""))  # 只写日期时，包含当天
        where = " AND ".join(conds)
        base_cols = "CycleRowId, ApplicationId, StreamId, DetectorId, CycleTime"
        have_rowid = True
        try:
            rows = cur.execute("SELECT rowid, " + base_cols + " FROM Chromatogram WHERE "  # nosec B608
                               + where + " ORDER BY CycleRowId DESC", args).fetchall()
        except sqlite3.OperationalError:   # WITHOUT ROWID 表：退回一次性读取
            have_rowid = False
            rows = [(None,) + r for r in cur.execute(
                "SELECT " + base_cols + ", BinTrc FROM Chromatogram WHERE "  # nosec B608
                + where + " ORDER BY CycleRowId DESC", args).fetchall()]
        seen = {}
        picked = []
        for row in rows:
            rid, cid, a, s, d, ct = row[:6]
            if last:
                cycles = seen.setdefault((a, s), [])
                if cid not in cycles:
                    if len(cycles) >= last:
                        continue
                    cycles.append(cid)
            picked.append(row)
        for row in reversed(picked):
            rid, cid, a, s, d, ct = row[:6]
            if have_rowid:
                got = cur.execute("SELECT BinTrc FROM Chromatogram WHERE rowid=?", (rid,)).fetchone()
                blob = got[0] if got else None
            else:
                blob = row[6]
            if blob is None:
                continue
            isv, cal = cyc.get(cid, (None, None))
            yield Item(source=os.path.basename(path), analyzer=serial, app=a, stream=s, det=d,
                       cycle_id=cid, cycle_time=str(ct), is_validation=isv, cal_level=cal,
                       blob=blob, app_name=names_a.get(a, ""), stream_name=names_s.get((a, s), ""))
    finally:
        con.close()


def find_inputs(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for fn in sorted(files):
                    if fn.lower().endswith(".mbd") or fn.lower().endswith("_v3.db"):
                        out.append(os.path.join(root, fn))
        elif os.path.isfile(p):
            out.append(p)
        else:
            raise FileNotFoundError(p)
    return out


# ---------------------------------------------------------------------------
# 输出：CSV
# ---------------------------------------------------------------------------

def safe(s):
    return re.sub(r"[^0-9A-Za-z._+-]+", "-", str(s)).strip("-") or "x"


def item_label(it):
    parts = [os.path.splitext(it.source)[0]]
    if it.app is not None:
        parts.append("app%s" % it.app)
    if it.stream is not None:
        parts.append("str%s" % it.stream)
    parts.append("det%s" % it.det)
    if it.cycle_time:
        parts.append(it.cycle_time)
    elif it.cycle_id is not None:
        parts.append("cycle%s" % it.cycle_id)
    return safe("_".join(parts))


def write_csv(path, info, series):
    t, tx, raw, uv, val = series
    xt = safe(info["x_title"] or "x")
    yt = safe(info["y_title"] or "y")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["time_s", "time_" + xt, "raw_counts", "microvolt", "value_" + yt])
        for row in zip(t, tx, raw, uv, val):
            w.writerow(["%.6g" % row[0], "%.6g" % row[1], row[2], "%.8g" % row[3], "%.8g" % row[4]])


def extract(paths, outdir, last=None, app=None, stream=None, det=None, since=None, until=None,
            log=print):
    """导出 CSV（不含图片，供服务端命令行/远程调用使用）。"""
    os.makedirs(outdir, exist_ok=True)
    csv_dir = os.path.join(outdir, "csv")
    os.makedirs(csv_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="chrom_")
    index_rows, bad = [], 0
    try:
        for src in find_inputs(paths):
            low = src.lower()
            log("读取：%s" % src)
            gen = iter_mbd(src) if low.endswith(".mbd") else iter_db(src, tmp, app, stream, det,
                                                                     since, until, last)
            try:
                for it in gen:
                    try:
                        info = parse_trc(it.blob)
                    except TraceError as e:
                        bad += 1
                        log("  跳过一条无法解析的谱图（检测器 %s）：%s" % (it.det, e))
                        continue
                    if not info["data"]:
                        continue
                    series = to_series(info)
                    label = item_label(it)
                    write_csv(os.path.join(csv_dir, label + ".csv"), info, series)
                    t, tx, raw, uv, val = series
                    index_rows.append([csv_cell(x) for x in (
                        it.source, it.analyzer, it.app, it.app_name, it.stream,
                        it.stream_name, it.det, info["det_name"] or info["trace_name"],
                        it.cycle_id, it.cycle_time, it.is_validation, it.cal_level,
                        len(raw), "%.6g" % (info["sample_period"] or 0),
                        "%.6g" % (t[-1] if t else 0), min(raw), max(raw),
                        os.path.join("csv", label + ".csv"))])
            except (TraceError, sqlite3.Error, OSError) as e:
                bad += 1
                log("  跳过无法读取的数据源 %s：%s" % (src, e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    head = ["来源文件", "仪器序列号", "应用号", "应用名", "流路号", "流路名", "检测器号", "检测器名",
            "周期编号", "周期时间", "是否验证周期", "标定级别", "点数", "采样间隔(秒)", "总时长(秒)",
            "原始值最小", "原始值最大", "CSV文件"]
    with open(os.path.join(outdir, "index.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(index_rows)
    log("完成：共导出 %d 条谱图%s，输出文件夹：%s" % (
        len(index_rows), "（另有 %d 条无法解析）" % bad if bad else "", os.path.abspath(outdir)))
    return len(index_rows)


def list_sources(paths, log=print):
    """列出数据源概况，不导出。"""
    tmp = tempfile.mkdtemp(prefix="chrom_")
    try:
        for p in find_inputs(paths):
            low = p.lower()
            log("== %s" % p)
            try:
                _list_one(p, low, tmp, log)
            except (TraceError, sqlite3.Error, OSError) as e:
                log("   无法读取：%s" % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _list_one(p, low, tmp, log):
    if low.endswith(".mbd"):
        for it in iter_mbd(p):
            info = parse_trc(it.blob)
            dur = len(info["data"]) * (_fin(info["sample_period"], 0.0))
            log("   检测器 %s：%d 个点，采样间隔 %.4g 秒，约 %.1f 秒" % (
                it.det, len(info["data"]), _fin(info["sample_period"], 0.0), dur))
        return
    con = _open_db_copy(p, tmp)
    try:
        ov = db_overview(con)
    finally:
        con.close()
    for sn, name, ver in ov["analyzers"]:
        log("   仪器：序列号 %s，名称 %s，软件版本 %s" % (sn, name, ver))
    if not ov["groups"]:
        log("   （没有谱图）")
    for g in ov["groups"]:
        log("   应用 %s %s / 流路 %s %s / 检测器 %s：%d 条谱图（%s ～ %s）" % (
            g["app"], g["app_name"], g["stream"], g["stream_name"], g["det"],
            g["n"], g["first"], g["last"]))
