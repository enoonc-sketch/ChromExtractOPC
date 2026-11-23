# -*- coding: utf-8 -*-
"""modbus_tools.py —— Modbus Poll 主站测试 + Modbus Slave 从站模拟器（v2.8）

等效于现场常用的 Modbus Poll / Modbus Slave 两个工具的核心功能，纯 Python 实现，
不依赖第三方 Modbus 库：CRC、报文十六进制显示、串口打开、字节序重排复用
nettest 的既有实现。

- Poll（主站）：Modbus TCP / RTU，读 01/02/03/04，写 05/06/15/16，单次或连续
  轮询；寄存器按 int16/uint16/int32/uint32/float32/float64 × 字节序解码显示；
  每次通讯给出请求/响应报文、耗时与异常码中文说明。
- Slave（从站模拟器）：Modbus TCP / RTU，四区数据（线圈 / 离散输入 / 保持
  寄存器 / 输入寄存器），响应读写与异常码，带通讯日志。只在用户手动启动时
  监听，默认不启动。

注意：485 配型自诊断（nettest.diagnose_rtu485）仍只发读功能码 03/04；本模块的
写功能是独立入口（Poll），不与配型诊断共用路径，不会污染配型诊断。
"""
import socket
import struct
import threading
import time

import nettest as nt

FUNC_NAMES = {1: "读线圈", 2: "读离散输入", 3: "读保持寄存器", 4: "读输入寄存器",
              5: "写单线圈", 6: "写单寄存器", 15: "写多线圈", 16: "写多寄存器"}
READ_FUNCS = (1, 2, 3, 4)
WRITE_FUNCS = (5, 6, 15, 16)
BIT_FUNCS = (1, 2, 5, 15)
REG_COUNT = {"int16": 1, "uint16": 1, "int32": 2, "uint32": 2, "float32": 2, "float64": 4}
DTYPES = tuple(REG_COUNT)
BYTE_ORDERS = nt.BYTE_ORDERS
EXC_TEXT = nt.MODBUS_EXC

# 协议上限：读位 ≤2000、读寄存器 ≤125、写多位 ≤1968、写多寄存器 ≤123
_LIMITS = {1: 2000, 2: 2000, 3: 125, 4: 125, 15: 1968, 16: 123}


class ModbusExc(Exception):
    """从站侧异常：code 为 Modbus 异常码（1 非法功能 / 2 非法地址 / 3 非法数据值）。"""

    def __init__(self, code):
        self.code = int(code)
        super().__init__("异常码 %d：%s" % (self.code, EXC_TEXT.get(self.code, "未知")))


# ---------------------------------------------------------------------------
# 数据类型编解码（复用 nettest 的字节序重排，与 485 配型同一套口径）
# ---------------------------------------------------------------------------

def _check_dtype_order(dtype, order):
    dtype = str(dtype or "uint16").lower()
    order = str(order or "ABCD").upper()
    if dtype not in REG_COUNT:
        raise nt.TestError("数据类型只能是：%s" % " / ".join(DTYPES))
    if order not in BYTE_ORDERS:
        raise nt.TestError("字节序只能是：%s" % " / ".join(BYTE_ORDERS))
    return dtype, order


