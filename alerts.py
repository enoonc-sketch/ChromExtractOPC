#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
alerts.py —— 异常告警：服务卡死、扫描出错、没有数据、通道长时间不更新、数据源丢失。

设计：
  - 纯逻辑、线程安全，不依赖 OPC UA，便于测试（时间可注入）；
  - 每个告警有"出现 / 恢复"两个事件，只在状态变化时记日志和推送，不会每轮刷屏；
  - 告警出口：日志（logs/server.log）、OPC UA 节点 Chromatograph/Alarms、网页设置页的状态面板、
    可选 Webhook（企业微信 / 钉钉 / 飞书 / 通用 JSON）、可选邮件（SMTP）。
"""
import json
import threading
import time
from datetime import datetime

LEVELS = ("严重", "警告")


def _now_text(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d %H:%M:%S")


class Alert:
    __slots__ = ("key", "level", "text", "since", "since_text")

    def __init__(self, key, level, text, since):
        self.key, self.level, self.text, self.since = key, level, text, since
        self.since_text = _now_text(since)

    def as_dict(self):
        return {"key": self.key, "level": self.level, "text": self.text, "since": self.since_text}


class AlertMonitor:
    HISTORY = 200

    def __init__(self, cfg, log=None, clock=time.time, notifier=None):
        self.cfg, self.log, self.clock = cfg, log, clock
        self.notifier = notifier
        self._lock = threading.Lock()
        self.active = {}                # key -> Alert
        self.history = []               # 最近的出现/恢复事件
        self.channels = {}              # 通道 -> {"cycle_id", "changed", "label", "cycle_time"}
        self.started = clock()
        self.last_scan = None           # 最近一次扫描完成（无论成功失败）的时间
        self.last_ok_scan = None
        self.scan_errors = 0
        self.last_error = ""
        self.channel_count = 0
        self.probe_alerts = {}          # 设备检测：检测编号 -> (级别, 文字)
        self.env_alerts = {}            # 环境检测：项目名 -> (级别, 文字)

    # ---- 环境检测（envwatch.py 调用）：整体替换，没出现的项自动恢复 ----
    def set_env(self, problems):
        with self._lock:
            self.env_alerts = dict(problems or {})
        self.evaluate()

    # ---- 设备检测（probes.py 调用）----
    def set_probe(self, pid, level, text=None):
        """level=None 表示恢复。"""
        with self._lock:
            if level is None:
                self.probe_alerts.pop(pid, None)
            else:
                self.probe_alerts[pid] = (level, text)
        self.evaluate()

    def forget_probe(self, pid):
        """检测项被停用/删除/修改：告警直接移除，不发"恢复"。"""
        key = "probe:" + pid
        with self._lock:
            self.probe_alerts.pop(pid, None)
            a = self.active.pop(key, None)
            if a is not None:
                self._remember("移除", a, self.clock())
        if a is not None:
            if self.log:
                self.log.info("设备检测已停用/修改，移除告警：%s", a.text)
            if self.notifier is not None and hasattr(self.notifier, "forget"):
                self.notifier.forget([key])

    # ---- 配置 ----
    def _stale_sec(self, key=None):
        rule = self._rule(key) if key else None
        if rule and rule.get("stale_minutes") is not None:
            return max(0, int(rule["stale_minutes"])) * 60
        return max(0, int(self.cfg.get("alert_stale_minutes", 120))) * 60

    def _rule(self, key):
        """该通道当前生效的屏蔽/覆盖规则（过期的忽略）。"""
        now = _now_text(self.clock())[:16]
        for r in self.cfg.get("alert_channel_rules") or []:
            if r.get("channel") == key and not (r.get("until") and r["until"] <= now):
                return r
        return None

    def _muted(self, key):
        r = self._rule(key)
        return bool(r and r.get("mute"))

    def _poll(self):
        return max(5, int(self.cfg.get("poll_seconds", 30)))

    # ---- 扫描线程调用 ----
    def on_scan(self, recs):
        now = self.clock()
        with self._lock:
            self.last_scan = self.last_ok_scan = now
            self.scan_errors = 0
            self.channel_count = len(recs)
            seen = set()
            for r in recs:
                k = "%s|%s|%s|%s" % (r.get("source"), r.get("app"), r.get("stream"), r.get("det"))
                seen.add(k)
                label = "%s 应用%s 流路%s 检测器%s" % (r.get("source"), r.get("app"), r.get("stream"), r.get("det"))
                if r.get("stream_name"):
                    label += "（%s）" % r["stream_name"]
                ch = self.channels.get(k)
                if ch is None or ch["cycle_id"] != r.get("cycle_id"):
                    self.channels[k] = {"cycle_id": r.get("cycle_id"), "changed": now, "label": label,
                                        "cycle_time": r.get("cycle_time") or "", "present": True}
                else:
                    ch["present"] = True
                    ch["label"] = label
            for k, ch in self.channels.items():
                if k not in seen:
                    ch["present"] = False
        self.evaluate()

    def on_scan_error(self, err):
        with self._lock:
            self.last_scan = self.clock()
            self.scan_errors += 1
            self.last_error = str(err)[:300]
        self.evaluate()

    # ---- 判断（扫描线程和定时任务都会调用；扫描线程卡死时由定时任务发现）----
    def evaluate(self):
        now = self.clock()
        want = {}
        with self._lock:
            poll = self._poll()
            ref = self.last_scan if self.last_scan is not None else self.started
            if now - ref > max(3 * poll, 120):
                want["scan_stuck"] = ("严重", "数据扫描已 %d 分钟没有完成，服务可能卡死（数据文件被锁住或磁盘/网络盘无响应）。"
                                      % ((now - ref) // 60))
            n_err = int(self.cfg.get("alert_scan_errors", 3))
            if n_err > 0 and self.scan_errors >= n_err:
                want["scan_error"] = ("严重", "连续 %d 次扫描出错：%s" % (self.scan_errors, self.last_error))
            if self.last_ok_scan is not None and self.channel_count == 0:
                want["no_data"] = ("警告", "监控目录里没有找到任何谱图数据。请检查 watch_dirs 和数据文件（*_V3.db / *.mbd）。")
            for name, (lv, text) in self.env_alerts.items():
                want["env:" + name] = (lv, text)
            for pid, (lv, text) in self.probe_alerts.items():
                want["probe:" + pid] = (lv, text)
            for k, ch in self.channels.items():
                if self._muted(k):
                    continue
                stale = self._stale_sec(k)
                if not ch["present"]:
                    want["missing:" + k] = ("警告", "数据源消失或读不了：%s（OPC UA 节点已标记 Good=False）" % ch["label"])
                elif stale and now - ch["changed"] > stale:
                    want["stale:" + k] = ("警告", "%s 已 %d 分钟没有新周期（最后周期 %s）。分析仪可能停机或数据库不再更新。"
                                         % (ch["label"], (now - ch["changed"]) // 60, ch["cycle_time"] or "未知"))
            raised, cleared = [], []
            for key, (lv, text) in want.items():
                a = self.active.get(key)
                if a is None:
                    a = self.active[key] = Alert(key, lv, text, now)
                    raised.append(a)
                else:
                    a.text, a.level = text, lv          # 时长等文字随时更新，不算新告警
            dropped = []
            for key in [k for k in self.active if k not in want]:
                ch_key = key.split(":", 1)[1] if ":" in key else None
                if ch_key and self._muted(ch_key):
                    dropped.append(self.active.pop(key))      # 被屏蔽：静默移除，不发"恢复"
                else:
                    cleared.append(self.active.pop(key))
            for a in raised:
                self._remember("出现", a, now)
            for a in cleared:
                self._remember("恢复", a, now)
            for a in dropped:
                self._remember("屏蔽", a, now)
        for a in raised:
            if self.log:
                (self.log.error if a.level == "严重" else self.log.warning)("告警[%s]：%s", a.level, a.text)
        for a in cleared:
            if self.log:
                self.log.info("告警恢复：%s", a.text)
        if dropped and self.log:
            for a in dropped:
                self.log.info("通道已屏蔽告警，移除：%s", a.text)
        if dropped and self.notifier is not None and hasattr(self.notifier, "forget"):
            self.notifier.forget([a.key for a in dropped])
        if (raised or cleared) and self.notifier is not None:
            self.notifier(raised, cleared)
        return raised, cleared

    def _remember(self, kind, a, now):
        self.history.append({"time": _now_text(now), "event": kind, "level": a.level, "text": a.text})
        del self.history[:-self.HISTORY]

    def _channel_list(self):
        """给网页的通道状态表（调用方已持锁）。"""
        now = self.clock()
        out = []
        for k, ch in sorted(self.channels.items(), key=lambda kv: kv[1]["label"]):
            r = self._rule(k)
            out.append({"key": k, "label": ch["label"], "cycle_time": ch["cycle_time"],
                        "present": ch["present"], "idle_min": int((now - ch["changed"]) // 60),
                        "muted": bool(r and r.get("mute")),
                        "stale_minutes": (r or {}).get("stale_minutes"),
                        "until": (r or {}).get("until", ""), "note": (r or {}).get("note", ""),
                        "alerting": any(a.key.endswith(":" + k) for a in self.active.values())})
        return out

    def active_alerts(self):
        with self._lock:
            return list(self.active.values())

    # ---- 给 OPC UA / 网页 ----
    def snapshot(self):
        q = quiet_status(self.cfg.get("alert_quiet") or [], self.clock())
        with self._lock:
            act = sorted(self.active.values(), key=lambda a: (LEVELS.index(a.level), a.since))
            return {
                "active": [a.as_dict() for a in act],
                "history": list(reversed(self.history[-50:])),
                "last_scan": _now_text(self.last_scan) if self.last_scan else "",
                "channels": self.channel_count,
                "scan_errors": self.scan_errors,
                "worst": act[0].level if act else "正常",
                "quiet": q,
                "channel_list": self._channel_list(),
                "quiet_list": [dict(it, text=describe_quiet(it)) for it in (self.cfg.get("alert_quiet") or [])],
            }


# ---------------------------------------------------------------------------
# Webhook 推送（可选）
# ---------------------------------------------------------------------------

def _payload(kind, text):
    """按常见机器人格式组包：企业微信 / 钉钉 用 msgtype=text，飞书用 msg_type=text，其它发通用 JSON。"""
    if kind in ("wecom", "dingtalk"):
        return {"msgtype": "text", "text": {"content": text}}
    if kind == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    return {"source": "ChromExtractOPC", "text": text}


def guess_kind(url):
    u = (url or "").lower()
    if "qyapi.weixin.qq.com" in u:
        return "wecom"
    if "oapi.dingtalk.com" in u:
        return "dingtalk"
    if "feishu.cn" in u or "larksuite.com" in u:
        return "feishu"
    return "json"


def make_webhook_notifier(cfg, log=None, opener=None):
    """返回 notifier(raised, cleared)。在后台线程发送，网络慢不会拖住扫描；只允许 https。
    钉钉机器人需在机器人"安全设置"里加关键词"色谱"，或改用 IP 白名单。"""
    import urllib.request

    def send(text):
        url = str(cfg.get("alert_webhook") or "").strip()
        if not url:
            return
        if not url.lower().startswith("https://"):
            if log:
                log.error("告警推送地址必须以 https:// 开头，已忽略：%s", url[:60])
            return
        body = json.dumps(_payload(guess_kind(url), text), ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"},
                                     method="POST")
        try:
            with (opener or urllib.request.urlopen)(req, timeout=10) as r:
                r.read(2048)
        except Exception as e:  # noqa
            if log:
                log.warning("告警推送失败：%s", e)

    def notifier(raised, cleared):
        if not str(cfg.get("alert_webhook") or "").strip():
            return
        name = cfg.get("server_name") or "色谱提取 OPC UA 服务"
        lines = ["【%s】%s" % (name, _now_text())]
        lines += ["⚠ [%s] %s" % (a.level, a.text) for a in raised]
        lines += ["✓ 已恢复：%s" % a.text for a in cleared]
        threading.Thread(target=send, args=("\n".join(lines),), daemon=True).start()

    notifier.send = send          # 网页"发送测试消息"用
    return notifier


# ---------------------------------------------------------------------------
# 邮件通知（可选，SMTP，只用标准库）
# ---------------------------------------------------------------------------

import re as _re

EMAIL_RE = _re.compile(r"^[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+$")


def parse_recipients(text):
    """'a@x.com, b@y.com；c@z.com' -> 列表。格式不对抛 ValueError。"""
    out = []
    for p in _re.split(r"[,;，；\s]+", str(text or "")):
        p = p.strip()
        if not p:
            continue
        if not EMAIL_RE.match(p):
            raise ValueError("邮箱地址格式不对：%s" % p)
        if p not in out:
            out.append(p)
    if len(out) > 20:
        raise ValueError("收件人最多 20 个。")
    return out


def build_email(cfg, subject, body):
    from email.message import EmailMessage
    from email.utils import formatdate, make_msgid
    msg = EmailMessage()
    sender = cfg.get("smtp_from") or cfg.get("smtp_user")
    msg["From"] = sender
    msg["To"] = ", ".join(parse_recipients(cfg.get("alert_email_to")))
    msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")[:200]     # 防邮件头注入
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="chromextract.local")
    msg.set_content(body)
    return msg


def send_email(cfg, subject, body, smtp_factory=None, password=None):
    """同步发送一封邮件；失败抛异常（给"测试邮件"按钮直接显示原因）。"""
    import smtplib
    import ssl
    host = str(cfg.get("smtp_host") or "").strip()
    if not host:
        raise ValueError("没有填写 SMTP 服务器。")
    if not parse_recipients(cfg.get("alert_email_to")):
        raise ValueError("没有填写收件人。")
    if not (cfg.get("smtp_from") or cfg.get("smtp_user")):
        raise ValueError("没有填写发件人地址。")
    port = int(cfg.get("smtp_port") or 465)
    sec = str(cfg.get("smtp_security") or "ssl").lower()
    if password is None:
        import config_tools
        password = config_tools.unprotect_secret(cfg.get("smtp_password"))
    msg = build_email(cfg, subject, body)
    ctx = ssl.create_default_context()           # 校验服务器证书，防止被中间人截获密码
    if smtp_factory is not None:
        s = smtp_factory(host, port, sec, ctx)
    elif sec == "ssl":
        s = smtplib.SMTP_SSL(host, port, timeout=20, context=ctx)
    else:
        s = smtplib.SMTP(host, port, timeout=20)
    try:
        s.ehlo()
        if sec == "starttls":
            s.starttls(context=ctx)
            s.ehlo()
        if cfg.get("smtp_user"):
            if sec == "none":
                raise ValueError("未加密的连接不允许发送登录密码：请把加密方式改为 SSL 或 STARTTLS，或清空用户名（免登录中继）。")
            s.login(cfg["smtp_user"], password)
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:  # noqa
            pass


class EmailNotifier:
    """告警出现/恢复时发邮件。后台线程发送；每小时最多 MAX_PER_HOUR 封，防止告警反复跳变时刷爆邮箱。"""
    MAX_PER_HOUR = 20

    def __init__(self, cfg, log=None, clock=time.time, sender=None, background=True):
        self.cfg, self.log, self.clock = cfg, log, clock
        self.sender = sender or (lambda subj, body: send_email(self.cfg, subj, body))
        self.background = background
        self._sent = []
        self._dropped = 0
        self._lock = threading.Lock()

    def enabled(self):
        return bool(str(self.cfg.get("smtp_host") or "").strip() and str(self.cfg.get("alert_email_to") or "").strip())

    def __call__(self, raised, cleared):
        if not self.enabled():
            return
        now = self.clock()
        with self._lock:
            self._sent = [t for t in self._sent if now - t < 3600]
            if len(self._sent) >= self.MAX_PER_HOUR:
                self._dropped += 1
                if self._dropped == 1 and self.log:
                    self.log.warning("告警邮件 1 小时内已发 %d 封，暂停发送（告警仍记录在日志和网页上）", self.MAX_PER_HOUR)
                return
            self._sent.append(now)
            dropped, self._dropped = self._dropped, 0
        name = self.cfg.get("server_name") or "色谱提取 OPC UA 服务"
        worst = "严重" if any(a.level == "严重" for a in raised) else ("警告" if raised else "恢复")
        subj = "[%s] %s：%s" % (worst, name, (raised[0].text if raised else cleared[0].text)[:60])
        lines = ["服务：%s" % name, "时间：%s" % _now_text(), ""]
        if raised:
            lines.append("新出现的告警：")
            lines += ["  [%s] %s" % (a.level, a.text) for a in raised]
            lines.append("")
        if cleared:
            lines.append("已恢复：")
            lines += ["  %s" % a.text for a in cleared]
            lines.append("")
        if dropped:
            lines.append("（此前有 %d 次告警变化因发送过于频繁未发邮件，详见日志）" % dropped)
        lines.append("详情请查看设置页面的“运行告警”或 logs\\server.log。")
        body = "\n".join(lines)

        def go():
            try:
                self.sender(subj, body)
                if self.log:
                    self.log.info("告警邮件已发送：%s", subj)
            except Exception as e:  # noqa
                if self.log:
                    self.log.warning("告警邮件发送失败：%s", e)
        if self.background:
            threading.Thread(target=go, daemon=True).start()
        else:
            go()


LEVEL_CHOICES = ("all", "critical", "off")
WEEKDAYS = "一二三四五六日"      # 全部告警 / 只发严重 / 不发


def level_ok(choice, level):
    choice = str(choice or "all").lower()
    if choice == "off":
        return False
    if choice == "critical":
        return level == "严重"
    return True


def filter_for(choice, raised, cleared):
    """按渠道的级别设置过滤。恢复消息按告警"出现时的级别"判断：
    没通知过出现的渠道，也不会收到恢复（避免只收到"已恢复"却不知道出过什么事）。"""
    return ([a for a in raised if level_ok(choice, a.level)],
            [a for a in cleared if level_ok(choice, a.level)])


# ---------------------------------------------------------------------------
# 静默时段：计划停机/检修时不推送（日志、网页、OPC UA 照常显示全部告警）
#   一次性： {"type": "once",  "start": "2026-11-23 08:00", "end": "2026-11-23 18:00", "note": "检修"}
#   每天：   {"type": "daily", "start": "22:00", "end": "06:00", "days": [1,2,3,4,5], "note": "夜班"}
#            days 为星期几（1=周一 … 7=周日，空 = 每天）；end 早于 start 表示跨过午夜。
# 时间均为本机时间。
# ---------------------------------------------------------------------------

def _parse_dt(s):
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("日期时间格式应为 2026-11-23 08:00，当前为：%r" % s)


def _parse_hm(s):
    try:
        t = datetime.strptime(str(s).strip(), "%H:%M")
    except ValueError:
        raise ValueError("时间格式应为 时:分，例如 22:00，当前为：%r" % s)
    return t.hour * 60 + t.minute


def normalize_quiet(items):
    """校验并规整静默时段列表；不合法抛 ValueError（给人看的原因）。"""
    if items in (None, ""):
        return []
    if not isinstance(items, list):
        raise ValueError("静默时段必须是列表")
    if len(items) > 50:
        raise ValueError("静默时段最多 50 条")
    out = []
    for n, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise ValueError("第 %d 条静默时段格式不对" % n)
        typ = str(it.get("type") or "once").lower()
        note = str(it.get("note") or "").replace("\r", " ").replace("\n", " ").strip()[:60]
        try:
            if typ == "once":
                a, b = _parse_dt(it.get("start")), _parse_dt(it.get("end"))
                if b <= a:
                    raise ValueError("结束时间必须晚于开始时间")
                if (b - a).days > 31:
                    raise ValueError("一次静默最长 31 天，防止忘记关闭")
                out.append({"type": "once", "start": a.strftime("%Y-%m-%d %H:%M"),
                            "end": b.strftime("%Y-%m-%d %H:%M"), "note": note})
            elif typ == "daily":
                a, b = _parse_hm(it.get("start")), _parse_hm(it.get("end"))
                if a == b:
                    raise ValueError("开始和结束时间不能相同")
                days = sorted({int(d) for d in (it.get("days") or [])})
                if any(d < 1 or d > 7 for d in days):
                    raise ValueError("星期只能是 1～7")
                out.append({"type": "daily", "start": "%02d:%02d" % divmod(a, 60),
                            "end": "%02d:%02d" % divmod(b, 60), "days": days, "note": note})
            else:
                raise ValueError("类型只能是 once 或 daily")
        except ValueError as e:
            raise ValueError("第 %d 条静默时段：%s" % (n, e))
    return out


def quiet_status(items, ts=None):
    """返回 None（不在静默中）或 {"until": 文字, "note": 备注}。"""
    now = datetime.fromtimestamp(ts if ts is not None else time.time())
    for it in items or []:
        try:
            if it["type"] == "once":
                a, b = _parse_dt(it["start"]), _parse_dt(it["end"])
                if a <= now < b:
                    return {"until": it["end"], "note": it.get("note", "")}
            else:
                a, b = _parse_hm(it["start"]), _parse_hm(it["end"])
                m = now.hour * 60 + now.minute
                days = it.get("days") or []
                wd = now.isoweekday()
                if a < b:
                    hit = a <= m < b and (not days or wd in days)
                else:                                   # 跨午夜：22:00-06:00
                    if m >= a:
                        hit = not days or wd in days
                    elif m < b:
                        prev = 7 if wd == 1 else wd - 1   # 凌晨这段属于前一天开始的时段
                        hit = not days or prev in days
                    else:
                        hit = False
                if hit:
                    return {"until": it["end"], "note": it.get("note", "")}
        except Exception:  # noqa  已校验过，异常条目直接忽略
            continue
    return None


def normalize_channel_rules(items):
    """通道告警规则：[{"channel": 通道键, "mute": true/false, "stale_minutes": 数字或空,
    "until": "2026-12-01 00:00" 或空（到期自动失效）, "note": 备注}]"""
    if items in (None, ""):
        return []
    if not isinstance(items, list) or len(items) > 500:
        raise ValueError("通道规则必须是列表，最多 500 条")
    out, seen = [], set()
    for n, it in enumerate(items, 1):
        try:
            if not isinstance(it, dict):
                raise ValueError("格式不对")
            ch = str(it.get("channel") or "").strip()
            if not ch or len(ch) > 400 or any(c in ch for c in "\r\n"):
                raise ValueError("缺少通道")
            if ch in seen:
                raise ValueError("同一通道重复")
            seen.add(ch)
            mute = bool(it.get("mute"))
            sm = it.get("stale_minutes")
            if sm in ("", None):
                sm = None
            else:
                sm = int(sm)
                if not 0 <= sm <= 100000:
                    raise ValueError("无新周期分钟数应为 0～100000")
            until = str(it.get("until") or "").strip()
            if until:
                until = _parse_dt(until).strftime("%Y-%m-%d %H:%M")
            if not mute and sm is None:
                raise ValueError("既没屏蔽也没设置分钟数，这条规则没有作用")
            note = str(it.get("note") or "").replace("\r", " ").replace("\n", " ").strip()[:60]
            out.append({"channel": ch, "mute": mute, "stale_minutes": sm, "until": until, "note": note})
        except (ValueError, TypeError) as e:
            raise ValueError("第 %d 条通道规则：%s" % (n, e))
    return out


def describe_quiet(it):
    if it["type"] == "once":
        s = "%s ～ %s" % (it["start"], it["end"])
    else:
        days = it.get("days") or []
        s = "每天" if not days else "每周" + "、".join(WEEKDAYS[d - 1] for d in days)
        s += " %s ～ %s" % (it["start"], it["end"])
    return s + ("（%s）" % it["note"] if it.get("note") else "")


def make_notifier(cfg, log=None, hook=None, mail=None, clock=time.time):
    """组合所有告警出口：Webhook + 邮件，各自按级别过滤，并遵守静默时段。
      - 静默中：不推送，告警照常记录；
      - 静默结束（或告警出现时正好不在静默）：把仍未恢复、且该渠道还没通知过的告警补发一次；
      - 恢复通知只发给通知过该告警的渠道（静默中出现又在静默中恢复的，不打扰任何人）。
    级别、静默时段都从 cfg 实时读取，网页上改完即生效。"""
    hook = hook or make_webhook_notifier(cfg, log)
    mail = mail or EmailNotifier(cfg, log)
    routes = (("webhook", hook, "alert_webhook_level"), ("email", mail, "alert_email_level"))
    notified = {name: {} for name, _, _ in routes}      # 渠道 -> {告警 key: Alert}
    lock = threading.Lock()
    state = {"quiet": None}

    def _quiet():
        q = quiet_status(cfg.get("alert_quiet") or [], clock())
        if (q is None) != (state["quiet"] is None) and log:
            if q:
                log.info("进入告警静默时段（至 %s%s）：期间不推送，告警照常记录", q["until"],
                         "，" + q["note"] if q["note"] else "")
            else:
                log.info("告警静默时段结束")
        state["quiet"] = q
        return q

    def _dispatch(raised, cleared, catch_up=False):
        quiet = _quiet()
        for name, fn, key in routes:
            level = cfg.get(key, "all")
            with lock:
                sent = notified[name]
                r = [] if quiet else [a for a in raised if level_ok(level, a.level) and a.key not in sent]
                c = [a for a in cleared if a.key in sent] if not quiet else []
                for a in r:
                    sent[a.key] = a
                for a in c:
                    sent.pop(a.key, None)
            if not (r or c):
                continue
            try:
                fn(r, c)
            except Exception as e:  # noqa
                if log:
                    log.warning("告警推送出错：%s", e)

    def notifier(raised, cleared):
        _dispatch(raised, cleared)

    def tick(active):
        """定时调用（传入当前所有未恢复的告警）：静默结束后补发——
        仍未恢复且没通知过的告警补发"出现"；静默前通知过、静默期间已恢复的补发"恢复"。"""
        active = list(active)
        keys = {a.key for a in active}
        with lock:
            gone = {k: a for sent in notified.values() for k, a in sent.items() if k not in keys}
        if gone and getattr(notifier, "is_muted", None):
            muted = [k for k in gone if notifier.is_muted(k)]
            if muted:
                forget(muted)
                for k in muted:
                    gone.pop(k, None)
        _dispatch(active, list(gone.values()), catch_up=True)

    def forget(keys):
        """通道被屏蔽：忘掉这些告警，之后不会给它们补发"恢复"。"""
        with lock:
            for sent in notified.values():
                for k in keys:
                    sent.pop(k, None)

    notifier.tick = tick
    notifier.forget = forget
    notifier.quiet = _quiet
    notifier.webhook, notifier.email = hook, mail
    return notifier
