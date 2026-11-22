#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stability.py —— 长时间稳定性测试：按固定间隔反复执行同一个连接测试，统计
成功率 / 丢包率、延迟（最小 / 平均 / P50 / P95 / P99 / 最大）、抖动、最长连续失败、错误分布，
并按时间分段画出趋势。每次测试的明细实时写入 CSV（logs/stability/），断电也不会丢已测的数据。

支持：ping、tcp、dns、opcua、modbus_tcp、modbus_rtu、serial（串口收发，收到数据算成功）。
同一时间只运行一个稳定性测试；最长 72 小时。
"""
import csv
import os
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime

ALLOWED = {"ping": "Ping", "tcp": "TCP 端口", "dns": "DNS 解析", "opcua": "OPC UA",
           "modbus_tcp": "Modbus TCP", "modbus_rtu": "Modbus RTU", "serial": "串口收发"}
MAX_MINUTES = 72 * 60
MIN_INTERVAL = {"ping": 0.5, "opcua": 1.0}       # 其余 0.2 秒
BUCKETS = 120                                     # 趋势图分段数
KEEP_FILES = 100                                  # 多目标测试一次会产生多个文件
MAX_TARGETS = 16
SERIAL_KINDS = ("modbus_rtu", "serial")


class StabilityError(Exception):
    pass


def _default_runner(kind, params):
    import nettest
    return nettest.run_test(kind, params)


def _cat(summary):
    """把错误说明归类：数字换成 #，同类错误合并计数。"""
    return re.sub(r"\d+(?:\.\d+)?", "#", str(summary or "未知错误"))[:100]


def _cell(v):
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s     # 防 Excel 公式注入


def _fmt_dur(sec):
    sec = int(max(0, sec))
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


HINTS = (
    ("CRC", "出现 CRC 校验错误：多为线路干扰。检查屏蔽线是否单端接地、总线两端是否加 120Ω 终端电阻、是否和动力电缆同槽、波特率是否偏高。"),
    ("没有应答", "间歇性无应答：检查接线端子是否松动、485 总线上设备是否过多/线过长、从站是否忙（可适当加大间隔和超时）。"),
    ("超时", "间歇性超时：检查网线/交换机端口、网络是否拥塞，或对方设备负载过高。"),
    ("拒绝连接", "出现拒绝连接：对方服务间歇性重启，或连接数达到上限。"),
    ("没有回复", "ping 间歇丢包：检查网线、交换机、无线网桥，或对方设备 CPU 负载。"),
    ("被占用", "串口被占用：测试期间不要同时运行组态软件或串口助手。"),
    ("应答站号", "站号不一致：总线上可能有站号冲突的设备。"),
)


