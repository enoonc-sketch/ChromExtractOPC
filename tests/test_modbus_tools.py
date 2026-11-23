# -*- coding: utf-8 -*-
"""Modbus Poll 主站 + Slave 从站模拟器（v2.8）：四区读写往返、异常响应、
数据类型×字节序编解码、TCP 回环互测、RTU 假串口互测。不需要真实硬件。"""
import os
import struct
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import modbus_tools as mt  # noqa: E402
import nettest as nt  # noqa: E402


class TypedCodec(unittest.TestCase):
    def test_float32_all_orders_roundtrip(self):
        for order in mt.BYTE_ORDERS:
            regs = mt.encode_value(123.45, "float32", order)
            self.assertEqual(len(regs), 2)
            self.assertAlmostEqual(mt.decode_value(regs, "float32", order), 123.45, places=4)

    def test_float32_known_bytes(self):
        # 123.45 的 IEEE754 大端字节 42 F6 E6 66（与 485 配型测试同一口径）
        self.assertEqual(mt.encode_value(123.45, "float32", "ABCD"), [0x42F6, 0xE666])
        self.assertEqual(mt.encode_value(123.45, "float32", "CDAB"), [0xE666, 0x42F6])
        self.assertEqual(mt.encode_value(123.45, "float32", "DCBA"), [0x66E6, 0xF642])

    def test_int_and_float64_roundtrip(self):
        for dtype, val in (("int16", -1234), ("uint16", 60000), ("int32", -100000),
                           ("uint32", 4000000000), ("float64", 3.14159265358979)):
            for order in mt.BYTE_ORDERS:
                regs = mt.encode_value(val, dtype, order)
                self.assertEqual(len(regs), mt.REG_COUNT[dtype])
                got = mt.decode_value(regs, dtype, order)
                if isinstance(val, float):
                    self.assertAlmostEqual(got, val, places=12)
                else:
                    self.assertEqual(got, val)

    def test_decode_regs_steps(self):
        regs = mt.encode_value(1.5, "float32", "ABCD") + mt.encode_value(2.5, "float32", "ABCD")
        got = mt.decode_regs(regs, "float32", "ABCD")
        self.assertEqual([o for o, _v in got], [0, 2])
        self.assertAlmostEqual(got[1][1], 2.5)

    def test_parse_values(self):
        self.assertEqual(mt.parse_values("1,0,1", 15), [1, 0, 1])
        self.assertEqual(mt.parse_values("10, 20", 16), [10, 20])
        regs = mt.parse_values("123.45", 16, "float32", "ABCD")
        self.assertEqual(regs, [0x42F6, 0xE666])
        with self.assertRaises(nt.TestError):
            mt.parse_values("2", 15)
        with self.assertRaises(nt.TestError):
            mt.parse_values("1,2", 6)


class PduAndStore(unittest.TestCase):
    def setUp(self):
        self.store = mt.DataStore(size=100)

    def test_read_write_all_areas(self):
        s = self.store
        s.write_bits("coils", 0, [1, 0, 1])
        s.write_bits("discrete", 5, [1, 1])
        s.write_regs("holding", 10, [111, 222])
        s.write_regs("input", 20, [333])
        self.assertEqual(s.read_bits("coils", 0, 3), [1, 0, 1])
        self.assertEqual(s.read_bits("discrete", 5, 2), [1, 1])
        self.assertEqual(s.read_regs("holding", 10, 2), [111, 222])
        self.assertEqual(s.read_regs("input", 20, 1), [333])

    def test_typed_store(self):
        self.store.set_typed("holding", 4, 123.45, "float32", "CDAB")
        self.assertAlmostEqual(self.store.get_typed("holding", 4, "float32", "CDAB"), 123.45, places=4)
        self.assertEqual(self.store.read_regs("holding", 4, 2), [0xE666, 0x42F6])

    def test_handle_pdu_read_write(self):
        s = self.store
        r = mt.handle_pdu(s, mt.build_write_pdu(16, 10, [1, 2, 3]))
        self.assertEqual(mt.parse_write_pdu(16, r), (10, 3))
        r = mt.handle_pdu(s, mt.build_read_pdu(3, 10, 3))
        self.assertEqual(mt.parse_read_pdu(3, 3, r), ([1, 2, 3], "寄存器"))
        r = mt.handle_pdu(s, mt.build_write_pdu(5, 7, 1))
        self.assertEqual(s.read_bits("coils", 7, 1), [1])
        r = mt.handle_pdu(s, mt.build_write_pdu(15, 0, [1, 0, 1, 1]))
        self.assertEqual(mt.parse_read_pdu(1, 4, mt.handle_pdu(s, mt.build_read_pdu(1, 0, 4))),
                         ([1, 0, 1, 1], "位"))
        r = mt.handle_pdu(s, mt.build_write_pdu(6, 3, 999))
        self.assertEqual(mt.parse_write_pdu(6, r), (3, 999))

    def test_exceptions(self):
        s = self.store
        # 越界地址 → 异常码 2
        r = mt.handle_pdu(s, mt.build_read_pdu(3, 99, 2))
        self.assertEqual(r, bytes([0x83, 2]))
        # 未知功能码 → 异常码 1
        r = mt.handle_pdu(s, bytes([7, 0, 0, 0, 1]))
        self.assertEqual(r, bytes([0x87, 1]))
        # 数量超限 → 异常码 3
        r = mt.handle_pdu(s, struct.pack(">BHH", 3, 0, 126))
        self.assertEqual(r, bytes([0x83, 3]))
        # 写单线圈非法值 → 异常码 3
        r = mt.handle_pdu(s, struct.pack(">BHH", 5, 0, 0x1234))
        self.assertEqual(r, bytes([0x85, 3]))
        # 16 字节数与数量对不上 → 异常码 3
        r = mt.handle_pdu(s, struct.pack(">BHHB", 16, 0, 2, 2) + b"\x00\x01")
        self.assertEqual(r, bytes([0x90, 3]))