def encode_value(value, dtype, order="ABCD"):
    """把一个数值编码成寄存器（uint16）列表。字节序重排是自逆变换，编解码共用。"""
    dtype, order = _check_dtype_order(dtype, order)
    try:
        if dtype == "uint16":
            v = int(value) & 0xFFFF
            raw = struct.pack(">H", v)
            if order in ("BADC", "DCBA"):
                raw = raw[::-1]
            return [struct.unpack(">H", raw)[0]]
        if dtype == "int16":
            raw = struct.pack(">h", int(value))
            if order in ("BADC", "DCBA"):
                raw = raw[::-1]
            return [struct.unpack(">H", raw)[0]]
        if dtype == "int32":
            raw = nt._order_bytes(struct.pack(">i", int(value)), order)
        elif dtype == "uint32":
            raw = nt._order_bytes(struct.pack(">I", int(value) & 0xFFFFFFFF), order)
        elif dtype == "float32":
            raw = nt._order_bytes(struct.pack(">f", float(value)), order)
        else:  # float64
            raw = nt._order_bytes8(struct.pack(">d", float(value)), order)
    except (ValueError, TypeError, struct.error, OverflowError):
        raise nt.TestError("数值 %r 不能按 %s 编码" % (value, dtype))
    return list(struct.unpack(">%dH" % (len(raw) // 2), raw))


def decode_value(regs, dtype, order="ABCD"):
    """把寄存器列表（长度须与类型匹配）解码成一个数值。"""
    dtype, order = _check_dtype_order(dtype, order)
    need = REG_COUNT[dtype]
    if len(regs) < need:
        raise nt.TestError("%s 需要 %d 个寄存器，只有 %d 个" % (dtype, need, len(regs)))
    regs = list(regs[:need])
    if need == 1:
        raw = struct.pack(">H", regs[0])
        if order in ("BADC", "DCBA"):
            raw = raw[::-1]
        fmt = ">h" if dtype == "int16" else ">H"
        return struct.unpack(fmt, raw)[0]
    if need == 2:
        raw = nt._order_bytes(struct.pack(">HH", *regs), order)
        fmt = {"int32": ">i", "uint32": ">I", "float32": ">f"}[dtype]
        return struct.unpack(fmt, raw)[0]
    raw = nt._order_bytes8(struct.pack(">HHHH", *regs), order)
    return struct.unpack(">d", raw)[0]


def decode_regs(regs, dtype, order="ABCD"):
    """按类型步进解码一串寄存器，返回 [(寄存器偏移, 值), ...]，尾巴不够一个值就停。"""
    dtype, order = _check_dtype_order(dtype, order)
    step = REG_COUNT[dtype]
    out = []
    for i in range(0, len(regs) - step + 1, step):
        out.append((i, decode_value(regs[i:i + step], dtype, order)))
    return out


def format_read_values(values, kind, addr, dtype="uint16", order="ABCD"):
    """把一次读的结果排成显示行（桌面界面 / 命令行共用）。"""
    lines = []
    if kind == "位":
        for i in range(0, len(values), 16):
            chunk = values[i:i + 16]
            lines.append("地址 %d 起：%s" % (addr + i, " ".join(str(int(v)) for v in chunk)))
        return lines
    dtype, order = _check_dtype_order(dtype, order)
    for off, v in decode_regs(values, dtype, order):
        lines.append("地址 %d：%s（%s/%s）" % (addr + off, _fmt_num(v), dtype, order))
    raw = " ".join("0x%04X" % r for r in values[:16])
    lines.append("原始寄存器：%s%s" % (raw, " …共 %d 个" % len(values) if len(values) > 16 else ""))
    return lines


def _fmt_num(v):
    if isinstance(v, float):
        return "%g" % v
    return str(v)


def parse_values(text, func, dtype="uint16", order="ABCD"):
    """解析写入值文本（逗号/空格分隔）。
    位功能（05/15）：返回 0/1 列表；寄存器功能（06/16）：按数据类型+字节序编码成寄存器列表。"""
    func = int(func)
    parts = [p for p in str(text or "").replace("，", ",").replace(";", ",").split(",") if p.strip()]
    if not parts:
        # 允许空格分隔
        parts = str(text or "").split()
    if not parts:
        raise nt.TestError("请填写要写入的值（多个值用逗号分隔）")
    if func in (5, 15):
        bits = []
        for p in parts:
            p = p.strip().lower()
            if p in ("1", "true", "on", "开"):
                bits.append(1)
            elif p in ("0", "false", "off", "关"):
                bits.append(0)
            else:
                raise nt.TestError("线圈值只能是 0 或 1：%s" % p)
        if func == 5 and len(bits) != 1:
            raise nt.TestError("写单线圈（05）只能写一个值")
        return bits
    if func in (6, 16):
        dtype, order = _check_dtype_order(dtype, order)
        regs = []
        for p in parts:
            try:
                v = float(p) if dtype in ("float32", "float64") else int(p, 0)
            except ValueError:
                raise nt.TestError("数值格式不对：%s" % p)
            regs += encode_value(v, dtype, order)
        if func == 6 and len(regs) != 1:
            raise nt.TestError("写单寄存器（06）只能写一个 16 位值")
        return regs
    raise nt.TestError("功能码 %d 不是写功能" % func)


# ---------------------------------------------------------------------------
# PDU 组帧 / 解析
# ---------------------------------------------------------------------------

def _check_addr_count(func, addr, count):
    try:
        func, addr, count = int(func), int(addr), int(count)
    except (TypeError, ValueError):
        raise nt.TestError("功能码、地址、数量都必须是数字")
    if func not in FUNC_NAMES:
        raise nt.TestError("不支持的功能码：%s（支持 01/02/03/04/05/06/15/16）" % func)
    if not 0 <= addr <= 65535:
        raise nt.TestError("地址范围 0～65535（注意：40001 这种写法要减去 40001，即填 0）")
    if func in _LIMITS and not 1 <= count <= _LIMITS[func]:
        raise nt.TestError("功能码 %02d 数量范围 1～%d" % (func, _LIMITS[func]))
    if addr + count > 65536:
        raise nt.TestError("地址 + 数量超出 65536")
    return func, addr, count


def build_read_pdu(func, addr, count):
    func, addr, count = _check_addr_count(func, addr, count)
    if func not in READ_FUNCS:
        raise nt.TestError("功能码 %02d 不是读功能" % func)
    return struct.pack(">BHH", func, addr, count)


def build_write_pdu(func, addr, values):
    """values：05 为单个 0/1（或 [0/1]），06 为单个寄存器值，15 为位列表，16 为寄存器列表。"""
    func = int(func)
    addr = int(addr)
    if not 0 <= addr <= 65535:
        raise nt.TestError("地址范围 0～65535")
    if func == 5:
        bit = values[0] if isinstance(values, (list, tuple)) else values
        return struct.pack(">BHH", 5, addr, 0xFF00 if int(bit) else 0x0000)
    if func == 6:
        v = values[0] if isinstance(values, (list, tuple)) else values
        return struct.pack(">BHH", 6, addr, int(v) & 0xFFFF)
    if func == 15:
        bits = [1 if int(b) else 0 for b in values]
        _check_addr_count(15, addr, len(bits))
        data = bytearray((len(bits) + 7) // 8)
        for i, b in enumerate(bits):
            if b:
                data[i // 8] |= 1 << (i % 8)
        return struct.pack(">BHHB", 15, addr, len(bits), len(data)) + bytes(data)
    if func == 16:
        regs = [int(v) & 0xFFFF for v in values]
        _check_addr_count(16, addr, len(regs))
        return struct.pack(">BHHB", 16, addr, len(regs), len(regs) * 2) + \
            b"".join(struct.pack(">H", r) for r in regs)
    raise nt.TestError("功能码 %02d 不是写功能" % func)


def exception_pdu(func, code):
    return bytes([int(func) | 0x80, int(code) & 0xFF])


def parse_read_pdu(func, count, pdu):
    """解析读响应 PDU（不含站号/MBAP/CRC），返回 (值列表, "位"/"寄存器")。"""
    if not pdu or pdu[0] != int(func):
        raise nt.TestError("应答功能码不一致")
    return nt.parse_pdu(int(func), int(count), pdu[1:])


def parse_write_pdu(func, pdu):
    """解析写响应 PDU，返回 (地址, 值或数量)。"""
    if len(pdu) < 5 or pdu[0] != int(func):
        raise nt.TestError("写应答格式不对：%s" % nt._hexdump(pdu))
    addr, v = struct.unpack(">HH", pdu[1:5])
    return addr, v


# ---------------------------------------------------------------------------
# 从站数据区（四区）与 PDU 处理
# ---------------------------------------------------------------------------

class DataStore:
    """从站模拟器四区数据：线圈 / 离散输入（位）、保持寄存器 / 输入寄存器（16 位）。"""

    AREAS = ("coils", "discrete", "holding", "input")
    AREA_NAMES = {"coils": "线圈", "discrete": "离散输入", "holding": "保持寄存器", "input": "输入寄存器"}

    def __init__(self, size=10000):
        size = int(size)
        if not 1 <= size <= 65536:
            raise nt.TestError("数据区大小范围 1～65536")
        self.size = size
        self.coils = [0] * size
        self.discrete = [0] * size
        self.holding = [0] * size
        self.input = [0] * size
        self.lock = threading.Lock()

    def _check(self, addr, count):
        if addr < 0 or count < 1 or addr + count > self.size:
            raise ModbusExc(2)  # 非法数据地址

    def read_bits(self, area, addr, count):
        with self.lock:
            self._check(addr, count)
            return list(getattr(self, area)[addr:addr + count])

    def write_bits(self, area, addr, bits):
        with self.lock:
            self._check(addr, len(bits))
            data = getattr(self, area)
            for i, b in enumerate(bits):
                data[addr + i] = 1 if b else 0

    def read_regs(self, area, addr, count):
        with self.lock:
            self._check(addr, count)
            return list(getattr(self, area)[addr:addr + count])

    def write_regs(self, area, addr, regs):
        with self.lock:
            self._check(addr, len(regs))
            data = getattr(self, area)
            for i, r in enumerate(regs):
                data[addr + i] = int(r) & 0xFFFF

    def set_typed(self, area, addr, value, dtype="uint16", order="ABCD"):
        """按数据类型+字节序把一个数值写进寄存器区，返回占用的寄存器个数。"""
        if area not in ("holding", "input"):
            raise nt.TestError("按数据类型写入只适用于寄存器区")
        regs = encode_value(value, dtype, order)
        self.write_regs(area, int(addr), regs)
        return len(regs)

    def get_typed(self, area, addr, dtype="uint16", order="ABCD"):
        if area not in ("holding", "input"):
            raise nt.TestError("按数据类型读取只适用于寄存器区")
        dtype, order = _check_dtype_order(dtype, order)
        regs = self.read_regs(area, int(addr), REG_COUNT[dtype])
        return decode_value(regs, dtype, order)


def handle_pdu(store, pdu):
    """从站 PDU 处理：输入请求 PDU（功能码起，不含站号/MBAP/CRC），返回响应 PDU。"""
    if not pdu:
        return b""
    func = pdu[0]
    try:
        if func in (1, 2, 3, 4):
            if len(pdu) < 5:
                raise ModbusExc(3)
            addr, count = struct.unpack(">HH", pdu[1:5])
            if not 1 <= count <= _LIMITS[func]:
                raise ModbusExc(3)
            if func in (1, 2):
                bits = store.read_bits("coils" if func == 1 else "discrete", addr, count)
                data = bytearray((count + 7) // 8)
                for i, b in enumerate(bits):
                    if b:
                        data[i // 8] |= 1 << (i % 8)
                return bytes([func, len(data)]) + bytes(data)
            regs = store.read_regs("holding" if func == 3 else "input", addr, count)
            return bytes([func, len(regs) * 2]) + b"".join(struct.pack(">H", r) for r in regs)
        if func == 5:
            if len(pdu) < 5:
                raise ModbusExc(3)
            addr, v = struct.unpack(">HH", pdu[1:5])
            if v not in (0x0000, 0xFF00):
                raise ModbusExc(3)
            store.write_bits("coils", addr, [1 if v == 0xFF00 else 0])
            return pdu[:5]
        if func == 6:
            if len(pdu) < 5:
                raise ModbusExc(3)
            addr, v = struct.unpack(">HH", pdu[1:5])
            store.write_regs("holding", addr, [v])
            return pdu[:5]
        if func == 15:
            if len(pdu) < 6:
                raise ModbusExc(3)
            addr, count, bc = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= count <= _LIMITS[15] or bc != (count + 7) // 8 or len(pdu) < 6 + bc:
                raise ModbusExc(3)
            bits = [(pdu[6 + i // 8] >> (i % 8)) & 1 for i in range(count)]
            store.write_bits("coils", addr, bits)
            return struct.pack(">BHH", 15, addr, count)
        if func == 16:
            if len(pdu) < 6:
                raise ModbusExc(3)
            addr, count, bc = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= count <= _LIMITS[16] or bc != count * 2 or len(pdu) < 6 + bc:
                raise ModbusExc(3)
            regs = list(struct.unpack(">%dH" % count, pdu[6:6 + bc]))
            store.write_regs("holding", addr, regs)
            return struct.pack(">BHH", 16, addr, count)
        raise ModbusExc(1)  # 非法功能
    except ModbusExc as e:
        return exception_pdu(func, e.code)


# ---------------------------------------------------------------------------
# 主站（Poll）：TCP / RTU
# ---------------------------------------------------------------------------

def _mk_result(ok, summary, details=None, ms=None, values=None, kind=None,
               req_hex="", resp_hex="", exc_code=None):
    r = nt._result(ok, summary, details or [], ms)
    r.update({"values": values, "kind": kind, "req_hex": req_hex,
              "resp_hex": resp_hex, "exc_code": exc_code, "latency_ms": ms})
    return r


class _MasterBase:
    unit = 1

    def request(self, pdu):  # pragma: no cover - 子类实现
        raise NotImplementedError

    def read(self, func, addr, count):
        func = int(func)
        try:
            pdu = build_read_pdu(func, addr, count)
        except nt.TestError as e:
            return _mk_result(False, str(e))
        return self._do(pdu, func, int(count))

    def write(self, func, addr, values):
        func = int(func)
        try:
            pdu = build_write_pdu(func, addr, values)
        except nt.TestError as e:
            return _mk_result(False, str(e))
        return self._do(pdu, func, None)

    def _do(self, pdu, func, count):
        try:
            resp_pdu, req_frame, resp_frame, ms = self.request(pdu)
        except nt.TestError as e:
            return _mk_result(False, str(e))
        except Exception as e:  # noqa
            return _mk_result(False, "通讯出错：%s" % e)
        details = ["发送：%s" % nt._hexdump(req_frame), "接收：%s" % nt._hexdump(resp_frame)]
        req_hex, resp_hex = nt._hexdump(req_frame), nt._hexdump(resp_frame)
        if resp_pdu and resp_pdu[0] == (func | 0x80):
            code = resp_pdu[1] if len(resp_pdu) > 1 else 0
            return _mk_result(False, "设备返回异常码 %d：%s" % (code, EXC_TEXT.get(code, "未知")),
                              details, ms, req_hex=req_hex, resp_hex=resp_hex, exc_code=code)
        try:
            if func in READ_FUNCS:
                values, kind = parse_read_pdu(func, count, resp_pdu)
                return _mk_result(True, "%s成功：站号 %d，%d 个%s，耗时 %.0f ms" % (
                    FUNC_NAMES[func], self.unit, len(values), kind, ms),
                    details, ms, values=values, kind=kind, req_hex=req_hex, resp_hex=resp_hex)
            addr, v = parse_write_pdu(func, resp_pdu)
            what = ("值 %s" % ("开(1)" if v == 0xFF00 else "关(0)")) if func == 5 else \
                   ("值 %d (0x%04X)" % (v, v) if func == 6 else "数量 %d" % v)
            return _mk_result(True, "%s成功：站号 %d，地址 %d，%s，耗时 %.0f ms" % (
                FUNC_NAMES[func], self.unit, addr, what, ms),
                details, ms, req_hex=req_hex, resp_hex=resp_hex)
        except nt.TestError as e:
            return _mk_result(False, str(e), details, ms, req_hex=req_hex, resp_hex=resp_hex)


class TcpMaster(_MasterBase):
    """Modbus TCP 主站：保持一条连接连续请求，断线自动重连。"""

    def __init__(self, host, port=502, unit=1, timeout=3.0):
        self.host = nt._host(host)
        self.port = nt._port(port, 502)
        self.unit = int(unit)
        if not 0 <= self.unit <= 255:
            raise nt.TestError("站号（Unit ID）范围 0～255")
        self.timeout = nt._timeout(timeout, 3.0)
        self.sock = None
        self._tid = 0
        self._lock = threading.Lock()

    def connect(self):
        self.close()
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise nt.TestError("连不上 %s:%d：%s" % (self.host, self.port, e))
        self.sock.settimeout(self.timeout)
        return self

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:  # noqa
                pass
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise nt.TestError("对方断开了连接（站号不对时有的网关会直接断开）")
            buf += chunk
        return buf

    def request(self, pdu):
        with self._lock:
            if self.sock is None:
                self.connect()
            self._tid = (self._tid + 1) & 0xFFFF
            tid = self._tid
            frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, self.unit) + pdu
            t0 = time.perf_counter()
            try:
                self.sock.sendall(frame)
                head = self._recv_exact(7)
                rtid, _proto, ln, _runit = struct.unpack(">HHHB", head)
                if not 2 <= ln <= 260:
                    raise nt.TestError("报文长度字段异常：%d（对方可能不是 Modbus TCP 设备）" % ln)
                body = self._recv_exact(ln - 1)
            except socket.timeout:
                self.close()
                raise nt.TestError("设备 %.1f 秒内没有应答" % self.timeout)
            except OSError as e:
                self.close()
                raise nt.TestError("TCP 通讯出错：%s" % e)
            ms = (time.perf_counter() - t0) * 1000
            if rtid != tid:
                raise nt.TestError("事务号不一致（%d ≠ %d），连接已错位，请重连" % (rtid, tid))
            return body, frame, head + body, ms


class RtuMaster(_MasterBase):
    """Modbus RTU 主站：串口打开复用 nettest._open_serial（factory 可注入做测试）。"""

    def __init__(self, port, baud=9600, parity="N", stopbits=1, unit=1, timeout=1.0,
                 rs485_rts=False, factory=None):
        self.port, self.baud, _bs, self.parity, self.stopbits = nt._serial_args(
            port, baud, 8, parity, stopbits)
        self.unit = int(unit)
        if not 0 <= self.unit <= 247:
            raise nt.TestError("站号范围 0～247（RTU 一般 1～247）")
        self.timeout = nt._timeout(timeout, 1.0)
        self.rs485_rts = rs485_rts
        self.factory = factory
        self.ser = None
        self._port_lock = None
        self._lock = threading.Lock()

    def connect(self):
        self.close()
        lock = nt.port_lock(self.port)
        if lock is not None and not lock.acquire(blocking=False):
            raise nt.TestError("%s 正在被其它测试占用，请稍后再试" % self.port)
        self._port_lock = lock
        try:
            self.ser = nt._open_serial(self.port, self.baud, 8, self.parity, self.stopbits,
                                       self.timeout, self.rs485_rts, self.factory)
        except Exception:
            if lock is not None:
                lock.release()
            self._port_lock = None
            raise
        return self

    def close(self):
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa
                pass
            self.ser = None
        if self._port_lock is not None:
            try:
                self._port_lock.release()
            except Exception:  # noqa
                pass
            self._port_lock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    @staticmethod
    def _resp_frame_len(buf, func):
        """按应答头推算帧长；字节不够返回 None。"""
        if len(buf) < 2:
            return None
        if buf[1] == (func | 0x80):
            return 5
        if func in READ_FUNCS:
            if len(buf) < 3:
                return None
            return 3 + buf[2] + 2
        return 8  # 写功能应答固定 8 字节

    def _read_response(self, frame, func):
        """读一帧应答。半双工转换器会把请求回显回来：回显与请求逐字节相同，
        认出来就跳过；05/06 的应答本身就与请求相同（回显也一样），直接收。"""
        deadline = time.monotonic() + self.timeout
        buf = b""
        while time.monotonic() < deadline:
            if func in (5, 6):
                if len(buf) >= 8:
                    return buf[:8]
            else:
                if buf.startswith(frame):
                    buf = buf[len(frame):]      # 确认是回显，去掉后继续等应答
                    continue
                if not (len(buf) < len(frame) and frame.startswith(buf)):
                    flen = self._resp_frame_len(buf, func)
                    if flen and len(buf) >= flen:
                        return buf[:flen]
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk
            else:
                time.sleep(0.002)
        return buf  # 超时：把收到的残字节交回上层报错

    def request(self, pdu):
        with self._lock:
            if self.ser is None:
                self.connect()
            frame_body = bytes([self.unit]) + pdu
            frame = frame_body + struct.pack("<H", nt.crc16_modbus(frame_body))
            try:
                self.ser.reset_input_buffer()
            except Exception:  # noqa
                pass
            t0 = time.perf_counter()
            nt._write(self.ser, frame)
            resp = self._read_response(frame, pdu[0])
            ms = (time.perf_counter() - t0) * 1000
            if not resp:
                raise nt.TestError("站号 %d 在 %.1f 秒内没有应答（排查：站号、串口参数、"
                                   "RS-485 A/B 是否接反、是否需要 RTS 控制收发方向）" % (self.unit, self.timeout))
            if len(resp) < 5:
                raise nt.TestError("响应太短（%d 字节）：%s" % (len(resp), nt._hexdump(resp)))
            if nt.crc16_modbus(resp[:-2]) != struct.unpack("<H", resp[-2:])[0]:
                raise nt.TestError("CRC 校验错误：线路干扰、波特率/校验位不一致，或总线上有多个设备同时应答。"
                                   "收到：%s" % nt._hexdump(resp))
            if resp[0] != self.unit:
                raise nt.TestError("应答站号是 %d，不是请求的 %d（站号冲突或收到了别的设备的数据）"
                                   % (resp[0], self.unit))
            return resp[1:-2], frame, resp, ms


def poll(master, func, addr, count, interval_sec=1.0, stop_event=None, callback=None,
         max_rounds=None):
    """连续轮询读：每轮回调 callback(result)，stop_event 置位或达到 max_rounds 停。返回轮数。"""
    stop_event = stop_event or threading.Event()
    interval = max(0.05, float(interval_sec or 1.0))
    rounds = 0
    while not stop_event.is_set():
        r = master.read(func, addr, count)
        rounds += 1
        if callback is not None:
            callback(r)
        if max_rounds is not None and rounds >= int(max_rounds):
            break
        stop_event.wait(interval)
    return rounds


# ---------------------------------------------------------------------------
# 从站模拟器（Slave）：TCP / RTU
# ---------------------------------------------------------------------------

def _log_entry(transport, unit, func, req, resp, note=""):
    return {"time": time.strftime("%H:%M:%S"), "transport": transport, "unit": unit,
            "func": func, "req_hex": nt._hexdump(req), "resp_hex": nt._hexdump(resp),
            "note": note}


class TcpSlave:
    """Modbus TCP 从站模拟器：手动 start() 才监听；port=0 时用随机端口（测试）。"""

    def __init__(self, store, host="0.0.0.0", port=502, unit=1, log=None):
        self.store = store
        self.host = str(host or "0.0.0.0")
        self.port = int(port)
        self.unit = int(unit)
        if not 0 <= self.unit <= 255:
            raise nt.TestError("站号（Unit ID）范围 0～255")
        self.log = log
        self._srv = None
        self._thread = None
        self._stop = threading.Event()
        self._conns = set()
        self._conns_lock = threading.Lock()

    @property
    def running(self):
        return self._srv is not None

    @property
    def address(self):
        return "%s:%d" % (self.host, self.port)

    def _emit(self, entry):
        if self.log is not None:
            try:
                self.log(entry)
            except Exception:  # noqa
                pass

    def start(self):
        if self._srv is not None:
            return self
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((self.host, self.port))
            srv.listen(8)
        except OSError as e:
            srv.close()
            raise nt.TestError("监听 %s:%d 失败：%s（端口被占用？502 需要管理员权限时换个端口如 1502）"
                               % (self.host, self.port, e))
        srv.settimeout(0.5)
        self._srv = srv
        self.port = srv.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except Exception:  # noqa
                pass
            self._srv = None
        with self._conns_lock:
            conns = list(self._conns)
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._conns_lock:
                self._conns.add(conn)
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _serve_conn(self, conn):
        try:
            conn.settimeout(30)
            while not self._stop.is_set():
                head = self._recv_exact(conn, 7)
                if head is None:
                    break
                tid, proto, ln, unit = struct.unpack(">HHHB", head)
                if not 2 <= ln <= 260:
                    break
                pdu = self._recv_exact(conn, ln - 1)
                if pdu is None:
                    break
                func = pdu[0] if pdu else 0
                if unit not in (self.unit, 0):
                    self._emit(_log_entry("TCP", unit, func, head + pdu, b"",
                                          "站号不符（本站 %d），不应答" % self.unit))
                    continue
                resp_pdu = handle_pdu(self.store, pdu)
                if unit == 0:  # 广播：只执行写，不应答
                    self._emit(_log_entry("TCP", unit, func, head + pdu, b"", "广播，不应答"))
                    continue
                resp = struct.pack(">HHHB", tid, 0, len(resp_pdu) + 1, unit) + resp_pdu
                conn.sendall(resp)
                self._emit(_log_entry("TCP", unit, func, head + pdu, resp))
        except (OSError, socket.timeout):
            pass
        finally:
            with self._conns_lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except Exception:  # noqa
                pass


def _rtu_frame_len(buf):
    """按功能码推算 RTU 请求帧总长；字节不够返回 None；未知功能码返回 8（按最短帧试）。"""
    if len(buf) < 2:
        return None
    func = buf[1]
    if func in (1, 2, 3, 4, 5, 6):
        return 8
    if func in (15, 16):
        if len(buf) < 7:
            return None
        return 9 + buf[6]
    return 8


class RtuSlave:
    """Modbus RTU 从站模拟器：手动 start() 才打开串口监听（factory 可注入做测试）。"""

    def __init__(self, store, port, baud=9600, parity="N", stopbits=1, unit=1,
                 factory=None, log=None):
        self.store = store
        self.port, self.baud, _bs, self.parity, self.stopbits = nt._serial_args(
            port, baud, 8, parity, stopbits)
        self.unit = int(unit)
        if not 1 <= self.unit <= 247:
            raise nt.TestError("站号范围 1～247")
        self.factory = factory
        self.log = log
        self.ser = None
        self._thread = None
        self._stop = threading.Event()
        self._port_lock = None

    @property
    def running(self):
        return self.ser is not None

    @property
    def address(self):
        return "%s %d,8,%s,%g 站号 %d" % (self.port, self.baud, self.parity, self.stopbits, self.unit)

    def _emit(self, entry):
        if self.log is not None:
            try:
                self.log(entry)
            except Exception:  # noqa
                pass

    def start(self):
        if self.ser is not None:
            return self
        lock = nt.port_lock(self.port)
        if lock is not None and not lock.acquire(blocking=False):
            raise nt.TestError("%s 正在被其它测试占用，请稍后再试" % self.port)
        self._port_lock = lock
        try:
            self.ser = nt._open_serial(self.port, self.baud, 8, self.parity, self.stopbits,
                                       0.2, False, self.factory)
        except Exception:
            if lock is not None:
                lock.release()
            self._port_lock = None
            raise
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:  # noqa
                pass
            self.ser = None
        if self._port_lock is not None:
            try:
                self._port_lock.release()
            except Exception:  # noqa
                pass
            self._port_lock = None

    def _loop(self):
        buf = b""
        last_rx = time.monotonic()
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(256)
            except Exception:  # noqa
                break
            if chunk:
                buf += chunk
                last_rx = time.monotonic()
            # 帧间静默过久：残帧丢弃（RTU 以静默间隔分帧）
            if buf and not chunk and time.monotonic() - last_rx > 0.3:
                want = _rtu_frame_len(buf)
                if want is None or len(buf) < want:
                    buf = b""
                    continue
            while True:
                want = _rtu_frame_len(buf)
                if want is None or len(buf) < want:
                    break
                frame, buf = buf[:want], buf[want:]
                self._handle_frame(frame)
            if not chunk:
                time.sleep(0.005)

    def _handle_frame(self, frame):
        if len(frame) < 4:
            return
        if nt.crc16_modbus(frame[:-2]) != struct.unpack("<H", frame[-2:])[0]:
            self._emit(_log_entry("RTU", frame[0], frame[1] if len(frame) > 1 else 0,
                                  frame, b"", "CRC 错误，丢弃"))
            return
        unit, func = frame[0], frame[1]
        if unit not in (self.unit, 0):
            self._emit(_log_entry("RTU", unit, func, frame, b"",
                                  "站号不符（本站 %d），不应答" % self.unit))
            return
        resp_pdu = handle_pdu(self.store, frame[1:-2])
        if unit == 0:
            self._emit(_log_entry("RTU", unit, func, frame, b"", "广播，不应答"))
            return
        body = bytes([unit]) + resp_pdu
        resp = body + struct.pack("<H", nt.crc16_modbus(body))
        nt._write(self.ser, resp)
        self._emit(_log_entry("RTU", unit, func, frame, resp))
