# -*- coding: utf-8 -*-
"""v3.0 通讯诊断（commscan.py）：IP 范围 / 端口解析、并发 IP 扫描、端口扫描、ARP 解析、CSV 导出、
串口列表、波特率自动探测、Modbus RTU 站号扫描、串口锁互斥、只读保证。
全部用假的 ping / socket / 反向解析 / arp / 串口工厂，不需要网络和硬件。"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import commscan as cs  # noqa: E402
import nettest as nt   # noqa: E402


class Parse(unittest.TestCase):
    def test_cidr_range_single(self):
        self.assertEqual(len(cs.parse_ip_targets("192.168.1.0/24")), 254)
        self.assertEqual(cs.parse_ip_targets("192.168.1.0/24")[0], "192.168.1.1")
        self.assertEqual(cs.parse_ip_targets("10.0.0.5-10.0.0.7"), ["10.0.0.5", "10.0.0.6", "10.0.0.7"])
        self.assertEqual(cs.parse_ip_targets("10.0.0.250-252"), ["10.0.0.250", "10.0.0.251", "10.0.0.252"])
        self.assertEqual(cs.parse_ip_targets("10.0.0.9-10.0.0.8"), ["10.0.0.8", "10.0.0.9"])
        self.assertEqual(cs.parse_ip_targets("1.2.3.4, 1.2.3.4\n1.2.3.5"), ["1.2.3.4", "1.2.3.5"])
        self.assertEqual(cs.parse_ip_targets("192.168.5.7/32"), ["192.168.5.7"])

    def test_limits_and_errors(self):
        self.assertEqual(len(cs.parse_ip_targets("10.0.0.0/22")), 1022)
        for bad in ("", "10.0.0.0/16", "10.0.0.1-10.0.8.1", "abc", "300.1.1.1", "::1", "10.0.0.0/33"):
            with self.assertRaises(cs.CommScanError, msg=bad):
                cs.parse_ip_targets(bad)

    def test_ports(self):
        self.assertEqual(cs.parse_ports("502, 4840 8000-8002,502"), [502, 4840, 8000, 8001, 8002])
        self.assertEqual(len(cs.parse_ports("1-1024")), 1024)
        for bad in ("0", "70000", "a", "1-2000", ""):
            if bad == "":
                self.assertEqual(cs.parse_ports(bad), [])
                continue
            with self.assertRaises(cs.CommScanError, msg=bad):
                cs.parse_ports(bad)

    def test_services_and_wiring(self):
        for p in cs.INDUSTRIAL_PORTS:
            self.assertTrue(cs.service_name(p), p)
        self.assertIn("Modbus", cs.service_name(502))
        self.assertIn("OPC UA", cs.service_name(4840))
        self.assertEqual(cs.service_name(1), "")
        self.assertIn("A 接 A", cs.wiring_tip("RS485"))
        self.assertIn("TX+", cs.wiring_tip("rs422"))
        self.assertIn("GND", cs.wiring_tip("RS232"))


class _P:
    def __init__(self, out, rc=0):
        self.stdout, self.stderr, self.returncode = out.encode("utf-8"), b"", rc


class Primitives(unittest.TestCase):
    def test_ping_parse(self):
        ok = _P("来自 192.168.1.1 的回复: 字节=32 时间=3ms TTL=64")
        self.assertEqual(cs.ping_once("192.168.1.1", runner=lambda c: ok), (True, 3.0))
        lt = _P("Reply from 10.0.0.1: bytes=32 time<1ms TTL=128")
        self.assertEqual(cs.ping_once("10.0.0.1", runner=lambda c: lt), (True, 0.5))
        unreachable = _P("来自 192.168.1.5 的回复: 无法访问目标主机。", 1)
        self.assertEqual(cs.ping_once("192.168.1.9", runner=lambda c: unreachable), (False, None))
        cmds = []
        cs.ping_once("1.2.3.4", runner=lambda c: cmds.append(c) or ok)
        self.assertIsInstance(cmds[0], list)                    # 参数列表，不经过命令行解释
        self.assertEqual(cmds[0][-1], "1.2.3.4")

        def boom(c):
            raise FileNotFoundError("ping")
        self.assertEqual(cs.ping_once("1.2.3.4", runner=boom), (False, None))

    def test_tcp_probe(self):
        class S:
            closed = False

            def close(self):
                S.closed = True

        def conn_open(addr, timeout):
            return S()

        def conn_refused(addr, timeout):
            raise ConnectionRefusedError()

        def conn_timeout(addr, timeout):
            raise socket.timeout()
        self.assertEqual(cs.tcp_probe("1.1.1.1", 502, connect=conn_open)[0], "open")
        self.assertTrue(S.closed)                                # 连上立即断开
        self.assertEqual(cs.tcp_probe("1.1.1.1", 502, connect=conn_refused), ("closed", None))
        self.assertEqual(cs.tcp_probe("1.1.1.1", 502, connect=conn_timeout), ("filtered", None))

    def test_reverse_dns(self):
        self.assertEqual(cs.reverse_dns("1.1.1.1", resolver=lambda ip: ("plc-01", [], [ip])), "plc-01")
        self.assertEqual(cs.reverse_dns("1.1.1.1", resolver=lambda ip: ("1.1.1.1", [], [])), "")

        def fail(ip):
            raise socket.herror()
        self.assertEqual(cs.reverse_dns("1.1.1.1", resolver=fail), "")

    def test_arp_parse(self):
        win = """
