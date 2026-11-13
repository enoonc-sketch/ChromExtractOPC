#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全回归测试：每一条都对应一次真实发现过的问题或一项防护措施。

运行：  python -m unittest discover -s tests -v
前提：  pip install asyncua cryptography
说明：  集成测试会真的启动服务（随机端口、临时目录），测完自动清理，不会改动项目目录。
"""
import asyncio
import json
import os
import random
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import chrom_core as cc          # noqa: E402
import server_app as sa          # noqa: E402
import config_tools as ct        # noqa: E402
from make_demo_db import build_trc   # noqa: E402


def _blob_with_n(n, payload_points=None):
    """构造一条声称有 n 个点的 BinTrc。payload_points 为实际携带的点数（默认等于 n）。"""
    k = n if payload_points is None else payload_points
    head = b"BinTrc\x00" + struct.pack("<I", 1) + b"Chan\x00" + struct.pack("<I", 1)
    head += struct.pack("<fff", 0.25, 1.0, 0.0) + struct.pack("<i", n)
    return head + b"\x00" * (4 * k) + struct.pack("<d", 1.0) + struct.pack("<f", 4.0)


def _make_db(path, trc_blobs, app_name="常规分析", stream_name="流路A"):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Analyzer (SerialNumber TEXT, Name TEXT, Version TEXT)")
    con.execute("CREATE TABLE Application (ApplicationId INTEGER, Name TEXT)")
    con.execute("CREATE TABLE Stream (ApplicationId INTEGER, StreamId INTEGER, Name TEXT)")
    con.execute("CREATE TABLE Cycle (CycleRowId INTEGER, IsValidation INTEGER, CalibrationLevel INTEGER)")
    con.execute("CREATE TABLE Chromatogram (CycleRowId INTEGER, ApplicationId INTEGER, "
                "StreamId INTEGER, DetectorId INTEGER, CycleTime TEXT, BinTrc BLOB)")
    con.execute("INSERT INTO Analyzer VALUES ('SN-TEST', 'x', '1')")
    con.execute("INSERT INTO Application VALUES (1, ?)", (app_name,))
    con.execute("INSERT INTO Stream VALUES (1, 1, ?)", (stream_name,))
    for i, b in enumerate(trc_blobs, 1):
        con.execute("INSERT INTO Cycle VALUES (?,0,0)", (i,))
        con.execute("INSERT INTO Chromatogram VALUES (?,1,1,1,?,?)",
                    (i, "2026-01-01 00:%02d:00" % i, b))
    con.commit()
    con.close()


# ===========================================================================
# 一、单元测试：解析器 / 文件 / 密码 / 登录规则
# ===========================================================================

class ParserHardening(unittest.TestCase):
    def test_random_corruption_only_raises_traceerror(self):
        """随机破坏谱图 3000 次：只允许抛 TraceError，不允许出现其他异常（否则会拖垮扫描线程）。"""
        good = build_trc(n=1200)
        rnd = random.Random(7)
        for _ in range(3000):
            b = bytearray(good)
            for _ in range(rnd.randint(1, 12)):
                b[rnd.randrange(len(b))] = rnd.randrange(256)
            if rnd.random() < .3:
                b = b[:rnd.randrange(len(b))]
            try:
                cc.to_series(cc.parse_trc(bytes(b)))
            except cc.TraceError:
                pass

    def test_point_count_over_limit_is_rejected(self):
        """声称点数超过上限 → 直接拒绝（曾经 64MB 畸形谱图会把进程撑爆被系统杀掉）。"""
        with self.assertRaises(cc.TraceError):
            cc.parse_trc(_blob_with_n(cc.MAX_TRACE_POINTS + 1))

    def test_oversize_blob_is_rejected(self):
        with self.assertRaises(cc.TraceError):
            cc.parse_trc(b"\x00" * (cc.MAX_BLOB_BYTES + 1))

    def test_downsample_before_expand_keeps_memory_small(self):
        """200 万点的谱图抽成 2 万点：内存峰值必须远小于旧实现（原先放大约 36 倍 ≈ 285MB）。"""
        info = cc.parse_trc(_blob_with_n(2_000_000))
        tracemalloc.start()
        series = cc.to_series(info, max_points=20000)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(len(series[0]), 20000)
        self.assertLess(peak, 20 * 1024 * 1024, "抽点后内存峰值 %.1f MB 过大" % (peak / 1e6))

    def test_non_finite_values_are_neutralised(self):
        info = cc.parse_trc(build_trc(n=50))
        info["sample_period"] = float("nan")
        info["unit_mult"] = float("inf")
        info["y_mult"] = float("-inf")
        t, tx, raw, uv, val = cc.to_series(info)
        for col in (t, tx, uv, val):
            self.assertTrue(all(x == x and abs(x) != float("inf") for x in col))


class MbdHardening(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _mbd(self, content):
        p = os.path.join(self.d, "t.mbd")
        with open(p, "wb") as f:
            f.write(content)
        return p

    def test_truncated_header(self):
        with self.assertRaises(cc.TraceError):
            list(cc.iter_mbd(self._mbd(b"EZBD" + b"\0" * 4)))

    def test_not_an_mbd(self):
        with self.assertRaises(cc.TraceError):
            list(cc.iter_mbd(self._mbd(b"hello world, definitely not mbd")))

    def test_absurd_entry_count(self):
        with self.assertRaises(cc.TraceError):
            list(cc.iter_mbd(self._mbd(b"EZBD" + b"\0" * 8 + struct.pack("<i", 2_000_000_000) + b"\0" * 64)))

    def test_negative_offset_is_ignored(self):
        """负偏移曾被接受（切片从文件尾倒着取，读到错误数据）。"""
        entry = b"trc\0" + b"\0" * 8 + struct.pack("<iii", 1, 50, -30)
        buf = b"EZBD" + b"\0" * 8 + struct.pack("<i", 1) + entry + b"A" * 100
        self.assertEqual(list(cc.iter_mbd(self._mbd(buf))), [])

    def test_offset_past_end_is_ignored(self):
        entry = b"trc\0" + b"\0" * 8 + struct.pack("<iii", 1, 50, 10_000)
        buf = b"EZBD" + b"\0" * 8 + struct.pack("<i", 1) + entry + b"A" * 100
        self.assertEqual(list(cc.iter_mbd(self._mbd(buf))), [])


class DatabaseHardening(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_non_sqlite_file_rejected(self):
        p = os.path.join(self.d, "fake_V3.db")
        with open(p, "wb") as f:
            f.write(b"this is not a database" * 100)
        with self.assertRaises(cc.TraceError):
            list(cc.iter_db(p, self.tmp))

    def test_connection_is_read_only_and_locked_down(self):
        p = os.path.join(self.d, "ok_V3.db")
        _make_db(p, [build_trc(n=100)])
        con = cc._open_db_copy(p, self.tmp)
        try:
            for sql in ("CREATE TABLE evil(a)", "INSERT INTO Analyzer VALUES ('a','b','c')",
                        "DELETE FROM Chromatogram", "DROP TABLE Cycle",
                        "ATTACH DATABASE '%s' AS x" % os.path.join(self.d, "other.db"),
                        "PRAGMA writable_schema=ON"):
                with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                    con.execute(sql)
            # 正常查询仍然可用
            self.assertEqual(con.execute("SELECT COUNT(*) FROM Chromatogram").fetchone()[0], 1)
        finally:
            con.close()

    def test_original_file_is_never_modified(self):
        p = os.path.join(self.d, "ok_V3.db")
        _make_db(p, [build_trc(n=100)])
        with open(p, "rb") as f:
            before = f.read()
        list(cc.iter_db(p, self.tmp))
        with open(p, "rb") as f:
            self.assertEqual(before, f.read())

    def test_oversize_trace_row_is_skipped_not_loaded(self):
        p = os.path.join(self.d, "big_V3.db")
        _make_db(p, [build_trc(n=100), b"\x00" * (cc.MAX_BLOB_BYTES + 10)])
        items = list(cc.iter_db(p, self.tmp))
        self.assertEqual(len(items), 1)

    def test_text_fields_are_sanitised(self):
        """库内名称里的换行/控制字符会被清掉（防日志伪造、防污染节点名）。"""
        p = os.path.join(self.d, "ctl_V3.db")
        _make_db(p, [build_trc(n=100)], app_name="正常\r\n2099-01-01 [ERROR] 伪造的日志行\x00\x07")
        it = list(cc.iter_db(p, self.tmp))[0]
        self.assertNotRegex(it.app_name, r"[\x00-\x1f]")

    def test_overlong_text_is_truncated(self):
        p = os.path.join(self.d, "long_V3.db")
        _make_db(p, [build_trc(n=100)], app_name="A" * 5000)
        it = list(cc.iter_db(p, self.tmp))[0]
        self.assertLessEqual(len(it.app_name), cc.MAX_TEXT_LEN)

    def test_latest_cycle_selection_unchanged(self):
        """优化为'先取元数据再按需取 BLOB'后，行为必须与原来一致。"""
        p = os.path.join(self.d, "cyc_V3.db")
        _make_db(p, [build_trc(n=100 + 10 * i) for i in range(6)])
        items = list(cc.iter_db(p, self.tmp, last=2))
        self.assertEqual([it.cycle_id for it in items], [5, 6])


class CsvInjection(unittest.TestCase):
    def test_cell_escaping(self):
        for bad in ("=1+1", "+cmd", "-2+3", "@SUM(A1)", "\t=x", "\r=x"):
            self.assertTrue(cc.csv_cell(bad).startswith("'"), bad)
        self.assertEqual(cc.csv_cell("正常文本"), "正常文本")
        self.assertEqual(cc.csv_cell(-5), -5)      # 数值不受影响
        self.assertEqual(cc.csv_cell(None), None)

    def test_index_csv_end_to_end(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "evil_V3.db")
            _make_db(p, [build_trc(n=100)], app_name='=cmd|"/c calc"!A1', stream_name="@SUM(1+1)")
            out = os.path.join(d, "out")
            cc.extract([p], out, log=lambda *a: None)
            with open(os.path.join(out, "index.csv"), encoding="utf-8-sig") as f:
                text = f.read()
            self.assertIn("'=cmd", text)
            self.assertIn("'@SUM", text)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class PasswordsAndUsers(unittest.TestCase):
    def test_hash_roundtrip(self):
        h = sa.hash_password("正确的密码-123")
        self.assertTrue(h.startswith("pbkdf2_sha256$"))
        self.assertTrue(sa.verify_password("正确的密码-123", h))
        self.assertFalse(sa.verify_password("错误的密码", h))
        self.assertNotEqual(h, sa.hash_password("正确的密码-123"), "同一密码两次哈希必须不同（随机盐）")

    def test_garbage_hash_never_matches(self):
        for junk in ("", "plain", "pbkdf2_sha256$x$y$z", "md5$1$00$00", "pbkdf2_sha256$1$00$00",
                     "pbkdf2_sha256$99999999999$00$00"):
            self.assertFalse(sa.verify_password("anything", junk), junk)

    def test_plaintext_password_in_config_is_refused(self):
        cfg = dict(sa.DEFAULT_CONFIG, users={"alice": "明文密码"})
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(cfg, ROOT)

    def test_no_one_can_connect_config_is_refused(self):
        cfg = dict(sa.DEFAULT_CONFIG, allow_anonymous=False, users={})
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(cfg, ROOT)

    def test_bad_values_are_clamped(self):
        cfg = dict(sa.DEFAULT_CONFIG, poll_seconds=0, max_connections=10 ** 9, max_points=1, last_n=-3)
        sa.normalize_config(cfg, ROOT)
        self.assertEqual((cfg["poll_seconds"], cfg["max_connections"], cfg["max_points"], cfg["last_n"]),
                         (5, 200, 100, 1))

    def test_relative_watch_dir_resolves_against_program_dir(self):
        cfg = dict(sa.DEFAULT_CONFIG, watch_dirs=["data"])
        old = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())          # 模拟从别的目录/计划任务启动
            sa.normalize_config(cfg, ROOT)
        finally:
            os.chdir(old)
        self.assertEqual(cfg["watch_dirs"], [os.path.join(ROOT, "data")])

    def test_bad_endpoint_and_security_refused(self):
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(dict(sa.DEFAULT_CONFIG, endpoint="http://x"), ROOT)
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(dict(sa.DEFAULT_CONFIG, security="whatever"), ROOT)


class UserManagerRules(unittest.TestCase):
    def setUp(self):
        from asyncua.crypto.permission_rules import UserRole
        self.Admin, self.User = UserRole.Admin, UserRole.User
        self.um = sa.ChromUserManager({"alice": sa.hash_password("Passw0rd!x")}, allow_anonymous=False)

    def test_admin_username_gets_nothing_without_password(self):
        """asyncua 默认管理器：用户名 admin + 任意密码 = 管理员。我们的必须拒绝。"""
        um = sa.ChromUserManager({}, allow_anonymous=True)
        self.assertIsNone(um.get_user(None, username="admin", password="随便"))
        self.assertIsNone(um.get_user(None, username="Admin", password=""))

    def test_anonymous_is_read_only_never_admin(self):
        um = sa.ChromUserManager({}, allow_anonymous=True)
        self.assertEqual(um.get_user(None).role, self.User)

    def test_anonymous_refused_when_disabled(self):
        self.assertIsNone(self.um.get_user(None))

    def test_valid_login_is_user_not_admin(self):
        u = self.um.get_user(None, username="alice", password="Passw0rd!x")
        self.assertEqual(u.role, self.User)
        self.assertNotEqual(u.role, self.Admin)

    def test_wrong_password_and_unknown_user(self):
        self.assertIsNone(self.um.get_user(None, username="alice", password="nope"))
        self.assertIsNone(self.um.get_user(None, username="ghost", password="Passw0rd!x"))

    def test_lockout_after_repeated_failures(self):
        for _ in range(sa.ChromUserManager.MAX_FAILS):
            self.assertIsNone(self.um.get_user(None, username="alice", password="bad"))
        # 即使这次密码是对的，窗口期内也被锁定
        self.assertIsNone(self.um.get_user(None, username="alice", password="Passw0rd!x"))

    def test_username_spray_does_not_grow_memory_unbounded(self):
        for i in range(3000):
            self.um.get_user(None, username="u%d" % i, password="x")
        self.assertLessEqual(len(self.um._fails), 1001)


class TextHelpers(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(cc.clean_text("a\r\nb\x00c"), "a  b c")
        self.assertEqual(len(cc.clean_text("x" * 1000)), cc.MAX_TEXT_LEN)

    def test_log_formatter_blocks_forged_lines(self):
        import logging
        rec = logging.LogRecord("t", logging.INFO, "", 0, "正常\n2099-01-01 [ERROR] 伪造", None, None)
        self.assertNotIn("\n", sa._SafeFormatter("%(message)s").format(rec))


# ===========================================================================
# 二、集成测试：真的启动服务，用各种身份去连
# ===========================================================================

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Server:
    """在临时目录里启动一个真实服务进程。"""

    def __init__(self, **overrides):
        self.dir = tempfile.mkdtemp(prefix="chromtest_")
        self.port = _free_port()
        self.data = os.path.join(self.dir, "data")
        os.makedirs(self.data)
        _make_db(os.path.join(self.data, "demo_V3.db"), [build_trc(n=300), build_trc(n=400)])
        cfg = dict(sa.DEFAULT_CONFIG)
        cfg.update(endpoint="opc.tcp://127.0.0.1:%d/ChromExtract" % self.port,
                   watch_dirs=[self.data], poll_seconds=5,
                   cert_dir=os.path.join(self.dir, "certs"),
                   trusted_client_certs_dir=os.path.join(self.dir, "trusted"),
                   csv_export_dir=os.path.join(self.dir, "export"),
                   log_file=os.path.join(self.dir, "server.log"))
        cfg.update(overrides)
        self.cfg_path = os.path.join(self.dir, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server_app.py"),
                                      "--config", self.cfg_path],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._wait_ready()

    def _wait_ready(self):
        t0 = time.time()
        while time.time() - t0 < 40:
            if self.proc.poll() is not None:
                raise RuntimeError("服务提前退出：\n" + self.proc.stdout.read().decode("utf-8", "replace"))
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    pass
                time.sleep(2.5)       # 等第一轮扫描建好节点
                return
            except OSError:
                time.sleep(0.3)
        raise RuntimeError("服务启动超时")

    @property
    def url(self):
        return "opc.tcp://127.0.0.1:%d/ChromExtract/" % self.port

    def log(self):
        try:
            with open(os.path.join(self.dir, "server.log"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def stop(self):
        try:
            self.proc.stdout.close()
        except Exception:  # noqa
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


def _gen_client_cert(outdir, name, uri):
    import datetime
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nm).issuer_name(nm).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(uri), x509.DNSName("localhost")]), False)
            .add_extension(x509.KeyUsage(True, True, True, True, False, False, False, False, False), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH,
                                                  ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .sign(key, hashes.SHA256()))
    cp, kp = os.path.join(outdir, name + ".der"), os.path.join(outdir, name + ".pem")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.DER))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return cp, kp


async def _client(url, user=None, pw=None, cert=None, key=None, uri=None, secure=False):
    from asyncua import Client, ua
    from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
    c = Client(url, timeout=8)
    if user is not None:
        c.set_user(user)
        c.set_password(pw)
    if secure:
        if uri:
            c.application_uri = uri
        await c.set_security(SecurityPolicyBasic256Sha256, certificate=cert, private_key=key,
                             mode=ua.MessageSecurityMode.SignAndEncrypt)
    await c.connect()
    return c


async def _first_nodes(c):
    ns = await c.get_namespace_index("urn:chromextract:opcua")
    root = await c.nodes.objects.get_child(["%d:Chromatograph" % ns])
    st = [n for n in await root.get_children() if (await n.read_browse_name()).Name.startswith("Station_")][0]
    dev = [n for n in await st.get_children() if (await n.read_browse_name()).Name.startswith("App")][0]
    good = await (await dev.get_child(["%d:Status" % ns])).get_child(["%d:Good" % ns])
    val = await (await dev.get_child(["%d:Trace" % ns])).get_child(["%d:ValueArray" % ns])
    return ns, root, good, val


class OpenModeIntegration(unittest.TestCase):
    """默认（开放只读）模式：匿名能读，但谁也改不了数据。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_anonymous_can_read(self):
        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root, good, val = await _first_nodes(c)
                self.assertTrue(await good.read_value())
                self.assertGreater(len(await val.read_value()), 100)
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_anonymous_cannot_write_or_add_nodes(self):
        from asyncua import ua

        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root, good, val = await _first_nodes(c)
                with self.assertRaises(Exception):
                    await good.write_value(False)
                with self.assertRaises(Exception):
                    await val.write_value(ua.Variant([0.0] * 5, ua.VariantType.Double))
                with self.assertRaises(Exception):
                    await root.add_variable(ua.NodeId(9999, ns), "Injected", 1)
                self.assertGreater(len(await val.read_value()), 100, "数据被篡改了！")
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_admin_username_with_any_password_is_rejected(self):
        """关键回归：asyncua 默认管理器会让 admin+任意密码 变成管理员。"""
        async def go():
            with self.assertRaises(Exception):
                await _client(self.srv.url, user="admin", pw="随便乱填")
        asyncio.run(go())

    def test_export_is_throttled_with_clear_error(self):
        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root, _, _ = await _first_nodes(c)
                ok, codes = 0, set()
                for _ in range(10):
                    try:
                        await root.call_method("%d:ExportCSV" % ns)
                        ok += 1
                    except Exception as e:
                        codes.add(type(e).__name__)
                self.assertEqual(ok, 1)
                self.assertEqual(codes, {"BadTooManyOperations"})
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_heartbeat_advances_without_new_data(self):
        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root, _, _ = await _first_nodes(c)
                hb = await root.get_child(["%d:Heartbeat" % ns])
                a = await hb.read_value()
                await asyncio.sleep(7)
                self.assertGreater(await hb.read_value(), a)
            finally:
                await c.disconnect()
        asyncio.run(go())


