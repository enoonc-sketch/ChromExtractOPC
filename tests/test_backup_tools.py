# -*- coding: utf-8 -*-
"""v3.1 配置备份与还原（backup_tools.py）的测试：

往返导出还原、加密 / 错误密码、篡改哈希、路径穿越 / 绝对路径、清单外文件、版本兼容、
跨机 smtp_password 清空、自动备份与保留个数、校验失败不动文件、写入中途失败回滚、信任列表以备份为准。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import backup_tools as bt      # noqa: E402
import chrom_core              # noqa: E402
import config_tools as ct      # noqa: E402
import server_app as sa        # noqa: E402

KEY_PEM = b"-----BEGIN PRIVATE KEY-----\nMIIBVQ==\n-----END PRIVATE KEY-----\n"


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def _read(path):
    with open(path, "rb") as f:
        return f.read()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "app")
        self.cfg_path = os.path.join(self.base, "config.json")
        cfg = {"poll_seconds": 45, "users": {"admin": ct.hash_password("Abcdef12!")},
               "smtp_password": "dpapi:QUJDREVG", "web_port": 8841}
        _write(self.cfg_path, json.dumps(cfg, ensure_ascii=False))
        _write(os.path.join(self.base, "certs", "server_cert.der"), b"\x30\x82CERT")
        _write(os.path.join(self.base, "certs", "server_key.pem"), KEY_PEM)
        _write(os.path.join(self.base, "certs", "web_cert.pem"), b"-----BEGIN CERTIFICATE-----\nX\n")
        _write(os.path.join(self.base, "certs", "web_key.pem"), KEY_PEM)
        _write(os.path.join(self.base, "certs", "trusted", "scada.der"), b"\x30\x82TRUST")
        _write(os.path.join(self.base, "logs", "server.log"), "日志一行\n")
        self.validate = bt.make_validator(sa, self.base)
        self.bak = os.path.join(self.tmp, "out", "b.zip")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def export(self, **kw):
        kw.setdefault("machine", "M1")
        kw.setdefault("hostname", "PC-A")
        return bt.export_backup(self.bak, self.base, self.cfg_path, **kw)


class ExportTests(_Base):
    def test_manifest_and_files(self):
        r = self.export(include_logs=True)
        self.assertFalse(r["encrypted"])
        self.assertIn("certs/server_key.pem", r["private_keys"])
        self.assertIn("certs/web_key.pem", r["private_keys"])
        self.assertTrue(any("DPAPI" in w for w in r["warnings"]))
        with zipfile.ZipFile(self.bak) as z:
            m = json.loads(z.read("manifest.json"))
            names = set(z.namelist())
        self.assertEqual(m["app_version"], chrom_core.__version__)
        self.assertEqual(m["hostname"], "PC-A")
        self.assertEqual(m["machine_id"], "M1")
        self.assertIn("created", m)
        paths = {f["path"] for f in m["files"]}
        for p in ("config.json", "certs/server_cert.der", "certs/server_key.pem", "certs/web_cert.pem",
                  "certs/trusted/scada.der", "logs/server.log"):
            self.assertIn(p, paths)
            self.assertIn(p, names)
        for f in m["files"]:
            self.assertEqual(len(f["sha256"]), 64)

    def test_logs_optional(self):
        self.export()
        m = bt.read_manifest(self.bak)
        self.assertFalse(any(f["path"].startswith("logs/") for f in m["files"]))

    def test_encrypted_package_hides_content(self):
        self.export(password="Secret#123")
        with zipfile.ZipFile(self.bak) as z:
            self.assertEqual(sorted(z.namelist()), ["manifest.json", "payload.bin"])
            outer = json.loads(z.read("manifest.json"))
            blob = z.read("payload.bin")
        self.assertTrue(outer["encrypted"])
        self.assertNotIn("files", outer)
        self.assertEqual(outer["kdf"]["name"], "pbkdf2_sha256")
        self.assertGreaterEqual(outer["kdf"]["iterations"], 100000)
        self.assertNotIn(b"PRIVATE KEY", blob)
        self.assertNotIn(b"pbkdf2_sha256$", blob)

    def test_trusted_outside_cert_dir(self):
        raw = ct.read_raw(self.cfg_path)
        raw["trusted_client_certs_dir"] = "clients"
        ct.write_atomic(self.cfg_path, raw)
        _write(os.path.join(self.base, "clients", "dcs.der"), b"\x30DCS")
        self.export()
        paths = {f["path"] for f in bt.read_manifest(self.bak)["files"]}
        self.assertIn("trusted/dcs.der", paths)


class RestoreTests(_Base):
    def _modify_local(self):
        raw = ct.read_raw(self.cfg_path)
        raw["poll_seconds"] = 99
        ct.write_atomic(self.cfg_path, raw)
        _write(os.path.join(self.base, "certs", "server_key.pem"), b"NEW KEY")

    def test_round_trip_same_machine(self):
        self.export()
        self._modify_local()
        info = bt.inspect_backup(self.bak, self.base, self.cfg_path, machine="M1")
        acts = {a[1]: a[0] for a in info["actions"]}
        self.assertEqual(acts["config.json"], "覆盖")
        self.assertEqual(acts["certs/server_key.pem"], "覆盖")
        self.assertFalse(info["cross_machine"])
        r = bt.restore_backup(self.bak, self.base, self.cfg_path, validate=self.validate, machine="M1")
        self.assertTrue(r["restart_needed"])
        raw = ct.read_raw(self.cfg_path)
        self.assertEqual(raw["poll_seconds"], 45)
        self.assertEqual(raw["smtp_password"], "dpapi:QUJDREVG")       # 同一台电脑保留
        self.assertEqual(_read(os.path.join(self.base, "certs", "server_key.pem")), KEY_PEM)
        self.assertTrue(r["auto_backup"] and os.path.isfile(r["auto_backup"]))
        # 自动备份里是还原前的状态
        m, files = bt.load_backup(r["auto_backup"])
        self.assertEqual(json.loads(files["config.json"])["poll_seconds"], 99)
        self.assertEqual(files["certs/server_key.pem"], b"NEW KEY")

    def test_round_trip_encrypted(self):
        self.export(password="Secret#123")
        self._modify_local()
        with self.assertRaises(bt.PasswordRequired):
            bt.inspect_backup(self.bak, self.base, self.cfg_path, machine="M1")
        bt.restore_backup(self.bak, self.base, self.cfg_path, password="Secret#123",
                          validate=self.validate, machine="M1")
        self.assertEqual(ct.read_raw(self.cfg_path)["poll_seconds"], 45)

    def test_wrong_password(self):
        self.export(password="Secret#123")
        before = _read(self.cfg_path)
        with self.assertRaises(bt.WrongPassword):
            bt.restore_backup(self.bak, self.base, self.cfg_path, password="wrong-pass",
                              validate=self.validate, machine="M1")
        self.assertEqual(_read(self.cfg_path), before)

    def test_tampered_outer_manifest_breaks_decryption(self):
        self.export(password="Secret#123")
        with zipfile.ZipFile(self.bak) as z:
            outer = json.loads(z.read("manifest.json"))
            payload = z.read("payload.bin")
        outer["hostname"] = "EVIL"
        with zipfile.ZipFile(self.bak, "w") as z:
            z.writestr("manifest.json", json.dumps(outer))
            z.writestr("payload.bin", payload)
        with self.assertRaises(bt.WrongPassword):
            bt.load_backup(self.bak, "Secret#123")

    def test_cross_machine_clears_smtp(self):
        self.export()
        info = bt.inspect_backup(self.bak, self.base, self.cfg_path, machine="OTHER")
        self.assertTrue(info["cross_machine"] and info["smtp_cleared"])
        self.assertTrue(any("SMTP" in w for w in info["warnings"]))
        r = bt.restore_backup(self.bak, self.base, self.cfg_path, validate=self.validate, machine="OTHER")
        self.assertTrue(r["smtp_cleared"])
        raw = ct.read_raw(self.cfg_path)
        self.assertEqual(raw["smtp_password"], "")
        self.assertEqual(raw["poll_seconds"], 45)
        self.assertIn("admin", raw["users"])               # 账号哈希照常还原

    def test_validation_failure_leaves_files_untouched(self):
        raw = ct.read_raw(self.cfg_path)
        raw["poll_seconds"] = "abc"                         # normalize_config 会拒绝
        ct.write_atomic(self.cfg_path, raw)
        self.export()
        good = {"poll_seconds": 30}
        ct.write_atomic(self.cfg_path, good)
        before = _read(self.cfg_path)
        key_before = _read(os.path.join(self.base, "certs", "server_key.pem"))
        with self.assertRaises(bt.BackupError):
            bt.restore_backup(self.bak, self.base, self.cfg_path, validate=self.validate, machine="M1")
        self.assertEqual(_read(self.cfg_path), before)
        self.assertEqual(_read(os.path.join(self.base, "certs", "server_key.pem")), key_before)

    def test_rollback_when_write_fails_midway(self):
        self.export()
        _write(os.path.join(self.base, "certs", "trusted", "extra.der"), b"EXTRA")
        os.remove(os.path.join(self.base, "certs", "web_cert.pem"))
        self._modify_local()
        before_cfg = _read(self.cfg_path)
        calls = []

        def boom(dst):
            calls.append(dst)
            if len(calls) == 3:
                raise OSError("磁盘已满（模拟）")
        with self.assertRaises(bt.BackupError) as cm:
            bt.restore_backup(self.bak, self.base, self.cfg_path, validate=self.validate, machine="M1",
                              _after_write=boom)
        self.assertIn("回滚", str(cm.exception))
        self.assertEqual(_read(self.cfg_path), before_cfg)
        self.assertEqual(_read(os.path.join(self.base, "certs", "server_key.pem")), b"NEW KEY")
        self.assertFalse(os.path.exists(os.path.join(self.base, "certs", "web_cert.pem")))   # 新增的被撤销
        self.assertTrue(os.path.exists(os.path.join(self.base, "certs", "trusted", "extra.der")))

    def test_rollback_when_disk_config_invalid_after_write(self):
        self.export()
        self._modify_local()
        before = _read(self.cfg_path)
        state = {"n": 0}

        def validate(cfg):
            state["n"] += 1
            if state["n"] >= 2:                 # 写入后的第二次校验失败
                raise sa.ConfigError("模拟：写入后校验失败")
            return self.validate(cfg)
        with self.assertRaises(bt.BackupError):
            bt.restore_backup(self.bak, self.base, self.cfg_path, validate=validate, machine="M1")
        self.assertEqual(_read(self.cfg_path), before)

    def test_trusted_list_replaced_by_backup(self):
        self.export()
        _write(os.path.join(self.base, "certs", "trusted", "intruder.der"), b"X")
        info = bt.inspect_backup(self.bak, self.base, self.cfg_path, machine="M1")
        self.assertIn(("删除", "", os.path.join(self.base, "certs", "trusted", "intruder.der")), info["actions"])
        bt.restore_backup(self.bak, self.base, self.cfg_path, validate=self.validate, machine="M1")
        self.assertFalse(os.path.exists(os.path.join(self.base, "certs", "trusted", "intruder.der")))
        self.assertTrue(os.path.exists(os.path.join(self.base, "certs", "trusted", "scada.der")))

    def test_auto_backup_keeps_last_n(self):
        self.export()
        for i in range(5):
            bt.restore_backup(self.bak, self.base, self.cfg_path, validate=self.validate, machine="M1", keep=3,
                              now=datetime(2026, 11, 26, 10, 0, i))
        self.assertEqual(len(bt.list_auto_backups(self.base)), 3)

    def test_logs_restored_to_separate_folder(self):
        self.export(include_logs=True)
        info = bt.inspect_backup(self.bak, self.base, self.cfg_path, include_logs=True, machine="M1",
                                 now=datetime(2026, 11, 26, 9, 0, 0))
        dst = info["targets"]["logs/server.log"]
        self.assertIn("restored_20261126_090000", dst)


class TamperTests(_Base):
    def _repack(self, mutate):
        with zipfile.ZipFile(self.bak) as z:
            items = {n: z.read(n) for n in z.namelist()}
        mutate(items)
        with zipfile.ZipFile(self.bak, "w") as z:
            for n, data in items.items():
                z.writestr(n, data)

    def test_tampered_file_hash(self):
        self.export()

        def mut(items):
            items["config.json"] = items["config.json"].replace(b"45", b"46")
        self._repack(mut)
        with self.assertRaises(bt.BackupError) as cm:
            bt.load_backup(self.bak)
        self.assertIn("SHA-256", str(cm.exception))

    def test_extra_file_not_in_manifest(self):
        self.export()

        def mut(items):
            items["certs/evil.pem"] = b"x"
        self._repack(mut)
        with self.assertRaises(bt.BackupError):
            bt.load_backup(self.bak)

    def test_missing_file(self):
        self.export()
        self._repack(lambda items: items.pop("certs/server_cert.der"))
        with self.assertRaises(bt.BackupError):
            bt.load_backup(self.bak)

    def _evil(self, name):
        self.export()

        def mut(items):
            m = json.loads(items["manifest.json"])
            m["files"].append({"path": name, "size": 1, "sha256": bt.sha256_bytes(b"x")})
            items["manifest.json"] = json.dumps(m).encode()
            items[name] = b"x"
        self._repack(mut)
        before = _read(self.cfg_path)
        with self.assertRaises(bt.BackupError) as cm:
            bt.restore_backup(self.bak, self.base, self.cfg_path, validate=self.validate, machine="M1")
        self.assertEqual(_read(self.cfg_path), before)
        return str(cm.exception)

    def test_path_traversal_rejected(self):
        self.assertIn("不安全", self._evil("certs/../../evil.txt"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_absolute_path_rejected(self):
        self.assertIn("不安全", self._evil("/etc/evil"))

    def test_drive_letter_and_backslash_rejected(self):
        self.assertIsNotNone(bt.check_member_name("C:/Windows/evil.dll"))
        self.assertIsNotNone(bt.check_member_name("certs\\..\\evil"))
        self.assertIsNotNone(bt.check_member_name("other/file"))
        self.assertIsNone(bt.check_member_name("certs/trusted/a.der"))

    def test_newer_version_rejected(self):
        self.export()

        def mut(items):
            m = json.loads(items["manifest.json"])
            m["app_version"] = "99.0"
            items["manifest.json"] = json.dumps(m).encode()
        self._repack(mut)
        with self.assertRaises(bt.BackupError) as cm:
            bt.load_backup(self.bak)
        self.assertIn("更新的程序版本", str(cm.exception))

    def test_newer_format_rejected(self):
        self.export()

        def mut(items):
            m = json.loads(items["manifest.json"])
            m["format"] = 99
            items["manifest.json"] = json.dumps(m).encode()
        self._repack(mut)
        with self.assertRaises(bt.BackupError):
            bt.load_backup(self.bak)

    def test_not_a_zip(self):
        _write(self.bak, b"not a zip")
        with self.assertRaises(bt.BackupError):
            bt.load_backup(self.bak)

    def test_size_limit(self):
        self.export()
        old = bt.LIMITS["total"]
        bt.LIMITS["total"] = 10
        try:
            with self.assertRaises(bt.BackupError) as cm:
                bt.load_backup(self.bak)
            self.assertIn("太大", str(cm.exception))
        finally:
            bt.LIMITS["total"] = old

    def test_summary_text(self):
        self.export()
        info = bt.inspect_backup(self.bak, self.base, self.cfg_path, machine="OTHER")
        text = bt.summary_text(info)
        self.assertIn("PC-A", text)
        self.assertIn("覆盖", text)


if __name__ == "__main__":
    unittest.main()
