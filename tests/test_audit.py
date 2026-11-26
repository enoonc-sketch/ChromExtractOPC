# -*- coding: utf-8 -*-
"""v3.1 用户权限与操作审计（audit.py）测试：角色权限、登录 / 失败锁定、空闲锁定、首次创建管理员、
最后一个管理员不能降级、删除账号同时删除角色、敏感值打码、哈希链（改 / 删 / 插行都能发现）、查询与 CSV 导出、
网页设置页的修改也写审计。"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import audit                 # noqa: E402
import config_tools as ct    # noqa: E402
import server_app as sa      # noqa: E402
import web_settings          # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Roles(unittest.TestCase):
    def test_permissions(self):
        self.assertTrue(audit.allowed("operator", "view"))
        self.assertFalse(audit.allowed("operator", "config"))
        self.assertTrue(audit.allowed("engineer", "config"))
        self.assertTrue(audit.allowed("engineer", "export"))
        self.assertFalse(audit.allowed("engineer", "backup"))
        self.assertFalse(audit.allowed("engineer", "users"))
        self.assertTrue(audit.allowed("admin", "restore"))
        self.assertFalse(audit.allowed(None, "view"))
        self.assertTrue(audit.allowed("admin", "unknown-perm"))       # 没登记的操作只有管理员能做
        self.assertFalse(audit.allowed("engineer", "unknown-perm"))

    def test_normalize(self):
        self.assertEqual(audit.normalize_roles({"a": "Admin"}), {"a": "admin"})
        self.assertEqual(audit.normalize_roles({"a": "admin", "gone": "engineer"}, {"a": "h"}), {"a": "admin"})
        with self.assertRaises(ValueError):
            audit.normalize_roles({"a": "root"})
        with self.assertRaises(ValueError):
            audit.normalize_roles(["a"])

    def test_config_validation(self):
        cfg = sa.load_config(None)
        cfg.update(users={"a": ct.hash_password("Abcdef12!")}, user_roles={"a": "boss"})
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(cfg, ".")


class Sessions(unittest.TestCase):
    def setUp(self):
        self.cfg = {"users": {"eng": ct.hash_password("Engineer1!"), "op": ct.hash_password("Operator1!")},
                    "user_roles": {"eng": "engineer"}}
        self.clock = Clock()
        self.s = audit.Session(lambda: self.cfg, ct.verify_password, idle_minutes=15, clock=self.clock)

    def test_login_roles(self):
        self.assertEqual(self.s.effective_role, "operator")
        self.assertFalse(self.s.can("config"))
        self.assertEqual(self.s.login("eng", "Engineer1!"), "engineer")
        self.assertTrue(self.s.can("config"))
        self.assertFalse(self.s.can("backup"))
        self.assertEqual(self.s.login("op", "Operator1!"), "operator")      # 没写角色 = 操作员
        self.s.logout()
        self.assertIsNone(self.s.user)

    def test_wrong_password_and_lockout(self):
        for _ in range(5):
            with self.assertRaises(audit.AuthError):
                self.s.login("eng", "bad")
        with self.assertRaises(audit.AuthError) as cm:
            self.s.login("eng", "Engineer1!")
        self.assertIn("1 分钟", str(cm.exception))
        self.clock.t += 61
        self.assertEqual(self.s.login("eng", "Engineer1!"), "engineer")

    def test_idle_lock(self):
        self.s.login("eng", "Engineer1!")
        self.clock.t += 14 * 60
        self.assertFalse(self.s.idle_expired())
        self.s.touch()
        self.clock.t += 15 * 60
        self.assertTrue(self.s.idle_expired())
        self.s.idle_minutes = 0
        self.assertFalse(self.s.idle_expired())

    def test_auth_disabled(self):
        s = audit.Session(lambda: self.cfg, ct.verify_password, auth_enabled=False)
        self.assertEqual(s.effective_role, "admin")
        self.assertTrue(s.can("restore"))
        self.assertEqual(s.actor(), "desktop")


class AdminSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = os.path.join(self.tmp, "config.json")
        ct.write_atomic(self.p, {"users": {"old": ct.hash_password("OldPass12!")}})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_promote(self):
        self.assertFalse(audit.has_admin(ct.read_raw(self.p)))
        self.assertEqual(audit.create_admin(self.p, "boss", "Strong#Pw9", ct), "created")
        raw = ct.read_raw(self.p)
        self.assertTrue(audit.has_admin(raw))
        self.assertTrue(raw["users"]["boss"].startswith("pbkdf2_sha256$"))
        with self.assertRaises(audit.AuthError):
            audit.create_admin(self.p, "old", "wrong", ct)
        self.assertEqual(audit.create_admin(self.p, "old", "OldPass12!", ct), "promoted")

    def test_last_admin_cannot_be_demoted_and_remove_user_cleans_role(self):
        audit.create_admin(self.p, "boss", "Strong#Pw9", ct)
        with self.assertRaises(audit.AuthError):
            audit.set_role(self.p, "boss", "engineer", ct)
        audit.set_role(self.p, "old", "admin", ct)
        audit.set_role(self.p, "boss", "engineer", ct)
        ct.remove_user(self.p, "boss")
        self.assertNotIn("boss", ct.read_raw(self.p)["user_roles"])
        cfg = sa.load_config(self.p)
        sa.normalize_config(cfg, self.tmp)


class Masking(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(audit.mask("abc", "smtp_password"), "***")
        self.assertEqual(audit.mask({"a": "pbkdf2_sha256$x"}, "users"), {"a": "***"})
        self.assertEqual(audit.mask({"poll_seconds": 5, "password": "x"}), {"poll_seconds": 5, "password": "***"})
        self.assertTrue(audit.mask("x" * 1000, "note").endswith("…"))
        d = audit.diff({"poll_seconds": 30, "smtp_password": "a"}, {"poll_seconds": 60, "smtp_password": "b"})
        self.assertEqual(d, {"poll_seconds": [30, 60], "smtp_password": ["***", "***"]})


class Chain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "logs", "audit.log")
        self.log = audit.AuditLog(self.path)
        for i in range(5):
            self.log.record("eng", "save", "config", {"poll_seconds": 30}, {"poll_seconds": 30 + i},
                            detail="第 %d 次" % i)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _lines(self):
        with open(self.path, encoding="utf-8") as f:
            return f.readlines()

    def _write(self, lines):
        with open(self.path, "w", encoding="utf-8") as f:
            f.writelines(lines)

    def test_intact(self):
        ok, probs = self.log.verify_chain()
        self.assertTrue(ok, probs)
        self.assertEqual(len(self.log.read()), 5)
        # 新实例接着写也能接上链
        audit.AuditLog(self.path).record("x", "login")
        self.assertTrue(audit.AuditLog(self.path).verify_chain()[0])

    def test_modified_line(self):
        lines = self._lines()
        lines[2] = lines[2].replace('"user":"eng"', '"user":"admin"')
        self._write(lines)
        ok, probs = self.log.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(probs[0][0], 3)

    def test_deleted_line(self):
        lines = self._lines()
        del lines[1]
        self._write(lines)
        self.assertFalse(self.log.verify_chain()[0])

    def test_inserted_line(self):
        lines = self._lines()
        fake = json.loads(lines[1])
        fake["detail"] = "伪造"
        lines.insert(2, json.dumps(fake, ensure_ascii=False) + "\n")
        self._write(lines)
        self.assertFalse(self.log.verify_chain()[0])

    def test_query_and_export(self):
        self.log.record("op", "login", ok=False, detail="密码错误")
        self.assertEqual(len(self.log.query(user="op")), 1)
        self.assertEqual(len(self.log.query(action="save")), 5)
        self.assertEqual(len(self.log.query(text="第 3 次")), 1)
        p = os.path.join(self.tmp, "a.csv")
        self.log.export_csv(self.log.query(), p)
        with open(p, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[-1][7], "FAIL")

    def test_password_never_written(self):
        self.log.record("admin", "add_user", "users", None, {"name": "a", "password": "SuperSecret1!"})
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("SuperSecret1!", f.read())


class WebAudit(unittest.TestCase):
    """网页设置页（SettingsServer.handle_action）的修改也记审计。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = os.path.join(self.tmp, "config.json")
        ct.write_atomic(self.p, {"poll_seconds": 30})
        self.srv = web_settings.SettingsServer(self.p, self.tmp, sa)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_is_audited(self):
        self.srv.handle_action("save", {"settings": {"poll_seconds": 60}}, actor="web:alice")
        self.srv.handle_action("set_smtp_password", {"password": "Secret123"}, actor="web:alice")
        log = audit.get_log(os.path.join(self.tmp, "logs", "audit.log"))
        rows = log.query()
        self.assertEqual(rows[0]["user"], "alice")
        self.assertEqual(rows[0]["source"], "web")
        self.assertEqual(rows[0]["before"], {"poll_seconds": 30})
        self.assertEqual(rows[0]["after"], {"poll_seconds": 60})
        with open(os.path.join(self.tmp, "logs", "audit.log"), encoding="utf-8") as f:
            self.assertNotIn("Secret123", f.read())
        self.assertTrue(log.verify_chain()[0])

    def test_failed_action_is_audited(self):
        with self.assertRaises(Exception):
            self.srv.handle_action("save", {"settings": {"poll_seconds": "abc"}}, actor="desktop:eng")
        rows = audit.get_log(os.path.join(self.tmp, "logs", "audit.log")).query()
        self.assertFalse(rows[-1]["ok"])
        self.assertEqual(rows[-1]["source"], "desktop")


if __name__ == "__main__":
    unittest.main()