class HardenedModeIntegration(unittest.TestCase):
    """加密 + 强制登录 + 客户端证书白名单。"""

    @classmethod
    def setUpClass(cls):
        cls.pw = "Str0ng-Passw0rd!"
        pre = tempfile.mkdtemp(prefix="chromcli_")
        cls.cli_dir = pre
        cls.trusted_uri = "urn:test:trusted-client"
        cls.other_uri = "urn:test:stranger"
        cls.t_cert, cls.t_key = _gen_client_cert(pre, "trusted", cls.trusted_uri)
        cls.o_cert, cls.o_key = _gen_client_cert(pre, "stranger", cls.other_uri)
        cls.srv = _Server(security="sign_encrypt", allow_anonymous=False,
                          users={"alice": sa.hash_password(cls.pw)})
        os.makedirs(os.path.join(cls.srv.dir, "trusted"), exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.cli_dir, ignore_errors=True)

    def _secure(self, **kw):
        return _client(self.srv.url, cert=self.t_cert, key=self.t_key, uri=self.trusted_uri, secure=True, **kw)

    def test_correct_login_works_and_is_read_only(self):
        async def go():
            c = await self._secure(user="alice", pw=self.pw)
            try:
                ns, root, good, val = await _first_nodes(c)
                self.assertGreater(len(await val.read_value()), 100)
                with self.assertRaises(Exception):          # 登录用户同样不能改写
                    await good.write_value(False)
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_anonymous_refused(self):
        async def go():
            with self.assertRaises(Exception):
                await self._secure()
        asyncio.run(go())

    def test_wrong_password_refused(self):
        async def go():
            with self.assertRaises(Exception):
                await self._secure(user="alice", pw="wrong-password")
        asyncio.run(go())

    def test_admin_refused(self):
        async def go():
            with self.assertRaises(Exception):
                await self._secure(user="admin", pw="admin")
        asyncio.run(go())

    def test_unencrypted_channel_is_not_offered(self):
        """加密模式不能留不加密的后门（否则客户端可以选'不加密'绕过）。"""
        async def go():
            with self.assertRaises(Exception):
                await _client(self.srv.url, user="alice", pw=self.pw)    # 不加密通道
        asyncio.run(go())


