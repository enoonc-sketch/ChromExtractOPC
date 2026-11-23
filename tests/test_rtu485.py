# -*- coding: utf-8 -*-
"""RS-485 数据配型自诊断（v2.5）：CRC/组帧、参数扫描命中、数据类型与字节序配型、
无应答报未找到、只发读功能码。用内存假从站，不需要真实硬件。"""
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import nettest as nt  # noqa: E402


class FakeSlave:
    """内存 Modbus RTU 从站：只在参数（波特率/校验/站号）对时应答，记录收到的每一帧。"""

    def __init__(self, registers, want_baud=19200, want_parity="E", want_unit=7, **k):
        self.registers = registers
        self.want_baud, self.want_parity, self.want_unit = want_baud, want_parity, want_unit
        self.got_baud = k.get("baud")
        self.got_parity = k.get("parity")
        self.buf = b""
        self.frames = []

    def reset_input_buffer(self):
        self.buf = b""

    def write(self, d):
        self.frames.append(bytes(d))
        if self.got_baud != self.want_baud or self.got_parity != self.want_parity:
            return  # 参数不对：真设备不会应答（或应答乱码，这里按无应答处理）
        if len(d) != 8 or nt.crc16_modbus(d[:-2]) != int.from_bytes(d[-2:], "little"):
            return
        unit, func = d[0], d[1]
        if unit != self.want_unit:
            return
        addr = int.from_bytes(d[2:4], "big")
        cnt = int.from_bytes(d[4:6], "big")
        vals = self.registers[addr:addr + cnt]
        body = bytes([unit, func, len(vals) * 2]) + b"".join(v.to_bytes(2, "big") for v in vals)
        self.buf += body + nt.crc16_modbus(body).to_bytes(2, "little")

    def flush(self):
        pass

    def read(self, n):
        d, self.buf = self.buf[:min(n, 4)], self.buf[min(n, 4):]
        return d

    def close(self):
        pass


def make_factory(registers, **kw):
    holder = {}

    def factory(**k):
        s = FakeSlave(registers, **kw, **k)
        holder.setdefault("all", []).append(s)
        return s

    factory.holder = holder
    return factory


def float_regs(value, order="ABCD"):
    raw = struct.pack(">f", value)
    if order == "CDAB":
        raw = raw[2:] + raw[:2]
    elif order == "BADC":
        raw = bytes([raw[1], raw[0], raw[3], raw[2]])
    elif order == "DCBA":
        raw = raw[::-1]
    return list(struct.unpack(">HH", raw))


class CrcAndFrame(unittest.TestCase):
    def test_crc_known(self):
        self.assertEqual(nt.build_rtu_request(1, 3, 0, 10).hex(), "01030000000ac5cd")

    def test_only_read_funcs_allowed(self):
        with self.assertRaises(nt.TestError):
            nt._rtu_read_once("COM3", 9600, "N", 1, 1, 6, 0, 1, 0.1, factory=make_factory([0]))


class Matching(unittest.TestCase):
    def test_float32_abcd_first(self):
        regs = float_regs(123.45, "ABCD")
        m = nt.match_data_types(regs, regs)
        self.assertEqual(m["best"]["dtype"], "float32")
        self.assertEqual(m["best"]["order"], "ABCD")
        self.assertAlmostEqual(m["best"]["value"], 123.45, places=2)

    def test_float32_cdab_first(self):
        regs = float_regs(123.45, "CDAB")
        m = nt.match_data_types(regs, regs)
        self.assertEqual(m["best"]["dtype"], "float32")
        self.assertEqual(m["best"]["order"], "CDAB")
        self.assertAlmostEqual(m["best"]["value"], 123.45, places=2)

    def test_int32_byte_order(self):
        regs = list(struct.unpack(">HH", struct.pack(">i", 100000)))
        m = nt.match_data_types(regs, regs)
        self.assertIn(m["best"]["dtype"], ("int32", "uint32"))
        self.assertEqual(m["best"]["order"], "ABCD")
        self.assertEqual(m["best"]["value"], 100000)

    def test_config_snippet(self):
        regs = float_regs(0.5, "ABCD")
        m = nt.match_data_types(regs, regs)
        self.assertIn("float32", m["config_snippet"])
        self.assertIn("ABCD", m["config_snippet"])


class DiagnoseFlow(unittest.TestCase):
    def test_probe_finds_params(self):
        regs = float_regs(123.45, "ABCD") + [0, 0]
        f = make_factory(regs)
        params, got = nt.probe_rtu_params("COM3", unit=7, timeout=0.05, factory=f)
        self.assertIsNotNone(params)
        self.assertEqual(params["baud"], 19200)
        self.assertEqual(params["parity"], "E")
        self.assertEqual(got[:2], regs[:2])

    def test_diagnose_end_to_end(self):
        regs = float_regs(123.45, "ABCD") + [0, 0]
        f = make_factory(regs)
        r = nt.diagnose_rtu485("COM3", unit=7, timeout=0.05, factory=f)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["best"]["dtype"], "float32")
        self.assertEqual(r["best"]["order"], "ABCD")
        # 只发读功能码 03/04
        for s in f.holder["all"]:
            for fr in s.frames:
                self.assertIn(fr[1], (3, 4))

    def test_no_device_reports_not_found(self):
        f = make_factory([0, 0, 0, 0], want_baud=12345)  # 波特率不在扫描表内，永远不应答
        r = nt.diagnose_rtu485("COM3", unit=1, baud=9600, timeout=0.02, factory=f)
        self.assertFalse(r["ok"])
        self.assertIn("没有 Modbus RTU 设备应答", r["summary"])
        self.assertTrue(any("A/B" in d for d in r["details"]))

    def test_scan_slaves_finds_unit(self):
        regs = float_regs(10.0, "ABCD") + [0, 0]
        f = make_factory(regs, want_baud=9600, want_parity="N", want_unit=7)
        params, _got = nt.probe_rtu_params("COM3", timeout=0.02, factory=f,
                                           bauds=(9600,), scan_slaves=True, deadline_sec=60)
        self.assertIsNotNone(params)
        self.assertEqual(params["unit"], 7)


if __name__ == "__main__":
    unittest.main()
