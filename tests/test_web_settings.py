# -*- coding: utf-8 -*-
"""网页设置页测试：只改允许的项、校验失败不写文件、不泄露密码哈希、令牌和登录保护。"""
import json, os, shutil, sys, tempfile, unittest, urllib.request, urllib.error, base64
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import config_tools as ct      # noqa: E402
import server_app as sa        # noqa: E402
import web_settings as ws      # noqa: E402


class WebSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"poll_seconds": 30}, f)
        self.s = ws.SettingsServer(self.cfg, self.tmp, sa, port=0)

    def tearDown(self):
        self.s.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_only_changed_keys(self):
        self.s.handle_action("save", {"settings": {"poll_seconds": "60", "remote_export": False}})
        raw = ct.read_raw(self.cfg)
        self.assertEqual(raw, {"poll_seconds": 60, "remote_export": False})

    def test_invalid_rejected_file_untouched(self):
        before = open(self.cfg, "rb").read()
        for bad in ({"endpoint": "http://x"}, {"security": "abc"},
                    {"watch_dirs": [os.path.join(self.tmp, "nope")]}, {"users": {}},
                    {"allow_anonymous": False}):
            with self.assertRaises(ct.ConfigToolError):
                self.s.handle_action("save", {"settings": bad})
        self.assertEqual(open(self.cfg, "rb").read(), before)

    def test_users_never_leak_hash(self):
        self.s.handle_action("add_user", {"name": "dcs", "password": "Str0ng!pass"})
        st = json.dumps(self.s.state())
        self.assertIn("dcs", st)
        self.assertNotIn("pbkdf2", st)

    def test_http_token_required(self):
        self.s.start()
        self.assertEqual(urllib.request.urlopen(self.s.url() + "api/state").status, 200)
        rq = urllib.request.Request(self.s.url() + "api/save", data=b'{"settings":{}}',
                                    headers={"X-Token": "bad"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(rq)
        self.assertEqual(cm.exception.code, 403)

    def test_remote_host_requires_login(self):
        ct.add_user(self.cfg, "dcs", "Str0ng!pass")
        s = ws.SettingsServer(self.cfg, self.tmp, sa, host="0.0.0.0", port=0)
        self.assertTrue(s.need_login)
        self.assertFalse(s._check_auth(None))
        ok = "Basic " + base64.b64encode(b"dcs:Str0ng!pass").decode()
        bad = "Basic " + base64.b64encode(b"dcs:wrong").decode()
        self.assertTrue(s._check_auth(ok))
        self.assertFalse(s._check_auth(bad))


class WebHttps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_auto_mode(self):
        self.assertFalse(ws.SettingsServer(self.cfg, self.tmp, sa).https)                 # 本机：不需要
        self.assertTrue(ws.SettingsServer(self.cfg, self.tmp, sa, host="0.0.0.0").https)  # 对外：自动开
        self.assertFalse(ws.SettingsServer(self.cfg, self.tmp, sa, host="0.0.0.0", https="off").https)

    def test_cert_generated_and_reused(self):
        d = os.path.join(self.tmp, "certs")
        c1, _ = ws.ensure_web_cert(d)
        fp1 = ws.cert_fingerprint(c1)
        c2, _ = ws.ensure_web_cert(d)
        self.assertEqual(fp1, ws.cert_fingerprint(c2))        # 没变化就不重新生成
        ws.ensure_web_cert(d, host="10.9.8.7")                 # 新地址不在证书里 -> 重新生成
        self.assertNotEqual(fp1, ws.cert_fingerprint(c1))

    def test_https_roundtrip(self):
        import ssl
        s = ws.SettingsServer(self.cfg, self.tmp, sa, port=0, https="on",
                              cert_dir=os.path.join(self.tmp, "certs"))
        s.start()
        try:
            self.assertTrue(s.url().startswith("https://"))
            ctx = ssl.create_default_context(cafile=os.path.join(self.tmp, "certs", "web_cert.pem"))
            with urllib.request.urlopen(s.url() + "api/state", context=ctx) as r:
                st = json.loads(r.read().decode())
            self.assertTrue(st["https"])
            self.assertTrue(st["fingerprint"])
            with self.assertRaises(Exception):                      # 明文 HTTP 访问不到
                urllib.request.urlopen(s.url().replace("https", "http") + "api/state", timeout=3)
        finally:
            s.stop()

    def test_bad_values_rejected(self):
        for bad in ({"web_https": "maybe"}, {"web_cert_file": "a.pem"}):
            cfg = dict(sa.DEFAULT_CONFIG, **bad)
            with self.assertRaises(sa.ConfigError):
                sa.normalize_config(cfg, self.tmp)

    def test_report_warns_plain_remote(self):
        cfg = dict(sa.DEFAULT_CONFIG, web_host="0.0.0.0", web_https="off", _base_dir=self.tmp)
        self.assertIn("风险", [lv for lv, _ in sa.security_report(cfg)])


class Layout(unittest.TestCase):
    """设置页面分成 4 个标签页；每个区块只出现一次；脚本引用的元素都存在。"""

    def test_tabs_and_sections(self):
        import re
        page = ws.PAGE
        for t in ("status", "alerts", "tools", "system"):
            self.assertIn('id="tab_%s"' % t, page)
        self.assertEqual(page.count("<section"), page.count("</section>"))
        for title in ("运行告警", "设备检测", "通道告警设置", "告警设置", "驱动与软件环境检测", "连接测试",
                      "数据源", "OPC UA 服务", "CSV 导出", "高级设置", "登录账号", "安全体检"):
            self.assertEqual(len(re.findall(r"<(?:h2|summary)>" + title, page)), 1, title)
        self.assertIn('id="savebar"', page)

    def test_js_ids_exist(self):
        import re
        page = ws.PAGE
        ids = set(re.findall(r'id="([A-Za-z_]\w*)"', page))
        used = set(re.findall(r'\$\("([A-Za-z_]\w*)"\)', page))
        missing = [x for x in used - ids if not x.startswith("nt_")]   # nt_ 开头的是动态生成的测试参数
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
