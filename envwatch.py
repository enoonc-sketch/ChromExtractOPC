#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
envwatch.py —— 启动时（以及之后定期）自动做驱动与软件环境检测，发现问题就告警。

  - 服务启动后延迟几秒在后台检测，不拖慢启动；之后每 env_check_hours 小时复查一次（0 = 只在启动时查）；
  - 检测结果里"问题"级别的项变成告警（警告级），走现有告警渠道；复查时问题消失自动恢复；
  - "注意"级别只记日志，不告警，避免打扰；
  - 数据目录类问题不在这里告警（已有"没有数据"告警，避免重复）；
  - 每次结果都记日志，网页可查看最近一次结果。
"""
import threading
import time
from datetime import datetime

SKIP_NAMES = ("数据目录",)          # 已有其它告警覆盖的项


def _key(name):
    return "".join(c if c.isalnum() else "_" for c in str(name))[:60] or "item"


class EnvWatch:
    def __init__(self, cfg, monitor, log=None, check=None, first_delay=8):
        self.cfg, self.monitor, self.log = cfg, monitor, log
        self.first_delay = first_delay
        if check is None:
            import drivercheck
            check = drivercheck.run_all
        self.check = check
        self.stop_evt = threading.Event()
        self.lock = threading.Lock()
        self.last = None              # 最近一次结果
        self.last_time = ""
        self.running = False
        self._noted = set()           # 已记过日志的"注意"项，复查时不重复刷

    def run_once(self):
        with self.lock:
            if self.running:
                return self.last
            self.running = True
        try:
            try:
                r = self.check(self.cfg)
            except Exception as e:  # noqa
                r = {"ok": False, "summary": "环境检测出错：%s" % e, "items": [], "elapsed_ms": 0}
            problems = {}
            for it in r.get("items", []):
                if it.get("name", "").startswith(SKIP_NAMES):
                    continue
                if it.get("level") == "问题":
                    text = "环境检测：%s：%s%s" % (it["name"], it["text"], "（处理：%s）" % it["fix"] if it.get("fix") else "")
                    problems[_key(it["name"])] = ("警告", text[:400])
                elif it.get("level") == "注意" and self.log:
                    sig = (it["name"], it["text"])
                    if sig not in self._noted:
                        self._noted.add(sig)
                        self.log.warning("环境检测[注意]：%s：%s", it["name"], it["text"])
            self.monitor.set_env(problems)
            with self.lock:
                self.last, self.last_time = r, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if self.log:
                self.log.info("环境检测完成：%s（%d ms）", r.get("summary"), r.get("elapsed_ms", 0))
            return r
        finally:
            with self.lock:
                self.running = False

    def _loop(self):
        if self.stop_evt.wait(self.first_delay):
            return
        if not self.cfg.get("env_check_on_start", True):
            return
        while not self.stop_evt.is_set():
            self.run_once()
            hours = float(self.cfg.get("env_check_hours", 6) or 0)
            if hours <= 0:
                return
            if self.stop_evt.wait(hours * 3600):
                return

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="envwatch").start()
        return self

    def stop(self):
        self.stop_evt.set()

    def status(self):
        with self.lock:
            return {"time": self.last_time, "running": self.running, "result": self.last}
