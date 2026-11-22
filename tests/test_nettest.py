# -*- coding: utf-8 -*-
"""连接测试：Modbus 报文/CRC、串口（用模拟设备）、参数校验防注入、ping 输出解析。不需要真实硬件。"""
import os, sys, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import nettest as nt   # noqa: E402


class FakeSerial:
    """模拟串口设备：Modbus 从站 / 回环 / 不应答，可选 485 回显；每次 read 只吐一点，模拟真实串口。"""

    def __init__(self, mode="slave", echo=False, **k):
        self.buf, self.mode, self.echo = b"", mode, echo

    def reset_input_buffer(self):
        self.buf = b""

    def write(self, d):
        if self.echo:
            self.buf += d
        if self.mode == "loop":
            self.buf += d
        elif self.mode == "slave" and len(d) == 8 and nt.crc16_modbus(d[:-2]) == int.from_bytes(d[-2:], "little"):
            cnt = int.from_bytes(d[4:6], "big")
            body = bytes([d[0], d[1], cnt * 2]) + b"".join((100 + i).to_bytes(2, "big") for i in range(cnt))
            self.buf += body + nt.crc16_modbus(body).to_bytes(2, "little")

    def flush(self):
        pass

    def read(self, n):
        d, self.buf = self.buf[:min(n, 4)], self.buf[min(n, 4):]
        return d

    def close(self):
        pass


def dev(**kw):
    return lambda **k: FakeSerial(**kw)


class Modbus(unittest.TestCase):
    def test_crc_known_frame(self):
        self.assertEqual(nt.build_rtu_request(1, 3, 0, 10).hex(), "01030000000ac5cd")

    def test_parse_ok_crc_and_exception(self):
        req = nt.build_rtu_request(1, 3, 0, 2)
        pdu = bytes([1, 3, 4, 0, 100, 1, 44])
        ok = pdu + nt.crc16_modbus(pdu).to_bytes(2, "little")
        self.assertEqual(nt.parse_rtu_response(req, ok)[0], [100, 300])
        with self.assertRaises(nt.TestError):
            nt.parse_rtu_response(req, ok[:-1] + b"\x00")
        exc = bytes([1, 0x83, 2])
        with self.assertRaises(nt.TestError) as cm:
            nt.parse_rtu_response(req, exc + nt.crc16_modbus(exc).to_bytes(2, "little"))
        self.assertIn("非法数据地址", str(cm.exception))

    def test_args(self):
        for bad in ((1, 5, 0, 1), (1, 3, 40001 + 70000, 1), (1, 3, 0, 126), (300, 3, 0, 1)):
            with self.assertRaises(nt.TestError):
                nt.build_rtu_request(*bad)


class Serial(unittest.TestCase):
    def test_rtu_ok(self):
        r = nt.test_modbus_rtu("COM3", count=3, timeout=0.5, factory=dev())
        self.assertTrue(r["ok"], r)
        self.assertIn("地址 2：102", "\n".join(r["details"]))

    def test_rtu_with_485_echo(self):
        r = nt.test_modbus_rtu("COM3", count=5, timeout=0.5, factory=dev(echo=True))
        self.assertTrue(r["ok"], r)

    def test_rtu_no_answer(self):
        r = nt.test_modbus_rtu("COM3", timeout=0.2, factory=dev(mode="none"))
        self.assertFalse(r["ok"])

    def test_loopback(self):
        self.assertTrue(nt.test_serial_loopback("COM3", timeout=0.3, factory=dev(mode="loop"))["ok"])
        self.assertFalse(nt.test_serial_loopback("COM3", timeout=0.2, factory=dev(mode="none"))["ok"])

    def test_send_receive(self):
        r = nt.test_serial("COM3", send="01 03 00 00 00 02 C4 0B", timeout=0.3, factory=dev())
        self.assertTrue(r["ok"])


class Validation(unittest.TestCase):
    def test_rejects_bad_input(self):
        for kind, p in (("serial", {"port": "COM3;del x"}), ("serial", {"port": "COM3", "baud": "x"}),
                        ("serial", {"port": "COM3", "parity": "X"}), ("serial", {"port": "COM3", "send": "zz"}),
                        ("ping", {"host": "-f 1.1.1.1"}), ("ping", {"host": "a b"}),
                        ("tcp", {"host": "1.1.1.1", "port": "70000"}), ("opcua", {"endpoint": "http://x"})):
            r = nt.run_test(kind, p)
            self.assertFalse(r["ok"], (kind, p))

    def test_unknown_kind(self):
        with self.assertRaises(nt.TestError):
            nt.run_test("telnet", {})

    def test_timeout_capped(self):
        self.assertEqual(nt._timeout(999), nt.MAX_TIMEOUT)


class Ping(unittest.TestCase):
    class P:
        def __init__(self, out):
            self.stdout, self.stderr = out, b""

    def test_chinese_windows_output(self):
        out = "来自 192.168.1.10 的回复: 字节=32 时间=1ms TTL=64\r\n".encode("gbk") * 2
        r = nt.test_ping("192.168.1.10", count=2, runner=lambda c: self.P(out))
        self.assertTrue(r["ok"])
        self.assertIn("2/2", r["summary"])

    def test_timeout_output(self):
        r = nt.test_ping("192.168.1.10", count=2, runner=lambda c: self.P("请求超时。\r\n".encode("gbk") * 2))
        self.assertFalse(r["ok"])

    def test_command_is_list(self):
        seen = []
        nt.test_ping("10.0.0.1", runner=lambda c: (seen.append(c), self.P(b""))[1])
        self.assertIsInstance(seen[0], list)          # 不经过 shell，地址不会被当命令执行
        self.assertEqual(seen[0][-1], "10.0.0.1")


class Network(unittest.TestCase):
    def test_tcp_local_listener(self):
        import socket
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        try:
            r = nt.test_tcp("127.0.0.1", srv.getsockname()[1], timeout=2)
            self.assertTrue(r["ok"], r)
        finally:
            srv.close()

    def test_tcp_refused(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertFalse(nt.test_tcp("127.0.0.1", port, timeout=1)["ok"])


if __name__ == "__main__":
    unittest.main()