class ClientWhitelistIntegration(unittest.TestCase):
    """客户端证书白名单：只放行证书在 trusted 目录里的客户端。"""

    @classmethod
    def setUpClass(cls):
        cls.cli_dir = tempfile.mkdtemp(prefix="chromcli_")
        cls.uri_ok, cls.uri_bad = "urn:test:ok-client", "urn:test:evil-client"
        cls.ok_cert, cls.ok_key = _gen_client_cert(cls.cli_dir, "ok", cls.uri_ok)
        cls.bad_cert, cls.bad_key = _gen_client_cert(cls.cli_dir, "evil", cls.uri_bad)
        # 先建好 trusted 目录并放入合法客户端证书，再启动服务
        cls._pre = tempfile.mkdtemp(prefix="chromtrust_")
        shutil.copy(cls.ok_cert, os.path.join(cls._pre, "ok.der"))
        cls.srv = _Server(security="sign_encrypt", trusted_client_certs_dir=cls._pre)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.cli_dir, ignore_errors=True)
        shutil.rmtree(cls._pre, ignore_errors=True)

    def test_trusted_client_allowed(self):
        async def go():
            c = await _client(self.srv.url, cert=self.ok_cert, key=self.ok_key, uri=self.uri_ok, secure=True)
            try:
                ns, root, good, val = await _first_nodes(c)
                self.assertGreater(len(await val.read_value()), 100)
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_untrusted_client_refused(self):
        async def go():
            with self.assertRaises(Exception):
                await _client(self.srv.url, cert=self.bad_cert, key=self.bad_key,
                              uri=self.uri_bad, secure=True)
        asyncio.run(go())
        self.assertIn("白名单", self.srv.log())


