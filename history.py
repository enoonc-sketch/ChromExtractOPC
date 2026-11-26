#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
history.py —— 历史数据与运行事件存储（v3.1 新增；SQLite，不依赖 tkinter）

  - 服务运行期间（就在窗口进程的服务线程里，不新增任何后台进程），每次扫描到新周期，
    把通道元数据 + 曲线（time/raw/uv/value 四列，float64 + zlib 压缩）写入 history/history.db；
  - 同时记录运行事件：服务启动/停止、在线时段、告警出现/恢复、客户端连接数变化，供运行报表统计；
  - 按保留天数和容量上限自动清理（最旧的先删）；
  - 窗口按日期范围 / 通道查询周期列表，取出曲线叠加对比，导出所选周期 CSV。
"""
import array
import csv
import json
import os
import sqlite3
import threading
import time
import zlib
from datetime import datetime, timedelta

TS = "%Y-%m-%d %H:%M:%S"
SERIES = ("time", "raw", "uv", "value")
DEFAULT_FILE = "history/history.db"
CLEANUP_EVERY_SEC = 3600
ALIVE_EVERY_SEC = 60
MAX_QUERY = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL, label TEXT, cycle_id INTEGER, cycle_time TEXT NOT NULL,
    stored_at TEXT NOT NULL, num_points INTEGER, sample_period REAL, duration REAL,
    y_title TEXT, meta TEXT, curve BLOB, size INTEGER,
    UNIQUE(channel, cycle_id, cycle_time));
CREATE INDEX IF NOT EXISTS ix_cycles_time ON cycles(cycle_time);
CREATE INDEX IF NOT EXISTS ix_cycles_channel ON cycles(channel, cycle_time);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
    key TEXT, level TEXT, text TEXT, value REAL);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts, kind);
CREATE TABLE IF NOT EXISTS uptime (
    id INTEGER PRIMARY KEY AUTOINCREMENT, start TEXT NOT NULL, last TEXT NOT NULL);
"""


class HistoryError(Exception):
    pass


def now_str(now=None):
    return (now or datetime.now()).strftime(TS)


