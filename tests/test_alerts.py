# -*- coding: utf-8 -*-
"""异常告警测试：出现/恢复只通知一次、各类告警能触发和恢复、推送格式、配置校验。"""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import alerts                 # noqa: E402
import server_app as sa       # noqa: E402


def rec(cid, src="a_V3.db"):
    return {"source": src, "app": 1, "stream": 2, "det": 1, "cycle_id": cid, "cycle_time": "t"}


class Monitor(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.sent = []
        self.cfg = {"poll_seconds": 30, "alert_stale_minutes": 60, "alert_scan_errors": 3}
        self.m = alerts.AlertMonitor(self.cfg, clock=lambda: self.t[0],
                                     notifier=lambda r, c: self.sent.append((len(r), len(c))))

    def test_stale_raise_once_then_clear(self):
        self.m.on_scan([rec(1)])
        self.assertFalse(self.m.active)
        self.t[0] += 3601
        self.m.on_scan([rec(1)])
        self.m.on_scan([rec(1)])
        self.assertEqual(self.sent, [(1, 0)])            # 不会每轮重复通知
        self.m.on_scan([rec(2)])                          # 新周期到来 -> 恢复
        self.assertEqual(self.sent[-1], (0, 1))
        self.assertFalse(self.m.active)

    def test_stale_disabled(self):
        self.cfg["alert_stale_minutes"] = 0
        self.m.on_scan([rec(1)])
        self.t[0] += 10 ** 6
        self.m.on_scan([rec(1)])
        self.assertFalse([k for k in self.m.active if k.startswith("stale")])

    def test_missing_and_no_data(self):
        self.m.on_scan([rec(1)])
        self.m.on_scan([])
        self.assertIn("no_data", self.m.active)
        self.assertTrue(any(k.startswith("missing:") for k in self.m.active))

    def test_scan_errors_and_stuck(self):
        self.m.on_scan([rec(1)])
        for _ in range(3):
            self.m.on_scan_error("database is locked")
        self.assertEqual(self.m.active["scan_error"].level, "严重")
        self.t[0] += 600
        self.m.evaluate()                                  # 扫描线程不再调用时，由定时检查发现
        self.assertIn("scan_stuck", self.m.active)
        self.m.on_scan([rec(1)])
        self.assertFalse(self.m.active)

    def test_snapshot_worst(self):
        self.m.on_scan([])
        self.assertEqual(self.m.snapshot()["worst"], "警告")


class Webhook(unittest.TestCase):
    def test_payload_kinds(self):
        self.assertEqual(alerts.guess_kind("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=1"), "wecom")
        self.assertEqual(alerts.guess_kind("https://oapi.dingtalk.com/robot/send?access_token=1"), "dingtalk")
        self.assertEqual(alerts.guess_kind("https://open.feishu.cn/open-apis/bot/v2/hook/1"), "feishu")
        self.assertEqual(alerts._payload("feishu", "x"), {"msg_type": "text", "content": {"text": "x"}})

    def test_only_https(self):
        cfg = dict(sa.DEFAULT_CONFIG, alert_webhook="http://example.com/hook")
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(cfg, ROOT)

    def test_send_uses_opener(self):
        got = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b""

        def opener(req, timeout):
            got.append(req.data)
            return Resp()
        alerts.make_webhook_notifier({"alert_webhook": "https://oapi.dingtalk.com/robot/send"},
                                     opener=opener).send("色谱 测试")
        self.assertIn("色谱 测试", got[0].decode("utf-8"))


class _A:
    def __init__(self, level, text, key=None):
        self.level, self.text, self.key = level, text, key or text


class _FakeSMTP:
    def __init__(self, *a):
        self.calls = []

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self, context):
        self.calls.append("starttls")

    def login(self, u, p):
        self.calls.append(("login", u, p))

    def send_message(self, m):
        self.calls.append(("send", m["To"], m["Subject"]))

    def quit(self):
        self.calls.append("quit")