class MaliciousDataIntegration(unittest.TestCase):
    """监控目录里混进了各种畸形/恶意文件：服务必须活着，并继续提供正常数据。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server()
        d = cls.srv.data
        with open(os.path.join(d, "garbage_V3.db"), "wb") as f:
            f.write(os.urandom(4096))
        with open(os.path.join(d, "short.mbd"), "wb") as f:
            f.write(b"EZBD")
        with open(os.path.join(d, "negoff.mbd"), "wb") as f:
            f.write(b"EZBD" + b"\0" * 8 + struct.pack("<i", 1) + b"trc\0" + b"\0" * 8
                    + struct.pack("<iii", 1, 50, -30) + b"A" * 100)
        _make_db(os.path.join(d, "bomb_V3.db"), [_blob_with_n(cc.MAX_TRACE_POINTS + 5, payload_points=10)])
        _make_db(os.path.join(d, "ctl_V3.db"), [build_trc(n=100)],
                 app_name="x\n2099-01-01 [ERROR] 伪造日志")
        time.sleep(7)                          # 等下一轮扫描处理这些文件

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_server_survives_and_still_serves_good_data(self):
        self.assertIsNone(self.srv.proc.poll(), "服务被恶意文件打崩了")

        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root, good, val = await _first_nodes(c)
                self.assertGreater(len(await val.read_value()), 100)
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_log_has_no_forged_line(self):
        for line in self.srv.log().splitlines():
            self.assertFalse(line.startswith("2099-01-01"), "日志被伪造：" + line)


# ===========================================================================
# 三、配置工具 / 热加载 / 窗口面板（v2.1 升级项）
# ===========================================================================

class ConfigToolsTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.json")
        with open(self.p, "w", encoding="utf-8") as f:
            json.dump({"watch_dirs": ["D:/数据"], "poll_seconds": 30, "自定义": "保留我"}, f, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _raw(self):
        return ct.read_raw(self.p)

    def test_add_user_keeps_other_keys_and_makes_backup(self):
        with open(self.p, "rb") as f:
            before = f.read()
        self.assertEqual(ct.add_user(self.p, "dcs", "Good-Pass-2026!"), "added")
        raw = self._raw()
        self.assertEqual(raw["自定义"], "保留我")
        self.assertEqual(raw["watch_dirs"], ["D:/数据"])
        self.assertNotIn("max_points", raw, "不应把默认值全部展开写进用户配置")
        with open(self.p + ".bak", "rb") as f:
            self.assertEqual(f.read(), before)

    def test_password_is_never_stored_in_plaintext(self):
        ct.add_user(self.p, "dcs", "Good-Pass-2026!")
        with open(self.p, encoding="utf-8") as f:
            self.assertNotIn("Good-Pass-2026!", f.read())
        self.assertTrue(ct.verify_password("Good-Pass-2026!", self._raw()["users"]["dcs"]))

    def test_weak_password_rejected_and_file_untouched(self):
        with open(self.p, "rb") as f:
            before = f.read()
        for bad in ("short", "12345678", "aaaaaaaaaa", "xx-dcs-xx-12", "a" * 200):
            with self.assertRaises(ct.ConfigToolError, msg=bad):
                ct.add_user(self.p, "dcs", bad)
        with open(self.p, "rb") as f:
            self.assertEqual(f.read(), before)

    def test_dangerous_usernames_rejected(self):
        for bad in ("", "a&b", "x;y", "a b", "../etc", "a|b", "user\nname", "x" * 65, "a\"b", "%PATH%"):
            with self.assertRaises(ct.ConfigToolError, msg=repr(bad)):
                ct.add_user(self.p, bad, "Good-Pass-2026!")
        ct.add_user(self.p, "操作员_01", "Good-Pass-2026!")      # 中文、下划线是允许的

    def test_reset_password_of_existing_user(self):
        ct.add_user(self.p, "dcs", "Good-Pass-2026!")
        self.assertEqual(ct.add_user(self.p, "dcs", "Another-Pass-77!"), "updated")
        h = self._raw()["users"]["dcs"]
        self.assertTrue(ct.verify_password("Another-Pass-77!", h))
        self.assertFalse(ct.verify_password("Good-Pass-2026!", h))

    def test_cannot_remove_last_user_when_anonymous_disabled(self):
        ct.harden(self.p, "dcs", "Good-Pass-2026!")
        with self.assertRaises(ct.ConfigToolError):
            ct.remove_user(self.p, "dcs")
        ct.add_user(self.p, "second", "Good-Pass-2026!")
        ct.remove_user(self.p, "dcs")                  # 还有别的账号，可以删
        self.assertEqual(ct.list_users(self.p), ["second"])

    def test_remove_unknown_user(self):
        with self.assertRaises(ct.ConfigToolError):
            ct.remove_user(self.p, "ghost")

    def test_harden_result_passes_config_validation(self):
        ct.harden(self.p, "dcs", "Good-Pass-2026!")
        raw = self._raw()
        self.assertEqual((raw["security"], raw["allow_anonymous"], raw["allow_insecure_fallback"]),
                         ("sign_encrypt", False, False))
        cfg = sa.load_config(self.p)
        sa.normalize_config(cfg, self.d)                 # 加固后的配置必须是合法配置
        levels = [lv for lv, _ in sa.security_report(cfg)]
        self.assertNotIn("风险", levels)

    def test_corrupt_config_is_refused_and_not_overwritten(self):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 JSON")
        with self.assertRaises(ct.ConfigToolError):
            ct.add_user(self.p, "dcs", "Good-Pass-2026!")
        with open(self.p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ 这不是合法 JSON")

    def test_failed_write_leaves_original_intact(self):
        with open(self.p, "rb") as f:
            before = f.read()
        real = os.replace
        os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("磁盘已满（模拟）"))
        try:
            with self.assertRaises(ct.ConfigToolError):
                ct.add_user(self.p, "dcs", "Good-Pass-2026!")
        finally:
            os.replace = real
        with open(self.p, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(self.p + ".tmp"), "失败后不应遗留临时文件")

    def test_bom_config_is_readable(self):
        with open(self.p, "wb") as f:
            f.write(b"\xef\xbb\xbf" + '{"poll_seconds": 30}'.encode("utf-8"))   # 记事本保存的带 BOM 文件
        self.assertEqual(ct.read_raw(self.p)["poll_seconds"], 30)


class AdminMenuTests(unittest.TestCase):
    """交互菜单：用假输入驱动。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.json")
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("{}")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _run(self, inputs, passwords):
        import builtins
        import contextlib
        import getpass
        import io
        it, pw = iter(inputs), iter(passwords)
        old_in, old_gp = builtins.input, getpass.getpass
        builtins.input = lambda *a: next(it)
        getpass.getpass = lambda *a: next(pw)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = sa.admin_menu(self.p)
        finally:
            builtins.input, getpass.getpass = old_in, old_gp
        return rc, out.getvalue()

    def test_menu_add_list_remove(self):
        rc, out = self._run(["3", "dcs", "3", "op2", "5", "4", "dcs", "5", "0"],
                            ["Good-Pass-2026!", "Good-Pass-2026!", "Other-Pass-2026!", "Other-Pass-2026!"])
        self.assertEqual(rc, 0)
        self.assertEqual(ct.list_users(self.p), ["op2"])
        self.assertIn("已创建", out)

    def test_menu_harden_then_checkup(self):
        # 选 2（一键加固）→ 输入用户名 dcs → 两次密码；再选 1（体检）；0 退出
        rc, out = self._run(["2", "dcs", "1", "0"], ["Good-Pass-2026!", "Good-Pass-2026!"])
        self.assertEqual(rc, 0)
        self.assertEqual(ct.read_raw(self.p)["security"], "sign_encrypt")
        self.assertIn("通信已启用", out)

    def test_menu_survives_errors_and_junk_input(self):
        rc, out = self._run(["9", "abc", "4", "ghost", "3", "bad name!", "0"], [])
        self.assertEqual(rc, 0)
        self.assertIn("没有这个账号", out)

    def test_menu_handles_eof(self):
        import builtins
        old = builtins.input
        builtins.input = lambda *a: (_ for _ in ()).throw(EOFError())
        try:
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(sa.admin_menu(self.p), 0)
        finally:
            builtins.input = old


