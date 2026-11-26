# -*- coding: utf-8 -*-
"""v3.1 Modbus TCP 数据转发（modbus_server.py）测试：帧解析、只读（非 03/04 返回异常 01）、
地址越界、站号、字节序、映射表校验与自动生成、白名单、真实 socket 回环、映射 CSV 导出。"""
import csv
import os
import shutil
import socket
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import modbus_server as ms  # noqa: E402


def frame(func, start, qty, unit=1, tid=7):
    pdu = struct.pack(">BHH", func, start, qty)
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


class Frames(unittest.TestCase):
    regs = {0: 1, 1: 2, 2: 0xABCD, 9: 9}

    def test_read_holding_and_input(self):
        for fc in (3, 4):
            resp = ms.handle_frame(frame(fc, 0, 3), self.regs, 9)
            tid, proto, ln, uid = struct.unpack(">HHHB", resp[:7])
            self.assertEqual((tid, proto, uid), (7, 0, 1))
            self.assertEqual(ln, len(resp) - 6)
            self.assertEqual(resp[7], fc)
            self.assertEqual(resp[8], 6)
            self.assertEqual(struct.unpack(">HHH", resp[9:15]), (1, 2, 0xABCD))

    def test_gaps_read_as_zero(self):
        resp = ms.handle_frame(frame(3, 3, 7), self.regs, 9)
        vals = struct.unpack(">7H", resp[9:])
        self.assertEqual(vals[-1], 9)
        self.assertEqual(vals[0], 0)

    def test_write_functions_rejected_with_exception_01(self):
        for fc in (1, 2, 5, 6, 15, 16, 22, 23, 43):
            pdu = bytes((fc,)) + b"\x00\x00\x00\x01\x02\x00\x01"
            resp = ms.handle_pdu(pdu, self.regs, 9)
            self.assertEqual(resp, bytes((fc | 0x80, 1)), fc)

    def test_illegal_address_and_quantity(self):
        self.assertEqual(ms.handle_pdu(struct.pack(">BHH", 3, 8, 5), self.regs, 9), b"\x83\x02")
        self.assertEqual(ms.handle_pdu(struct.pack(">BHH", 3, 0, 0), self.regs, 9), b"\x83\x03")
        self.assertEqual(ms.handle_pdu(struct.pack(">BHH", 4, 0, 126), self.regs, 200), b"\x84\x03")
        self.assertEqual(ms.handle_pdu(b"\x03\x00", self.regs, 9), b"\x83\x03")
        self.assertEqual(ms.handle_pdu(struct.pack(">BHH", 3, 0, 1), {}, -1), b"\x83\x02")

    def test_bad_protocol_closes(self):
        bad = bytearray(frame(3, 0, 1))
        bad[2] = 1
        self.assertIsNone(ms.handle_frame(bytes(bad), self.regs, 9))
        self.assertIsNone(ms.handle_frame(b"\x00\x01", self.regs, 9))
        wrong_len = bytearray(frame(3, 0, 1))
        wrong_len[5] = 9
        self.assertIsNone(ms.handle_frame(bytes(wrong_len), self.regs, 9))

    def test_unit_filter(self):
        resp = ms.handle_frame(frame(3, 0, 1, unit=5), self.regs, 9, unit=1)
        self.assertEqual(resp[7:], b"\x83\x0b")
        resp = ms.handle_frame(frame(3, 0, 1, unit=5), self.regs, 9, unit=0)
        self.assertEqual(resp[7], 3)


class Encoding(unittest.TestCase):
    def test_float32_orders(self):
        regs = ms.encode_value(123.456, "float32", "ABCD")
        b = struct.pack(">f", 123.456)
        self.assertEqual(regs, [b[0] << 8 | b[1], b[2] << 8 | b[3]])
        self.assertEqual(ms.encode_value(123.456, "float32", "CDAB"), [regs[1], regs[0]])
        for o in ms.ORDERS:
            self.assertAlmostEqual(ms.decode_value(ms.encode_value(-7.25, "float32", o), "float32", o), -7.25)
        self.assertEqual(ms.decode_value(ms.encode_value(70000, "uint32", "CDAB"), "uint32", "CDAB"), 70000)
        self.assertEqual(ms.decode_value(ms.encode_value(-5, "int16"), "int16"), -5)

    def test_clamping_and_nan(self):
        self.assertEqual(ms.encode_value(70000, "uint16"), [65535])
        self.assertEqual(ms.encode_value(-1, "uint16"), [0])
        self.assertEqual(ms.encode_value(float("nan"), "float32"), [0, 0])
        self.assertEqual(ms.encode_value(None, "int32"), [0, 0])


