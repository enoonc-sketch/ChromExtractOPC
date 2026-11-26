#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
commscan.py —— 通讯诊断（v3.0 新增，桌面窗口“通讯诊断”页使用；基于 nettest.py 扩展）

网络：
  scan_ip_range   网段 / 起止 IP 并发 Ping 扫描在线主机：延迟、主机名（反向解析）、MAC（arp -a），
                  可同时扫常用工业端口（502 Modbus、4840 OPC UA、102 S7、44818 EtherNet/IP、20000 DNP3、80/443…）
  scan_ports      单个主机扫描端口范围 / 列表，显示开放端口和常见服务名
串口（RS-232 / RS-485 / RS-422）：
  scan_serial_ports       列出串口：名称、描述、厂商、VID/PID、驱动芯片
  detect_baud             波特率自动探测：依次尝试 1200～115200 × 校验位，发 Modbus RTU 读请求看是否有合法 CRC 应答
  scan_modbus_rtu_slaves  Modbus RTU 从站地址扫描（1～247，可配置功能码 / 寄存器）
  serial_loopback / serial_raw  环回测试、原始收发（HEX / 文本），直接复用 nettest

原则：
  - 只读探测：Ping、TCP 连接后立即断开、Modbus 读功能码 01～04，绝不写寄存器；
  - 所有扫描都有地址数 / 并发数上限，可随时用 stop（threading.Event）中途停止，progress(已完成, 总数, 文字) 报进度；
  - 串口操作先拿 nettest.port_lock，与单次测试、稳定性测试、设备检测互斥，绝不同时打开同一个串口；
  - ping / socket / 反向解析 / arp / 串口都可以注入假的工厂函数，单元测试不需要网络和硬件。