def norm_time(v, fallback=None):
    """各种时间写法 → 'YYYY-MM-DD HH:MM:SS'；解析不了用 fallback。"""
    s = str(v or "").strip().replace("T", " ")
    for fmt in (TS, "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt).strftime(TS)
        except ValueError:
            continue
    if len(s) >= 19:
        try:
            return datetime.strptime(s[:19], TS).strftime(TS)
        except ValueError:
            pass
    return fallback


def pack_curve(rec):
    parts = [array.array("d", [float(v) for v in (rec.get(k) or [])]) for k in SERIES]
    n = min(len(a) for a in parts)
    header = json.dumps({"n": n, "series": list(SERIES), "fmt": "d"}).encode("ascii")
    body = b"".join(a[:n].tobytes() for a in parts)
    return zlib.compress(len(header).to_bytes(4, "little") + header + body, 6)


def unpack_curve(blob):
    raw = zlib.decompress(blob)
    hl = int.from_bytes(raw[:4], "little")
    hdr = json.loads(raw[4:4 + hl].decode("ascii"))
    n, body = int(hdr["n"]), raw[4 + hl:]
    out = {}
    step = n * 8
    for i, k in enumerate(hdr["series"]):
        a = array.array("d")
        a.frombytes(body[i * step:(i + 1) * step])
        out[k] = list(a)
    return out


def channel_key(rec):
    return "%s|%s|%s|%s" % (rec.get("source"), rec.get("app"), rec.get("stream"), rec.get("det"))


class HistoryStore:
    """线程安全（单连接 + 锁）。服务线程写，窗口可另开一个只读实例查询。"""

    def __init__(self, path, retention_days=90, max_mb=1024, log=None, readonly=False):
        self.path = path
        self.retention_days = int(retention_days)
        self.max_mb = int(max_mb)
        self.log = log
        self._lock = threading.RLock()
        self._last_cleanup = time.monotonic() - CLEANUP_EVERY_SEC + 60   # 启动 1 分钟后做第一次清理
        self._last_alive = 0.0
        self._uptime_id = None
        self._active = None          # 告警键 -> (级别, 文字)
        self._last_clients = None
        if readonly and not os.path.exists(path):
            raise HistoryError("还没有历史数据（%s 不存在）：服务运行并扫描到新周期后才会开始记录。" % path)
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def close(self):
        with self._lock:
            try:
                self.db.close()
            except Exception:  # noqa
                pass

    # ---- 写入 ----
    def record_records(self, records, now=None, labeler=None):
        """写入快照里的新周期（已存在的跳过）。返回新增条数。"""
        n_new = 0
        stamp = now_str(now)
        with self._lock:
            for r in records or []:
                if str(r.get("analyzer")) == "TEST":
                    continue
                key = channel_key(r)
                ct = norm_time(r.get("cycle_time"), stamp)
                try:
                    blob = pack_curve(r)
                except (TypeError, ValueError, OverflowError):
                    continue
                meta = {k: r.get(k) for k in ("source", "analyzer", "app", "app_name", "stream", "stream_name",
                                               "det", "det_name", "x_title", "is_validation", "cal_level",
                                               "min_raw", "max_raw")}
                label = labeler(r) if labeler else key
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO cycles (channel,label,cycle_id,cycle_time,stored_at,num_points,"
                    "sample_period,duration,y_title,meta,curve,size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, label, int(r.get("cycle_id") or 0), ct, stamp, int(r.get("num_points") or 0),
                     float(r.get("sample_period") or 0), float(r.get("duration") or 0), r.get("y_title") or "",
                     json.dumps(meta, ensure_ascii=False, default=str), blob, len(blob)))
                n_new += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self.db.commit()
        self.maybe_cleanup(now)
        return n_new

    def add_event(self, kind, key="", level="", text="", value=None, now=None):
        with self._lock:
            self.db.execute("INSERT INTO events (ts,kind,key,level,text,value) VALUES (?,?,?,?,?,?)",
                            (now_str(now), kind, key or "", level or "", (text or "")[:500], value))
            self.db.commit()

    def service_started(self, now=None):
        t = now_str(now)
        with self._lock:
            cur = self.db.execute("INSERT INTO uptime (start,last) VALUES (?,?)", (t, t))
            self._uptime_id = cur.lastrowid
            self.db.commit()
        self._active, self._last_clients = {}, None
        self._last_alive = time.monotonic()
        self.add_event("service_start", now=now)

    def alive(self, now=None, force=False):
        """在线心跳：最多每分钟更新一次在线时段的结束时间。"""
        if self._uptime_id is None:
            return
        if not force and time.monotonic() - self._last_alive < ALIVE_EVERY_SEC:
            return
        self._last_alive = time.monotonic()
        with self._lock:
            self.db.execute("UPDATE uptime SET last=? WHERE id=?", (now_str(now), self._uptime_id))
            self.db.commit()

    def service_stopped(self, now=None):
        self.alive(now, force=True)
        for k, (lv, txt) in list((self._active or {}).items()):
            self.add_event("alert_off", k, lv, txt, now=now)   # 服务停止时仍未恢复的告警按“结束”记
        self._active = {}
        self.add_event("service_stop", now=now)
        self._uptime_id = None

    def track_alerts(self, snapshot, now=None):
        """与上次的活动告警比较，记录出现 / 恢复。snapshot = AlertMonitor.snapshot()。"""
        if self._active is None:
            self._active = {}
        cur = {}
        for a in (snapshot or {}).get("active") or []:
            k = str(a.get("key") or a.get("text") or "")
            cur[k] = (str(a.get("level") or ""), str(a.get("text") or ""))
        for k, v in cur.items():
            if k not in self._active:
                self.add_event("alert_on", k, v[0], v[1], now=now)
        for k, v in self._active.items():
            if k not in cur:
                self.add_event("alert_off", k, v[0], v[1], now=now)
        self._active = cur
        self.alive(now)

    def track_clients(self, count, now=None):
        if count is None:
            return
        count = int(count)
        if count != self._last_clients:
            self._last_clients = count
            self.add_event("clients", value=count, now=now)

    # ---- 清理 ----
    def db_bytes(self):
        tot = 0
        for suf in ("", "-wal"):
            try:
                tot += os.path.getsize(self.path + suf)
            except OSError:
                pass
        return tot

    def maybe_cleanup(self, now=None, force=False):
        if not force and time.monotonic() - self._last_cleanup < CLEANUP_EVERY_SEC:
            return None
        self._last_cleanup = time.monotonic()
        return self.cleanup(now)

    def cleanup(self, now=None):
        """删除超过保留天数的周期和事件；总量超过容量上限时从最旧的周期开始删。返回删除的周期数。"""
        cut = now_str((now or datetime.now()) - timedelta(days=max(1, self.retention_days)))
        removed = 0
        with self._lock:
            removed += self.db.execute("DELETE FROM cycles WHERE cycle_time < ?", (cut,)).rowcount or 0
            self.db.execute("DELETE FROM events WHERE ts < ?", (cut,))
            self.db.execute("DELETE FROM uptime WHERE last < ?", (cut,))
            self.db.commit()
            limit = self.max_mb * 1024 * 1024
            while True:
                used = self.db.execute("SELECT COALESCE(SUM(size),0), COUNT(*) FROM cycles").fetchone()
                if used[0] <= limit * 0.9 or used[1] == 0:
                    break
                batch = max(1, used[1] // 20)
                removed += self.db.execute(
                    "DELETE FROM cycles WHERE id IN (SELECT id FROM cycles ORDER BY cycle_time, id LIMIT ?)",
                    (batch,)).rowcount or 0
                self.db.commit()
            if removed:
                try:
                    self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self.db.execute("VACUUM")
                except sqlite3.DatabaseError:
                    pass
        if removed and self.log:
            self.log.info("历史数据清理：删除 %d 个旧周期", removed)
        return removed

    # ---- 查询 ----
    def channels(self):
        with self._lock:
            rows = self.db.execute("SELECT channel, MAX(label), COUNT(*) FROM cycles GROUP BY channel "
                                   "ORDER BY MAX(label)").fetchall()
        return [{"channel": r[0], "label": r[1] or r[0], "count": r[2]} for r in rows]

    def query_cycles(self, start=None, end=None, channels=None, limit=MAX_QUERY):
        sql = ("SELECT id, channel, label, cycle_id, cycle_time, num_points, sample_period, duration, "
               "y_title, size FROM cycles WHERE 1=1")
        args = []
        if start:
            sql += " AND cycle_time >= ?"
            args.append(start)
        if end:
            sql += " AND cycle_time <= ?"
            args.append(end)
        chans = [c for c in (channels or []) if c]
        if chans:
            sql += " AND channel IN (%s)" % ",".join("?" * len(chans))
            args.extend(chans)
        sql += " ORDER BY cycle_time DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        keys = ("id", "channel", "label", "cycle_id", "cycle_time", "num_points", "sample_period",
                "duration", "y_title", "size")
        return [dict(zip(keys, r)) for r in rows]

    def load_cycle(self, cid):
        with self._lock:
            r = self.db.execute("SELECT id, channel, label, cycle_id, cycle_time, num_points, sample_period, "
                                "duration, y_title, meta, curve FROM cycles WHERE id=?", (int(cid),)).fetchone()
        if r is None:
            raise HistoryError("没有这个周期（可能已被清理）：%s" % cid)
        rec = json.loads(r[9] or "{}")
        rec.update(id=r[0], channel=r[1], label=r[2], cycle_id=r[3], cycle_time=r[4], num_points=r[5],
                   sample_period=r[6], duration=r[7], y_title=r[8])
        rec.update(unpack_curve(r[10]))
        return rec

    def events(self, start=None, end=None, kinds=None):
        sql, args = "SELECT ts, kind, key, level, text, value FROM events WHERE 1=1", []
        if start:
            sql += " AND ts >= ?"
            args.append(start)
        if end:
            sql += " AND ts < ?"
            args.append(end)
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args.extend(kinds)
        sql += " ORDER BY ts, id"
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(zip(("ts", "kind", "key", "level", "text", "value"), r)) for r in rows]

    def last_event_before(self, ts, kind):
        with self._lock:
            r = self.db.execute("SELECT ts, kind, key, level, text, value FROM events WHERE ts < ? AND kind=? "
                                "ORDER BY ts DESC, id DESC LIMIT 1", (ts, kind)).fetchone()
        return dict(zip(("ts", "kind", "key", "level", "text", "value"), r)) if r else None

    def uptime_spans(self, start, end):
        with self._lock:
            rows = self.db.execute("SELECT start, last FROM uptime WHERE last >= ? AND start < ? ORDER BY start",
                                   (start, end)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def cycle_counts(self, start, end):
        with self._lock:
            rows = self.db.execute("SELECT channel, MAX(label), COUNT(*), MIN(cycle_time), MAX(cycle_time) "
                                   "FROM cycles WHERE cycle_time >= ? AND cycle_time < ? GROUP BY channel",
                                   (start, end)).fetchall()
        return [{"channel": r[0], "label": r[1], "count": r[2], "first": r[3], "last": r[4]} for r in rows]

    def stats(self):
        with self._lock:
            n, size = self.db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM cycles").fetchone()
            first = self.db.execute("SELECT MIN(cycle_time) FROM cycles").fetchone()[0]
        return {"cycles": n, "curve_bytes": size, "db_bytes": self.db_bytes(), "first": first}


# ---------------------------------------------------------------------------
# 导出与叠加对比
# ---------------------------------------------------------------------------

def _cell(v):
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@"):
        try:
            float(s)
        except ValueError:
            s = "'" + s
    return s


def export_cycles_csv(store, ids, path, headers=None):
    """导出所选周期（长表：每行一个采样点）。返回导出的周期数。"""
    hdr = headers or ("通道", "周期号", "周期时间", "时间(s)", "原始值", "微伏", "值")
    n = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(hdr))
        for cid in ids:
            rec = store.load_cycle(cid)
            n += 1
            for i, t in enumerate(rec.get("time") or []):
                w.writerow([_cell(rec.get("label") or rec.get("channel")), rec.get("cycle_id"),
                            rec.get("cycle_time"), "%.6g" % t,
                            "%.9g" % rec["raw"][i], "%.9g" % rec["uv"][i], "%.9g" % rec["value"][i]])
    return n


