#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probes.py —— 设备检测：服务运行时按设定间隔自动检测关键设备，接入现有告警。

  - 每个检测项：名称、类型（Ping / TCP / DNS / OPC UA / Modbus TCP / Modbus RTU / 串口收发）、参数、
    检测间隔、连续失败几次才告警、告警级别（警告 / 严重）、是否启用；
  - 连续失败达到次数 → 告警（走日志、网页、OPC UA Alarms、群机器人、邮件，遵守静默时段和渠道级别）；
    一次成功 → 自动恢复；停用或删除检测项 → 告警直接移除，不发"恢复"；
  - 配置保存在 config.json 的 alert_probes，在设置页面里管理；OPC UA 登录密码以系统加密保存；
  - 串口检测与单次测试、稳定性测试共用串口锁，不会同时打开同一个串口。
"""
import hashlib
import json
import re
import threading
import time
from collections import deque
from datetime import datetime

import nettest

KINDS = {"ping": "Ping", "tcp": "TCP 端口", "dns": "DNS 解析", "opcua": "OPC UA",
         "modbus_tcp": "Modbus TCP", "modbus_rtu": "Modbus RTU", "serial": "串口收发"}
LEVELS = ("警告", "严重")
MAX_PROBES = 32
MIN_INTERVAL = 10
MAX_INTERVAL = 86400
HISTORY = 60


def probe_id(item):
    pid = str((item or {}).get("id") or "").strip().lower()
    if pid:
        return pid
    return hashlib.sha1(str((item or {}).get("label") or "").encode("utf-8")).hexdigest()[:8]


def describe_target(kind, p):
    p = p or {}
    if kind in ("modbus_rtu", "serial"):
        s = "%s %s,%s,%s,%s" % (p.get("port", ""), p.get("baud", 9600), p.get("bytesize", 8),
                                p.get("parity", "N"), p.get("stopbits", 1))
        return s + (" 站号%s" % p.get("unit", 1) if kind == "modbus_rtu" else "")
    if kind == "opcua":
        return str(p.get("endpoint", ""))
    if kind in ("tcp", "modbus_tcp"):
        s = "%s:%s" % (p.get("host", ""), p.get("port", 502 if kind == "modbus_tcp" else ""))
        return s + (" 站号%s" % p.get("unit", 1) if kind == "modbus_tcp" else "")
    return str(p.get("host", ""))


def _check_params(kind, p):
    names = nettest.TESTS[kind][1]
    out = {}
    for k, v in (p or {}).items():
        if k == "password_enc" and kind == "opcua":
            out[k] = str(v)
            continue
        if k not in names or k == "password":
            continue
        if k == "rs485_rts":
            out[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "on", "yes")
            continue
        v = str(v).strip()
        if v == "":
            continue
        if len(v) > 300 or any(c in v for c in "\r\n"):
            raise ValueError("参数 %s 太长或包含换行" % k)
        out[k] = v
    try:
        if kind in ("ping", "tcp", "dns", "modbus_tcp"):
            nettest._host(out.get("host"))
        if kind == "tcp":
            nettest._port(out.get("port"))
        if kind == "modbus_tcp":
            nettest._port(out.get("port"), 502)
            nettest._mb_args(out.get("unit", 1), out.get("func", 3), out.get("addr", 0), out.get("count", 10))
        if kind == "opcua":
            ep = str(out.get("endpoint", ""))
            if not ep.startswith("opc.tcp://"):
                raise nettest.TestError("OPC UA 地址必须以 opc.tcp:// 开头")
        if kind in ("serial", "modbus_rtu"):
            nettest._serial_args(out.get("port"), out.get("baud", 9600), out.get("bytesize", 8),
                                 out.get("parity", "N"), out.get("stopbits", 1))
        if kind == "modbus_rtu":
            nettest._mb_args(out.get("unit", 1), out.get("func", 3), out.get("addr", 0), out.get("count", 10))
        if kind == "serial" and out.get("send"):
            nettest.parse_payload(out["send"], out.get("mode", "hex"))
        if "timeout" in out:
            nettest._timeout(out["timeout"])
    except nettest.TestError as e:
        raise ValueError(str(e))
    if kind == "ping":
        out.setdefault("count", "2")      # 2 个包：单个包偶发丢失不至于直接算失败
    return out


def normalize_probes(items):
    """校验并规整设备检测列表；不合法抛 ValueError（给人看的原因）。"""
    if items in (None, ""):
        return []
    if not isinstance(items, list):
        raise ValueError("设备检测必须是列表")
    if len(items) > MAX_PROBES:
        raise ValueError("设备检测最多 %d 个" % MAX_PROBES)
    out, labels, ids = [], set(), set()
    for n, it in enumerate(items, 1):
        label = ""
        try:
            if not isinstance(it, dict):
                raise ValueError("格式不对")
            label = str(it.get("label") or "").replace("\r", " ").replace("\n", " ").strip()[:40]
            if not label:
                raise ValueError("请填写名称")
            if label in labels:
                raise ValueError("名称重复")
            labels.add(label)
            kind = str(it.get("kind") or "")
            if kind not in KINDS:
                raise ValueError("不支持的检测类型：%s" % kind)
            pid = probe_id(dict(it, label=label))
            if not re.fullmatch(r"[a-z0-9]{4,16}", pid) or pid in ids:
                raise ValueError("编号不对或重复")
            ids.add(pid)
            interval = int(it.get("interval") or 60)
            if not MIN_INTERVAL <= interval <= MAX_INTERVAL:
                raise ValueError("检测间隔范围 %d～%d 秒" % (MIN_INTERVAL, MAX_INTERVAL))
            thr = int(it.get("fail_threshold") or 3)
            if not 1 <= thr <= 100:
                raise ValueError("连续失败次数范围 1～100")
            level = str(it.get("level") or "警告")
            if level not in LEVELS:
                raise ValueError("告警级别只能是 警告 或 严重")
            out.append({"id": pid, "label": label, "kind": kind, "params": _check_params(kind, it.get("params")),
                        "interval": interval, "fail_threshold": thr, "level": level,
                        "enabled": bool(it.get("enabled", True))})
        except (ValueError, TypeError) as e:
            raise ValueError("第 %d 个设备检测%s：%s" % (n, "（%s）" % label if label else "", e))
    return out


def _sig(p):
    return json.dumps([p["label"], p["kind"], p["params"], p["fail_threshold"], p["level"]],
                      sort_keys=True, ensure_ascii=False)


def run_probe_once(p, runner=None):
    """执行一次检测（解密 OPC UA 密码）。"""
    params = dict(p.get("params") or {})
    enc = params.pop("password_enc", "")
    if enc:
        try:
            import config_tools
            params["password"] = config_tools.unprotect_secret(enc)
        except Exception as e:  # noqa
            return {"ok": False, "summary": "OPC UA 密码无法解密，请删除后重新添加：%s" % e, "details": []}
    try:
        return (runner or nettest.run_test)(p["kind"], params)
    except Exception as e:  # noqa
        return {"ok": False, "summary": "检测出错：%s" % e, "details": []}


def status_from_config(items):
    """服务没运行时，给网页显示检测项列表（没有检测结果）。"""
    try:
        items = normalize_probes(items)
    except ValueError:
        items = []
    return [_row(p, None) for p in items]


def _row(p, st):
    checks = st["checks"] if st else 0
    failed = st["failed"] if st else 0
    return {"id": p["id"], "label": p["label"], "kind": p["kind"], "kind_name": KINDS[p["kind"]],
            "target": describe_target(p["kind"], p["params"]), "interval": p["interval"],
            "fail_threshold": p["fail_threshold"], "level": p["level"], "enabled": p["enabled"],
            "last": st["last"] if st else None, "fails": st["fails"] if st else 0,
            "alerting": bool(st and st["alerting"]), "checks": checks, "failed": failed,
            "uptime_pct": round((checks - failed) * 100.0 / checks, 2) if checks else None,
            "history": list(st["history"]) if st else []}


class ProbeMonitor:
    def __init__(self, cfg, monitor, log=None, runner=None, clock=time.monotonic, first_delay=5, stagger=2):
        self.cfg, self.monitor, self.log = cfg, monitor, log
        self.runner = runner
        self.clock = clock
        self.first_delay, self.stagger = first_delay, stagger
        self.lock = threading.Lock()
        self.state = {}
        self.stop_evt = threading.Event()

    def _probes(self):
        return [p for p in (self.cfg.get("alert_probes") or []) if isinstance(p, dict) and "id" in p]

    # ---- 配置变化 ----
    def sync(self):
        now = self.clock()
        forget = []
        with self.lock:
            live = {}
            for i, p in enumerate(self._probes()):
                st = self.state.get(p["id"])
                sig = _sig(p)
                if st is None or st["sig"] != sig:
                    if st is not None and st["alerting"]:
                        forget.append(p["id"])          # 改了检测内容：旧告警作废
                    st = {"sig": sig, "next": now + min(self.first_delay + self.stagger * i, p["interval"]),
                          "fails": 0, "running": False, "alerting": False, "last": None,
                          "history": deque(maxlen=HISTORY), "checks": 0, "failed": 0}
                st["probe"] = p
                if not p["enabled"] and (st["alerting"] or st["fails"]):
                    if st["alerting"]:
                        forget.append(p["id"])
                    st["alerting"], st["fails"] = False, 0
                live[p["id"]] = st
            forget += [k for k, st in self.state.items() if k not in live and st["alerting"]]
            self.state = live
        for k in forget:
            self.monitor.forget_probe(k)

    # ---- 调度 ----
    def run_due(self, sync=False):
        self.sync()
        now = self.clock()
        due = []
        with self.lock:
            for k, st in self.state.items():
                if st["probe"]["enabled"] and not st["running"] and st["next"] <= now:
                    st["running"] = True
                    st["next"] = now + st["probe"]["interval"]
                    due.append(k)
        for k in due:
            if sync:
                self._run(k)
            else:
                threading.Thread(target=self._run, args=(k,), daemon=True, name="probe-%s" % k).start()
        return due

    def has(self, pid):
        with self.lock:
            return pid in self.state

    def run_now(self, pid):
        """网页"立即检测"：同步执行并返回结果，同样计入告警判断。"""
        with self.lock:
            st = self.state.get(pid)
            if st is None:
                raise RuntimeError("没有这个设备检测。")
            if st["running"]:
                raise RuntimeError("这个检测正在进行，请稍候。")
            st["running"] = True
        return self._run(pid)

    def _run(self, k):
        with self.lock:
            st = self.state.get(k)
            if st is None:
                return None
            p, sig = st["probe"], st["sig"]
        r = run_probe_once(p, self.runner)
        raise_text, clear = None, False
        with self.lock:
            st = self.state.get(k)
            if st is None or st["sig"] != sig:      # 检测期间配置被改：结果作废
                return r
            st["running"] = False
            if r.get("busy"):                       # 串口被别的测试占用：这次不算
                return r
            ok = bool(r.get("ok"))
            st["checks"] += 1
            st["last"] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "ok": ok,
                          "summary": str(r.get("summary", ""))[:200], "latency": r.get("latency_ms")}
            st["history"].append(1 if ok else 0)
            if ok:
                st["fails"] = 0
                if st["alerting"]:
                    st["alerting"], clear = False, True
            else:
                st["failed"] += 1
                st["fails"] += 1
                if st["fails"] >= p["fail_threshold"] and p["enabled"]:
                    st["alerting"] = True
                    raise_text = "设备检测失败：%s（%s %s）连续 %d 次失败：%s" % (
                        p["label"], KINDS[p["kind"]], describe_target(p["kind"], p["params"]),
                        st["fails"], str(r.get("summary", ""))[:160])
        if raise_text:
            self.monitor.set_probe(k, p["level"], raise_text)
        elif clear:
            self.monitor.set_probe(k, None)
        return r

    # ---- 后台线程 ----
    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="probes").start()
        return self

    def _loop(self):
        while not self.stop_evt.is_set():
            try:
                self.run_due()
            except Exception as e:  # noqa
                if self.log:
                    self.log.error("设备检测调度出错：%s", e)
            self.stop_evt.wait(1)

    def stop(self):
        self.stop_evt.set()

    def status(self):
        with self.lock:
            return [_row(p, self.state.get(p["id"])) for p in self._probes()]