"""
import csv
import ipaddress
import os
import re
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime

from i18n import M, tr_msg

import nettest


class CommScanError(Exception):
    """给人看的错误（参数不对等）。"""


MAX_HOSTS = 1024
MAX_PORTS = 1024
MAX_WORKERS = 128

# 常见工业 / IT 端口与服务名（端口扫描显示用）
SERVICES = {
    20: "FTP 数据", 21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS", 80: "HTTP", 102: "S7 / ISO-TSAP（西门子）",
    110: "POP3", 123: "NTP", 135: "Windows RPC", 139: "NetBIOS", 161: "SNMP", 443: "HTTPS", 445: "SMB 文件共享",
    502: "Modbus TCP", 503: "Modbus TCP（备用）", 789: "Red Lion Crimson", 1089: "FF HSE", 1433: "SQL Server",
    1883: "MQTT", 1911: "Niagara Fox", 2222: "EtherNet/IP 隐式", 2404: "IEC 60870-5-104", 3389: "远程桌面",
    4000: "Emerson ROC", 4840: "OPC UA", 4843: "OPC UA（TLS）", 5007: "三菱 MELSEC", 5020: "Modbus TCP（常见替代）",
    5432: "PostgreSQL", 5900: "VNC", 8080: "HTTP（备用）", 8443: "HTTPS（备用）", 8840: "ChromExtract 设置网页",
    8883: "MQTT（TLS）", 9600: "欧姆龙 FINS", 18245: "GE SRTP", 20000: "DNP3", 34962: "PROFINET",
    44818: "EtherNet/IP（CIP）", 47808: "BACnet", 48898: "倍福 ADS",
}
INDUSTRIAL_PORTS = (502, 4840, 102, 44818, 20000, 80, 443)

# USB 转串口芯片（按 VID 判断）
USB_CHIPS = {0x0403: "FTDI（FT232/FT2232 等）", 0x067B: "Prolific PL2303", 0x10C4: "Silicon Labs CP210x",
             0x1A86: "WCH 沁恒 CH340/CH341/CH343", 0x04E2: "Exar / MaxLinear", 0x0557: "ATEN",
             0x2341: "Arduino", 0x1FC9: "NXP", 0x0483: "STMicroelectronics（虚拟串口）", 0x2E8A: "Raspberry Pi"}

INTERFACES = {
    "RS232": {"duplex": "全双工", "rts": False,
              "wiring": "RS-232（全双工，点对点，≤15 m）：本机 TX(DB9 第 3 脚) → 对方 RX，本机 RX(第 2 脚) ← 对方 TX，"
                        "GND(第 5 脚) 必须相连；两端都是电脑/DTE 时 TX/RX 要交叉（用交叉线 / 零调制解调器线）。"},
    "RS485": {"duplex": "半双工", "rts": True,
              "wiring": "RS-485（两线半双工，可多站）：A 接 A（D+ / T/R+），B 接 B（D- / T/R-），建议同时连 GND；"
                        "总线两端各接一个 120 Ω 终端电阻。转换器没有自动收发切换时勾选“RTS 控制收发方向”。"
                        "完全没反应时最常见的原因是 A/B 接反。"},
    "RS422": {"duplex": "全双工", "rts": False,
              "wiring": "RS-422（四线全双工）：本机 TX+ → 对方 RX+，TX- → 对方 RX-，本机 RX+ ← 对方 TX+，RX- ← 对方 TX-，"
                        "建议连 GND；正负接反时收不到或乱码。"},
}

DETECT_BAUDS = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
DETECT_PARITIES = ("N", "E", "O")


def service_name(port):
    return SERVICES.get(int(port), "")


def wiring_tip(kind):
    return INTERFACES.get(str(kind or "").upper(), INTERFACES["RS485"])["wiring"]


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------

def _ip(text):
    try:
        ip = ipaddress.ip_address(str(text).strip())
    except ValueError:
        raise CommScanError(M("不是有效的 IP 地址：%r", text))
    if ip.version != 4:
        raise CommScanError(M("IP 扫描只支持 IPv4：%s", ip))
    return ip


def parse_ip_targets(text, limit=MAX_HOSTS):
    """解析扫描范围，返回 IPv4 字符串列表（去重保序）。支持（可用逗号 / 换行分隔多段）：
       192.168.1.0/24（不含网络地址和广播地址）、192.168.1.10-192.168.1.50、192.168.1.10-50、单个 IP。"""
    parts = [p.strip() for p in re.split(r"[,\s;，；]+", str(text or "")) if p.strip()]
    if not parts:
        raise CommScanError("请填写扫描范围，例如 192.168.1.0/24 或 192.168.1.10-192.168.1.50")
    out, seen = [], set()

    def add_range(a, b):
        if int(b) < int(a):
            a, b = b, a
        if int(b) - int(a) + 1 + len(out) > limit:
            raise CommScanError(M("地址太多：最多一次扫描 %d 个地址，请缩小范围（例如 /24 = 254 个）", limit))
        for n in range(int(a), int(b) + 1):
            s = str(ipaddress.IPv4Address(n))
            if s not in seen:
                seen.add(s)
                out.append(s)

    for p in parts:
        if "/" in p:
            try:
                net = ipaddress.ip_network(p, strict=False)
            except ValueError:
                raise CommScanError(M("网段格式不对：%r（例如 192.168.1.0/24）", p))
            if net.version != 4:
                raise CommScanError(M("IP 扫描只支持 IPv4：%s", p))
            if net.num_addresses > limit + 2:
                raise CommScanError(M("网段 %s 有 %d 个地址，超过上限 %d，请缩小范围", p, net.num_addresses, limit))
            hosts = list(net.hosts()) if net.prefixlen < 31 else list(net)
            if hosts:
                add_range(hosts[0], hosts[-1])
        elif "-" in p:
            a, b = [x.strip() for x in p.split("-", 1)]
            ia = _ip(a)
            if re.fullmatch(r"\d{1,3}", b):
                head = str(ia).rsplit(".", 1)[0]
                ib = _ip("%s.%s" % (head, b))
            else:
                ib = _ip(b)
            add_range(ia, ib)
        else:
            ia = _ip(p)
            add_range(ia, ia)
    return out


def parse_ports(text, limit=MAX_PORTS):
    """'502,4840,8000-8010' → 排好序的端口列表；超过 limit 个报错。"""
    parts = [p.strip() for p in re.split(r"[,\s;，；]+", str(text or "")) if p.strip()]
    ports = set()
    for p in parts:
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", p)
        if not m:
            raise CommScanError(M("端口格式不对：%r（例如 502,4840,8000-8010）", p))
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a > b:
            a, b = b, a
        if a < 1 or b > 65535:
            raise CommScanError(M("端口范围 1～65535：%r", p))
        if b - a + 1 + len(ports) > limit * 4:
            raise CommScanError(M("端口太多：最多一次扫描 %d 个", limit))
        ports.update(range(a, b + 1))
        if len(ports) > limit:
            raise CommScanError(M("端口太多：最多一次扫描 %d 个，请缩小范围", limit))
    return sorted(ports)


def _clamp(v, lo, hi, default, name):
    try:
        v = float(v if v not in (None, "") else default)
    except (TypeError, ValueError):
        raise CommScanError(M("%s 必须是数字", name))
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# 网络探测原语（都可注入假实现）
# ---------------------------------------------------------------------------

def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_no_window())


def ping_once(ip, timeout=1.0, runner=None):
    """Ping 一次：返回 (是否回复, 延迟毫秒或 None)。参数以列表传入，不经过命令行解释。"""
    to = max(0.2, min(5.0, float(timeout)))
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(int(to * 1000)), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(round(to)))), ip]
    run = runner or (lambda c: _run(c, to + 5))
    try:
        p = run(cmd)
    except Exception:  # noqa  没有 ping 命令 / 超时
        return False, None
    out = nettest._decode_console(p.stdout or b"") + nettest._decode_console(p.stderr or b"")
    if not re.search(r"ttl[=:]", out, re.I):
        return False, None
    m = re.search(r"(?:time|时间)\s*([=<])\s*([\d.]+)\s*ms", out, re.I)
    if not m:
        return True, None
    return True, (0.5 if m.group(1) == "<" else float(m.group(2)))


def tcp_probe(ip, port, timeout=0.5, connect=None):
    """TCP 连接探测：('open' / 'closed' / 'filtered', 毫秒)。连上立即断开，不发送任何数据。"""
    connect = connect or socket.create_connection
    t0 = time.perf_counter()
    try:
        s = connect((ip, int(port)), timeout)
    except ConnectionRefusedError:
        return "closed", None
    except (socket.timeout, TimeoutError):
        return "filtered", None
    except OSError:
        return "filtered", None
    ms = (time.perf_counter() - t0) * 1000
    try:
        s.close()
    except Exception:  # noqa
        pass
    return "open", ms


def reverse_dns(ip, resolver=None):
    try:
        name = (resolver or socket.gethostbyaddr)(ip)
    except Exception:  # noqa
        return ""
    if isinstance(name, (tuple, list)):
        name = name[0]
    name = str(name or "")
    return "" if name == ip else name


_ARP_WIN = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})\s+", re.M)
_ARP_NIX = re.compile(r"\((\d{1,3}(?:\.\d{1,3}){3})\)\s+at\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})")


def parse_arp(text):
    """解析 arp -a 输出（Windows 中英文、Linux/macOS 格式）→ {ip: 'AA-BB-CC-DD-EE-FF'}。"""
    out = {}
    for rx in (_ARP_WIN, _ARP_NIX):
        for ip, mac in rx.findall(text or ""):
            mac = mac.replace(":", "-").upper()
            if mac in ("FF-FF-FF-FF-FF-FF", "00-00-00-00-00-00"):
                continue
            out[ip] = mac
    return out


def read_arp_table(runner=None):
    """读本机 ARP 缓存（无窗口运行 arp -a）。读不到返回 {}。"""
    try:
        p = (runner or (lambda c: _run(c, 10)))(["arp", "-a"])
    except Exception:  # noqa
        return {}
    return parse_arp(nettest._decode_console(p.stdout or b""))


# ---------------------------------------------------------------------------
# 并发执行（可停止、有进度）
# ---------------------------------------------------------------------------

def run_parallel(items, fn, workers=32, stop=None, progress=None, label=""):
    """多线程对每个 item 执行 fn(item)，返回 {item: 结果}（被停止时只含已完成的）。"""
    items = list(items)
    total = len(items)
    results = {}
    lock = threading.Lock()
    idx = [0]
    done = [0]

    def worker():
        while True:
            if stop is not None and stop.is_set():
                return
            with lock:
                if idx[0] >= total:
                    return
                item = items[idx[0]]
                idx[0] += 1
            try:
                r = fn(item)
            except Exception as e:  # noqa  单个目标出错不影响其它
                r = e
            with lock:
                results[item] = r
                done[0] += 1
                n = done[0]
            if progress is not None:
                try:
                    progress(n, total, "%s %d/%d" % (label, n, total))
                except Exception:  # noqa
                    pass

    n = max(1, min(int(workers), MAX_WORKERS, total or 1))
    threads = [threading.Thread(target=worker, daemon=True, name="commscan-%d" % i) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ---------------------------------------------------------------------------
# IP 扫描 / 端口扫描
# ---------------------------------------------------------------------------

def scan_ip_range(targets, ports=(), timeout=1.0, port_timeout=0.5, workers=64, resolve_names=True,
                  ping=None, connect=None, resolver=None, arp=None, stop=None, progress=None):
    """并发扫描在线主机。targets 可以是 parse_ip_targets 的结果或范围文字。
    在线 = Ping 有回复，或任一端口开放（对方禁 ping 时靠端口发现）。返回 dict：
      ok / summary / hosts[{ip, alive, latency_ms, hostname, mac, open_ports[{port, service, ms}], via}]
      total / scanned / stopped / elapsed_s"""
    if isinstance(targets, str):
        targets = parse_ip_targets(targets)
    targets = list(targets)
    if len(targets) > MAX_HOSTS:
        raise CommScanError(M("地址太多：最多一次扫描 %d 个", MAX_HOSTS))
    ports = [int(p) for p in (ports or [])]
    if len(ports) > 64:
        raise CommScanError("IP 扫描时同时扫的端口最多 64 个（大量端口请用“端口扫描”逐台扫）")
    to = _clamp(timeout, 0.2, 5, 1.0, "Ping 超时")
    pto = _clamp(port_timeout, 0.05, 5, 0.5, "端口超时")
    ping = ping or (lambda ip: ping_once(ip, to))
    t0 = time.monotonic()

    def one(ip):
        if stop is not None and stop.is_set():
            return None
        alive, lat = ping(ip)
        opened = []
        for p in ports:
            if stop is not None and stop.is_set():
                break
            state, ms = tcp_probe(ip, p, pto, connect)
            if state == "open":
                opened.append({"port": p, "service": service_name(p), "ms": round(ms or 0, 1)})
        if not alive and not opened:
            return None
        return {"ip": ip, "alive": True, "latency_ms": None if lat is None else round(lat, 1),
                "hostname": reverse_dns(ip, resolver) if resolve_names else "", "mac": "",
                "open_ports": opened, "via": "Ping" if alive else "端口"}

    res = run_parallel(targets, one, workers, stop, progress, "IP 扫描")
    hosts = [r for r in (res.get(ip) for ip in targets) if isinstance(r, dict)]
    stopped = bool(stop is not None and stop.is_set())
    if hosts:
        table = (arp or read_arp_table)()
        for h in hosts:
            h["mac"] = table.get(h["ip"], "")
    hosts.sort(key=lambda h: ipaddress.IPv4Address(h["ip"]))
    scanned = len(res)
    summary = M("扫描 %d/%d 个地址，发现 %d 台在线主机%s", scanned, len(targets), len(hosts), "（已中途停止）" if stopped else "")
    with_ports = sum(1 for h in hosts if h["open_ports"])
    if ports:
        summary = M("%s%s", summary, M("，其中 %d 台开放了所选端口", with_ports))
    return {"ok": True, "summary": summary, "hosts": hosts, "total": len(targets), "scanned": scanned,
            "stopped": stopped, "ports": ports, "elapsed_s": round(time.monotonic() - t0, 1)}


def scan_ports(host, ports, timeout=0.5, workers=64, connect=None, stop=None, progress=None):
    """扫描单个主机的端口（TCP connect，连上即断开）。返回 dict：
      ok / summary / host / open[{port, service, ms}] / closed / filtered / scanned / total / stopped"""
    h = nettest._host(host)
    if isinstance(ports, str):
        ports = parse_ports(ports)
    ports = [int(p) for p in ports]
    if not ports:
        raise CommScanError("请填写要扫描的端口，例如 1-1024 或 502,4840,102")
    if len(ports) > MAX_PORTS:
        raise CommScanError(M("端口太多：最多一次扫描 %d 个", MAX_PORTS))
    to = _clamp(timeout, 0.05, 5, 0.5, "超时")
    t0 = time.monotonic()
    res = run_parallel(ports, lambda p: tcp_probe(h, p, to, connect), workers, stop, progress, "端口扫描")
    opened = [{"port": p, "service": service_name(p), "ms": round(r[1] or 0, 1)}
              for p, r in sorted(res.items()) if isinstance(r, tuple) and r[0] == "open"]
    closed = sum(1 for r in res.values() if isinstance(r, tuple) and r[0] == "closed")
    filtered = sum(1 for r in res.values() if isinstance(r, tuple) and r[0] == "filtered")
    stopped = bool(stop is not None and stop.is_set())
    summary = M("%s：扫描 %d/%d 个端口，开放 %d 个%s", h, len(res), len(ports), len(opened), "（已中途停止）" if stopped else "")
    if opened:
        summary += "：" + "、".join("%d%s" % (o["port"], "(%s)" % o["service"] if o["service"] else "")
                                   for o in opened[:12])
    if not opened and filtered == len(res) and res:
        summary += "；全部无响应，可能主机不在线或防火墙丢弃了连接"
    return {"ok": True, "summary": summary, "host": h, "open": opened, "closed": closed, "filtered": filtered,
            "scanned": len(res), "total": len(ports), "stopped": stopped,
            "elapsed_s": round(time.monotonic() - t0, 1)}


def hosts_csv_rows(result):
    rows = [["IP", "延迟(ms)", "主机名", "MAC", "发现方式", "开放端口"]]
    for h in result.get("hosts") or []:
        rows.append([h["ip"], "" if h.get("latency_ms") is None else h["latency_ms"], h.get("hostname", ""),
                     h.get("mac", ""), h.get("via", ""),
                     " ".join("%d%s" % (p["port"], "(%s)" % p["service"] if p["service"] else "")
                              for p in h.get("open_ports") or [])])
    return rows


def ports_csv_rows(result):
    rows = [["主机", "端口", "服务", "连接耗时(ms)"]]
    for o in result.get("open") or []:
        rows.append([result.get("host", ""), o["port"], o["service"], o["ms"]])
    return rows


def _csv_cell(v):
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s        # 防 Excel 公式注入


def write_csv(rows, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([_csv_cell(x) for x in r])
    return path


def export_name(kind, now=None):
    return M("通讯诊断_%s_%s.csv", kind, (now or datetime.now()).strftime("%Y%m%d_%H%M%S"))


# ---------------------------------------------------------------------------
# 串口
# ---------------------------------------------------------------------------

def _chip(vid):
    return USB_CHIPS.get(vid, "") if vid is not None else ""


def scan_serial_ports(lister=None):
    """列出串口：[{device, description, manufacturer, vid, pid, vidpid, chip, serial_number, hwid}]。
    没装 pyserial 时抛 CommScanError。"""
    if lister is None:
        try:
            from serial.tools import list_ports
        except ImportError:
            raise CommScanError("缺少 pyserial 库，无法使用串口功能（源码运行时请执行 pip install pyserial）")
        lister = list_ports.comports
    out = []
    for p in lister():
        vid, pid = getattr(p, "vid", None), getattr(p, "pid", None)
        out.append({"device": getattr(p, "device", ""), "description": getattr(p, "description", "") or "",
                    "manufacturer": getattr(p, "manufacturer", "") or "",
                    "vid": vid, "pid": pid, "vidpid": "%04X:%04X" % (vid, pid) if vid is not None and pid is not None else "",
                    "chip": _chip(vid) or ("主板 / 内置串口" if vid is None else "未知芯片"),
                    "serial_number": getattr(p, "serial_number", "") or "", "hwid": getattr(p, "hwid", "") or ""})

    def _key(d):
        m = re.search(r"(\d+)$", d["device"])
        return (re.sub(r"\d+$", "", d["device"]), int(m.group(1)) if m else 0)
    return sorted(out, key=_key)


def _locked(port, fn, wait=15):
    lock = nettest.port_lock(port)
    if not lock.acquire(timeout=wait):
        raise CommScanError(M("%s 正被其它测试或设备检测使用，请稍后再试。", str(port).upper()))
    try:
        return fn()
    finally:
        lock.release()


def rtu_probe(s, unit, func, addr, count, timeout):
    """在已打开的串口上发一次 Modbus RTU 读请求。返回 (状态, 内容, 毫秒)：
      'ok'（值列表）/ 'exception'（异常码说明，说明线路参数是对的）/ 'crc'（收到但 CRC 错，多半参数不对或干扰）/
      'other'（应答不匹配）/ 'none'（无应答）。只读：功能码只允许 01～04。"""
    req = nettest.build_rtu_request(unit, func, addr, count)
    try:
        s.reset_input_buffer()
    except Exception:  # noqa
        pass
    t0 = time.perf_counter()
    nettest._write(s, req)
    want = nettest.expected_rtu_len(int(func), int(count))
    resp = nettest._read_until(s, timeout, want=want)
    if resp.startswith(req):                     # 半双工转换器回显
        resp = resp[len(req):]
        if len(resp) < want:
            resp += nettest._read_until(s, timeout, want=want - len(resp))
    ms = (time.perf_counter() - t0) * 1000
    if not resp:
        return "none", b"", ms
    if len(resp) >= 5 and nettest.crc16_modbus(resp[:-2]) == struct.unpack("<H", resp[-2:])[0]:
        if resp[0] == int(unit) and resp[1] == (int(func) | 0x80):
            return "exception", M("异常码 %d：%s", resp[2], nettest.MODBUS_EXC.get(resp[2], "未知")), ms
        try:
            values, _kind = nettest.parse_rtu_response(req, resp)
            return "ok", values, ms
        except nettest.TestError as e:
            return "other", str(e), ms
    return "crc", nettest._hexdump(resp[:32]), ms


def _serial_common(port, bytesize, stopbits):
    port, _b, bytesize, _p, stopbits = nettest._serial_args(port, 9600, bytesize, "N", stopbits)
    return port, bytesize, stopbits


def _read_args(unit, func, addr, count):
    try:
        return nettest._mb_args(unit, func, addr, count)
    except nettest.TestError as e:
        raise CommScanError(str(e))


def detect_baud(port, unit=1, func=3, addr=0, count=1, bauds=DETECT_BAUDS, parities=DETECT_PARITIES,
                stopbits=(1,), bytesize=8, timeout=0.3, rs485_rts=False, find_all=False,
                factory=None, stop=None, progress=None, lock_wait=15):
    """波特率自动探测：依次尝试 波特率 × 校验位 × 停止位，发 Modbus RTU 读请求，
    收到 CRC 正确、站号对得上的应答（含 Modbus 异常应答）即认为参数正确。
    find_all=False 时命中第一个就停。返回 dict：ok / summary / found[{baud, parity, stopbits, status, detail}] /
    tried / total / stopped / log[逐组结果]。"""
    unit, func, addr, count = _read_args(unit, func, addr, count)
    port, bytesize, _sb = _serial_common(port, bytesize, 1)
    to = _clamp(timeout, 0.02, 3, 0.3, "超时")
    combos = [(b, p, sb) for b in bauds for p in parities for sb in stopbits]
    found, log = [], []
    stopped = False

    def work():
        nonlocal stopped
        for i, (baud, parity, sb) in enumerate(combos, 1):
            if stop is not None and stop.is_set():
                stopped = True
                break
            if progress is not None:
                progress(i - 1, len(combos), M("波特率探测：%d,%s,%g（%d/%d）", baud, parity, sb, i, len(combos)))
            try:
                s = nettest._open_serial(port, baud, bytesize, parity, sb, to, rs485_rts, factory)
            except nettest.TestError as e:
                raise CommScanError(str(e))
            try:
                state, info, ms = rtu_probe(s, unit, func, addr, count, to)
            finally:
                try:
                    s.close()
                except Exception:  # noqa
                    pass
            text = {"ok": "应答正确", "exception": M("应答正确（%s）", info), "crc": "收到乱码 / CRC 错", "other": str(info),
                    "none": "无应答"}[state]
            log.append("%6d,%s,%g → %s" % (baud, parity, sb, text))
            if state in ("ok", "exception"):
                found.append({"baud": baud, "parity": parity, "stopbits": sb, "status": state,
                              "detail": info if state == "exception" else M("读到 %d 个值", len(info)), "ms": round(ms, 1)})
                if not find_all:
                    break
        if progress is not None:
            progress(len(combos), len(combos), "波特率探测完成")

    _locked(port, work, lock_wait)
    if found:
        f = found[0]
        summary = M("找到串口参数：%d 波特，校验 %s（%s），停止位 %g，站号 %d 有应答", f["baud"], f["parity"], nettest.PARITY.get(f["parity"], ""), f["stopbits"], unit)
        if len(found) > 1:
            summary = M("%s%s", summary, M("（另有 %d 组也有应答）", len(found) - 1))
    else:
        summary = M("试了 %d 组参数都没有正确应答%s", len(log), "（已中途停止）" if stopped else "")
    tips = [] if found else ["排查：站号是否正确（不确定可先用“站号扫描”）、RS-485 A/B 是否接反、是否需要勾选 RTS 方向控制、"
                             "设备是不是 Modbus RTU 协议；收到乱码多半是波特率或校验位不对。"]
    return {"ok": bool(found), "summary": summary, "found": found, "tried": len(log), "total": len(combos),
            "stopped": stopped, "log": log, "tips": tips, "port": port}


def scan_modbus_rtu_slaves(port, baud=9600, parity="N", stopbits=1, bytesize=8, first=1, last=247, func=3,
                           addr=0, count=1, timeout=0.2, rs485_rts=False, factory=None, stop=None, progress=None,
                           lock_wait=15):
    """Modbus RTU 从站地址扫描：按给定串口参数对 first～last 每个站号发一次读请求（只读）。
    返回 dict：ok / summary / slaves[{unit, status, detail, ms}] / garbled[站号] / scanned / total / stopped。"""
    try:
        first, last = int(first), int(last)
    except (TypeError, ValueError):
        raise CommScanError("站号范围必须是数字")
    if not 1 <= first <= last <= 247:
        raise CommScanError("站号范围 1～247，且起始不能大于结束")
    _u, func, addr, count = _read_args(first, func, addr, count)
    try:
        port, baud, bytesize, parity, stopbits = nettest._serial_args(port, baud, bytesize, parity, stopbits)
    except nettest.TestError as e:
        raise CommScanError(str(e))
    to = _clamp(timeout, 0.02, 3, 0.2, "超时")
    units = list(range(first, last + 1))
    slaves, garbled = [], []
    state = {"scanned": 0, "stopped": False}

    def work():
        try:
            s = nettest._open_serial(port, baud, bytesize, parity, stopbits, to, rs485_rts, factory)
        except nettest.TestError as e:
            raise CommScanError(str(e))
        try:
            for i, u in enumerate(units, 1):
                if stop is not None and stop.is_set():
                    state["stopped"] = True
                    break
                st, info, ms = rtu_probe(s, u, func, addr, count, to)
                state["scanned"] = i
                if st == "ok":
                    vals = " ".join(str(v) for v in info[:8])
                    slaves.append({"unit": u, "status": "应答", "detail": M("读到：%s", vals), "ms": round(ms, 1)})
                elif st == "exception":
                    slaves.append({"unit": u, "status": "应答（异常）", "detail": info, "ms": round(ms, 1)})
                elif st in ("crc", "other"):
                    garbled.append(u)
                if progress is not None:
                    progress(i, len(units), M("站号扫描 %d/%d（已发现 %d 个）", i, len(units), len(slaves)))
        finally:
            try:
                s.close()
            except Exception:  # noqa
                pass

    _locked(port, work, lock_wait)
    summary = M("%s %d,%s,%g：扫描站号 %d～%d（%d/%d），发现 %d 个从站%s", port, baud, parity, stopbits, first, last, state["scanned"], len(units), len(slaves), "：" + "、".join(str(x["unit"]) for x in slaves[:30]) if slaves else "")
    if state["stopped"]:
        summary += "（已中途停止）"
    tips = []
    if garbled:
        tips.append(M("站号 %s 收到乱码或不匹配的应答：可能多个设备同站号冲突、线路干扰或终端电阻问题。", "、".join(map(str, garbled[:20]))))
    if not slaves:
        tips.append("一个都没找到：先用“波特率探测”确认串口参数，再检查 A/B 接线和 RTS 方向控制。")
    return {"ok": bool(slaves), "summary": summary, "slaves": slaves, "garbled": garbled,
            "scanned": state["scanned"], "total": len(units), "stopped": state["stopped"], "tips": tips, "port": port}


def serial_loopback(port, baud=9600, bytesize=8, parity="N", stopbits=1, timeout=1.0, interface="RS232"):
    """环回测试（复用 nettest，带串口锁）。RS-485 半双工不能用 TX/RX 短接的方法。"""
    if str(interface).upper() == "RS485":
        return {"ok": False, "summary": "RS-485 是半双工，不能做 TX/RX 短接环回",
                "details": ["请换成 RS-232/422 方式，或用另一个 485 转换器对接、用“原始收发”互发。"]}
    return nettest.run_test("loopback", {"port": port, "baud": baud, "bytesize": bytesize, "parity": parity,
                                         "stopbits": stopbits, "timeout": timeout})


def serial_raw(port, baud=9600, bytesize=8, parity="N", stopbits=1, send="", mode="hex", timeout=1.0,
               interface="RS232", rs485_rts=None):
    """原始收发（复用 nettest.test_serial，带串口锁）。rs485_rts 缺省按接口类型：仅 RS-485 可启用。"""
    rts = bool(rs485_rts) and str(interface).upper() == "RS485"
    return nettest.run_test("serial", {"port": port, "baud": baud, "bytesize": bytesize, "parity": parity,
                                       "stopbits": stopbits, "send": send, "mode": mode, "timeout": timeout,
                                       "rs485_rts": rts})


def result_text(r):
    """把扫描 / 测试结果拼成给人看的多行文字（按当前界面语言翻译；结果字典本身保持中文）。"""
    t = tr_msg
    lines = [t("[ 成功 ] " if r.get("ok") else "[ 失败 ] ") + t(r.get("summary", ""))]
    for f in r.get("found") or []:
        lines.append(t(M("  ✓ %d,%s,%g  %s（%.0f ms）", f["baud"], f["parity"], f["stopbits"], f["detail"], f["ms"])))
    for s in r.get("slaves") or []:
        lines.append(t(M("  站号 %3d  %s  %s（%.0f ms）", s["unit"], s["status"], s["detail"], s["ms"])))
    for line in r.get("details") or []:
        lines.append("  " + t(line))
    if r.get("log"):
        lines.append(t("逐组结果："))
        lines += ["  " + t(x) for x in r["log"]]
    for tip in r.get("tips") or []:
        lines.append(t("提示：") + t(tip))
    return "\n".join(lines)