class Email(unittest.TestCase):
    CFG = {"smtp_host": "smtp.x.com", "smtp_port": 587, "smtp_security": "starttls",
           "smtp_user": "u@x.com", "alert_email_to": "a@x.com, b@y.cn", "server_name": "站A"}

    def test_recipients(self):
        self.assertEqual(alerts.parse_recipients("a@x.com；b@y.cn, a@x.com"), ["a@x.com", "b@y.cn"])
        for bad in ("a@x", "a@x.com\nBcc: e@z.com", "x"):
            with self.assertRaises(ValueError):
                alerts.parse_recipients(bad)

    def test_no_header_injection(self):
        m = alerts.build_email(self.CFG, "主题\r\nBcc: evil@z.com", "正文")
        self.assertNotIn("\n", m["Subject"])
        self.assertIsNone(m["Bcc"])

    def test_starttls_login_send(self):
        fs = _FakeSMTP()
        alerts.send_email(self.CFG, "s", "b", smtp_factory=lambda *a: fs, password="pw")
        self.assertEqual(fs.calls[:4], ["ehlo", "starttls", "ehlo", ("login", "u@x.com", "pw")])
        self.assertEqual(fs.calls[4][1], "a@x.com, b@y.cn")

    def test_never_login_over_plaintext(self):
        fs = _FakeSMTP()
        with self.assertRaises(ValueError):
            alerts.send_email(dict(self.CFG, smtp_security="none"), "s", "b",
                              smtp_factory=lambda *a: fs, password="pw")
        self.assertFalse([c for c in fs.calls if isinstance(c, tuple)])

    def test_rate_limit_and_summary(self):
        t, sent = [0.0], []
        n = alerts.EmailNotifier(self.CFG, clock=lambda: t[0], sender=lambda s, b: sent.append((s, b)),
                                 background=False)
        for i in range(25):
            n([_A("严重", "错误 %d" % i)], [])
        self.assertEqual(len(sent), alerts.EmailNotifier.MAX_PER_HOUR)
        t[0] += 3700
        n([], [_A("警告", "恢复了")])
        self.assertTrue(sent[-1][0].startswith("[恢复]"))
        self.assertIn("5 次", sent[-1][1])

    def test_disabled_without_host(self):
        sent = []
        alerts.EmailNotifier({"alert_email_to": "a@x.com"}, sender=lambda s, b: sent.append(1),
                             background=False)([_A("严重", "x")], [])
        self.assertFalse(sent)

    def test_config_validation(self):
        for bad in ({"smtp_security": "tls"}, {"alert_email_to": "not-an-email"},
                    {"smtp_user": "u@x.com", "smtp_security": "none"}, {"smtp_from": "bad"}):
            with self.assertRaises(sa.ConfigError):
                sa.normalize_config(dict(sa.DEFAULT_CONFIG, **bad), ROOT)