class Mapping(unittest.TestCase):
    def test_normalize(self):
        m = ms.normalize_map([{"address": 10, "channel": "a", "field": "cycle_id", "type": "uint32"},
                              {"address": 0, "channel": "", "field": "service_heartbeat"}])
        self.assertEqual([x["address"] for x in m], [0, 10])
        self.assertEqual(m[0]["channel"], "*")
        with self.assertRaises(ms.MapError):
            ms.normalize_map([{"address": 0, "channel": "a", "field": "cycle_id", "type": "uint32"},
                              {"address": 1, "channel": "a", "field": "good", "type": "uint16"}])
        with self.assertRaises(ms.MapError):
            ms.normalize_map([{"address": 65535, "channel": "a", "field": "cycle_id", "type": "float32"}])
        with self.assertRaises(ms.MapError):
            ms.normalize_map([{"address": 0, "channel": "a", "field": "nope"}])
        with self.assertRaises(ms.MapError):
            ms.normalize_map([{"address": 0, "channel": "", "field": "cycle_id"}])
        with self.assertRaises(ms.MapError):
            ms.normalize_map([{"address": 0, "channel": "a", "field": "peak_area_21", "type": "float32"}])

    def test_auto_map_and_values(self):
        chans = [("s|1|1|1", "通道A"), ("s|1|2|1", "通道B")]
        m = ms.auto_map(chans, block=50, peaks=3)
        self.assertEqual(len({x["address"] for x in m}), len(m))
        ctx = ms.Context()
        recs = [{"source": "s", "app": 1, "stream": 1, "det": 1, "cycle_id": 123456, "num_points": 600,
                 "cycle_time": "2026-11-26 10:00:00", "duration": 300.0},
                {"source": "s", "app": 1, "stream": 2, "det": 1, "cycle_id": 5, "num_points": 10,
                 "cycle_time": "2026-11-26 10:00:00", "duration": 60.0}]
        pk = {"s|1|1|1": [{"rt": 50.0, "height": 100.0, "area": 501.3, "area_pct": 60.0},
                          {"rt": 120.0, "height": 40.0, "area": 300.8, "area_pct": 40.0}]}
        snap = {"active": [{"key": "stale:s|1|1|1", "level": "警告", "text": "x"},
                           {"key": "scan_error", "level": "严重", "text": "y"}]}
        ms.update_context(ctx, recs, snap, pk, clients=3)
        from datetime import datetime
        regs = ms.build_registers(m, ctx, "CDAB", now=datetime(2026, 11, 26, 10, 30, 0))

        def val(ch, field):
            it = next(x for x in m if x["channel"] == ch and x["field"] == field)
            n = ms.TYPES[it["type"]]
            return ms.decode_value([regs[it["address"] + i] for i in range(n)], it["type"], "CDAB")
        self.assertEqual(val("*", "service_heartbeat"), 1)
        self.assertEqual(val("*", "alert_count"), 2)
        self.assertEqual(val("*", "alert_severity"), 2)
        self.assertEqual(val("*", "client_count"), 3)
        self.assertEqual(val("*", "channel_count"), 2)
        self.assertEqual(val("s|1|1|1", "cycle_id"), 123456)
        self.assertEqual(val("s|1|1|1", "alerts"), 1)
        self.assertEqual(val("s|1|1|1", "good"), 1)
        self.assertAlmostEqual(val("s|1|1|1", "age_min"), 30.0, places=3)
        self.assertEqual(val("s|1|1|1", "peak_count"), 2)
        self.assertAlmostEqual(val("s|1|1|1", "peak_area_total"), 802.1, places=2)
        self.assertAlmostEqual(val("s|1|1|1", "peak_rt_2"), 120.0, places=3)
        self.assertEqual(val("s|1|1|1", "peak_area_3"), 0.0)
        self.assertEqual(val("s|1|1|1", "heartbeat"), 1)
        # 周期号不变：通道心跳不变，服务心跳 +1；数据源消失：good=0
        ms.update_context(ctx, recs[1:], snap, pk)
        regs = ms.build_registers(m, ctx, "CDAB")
        self.assertEqual(val("s|1|1|1", "good"), 0)
        self.assertEqual(val("s|1|1|1", "heartbeat"), 1)
        self.assertEqual(val("*", "service_heartbeat"), 2)

    def test_export_csv(self):
        tmp = tempfile.mkdtemp()
        try:
            m = ms.auto_map([("k", "=通道")], peaks=1)
            p = ms.export_map_csv(m, os.path.join(tmp, "map.csv"), "CDAB", func=4)
            with open(p, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[1][1], "30001")
            body = [r for r in rows[1:] if r[4] == "cycle_id"][0]
            self.assertEqual(body[3], "'=通道")
            self.assertEqual(body[6], "CDAB")
        finally:
            shutil.rmtree(tmp)