class TcpLoopback(unittest.TestCase):
    """自家主站 ↔ 自家从站 TCP 回环互测。"""

    @classmethod
    def setUpClass(cls):
        cls.store = mt.DataStore(size=1000)
        cls.logs = []
        cls.slave = mt.TcpSlave(cls.store, host="127.0.0.1", port=0, unit=1,
                                log=cls.logs.append).start()
        cls.master = mt.TcpMaster("127.0.0.1", cls.slave.port, unit=1, timeout=3).connect()

    @classmethod
    def tearDownClass(cls):
        cls.master.close()
        cls.slave.stop()

    def test_write_then_read_back(self):
        m = self.master
        r = m.write(16, 100, mt.parse_values("123.45", 16, "float32", "ABCD"))
        self.assertTrue(r["ok"], r["summary"])
        r = m.read(3, 100, 2)
        self.assertTrue(r["ok"], r["summary"])
        self.assertAlmostEqual(mt.decode_value(r["values"], "float32", "ABCD"), 123.45, places=4)
        self.assertIn("发送：", r["details"][0])

    def test_bits_and_single_writes(self):
        m = self.master
        self.assertTrue(m.write(5, 10, 1)["ok"])
        self.assertTrue(m.write(15, 20, [1, 1, 0, 1])["ok"])
        r = m.read(1, 20, 4)
        self.assertEqual(r["values"], [1, 1, 0, 1])
        self.assertTrue(m.write(6, 30, 4242)["ok"])
        self.assertEqual(m.read(3, 30, 1)["values"], [4242])

    def test_input_areas_are_readable(self):
        self.store.write_regs("input", 50, [7, 8])
        self.store.write_bits("discrete", 60, [0, 1])
        self.assertEqual(self.master.read(4, 50, 2)["values"], [7, 8])
        self.assertEqual(self.master.read(2, 60, 2)["values"], [0, 1])

    def test_out_of_range_exception(self):
        r = self.master.read(3, 999, 2)  # 数据区只有 1000 个
        self.assertFalse(r["ok"])
        self.assertEqual(r["exc_code"], 2)
        self.assertIn("非法数据地址", r["summary"])

    def test_poll_rounds(self):
        got = []
        n = mt.poll(self.master, 3, 30, 1, interval_sec=0.05,
                    callback=got.append, max_rounds=3)
        self.assertEqual(n, 3)
        self.assertTrue(all(r["ok"] for r in got))

    def test_wrong_unit_no_response(self):
        bad = mt.TcpMaster("127.0.0.1", self.slave.port, unit=9, timeout=0.6).connect()
        try:
            r = bad.read(3, 0, 1)
            self.assertFalse(r["ok"])
        finally:
            bad.close()

    def test_slave_log_recorded(self):
        self.master.read(3, 0, 1)
        time.sleep(0.2)
        self.assertTrue(any(e["func"] == 3 and e["resp_hex"] for e in self.logs))


class _SerialEnd:
    """内存串口一端：write 写到对端读缓冲，read 取自己缓冲。"""

    def __init__(self):
        self.rx = bytearray()
        self.peer = None
        self._lock = threading.Lock()

    def _feed(self, data):
        with self._lock:
            self.rx += data

    def write(self, data):
        self.peer._feed(bytes(data))
        return len(data)

    def read(self, n=1):
        with self._lock:
            d = bytes(self.rx[:n])
            del self.rx[:n]
            return d

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self._lock:
            self.rx.clear()

    def close(self):
        pass


def _serial_pair():
    a, b = _SerialEnd(), _SerialEnd()
    a.peer, b.peer = b, a
    return a, b


class RtuLoopback(unittest.TestCase):
    """自家 RTU 主站 ↔ 自家 RTU 从站，内存假串口互测（不碰真硬件）。"""

    @classmethod
    def setUpClass(cls):
        cls.store = mt.DataStore(size=1000)
        cls.store.write_regs("holding", 0, [0x42F6, 0xE666, 10, 20])
        master_end, slave_end = _serial_pair()
        # 主从是两根线两端、两个口名（同一口名会被 port_lock 判为占用，与真实情况一致）
        cls.slave = mt.RtuSlave(cls.store, "COM99", unit=7,
                                factory=lambda **k: slave_end).start()
        cls.master = mt.RtuMaster("COM98", unit=7, timeout=2.0,
                                  factory=lambda **k: master_end).connect()

    @classmethod
    def tearDownClass(cls):
        cls.master.close()
        cls.slave.stop()

    def test_rtu_read(self):
        r = self.master.read(3, 0, 4)
        self.assertTrue(r["ok"], r["summary"])
        self.assertEqual(r["values"], [0x42F6, 0xE666, 10, 20])
        self.assertAlmostEqual(mt.decode_value(r["values"][:2], "float32", "ABCD"), 123.45, places=4)

    def test_rtu_write_and_read_back(self):
        r = self.master.write(6, 5, 777)
        self.assertTrue(r["ok"], r["summary"])
        self.assertEqual(self.master.read(3, 5, 1)["values"], [777])
        r = self.master.write(15, 8, [1, 0, 1])
        self.assertTrue(r["ok"], r["summary"])
        self.assertEqual(self.master.read(1, 8, 3)["values"], [1, 0, 1])

    def test_rtu_exception(self):
        r = self.master.read(3, 999, 2)
        self.assertFalse(r["ok"])
        self.assertEqual(r["exc_code"], 2)


if __name__ == "__main__":
    unittest.main()
