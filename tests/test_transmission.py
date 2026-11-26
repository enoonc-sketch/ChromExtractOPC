# -*- coding: utf-8 -*-
"""数据传输检测与发送（v2.4）测试：合成记录、状态判定、配置夹取 + 真服务集成测试。

运行：  python -m unittest discover -s tests -v
前提：  pip install asyncua cryptography
说明：  集成测试会真的启动服务（随机端口、临时目录），测完自动清理，不会改动项目目录。
        服务 harness 写法复用 tests/test_security.py 的 _Server/_client 模式（不改那个文件）。
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import demo_data                 # noqa: E402
import server_app as sa          # noqa: E402


# ---------------------------------------------------------------------------
# 单元测试
# ---------------------------------------------------------------------------

class MakeTestRecord(unittest.TestCase):
    def test_fields_complete_and_consistent(self):
        r = sa.make_test_record(cycle_id=7, num_points=600)
        for k in ("source", "analyzer", "app", "app_name", "stream", "stream_name",
                  "det", "det_name", "cycle_id", "cycle_time", "is_validation",
                  "cal_level", "num_points", "sample_period", "duration",
                  "min_raw", "max_raw", "y_title", "x_title",
                  "time", "raw", "uv", "value"):
            self.assertIn(k, r, "缺字段 " + k)
        self.assertEqual(r["analyzer"], "TEST")
        self.assertEqual(r["source"], "TEST-SEND")
        self.assertEqual(r["det_name"], "TEST")
        self.assertEqual(r["cycle_id"], 7)
        self.assertEqual(r["num_points"], 600)
        for arr in ("time", "raw", "uv", "value"):
            self.assertEqual(len(r[arr]), 600, arr)
        self.assertLessEqual(r["min_raw"], r["max_raw"])
        self.assertGreater(r["duration"], 0)

    def test_default_points_about_600(self):
        r = sa.make_test_record()
        self.assertEqual(r["num_points"], 600)


class TransmissionStatus(unittest.TestCase):
    def test_no_channels(self):
        self.assertEqual(sa._transmission_status(0, 0, 0, 7200), "无数据")
        self.assertEqual(sa._transmission_status(0, 0, 99999, 7200), "无数据")

    def test_fresh_and_good(self):
        self.assertEqual(sa._transmission_status(2, 2, 10, 7200), "正常")
        self.assertEqual(sa._transmission_status(2, 1, 7200, 7200), "正常")

    def test_stale_or_no_good(self):
        self.assertEqual(sa._transmission_status(2, 2, 7201, 7200), "数据较旧")
        self.assertEqual(sa._transmission_status(2, 0, 10, 7200), "数据较旧")
        self.assertEqual(sa._transmission_status(1, 1, None, 7200), "数据较旧")


class ConfigClamp(unittest.TestCase):
    def _norm(self, **over):
        cfg = dict(sa.DEFAULT_CONFIG)
        cfg.update(over)
        tmp = tempfile.mkdtemp()
        try:
            sa.normalize_config(cfg, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return cfg

    def test_defaults(self):
        cfg = self._norm()
        self.assertTrue(cfg["allow_test_send"])
        self.assertEqual(cfg["test_send_min_interval_sec"], 5)
        self.assertEqual(cfg["transmission_stale_sec"], 7200)

    def test_clamped(self):
        cfg = self._norm(test_send_min_interval_sec=99999, transmission_stale_sec=1)
        self.assertEqual(cfg["test_send_min_interval_sec"], 3600)
        self.assertEqual(cfg["transmission_stale_sec"], 60)
        cfg = self._norm(test_send_min_interval_sec=-5, transmission_stale_sec=10 ** 9)
        self.assertEqual(cfg["test_send_min_interval_sec"], 0)
        self.assertEqual(cfg["transmission_stale_sec"], 604800)

    def test_local_endpoint(self):
        self.assertEqual(sa._local_endpoint("opc.tcp://0.0.0.0:4840/ChromExtract"),
                         "opc.tcp://127.0.0.1:4840/ChromExtract")


# ---------------------------------------------------------------------------
# 集成测试（真起服务）
# ---------------------------------------------------------------------------

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Server:
    """在临时目录里启动一个真实服务进程（写法同 test_security._Server）。"""

    def __init__(self, **overrides):
        self.dir = tempfile.mkdtemp(prefix="chromtx_")
        self.port = _free_port()
        self.data = os.path.join(self.dir, "data")
        os.makedirs(self.data)
        demo_data.make_demo_db(os.path.join(self.data, "demo_V3.db"))
        cfg = dict(sa.DEFAULT_CONFIG)
        cfg.update(endpoint="opc.tcp://127.0.0.1:%d/ChromExtract" % self.port,
                   watch_dirs=[self.data], poll_seconds=5,
                   cert_dir=os.path.join(self.dir, "certs"),
                   trusted_client_certs_dir=os.path.join(self.dir, "trusted"),
                   csv_export_dir=os.path.join(self.dir, "export"),
                   log_file=os.path.join(self.dir, "server.log"),
                   test_send_min_interval_sec=0)
        cfg.update(overrides)
        self.cfg_path = os.path.join(self.dir, "config.json")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "server_runner.py"),
                                      self.cfg_path],
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


async def _client(url):
    from asyncua import Client
    c = Client(url, timeout=10)
    await c.connect()
    return c


async def _root(c):
    ns = await c.get_namespace_index("urn:chromextract:opcua")
    root = await c.nodes.objects.get_child(["%d:Chromatograph" % ns])
    return ns, root


async def _real_good(c, ns, root):
    """返回第一个真实通道（排除 Station_TEST）的 Good 节点。"""
    for st in await root.get_children():
        name = (await st.read_browse_name()).Name
        if not name.startswith("Station_") or name == "Station_TEST":
            continue
        for dev in await st.get_children():
            if (await dev.read_browse_name()).Name.startswith("App"):
                return await dev.get_child(["%d:Status" % ns, "%d:Good" % ns])
    return None


class TransmissionIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_transmission_check_all_steps(self):
        result = asyncio.run(sa.transmission_check_async(self.srv.url, duration_sec=6))
        self.assertTrue(result["ok"], result["steps"])
        names = [s["name"] for s in result["steps"]]
        for want in ("连接建会话", "读 ServerVersion", "读 Diagnostics/TestCounter",
                     "订阅 TestSignal+Heartbeat", "读真实通道曲线", "EchoTest 往返"):
            self.assertIn(want, names)
        for s in result["steps"]:
            self.assertTrue(s["ok"], s)

    def test_transmission_check_with_send(self):
        result = asyncio.run(sa.transmission_check_async(
            self.srv.url, duration_sec=2, do_send_test=True))
        self.assertTrue(result["ok"], result["steps"])
        send = [s for s in result["steps"] if s["name"].startswith("SendTestData")]
        self.assertEqual(len(send), 1)
        self.assertTrue(send[0]["ok"], send[0])

    def test_subscription_receives_notifications(self):
        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root = await _root(c)
                diag = await root.get_child(["%d:Diagnostics" % ns])
                sig = await diag.get_child(["%d:TestSignal" % ns])
                hb = await root.get_child(["%d:Heartbeat" % ns])
                handler = sa._SubHandler()
                sub = await c.create_subscription(100, handler)
                try:
                    await sub.subscribe_data_change([sig, hb])
                    await asyncio.sleep(7)
                finally:
                    await sub.delete()
                # 订阅初始值也算传输证据；心跳每轮扫描（5 秒）更新，7 秒内应至少有初始通知
                self.assertGreaterEqual(len(handler.events), 1)
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_echo_roundtrip(self):
        async def go():
            from asyncua import ua
            c = await _client(self.srv.url)
            try:
                ns, root = await _root(c)
                probe = "双向传输-test-123"
                out = await root.call_method("%d:EchoTest" % ns,
                                             ua.Variant(probe, ua.VariantType.String))
                vals = list(out)
                echoed = vals[0].Value if hasattr(vals[0], "Value") else vals[0]
                self.assertEqual(echoed, probe)
                self.assertTrue(str(vals[1]))
                self.assertGreaterEqual(int(vals[2]), 1)
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_send_test_data_and_real_good_stays(self):
        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root = await _root(c)
                good = await _real_good(c, ns, root)
                self.assertIsNotNone(good)
                self.assertTrue(await good.read_value(), "发送前真实通道应为 Good")
                out = await root.call_method("%d:SendTestData" % ns)
                vals = list(out)
                self.assertGreater(int(vals[1]), 0)
                # Station_TEST 可读且点数一致
                test_st = await root.get_child(["%d:Station_TEST" % ns])
                devs = [d for d in await test_st.get_children()
                        if (await d.read_browse_name()).Name.startswith("App")]
                self.assertEqual(len(devs), 1)
                npts = await (await devs[0].get_child(
                    ["%d:LatestCycle" % ns, "%d:NumPoints" % ns])).read_value()
                arr = await (await devs[0].get_child(
                    ["%d:Trace" % ns, "%d:ValueArray" % ns])).read_value()
                self.assertEqual(int(npts), len(arr))
                self.assertGreater(len(arr), 0)
                # 真实通道不受影响
                self.assertTrue(await good.read_value(), "发送测试数据后真实通道 Good 被改了！")
                # Diagnostics 反映真实数据
                diag = await root.get_child(["%d:Diagnostics" % ns])
                status = await (await diag.get_child(
                    ["%d:TransmissionStatus" % ns])).read_value()
                self.assertEqual(status, "正常")
            finally:
                await c.disconnect()
        asyncio.run(go())

    def test_anonymous_still_cannot_write(self):
        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root = await _root(c)
                diag = await root.get_child(["%d:Diagnostics" % ns])
                for path in (["%d:TestSignal" % ns], ["%d:TestCounter" % ns],
                             ["%d:TransmissionStatus" % ns]):
                    node = await diag.get_child(path)
                    with self.assertRaises(Exception, msg=str(path)):
                        await node.write_value(0)
                hb = await root.get_child(["%d:Heartbeat" % ns])
                with self.assertRaises(Exception):
                    await hb.write_value(999)
            finally:
                await c.disconnect()
        asyncio.run(go())


class SendDeniedIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _Server(allow_test_send=False)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_send_denied_when_disabled(self):
        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root = await _root(c)
                with self.assertRaises(Exception) as cm:
                    await root.call_method("%d:SendTestData" % ns)
                self.assertIn("BadUserAccessDenied", type(cm.exception).__name__)
                # EchoTest 不受开关影响（只回显，不写数据）
                from asyncua import ua
                out = await root.call_method("%d:EchoTest" % ns,
                                             ua.Variant("hi", ua.VariantType.String))
                self.assertEqual(list(out)[0], "hi")
            finally:
                await c.disconnect()
        asyncio.run(go())


# ---------------------------------------------------------------------------
# 智能诊断测试（v2.4）
# ---------------------------------------------------------------------------

def _diag_cfg(tmp, **over):
    data = os.path.join(tmp, "data")
    os.makedirs(data, exist_ok=True)
    demo_data.make_demo_db(os.path.join(data, "demo_V3.db"))
    logp = os.path.join(tmp, "server.log")
    with open(logp, "w", encoding="utf-8") as f:
        f.write("2026-01-01 00:00:00 [INFO] 服务已启动\n")
    cfg = dict(sa.DEFAULT_CONFIG)
    cfg.update(endpoint="opc.tcp://127.0.0.1:4840/ChromExtract", watch_dirs=[data],
               poll_seconds=5, log_file=logp, _base_dir=tmp, _cfg_path=None)
    cfg.update(over)
    return cfg


def _healthy_opc(**over):
    from datetime import datetime as _dt
    opc = dict(connected=True, error="", server_version="2.4", heartbeat1=10, heartbeat2=11,
               last_scan_time=_dt.now().strftime("%Y-%m-%d %H:%M:%S"),
               diag=dict(channel_count=2, good_count=2, total_points=2300,
                         last_change=_dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                         data_age=5.0, status="正常", test_counter=11),
               channels=[
                   dict(station="Station_DEMO", device="App1_Stream1_Det1", good=True,
                        last_update="2026-01-01 00:00:00", num_points=1400, trace_len=1400,
                        min_raw=100.0, max_raw=9000.0),
                   dict(station="Station_DEMO", device="App1_Stream1_Det2", good=True,
                        last_update="2026-01-01 00:00:00", num_points=900, trace_len=900,
                        min_raw=100.0, max_raw=8000.0)],
               test_present=False)
    opc.update(over)
    return opc


def _healthy_tx():
    return dict(ok=True, endpoint="opc.tcp://127.0.0.1:4840/ChromExtract", steps=[
        dict(name="连接建会话", ok=True, detail="已连接", latency_ms=10.0),
        dict(name="读 ServerVersion", ok=True, detail="服务版本：2.4", latency_ms=5.0),
        dict(name="读 Diagnostics/TestCounter", ok=True, detail="TestCounter=11", latency_ms=5.0),
        dict(name="订阅 TestSignal+Heartbeat", ok=True, detail="收到 4 条通知，平均间隔 1.50 秒", latency_ms=6000.0),
        dict(name="读真实通道曲线", ok=True, detail="NumPoints=1400，ValueArray 1400 点", latency_ms=20.0),
        dict(name="EchoTest 往返", ok=True, detail="回显一致", latency_ms=5.0)])


class DiagnosisUnits(unittest.TestCase):
    def test_light_diagnosis(self):
        self.assertEqual(sa._light_diagnosis(2, 2, 5, 7200)[0], "正常")
        self.assertEqual(sa._light_diagnosis(0, 0, 0, 7200)[0], "注意")
        self.assertEqual(sa._light_diagnosis(2, 0, 5, 7200)[0], "异常")
        self.assertEqual(sa._light_diagnosis(2, 2, 99999, 7200)[0], "注意")

    def test_channels_good_false_means_datasource_lost(self):
        chans = [dict(station="Station_X", device="App1_Stream1_Det1", good=False,
                      last_update="2026-01-01 00:00:00", num_points=100, trace_len=100,
                      min_raw=1.0, max_raw=2.0)]
        f = sa._diagnose_channels(chans, channel_count=1, good_count=0)
        self.assertEqual(f["level"], "fail")
        self.assertIn("数据源丢失", f["advice"])
        # 长度不一致 -> 解析异常
        chans[0]["good"] = True
        chans[0]["trace_len"] = 99
        f2 = sa._diagnose_channels(chans, channel_count=1, good_count=1)
        self.assertEqual(f2["level"], "fail")
        self.assertIn("解析异常", f2["advice"])

    def test_log_tail_counts(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "server.log")
            cfg = _diag_cfg(tmp, log_file=p)   # 先建配置（会写一个干净日志），再覆盖成带错误的日志
            with open(p, "w", encoding="utf-8") as f:
                f.write("2026-01-01 [ERROR] 扫描出错：boom\n")
                f.write("2026-01-01 [ERROR] 扫描出错：boom2\n")
                f.write("2026-01-01 [ERROR] 更新节点失败（x）：bad\n")
                f.write("2026-01-01 [WARNING] 登录失败：用户 alice\n")
                f.write("2026-01-01 [WARNING] 已拒绝不在白名单内的客户端证书（urn:x）\n")
                f.write("2026-01-01 [INFO] 正常一行\n")
            scan = sa._scan_log_tail(p, 200)
            self.assertTrue(scan["exists"])
            self.assertEqual(scan["counts"]["扫描出错"], 2)
            self.assertEqual(scan["counts"]["更新节点失败"], 1)
            self.assertEqual(scan["counts"]["登录失败"], 1)
            self.assertEqual(scan["counts"]["证书拒绝"], 1)
            self.assertEqual(scan["total_hits"], 5)
            self.assertIn("扫描出错", scan["first_excerpt"])
            # diagnose 里日志一项应带出计数（cfg 已在上面建好，不要再调 _diag_cfg 覆盖日志）
            result = sa.diagnose(cfg, transmission_result=_healthy_tx(), _opc=_healthy_opc())
            log_f = [x for x in result["findings"] if x["name"] == "近期日志"][0]
            self.assertEqual(log_f["level"], "warn")
            self.assertIn("扫描出错×2", log_f["evidence"])
            self.assertEqual(result["log_scan"]["counts"]["扫描出错"], 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_watch_dirs_empty_dir_fails(self):
        tmp = tempfile.mkdtemp()
        try:
            empty = os.path.join(tmp, "empty")
            os.makedirs(empty)
            cfg = _diag_cfg(tmp, watch_dirs=[empty])
            result = sa.diagnose(cfg, transmission_result=_healthy_tx(), _opc=_healthy_opc())
            watch_f = [x for x in result["findings"] if x["name"] == "监控目录"][0]
            self.assertEqual(watch_f["level"], "fail")
            self.assertIn("监控目录", watch_f["advice"] + watch_f["evidence"])
            self.assertEqual(result["overall"], "异常")
            self.assertTrue(any("watch_dirs" in a for a in result["actions"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_good_false_snapshot_diagnosed(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg = _diag_cfg(tmp)
            opc = _healthy_opc(
                diag=dict(channel_count=1, good_count=0, total_points=100,
                          last_change="2026-01-01 00:00:00", data_age=5.0,
                          status="数据较旧", test_counter=11),
                channels=[dict(station="Station_X", device="App1_Stream1_Det1", good=False,
                               last_update="2026-01-01 00:00:00", num_points=100, trace_len=100,
                               min_raw=1.0, max_raw=2.0)])
            result = sa.diagnose(cfg, transmission_result=_healthy_tx(), _opc=opc)
            ch_f = [x for x in result["findings"] if x["name"] == "通道健康"][0]
            self.assertEqual(ch_f["level"], "fail")
            self.assertIn("数据源丢失", ch_f["advice"])
            self.assertEqual(result["overall"], "异常")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_healthy_injected_is_normal(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg = _diag_cfg(tmp)
            result = sa.diagnose(cfg, transmission_result=_healthy_tx(), _opc=_healthy_opc())
            self.assertEqual(result["overall"], "正常", result["findings"])
            self.assertEqual(result["actions"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DiagnosisIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_healthy_demo_overall_normal(self):
        tx = asyncio.run(sa.transmission_check_async(self.srv.url, duration_sec=6))
        self.assertTrue(tx["ok"], tx["steps"])
        cfg = dict(sa.DEFAULT_CONFIG)
        cfg.update(endpoint=self.srv.url, watch_dirs=[self.srv.data], poll_seconds=5,
                   log_file=os.path.join(self.srv.dir, "server.log"),
                   _base_dir=self.srv.dir, _cfg_path=self.srv.cfg_path)
        result = sa.diagnose(cfg, transmission_result=tx)
        self.assertEqual(result["overall"], "正常", result["findings"])

    def test_diagnosis_nodes_readable_and_readonly(self):
        async def go():
            c = await _client(self.srv.url)
            try:
                ns, root = await _root(c)
                diag = await root.get_child(["%d:Diagnostics" % ns])
                level = await (await diag.get_child(["%d:DiagnosisLevel" % ns])).read_value()
                summary = await (await diag.get_child(["%d:DiagnosisSummary" % ns])).read_value()
                when = await (await diag.get_child(["%d:LastDiagnosisTime" % ns])).read_value()
                self.assertIn(level, ("正常", "注意", "异常"))
                self.assertTrue(str(summary))
                self.assertTrue(str(when))
                node = await diag.get_child(["%d:DiagnosisLevel" % ns])
                with self.assertRaises(Exception):
                    await node.write_value("正常")
            finally:
                await c.disconnect()
        asyncio.run(go())


class NettestOpcuaTransmission(unittest.TestCase):
    """v2.7：连接测试的 OPC UA 项要读出数据传输数据（Diagnostics），不只是连通。"""

    @classmethod
    def setUpClass(cls):
        cls.srv = _Server()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def test_opcua_probe_reads_transmission_data(self):
        import nettest
        r = nettest.test_opcua(self.srv.url, timeout=10)
        self.assertTrue(r["ok"], r)
        text = "\n".join(r.get("details") or [])
        self.assertIn("数据传输：心跳计数", text)
        self.assertIn("通道", text)
        self.assertIn("订阅心跳 3 秒收到", text)


if __name__ == "__main__":
    unittest.main()