接口: 192.168.1.100 --- 0x5
  Internet 地址         物理地址              类型
  192.168.1.1           00-1a-2b-3c-4d-5e     动态
  192.168.1.20          aa-bb-cc-dd-ee-ff     动态
  192.168.1.255         ff-ff-ff-ff-ff-ff     静态
"""
        nix = "? (10.0.0.2) at 00:11:22:33:44:55 [ether] on eth0\n"
        t = cs.parse_arp(win + nix)
        self.assertEqual(t["192.168.1.1"], "00-1A-2B-3C-4D-5E")
        self.assertEqual(t["10.0.0.2"], "00-11-22-33-44-55")
        self.assertNotIn("192.168.1.255", t)
        self.assertEqual(cs.read_arp_table(runner=lambda c: _P(win))["192.168.1.20"], "AA-BB-CC-DD-EE-FF")

        def boom(c):
            raise OSError()
        self.assertEqual(cs.read_arp_table(runner=boom), {})


class _Sock:
    def close(self):
        pass


class FakeNet:
    """假网络：alive 里的主机回 ping；open_ports 里的 (ip, port) 可连接；其余端口拒绝连接。"""

    def __init__(self, alive, open_ports):
        self.alive, self.open_ports = set(alive), set(open_ports)
        self.pings, self.conns = [], []
        self.lock = threading.Lock()

    def ping(self, ip):
        with self.lock:
            self.pings.append(ip)
        return (ip in self.alive), (1.5 if ip in self.alive else None)

    def connect(self, addr, timeout):
        with self.lock:
            self.conns.append(addr)
        if addr in self.open_ports:
            return _Sock()
        raise ConnectionRefusedError()


class IpScan(unittest.TestCase):
    def test_scan_finds_hosts_ports_mac(self):
        net = FakeNet(["10.0.0.3", "10.0.0.7"], [("10.0.0.7", 502), ("10.0.0.9", 4840)])
        prog = []
        r = cs.scan_ip_range("10.0.0.1-10.0.0.10", ports=[502, 4840], ping=net.ping, connect=net.connect,
                             resolver=lambda ip: ("plc-%s" % ip.split(".")[-1], [], []),
                             arp=lambda: {"10.0.0.7": "00-11-22-33-44-55"}, workers=4,
                             progress=lambda n, t, s: prog.append((n, t)))
        ips = [h["ip"] for h in r["hosts"]]
        self.assertEqual(ips, ["10.0.0.3", "10.0.0.7", "10.0.0.9"])          # 10.0.0.9 禁 ping 但端口开放
        h7 = r["hosts"][1]
        self.assertEqual(h7["mac"], "00-11-22-33-44-55")
        self.assertEqual(h7["hostname"], "plc-7")
        self.assertEqual([p["port"] for p in h7["open_ports"]], [502])
        self.assertEqual(r["hosts"][2]["via"], "端口")
        self.assertEqual((r["scanned"], r["total"], r["stopped"]), (10, 10, False))
        self.assertEqual(len(net.pings), 10)
        self.assertEqual(prog[-1], (10, 10))
        self.assertIn("3 台在线", r["summary"])

    def test_stop_midway(self):
        stop = threading.Event()

        def ping(ip):
            if ip.endswith(".5"):
                stop.set()
            return True, 1.0
        r = cs.scan_ip_range("10.0.1.1-10.0.1.200", ping=ping, connect=None, resolve_names=False,
                             arp=lambda: {}, workers=2, stop=stop)
        self.assertTrue(r["stopped"])
        self.assertLess(r["scanned"], 200)
        self.assertIn("中途停止", r["summary"])

    def test_limits(self):
        with self.assertRaises(cs.CommScanError):
            cs.scan_ip_range(["10.0.0.1"] * (cs.MAX_HOSTS + 1), ping=lambda ip: (False, None))
        with self.assertRaises(cs.CommScanError):
            cs.scan_ip_range("10.0.0.1", ports=list(range(1, 100)), ping=lambda ip: (False, None))

    def test_csv_export(self):
        net = FakeNet(["10.0.0.3"], [("10.0.0.3", 502)])
        r = cs.scan_ip_range("10.0.0.3", ports=[502], ping=net.ping, connect=net.connect,
                             resolver=lambda ip: ("=cmd|calc", [], []), arp=lambda: {})
        tmp = tempfile.mkdtemp()
        try:
            p = cs.write_csv(cs.hosts_csv_rows(r), os.path.join(tmp, "sub", cs.export_name("IP扫描")))
            text = open(p, encoding="utf-8-sig").read()
            self.assertIn("10.0.0.3", text)
            self.assertIn("502(Modbus TCP)", text)
            self.assertIn("'=cmd|calc", text)                               # 防 Excel 公式注入
            self.assertTrue(os.path.basename(p).startswith("通讯诊断_IP扫描_"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PortScan(unittest.TestCase):
    def test_scan(self):
        net = FakeNet([], [("10.0.0.5", 502), ("10.0.0.5", 102), ("10.0.0.5", 44818)])
        r = cs.scan_ports("10.0.0.5", "100-110,502,4840,44818", connect=net.connect, workers=8)
        self.assertEqual([o["port"] for o in r["open"]], [102, 502, 44818])
        self.assertIn("S7", r["open"][0]["service"])
        self.assertEqual(r["closed"], 11)
        self.assertEqual(r["total"], 14)
        self.assertEqual(cs.ports_csv_rows(r)[1][:2], ["10.0.0.5", 102])

    def test_filtered_and_bad_input(self):
        def to(addr, t):
            raise socket.timeout()
        r = cs.scan_ports("10.0.0.6", [80, 443], connect=to)
        self.assertEqual(r["filtered"], 2)
        self.assertIn("防火墙", r["summary"])
        with self.assertRaises(cs.CommScanError):
            cs.scan_ports("10.0.0.6", "", connect=to)
        with self.assertRaises(nt.TestError):
            cs.scan_ports("-bad host", [80], connect=to)


# ---------------------------------------------------------------------------
# 串口
# ---------------------------------------------------------------------------

class FakeBus:
    """内存 RS-485 总线：只有 baud/parity 都对时从站才应答；units 里的站号正常应答，exc_units 回异常码 2，
    garble_units 回 CRC 错的乱码；echo=True 模拟半双工转换器回显。记录所有发出的帧（检查只读）。"""

    def __init__(self, baud=19200, parity="E", units=(3, 7), exc_units=(), garble_units=(), echo=False):
        self.baud, self.parity = baud, parity
        self.units, self.exc_units, self.garble_units = set(units), set(exc_units), set(garble_units)
        self.echo = echo
        self.frames, self.opened = [], []

    def factory(self, **k):
        bus = self

        class Port:
            def __init__(self):
                self.buf = b""
                bus.opened.append((k.get("baud"), k.get("parity"), k.get("stopbits")))

            def reset_input_buffer(self):
                self.buf = b""

            def write(self, d):
                d = bytes(d)
                bus.frames.append(d)
                if bus.echo:
                    self.buf += d
                if k.get("baud") != bus.baud or k.get("parity") != bus.parity:
                    if k.get("baud") == bus.baud:
                        self.buf += b"\x7f\x13\x99"           # 校验位不对：收到乱码
                    return
                unit, func = d[0], d[1]
                if unit in bus.garble_units:
                    self.buf += bytes([unit, func, 2, 0, 1, 0, 0])
                    return
                if unit in bus.exc_units:
                    body = bytes([unit, func | 0x80, 2])
                elif unit in bus.units:
                    cnt = int.from_bytes(d[4:6], "big")
                    body = bytes([unit, func, cnt * 2]) + b"".join((100 + unit + i).to_bytes(2, "big") for i in range(cnt))
                else:
                    return
                self.buf += body + nt.crc16_modbus(body).to_bytes(2, "little")

            def flush(self):
                pass

            def read(self, n):
                d, self.buf = self.buf[:n], self.buf[n:]
                return d

            def close(self):
                pass
        return Port()

    def only_reads(self):
        return all(f[1] in (1, 2, 3, 4) for f in self.frames)


class SerialList(unittest.TestCase):
    def test_list_and_chip(self):
        class P:
            def __init__(self, dev, vid=None, pid=None, desc=""):
                self.device, self.vid, self.pid, self.description = dev, vid, pid, desc
                self.manufacturer, self.serial_number, self.hwid = "", "", ""
        ports = cs.scan_serial_ports(lister=lambda: [P("COM10", 0x1A86, 0x7523, "USB-SERIAL CH340"),
                                                     P("COM2"), P("COM3", 0x0403, 0x6001, "USB Serial Port"),
                                                     P("COM4", 0x9999, 0x0001)])
        self.assertEqual([p["device"] for p in ports], ["COM2", "COM3", "COM4", "COM10"])   # 按数字排序
        self.assertIn("FTDI", ports[1]["chip"])
        self.assertEqual(ports[1]["vidpid"], "0403:6001")
        self.assertIn("CH340", ports[3]["chip"])
        self.assertIn("内置", ports[0]["chip"])
        self.assertEqual(ports[2]["chip"], "未知芯片")


class BaudDetect(unittest.TestCase):
    def test_finds_baud_and_parity(self):
        bus = FakeBus(baud=19200, parity="E", units=(3,))
        prog = []
        r = cs.detect_baud("COM3", unit=3, timeout=0.02, factory=bus.factory,
                           progress=lambda n, t, s: prog.append(s))
        self.assertTrue(r["ok"])
        self.assertEqual((r["found"][0]["baud"], r["found"][0]["parity"]), (19200, "E"))
        self.assertIn("19200 波特", r["summary"])
        self.assertEqual(bus.opened[0][0], 1200)                       # 从 1200 开始依次尝试
        self.assertTrue(any("CRC" in x or "乱码" in x for x in r["log"]))  # 19200,N 收到乱码
        self.assertTrue(bus.only_reads())
        self.assertTrue(prog)

    def test_exception_reply_counts_and_echo(self):
        bus = FakeBus(baud=9600, parity="N", exc_units=(1,), echo=True)
        r = cs.detect_baud("COM4", unit=1, bauds=(4800, 9600), parities=("N",), timeout=0.02, factory=bus.factory)
        self.assertTrue(r["ok"])
        self.assertEqual(r["found"][0]["status"], "exception")
        self.assertEqual(r["tried"], 2)

    def test_not_found_and_stop(self):
        bus = FakeBus(baud=115200, parity="O", units=())
        r = cs.detect_baud("COM5", bauds=(9600, 19200), timeout=0.02, factory=bus.factory)
        self.assertFalse(r["ok"])
        self.assertEqual(r["tried"], 6)
        self.assertTrue(r["tips"])
        stop = threading.Event()
        stop.set()
        r = cs.detect_baud("COM5", timeout=0.02, factory=bus.factory, stop=stop)
        self.assertTrue(r["stopped"])
        self.assertEqual(r["tried"], 0)

    def test_write_function_codes_refused(self):
        for func in (5, 6, 15, 16):
            with self.assertRaises(cs.CommScanError):
                cs.detect_baud("COM3", func=func, factory=FakeBus().factory)
            with self.assertRaises(cs.CommScanError):
                cs.scan_modbus_rtu_slaves("COM3", func=func, factory=FakeBus().factory)


class SlaveScan(unittest.TestCase):
    def test_scan_range(self):
        bus = FakeBus(baud=9600, parity="N", units=(3, 7), exc_units=(5,), garble_units=(9,))
        r = cs.scan_modbus_rtu_slaves("COM6", baud=9600, parity="N", first=1, last=10, func=4, addr=10, count=2,
                                      timeout=0.02, factory=bus.factory)
        self.assertEqual([s["unit"] for s in r["slaves"]], [3, 5, 7])
        self.assertEqual(r["slaves"][1]["status"], "应答（异常）")
        self.assertIn("103 104", r["slaves"][0]["detail"])
        self.assertEqual(r["garbled"], [9])
        self.assertEqual((r["scanned"], r["total"]), (10, 10))
        self.assertEqual(len(bus.opened), 1)                          # 整个扫描只打开一次串口
        self.assertTrue(bus.only_reads())
        self.assertTrue(all(f[1] == 4 for f in bus.frames))
        self.assertIn("3、5、7", r["summary"])
        self.assertIn("站号 9", cs.result_text(r))

    def test_bad_range_and_stop(self):
        for a, b in ((0, 5), (5, 3), (1, 248)):
            with self.assertRaises(cs.CommScanError):
                cs.scan_modbus_rtu_slaves("COM6", first=a, last=b, factory=FakeBus().factory)
        stop = threading.Event()
        bus = FakeBus(baud=9600, parity="N", units=(2,))

        def prog(n, t, s):
            if n >= 3:
                stop.set()
        r = cs.scan_modbus_rtu_slaves("COM6", first=1, last=247, timeout=0.02, factory=bus.factory,
                                      stop=stop, progress=prog)
        self.assertTrue(r["stopped"])
        self.assertEqual(r["scanned"], 3)

    def test_port_lock_shared_with_nettest(self):
        """与 nettest（单次测试 / 稳定性测试 / 设备检测）共用同一把串口锁。"""
        lock = nt.port_lock("COM7")
        lock.acquire()
        try:
            t0 = time.monotonic()
            with self.assertRaises(cs.CommScanError) as cm:
                cs.scan_modbus_rtu_slaves("com7", first=1, last=2, factory=FakeBus().factory, lock_wait=0.2)
            self.assertIn("正被其它测试", str(cm.exception))
            with self.assertRaises(cs.CommScanError):
                cs.detect_baud("COM7", factory=FakeBus().factory, lock_wait=0.2)
            self.assertLess(time.monotonic() - t0, 5)
        finally:
            lock.release()

    def test_rs485_loopback_refused(self):
        r = cs.serial_loopback("COM3", interface="RS485")
        self.assertFalse(r["ok"])
        self.assertIn("半双工", r["summary"])


if __name__ == "__main__":
    unittest.main()
