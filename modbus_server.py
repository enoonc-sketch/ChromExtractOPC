#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
modbus_server.py —— Modbus TCP 数据转发（只读从站，v3.1 新增；纯标准库 socket，不加依赖）

  - 只支持功能码 03（读保持寄存器）/ 04（读输入寄存器），两者读同一张寄存器表；
    其它功能码（包括所有写功能码）一律返回异常码 01（非法功能），绝不允许 DCS 写入；
  - 默认监听 127.0.0.1:502，不对外；改成其它地址时窗口里会有安全提示；
  - 允许来源 IP 白名单（单个 IP 或 CIDR）；不在名单里的连接直接断开；
  - 寄存器映射表：每一项 = 地址 + 通道 + 字段 + 类型（uint16/int16/uint32/int32/float32），
    32 位数据按 modbus_server_order（ABCD / CDAB / BADC / DCBA）排列；
  - 随服务启停（服务线程里的一个工作线程，不是独立进程）；寄存器在每轮扫描 / 新快照后刷新。
"""
import csv
import ipaddress
import socket
import struct
import threading
import time
from datetime import datetime

from i18n import M

FUNC_READ = (3, 4)
TYPES = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2}
ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")
MAX_READ = 125
MAX_CLIENTS = 16
IDLE_TIMEOUT = 120
GLOBAL = "*"
GLOBAL_FIELDS = ("service_heartbeat", "channel_count", "alert_count", "alert_severity", "client_count")
PEAK_N = 20
CHANNEL_FIELDS = (("cycle_id", "uint32"), ("num_points", "uint32"), ("heartbeat", "uint16"),
                  ("alerts", "uint16"), ("good", "uint16"), ("age_min", "float32"),
                  ("duration", "float32"), ("peak_count", "uint16"), ("peak_area_total", "float32"))
FIELD_DESC = {
    "service_heartbeat": "服务心跳（每轮扫描 +1，0～65535 循环）",
    "channel_count": "通道数", "alert_count": "当前告警数", "alert_severity": "最高告警级别（0 正常 / 1 警告 / 2 严重）",
    "client_count": "OPC UA 客户端连接数",
    "cycle_id": "周期号", "num_points": "点数", "heartbeat": "通道心跳（每出一个新周期 +1）",
    "alerts": "该通道的告警数", "good": "数据有效（1 有效 / 0 数据源消失）", "age_min": "距最新周期的分钟数",
    "duration": "周期时长（秒）", "peak_count": "峰个数", "peak_area_total": "峰面积合计",
    "peak_area": "第 %s 个峰的面积", "peak_height": "第 %s 个峰的峰高", "peak_rt": "第 %s 个峰的保留时间（秒）",
    "peak_pct": "第 %s 个峰的面积%%",
}
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class MapError(ValueError):
    pass


def field_ok(field):
    if field in GLOBAL_FIELDS or field in dict(CHANNEL_FIELDS):
        return True
    for p in ("peak_area_", "peak_height_", "peak_rt_", "peak_pct_"):
        if field.startswith(p):
            n = field[len(p):]
            return n.isdigit() and 1 <= int(n) <= PEAK_N
    return False


def describe_field(field):
    if field in FIELD_DESC:
        return FIELD_DESC[field]
    for p in ("peak_area", "peak_height", "peak_rt", "peak_pct"):
        if field.startswith(p + "_"):
            return M(FIELD_DESC[p], field.rsplit("_", 1)[1])
    return field


def normalize_map(items):
    """校验寄存器映射表：地址 0～65535、类型合法、字段合法、地址不重叠。返回按地址排序的列表。"""
    out, used = [], {}
    if items is None:
        return []
    if not isinstance(items, list):
        raise MapError("modbus_server_map 必须是列表")
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise MapError("映射第 %d 项格式不对" % i)
        try:
            addr = int(it.get("address"))
        except (TypeError, ValueError):
            raise MapError("映射第 %d 项的地址必须是整数" % i)
        typ = str(it.get("type") or "uint16").lower()
        if typ not in TYPES:
            raise MapError("映射第 %d 项的类型只能是 %s" % (i, " / ".join(TYPES)))
        field = str(it.get("field") or "")
        if not field_ok(field):
            raise MapError("映射第 %d 项的字段不认识：%r" % (i, field))
        ch = str(it.get("channel") or "")
        if field in GLOBAL_FIELDS:
            ch = GLOBAL
        elif not ch or ch == GLOBAL:
            raise MapError("映射第 %d 项（%s）需要填写通道" % (i, field))
        n = TYPES[typ]
        if not 0 <= addr <= 65536 - n:
            raise MapError("映射第 %d 项的地址超出范围 0～%d" % (i, 65536 - n))
        for a in range(addr, addr + n):
            if a in used:
                raise MapError("地址 %d 重复：第 %d 项与第 %d 项重叠" % (a, used[a], i))
            used[a] = i
        out.append({"address": addr, "channel": ch, "field": field, "type": typ,
                    "label": str(it.get("label") or "")[:120]})
    return sorted(out, key=lambda x: x["address"])


def normalize_allow(items):
    """白名单：IP 或 CIDR 列表（字符串也可，逗号/换行分隔）。返回规范化后的字符串列表。"""
    if items is None:
        return []
    if isinstance(items, str):
        items = [x for x in items.replace("\n", ",").split(",")]
    out = []
    for x in items:
        x = str(x).strip()
        if not x:
            continue
        try:
            net = ipaddress.ip_network(x, strict=False)
        except ValueError:
            raise MapError("白名单里的地址不对：%r（写 IP 或网段，例如 192.168.1.10 或 192.168.1.0/24）" % x)
        out.append(str(net) if net.num_addresses > 1 else str(net.network_address))
    return out


def ip_allowed(ip, allow):
    if not allow:
        return True
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    for x in allow:
        try:
            if a in ipaddress.ip_network(x, strict=False):
                return True
        except ValueError:
            continue
    return False


def is_public_bind(host):
    return str(host or "").strip() not in LOOPBACK


def security_warnings(host, allow):
    w = []
    if is_public_bind(host):
        w.append(M("Modbus TCP 没有任何认证和加密：监听 %s 会让网络上的设备都能读取数据。", host))
        if not allow:
            w.append("没有设置来源 IP 白名单：建议只允许 DCS / SCADA 的地址。")
    return w


def encode_value(value, typ, order="ABCD"):
    """值 → 寄存器列表（uint16）。越界的整数被夹到类型范围内，NaN/None 写 0。"""
    try:
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            v = 0.0
    except (TypeError, ValueError):
        v = 0.0
    if typ == "float32":
        b = struct.pack(">f", v)
    elif typ == "uint16":
        b = struct.pack(">H", int(max(0, min(65535, round(v)))))
    elif typ == "int16":
        b = struct.pack(">h", int(max(-32768, min(32767, round(v)))))
    elif typ == "uint32":
        b = struct.pack(">I", int(max(0, min(4294967295, round(v)))))
    elif typ == "int32":
        b = struct.pack(">i", int(max(-2147483648, min(2147483647, round(v)))))
    else:
        raise MapError("类型不对：%s" % typ)
    if len(b) == 4:
        A, B, C, D = b[0], b[1], b[2], b[3]
        b = {"ABCD": bytes((A, B, C, D)), "CDAB": bytes((C, D, A, B)),
             "BADC": bytes((B, A, D, C)), "DCBA": bytes((D, C, B, A))}[order if order in ORDERS else "ABCD"]
    return [struct.unpack(">H", b[i:i + 2])[0] for i in range(0, len(b), 2)]


def decode_value(regs, typ, order="ABCD"):
    """寄存器 → 值（测试与“读回核对”用）。"""
    b = b"".join(struct.pack(">H", r) for r in regs)
    if len(b) == 4:
        x = b
        A, B, C, D = {"ABCD": (0, 1, 2, 3), "CDAB": (2, 3, 0, 1), "BADC": (1, 0, 3, 2), "DCBA": (3, 2, 1, 0)}[order]
        b = bytes((x[A], x[B], x[C], x[D]))
    return struct.unpack({"uint16": ">H", "int16": ">h", "uint32": ">I", "int32": ">i", "float32": ">f"}[typ], b)[0]


def auto_map(channels, base=0, block=50, peaks=5):
    """按通道顺序生成映射：地址 base 起放全局字段（10 个寄存器），之后每个通道占 block 个寄存器。
    channels：[(通道键, 显示名)]。"""
    items, a = [], base
    for f in GLOBAL_FIELDS:
        items.append({"address": a, "channel": GLOBAL, "field": f, "type": "uint16"})
        a += 1
    a = base + 10
    need = sum(TYPES[t] for _f, t in CHANNEL_FIELDS) + peaks * 6
    if block < need:
        raise MapError("每个通道至少需要 %d 个寄存器（当前 %d）" % (need, block))
    for key, label in channels:
        b = a
        for f, t in CHANNEL_FIELDS:
            items.append({"address": b, "channel": key, "field": f, "type": t, "label": label})
            b += TYPES[t]
        for n in range(1, peaks + 1):
            for p in ("peak_rt", "peak_area", "peak_height"):
                items.append({"address": b, "channel": key, "field": "%s_%d" % (p, n), "type": "float32",
                              "label": label})
                b += 2
        a += block
    return normalize_map(items)


class Context:
    """生成寄存器所需的当前数据（由服务在每轮扫描后更新）。"""

    def __init__(self):
        self.records = {}       # 通道键 -> 记录（不含曲线也可）
        self.peaks = {}         # 通道键 -> [峰]
        self.alerts = {}        # 通道键 -> 告警数
        self.alert_count = 0
        self.severity = 0
        self.clients = 0
        self.beat = 0
        self.ch_beats = {}      # 通道键 -> 心跳
        self._last_cycle = {}
        self.missing = set()


def _age_min(ct, now):
    try:
        t = datetime.strptime(str(ct)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
        return max(0.0, (now - t).total_seconds() / 60.0)
    except ValueError:
        return 0.0


def field_value(ctx, ch, field, now=None):
    now = now or datetime.now()
    if field == "service_heartbeat":
        return ctx.beat % 65536
    if field == "channel_count":
        return len(ctx.records)
    if field == "alert_count":
        return ctx.alert_count
    if field == "alert_severity":
        return ctx.severity
    if field == "client_count":
        return ctx.clients
    r = ctx.records.get(ch)
    if r is None:
        return 0
    pk = ctx.peaks.get(ch) or []
    if field == "cycle_id":
        return r.get("cycle_id") or 0
    if field == "num_points":
        return r.get("num_points") or 0
    if field == "heartbeat":
        return ctx.ch_beats.get(ch, 0) % 65536
    if field == "alerts":
        return ctx.alerts.get(ch, 0)
    if field == "good":
        return 0 if ch in ctx.missing else 1
    if field == "age_min":
        return _age_min(r.get("cycle_time"), now)
    if field == "duration":
        return r.get("duration") or 0
    if field == "peak_count":
        return len(pk)
    if field == "peak_area_total":
        return sum(p["area"] for p in pk)
    name, n = field.rsplit("_", 1)
    i = int(n) - 1
    if i >= len(pk):
        return 0
    return pk[i][{"peak_area": "area", "peak_height": "height", "peak_rt": "rt", "peak_pct": "area_pct"}[name]]


def build_registers(mapping, ctx, order="ABCD", now=None):
    regs = {}
    for it in mapping:
        vals = encode_value(field_value(ctx, it["channel"], it["field"], now), it["type"], order)
        for i, v in enumerate(vals):
            regs[it["address"] + i] = v
    return regs


def update_context(ctx, records, alert_snap=None, peaks=None, clients=None, channel_key=None):
    """用新快照更新上下文：通道心跳在周期号变化时 +1；服务心跳每次调用 +1。"""
    ck = channel_key or (lambda r: "%s|%s|%s|%s" % (r.get("source"), r.get("app"), r.get("stream"), r.get("det")))
    seen = {}
    for r in records or []:
        k = ck(r)
        seen[k] = {x: r.get(x) for x in ("cycle_id", "num_points", "cycle_time", "duration")}
        if ctx._last_cycle.get(k) != r.get("cycle_id"):
            ctx._last_cycle[k] = r.get("cycle_id")
            ctx.ch_beats[k] = ctx.ch_beats.get(k, 0) + 1
    ctx.missing = set(k for k in ctx.records if k not in seen)
    for k in ctx.missing:
        seen[k] = ctx.records[k]
    ctx.records = seen
    if peaks is not None:
        ctx.peaks = dict(peaks)
    if alert_snap is not None:
        act = alert_snap.get("active") or []
        ctx.alert_count = len(act)
        sev = {"警告": 1, "严重": 2}
        ctx.severity = max([sev.get(a.get("level"), 0) for a in act] or [0])
        per = {}
        for a in act:
            key = str(a.get("key") or "")
            ch = key.split(":", 1)[1] if ":" in key else ""
            if ch:
                per[ch] = per.get(ch, 0) + 1
        ctx.alerts = per
    if clients is not None:
        ctx.clients = int(clients)
    ctx.beat += 1
    return ctx


# ---------------------------------------------------------------------------
# 帧处理（纯函数，可单独测试）
# ---------------------------------------------------------------------------

def exception_pdu(func, code):
    return bytes(((func | 0x80) & 0xFF, code))


def handle_pdu(pdu, regs, max_addr):
    """处理一个 PDU，返回响应 PDU。只读：03/04；其它功能码返回异常 01。"""
    if not pdu:
        return exception_pdu(0, 1)
    func = pdu[0]
    if func not in FUNC_READ:
        return exception_pdu(func, 1)
    if len(pdu) != 5:
        return exception_pdu(func, 3)
    start, qty = struct.unpack(">HH", pdu[1:5])
    if not 1 <= qty <= MAX_READ:
        return exception_pdu(func, 3)
    if start + qty - 1 > max_addr:
        return exception_pdu(func, 2)
    data = b"".join(struct.pack(">H", regs.get(a, 0)) for a in range(start, start + qty))
    return bytes((func, len(data))) + data


def handle_frame(frame, regs, max_addr, unit=0):
    """处理一个完整的 Modbus TCP 帧（MBAP + PDU），返回响应帧；协议号不对返回 None（断开）。"""
    if len(frame) < 8:
        return None
    tid, proto, length, uid = struct.unpack(">HHHB", frame[:7])
    if proto != 0 or length != len(frame) - 6 or length < 2:
        return None
    pdu = frame[7:]
    if unit and uid != unit:
        resp = exception_pdu(pdu[0] if pdu else 0, 0x0B)       # 网关目标设备无响应
    else:
        resp = handle_pdu(pdu, regs, max_addr)
    return struct.pack(">HHHB", tid, 0, len(resp) + 1, uid) + resp


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class ModbusForwarder:
    """只读 Modbus TCP 从站。start() 后在工作线程里监听；update() 换新的寄存器表；stop() 关闭。"""

    def __init__(self, host="127.0.0.1", port=502, allow=None, unit=0, log=None):
        self.host, self.port = host or "127.0.0.1", int(port)
        self.allow = normalize_allow(allow)
        self.unit = int(unit or 0)
        self.log = log
        self._regs = {}
        self._max_addr = -1
        self._lock = threading.Lock()
        self._sock = None
        self._stop = threading.Event()
        self._clients = set()
        self.requests = 0
        self.rejected = 0
        self.error = ""

    def update(self, regs):
        with self._lock:
            self._regs = dict(regs)
            self._max_addr = max(self._regs) if self._regs else -1

    def snapshot(self):
        with self._lock:
            return dict(self._regs), self._max_addr

    def start(self):
        fam = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((self.host, self.port))
            s.listen(8)
        except OSError:
            s.close()
            raise
        s.settimeout(0.5)
        self._sock = s
        self.port = s.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True, name="modbus-forward").start()
        if self.log:
            self.log.info("Modbus TCP 转发已启动：%s:%d（只读，功能码 03/04）%s", self.host, self.port,
                          "，白名单 %s" % ",".join(self.allow) if self.allow else "")
            for w in security_warnings(self.host, self.allow):
                self.log.warning("安全提示：%s", w)
        return self

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                c, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            ip = addr[0]
            if not ip_allowed(ip, self.allow) or len(self._clients) >= MAX_CLIENTS:
                self.rejected += 1
                if self.log:
                    self.log.warning("Modbus TCP 转发：已拒绝来自 %s 的连接（%s）", ip,
                                     "不在白名单" if not ip_allowed(ip, self.allow) else "连接数已满")
                try:
                    c.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._serve, args=(c, ip), daemon=True).start()

    def _serve(self, c, ip):
        self._clients.add(c)
        c.settimeout(IDLE_TIMEOUT)
        try:
            while not self._stop.is_set():
                head = _recv_exact(c, 7)
                if head is None:
                    break
                length = struct.unpack(">H", head[4:6])[0]
                if not 2 <= length <= 260:
                    break
                rest = _recv_exact(c, length - 1)
                if rest is None:
                    break
                regs, mx = self.snapshot()
                resp = handle_frame(head + rest, regs, mx, self.unit)
                if resp is None:
                    break
                self.requests += 1
                c.sendall(resp)
        except (OSError, socket.timeout):
            pass
        finally:
            self._clients.discard(c)
            try:
                c.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for c in list(self._clients):
            try:
                c.close()
            except OSError:
                pass
        if self.log:
            self.log.info("Modbus TCP 转发已停止")


def client_read(host, port, start, qty, func=3, unit=1, timeout=3.0, tid=1):
    """最小的 Modbus TCP 读客户端（测试 / 窗口“读回核对”用），返回寄存器列表或 ('exception', 代码)。"""
    pdu = struct.pack(">BHH", func, start, qty)
    frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(frame)
        head = _recv_exact(s, 7)
        if head is None:
            raise OSError("连接被关闭")
        length = struct.unpack(">H", head[4:6])[0]
        body = _recv_exact(s, length - 1)
    if body[0] & 0x80:
        return ("exception", body[1])
    n = body[1]
    return [struct.unpack(">H", body[2 + i:4 + i])[0] for i in range(0, n, 2)]


def export_map_csv(mapping, path, order="ABCD", func=3, headers=None):
    """导出映射表 CSV（给 DCS 组态用）：0 基地址、Modbus 1 基地址（4xxxx/3xxxx）、通道、字段、类型、字节序、说明。"""
    hdr = headers or ("0基地址", "Modbus地址", "寄存器数", "通道", "字段", "类型", "字节序", "说明")
    base = 40001 if func == 3 else 30001
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(hdr))
        for it in mapping:
            n = TYPES[it["type"]]
            ch = it.get("label") or it["channel"]
            if str(ch)[:1] in ("=", "+", "-", "@"):
                ch = "'" + str(ch)
            w.writerow([it["address"], base + it["address"], n, ch, it["field"], it["type"],
                        order if n == 2 else "", describe_field(it["field"])])
    return path


def start_from_config(cfg, log=None):
    """服务启动时调用：未启用返回 None；端口被占用等错误写日志并返回 None（不影响 OPC UA 服务）。"""
    if not cfg.get("modbus_server_enabled"):
        return None
    try:
        fw = ModbusForwarder(cfg.get("modbus_server_host"), cfg.get("modbus_server_port", 502),
                             cfg.get("modbus_server_allow"), cfg.get("modbus_server_unit", 0), log=log)
        return fw.start()
    except Exception as e:  # noqa
        if log:
            log.error("Modbus TCP 转发未能启动（%s:%s）：%s。端口 502 需要没有被别的程序占用。",
                      cfg.get("modbus_server_host"), cfg.get("modbus_server_port"), e)
        return None
