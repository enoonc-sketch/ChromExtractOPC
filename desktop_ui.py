#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desktop_ui.py —— 桌面主窗口（tkinter）。v2.6 新增；v2.9 起是本程序唯一的运行方式（桌面软件）

v2.9 变化（不再有后台运行）：
  - 双击 EXE 即打开本窗口；OPC UA 服务在本进程的工作线程里运行（ServiceController），
    窗口顶部可「启动服务 / 停止服务 / 重启服务」，关闭窗口 = 停止服务（会先确认）。
  - 新增「数据与导出」标签页：选择数据目录、查看数据源、导出 CSV。
  - 顶部新增「打开网页设置页」「打开日志文件夹」「自检」按钮。

v3.0 新增（逻辑都在 desktop_features.py，可脱离 tkinter 测试；本文件只负责画界面）：
  - 「通道数据」页：每个通道的最新周期时间 / 周期号 / 点数 / 采样间隔 / 时长 / 状态告警 / 最后更新，
    每 5 秒自动刷新，双击一行跳到谱图预览。
  - 「谱图预览」页：tkinter Canvas 画最新周期谱图曲线（不依赖 matplotlib），可切换通道和
    原始值 / 微伏 / 值，随窗口缩放，显示坐标轴、周期时间、点数。服务运行中用服务的最新快照，
    服务没运行时直接读数据目录。
  - 「客户端连接」页：当前 OPC UA 会话（客户端地址、会话名、用户、连接时间）和连接数 / 上限。
  - 「常用设置」页：端口、扫描周期、告警阈值、导出目录、网页等常用项 + 告警桌面提醒 +
    登录后自动打开窗口 / 自动启动服务 / 启动时最小化；保存时说明哪些自动生效、哪些需重启，
    可一键“保存并重启服务”。
  - 新告警出现时：任务栏闪烁 + Windows 通知气泡（失败时窗口内置顶小提示框），遵守静默时段。
  - 「通讯诊断」页（commscan.py）：IP 扫描（Ping + 主机名 + MAC + 常用工业端口）、单主机端口扫描、
    串口列表（驱动芯片 VID/PID）、RS232/485/422 接线提示、波特率自动探测、Modbus RTU 站号扫描、环回与原始收发；
    全部在工作线程运行，有进度条和停止按钮，结果可导出 CSV，只读探测不写寄存器。

设计：
  - 逻辑层 DesktopLogic 直接复用 web_settings.SettingsServer 的动作/状态方法
    （只构造对象、不启动 HTTP 服务）：保存仍走 config_tools.update_settings
    （校验、备份、原子替换，密码只存哈希），校验规则与服务启动时完全一致，
    告警/设备检测/连接测试/稳定性测试也走同一套后端，行为与网页版相同。
  - 窗口层用 tkinter + ttk.Notebook，四个标签页与网页版一致：
    运行状态 / 数据与导出 / 告警与通知 / 诊断工具 / 系统设置，外加原图形窗口的日志窗和
    数据传输检测 / 发送测试数据 / 智能诊断 / 485配型诊断 / 安全设置按钮。
  - 本模块 import 时不创建任何窗口；只有 run_desktop() 被调用才建窗口，
    无显示环境下 import 不报错（方便自动测试只测逻辑层）。
  - 网页服务（web_settings.py）保留：服务运行时仍可按 web_ui 配置开启，
    供远程/浏览器访问与自检使用；桌面窗口是本机默认入口。