class Security(unittest.TestCase):
    def test_allow_list(self):
        allow = ms.normalize_allow("192.168.1.10, 10.0.0.0/8\n")
        self.assertEqual(allow, ["192.168.1.10", "10.0.0.0/8"])
        self.assertTrue(ms.ip_allowed("10.2.3.4", allow))
        self.assertTrue(ms.ip_allowed("::ffff:192.168.1.10", allow))
        self.assertFalse(ms.ip_allowed("192.168.1.11", allow))
        self.assertTrue(ms.ip_allowed("8.8.8.8", []))
        with self.assertRaises(ms.MapError):
            ms.normalize_allow(["999.1.1.1"])

    def test_warnings(self):
        self.assertEqual(ms.security_warnings("127.0.0.1", []), [])
        self.assertEqual(len(ms.security_warnings("0.0.0.0", [])), 2)
        self.assertEqual(len(ms.security_warnings("0.0.0.0", ["10.0.0.0/8"])), 1)


class Loopback(unittest.TestCase):
    def setUp(self):
        self.fw = ms.ModbusForwarder("127.0.0.1", 0).start()
        self.fw.update({0: 11, 1: 22, 2: 33})

    def tearDown(self):
        self.fw.stop()

    def test_read_over_socket(self):
        self.assertEqual(ms.client_read("127.0.0.1", self.fw.port, 0, 3, func=3), [11, 22, 33])
        self.assertEqual(ms.client_read("127.0.0.1", self.fw.port, 1, 2, func=4), [22, 33])
        self.assertEqual(ms.client_read("127.0.0.1", self.fw.port, 2, 5), ("exception", 2))
        self.assertGreaterEqual(self.fw.requests, 3)

    def test_write_rejected_over_socket(self):
        pdu = struct.pack(">BHH", 6, 0, 99)
        with socket.create_connection(("127.0.0.1", self.fw.port), timeout=3) as s:
            s.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
            data = s.recv(64)
        self.assertEqual(data[7:9], b"\x86\x01")
        self.assertEqual(self.fw.snapshot()[0][0], 11)          # 值没有被改

    def test_whitelist_blocks(self):
        fw2 = ms.ModbusForwarder("127.0.0.1", 0, allow=["10.0.0.0/8"]).start()
        try:
            with self.assertRaises(OSError):
                ms.client_read("127.0.0.1", fw2.port, 0, 1, timeout=2)
            self.assertEqual(fw2.rejected, 1)
        finally:
            fw2.stop()

    def test_start_from_config(self):
        self.assertIsNone(ms.start_from_config({"modbus_server_enabled": False}))
        busy = ms.start_from_config({"modbus_server_enabled": True, "modbus_server_host": "127.0.0.1",
                                     "modbus_server_port": self.fw.port})
        if busy is not None:            # 个别系统允许端口复用
            busy.stop()


if __name__ == "__main__":
    unittest.main()
