#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nettest.py —— 现场连接测试工具（网页设置页、命令行 --nettest 共用）

网络：Ping、TCP 端口、DNS 解析、OPC UA 服务器、Modbus TCP、本机网卡
串口（RS-232 / RS-485 / RS-422）：列出串口、收发测试、回环自检、Modbus RTU 读寄存器

每个测试返回 {"ok": bool, "summary": 一句话结论, "details": [逐行说明]}，失败时给出可能原因。
所有测试都有超时上限；串口需要 pyserial（已打包进 EXE）。
"""
import ipaddress
import os
import re
import socket
import struct
import subprocess
import time

MAX_TIMEOUT = 10.0
import threading as _threading
_PORT_LOCKS = {}
_PORT_GUARD = _threading.Lock()


def port_lock(port):
    """每个串口一把锁：单次测试、稳定性测试、设备检测都要先拿锁，绝不同时打开同一个串口。"""
    key = str(port or "").strip().upper()
    with _PORT_GUARD:
        return _PORT_LOCKS.setdefault(key, _threading.Lock())
MAX_BYTES = 4096
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:_\[\]%]{1,253}$")


class TestError(Exception):
    """给人看的错误（参数不对等）。"""


def _result(ok, summary, details=None, latency=None):
    r = {"ok": bool(ok), "summary": summary, "details": list(details or [])}
    if latency is not None:
        r["latency_ms"] = round(float(latency), 2)      # 纯通信耗时（不含启动进程等开销），稳定性统计用
    return r


def _host(h):
    h = str(h or "").strip()
    if not h or not HOST_RE.match(h) or h.startswith("-"):
        raise TestError("主机地址不对：只能是 IP 或主机名，例如 192.168.1.10")
    return h.strip("[]")


def _port(p, default=None):
    try:
        p = int(p if p not in (None, "") else default)
    except (TypeError, ValueError):
        raise TestError("端口必须是数字")
    if not 1 <= p <= 65535:
        raise TestError("端口范围 1～65535")
    return p


def _timeout(t, default=3.0):
    try:
        t = float(t if t not in (None, "") else default)
    except (TypeError, ValueError):
        raise TestError("超时必须是数字（秒）")
    return max(0.1, min(MAX_TIMEOUT, t))


def _hexdump(b):
    return " ".join("%02X" % x for x in b)


def _printable(b):
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


# ---------------------------------------------------------------------------
# 网络
# ---------------------------------------------------------------------------

def test_dns(host):
    h = _host(host)
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror as e:
        return _result(False, "无法解析 %s" % h,
                       ["错误：%s" % e, "可能原因：主机名拼错、DNS 服务器不可用；现场网络没有 DNS 时请直接用 IP 地址。"])
    ms = (time.perf_counter() - t0) * 1000
    addrs = sorted({i[4][0] for i in infos})
    return _result(True, "%s → %s（%.0f ms）" % (h, "、".join(addrs), ms), ["解析到 %d 个地址" % len(addrs)], ms)


def _decode_console(b):
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", "replace")


def test_ping(host, count=4, timeout=1.0, runner=None):
    """调用系统 ping（参数以列表传入，不经过命令行解释，不会被注入）。"""
    h = _host(host)
    try:
        n = max(1, min(10, int(count or 4)))
    except (TypeError, ValueError):
        raise TestError("次数必须是数字")
    to = _timeout(timeout, 1.0)
    if os.name == "nt":
        cmd = ["ping", "-n", str(n), "-w", str(int(to * 1000)), h]
    else:
        cmd = ["ping", "-c", str(n), "-W", str(max(1, int(to))), h]
    run = runner or (lambda c: subprocess.run(c, capture_output=True, timeout=n * (to + 1) + 5,
                                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
    try:
        p = run(cmd)
    except FileNotFoundError:
        return _result(False, "本机没有 ping 命令", [])
    except subprocess.TimeoutExpired:
        return _result(False, "ping %s 超时" % h, [])
    out = _decode_console(p.stdout or b"") + _decode_console(p.stderr or b"")
    lines = [l.rstrip() for l in out.splitlines() if l.strip()]
    # 成功的回复行里一定有 TTL（中英文 Windows、Linux 都是）
    replies = [l for l in lines if re.search(r"ttl[=:]", l, re.I)]
    times = [float(x) for x in re.findall(r"(?:time|时间)[=<]\s*([\d.]+)\s*ms", out, re.I)]
    if replies:
        lat = ("，平均 %.1f ms" % (sum(times) / len(times))) if times else ""
        return _result(True, "%s 可达：%d/%d 个回复%s" % (h, len(replies), n, lat), lines,
                       (sum(times) / len(times)) if times else None)
    tips = ["可能原因：IP 不对、网线/交换机不通、不在同一网段、对方防火墙禁止 ping（禁 ping 时可改用 TCP 端口测试）。"]
    return _result(False, "%s 没有回复（%d 个全部丢失）" % (h, n), lines + tips)


def test_tcp(host, port, timeout=3.0, send_text=""):
    h, p, to = _host(host), _port(port), _timeout(timeout)
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((h, p), timeout=to)
    except socket.timeout:
        return _result(False, "%s:%d 连接超时（%.1f 秒）" % (h, p, to),
                       ["可能原因：地址不对、网络不通，或防火墙把包直接丢弃。"])
    except ConnectionRefusedError:
        return _result(False, "%s:%d 拒绝连接" % (h, p),
                       ["网络是通的，但对方这个端口没有程序在监听（服务没启动或端口号不对）。"])
    except OSError as e:
        return _result(False, "%s:%d 连接失败：%s" % (h, p, e), [])
    ms = (time.perf_counter() - t0) * 1000
    details = ["本机 %s:%d → 对方 %s:%d" % (s.getsockname()[:2] + s.getpeername()[:2])]
    try:
        if send_text:
            s.sendall(str(send_text).encode("utf-8")[:MAX_BYTES])
            s.settimeout(min(to, 2.0))
            try:
                data = s.recv(MAX_BYTES)
                details.append("收到 %d 字节：%s" % (len(data), _printable(data)[:200]))
                details.append("HEX：%s" % _hexdump(data[:64]))
            except socket.timeout:
                details.append("已发送，对方 2 秒内没有回应（很多协议不回应纯文本，属正常）。")
    finally:
        s.close()
    return _result(True, "%s:%d 端口开放，连接耗时 %.0f ms" % (h, p, ms), details, ms)


def list_interfaces():
    """本机网卡和 IP（用于确认现场电脑和 DCS 是否在同一网段）。"""
    details = []
    try:
        name = socket.gethostname()
        details.append("主机名：%s" % name)
        ips = sorted({i[4][0] for i in socket.getaddrinfo(name, None)})
        for ip in ips:
            kind = "IPv6" if ":" in ip else "IPv4"
            try:
                a = ipaddress.ip_address(ip.split("%")[0])
                tag = "（本机回环）" if a.is_loopback else ("（链路本地，多为网卡未拿到地址）" if a.is_link_local else "")
            except ValueError:
                tag = ""
            details.append("%s：%s%s" % (kind, ip, tag))
    except OSError as e:
        return _result(False, "读取本机网卡失败：%s" % e, [])
    if os.name == "nt":
        details.append("更详细的网卡、子网掩码、网关信息请在命令行运行 ipconfig /all")
    return _result(True, "本机共 %d 个地址" % (len(details) - 1), details)


def test_opcua(endpoint, username="", password="", timeout=5.0):
    """连接 OPC UA 服务器（不加密），读服务器状态、时间并浏览 Objects。"""
    import asyncio
    ep = str(endpoint or "").strip()
    if not ep.startswith("opc.tcp://") or len(ep) > 300:
        raise TestError("地址必须以 opc.tcp:// 开头，例如 opc.tcp://192.168.1.10:4840/ChromExtract")
    to = _timeout(timeout, 5.0)

    async def run():
        from asyncua import Client
        c = Client(url=ep, timeout=to)
        if username:
            c.set_user(username)
            c.set_password(password or "")
        t0 = time.perf_counter()
        await asyncio.wait_for(c.connect(), timeout=to + 2)
        try:
            ms = (time.perf_counter() - t0) * 1000
            details = ["连接耗时 %.0f ms" % ms]
            try:
                st = await c.nodes.server_state.read_value()
                details.append("服务器状态：%s" % getattr(st, "name", st))
            except Exception:  # noqa
                pass
            try:
                details.append("服务器时间：%s" % await c.nodes.server.get_child(
                    ["0:ServerStatus", "0:CurrentTime"]).read_value())
            except Exception:  # noqa
                pass
            kids = await c.nodes.objects.get_children()
            names = []
            for k in kids[:30]:
                try:
                    names.append((await k.read_browse_name()).Name)
                except Exception:  # noqa
                    pass
            details.append("Objects 下有 %d 个节点：%s" % (len(kids), "、".join(names)))
            return _result(True, "OPC UA 连接成功：%s" % ep, details, ms)
        finally:
            await c.disconnect()

    try:
        return asyncio.run(run())
    except ImportError:
        return _result(False, "缺少 asyncua 库", [])
    except asyncio.TimeoutError:
        return _result(False, "连接 %s 超时" % ep, ["先用 TCP 端口测试确认 4840 端口是否开放。"])
    except Exception as e:  # noqa
        msg = str(e) or type(e).__name__
        tips = []
        if "BadUserAccessDenied" in msg or "BadIdentityToken" in msg:
            tips.append("用户名/密码不对，或服务器不允许匿名登录。")
        if "BadSecurity" in msg or "SecurityPolicy" in msg:
            tips.append("服务器只开放了加密端点，本测试只能用不加密方式连接；请用 UaExpert 等客户端测试加密连接。")
        return _result(False, "OPC UA 连接失败：%s" % msg[:200], tips)


# ---------------------------------------------------------------------------
# Modbus（RTU 与 TCP 共用的报文处理）
# ---------------------------------------------------------------------------

MODBUS_EXC = {1: "非法功能码（设备不支持这个功能码）", 2: "非法数据地址（寄存器地址不存在）",
              3: "非法数据值（数量超出范围）", 4: "从站设备故障", 5: "确认（处理中）",
              6: "从站忙", 10: "网关路径不可用", 11: "网关目标设备无响应"}


def crc16_modbus(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _mb_args(unit, func, addr, count):
    try:
        unit, func, addr, count = int(unit), int(func), int(addr), int(count)
    except (TypeError, ValueError):
        raise TestError("站号、功能码、地址、数量都必须是数字")
    if not 0 <= unit <= 247:
        raise TestError("站号范围 0～247（RTU 一般 1～247）")
    if func not in (1, 2, 3, 4):
        raise TestError("只支持读功能码：01 线圈、02 离散输入、03 保持寄存器、04 输入寄存器")
    if not 0 <= addr <= 65535:
        raise TestError("地址范围 0～65535（注意：40001 这种写法要减去 40001，即填 0）")
    limit = 2000 if func in (1, 2) else 125
    if not 1 <= count <= limit:
        raise TestError("数量范围 1～%d" % limit)
    return unit, func, addr, count


def build_rtu_request(unit, func, addr, count):
    unit, func, addr, count = _mb_args(unit, func, addr, count)
    pdu = struct.pack(">BBHH", unit, func, addr, count)
    return pdu + struct.pack("<H", crc16_modbus(pdu))


def expected_rtu_len(func, count):
    n = (count + 7) // 8 if func in (1, 2) else 2 * count
    return 5 + n


def parse_pdu(func, count, body):
    """body = 功能码之后的部分（字节数 + 数据）。返回 (值列表, 说明)。"""
    if not body:
        raise TestError("响应为空")
    n = body[0]
    data = body[1:1 + n]
    if len(data) != n:
        raise TestError("响应长度不对：声明 %d 字节，实际 %d 字节" % (n, len(data)))
    if func in (1, 2):
        bits = [(data[i // 8] >> (i % 8)) & 1 for i in range(min(count, n * 8))]
        return bits, "位"
    regs = list(struct.unpack(">%dH" % (n // 2), data[:n // 2 * 2]))
    return regs, "寄存器"


def parse_rtu_response(req, resp):
    unit, func = req[0], req[1]
    count = struct.unpack(">H", req[4:6])[0]
    if len(resp) < 5:
        raise TestError("响应太短（%d 字节）：%s" % (len(resp), _hexdump(resp)))
    if crc16_modbus(resp[:-2]) != struct.unpack("<H", resp[-2:])[0]:
        raise TestError("CRC 校验错误：线路干扰、波特率/校验位不一致，或总线上有多个设备同时应答。收到：%s" % _hexdump(resp))
    if resp[0] != unit:
        raise TestError("应答站号是 %d，不是请求的 %d（站号冲突或收到了别的设备的数据）" % (resp[0], unit))
    if resp[1] == (func | 0x80):
        raise TestError("设备返回异常码 %d：%s" % (resp[2], MODBUS_EXC.get(resp[2], "未知")))
    if resp[1] != func:
        raise TestError("应答功能码 %d 与请求 %d 不一致" % (resp[1], func))
    return parse_pdu(func, count, resp[2:-2])


def _fmt_values(values, kind, addr):
    lines = []
    if kind == "位":
        lines.append("地址 %d 起：%s" % (addr, " ".join(map(str, values[:64]))))
    else:
        for i, v in enumerate(values[:40]):
            lines.append("地址 %d：%d（0x%04X，有符号 %d）" % (addr + i, v, v, v - 65536 if v >= 32768 else v))
        if len(values) >= 2:
            f = struct.unpack(">f", struct.pack(">HH", values[0], values[1]))[0]
            lines.append("前两个寄存器按 32 位浮点（高字在前）解释：%g" % f)
        if len(values) > 40:
            lines.append("……共 %d 个，只显示前 40 个" % len(values))
    return lines


def test_modbus_tcp(host, port=502, unit=1, func=3, addr=0, count=10, timeout=3.0):
    h, p, to = _host(host), _port(port, 502), _timeout(timeout)
    unit, func, addr, count = _mb_args(unit, func, addr, count)
    tid = int(time.time() * 1000) & 0xFFFF
    req = struct.pack(">HHHBBHH", tid, 0, 6, unit, func, addr, count)
    try:
        s = socket.create_connection((h, p), timeout=to)
    except OSError as e:
        return _result(False, "连不上 %s:%d：%s" % (h, p, e), ["先用 TCP 端口测试排查网络。"])
    try:
        s.settimeout(to)
        t0 = time.perf_counter()
        s.sendall(req)
        head = b""
        while len(head) < 7:
            chunk = s.recv(7 - len(head))
            if not chunk:
                raise TestError("对方断开了连接（站号不对时有的网关会直接断开）")
            head += chunk
        rtid, proto, ln, runit = struct.unpack(">HHHB", head)
        if not 2 <= ln <= 260:
            raise TestError("报文长度字段异常：%d（对方可能不是 Modbus TCP 设备）" % ln)
        body = b""
        while len(body) < ln - 1:
            chunk = s.recv(ln - 1 - len(body))
            if not chunk:
                raise TestError("响应不完整")
            body += chunk
        ms = (time.perf_counter() - t0) * 1000
        details = ["发送：%s" % _hexdump(req), "接收：%s" % _hexdump(head + body)]
        if rtid != tid:
            details.append("注意：事务号不一致（%d ≠ %d）" % (rtid, tid))
        if body[0] == (func | 0x80):
            return _result(False, "设备返回异常码 %d：%s" % (body[1], MODBUS_EXC.get(body[1], "未知")), details)
        values, kind = parse_pdu(func, count, body[1:])
        return _result(True, "Modbus TCP 读取成功：站号 %d，%d 个%s，耗时 %.0f ms" % (unit, len(values), kind, ms),
                       details + _fmt_values(values, kind, addr), ms)
    except socket.timeout:
        return _result(False, "设备 %.1f 秒内没有应答" % to, ["端口是通的，可能站号（Unit ID）不对，或这个端口不是 Modbus TCP。"])
    except TestError as e:
        return _result(False, str(e), [])
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 串口：RS-232 / RS-485 / RS-422
# ---------------------------------------------------------------------------

PARITY = {"N": "无", "E": "偶", "O": "奇", "M": "标记", "S": "空格"}


def list_serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return _result(False, "缺少 pyserial 库，无法使用串口功能", ["源码运行时请执行：pip install pyserial"])
    ports = sorted(list_ports.comports(), key=lambda p: p.device)
    if not ports:
        return _result(False, "没有找到任何串口",
                       ["USB 转 485/232 请确认已插好并安装驱动（设备管理器 → 端口(COM 和 LPT)）。"])
    details = []
    for p in ports:
        extra = []
        if p.vid is not None:
            extra.append("VID:PID=%04X:%04X" % (p.vid, p.pid))
        if p.manufacturer:
            extra.append(p.manufacturer)
        details.append("%s  %s  %s" % (p.device, p.description or "", " ".join(extra)))
    return _result(True, "找到 %d 个串口" % len(ports), details)


def _serial_args(port, baud, bytesize, parity, stopbits):
    port = str(port or "").strip()
    if not re.match(r"^(COM\d{1,3}|/dev/[A-Za-z0-9_./\-]{1,60})$", port, re.I):
        raise TestError("串口名不对，例如 COM3")
    try:
        baud = int(baud or 9600)
        bytesize = int(bytesize or 8)
        stopbits = float(stopbits or 1)
    except (TypeError, ValueError):
        raise TestError("波特率、数据位、停止位必须是数字")
    if not 50 <= baud <= 4000000:
        raise TestError("波特率范围 50～4000000")
    if bytesize not in (5, 6, 7, 8):
        raise TestError("数据位只能是 5/6/7/8")
    if stopbits not in (1, 1.5, 2):
        raise TestError("停止位只能是 1/1.5/2")
    parity = str(parity or "N").upper()[:1]
    if parity not in PARITY:
        raise TestError("校验位只能是 N（无）/ E（偶）/ O（奇）")
    return port, baud, bytesize, parity, stopbits


def _open_serial(port, baud, bytesize, parity, stopbits, timeout, rs485_rts=False, factory=None):
    if factory is not None:
        return factory(port=port, baud=baud, bytesize=bytesize, parity=parity, stopbits=stopbits, timeout=timeout)
    try:
        import serial
    except ImportError:
        raise TestError("缺少 pyserial 库（源码运行时请执行 pip install pyserial）")
    try:
        s = serial.Serial(port=port, baudrate=baud, bytesize=bytesize, parity=parity,
                          stopbits={1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE,
                                    2: serial.STOPBITS_TWO}[stopbits],
                          timeout=0.05, write_timeout=timeout)
    except serial.SerialException as e:
        msg = str(e)
        if "PermissionError" in msg or "Access is denied" in msg or "拒绝访问" in msg:
            raise TestError("%s 被占用：其它程序（组态软件、串口助手）正在使用它，请先关闭。" % port)
        if "FileNotFoundError" in msg or "could not open" in msg:
            raise TestError("打不开 %s：串口不存在，或 USB 转换器被拔出。" % port)
        raise TestError("打不开 %s：%s" % (port, msg))
    if rs485_rts:
        # 不带自动收发切换的 485 转换器：发送时拉高 RTS，发完拉低
        try:
            from serial.rs485 import RS485Settings
            s.rs485_mode = RS485Settings(rts_level_for_tx=True, rts_level_for_rx=False)
        except Exception:  # noqa  Windows 部分驱动不支持，退回手动控制
            s._manual_rts = True
    return s


def _write(s, data):
    if getattr(s, "_manual_rts", False):
        s.rts = True
        s.write(data)
        s.flush()
        s.rts = False
    else:
        s.write(data)
        s.flush()


def _read_until(s, timeout, want=None, gap=0.05):
    """读到 want 字节，或超时，或收到数据后线路静默超过 gap 秒（帧结束）。"""
    buf = b""
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end and len(buf) < MAX_BYTES:
        chunk = s.read(256)
        now = time.monotonic()
        if chunk:
            buf += chunk
            last = now
            if want and len(buf) >= want:
                break
        elif buf and last and now - last > gap and not want:
            break
        elif buf and want and last and now - last > max(gap, 0.3):
            break       # 收到一部分就停了（例如异常应答只有 5 字节）
    return buf


def parse_payload(text, mode):
    text = str(text or "")
    if mode == "hex":
        h = re.sub(r"[\s,]|0x", "", text, flags=re.I)
        if len(h) % 2 or not re.fullmatch(r"[0-9A-Fa-f]*", h):
            raise TestError("十六进制格式不对，例如：01 03 00 00 00 0A")
        data = bytes.fromhex(h)
    else:
        data = text.replace("\\r", "\r").replace("\\n", "\n").encode("utf-8")
    if len(data) > MAX_BYTES:
        raise TestError("一次最多发送 %d 字节" % MAX_BYTES)
    return data


def test_serial(port, baud=9600, bytesize=8, parity="N", stopbits=1, send="", mode="hex",
                timeout=1.0, rs485_rts=False, factory=None):
    """串口收发测试：打开串口 → 发送（可选）→ 在超时内接收并显示。"""
    port, baud, bytesize, parity, stopbits = _serial_args(port, baud, bytesize, parity, stopbits)
    to = _timeout(timeout, 1.0)
    data = parse_payload(send, mode) if send else b""
    setting = "%s %d,%d,%s,%g" % (port, baud, bytesize, parity, stopbits)
    try:
        s = _open_serial(port, baud, bytesize, parity, stopbits, to, rs485_rts, factory)
    except TestError as e:
        return _result(False, str(e), [])
    try:
        try:
            s.reset_input_buffer()
        except Exception:  # noqa
            pass
        details = ["串口参数：%s（%s校验）" % (setting, PARITY[parity])]
        if data:
            _write(s, data)
            details.append("发送 %d 字节：%s" % (len(data), _hexdump(data[:64])))
        resp = _read_until(s, to)
        if resp:
            details += ["收到 %d 字节" % len(resp), "HEX：%s" % _hexdump(resp[:256]), "文本：%s" % _printable(resp[:256])]
            if data and resp == data:
                details.append("注意：收到的内容和发送的完全一样，可能是 RS-485 转换器回显或 TX/RX 短接。")
            return _result(True, "串口 %s 打开正常，收到 %d 字节" % (port, len(resp)), details)
        if data:
            details += ["%.1f 秒内没有收到任何数据。排查顺序：" % to,
                        "  1. 波特率 / 数据位 / 校验位 / 停止位是否与设备一致；",
                        "  2. RS-485：A/B 线是否接反（接反最常见），是否共地，远距离总线两端是否加 120Ω 终端电阻；",
                        "  3. RS-232：TX/RX 是否需要交叉（直连线/交叉线），设备是否需要 RTS/DTR；",
                        "  4. 不带自动收发切换的 485 转换器，请勾选“RTS 控制收发方向”。"]
            return _result(False, "串口已打开、已发送，但设备没有应答", details)
        return _result(True, "串口 %s 打开正常（只监听，%.1f 秒内线路上没有数据）" % (port, to), details)
    except Exception as e:  # noqa
        return _result(False, "串口通信出错：%s" % e, [])
    finally:
        try:
            s.close()
        except Exception:  # noqa
            pass


def test_serial_loopback(port, baud=9600, bytesize=8, parity="N", stopbits=1, timeout=1.0, factory=None):
    """回环自检：把串口 TX 和 RX 短接（RS-232 的 2、3 脚），确认串口本身和转换器是好的。"""
    port, baud, bytesize, parity, stopbits = _serial_args(port, baud, bytesize, parity, stopbits)
    to = _timeout(timeout, 1.0)
    pattern = bytes([0x55, 0xAA, 0x00, 0xFF]) + os.urandom(12)
    try:
        s = _open_serial(port, baud, bytesize, parity, stopbits, to, False, factory)
    except TestError as e:
        return _result(False, str(e), [])
    try:
        try:
            s.reset_input_buffer()
        except Exception:  # noqa
            pass
        _write(s, pattern)
        resp = _read_until(s, to, want=len(pattern))
        details = ["发送：%s" % _hexdump(pattern), "接收：%s" % (_hexdump(resp) or "（无）")]
        if resp == pattern:
            return _result(True, "回环自检通过：%s 收发正常" % port, details + ["串口和转换器本身是好的，问题在线缆或对方设备。"])
        if not resp:
            return _result(False, "回环自检失败：没有收到数据",
                           details + ["确认已把 TX 和 RX 短接（DB9 的 2 脚和 3 脚）；RS-485 是半双工，不能用这种方法自检。"])
        return _result(False, "回环自检失败：收到的数据不一致（%d/%d 字节）" % (len(resp), len(pattern)),
                       details + ["可能是波特率过高、线路干扰或转换器质量问题。"])
    finally:
        try:
            s.close()
        except Exception:  # noqa
            pass


def test_modbus_rtu(port, baud=9600, bytesize=8, parity="N", stopbits=1, unit=1, func=3, addr=0, count=10,
                    timeout=1.0, rs485_rts=False, factory=None):
    port, baud, bytesize, parity, stopbits = _serial_args(port, baud, bytesize, parity, stopbits)
    to = _timeout(timeout, 1.0)
    req = build_rtu_request(unit, func, addr, count)
    try:
        s = _open_serial(port, baud, bytesize, parity, stopbits, to, rs485_rts, factory)
    except TestError as e:
        return _result(False, str(e), [])
    try:
        try:
            s.reset_input_buffer()
        except Exception:  # noqa
            pass
        t0 = time.perf_counter()
        _write(s, req)
        want = expected_rtu_len(int(func), int(count))
        resp = _read_until(s, to, want=want)
        ms = (time.perf_counter() - t0) * 1000
        # 半双工转换器会把自己发出的请求回显回来：去掉回显，并把还没读到的应答补读完整
        if resp.startswith(req):
            resp = resp[len(req):]
            if len(resp) < want:
                resp += _read_until(s, to, want=want - len(resp))
        details = ["串口参数：%s %d,%d,%s,%g" % (port, baud, bytesize, parity, stopbits),
                   "发送：%s" % _hexdump(req), "接收：%s" % (_hexdump(resp) or "（无）")]
        if not resp:
            return _result(False, "站号 %d 在 %.1f 秒内没有应答" % (int(unit), to),
                           details + ["排查：站号是否正确、串口参数是否一致、RS-485 A/B 是否接反、是否需要勾选“RTS 控制收发方向”。"])
        if resp == req:
            return _result(False, "只收到了自己发出的请求（回显），设备没有应答", details)
        try:
            values, kind = parse_rtu_response(req, resp)
        except TestError as e:
            return _result(False, str(e), details)
        return _result(True, "Modbus RTU 读取成功：站号 %d，%d 个%s，耗时 %.0f ms" % (int(unit), len(values), kind, ms),
                       details + _fmt_values(values, kind, int(addr)), ms)
    finally:
        try:
            s.close()
        except Exception:  # noqa
            pass




# ---------------------------------------------------------------------------
# RS-485 数据配型自诊断（v2.5 新增）：自动试串口参数 + 数据类型/字节序配型
# 只发读功能码 03/04，绝不写寄存器。串口打开走 _open_serial（可注入 factory 做测试）。
# ---------------------------------------------------------------------------

RTU_BAUDS = (9600, 19200, 38400, 115200, 4800, 2400, 57600)
RTU_PARITIES = ("N", "E", "O")
RTU_STOPBITS = (1, 2)
BYTE_ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")


def _order_bytes(b4, order):
    """4 字节按字节序重排：ABCD 原样 / CDAB 字交换 / BADC 字内字节交换 / DCBA 全反。"""
    a, b, c, d = b4
    return {"ABCD": bytes([a, b, c, d]), "CDAB": bytes([c, d, a, b]),
            "BADC": bytes([b, a, d, c]), "DCBA": bytes([d, c, b, a])}[order]


def _order_bytes8(b8, order):
    a, b, c, d, e, f, g, h = b8
    return {"ABCD": bytes([a, b, c, d, e, f, g, h]),
            "CDAB": bytes([g, h, e, f, c, d, a, b]),
            "BADC": bytes([b, a, d, c, f, e, h, g]),
            "DCBA": bytes([h, g, f, e, d, c, b, a])}[order]


def decode_candidates(regs):
    """把寄存器（uint16 列表）按全部类型×字节序解码，返回候选列表（未打分）。
    16 位类型逐个寄存器解（大端/小端各一份）；32 位类型逐对解；64 位需 4 个寄存器。"""
    out = []
    for i, r in enumerate(regs):
        for endian, order in (("big", "AB"), ("little", "BA")):
            raw = r.to_bytes(2, endian)
            out.append({"dtype": "uint16", "order": order, "index": i,
                        "value": int.from_bytes(raw, "big") if endian == "little" else r})
            v = int.from_bytes(raw, "big", signed=True) if endian == "little" else (
                r - 65536 if r >= 32768 else r)
            out.append({"dtype": "int16", "order": order, "index": i, "value": v})
    for i in range(0, len(regs) - 1, 2):
        b4 = struct.pack(">HH", regs[i], regs[i + 1])
        for order in BYTE_ORDERS:
            raw = _order_bytes(b4, order)
            out.append({"dtype": "uint32", "order": order, "index": i,
                        "value": int.from_bytes(raw, "big")})
            out.append({"dtype": "int32", "order": order, "index": i,
                        "value": int.from_bytes(raw, "big", signed=True)})
            out.append({"dtype": "float32", "order": order, "index": i,
                        "value": struct.unpack(">f", raw)[0]})
    for i in range(0, len(regs) - 3, 4):
        b8 = struct.pack(">HHHH", *regs[i:i + 4])
        for order in BYTE_ORDERS:
            raw = _order_bytes8(b8, order)
            out.append({"dtype": "float64", "order": order, "index": i,
                        "value": struct.unpack(">d", raw)[0]})
    return out


def _score_value(v, dtype):
    """合理性打分 0~100：有限值、量级像过程量（温度/压力/流量等）、不是极端值。"""
    import math as _math
    if isinstance(v, float):
        if _math.isnan(v) or _math.isinf(v):
            return 0.0, "不是有限数（NaN/Inf），配型肯定不对"
        if v == 0.0:
            return 55.0, "值为 0（可能是真零，也可能配错了字节序）"
        av = abs(v)
        if 1e-4 <= av <= 1e7:
            s = 90.0
            note = "量级像正常过程量"
        elif av < 1e-4:
            s, note = 25.0, "数值极小，多半字节序不对"
        else:
            s, note = 20.0, "数值极大，多半字节序不对"
        # 小数位过于杂乱（像随机位模式）扣分；小数位干净（像设定值/测量值）加分
        if av >= 1e-4 and abs(v - round(v, 6)) > 0 and len(repr(v)) > 14:
            s -= 10.0
            note += "；小数位杂乱，像随机位模式"
        elif av >= 1e-4 and (round(v, 4) == v or len(repr(v)) <= 10):
            s += 8.0
            note += "；小数位干净，像真实测量值"
        return s, note
    # 整数
    if v == 0:
        return 55.0, "值为 0（可能是真零，也可能配错了字节序）"
    av = abs(v)
    limit = 2 ** 31 if dtype == "int32" else 2 ** 32
    if dtype in ("int16", "uint16"):
        limit = 32768 if dtype == "int16" else 65536
    if av >= limit - 1:
        return 15.0, "接近类型上限，像溢出/配错"
    if dtype in ("int32", "uint32") and av >= 10000 and av % 100 == 0:
        return 88.0, "整百/整千的整数，像带缩放系数的过程量（如 100000 = 100.000）"
    if av <= 100000:
        return 80.0, "量级像正常过程量（注意是否有缩放系数）"
    return 45.0, "数值偏大，检查字节序与缩放系数"


def match_data_types(regs1, regs2=None):
    """数据配型：对两次读到的寄存器逐候选打分（第二次读用于稳定性加分），
    返回按分数排序的候选 + 推荐配型 + 可粘贴配置片段。"""
    cands = decode_candidates(regs1)
    second = {}
    if regs2 is not None:
        for c in decode_candidates(regs2):
            second[(c["dtype"], c["order"], c["index"])] = c["value"]
    for c in cands:
        s, note = _score_value(c["value"], c["dtype"])
        v2 = second.get((c["dtype"], c["order"], c["index"]))
        if v2 is not None:
            if v2 == c["value"]:
                s += 8.0
                note += "；两次读数一致，稳定"
            elif isinstance(c["value"], float) and c["value"] and abs(v2 - c["value"]) <= abs(c["value"]) * 0.05:
                s += 6.0
                note += "；两次读数接近，变化平滑"
            elif isinstance(c["value"], (int, float)) and isinstance(v2, (int, float)) and \
                    abs(v2 - c["value"]) > max(1.0, abs(c["value"]) * 0.5):
                s -= 15.0
                note += "；两次读数跳变大，不像同一物理量"
        c["score"] = round(max(0.0, min(100.0, s)), 1)
        c["note"] = note
    # 同一对寄存器若浮点解得通顺，16 位碎片与整数解释多半是假象，降权
    good_float = {(c["index"]) for c in cands
                  if c["dtype"] in ("float32", "float64") and c["score"] >= 85}
    for c in cands:
        if c["dtype"] in ("int16", "uint16") and (c["index"] in good_float or c["index"] - 1 in good_float):
            c["score"] = round(max(0.0, c["score"] - 30.0), 1)
            c["note"] += "；同一对寄存器浮点解得通顺，这多半是浮点的高/低字碎片"
        elif c["dtype"] in ("int32", "uint32") and c["index"] in good_float:
            c["score"] = round(max(0.0, c["score"] - 20.0), 1)
            c["note"] += "；同一对寄存器浮点解得通顺，整数解释多半是假象"
    # 32 位整数解得通顺时，其高/低 16 位碎片降权
    good_int = {c["index"] for c in cands
                if c["dtype"] in ("int32", "uint32") and c["score"] >= 85}
    for c in cands:
        if c["dtype"] in ("int16", "uint16") and (c["index"] in good_int or c["index"] - 1 in good_int):
            c["score"] = round(max(0.0, c["score"] - 20.0), 1)
            c["note"] += "；同一对寄存器 32 位整数解得通顺，这多半是它的高/低字碎片"
    cands.sort(key=lambda c: -c["score"])
    best = cands[0] if cands else None
    snippet = ""
    if best:
        snippet = ("# 推荐配型（寄存器地址偏移 %d 起）\n"
                   "data_type = \"%s\"\nbyte_order = \"%s\"\n"
                   "# 若数值差 10/100/1000 倍，再加 scale 缩放系数" % (
                       best["index"], best["dtype"], best["order"]))
    return {"candidates": cands, "best": best, "config_snippet": snippet}


def _rtu_read_once(port, baud, parity, stopbits, unit, func, addr, count, timeout, factory=None):
    """按一组参数发一次读请求，成功返回寄存器列表，失败抛 TestError。只读（03/04）。"""
    if int(func) not in (3, 4):
        raise TestError("485 配型诊断只允许读功能码 03/04")
    req = build_rtu_request(unit, func, addr, count)
    s = _open_serial(port, baud, 8, parity, stopbits, timeout, False, factory)
    try:
        try:
            s.reset_input_buffer()
        except Exception:  # noqa
            pass
        _write(s, req)
        want = expected_rtu_len(int(func), int(count))
        resp = _read_until(s, timeout, want=want)
        if resp.startswith(req):
            resp = resp[len(req):]
            if len(resp) < want:
                resp += _read_until(s, timeout, want=want - len(resp))
        if not resp:
            raise TestError("无应答")
        values, _kind = parse_rtu_response(req, resp)
        return values
    finally:
        try:
            s.close()
        except Exception:  # noqa
            pass


def probe_rtu_params(port, unit=1, func=3, addr=0, count=4, bauds=None, timeout=0.3,
                     factory=None, progress=None, scan_slaves=False, deadline_sec=180):
    """自动试串口参数：波特率×校验×停止位（×站号扫描）。命中返回 (参数dict, 寄存器)，
    全部试完没命中返回 (None, 已试组合数)。progress(文本) 可选进度回调。"""
    bauds = tuple(bauds) if bauds else RTU_BAUDS
    units = range(1, 248) if scan_slaves else (int(unit),)
    tried = 0
    end = time.monotonic() + max(5, int(deadline_sec))
    for baud in bauds:
        for parity in RTU_PARITIES:
            for stopbits in RTU_STOPBITS:
                for u in units:
                    if time.monotonic() > end:
                        return None, tried
                    tried += 1
                    if progress and (tried == 1 or tried % 50 == 0):
                        progress("已试 %d 组参数（当前 %d,%s,%g 站号 %d）…" % (tried, baud, parity, stopbits, u))
                    try:
                        regs = _rtu_read_once(port, baud, parity, stopbits, u, func, addr,
                                              count, timeout, factory)
                    except TestError:
                        continue
                    return {"baud": baud, "parity": parity, "stopbits": stopbits,
                            "unit": u, "func": int(func)}, regs
    return None, tried


def diagnose_rtu485(port, unit=1, func=3, addr=0, count=4, baud=None, timeout=0.3,
                    scan_slaves=False, factory=None, progress=None):
    """RS-485 数据配型自诊断全流程：试参数 → 连读两次 → 类型/字节序配型排名。
    返回 _result 风格 dict，另带 params/candidates/best/config_snippet。"""
    port, _b, _bs, _p, _sb = _serial_args(port, baud or 9600, 8, "N", 1)
    to = _timeout(timeout, 0.3)
    lock = port_lock(port)
    if lock is not None and not lock.acquire(blocking=False):
        return _result(False, "%s 正在被其它测试占用，请稍后再试" % port, [])
    try:
        params, regs_or_tried = probe_rtu_params(
            port, unit=unit, func=func, addr=addr, count=count,
            bauds=(int(baud),) if baud else None, timeout=to, factory=factory,
            progress=progress, scan_slaves=scan_slaves)
        if params is None:
            return _result(False, "试了 %d 组参数，%s 上没有 Modbus RTU 设备应答" % (regs_or_tried, port),
                           ["排查：1) RS-485 A/B 是否接反（交换试试）；2) 总线两端 120Ω 终端电阻；",
                            "3) 设备站号（可用 --scan-slaves 扫 1-247）；4) 设备是否上电、波特率是否在常用表内；",
                            "5) USB 转 485 驱动与 COM 口号是否正确（--list-serial 查看）。"])
        regs1 = regs_or_tried
        details = ["命中参数：%s %d,8,%s,%g 站号 %d 功能码 %02d 地址 %d 数量 %d" % (
            port, params["baud"], params["parity"], params["stopbits"], params["unit"],
            params["func"], int(addr), int(count)),
            "原始寄存器：" + " ".join("0x%04X" % r for r in regs1)]
        regs2 = None
        try:
            regs2 = _rtu_read_once(port, params["baud"], params["parity"], params["stopbits"],
                                   params["unit"], params["func"], addr, count, to, factory)
            details.append("第二次读数：" + " ".join("0x%04X" % r for r in regs2))
        except TestError as e:
            details.append("第二次读数失败（%s），只按一次读数配型" % e)
        m = match_data_types(regs1, regs2)
        for c in m["candidates"][:8]:
            details.append("候选 %s/%s（偏移 %d）：%r  分数 %.0f —— %s" % (
                c["dtype"], c["order"], c["index"], c["value"], c["score"], c["note"]))
        best = m["best"]
        summary = "485 配型诊断完成：推荐 %s + 字节序 %s（偏移 %d，值 %r，分数 %.0f）" % (
            best["dtype"], best["order"], best["index"], best["value"], best["score"]) if best else "无候选"
        r = _result(True, summary, details + (["配置片段：", m["config_snippet"]] if m["config_snippet"] else []))
        r.update({"params": params, "candidates": m["candidates"], "best": best,
                  "config_snippet": m["config_snippet"], "registers": regs1})
        return r
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:  # noqa
                pass


# ---------------------------------------------------------------------------
# 统一入口（网页 / 命令行）
# ---------------------------------------------------------------------------

TESTS = {
    "ping": (test_ping, ("host", "count", "timeout")),
    "tcp": (test_tcp, ("host", "port", "timeout", "send_text")),
    "dns": (test_dns, ("host",)),
    "interfaces": (list_interfaces, ()),
    "opcua": (test_opcua, ("endpoint", "username", "password", "timeout")),
    "modbus_tcp": (test_modbus_tcp, ("host", "port", "unit", "func", "addr", "count", "timeout")),
    "serial_list": (list_serial_ports, ()),
    "serial": (test_serial, ("port", "baud", "bytesize", "parity", "stopbits", "send", "mode", "timeout", "rs485_rts")),
    "loopback": (test_serial_loopback, ("port", "baud", "bytesize", "parity", "stopbits", "timeout")),
    "rtu485_diag": (diagnose_rtu485, ("port", "unit", "func", "addr", "count", "baud", "timeout")),
    "modbus_rtu": (test_modbus_rtu, ("port", "baud", "bytesize", "parity", "stopbits", "unit", "func", "addr",
                                     "count", "timeout", "rs485_rts")),
}


def run_test(kind, params):
    if kind not in TESTS:
        raise TestError("未知的测试类型：%s" % kind)
    fn, names = TESTS[kind]
    params = params if isinstance(params, dict) else {}
    kw = {k: params[k] for k in names if k in params and params[k] not in (None, "")}
    if "rs485_rts" in kw:
        kw["rs485_rts"] = str(kw["rs485_rts"]).lower() in ("1", "true", "on", "yes")
    t0 = time.perf_counter()
    lock = port_lock(kw.get("port")) if kind in ("serial", "loopback", "modbus_rtu") and kw.get("port") else None
    if lock is not None and not lock.acquire(timeout=float(params.get("_wait", 15))):
        r = _result(False, "%s 正被其它测试或设备检测使用，请稍后再试。" % str(kw.get("port")).upper(), [])
        r["busy"] = True
        r["elapsed_ms"], r["kind"] = int((time.perf_counter() - t0) * 1000), kind
        return r
    try:
        r = fn(**kw)
    except TestError as e:
        r = _result(False, str(e), [])
    finally:
        if lock is not None:
            lock.release()
    r["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    r["kind"] = kind
    return r