"""
import os
import threading
from datetime import datetime

import chrom_core
import commscan
import config_tools
import desktop_features as df
import i18n
import web_settings
from i18n import M, tr, tr_msg, untr

# 与网页版 F 完全一致的字段分组（保存时只提交改动过的项）
STR_FIELDS = ["endpoint", "server_name", "security", "csv_export_dir", "web_host", "web_https",
              "web_cert_file", "web_key_file", "alert_webhook", "alert_email_to", "smtp_host",
              "smtp_security", "smtp_user", "smtp_from", "alert_webhook_level", "alert_email_level"]
NUM_FIELDS = ["poll_seconds", "last_n", "max_points", "max_connections", "export_min_interval_sec",
              "max_trace_points", "max_file_mb", "log_max_mb", "log_backups", "web_port",
              "alert_stale_minutes", "alert_scan_errors", "smtp_port", "env_check_hours"]
BOOL_FIELDS = ["allow_anonymous", "allow_insecure_fallback", "remote_export", "web_ui",
               "env_check_on_start"]
ALL_FIELDS = STR_FIELDS + NUM_FIELDS + BOOL_FIELDS + ["watch_dirs"]

# 连接测试的种类与参数（与网页版 NT/SER 一致）：(键, 标签, 默认值, 类型)
SERIAL_FIELDS = [("port", "串口", "COM3", "port"), ("baud", "波特率", "9600", "baud"),
                 ("bytesize", "数据位", "8", "bits"), ("parity", "校验", "N", "parity"),
                 ("stopbits", "停止位", "1", "stop")]
NETTEST_SPECS = {
    "ping": {"t": "Ping", "f": [("host", "目标地址", "192.168.1.10", ""),
                                ("count", "次数", "4", ""), ("timeout", "每次超时(秒)", "1", "")]},
    "tcp": {"t": "TCP 端口", "f": [("host", "目标地址", "192.168.1.10", ""), ("port", "端口", "4840", ""),
                                   ("timeout", "超时(秒)", "3", ""), ("send_text", "发送文本（可选）", "", "")]},
    "dns": {"t": "DNS 解析", "f": [("host", "主机名", "dcs-server", "")]},
    "interfaces": {"t": "本机网卡", "f": []},
    "opcua": {"t": "OPC UA", "f": [("endpoint", "服务器地址", "opc.tcp://127.0.0.1:4840/ChromExtract", ""),
                                   ("username", "用户名（可选）", "", ""),
                                   ("password", "密码（可选）", "", "password"),
                                   ("timeout", "超时(秒)", "5", "")]},
    "modbus_tcp": {"t": "Modbus TCP", "f": [("host", "设备地址", "192.168.1.20", ""), ("port", "端口", "502", ""),
                                            ("unit", "站号", "1", ""), ("func", "功能码", "3", "fc"),
                                            ("addr", "起始地址（从 0 开始）", "0", ""),
                                            ("count", "数量", "10", ""), ("timeout", "超时(秒)", "3", "")]},
    "serial_list": {"t": "串口列表", "f": []},
    "serial": {"t": "串口收发 232/485", "f": "S",
               "x": [("send", "发送内容（留空 = 只监听）", "01 03 00 00 00 0A C5 CD", ""),
                     ("mode", "格式", "hex", "mode"), ("timeout", "接收等待(秒)", "1", ""),
                     ("rs485_rts", "RTS 控制收发方向（无自动切换的 485 转换器）", "", "chk")]},
    "loopback": {"t": "串口回环自检", "f": "S", "x": [("timeout", "超时(秒)", "1", "")],
                 "tip": "先把串口 TX 和 RX 短接（DB9 的 2、3 脚），用于确认串口和转换器本身是好的。RS-485 半双工不适用。"},
    "modbus_rtu": {"t": "Modbus RTU", "f": "S",
                   "x": [("unit", "站号", "1", ""), ("func", "功能码", "3", "fc"),
                         ("addr", "起始地址（从 0 开始）", "0", ""), ("count", "数量", "10", ""),
                         ("timeout", "超时(秒)", "1", ""), ("rs485_rts", "RTS 控制收发方向", "", "chk")]},
}
STAB_KINDS = ["ping", "tcp", "dns", "opcua", "modbus_tcp", "modbus_rtu", "serial"]
PROBE_KINDS = STAB_KINDS
SELECT_OPTIONS = {
    "security": [("none", "none（不加密）"), ("sign", "sign（签名）"),
                 ("sign_encrypt", "sign_encrypt（签名+加密，推荐）")],
    "alert_webhook_level": [("all", "严重和警告都发"), ("critical", "只发严重告警"), ("off", "不发")],
    "alert_email_level": [("all", "严重和警告都发"), ("critical", "只发严重告警"), ("off", "不发")],
    "smtp_security": [("ssl", "SSL（一般 465）"), ("starttls", "STARTTLS（一般 587）"),
                      ("none", "不加密（仅内网免登录中继，25）")],
    "web_https": [("auto", "auto（对外开放时自动启用，推荐）"), ("on", "on（始终启用）"),
                  ("off", "off（不启用）")],
}
RESTART_TAG = "（需重启）"


def nettest_fields(kind):
    """某种连接测试的完整字段表（串口类自动拼上串口参数）。"""
    spec = NETTEST_SPECS[kind]
    return list(SERIAL_FIELDS) + list(spec["x"]) if spec["f"] == "S" else list(spec["f"])


def build_nettest_params(kind, raw):
    """从表单原始值拼连接测试参数：只取该种类声明过的键，勾选项转 bool。"""
    params = {}
    for key, _label, _default, typ in nettest_fields(kind):
        if key not in raw:
            continue
        val = raw[key]
        params[key] = bool(val) if typ == "chk" else val
    return params


def collect_settings(values, orig):
    """对比表单值与原配置，只返回改动过的项（与网页版 save() 相同的比较规则）。

    values: {字段: 字符串/bool，watch_dirs 可为列表或多行文本}
    orig:   state()["config"]（合并默认值后的视图）
    """
    changed = {}
    for k in STR_FIELDS + NUM_FIELDS:
        if k in values and str(values[k]) != str(orig.get(k, "")):
            changed[k] = values[k]
    for k in BOOL_FIELDS:
        if k in values and bool(values[k]) != bool(orig.get(k)):
            changed[k] = bool(values[k])
    if "watch_dirs" in values:
        wd = values["watch_dirs"]
        if isinstance(wd, str):
            wd = [x.strip() for x in wd.split("\n") if x.strip()]
        if list(wd) != list(orig.get("watch_dirs") or []):
            changed["watch_dirs"] = list(wd)
    return changed


def quiet_once_item(start, end, note=""):
    """一次性静默时段的条目（start/end 为 'YYYY-MM-DD HH:MM'）。"""
    return {"type": "once", "start": (start or "").strip(), "end": (end or "").strip(),
            "note": (note or "").strip()}


def quiet_daily_item(start, end, days, note=""):
    """每天重复静默时段的条目（days 为 1..7 的列表，空 = 每天）。"""
    return {"type": "daily", "start": (start or "").strip(), "end": (end or "").strip(),
            "days": [int(d) for d in (days or [])], "note": (note or "").strip()}


def channel_rule_body(channel, mute, stale_minutes, until, note):
    """通道告警规则的提交体（与网页版 channel_rule 相同形状）。"""
    return {"channel": channel, "mute": bool(mute),
            "stale_minutes": ("" if stale_minutes in (None, "") else stale_minutes),
            "until": (until or "").strip(), "note": (note or "").strip()}


def probe_body(edit_id, label, kind, params, interval, fail_threshold, level, enabled=True):
    """设备检测的提交体（与网页版 probe_save 相同形状）。"""
    return {"id": edit_id or "", "label": (label or "").strip(), "kind": kind,
            "params": dict(params or {}), "interval": interval,
            "fail_threshold": fail_threshold, "level": level, "enabled": bool(enabled)}


# ---- Modbus Poll / Slave（v2.8）表单参数拼装（纯函数，坏值抛 ValueError） ----
MODBUS_FUNC_LABELS = ["01 读线圈", "02 读离散输入", "03 读保持寄存器", "04 读输入寄存器",
                      "05 写单线圈", "06 写单寄存器", "15 写多线圈", "16 写多寄存器"]
MODBUS_AREA_LABELS = {"线圈": "coils", "离散输入": "discrete", "保持寄存器": "holding", "输入寄存器": "input"}
MODBUS_DTYPES = ["uint16", "int16", "uint32", "int32", "float32", "float64"]
MODBUS_ORDERS = ["ABCD", "CDAB", "BADC", "DCBA"]


def _mb_int(raw, key, label, lo, hi, default=None):
    v = str(raw.get(key, "")).strip()
    if v == "" and default is not None:
        return default
    try:
        n = int(v)
    except ValueError:
        raise ValueError(tr("%s 必须是数字") % label)
    if not lo <= n <= hi:
        raise ValueError(tr("%s 范围 %d～%d") % (label, lo, hi))
    return n


def _mb_transport(raw):
    return "rtu" if str(raw.get("transport", "")).strip().lower() in ("rtu", "串口") else "tcp"


def _mb_serial(raw):
    parity = str(raw.get("parity") or "N").strip().upper()[:1]
    if parity not in ("N", "E", "O"):
        raise ValueError(tr("校验位只能是 N（无）/ E（偶）/ O（奇）"))
    try:
        stopbits = float(raw.get("stopbits") or 1)
    except (TypeError, ValueError):
        raise ValueError(tr("停止位必须是数字"))
    if stopbits not in (1, 1.5, 2):
        raise ValueError(tr("停止位只能是 1 / 1.5 / 2"))
    return {"serial": str(raw.get("serial") or "").strip(),
            "baud": _mb_int(raw, "baud", tr("波特率"), 50, 4000000, 9600),
            "parity": parity, "stopbits": stopbits}


def build_modbus_poll_params(raw):
    """Modbus Poll 表单原始值 → 参数 dict。"""
    p = {"transport": _mb_transport(raw),
         "unit": _mb_int(raw, "unit", tr("站号"), 0, 255, 1),
         "addr": _mb_int(raw, "addr", tr("地址"), 0, 65535, 0),
         "count": _mb_int(raw, "count", tr("数量"), 1, 2000, 10),
         "dtype": str(raw.get("dtype") or "uint16"),
         "order": str(raw.get("order") or "ABCD").upper(),
         "values_text": str(raw.get("values") or "")}
    try:
        p["func"] = int(str(raw.get("func") or "03").split()[0])
    except (ValueError, IndexError):
        raise ValueError(tr("功能码不对"))
    if p["func"] not in (1, 2, 3, 4, 5, 6, 15, 16):
        raise ValueError(tr("不支持的功能码：%s") % p["func"])
    if p["dtype"] not in MODBUS_DTYPES:
        raise ValueError(tr("数据类型只能是：%s") % " / ".join(MODBUS_DTYPES))
    if p["order"] not in MODBUS_ORDERS:
        raise ValueError(tr("字节序只能是：%s") % " / ".join(MODBUS_ORDERS))
    try:
        p["interval"] = float(raw.get("interval") or 1)
    except (TypeError, ValueError):
        raise ValueError(tr("轮询间隔必须是数字（秒）"))
    if p["interval"] < 0.05:
        raise ValueError(tr("轮询间隔至少 0.05 秒"))
    if p["transport"] == "tcp":
        p["host"] = str(raw.get("host") or "").strip()
        if not p["host"]:
            raise ValueError(tr("请填写目标设备地址"))
        p["port"] = _mb_int(raw, "port", tr("TCP 端口"), 1, 65535, 502)
    else:
        p.update(_mb_serial(raw))
        if not p["serial"]:
            raise ValueError(tr("请填写串口，例如 COM3"))
    return p


def build_modbus_slave_params(raw):
    """Modbus Slave 表单原始值 → 参数 dict。"""
    p = {"transport": _mb_transport(raw),
         "unit": _mb_int(raw, "unit", tr("站号"), 0, 255, 1),
         "size": _mb_int(raw, "size", tr("每区大小"), 1, 65536, 10000)}
    if p["transport"] == "tcp":
        p["host"] = str(raw.get("host") or "0.0.0.0").strip() or "0.0.0.0"
        p["tcp_port"] = _mb_int(raw, "tcp_port", tr("监听端口"), 0, 65535, 502)
    else:
        p.update(_mb_serial(raw))
        if not p["serial"]:
            raise ValueError(tr("请填写串口，例如 COM3"))
        if not 1 <= p["unit"] <= 247:
            raise ValueError(tr("RTU 站号范围 1～247"))
    return p


# ---------------------------------------------------------------------------
# v2.9：数据目录 / 数据源 / CSV 导出的纯函数（不依赖窗口，可单独测试）
# ---------------------------------------------------------------------------

def resolve_dirs(dirs, base_dir):
    """配置里的相对目录按程序目录解析（与服务启动时一致），去空、去重、保持顺序。"""
    out = []
    for d in dirs or []:
        d = str(d or "").strip().strip('"').strip()
        if not d:
            continue
        if not os.path.isabs(d):
            d = os.path.join(base_dir, d)
        d = os.path.normpath(d)
        if d not in out:
            out.append(d)
    return out


def merge_watch_dirs(existing, new_dir, base_dir, replace=False):
    """“选择数据目录”（replace=True，只用新目录）/“添加目录”（追加到现有目录后面）。"""
    new = resolve_dirs([new_dir], base_dir)
    if not new:
        raise ValueError(tr("没有选择文件夹。"))
    return new if replace else resolve_dirs(list(existing or []) + new, base_dir)


def parse_last_n(text):
    """导出时“每个流路最近 N 个周期”：留空 = 全部（None），否则必须是正整数。"""
    t = str(text or "").strip()
    if not t:
        return None
    try:
        n = int(t)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(tr("“最近 N 个周期”请填正整数，留空表示全部导出。"))
    return n


def default_export_dir(cfg_or_state, base_dir):
    """默认导出根目录：配置的 csv_export_dir（相对路径按程序目录解析）。"""
    d = str((cfg_or_state or {}).get("csv_export_dir") or "谱图导出")
    return d if os.path.isabs(d) else os.path.join(base_dir, d)


def make_export_dir(root, now=None):
    """每次导出放到带时间戳的子文件夹，避免覆盖上一次的 index.csv。"""
    return os.path.join(root, "桌面导出_" + (now or datetime.now()).strftime("%Y%m%d_%H%M%S"))


def service_status_text(running, up, error, endpoint):
    """窗口顶部状态栏文字与颜色：(文字, 颜色)。"""
    if running and up:
        return tr("● 服务运行中：%s（DCS/SCADA 可连接；关闭窗口会停止服务）") % endpoint, "#1a7f37"
    if running:
        return tr("服务启动中……"), "#9a6700"
    if error:
        return tr("■ 服务未运行：启动失败（%s）。处理后点“启动服务”重试。") % error, "#cf222e"
    return tr("■ 服务已停止：客户端读不到数据。点“启动服务”开始发布。"), "#57606a"


class ServiceController:
    """在本进程的工作线程里运行 OPC UA 服务（不是后台进程：窗口关闭时一起停止）。

    sa 是 server_app 模块。start() 立即返回；is_up() 表示服务已真正开始监听并发布数据；
    stop() 发出停止信号并等待工作线程结束。不依赖 tkinter，可单独测试。"""

    def __init__(self, sa):
        self.sa = sa
        self.cfg = None
        self.error = ""
        self.web = None
        self._thread = None
        self._loop = None
        self._stop = None
        self._done = threading.Event()
        self._done.set()

    def running(self):
        return self._thread is not None and not self._done.is_set()

    def is_up(self):
        return self.running() and self.sa.ALERTS.get("monitor") is not None

    def start(self, cfg, with_web=True):
        """启动服务。已在运行返回 False。"""
        if self.running():
            return False
        import asyncio
        self.cfg, self.error = cfg, ""
        self._done.clear()
        ready = threading.Event()
        sa = self.sa

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _main():
                self._loop, self._stop = loop, asyncio.Event()
                ready.set()
                await sa.run_server(cfg, self._stop)

            try:
                loop.run_until_complete(_main())
            except Exception as e:  # noqa  最常见：端口被占用
                self.error = str(e) or type(e).__name__
                sa.get_log().error("OPC UA 服务未能运行：%s。如果是端口被占用，请关闭占用 %s 的程序，"
                                   "或在“系统设置”里改 endpoint 端口后重新启动服务。", self.error, cfg.get("endpoint"))
            finally:
                ready.set()
                try:
                    loop.close()
                except Exception:  # noqa
                    pass
                self._loop = self._stop = None
                self._done.set()

        self._thread = threading.Thread(target=worker, daemon=True, name="opcua-service")
        self._thread.start()
        ready.wait(5)
        if with_web:
            self.ensure_web(cfg)
        return True

    def stop(self, timeout=10):
        """请求停止并等待；返回是否在 timeout 秒内停下。同时关闭网页设置页。"""
        lp, ev = self._loop, self._stop
        if lp is not None and ev is not None:
            try:
                lp.call_soon_threadsafe(ev.set)
            except RuntimeError:      # 事件循环已关闭
                pass
        ok = self._done.wait(timeout)
        self.stop_web()
        return ok

    def ensure_web(self, cfg):
        """网页设置页（按 web_ui 配置）；已开启就复用。失败返回 None（原因写日志）。"""
        if self.web is None:
            self.web = self.sa.start_web_settings(cfg)
        return self.web

    def stop_web(self):
        if self.web is not None:
            try:
                self.web.stop()
            except Exception:  # noqa
                pass
            self.web = None


class DesktopLogic:
    """桌面窗口的逻辑层：复用 SettingsServer 的动作与状态方法（不启动 HTTP）。

    cfg_mod 是 server_app 模块（提供 DEFAULT_CONFIG / load_config / normalize_config /
    security_report / HOT_KEYS / RESTART_KEYS / ALERTS 等）。
    """

    def __init__(self, cfg_path, base_dir, cfg_mod, log=None):
        self.cfg_path, self.base_dir, self.m = cfg_path, base_dir, cfg_mod
        self.srv = web_settings.SettingsServer(cfg_path, base_dir, cfg_mod, log=log)
        self.actor = None          # v3.1：窗口设置为返回 "desktop:用户名" 的函数，写审计用

    def _who(self):
        return self.actor() if callable(self.actor) else "desktop"

    # ---- 状态读取 ----
    def state(self):
        st = self.srv.state()
        st["alerts"] = self.srv.alerts()
        return st

    def alerts(self):
        return self.srv.alerts()

    def probes(self):
        return self.srv.probe_status()

    def env(self):
        return self.srv.env_status()

    def stability(self):
        return self.srv.stab_status()

    # ---- 动作（与网页版 handle_action 完全相同）----
    def action(self, name, body=None):
        return self.srv.handle_action(name, body or {}, actor=self._who())

    def save(self, values):
        """保存表单：只提交改动项，校验不过抛 ConfigToolError 且不写文件。返回提示文字。"""
        changed = collect_settings(values, self.srv.state()["config"])
        if not changed:
            return tr("没有改动。")
        return self.srv.handle_action("save", {"settings": changed}, actor=self._who())

    def nettest(self, kind, params):
        return self.srv.handle_action("nettest", {"kind": kind, "params": params}, actor=self._who())

    def stop_stability(self):
        self.srv.stop()     # 只停稳定性测试；HTTP 未启动，stop() 里 httpd 为 None

    def stab_csv_path(self, name):
        return os.path.join(self.srv._stab_dir(), name) if name else ""

    # ---- v2.9：数据目录 / 数据源 / CSV 导出 ----
    def watch_dirs(self):
        """当前配置文件里的监控目录（绝对路径）。"""
        return resolve_dirs(config_tools.read_raw(self.cfg_path).get("watch_dirs"), self.base_dir)

    def set_watch_dirs(self, dirs):
        """写入监控目录（必须存在，保存为绝对路径，其它配置不动）。返回保存后的列表。"""
        return config_tools.set_watch_dirs(self.cfg_path, resolve_dirs(dirs, self.base_dir))

    def _sources(self, dirs):
        dirs = resolve_dirs(dirs if dirs is not None else self.watch_dirs(), self.base_dir)
        if not dirs:
            raise config_tools.ConfigToolError(tr("还没有设置数据目录：请先点“选择数据目录…”。"))
        missing = [d for d in dirs if not os.path.exists(d)]
        if missing:
            raise config_tools.ConfigToolError(tr("找不到数据目录：%s") % tr("、").join(missing))
        return dirs

    def list_sources_text(self, dirs=None):
        """查看数据源（与命令行 --list 相同），返回给人看的文字。"""
        dirs = self._sources(dirs)
        lines = []
        chrom_core.list_sources(dirs, log=lines.append)
        return "\n".join(lines) or tr("目录里没有找到 *_V3.db 或 .mbd 数据文件：%s") % tr("、").join(dirs)

    def export_csv(self, outdir, last=None, dirs=None):
        """导出 CSV（与命令行 --extract 相同）。返回 (导出条数, 过程文字)。"""
        dirs = self._sources(dirs)
        lines = []
        n = chrom_core.extract(dirs, outdir, last=last, log=lines.append)
        return n, "\n".join(lines)

    # ---- v3.0：常用设置 ----
    def save_quick(self, values):
        """保存“常用设置”页：先做范围校验（QuickSettingError，列出全部原因），
        再走 config_tools.update_settings + normalize_config。返回 save_quick_settings 的结果字典。"""
        import audit
        upd = df.quick_updates(values, self.srv.state()["config"])
        before = config_tools.read_raw(self.cfg_path)
        res = df.save_quick_settings(self.cfg_path, self.base_dir, self.m, upd)
        try:
            ch = audit.diff(before, config_tools.read_raw(self.cfg_path))
            who = self._who()
            audit.get_log(os.path.join(self.base_dir, "logs", "audit.log")).record(
                who.split(":", 1)[-1], "save_quick", ",".join(sorted(ch)), {k: v[0] for k, v in ch.items()} or None,
                {k: v[1] for k, v in ch.items()} or None, source="desktop")
        except Exception:  # noqa
            pass
        return res


# ---------------------------------------------------------------------------
# 窗口层（tkinter）。以下代码只在 run_desktop() 调用时执行，import 本模块不建窗口。
# ---------------------------------------------------------------------------

def _scrollable(parent, tk, ttk):
    """在 parent 里放一个可滚动区域，返回内层 Frame。"""
    canvas = tk.Canvas(parent, highlightthickness=0)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def _wheel(e):
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
    canvas.bind_all("<MouseWheel>", _wheel, add="+")
    return inner


# ---------------------------------------------------------------------------
# v3.1 窗口功能（逻辑都在不依赖 tkinter 的模块里：backup_tools / history / peaks /
# modbus_server / reports / audit / i18n；这里只画界面、调用逻辑）
# ---------------------------------------------------------------------------

# 按钮 / 操作 → 需要的权限（audit.PERMISSIONS）。窗口建控件前把这些方法换成“先检查权限”的版本。
GUARDED = {
    "service": ("_start_service", "_stop_service", "_restart_service", "_selftest"),
    "config": ("_save", "_qs_save", "_apply_watch_dirs", "_pick_data_dir", "_ch_edit", "_pr_add", "_pr_edit",
               "_pr_toggle", "_pr_remove", "_quiet_remove", "_quiet_once", "_quiet_daily", "_set_smtp_pw",
               "_clear_smtp_pw", "_toggle_login", "_do_action", "_open_web", "_pk_save", "_mb_save_settings",
               "_mb_save_map", "_mb_auto_map", "_mb_add_row", "_mb_remove_rows", "_rp_save_settings"),
    "diagnose": ("_gui_transmission", "_gui_diagnose", "_gui_rtu485", "_nt_run", "_st_start", "_mt_start",
                 "_mbp_once", "_mbp_toggle_poll", "_mbs_toggle", "_mbs_write_area", "_dc_run", "_ip_scan",
                 "_ps_scan", "_sp_detect", "_sp_slaves", "_sp_loopback", "_sp_raw", "_pr_run", "_test_notify"),
    "export": ("_export_csv", "_cs_export", "_hs_export", "_pk_export", "_mb_export_map"),
    "report": ("_rp_generate",),
    "users": ("_add_user", "_remove_user", "_ur_set_role", "_ur_save_auth"),
    "security": ("_open_security", "_harden"),
    "backup": ("_bk_export",),
    "restore": ("_bk_restore", "_bk_pick_restore", "_bk_restore_auto"),
    "audit_export": ("_au_export",),
}
LANG_OPTIONS = (("auto", "跟随系统"), ("zh", "中文"), ("en", "English"))
HIST_SERIES = (("value", "值"), ("uv", "微伏"), ("raw", "原始值"))
PEAK_FIELDS = (("min_height", "最小峰高"), ("min_width", "最小半峰宽(s)"), ("noise_factor", "噪声倍数"),
               ("smooth", "平滑点数"), ("start", "积分起点(s)"), ("end", "积分终点(s)"))
REPORT_MODES = (("off", "不自动生成"), ("daily", "每天自动生成"), ("shift", "每班自动生成"))
ROLE_OPTIONS = (("operator", "操作员（只看）"), ("engineer", "工程师（改配置、诊断、导出）"),
                ("admin", "管理员（账号、安全、备份还原）"))


class V31Mixin:
    """DesktopWindow 的 v3.1 部分：登录与权限、语言切换、峰表、历史对比、Modbus 转发、运行报表、备份与审计。"""

    # ---- 初始化（在建任何控件之前调用）----
    def _v31_pre(self):
        import audit
        import history
        import peaks
        self._audit_mod, self._hist_mod, self._peaks_mod = audit, history, peaks
        cfg = self.cfg
        i18n.init_from_config(cfg)
        self._lang = i18n.get_language()
        self.session = audit.Session(lambda: config_tools.read_raw(self.cfg["_cfg_path"]),
                                     config_tools.verify_password,
                                     idle_minutes=cfg.get("desktop_idle_lock_minutes", 15),
                                     auth_enabled=cfg.get("desktop_auth", True))
        self.audit_log = audit.get_log(cfg)
        self.logic.actor = self._actor
        self._role_widgets = []     # [(控件, 权限)]
        self._raw = {}
        for perm, names in GUARDED.items():
            for name in names:
                fn = getattr(self, name, None)
                if fn is not None:
                    self._raw[name] = fn
                    setattr(self, name, self._guard(perm, fn))
        self._peak_params = peaks.params_from_config(cfg)
        self._pk_rows = []
        self._hist_store = None
        self._hs_rows = {}
        self._hs_overlay = []
        self._mb_map = list(cfg.get("modbus_server_map") or [])
        self._bk_info = None
        self._bk_path = ""
        self._bk_password = None
        self._rp_files = []
        self._au_rows = []
        self._auto_rep = None

    def _actor(self):
        return "desktop:" + self.session.actor()

    def _audit(self, action, target="", before=None, after=None, ok=True, detail=""):
        try:
            self.audit_log.record(self.session.actor(), action, target, before, after, source="desktop",
                                  ok=ok, detail=str(detail)[:300])
        except Exception as e:  # noqa
            self.sa.get_log().error("审计日志写入失败：%s", e)

    def _unguarded(self, name):
        return self._raw.get(name) or getattr(self, name)

    def _guard(self, perm, fn):
        def wrapped(*a, **k):
            if not self.session.can(perm):
                import audit
                need = audit.ROLE_NAMES[audit.PERMISSIONS.get(perm, "admin")]
                self._show(False, tr("这个操作需要“%s”或更高权限。\n请点窗口上方的“登录”，用有权限的账号登录。") % tr(need))
                return None
            self.session.touch()
            return fn(*a, **k)
        return wrapped

    def _perm(self, widget, perm):
        self._role_widgets.append((widget, perm))
        return widget

    # ---- 登录栏 ----
    def _build_login_bar(self, root):
        tk = self.tk
        bar = tk.Frame(root)
        bar.pack(fill="x", padx=10, pady=(0, 2))
        self.user_var = tk.StringVar(value="")
        self.user_lbl = tk.Label(bar, textvariable=self.user_var, anchor="w", fg="#1f4e79")
        self.user_lbl.pack(side="left")
        self.btn_login = tk.Button(bar, text=tr("登录"), command=self._login_dialog)
        self.btn_login.pack(side="left", padx=4)
        self.btn_logout = tk.Button(bar, text=tr("注销"), command=self._logout)
        self.btn_logout.pack(side="left", padx=2)
        self.btn_lock = tk.Button(bar, text=tr("锁定"), command=lambda: self._lock(tr("手动锁定")))
        self.btn_lock.pack(side="left", padx=2)
        self.btn_admin = tk.Button(bar, text=tr("创建管理员…"), command=self._create_admin_dialog, fg="#cf222e")
        self.btn_admin.pack(side="left", padx=8)
        for ev in ("<Any-KeyPress>", "<Any-ButtonPress>"):
            try:
                root.bind_all(ev, lambda _e: self.session.touch(), add="+")
            except Exception:  # noqa
                pass
        self._render_user()

    def _render_user(self):
        import audit
        s = self.session
        if not s.auth_enabled:
            text = tr("桌面登录已关闭：所有操作都可用（可在“备份与审计 → 账号与角色”开启）")
        elif s.user:
            text = tr("当前用户：%s（%s）") % (s.user, tr(audit.ROLE_NAMES.get(s.role, s.role)))
        else:
            text = tr("未登录：操作员（只读）。修改配置、诊断、导出请先登录。")
        self.user_var.set(text)
        need_admin = s.auth_enabled and not audit.has_admin(config_tools.read_raw(self.cfg["_cfg_path"]))
        try:
            if need_admin:
                self.btn_admin.pack(side="left", padx=8)
            else:
                self.btn_admin.pack_forget()
            self.btn_login.configure(state="normal" if s.auth_enabled else "disabled")
            self.btn_logout.configure(state="normal" if s.user else "disabled")
            self.btn_lock.configure(state="normal" if s.user else "disabled")
        except Exception:  # noqa
            pass
        self._apply_role()

    def _apply_role(self):
        for w, perm in self._role_widgets:
            try:
                w.configure(state="normal" if self.session.can(perm) else "disabled")
            except Exception:  # noqa
                pass
        try:
            self._set_service_buttons()
        except Exception:  # noqa
            pass

    def _do_login(self, name, password):
        import audit
        try:
            role = self.session.login(name, password)
        except audit.AuthError as e:
            self._audit("login", name, ok=False, detail=str(e))
            self._show(False, tr_msg(e))
            return False
        self._audit("login", name, detail=role)
        self._render_user()
        return True

    def _login_dialog(self):
        name = self.sd.askstring(tr("登录"), tr("用户名："), parent=self.root)
        if not name:
            return
        pw = self.sd.askstring(tr("登录"), tr("密码："), show="*", parent=self.root)
        if pw is None:
            return
        self._do_login(name.strip(), pw)

    def _logout(self):
        if self.session.user:
            self._audit("logout", self.session.user)
        self.session.logout()
        self._render_user()

    def _lock(self, why=""):
        if not self.session.user:
            return
        self._audit("lock", self.session.user, detail=why)
        self.session.logout()
        self._render_user()
        self.user_var.set(tr("已锁定（%s）：请重新登录。") % why)

    def _poll_idle(self):
        if self._closed:
            return
        if self.session.idle_expired():
            self._lock(tr("空闲 %d 分钟自动锁定") % self.session.idle_minutes)
        self.root.after(30000, self._poll_idle)

    def _do_create_admin(self, name, pw):
        import audit
        try:
            res = audit.create_admin(self.cfg["_cfg_path"], name, pw, config_tools)
        except (audit.AuthError, config_tools.ConfigToolError) as e:
            self._show(False, tr_msg(e))
            return False
        self._audit("create_admin", name, detail=res)
        self.session.login(name, pw)
        self._render_user()
        self._show(True, tr("已创建管理员 %s 并登录。\n同一个账号也可以登录 OPC UA（只读）。") % name)
        return True

    def _create_admin_dialog(self):
        name = self.sd.askstring(tr("创建管理员"), tr("管理员用户名："), parent=self.root)
        if not name:
            return
        pw = self.sd.askstring(tr("创建管理员"), tr("密码（至少 8 位）："), show="*", parent=self.root)
        if not pw:
            return
        if self.sd.askstring(tr("创建管理员"), tr("再输入一次密码："), show="*", parent=self.root) != pw:
            return self._show(False, tr("两次输入的密码不一致。"))
        self._do_create_admin(name.strip(), pw)

    # ---- 语言 ----
    def _build_menu(self, root):
        tk = self.tk
        try:
            menubar = tk.Menu(root)
            lm = tk.Menu(menubar, tearoff=0)
            self.lang_menu_var = tk.StringVar(value=str(self.cfg.get("desktop_language", "auto")))
            for code, label in LANG_OPTIONS:
                lm.add_radiobutton(label=label if code != "auto" else "跟随系统 / Auto", value=code,
                                   variable=self.lang_menu_var, command=lambda c=code: self._set_language(c))
            menubar.add_cascade(label="语言 / Language", menu=lm)
            root.config(menu=menubar)
            self.menubar = menubar
        except Exception:  # noqa
            self.menubar = None

    def _build_lang_section(self, inner):
        tk, ttk = self.tk, self.ttk
        sec = self._section(inner, tr("界面语言"))
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("界面语言"), width=26, anchor="w").pack(side="left")
        self.lang_combo = ttk.Combobox(row, values=[tr(t) for _c, t in LANG_OPTIONS], state="readonly", width=16)
        self.lang_combo.pack(side="left")
        codes = [c for c, _t in LANG_OPTIONS]
        cur = str(self.cfg.get("desktop_language", "auto"))
        self.lang_combo.current(codes.index(cur) if cur in codes else 0)
        self.lang_combo.bind("<<ComboboxSelected>>",
                             lambda _e: self._set_language(codes[max(0, self.lang_combo.current())]))
        tk.Label(sec, text=tr("切换后立即生效并保存（desktop_language）。日志内容和 OPC UA 节点名不翻译，"
                              "以免影响 DCS 组态；后台返回的个别诊断细节仍为中文。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")

    def _set_language(self, code):
        """切换界面语言：保存到配置，然后把窗口里所有已知文字换成新语言（即时刷新）。"""
        old = i18n.get_language()
        new = i18n.set_language(code)
        try:
            config_tools.update_settings(self.cfg["_cfg_path"], {"desktop_language": code})
            self.cfg["desktop_language"] = code
            self._audit("language", "desktop_language", None, {"desktop_language": code})
        except Exception as e:  # noqa
            self.sa.get_log().error("保存界面语言失败：%s", e)
        try:
            self.lang_menu_var.set(code)
            self.lang_combo.current([c for c, _t in LANG_OPTIONS].index(code))
        except Exception:  # noqa
            pass
        if old != new:
            n = self._retranslate(old, new)
            self._lang = new
            self.sa.get_log().info("界面语言已切换为 %s（刷新 %d 处文字）", new, n)
        return new

    def _retranslate(self, old, new):
        """遍历窗口里的控件，把旧语言的显示文字换成新语言。返回改了几处。"""
        count = 0

        def fix(text):
            return i18n.retranslate_text(text, old, new)

        def walk(w):
            nonlocal count
            try:
                t = w.cget("text")
            except Exception:  # noqa
                t = None
            nt = fix(t) if isinstance(t, str) else None
            if nt is not None:
                try:
                    w.configure(text=nt)
                    count += 1
                except Exception:  # noqa
                    pass
            try:                                   # Notebook 标签
                n_tabs = w.index("end")
                if isinstance(n_tabs, int):
                    for i in range(n_tabs):
                        nt = fix(w.tab(i, "text"))
                        if nt is not None:
                            w.tab(i, text=nt)
                            count += 1
            except Exception:  # noqa
                pass
            try:                                   # Treeview 表头
                cols = w.cget("columns")
                if cols and not isinstance(cols, str):
                    for col in cols:
                        nt = fix(w.heading(col, "text"))
                        if nt is not None:
                            w.heading(col, text=nt)
                            count += 1
            except Exception:  # noqa
                pass
            try:                                   # 下拉框：选项和当前值
                vals = w.cget("values")
                if vals and not isinstance(vals, str):
                    cur = w.get()
                    nv = [fix(v) or v for v in vals]
                    if list(nv) != list(vals):
                        w.configure(values=nv)
                        count += 1
                    nc = fix(cur)
                    if nc is not None:
                        w.set(nc)
            except Exception:  # noqa
                pass
            try:
                kids = w.winfo_children()
            except Exception:  # noqa
                kids = []
            for k in kids:
                walk(k)
        walk(self.root)
        # 绑定在 StringVar 上的文字（状态栏、提示等）
        var_cls = getattr(self.tk, "StringVar", None)
        for v in list(vars(self).values()):
            if var_cls is not None and isinstance(v, var_cls):
                try:
                    nt = fix(v.get())
                    if nt is not None:
                        v.set(nt)
                        count += 1
                except Exception:  # noqa
                    pass
        try:
            self.root.title(tr("色谱提取 OPC UA 服务 v%s") % self.sa.__version__)
        except Exception:  # noqa
            pass
        # 动态内容重新生成一遍
        for fn in (self._render_user, self._set_service_buttons, self._render_channel_table,
                   lambda: self._pv_draw(force=True), self._reload, self._refresh_sec,
                   self._pk_render, self._mb_render_map, self._mb_render_status):
            try:
                fn()
            except Exception:  # noqa
                pass
        return count

    # ---- 谱图预览：峰识别与峰表 ----
    def _build_peaks_panel(self, tab):
        tk, ttk = self.tk, self.ttk
        frm = tk.LabelFrame(tab, text=tr("峰识别与峰表"), padx=6, pady=4)
        frm.pack(fill="x", padx=8, pady=(0, 6))
        row = tk.Frame(frm)
        row.pack(fill="x")
        self.pk_show = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text=tr("在曲线上标注峰"), variable=self.pk_show,
                       command=lambda: self._pv_draw(force=True)).pack(side="left")
        self.pk_vars = {}
        for key, label in PEAK_FIELDS:
            tk.Label(row, text=tr(label)).pack(side="left", padx=(8, 2))
            v = tk.StringVar(value="%g" % self._peak_params[key])
            tk.Entry(row, textvariable=v, width=7).pack(side="left")
            self.pk_vars[key] = v
        row2 = tk.Frame(frm)
        row2.pack(fill="x", pady=2)
        tk.Button(row2, text=tr("按参数重新找峰"), command=self._pk_apply).pack(side="left", padx=2)
        self._perm(tk.Button(row2, text=tr("保存为默认参数"), command=self._pk_save), "config").pack(side="left", padx=2)
        self._perm(tk.Button(row2, text=tr("导出峰表 CSV"), command=self._pk_export), "export").pack(side="left", padx=2)
        self.pk_note = tk.StringVar(value="")
        tk.Label(row2, textvariable=self.pk_note, fg="#555").pack(side="left", padx=8)
        import peaks
        cols = tuple("c%d" % i for i in range(len(peaks.CSV_COLUMNS)))
        self.pk_tree = ttk.Treeview(frm, columns=cols, show="headings", height=5)
        for c, title in zip(cols, peaks.CSV_COLUMNS):
            self.pk_tree.heading(c, text=tr(title))
            self.pk_tree.column(c, width=100, anchor="e")
        self.pk_tree.pack(fill="x")

    def _pk_read_params(self):
        import peaks
        raw = {k: v.get().strip() for k, v in self.pk_vars.items()}
        return peaks.normalize_params({k: v for k, v in raw.items() if v != ""})

    def _pk_apply(self):
        try:
            self._peak_params = self._pk_read_params()
        except ValueError as e:
            return self._show(False, tr_msg(e))
        self._pv_draw(force=True)

    def _pk_save(self):
        try:
            p = self._pk_read_params()
        except ValueError as e:
            return self._show(False, tr_msg(e))
        import peaks
        upd = {ck: p[pk] for ck, pk in peaks.CONFIG_KEYS.items()}
        try:
            changed = config_tools.update_settings(self.cfg["_cfg_path"], upd, self.logic.srv._validate)
        except config_tools.ConfigToolError as e:
            return self._show(False, tr_msg(e))
        self._peak_params = p
        self._audit("save", "peak_*", None, upd)
        self._show(True, tr("峰识别参数已保存（%d 项改动）。服务运行中下一轮扫描起按新参数计算。") % len(changed))

    def _pk_compute(self, rec):
        import peaks
        if rec is None:
            return []
        try:
            return peaks.peaks_for_record(rec, self._peak_params, self.pv_series.get())["peaks"]
        except Exception as e:  # noqa
            self.pk_note.set(tr("找峰失败：%s") % e)
            return []

    def _pk_render(self):
        import peaks
        tree = self.pk_tree
        for iid in tree.get_children():
            tree.delete(iid)
        for row in peaks.table_rows(self._pk_rows):
            tree.insert("", "end", values=row)
        self.pk_note.set(tr("共 %d 个峰") % len(self._pk_rows) if self._pk_rows else tr("没有找到峰（可调低噪声倍数或最小峰高）"))

    def _pv_draw_peaks(self, c, lay, rec):
        """在谱图上标注峰：顶点编号、起止点基线。"""
        self._pk_rows = self._pk_compute(rec)
        self._pk_render()
        if not self.pk_show.get() or lay.get("empty") or "xrange" not in lay:
            return 0
        x0, y0, x1, y1 = lay["box"]
        xmin, xmax = lay["xrange"]
        ymin, ymax = lay["yrange"]

        def px(v):
            return x0 + (v - xmin) / (xmax - xmin) * (x1 - x0)

        def py(v):
            return y1 - (v - ymin) / (ymax - ymin) * (y1 - y0)
        n = 0
        for p in self._pk_rows[:60]:
            ax, ay = px(p["rt"]), py(p["apex_y"])
            c.create_line(px(p["start"]), py(p["start_y"]), px(p["end"]), py(p["end_y"]), fill="#d1242f", dash=(3, 2))
            c.create_line(ax, ay - 2, ax, ay - 12, fill="#d1242f")
            c.create_text(ax, ay - 14, text=str(p["no"]), anchor="s", font=("Microsoft YaHei", 8), fill="#d1242f")
            n += 1
        return n

    def _pk_export(self):
        import peaks
        if not self._pk_rows:
            return self._show(False, tr("当前谱图没有峰可导出。"))
        rec = df.find_record(self._recs, self._pv_key) or {}
        root = default_export_dir(self.cfg, self.cfg["_base_dir"])
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "峰表_%s_%s.csv" % (rec.get("cycle_id"), datetime.now().strftime("%Y%m%d_%H%M%S")))
        meta = [(tr("通道"), df.channel_label(rec)), (tr("周期时间"), rec.get("cycle_time")),
                (tr("周期号"), rec.get("cycle_id")), (tr("数据"), tr(dict(df.SERIES).get(self.pv_series.get(), "")))]
        peaks.export_csv(self._pk_rows, path, meta, [tr(h) for h in peaks.CSV_COLUMNS])
        self._audit("export", "peaks", detail=path)
        self._show(True, tr("峰表已导出：%s") % path)

    # ---- 历史对比 ----
    def _build_history_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("历史对比"))
        self.tab_history = tab
        top = tk.Frame(tab)
        top.pack(fill="x", padx=8, pady=(8, 2))
        today = datetime.now().strftime("%Y-%m-%d")
        self.hs_from, self.hs_to = tk.StringVar(value=today), tk.StringVar(value=today)
        tk.Label(top, text=tr("日期从")).pack(side="left")
        tk.Entry(top, textvariable=self.hs_from, width=11).pack(side="left", padx=2)
        tk.Label(top, text=tr("到")).pack(side="left")
        tk.Entry(top, textvariable=self.hs_to, width=11).pack(side="left", padx=2)
        tk.Label(top, text=tr("  通道")).pack(side="left")
        self.hs_chan = ttk.Combobox(top, values=[tr("全部通道")], state="readonly", width=50)
        self.hs_chan.current(0)
        self.hs_chan.pack(side="left", padx=2)
        self._hs_chan_keys = [""]
        tk.Button(top, text=tr("查询"), command=self._hs_query).pack(side="left", padx=4)
        tk.Button(top, text=tr("近 7 天"), command=lambda: self._hs_range(7)).pack(side="left", padx=2)
        self.hs_note = tk.StringVar(value=tr("服务运行期间每个新周期都会存入 history\\history.db；在这里按日期和通道查询。"))
        tk.Label(tab, textvariable=self.hs_note, anchor="w", fg="#555").pack(fill="x", padx=8)
        mid = tk.Frame(tab)
        mid.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("time", "channel", "cycle", "points", "duration")
        self.hs_tree = ttk.Treeview(mid, columns=cols, show="headings", height=8, selectmode="extended")
        for col, title, width in (("time", tr("周期时间"), 150), ("channel", tr("通道"), 420),
                                  ("cycle", tr("周期号"), 80), ("points", tr("点数"), 70),
                                  ("duration", tr("时长(s)"), 80)):
            self.hs_tree.heading(col, text=title)
            self.hs_tree.column(col, width=width, anchor="w")
        self.hs_tree.pack(fill="x")
        bar = tk.Frame(mid)
        bar.pack(fill="x", pady=2)
        tk.Button(bar, text=tr("叠加对比所选周期"), command=self._hs_compare).pack(side="left", padx=2)
        self._perm(tk.Button(bar, text=tr("导出所选周期 CSV"), command=self._hs_export), "export").pack(side="left", padx=2)
        self.hs_series = tk.StringVar(value="value")
        for key, text in HIST_SERIES:
            tk.Radiobutton(bar, text=tr(text), value=key, variable=self.hs_series,
                           command=self._hs_draw).pack(side="left")
        tk.Label(bar, text=tr("（按住 Ctrl / Shift 多选，最多叠加 10 条）"), fg="#777").pack(side="left", padx=6)
        self.hs_canvas = tk.Canvas(mid, bg="white", highlightthickness=1, highlightbackground="#d0d7de", height=300)
        self.hs_canvas.pack(fill="both", expand=True, pady=4)
        self.hs_canvas.bind("<Configure>", lambda _e: self._hs_draw())

    def _hs_open(self):
        if self._hist_store is None:
            path = self._hist_mod.history_path(self.cfg)
            self._hist_store = self._hist_mod.HistoryStore(path, readonly=True)
        return self._hist_store

    def _hs_range(self, days):
        from datetime import timedelta
        self.hs_to.set(datetime.now().strftime("%Y-%m-%d"))
        self.hs_from.set((datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d"))
        self._hs_query()

    def _hs_query(self):
        f, t = self.hs_from.get().strip(), self.hs_to.get().strip()
        for v in (f, t):
            if v and not self._hist_mod.norm_time(v):
                return self._show(False, tr("日期格式不对：%s（写成 2026-11-26）") % v)
        start = (self._hist_mod.norm_time(f) or "") if f else None
        end = (self._hist_mod.norm_time(t)[:10] + " 23:59:59") if t else None
        idx = self.hs_chan.current()
        chan = self._hs_chan_keys[idx] if 0 <= idx < len(self._hs_chan_keys) else ""

        def work():
            st = self._hs_open()
            return st.channels(), st.query_cycles(start, end, [chan] if chan else None), st.stats()

        def done(res, err):
            if err is not None:
                self.hs_note.set(tr_msg(err))
                return
            chans, rows, stats = res
            self._hs_chan_keys = [""] + [c["channel"] for c in chans]
            self.hs_chan.configure(values=[tr("全部通道")] + [df.translate_label(c["label"]) for c in chans])
            self.hs_chan.current(self._hs_chan_keys.index(chan) if chan in self._hs_chan_keys else 0)
            tree = self.hs_tree
            for iid in tree.get_children():
                tree.delete(iid)
            self._hs_rows = {}
            for r in rows:
                iid = tree.insert("", "end", values=(r["cycle_time"], df.translate_label(r["label"]), r["cycle_id"],
                                                     r["num_points"], "%.1f" % (r["duration"] or 0)))
                self._hs_rows[iid] = r["id"]
            self.hs_note.set(tr("找到 %d 个周期（库中共 %d 个，%.1f MB，最早 %s）") % (
                len(rows), stats["cycles"], stats["db_bytes"] / 1048576.0, stats["first"] or "-"))
        self._bg(work, done)

    def _hs_selected(self):
        return [self._hs_rows[i] for i in self.hs_tree.selection() if i in self._hs_rows]

    def _hs_compare(self):
        ids = self._hs_selected()
        if not ids:
            return self._show(False, tr("请先在列表里选中要对比的周期（可按住 Ctrl 多选）。"))
        ids = ids[:self._hist_mod.MAX_OVERLAY]

        def done(recs, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            for r in recs:
                r["label"] = df.translate_label(r.get("label"))
            self._hs_overlay = recs
            self._hs_draw()
        self._bg(lambda: [self._hs_open().load_cycle(i) for i in ids], done)

    def _hs_draw(self):
        c = self.hs_canvas
        try:
            w, h = int(c.winfo_width()), int(c.winfo_height())
        except Exception:  # noqa
            return
        c.delete("all")
        if not self._hs_overlay:
            c.create_text(w / 2, h / 2, text=tr("选中周期后点“叠加对比所选周期”"), fill="#777")
            return
        lay = self._hist_mod.overlay_layout(self._hs_overlay, self.hs_series.get(), w, h)
        if not lay["ok"]:
            c.create_text(w / 2, h / 2, text=tr(lay["msg"]), fill="#777")
            return
        x0, y0, x1, y1 = lay["box"]
        font = ("Microsoft YaHei", 8)
        for px_, label in lay["xticks"]:
            c.create_line(px_, y0, px_, y1, fill="#eef1f4")
            c.create_text(px_, y1 + 4, text=label, anchor="n", font=font, fill="#57606a")
        for py_, label in lay["yticks"]:
            c.create_line(x0, py_, x1, py_, fill="#eef1f4")
            c.create_text(x0 - 4, py_, text=label, anchor="e", font=font, fill="#57606a")
        c.create_rectangle(x0, y0, x1, y1, outline="#8c959f")
        for i, (color, flat, legend) in enumerate(lay["lines"]):
            if len(flat) >= 4:
                c.create_line(*flat, fill=color, width=1)
            ly = y0 + 4 + i * 14
            c.create_line(x1 + 8, ly + 6, x1 + 26, ly + 6, fill=color, width=2)
            c.create_text(x1 + 30, ly, text=legend[-40:], anchor="nw", font=font, fill="#333")
        c.create_text((x0 + x1) / 2, h - 2, text=tr("时间（%s）") % "s", anchor="s", font=font, fill="#333")

    def _hs_export(self):
        ids = self._hs_selected()
        if not ids:
            return self._show(False, tr("请先选中要导出的周期。"))
        root = default_export_dir(self.cfg, self.cfg["_base_dir"])
        path = os.path.join(root, "历史周期_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        hdr = [tr(h) for h in ("通道", "周期号", "周期时间", "时间(s)", "原始值", "微伏", "值")]

        def work():
            os.makedirs(root, exist_ok=True)
            return self._hist_mod.export_cycles_csv(self._hs_open(), ids, path, hdr)

        def done(n, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            self._audit("export", "history", detail="%d -> %s" % (n, path))
            self._show(True, tr("已导出 %d 个周期：%s") % (n, path))
        self._bg(work, done)

    # ---- Modbus 转发 ----
    def _build_modbus_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        import modbus_server as ms
        self._ms = ms
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("Modbus 转发"))
        self.tab_modbus = tab
        inner = _scrollable(tab, tk, ttk)
        sec = self._section(inner, tr("Modbus TCP 只读从站"))
        tk.Label(sec, text=tr("把每个通道的周期号、点数、心跳、告警数、峰面积等写成 Modbus 寄存器，供只支持 Modbus 的 DCS 读取。"
                              "只支持功能码 03 / 04（读），其它功能码返回异常码 01，DCS 无法写入。随服务启停。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self.mb_status = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.mb_status, anchor="w", fg="#1f4e79").pack(fill="x", pady=2)
        cfg = self.cfg
        self.mb_enabled = tk.BooleanVar(value=bool(cfg.get("modbus_server_enabled")))
        self._row(sec, tr("启用 Modbus TCP 转发") + tr(RESTART_TAG), lambda r: tk.Checkbutton(r, variable=self.mb_enabled))
        self.mb_host = tk.StringVar(value=str(cfg.get("modbus_server_host", "127.0.0.1")))
        self._row(sec, tr("监听地址") + tr(RESTART_TAG), lambda r: tk.Entry(r, textvariable=self.mb_host),
                  tr("127.0.0.1 = 只有本机能读（默认）；0.0.0.0 或网卡地址 = 对外开放（Modbus 没有认证，务必配白名单）"))
        self.mb_port = tk.StringVar(value=str(cfg.get("modbus_server_port", 502)))
        self._row(sec, tr("端口") + tr(RESTART_TAG), lambda r: tk.Entry(r, textvariable=self.mb_port))
        self.mb_allow = tk.StringVar(value=", ".join(cfg.get("modbus_server_allow") or []))
        self._row(sec, tr("允许的来源 IP / 网段") + tr(RESTART_TAG), lambda r: tk.Entry(r, textvariable=self.mb_allow),
                  tr("逗号分隔，例如 192.168.1.20, 10.0.0.0/24；留空 = 不限制"))
        self.mb_unit = tk.StringVar(value=str(cfg.get("modbus_server_unit", 0)))
        self._row(sec, tr("站号（0 = 任意）") + tr(RESTART_TAG), lambda r: tk.Entry(r, textvariable=self.mb_unit))
        self.mb_order = tk.StringVar(value=str(cfg.get("modbus_server_order", "ABCD")))
        self._row(sec, tr("32 位数据字节序"),
                  lambda r: ttk.Combobox(r, textvariable=self.mb_order, values=list(ms.ORDERS), state="readonly", width=8))
        self._perm(tk.Button(sec, text=tr("保存转发设置"), command=self._mb_save_settings), "config").pack(anchor="w", pady=4)

        sec = self._section(inner, tr("寄存器映射表"))
        row = tk.Frame(sec)
        row.pack(fill="x")
        tk.Label(row, text=tr("每通道寄存器数")).pack(side="left")
        self.mb_block = tk.StringVar(value="50")
        tk.Entry(row, textvariable=self.mb_block, width=5).pack(side="left", padx=2)
        tk.Label(row, text=tr("每通道峰数")).pack(side="left", padx=(8, 0))
        self.mb_npeaks = tk.StringVar(value="5")
        tk.Entry(row, textvariable=self.mb_npeaks, width=4).pack(side="left", padx=2)
        self._perm(tk.Button(row, text=tr("按当前通道自动生成"), command=self._mb_auto_map), "config").pack(side="left", padx=6)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.mb_new_addr = tk.StringVar(value="")
        tk.Label(row, text=tr("地址")).pack(side="left")
        tk.Entry(row, textvariable=self.mb_new_addr, width=6).pack(side="left", padx=2)
        tk.Label(row, text=tr("通道")).pack(side="left")
        self.mb_new_chan = ttk.Combobox(row, values=[], state="readonly", width=40)
        self.mb_new_chan.pack(side="left", padx=2)
        self._mb_chan_keys = []
        tk.Label(row, text=tr("字段")).pack(side="left")
        fields = list(ms.GLOBAL_FIELDS) + [f for f, _t in ms.CHANNEL_FIELDS] + \
            ["%s_%d" % (p, n) for n in range(1, 6) for p in ("peak_rt", "peak_area", "peak_height", "peak_pct")]
        self.mb_new_field = ttk.Combobox(row, values=fields, state="readonly", width=18)
        self.mb_new_field.pack(side="left", padx=2)
        tk.Label(row, text=tr("类型")).pack(side="left")
        self.mb_new_type = ttk.Combobox(row, values=list(ms.TYPES), state="readonly", width=8)
        self.mb_new_type.current(4)
        self.mb_new_type.pack(side="left", padx=2)
        self._perm(tk.Button(row, text=tr("添加"), command=self._mb_add_row), "config").pack(side="left", padx=4)
        cols = ("addr", "maddr", "chan", "field", "type", "desc")
        self.mb_tree = ttk.Treeview(sec, columns=cols, show="headings", height=10, selectmode="extended")
        for col, title, width in (("addr", tr("0基地址"), 70), ("maddr", tr("Modbus地址"), 90), ("chan", tr("通道"), 330),
                                  ("field", tr("字段"), 130), ("type", tr("类型"), 70), ("desc", tr("说明"), 260)):
            self.mb_tree.heading(col, text=title)
            self.mb_tree.column(col, width=width, anchor="w")
        self.mb_tree.pack(fill="x", pady=2)
        bar = tk.Frame(sec)
        bar.pack(fill="x")
        self._perm(tk.Button(bar, text=tr("删除所选"), command=self._mb_remove_rows), "config").pack(side="left", padx=2)
        self._perm(tk.Button(bar, text=tr("保存映射表"), command=self._mb_save_map), "config").pack(side="left", padx=2)
        self._perm(tk.Button(bar, text=tr("导出映射表 CSV（给 DCS 组态）"), command=self._mb_export_map), "export").pack(side="left", padx=2)
        self.mb_map_note = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.mb_map_note, fg="#777", anchor="w").pack(fill="x")
        self._mb_render_map()
        self._mb_render_status()

    def _mb_render_status(self):
        fw = self.sa.ALERTS.get("modbus")
        cfg = self.cfg
        if fw is not None:
            text = tr("运行中：%s:%d，已应答 %d 次，拒绝连接 %d 次") % (fw.host, fw.port, fw.requests, fw.rejected)
        elif cfg.get("modbus_server_enabled"):
            text = tr("已启用，服务未运行或端口被占用（看运行日志）")
        else:
            text = tr("未启用")
        warns = self._ms.security_warnings(cfg.get("modbus_server_host"), cfg.get("modbus_server_allow"))
        self.mb_status.set(text + ("　⚠ " + " ".join(tr_msg(w) for w in warns) if warns else ""))

    def _mb_channel_label(self, key):
        for k, label in df.channel_options(self._recs):
            if k == key:
                return label
        return key

    def _mb_render_map(self):
        ms = self._ms
        tree = self.mb_tree
        for iid in tree.get_children():
            tree.delete(iid)
        base = 40001
        for it in self._mb_map:
            ch = tr("（全局）") if it["channel"] == ms.GLOBAL else (df.translate_label(it.get("label")) or self._mb_channel_label(it["channel"]))
            tree.insert("", "end", values=(it["address"], base + it["address"], ch, it["field"], it["type"],
                                           tr_msg(ms.describe_field(it["field"]))))
        regs = sum(ms.TYPES[i["type"]] for i in self._mb_map)
        self.mb_map_note.set(tr("共 %d 项、%d 个寄存器。地址列“Modbus地址”按 4xxxx（功能码 03）写法；用 04 读时对应 3xxxx。") % (len(self._mb_map), regs))
        opts = df.channel_options(self._recs)
        self._mb_chan_keys = [k for k, _l in opts]
        try:
            self.mb_new_chan.configure(values=[l for _k, l in opts])
        except Exception:  # noqa
            pass

    def _mb_auto_map(self):
        ms = self._ms
        opts = df.channel_options(self._recs)
        if not opts:
            return self._show(False, tr("还没有通道数据：请先启动服务或设置数据目录，等通道出现后再生成。"))
        try:
            block, npk = int(self.mb_block.get()), int(self.mb_npeaks.get())
            self._mb_map = ms.auto_map([(k, df.channel_label(r_, "zh")) for k, r_ in
                                        ((k, df.find_record(self._recs, k)) for k, _l in opts)], 0, block, npk)
        except (ValueError, ms.MapError) as e:
            return self._show(False, tr_msg(e))
        self._mb_render_map()
        self.mb_map_note.set(self.mb_map_note.get() + tr("（尚未保存）"))

    def _mb_add_row(self):
        ms = self._ms
        field = self.mb_new_field.get()
        idx = self.mb_new_chan.current()
        chan = ms.GLOBAL if field in ms.GLOBAL_FIELDS else (self._mb_chan_keys[idx] if 0 <= idx < len(self._mb_chan_keys) else "")
        item = {"address": self.mb_new_addr.get().strip(), "channel": chan, "field": field,
                "type": self.mb_new_type.get() or "uint16"}
        if chan and chan != ms.GLOBAL:
            item["label"] = df.channel_label(df.find_record(self._recs, chan) or {}, "zh")
        try:
            self._mb_map = ms.normalize_map(self._mb_map + [item])
        except ms.MapError as e:
            return self._show(False, tr_msg(e))
        self._mb_render_map()

    def _mb_remove_rows(self):
        sel = self.mb_tree.selection()
        order = self.mb_tree.get_children()
        drop = {order.index(i) for i in sel if i in order}
        self._mb_map = [it for i, it in enumerate(self._mb_map) if i not in drop]
        self._mb_render_map()

    def _mb_save_map(self):
        ms = self._ms
        try:
            before, after = config_tools.set_value(self.cfg["_cfg_path"], "modbus_server_map", self._mb_map,
                                                   ms.normalize_map, self.logic.srv._validate)
        except config_tools.ConfigToolError as e:
            return self._show(False, tr_msg(e))
        self.cfg["modbus_server_map"] = after
        self._audit("save", "modbus_server_map", {"items": len(before or [])}, {"items": len(after)})
        self._show(True, tr("映射表已保存（%d 项）。服务运行中下一轮扫描起按新映射输出。") % len(after))

    def _mb_save_settings(self):
        ms = self._ms
        upd = {"modbus_server_enabled": bool(self.mb_enabled.get()), "modbus_server_host": self.mb_host.get().strip(),
               "modbus_server_port": self.mb_port.get().strip(), "modbus_server_allow": self.mb_allow.get(),
               "modbus_server_unit": self.mb_unit.get().strip(), "modbus_server_order": self.mb_order.get()}
        try:
            allow = ms.normalize_allow(upd["modbus_server_allow"])
        except ms.MapError as e:
            return self._show(False, tr_msg(e))
        warns = ms.security_warnings(upd["modbus_server_host"], allow)
        if upd["modbus_server_enabled"] and warns and not self.mb.askyesno(
                tr("安全提示"), "\n".join(tr_msg(w) for w in warns) + "\n\n" + tr("确定要对外开放吗？"), parent=self.root):
            return
        upd["modbus_server_allow"] = allow
        try:
            changed = config_tools.update_settings(self.cfg["_cfg_path"], upd, self.logic.srv._validate)
        except config_tools.ConfigToolError as e:
            return self._show(False, tr_msg(e))
        for k in changed:
            self.cfg[k] = config_tools.read_raw(self.cfg["_cfg_path"]).get(k)
        self._audit("save", ",".join(changed), None, {k: upd[k] for k in changed})
        self._mb_render_status()
        if changed and self.svc.running() and self.mb.askyesno(
                tr("需要重启服务"), tr("Modbus 转发的启用 / 地址 / 端口 / 白名单需要重启服务才生效。现在重启吗？"), parent=self.root):
            self._restart_service()
        elif not changed:
            self._show(True, tr("没有改动。"))

    def _mb_export_map(self):
        ms = self._ms
        if not self._mb_map:
            return self._show(False, tr("映射表是空的：先“按当前通道自动生成”或添加。"))
        root = default_export_dir(self.cfg, self.cfg["_base_dir"])
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "Modbus映射表_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        hdr = [tr(h) for h in ("0基地址", "Modbus地址", "寄存器数", "通道", "字段", "类型", "字节序", "说明")]
        ms.export_map_csv(self._mb_map, path, self.cfg.get("modbus_server_order", "ABCD"), 3, hdr)
        self._audit("export", "modbus_map", detail=path)
        self._show(True, tr("映射表已导出：%s") % path)

    # ---- 运行报表 ----
    def _build_reports_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        import reports
        self._rp = reports
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("运行报表"))
        self.tab_reports = tab
        inner = _scrollable(tab, tk, ttk)
        sec = self._section(inner, tr("生成报表"))
        tk.Label(sec, text=tr("按日或按班统计：通道状态、周期数、告警次数与持续时长、服务在线时长、客户端连接数峰值。"
                              "数据来自服务运行期间记录的历史库。导出 .xlsx（Excel / WPS 直接打开）和 .html（浏览器打印成 PDF）。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=4)
        self.rp_kind = tk.StringVar(value="day")
        tk.Radiobutton(row, text=tr("按日"), value="day", variable=self.rp_kind).pack(side="left")
        tk.Radiobutton(row, text=tr("按班"), value="shift", variable=self.rp_kind).pack(side="left")
        tk.Label(row, text=tr("  日期")).pack(side="left")
        self.rp_date = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        tk.Entry(row, textvariable=self.rp_date, width=11).pack(side="left", padx=2)
        tk.Label(row, text=tr("  班次")).pack(side="left")
        self.rp_shift = ttk.Combobox(row, values=[s["name"] for s in self.cfg.get("desktop_report_shifts") or []],
                                     state="readonly", width=10)
        try:
            self.rp_shift.current(0)
        except Exception:  # noqa
            pass
        self.rp_shift.pack(side="left", padx=2)
        self._perm(tk.Button(row, text=tr("生成报表"), command=self._rp_generate), "report").pack(side="left", padx=8)
        tk.Button(row, text=tr("打开报表文件夹"), command=lambda: self._open_path(self._rp_dir(True))).pack(side="left")
        self.rp_out = self._scroll_text(sec, 10)

        sec = self._section(inner, tr("自动生成（窗口运行期间）"))
        modes = [m for m, _t in REPORT_MODES]
        self.rp_auto = ttk.Combobox(sec, values=[tr(t) for _m, t in REPORT_MODES], state="readonly", width=16)
        cur = self.cfg.get("desktop_report_auto", "off")
        self.rp_auto.current(modes.index(cur) if cur in modes else 0)
        self._row(sec, tr("自动生成"), lambda r: self.rp_auto,
                  tr("由本窗口里的定时器完成（每分钟检查一次，上一个日 / 班结束后生成），不建 Windows 计划任务；窗口关闭期间不会生成。"))
        self.rp_shifts = tk.StringVar(value=reports.shifts_text(self.cfg.get("desktop_report_shifts") or reports.DEFAULT_SHIFTS))
        self._row(sec, tr("班次（名称=开始时间）"), lambda r: tk.Entry(r, textvariable=self.rp_shifts),
                  tr("例如 白班=08:00, 夜班=20:00；班次属于它开始的那一天"))
        self.rp_dir_var = tk.StringVar(value=str(self.cfg.get("desktop_report_dir") or "运行报表"))
        self._row(sec, tr("报表目录"), lambda r: tk.Entry(r, textvariable=self.rp_dir_var))
        self._perm(tk.Button(sec, text=tr("保存报表设置"), command=self._rp_save_settings), "config").pack(anchor="w", pady=4)

    def _scroll_text(self, parent, height):
        from tkinter import scrolledtext
        w = scrolledtext.ScrolledText(parent, height=height, state="disabled", wrap="word")
        w.pack(fill="both", expand=True, pady=4)
        return w

    def _rp_dir(self, create=False):
        d = str(self.cfg.get("desktop_report_dir") or "运行报表")
        d = d if os.path.isabs(d) else os.path.join(self.cfg["_base_dir"], d)
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    def _rp_period(self):
        rp = self._rp
        day = datetime.strptime(self.rp_date.get().strip(), "%Y-%m-%d").date()
        if self.rp_kind.get() == "shift":
            name = self.rp_shift.get()
            for p in rp.shift_periods(day, self.cfg.get("desktop_report_shifts")):
                if p["shift"] == name:
                    return p
            raise ValueError(tr("请选择班次"))
        return rp.day_period(day)

    def _rp_store(self):
        return self._hist_mod.HistoryStore(self._hist_mod.history_path(self.cfg), readonly=True)

    def _rp_generate(self, period=None, auto=False):
        try:
            period = period or self._rp_period()
        except ValueError as e:
            return self._show(False, tr("日期格式不对（写成 2026-11-26）：%s") % tr_msg(e))
        out_dir = self._rp_dir(True)
        labels = {k: l for k, l in df.channel_options(self._recs)}

        def work():
            st = self._rp_store()
            try:
                return self._rp.generate(st, period, out_dir, ("xlsx", "html"), tr=tr, labels=labels)
            finally:
                st.close()

        def done(res, err):
            if err is not None:
                if not auto:
                    self._show(False, tr_msg(err))
                self.sa.get_log().error("运行报表生成失败（%s）：%s", period["name"], err)
                return
            files, rep = res
            if self._auto_rep is not None:
                self._auto_rep.mark(period)
            self._audit("report", period["name"], detail=", ".join(os.path.basename(f) for f in files))
            self.sa.get_log().info("运行报表已生成：%s", "、".join(files))
            lines = [tr("报表：%s（%s ～ %s）") % (period["name"], rep["start"], rep["end"]),
                     tr("服务在线 %s（%.1f%%），启动 %d 次；周期 %d 个；告警 %d 次，累计 %s；客户端连接数峰值 %d") % (
                         self._rp.fmt_dur(rep["uptime_seconds"]), rep["uptime_pct"], rep["service_starts"],
                         rep["cycles_total"], rep["alert_count"], self._rp.fmt_dur(rep["alert_seconds"]),
                         rep["clients_peak"])]
            for r in rep["channels"]:
                lines.append("  %s：%s %d，%s %d，%s" % (df.translate_label(r["label"]), tr("周期"), r["cycles"],
                                                       tr("告警"), r["alerts"], tr(r["status"])))
            lines += [tr("文件：")] + ["  " + f for f in files]
            self._set_text(self.rp_out, "\n".join(lines))
        self._bg(work, done)

    def _rp_save_settings(self):
        rp = self._rp
        modes = [m for m, _t in REPORT_MODES]
        idx = self.rp_auto.current()
        mode = modes[idx] if 0 <= idx < len(modes) else "off"
        try:
            shifts = rp.parse_shifts_text(self.rp_shifts.get())
            before, after = config_tools.set_value(self.cfg["_cfg_path"], "desktop_report_shifts", shifts,
                                                   rp.normalize_shifts, self.logic.srv._validate)
            config_tools.update_settings(self.cfg["_cfg_path"], {"desktop_report_auto": mode,
                                                                 "desktop_report_dir": self.rp_dir_var.get().strip()},
                                         self.logic.srv._validate)
        except (ValueError, config_tools.ConfigToolError) as e:
            return self._show(False, tr_msg(e))
        self.cfg.update(desktop_report_auto=mode, desktop_report_shifts=after,
                        desktop_report_dir=self.rp_dir_var.get().strip())
        self._audit("save", "desktop_report_*", None, {"mode": mode, "shifts": rp.shifts_text(after)})
        self._auto_rep = None
        self.rp_shift.configure(values=[s["name"] for s in after])
        self._show(True, tr("报表设置已保存，立即生效。"))

    def _poll_reports(self):
        if self._closed:
            return
        try:
            mode = self.cfg.get("desktop_report_auto", "off")
            if mode != "off":
                if self._auto_rep is None or self._auto_rep.mode != mode:
                    self._auto_rep = self._rp.AutoReporter(mode, self.cfg.get("desktop_report_shifts"), self._rp_dir())
                for p in self._auto_rep.due():
                    self._auto_rep.mark(p)
                    self._unguarded("_rp_generate")(p, auto=True)     # 定时器自动生成：不受当前登录角色限制
        except Exception as e:  # noqa
            self.sa.get_log().error("自动生成报表检查出错：%s", e)
        self.root.after(60000, self._poll_reports)

    # ---- 备份与审计 ----
    def _build_backup_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("备份与审计"))
        self.tab_backup = tab
        inner = _scrollable(tab, tk, ttk)
        sec = self._section(inner, tr("导出配置备份"))
        tk.Label(sec, text=tr("打包 config.json（含账号密码哈希）、certs 下的 OPC UA 与网页证书和私钥、信任的客户端证书，可选日志。"
                              "备份里有私钥，强烈建议设置密码加密（AES-256-GCM）。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.bk_encrypt = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text=tr("用密码加密"), variable=self.bk_encrypt).pack(side="left")
        self.bk_pw, self.bk_pw2 = tk.StringVar(), tk.StringVar()
        tk.Label(row, text=tr("密码")).pack(side="left", padx=(8, 2))
        tk.Entry(row, textvariable=self.bk_pw, show="*", width=16).pack(side="left")
        tk.Label(row, text=tr("再输一次")).pack(side="left", padx=(8, 2))
        tk.Entry(row, textvariable=self.bk_pw2, show="*", width=16).pack(side="left")
        self.bk_logs = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text=tr("包含日志"), variable=self.bk_logs).pack(side="left", padx=8)
        self._perm(tk.Button(row, text=tr("导出备份…"), command=self._bk_export), "backup").pack(side="left", padx=8)

        sec = self._section(inner, tr("从备份还原"))
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self._perm(tk.Button(row, text=tr("选择备份文件并预览…"), command=self._bk_pick_restore), "restore").pack(side="left")
        self.bk_restore_logs = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text=tr("同时还原日志（放到 logs\\restored_时间）"), variable=self.bk_restore_logs).pack(side="left", padx=8)
        self.bk_preview = self._scroll_text(sec, 9)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.btn_bk_restore = self._perm(tk.Button(row, text=tr("还原"), command=lambda: self._bk_restore(False)), "restore")
        self.btn_bk_restore.pack(side="left", padx=2)
        self.btn_bk_restart = self._perm(tk.Button(row, text=tr("还原并重启服务"), command=lambda: self._bk_restore(True)), "restore")
        self.btn_bk_restart.pack(side="left", padx=2)
        tk.Label(sec, text=tr("还原前会自动把当前配置另存到 backups\\自动备份_*.zip（保留最近 N 个，N = backup_keep）。"
                              "校验不通过或写入失败会自动回滚。"), fg="#777", anchor="w", justify="left",
                 wraplength=860).pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.bk_auto_lb = tk.Listbox(row, height=4, exportselection=False, width=70)
        self.bk_auto_lb.pack(side="left", fill="x", expand=True)
        self._perm(tk.Button(row, text=tr("预览所选自动备份"), command=self._bk_restore_auto), "restore").pack(side="left", padx=4)
        self._bk_render_auto()

        sec = self._section(inner, tr("账号与角色"))
        tk.Label(sec, text=tr("账号与 OPC UA 登录账号是同一套（在“系统设置 → 登录账号”添加）。没有指定角色的账号按操作员。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        cols = ("user", "role")
        self.ur_tree = ttk.Treeview(sec, columns=cols, show="headings", height=5)
        self.ur_tree.heading("user", text=tr("用户名"))
        self.ur_tree.heading("role", text=tr("角色"))
        self.ur_tree.column("user", width=200)
        self.ur_tree.column("role", width=360)
        self.ur_tree.pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.ur_role = ttk.Combobox(row, values=[tr(t) for _r, t in ROLE_OPTIONS], state="readonly", width=30)
        self.ur_role.current(0)
        self.ur_role.pack(side="left")
        self._perm(tk.Button(row, text=tr("设置所选账号的角色"), command=self._ur_set_role), "users").pack(side="left", padx=4)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.ur_auth = tk.BooleanVar(value=bool(self.cfg.get("desktop_auth", True)))
        tk.Checkbutton(row, text=tr("桌面窗口需要登录（推荐）"), variable=self.ur_auth).pack(side="left")
        tk.Label(row, text=tr("  空闲自动锁定（分钟，0 = 不锁定）")).pack(side="left")
        self.ur_idle = tk.StringVar(value=str(self.cfg.get("desktop_idle_lock_minutes", 15)))
        tk.Entry(row, textvariable=self.ur_idle, width=5).pack(side="left", padx=2)
        self._perm(tk.Button(row, text=tr("保存"), command=self._ur_save_auth), "users").pack(side="left", padx=6)
        self._ur_render()

        sec = self._section(inner, tr("操作审计日志"))
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.au_user, self.au_text = tk.StringVar(), tk.StringVar()
        self.au_from, self.au_to = tk.StringVar(), tk.StringVar()
        for label, var, w in ((tr("用户"), self.au_user, 10), (tr("包含文字"), self.au_text, 14),
                              (tr("从"), self.au_from, 11), (tr("到"), self.au_to, 11)):
            tk.Label(row, text=label).pack(side="left", padx=(6, 2))
            tk.Entry(row, textvariable=var, width=w).pack(side="left")
        tk.Button(row, text=tr("查询"), command=self._au_query).pack(side="left", padx=6)
        tk.Button(row, text=tr("校验防篡改"), command=self._au_verify).pack(side="left", padx=2)
        self._perm(tk.Button(row, text=tr("导出 CSV"), command=self._au_export), "audit_export").pack(side="left", padx=2)
        cols = ("time", "user", "source", "action", "target", "ok")
        self.au_tree = ttk.Treeview(sec, columns=cols, show="headings", height=8)
        for col, title, width in (("time", tr("时间"), 140), ("user", tr("用户"), 110), ("source", tr("来源"), 70),
                                  ("action", tr("操作"), 120), ("target", tr("对象 / 修改"), 420), ("ok", tr("结果"), 50)):
            self.au_tree.heading(col, text=title)
            self.au_tree.column(col, width=width, anchor="w")
        self.au_tree.pack(fill="x")
        self.au_note = tk.StringVar(value=tr("审计日志：logs\\audit.log（每行带哈希链，删改任何一行都能被“校验防篡改”发现）"))
        tk.Label(sec, textvariable=self.au_note, fg="#777", anchor="w").pack(fill="x")

    # -- 备份 --
    def _bk_render_auto(self):
        import backup_tools
        self._bk_autos = list(reversed(backup_tools.list_auto_backups(self.cfg["_base_dir"])))
        self.bk_auto_lb.delete(0, "end")
        for p in self._bk_autos:
            self.bk_auto_lb.insert("end", os.path.basename(p))

    def _bk_export(self, path=None):
        import backup_tools
        pw = None
        if self.bk_encrypt.get():
            pw = self.bk_pw.get()
            if len(pw) < 8:
                return self._show(False, tr("备份密码至少 8 个字符。"))
            if pw != self.bk_pw2.get():
                return self._show(False, tr("两次输入的备份密码不一致。"))
        base, cfgp = self.cfg["_base_dir"], self.cfg["_cfg_path"]
        try:
            plan = backup_tools.plan_export(base, cfgp, bool(self.bk_logs.get()))
        except backup_tools.BackupError as e:
            return self._show(False, tr_msg(e))
        if plan["private_keys"] and not pw and not self.mb.askyesno(
                tr("安全警告"), tr("备份里包含证书私钥（%s），而你没有设置密码：\n拿到这个文件的人可以冒充本服务器。\n\n"
                                  "确定不加密导出吗？（建议勾选“用密码加密”）") % "、".join(plan["private_keys"]),
                icon="warning", parent=self.root):
            return
        for w in plan["warnings"]:
            self.mb.showwarning(tr("注意"), tr_msg(w), parent=self.root)
        if path is None:
            path = self.fd.asksaveasfilename(title=tr("保存备份包"), defaultextension=".zip",
                                             initialfile=backup_tools.default_backup_name(),
                                             filetypes=[(tr("备份包"), "*.zip")], parent=self.root)
        if not path:
            return

        def done(r, err):
            if err is not None:
                self._audit("backup", path, ok=False, detail=err)
                return self._show(False, tr_msg(err))
            self._audit("backup", path, detail="%d files, %s" % (r["files"], "encrypted" if r["encrypted"] else "plain"))
            self.bk_pw.set("")
            self.bk_pw2.set("")
            self._show(True, tr("已导出备份：%s\n%d 个文件，%s。") % (r["path"], r["files"],
                                                              tr("已加密") if r["encrypted"] else tr("未加密")))
        self._bg(lambda: backup_tools.export_backup(path, base, cfgp, pw, bool(self.bk_logs.get())), done)

    def _bk_pick_restore(self, path=None):
        if path is None:
            path = self.fd.askopenfilename(title=tr("选择备份包"), filetypes=[(tr("备份包"), "*.zip")], parent=self.root)
        if not path:
            return
        self._bk_inspect(path)

    def _bk_restore_auto(self):
        sel = self.bk_auto_lb.curselection()
        if not sel:
            return self._show(False, tr("请先选中一个自动备份。"))
        self._bk_inspect(self._bk_autos[sel[0]])

    def _bk_inspect(self, path, password=None):
        import backup_tools
        base, cfgp = self.cfg["_base_dir"], self.cfg["_cfg_path"]
        try:
            info = backup_tools.inspect_backup(path, base, cfgp, password, bool(self.bk_restore_logs.get()))
        except backup_tools.PasswordRequired:
            pw = self.sd.askstring(tr("备份包已加密"), tr("请输入导出时设置的密码："), show="*", parent=self.root)
            if not pw:
                return None
            return self._bk_inspect(path, pw)
        except backup_tools.BackupError as e:
            self._bk_info = None
            self._set_text(self.bk_preview, tr("不能还原：%s") % tr_msg(e))
            self._audit("restore_check", path, ok=False, detail=e)
            return None
        self._bk_info, self._bk_path, self._bk_password = info, path, password
        self._set_text(self.bk_preview, backup_tools.summary_text(info, tr))
        return info

    def _bk_restore(self, restart=False):
        import backup_tools
        if not self._bk_info:
            return self._show(False, tr("请先“选择备份文件并预览”。"))
        n = len(self._bk_info["actions"])
        if not self.mb.askyesno(tr("确认还原"), tr("将覆盖 / 新增 / 删除 %d 个文件（见预览）。还原前会自动备份当前配置。\n确定还原吗？") % n,
                                icon="warning", parent=self.root):
            return
        base, cfgp = self.cfg["_base_dir"], self.cfg["_cfg_path"]
        validate = backup_tools.make_validator(self.sa, base)
        keep = self.cfg.get("backup_keep", 10)
        path, pw, logs = self._bk_path, self._bk_password, bool(self.bk_restore_logs.get())

        def done(r, err):
            if err is not None:
                self._audit("restore", path, ok=False, detail=err)
                return self._show(False, tr_msg(err))
            self._audit("restore", path, None, {"files": len(r["written"]), "removed": len(r["removed"]),
                                                "smtp_cleared": r["smtp_cleared"]})
            self._bk_info = None
            self._bk_render_auto()
            msg = tr("已还原 %d 个文件。还原前的配置已备份到：%s") % (len(r["written"]), r["auto_backup"] or "-")
            if r["smtp_cleared"]:
                msg += "\n" + tr("备份来自另一台电脑：SMTP 密码无法在本机解密，已清空，请在“告警与通知”里重新填写。")
            try:
                self.cfg = self._fresh_cfg()
            except Exception:  # noqa
                pass
            self._reload()
            if restart:
                self._show(True, msg + "\n" + tr("现在重启服务……"))
                self._unguarded("_restart_service")() if self.svc.running() else self._unguarded("_start_service")()
            else:
                self._show(True, msg + "\n" + tr("证书、端口等设置要重启服务后才生效（可点“重启服务”）。"))
        self._bg(lambda: backup_tools.restore_backup(path, base, cfgp, pw, logs, validate=validate, keep=keep), done)

    # -- 角色 --
    def _ur_render(self):
        import audit
        raw = config_tools.read_raw(self.cfg["_cfg_path"])
        roles = raw.get("user_roles") or {}
        tree = self.ur_tree
        for iid in tree.get_children():
            tree.delete(iid)
        self._ur_users = {}
        for name in sorted(raw.get("users") or {}):
            r = roles.get(name, "operator")
            iid = tree.insert("", "end", values=(name, tr(dict(ROLE_OPTIONS)[r])))
            self._ur_users[iid] = name

    def _ur_set_role(self):
        import audit
        sel = [self._ur_users[i] for i in self.ur_tree.selection() if i in self._ur_users]
        if not sel:
            return self._show(False, tr("请先选中账号。"))
        idx = self.ur_role.current()
        role = [r for r, _t in ROLE_OPTIONS][idx if 0 <= idx < len(ROLE_OPTIONS) else 0]
        raw = config_tools.read_raw(self.cfg["_cfg_path"])
        for name in sel:
            before = (raw.get("user_roles") or {}).get(name, "operator")
            try:
                audit.set_role(self.cfg["_cfg_path"], name, role, config_tools)
            except (audit.AuthError, ValueError) as e:
                return self._show(False, tr_msg(e))
            self._audit("set_role", name, {"role": before}, {"role": role})
        if self.session.user in sel:
            self.session.role = role
        self._ur_render()
        self._render_user()

    def _ur_save_auth(self):
        import audit
        want = bool(self.ur_auth.get())
        if want and not audit.has_admin(config_tools.read_raw(self.cfg["_cfg_path"])):
            return self._show(False, tr("还没有管理员账号：开启登录前请先“创建管理员…”，否则没人能修改设置。"))
        try:
            changed = config_tools.update_settings(self.cfg["_cfg_path"], {"desktop_auth": want,
                                                                           "desktop_idle_lock_minutes": self.ur_idle.get()},
                                                   self.logic.srv._validate)
        except config_tools.ConfigToolError as e:
            return self._show(False, tr_msg(e))
        raw = config_tools.read_raw(self.cfg["_cfg_path"])
        self.cfg.update(desktop_auth=want, desktop_idle_lock_minutes=raw.get("desktop_idle_lock_minutes", 15))
        self.session.auth_enabled = want
        self.session.idle_minutes = int(self.cfg["desktop_idle_lock_minutes"])
        self._audit("save", ",".join(changed) or "desktop_auth", None, {"desktop_auth": want,
                                                                       "idle": self.session.idle_minutes})
        self._render_user()
        self._show(True, tr("已保存，立即生效。"))

    # -- 审计 --
    def _au_query(self):
        f, t = self.au_from.get().strip(), self.au_to.get().strip()
        rows = self.audit_log.query(start=f or None, end=(t + " 23:59:59") if t and len(t) == 10 else (t or None),
                                    user=self.au_user.get().strip() or None, text=self.au_text.get().strip() or None)
        self._au_rows = rows
        tree = self.au_tree
        for iid in tree.get_children():
            tree.delete(iid)
        for r in rows[-1000:][::-1]:
            chg = ""
            if r.get("after") not in (None, "", {}):
                chg = " → %s" % json_short(r.get("after"))
            tree.insert("", "end", values=(r.get("time"), r.get("user"), r.get("source"), r.get("action"),
                                           (str(r.get("target") or "") + chg)[:300], "OK" if r.get("ok") else "FAIL"))
        self.au_note.set(tr("共 %d 条（显示最近 1000 条）") % len(rows))

    def _au_verify(self):
        ok, probs = self.audit_log.verify_chain()
        if ok:
            self._show(True, tr("审计日志完整：哈希链校验通过（%d 条）。") % len(self.audit_log.read()))
        else:
            self._show(False, tr("审计日志被改动过！") + "\n" + "\n".join(
                tr("第 %d 行：%s") % (ln, tr(why)) for ln, why in probs[:20]))

    def _au_export(self):
        rows = self._au_rows or self.audit_log.query()
        root = default_export_dir(self.cfg, self.cfg["_base_dir"])
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "审计日志_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        hdr = [tr(h) for h in ("时间", "用户", "来源", "操作", "对象", "修改前", "修改后", "结果", "说明", "哈希")]
        self.audit_log.export_csv(rows, path, hdr)
        self._audit("audit_export", path, detail=len(rows))
        self._show(True, tr("已导出 %d 条：%s") % (len(rows), path))


def json_short(v, n=160):
    import json
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:  # noqa
        s = str(v)
    return s if len(s) <= n else s[:n] + "…"



class DesktopWindow(V31Mixin):
    """桌面主窗口：服务控制栏 + 登录栏 + 十四个标签页 + 日志窗（v3.1 起继承 V31Mixin）。
    start_service=True 时窗口打开后自动启动 OPC UA 服务；服务随窗口关闭而停止。
    minimized=True 时窗口打开后最小化到任务栏（v3.0“启动时最小化”）。"""

    def __init__(self, cfg, start_service=True, minimized=False):
        import queue
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

        import server_app as sa

        self.tk, self.ttk, self.mb, self.sd, self.fd = tk, ttk, messagebox, simpledialog, filedialog
        self.sa = sa
        self.cfg = cfg
        self.start_service = start_service
        self.svc = ServiceController(sa)
        self.logic = DesktopLogic(cfg["_cfg_path"], cfg["_base_dir"], sa, log=sa.get_log())
        self._q = queue.Queue()
        self._orig = {}
        self._vars = {}          # 字段 -> tk Variable（系统/告警两页的配置表单）
        self._nt_vars = {}       # 连接测试当前表单
        self._nt_kind = "ping"
        self._nt_ports = []
        self._pr_list = []
        self._pr_editing = ""
        self._mt_list = []
        self._dc_shown_time = ""
        self._closed = False
        self._busy = ""          # 正在进行的服务操作（停止中 / 自检中），期间禁用服务按钮
        self._was_up = False
        # v3.0：谱图快照 / 通道表 / 客户端连接 / 告警桌面提醒
        self.records = df.RecordSource(sa)
        self.ch_tracker = df.ChannelTracker()
        self.conn_tracker = df.ConnTracker()
        self.notify_tracker = df.AlertNotifyTracker()
        self._recs = []
        self._rec_origin = "none"
        self._rec_loading = False
        self._last_alerts = {}
        self._pv_keys = []        # 谱图预览下拉框：与显示名一一对应的通道键
        self._pv_key = ""
        self._pv_sig = None
        self._chd_keys = {}       # 通道表行 iid -> 通道键
        self._qvars = {}          # 常用设置表单：键 -> (Variable, 控件, 类型)
        self._tray = None
        self._popups = []
        self.minimized = minimized
        self._v31_pre()           # v3.1：语言、登录会话、权限检查（必须在建控件之前）

        sa._hide_own_console()
        root = tk.Tk()
        self.root = root
        root.title(tr("色谱提取 OPC UA 服务 v%s") % sa.__version__)
        sa._set_window_icon(root)
        root.geometry("1180x800")
        try:
            ttk.Style().configure("TNotebook.Tab", padding=(6, 2))
        except Exception:  # noqa
            pass
        self._build_menu(root)
        root.minsize(900, 620)

        # 顶部：服务状态 + 服务控制按钮（v2.9：桌面里直接启停服务）
        self.status = tk.StringVar(value=tr("服务未运行"))
        self.status_lbl = tk.Label(root, textvariable=self.status, anchor="w", fg="#57606a",
                                   font=("Microsoft YaHei", 11, "bold"))
        self.status_lbl.pack(fill="x", padx=10, pady=(8, 2))
        ctl = tk.Frame(root)
        ctl.pack(fill="x", padx=10, pady=(0, 2))
        self.btn_start = tk.Button(ctl, text=tr("▶ 启动服务"), width=11, command=self._start_service)
        self.btn_start.pack(side="left", padx=(0, 4))
        self.btn_stop = tk.Button(ctl, text=tr("■ 停止服务"), width=11, command=self._stop_service)
        self.btn_stop.pack(side="left", padx=4)
        self.btn_restart = tk.Button(ctl, text=tr("重启服务"), command=self._restart_service)
        self.btn_restart.pack(side="left", padx=4)
        self.btn_selftest = tk.Button(ctl, text=tr("自检"), command=self._selftest)
        self.btn_selftest.pack(side="left", padx=(16, 4))
        tk.Button(ctl, text=tr("打开网页设置页"), command=self._open_web).pack(side="left", padx=4)
        tk.Button(ctl, text=tr("打开日志文件夹"), command=self._open_logs).pack(side="left", padx=4)

        self.sec_var = tk.StringVar(value=sa.security_summary(cfg))
        bar = tk.Frame(root)
        bar.pack(fill="x", padx=10)
        tk.Label(bar, textvariable=self.sec_var, anchor="w").pack(side="left")
        for text, cmd in ((tr("安全设置…"), self._open_security), (tr("485配型诊断"), self._gui_rtu485),
                          (tr("发送测试数据"), lambda: self._gui_transmission(True)),
                          (tr("数据传输检测"), lambda: self._gui_transmission(False)),
                          (tr("智能诊断"), self._gui_diagnose)):
            tk.Button(bar, text=text, command=cmd).pack(side="right", padx=4)
        self._build_login_bar(root)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=10, pady=6)
        self.nb = nb
        self._build_status_tab(nb)
        self._build_channels_tab(nb)      # v3.0
        self._build_preview_tab(nb)       # v3.0（v3.1 加峰表）
        self._build_history_tab(nb)       # v3.1
        self._build_clients_tab(nb)       # v3.0
        self._build_data_tab(nb)
        self._build_reports_tab(nb)       # v3.1
        self._build_alerts_tab(nb)
        self._build_tools_tab(nb)
        self._build_comm_tab(nb)          # v3.0 通讯诊断
        self._build_modbus_tab(nb)        # v3.1
        self._build_quick_tab(nb)         # v3.0（v3.1 加界面语言）
        self._build_system_tab(nb)
        self._build_backup_tab(nb)        # v3.1 备份与还原 / 账号与角色 / 审计

        # 底部保存栏（与网页版 savebar 对应）
        savebar = tk.Frame(root)
        savebar.pack(fill="x", padx=10, pady=(0, 4))
        self.dirty_txt = tk.StringVar(value=tr("没有未保存的修改"))
        tk.Label(savebar, textvariable=self.dirty_txt, anchor="w").pack(side="left")
        tk.Button(savebar, text=tr("放弃修改"), command=self._reload).pack(side="right", padx=4)
        tk.Button(savebar, text=tr("保存设置"), command=self._save).pack(side="right", padx=4)

        # 日志窗（运行日志实时显示；完整日志在 logs\server.log）
        logbar = tk.Frame(root)
        logbar.pack(fill="x", padx=10)
        tk.Label(logbar, text=tr("运行日志"), anchor="w", fg="#555").pack(side="left")
        tk.Button(logbar, text=tr("清空显示"), command=self._clear_log).pack(side="right", padx=2)
        tk.Button(logbar, text=tr("打开日志文件"), command=self._open_log_file).pack(side="right", padx=2)
        self.box = scrolledtext.ScrolledText(root, height=8, state="disabled")
        self.box.pack(fill="both", padx=10, pady=(2, 8))

        self._setup_logging()
        self._reload()
        self._pump_log()
        self._poll_queue()
        self._refresh_sec()
        self._poll_alerts()
        self._poll_probes()
        self._poll_stability()
        self._poll_env()
        self._poll_service()
        self._poll_records()
        self._poll_sessions()
        self._poll_idle()
        self._poll_reports()
        self._render_user()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        if start_service:      # 打开窗口自动启动服务是程序行为，不需要登录
            root.after(200, self._unguarded("_start_service"))
        if minimized:
            root.after(300, self._minimize)

    # ---- 日志 ----
    def _setup_logging(self):
        import logging

        sa = self.sa

        class QHandler(logging.Handler):
            def emit(_self, record):
                sa.log_q.put(_self.format(record))

        sa.setup_logging(self.cfg, self.cfg["_base_dir"])
        logging.getLogger().addHandler(QHandler())

    def _log_dir(self):
        lf = self.cfg.get("log_file") or "logs/server.log"
        p = lf if os.path.isabs(lf) else os.path.join(self.cfg["_base_dir"], lf)
        return os.path.dirname(p), p

    def _open_path(self, path):
        try:
            os.startfile(path)          # Windows
        except Exception:  # noqa
            self._show(True, tr("位置：%s") % path)

    def _open_logs(self):
        d, _f = self._log_dir()
        os.makedirs(d, exist_ok=True)
        self._open_path(d)

    def _open_log_file(self):
        _d, f = self._log_dir()
        if not os.path.isfile(f):
            return self._show(False, tr("日志文件还没有生成：%s") % f)
        self._open_path(f)

    def _clear_log(self):
        self.box.configure(state="normal")
        self.box.delete("1.0", "end")
        self.box.configure(state="disabled")

    # ---- 服务控制（v2.9）----
    def _fresh_cfg(self):
        """按磁盘上已保存的 config.json 重新读取并校验（启动/重启服务时用，改过的设置才会生效）。"""
        sa = self.sa
        path, base = self.cfg["_cfg_path"], self.cfg["_base_dir"]
        cfg = sa.load_config(path)
        cfg["_base_dir"], cfg["_cfg_path"] = base, path
        sa.normalize_config(cfg, base)
        return cfg

    def _set_service_buttons(self):
        running = self.svc.running()
        busy = bool(self._busy)
        allowed = self.session.can("service") if hasattr(self, "session") else True
        self.btn_start.configure(state="disabled" if (running or busy or not allowed) else "normal")
        self.btn_stop.configure(state="normal" if (running and not busy and allowed) else "disabled")
        self.btn_restart.configure(state="normal" if (running and not busy and allowed) else "disabled")
        self.btn_selftest.configure(state="disabled" if busy else "normal")

    def _poll_service(self):
        if self._closed:
            return
        up = self.svc.is_up()
        if self._busy:
            text, color = self._busy, "#9a6700"
        else:
            text, color = service_status_text(self.svc.running(), up, self.svc.error,
                                              (self.svc.cfg or self.cfg).get("endpoint"))
        self.status.set(text)
        self.status_lbl.configure(fg=color)
        self._set_service_buttons()
        if up and not self._was_up:      # 刚启动成功：立即刷新告警/环境检测/通道数据
            self._poll_env()
            self._bg(self.logic.alerts, lambda a, err: err is None and self._render_alerts(a))
            self._refresh_records()
        elif self._was_up and not up:    # 刚停止：通道表改为直接读数据目录
            self._refresh_records(force=True)
        self._was_up = up
        self.root.after(1000, self._poll_service)

    def _start_service(self):
        if self.svc.running() or self._busy or self._closed:
            return
        try:
            cfg = self._fresh_cfg()
        except Exception as e:  # noqa  配置有误：不启动，告诉用户哪里错
            self.svc.error = tr("配置有误")
            return self._show(False, tr("配置有误，服务没有启动：\n%s\n\n请在“系统设置”里改正并保存后再启动。") % e)
        self.cfg = cfg
        for w in self.sa.watch_dir_warnings(cfg):
            self.sa.get_log().warning("数据目录提示：%s", w)
        self.sa.get_log().info("正在启动 OPC UA 服务：%s", cfg["endpoint"])
        self._audit("service_start", cfg["endpoint"])
        self.svc.start(cfg)
        self._set_service_buttons()

    def _stop_service(self, then=None, ask=True):
        if not self.svc.running() or self._busy:
            return
        if ask and not self.mb.askyesno(tr("请确认"), tr("停止服务后 DCS/SCADA 将读不到数据。确定停止吗？"), parent=self.root):
            return
        self._busy = tr("正在停止服务……")
        self._set_service_buttons()
        self._audit("service_stop", self.cfg.get("endpoint", ""))

        def done(ok, err):
            self._busy = ""
            if err is not None or not ok:
                self.sa.get_log().warning("服务 10 秒内没有完全停止：%s", err or "超时")
            if then is not None:
                then()
        self._bg(lambda: self.svc.stop(10), done)

    def _restart_service(self):
        if not self.svc.running():
            return self._start_service()
        if self.mb.askyesno(tr("请确认"), tr("重启服务会让客户端短暂断开（几秒），用于让“需重启”的设置生效。继续吗？"),
                            parent=self.root):
            self._stop_service(then=self._start_service, ask=False)

    def _selftest(self):
        """自检：在临时目录启动测试服务并用客户端读取（不影响配置，不占用 4840）。"""
        was_running = self.svc.running()
        if was_running and not self.mb.askyesno(
                tr("自检"), tr("自检需要先暂停正在运行的服务（约 10～30 秒），完成后自动重新启动。继续吗？"), parent=self.root):
            return
        self._busy = tr("自检中（约 10～30 秒）……")
        self._set_service_buttons()
        sa = self.sa

        def work():
            if was_running:
                self.svc.stop(10)
            sa.get_log().info("开始自检：临时目录造演示数据 → 启动测试服务 → 客户端读取 → 检查设置页面")
            return sa.selftest_core()

        def done(result, err):
            self._busy = ""
            ok, msg = (False, str(err)) if err is not None else result
            sa.get_log().info("自检%s：%s", "通过" if ok else "未通过", msg)
            self._show(ok, (tr("自检通过：%s\n\n程序本身工作正常。如果现场仍读不到数据，问题在数据目录或网络/防火墙。") % msg)
                       if ok else (tr("自检没有通过：%s\n\n请把日志截图发给技术支持。") % msg))
            if was_running:
                self._start_service()
        self._bg(work, done)

    def _open_web(self):
        import webbrowser
        if not self.cfg.get("web_ui", True):
            return self._show(False, tr("网页设置页已关闭：在“系统设置 → 高级设置”勾选“启用设置网页”并保存后再试。"))

        def done(web, err):
            if err is not None or web is None:
                return self._show(False, tr("网页设置页没能打开：%s\n详见下方运行日志（常见原因：端口 %s 被占用）。")
                                  % (err or tr("启动失败"), self.cfg.get("web_port", 8840)))
            url = web.url()
            self.sa.get_log().info("网页设置页：%s", url)
            try:
                webbrowser.open(url)
            except Exception:  # noqa
                self._show(True, tr("请在浏览器打开：%s") % url)
        self._bg(lambda: self.svc.ensure_web(self.cfg), done)

    def _on_close(self, confirm=True):
        if self._closed:
            return
        if confirm and (self.svc.running() or self._busy):
            if not self.mb.askyesno(tr("退出"), tr("关闭窗口会停止 OPC UA 服务，DCS/SCADA 将读不到数据。\n确定退出吗？"),
                                    parent=self.root):
                return
        self._closed = True
        try:
            self.logic.stop_stability()
        except Exception:  # noqa
            pass
        try:  # Modbus Poll 轮询与 Slave 从站随窗口关闭一起停
            if getattr(self, "_mbp_stop", None) is not None:
                self._mbp_stop.set()
            if getattr(self, "_mbs_slave", None) is not None:
                self._mbs_slave.stop()
                self._mbs_slave = None
        except Exception:  # noqa
            pass
        for ev in getattr(self, "_cs_stops", {}).values():   # 通讯诊断扫描随窗口关闭一起停
            ev.set()
        try:   # 托盘图标只在窗口打开期间存在
            if self._tray is not None:
                self._tray.close()
        except Exception:  # noqa
            pass
        try:
            self.root.destroy()
        except Exception:  # noqa
            pass
        # 窗口关了，服务也必须停：不留任何后台进程
        self.svc.stop(10)
        self.sa.get_log().info("窗口已关闭，服务已停止")

    def _pump_log(self):
        import queue as _queue
        try:
            while True:
                rec = self.sa.log_q.get_nowait()
                self.box.configure(state="normal")
                self.box.insert("end", rec + "\n")
                self.box.see("end")
                self.box.configure(state="disabled")
        except _queue.Empty:
            pass
        if not self._closed:
            self.root.after(300, self._pump_log)

    # ---- 通用小工具 ----
    def _bg(self, fn, done=None):
        """工作线程跑 fn()（不卡界面），结果经队列回主线程执行 done(result, error)。"""
        import threading

        def work():
            try:
                self._q.put((done, fn(), None))
            except Exception as e:  # noqa
                self._q.put((done, None, e))
        threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self):
        import queue as _queue
        try:
            while True:
                done, result, err = self._q.get_nowait()
                if done is not None:
                    done(result, err)
        except _queue.Empty:
            pass
        if not self._closed:
            self.root.after(200, self._poll_queue)

    def _show(self, ok, text):
        if ok:
            self.mb.showinfo(tr("提示"), text, parent=self.root)
        else:
            self.mb.showerror(tr("无法完成"), text, parent=self.root)

    def _do_action(self, name, body=None, refresh=True):
        """跑一个后端动作（工作线程），完成后提示并刷新。"""
        def done(result, err):
            if err is not None:
                self._show(False, tr_msg(err))
                return
            if isinstance(result, dict):       # 连接测试类：调用方自己处理
                return
            self._show(True, str(result))
            if refresh:
                self._reload()
        self._bg(lambda: self.logic.action(name, body or {}), done)

    def _section(self, parent, title):
        tk = self.tk
        frame = tk.LabelFrame(parent, text=title, font=("Microsoft YaHei", 11, "bold"),
                              fg="#1f4e79", padx=10, pady=8)
        frame.pack(fill="x", padx=6, pady=6)
        return frame

    def _row(self, parent, label, widget_maker, hint=""):
        tk = self.tk
        row = tk.Frame(parent)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, width=26, anchor="w").pack(side="left")
        widget = widget_maker(row)
        widget.pack(side="left", fill="x", expand=True)
        if hint:
            tk.Label(parent, text=hint, fg="#777", anchor="w", justify="left",
                     wraplength=820).pack(fill="x", padx=(190, 0))
        return widget

    def _field(self, parent, key, label, hint="", kind="str", options=None, restart=False):
        """配置表单字段：登记到 self._vars，返回控件。"""
        tk, ttk = self.tk, self.ttk
        if restart:
            label = label + tr(RESTART_TAG)
        if kind == "bool":
            var = tk.BooleanVar(value=False)
            w = self._row(parent, label, lambda r: tk.Checkbutton(r, variable=var), hint)
        elif kind == "select":
            var = tk.StringVar(value="")
            labels = [tr(t) for _v, t in options]
            values = [v for v, _t in options]
            w = self._row(parent, label, lambda r: ttk.Combobox(r, values=labels, state="readonly"), hint)
            w._values = values
            w._labels = labels
            w.bind("<<ComboboxSelected>>", lambda _e: self._mark_dirty())
        elif kind == "text":
            var = None
            w = self._row(parent, label, lambda r: tk.Text(r, height=4, width=60), hint)
        else:
            var = tk.StringVar(value="")
            w = self._row(parent, label, lambda r: tk.Entry(r, textvariable=var), hint)
        self._vars[key] = (var, w, kind)
        if var is not None:
            var.trace_add("write", lambda *_a: self._mark_dirty())
        else:
            w.bind("<KeyRelease>", lambda _e: self._mark_dirty())
        return w

    def _get_values(self):
        values = {}
        for key, (var, w, kind) in self._vars.items():
            if kind == "bool":
                values[key] = bool(var.get())
            elif kind == "select":
                idx = w.current()
                values[key] = w._values[idx] if 0 <= idx < len(w._values) else var.get()
            elif kind == "text":
                values[key] = w.get("1.0", "end").strip("\n")
            else:
                values[key] = var.get()
        return values

    def _set_values(self, cfg):
        for key, (var, w, kind) in self._vars.items():
            if kind == "bool":
                var.set(bool(cfg.get(key)))
            elif kind == "select":
                val = str(cfg.get(key, ""))
                var.set(val)
                if val in w._values:
                    w.current(w._values.index(val))
            elif kind == "text":
                w.delete("1.0", "end")
                w.insert("1.0", "\n".join(cfg.get(key) or []))
            else:
                var.set("" if cfg.get(key) is None else str(cfg.get(key)))

    def _mark_dirty(self):
        try:
            changed = collect_settings(self._get_values(), self._orig)
        except Exception:  # noqa
            return
        self.dirty_txt.set(tr("有 %d 项修改未保存") % len(changed) if changed else tr("没有未保存的修改"))

    def _save(self):
        values = self._get_values()

        def done(msg, err):
            if err is not None:
                self._show(False, tr_msg(err))
                return
            self._show(True, str(msg))
            self._reload()
        self._bg(lambda: self.logic.save(values), done)

    def _reload(self):
        def done(st, err):
            if err is not None:
                self._show(False, tr("读取配置失败：%s") % err)
                return
            self._render_state(st)
        self._bg(self.logic.state, done)

    def _render_state(self, st):
        self._orig = st["config"]
        self._set_values(st["config"])
        self._mark_dirty()
        self._render_data_dirs(st["config"].get("watch_dirs"))
        if not self.exp_dir.get().strip():
            self.exp_dir.set(default_export_dir(st["config"], self.cfg["_base_dir"]))
        # 安全体检
        lines = []
        if st.get("error"):
            lines.append(tr("[错误] %s") % st["error"])
        lines += ["[%s] %s" % (r["level"], r["text"]) for r in st.get("report", [])]
        self._set_text(self.report_text, "\n".join(lines))
        self.cfg_path_var.set(tr("配置文件：%s") % st.get("cfg_path", ""))
        self.smtp_pw_state.set(tr("密码已加密保存在配置文件里。QQ/163/企业邮箱请填“授权码”。")
                               if st["config"].get("smtp_password_set")
                               else tr("QQ/163/企业邮箱请填“授权码”，不是网页登录密码。"))
        self._qs_set(st["config"])
        self.users_lb.delete(0, "end")
        for u in st["config"].get("users") or []:
            self.users_lb.insert("end", u)
        if not st["config"].get("users"):
            self.users_lb.insert("end", tr("（无账号）"))
        self._render_alerts(st.get("alerts") or {})

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _refresh_sec(self):
        try:
            text = self.sa.security_summary(self.cfg)
            mon = self.sa.ALERTS.get("monitor")
            if mon is not None:
                snap = mon.snapshot()
                text = (tr("告警：%d 项（%s）    ") % (len(snap["active"]), snap["worst"]) if snap["active"]
                        else tr("告警：无    ")) + (tr("[推送静默至 %s]    ") % snap["quiet"]["until"]
                                              if snap.get("quiet") else "") + text
            self.sec_var.set(text)
        except Exception:  # noqa
            pass
        if not self._closed:
            self.root.after(5000, self._refresh_sec)

    # ---- 原图形窗口的快捷功能 ----
    def _open_security(self):
        self.sa.SecurityWindow(self.root, self.cfg, self.sa._TkDialogs(self.root))

    def _gui_transmission(self, send):
        sa, cfg = self.sa, self.cfg

        def work():
            import asyncio
            ep = sa._local_endpoint(cfg["endpoint"])
            base = cfg.get("_base_dir") or sa.program_dir()
            cd = cfg.get("cert_dir") or "certs"
            cert_dir = cd if os.path.isabs(cd) else os.path.join(base, cd)
            sa.get_log().info("开始%s（%s）…", "发送测试数据并检测" if send else "数据传输检测", ep)
            result = asyncio.run(sa.transmission_check_async(
                ep, namespace_uri=cfg.get("namespace_uri") or "urn:chromextract:opcua",
                security=cfg.get("security", "none"), cert_dir=cert_dir,
                duration_sec=6, do_send_test=bool(send)))
            for s in result.get("steps", []):
                sa.get_log().info("%s %s：%s", "[ 通过 ]" if s["ok"] else "[ 失败 ]", s["name"], s["detail"])
            sa.get_log().info("数据传输检测%s", "通过：数据传输正常" if result.get("ok") else "未通过，见上面失败步骤")
        self._bg(work)

    def _gui_diagnose(self):
        sa, cfg = self.sa, self.cfg

        def work():
            import asyncio
            ep = sa._local_endpoint(cfg["endpoint"])
            base = cfg.get("_base_dir") or sa.program_dir()
            cd = cfg.get("cert_dir") or "certs"
            cert_dir = cd if os.path.isabs(cd) else os.path.join(base, cd)
            sa.get_log().info("开始智能诊断（%s）…", ep)
            tx = asyncio.run(sa.transmission_check_async(
                ep, namespace_uri=cfg.get("namespace_uri") or "urn:chromextract:opcua",
                security=cfg.get("security", "none"), cert_dir=cert_dir,
                duration_sec=6, do_send_test=False))
            for s in tx.get("steps", []):
                sa.get_log().info("%s %s：%s", "[ 通过 ]" if s["ok"] else "[ 失败 ]", s["name"], s["detail"])
            result = sa.diagnose(cfg, transmission_result=tx)
            icons = {"ok": tr("[ 通过 ]"), "warn": tr("[ 注意 ]"), "fail": tr("[ 失败 ]")}
            for f in result.get("findings", []):
                sa.get_log().info("%s %s：%s", icons.get(f["level"], "[ ? ]"), f["name"], f["evidence"])
                if f["level"] != "ok":
                    sa.get_log().info("处理：%s", f["advice"])
            sa.get_log().info("智能诊断结论：%s", result.get("overall", ""))
            for i, a in enumerate(result.get("actions", []), 1):
                sa.get_log().info("处理清单 %d. %s", i, a)
        self._bg(work)

    def _gui_rtu485(self):
        port = self.sd.askstring(tr("485配型诊断"), tr("串口（例如 COM3）："), parent=self.root)
        if not port:
            return
        sa = self.sa

        def work():
            import nettest
            sa.get_log().info("开始 485 配型诊断（%s，自动试参数，只读）…", port)
            r = nettest.diagnose_rtu485(port.strip(), progress=lambda s: sa.get_log().info("%s", s))
            sa.get_log().info("%s %s", "[ 通过 ]" if r["ok"] else "[ 失败 ]", r["summary"])
            for line in r["details"]:
                for sub in str(line).split("\n"):
                    sa.get_log().info("%s", sub)
        self._bg(work)

    # ---- 标签页 2：数据与导出（v2.9 新增）----
    def _build_data_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        from tkinter import scrolledtext
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("数据与导出"))
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, tr("数据目录"))
        tk.Label(sec, text=tr("存放 *_V3.db / *.mbd 的文件夹（色谱工作站 DataBrowser 的数据目录）。选择后立即保存；"
                           "服务运行中会在下一轮扫描（默认 30 秒内）自动生效，无需重启。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self.data_dirs_var = tk.StringVar(value=tr("（读取中…）"))
        tk.Label(sec, textvariable=self.data_dirs_var, anchor="w", justify="left", fg="#1f4e79",
                 wraplength=860).pack(fill="x", pady=4)
        row = tk.Frame(sec)
        row.pack(fill="x")
        tk.Button(row, text=tr("选择数据目录…"), command=lambda: self._pick_data_dir(replace=True)).pack(side="left", padx=(0, 4))
        tk.Button(row, text=tr("添加目录…"), command=lambda: self._pick_data_dir(replace=False)).pack(side="left", padx=4)
        tk.Button(row, text=tr("打开数据目录"), command=self._open_data_dir).pack(side="left", padx=4)
        tk.Button(row, text=tr("查看数据源"), command=self._list_sources).pack(side="left", padx=(16, 4))
        self.src_out = scrolledtext.ScrolledText(sec, height=10, state="disabled", wrap="none")
        self.src_out.pack(fill="x", pady=4)

        sec = self._section(inner, tr("导出 CSV"))
        tk.Label(sec, text=tr("把数据目录里的谱图导出为 CSV：index.csv（总清单）+ csv\\（每条谱图一个文件：时间、原始计数、"
                           "微伏、显示值）。每次导出放在带时间的子文件夹里，不覆盖上次结果。读取的是数据库副本，"
                           "不影响 DataBrowser 运行；服务开着或停着都可以导出。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self.exp_dir = tk.StringVar(value="")
        self.exp_last = tk.StringVar(value="")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=tr("导出到"), width=26, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.exp_dir).pack(side="left", fill="x", expand=True)
        tk.Button(row, text=tr("浏览…"), command=self._pick_export_dir).pack(side="left", padx=4)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=tr("每个流路只导出最近 N 个周期"), width=26, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.exp_last, width=10).pack(side="left")
        tk.Label(row, text=tr("留空 = 全部导出（数据多时较慢）"), fg="#777").pack(side="left", padx=8)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=4)
        self.exp_btn = tk.Button(row, text=tr("开始导出"), command=self._export_csv)
        self.exp_btn.pack(side="left")
        tk.Button(row, text=tr("打开导出文件夹"), command=self._open_export_dir).pack(side="left", padx=8)
        self.exp_out = scrolledtext.ScrolledText(sec, height=8, state="disabled", wrap="word")
        self.exp_out.pack(fill="x", pady=4)
        self._last_export = ""

    def _render_data_dirs(self, dirs):
        dirs = resolve_dirs(dirs, self.cfg["_base_dir"])
        if not dirs:
            self.data_dirs_var.set(tr("当前数据目录：（未设置）—— 请点“选择数据目录…”"))
            return
        parts = []
        for d in dirs:
            parts.append("%s%s" % (d, "" if os.path.isdir(d) else tr("   ← 找不到这个文件夹")))
        self.data_dirs_var.set(tr("当前数据目录：\n  ") + "\n  ".join(parts))

    def _apply_watch_dirs(self, dirs):
        """只更新“数据目录”一项（不动表单里其它未保存的修改）。"""
        self._orig["watch_dirs"] = list(dirs)
        var, w, kind = self._vars.get("watch_dirs", (None, None, None))
        if w is not None and kind == "text":
            w.delete("1.0", "end")
            w.insert("1.0", "\n".join(dirs))
        self._render_data_dirs(dirs)
        self._mark_dirty()

    def _pick_data_dir(self, replace=True):
        cur = resolve_dirs(self._orig.get("watch_dirs"), self.cfg["_base_dir"])
        start = cur[0] if cur and os.path.isdir(cur[0]) else self.cfg["_base_dir"]
        d = self.fd.askdirectory(parent=self.root, initialdir=start,
                                 title=tr("选择存放 *_V3.db / *.mbd 的数据文件夹"))
        if not d:
            return
        try:
            dirs = merge_watch_dirs(cur, d, self.cfg["_base_dir"], replace=replace)
        except ValueError as e:
            return self._show(False, tr_msg(e))

        def done(saved, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            self._apply_watch_dirs(saved)
            n = 0
            for x in saved:
                try:
                    n += len(chrom_core.find_inputs([x]))
                except Exception:  # noqa
                    pass
            self.sa.get_log().info("数据目录已设置为：%s（共找到 %d 个数据文件）", "、".join(saved), n)
            self._show(True, tr("数据目录已保存，共找到 %d 个数据文件。%s") % (
                n, "" if n else tr("\n注意：暂时没有 *_V3.db 或 .mbd 文件，请确认路径是否正确。")))
        self._bg(lambda: self.logic.set_watch_dirs(dirs), done)

    def _open_data_dir(self):
        cur = [d for d in resolve_dirs(self._orig.get("watch_dirs"), self.cfg["_base_dir"]) if os.path.isdir(d)]
        if not cur:
            return self._show(False, tr("还没有设置有效的数据目录。"))
        self._open_path(cur[0])

    def _list_sources(self):
        self._set_text(self.src_out, tr("正在读取数据源（数据库较大时需要几十秒）…"))

        def done(text, err):
            self._set_text(self.src_out, str(err) if err is not None else text)
        self._bg(self.logic.list_sources_text, done)

    def _pick_export_dir(self):
        d = self.fd.askdirectory(parent=self.root, initialdir=self.exp_dir.get() or self.cfg["_base_dir"],
                                 title=tr("选择 CSV 导出位置"))
        if d:
            self.exp_dir.set(os.path.normpath(d))

    def _export_csv(self):
        try:
            last = parse_last_n(self.exp_last.get())
        except ValueError as e:
            return self._show(False, tr_msg(e))
        root = self.exp_dir.get().strip() or default_export_dir(self._orig, self.cfg["_base_dir"])
        out = make_export_dir(root)
        self.exp_btn.configure(state="disabled", text=tr("导出中…"))
        self._set_text(self.exp_out, tr("正在导出到 %s …") % out)

        def done(result, err):
            self.exp_btn.configure(state="normal", text=tr("开始导出"))
            if err is not None:
                self._set_text(self.exp_out, tr("导出失败：%s") % err)
                return self._show(False, tr("导出失败：%s") % err)
            n, text = result
            self._last_export = out
            self._set_text(self.exp_out, text)
            self.sa.get_log().info("桌面导出 CSV：%d 条谱图 → %s", n, out)
            self._show(n > 0, (tr("已导出 %d 条谱图到：\n%s") % (n, out)) if n else
                       tr("没有导出任何谱图（数据目录里没有可解析的谱图）。详情见导出区的过程信息。"))
        self._bg(lambda: self.logic.export_csv(out, last=last), done)

    def _open_export_dir(self):
        d = self._last_export or self.exp_dir.get().strip() or default_export_dir(self._orig, self.cfg["_base_dir"])
        if not os.path.isdir(d):
            return self._show(False, tr("导出文件夹还不存在（先导出一次）：%s") % d)
        self._open_path(d)

    # ---- 标签页 1：运行状态 ----
    def _build_status_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        from tkinter import scrolledtext
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("运行状态"))
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, tr("运行告警"))
        self.al_sum = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.al_sum, anchor="w", fg="#666").pack(fill="x")
        self.al_quiet = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.al_quiet, anchor="w", fg="#856404").pack(fill="x")
        self.al_active = scrolledtext.ScrolledText(sec, height=5, state="disabled", wrap="word")
        self.al_active.pack(fill="x", pady=4)
        tk.Label(sec, text=tr("最近的告警记录"), anchor="w").pack(fill="x")
        self.al_hist = scrolledtext.ScrolledText(sec, height=5, state="disabled", wrap="word")
        self.al_hist.pack(fill="x", pady=4)

        sec = self._section(inner, tr("设备检测"))
        tk.Label(sec, text=tr("服务运行时按间隔自动检测关键设备（DCS、网关、分析仪等），连续失败达到次数就告警，"
                           "一次成功自动恢复；告警走告警与通知页设置的群机器人 / 邮件，遵守静默时段和渠道级别。"
                           "添加方法：在“诊断工具”的连接测试里选好类型和参数、测通后，点“加为设备检测”。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self.pr_sum = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.pr_sum, anchor="w", fg="#666").pack(fill="x")
        cols = ("name", "target", "cond", "state", "last", "uptime")
        self.pr_tree = ttk.Treeview(sec, columns=cols, show="headings", height=6)
        for col, title, width in (("name", tr("名称"), 130), ("target", tr("检测"), 220), ("cond", tr("间隔 / 告警条件"), 150),
                                  ("state", tr("状态"), 110), ("last", tr("最近结果"), 260), ("uptime", tr("可用率"), 90)):
            self.pr_tree.heading(col, text=title)
            self.pr_tree.column(col, width=width, anchor="w")
        self.pr_tree.pack(fill="x", pady=4)
        btns = tk.Frame(sec)
        btns.pack(fill="x")
        for text, cmd in ((tr("立即检测"), self._pr_run), (tr("编辑"), self._pr_edit),
                          (tr("启用/停用"), self._pr_toggle), (tr("删除"), self._pr_remove),
                          (tr("刷新"), self._poll_probes_now)):
            tk.Button(btns, text=text, command=cmd).pack(side="left", padx=4)

        sec = self._section(inner, tr("通道告警设置"))
        tk.Label(sec, text=tr("长期停用或检修中的流路可以屏蔽告警；轮换间隔长的流路可以单独加大“无新周期”分钟数。"
                           "屏蔽时该通道的告警直接移除，不发“恢复”；可设到期时间自动解除。选中一行点“编辑规则”修改。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        cols = ("channel", "cycle", "state", "mute", "stale", "until", "note")
        self.ch_tree = ttk.Treeview(sec, columns=cols, show="headings", height=7)
        for col, title, width in (("channel", tr("通道"), 240), ("cycle", tr("最后周期"), 130), ("state", tr("状态"), 130),
                                  ("mute", tr("屏蔽告警"), 70), ("stale", tr("无新周期告警（分钟）"), 130),
                                  ("until", tr("到期"), 130), ("note", tr("备注"), 110)):
            self.ch_tree.heading(col, text=title)
            self.ch_tree.column(col, width=width, anchor="w")
        self.ch_tree.pack(fill="x", pady=4)
        tk.Button(sec, text=tr("编辑选中通道的规则…"), command=self._ch_edit).pack(anchor="w", pady=2)
        self._ch_rows = {}

    def _render_alerts(self, a):
        if not a:
            return
        # 静默横幅与列表（静默列表在告警页渲染）
        q = a.get("quiet")
        self.al_quiet.set(tr("推送静默中，至 %s%s") % (q["until"], tr("（%s）") % q["note"] if q.get("note") else "") if q else "")
        self._render_quiet_list(a.get("quiet_list") or [])
        if a.get("offline"):
            self.al_sum.set(tr("（服务未运行：点窗口上方“启动服务”后这里显示实时告警）"))
            self._set_text(self.al_active, "")
            self._set_text(self.al_hist, "")
        else:
            self.al_sum.set(tr("最近扫描 %s · %s 个通道") % (a.get("last_scan") or tr("尚未完成"), a.get("channels", 0)))
            act = a.get("active") or []
            self._set_text(self.al_active, "\n".join(
                tr("[%s] %s（自 %s）") % (x["level"], x["text"], x["since"]) for x in act) or tr("[正常] 没有告警"))
            hist = a.get("history") or []
            self._set_text(self.al_hist, "\n".join(
                tr("%s %s：%s") % (h["time"], tr("✓ 恢复") if h["event"] == "恢复" else "⚠ " + h["level"], h["text"])
                for h in hist) or tr("（无）"))
        self._render_channels(a)

    def _render_channels(self, a):
        rows = (a or {}).get("channel_list") or []
        self._ch_rows = {}
        for item in self.ch_tree.get_children():
            self.ch_tree.delete(item)
        if not rows:
            self.ch_tree.insert("", "end", values=(
                tr("服务未运行，看不到通道。启动服务后这里会列出所有通道。") if (a or {}).get("offline")
                else tr("还没有扫描到通道。"), "", "", "", "", "", ""))
            return
        for c in rows:
            if c.get("present") is None:
                state = "—"
            elif not c["present"]:
                state = tr("丢失")
            elif c.get("alerting"):
                state = tr("告警")
            else:
                state = tr("正常")
            if c.get("idle_min") is not None:
                state += tr("（%s 分钟前更新）") % c["idle_min"]
            iid = self.ch_tree.insert("", "end", values=(
                c["label"], c.get("cycle_time") or "", state,
                tr("是") if c.get("muted") else tr("否"),
                "" if c.get("stale_minutes") in (None, "") else c["stale_minutes"],
                c.get("until") or "", c.get("note") or ""))
            self._ch_rows[iid] = c

    def _ch_edit(self):
        tk = self.tk
        sel = self.ch_tree.selection()
        if not sel or sel[0] not in self._ch_rows:
            return self._show(False, tr("请先选中一个通道。"))
        c = self._ch_rows[sel[0]]
        win = tk.Toplevel(self.root)
        win.title(tr("通道告警规则"))
        win.geometry("460x280")
        tk.Label(win, text=c["label"], font=("Microsoft YaHei", 11, "bold"), anchor="w").pack(
            fill="x", padx=12, pady=(10, 4))
        mute = tk.BooleanVar(value=bool(c.get("muted")))
        tk.Checkbutton(win, text=tr("屏蔽该通道的告警"), variable=mute).pack(anchor="w", padx=12)
        stale = tk.StringVar(value="" if c.get("stale_minutes") in (None, "") else str(c["stale_minutes"]))
        until = tk.StringVar(value=c.get("until") or "")
        note = tk.StringVar(value=c.get("note") or "")
        for label, var, hint in ((tr("无新周期告警（分钟，留空=默认）"), stale, ""),
                                 (tr("到期时间（YYYY-MM-DD HH:MM，留空=不自动解除）"), until, ""),
                                 (tr("备注"), note, "")):
            tk.Label(win, text=label, anchor="w").pack(fill="x", padx=12, pady=(8, 0))
            tk.Entry(win, textvariable=var).pack(fill="x", padx=12)

        def save():
            body = {"rule": channel_rule_body(c["key"], mute.get(), stale.get().strip(),
                                              until.get().strip(), note.get())}
            win.destroy()
            self._do_action("channel_rule", body)
        tk.Button(win, text=tr("保存"), command=save).pack(pady=12)

    # ---- 设备检测（状态页表格）----
    def _poll_probes_now(self):
        self._bg(self.logic.probes, lambda r, e: None if e else self._render_probes(r))

    def _poll_probes(self):
        self._poll_probes_now()
        if not self._closed:
            self.root.after(10000, self._poll_probes)

    def _render_probes(self, j):
        rows = (j or {}).get("probes") or []
        self._pr_list = rows
        self.pr_sum.set(("" if (j or {}).get("running") else tr("（服务未运行，只能查看和立即检测）"))
                        + tr(" 共 %d 个，告警中 %d 个") % (len(rows), sum(1 for x in rows if x["alerting"]))
                        if rows else tr("还没有设备检测。"))
        for item in self.pr_tree.get_children():
            self.pr_tree.delete(item)
        for p in rows:
            if not p["enabled"]:
                state = tr("停用")
            elif p["alerting"]:
                state = p["level"]
            elif p.get("last"):
                state = tr("正常") if p["last"]["ok"] else tr("失败 %d/%d") % (p["fails"], p["fail_threshold"])
            else:
                state = tr("待检测")
            last = ""
            if p.get("last"):
                last = "%s %s" % (p["last"]["time"], (p["last"]["summary"] or "")[:60])
            uptime = "—" if p.get("uptime_pct") is None else tr("%s%%（%d 次）") % (p["uptime_pct"], p["checks"])
            self.pr_tree.insert("", "end", iid=p["id"], values=(
                p["label"], "%s %s" % (p["kind_name"], p["target"]),
                tr("每 %s 秒，连续 %s 次失败→%s") % (p["interval"], p["fail_threshold"], p["level"]),
                state, last, uptime))

    def _pr_selected(self):
        sel = self.pr_tree.selection()
        if not sel:
            self._show(False, tr("请先选中一个设备检测。"))
            return None
        return next((x for x in self._pr_list if x["id"] == sel[0]), None)

    def _pr_run(self):
        p = self._pr_selected()
        if p is None:
            return

        def done(result, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            self._render_nettest_result({"test": result, "msg": result.get("summary", "")})
            self._poll_probes_now()
        self._bg(lambda: self.logic.action("probe_run", {"id": p["id"]}), done)

    def _pr_edit(self):
        p = self._pr_selected()
        if p is None:
            return
        self.nb.select(2)     # 跳到诊断工具页
        self._nt_show(p["kind"])
        for k, v in (p.get("params") or {}).items():
            if k in self._nt_vars:
                var, _w, typ = self._nt_vars[k]
                if typ == "chk":
                    var.set(bool(v))
                else:
                    var.set("" if v is None else str(v))
        self.pr_label.set(p["label"])
        self.pr_iv.set(str(p["interval"]))
        self.pr_thr.set(str(p["fail_threshold"]))
        self.pr_lv.set(tr(p["level"]))
        self._pr_editing = p["id"]
        self._show(True, tr("已把「%s」的参数填到连接测试里，改完点“加为设备检测”保存%s。")
                   % (p["label"], tr("（密码留空 = 保留原密码）") if p["kind"] == "opcua" else ""))

    def _pr_toggle(self):
        p = self._pr_selected()
        if p is not None:
            self._do_action("probe_toggle", {"id": p["id"]}, refresh=False)
            self.root.after(600, self._poll_probes_now)

    def _pr_remove(self):
        p = self._pr_selected()
        if p is not None and self.mb.askyesno(tr("请确认"), tr("删除设备检测「%s」？") % p["label"], parent=self.root):
            self._do_action("probe_remove", {"id": p["id"]}, refresh=False)
            self.root.after(600, self._poll_probes_now)

    # ---- 标签页 2：告警与通知 ----
    def _build_alerts_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("告警与通知"))
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, tr("告警设置"))
        self._field(sec, "alert_stale_minutes", tr("通道无新周期告警（分钟）"),
                    tr("超过这么久没有新周期就告警（分析仪停机、数据库不再更新）；按分析周期设，例如周期 10 分钟可设 60；0 = 不检查"), "num")
        self._field(sec, "alert_scan_errors", tr("连续扫描出错告警（次）"), tr("0 = 不检查"), "num")
        self._field(sec, "alert_webhook", tr("告警推送地址（可选）"),
                    tr("企业微信 / 钉钉 / 飞书群机器人的 Webhook 地址，或任意接收 JSON 的 https 地址；告警出现和恢复时各推送一次。钉钉机器人请在安全设置里加关键词“色谱”"), "str")
        self._field(sec, "alert_webhook_level", tr("群机器人发送级别"), "", "select",
                    SELECT_OPTIONS["alert_webhook_level"])
        tk.Button(sec, text=tr("发送测试消息"), command=lambda: self._do_action("test_webhook")).pack(anchor="w", pady=4)

        sec = self._section(inner, tr("静默时段（计划停机 / 检修时不推送）"))
        tk.Label(sec, text=tr("静默期间群机器人和邮件都不发；日志、本窗口和 OPC UA 告警节点照常显示。"
                           "静默结束时仍未恢复的告警会补发一次；静默期间出现又恢复的不再打扰。"
                           "时间按本机时间，添加后立即生效。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self.quiet_lb = tk.Listbox(sec, height=4, exportselection=False)
        self.quiet_lb.pack(fill="x", pady=4)
        tk.Button(sec, text=tr("删除选中静默时段"), command=self._quiet_remove).pack(anchor="w")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=(8, 2))
        tk.Label(row, text=tr("临时静默"), width=26, anchor="w").pack(side="left")
        for text, cmd in ((tr("2 小时"), lambda: self._do_action("quiet_now", {"hours": 2})),
                          (tr("8 小时"), lambda: self._do_action("quiet_now", {"hours": 8})),
                          (tr("24 小时"), lambda: self._do_action("quiet_now", {"hours": 24})),
                          (tr("立即结束静默"), lambda: self._do_action("quiet_end"))):
            tk.Button(row, text=text, command=cmd).pack(side="left", padx=4)
        self.q_s, self.q_e, self.q_n = tk.StringVar(), tk.StringVar(), tk.StringVar()
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("一次性时段"), width=26, anchor="w").pack(side="left")
        for var, wdt in ((self.q_s, 18), (self.q_e, 18), (self.q_n, 14)):
            tk.Entry(row, textvariable=var, width=wdt).pack(side="left", padx=3)
        tk.Button(row, text=tr("添加"), command=self._quiet_once).pack(side="left", padx=4)
        tk.Label(sec, text=tr("格式：开始/结束填 YYYY-MM-DD HH:MM，第三框备注（如 大修）；最长 31 天"),
                 fg="#777", anchor="w").pack(fill="x", padx=(190, 0))
        self.d_s, self.d_e, self.d_n = tk.StringVar(), tk.StringVar(), tk.StringVar()
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("每天重复时段"), width=26, anchor="w").pack(side="left")
        for var, wdt in ((self.d_s, 10), (self.d_e, 10), (self.d_n, 14)):
            tk.Entry(row, textvariable=var, width=wdt).pack(side="left", padx=3)
        tk.Button(row, text=tr("添加"), command=self._quiet_daily).pack(side="left", padx=4)
        row = tk.Frame(sec)
        row.pack(fill="x", padx=(190, 0))
        tk.Label(row, text=tr("星期（都不勾 = 每天）："), fg="#777").pack(side="left")
        self._day_vars = []
        for i, ch in enumerate("一二三四五六日", 1):
            v = tk.BooleanVar(value=False)
            self._day_vars.append(v)
            tk.Checkbutton(row, text=tr("周" + ch), variable=v).pack(side="left")
        tk.Label(sec, text=tr("开始/结束填 HH:MM；结束早于开始表示跨午夜，例如 22:00～06:00"),
                 fg="#777", anchor="w").pack(fill="x", padx=(190, 0))

        sec = self._section(inner, tr("邮件通知（可选）"))
        self._field(sec, "alert_email_to", tr("收件人"),
                    tr("多个用逗号隔开；留空 = 不发邮件。告警出现和恢复时各发一封，每小时最多 20 封"), "str")
        self._field(sec, "alert_email_level", tr("邮件发送级别"),
                    tr("严重 = 扫描卡死、连续扫描出错；警告 = 无数据、数据源丢失、通道长时间不更新。恢复通知只发给收到过该告警的渠道"),
                    "select", SELECT_OPTIONS["alert_email_level"])
        self._field(sec, "smtp_host", tr("SMTP 服务器"), "", "str")
        self._field(sec, "smtp_port", tr("端口"), "", "num")
        self._field(sec, "smtp_security", tr("加密方式"), "", "select", SELECT_OPTIONS["smtp_security"])
        self._field(sec, "smtp_user", tr("用户名"), tr("免登录的内网中继可留空"), "str")
        self._field(sec, "smtp_from", tr("发件人地址"), tr("留空 = 用户名"), "str")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=tr("密码 / 授权码"), width=26, anchor="w").pack(side="left")
        self.smtp_pw = tk.StringVar()
        tk.Entry(row, textvariable=self.smtp_pw, show="*").pack(side="left", fill="x", expand=True)
        tk.Button(row, text=tr("保存密码"), command=self._set_smtp_pw).pack(side="left", padx=4)
        tk.Button(row, text=tr("清除"), command=self._clear_smtp_pw).pack(side="left", padx=4)
        self.smtp_pw_state = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.smtp_pw_state, fg="#777", anchor="w").pack(fill="x", padx=(190, 0))
        tk.Button(sec, text=tr("发送测试邮件"), command=lambda: self._do_action("test_email")).pack(anchor="w", pady=4)
        tk.Label(sec, text=tr("先点底部“保存设置”，再测试"), fg="#777", anchor="w").pack(fill="x")

    def _render_quiet_list(self, items):
        self.quiet_lb.delete(0, "end")
        for it in items:
            self.quiet_lb.insert("end", it.get("text") or str(it))
        if not items:
            self.quiet_lb.insert("end", tr("（没有静默时段）"))

    def _quiet_remove(self):
        sel = self.quiet_lb.curselection()
        if not sel:
            return self._show(False, tr("请先选中一条静默时段。"))
        self._do_action("quiet_remove", {"index": sel[0]})

    def _quiet_once(self):
        self._do_action("quiet_add", {"item": quiet_once_item(self.q_s.get(), self.q_e.get(), self.q_n.get())})

    def _quiet_daily(self):
        days = [i + 1 for i, v in enumerate(self._day_vars) if v.get()]
        self._do_action("quiet_add", {"item": quiet_daily_item(self.d_s.get(), self.d_e.get(), days, self.d_n.get())})

    def _set_smtp_pw(self):
        pw = self.smtp_pw.get()
        if not pw:
            return self._show(False, tr("请输入密码或授权码。"))

        def done(msg, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            self.smtp_pw.set("")
            self._show(True, str(msg))
            self._reload()
        self._bg(lambda: self.logic.action("set_smtp_password", {"password": pw}), done)

    def _clear_smtp_pw(self):
        if self.mb.askyesno(tr("请确认"), tr("确定清除 SMTP 密码吗？"), parent=self.root):
            self._do_action("set_smtp_password", {"password": ""})

    # ---- 标签页 3：诊断工具 ----
    def _build_tools_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        from tkinter import scrolledtext
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("诊断工具"))
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, tr("驱动与软件环境检测"))
        tk.Label(sec, text=tr("只读检查，不会安装或修改任何东西：串口设备和驱动（CH340 / FTDI / CP210x / PL2303 等，"
                           "含黄色感叹号设备）、运行库、防火墙服务、OPC UA 端口、数据目录、旧版本遗留的后台自启。"
                           "现场装好后、或串口突然不能用时先跑一遍。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self._field(sec, "env_check_on_start", tr("服务启动时自动检测"),
                    tr("服务启动后约 8 秒自动检测；“问题”级别的项会告警（警告级），复查时消失自动恢复；“注意”只记日志"),
                    "bool", restart=True)
        self._field(sec, "env_check_hours", tr("定期复查（小时）"), tr("0 = 只在启动时检查；改完点底部“保存设置”"), "num")
        self.env_last = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.env_last, anchor="w", fg="#555").pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=4)
        self.dc_btn = tk.Button(row, text=tr("开始检测"), command=self._dc_run)
        self.dc_btn.pack(side="left")
        self.dc_time = tk.StringVar(value="")
        tk.Label(row, textvariable=self.dc_time, fg="#777").pack(side="left", padx=8)
        self.dc_out = scrolledtext.ScrolledText(sec, height=8, state="disabled", wrap="word")
        self.dc_out.pack(fill="x", pady=4)

        sec = self._section(inner, tr("连接测试"))
        tk.Label(sec, text=tr("现场排查用：网络（Ping / TCP 端口 / DNS / OPC UA / Modbus TCP）和串口"
                           "（RS-232 / RS-485 / RS-422，Modbus RTU）。测试在本机执行。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        kind_row = tk.Frame(sec)
        kind_row.pack(fill="x", pady=4)
        self._nt_kind_btns = {}
        for kind, spec in NETTEST_SPECS.items():
            b = tk.Button(kind_row, text=tr(spec["t"]), command=lambda k=kind: self._nt_show(k))
            b.pack(side="left", padx=2, pady=2)
            self._nt_kind_btns[kind] = b
        self.nt_tip = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.nt_tip, fg="#856404", anchor="w").pack(fill="x")
        self.nt_form = tk.Frame(sec)
        self.nt_form.pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=4)
        self.nt_btn = tk.Button(row, text=tr("开始测试"), command=self._nt_run)
        self.nt_btn.pack(side="left")
        self.nt_time = tk.StringVar(value="")
        tk.Label(row, textvariable=self.nt_time, fg="#777").pack(side="left", padx=8)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("加为设备检测："), font=("Microsoft YaHei", 10, "bold")).pack(side="left")
        self.pr_label, self.pr_iv, self.pr_thr, self.pr_lv = (tk.StringVar(), tk.StringVar(value="60"),
                                                              tk.StringVar(value="3"), tk.StringVar(value=tr("警告")))
        tk.Label(row, text=tr("名称")).pack(side="left", padx=(6, 2))
        tk.Entry(row, textvariable=self.pr_label, width=14).pack(side="left")
        tk.Label(row, text=tr("间隔(秒)")).pack(side="left", padx=(6, 2))
        tk.Entry(row, textvariable=self.pr_iv, width=6).pack(side="left")
        tk.Label(row, text=tr("连续失败")).pack(side="left", padx=(6, 2))
        tk.Entry(row, textvariable=self.pr_thr, width=5).pack(side="left")
        tk.Label(row, text=tr("次告警")).pack(side="left", padx=2)
        ttk.Combobox(row, textvariable=self.pr_lv, values=[tr("警告"), tr("严重")], state="readonly", width=7).pack(side="left")
        tk.Button(row, text=tr("加为设备检测"), command=self._pr_add).pack(side="left", padx=6)
        self.nt_out = scrolledtext.ScrolledText(sec, height=7, state="disabled", wrap="word")
        self.nt_out.pack(fill="x", pady=4)

        # ---- Modbus Poll 主站测试（v2.8） ----
        self._mbp_polling = False
        self._mbp_stop = None
        sec = self._section(inner, tr("Modbus Poll 主站测试"))
        tk.Label(sec, text=tr("像 Modbus Poll 一样读/写设备数据：TCP 或 RTU，读 01/02/03/04，"
                           "写 05/06/15/16（写之前会弹窗确认目标和数值）。寄存器按数据类型+字节序解码显示，"
                           "每次通讯显示请求/响应报文和耗时。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        self.mbp_transport = tk.StringVar(value="TCP")
        self.mbp_host = tk.StringVar(value="127.0.0.1")
        self.mbp_port = tk.StringVar(value="502")
        self.mbp_serial = tk.StringVar(value="COM3")
        self.mbp_baud = tk.StringVar(value="9600")
        self.mbp_parity = tk.StringVar(value="N")
        self.mbp_stopbits = tk.StringVar(value="1")
        tk.Label(row, text=tr("方式")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbp_transport, values=["TCP", "RTU"],
                     state="readonly", width=6).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("主机")).pack(side="left")
        tk.Entry(row, textvariable=self.mbp_host, width=14).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("TCP端口")).pack(side="left")
        tk.Entry(row, textvariable=self.mbp_port, width=6).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("串口")).pack(side="left")
        tk.Entry(row, textvariable=self.mbp_serial, width=8).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("波特率")).pack(side="left")
        tk.Entry(row, textvariable=self.mbp_baud, width=7).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("校验")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbp_parity, values=["N", "E", "O"],
                     state="readonly", width=4).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("停止位")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbp_stopbits, values=["1", "1.5", "2"],
                     state="readonly", width=4).pack(side="left", padx=2)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        self.mbp_unit = tk.StringVar(value="1")
        self.mbp_func = tk.StringVar(value=tr("03 读保持寄存器"))
        self.mbp_addr = tk.StringVar(value="0")
        self.mbp_count = tk.StringVar(value="10")
        self.mbp_dtype = tk.StringVar(value="uint16")
        self.mbp_order = tk.StringVar(value="ABCD")
        tk.Label(row, text=tr("站号")).pack(side="left")
        tk.Entry(row, textvariable=self.mbp_unit, width=5).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("功能码")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbp_func, values=[tr(x) for x in MODBUS_FUNC_LABELS],
                     state="readonly", width=16).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("地址")).pack(side="left")
        tk.Entry(row, textvariable=self.mbp_addr, width=7).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("数量")).pack(side="left")
        tk.Entry(row, textvariable=self.mbp_count, width=6).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("数据类型")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbp_dtype, values=MODBUS_DTYPES,
                     state="readonly", width=9).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("字节序")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbp_order, values=MODBUS_ORDERS,
                     state="readonly", width=7).pack(side="left", padx=2)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        self.mbp_values = tk.StringVar(value="")
        self.mbp_interval = tk.StringVar(value="1")
        tk.Label(row, text=tr("写入值")).pack(side="left")
        tk.Entry(row, textvariable=self.mbp_values, width=30).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("（逗号分隔；寄存器按数据类型解释，如 float32 填 123.45）"), fg="#777").pack(side="left")
        tk.Label(row, text=tr("轮询间隔(秒)")).pack(side="left", padx=(8, 2))
        tk.Entry(row, textvariable=self.mbp_interval, width=6).pack(side="left")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        tk.Button(row, text=tr("单次执行"), command=self._mbp_once).pack(side="left")
        self.mbp_poll_btn = tk.Button(row, text=tr("开始轮询"), command=self._mbp_toggle_poll)
        self.mbp_poll_btn.pack(side="left", padx=6)
        tk.Button(row, text=tr("写入（确认后执行）"), command=self._mbp_once).pack(side="left")
        self.mbp_out = scrolledtext.ScrolledText(sec, height=8, state="disabled", wrap="word")
        self.mbp_out.pack(fill="x", pady=4)

        # ---- Modbus Slave 从站模拟器（v2.8） ----
        self._mbs_slave = None
        self._mbs_store = None
        self._mbs_logs = []
        self._mbs_log_idx = 0
        sec = self._section(inner, tr("Modbus Slave 从站模拟器"))
        tk.Label(sec, text=tr("像 Modbus Slave 一样模拟一台从站设备：TCP 或 RTU，四区数据"
                           "（线圈 / 离散输入 / 保持寄存器 / 输入寄存器）可编辑，带通讯日志。"
                           "只在点“启动从站”后才监听，默认不启动；监听地址见状态行。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        self.mbs_transport = tk.StringVar(value="TCP")
        self.mbs_host = tk.StringVar(value="0.0.0.0")
        self.mbs_tcp_port = tk.StringVar(value="502")
        self.mbs_serial = tk.StringVar(value="COM3")
        self.mbs_baud = tk.StringVar(value="9600")
        self.mbs_parity = tk.StringVar(value="N")
        self.mbs_stopbits = tk.StringVar(value="1")
        tk.Label(row, text=tr("方式")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbs_transport, values=["TCP", "RTU"],
                     state="readonly", width=6).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("监听地址")).pack(side="left")
        tk.Entry(row, textvariable=self.mbs_host, width=12).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("TCP端口")).pack(side="left")
        tk.Entry(row, textvariable=self.mbs_tcp_port, width=6).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("串口")).pack(side="left")
        tk.Entry(row, textvariable=self.mbs_serial, width=8).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("波特率")).pack(side="left")
        tk.Entry(row, textvariable=self.mbs_baud, width=7).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("校验")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbs_parity, values=["N", "E", "O"],
                     state="readonly", width=4).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("停止位")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbs_stopbits, values=["1", "1.5", "2"],
                     state="readonly", width=4).pack(side="left", padx=2)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        self.mbs_unit = tk.StringVar(value="1")
        self.mbs_size = tk.StringVar(value="10000")
        tk.Label(row, text=tr("站号")).pack(side="left")
        tk.Entry(row, textvariable=self.mbs_unit, width=5).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("每区大小")).pack(side="left")
        tk.Entry(row, textvariable=self.mbs_size, width=8).pack(side="left", padx=(2, 8))
        self.mbs_btn = tk.Button(row, text=tr("启动从站"), command=self._mbs_toggle)
        self.mbs_btn.pack(side="left", padx=6)
        self.mbs_status = tk.StringVar(value=tr("已停止（默认不监听）"))
        tk.Label(row, textvariable=self.mbs_status, fg="#555").pack(side="left", padx=8)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        self.mbs_area = tk.StringVar(value=tr("保持寄存器"))
        self.mbs_addr = tk.StringVar(value="0")
        self.mbs_count = tk.StringVar(value="10")
        self.mbs_value = tk.StringVar(value="")
        self.mbs_dtype = tk.StringVar(value="uint16")
        self.mbs_order = tk.StringVar(value="ABCD")
        tk.Label(row, text=tr("数据区")).pack(side="left")
        ttk.Combobox(row, textvariable=self.mbs_area, values=[tr(x) for x in MODBUS_AREA_LABELS],
                     state="readonly", width=11).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("地址")).pack(side="left")
        tk.Entry(row, textvariable=self.mbs_addr, width=7).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("数量")).pack(side="left")
        tk.Entry(row, textvariable=self.mbs_count, width=6).pack(side="left", padx=(2, 8))
        tk.Button(row, text=tr("读取数据区"), command=self._mbs_read_area).pack(side="left", padx=4)
        tk.Label(row, text=tr("值")).pack(side="left", padx=(8, 2))
        tk.Entry(row, textvariable=self.mbs_value, width=16).pack(side="left", padx=(2, 8))
        ttk.Combobox(row, textvariable=self.mbs_dtype, values=MODBUS_DTYPES,
                     state="readonly", width=9).pack(side="left", padx=(2, 4))
        ttk.Combobox(row, textvariable=self.mbs_order, values=MODBUS_ORDERS,
                     state="readonly", width=7).pack(side="left", padx=(2, 8))
        tk.Button(row, text=tr("写入数据区"), command=self._mbs_write_area).pack(side="left")
        self.mbs_area_out = scrolledtext.ScrolledText(sec, height=5, state="disabled", wrap="word")
        self.mbs_area_out.pack(fill="x", pady=4)
        tk.Label(sec, text=tr("通讯日志"), font=("Microsoft YaHei", 10, "bold")).pack(anchor="w")
        self.mbs_log = scrolledtext.ScrolledText(sec, height=7, state="disabled", wrap="word")
        self.mbs_log.pack(fill="x", pady=4)

        sec = self._section(inner, tr("长时间稳定性测试"))
        tk.Label(sec, text=tr("用上面同样的参数反复测试，统计丢包率、延迟和错误。支持 Ping / TCP 端口 / DNS / OPC UA / "
                           "Modbus TCP / Modbus RTU / 串口收发；测试期间请保持窗口打开（关闭窗口会停止测试）。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=tr("间隔(秒)")).pack(side="left")
        self.st_iv = tk.StringVar(value="1")
        tk.Entry(row, textvariable=self.st_iv, width=8).pack(side="left", padx=4)
        tk.Label(row, text=tr("时长")).pack(side="left", padx=(8, 2))
        self.st_min = tk.StringVar(value=tr("10 分钟"))
        ttk.Combobox(row, textvariable=self.st_min, state="readonly", width=12,
                     values=[tr("10 分钟"), tr("1 小时"), tr("8 小时"), tr("24 小时"), tr("72 小时")]).pack(side="left")
        tk.Button(row, text=tr("开始稳定性测试"), command=self._st_start).pack(side="left", padx=6)
        tk.Button(row, text=tr("停止"), command=lambda: self._do_action("stab_stop", refresh=False)).pack(side="left")
        tk.Label(sec, text=tr("多目标对比：在上面依次选好每个目标的参数，点“加入对比列表”，最多 16 个；"
                           "同一串口上的多个站会自动按顺序轮询（RS-485 不能同时通信），不同串口和网络目标真正并行。"
                           "结束后会分析各目标的失败是否同时发生。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x", pady=(8, 2))
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.mt_label = tk.StringVar()
        tk.Entry(row, textvariable=self.mt_label, width=16).pack(side="left")
        tk.Button(row, text=tr("加入对比列表"), command=self._mt_add).pack(side="left", padx=4)
        tk.Button(row, text=tr("开始多目标测试"), command=self._mt_start).pack(side="left", padx=4)
        tk.Button(row, text=tr("清空列表"), command=self._mt_clear).pack(side="left", padx=4)
        self.mt_lb = tk.Listbox(sec, height=4, exportselection=False)
        self.mt_lb.pack(fill="x", pady=4)
        self.st_view = scrolledtext.ScrolledText(sec, height=10, state="disabled", wrap="word")
        self.st_view.pack(fill="x", pady=4)
        self.st_canvas = tk.Canvas(sec, height=150, bg="#fafbfc", highlightthickness=1, highlightbackground="#eee")
        self.st_canvas.pack(fill="x", pady=4)
        row = tk.Frame(sec)
        row.pack(fill="x")
        self.st_csv = tk.StringVar(value="")
        tk.Label(row, textvariable=self.st_csv, fg="#777").pack(side="left")
        tk.Button(row, text=tr("打开明细 CSV"), command=self._open_csv).pack(side="left", padx=6)
        self._st_csv_name = ""
        self._nt_show("ping")

    # ---- 连接测试表单 ----
    def _nt_show(self, kind):
        tk, ttk = self.tk, self.ttk
        self._nt_kind = kind
        for k, b in self._nt_kind_btns.items():
            b.configure(relief="sunken" if k == kind else "raised")
        self.nt_tip.set(tr(NETTEST_SPECS[kind].get("tip", "")))
        for child in self.nt_form.winfo_children():
            child.destroy()
        self._nt_vars = {}
        fields = nettest_fields(kind)
        if not fields:
            tk.Label(self.nt_form, text=tr("无需参数，直接点“开始测试”。"), fg="#777").pack(anchor="w")
        for key, label, default, typ in fields:
            row = tk.Frame(self.nt_form)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=tr(label), width=26, anchor="w").pack(side="left")
            if typ == "chk":
                var = tk.BooleanVar(value=False)
                w = tk.Checkbutton(row, variable=var)
            elif typ == "password":
                var = tk.StringVar(value="")
                w = tk.Entry(row, textvariable=var, show="*")
            elif typ == "fc":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly",
                                 values=["1", "2", "3", "4"])
                tk.Label(row, text=tr("（01 线圈 / 02 离散输入 / 03 保持寄存器 / 04 输入寄存器）"),
                         fg="#777").pack(side="right")
            elif typ == "mode":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly", values=["hex", "text"])
            elif typ == "baud":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var,
                                 values=["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"])
            elif typ == "bits":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly", values=["8", "7"])
            elif typ == "parity":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly", values=["N", "E", "O"])
            elif typ == "stop":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly", values=["1", "2"])
            elif typ == "port":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, values=self._nt_ports)
                tk.Button(row, text=tr("扫描串口"), command=self._nt_scan).pack(side="right", padx=4)
            else:
                var = tk.StringVar(value=default)
                w = tk.Entry(row, textvariable=var)
            w.pack(side="left", fill="x", expand=True)
            self._nt_vars[key] = (var, w, typ)

    def _nt_params(self):
        raw = {}
        for key, (var, _w, typ) in self._nt_vars.items():
            raw[key] = bool(var.get()) if typ == "chk" else var.get()
        return build_nettest_params(self._nt_kind, raw)

    def _nt_scan(self):
        def done(result, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            self._render_nettest_result({"test": result, "msg": result.get("summary", "")})
            if result.get("ok"):
                self._nt_ports = [d.split()[0] for d in result.get("details", []) if d.split()]
                cur = self._nt_vars.get("port", (None,))[0]
                cur_val = cur.get() if cur else ""
                self._nt_show(self._nt_kind)
                if "port" in self._nt_vars:
                    self._nt_vars["port"][0].set(cur_val or (self._nt_ports[0] if self._nt_ports else ""))
        self._bg(lambda: self.logic.nettest("serial_list", {}), done)

    def _render_nettest_result(self, j):
        t = (j or {}).get("test")
        if not t:
            self._set_text(self.nt_out, (j or {}).get("msg") or tr("测试失败"))
            return
        text = ("✓ " if t["ok"] else "✗ ") + t["summary"]
        if t.get("details"):
            text += "\n" + "\n".join(t["details"])
        self._set_text(self.nt_out, text)
        self.nt_time.set(tr("耗时 %s ms") % t.get("elapsed_ms", 0))

    def _nt_run(self):
        kind, params = self._nt_kind, self._nt_params()
        self.nt_btn.configure(state="disabled", text=tr("测试中…"))
        self.nt_time.set("")

        def done(result, err):
            self.nt_btn.configure(state="normal", text=tr("开始测试"))
            if err is not None:
                self._render_nettest_result({"msg": str(err)})
                return
            self._render_nettest_result({"test": result, "msg": result.get("summary", "")})
        self._bg(lambda: self.logic.nettest(kind, params), done)

    def _pr_add(self):
        if self._nt_kind not in PROBE_KINDS:
            return self._show(False, tr("“%s”不能做设备检测，请选 Ping / TCP / DNS / OPC UA / Modbus / 串口收发。")
                              % tr(NETTEST_SPECS[self._nt_kind]["t"]))
        label = self.pr_label.get().strip()
        if not label:
            return self._show(False, tr("请填写设备检测的名称。"))
        old = next((x for x in self._pr_list if x["id"] == self._pr_editing), None)
        body = {"probe": probe_body(self._pr_editing, label, self._nt_kind, self._nt_params(),
                                    self.pr_iv.get(), self.pr_thr.get(), untr(self.pr_lv.get()),
                                    old["enabled"] if old else True)}

        def done(msg, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            self.pr_label.set("")
            self._pr_editing = ""
            self._show(True, str(msg))
            self._poll_probes_now()
        self._bg(lambda: self.logic.action("probe_save", body), done)

    # ---- Modbus Poll / Slave（v2.8） ----
    def _mbp_raw(self):
        return {"transport": self.mbp_transport.get(), "host": self.mbp_host.get(),
                "port": self.mbp_port.get(), "serial": self.mbp_serial.get(),
                "baud": self.mbp_baud.get(), "parity": self.mbp_parity.get(),
                "stopbits": self.mbp_stopbits.get(), "unit": self.mbp_unit.get(),
                "func": self.mbp_func.get(), "addr": self.mbp_addr.get(),
                "count": self.mbp_count.get(), "dtype": self.mbp_dtype.get(),
                "order": self.mbp_order.get(), "values": self.mbp_values.get(),
                "interval": self.mbp_interval.get()}

    @staticmethod
    def _mbp_master(p):
        import modbus_tools as mt
        if p["transport"] == "tcp":
            return mt.TcpMaster(p["host"], p["port"], unit=p["unit"], timeout=3.0)
        return mt.RtuMaster(p["serial"], baud=p["baud"], parity=p["parity"],
                            stopbits=p["stopbits"], unit=p["unit"], timeout=2.0)

    def _mbp_once(self):
        import modbus_tools as mt
        try:
            p = build_modbus_poll_params(self._mbp_raw())
        except ValueError as e:
            return self._show(False, tr_msg(e))
        values = None
        if p["func"] in mt.WRITE_FUNCS:
            try:
                values = mt.parse_values(p["values_text"], p["func"], p["dtype"], p["order"])
            except Exception as e:  # noqa
                return self._show(False, tr_msg(e))
            target = ("%s:%s" % (p["host"], p["port"])) if p["transport"] == "tcp" else p["serial"]
            # 写操作防误点：弹窗确认目标、站号、地址和数值
            if not self.mb.askyesno(
                    tr("确认写入"),
                    tr("将向 %s 站号 %d 地址 %d 执行%s：\n值 %s\n\n确认写入吗？") % (
                        target, p["unit"], p["addr"], mt.FUNC_NAMES[p["func"]], p["values_text"]),
                    parent=self.root):
                return

        def work():
            m = self._mbp_master(p)
            try:
                m.connect()
                if p["func"] in mt.WRITE_FUNCS:
                    r = m.write(p["func"], p["addr"], values)
                else:
                    r = m.read(p["func"], p["addr"], p["count"])
            finally:
                m.close()
            r["_addr"], r["_dtype"], r["_order"] = p["addr"], p["dtype"], p["order"]
            return r
        self._bg(work, self._mbp_render)

    def _mbp_render(self, result, err):
        if err is not None:
            return self._show(False, tr_msg(err))
        import modbus_tools as mt
        lines = [("✓ " if result["ok"] else "✗ ") + result["summary"]] + list(result["details"])
        if result["ok"] and result.get("values") is not None:
            lines += mt.format_read_values(result["values"], result["kind"],
                                           result.get("_addr", 0), result.get("_dtype", "uint16"),
                                           result.get("_order", "ABCD"))
        self._set_text(self.mbp_out, "\n".join(lines))

    def _mbp_toggle_poll(self):
        if self._mbp_polling:
            if self._mbp_stop is not None:
                self._mbp_stop.set()
            self.mbp_poll_btn.configure(text=tr("开始轮询"))
            return
        import modbus_tools as mt
        try:
            p = build_modbus_poll_params(self._mbp_raw())
        except ValueError as e:
            return self._show(False, tr_msg(e))
        if p["func"] in mt.WRITE_FUNCS:
            return self._show(False, tr("轮询只用于读功能码；写操作请用“写入（确认后执行）”。"))
        self._mbp_stop = threading.Event()
        self._mbp_polling = True
        self.mbp_poll_btn.configure(text=tr("停止轮询"))

        def work():
            m = self._mbp_master(p)
            try:
                m.connect()

                def cb(r):
                    r["_addr"], r["_dtype"], r["_order"] = p["addr"], p["dtype"], p["order"]
                    self._q.put((self._mbp_render, r, None))
                mt.poll(m, p["func"], p["addr"], p["count"], interval_sec=p["interval"],
                        stop_event=self._mbp_stop, callback=cb)
            except Exception as e:  # noqa
                self._q.put((self._mbp_render, None, e))
            finally:
                m.close()
                self._q.put((self._mbp_poll_done, None, None))
        threading.Thread(target=work, daemon=True).start()

    def _mbp_poll_done(self, _result, _err):
        self._mbp_polling = False
        try:
            self.mbp_poll_btn.configure(text=tr("开始轮询"))
        except Exception:  # noqa
            pass

    def _mbs_raw(self):
        return {"transport": self.mbs_transport.get(), "host": self.mbs_host.get(),
                "tcp_port": self.mbs_tcp_port.get(), "serial": self.mbs_serial.get(),
                "baud": self.mbs_baud.get(), "parity": self.mbs_parity.get(),
                "stopbits": self.mbs_stopbits.get(), "unit": self.mbs_unit.get(),
                "size": self.mbs_size.get()}

    def _mbs_store_or_new(self, size):
        import modbus_tools as mt
        if self._mbs_store is None or self._mbs_store.size != size:
            self._mbs_store = mt.DataStore(size=size)
        return self._mbs_store

    def _mbs_toggle(self):
        if self._mbs_slave is not None:
            return self._mbs_stop()
        import modbus_tools as mt
        try:
            p = build_modbus_slave_params(self._mbs_raw())
        except ValueError as e:
            return self._show(False, tr_msg(e))
        store = self._mbs_store_or_new(p["size"])
        self._mbs_logs = []
        self._mbs_log_idx = 0
        self._set_text(self.mbs_log, "")
        try:
            if p["transport"] == "tcp":
                slave = mt.TcpSlave(store, host=p["host"], port=p["tcp_port"],
                                    unit=p["unit"], log=self._mbs_logs.append)
            else:
                slave = mt.RtuSlave(store, p["serial"], baud=p["baud"], parity=p["parity"],
                                    stopbits=p["stopbits"], unit=p["unit"], log=self._mbs_logs.append)
            slave.start()
        except Exception as e:  # noqa
            return self._show(False, tr_msg(e))
        self._mbs_slave = slave
        self.mbs_status.set(tr("运行中：监听 %s（手动启动，关窗自动停）") % slave.address)
        self.mbs_btn.configure(text=tr("停止从站"))
        self._mbs_pump()

    def _mbs_stop(self):
        if self._mbs_slave is not None:
            try:
                self._mbs_slave.stop()
            except Exception:  # noqa
                pass
            self._mbs_slave = None
        self.mbs_status.set(tr("已停止（默认不监听）"))
        try:
            self.mbs_btn.configure(text=tr("启动从站"))
        except Exception:  # noqa
            pass

    def _mbs_pump(self):
        if self._closed or self._mbs_slave is None:
            return
        new = []
        while self._mbs_log_idx < len(self._mbs_logs):
            e = self._mbs_logs[self._mbs_log_idx]
            self._mbs_log_idx += 1
            new.append(tr("[%s] %s 站号 %s 功能码 %s 请求 %s 应答 %s%s") % (
                e["time"], e["transport"], e["unit"], e["func"], e["req_hex"],
                e["resp_hex"] or tr("（无）"), ("  " + e["note"]) if e["note"] else ""))
        if new:
            self.mbs_log.configure(state="normal")
            self.mbs_log.insert("end", "\n".join(new) + "\n")
            self.mbs_log.see("end")
            self.mbs_log.configure(state="disabled")
        self.root.after(500, self._mbs_pump)

    def _mbs_read_area(self):
        import modbus_tools as mt
        try:
            p = build_modbus_slave_params(self._mbs_raw())
            store = self._mbs_store_or_new(p["size"])
            area = MODBUS_AREA_LABELS[untr(self.mbs_area.get())]
            addr = _mb_int({"v": self.mbs_addr.get()}, "v", tr("地址"), 0, 65535, 0)
            count = _mb_int({"v": self.mbs_count.get()}, "v", tr("数量"), 1, 2000, 10)
            if area in ("coils", "discrete"):
                vals = store.read_bits(area, addr, count)
                lines = mt.format_read_values(vals, tr("位"), addr)
            else:
                vals = store.read_regs(area, addr, count)
                lines = mt.format_read_values(vals, tr("寄存器"), addr,
                                              self.mbs_dtype.get(), self.mbs_order.get())
        except Exception as e:  # noqa
            return self._show(False, tr_msg(e))
        self._set_text(self.mbs_area_out, "\n".join(lines))

    def _mbs_write_area(self):
        import modbus_tools as mt
        try:
            p = build_modbus_slave_params(self._mbs_raw())
            store = self._mbs_store_or_new(p["size"])
            area = MODBUS_AREA_LABELS[untr(self.mbs_area.get())]
            addr = _mb_int({"v": self.mbs_addr.get()}, "v", tr("地址"), 0, 65535, 0)
            if area in ("coils", "discrete"):
                bits = mt.parse_values(self.mbs_value.get(), 15)
                store.write_bits(area, addr, bits)
                msg = tr("已写入%s 地址 %d 起 %d 个位：%s") % (self.mbs_area.get(), addr, len(bits), bits)
            else:
                regs = mt.parse_values(self.mbs_value.get(), 16,
                                       self.mbs_dtype.get(), self.mbs_order.get())
                store.write_regs(area, addr, regs)
                msg = tr("已写入%s 地址 %d 起 %d 个寄存器：%s") % (
                    self.mbs_area.get(), addr, len(regs), " ".join("0x%04X" % r for r in regs))
        except Exception as e:  # noqa
            return self._show(False, tr_msg(e))
        self._set_text(self.mbs_area_out, msg)

    # ---- 驱动检测 ----
    def _dc_run(self):
        self.dc_btn.configure(state="disabled", text=tr("检测中（约 10～30 秒）…"))

        def done(result, err):
            self.dc_btn.configure(state="normal", text=tr("开始检测"))
            if err is not None:
                return self._show(False, tr_msg(err))
            d = (result or {}).get("drivercheck")
            if not d:
                return self._show(False, tr("检测失败"))
            self._dc_show(d, tr("刚刚"))
            self._poll_env()
        self._bg(lambda: self.logic.action("drivercheck"), done)

    def _dc_show(self, d, when=""):
        order = {"问题": 0, "注意": 1, "正常": 2}
        self.dc_time.set(tr("%s · 耗时 %s ms%s") % (d["summary"], d["elapsed_ms"], " · " + when if when else ""))
        lines = []
        for it in sorted(d["items"], key=lambda x: order.get(x["level"], 3)):
            lines.append(tr("[%s] %s：%s") % (it["level"], it["name"], it["text"]))
            if it.get("fix"):
                lines.append(tr("    处理：%s") % it["fix"])
        self._set_text(self.dc_out, "\n".join(lines))

    def _poll_env(self):
        def done(e, err):
            if err is not None or not e:
                if not e:
                    self.env_last.set(tr("服务未运行：自动检测只在服务运行时进行，可以点“开始检测”手动检查。"))
                return
            if e.get("running") and not e.get("result"):
                self.env_last.set(tr("正在进行启动检测…"))
                self.root.after(3000, self._poll_env)
                return
            if not e.get("result"):
                self.env_last.set(tr("启动检测还没开始（服务启动约 8 秒后进行，或已在设置里关闭）。"))
                self.root.after(5000, self._poll_env)
                return
            self.env_last.set(tr("上次自动检测：%s · %s") % (e["time"], e["result"]["summary"]))
            if self._dc_shown_time != e["time"] and not self.dc_out.get("1.0", "end").strip():
                self._dc_show(e["result"], e["time"])
            self._dc_shown_time = e["time"]
        self._bg(self.logic.env, done)

    # ---- 稳定性测试 ----
    _ST_MINUTES = {"10 分钟": "10", "1 小时": "60", "8 小时": "480", "24 小时": "1440", "72 小时": "4320"}

    def _st_start(self):
        if self._nt_kind not in STAB_KINDS:
            return self._show(False, tr("“%s”不支持长时间测试，请先选 Ping / TCP / DNS / OPC UA / Modbus / 串口收发。")
                              % tr(NETTEST_SPECS[self._nt_kind]["t"]))
        body = {"kind": self._nt_kind, "params": self._nt_params(), "interval": self.st_iv.get(),
                "minutes": self._ST_MINUTES.get(untr(self.st_min.get()), "10")}

        def done(msg, err):
            self._show(err is None, str(msg if err is None else err))
            self._poll_stability_now()
        self._bg(lambda: self.logic.action("stab_start", body), done)

    def _mt_add(self):
        if self._nt_kind not in STAB_KINDS:
            return self._show(False, tr("“%s”不支持长时间测试。") % tr(NETTEST_SPECS[self._nt_kind]["t"]))
        if len(self._mt_list) >= 16:
            return self._show(False, tr("最多 16 个目标。"))
        params = self._nt_params()
        params.pop("password", None)
        self._mt_list.append({"kind": self._nt_kind, "params": params,
                              "label": self.mt_label.get().strip() or tr("目标%d") % (len(self._mt_list) + 1)})
        self.mt_label.set("")
        self._mt_show()
        self._show(True, tr("已加入对比列表，共 %d 个。") % len(self._mt_list))

    def _mt_show(self):
        self.mt_lb.delete(0, "end")
        for t in self._mt_list:
            self.mt_lb.insert("end", "%s · %s" % (t["label"], tr(NETTEST_SPECS[t["kind"]]["t"])))
        if not self._mt_list:
            self.mt_lb.insert("end", tr("（列表为空）"))

    def _mt_clear(self):
        self._mt_list = []
        self._mt_show()

    def _mt_start(self):
        if len(self._mt_list) < 2:
            return self._show(False, tr("多目标对比至少需要 2 个目标（只有 1 个请直接点“开始稳定性测试”）。"))
        body = {"targets": self._mt_list, "interval": self.st_iv.get(),
                "minutes": self._ST_MINUTES.get(untr(self.st_min.get()), "10")}

        def done(msg, err):
            self._show(err is None, str(msg if err is None else err))
            self._poll_stability_now()
        self._bg(lambda: self.logic.action("stab_start", body), done)

    def _poll_stability_now(self):
        self._bg(self.logic.stability, lambda s, e: None if e else self._st_render(s))

    def _poll_stability(self):
        def done(s, err):
            if err is None:
                self._st_render(s)
            delay = 2000 if s and s.get("state") == "running" else 15000
            if not self._closed:
                self.root.after(delay, self._poll_stability)
        self._bg(self.logic.stability, done)

    def _st_render(self, s):
        if not s:
            self._set_text(self.st_view, "")
            self.st_canvas.delete("all")
            self.st_csv.set("")
            self._st_csv_name = ""
            return
        if s.get("multi"):
            self._mt_render(s)
            return
        state_txt = {"running": tr("运行中"), "finished": tr("已完成"), "stopped": tr("已手动停止"),
                     "error": tr("出错：") + str(s.get("error"))}.get(s["state"], s["state"])

        def num(v, u=""):
            return "—" if v is None else "%s%s" % (v, u)
        lines = [tr("%s %s · %s · %s / %s · 每 %s 秒") % (s["kind_name"], s["target"], state_txt,
                                                     s["elapsed_text"], s["total_text"], s["interval"]),
                 tr("进度 %s%%") % s["progress"],
                 s["verdict"],
                 tr("测试次数 %s，成功 %s，失败 %s，失败率 %s，最长连续失败 %s%s") % (
                     s["n"], s["ok"], s["fail"], num(s["loss_pct"], "%"), s["max_streak"],
                     tr("（%s 起）") % s["max_streak_at"] if s.get("max_streak_at") else ""),
                 tr("最小 %s，平均 %s，P95 %s，P99 %s，最大 %s，抖动 %s") % (
                     num(s["min"], " ms"), num(s["avg"], " ms"), num(s["p95"], " ms"),
                     num(s["p99"], " ms"), num(s["max"], " ms"), num(s["jitter"], " ms"))]
        if s.get("errors"):
            lines.append(tr("错误分布：") + tr("；").join(tr("%d 次：%s") % (e["count"], e["text"]) for e in s["errors"]))
        if s.get("hints"):
            lines.append(tr("排查建议：") + tr("；").join(s["hints"]))
        self._set_text(self.st_view, "\n".join(lines))
        self._st_chart(s)
        self._st_csv_name = s.get("csv") or ""
        self.st_csv.set(tr("明细 CSV：logs/stability/%s（%s）") % (self._st_csv_name,
                        tr("每 20 次写盘一次") if s["state"] == "running" else tr("已保存")) if self._st_csv_name else "")

    def _mt_render(self, s):
        state_txt = {"running": tr("运行中"), "finished": tr("已完成"), "stopped": tr("已手动停止")}.get(s["state"], s["state"])
        lines = [tr("多目标稳定性测试 · %d 个目标 · %s · %s / %s · 每 %s 秒") % (
            s["count"], state_txt, s["elapsed_text"], s["total_text"], s["interval"]),
            tr("进度 %s%%") % s["progress"], s["verdict"]]
        lines += list(s.get("notes") or [])
        for r in s["rows"]:
            lines.append(tr("%s（%s %s）：%d 次，失败率 %s%%，最长连续失败 %s，平均/P95/最大 %s/%s/%s ms") % (
                r["label"], r["kind_name"], r["target"], r["n"], r["loss_pct"], r["max_streak"],
                r["avg"], r["p95"], r["max"]))
            if r.get("errors"):
                lines.append(tr("    %d 次：%s") % (r["errors"][0]["count"], r["errors"][0]["text"]))
        self._set_text(self.st_view, "\n".join(lines))
        self.st_canvas.delete("all")
        self._st_csv_name = s.get("summary_csv") or ""
        self.st_csv.set(tr("对比汇总 CSV：logs/stability/%s") % self._st_csv_name if self._st_csv_name else "")

    def _st_chart(self, s):
        c = self.st_canvas
        c.delete("all")
        buckets = s.get("buckets") or []
        if not buckets:
            return
        w = max(c.winfo_width(), 600)
        h, pad = 130, 34
        n = max(1, int(s["duration"] / s["bucket_sec"]) if s.get("bucket_sec") else len(buckets))
        mx = max([1] + [b.get("max") or 0 for b in buckets])
        c.create_text(2, 8, text="%.0fms" % mx, anchor="nw", fill="#666", font=("Microsoft YaHei", 8))
        c.create_line(pad, h, w, h, fill="#ccc")
        pts = []
        for b in buckets:
            x = pad + (b["i"] / n) * (w - pad - 5)
            bw = max(1.5, (w - pad - 5) / n - 1)
            if b.get("fail"):
                bh = max(3, (b["fail"] / b["n"]) * (h - 20)) if b["n"] else 3
                c.create_rectangle(x, h - bh, x + bw, h, fill="#dc3545", outline="")
            if b.get("avg") is not None:
                pts += [x + bw / 2, h - (b["avg"] / mx) * (h - 20)]
        if len(pts) >= 4:
            c.create_line(*pts, fill="#1f4e79", width=2)
        c.create_text(pad, h + 4, text="0", anchor="nw", fill="#666", font=("Microsoft YaHei", 8))
        c.create_text(w - 70, h + 4, text=s.get("total_text", ""), anchor="nw", fill="#666",
                      font=("Microsoft YaHei", 8))
        c.create_text(w / 2 - 130, h + 4, text=tr("蓝线 = 平均延迟，红柱 = 失败比例（每段 %s 秒）") % s.get("bucket_sec"),
                      anchor="nw", fill="#666", font=("Microsoft YaHei", 8))

    def _open_csv(self):
        path = self.logic.stab_csv_path(self._st_csv_name)
        if not path or not os.path.isfile(path):
            return self._show(False, tr("还没有明细 CSV 文件。"))
        try:
            os.startfile(path)          # Windows
        except Exception:  # noqa
            self._show(True, tr("明细 CSV 在：%s") % path)

    # ---- 标签页 4：系统设置 ----
    def _build_system_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        from tkinter import scrolledtext
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("系统设置"))
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, tr("数据源"))
        self._field(sec, "watch_dirs", tr("监控目录（每行一个）"),
                    tr("存放 *_V3.db / *.mbd 的文件夹，必须真实存在，例如 D:\\GCP\\DataBrowser"), "text")
        self._field(sec, "poll_seconds", tr("扫描周期（秒）"), "", "num")
        self._field(sec, "last_n", tr("每流路最近周期数"), "", "num")
        self._field(sec, "max_points", tr("单曲线最大发布点数"), "", "num")

        sec = self._section(inner, tr("OPC UA 服务"))
        self._field(sec, "endpoint", tr("监听地址 endpoint"), "", "str", restart=True)
        self._field(sec, "server_name", tr("服务器名称"), "", "str", restart=True)
        self._field(sec, "max_connections", tr("最大连接数"), "", "num", restart=True)
        self._field(sec, "security", tr("通信安全模式"), "", "select", SELECT_OPTIONS["security"], restart=True)
        self._field(sec, "allow_anonymous", tr("允许匿名只读连接"), "", "bool")
        self._field(sec, "allow_insecure_fallback", tr("保留不加密备用入口"), tr("仅排障用，用完请关闭"), "bool", restart=True)

        sec = self._section(inner, tr("CSV 导出"))
        self._field(sec, "remote_export", tr("允许远程调用 ExportCSV"), "", "bool")
        self._field(sec, "export_min_interval_sec", tr("远程导出最短间隔（秒）"), "", "num")
        self._field(sec, "csv_export_dir", tr("导出目录"), "", "str")

        sec = self._section(inner, tr("高级设置"))
        self._field(sec, "max_trace_points", tr("单条谱图最大采样点"), "", "num")
        self._field(sec, "max_file_mb", tr("单个 .mbd 上限（MB）"), "", "num")
        self._field(sec, "log_max_mb", tr("单个日志大小（MB）"), "", "num")
        self._field(sec, "log_backups", tr("保留历史日志个数"), "", "num")
        self._field(sec, "web_ui", tr("启用设置网页"), tr("桌面窗口是本机默认入口；网页仍可开启供远程访问"), "bool", restart=True)
        self._field(sec, "web_host", tr("设置网页监听地址"),
                    tr("127.0.0.1 = 只有本机能打开（推荐）；改成其它地址后必须用账号登录"), "str", restart=True)
        self._field(sec, "web_port", tr("设置网页端口"), "", "num", restart=True)
        self._field(sec, "web_https", "HTTPS",
                    tr("未指定证书时自动生成自签名证书；浏览器首次会提示“不安全”，核对指纹一致后继续即可"),
                    "select", SELECT_OPTIONS["web_https"], restart=True)
        self._field(sec, "web_cert_file", tr("自有证书文件（PEM）"), tr("留空 = 自动生成"), "str", restart=True)
        self._field(sec, "web_key_file", tr("自有私钥文件（PEM）"), tr("留空 = 自动生成"), "str", restart=True)

        sec = self._section(inner, tr("登录账号"))
        self.users_lb = tk.Listbox(sec, height=4, exportselection=False)
        self.users_lb.pack(fill="x", pady=4)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.u_name, self.u_pw, self.u_pw2 = tk.StringVar(), tk.StringVar(), tk.StringVar()
        tk.Entry(row, textvariable=self.u_name, width=16).pack(side="left", padx=3)
        tk.Entry(row, textvariable=self.u_pw, show="*", width=18).pack(side="left", padx=3)
        tk.Entry(row, textvariable=self.u_pw2, show="*", width=18).pack(side="left", padx=3)
        tk.Button(row, text=tr("添加 / 重设"), command=self._add_user).pack(side="left", padx=4)
        tk.Button(row, text=tr("删除选中账号"), command=self._remove_user).pack(side="left", padx=4)
        tk.Label(sec, text=tr("依次填：用户名、密码（至少 8 位）、再输入一次"), fg="#777", anchor="w").pack(fill="x")
        tk.Button(sec, text=tr("一键加固（开启加密 + 关闭匿名 + 用上面的账号）"),
                  command=self._harden).pack(anchor="w", pady=4)

        sec = self._section(inner, tr("安全体检"))
        self.report_text = scrolledtext.ScrolledText(sec, height=8, state="disabled", wrap="word")
        self.report_text.pack(fill="x", pady=4)
        self.cfg_path_var = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.cfg_path_var, fg="#888", anchor="w").pack(fill="x")

    def _pw_body(self):
        name, pw = self.u_name.get().strip(), self.u_pw.get()
        if not name:
            self._show(False, tr("请填写用户名。"))
            return None
        if pw != self.u_pw2.get():
            self._show(False, tr("两次输入的密码不一致。"))
            return None
        return {"name": name, "password": pw}

    def _add_user(self):
        body = self._pw_body()
        if body is None:
            return

        def done(msg, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            self.u_pw.set("")
            self.u_pw2.set("")
            self._show(True, str(msg))
            self._reload()
        self._bg(lambda: self.logic.action("add_user", body), done)

    def _remove_user(self):
        sel = self.users_lb.curselection()
        if not sel:
            return self._show(False, tr("请先选中要删除的账号。"))
        name = self.users_lb.get(sel[0])
        if name not in ("（无账号）", tr("（无账号）"), "(no accounts)") and self.mb.askyesno(tr("请确认"), tr("确定删除账号 %s 吗？") % name, parent=self.root):
            self._do_action("remove_user", {"name": name})

    def _harden(self):
        body = self._pw_body()
        if body is None:
            return
        if not self.mb.askyesno(tr("请确认"), tr("一键加固会开启加密、关闭匿名访问。现有 DCS/OPC 客户端需要同步改成"
                                          "加密+账号登录，否则会连不上。继续吗？"), parent=self.root):
            return

        def done(msg, err):
            if err is not None:
                return self._show(False, tr_msg(err))
            self.u_pw.set("")
            self.u_pw2.set("")
            self._show(True, str(msg))
            self._reload()
        self._bg(lambda: self.logic.action("harden", body), done)

    # =====================================================================
    # v3.0：通道数据 / 谱图预览 / 客户端连接 / 常用设置 / 告警桌面提醒
    # （计算都在 desktop_features，这里只画界面）
    # =====================================================================
    def _build_channels_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("通道数据"))
        self.tab_channels = tab
        top = tk.Frame(tab)
        top.pack(fill="x", padx=8, pady=(8, 2))
        self.rec_note = tk.StringVar(value=tr("正在读取……"))
        tk.Label(top, textvariable=self.rec_note, anchor="w", fg="#555").pack(side="left", fill="x", expand=True)
        tk.Button(top, text=tr("在谱图预览中查看"), command=self._chd_open).pack(side="right", padx=4)
        tk.Button(top, text=tr("立即刷新"), command=lambda: self._refresh_records(force=True)).pack(side="right", padx=4)
        box = tk.Frame(tab)
        box.pack(fill="both", expand=True, padx=8, pady=4)
        cols = tuple(c for c, _t, _w in df.CHANNEL_COLUMNS)
        self.chd_tree = ttk.Treeview(box, columns=cols, show="headings", height=16)
        for col, title, width in df.CHANNEL_COLUMNS:
            self.chd_tree.heading(col, text=tr(title))
            self.chd_tree.column(col, width=width, anchor="w")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.chd_tree.yview)
        self.chd_tree.configure(yscrollcommand=bar.set)
        self.chd_tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.chd_tree.tag_configure("alert", foreground="#cf222e")
        self.chd_tree.tag_configure("offline", foreground="#6e7781")
        self.chd_tree.tag_configure("muted", foreground="#9a6700")
        self.chd_tree.bind("<Double-1>", self._chd_open)
        self.chd_tree.bind("<Return>", self._chd_open)
        tk.Label(tab, text=tr("每 5 秒自动刷新。服务运行中显示正在发布的数据和告警；服务没运行时直接读数据目录（不发布）。"
                           "双击一行打开该通道的谱图曲线。"), fg="#777", anchor="w").pack(fill="x", padx=8, pady=(0, 6))

    def _build_preview_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("谱图预览"))
        self.tab_preview = tab
        top = tk.Frame(tab)
        top.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(top, text=tr("通道：")).pack(side="left")
        self.pv_combo = ttk.Combobox(top, values=[], state="readonly", width=72)
        self.pv_combo.pack(side="left", padx=4)
        self.pv_combo.bind("<<ComboboxSelected>>", self._pv_on_combo)
        self.pv_series = tk.StringVar(value="raw")
        tk.Label(top, text=tr("  显示：")).pack(side="left")
        for key, text in df.SERIES:
            tk.Radiobutton(top, text=tr(text), value=key, variable=self.pv_series,
                           command=lambda: self._pv_draw(force=True)).pack(side="left")
        tk.Button(top, text=tr("刷新"), command=lambda: self._refresh_records(force=True)).pack(side="right", padx=4)
        self.pv_note = tk.StringVar(value="")
        tk.Label(tab, textvariable=self.pv_note, anchor="w", fg="#555").pack(fill="x", padx=8)
        self.pv_canvas = tk.Canvas(tab, bg="white", highlightthickness=1, highlightbackground="#d0d7de")
        self.pv_canvas.pack(fill="both", expand=True, padx=8, pady=4)
        self.pv_canvas.bind("<Configure>", lambda _e: self._pv_draw(force=True))
        self.pv_info = tk.StringVar(value="")
        tk.Label(tab, textvariable=self.pv_info, anchor="w", fg="#333", justify="left",
                 wraplength=980).pack(fill="x", padx=8, pady=(0, 6))
        self._build_peaks_panel(tab)           # v3.1 峰识别与峰表

    def _build_clients_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("客户端连接"))
        self.tab_clients = tab
        top = tk.Frame(tab)
        top.pack(fill="x", padx=8, pady=(8, 2))
        self.cl_sum = tk.StringVar(value=tr("服务未运行：没有客户端连接"))
        tk.Label(top, textvariable=self.cl_sum, anchor="w", font=("Microsoft YaHei", 10, "bold")).pack(side="left")
        tk.Button(top, text=tr("刷新"), command=self._refresh_sessions).pack(side="right", padx=4)
        cols = ("addr", "name", "user", "since", "state")
        self.cl_tree = ttk.Treeview(tab, columns=cols, show="headings", height=14)
        for col, title, width in (("addr", tr("客户端地址"), 180), ("name", tr("会话名"), 260), ("user", tr("用户"), 150),
                                  ("since", tr("连接时间"), 160), ("state", tr("状态"), 100)):
            self.cl_tree.heading(col, text=title)
            self.cl_tree.column(col, width=width, anchor="w")
        self.cl_tree.pack(fill="both", expand=True, padx=8, pady=4)
        tk.Label(tab, text=tr("每 5 秒自动刷新。连接时间为本窗口第一次看到该连接的时刻；连接数达到上限（系统设置 → 最大连接数）"
                           "后新的客户端会被拒绝。会话信息取自 OPC UA 服务端内部，个别 asyncua 版本取不到时显示“无法获取”。"),
                 fg="#777", anchor="w", justify="left", wraplength=960).pack(fill="x", padx=8, pady=(0, 6))

    def _build_quick_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("常用设置"))
        self.tab_quick = tab
        inner = _scrollable(tab, tk, ttk)
        sec = self._section(inner, tr("常用配置"))
        tk.Label(sec, text=tr("最常改的几项集中在这里。保存时用与服务启动完全相同的规则校验，不合法不会写入并说明原因；"
                           "保存后会告诉你哪些自动生效、哪些需要重启服务。其余设置在“告警与通知”“系统设置”页。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        for f in df.QUICK_FIELDS:
            self._qfield(sec, *f)
        sec = self._section(inner, tr("告警桌面提醒"))
        for f in df.DESKTOP_FIELDS[:3]:
            self._qfield(sec, *f)
        tk.Button(sec, text=tr("发送一条测试提醒"), command=self._test_notify).pack(anchor="w", pady=4)
        sec = self._section(inner, tr("启动与登录"))
        self.login_var = tk.BooleanVar(value=df.login_open_enabled())
        self._row(sec, tr("登录 Windows 后自动打开窗口"),
                  lambda r: tk.Checkbutton(r, variable=self.login_var, command=self._toggle_login),
                  tr("勾选后在当前用户的“启动”文件夹（shell:startup）放一个快捷方式，登录后自动打开本窗口；"
                  "取消勾选即删除。这只是登录后打开可见的桌面窗口，不是后台服务，也不是计划任务。"))
        self.login_path = tk.StringVar(value=tr("快捷方式位置：%s") % (df.startup_shortcut_path() or tr("（找不到启动文件夹）")))
        tk.Label(sec, textvariable=self.login_path, fg="#888", anchor="w").pack(fill="x", padx=(190, 0))
        for f in df.DESKTOP_FIELDS[3:]:
            self._qfield(sec, *f)
        self._build_lang_section(inner)        # v3.1
        bar = tk.Frame(inner)
        bar.pack(fill="x", padx=6, pady=8)
        tk.Button(bar, text=tr("保存"), width=10, command=self._qs_save).pack(side="left", padx=4)
        tk.Button(bar, text=tr("保存并重启服务"), command=lambda: self._qs_save(restart=True)).pack(side="left", padx=4)
        tk.Button(bar, text=tr("恢复为已保存的值"), command=self._reload).pack(side="left", padx=4)
        self.qs_msg = tk.StringVar(value="")
        tk.Label(inner, textvariable=self.qs_msg, fg="#1f4e79", anchor="w", justify="left",
                 wraplength=860).pack(fill="x", padx=10, pady=(0, 8))

    QS_RESTART = ("opc_port", "web_port", "web_ui")

    def _qfield(self, parent, key, label, kind, opt=None, hint=""):
        tk, ttk = self.tk, self.ttk
        label, hint = tr(label), tr(hint)
        if key in self.QS_RESTART:
            label += tr(RESTART_TAG)
        if kind == "bool":
            var = tk.BooleanVar(value=False)
            w = self._row(parent, label, lambda r: tk.Checkbutton(r, variable=var), hint)
        elif kind == "select":
            var = tk.StringVar(value="")
            w = self._row(parent, label, lambda r: ttk.Combobox(r, values=[tr(t) for _v, t in opt], state="readonly"), hint)
            w._values = [v for v, _t in opt]
        else:
            var = tk.StringVar(value="")
            w = self._row(parent, label, lambda r: tk.Entry(r, textvariable=var), hint)
        self._qvars[key] = (var, w, kind)
        return w

    def _qs_values(self):
        out = {}
        for key, (var, w, kind) in self._qvars.items():
            if kind == "bool":
                out[key] = bool(var.get())
            elif kind == "select":
                idx = w.current()
                out[key] = w._values[idx] if isinstance(idx, int) and 0 <= idx < len(w._values) else var.get()
            else:
                out[key] = var.get()
        return out

    def _qs_set(self, cfg):
        vals = df.quick_form_values(cfg)
        for key, (var, w, kind) in self._qvars.items():
            v = vals.get(key)
            if kind == "bool":
                var.set(bool(v))
            elif kind == "select":
                var.set(str(v))
                if v in w._values:
                    w.current(w._values.index(v))
            else:
                var.set("" if v is None else str(v))
        try:
            self.login_var.set(df.login_open_enabled())
        except Exception:  # noqa
            pass

    def _qs_save(self, restart=False):
        values = self._qs_values()

        def done(res, err):
            if err is not None:
                self.qs_msg.set(tr("保存失败：%s") % err)
                return self._show(False, tr("保存失败，配置文件没有改动：\n%s") % err)
            msg = res["message"]
            self.qs_msg.set(msg)
            self.sa.get_log().info("常用设置：%s", msg.replace("\n", " "))
            self._reload()
            if restart:
                if self.svc.running():
                    self._stop_service(then=self._start_service, ask=False)
                    msg += tr("\n\n正在重启服务（客户端会短暂断开几秒）……")
                else:
                    self._start_service()
                    msg += tr("\n\n服务原来没有运行，已按新设置启动。")
                return self._show(True, msg)
            if res["restart"] and self.svc.running():
                if self.mb.askyesno(tr("需要重启服务"), msg + tr("\n\n现在重启服务让这些设置生效吗？（客户端会短暂断开几秒）"),
                                    parent=self.root):
                    self._stop_service(then=self._start_service, ask=False)
                return None
            return self._show(True, msg)
        self._bg(lambda: self.logic.save_quick(values), done)

    def _toggle_login(self):
        want = bool(self.login_var.get())
        sa = self.sa

        def done(msg, err):
            self.login_var.set(df.login_open_enabled())
            if err is not None:
                return self._show(False, tr_msg(err))
            sa.get_log().info("登录后自动打开窗口：%s", "已开启" if want else "已关闭")
            self._show(True, str(msg))
        self._bg(lambda: df.set_login_open(want, df.current_shortcut_target(sa)), done)

    # ---- 通道数据 / 谱图快照 ----
    def _poll_records(self):
        if self._closed:
            return
        self._refresh_records()
        self.root.after(5000, self._poll_records)

    def _refresh_records(self, force=False):
        if self._rec_loading or self._closed:
            return
        self._rec_loading = True
        up = self.svc.is_up()
        try:
            mp = int((self._orig or self.cfg).get("max_points") or 20000)
        except (TypeError, ValueError):
            mp = 20000

        def load():
            dirs = [] if up else self.logic.watch_dirs()
            return self.records.get(up, dirs=dirs, last_n=1, max_points=mp, force=force)

        def done(res, err):
            self._rec_loading = False
            if err is not None:
                self.rec_note.set(tr("读取谱图数据失败：%s") % err)
                self.pv_note.set(self.rec_note.get())
                return
            recs, origin, note = res
            self._recs, self._rec_origin = recs, origin
            self.rec_note.set(note)
            self.pv_note.set(note)
            self._render_channel_table()
            self._pv_update_options()
            self._pv_draw()
        self._bg(load, done)

    def _render_channel_table(self):
        tree = self.chd_tree
        sel = [self._chd_keys.get(i) for i in tree.selection()]
        updated = self.ch_tracker.update(self._recs)
        rows = df.channel_rows(self._recs, self._last_alerts if self._rec_origin == "service" else None,
                               updated, self._rec_origin)
        for item in tree.get_children():
            tree.delete(item)
        self._chd_keys = {}
        if not rows:
            tree.insert("", "end", values=(tr("没有通道数据：%s") % self.rec_note.get(), "", "", "", "", "", "", ""))
            return
        for row in rows:
            iid = tree.insert("", "end", values=row["values"], tags=(row["tag"],))
            self._chd_keys[iid] = row["key"]
            if row["key"] in sel:
                try:
                    tree.selection_set(iid)
                except Exception:  # noqa
                    pass

    def _chd_open(self, event=None):
        tree = self.chd_tree
        iid = ""
        if event is not None and getattr(event, "y", None) is not None:
            try:
                iid = tree.identify_row(event.y)
            except Exception:  # noqa
                iid = ""
        if not iid:
            sel = tree.selection()
            iid = sel[0] if sel else ""
        key = self._chd_keys.get(iid)
        if not key:
            return self._show(False, tr("请先在表格里选中一个通道。")) if event is None else None
        self._pv_select_key(key)
        self.nb.select(self.tab_preview)

    # ---- 谱图预览 ----
    def _pv_update_options(self):
        opts = df.channel_options(self._recs)
        self._pv_keys = [k for k, _l in opts]
        self.pv_combo.configure(values=[l for _k, l in opts])
        if self._pv_key not in self._pv_keys:
            self._pv_key = self._pv_keys[0] if self._pv_keys else ""
        if self._pv_key:
            self.pv_combo.current(self._pv_keys.index(self._pv_key))
        else:
            self.pv_combo.set("")

    def _pv_select_key(self, key):
        self._pv_key = key
        if key in self._pv_keys:
            self.pv_combo.current(self._pv_keys.index(key))
        self._pv_draw(force=True)

    def _pv_on_combo(self, _event=None):
        idx = self.pv_combo.current()
        if isinstance(idx, int) and 0 <= idx < len(self._pv_keys):
            self._pv_select_key(self._pv_keys[idx])

    def _pv_draw(self, force=False):
        rec = df.find_record(self._recs, self._pv_key)
        sig = (df.record_signature(rec), self.pv_series.get())
        if not force and sig == self._pv_sig:
            return
        c = self.pv_canvas
        try:
            w, h = int(c.winfo_width()), int(c.winfo_height())
        except Exception:  # noqa
            return
        if w < 80 or h < 80:            # 窗口还没画出来：等 <Configure> 再画
            return
        self._pv_sig = sig
        lay = df.plot_layout(rec, self.pv_series.get(), w, h)
        c.delete("all")
        x0, y0, x1, y1 = lay["box"]
        font = ("Microsoft YaHei", 9)
        if lay["title"]:
            c.create_text(x0, 4, text=lay["title"], anchor="nw", font=("Microsoft YaHei", 10, "bold"), fill="#1f4e79")
        if lay["empty"]:
            c.create_text(w / 2, h / 2, text=lay["message"], fill="#777", font=("Microsoft YaHei", 11))
            self.pv_info.set(lay["info"])
            self._pv_draw_peaks(c, lay, None)
            return
        for px_, label in lay["xticks"]:
            c.create_line(px_, y0, px_, y1, fill="#eef1f4")
            c.create_line(px_, y1, px_, y1 + 4, fill="#57606a")
            c.create_text(px_, y1 + 6, text=label, anchor="n", font=font, fill="#57606a")
        for py_, label in lay["yticks"]:
            c.create_line(x0, py_, x1, py_, fill="#eef1f4")
            c.create_line(x0 - 4, py_, x0, py_, fill="#57606a")
            c.create_text(x0 - 6, py_, text=label, anchor="e", font=font, fill="#57606a")
        c.create_rectangle(x0, y0, x1, y1, outline="#8c959f")
        if len(lay["line"]) >= 4:
            c.create_line(*lay["line"], fill="#0969da", width=1)
        self._pv_draw_peaks(c, lay, rec)       # v3.1 标峰 + 峰表
        c.create_text((x0 + x1) / 2, h - 4, text=lay["x_title"], anchor="s", font=font, fill="#333")
        c.create_text(x0, y0 - 3, text=lay["y_title"], anchor="sw", font=font, fill="#333")
        self.pv_info.set(tr("%s · 绘制 %d 点（按像素抽点，保留峰值）") % (lay["info"], lay["drawn"]))

    # ---- 客户端连接 ----
    def _poll_sessions(self):
        if self._closed:
            return
        self._refresh_sessions()
        self.root.after(5000, self._poll_sessions)

    def _refresh_sessions(self):
        up = self.svc.is_up()
        try:
            res = self.conn_tracker.apply(df.collect_sessions(df.service_app(self.sa) if up else None))
        except Exception as e:  # noqa  任何版本差异都不能让窗口崩
            res = {"ok": False, "reason": tr("无法获取（%s）") % e, "sessions": []}
        self.cl_sum.set(df.sessions_summary(res) if up else tr("服务未运行：没有客户端连接（点窗口上方“启动服务”）"))
        tree = self.cl_tree
        for item in tree.get_children():
            tree.delete(item)
        rows = res.get("sessions") or []
        for s in rows:
            tree.insert("", "end", values=(s.get("address"), s.get("name"), s.get("user"),
                                           s.get("since", ""), s.get("state")))
        if up and not rows:
            tree.insert("", "end", values=(res.get("reason") or tr("当前没有客户端连接"), "", "", "", ""))

    # ---- 告警桌面提醒 ----
    def _hwnd(self):
        try:
            return int(self.root.wm_frame(), 16)      # Windows：窗口外框句柄（任务栏按钮属于它）
        except Exception:  # noqa
            try:
                return int(self.root.winfo_id())
            except Exception:  # noqa
                return 0

    def _minimize(self):
        try:
            self.root.iconify()
        except Exception:  # noqa
            pass

    def _notify_alerts(self, snap):
        try:
            new = self.notify_tracker.update(snap, self._orig or self.cfg)
        except Exception:  # noqa
            return None
        if not new:
            return None
        title, text, level = df.notification_text(new)
        return self._deliver(title, text, level)

    def _deliver(self, title, text, level="warning"):
        """任务栏闪烁 + 依次尝试：托盘气泡 → PowerShell 通知 → 窗口内置顶小提示框。返回实际用的方式。"""
        hwnd = self._hwnd()
        df.flash_window(hwnd)
        if self._tray is None:
            self._tray = df.TrayBalloon(hwnd, self.sa._resource(os.path.join("assets", "app.ico")))
        used = df.deliver_notification(title, text, level, [
            (tr("托盘气泡"), self._tray.show),
            (tr("Windows 通知"), lambda t, x, _l: df.powershell_toast(t, x)),
            (tr("窗口内提示框"), self._popup),
        ])
        self.sa.get_log().info("桌面提醒（%s）：%s", used or "失败", title)
        return used

    def _popup(self, title, text, level="warning"):
        tk = self.tk
        top = tk.Toplevel(self.root)
        top.title(title)
        try:
            top.attributes("-topmost", True)
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            top.geometry("380x170+%d+%d" % (max(0, int(sw) - 400), max(0, int(sh) - 240)))
        except Exception:  # noqa
            pass
        color = "#cf222e" if level == "error" else "#9a6700"
        tk.Label(top, text=title, fg=color, font=("Microsoft YaHei", 10, "bold"), anchor="w",
                 wraplength=360, justify="left").pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(top, text=text, anchor="w", justify="left", wraplength=360).pack(fill="both", expand=True, padx=10)
        bar = tk.Frame(top)
        bar.pack(fill="x", padx=10, pady=6)

        def view():
            try:
                self.root.deiconify()
                self.nb.select(0)
            except Exception:  # noqa
                pass
            close()

        def close():
            try:
                top.destroy()
            except Exception:  # noqa
                pass
        tk.Button(bar, text=tr("查看告警"), command=view).pack(side="right", padx=4)
        tk.Button(bar, text=tr("关闭"), command=close).pack(side="right", padx=4)
        top.after(15000, close)
        self._popups.append(top)
        return True

    def _test_notify(self):
        used = self._deliver(tr("色谱提取 OPC UA 服务：测试提醒"),
                             tr("这是一条测试提醒。新告警出现时会这样提醒你（任务栏同时闪烁）。"), "info")
        self._show(bool(used), tr("已发送测试提醒（方式：%s）。没看到的话，检查 Windows“通知”设置里是否允许通知。")
                   % used if used else tr("测试提醒没有发出去，详见运行日志。"))

    # =====================================================================
    # v3.0：通讯诊断（IP 扫描 / 端口扫描 / 串口扫描与诊断）。逻辑在 commscan.py，这里只画界面。
    # 所有扫描在工作线程里跑，有进度条和停止按钮；串口操作与设备检测共用 nettest 的串口锁。
    # =====================================================================
    def _build_comm_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text=tr("通讯诊断"))
        self.tab_comm = tab
        self._cs_stops, self._cs_running, self._cs_results = {}, {}, {}
        self.cs_prog, self.cs_status = {}, {}
        inner = _scrollable(tab, tk, ttk)
        tk.Label(inner, text=tr("只读探测：Ping、TCP 连接后立即断开、Modbus 读功能码 01～04，不会写任何寄存器。"
                             "扫描别人的网段前请先征得网络管理员同意（部分工控防火墙会把扫描当成攻击告警）。"),
                 fg="#9a6700", anchor="w", justify="left", wraplength=880).pack(fill="x", padx=8, pady=(6, 0))

        # ---- IP 扫描 ----
        sec = self._section(inner, tr("IP 扫描（在线主机）"))
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("范围")).pack(side="left")
        self.ip_range = tk.StringVar(value="192.168.1.0/24")
        tk.Entry(row, textvariable=self.ip_range, width=34).pack(side="left", padx=4)
        tk.Label(row, text=tr("Ping 超时(秒)")).pack(side="left")
        self.ip_timeout = tk.StringVar(value="1")
        tk.Entry(row, textvariable=self.ip_timeout, width=5).pack(side="left", padx=4)
        tk.Label(row, text=tr("并发数")).pack(side="left")
        self.ip_workers = tk.StringVar(value="64")
        tk.Entry(row, textvariable=self.ip_workers, width=5).pack(side="left", padx=4)
        self.ip_names = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text=tr("反向解析主机名"), variable=self.ip_names).pack(side="left", padx=4)
        tk.Label(sec, text=tr("范围写法：192.168.1.0/24、192.168.1.10-192.168.1.50、192.168.1.10-50，多段用逗号隔开；"
                           "一次最多 %d 个地址。对方禁 ping 时，勾选的端口开放也算在线。") % commscan.MAX_HOSTS,
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("同时扫端口：")).pack(side="left")
        self.ip_port_vars = {}
        for p in commscan.INDUSTRIAL_PORTS:
            v = tk.BooleanVar(value=p in (502, 4840))
            self.ip_port_vars[p] = v
            tk.Checkbutton(row, text="%d %s" % (p, commscan.service_name(p).split("（")[0]), variable=v).pack(side="left")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("自定义端口（如 5020,9600）")).pack(side="left")
        self.ip_custom_ports = tk.StringVar(value="")
        tk.Entry(row, textvariable=self.ip_custom_ports, width=30).pack(side="left", padx=4)
        self._cs_bar(sec, "ip", ((tr("开始扫描"), self._ip_scan), (tr("停止"), lambda: self._cs_stop("ip")),
                                 (tr("导出 CSV"), lambda: self._cs_export("ip"))))
        cols = ("ip", "latency", "hostname", "mac", "ports", "via")
        self.ip_tree = ttk.Treeview(sec, columns=cols, show="headings", height=8)
        for col, title, width in (("ip", "IP", 120), ("latency", tr("延迟(ms)"), 70), ("hostname", tr("主机名"), 180),
                                  ("mac", "MAC", 140), ("ports", tr("开放端口"), 260), ("via", tr("发现方式"), 70)):
            self.ip_tree.heading(col, text=title)
            self.ip_tree.column(col, width=width, anchor="w")
        self.ip_tree.pack(fill="x", pady=4)

        # ---- 端口扫描 ----
        sec = self._section(inner, tr("端口扫描（单个主机）"))
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("主机")).pack(side="left")
        self.ps_host = tk.StringVar(value="192.168.1.10")
        tk.Entry(row, textvariable=self.ps_host, width=20).pack(side="left", padx=4)
        tk.Label(row, text=tr("端口")).pack(side="left")
        self.ps_ports = tk.StringVar(value="1-1024,4840,44818,20000")
        tk.Entry(row, textvariable=self.ps_ports, width=34).pack(side="left", padx=4)
        tk.Label(row, text=tr("超时(秒)")).pack(side="left")
        self.ps_timeout = tk.StringVar(value="0.5")
        tk.Entry(row, textvariable=self.ps_timeout, width=5).pack(side="left", padx=4)
        tk.Button(row, text=tr("填入常用工业端口"),
                  command=lambda: self.ps_ports.set(",".join(str(p) for p in sorted(commscan.SERVICES)))
                  ).pack(side="left", padx=4)
        tk.Label(sec, text=tr("端口写法：502,4840,8000-8010；一次最多 %d 个。只做 TCP 连接，连上立即断开。") % commscan.MAX_PORTS,
                 fg="#777", anchor="w").pack(fill="x")
        self._cs_bar(sec, "ps", ((tr("开始扫描"), self._ps_scan), (tr("停止"), lambda: self._cs_stop("ps")),
                                 (tr("导出 CSV"), lambda: self._cs_export("ps"))))
        cols = ("port", "service", "ms")
        self.ps_tree = ttk.Treeview(sec, columns=cols, show="headings", height=6)
        for col, title, width in (("port", tr("开放端口"), 90), ("service", tr("常见服务"), 300), ("ms", tr("连接耗时(ms)"), 100)):
            self.ps_tree.heading(col, text=title)
            self.ps_tree.column(col, width=width, anchor="w")
        self.ps_tree.pack(fill="x", pady=4)

        # ---- 串口 ----
        sec = self._section(inner, tr("串口扫描与诊断（RS-232 / RS-485 / RS-422）"))
        tk.Button(sec, text=tr("刷新串口列表"), command=self._sp_list).pack(anchor="w")
        cols = ("device", "desc", "chip", "vidpid", "maker")
        self.sp_tree = ttk.Treeview(sec, columns=cols, show="headings", height=4)
        for col, title, width in (("device", tr("串口"), 80), ("desc", tr("描述"), 260), ("chip", tr("驱动芯片"), 200),
                                  ("vidpid", "VID:PID", 90), ("maker", tr("厂商"), 140)):
            self.sp_tree.heading(col, text=title)
            self.sp_tree.column(col, width=width, anchor="w")
        self.sp_tree.pack(fill="x", pady=4)
        self.sp_tree.bind("<<TreeviewSelect>>", self._sp_pick)
        self._sp_rows = {}
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.sp_vars = {}
        for key, label, default, width in (("port", tr("串口"), "COM3", 8), ("baud", tr("波特率"), "9600", 7),
                                           ("bytesize", tr("数据位"), "8", 3), ("parity", tr("校验 N/E/O"), "N", 3),
                                           ("stopbits", tr("停止位"), "1", 3), ("timeout", tr("超时(秒)"), "0.3", 5)):
            tk.Label(row, text=label).pack(side="left")
            v = tk.StringVar(value=default)
            self.sp_vars[key] = v
            tk.Entry(row, textvariable=v, width=width).pack(side="left", padx=(2, 8))
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("接口类型")).pack(side="left")
        self.sp_iface = ttk.Combobox(row, values=["RS232", "RS485", "RS422"], state="readonly", width=8)
        self.sp_iface.pack(side="left", padx=4)
        self.sp_iface.current(1)
        self.sp_iface.bind("<<ComboboxSelected>>", lambda _e: self._sp_iface_changed())
        self.sp_rts = tk.BooleanVar(value=False)
        self.sp_rts_btn = tk.Checkbutton(row, text=tr("RTS 控制收发方向（仅 RS-485 半双工）"), variable=self.sp_rts)
        self.sp_rts_btn.pack(side="left", padx=6)
        self.sp_wiring = tk.StringVar(value=tr(commscan.wiring_tip("RS485")))
        tk.Label(sec, textvariable=self.sp_wiring, fg="#1f4e79", anchor="w", justify="left",
                 wraplength=860).pack(fill="x", pady=2)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        for key, label, default, width in (("unit", tr("站号"), "1", 4), ("first", tr("扫描站号从"), "1", 4),
                                           ("last", tr("到"), "247", 4), ("addr", tr("起始地址"), "0", 6),
                                           ("count", tr("数量"), "1", 4)):
            tk.Label(row, text=label).pack(side="left")
            v = tk.StringVar(value=default)
            self.sp_vars[key] = v
            tk.Entry(row, textvariable=v, width=width).pack(side="left", padx=(2, 8))
        tk.Label(row, text=tr("功能码")).pack(side="left")
        self.sp_func = ttk.Combobox(row, values=[tr("01 读线圈"), tr("02 读离散输入"), tr("03 读保持寄存器"), tr("04 读输入寄存器")],
                                    state="readonly", width=14)
        self.sp_func.pack(side="left", padx=4)
        self.sp_func.current(2)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=tr("原始发送")).pack(side="left")
        self.sp_send = tk.StringVar(value="01 03 00 00 00 01 84 0A")
        tk.Entry(row, textvariable=self.sp_send, width=40).pack(side="left", padx=4)
        self.sp_mode = ttk.Combobox(row, values=["hex", "text"], state="readonly", width=6)
        self.sp_mode.pack(side="left", padx=4)
        self.sp_mode.current(0)
        tk.Label(sec, text=tr("波特率探测依次尝试 1200～115200 × 无/偶/奇校验，收到 CRC 正确的应答（含 Modbus 异常应答）即判定参数正确；"
                           "站号扫描按上面的串口参数逐个站号发读请求（1～247 全扫约需 1～2 分钟）。"),
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self._cs_bar(sec, "serial", ((tr("波特率探测"), self._sp_detect), (tr("站号扫描"), self._sp_slaves),
                                     (tr("环回测试"), self._sp_loopback), (tr("原始收发"), self._sp_raw),
                                     (tr("停止"), lambda: self._cs_stop("serial"))))
        self.sp_out = self.tk.Text(sec, height=12, state="disabled", wrap="word")
        self.sp_out.pack(fill="x", pady=4)
        self._sp_iface_changed()

    def _cs_bar(self, parent, key, buttons):
        tk, ttk = self.tk, self.ttk
        row = tk.Frame(parent)
        row.pack(fill="x", pady=3)
        for text, cmd in buttons:
            tk.Button(row, text=text, command=cmd).pack(side="left", padx=3)
        self.cs_prog[key] = ttk.Progressbar(row, mode="determinate", maximum=100, length=220)
        self.cs_prog[key].pack(side="left", padx=8)
        self.cs_status[key] = tk.StringVar(value="")
        tk.Label(row, textvariable=self.cs_status[key], fg="#555", anchor="w").pack(side="left", fill="x", expand=True)

    def _cs_progress(self, key, n, total, text):
        try:
            self.cs_prog[key].configure(value=(100.0 * n / total) if total else 0)
        except Exception:  # noqa
            pass
        self.cs_status[key].set(text)

    def _cs_start(self, key, work, done_cb):
        """在工作线程跑 work(stop, progress)；进度经队列回到界面线程。同一类扫描同时只跑一个。"""
        import threading
        if self._cs_running.get(key):
            return self._show(False, tr("上一次扫描还在进行，请等它结束或点“停止”。"))
        stop = threading.Event()
        self._cs_stops[key] = stop
        self._cs_running[key] = True
        self._cs_progress(key, 0, 1, tr("进行中……"))

        def progress(n, total, text):
            self._q.put((lambda _r, _e: self._cs_progress(key, n, total, text), None, None))

        def done(res, err):
            self._cs_running[key] = False
            if err is not None:
                self._cs_progress(key, 0, 1, tr("失败：%s") % err)
                return self._show(False, tr_msg(err))
            self._cs_results[key] = res
            self._cs_progress(key, 1, 1, res.get("summary", tr("完成")) if isinstance(res, dict) else tr("完成"))
            done_cb(res)
        self._bg(lambda: work(stop, progress), done)

    def _cs_stop(self, key):
        ev = self._cs_stops.get(key)
        if ev is not None and self._cs_running.get(key):
            ev.set()
            self.cs_status[key].set(tr("正在停止……"))

    def _cs_export(self, key):
        res = self._cs_results.get(key)
        if not res:
            return self._show(False, tr("还没有扫描结果可以导出。"))
        rows = commscan.hosts_csv_rows(res) if key == "ip" else commscan.ports_csv_rows(res)
        root = default_export_dir(self._orig or self.cfg, self.cfg["_base_dir"])
        name = commscan.export_name("IP扫描" if key == "ip" else "端口扫描")

        def done(path, err):
            if err is not None:
                return self._show(False, tr("导出失败：%s") % err)
            self.sa.get_log().info("通讯诊断结果已导出：%s", path)
            self._show(True, tr("已导出 %d 行：\n%s") % (len(rows) - 1, path))
        self._bg(lambda: commscan.write_csv(rows, os.path.join(root, name)), done)

    def _ip_scan(self):
        try:
            targets = commscan.parse_ip_targets(self.ip_range.get())
            ports = [p for p, v in self.ip_port_vars.items() if v.get()]
            ports += commscan.parse_ports(self.ip_custom_ports.get(), limit=64)
            ports = sorted(set(ports))
            workers = int(float(self.ip_workers.get() or 64))
        except (commscan.CommScanError, ValueError) as e:
            return self._show(False, tr_msg(e))
        to, names = self.ip_timeout.get(), bool(self.ip_names.get())
        for item in self.ip_tree.get_children():
            self.ip_tree.delete(item)
        self.sa.get_log().info("IP 扫描：%s（%d 个地址，端口 %s）", self.ip_range.get(), len(targets), ports or "无")

        def done(res):
            for hst in res["hosts"]:
                self.ip_tree.insert("", "end", values=(
                    hst["ip"], "" if hst["latency_ms"] is None else hst["latency_ms"], hst["hostname"], hst["mac"],
                    " ".join("%d%s" % (p["port"], "(%s)" % p["service"].split("（")[0] if p["service"] else "")
                             for p in hst["open_ports"]), hst["via"]))
            self.sa.get_log().info("IP 扫描完成：%s", res["summary"])
        self._cs_start("ip", lambda stop, prog: commscan.scan_ip_range(
            targets, ports=ports, timeout=to, workers=workers, resolve_names=names, stop=stop, progress=prog), done)

    def _ps_scan(self):
        try:
            ports = commscan.parse_ports(self.ps_ports.get())
        except commscan.CommScanError as e:
            return self._show(False, tr_msg(e))
        host, to = self.ps_host.get(), self.ps_timeout.get()
        for item in self.ps_tree.get_children():
            self.ps_tree.delete(item)

        def done(res):
            for o in res["open"]:
                self.ps_tree.insert("", "end", values=(o["port"], o["service"] or tr("（未知）"), o["ms"]))
            if not res["open"]:
                self.ps_tree.insert("", "end", values=("", tr("没有发现开放端口"), ""))
            self.sa.get_log().info("端口扫描完成：%s", res["summary"])
        self._cs_start("ps", lambda stop, prog: commscan.scan_ports(host, ports, timeout=to, stop=stop,
                                                                     progress=prog), done)

    # ---- 串口 ----
    def _sp_list(self):
        def done(ports, err):
            for item in self.sp_tree.get_children():
                self.sp_tree.delete(item)
            self._sp_rows = {}
            if err is not None:
                return self._show(False, tr_msg(err))
            for p in ports:
                iid = self.sp_tree.insert("", "end", values=(p["device"], p["description"], p["chip"], p["vidpid"],
                                                             p["manufacturer"]))
                self._sp_rows[iid] = p["device"]
            if not ports:
                self.sp_tree.insert("", "end", values=("", tr("没有找到串口：USB 转换器是否插好、驱动是否安装（设备管理器 → 端口）"),
                                                       "", "", ""))
        self._bg(commscan.scan_serial_ports, done)

    def _sp_pick(self, _event=None):
        sel = self.sp_tree.selection()
        if sel and sel[0] in self._sp_rows:
            self.sp_vars["port"].set(self._sp_rows[sel[0]])

    def _sp_iface_changed(self):
        kind = self.sp_iface.get() or "RS485"
        self.sp_wiring.set(tr("%s（%s）接线：%s") % (kind, tr(commscan.INTERFACES[kind]["duplex"]), tr(commscan.wiring_tip(kind))))
        if kind != "RS485":
            self.sp_rts.set(False)
        self.sp_rts_btn.configure(state="normal" if kind == "RS485" else "disabled")

    def _sp_params(self):
        v = {k: var.get().strip() for k, var in self.sp_vars.items()}
        idx = self.sp_func.current()
        v["func"] = (idx + 1) if isinstance(idx, int) and 0 <= idx < 4 else 3
        v["interface"] = self.sp_iface.get() or "RS485"
        v["rs485_rts"] = bool(self.sp_rts.get()) and v["interface"] == "RS485"
        return v

    def _sp_show(self, res):
        self._set_text(self.sp_out, commscan.result_text(res))
        self.sa.get_log().info("串口诊断：%s", res.get("summary"))

    def _sp_detect(self):
        p = self._sp_params()
        self._set_text(self.sp_out, tr("波特率探测中（%s，站号 %s）……") % (p["port"], p["unit"]))
        self._cs_start("serial", lambda stop, prog: commscan.detect_baud(
            p["port"], unit=p["unit"], func=p["func"], addr=p["addr"], count=p["count"], bytesize=p["bytesize"],
            stopbits=(float(p["stopbits"] or 1),), timeout=p["timeout"], rs485_rts=p["rs485_rts"],
            stop=stop, progress=prog), self._sp_detect_done)

    def _sp_detect_done(self, res):
        self._sp_show(res)
        if res.get("found"):                 # 把探测到的参数填回表单，方便接着做站号扫描
            f = res["found"][0]
            self.sp_vars["baud"].set(str(f["baud"]))
            self.sp_vars["parity"].set(f["parity"])
            self.sp_vars["stopbits"].set("%g" % f["stopbits"])

    def _sp_slaves(self):
        p = self._sp_params()
        self._set_text(self.sp_out, tr("站号扫描中（%s %s,%s,%s，站号 %s～%s）……") % (
            p["port"], p["baud"], p["parity"], p["stopbits"], p["first"], p["last"]))
        self._cs_start("serial", lambda stop, prog: commscan.scan_modbus_rtu_slaves(
            p["port"], baud=p["baud"], parity=p["parity"], stopbits=p["stopbits"], bytesize=p["bytesize"],
            first=p["first"], last=p["last"], func=p["func"], addr=p["addr"], count=p["count"],
            timeout=p["timeout"], rs485_rts=p["rs485_rts"], stop=stop, progress=prog), self._sp_show)

    def _sp_loopback(self):
        p = self._sp_params()
        self._cs_start("serial", lambda stop, prog: commscan.serial_loopback(
            p["port"], baud=p["baud"], bytesize=p["bytesize"], parity=p["parity"], stopbits=p["stopbits"],
            timeout=max(1.0, float(p["timeout"] or 1)), interface=p["interface"]), self._sp_show)

    def _sp_raw(self):
        p = self._sp_params()
        send, mode = self.sp_send.get(), self.sp_mode.get() or "hex"
        self._cs_start("serial", lambda stop, prog: commscan.serial_raw(
            p["port"], baud=p["baud"], bytesize=p["bytesize"], parity=p["parity"], stopbits=p["stopbits"],
            send=send, mode=mode, timeout=max(0.5, float(p["timeout"] or 1)), interface=p["interface"],
            rs485_rts=p["rs485_rts"]), self._sp_show)

    # ---- 状态轮询 ----
    def _poll_alerts(self):
        def done(a, err):
            if err is None:
                self._render_alerts(a)
                self._last_alerts = a or {}
                self._notify_alerts(a)
            if not self._closed:
                self.root.after(15000, self._poll_alerts)
        self._bg(self.logic.alerts, done)


def run_desktop(cfg, start_service=True, minimized=False):
    """打开桌面主窗口并进入消息循环，窗口关闭后返回。
    start_service=True：窗口打开后自动启动服务（默认，双击 EXE）；
    False：不自动启动（--settings，或“打开窗口后自动启动服务”未勾选），可在窗口里点“启动服务”。
    minimized=True：窗口打开后最小化到任务栏（“启动时最小化”）。
    返回前保证服务已停止——本程序没有任何后台/常驻运行方式。"""
    win = DesktopWindow(cfg, start_service=start_service, minimized=minimized)
    try:
        win.root.mainloop()
    finally:
        if win.svc.running():
            win.svc.stop(10)
    return 0