class StabilityRun:
    def __init__(self, kind, params, interval=1.0, duration_min=10, out_dir=None,
                 runner=None, clock=time.time, sleeper=None, log=None, tag=""):
        if kind not in ALLOWED:
            raise StabilityError("这个测试不支持长时间运行。支持：%s" % "、".join(ALLOWED.values()))
        try:
            interval = float(interval)
            duration_min = float(duration_min)
        except (TypeError, ValueError):
            raise StabilityError("间隔和时长必须是数字")
        lo = MIN_INTERVAL.get(kind, 0.2)
        if not lo <= interval <= 3600:
            raise StabilityError("%s 的测试间隔范围 %g～3600 秒" % (ALLOWED[kind], lo))
        if not 1 <= duration_min <= MAX_MINUTES:
            raise StabilityError("时长范围 1 分钟～72 小时")
        if interval > duration_min * 60:
            raise StabilityError("间隔不能比总时长还长")
        params = dict(params or {})
        if kind == "ping":
            params["count"] = 1                    # 每次只 ping 一个包，才能按次统计丢包
        self.kind, self.params, self.interval, self.duration = kind, params, interval, duration_min * 60
        self.runner, self.clock, self.log = runner or _default_runner, clock, log
        self.stop_evt = threading.Event()
        self.sleeper = sleeper or self.stop_evt.wait
        self.out_dir = out_dir
        self.tag = tag
        self.label = ""
        self.csv_path = None
        self.lock = threading.Lock()
        self.state = "ready"
        self.started = self.ended = None
        self.n = self.ok = 0
        self.lat = []
        self.recent = deque(maxlen=300)
        self.errors = Counter()
        self.err_example = {}
        self.streak = self.max_streak = 0
        self.max_streak_at = ""
        self.cur_streak_from = None
        self.jit_sum, self.jit_n, self.prev_lat = 0.0, 0, None
        self.bucket_sec = max(self.interval, self.duration / BUCKETS)
        self.buckets = {}
        self.error_text = ""
        self._stats_cache = (0, None)

    # ---- 描述 ----
    def target(self):
        p = self.params
        if self.kind in ("modbus_rtu", "serial"):
            return "%s %s,%s,%s,%s" % (p.get("port", ""), p.get("baud", 9600), p.get("bytesize", 8),
                                       p.get("parity", "N"), p.get("stopbits", 1)) + (
                " 站号%s" % p.get("unit", 1) if self.kind == "modbus_rtu" else "")
        if self.kind == "opcua":
            return str(p.get("endpoint", ""))
        if self.kind in ("tcp", "modbus_tcp"):
            return "%s:%s" % (p.get("host", ""), p.get("port", 502 if self.kind == "modbus_tcp" else ""))
        return str(p.get("host", ""))

    # ---- 运行 ----
    def start(self):
        threading.Thread(target=self.run_blocking, daemon=True, name="stability").start()
        return self

    def stop(self):
        self.stop_evt.set()

    def _open_csv(self):
        if not self.out_dir:
            return None
        os.makedirs(self.out_dir, exist_ok=True)
        old = sorted(f for f in os.listdir(self.out_dir) if f.startswith("stability_") and f.endswith(".csv"))
        for f in old[:max(0, len(old) - KEEP_FILES + 1)]:
            try:
                os.remove(os.path.join(self.out_dir, f))
            except OSError:
                pass
        name = "stability_%s_%s%s.csv" % (self.kind, datetime.fromtimestamp(self.clock()).strftime("%Y%m%d_%H%M%S"),
                                          self.tag)
        self.csv_path = os.path.join(self.out_dir, name)
        f = open(self.csv_path, "w", newline="", encoding="utf-8-sig")
        w = csv.writer(f)
        w.writerow(["# 稳定性测试", ALLOWED[self.kind], _cell(self.target()),
                    "间隔 %g 秒" % self.interval, "时长 %g 分钟" % (self.duration / 60)])
        w.writerow(["序号", "时间", "结果", "延迟(ms)", "说明"])
        return f, w

    def run_blocking(self):
        self.started = self.clock()
        self.state = "running"
        out = None
        try:
            out = self._open_csv()
        except OSError as e:
            if self.log:
                self.log.warning("稳定性测试明细文件无法创建（只在页面上统计）：%s", e)
        if self.log:
            self.log.info("稳定性测试开始：%s %s，每 %g 秒一次，共 %g 分钟", ALLOWED[self.kind], self.target(),
                          self.interval, self.duration / 60)
        end = self.started + self.duration
        nxt = self.started
        try:
            while not self.stop_evt.is_set() and self.clock() < end:
                t = self.clock()
                try:
                    r = self.runner(self.kind, self.params)
                except Exception as e:  # noqa
                    r = {"ok": False, "summary": "测试出错：%s" % e, "details": []}
                self._record(t, r)
                if out:
                    lat = r.get("latency_ms", r.get("elapsed_ms"))
                    out[1].writerow([self.n, datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                                     "成功" if r.get("ok") else "失败", "" if lat is None else lat,
                                     _cell(r.get("summary", ""))])
                    if self.n % 20 == 0:
                        out[0].flush()
                nxt += self.interval
                wait = nxt - self.clock()
                if wait < 0:                 # 单次测试比间隔还慢（如超时）：不追赶，从现在重新计时
                    nxt = self.clock()
                    wait = 0
                self.sleeper(min(wait, max(0, end - self.clock())))
            self.state = "stopped" if self.stop_evt.is_set() else "finished"
        except Exception as e:  # noqa
            self.state, self.error_text = "error", str(e)
        finally:
            self.ended = self.clock()
            if out:
                try:
                    s = self.stats()
                    out[1].writerow([])
                    for k, v in (("测试次数", s["n"]), ("失败次数", s["fail"]), ("失败率%", s["loss_pct"]),
                                 ("平均延迟ms", s["avg"]), ("P95延迟ms", s["p95"]), ("最大延迟ms", s["max"]),
                                 ("抖动ms", s["jitter"]), ("最长连续失败", s["max_streak"]), ("结论", s["verdict"])):
                        out[1].writerow(["#", k, "" if v is None else v])
                    out[0].close()
                except Exception:  # noqa
                    pass
            if self.log:
                s = self.stats()
                self.log.info("稳定性测试结束（%s）：%d 次，失败 %d 次（%.2f%%），平均 %s ms，P95 %s ms。%s",
                              {"stopped": "手动停止", "finished": "到时完成", "error": "出错"}.get(self.state, self.state),
                              s["n"], s["fail"], s["loss_pct"] or 0, s["avg"], s["p95"], s["verdict"])

    def _record(self, t, r):
        ok = bool(r.get("ok"))
        lat = r.get("latency_ms", r.get("elapsed_ms")) if ok else None
        with self.lock:
            self.n += 1
            b = self.buckets.setdefault(int((t - self.started) // self.bucket_sec), [0, 0, 0.0, 0.0])
            b[0] += 1
            if ok:
                self.ok += 1
                if lat is not None:
                    lat = float(lat)
                    self.lat.append(lat)
                    b[2] += lat
                    b[3] = max(b[3], lat)
                    if self.prev_lat is not None:
                        self.jit_sum += abs(lat - self.prev_lat)
                        self.jit_n += 1
                    self.prev_lat = lat
                if self.streak > self.max_streak:
                    self.max_streak, self.max_streak_at = self.streak, self.cur_streak_from
                self.streak = 0
            else:
                b[1] += 1
                c = _cat(r.get("summary"))
                self.errors[c] += 1
                self.err_example.setdefault(c, str(r.get("summary", ""))[:160])
                if self.streak == 0:
                    self.cur_streak_from = datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S")
                self.streak += 1
            self.recent.append((round(t - self.started, 2), ok, lat))

    # ---- 统计 ----
    def stats(self):
        with self.lock:
            n, ok = self.n, self.ok
            if self._stats_cache[0] == n and self._stats_cache[1]:
                base = self._stats_cache[1]
            else:
                lat = sorted(self.lat)
                r2 = lambda v: None if v is None else round(v, 2)
                base = {"min": r2(lat[0]) if lat else None, "max": r2(lat[-1]) if lat else None,
                        "avg": r2(sum(lat) / len(lat)) if lat else None,
                        "p50": r2(_pct(lat, .5)), "p95": r2(_pct(lat, .95)), "p99": r2(_pct(lat, .99))}
                self._stats_cache = (n, base)
            streak = max(self.max_streak, self.streak)
            streak_at = self.max_streak_at if self.max_streak >= self.streak else self.cur_streak_from
            jitter = round(self.jit_sum / self.jit_n, 2) if self.jit_n else None
            errors = [{"text": self.err_example[c], "count": k} for c, k in self.errors.most_common(10)]
            buckets = [{"i": i, "n": b[0], "fail": b[1],
                        "avg": round(b[2] / (b[0] - b[1]), 2) if b[0] > b[1] else None, "max": round(b[3], 2)}
                       for i, b in sorted(self.buckets.items())]
            recent = list(self.recent)
        fail = n - ok
        loss = round(fail * 100.0 / n, 2) if n else None
        verdict, hints = self._verdict(n, fail, loss, streak, base, jitter)
        hints = hints + [h for key, h in HINTS if any(key in e["text"] for e in errors)]
        now = self.ended or self.clock()
        elapsed = (now - self.started) if self.started else 0
        return dict(base, kind=self.kind, kind_name=ALLOWED[self.kind], target=self.target(), state=self.state,
                    error=self.error_text, interval=self.interval, duration=self.duration,
                    elapsed=round(elapsed, 1), elapsed_text=_fmt_dur(elapsed), total_text=_fmt_dur(self.duration),
                    progress=round(min(100.0, elapsed * 100.0 / self.duration), 1) if self.duration else 0,
                    started=datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M:%S") if self.started else "",
                    n=n, ok=ok, fail=fail, loss_pct=loss, jitter=jitter, max_streak=streak,
                    max_streak_at=streak_at or "", errors=errors, verdict=verdict, hints=hints,
                    bucket_sec=round(self.bucket_sec, 1), buckets=buckets, recent=recent,
                    csv=os.path.basename(self.csv_path) if self.csv_path else "")

    @staticmethod
    def _verdict(n, fail, loss, streak, base, jitter):
        if not n:
            return "尚无数据", []
        hints = []
        if fail == 0:
            v = "稳定：%d 次全部成功" % n
            if base["avg"] and base["p99"] and base["p99"] > max(5 * base["avg"], base["avg"] + 50):
                v += "，但偶有延迟尖峰（P99 %.0f ms，平均 %.0f ms）" % (base["p99"], base["avg"])
                hints.append("偶发延迟尖峰：网络拥塞、交换机广播风暴，或对方设备周期性忙碌。")
        elif loss < 1 and streak <= 2:
            v = "基本稳定：偶发失败 %d 次（%.2f%%）" % (fail, loss)
        elif streak >= 5 and loss < 20:
            v = "不稳定：出现连续 %d 次失败，疑似间歇性断线" % streak
            hints.append("连续失败通常是物理链路间歇断开：检查接头、端子、网线水晶头、USB 转换器是否接触不良或休眠。")
        else:
            v = "不稳定：失败率 %.2f%%" % loss
        return v, hints


# ---------------------------------------------------------------------------
# 多目标并行测试
# ---------------------------------------------------------------------------

def _wrap_bus(runner, lock, gap):
    """同一串口上的多个目标共用一把锁：RS-485 是半双工总线，同一时间只能有一个站在通信。"""
    if lock is None:
        return runner

    def go(kind, params):
        with lock:
            try:
                return runner(kind, params)
            finally:
                if gap:
                    time.sleep(gap)          # 帧间隔，给从站留出切换时间
    return go


class MultiRun:
    """同时对多个目标做稳定性测试并对比。所有目标使用相同的间隔和时长，时间段对齐，便于判断失败是否同时发生。"""

    def __init__(self, targets, interval=1.0, duration_min=10, out_dir=None, log=None,
                 runner=None, clock=time.time, bus_gap=0.02, run_factory=None):
        if not isinstance(targets, list) or not targets:
            raise StabilityError("请至少加入 1 个测试目标。")
        if len(targets) > MAX_TARGETS:
            raise StabilityError("一次最多 %d 个目标。" % MAX_TARGETS)
        self.log, self.clock, self.out_dir = log, clock, out_dir
        self.port_locks, port_cfg = {}, {}
        self.runs = []
        self.state = "ready"
        self.started = self.ended = None
        self.summary_csv = ""
        base = runner or _default_runner
        for i, t in enumerate(targets, 1):
            if not isinstance(t, dict):
                raise StabilityError("第 %d 个目标格式不对。" % i)
            kind, params = str(t.get("kind") or ""), dict(t.get("params") or {})
            label = str(t.get("label") or "").replace("\r", " ").replace("\n", " ").strip()[:40] or "目标%d" % i
            lock = None
            if kind in SERIAL_KINDS:
                port = str(params.get("port", "")).strip().upper()
                sig = tuple(str(params.get(k, d)) for k, d in
                            (("baud", 9600), ("bytesize", 8), ("parity", "N"), ("stopbits", 1)))
                if port in port_cfg and port_cfg[port] != sig:
                    raise StabilityError("第 %d 个目标（%s）：同一串口 %s 上的目标，波特率/数据位/校验/停止位必须一致。"
                                         % (i, label, port))
                port_cfg[port] = sig
                lock = self.port_locks.setdefault(port, threading.Lock())
            wrap = (lambda fn, _l=lock: _wrap_bus(fn, _l, bus_gap))
            try:
                if run_factory is not None:
                    r = run_factory(i, kind, params, wrap)
                else:
                    r = StabilityRun(kind, params, interval, duration_min, out_dir=out_dir,
                                     runner=wrap(base), clock=clock, tag="_t%d" % i)
            except StabilityError as e:
                raise StabilityError("第 %d 个目标（%s）：%s" % (i, label, e))
            r.label = label
            self.runs.append(r)
        self.interval, self.duration = self.runs[0].interval, self.runs[0].duration
        self.threads = []
        self._fin_lock = threading.Lock()
        self.done = threading.Event()

    def shared_ports(self):
        """{串口: 目标数}，只列出被多个目标共用的串口。"""
        cnt = Counter(str(r.params.get("port", "")).upper() for r in self.runs if r.kind in SERIAL_KINDS)
        return {p: n for p, n in cnt.items() if n > 1}

    def busy_ports(self):
        if self.state != "running":
            return set()
        return {str(r.params.get("port", "")).upper() for r in self.runs if r.kind in SERIAL_KINDS}

    def start(self, watch=True):
        self.started = self.clock()
        self.state = "running"
        if self.log:
            self.log.info("多目标稳定性测试开始：%d 个目标，每 %g 秒，共 %g 分钟", len(self.runs), self.interval,
                          self.duration / 60)
        for r in self.runs:
            th = threading.Thread(target=r.run_blocking, daemon=True, name="stability-%s" % r.label)
            th.start()
            self.threads.append(th)
        if watch:
            threading.Thread(target=self._watch, daemon=True).start()
        return self

    def _watch(self):
        for th in self.threads:
            th.join()
        self._finish()

    def run_blocking(self):
        """同步运行到结束（命令行、测试用）。"""
        self.start(watch=False)
        for th in self.threads:
            th.join()
        self._finish()

    def _finish(self):
        with self._fin_lock:                 # 只收尾一次（后台监视线程和调用方都可能到这里）
            if self.state != "running":
                return
            self.ended = self.clock()
            final = "stopped" if any(r.state == "stopped" for r in self.runs) else "finished"
            try:
                self._write_summary()
            except OSError as e:
                if self.log:
                    self.log.warning("多目标对比结果无法保存：%s", e)
            self.state = final               # 先写完汇总再改状态：页面看到"已完成"时汇总文件一定已经在
            self.done.set()
        if self.log:
            s = self.stats()
            self.log.info("多目标稳定性测试结束：%s", s["verdict"])

    def stop(self):
        for r in self.runs:
            r.stop()

    # ---- 对比与关联分析 ----
    def stats(self):
        per = [r.stats() for r in self.runs]
        for s, r in zip(per, self.runs):
            s["label"] = r.label
        nb = max(1, int(-(-self.duration // per[0]["bucket_sec"]))) if per else 1
        fail_by_bucket = Counter()
        rows = []
        for s in per:
            fb = {b["i"]: b for b in s["buckets"] if b["fail"]}
            for i in fb:
                fail_by_bucket[i] += 1
            rows.append({"label": s["label"], "kind_name": s["kind_name"], "target": s["target"], "state": s["state"],
                         "n": s["n"], "fail": s["fail"], "loss_pct": s["loss_pct"], "max_streak": s["max_streak"],
                         "max_streak_at": s["max_streak_at"], "avg": s["avg"], "p95": s["p95"], "max": s["max"],
                         "jitter": s["jitter"], "verdict": s["verdict"], "csv": s["csv"],
                         "errors": s["errors"][:3], "hints": s["hints"],
                         "strip": [[i, round(b["fail"] / b["n"], 3)] for i, b in sorted(fb.items())]})
        failing = [r for r in rows if r["fail"]]
        any_buckets = len(fail_by_bucket)
        shared = sum(1 for v in fail_by_bucket.values() if v >= 2)
        notes = []
        if not any(r["n"] for r in rows):
            verdict = "尚无数据"
        elif not failing:
            verdict = "全部 %d 个目标稳定，没有失败" % len(rows)
        elif len(failing) == 1 and len(rows) > 1:
            verdict = "只有「%s」出现失败，其它 %d 个目标正常" % (failing[0]["label"], len(rows) - 1)
            notes.append("问题很可能在「%s」这台设备本身或它的支路：接线端子、网口/通讯模块、站号设置、它那段线缆。"
                         % failing[0]["label"])
        elif len(failing) >= 2 and any_buckets and shared / any_buckets >= 0.5:
            verdict = "%d 个目标在相同时段一起失败（%d 个时段重合）" % (len(failing), shared)
            notes.append("多个目标同时出问题，故障多在它们共用的部分：交换机/网关、485 总线主干或终端电阻、"
                         "本机网卡或 USB 转换器、公共电源或接地。")
        elif len(failing) >= 2:
            verdict = "%d 个目标各自零散失败，时间上基本不重合" % len(failing)
            notes.append("失败时间不重合，更像各自支路的问题，建议逐个排查失败率最高的目标。")
        else:
            verdict = rows[0]["verdict"]
        for port, k in self.shared_ports().items():
            notes.append("%s 上的 %d 个目标按顺序轮询（RS-485 半双工，不能同时通信），每个目标的实际间隔可能比设置的长。"
                         % (port, k))
        worst = max(rows, key=lambda r: ((r["loss_pct"] or 0), r["max_streak"])) if failing else None
        elapsed = ((self.ended or self.clock()) - self.started) if self.started else 0
        return {"state": self.state, "count": len(rows), "interval": self.interval, "duration": self.duration,
                "elapsed_text": _fmt_dur(elapsed), "total_text": _fmt_dur(self.duration),
                "progress": round(min(100.0, elapsed * 100.0 / self.duration), 1) if self.duration else 0,
                "bucket_sec": per[0]["bucket_sec"] if per else 0, "n_buckets": nb,
                "verdict": verdict, "notes": notes, "worst": worst["label"] if worst else "",
                "shared_buckets": sorted(i for i, v in fail_by_bucket.items() if v >= 2),
                "rows": rows, "summary_csv": self.summary_csv}

    def _write_summary(self):
        if not self.out_dir:
            return
        os.makedirs(self.out_dir, exist_ok=True)
        name = "stability_multi_%s.csv" % datetime.fromtimestamp(self.started).strftime("%Y%m%d_%H%M%S")
        s = self.stats()
        with open(os.path.join(self.out_dir, name), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["# 多目标稳定性测试", "间隔 %g 秒" % self.interval, "时长 %g 分钟" % (self.duration / 60),
                        "开始 %s" % datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M:%S")])
            w.writerow(["名称", "类型", "目标", "次数", "失败", "失败率%", "最长连续失败", "平均ms", "P95ms",
                        "最大ms", "抖动ms", "结论", "明细文件"])
            for r in s["rows"]:
                w.writerow([_cell(x) for x in (r["label"], r["kind_name"], r["target"], r["n"], r["fail"],
                                               r["loss_pct"], r["max_streak"], r["avg"], r["p95"], r["max"],
                                               r["jitter"], r["verdict"], r["csv"])])
            w.writerow([])
            w.writerow(["# 总体结论", _cell(s["verdict"])])
            for n in s["notes"]:
                w.writerow(["# 说明", _cell(n)])
        self.summary_csv = name