PALETTE = ("#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b", "#e377c2", "#17becf",
           "#7f7f7f", "#bcbd22")
MAX_OVERLAY = 10


def overlay_layout(recs, kind, width, height, margins=(70, 150, 24, 46)):
    """多条曲线叠加的坐标：共用坐标轴，每条一个颜色 + 图例。
    返回 {"ok", "lines": [(颜色, 扁平坐标, 图例)], "xticks", "yticks", "box", "msg"}。"""
    import desktop_features as df
    ml, mr, mt, mb = margins
    W, H = int(width), int(height)
    box = (ml, mt, W - mr, H - mb)
    out = {"ok": False, "lines": [], "xticks": [], "yticks": [], "box": box, "msg": ""}
    if W - ml - mr < 40 or H - mt - mb < 40:
        out["msg"] = "窗口太小"
        return out
    series = []
    for r in list(recs)[:MAX_OVERLAY]:
        xs, ys = df.series_of(r, kind)
        pts = [(x, y) for x, y in zip(xs, ys) if df._finite(x) is not None and df._finite(y) is not None]
        if pts:
            series.append((r, pts))
    if not series:
        out["msg"] = "没有可画的数据"
        return out
    xlo = min(p[0] for _r, pts in series for p in pts)
    xhi = max(p[0] for _r, pts in series for p in pts)
    ylo = min(p[1] for _r, pts in series for p in pts)
    yhi = max(p[1] for _r, pts in series for p in pts)
    if xhi == xlo:
        xhi = xlo + 1
    if yhi == ylo:
        ylo, yhi = ylo - 1, yhi + 1
    pad = (yhi - ylo) * 0.05
    ylo, yhi = ylo - pad, yhi + pad
    x0, y0, x1, y1 = box
    sx, sy = (x1 - x0) / (xhi - xlo), (y1 - y0) / (yhi - ylo)
    for i, (r, pts) in enumerate(series):
        flat = []
        for x, y in df.downsample_minmax([p[0] for p in pts], [p[1] for p in pts], max(10, x1 - x0)):
            flat.extend((round(x0 + (x - xlo) * sx, 1), round(y1 - (y - ylo) * sy, 1)))
        legend = "%s #%s %s" % (r.get("label") or r.get("channel") or "", r.get("cycle_id"), r.get("cycle_time") or "")
        out["lines"].append((PALETTE[i % len(PALETTE)], flat, legend))
    xst, yst = df.nice_step(xhi - xlo, 8), df.nice_step(yhi - ylo, 6)
    out["xticks"] = [(round(x0 + (v - xlo) * sx, 1), df.fmt_tick(v, xst)) for v in df.nice_ticks(xlo, xhi, 8)]
    out["yticks"] = [(round(y1 - (v - ylo) * sy, 1), df.fmt_tick(v, yst)) for v in df.nice_ticks(ylo, yhi, 6)]
    out["ok"] = True
    out["range"] = (xlo, xhi, ylo, yhi)
    return out


def open_from_config(cfg, log=None):
    """服务启动时调用：history_enabled=false 返回 None；打开失败写日志并返回 None（不影响服务）。"""
    if not cfg.get("history_enabled", True):
        return None
    try:
        return HistoryStore(history_path(cfg), cfg.get("history_retention_days", 90),
                            cfg.get("history_max_mb", 1024), log=log)
    except Exception as e:  # noqa
        if log:
            log.error("历史数据库无法打开（历史记录与报表统计暂停）：%s", e)
        return None


def history_path(cfg):
    p = str(cfg.get("history_file") or DEFAULT_FILE)
    return p if os.path.isabs(p) else os.path.join(cfg.get("_base_dir") or ".", p)