class HotReloadUnit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.json")
        self._write({"poll_seconds": 30})
        self.cfg = sa.load_config(self.p)
        self.cfg.update(_base_dir=self.d, _cfg_path=self.p)
        sa.normalize_config(self.cfg, self.d)
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _write(self, data):
        with open(self.p, "w", encoding="utf-8") as f:
            json.dump(data, f)
        # 保证 mtime 变化（部分文件系统时间精度低）
        t = time.time() + getattr(self, "_bump", 0)
        self._bump = getattr(self, "_bump", 0) + 2
        os.utime(self.p, (t, t))

    def _reload(self, stamp):
        return sa.try_hot_reload(self.cfg, stamp, lambda u, a: self.calls.append((dict(u), a)))

    def test_hot_keys_apply_without_restart(self):
        stamp = sa._cfg_stamp(self.p)
        self._write({"poll_seconds": 30, "allow_anonymous": False,
                     "users": {"a": ct.hash_password("Good-Pass-2026!")}, "remote_export": False})
        self._reload(stamp)
        self.assertFalse(self.cfg["allow_anonymous"])
        self.assertFalse(self.cfg["remote_export"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(self.calls[0][0]), ["a"])

    def test_invalid_config_is_ignored(self):
        stamp = sa._cfg_stamp(self.p)
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("{ 写到一半")
        os.utime(self.p, (time.time() + 50, time.time() + 50))
        self._reload(stamp)
        self.assertTrue(self.cfg["allow_anonymous"], "坏配置不应改变运行中的设置")
        self.assertEqual(self.calls, [])

    def test_dangerous_config_is_ignored(self):
        """热加载同样要过校验：明文密码、无人可连的配置都不能被接受。"""
        stamp = sa._cfg_stamp(self.p)
        self._write({"users": {"a": "明文密码"}})
        self._reload(stamp)
        self.assertEqual(self.cfg["users"], {})
        stamp = sa._cfg_stamp(self.p)
        self._write({"allow_anonymous": False, "users": {}})
        self._reload(stamp)
        self.assertTrue(self.cfg["allow_anonymous"])

    def test_restart_keys_are_not_applied(self):
        stamp = sa._cfg_stamp(self.p)
        self._write({"security": "sign_encrypt", "endpoint": "opc.tcp://0.0.0.0:5999/x"})
        self._reload(stamp)
        self.assertEqual(self.cfg["security"], "none")
        self.assertIn("4840", self.cfg["endpoint"])

    def test_no_change_no_work(self):
        stamp = sa._cfg_stamp(self.p)
        self.assertEqual(self._reload(stamp), stamp)
        self.assertEqual(self.calls, [])


class HotReloadIntegration(unittest.TestCase):
    """真实服务运行中，改配置文件：新账号和关闭匿名不重启就生效；写坏配置不影响运行。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def _can_read(self, **kw):
        async def go():
            try:
                c = await _client(self.srv.url, **kw)
            except Exception:
                return False
            try:
                await _first_nodes(c)
                return True
            finally:
                await c.disconnect()
        return asyncio.run(go())

    def _wait_for(self, fn, expect, timeout=25):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if fn() == expect:
                return True
            time.sleep(1)
        return False

    def test_live_reload_flow(self):
        path = self.srv.cfg_path
        self.assertTrue(self._can_read(), "初始应允许匿名")
        # 1) 写坏配置：服务继续按原配置运行
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 写到一半断电了")
        time.sleep(7)
        self.assertTrue(self._can_read(), "坏配置不应影响运行中的服务")
        self.assertIn("配置文件有误", self.srv.log())
        # 2) 恢复并改成：关闭匿名 + 新增账号（相当于在窗口/菜单里操作）
        raw = {"endpoint": "opc.tcp://127.0.0.1:%d/ChromExtract" % self.srv.port,
               "watch_dirs": [self.srv.data], "poll_seconds": 5,
               "cert_dir": os.path.join(self.srv.dir, "certs"),
               "trusted_client_certs_dir": os.path.join(self.srv.dir, "trusted"),
               "csv_export_dir": os.path.join(self.srv.dir, "export"),
               "log_file": os.path.join(self.srv.dir, "server.log")}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        ct.add_user(path, "alice", "Good-Pass-2026!")
        ct.write_atomic(path, dict(ct.read_raw(path), allow_anonymous=False))
        self.assertTrue(self._wait_for(self._can_read, False), "关闭匿名后匿名应被拒（不重启）")
        self.assertTrue(self._can_read(user="alice", pw="Good-Pass-2026!"), "新账号应无需重启即可登录")
        self.assertFalse(self._can_read(user="alice", pw="wrong-password-1"))
        log = self.srv.log()
        self.assertIn("已自动生效", log)
        self.assertIn("登录成功：用户 alice", log)
        # 3) 改加密方式：只提示需要重启，不会半途生效
        ct.write_atomic(path, dict(ct.read_raw(path), security="sign_encrypt"))
        time.sleep(7)
        self.assertIn("需要重启服务才会生效", self.srv.log())
        self.assertTrue(self._can_read(user="alice", pw="Good-Pass-2026!"), "运行中的服务通道方式不应被改动")


class SecurityWindowTests(unittest.TestCase):
    """窗口里的安全面板：用假对话框驱动（需要 tkinter 与显示环境，没有则自动跳过）。"""

    class FakeDialogs:
        def __init__(self):
            self.answers, self.msgs, self.confirm_answer = [], [], True

        def ask(self, title, prompt, secret=False):
            return self.answers.pop(0) if self.answers else None

        def info(self, t):
            self.msgs.append(("info", t))

        def error(self, t):
            self.msgs.append(("error", t))

        def confirm(self, t):
            return self.confirm_answer

    def setUp(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as e:  # noqa
            self.skipTest("没有可用的图形环境：%s" % e)
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.json")
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("{}")
        self.cfg = sa.load_config(self.p)
        self.cfg.update(_base_dir=self.d, _cfg_path=self.p)
        sa.normalize_config(self.cfg, self.d)
        self.dlg = self.FakeDialogs()
        self.win = sa.SecurityWindow(self.root, self.cfg, self.dlg)

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:  # noqa
            pass
        shutil.rmtree(getattr(self, "d", ""), ignore_errors=True)

    def _report_text(self):
        return self.win.report.get("1.0", "end")

    def test_initial_report_warns_about_open_mode(self):
        self.assertIn("无需登录", self._report_text())

    def test_add_remove_user_through_window(self):
        self.dlg.answers = ["dcs", "Good-Pass-2026!", "Good-Pass-2026!"]
        self.win.add_user()
        self.assertEqual(ct.list_users(self.p), ["dcs"])
        self.assertEqual(self.win.users_lb.get(0), "dcs")
        self.win.users_lb.selection_set(0)
        self.win.remove_user()
        self.assertEqual(ct.list_users(self.p), [])

    def test_mismatched_or_weak_password_is_rejected_in_window(self):
        self.dlg.answers = ["dcs", "Good-Pass-2026!", "Different-Pass-1!"]
        self.win.add_user()
        self.assertEqual(ct.list_users(self.p), [])
        self.dlg.answers = ["dcs", "12345678"]
        self.win.add_user()
        self.assertEqual(ct.list_users(self.p), [])
        self.assertTrue(any(k == "error" for k, _ in self.dlg.msgs))

    def test_harden_through_window_and_cancel(self):
        self.dlg.confirm_answer = False
        self.dlg.answers = ["dcs", "Good-Pass-2026!", "Good-Pass-2026!"]
        self.win.harden()
        self.assertEqual(ct.read_raw(self.p), {}, "取消时不能改动配置")
        self.dlg.confirm_answer = True
        self.win.harden()
        raw = ct.read_raw(self.p)
        self.assertEqual((raw["security"], raw["allow_anonymous"]), ("sign_encrypt", False))
        self.assertIn("通信已启用", self._report_text())

    def test_broken_config_shown_not_crashing(self):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("{ 坏了")
        self.win.refresh()
        self.assertIn("有误", self._report_text())

    def test_security_summary_text(self):
        self.assertIn("注意", sa.security_summary(self.cfg))
        risky = dict(self.cfg, users={"a": ct.hash_password("Good-Pass-2026!")}, security="none")
        self.assertIn("风险", sa.security_summary(risky))


if __name__ == "__main__":
    unittest.main(verbosity=2)