class Secret(unittest.TestCase):
    def test_roundtrip_and_hidden_from_web(self):
        import json, shutil, tempfile
        import config_tools as ct
        import web_settings as ws
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({}, f)
            ct.set_smtp_password(p, "授权码abc123")
            stored = ct.read_raw(p)["smtp_password"]
            self.assertNotIn("abc123", stored)
            self.assertTrue(stored.startswith("dpapi:" if os.name == "nt" else "b64:"))
            self.assertEqual(ct.unprotect_secret(stored), "授权码abc123")
            st = json.dumps(ws.SettingsServer(p, tmp, sa).state(), ensure_ascii=False)
            self.assertNotIn(stored, st)
            self.assertIn('"smtp_password_set": true', st)
            ct.set_smtp_password(p, "")
            self.assertEqual(ct.read_raw(p)["smtp_password"], "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_web_cannot_set_password_via_save(self):
        import config_tools as ct
        self.assertNotIn("smtp_password", ct.EDITABLE)


class Routing(unittest.TestCase):
    """按级别分配渠道：恢复只发给收到过该告警的渠道；改级别实时生效。"""

    def setUp(self):
        self.cfg = {"alert_webhook_level": "all", "alert_email_level": "critical"}
        self.h, self.m = [], []
        rec = lambda box: (lambda r, c: box.append(([a.text for a in r], [a.text for a in c])))
        self.n = alerts.make_notifier(self.cfg, hook=rec(self.h), mail=rec(self.m))

    def test_split_by_level(self):
        self.n([_A("警告", "stale"), _A("严重", "stuck")], [])
        self.assertEqual(self.h[-1], (["stale", "stuck"], []))
        self.assertEqual(self.m[-1], (["stuck"], []))

    def test_recovery_only_where_raised(self):
        # 先出现：webhook 收到（级别 all），邮件不收（只收严重）
        self.n([_A("警告", "stale")], [])
        self.assertEqual(self.h[-1], (["stale"], []))
        self.assertEqual(len(self.m), 0)
        # 再恢复：只发给收到过该告警的 webhook，邮件仍不收
        self.n([], [_A("警告", "stale")])
        self.assertEqual(len(self.m), 0)
        self.assertEqual(self.h[-1], ([], ["stale"]))

    def test_off_and_live_change(self):
        self.cfg["alert_webhook_level"] = "off"
        self.cfg["alert_email_level"] = "all"
        self.n([_A("警告", "x")], [])
        self.assertEqual(self.h, [])
        self.assertEqual(self.m[-1], (["x"], []))

    def test_config_validation(self):
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(dict(sa.DEFAULT_CONFIG, alert_email_level="warning"), ROOT)


def _ts(s):
    from datetime import datetime
    return datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()


class Quiet(unittest.TestCase):
    """静默时段：跨午夜、星期、一次性；静默中不推送，结束后补发；改动实时生效。2026-11-23 是周一。"""

    def test_daily_across_midnight(self):
        q = alerts.normalize_quiet([{"type": "daily", "start": "22:00", "end": "06:00", "days": [1]}])
        for when, hit in (("2026-11-23 23:00", True), ("2026-11-24 05:59", True),   # 周一晚上延续到周二凌晨
                          ("2026-11-24 06:00", False), ("2026-11-23 05:00", False),  # 周一凌晨属于周日的时段
                          ("2026-11-24 23:00", False)):
            self.assertEqual(alerts.quiet_status(q, _ts(when)) is not None, hit, when)

    def test_once_bounds(self):
        q = alerts.normalize_quiet([{"type": "once", "start": "2026-11-23 08:00", "end": "2026-11-23 18:00"}])
        self.assertIsNotNone(alerts.quiet_status(q, _ts("2026-11-23 08:00")))
        self.assertIsNone(alerts.quiet_status(q, _ts("2026-11-23 18:00")))

    def test_invalid(self):
        for bad in ([{"type": "once", "start": "2026-11-23 08:00", "end": "2026-11-23 07:00"}],
                    [{"type": "once", "start": "2026-11-01 00:00", "end": "2026-12-15 00:00"}],
                    [{"type": "daily", "start": "08:00", "end": "08:00"}],
                    [{"type": "daily", "start": "25:00", "end": "01:00"}],
                    [{"type": "daily", "start": "01:00", "end": "02:00", "days": [8]}],
                    [{"type": "weekly"}], "x"):
            with self.assertRaises(ValueError):
                alerts.normalize_quiet(bad)
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(dict(sa.DEFAULT_CONFIG, alert_quiet=[{"type": "x"}]), ROOT)

    def _setup(self, start):
        self.now = [_ts(start)]
        self.cfg = {"alert_webhook_level": "all", "alert_email_level": "critical",
                    "alert_quiet": alerts.normalize_quiet(
                        [{"type": "once", "start": "2026-11-23 08:00", "end": "2026-11-23 18:00"}])}
        self.h, self.m = [], []
        rec = lambda box: (lambda r, c: box.append(([a.key for a in r], [a.key for a in c])))
        self.n = alerts.make_notifier(self.cfg, hook=rec(self.h), mail=rec(self.m), clock=lambda: self.now[0])

    def test_silenced_then_catch_up(self):
        self._setup("2026-11-23 09:00")
        stuck, stale, blip = _A("严重", "卡", "stuck"), _A("警告", "旧", "stale"), _A("警告", "闪", "blip")
        self.n([stuck, stale, blip], [])
        self.n([], [blip])                                  # 静默中出现又恢复：谁都不打扰
        self.n.tick([stuck, stale])
        self.assertEqual((self.h, self.m), ([], []))
        self.now[0] = _ts("2026-11-23 18:00")
        self.n.tick([stuck, stale])
        self.assertEqual(self.h, [(["stuck", "stale"], [])])
        self.assertEqual(self.m, [(["stuck"], [])])         # 邮件仍然只发严重
        self.n.tick([stuck, stale])
        self.assertEqual(len(self.h), 1)                     # 不会重复补发

    def test_recovery_during_silence_sent_after(self):
        self._setup("2026-11-23 07:00")
        stuck = _A("严重", "卡", "stuck")
        self.n([stuck], [])                                  # 静默前已通知
        self.now[0] = _ts("2026-11-23 10:00")
        self.n([], [stuck])
        self.n.tick([])
        self.assertEqual(len(self.h), 1)                     # 静默中不发恢复
        self.now[0] = _ts("2026-11-23 18:01")
        self.n.tick([])
        self.assertEqual(self.h[-1], ([], ["stuck"]))        # 结束后补发恢复，接收方不会一直以为还在出错
        self.assertEqual(self.m[-1], ([], ["stuck"]))

    def test_web_quick_silence(self):
        import json, shutil, tempfile
        import config_tools as ct
        import web_settings as ws
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({}, f)
            s = ws.SettingsServer(p, tmp, sa)
            s.handle_action("quiet_now", {"hours": 2})
            self.assertIsNotNone(s.alerts()["quiet"])
            with self.assertRaises(ct.ConfigToolError):
                s.handle_action("quiet_now", {"hours": 100})
            s.handle_action("quiet_add", {"item": {"type": "daily", "start": "22:00", "end": "06:00"}})
            s.handle_action("quiet_end", {})
            left = ct.read_raw(p)["alert_quiet"]
            self.assertEqual([q["type"] for q in left], ["daily"])   # 只结束一次性的，每天的保留
            s.handle_action("quiet_remove", {"index": 0})
            self.assertEqual(ct.read_raw(p)["alert_quiet"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ChannelRules(unittest.TestCase):
    """按通道屏蔽告警 / 单独设置无新周期分钟数；到期自动解除；屏蔽时不发恢复。"""
    K1, K2 = "a_V3.db|1|1|1", "a_V3.db|1|2|1"

    def setUp(self):
        self.now = [_ts("2026-11-23 08:00")]
        self.cfg = {"poll_seconds": 30, "alert_stale_minutes": 60, "alert_scan_errors": 3,
                    "alert_channel_rules": [], "alert_webhook_level": "all", "alert_email_level": "off"}
        self.h = []
        self.n = alerts.make_notifier(self.cfg, hook=lambda r, c: self.h.append(([a.key for a in r], [a.key for a in c])),
                                      mail=lambda r, c: None, clock=lambda: self.now[0])
        self.m = alerts.AlertMonitor(self.cfg, clock=lambda: self.now[0], notifier=self.n)
        self.n.is_muted = lambda k: ":" in k and self.m._muted(k.split(":", 1)[1])

    def _r(self, cid, stream):
        return {"source": "a_V3.db", "app": 1, "stream": stream, "det": 1, "cycle_id": cid, "cycle_time": "t"}

    def _rules(self, *items):
        self.cfg["alert_channel_rules"] = alerts.normalize_channel_rules(list(items))

    def test_mute_removes_without_recovery(self):
        self.m.on_scan([self._r(1, 1), self._r(1, 2)])
        self.now[0] += 3601
        self.m.on_scan([self._r(1, 1), self._r(1, 2)])
        sent = len(self.h)
        self._rules({"channel": self.K1, "mute": True})
        self.m.evaluate()
        self.assertNotIn("stale:" + self.K1, self.m.active)
        self.assertEqual(len(self.h), sent)                    # 屏蔽不发"恢复"
        self.n.tick(self.m.active_alerts())
        self.assertEqual(len(self.h), sent)                    # 之后也不补发
        self.m.on_scan([self._r(1, 2)])                         # 屏蔽的通道消失也不告警
        self.assertNotIn("missing:" + self.K1, self.m.active)

    def test_custom_stale(self):
        self._rules({"channel": self.K2, "stale_minutes": 300})
        self.m.on_scan([self._r(1, 1), self._r(1, 2)])
        self.now[0] += 3601
        self.m.on_scan([self._r(1, 1), self._r(1, 2)])
        self.assertIn("stale:" + self.K1, self.m.active)
        self.assertNotIn("stale:" + self.K2, self.m.active)
        self.now[0] += 4 * 3600
        self.m.on_scan([self._r(1, 1), self._r(1, 2)])
        self.assertIn("stale:" + self.K2, self.m.active)

    def test_mute_expires(self):
        self._rules({"channel": self.K1, "mute": True, "until": "2026-11-23 09:00"})
        self.m.on_scan([self._r(1, 1)])
        self.m.on_scan([])
        self.assertNotIn("missing:" + self.K1, self.m.active)
        self.now[0] = _ts("2026-11-23 09:00")
        self.m.on_scan([])
        self.assertIn("missing:" + self.K1, self.m.active)    # 到期自动恢复告警

    def test_channel_list(self):
        self._rules({"channel": self.K1, "mute": True, "note": "停用"})
        self.m.on_scan([self._r(1, 1)])
        row = self.m.snapshot()["channel_list"][0]
        self.assertTrue(row["muted"])
        self.assertEqual(row["note"], "停用")

    def test_invalid(self):
        for bad in ([{"channel": self.K1}], [{"channel": self.K1, "mute": True}] * 2,
                    [{"channel": "", "mute": True}], [{"channel": self.K1, "stale_minutes": -1}],
                    [{"channel": self.K1, "mute": True, "until": "明天"}]):
            with self.assertRaises(ValueError):
                alerts.normalize_channel_rules(bad)

    def test_web_set_and_clear(self):
        import json, shutil, tempfile
        import config_tools as ct
        import web_settings as ws
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({}, f)
            s = ws.SettingsServer(p, tmp, sa)
            s.handle_action("channel_rule", {"rule": {"channel": self.K1, "mute": True, "until": "", "note": "x"}})
            self.assertTrue(ct.read_raw(p)["alert_channel_rules"][0]["mute"])
            self.assertTrue(s.alerts()["channel_list"][0]["muted"])          # 服务没运行也能看到规则
            s.handle_action("channel_rule", {"rule": {"channel": self.K1, "mute": False, "stale_minutes": ""}})
            self.assertEqual(ct.read_raw(p)["alert_channel_rules"], [])     # 取消屏蔽 = 删除规则
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
