#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
色谱提取 OPC UA 服务（ChromExtract OPC UA Server）

功能：
  1. 监控配置目录中的 DataBrowser 日志数据库（*_V3.db）和 .mbd 谱图文件；
  2. 解析最新谱图（BinTrc），通过 OPC UA Server 发布，供 DCS/SCADA/实时库订阅；
  3. 提供 OPC UA 方法 ExportCSV，可远程触发把最新数据导出为 CSV；
  4. 桌面软件（v2.9）：双击即打开桌面主窗口，在窗口里启动/停止服务、选数据目录、查看数据源、
     导出 CSV、看状态告警和日志、做安全设置与诊断；关闭窗口即停止服务。不再提供后台/服务/
     开机自启运行方式。命令行工具（--extract / --list / --selftest 等）仍保留。
  5. 数据传输检测与发送（v2.5）：Diagnostics 诊断节点（只读）+ SendTestData/EchoTest
     方法 + --check-transmission 客户端检测，证明数据真的在传（不止端口通）。
  6. 智能诊断（v2.5）：--diagnose / GUI「智能诊断」一键收齐配置—服务—数据—传输—日志
     五层证据，逐项给通过/注意/失败与具体处理步骤（纯规则，离线可用，不用大模型）。

用法：
  server_app.py                      # 打开桌面主窗口，并按同目录 config.json 启动 OPC UA 服务
  server_app.py --settings           # 打开桌面主窗口，但不自动启动服务（窗口里可手动启动）
  server_app.py --config 配置.json   # 指定配置文件
  server_app.py --extract 数据文件夹 -o 输出文件夹 [--last 20]   # 命令行工具（不开窗口）
  server_app.py --list 数据文件夹
  server_app.py --selftest

OPC UA 地址空间（ns=<namespace index>，启动日志会打印实际值）：
  Objects/Chromatograph
    ├─ Diagnostics/  TestSignal, TestCounter, ChannelCount, GoodChannelCount,
    │                TotalPoints, LastDataChangeTime, DataAgeSeconds,
    │                TransmissionStatus, ServerStartTime, UptimeSeconds,
    │                DiagnosisLevel, DiagnosisSummary, LastDiagnosisTime（只读）
    ├─ SendTestData() / EchoTest(text)  方法（发送测试数据 / 双向回显，不开写权限）
    └─ Station_<仪器序列号>
         ├─ Info/  SerialNumber, Name, SoftwareVersion, SourceFile
         └─ App<应用>_Stream<流路>_Det<检测器>
              ├─ LatestCycle/  CycleTime, CycleId, IsValidation, CalibrationLevel,
              │                NumPoints, SamplePeriod, Duration, DetName, YTitle, XTitle
              ├─ Trace/        TimeArray, RawArray, MicrovoltArray, ValueArray
              └─ Status/       Revision, LastUpdate, Good
"""
import argparse
import asyncio
import collections
import getpass
import json
import logging
import logging.handlers
import math
import os
import queue
import sys
import tempfile
import threading
import time
from datetime import datetime

import alerts
import chrom_core
import config_tools
from config_tools import hash_password, verify_password   # noqa: F401  (hash_password 也供测试使用)

__version__ = chrom_core.__version__


def _force_utf8_stdout():
    """Windows 控制台默认编码（如 cp1252）写不下中文会直接崩溃；
    程序输出大量中文，启动时就把 stdout/stderr 强制切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


DEFAULT_CONFIG = {
    "endpoint": "opc.tcp://0.0.0.0:4840/ChromExtract",
    "namespace_uri": "urn:chromextract:opcua",
    "server_name": "色谱提取 OPC UA 服务",
    "watch_dirs": [],
    "poll_seconds": 30,
    "last_n": 5,
    "security": "none",           # none / sign / sign_encrypt
    "cert_dir": "certs",
    "csv_export_dir": "谱图导出",
    "log_file": "logs/server.log",
    "max_points": 20000,
    # ---- 安全相关（v2.1 新增）----
    "allow_anonymous": True,          # 是否允许不登录的只读连接；设为 false 需配置 users
    "users": {},                      # {"用户名": "pbkdf2_sha256$..."}，用 --hash-password 生成
    "allow_insecure_fallback": False, # 加密模式下是否仍开放不加密端点（仅排障用）
    "trusted_client_certs_dir": "certs/trusted",  # 里面放了客户端证书就只允许这些客户端（加密模式）
    "remote_export": True,            # 是否允许客户端远程调用 ExportCSV
    "export_min_interval_sec": 10,    # 远程导出最短间隔，防止被反复调用写满磁盘
    "max_connections": 20,            # 同时连接数上限
    "max_trace_points": 2000000,      # 单条谱图最多采样点数（超出视为损坏）
    "max_file_mb": 256,               # 单个 .mbd 文件大小上限
    "log_max_mb": 5,                  # 单个日志文件大小，超出自动轮转
    "log_backups": 5,                 # 保留的历史日志个数
    # ---- 网页设置页（v2.4 新增）----
    "web_ui": True,                   # 服务运行时同时开启浏览器设置页面
    "web_host": "127.0.0.1",          # 默认只允许本机访问；改成其它地址后必须账号登录
    "web_port": 8840,
    "web_https": "auto",              # auto：对外开放时自动 HTTPS；on / off
    "web_cert_file": "",              # 留空 = 自动生成自签名证书 certs/web_cert.pem
    "web_key_file": "",
    # ---- 异常告警（v2.4 新增）----
    "alert_stale_minutes": 120,       # 某通道超过这么多分钟没有新周期就告警；0 = 不检查
    "alert_scan_errors": 3,           # 连续扫描出错次数达到就告警；0 = 不检查
    "alert_webhook": "",              # 可选：企业微信/钉钉/飞书机器人或任意 https 地址，告警出现/恢复时推送
    "alert_email_to": "",             # 可选：告警邮件收件人，多个用逗号隔开
    "smtp_host": "",                  # SMTP 服务器，例如 smtp.exmail.qq.com
    "smtp_port": 465,
    "smtp_security": "ssl",           # ssl（465）/ starttls（587）/ none（仅内网免登录中继，25）
    "smtp_user": "",
    "smtp_password": "",              # 在设置页面里设置，Windows 上以 DPAPI 加密保存，不要手写
    "smtp_from": "",                  # 发件人，留空 = smtp_user
    # 各渠道发送哪些级别：all = 严重和警告都发 / critical = 只发严重 / off = 不发
    "alert_webhook_level": "all",
    "alert_email_level": "critical",
    "alert_quiet": [],                # 静默时段（计划停机/检修时不推送），在设置页面里添加
    "alert_channel_rules": [],        # 按通道屏蔽告警 / 单独设置无新周期分钟数，在设置页面通道表里设置
    "alert_probes": [],               # 设备检测：定时 Ping / 端口 / Modbus 等，失败走告警；在设置页面里添加
    "env_check_on_start": True,       # 服务启动后自动做驱动与软件环境检测，"问题"级别的项告警
    "env_check_hours": 6,             # 之后每几小时复查一次；0 = 只在启动时检查
    # ---- 数据传输检测与发送（v2.4 新增）----
    "allow_test_send": True,          # 是否允许客户端远程调用 SendTestData（仅写 Station_TEST 测试通道）
    "test_send_min_interval_sec": 5,  # 远程发送测试数据的最短间隔，防止被反复调用
    "transmission_stale_sec": 7200,   # 数据超过多少秒没更新视为"数据较旧"（Diagnostics.TransmissionStatus）
    # ---- 桌面窗口（v3.0 新增；只影响本机窗口，不影响 OPC UA 服务）----
    "desktop_notify": True,           # 新告警出现时：任务栏闪烁 + Windows 通知气泡
    "desktop_notify_level": "all",    # all = 严重和警告都提醒 / critical = 只提醒严重
    "desktop_notify_quiet": True,     # 静默时段（alert_quiet）内不弹桌面提醒
    "desktop_auto_start_service": True,   # 打开窗口后自动启动服务（--settings 打开时一律不自动启动）
    "desktop_start_minimized": False,     # 打开窗口时最小化（配合“登录后自动打开窗口”）
    # ---- v3.1 新增 ----
    "desktop_language": "auto",       # 界面语言：auto（按 Windows 系统界面语言）/ zh / en；日志与 OPC UA 节点名不翻译
    "backup_keep": 10,                # 还原前自动备份（backups\自动备份_*.zip）保留最近几个
    # 历史数据（服务运行期间写入 history/history.db，窗口里查询对比、运行报表统计用）
    "history_enabled": True,
    "history_file": "history/history.db",
    "history_retention_days": 90,     # 保留天数，超过自动删除
    "history_max_mb": 1024,           # 曲线数据容量上限（MB），超过从最旧的周期删起
    # 峰识别（谱图预览标峰、峰表、可选发布到 OPC UA / Modbus）
    "peak_min_height": 0.0,           # 最小峰高（0 = 只按噪声阈值）
    "peak_min_width": 0.0,            # 最小半峰宽（秒）
    "peak_noise_factor": 5.0,         # 阈值 = 噪声 × 倍数
    "peak_smooth": 5,                 # 平滑点数
    "peak_start": 0.0,                # 积分起点（秒）
    "peak_end": 0.0,                  # 积分终点（秒，0 = 到最后）
    "peak_series": "value",           # 用哪列数据找峰：value / uv / raw
    "peaks_publish": False,           # 在每个通道下发布 Peaks 节点（RetentionTimes / Heights / Areas …）
    "peaks_publish_max": 50,          # 最多发布几个峰
    # Modbus TCP 数据转发（只读从站，随服务启停）
    "modbus_server_enabled": False,
    "modbus_server_host": "127.0.0.1",    # 默认只本机；改成 0.0.0.0 或网卡地址才对外（无认证，务必配白名单）
    "modbus_server_port": 502,
    "modbus_server_allow": [],        # 允许的来源 IP / 网段，空 = 不限制
    "modbus_server_unit": 0,          # 站号，0 = 任意站号都应答
    "modbus_server_order": "ABCD",    # 32 位数据的字节序：ABCD / CDAB / BADC / DCBA
    "modbus_server_map": [],          # 寄存器映射表，在窗口“Modbus 转发”页编辑
    # 运行报表（窗口内定时器生成，不建计划任务）
    "desktop_report_auto": "off",     # off / daily（每天） / shift（每班）
    "desktop_report_shifts": [{"name": "白班", "start": "08:00"}, {"name": "夜班", "start": "20:00"}],
    "desktop_report_dir": "运行报表",
    # 桌面窗口登录与权限（账号复用 users；角色：operator 操作员 / engineer 工程师 / admin 管理员）
    "desktop_auth": True,             # 关闭后窗口不需要登录，所有操作都可用（不推荐）
    "desktop_idle_lock_minutes": 15,  # 空闲多少分钟自动锁定（退回只读），0 = 不锁定
    "user_roles": {},
}

# 桌面窗口自己的设置项：保存后窗口立即使用，与服务的热加载/重启无关
DESKTOP_KEYS = ("desktop_notify", "desktop_notify_level", "desktop_notify_quiet",
                "desktop_auto_start_service", "desktop_start_minimized",
                "desktop_language", "desktop_report_auto", "desktop_report_shifts", "desktop_report_dir",
                "desktop_auth", "desktop_idle_lock_minutes")
DESKTOP_BOOL_KEYS = ("desktop_notify", "desktop_notify_quiet", "desktop_auto_start_service",
                     "desktop_start_minimized", "desktop_auth")


APP_URI = "urn:chromextract:opcua:server"   # 服务端应用 URI，必须与证书里一致

log_q = queue.Queue()
ALERTS = {}      # {"monitor": AlertMonitor}，服务运行时供网页/窗口读取


def get_log():
    return logging.getLogger("chrom")


class _SafeFormatter(logging.Formatter):
    """日志里可能带有来自数据文件的文本，换行一律压成空格，防止伪造日志行。"""

    def format(self, record):
        return super().format(record).replace("\r", " ").replace("\n", " ")


def setup_logging(cfg, base_dir, console=True):
    fmt = _SafeFormatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    root.handlers = []
    if console and sys.stdout is not None:   # 窗口版 EXE 没有控制台，不往控制台写
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if cfg.get("log_file"):
        lf = os.path.join(base_dir, cfg["log_file"])
        os.makedirs(os.path.dirname(lf), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            lf, maxBytes=int(cfg.get("log_max_mb", 5)) * 1024 * 1024,
            backupCount=int(cfg.get("log_backups", 5)), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


# ---------------------------------------------------------------------------
# 数据快照：在工作线程扫描数据源，解析出最新谱图
# ---------------------------------------------------------------------------

def _file_sig(path):
    """文件签名：修改时间 + 大小（含 -wal）。签名没变就不必重新拷贝解析。"""
    sig = []
    for p in (path, path + "-wal"):
        try:
            st = os.stat(p)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def _read_source(src, last_n, max_points, tmp_holder):
    """读一个数据源，返回它的最新谱图记录列表。读不了就抛异常。"""
    if src.lower().endswith(".mbd"):
        items = list(chrom_core.iter_mbd(src))
    else:
        if tmp_holder[0] is None:
            tmp_holder[0] = tempfile.mkdtemp(prefix="chrom_opc_")
        items = list(chrom_core.iter_db(src, tmp_holder[0], last=last_n))
    latest = {}
    for it in items:
        key = (it.source, it.app, it.stream, it.det)
        old = latest.get(key)
        if old is None or (it.cycle_id or 0) > (old.cycle_id or 0):
            latest[key] = it
    records = []
    for it in latest.values():
        try:
            info = chrom_core.parse_trc(it.blob)
        except chrom_core.TraceError as e:
            get_log().warning("跳过无法解析的谱图（%s det=%s）：%s", it.source, it.det, e)
            continue
        if not info["data"]:
            continue
        # 先抽点再展开成列表：内存与 max_points 成正比，与原始点数无关
        t, tx, raw, uv, val = chrom_core.to_series(info, max_points=max_points)
        records.append(dict(
            source=it.source, analyzer=it.analyzer, app=it.app, app_name=it.app_name,
            stream=it.stream, stream_name=it.stream_name, det=it.det,
            det_name=info["det_name"] or info["trace_name"],
            cycle_id=it.cycle_id, cycle_time=str(it.cycle_time),
            is_validation=it.is_validation, cal_level=it.cal_level,
            num_points=len(val), sample_period=chrom_core._fin(info["sample_period"], 0.0),
            duration=t[-1] if t else 0.0, min_raw=min(raw), max_raw=max(raw),
            y_title=info["y_title"], x_title=info["x_title"] or "s",
            time=[float(v) for v in t], raw=[float(v) for v in raw],
            uv=[float(v) for v in uv], value=[float(v) for v in val],
        ))
    return records


def gather_snapshot(watch_dirs, last_n, max_points, cache=None):
    """扫描所有数据源，返回 [dict] 每个检测器一条最新谱图记录。
    cache：{路径: (文件签名, 记录)}；文件没变化就直接复用，避免每轮都拷贝整个数据库。"""
    records = []
    tmp_holder = [None]
    try:
        srcs = chrom_core.find_inputs(watch_dirs)
    except FileNotFoundError as e:
        get_log().warning("监控路径不存在：%s", e)
        return records
    try:
        for src in srcs:
            sig = _file_sig(src)
            if cache is not None and src in cache and cache[src][0] == sig:
                records.extend(cache[src][1])
                continue
            try:
                recs = _read_source(src, last_n, max_points, tmp_holder)
            except Exception as e:  # noqa
                get_log().warning("读取 %s 失败：%s", src, e)
                if cache is not None:
                    cache.pop(src, None)   # 读失败不缓存，下一轮重试
                continue
            if cache is not None:
                cache[src] = (sig, recs)
            records.extend(recs)
    finally:
        if tmp_holder[0]:
            import shutil
            shutil.rmtree(tmp_holder[0], ignore_errors=True)
    if cache is not None:
        for k in [k for k in cache if k not in srcs]:
            del cache[k]                  # 文件已消失
    return records


# 改了配置文件后，下面这些项不用重启就能生效；其余项（端口、加密方式、证书等）需要重启。
HOT_KEYS = ("users", "allow_anonymous", "remote_export", "export_min_interval_sec",
            "watch_dirs", "last_n", "max_points", "poll_seconds",
            "alert_stale_minutes", "alert_scan_errors", "alert_webhook",
            "alert_email_to", "smtp_host", "smtp_port", "smtp_security", "smtp_user",
            "smtp_password", "smtp_from", "alert_webhook_level", "alert_email_level", "alert_quiet",
            "alert_channel_rules", "alert_probes", "env_check_hours",
            "allow_test_send", "test_send_min_interval_sec", "transmission_stale_sec",
            # v3.1：峰识别参数、Modbus 映射 / 字节序、账号角色（服务每轮按新值计算）
            "peak_min_height", "peak_min_width", "peak_noise_factor", "peak_smooth", "peak_start", "peak_end",
            "peak_series", "peaks_publish_max", "modbus_server_map", "modbus_server_order", "user_roles",
            "backup_keep")
RESTART_KEYS = ("env_check_on_start", "endpoint", "security", "allow_insecure_fallback", "cert_dir",
                "trusted_client_certs_dir", "max_connections", "namespace_uri", "server_name",
                # v3.1
                "history_enabled", "history_file", "history_retention_days", "history_max_mb",
                "peaks_publish", "modbus_server_enabled", "modbus_server_host", "modbus_server_port",
                "modbus_server_allow", "modbus_server_unit")


def _cfg_stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def try_hot_reload(cfg, stamp, reload_cb=None):
    """配置文件有变化就重新读取并就地更新 cfg 里的"热"项。
    返回新的 stamp。读不了/校验不过就保持旧配置不变，只记一条错误日志。"""
    path = cfg.get("_cfg_path")
    if not path:
        return stamp
    now = _cfg_stamp(path)
    if now == stamp or now is None:
        return stamp
    try:
        new = load_config(path)
        new["_base_dir"] = cfg["_base_dir"]
        new["_cfg_path"] = path
        normalize_config(new, cfg["_base_dir"])
    except Exception as e:  # noqa
        get_log().error("配置文件有误，已忽略这次修改，继续使用原配置：%s", e)
        return now
    changed = [k for k in HOT_KEYS if new.get(k) != cfg.get(k)]
    need_restart = [k for k in RESTART_KEYS if new.get(k) != cfg.get(k)]
    for k in changed:
        cfg[k] = new[k]
    if "users" in changed or "allow_anonymous" in changed:
        if reload_cb is not None:
            reload_cb(cfg.get("users"), cfg.get("allow_anonymous", True))
    if changed:
        get_log().info("配置文件已更新，已自动生效：%s", "、".join(changed))
        for lv, msg in security_report(cfg):
            if lv == "风险":
                get_log().warning("安全提示[%s]：%s", lv, msg)
    if need_restart:
        get_log().warning("配置文件里这些项改了，但需要重启服务才会生效：%s", "、".join(need_restart))
    return now


def watcher_thread(snapshot_cb, stop_evt, cfg, beat_cb=None, reload_cb=None, monitor=None, history_cb=None):
    """循环扫描数据源；发现新周期即通过回调推给 OPC UA 循环。"""
    last_sig = None
    cache = {}
    stamp = _cfg_stamp(cfg.get("_cfg_path") or "")
    while not stop_evt.is_set():
        try:
            stamp = try_hot_reload(cfg, stamp, reload_cb)
        except Exception as e:  # noqa
            get_log().error("检查配置文件出错：%s", e)
        interval = max(5, int(cfg.get("poll_seconds", 30)))
        try:
            recs = gather_snapshot(cfg.get("watch_dirs") or [], cfg.get("last_n", 5),
                                   cfg.get("max_points", 20000), cache)
            sig = {r["source"] + "|%s|%s|%s" % (r["app"], r["stream"], r["det"]): r["cycle_id"]
                   for r in recs}
            if monitor is not None:
                monitor.on_scan(recs)
            if sig != last_sig:
                new_cnt = sum(1 for k, v in sig.items() if (last_sig or {}).get(k) != v)
                get_log().info("扫描到 %d 个检测器通道，其中 %d 个有更新", len(sig), new_cnt)
                last_sig = sig
                snapshot_cb(recs)
                if history_cb is not None:
                    history_cb(recs)
        except Exception as e:  # noqa
            get_log().error("扫描出错：%s", e)
            if monitor is not None:
                try:
                    monitor.on_scan_error(e)
                except Exception:  # noqa
                    pass
        if beat_cb is not None:
            beat_cb()          # 心跳：即使没有新数据，客户端也能确认服务还活着
        stop_evt.wait(interval)


# ---------------------------------------------------------------------------
# OPC UA 服务
# ---------------------------------------------------------------------------

def _channel_key(r):
    """通道键：来源|应用|流路|检测器（与 alerts / desktop_features / history 一致）。"""
    return "%s|%s|%s|%s" % (r.get("source"), r.get("app"), r.get("stream"), r.get("det"))


def _channel_label(r):
    try:
        import desktop_features as _df
        return _df.channel_label(r, "zh")     # 历史库里固定存中文，界面显示时再翻译
    except Exception:  # noqa
        return _channel_key(r)


def _safe_node(s):
    import re as _re
    return _re.sub(r"[^0-9A-Za-z_.-]+", "_", str(s)).strip("_") or "x"


# ---------------------------------------------------------------------------
# 登录与权限
# ---------------------------------------------------------------------------

_dummy_hash_cache = []


def _dummy_hash():
    if not _dummy_hash_cache:
        _dummy_hash_cache.append(hash_password("x"))
    return _dummy_hash_cache[0]


class ChromUserManager:
    """替换 asyncua 自带的默认管理器。

    默认管理器的缺陷：用户名填 admin、密码随便填，就会被授予管理员权限（可改写所有数据）。
    本管理器规则：
      - 任何远程用户（含匿名）最高只是只读用户，永远不会得到管理员权限；
      - 配置了 users 时，用户名密码必须与哈希匹配（恒定时间比较，不存在的用户也做同样计算，防探测）；
      - 连续输错密码会被临时锁定，防暴力猜解；
      - allow_anonymous=false 时拒绝不登录的连接。
    """
    MAX_FAILS = 5          # 窗口内最多允许输错次数
    WINDOW = 60.0          # 秒

    def __init__(self, users=None, allow_anonymous=True):
        self.users = {str(k): str(v) for k, v in (users or {}).items()}
        self.allow_anonymous = bool(allow_anonymous)
        self._fails = collections.defaultdict(list)

    def _recent(self, key):
        now = time.monotonic()
        lst = [t for t in self._fails.get(key, []) if now - t < self.WINDOW]
        if lst:
            self._fails[key] = lst
        else:
            self._fails.pop(key, None)
        return lst

    def _fail(self, key):
        if len(self._fails) > 1000:        # 防止被随机用户名撑爆内存
            self._fails.clear()
        self._fails[key].append(time.monotonic())

    def get_user(self, iserver, username=None, password=None, certificate=None):
        from asyncua.crypto.permission_rules import User, UserRole
        if not username:
            if self.allow_anonymous:
                return User(role=UserRole.User)
            get_log().warning("已拒绝匿名连接（allow_anonymous=false）")
            return None
        if len(self._recent(username)) >= self.MAX_FAILS or len(self._recent("*")) >= 5 * self.MAX_FAILS:
            get_log().warning("登录尝试过于频繁，已临时拒绝：%s", chrom_core.clean_text(username, 40))
            return None
        stored = self.users.get(username)
        ok = verify_password(password or "", stored if stored else _dummy_hash())
        if not (ok and stored):
            self._fail(username)
            self._fail("*")
            get_log().warning("登录失败：用户 %s", chrom_core.clean_text(username, 40))
            return None
        self._fails.pop(username, None)       # 登录成功，清掉这个账号之前的失败记录
        get_log().info("登录成功：用户 %s", chrom_core.clean_text(username, 40))
        return User(role=UserRole.User, name=username)

    def reload(self, users, allow_anonymous):
        """热更新账号和匿名开关（引用整体替换，线程安全）。"""
        self.users = {str(k): str(v) for k, v in (users or {}).items()}
        self.allow_anonymous = bool(allow_anonymous)


def load_trusted_fingerprints(directory):
    """读取客户端证书白名单目录，返回 SHA-256 指纹集合。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    fps = set()
    if not os.path.isdir(directory):
        return fps
    for fn in sorted(os.listdir(directory)):
        if not fn.lower().endswith((".der", ".cer", ".crt", ".pem")):
            continue
        try:
            with open(os.path.join(directory, fn), "rb") as f:
                data = f.read()
            cert = (x509.load_pem_x509_certificate(data) if b"BEGIN CERTIFICATE" in data
                    else x509.load_der_x509_certificate(data))
            fps.add(cert.fingerprint(hashes.SHA256()).hex())
        except Exception as e:  # noqa
            get_log().warning("忽略无法读取的证书 %s：%s", fn, e)
    return fps


async def make_cert(cfg, base_dir):
    """security 非 none 时自动生成自签名证书（需要 cryptography 库）。"""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
        import datetime
        import ipaddress
        import socket
    except Exception as e:  # noqa
        # 配置要求加密却悄悄改成不加密，等于欺骗使用者；宁可拒绝启动。
        raise RuntimeError("配置要求加密（security=%s），但无法加载 cryptography 库：%s"
                           % (cfg.get("security"), e))

    cert_dir = os.path.join(base_dir, cfg.get("cert_dir") or "certs")
    os.makedirs(cert_dir, exist_ok=True)
    cert_p = os.path.join(cert_dir, "server_cert.der")
    key_p = os.path.join(cert_dir, "server_key.pem")
    def _cert_ok():
        try:
            with open(cert_p, "rb") as f:
                c = x509.load_der_x509_certificate(f.read())
            san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            return APP_URI in san.get_values_for_type(x509.UniformResourceIdentifier)
        except Exception:  # noqa
            return False

    if not (os.path.exists(cert_p) and os.path.exists(key_p)) or not _cert_ok():
        get_log().info("生成自签名证书到 %s ...", cert_dir)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = [x509.NameAttribute(NameOID.COMMON_NAME, u"ChromExtractOPC")]
        san = [x509.DNSName(u"localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
               x509.UniformResourceIdentifier(APP_URI)]
        try:
            host = socket.gethostname()
            san.append(x509.DNSName(host))
            san.append(x509.IPAddress(ipaddress.IPv4Address(socket.gethostbyname(host))))
        except Exception:  # noqa
            pass
        _now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name(name)).issuer_name(x509.Name(name))
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(_now - datetime.timedelta(days=1))
                .not_valid_after(_now + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName(san), critical=False)
                .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True,
                                             content_commitment=False, data_encipherment=True,
                                             key_agreement=False, key_cert_sign=False,
                                             crl_sign=False, encipher_only=False,
                                             decipher_only=False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                               critical=False)
                .sign(key, hashes.SHA256()))
        with open(cert_p, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.DER))
        with open(key_p, "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
        try:
            os.chmod(key_p, 0o600)      # POSIX 下仅本人可读；Windows 请靠目录权限保护 certs\\
        except OSError:
            pass
    cfg["_cert_path"], cfg["_key_path"] = cert_p, key_p
    return True


def _transmission_status(channel_count, good_count, data_age_sec, stale_sec):
    """Diagnostics.TransmissionStatus 判定（纯函数，便于单测）。

    无通道 -> "无数据"；有通道且有 Good 通道且数据年龄不超阈值 -> "正常"；
    其余（有通道但全不可信，或数据太旧，或从未有过数据时间）-> "数据较旧"。
    """
    try:
        n = int(channel_count or 0)
    except (TypeError, ValueError):
        n = 0
    try:
        g = int(good_count or 0)
    except (TypeError, ValueError):
        g = 0
    if n <= 0:
        return "无数据"
    try:
        age = float(data_age_sec)
        stale = float(stale_sec)
    except (TypeError, ValueError):
        return "数据较旧"
    if g > 0 and age <= stale:
        return "正常"
    return "数据较旧"


def make_test_record(cycle_id=1, num_points=600, now=None):
    """生成一条合成谱图记录，字段与 _read_source/_upsert 期望完全一致。

    只用于 SendTestData：写入独立的 Station_TEST 测试通道，绝不冒充真实仪器数据
    （analyzer/source/det_name 都带 TEST 标记，客户端一眼能分清）。
    曲线为正弦基线 + 三个高斯峰，与演示数据同风格，约 600 点。
    """
    n = max(10, int(num_points or 600))
    sp = 0.25
    t = [i * sp for i in range(n)]
    raw = []
    for v in t:
        x = 1000.0 + 300.0 * math.sin(v / 3.0)
        for c, a, w in ((30.0, 8000.0, 0.6), (75.0, 12000.0, 0.8), (120.0, 5000.0, 0.5)):
            x += a * math.exp(-((v - c) ** 2) / (2 * w * w))
        raw.append(float(x))
    uv = [float(v) for v in raw]
    val = [float(v) for v in uv]
    when = now or datetime.now()
    return dict(
        source="TEST-SEND", analyzer="TEST", app=0, app_name="测试应用",
        stream=0, stream_name="测试流路", det=0, det_name="TEST",
        cycle_id=int(cycle_id or 1), cycle_time=when.strftime("%Y-%m-%d %H:%M:%S"),
        is_validation=False, cal_level=0, num_points=n, sample_period=sp,
        duration=t[-1] if t else 0.0, min_raw=min(raw), max_raw=max(raw),
        y_title="uV", x_title="s", time=[float(v) for v in t],
        raw=raw, uv=uv, value=val,
    )


def _local_endpoint(endpoint):
    """把服务端监听地址换成本机客户端能连的地址（0.0.0.0 -> 127.0.0.1）。"""
    ep = str(endpoint or "").strip()
    return ep.replace("0.0.0.0", "127.0.0.1")


def _light_diagnosis(channel_count, good_count, data_age_sec, stale_sec):
    """服务端轻量诊断摘要（纯函数）：只用服务端已有计数，不连客户端、不扫文件。

    返回 (level, summary)，level 为 正常/注意/异常，与完整诊断 diagnose() 的 overall 同口径。
    完整五层诊断（配置/目录/日志/传输）只在 CLI/GUI/工具里跑，beat 里只刷新这个摘要。
    """
    try:
        n = int(channel_count or 0)
    except (TypeError, ValueError):
        n = 0
    try:
        g = int(good_count or 0)
    except (TypeError, ValueError):
        g = 0
    try:
        age = float(data_age_sec or 0)
    except (TypeError, ValueError):
        age = 0.0
    try:
        stale = float(stale_sec or 7200)
    except (TypeError, ValueError):
        stale = 7200.0
    if n <= 0:
        return "注意", "暂无数据通道：请检查监控目录与数据源（完整诊断请运行 --diagnose）"
    if g <= 0:
        return "异常", "共 %d 个通道但无一可信（Good=False）：数据源可能丢失，请查监控目录与源文件" % n
    if age > stale:
        return "注意", "通道 %d/%d 可信，但数据已 %.0f 秒未更新（阈值 %.0f 秒）：可能是色谱无新周期，请确认仪器周期" % (
            g, n, age, stale)
    if g < n:
        return "注意", "通道 %d/%d 可信，数据年龄 %.0f 秒：有 %d 个通道不可信，请查对应数据源" % (
            g, n, age, n - g)
    return "正常", "通道 %d/%d 正常，数据年龄 %.0f 秒" % (g, n, age)


class OpcUaApp:
    """构建并维护 OPC UA 地址空间。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.server = None
        self.idx = None
        self.root = None
        self.stations = {}   # serial -> node
        self.devices = {}    # (serial, app, stream, det) -> dict of nodes
        self.revision = 0
        self.latest_records = []
        self._lock = asyncio.Lock()
        self._last_export = -1e9
        self._export_lock = threading.Lock()   # asyncua 把同步方法放进线程池执行，必须防并发
        self._beats = 0
        # ---- 数据传输检测与发送（v2.4）----
        self._loop = None                      # start() 里保存事件循环，供同步方法桥接 async _upsert
        self._test_counter = 0
        self._test_cycle = 0
        self._last_test_send = -1e9
        self._test_send_lock = threading.Lock()
        self._echo_count = 0
        self._echo_lock = threading.Lock()
        self._server_start_monotonic = None
        self._server_start_str = ""
        self._last_data_change_monotonic = None
        self._last_data_change_str = ""
        self._channel_count = 0
        self._good_count = 0
        self._total_points = 0
        self.n_diag = None
        self.diag_nodes = {}
        # v3.1
        self.history = None
        self.modbus = None
        self.peaks = {}                        # 通道键 -> [峰]（最新周期）
        self._peak_cache = None
        self._mb_ctx = None

    async def start(self):
        from asyncua import Server, ua
        cfg = self.cfg
        self._loop = asyncio.get_running_loop()
        self._server_start_monotonic = time.monotonic()
        self._server_start_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        um = ChromUserManager(cfg.get("users"), cfg.get("allow_anonymous", True))
        self.user_manager = um
        try:
            self.server = Server(user_manager=um)
        except TypeError:                      # 旧版 asyncua
            self.server = Server()
            self.server.iserver.user_manager = um
        await self.server.init()
        try:
            self.server.allow_remote_admin(False)     # 绝不开放远程管理员
        except Exception:  # noqa
            pass
        try:
            self.server.iserver.max_connections = int(cfg.get("max_connections", 20))
            from asyncua.server.internal_session import InternalSession
            InternalSession.max_connections = int(cfg.get("max_connections", 20))
        except Exception:  # noqa
            pass
        self.server.set_endpoint(cfg["endpoint"])
        self.server.set_server_name(cfg.get("server_name") or "ChromExtract OPC UA Server")
        await self.server.set_application_uri(APP_URI)
        self.idx = await self.server.register_namespace(cfg.get("namespace_uri") or
                                                        "urn:chromextract:opcua")

        sec = str(cfg.get("security", "none")).lower()
        if sec in ("sign", "sign_encrypt"):
            await make_cert(cfg, cfg["_base_dir"])
            await self.server.load_certificate(cfg["_cert_path"])
            await self.server.load_private_key(cfg["_key_path"])
            secure = (ua.SecurityPolicyType.Basic256Sha256_Sign if sec == "sign"
                      else ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt)
            # 只开放加密端点；否则客户端可以选"不加密"绕过加密，等于没加密。
            policies = [secure]
            if cfg.get("allow_insecure_fallback"):
                policies.insert(0, ua.SecurityPolicyType.NoSecurity)
                get_log().warning("allow_insecure_fallback=true：同时开放了不加密端点（仅排障用）")
            self.server.set_security_policy(policies)
            get_log().info("安全模式：%s（证书在 %s）",
                           sec, os.path.join(cfg["_base_dir"], cfg.get("cert_dir", "certs")))
            self._setup_client_whitelist(cfg)
        else:
            self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

        objects = self.server.nodes.objects
        self.root = await objects.add_object(self.idx, "Chromatograph")
        # 服务器级节点
        self.n_server_info = await self.root.add_variable(
            self.idx, "ServerVersion", __version__)
        self.n_status = await self.root.add_variable(self.idx, "WatchDirs",
                                                     ",".join(cfg.get("watch_dirs") or []))
        # 心跳：每轮扫描 +1，客户端据此判断服务是否卡死（没有新周期时数据本身不会变）
        self.n_beat = await self.root.add_variable(self.idx, "Heartbeat", 0)
        self.n_scan = await self.root.add_variable(self.idx, "LastScanTime", "")
        # 告警：客户端/DCS 可直接订阅 Alarms.ActiveCount 或 Alarms.Severity 做报警
        al = await self.root.add_object(self.idx, "Alarms")
        self.n_al_count = await al.add_variable(self.idx, "ActiveCount", 0)
        self.n_al_sev = await al.add_variable(self.idx, "Severity", 0)        # 0 正常 / 1 警告 / 2 严重
        self.n_al_text = await al.add_variable(self.idx, "Text", "")
        self.n_al_time = await al.add_variable(self.idx, "LastChange", "")
        self.n_al_quiet = await al.add_variable(self.idx, "NotifySilenced", False)   # 推送是否处于静默时段
        # 远程方法：ExportCSV -> 导出最新数据为 CSV，返回输出路径
        self.n_export = await self.root.add_method(
            self.idx, "ExportCSV", self._export_csv, [],
            [ua.VariantType.String])
        # ---- 数据传输检测与发送（v2.4）：Diagnostics 只读诊断节点 + 两个方法 ----
        # 全部只读：远程用户最高只读（ChromUserManager），这些变量同样不可被客户端改写；
        # 发送测试数据走方法调用（服务端内部 _upsert），不开任何写权限。
        self.n_diag = await self.root.add_object(self.idx, "Diagnostics")
        vt = ua.VariantType
        self.diag_nodes = dict(
            test_signal=await self.n_diag.add_variable(
                self.idx, "TestSignal", ua.Variant(0.0, vt.Double)),
            test_counter=await self.n_diag.add_variable(
                self.idx, "TestCounter", ua.Variant(0, vt.Int64)),
            channel_count=await self.n_diag.add_variable(
                self.idx, "ChannelCount", ua.Variant(0, vt.Int32)),
            good_count=await self.n_diag.add_variable(
                self.idx, "GoodChannelCount", ua.Variant(0, vt.Int32)),
            total_points=await self.n_diag.add_variable(
                self.idx, "TotalPoints", ua.Variant(0, vt.Int64)),
            last_change=await self.n_diag.add_variable(
                self.idx, "LastDataChangeTime", ""),
            data_age=await self.n_diag.add_variable(
                self.idx, "DataAgeSeconds", ua.Variant(0.0, vt.Double)),
            status=await self.n_diag.add_variable(
                self.idx, "TransmissionStatus", "无数据"),
            start_time=await self.n_diag.add_variable(
                self.idx, "ServerStartTime", self._server_start_str),
            uptime=await self.n_diag.add_variable(
                self.idx, "UptimeSeconds", ua.Variant(0.0, vt.Double)),
            diagnosis_level=await self.n_diag.add_variable(
                self.idx, "DiagnosisLevel", "注意"),
            diagnosis_summary=await self.n_diag.add_variable(
                self.idx, "DiagnosisSummary", "服务刚启动，等待第一轮扫描"),
            last_diagnosis_time=await self.n_diag.add_variable(
                self.idx, "LastDiagnosisTime", ""),
        )
        self.n_send_test = await self.root.add_method(
            self.idx, "SendTestData", self._send_test_data, [],
            [vt.String, vt.Int64])
        self.n_echo = await self.root.add_method(
            self.idx, "EchoTest", self._echo_test, [vt.String],
            [vt.String, vt.String, vt.Int64])
        await self.server.start()
        get_log().info("OPC UA 服务已启动：%s", cfg["endpoint"])

    def _setup_client_whitelist(self, cfg):
        """certs/trusted 里放了客户端证书，就只允许这些客户端连接。"""
        from asyncua import ua
        from asyncua.common.utils import ServiceError
        from cryptography.hazmat.primitives import hashes
        tdir = os.path.join(cfg["_base_dir"], cfg.get("trusted_client_certs_dir") or "certs/trusted")
        os.makedirs(tdir, exist_ok=True)
        trusted = load_trusted_fingerprints(tdir)
        if not trusted:
            get_log().warning("未启用客户端证书白名单：%s 为空，任何持自签名证书的客户端都能连接", tdir)
            return

        async def validator(cert, app_description):
            if cert.fingerprint(hashes.SHA256()).hex() not in trusted:
                get_log().warning("已拒绝不在白名单内的客户端证书（%s）",
                                  chrom_core.clean_text(str(getattr(app_description, "ApplicationUri", "")), 80))
                raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)

        self.server.set_certificate_validator(validator)
        get_log().info("已启用客户端证书白名单：%d 个客户端", len(trusted))

    async def write_alarms(self, snap):
        sev = {"正常": 0, "警告": 1, "严重": 2}.get(snap["worst"], 0)
        text = "；".join("[%s] %s" % (a["level"], a["text"]) for a in snap["active"])[:2000]
        try:
            await self.n_al_quiet.write_value(bool(snap.get("quiet")))
            if (sev, text) != getattr(self, "_al_last", None):
                self._al_last = (sev, text)
                await self.n_al_count.write_value(len(snap["active"]))
                await self.n_al_sev.write_value(sev)
                await self.n_al_text.write_value(text)
                await self.n_al_time.write_value(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa
            pass
    def _diag_age_uptime(self):
        now = time.monotonic()
        uptime = (now - self._server_start_monotonic) if self._server_start_monotonic else 0.0
        age = (now - self._last_data_change_monotonic) if self._last_data_change_monotonic else 0.0
        return float(age), float(uptime)

    async def _write_diag(self):
        """把当前诊断计数写进 Diagnostics 节点（beat 与 apply_snapshot 共用）。"""
        if not self.diag_nodes:
            return
        from asyncua import ua
        age, uptime = self._diag_age_uptime()
        stale = self.cfg.get("transmission_stale_sec", 7200)
        status = _transmission_status(self._channel_count, self._good_count, age, stale)
        d = self.diag_nodes
        await d["test_counter"].write_value(ua.Variant(int(self._test_counter), ua.VariantType.Int64))
        await d["test_signal"].write_value(ua.Variant(float(math.sin(float(self._test_counter))),
                                                       ua.VariantType.Double))
        await d["channel_count"].write_value(ua.Variant(int(self._channel_count), ua.VariantType.Int32))
        await d["good_count"].write_value(ua.Variant(int(self._good_count), ua.VariantType.Int32))
        await d["total_points"].write_value(ua.Variant(int(self._total_points), ua.VariantType.Int64))
        await d["last_change"].write_value(self._last_data_change_str or "")
        await d["data_age"].write_value(ua.Variant(float(age), ua.VariantType.Double))
        await d["status"].write_value(status)
        await d["uptime"].write_value(ua.Variant(float(uptime), ua.VariantType.Double))
        # 智能诊断轻量摘要（服务端证据，beat/apply_snapshot 每次刷新；完整诊断见 diagnose()）
        if "diagnosis_level" in d:
            level, summary = _light_diagnosis(self._channel_count, self._good_count, age, stale)
            await d["diagnosis_level"].write_value(level)
            await d["diagnosis_summary"].write_value(summary)
            await d["last_diagnosis_time"].write_value(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    async def beat(self):
        self._beats += 1
        self._test_counter += 1
        try:
            await self.n_beat.write_value(self._beats)
            await self.n_scan.write_value(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            await self._write_diag()
        except Exception:  # noqa
            pass

    async def _upsert_test(self, record):
        """只把测试记录写进 Station_TEST（与 apply_snapshot 共用同一把锁串行化）。
        绝不改 latest_records、绝不把真实通道标 Good=False。"""
        async with self._lock:
            await self._upsert(record)

    @staticmethod
    def _send_test_data(parent=None):
        """OPC UA 方法（线程池执行）：生成一条合成谱图并发送到 Station_TEST 测试通道。

        受 allow_test_send 开关与 test_send_min_interval_sec 限流控制；
        通过事件循环桥接 async _upsert_test，真实通道的 Good 状态不受影响。
        返回 [说明 String, 点数 Int64]；被拒绝时返回标准状态码。
        """
        from asyncua import ua
        app = OpcUaApp.current
        cfg = app.cfg
        if not cfg.get("allow_test_send", True):
            get_log().warning("SendTestData 被调用，但 allow_test_send=false，已拒绝")
            return ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)
        if not app._test_send_lock.acquire(blocking=False):
            return ua.StatusCode(ua.StatusCodes.BadTooManyOperations)
        try:
            now = time.monotonic()
            if now - app._last_test_send < float(cfg.get("test_send_min_interval_sec", 5)):
                return ua.StatusCode(ua.StatusCodes.BadTooManyOperations)
            app._last_test_send = now
            app._test_cycle += 1
            record = make_test_record(cycle_id=app._test_cycle)
            loop = app._loop
            if loop is None or not loop.is_running():
                get_log().error("SendTestData 失败：服务事件循环不可用")
                return ua.StatusCode(ua.StatusCodes.BadInternalError)
            fut = asyncio.run_coroutine_threadsafe(app._upsert_test(record), loop)
            try:
                fut.result(timeout=10)
            except Exception as e:  # noqa
                get_log().error("SendTestData 写入测试通道失败：%s", chrom_core.clean_text(str(e), 200))
                return ua.StatusCode(ua.StatusCodes.BadInternalError)
            n = int(record["num_points"])
            get_log().info("远程调用 SendTestData：已发送测试数据到 Station_TEST（%d 点，周期 %d）",
                           n, app._test_cycle)
            return [ua.Variant("已发送测试数据到 Station_TEST（%d 点）" % n, ua.VariantType.String),
                    ua.Variant(n, ua.VariantType.Int64)]
        finally:
            app._test_send_lock.release()

    @staticmethod
    def _echo_test(parent=None, text=""):
        """OPC UA 方法（线程池执行）：原样回显入参，证明双向传输通畅。

        返回 [回显 String, 服务端时间 String, 调用计数 Int64]。
        日志里的外部文本一律经 clean_text 清洗，防日志注入。
        """
        from asyncua import ua
        app = OpcUaApp.current
        raw = text
        if hasattr(raw, "Value"):
            raw = raw.Value
        echo = "" if raw is None else str(raw)
        with app._echo_lock:
            app._echo_count += 1
            count = app._echo_count
        get_log().info("远程调用 EchoTest（第 %d 次）：%s", count,
                       chrom_core.clean_text(echo, 80))
        return [ua.Variant(echo, ua.VariantType.String),
                ua.Variant(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), ua.VariantType.String),
                ua.Variant(int(count), ua.VariantType.Int64)]

    @staticmethod
    def _export_csv(parent=None):
        """OPC UA 方法调用（asyncua 在线程池里执行）：导出当前数据为 CSV。
        被拒绝时直接返回标准状态码，客户端能看到明确原因，而不是"未知错误"。"""
        from asyncua import ua
        app = OpcUaApp.current
        cfg = app.cfg
        if not cfg.get("remote_export", True):
            get_log().warning("ExportCSV 被调用，但 remote_export=false，已拒绝")
            return ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)
        if not app._export_lock.acquire(blocking=False):      # 已有一次导出在进行
            return ua.StatusCode(ua.StatusCodes.BadTooManyOperations)
        try:
            now = time.monotonic()
            if now - app._last_export < float(cfg.get("export_min_interval_sec", 10)):
                return ua.StatusCode(ua.StatusCodes.BadTooManyOperations)
            app._last_export = now
            outdir = os.path.join(cfg["_base_dir"], cfg.get("csv_export_dir") or "谱图导出")
            n = 0
            recs = app.latest_records
            if recs:
                n = _write_records_csv(recs, outdir)
            get_log().info("远程调用 ExportCSV：%d 条 -> %s", n, outdir)
            return [ua.Variant(os.path.abspath(outdir), ua.VariantType.String)]
        finally:
            app._export_lock.release()

    async def apply_snapshot(self, records):
        async with self._lock:   # 串行化，避免两次快照同时创建同名节点
            self.latest_records = records
            self.revision += 1
            seen = set()
            ok_cnt, total_pts = 0, 0
            for r in records:
                seen.add((r["analyzer"], r["app"], r["stream"], r["det"]))
                try:
                    await self._upsert(r)
                    ok_cnt += 1
                    total_pts += int(r.get("num_points") or 0)
                except Exception as e:  # noqa
                    get_log().error("更新节点失败（%s）：%s", r["source"], e)
            # 数据源丢失/读不了时，旧数据仍留在节点里；必须明确标记为"不可信"，不能让 DCS 把陈旧值当真
            # Station_TEST 测试通道除外：它由 SendTestData 单独维护，不随真实快照变 Good。
            for key, nodes in self.devices.items():
                if key not in seen and key[0] != "TEST":
                    try:
                        await nodes["good"].write_value(False)
                    except Exception:  # noqa
                        pass
            # Diagnostics：通道计数只统计真实快照（不含 Station_TEST），诚实反映真实数据新鲜度
            self._channel_count = len(records)
            self._good_count = ok_cnt
            self._total_points = total_pts
            self._last_data_change_monotonic = time.monotonic()
            self._last_data_change_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                await self._write_diag()
            except Exception:  # noqa
                pass
            try:
                await self.n_server_info.write_value(__version__)
            except Exception:  # noqa
                pass
        self.update_modbus()

    # ---- v3.1：峰识别 / Modbus 转发 ----
    def compute_peaks(self, r):
        """最新周期的峰（按通道 + 周期号缓存）。参数取当前配置（热加载后立即按新参数算）。"""
        import peaks as _peaks
        if self._peak_cache is None:
            self._peak_cache = _peaks.PeakCache()
        params = _peaks.params_from_config(self.cfg)
        series = self.cfg.get("peak_series", "value")
        key = _channel_key(r)
        return self._peak_cache.get(key, r.get("cycle_id"), params, series,
                                    lambda: _peaks.peaks_for_record(r, params, series)["peaks"])

    def _needs_peaks(self):
        if self.cfg.get("peaks_publish"):
            return True
        return self.modbus is not None and any(str(m.get("field", "")).startswith("peak")
                                               for m in self.cfg.get("modbus_server_map") or [])

    def update_modbus(self, alert_snap=None):
        if self.modbus is None:
            return
        import modbus_server as _mbs
        try:
            if self._mb_ctx is None:
                self._mb_ctx = _mbs.Context()
            if self._needs_peaks():
                for r in self.latest_records:
                    try:
                        self.peaks[_channel_key(r)] = self.compute_peaks(r)
                    except Exception:  # noqa
                        pass
            clients = self.client_count()
            _mbs.update_context(self._mb_ctx, self.latest_records, alert_snap, self.peaks, clients, _channel_key)
            self.modbus.update(_mbs.build_registers(self.cfg.get("modbus_server_map") or [], self._mb_ctx,
                                                    self.cfg.get("modbus_server_order", "ABCD")))
        except Exception as e:  # noqa
            get_log().error("Modbus 转发寄存器更新失败：%s", e)

    def client_count(self):
        """当前 OPC UA 客户端连接数；取不到返回 None（不记录，避免把“取不到”记成 0）。"""
        try:
            import desktop_features as _df
            res = _df.collect_sessions(self)
            return int(res.get("count") or 0) if res.get("ok") else None
        except Exception:  # noqa
            return None

    def after_alarms(self, snap):
        """告警循环（每 15 秒）：记录告警出现 / 恢复和客户端连接数，刷新 Modbus 心跳与告警数。"""
        clients = self.client_count()
        if self.history is not None:
            try:
                self.history.track_alerts(snap)
                self.history.track_clients(clients)
            except Exception as e:  # noqa
                get_log().error("历史事件写入失败：%s", e)
        self.update_modbus(snap)

    async def _write_peaks(self, r, n):
        """在通道下发布 Peaks（节点名固定英文，不随界面语言变化）。"""
        from asyncua import ua
        import peaks as _peaks
        try:
            pk = self.compute_peaks(r)
        except Exception as e:  # noqa
            get_log().warning("峰识别失败（%s）：%s", r.get("source"), e)
            pk = []
        self.peaks[_channel_key(r)] = pk
        arr = _peaks.opcua_arrays(pk, self.cfg.get("peaks_publish_max", 50))
        dvt = ua.VariantType.Double
        await n["pk_count"].write_value(ua.Variant(int(arr["Count"]), ua.VariantType.Int32))
        for name in ("RetentionTimes", "Heights", "Areas", "AreaPercents", "Widths"):
            await n["pk_" + name].write_value(ua.Variant(arr[name], dvt))

    async def _station(self, serial, source):
        name = "Station_" + _safe_node(serial or _safe_node(source))
        if name not in self.stations:
            st = await self.root.add_object(self.idx, name)
            info = await st.add_object(self.idx, "Info")
            await info.add_variable(self.idx, "SerialNumber", serial or "")
            await info.add_variable(self.idx, "SourceFile", source)
            self.stations[name] = st
        return self.stations[name]

    async def _upsert(self, r):
        from asyncua import ua
        st = await self._station(r["analyzer"], r["source"])
        dev_name = "App%s_Stream%s_Det%s" % (
            _safe_node(r["app"] if r["app"] is not None else "-"),
            _safe_node(r["stream"] if r["stream"] is not None else "-"),
            _safe_node(r["det"] if r["det"] is not None else "-"))
        key = (r["analyzer"], r["app"], r["stream"], r["det"])
        if key not in self.devices:
            dev = await st.add_object(self.idx, dev_name)
            lc = await dev.add_object(self.idx, "LatestCycle")
            tr = await dev.add_object(self.idx, "Trace")
            sts = await dev.add_object(self.idx, "Status")
            empty_f = ua.Variant([], ua.VariantType.Double)
            nodes = dict(
                cycle_time=await lc.add_variable(self.idx, "CycleTime", ""),
                cycle_id=await lc.add_variable(self.idx, "CycleId", 0),
                is_val=await lc.add_variable(self.idx, "IsValidation", False),
                cal=await lc.add_variable(self.idx, "CalibrationLevel", 0),
                npts=await lc.add_variable(self.idx, "NumPoints", 0),
                sp=await lc.add_variable(self.idx, "SamplePeriod", 0.0),
                dur=await lc.add_variable(self.idx, "Duration", 0.0),
                det_name=await lc.add_variable(self.idx, "DetName", ""),
                y_title=await lc.add_variable(self.idx, "YTitle", ""),
                x_title=await lc.add_variable(self.idx, "XTitle", ""),
                min_raw=await lc.add_variable(self.idx, "MinRaw", 0.0),
                max_raw=await lc.add_variable(self.idx, "MaxRaw", 0.0),
                t_arr=await tr.add_variable(self.idx, "TimeArray", empty_f),
                raw_arr=await tr.add_variable(self.idx, "RawArray", empty_f),
                uv_arr=await tr.add_variable(self.idx, "MicrovoltArray", empty_f),
                val_arr=await tr.add_variable(self.idx, "ValueArray", empty_f),
                rev=await sts.add_variable(self.idx, "Revision", 0),
                last=await sts.add_variable(self.idx, "LastUpdate", ""),
                good=await sts.add_variable(self.idx, "Good", False),
                app_name=await dev.add_variable(self.idx, "AppName", r["app_name"] or ""),
                stream_name=await dev.add_variable(self.idx, "StreamName", r["stream_name"] or ""),
            )
            if self.cfg.get("peaks_publish"):
                pko = await dev.add_object(self.idx, "Peaks")
                nodes["pk_count"] = await pko.add_variable(self.idx, "Count", ua.Variant(0, ua.VariantType.Int32))
                for name in ("RetentionTimes", "Heights", "Areas", "AreaPercents", "Widths"):
                    nodes["pk_" + name] = await pko.add_variable(self.idx, name, empty_f)
            self.devices[key] = nodes
            get_log().info("新增节点：%s / %s", "Chromatograph", dev_name)
        n = self.devices[key]
        vt = self.cfg.get("max_points", 20000)
        dvt = ua.VariantType.Double
        await n["cycle_time"].write_value(r["cycle_time"])
        await n["cycle_id"].write_value(int(r["cycle_id"] or 0))
        await n["is_val"].write_value(bool(r["is_validation"]))
        await n["cal"].write_value(int(r["cal_level"] or 0))
        await n["npts"].write_value(int(r["num_points"]))
        await n["sp"].write_value(float(r["sample_period"] or 0))
        await n["dur"].write_value(float(r["duration"] or 0))
        await n["det_name"].write_value(r["det_name"] or "")
        await n["y_title"].write_value(r["y_title"] or "")
        await n["x_title"].write_value(r["x_title"] or "")
        await n["min_raw"].write_value(float(r["min_raw"]))
        await n["max_raw"].write_value(float(r["max_raw"]))
        await n["app_name"].write_value(r["app_name"] or "")
        await n["stream_name"].write_value(r["stream_name"] or "")
        await n["t_arr"].write_value(ua.Variant(r["time"], dvt))
        await n["raw_arr"].write_value(ua.Variant(r["raw"], dvt))
        await n["uv_arr"].write_value(ua.Variant(r["uv"], dvt))
        await n["val_arr"].write_value(ua.Variant(r["value"], dvt))
        await n["rev"].write_value(self.revision)
        await n["last"].write_value(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        await n["good"].write_value(True)
        if "pk_count" in n:
            await self._write_peaks(r, n)


def _write_records_csv(records, outdir):
    """把快照记录写成 index.csv + 每条谱图一个 CSV。"""
    import csv as _csv
    os.makedirs(outdir, exist_ok=True)
    csv_dir = os.path.join(outdir, "csv")
    os.makedirs(csv_dir, exist_ok=True)
    rows = []
    for r in records:
        label = _safe_node("%s_app%s_str%s_det%s_%s" % (
            os.path.splitext(r["source"])[0], r["app"], r["stream"], r["det"],
            r["cycle_time"]))
        p = os.path.join(csv_dir, label + ".csv")
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["time_s", "time_%s" % chrom_core.safe(r["x_title"] or "x"),
                        "raw_counts", "microvolt", "value_%s" % chrom_core.safe(r["y_title"] or "y")])
            for row in zip(r["time"], r["raw"], r["uv"], r["value"]):
                w.writerow(["%.6g" % row[0], "%.6g" % row[1], "%.8g" % row[2], "%.8g" % row[3]])
        rows.append([chrom_core.csv_cell(x) for x in (
            r["source"], r["analyzer"], r["app"], r["app_name"], r["stream"],
            r["stream_name"], r["det"], r["det_name"], r["cycle_id"], r["cycle_time"],
            r["is_validation"], r["cal_level"], r["num_points"], r["sample_period"],
            r["duration"], r["min_raw"], r["max_raw"],
            os.path.join("csv", label + ".csv"))])
    with open(os.path.join(outdir, "index.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = _csv.writer(f)
        w.writerow(["来源文件", "仪器序列号", "应用号", "应用名", "流路号", "流路名", "检测器号",
                    "检测器名", "周期编号", "周期时间", "是否验证周期", "标定级别", "点数",
                    "采样间隔(秒)", "总时长(秒)", "原始值最小", "原始值最大", "CSV文件"])
        w.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------------------
# 异步主循环
# ---------------------------------------------------------------------------

async def run_server(cfg, stop_evt=None):
    cfg["_base_dir"] = cfg.get("_base_dir") or os.getcwd()
    app = OpcUaApp(cfg)
    OpcUaApp.current = app
    await app.start()

    loop = asyncio.get_running_loop()
    stop_evt = stop_evt or asyncio.Event()
    pending = set()

    def beat_cb():
        loop.call_soon_threadsafe(_spawn_beat)

    def _spawn_beat():
        t = asyncio.ensure_future(app.beat())
        pending.add(t)
        t.add_done_callback(pending.discard)

    def _spawn(recs):
        t = asyncio.ensure_future(app.apply_snapshot(recs))
        pending.add(t)                       # 持有引用，防止任务被垃圾回收
        t.add_done_callback(pending.discard)

    def snapshot_cb(recs):
        loop.call_soon_threadsafe(_spawn, recs)

    stop_flag = threading.Event()
    notifier = alerts.make_notifier(cfg, get_log())
    monitor = alerts.AlertMonitor(cfg, log=get_log(), notifier=notifier)
    notifier.is_muted = lambda k: (k.startswith(("stale:", "missing:")) and monitor._muted(k.split(":", 1)[1]))
    import probes
    probe_mon = probes.ProbeMonitor(cfg, monitor, log=get_log()).start()
    ALERTS["probes"] = probe_mon
    import envwatch
    env_mon = envwatch.EnvWatch(cfg, monitor, log=get_log()).start()
    ALERTS["env"] = env_mon
    ALERTS["monitor"] = monitor
    # v3.1：历史数据与运行事件（同一进程内，不新增后台进程）、Modbus TCP 转发
    import history as _history
    import modbus_server as _mbs
    hist = _history.open_from_config(cfg, get_log())
    app.history = hist
    if hist is not None:
        try:
            hist.service_started()
        except Exception as e:  # noqa
            get_log().error("历史数据库写入失败：%s", e)
    app.modbus = _mbs.start_from_config(cfg, get_log())
    ALERTS["modbus"] = app.modbus

    def history_cb(recs):
        if hist is None:
            return
        try:
            n = hist.record_records(recs, labeler=_channel_label)
            if n:
                get_log().info("历史数据：新记录 %d 个周期", n)
        except Exception as e:  # noqa
            get_log().error("历史数据写入失败：%s", e)

    th = threading.Thread(target=watcher_thread,
                          args=(snapshot_cb, stop_flag, cfg, beat_cb, app.user_manager.reload, monitor,
                                history_cb),
                          daemon=True)
    th.start()

    async def alarm_loop():
        # 独立于扫描线程：扫描线程卡死时也能发现并告警
        while not stop_evt.is_set():
            try:
                monitor.evaluate()
                notifier.tick(monitor.active_alerts())     # 静默结束后补发仍未恢复的告警
                snap = monitor.snapshot()
                await app.write_alarms(snap)
                app.after_alarms(snap)
            except Exception as e:  # noqa
                get_log().error("告警检查出错：%s", e)
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
    alarm_task = asyncio.ensure_future(alarm_loop())
    pending.add(alarm_task)
    get_log().info("数据监控已启动，监控目录：%s（每 %s 秒扫描）",
                   cfg.get("watch_dirs") or ["（空）"], cfg.get("poll_seconds", 30))

    try:
        await stop_evt.wait()
    finally:
        stop_flag.set()
        alarm_task.cancel()
        probe_mon.stop()
        env_mon.stop()
        ALERTS.pop("env", None)
        ALERTS.pop("monitor", None)
        ALERTS.pop("probes", None)
        ALERTS.pop("modbus", None)
        if app.modbus is not None:
            app.modbus.stop()
        if hist is not None:
            try:
                hist.service_stopped()
            except Exception:  # noqa
                pass
            hist.close()
        await app.server.stop()
        get_log().info("服务已停止")


# ---------------------------------------------------------------------------
# 数据传输检测（客户端）：真的连上、读数、订阅、回显，证明数据在传
# ---------------------------------------------------------------------------

class _SubHandler:
    """订阅回调：只记录通知（节点、值、时刻），不做任何写操作。"""

    def __init__(self):
        self.events = []   # [(monotonic, nodeid_str, value)]

    def datachange_notification(self, node, val, data):
        try:
            nid = str(node.nodeid) if hasattr(node, "nodeid") else str(node)
        except Exception:  # noqa
            nid = "?"
        self.events.append((time.monotonic(), nid, val))


async def transmission_check_async(endpoint, namespace_uri="urn:chromextract:opcua",
                                   security="none", cert_dir="certs", user=None,
                                   password=None, duration_sec=6, do_send_test=False):
    """OPC UA 数据传输检测（只读 + 方法调用，绝不写变量）。

    返回 {"ok": bool, "endpoint": endpoint, "steps": [{name, ok, detail, latency_ms}, ...]}。
    任何一步失败都记录在 steps 里并继续能做的步骤，不抛异常（连接失败时直接返回）。
    订阅一步：订阅建立成功即算传输通道可用；窗口内无变化多半是心跳/扫描间隔较长，
    detail 里会如实说明，不误判为失败。无真实通道时读曲线一步记为跳过（ok=True）。
    """
    from asyncua import Client, ua

    steps = []

    def _add(name, ok, detail, latency_ms=None):
        steps.append(dict(name=name, ok=bool(ok), detail=str(detail),
                          latency_ms=(round(float(latency_ms), 1) if latency_ms is not None else None)))

    endpoint = str(endpoint or "").strip()
    try:
        dur = max(0.5, float(duration_sec or 6))
    except (TypeError, ValueError):
        dur = 6.0
    client = Client(url=endpoint, timeout=10)
    connected = False
    try:
        # 1) 连接建会话
        t0 = time.perf_counter()
        try:
            if user is not None:
                client.set_user(user)
                client.set_password(password or "")
            sec = str(security or "none").lower()
            if sec != "none":
                from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
                client.application_uri = APP_URI
                await client.set_security(
                    SecurityPolicyBasic256Sha256,
                    certificate=os.path.join(cert_dir, "server_cert.der"),
                    private_key=os.path.join(cert_dir, "server_key.pem"),
                    mode=(ua.MessageSecurityMode.SignAndEncrypt if sec == "sign_encrypt"
                          else ua.MessageSecurityMode.Sign))
            await client.connect()
            connected = True
            ns = await client.get_namespace_index(namespace_uri)
            root = await client.nodes.objects.get_child(["%d:Chromatograph" % ns])
            _add("连接建会话", True, "已连接 %s（命名空间 ns=%d）" % (endpoint, ns),
                 (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa
            _add("连接建会话", False, "%s: %s" % (type(e).__name__, e),
                 (time.perf_counter() - t0) * 1000)
            return dict(ok=False, endpoint=endpoint, steps=steps)

        # 2) 读 ServerVersion
        t0 = time.perf_counter()
        try:
            ver = await (await root.get_child(["%d:ServerVersion" % ns])).read_value()
            _add("读 ServerVersion", True, "服务版本：%s" % ver,
                 (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa
            _add("读 ServerVersion", False, "%s: %s" % (type(e).__name__, e),
                 (time.perf_counter() - t0) * 1000)

        # 3) 读 Diagnostics/TestCounter（顺带读状态，方便人看）
        t0 = time.perf_counter()
        diag = None
        try:
            diag = await root.get_child(["%d:Diagnostics" % ns])
            counter = await (await diag.get_child(["%d:TestCounter" % ns])).read_value()
            status = await (await diag.get_child(["%d:TransmissionStatus" % ns])).read_value()
            _add("读 Diagnostics/TestCounter", True,
                 "TestCounter=%s，TransmissionStatus=%s" % (counter, status),
                 (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa
            _add("读 Diagnostics/TestCounter", False,
                 "%s: %s（服务端可能是旧版本，没有 Diagnostics 节点）" % (type(e).__name__, e),
                 (time.perf_counter() - t0) * 1000)

        # 4) 订阅 TestSignal + Heartbeat
        t0 = time.perf_counter()
        try:
            if diag is None:
                diag = await root.get_child(["%d:Diagnostics" % ns])
            sig_node = await diag.get_child(["%d:TestSignal" % ns])
            hb_node = await root.get_child(["%d:Heartbeat" % ns])
            handler = _SubHandler()
            sub = await client.create_subscription(100, handler)
            try:
                await sub.subscribe_data_change([sig_node, hb_node])
                await asyncio.sleep(dur)
            finally:
                try:
                    await sub.delete()
                except Exception:  # noqa
                    pass
            ev = handler.events
            if len(ev) >= 2:
                gaps = [ev[i + 1][0] - ev[i][0] for i in range(len(ev) - 1)]
                avg = sum(gaps) / len(gaps) if gaps else 0.0
                detail = "收到 %d 条通知，平均间隔 %.2f 秒" % (len(ev), avg)
            elif len(ev) == 1:
                detail = ("收到 1 条通知（订阅初始值），窗口内无新变化；"
                          "心跳随扫描轮次更新，间隔较长时属正常，订阅通道本身可用")
            else:
                detail = ("订阅已建立，但窗口内未收到通知；心跳随扫描轮次更新"
                          "（扫描间隔较长时窗口内无变化属正常），请加长 --check-seconds 再确认")
            _add("订阅 TestSignal+Heartbeat", True, detail,
                 (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa
            _add("订阅 TestSignal+Heartbeat", False, "%s: %s" % (type(e).__name__, e),
                 (time.perf_counter() - t0) * 1000)

        # 5) 读一个真实通道的 Trace 数组（无通道记跳过，不崩溃）
        t0 = time.perf_counter()
        try:
            found = None
            for st in await root.get_children():
                sname = (await st.read_browse_name()).Name
                if not sname.startswith("Station_") or sname == "Station_TEST":
                    continue
                for dev in await st.get_children():
                    dname = (await dev.read_browse_name()).Name
                    if dname.startswith("App"):
                        found = (sname, dname, dev)
                        break
                if found:
                    break
            if found is None:
                _add("读真实通道曲线", True,
                     "跳过（无数据）：暂无真实检测器通道，传输通道本身已由订阅证明可用",
                     (time.perf_counter() - t0) * 1000)
            else:
                sname, dname, dev = found
                npts = await (await dev.get_child(
                    ["%d:LatestCycle" % ns, "%d:NumPoints" % ns])).read_value()
                arr = await (await dev.get_child(
                    ["%d:Trace" % ns, "%d:ValueArray" % ns])).read_value()
                n = len(arr) if arr is not None else 0
                consistent = (int(npts or 0) == n)
                _add("读真实通道曲线", consistent and n > 0,
                     "%s/%s：NumPoints=%s，ValueArray %d 点%s" % (
                         sname, dname, npts, n, "" if consistent else "（不一致！）"),
                     (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa
            _add("读真实通道曲线", False, "%s: %s" % (type(e).__name__, e),
                 (time.perf_counter() - t0) * 1000)

        # 6) EchoTest 往返
        t0 = time.perf_counter()
        try:
            probe = "ChromExtractOPC-transmission-check"
            out = await root.call_method("%d:EchoTest" % ns,
                                         ua.Variant(probe, ua.VariantType.String))
            vals = list(out) if isinstance(out, (list, tuple)) else [out]
            echoed = vals[0] if vals else None
            if hasattr(echoed, "Value"):
                echoed = echoed.Value
            ok = (echoed == probe)
            _add("EchoTest 往返", ok,
                 "发送 %r，收到 %r（服务端时间 %s，计数 %s）" % (
                     probe, echoed,
                     vals[1] if len(vals) > 1 else "?",
                     vals[2] if len(vals) > 2 else "?"),
                 (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa
            _add("EchoTest 往返", False,
                 "%s: %s（服务端可能是旧版本，没有 EchoTest 方法）" % (type(e).__name__, e),
                 (time.perf_counter() - t0) * 1000)

        # 7) 可选：SendTestData 并验证 Station_TEST 可读
        if do_send_test:
            t0 = time.perf_counter()
            try:
                out = await root.call_method("%d:SendTestData" % ns)
                vals = list(out) if isinstance(out, (list, tuple)) else [out]
                msg = vals[0] if vals else ""
                if hasattr(msg, "Value"):
                    msg = msg.Value
                test_st = await root.get_child(["%d:Station_TEST" % ns])
                devs = [d for d in await test_st.get_children()
                        if (await d.read_browse_name()).Name.startswith("App")]
                if not devs:
                    _add("SendTestData 发送测试数据", False,
                         "方法已调用（%s），但 Station_TEST 下没有通道" % msg,
                         (time.perf_counter() - t0) * 1000)
                else:
                    dev = devs[0]
                    npts = await (await dev.get_child(
                        ["%d:LatestCycle" % ns, "%d:NumPoints" % ns])).read_value()
                    arr = await (await dev.get_child(
                        ["%d:Trace" % ns, "%d:ValueArray" % ns])).read_value()
                    n = len(arr) if arr is not None else 0
                    _add("SendTestData 发送测试数据", n > 0 and int(npts or 0) == n,
                         "%s；Station_TEST 可读：NumPoints=%s，ValueArray %d 点" % (msg, npts, n),
                         (time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa
                _add("SendTestData 发送测试数据", False,
                     "%s: %s" % (type(e).__name__, e),
                     (time.perf_counter() - t0) * 1000)

        return dict(ok=all(s["ok"] for s in steps), endpoint=endpoint, steps=steps)
    finally:
        if connected:
            try:
                await client.disconnect()
            except Exception:  # noqa
                pass


def _print_transmission_result(result):
    print("数据传输检测：%s" % result.get("endpoint", ""))
    for s in result.get("steps", []):
        lat = ("（%.0f ms）" % s["latency_ms"]) if s.get("latency_ms") is not None else ""
        print("  %s %s：%s%s" % ("[ 通过 ]" if s["ok"] else "[ 失败 ]", s["name"], s["detail"], lat))
    print("[ 通过 ] 数据传输正常" if result.get("ok") else "[ 失败 ] 数据传输有问题，见上面失败步骤")


def run_transmission_check(endpoint=None, cfg=None, duration_sec=6, do_send_test=False,
                           security=None, cert_dir=None, user=None, password=None,
                           namespace_uri=None):
    """同步封装：跑一遍 transmission_check_async，打印中文报告，返回 0/1。"""
    _force_utf8_stdout()
    if not endpoint:
        endpoint = _local_endpoint((cfg or {}).get("endpoint") or DEFAULT_CONFIG["endpoint"])
    if security is None:
        security = (cfg or {}).get("security", "none")
    if cert_dir is None:
        base = (cfg or {}).get("_base_dir") or program_dir()
        cd = (cfg or {}).get("cert_dir") or "certs"
        cert_dir = cd if os.path.isabs(cd) else os.path.join(base, cd)
    if namespace_uri is None:
        namespace_uri = (cfg or {}).get("namespace_uri") or "urn:chromextract:opcua"
    result = asyncio.run(transmission_check_async(
        endpoint, namespace_uri=namespace_uri, security=security, cert_dir=cert_dir,
        user=user, password=password, duration_sec=duration_sec, do_send_test=do_send_test))
    _print_transmission_result(result)
    return 0 if result.get("ok") else 1


# ---------------------------------------------------------------------------
# 智能诊断（纯规则，离线可用）：配置—服务—数据—传输—日志五层证据 + 处理步骤
# ---------------------------------------------------------------------------

_LOG_KEYWORDS = (
    ("扫描出错", ("扫描出错",)),
    ("更新节点失败", ("更新节点失败",)),
    ("登录失败", ("登录失败",)),
    ("证书拒绝", ("已拒绝不在白名单", "BadCertificateUntrusted", "已拒绝匿名连接")),
    ("服务无法启动", ("服务无法启动", "服务没能启动")),
)


def _scan_log_tail(log_path, max_lines=200):
    """读日志尾部并统计错误关键词（只计数 + 摘录首条，不把整段日志塞进报告）。"""
    counts = {label: 0 for label, _ in _LOG_KEYWORDS}
    result = dict(exists=False, path=str(log_path or ""), lines_read=0,
                  counts=counts, total_hits=0, first_excerpt="")
    if not log_path or not os.path.isfile(log_path):
        return result
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return result
    tail = lines[-max(1, int(max_lines or 200)):]
    result["exists"] = True
    result["lines_read"] = len(tail)
    for line in tail:
        hit = False
        for label, pats in _LOG_KEYWORDS:
            if any(p in line for p in pats):
                counts[label] += 1
                hit = True
        if hit and not result["first_excerpt"]:
            result["first_excerpt"] = chrom_core.clean_text(line.strip(), 200)
    result["total_hits"] = sum(counts.values())
    return result


def _diagnose_channels(channels, channel_count=None, good_count=None):
    """通道健康判定（纯函数）：输入逐通道证据列表，输出一条 finding。

    channels 为 None 表示 OPC 未连接、拿不到证据；为空列表表示无真实通道。
    每条通道：{station, device, good, last_update, num_points, trace_len, min_raw, max_raw}
    """
    if channels is None:
        return dict(name="通道健康", level="fail",
                    evidence="无法读取通道（服务未连接，无逐通道证据）",
                    advice="先恢复服务连接（见服务存活一项），再重新运行 --diagnose 看通道明细")
    n = len(channels)
    if channel_count is None:
        channel_count = n
    good_n = sum(1 for c in channels if c.get("good"))
    if good_count is None:
        good_count = good_n
    if n == 0:
        return dict(name="通道健康", level="fail",
                    evidence="ChannelCount=%s，无真实检测器通道" % channel_count,
                    advice="先解决监控目录/数据源（见监控目录一项）：确认 watch_dirs 里有 *_V3.db/.mbd "
                           "且 DataBrowser 在产生数据，等一轮扫描后重新运行 --diagnose")
    bad = [c for c in channels if not c.get("good")]
    mismatch = [c for c in channels
                if c.get("trace_len") is not None and c.get("num_points") is not None
                and int(c["trace_len"]) != int(c["num_points"])]
    sanity = [c for c in channels
              if c.get("min_raw") is not None and c.get("max_raw") is not None
              and float(c["min_raw"]) > float(c["max_raw"])]
    names = lambda lst: "、".join("%s/%s" % (c.get("station", "?"), c.get("device", "?"))
                                  for c in lst[:5]) + ("等" if len(lst) > 5 else "")
    ev = "共 %d 个通道，可信 %d 个" % (n, good_n)
    if bad:
        ev += "；不可信（Good=False）：%s" % names(bad)
    if mismatch:
        ev += "；数组长度不一致：%s" % names(mismatch)
    if sanity:
        ev += "；MinRaw>MaxRaw 异常：%s" % names(sanity)
    if mismatch or sanity:
        return dict(name="通道健康", level="fail", evidence=ev,
                    advice="解析异常：保留出问题通道对应的源文件（不要覆盖），用 --list/--extract "
                           "对该文件复现解析，确认文件是否损坏或被写一半；复现材料留给技术支持排查")
    if bad and good_n == 0:
        return dict(name="通道健康", level="fail", evidence=ev,
                    advice="数据源丢失：通道旧值已标 Good=False 不可信。请查监控目录里源文件是否还在、"
                           "DataBrowser 是否停写、文件是否被移走；恢复数据源后等一轮扫描（poll_seconds）"
                           "看 Good 是否变回 True，不要直接删节点")
    if bad:
        return dict(name="通道健康", level="warn", evidence=ev,
                    advice="部分通道数据源丢失：逐个查不可信通道对应的源文件是否还在、检测器是否停机；"
                           "恢复后等一轮扫描复核，其余正常通道可继续使用")
    return dict(name="通道健康", level="ok", evidence=ev, advice="无需处理")


async def _collect_opc_evidence_async(endpoint, namespace_uri="urn:chromextract:opcua",
                                      security="none", cert_dir="certs", user=None,
                                      password=None, heartbeat_wait_sec=2.0):
    """诊断用的一次性 OPC 只读采集：心跳两次 + Diagnostics + 逐通道明细（只连一次）。"""
    from asyncua import Client, ua
    out = dict(connected=False, error="", server_version="", heartbeat1=None,
               heartbeat2=None, last_scan_time="", diag={}, channels=[], test_present=False)
    client = Client(url=str(endpoint or ""), timeout=10)
    try:
        if user is not None:
            client.set_user(user)
            client.set_password(password or "")
        sec = str(security or "none").lower()
        if sec != "none":
            from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
            client.application_uri = APP_URI
            await client.set_security(
                SecurityPolicyBasic256Sha256,
                certificate=os.path.join(cert_dir, "server_cert.der"),
                private_key=os.path.join(cert_dir, "server_key.pem"),
                mode=(ua.MessageSecurityMode.SignAndEncrypt if sec == "sign_encrypt"
                      else ua.MessageSecurityMode.Sign))
        await client.connect()
        out["connected"] = True
        ns = await client.get_namespace_index(namespace_uri)
        root = await client.nodes.objects.get_child(["%d:Chromatograph" % ns])
        try:
            out["server_version"] = await (await root.get_child(
                ["%d:ServerVersion" % ns])).read_value()
        except Exception:  # noqa
            pass
        hb_node = await root.get_child(["%d:Heartbeat" % ns])
        scan_node = await root.get_child(["%d:LastScanTime" % ns])
        out["heartbeat1"] = await hb_node.read_value()
        out["last_scan_time"] = await scan_node.read_value()
        diag = {}
        try:
            dnode = await root.get_child(["%d:Diagnostics" % ns])
            for key, bname in (("channel_count", "ChannelCount"),
                               ("good_count", "GoodChannelCount"),
                               ("total_points", "TotalPoints"),
                               ("last_change", "LastDataChangeTime"),
                               ("data_age", "DataAgeSeconds"),
                               ("status", "TransmissionStatus"),
                               ("test_counter", "TestCounter")):
                try:
                    diag[key] = await (await dnode.get_child(["%d:%s" % (ns, bname)])).read_value()
                except Exception:  # noqa
                    diag[key] = None
        except Exception:  # noqa
            diag = {}
        out["diag"] = diag
        channels = []
        for st in await root.get_children():
            sname = (await st.read_browse_name()).Name
            if not sname.startswith("Station_"):
                continue
            if sname == "Station_TEST":
                out["test_present"] = True
                continue
            for dev in await st.get_children():
                dname = (await dev.read_browse_name()).Name
                if not dname.startswith("App"):
                    continue
                if len(channels) >= 20:
                    break
                rec = dict(station=sname, device=dname, good=None, last_update="",
                           num_points=None, trace_len=None, min_raw=None, max_raw=None)
                try:
                    rec["good"] = bool(await (await dev.get_child(
                        ["%d:Status" % ns, "%d:Good" % ns])).read_value())
                    rec["last_update"] = await (await dev.get_child(
                        ["%d:Status" % ns, "%d:LastUpdate" % ns])).read_value()
                    rec["num_points"] = await (await dev.get_child(
                        ["%d:LatestCycle" % ns, "%d:NumPoints" % ns])).read_value()
                    rec["min_raw"] = await (await dev.get_child(
                        ["%d:LatestCycle" % ns, "%d:MinRaw" % ns])).read_value()
                    rec["max_raw"] = await (await dev.get_child(
                        ["%d:LatestCycle" % ns, "%d:MaxRaw" % ns])).read_value()
                    arr = await (await dev.get_child(
                        ["%d:Trace" % ns, "%d:ValueArray" % ns])).read_value()
                    rec["trace_len"] = len(arr) if arr is not None else 0
                except Exception:  # noqa
                    pass
                channels.append(rec)
            if len(channels) >= 20:
                break
        out["channels"] = channels
        wait = max(0.0, float(heartbeat_wait_sec or 0))
        if wait:
            await asyncio.sleep(wait)
        try:
            out["heartbeat2"] = await hb_node.read_value()
            out["last_scan_time"] = await scan_node.read_value()
        except Exception:  # noqa
            out["heartbeat2"] = out["heartbeat1"]
        return out
    except Exception as e:  # noqa
        out["error"] = "%s: %s" % (type(e).__name__, e)
        return out
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa
            pass


def _collect_opc_evidence(endpoint, namespace_uri="urn:chromextract:opcua",
                          security="none", cert_dir="certs", user=None,
                          password=None, heartbeat_wait_sec=2.0):
    return asyncio.run(_collect_opc_evidence_async(
        endpoint, namespace_uri=namespace_uri, security=security, cert_dir=cert_dir,
        user=user, password=password, heartbeat_wait_sec=heartbeat_wait_sec))


def diagnose(cfg, transmission_result=None, log_tail_lines=200,
             heartbeat_wait_sec=2.0, _opc=None):
    """智能诊断（纯规则）：收齐配置—服务—数据—传输—日志五层证据并给出处理步骤。

    cfg：配置 dict（可为未规整的原始配置，内部会复制一份走 normalize_config 复核，
    不改调用方的 cfg）。transmission_result：已跑的 transmission_check_async 结果，
    传输健康一项直接复用它，不在诊断里重跑订阅。_opc 仅供测试注入 OPC 证据。
    返回 {overall: 正常/注意/异常, findings: [{name, level, evidence, advice}]（按严重度排序）,
          actions: [去重后的处理清单], endpoint, checked_at, log_scan, opc_connected}。
    """
    cfg = dict(cfg or {})
    base = cfg.get("_base_dir") or program_dir()
    findings = []

    def _f(name, level, evidence, advice):
        findings.append(dict(name=name, level=level, evidence=str(evidence), advice=str(advice)))

    # 1) 配置合法性（复用 normalize_config + security_report，不重复造轮子）
    norm = dict(cfg)
    norm_ok = True
    try:
        normalize_config(norm, base)
    except Exception as e:  # noqa
        norm_ok = False
        _f("配置合法性", "fail", "配置校验失败：%s" % e,
           "按报错改 config.json 对应项（endpoint 必须 opc.tcp:// 开头、security 只能 none/sign/"
           "sign_encrypt、users 密码必须是哈希不要手写明文，用 --add-user 生成）；改完运行 "
           "--check-security 复核，再重启服务")
        norm = cfg
    if norm_ok:
        report = security_report(norm)
        risks = [m for lv, m in report if lv == "风险"]
        notes = [m for lv, m in report if lv == "注意"]
        if risks:
            _f("配置合法性", "warn",
               "配置合法，但安全体检有 %d 项风险：%s" % (len(risks), "；".join(risks)),
               "逐项处理上面风险（例如有密码却未加密时同时启用 sign_encrypt、关掉 "
               "allow_insecure_fallback）；处理后运行 --check-security 复核")
        else:
            _f("配置合法性", "ok",
               "配置合法（endpoint=%s，security=%s，安全体检无风险，注意事项 %d 项）" % (
                   norm.get("endpoint", ""), norm.get("security", "none"), len(notes)),
               "无需处理")

    # 2) 监控目录（复用 watch_dir_warnings + find_inputs 计数）
    warnings = watch_dir_warnings(norm)
    total_files = 0
    for d in (norm.get("watch_dirs") or []):
        try:
            total_files += len(chrom_core.find_inputs([d]))
        except Exception:  # noqa
            pass
    if warnings:
        _f("监控目录", "fail",
           "；".join(warnings) + "（共找到 %d 个数据文件）" % total_files,
           "装好了却没数据多半是目录不对：1) 打开 config.json 核对 watch_dirs 是否为存放 "
           "*_V3.db/.mbd 的文件夹（反斜杠写 \\\\ 或改用 /）；2) 确认文件夹里真有 *_V3.db/.mbd "
           "且 DataBrowser 在写；3) 可运行 --set-watch-dir <文件夹> 写入；4) 等一轮扫描"
           "（poll_seconds=%s 秒）后重新运行 --diagnose 复核" % norm.get("poll_seconds", 30))
    else:
        _f("监控目录", "ok",
           "%d 个监控目录，共找到 %d 个 *_V3.db/.mbd 文件" % (
               len(norm.get("watch_dirs") or []), total_files),
           "无需处理")

    # OPC 证据（只连一次；传输检测已确认连不上时不再重复等超时）
    endpoint = _local_endpoint(norm.get("endpoint") or DEFAULT_CONFIG["endpoint"])
    tx_connect_failed = False
    if transmission_result:
        for s in transmission_result.get("steps", []):
            if s.get("name") == "连接建会话" and not s.get("ok"):
                tx_connect_failed = True
    if _opc is not None:
        opc = _opc
    elif tx_connect_failed:
        opc = dict(connected=False, error="传输检测已确认无法连接", channels=[], diag={})
    else:
        cd = norm.get("cert_dir") or "certs"
        cert_dir = cd if os.path.isabs(cd) else os.path.join(base, cd)
        try:
            opc = _collect_opc_evidence(
                endpoint, namespace_uri=norm.get("namespace_uri") or "urn:chromextract:opcua",
                security=norm.get("security", "none"), cert_dir=cert_dir,
                heartbeat_wait_sec=heartbeat_wait_sec)
        except Exception as e:  # noqa
            opc = dict(connected=False, error="%s: %s" % (type(e).__name__, e),
                       channels=[], diag={})
    connected = bool(opc.get("connected"))
    diag = opc.get("diag") or {}
    poll = int(norm.get("poll_seconds", 30) or 30)
    sec_mode = str(norm.get("security", "none")).lower()

    # 3) 服务存活（心跳推进 / LastScanTime 年龄，如实标注判断依据）
    alive = False
    if not connected:
        if sec_mode == "none":
            sec_advice = "当前 security=none，客户端不要用加密连接；若客户端报安全策略错误，把客户端改成不加密"
        else:
            sec_advice = ("当前 security=%s，客户端必须用 Basic256Sha256 加密 + 信任服务端证书"
                          "（账号登录时还要填对用户名密码），不要用不加密客户端硬连" % sec_mode)
        _f("服务存活", "fail",
           "无法连接 %s：%s" % (endpoint, opc.get("error") or "未知错误"),
           "服务可能没启动或卡死：1) 确认服务进程在运行（GUI 状态栏/任务管理器看 ChromExtractOPC）；"
           "2) 查端口占用（endpoint 端口是否被旧实例占用，必要时改 config.json 的 endpoint 端口后重启）；"
           "3) 看 logs/crash.log 与服务日志尾部；4) 查防火墙是否放行该端口；5) %s" % sec_advice)
    else:
        hb1, hb2 = opc.get("heartbeat1"), opc.get("heartbeat2")
        scan_txt = str(opc.get("last_scan_time") or "")
        age = None
        try:
            age = (datetime.now() - datetime.strptime(scan_txt, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except (ValueError, TypeError):
            age = None
        threshold = poll * 2 + 10
        advanced = (hb1 is not None and hb2 is not None and hb2 > hb1)
        if advanced:
            alive = True
            _f("服务存活", "ok",
               "判断依据：Heartbeat %s -> %s（等待 %.0f 秒内推进），LastScanTime=%s" % (
                   hb1, hb2, float(heartbeat_wait_sec or 0), scan_txt or "（空）"),
               "无需处理")
        elif age is not None and age <= threshold:
            alive = True
            _f("服务存活", "ok",
               "判断依据：LastScanTime=%s，年龄 %.0f 秒（阈值 %d 秒）；短窗口内心跳未变属预期"
               "（扫描间隔 %d 秒），服务在按轮次扫描" % (scan_txt, age, threshold, poll),
               "无需处理")
        elif age is not None:
            _f("服务存活", "fail",
               "判断依据：Heartbeat %s -> %s 未推进，LastScanTime=%s 年龄 %.0f 秒（阈值 %d 秒），"
               "服务疑似卡死" % (hb1, hb2, scan_txt, age, threshold),
               "服务疑似卡死：1) 看服务日志尾部与 logs/crash.log 有无反复报错；2) 重启服务后运行 "
               "--diagnose 复核心跳；3) 若反复卡死，保留日志与源文件清单排查（先查监控目录是否指向"
               "网络盘/无权限目录）")
        else:
            _f("服务存活", "warn",
               "已连接但 LastScanTime 为空或无法解析（%r），心跳 %s -> %s，存活证据不足" % (
                   scan_txt, hb1, hb2),
               "等一轮扫描（poll_seconds=%d 秒）后重新运行 --diagnose；若仍为空，查服务日志看扫描线程"
               "是否报错" % poll)

    # 4) 通道健康
    findings.append(_diagnose_channels(
        opc.get("channels") if connected else None,
        channel_count=diag.get("channel_count"), good_count=diag.get("good_count")))

    # 5) 数据新鲜度（区分无新周期与卡死）
    stale = norm.get("transmission_stale_sec", 7200)
    if not connected:
        _f("数据新鲜度", "fail", "服务未连接，无法判断数据新鲜度",
           "先恢复服务连接（见服务存活一项），再重新运行 --diagnose")
    else:
        ch_n = diag.get("channel_count")
        age_v = diag.get("data_age")
        last_ch = str(diag.get("last_change") or "")
        status_v = str(diag.get("status") or "")
        if not ch_n:
            _f("数据新鲜度", "fail",
               "无通道，从未收到数据（LastDataChangeTime=%s，TransmissionStatus=%s）" % (
                   last_ch or "（空）", status_v or "（空）"),
               "先解决监控目录/数据源（见监控目录一项），有数据后再看新鲜度")
        elif not last_ch:
            _f("数据新鲜度", "fail",
               "有通道但 LastDataChangeTime 为空，无法确认数据更新时间",
               "等一轮扫描后重新运行 --diagnose；若仍为空，查服务日志看更新节点是否失败")
        elif age_v is not None and float(age_v) <= float(stale):
            _f("数据新鲜度", "ok",
               "DataAgeSeconds=%.0f <= 阈值 %s，TransmissionStatus=%s，最近更新 %s" % (
                   float(age_v), stale, status_v, last_ch),
               "无需处理")
        elif alive:
            _f("数据新鲜度", "warn",
               "DataAgeSeconds=%s > 阈值 %s，但心跳正常，TransmissionStatus=%s" % (
                   age_v, stale, status_v),
               "数据较旧但服务活着，多半是色谱无新周期（不是故障）：确认仪器是否在跑周期、"
               "DataBrowser 是否产生新周期、源文件修改时间是否更新；确认无新周期时无需处理，"
               "有新周期后等一轮扫描复核；若仪器在跑却无新数据，再查监控目录与源文件")
        else:
            _f("数据新鲜度", "fail",
               "DataAgeSeconds=%s > 阈值 %s 且心跳异常，TransmissionStatus=%s" % (
                   age_v, stale, status_v),
               "数据旧且服务疑似卡死：先按服务存活一项恢复服务，再等一轮扫描复核新鲜度")

    # 6) 传输健康（复用传入的传输检测结果，不重跑订阅）
    if transmission_result is None:
        _f("传输健康", "warn", "未提供传输检测结果（诊断未重跑订阅）",
           "运行 --check-transmission（或 --diagnose 会自动先跑一次）补齐连接/订阅/Echo 证据")
    else:
        steps = transmission_result.get("steps", [])
        by_name = {s.get("name"): s for s in steps}
        failed = [s for s in steps if not s.get("ok")]
        conn = by_name.get("连接建会话", {})
        sub = by_name.get("订阅 TestSignal+Heartbeat", {})
        read_step = by_name.get("读真实通道曲线", {})
        echo = by_name.get("EchoTest 往返", {})
        # 订阅一步的耗时包含等待窗口本身，不算延迟；只看连接/读/回显等单步延迟
        max_lat = max([s["latency_ms"] for s in steps
                       if s.get("latency_ms") is not None
                       and s.get("name") != "订阅 TestSignal+Heartbeat"] or [0])
        ev = "共 %d 步，通过 %d，失败 %d，最大单步延迟 %.0f ms" % (
            len(steps), len(steps) - len(failed), len(failed), max_lat)
        if failed:
            ev += "；失败步骤：" + "、".join(s.get("name", "?") for s in failed)
        if not conn.get("ok", True):
            if sec_mode == "none":
                advice = ("连接失败：1) 确认服务已启动且端口未被占用；2) 查防火墙放行；"
                          "3) 当前 security=none，客户端必须用不加密连接，不要用加密客户端")
            else:
                advice = ("连接失败：1) 确认服务已启动且端口未被占用；2) 查防火墙放行；"
                          "3) 当前 security=%s，客户端必须用 Basic256Sha256 加密并信任服务端证书"
                          "（账号模式还要填对用户名密码），不加密客户端会被拒绝" % sec_mode)
            _f("传输健康", "fail", ev + "；连接详情：%s" % conn.get("detail", ""), advice)
        elif failed:
            _f("传输健康", "fail", ev,
               "按失败步骤逐个处理：读数失败多为旧版服务端缺节点（升级到 v2.4）；Echo 失败同理；"
               "曲线不一致按通道健康一项保留源文件排查；处理后运行 --check-transmission 复核")
        elif "未收到通知" in str(sub.get("detail", "")) and read_step.get("ok"):
            _f("传输健康", "warn",
               ev + "；订阅窗口内无通知但读取正常：%s" % sub.get("detail", ""),
               "订阅无通知但读取正常，多为发布间隔/网络抖动：1) 看 poll_seconds=%d，心跳只随扫描"
               "轮次更新，加大 --check-seconds（如 35 秒）再测；2) 查网络抖动与防火墙是否拦截长连接；"
               "3) Echo 正常说明双向通道可用，不必重启服务" % poll)
        elif max_lat > 2000:
            _f("传输健康", "warn", ev + "；单步延迟偏高",
               "延迟偏高：查网络负载与服务端机器负载，确认无大文件拷贝占满带宽；"
               "复测仍高时保留 --check-transmission 输出与服务日志排查")
        else:
            _f("传输健康", "ok", ev + "；订阅：%s" % sub.get("detail", ""), "无需处理")

    # 7) 近期日志错误（只计数 + 首条摘录）
    log_file = norm.get("log_file") or ""
    if not log_file:
        scan = dict(exists=False, path="", lines_read=0, counts={}, total_hits=0, first_excerpt="")
        _f("近期日志", "warn", "无日志可查：config 的 log_file 为空",
           "在 config.json 设置 log_file（如 logs/server.log）后重启服务，排障时保留最近日志不要清空")
    else:
        log_path = log_file if os.path.isabs(log_file) else os.path.join(base, log_file)
        scan = _scan_log_tail(log_path, log_tail_lines)
        if not scan["exists"]:
            _f("近期日志", "warn", "无日志可查：%s 不存在" % log_path,
               "确认服务已启动过且 log_file 路径正确；服务启动后日志写到该路径，"
               "排障时保留最近日志不要清空")
        elif scan["total_hits"] == 0:
            _f("近期日志", "ok",
               "尾部 %d 行未发现扫描/节点/登录/证书错误（%s）" % (scan["lines_read"], log_path),
               "无需处理")
        else:
            cnt_txt = "，".join("%s×%d" % (k, v) for k, v in scan["counts"].items() if v)
            _f("近期日志", "warn",
               "尾部 %d 行发现错误：%s；首条：%s" % (scan["lines_read"], cnt_txt, scan["first_excerpt"]),
               "按计数对症：扫描出错查监控目录与源文件可读性；更新节点失败保留源文件排查解析；"
               "登录失败查账号与是否有反复试错；证书拒绝把可信客户端证书放进 trusted 目录或改对"
               "客户端安全设置；处理后看日志是否还新增同类错误")

    order = {"fail": 0, "warn": 1, "ok": 2}
    findings.sort(key=lambda x: order.get(x["level"], 1))
    if any(f["level"] == "fail" for f in findings):
        overall = "异常"
    elif any(f["level"] == "warn" for f in findings):
        overall = "注意"
    else:
        overall = "正常"
    actions = []
    for f in findings:
        if f["level"] in ("fail", "warn") and f["advice"] and f["advice"] != "无需处理" \
                and f["advice"] not in actions:
            actions.append(f["advice"])
    return dict(overall=overall, findings=findings, actions=actions, endpoint=endpoint,
                checked_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                log_scan=scan, opc_connected=connected)


def _print_diagnosis_result(result):
    icons = {"ok": "[ 通过 ]", "warn": "[ 注意 ]", "fail": "[ 失败 ]"}
    print("智能诊断：%s（%s）" % (result.get("endpoint", ""), result.get("checked_at", "")))
    for f in result.get("findings", []):
        print("  %s %s：%s" % (icons.get(f["level"], "[ ? ]"), f["name"], f["evidence"]))
        if f["level"] != "ok":
            print("         处理：%s" % f["advice"])
    print("结论：%s" % result.get("overall", ""))
    if result.get("actions"):
        print("处理清单（按可能性排序）：")
        for i, a in enumerate(result["actions"], 1):
            print("  %d. %s" % (i, a))


def run_diagnose(cfg, duration_sec=6, do_send_test=False):
    """CLI/GUI 共用：先跑一次传输检测，再喂给 diagnose 打印报告。异常返回 1，否则 0。"""
    _force_utf8_stdout()
    ep = _local_endpoint((cfg or {}).get("endpoint") or DEFAULT_CONFIG["endpoint"])
    base = (cfg or {}).get("_base_dir") or program_dir()
    cd = (cfg or {}).get("cert_dir") or "certs"
    cert_dir = cd if os.path.isabs(cd) else os.path.join(base, cd)
    tx = asyncio.run(transmission_check_async(
        ep, namespace_uri=(cfg or {}).get("namespace_uri") or "urn:chromextract:opcua",
        security=(cfg or {}).get("security", "none"), cert_dir=cert_dir,
        duration_sec=duration_sec, do_send_test=do_send_test))
    _print_transmission_result(tx)
    result = diagnose(cfg, transmission_result=tx)
    _print_diagnosis_result(result)
    return 1 if result.get("overall") == "异常" else 0


# ---------------------------------------------------------------------------
# GUI 监控窗口（可选）
# ---------------------------------------------------------------------------

class SecurityWindow:
    """窗口里的"安全设置"面板：体检、一键加固、添加/删除账号。
    dialogs 提供 ask(标题, 提示, secret=False) / info(文字) / error(文字) / confirm(文字)，
    真实界面用 tkinter 对话框，自动测试里换成假的。"""

    def __init__(self, parent, cfg, dialogs, tk=None):
        import tkinter as _tk
        from tkinter import scrolledtext
        tk = tk or _tk
        self.cfg, self.path, self.dlg = cfg, cfg["_cfg_path"], dialogs
        self.win = tk.Toplevel(parent)
        self.win.title("安全设置")
        self.win.geometry("700x520")
        tk.Label(self.win, text="安全体检", anchor="w", font=("Microsoft YaHei", 11, "bold")).pack(
            fill="x", padx=10, pady=(10, 0))
        self.report = scrolledtext.ScrolledText(self.win, height=11, state="disabled", wrap="word")
        self.report.pack(fill="x", padx=10, pady=4)
        tk.Label(self.win, text="登录账号", anchor="w", font=("Microsoft YaHei", 11, "bold")).pack(
            fill="x", padx=10, pady=(8, 0))
        self.users_lb = tk.Listbox(self.win, height=5, exportselection=False)
        self.users_lb.pack(fill="x", padx=10, pady=4)
        bar = tk.Frame(self.win)
        bar.pack(fill="x", padx=10, pady=6)
        for text, cmd in (("一键加固…", self.harden), ("添加 / 重设账号…", self.add_user),
                          ("删除选中账号", self.remove_user), ("刷新", self.refresh)):
            tk.Button(bar, text=text, command=cmd).pack(side="left", padx=4)
        self.note = tk.Label(self.win, text="", anchor="w", justify="left", fg="#555", wraplength=660)
        self.note.pack(fill="x", padx=10, pady=4)
        self.refresh()

    # ---- 内部 ----
    def _set_report(self, text):
        self.report.configure(state="normal")
        self.report.delete("1.0", "end")
        self.report.insert("end", text)
        self.report.configure(state="disabled")

    def refresh(self):
        try:
            cfg = load_normalized(self.path)
            lines = ["[%s] %s" % (lv, msg) for lv, msg in security_report(cfg)]
        except (ConfigError, config_tools.ConfigToolError, ValueError, OSError) as e:
            lines = ["配置文件有误：%s" % e]
        self._set_report("\n\n".join(lines))
        self.users_lb.delete(0, "end")
        try:
            for u in config_tools.list_users(self.path):
                self.users_lb.insert("end", u)
        except config_tools.ConfigToolError:
            pass

    def _ask_password(self, username):
        pw = self.dlg.ask("设置密码", "为 %s 设置密码（至少 8 位）：" % username, secret=True)
        if pw is None:
            return None
        err = config_tools.check_password_strength(pw, username)
        if err:
            self.dlg.error(err)
            return None
        if self.dlg.ask("确认密码", "再输入一次：", secret=True) != pw:
            self.dlg.error("两次输入不一致。")
            return None
        return pw

    def _saved(self, restart=False):
        self.note.configure(text="已保存。账号和匿名开关的改动会在下一轮扫描（最长 %s 秒）内自动生效。%s" % (
            self.cfg.get("poll_seconds", 30), "\n加密设置需要重启服务才会生效。" if restart else ""))
        self.refresh()

    # ---- 按钮 ----
    def add_user(self):
        name = (self.dlg.ask("账号", "用户名：") or "").strip()
        if not name:
            return
        err = config_tools.check_username(name)
        if err:
            return self.dlg.error(err)
        pw = self._ask_password(name)
        if pw is None:
            return
        try:
            config_tools.add_user(self.path, name, pw)
        except config_tools.ConfigToolError as e:
            return self.dlg.error(str(e))
        self._saved()

    def remove_user(self):
        sel = self.users_lb.curselection()
        if not sel:
            return self.dlg.info("请先在列表里选中要删除的账号。")
        name = self.users_lb.get(sel[0])
        if not self.dlg.confirm("确定删除账号 %s 吗？" % name):
            return
        try:
            config_tools.remove_user(self.path, name)
        except config_tools.ConfigToolError as e:
            return self.dlg.error(str(e))
        self._saved()

    def harden(self):
        if not self.dlg.confirm("一键加固会：\n  • 开启通信加密\n  • 关闭匿名访问（必须登录）\n  • 创建/重设一个登录账号\n\n"
                                "现有的 DCS/OPC 客户端需要同步改成加密+账号登录，否则会连不上。继续吗？"):
            return
        name = (self.dlg.ask("账号", "登录用户名（例如 dcs）：") or "").strip()
        if not name:
            return
        err = config_tools.check_username(name)
        if err:
            return self.dlg.error(err)
        pw = self._ask_password(name)
        if pw is None:
            return
        try:
            changes = config_tools.harden(self.path, name, pw)
        except config_tools.ConfigToolError as e:
            return self.dlg.error(str(e))
        self.dlg.info("已完成：\n" + "\n".join("  ✓ " + c for c in changes) + "\n\n请重启服务让加密生效。")
        self._saved(restart=True)


class _TkDialogs:
    def __init__(self, parent):
        from tkinter import messagebox, simpledialog
        self.p, self.mb, self.sd = parent, messagebox, simpledialog

    def ask(self, title, prompt, secret=False):
        return self.sd.askstring(title, prompt, parent=self.p, show="*" if secret else None)

    def info(self, text):
        self.mb.showinfo("提示", text, parent=self.p)

    def error(self, text):
        self.mb.showerror("无法完成", text, parent=self.p)

    def confirm(self, text):
        return self.mb.askyesno("请确认", text, parent=self.p)


def security_summary(cfg):
    """给主窗口状态栏用的一句话摘要。"""
    levels = [lv for lv, _ in security_report(cfg)]
    risk, note = levels.count("风险"), levels.count("注意")
    return "安全体检：%s" % ("有 %d 项风险" % risk if risk else
                          ("%d 项注意事项，无风险" % note if note else "全部正常"))


def ssl_error_types():
    import ssl
    return (ssl.SSLError, ValueError)


def start_web_settings(cfg, open_browser=False):
    """开启浏览器设置页面；失败（如端口被占用）只记日志，不影响 OPC UA 服务。"""
    if not cfg.get("web_ui", True):
        return None
    import web_settings
    try:
        return web_settings.SettingsServer(cfg["_cfg_path"], cfg["_base_dir"], sys.modules[__name__],
                                           host=cfg.get("web_host") or "127.0.0.1",
                                           port=cfg.get("web_port", 8840), log=get_log(),
                                           https=cfg.get("web_https", "auto"),
                                           cert_file=cfg.get("web_cert_file", ""),
                                           key_file=cfg.get("web_key_file", ""),
                                           cert_dir=os.path.join(cfg["_base_dir"], cfg.get("cert_dir") or "certs"),
                                           ).start(open_browser=open_browser)
    except ImportError as e:
        get_log().error("设置页面需要 HTTPS，但无法加载 cryptography 库：%s", e)
        return None
    except (ssl_error_types()) as e:
        get_log().error("设置页面 HTTPS 证书无法使用（检查 web_cert_file / web_key_file）：%s", e)
        return None
    except OSError as e:
        get_log().error("设置页面无法启动（端口 %s 可能被占用，可在 config.json 改 web_port）：%s",
                        cfg.get("web_port"), e)
        return None


def run_settings_only(cfg):
    """打开桌面主窗口但不自动启动 OPC UA 服务（窗口里可随时点“启动服务”），关闭窗口结束。"""
    return run_gui(cfg, start_service=False)


def _resource(rel):
    """打包后资源在 PyInstaller 解压目录（sys._MEIPASS），源码运行时在程序目录。"""
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), rel)


def _set_window_icon(win):
    try:
        ico = _resource(os.path.join("assets", "app.ico"))
        if os.path.exists(ico):
            win.iconbitmap(default=ico)       # default=：之后弹出的子窗口/对话框也用同一个图标
    except Exception:  # noqa  图标失败不影响使用（例如非 Windows 系统）
        pass


def run_gui(cfg, start_service=True, minimized=False):
    """打开桌面主窗口。start_service=True 时窗口打开后自动启动 OPC UA 服务（默认）。
    服务运行在本进程的工作线程里，窗口关闭即停止，见 desktop_ui.py。"""
    _hide_own_console()
    import desktop_ui
    return desktop_ui.run_desktop(cfg, start_service=start_service, minimized=minimized)



def _own_console():
    """Windows 上，这个控制台窗口是不是只属于本程序（即双击 EXE 启动，而不是在 cmd 里运行）。
    单文件 EXE 会有"启动器+本体"两个进程共用一个窗口，所以允许 {自己, 父进程}。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        arr = (ctypes.c_uint * 64)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(arr, 64)
        if n == 0 or n > 64:
            return False
        return set(arr[:n]) <= {os.getpid(), os.getppid()}
    except Exception:  # noqa
        return False


def _hide_own_console():
    """图形窗口模式下，把多余的黑色控制台窗口藏起来（仅双击启动时）。"""
    if not getattr(sys, "frozen", False) or not _own_console():
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:  # noqa
        pass


def _pause_before_exit(rc):
    """双击命令行版 EXE 出错退出时停住窗口，让人看清原因。
    被脚本/安装程序/管道调用时 stdin 不是终端，不会停住。"""
    if rc in (0, None) or not getattr(sys, "frozen", False):
        return
    try:
        if _own_console() and sys.stdin is not None and sys.stdin.isatty():
            input("\n程序已退出（代码 %s）。请把上面的文字截图或复制给技术支持。按回车键关闭窗口……" % rc)
    except Exception:  # noqa
        pass


def watch_dir_warnings(cfg):
    """启动时检查监控目录，返回给人看的提示列表。目录不对是\"装好了却没数据\"的头号原因。"""
    out = []
    dirs = cfg.get("watch_dirs") or []
    if not dirs:
        out.append("config.json 里的 watch_dirs 是空的：服务能启动，但没有任何数据可发布。"
                   "请填入存放 *_V3.db / .mbd 文件的文件夹。")
    for d in dirs:
        if not os.path.exists(d):
            out.append("监控目录不存在：%s。请在 config.json 的 watch_dirs 里改成实际路径"
                       "（路径里的反斜杠要写成 \\\\ 或改用 /）。" % d)
            continue
        try:
            n = len(chrom_core.find_inputs([d]))
        except Exception:  # noqa
            n = 0
        if n == 0:
            out.append("监控目录里没有找到 *_V3.db 或 .mbd 文件：%s" % d)
    return out


# ---------------------------------------------------------------------------
# 打包后自检：真的起一个服务，再用客户端连上读一遍
# ---------------------------------------------------------------------------

async def _selftest_async(cfg, port, timeout=45):
    from asyncua import Client
    stop = asyncio.Event()
    task = asyncio.ensure_future(run_server(cfg, stop))
    url = "opc.tcp://127.0.0.1:%d/ChromExtract" % port
    last = "尚未连上"
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if task.done():
                exc = task.exception() if not task.cancelled() else None
                return False, "服务没能启动：%s" % (exc or "已退出")
            try:
                async with Client(url, timeout=5) as c:
                    idx = await c.get_namespace_index(cfg["namespace_uri"])
                    chrom = await c.nodes.objects.get_child(["%d:Chromatograph" % idx])
                    channels = 0
                    for st in await chrom.get_children():
                        if (await st.read_browse_name()).Name.startswith("Station_"):
                            for dev in await st.get_children():
                                nm = (await dev.read_browse_name()).Name
                                if not nm.startswith("App"):
                                    continue
                                npts = await (await dev.get_child(
                                    ["%d:LatestCycle" % idx, "%d:NumPoints" % idx])).read_value()
                                arr = await (await dev.get_child(
                                    ["%d:Trace" % idx, "%d:ValueArray" % idx])).read_value()
                                if npts > 0 and len(arr) > 0:
                                    channels += 1
                    if channels:
                        return True, "读到 %d 个检测器通道的谱图数据" % channels
                    last = "已连上，但数据还没读出来"
            except Exception as e:  # noqa
                last = "%s: %s" % (type(e).__name__, e)
            await asyncio.sleep(1)
        return False, "超时（%d 秒）。最后一次状态：%s" % (timeout, last)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, 10)
        except Exception:  # noqa
            pass


def _selftest_web(tmp):
    """自检设置页面：用 HTTPS 启动在随机端口，读一次状态（确认证书生成、TLS、页面都正常）。"""
    import ssl
    import urllib.request
    import web_settings
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"poll_seconds": 30}, f)
    srv = web_settings.SettingsServer(cfg_path, tmp, sys.modules[__name__], port=0, https="on",
                                      cert_dir=os.path.join(tmp, "certs"))
    srv.start()
    try:
        ctx = ssl.create_default_context(cafile=os.path.join(tmp, "certs", "web_cert.pem"))
        with urllib.request.urlopen(srv.url() + "api/state", context=ctx, timeout=10) as r:
            st = json.loads(r.read().decode("utf-8"))
        if not st.get("https") or st["config"].get("poll_seconds") != 30:
            return False, "设置页面返回内容不对"
        return True, "设置页面（HTTPS）正常"
    finally:
        srv.stop()


def run_rtu485_cli(args):
    """--diagnose-485：自动试参数 + 数据配型，打印结论。只读，不写寄存器。"""
    _force_utf8_stdout()
    import nettest
    if args.function not in (3, 4):
        print("[ 失败 ] 485 配型诊断只允许读功能码 03/04（写功能请用 --modbus-poll）")
        return 2
    try:
        r = nettest.diagnose_rtu485(
            args.diagnose_485, unit=args.slave, func=args.function, addr=args.register,
            count=args.count, baud=args.baud, timeout=max(0.05, args.timeout_ms / 1000.0),
            scan_slaves=args.scan_slaves,
            progress=lambda s: print("    " + s))
    except nettest.TestError as e:
        print("[ 失败 ] " + str(e))
        return 1
    print(("[ 成功 ] " if r["ok"] else "[ 失败 ] ") + r["summary"])
    for line in r["details"]:
        for sub in str(line).split("\n"):
            print("    " + sub)
    return 0 if r["ok"] else 1


def _print_modbus_result(r, addr, dtype, order):
    import modbus_tools as mt
    print(("[ 成功 ] " if r["ok"] else "[ 失败 ] ") + r["summary"])
    for line in r["details"]:
        print("    " + line)
    if r["ok"] and r.get("values") is not None:
        for line in mt.format_read_values(r["values"], r["kind"], addr, dtype, order):
            print("    " + line)


def run_modbus_poll_cli(args):
    """--modbus-poll：Modbus 主站测试（读 01-04 / 写 05/06/15/16，单次或连续轮询）。"""
    _force_utf8_stdout()
    import modbus_tools as mt
    import nettest
    timeout = max(0.3, args.timeout_ms / 1000.0)
    try:
        if args.tcp:
            host, _sep, port = str(args.tcp).rpartition(":")
            if not host:
                host, port = str(args.tcp), "502"
            master = mt.TcpMaster(host, int(port or 502), unit=args.slave, timeout=timeout)
            target = "TCP %s:%s 站号 %d" % (host, port or 502, args.slave)
        elif args.rtu:
            master = mt.RtuMaster(args.rtu, baud=args.baud or 9600, parity=args.parity,
                                  stopbits=args.stopbits, unit=args.slave, timeout=timeout)
            target = "RTU %s %d,8,%s,%g 站号 %d" % (args.rtu, args.baud or 9600,
                                                  args.parity, args.stopbits, args.slave)
        else:
            print("[ 失败 ] 请指定目标：--tcp 主机:端口 或 --rtu 串口")
            return 2
        master.connect()
    except (nettest.TestError, ValueError) as e:
        print("[ 失败 ] " + str(e))
        return 1
    print("Modbus Poll：%s，功能码 %02d（%s），地址 %d" % (
        target, args.function, mt.FUNC_NAMES.get(args.function, "?"), args.address))
    try:
        if args.function in mt.WRITE_FUNCS:
            try:
                values = mt.parse_values(args.values, args.function, args.data_type, args.byte_order)
            except nettest.TestError as e:
                print("[ 失败 ] " + str(e))
                return 2
            r = master.write(args.function, args.address, values)
            _print_modbus_result(r, args.address, args.data_type, args.byte_order)
            return 0 if r["ok"] else 1
        if args.poll_seconds and args.poll_seconds > 0:
            print("连续轮询（间隔 %g 秒，Ctrl+C 停止）…" % args.poll_seconds)
            last = {"ok": False}

            def _show(r):
                last.update(r)
                _print_modbus_result(r, args.address, args.data_type, args.byte_order)
            try:
                mt.poll(master, args.function, args.address, args.count,
                        interval_sec=args.poll_seconds, callback=_show)
            except KeyboardInterrupt:
                print("已停止轮询。")
            return 0 if last["ok"] else 1
        r = master.read(args.function, args.address, args.count)
        _print_modbus_result(r, args.address, args.data_type, args.byte_order)
        return 0 if r["ok"] else 1
    finally:
        master.close()


def run_modbus_slave_cli(args):
    """--modbus-slave：Modbus 从站模拟器（手动启动才监听，Ctrl+C 停止）。"""
    _force_utf8_stdout()
    import modbus_tools as mt
    import nettest
    try:
        store = mt.DataStore(size=args.size)

        def _log(e):
            print("[%s] %s 站号 %s 功能码 %s  请求 %s  应答 %s%s" % (
                e["time"], e["transport"], e["unit"], e["func"],
                e["req_hex"], e["resp_hex"] or "（无）",
                ("  " + e["note"]) if e["note"] else ""), flush=True)
        if args.rtu:
            slave = mt.RtuSlave(store, args.rtu, baud=args.baud or 9600, parity=args.parity,
                                stopbits=args.stopbits, unit=args.slave, log=_log)
        else:
            slave = mt.TcpSlave(store, host=args.host, port=args.tcp_port,
                                unit=args.slave, log=_log)
        slave.start()
    except nettest.TestError as e:
        print("[ 失败 ] " + str(e))
        return 1
    print("Modbus Slave 从站模拟器已启动：监听 %s，每区 %d 个数据。Ctrl+C 停止。"
          % (slave.address, args.size), flush=True)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("正在停止…")
    finally:
        slave.stop()
    return 0


def run_nettest_cli(argv):
    """--nettest 类型 [位置参数...] [键=值...]"""
    import nettest
    kind, rest = argv[0], argv[1:]
    if kind not in nettest.TESTS:
        print("未知的测试类型：%s\n可用：%s" % (kind, " ".join(nettest.TESTS)))
        return 2
    names = nettest.TESTS[kind][1]
    params, pos = {}, 0
    for a in rest:
        if "=" in a:
            k, v = a.split("=", 1)
            params[k.strip()] = v
        elif pos < len(names):
            params[names[pos]] = a
            pos += 1
    r = nettest.run_test(kind, params)
    print(("[ 成功 ] " if r["ok"] else "[ 失败 ] ") + r["summary"])
    for line in r["details"]:
        print("    " + line)
    return 0 if r["ok"] else 1


def run_stability_cli(argv):
    """--stability 类型 [位置参数...] [键=值...] interval=秒 minutes=分钟；Ctrl+C 提前结束并输出统计。"""
    import nettest
    import stability
    kind, rest = argv[0], argv[1:]
    if kind not in stability.ALLOWED:
        print("不支持的类型：%s\n可用：%s" % (kind, " ".join(stability.ALLOWED)))
        return 2
    names = nettest.TESTS[kind][1]
    params, pos, interval, minutes = {}, 0, 1.0, 10.0
    for a in rest:
        if "=" in a:
            k, v = a.split("=", 1)
            if k == "interval":
                interval = v
            elif k == "minutes":
                minutes = v
            else:
                params[k.strip()] = v
        elif pos < len(names):
            params[names[pos]] = a
            pos += 1
    out_dir = os.path.join(program_dir(), "logs", "stability")
    try:
        run = stability.StabilityRun(kind, params, interval, minutes, out_dir=out_dir)
    except stability.StabilityError as e:
        print(e)
        return 2
    print("稳定性测试：%s %s，每 %s 秒一次，共 %s 分钟。按 Ctrl+C 提前结束。" % (
        stability.ALLOWED[kind], run.target(), interval, minutes))
    th = threading.Thread(target=run.run_blocking, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(10)
            st = run.stats()
            print("  %s  %d 次  失败 %d（%s%%）  平均 %s ms  P95 %s ms" % (
                st["elapsed_text"], st["n"], st["fail"], st["loss_pct"], st["avg"], st["p95"]))
    except KeyboardInterrupt:
        run.stop()
        th.join(15)
    st = run.stats()
    print("\n结论：%s" % st["verdict"])
    print("次数 %d，失败 %d（%s%%），最长连续失败 %d" % (st["n"], st["fail"], st["loss_pct"], st["max_streak"]))
    print("延迟 ms：最小 %s / 平均 %s / P95 %s / P99 %s / 最大 %s，抖动 %s" % (
        st["min"], st["avg"], st["p95"], st["p99"], st["max"], st["jitter"]))
    for e in st["errors"]:
        print("  错误 %d 次：%s" % (e["count"], e["text"]))
    for h in st["hints"]:
        print("  建议：" + h)
    if st["csv"]:
        print("明细：%s" % os.path.join(out_dir, st["csv"]))
    return 0 if st["fail"] == 0 else 1


def run_stability_multi_cli(argv):
    import stability
    path, interval, minutes = argv[0], 1.0, 10.0
    for a in argv[1:]:
        k, _, v = a.partition("=")
        if k == "interval":
            interval = v
        elif k == "minutes":
            minutes = v
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            targets = json.load(f)
    except (OSError, ValueError) as e:
        print("读不了目标文件 %s：%s" % (path, e))
        return 2
    out_dir = os.path.join(program_dir(), "logs", "stability")
    try:
        m = stability.MultiRun(targets, interval, minutes, out_dir=out_dir)
    except stability.StabilityError as e:
        print(e)
        return 2
    print("多目标稳定性测试：%d 个目标，每 %s 秒，共 %s 分钟。按 Ctrl+C 提前结束。" % (len(m.runs), interval, minutes))
    th = threading.Thread(target=m.run_blocking, daemon=True)
    th.start()
    try:
        while th.is_alive():
            th.join(15)
            st = m.stats()
            print("  %s  " % st["elapsed_text"] + "  ".join(
                "%s:%d次/失败%d" % (r["label"], r["n"], r["fail"]) for r in st["rows"]))
    except KeyboardInterrupt:
        m.stop()
        th.join(20)
    st = m.stats()
    print("\n%-12s %-28s %7s %7s %8s %10s %10s" % ("名称", "目标", "次数", "失败", "失败率%", "连续失败", "平均ms"))
    for r in st["rows"]:
        print("%-12s %-28s %7d %7d %8s %10d %10s" % (r["label"][:12], r["target"][:28], r["n"], r["fail"],
                                                    r["loss_pct"], r["max_streak"], r["avg"]))
    print("\n结论：%s" % st["verdict"])
    for n in st["notes"]:
        print("  " + n)
    if st["summary_csv"]:
        print("汇总：%s" % os.path.join(out_dir, st["summary_csv"]))
    return 0 if not any(r["fail"] for r in st["rows"]) else 1


def selftest_core():
    """自检核心（命令行 --selftest 与桌面窗口「自检」按钮共用）：在临时目录造演示数据，
    启动真实的 OPC UA 服务，用客户端读一遍，再检查 HTTPS 设置页面。
    不碰 config.json，不占用 4840 端口。返回 (是否通过, 说明)。
    注意：自检期间会临时占用全局 ALERTS / OpcUaApp.current，桌面窗口需先停止正式服务再调用。"""
    import shutil
    import socket
    root = logging.getLogger()
    old_level = root.level
    tmp = tempfile.mkdtemp(prefix="chrom_selftest_")
    try:
        import demo_data
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        demo_data.make_demo_db(os.path.join(data, "selftest_V3.db"))
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(endpoint="opc.tcp://127.0.0.1:%d/ChromExtract" % port,
                   watch_dirs=[data], security="none", log_file="")
        cfg["_base_dir"], cfg["_cfg_path"] = tmp, None
        normalize_config(cfg, tmp)
        root.setLevel(logging.ERROR)
        ok, msg = asyncio.run(_selftest_async(cfg, port))
        if ok:
            ok2, msg2 = _selftest_web(tmp)
            ok, msg = ok2, msg + "；" + msg2
    except Exception as e:  # noqa
        ok, msg = False, "%s: %s" % (type(e).__name__, e)
    finally:
        root.setLevel(old_level)      # 恢复日志级别：桌面窗口里自检完日志窗还要继续显示
        shutil.rmtree(tmp, ignore_errors=True)
    return ok, msg


def run_selftest():
    """--selftest：命令行自检。通过返回 0，失败返回 1。"""
    _force_utf8_stdout()  # 测试可直接调用本函数（不经 main），cp1252 控制台也要能印中文
    print("色谱提取 OPC UA 服务 v%s —— 自检开始（约需 10 秒）" % __version__)
    ok, msg = selftest_core()
    print(("[ 通过 ] " if ok else "[ 失败 ] ") + msg)
    if ok:
        print("程序本身工作正常。如果现场仍读不到数据，问题在 config.json 的监控目录或网络/防火墙。")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            user = json.load(f)
        cfg.update(user)
    return cfg


class ConfigError(Exception):
    pass


def _cfg_bool(cfg, key):
    """开关类配置：只接受 true/false（也容忍 0/1 和 "true"/"false" 等写法），其它值直接报错。"""
    v = cfg.get(key, DEFAULT_CONFIG[key])
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
        return v.strip().lower() in ("true", "1", "yes", "on")
    raise ConfigError("配置项 %s 只能是 true 或 false，当前为：%r" % (key, v))


def _clamp_int(cfg, key, lo, hi):
    try:
        v = int(cfg.get(key, DEFAULT_CONFIG[key]))
    except (TypeError, ValueError):
        raise ConfigError("配置项 %s 必须是整数，当前为：%r" % (key, cfg.get(key)))
    cfg[key] = max(lo, min(hi, v))


def normalize_config(cfg, base_dir):
    """校验并规整配置：数值夹到合理范围，相对路径按程序目录解析，明显的错误直接报出来。"""
    ep = str(cfg.get("endpoint", ""))
    if not ep.startswith("opc.tcp://"):
        raise ConfigError("endpoint 必须以 opc.tcp:// 开头，当前为：%r" % ep)
    sec = str(cfg.get("security", "none")).lower()
    if sec not in ("none", "sign", "sign_encrypt"):
        raise ConfigError("security 只能是 none / sign / sign_encrypt，当前为：%r" % sec)
    cfg["security"] = sec
    wh = str(cfg.get("web_https", "auto")).lower()
    if wh not in ("auto", "on", "off"):
        raise ConfigError("web_https 只能是 auto / on / off，当前为：%r" % wh)
    cfg["web_https"] = wh
    hook = str(cfg.get("alert_webhook") or "").strip()
    if hook and not hook.lower().startswith("https://"):
        raise ConfigError("alert_webhook 必须以 https:// 开头（告警内容不应明文传输），当前为：%r" % hook[:60])
    cfg["alert_webhook"] = hook
    ss = str(cfg.get("smtp_security") or "ssl").lower()
    if ss not in ("ssl", "starttls", "none"):
        raise ConfigError("smtp_security 只能是 ssl / starttls / none，当前为：%r" % ss)
    cfg["smtp_security"] = ss
    try:
        import probes as _probes
        cfg["alert_probes"] = _probes.normalize_probes(cfg.get("alert_probes"))
    except ValueError as e:
        raise ConfigError("alert_probes：%s" % e)
    try:
        cfg["alert_channel_rules"] = alerts.normalize_channel_rules(cfg.get("alert_channel_rules"))
    except ValueError as e:
        raise ConfigError("alert_channel_rules：%s" % e)
    try:
        cfg["alert_quiet"] = alerts.normalize_quiet(cfg.get("alert_quiet"))
    except ValueError as e:
        raise ConfigError("alert_quiet：%s" % e)
    for k in ("alert_webhook_level", "alert_email_level"):
        v = str(cfg.get(k) or "all").lower()
        if v not in alerts.LEVEL_CHOICES:
            raise ConfigError("%s 只能是 all / critical / off，当前为：%r" % (k, v))
        cfg[k] = v
    try:
        alerts.parse_recipients(cfg.get("alert_email_to"))
    except ValueError as e:
        raise ConfigError("alert_email_to：%s" % e)
    for k in ("smtp_host", "smtp_user", "smtp_from"):
        v = str(cfg.get(k) or "").strip()
        if any(c in v for c in "\r\n"):
            raise ConfigError("%s 不能包含换行" % k)
        cfg[k] = v
    if cfg["smtp_from"] and not alerts.EMAIL_RE.match(cfg["smtp_from"]):
        raise ConfigError("smtp_from 不是有效的邮箱地址：%r" % cfg["smtp_from"])
    if cfg["smtp_user"] and ss == "none":
        raise ConfigError("smtp_security=none 时不能配置 smtp_user：密码会明文传输。请改用 ssl 或 starttls。")
    if bool(cfg.get("web_cert_file")) != bool(cfg.get("web_key_file")):
        raise ConfigError("web_cert_file 和 web_key_file 必须同时填写（或都留空自动生成）")
    for key, lo, hi in (("poll_seconds", 5, 3600), ("last_n", 1, 100),
                        ("max_points", 100, 200000), ("max_connections", 1, 200),
                        ("export_min_interval_sec", 0, 3600), ("log_max_mb", 1, 100),
                        ("log_backups", 0, 20), ("max_trace_points", 1000, 5000000),
                        ("max_file_mb", 1, 2048), ("web_port", 1, 65535),
                        ("alert_stale_minutes", 0, 100000), ("alert_scan_errors", 0, 1000),
                        ("smtp_port", 1, 65535), ("env_check_hours", 0, 168),
                        ("test_send_min_interval_sec", 0, 3600),
                        ("transmission_stale_sec", 60, 604800)):
        _clamp_int(cfg, key, lo, hi)
    dirs = cfg.get("watch_dirs") or []
    if isinstance(dirs, str):
        dirs = [dirs]
    # 相对路径按"程序所在目录"解析，而不是启动时的当前目录
    # （从快捷方式/安装程序启动时，当前目录常常是 C:\Windows\System32，会找不到数据）
    cfg["watch_dirs"] = [d if os.path.isabs(d) else os.path.normpath(os.path.join(base_dir, d))
                         for d in map(str, dirs)]
    users = cfg.get("users") or {}
    if not isinstance(users, dict):
        raise ConfigError("users 必须是 {\"用户名\": \"密码哈希\"} 的形式")
    for name, h in users.items():
        if not str(h).startswith("pbkdf2_sha256$"):
            raise ConfigError("用户 %r 的密码必须是哈希值，不能直接写明文。\n"
                              "请不要手写：双击「安全管理.bat」，或运行 ChromExtractOPC.exe --add-user 用户名，"
                              "程序会自动生成并保存。" % name)
    cfg["users"] = users
    if not cfg.get("allow_anonymous", True) and not users:
        raise ConfigError("allow_anonymous=false 但没有配置任何 users，将没有人能连接。")
    cfg["allow_test_send"] = bool(cfg.get("allow_test_send", True))
    # v3.0 桌面窗口设置
    for k in DESKTOP_BOOL_KEYS:
        cfg[k] = _cfg_bool(cfg, k)
    lv = str(cfg.get("desktop_notify_level") or "all").strip().lower()
    if lv not in ("all", "critical"):
        raise ConfigError("desktop_notify_level 只能是 all / critical，当前为：%r" % lv)
    cfg["desktop_notify_level"] = lv
    _normalize_v31(cfg)
    chrom_core.set_limits(max_points=cfg["max_trace_points"], max_file_mb=cfg["max_file_mb"])
    return cfg


def _clamp_float(cfg, key, lo, hi):
    try:
        v = float(cfg.get(key, DEFAULT_CONFIG[key]))
    except (TypeError, ValueError):
        raise ConfigError("配置项 %s 必须是数字，当前为：%r" % (key, cfg.get(key)))
    if v != v or v in (float("inf"), float("-inf")):
        raise ConfigError("配置项 %s 不能是无穷大或 NaN" % key)
    cfg[key] = max(lo, min(hi, v))


def _normalize_v31(cfg):
    """v3.1 新配置项校验：界面语言、历史数据、峰识别、Modbus 转发、运行报表、桌面登录与角色。"""
    import i18n
    import modbus_server
    import peaks as _peaks
    import reports
    import audit
    try:
        cfg["desktop_language"] = i18n.normalize_language(cfg.get("desktop_language", "auto"))
    except ValueError as e:
        raise ConfigError(str(e))
    cfg["history_enabled"] = _cfg_bool(cfg, "history_enabled")
    cfg["peaks_publish"] = _cfg_bool(cfg, "peaks_publish")
    cfg["modbus_server_enabled"] = _cfg_bool(cfg, "modbus_server_enabled")
    for key, lo, hi in (("history_retention_days", 1, 3650), ("history_max_mb", 10, 102400),
                        ("peak_smooth", 1, 501), ("peaks_publish_max", 1, 500),
                        ("modbus_server_port", 1, 65535), ("modbus_server_unit", 0, 255),
                        ("desktop_idle_lock_minutes", 0, 1440), ("backup_keep", 1, 100)):
        _clamp_int(cfg, key, lo, hi)
    for key, lo, hi in (("peak_min_height", 0.0, 1e12), ("peak_min_width", 0.0, 3600.0),
                        ("peak_noise_factor", 0.5, 1000.0), ("peak_start", 0.0, 1e6), ("peak_end", 0.0, 1e6)):
        _clamp_float(cfg, key, lo, hi)
    try:
        _peaks.params_from_config(cfg)
    except ValueError as e:
        raise ConfigError("峰识别参数：%s" % e)
    ps = str(cfg.get("peak_series") or "value").strip().lower()
    if ps not in ("value", "uv", "raw"):
        raise ConfigError("peak_series 只能是 value / uv / raw，当前为：%r" % ps)
    cfg["peak_series"] = ps
    hf = str(cfg.get("history_file") or "history/history.db").strip()
    if any(c in hf for c in "\r\n\x00") or not hf.lower().endswith(".db"):
        raise ConfigError("history_file 必须是 .db 文件路径，当前为：%r" % hf)
    cfg["history_file"] = hf
    host = str(cfg.get("modbus_server_host") or "127.0.0.1").strip()
    import ipaddress
    if host != "localhost":
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise ConfigError("modbus_server_host 必须是 IP 地址（例如 127.0.0.1 或 0.0.0.0），当前为：%r" % host)
    cfg["modbus_server_host"] = host
    order = str(cfg.get("modbus_server_order") or "ABCD").strip().upper()
    if order not in modbus_server.ORDERS:
        raise ConfigError("modbus_server_order 只能是 ABCD / CDAB / BADC / DCBA，当前为：%r" % order)
    cfg["modbus_server_order"] = order
    try:
        cfg["modbus_server_allow"] = modbus_server.normalize_allow(cfg.get("modbus_server_allow"))
        cfg["modbus_server_map"] = modbus_server.normalize_map(cfg.get("modbus_server_map"))
    except ValueError as e:
        raise ConfigError("Modbus 转发：%s" % e)
    try:
        cfg["desktop_report_auto"] = reports.normalize_mode(cfg.get("desktop_report_auto"))
        cfg["desktop_report_shifts"] = reports.normalize_shifts(cfg.get("desktop_report_shifts"))
    except ValueError as e:
        raise ConfigError("运行报表：%s" % e)
    rd = str(cfg.get("desktop_report_dir") or "运行报表").strip()
    if any(c in rd for c in "\r\n\x00"):
        raise ConfigError("desktop_report_dir 不能包含换行")
    cfg["desktop_report_dir"] = rd
    try:
        # 已删除的账号的角色直接忽略（不报错，避免删账号后配置“坏掉”）
        cfg["user_roles"] = audit.normalize_roles(cfg.get("user_roles"), cfg.get("users") or {})
    except ValueError as e:
        raise ConfigError("user_roles：%s" % e)


def security_report(cfg):
    """安全体检：返回 [(级别, 说明)]，级别：正常 / 注意 / 风险。"""
    out = []
    sec = cfg.get("security", "none")
    users = cfg.get("users") or {}
    anon = cfg.get("allow_anonymous", True)
    host = cfg.get("endpoint", "").split("//", 1)[-1].split(":")[0]
    if sec == "none":
        out.append(("注意", "通信未加密（security=none）：同一网络里的人可以监听数据。建议只在隔离的控制网使用，或改为 sign_encrypt。"))
    else:
        out.append(("正常", "通信已启用 %s（Basic256Sha256）。" % sec))
        if cfg.get("allow_insecure_fallback"):
            out.append(("风险", "allow_insecure_fallback=true：仍开放不加密端点，加密形同虚设（仅排障用，用完请关）。"))
    if anon and not users:
        out.append(("注意", "任何人无需登录即可只读访问。数据不可被改写，但可被读取。"))
    elif anon and users:
        out.append(("注意", "已配置登录账号，但仍允许匿名只读连接。要强制登录请设 allow_anonymous=false。"))
    else:
        out.append(("正常", "必须登录才能访问（%d 个账号，密码以哈希保存）。" % len(users)))
    if users and sec == "none":
        out.append(("风险", "配置了用户名密码但没有加密：密码会在网络上明文传输！请同时启用 sign_encrypt。"))
    if sec != "none":
        tdir = os.path.join(cfg.get("_base_dir", "."), cfg.get("trusted_client_certs_dir") or "certs/trusted")
        try:
            n = len(load_trusted_fingerprints(tdir))
        except Exception:  # noqa
            n = 0
        if n:
            out.append(("正常", "客户端证书白名单已启用（%d 个客户端）。" % n))
        else:
            out.append(("注意", "未启用客户端证书白名单：把可信客户端的证书放进 %s 即可启用。" % tdir))
    if host in ("0.0.0.0", "::", ""):
        out.append(("注意", "监听所有网卡（0.0.0.0）。如只给某一块网卡使用，请把 endpoint 改成该网卡 IP，并在防火墙限制来源地址。"))
    if str(cfg.get("smtp_password") or "").startswith("b64:"):
        out.append(("注意", "SMTP 密码只做了简单编码（非 Windows 系统无法用系统加密），请限制 config.json 的访问权限。"))
    if cfg.get("smtp_host") and cfg.get("smtp_security") == "none":
        out.append(("注意", "告警邮件使用未加密连接（smtp_security=none），只适合内网免登录中继。"))
    if cfg.get("web_ui", True):
        wh_host = str(cfg.get("web_host") or "127.0.0.1")
        if wh_host not in ("127.0.0.1", "localhost", "::1"):
            if cfg.get("web_https", "auto") == "off":
                out.append(("风险", "设置页面对外开放（%s）但关闭了 HTTPS：登录密码会明文传输。请把 web_https 改为 auto 或 on。" % wh_host))
            else:
                out.append(("注意", "设置页面对外开放（%s），已启用 HTTPS 和账号登录。建议在防火墙限制来源地址。" % wh_host))
    if cfg.get("remote_export", True):
        out.append(("注意", "允许远程调用 ExportCSV（已限流 %s 秒/次）。不需要可设 remote_export=false。"
                    % cfg.get("export_min_interval_sec")))
    if cfg.get("allow_test_send", True):
        out.append(("注意", "允许远程调用 SendTestData 发送测试数据（已限流 %s 秒/次，仅写 Station_TEST "
                            "测试通道，不改真实数据）。不需要可设 allow_test_send=false。"
                    % cfg.get("test_send_min_interval_sec")))
    else:
        out.append(("正常", "已禁止远程发送测试数据（allow_test_send=false）。"))
    return out


def print_security_report(cfg):
    icons = {"正常": "[ 正常 ]", "注意": "[ 注意 ]", "风险": "[ 风险 ]"}
    print("安全体检（配置文件：%s）" % cfg.get("_cfg_path", ""))
    for lv, msg in security_report(cfg):
        print("  %s %s" % (icons[lv], msg))


def program_dir():
    """程序所在目录（打包后是 exe 所在目录）。配置里的相对路径、证书、日志都以它为准。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_normalized(cfg_path):
    """读取并校验配置，返回可直接用于体检的 cfg；有问题抛 ConfigError。"""
    cfg = load_config(cfg_path)
    cfg["_base_dir"] = program_dir()
    cfg["_cfg_path"] = cfg_path
    normalize_config(cfg, cfg["_base_dir"])
    return cfg


def _prompt_password(username):
    """交互式输入密码（两次确认 + 强度检查），最多尝试 3 次。失败返回 None。"""
    for _ in range(3):
        pw = getpass.getpass("为 %s 设置密码（输入时不显示）：" % username)
        err = config_tools.check_password_strength(pw, username)
        if err:
            print("  " + err)
            continue
        if getpass.getpass("再输入一次：") != pw:
            print("  两次输入不一致，请重来。")
            continue
        return pw
    print("尝试次数过多，已取消。")
    return None


def _show_cfg_report(cfg_path):
    try:
        cfg = load_normalized(cfg_path)
    except ConfigError as e:
        print("配置有误：%s" % e)
        return
    print_security_report(cfg)


_RELOAD_NOTE = ("提示：如果服务正在运行，账号和匿名开关的改动会在下一轮扫描时自动生效；"
                "加密相关的改动需要重启服务。")


def do_add_user(cfg_path, name):
    pw = _prompt_password(name)
    if pw is None:
        return 2
    r = config_tools.add_user(cfg_path, name, pw)
    print("账号 %s：%s。" % (name, "已创建" if r == "added" else "密码已重设"))
    print(_RELOAD_NOTE)
    return 0


def do_harden(cfg_path, name=None):
    if not name:
        name = input("给服务设置一个登录用户名（例如 dcs）：").strip()
    err = config_tools.check_username(name)
    if err:
        print(err)
        return 2
    pw = _prompt_password(name)
    if pw is None:
        return 2
    for line in config_tools.harden(cfg_path, name, pw):
        print("  ✓ " + line)
    print()
    _show_cfg_report(cfg_path)
    print("\n接下来：")
    print("  1. 重启服务，加密设置才会生效。")
    print("  2. 客户端（DCS/OPC 客户端）需要改为：加密 Basic256Sha256 + 用户名密码，并在客户端里信任本服务的证书。")
    print("  3. 建议再运行 设置防火墙.bat，只允许可信地址访问。")
    return 0


def admin_menu(cfg_path):
    """交互式安全管理菜单：给不想改 JSON 的人用。"""
    while True:
        print("\n===== 色谱提取服务 · 安全管理 =====")
        print("配置文件：%s" % cfg_path)
        print("  1  安全体检")
        print("  2  一键加固（开启加密 + 关闭匿名 + 创建账号）")
        print("  3  添加账号 / 重设密码")
        print("  4  删除账号")
        print("  5  查看账号")
        print("  0  退出")
        try:
            choice = input("请选择：").strip()
        except EOFError:
            return 0
        try:
            if choice == "1":
                _show_cfg_report(cfg_path)
            elif choice == "2":
                do_harden(cfg_path)
            elif choice == "3":
                name = input("用户名：").strip()
                err = config_tools.check_username(name)
                print(err) if err else do_add_user(cfg_path, name)
            elif choice == "4":
                users = config_tools.list_users(cfg_path)
                print("现有账号：%s" % ("、".join(users) or "（无）"))
                name = input("要删除的用户名：").strip()
                config_tools.remove_user(cfg_path, name)
                print("账号 %s 已删除。" % name)
                print(_RELOAD_NOTE)
            elif choice == "5":
                users = config_tools.list_users(cfg_path)
                print("账号：%s" % ("、".join(users) or "（无）"))
            elif choice == "0":
                return 0
            else:
                print("请输入菜单里的数字。")
        except config_tools.ConfigToolError as e:
            print("操作失败：%s" % e)


# v2.9 起取消的后台运行方式：旧脚本/快捷方式还带着这些参数时给出明确说明，而不是 argparse 的英文报错
REMOVED_ARGS = ("--background", "--status", "--stop", "--service", "--install-service",
                "--uninstall-service", "--start-service", "--stop-service", "--service-status")
REMOVED_NOTE = ("v2.9 起本程序改为桌面软件：已取消后台静默运行、Windows 服务和开机自启（参数 %s 不再可用）。\n"
                "请双击 ChromExtractOPC.exe 打开主窗口，在窗口里启动/停止服务；关闭窗口即停止服务。\n"
                "旧版本遗留的计划任务或 Windows 服务可运行 卸载.bat（管理员）清理。")


def main(argv=None):
    _force_utf8_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    legacy = [a for a in argv if a.split("=", 1)[0] in REMOVED_ARGS]
    if legacy:
        print(REMOVED_NOTE % "、".join(legacy))
        return 2
    ap = argparse.ArgumentParser(description="色谱提取 OPC UA 服务（桌面软件；带下列工具参数时只运行该工具、不开窗口）")
    ap.add_argument("--version", action="version", version="ChromExtractOPC %s" % __version__)
    ap.add_argument("--selftest", action="store_true",
                    help="自检：造演示数据、启动服务、用客户端读一遍（不影响现有配置）")
    ap.add_argument("--config", default=None, help="配置文件（默认：程序目录 config.json）")
    ap.add_argument("--gui", action="store_true",
                    help="打开桌面主窗口并自动启动服务（不带任何参数时的默认行为）")
    ap.add_argument("--desktop", action="store_true", help="同 --gui：打开桌面主窗口")
    ap.add_argument("--settings", action="store_true",
                    help="打开桌面主窗口，但不自动启动服务（窗口里可点“启动服务”）")
    ap.add_argument("--stability", nargs="+", metavar="参数",
                    help="长时间稳定性测试，参数同 --nettest，另加 interval=秒 minutes=分钟，例如："
                         "--stability modbus_rtu port=COM3 baud=9600 unit=1 interval=1 minutes=60")
    ap.add_argument("--stability-multi", nargs="+", metavar="参数",
                    help="多目标稳定性测试：--stability-multi 目标.json [interval=秒] [minutes=分钟]；"
                         '目标.json 例如 [{"label":"GC-1","kind":"modbus_rtu","params":{"port":"COM3","unit":1}}, ...]')
    ap.add_argument("--drivercheck", action="store_true", help="驱动与软件环境检测（串口驱动、运行库、防火墙、端口、数据目录、旧版后台自启残留）")
    ap.add_argument("--nettest", nargs="+", metavar="参数",
                    help="连接测试，例如：--nettest ping 192.168.1.10 / --nettest tcp 192.168.1.10 4840 / "
                         "--nettest serial_list / --nettest modbus_rtu port=COM3 baud=9600 unit=1 "
                         "（类型：ping tcp dns interfaces opcua modbus_tcp serial_list serial loopback modbus_rtu）")
    ap.add_argument("--extract", nargs="+", metavar="源",
                    help="命令行导出 CSV（源可以是 *_V3.db / *.mbd / 文件夹）")
    ap.add_argument("-o", "--out", default="谱图导出", help="导出文件夹")
    ap.add_argument("--last", type=int, default=None, help="每个流路只导出最近 N 个周期")
    ap.add_argument("--list", nargs="+", metavar="源", help="只查看数据源里有什么")
    ap.add_argument("--hash-password", action="store_true",
                    help="生成登录密码的哈希（粘贴到 config.json 的 users 里）")
    ap.add_argument("--check-security", action="store_true", help="安全体检：检查当前配置的安全状况")
    ap.add_argument("--check-transmission", nargs="?", const="", default=None, metavar="endpoint",
                    help="数据传输检测：连接 OPC UA 服务并逐步验证读/订阅/回显（缺省用 config 的 endpoint）")
    ap.add_argument("--send-test-data", action="store_true",
                    help="配合 --check-transmission：检测时顺带调用 SendTestData 发送测试数据")
    ap.add_argument("--check-seconds", type=float, default=6,
                    help="数据传输检测的订阅时长（秒，默认 6）")
    ap.add_argument("--diagnose", action="store_true",
                    help="智能诊断：自动跑一次传输检测，再做配置—服务—数据—传输—日志五层诊断")
    ap.add_argument("--diagnose-485", metavar="串口", default=None,
                    help="RS-485 数据配型自诊断：自动试串口参数读 Modbus RTU 寄存器，"
                         "再试数据类型与字节序给出推荐配型，例如：--diagnose-485 COM3")
    ap.add_argument("--slave", type=int, default=1, help="485 诊断的从站站号（默认 1）")
    ap.add_argument("--scan-slaves", action="store_true",
                    help="485 诊断时扫描站号 1-247（慢，有超时上限）")
    ap.add_argument("--register", type=int, default=0, help="485 诊断的起始寄存器地址（默认 0）")
    ap.add_argument("--count", type=int, default=4, help="485 诊断读的寄存器个数（默认 4）")
    ap.add_argument("--function", type=int, default=3, choices=(1, 2, 3, 4, 5, 6, 15, 16),
                    help="功能码：485 诊断只用 3/4（只读）；Modbus Poll 可用 01/02/03/04/05/06/15/16")
    ap.add_argument("--baud", type=int, default=None, help="485 诊断指定波特率（不指定则自动试常用波特率）")
    ap.add_argument("--timeout-ms", type=int, default=300, help="485 诊断单次应答超时毫秒（默认 300）")
    ap.add_argument("--list-serial", action="store_true", help="列出本机串口")
    ap.add_argument("--modbus-poll", action="store_true",
                    help="Modbus 主站测试（Poll）：配合 --tcp 或 --rtu、--slave、--function、--address、--count；"
                         "写功能配合 --values，例如：--modbus-poll --tcp 192.168.1.10:502 --function 3 --address 0 --count 10")
    ap.add_argument("--modbus-slave", action="store_true",
                    help="Modbus 从站模拟器（Slave）：配合 --tcp-port 或 --rtu、--slave，Ctrl+C 停止")
    ap.add_argument("--tcp", metavar="主机:端口", default=None,
                    help="Modbus Poll 的 TCP 目标，例如 192.168.1.10:502（端口可省略，默认 502）")
    ap.add_argument("--rtu", metavar="串口", default=None,
                    help="Modbus Poll/Slave 的串口，例如 COM3")
    ap.add_argument("--tcp-port", type=int, default=502, help="Modbus Slave 的 TCP 监听端口（默认 502）")
    ap.add_argument("--host", default="0.0.0.0", help="Modbus Slave 的监听地址（默认 0.0.0.0）")
    ap.add_argument("--address", type=int, default=0, help="Modbus Poll 的起始地址（默认 0）")
    ap.add_argument("--poll-seconds", type=float, default=0,
                    help="Modbus Poll 连续轮询间隔秒（0=只测一次，>0 时一直轮询到 Ctrl+C）")
    ap.add_argument("--values", default=None,
                    help="Modbus Poll 写功能的值（逗号分隔；寄存器按 --data-type/--byte-order 解释）")
    ap.add_argument("--data-type", default="uint16",
                    choices=("int16", "uint16", "int32", "uint32", "float32", "float64"),
                    help="寄存器数据类型（默认 uint16）")
    ap.add_argument("--byte-order", default="ABCD", choices=("ABCD", "CDAB", "BADC", "DCBA"),
                    help="字节序（默认 ABCD）")
    ap.add_argument("--parity", default="N", choices=("N", "E", "O"), help="RTU 校验位（默认 N）")
    ap.add_argument("--stopbits", type=float, default=1, choices=(1, 1.5, 2), help="RTU 停止位（默认 1）")
    ap.add_argument("--size", type=int, default=10000, help="Modbus Slave 每区数据个数（默认 10000）")
    ap.add_argument("--admin", action="store_true", help="安全管理菜单（体检/一键加固/账号管理）")
    ap.add_argument("--harden", action="store_true", help="一键加固：开启加密、关闭匿名、创建登录账号")
    ap.add_argument("--add-user", metavar="用户名", help="添加账号或重设密码（会提示输入密码）")
    ap.add_argument("--remove-user", metavar="用户名", help="删除账号")
    ap.add_argument("--list-users", action="store_true", help="列出账号")
    ap.add_argument("--backup", metavar="文件", default=None,
                    help="导出配置备份包（config.json、账号、证书与私钥、信任的客户端证书）到指定 .zip；"
                         "配合 --encrypt 用密码加密（推荐），--with-logs 同时备份日志")
    ap.add_argument("--restore", metavar="文件", default=None,
                    help="从备份包还原配置（先校验并显示将覆盖的文件，确认后还原；还原前自动备份当前配置）")
    ap.add_argument("--encrypt", action="store_true", help="配合 --backup：用密码加密备份包（会提示输入密码）")
    ap.add_argument("--with-logs", action="store_true", help="配合 --backup / --restore：包含日志")
    ap.add_argument("--yes", action="store_true", help="配合 --restore：不再询问，直接还原")
    ap.add_argument("--set-watch-dir", nargs="+", metavar="文件夹",
                    help="把数据目录（存放 *_V3.db / .mbd 的文件夹）写进配置文件")
    args = ap.parse_args(argv)

    if args.selftest:
        return run_selftest()

    if args.nettest:
        return run_nettest_cli(args.nettest)
    if args.stability:
        return run_stability_cli(args.stability)
    if args.stability_multi:
        return run_stability_multi_cli(args.stability_multi)
    if args.modbus_poll:
        return run_modbus_poll_cli(args)
    if args.modbus_slave:
        return run_modbus_slave_cli(args)

    base_dir = program_dir()
    cfg_path = args.config or os.path.join(base_dir, "config.json")

    if args.admin or args.harden or args.add_user or args.remove_user or args.list_users:
        try:
            if args.admin:
                return admin_menu(cfg_path)
            if args.harden:
                return do_harden(cfg_path)
            if args.add_user:
                err = config_tools.check_username(args.add_user)
                if err:
                    print(err)
                    return 2
                return do_add_user(cfg_path, args.add_user)
            if args.remove_user:
                config_tools.remove_user(cfg_path, args.remove_user)
                print("账号 %s 已删除。" % args.remove_user)
                print(_RELOAD_NOTE)
                return 0
            print("账号：%s" % ("、".join(config_tools.list_users(cfg_path)) or "（无）"))
            return 0
        except config_tools.ConfigToolError as e:
            print("操作失败：%s" % e)
            return 2

    if args.backup:
        return run_backup_cli(args.backup, base_dir, cfg_path, args.encrypt, args.with_logs)
    if args.restore:
        return run_restore_cli(args.restore, base_dir, cfg_path, args.with_logs, args.yes)

    if args.set_watch_dir:
        try:
            dirs = config_tools.set_watch_dirs(cfg_path, args.set_watch_dir)
        except config_tools.ConfigToolError as e:
            print("操作失败：%s" % e)
            return 2
        print("数据目录已设置为：")
        for d in dirs:
            try:
                n = len(chrom_core.find_inputs([d]))
            except Exception:  # noqa
                n = 0
            print("  %s  （其中找到 %d 个数据文件）" % (d, n))
            if n == 0:
                print("    注意：这个文件夹里暂时没有 *_V3.db 或 .mbd 文件，请确认路径是否对。")
        print("服务运行中的话，下一轮扫描（最长 30 秒）内自动生效。")
        return 0

    if args.hash_password:
        pw = getpass.getpass("输入密码：")
        if len(pw) < 8:
            print("密码太短：至少 8 个字符。")
            return 2
        if getpass.getpass("再输入一次：") != pw:
            print("两次输入不一致。")
            return 2
        print('\n把下面这一行放进 config.json 的 "users" 里（把"用户名"换成实际用户名）：\n')
        print('    "users": { "用户名": "%s" }' % hash_password(pw))
        return 0

    cfg = load_config(cfg_path)
    cfg["_base_dir"] = base_dir
    cfg["_cfg_path"] = cfg_path
    try:
        normalize_config(cfg, base_dir)
    except ConfigError as e:
        msg = "配置有误（%s）：\n  %s" % (cfg_path, e)
        print(msg)
        if _wants_window(args):
            show_error_dialog("配置有误", msg + "\n\n请用记事本修改 config.json（修改前会自动备份为 config.json.bak），"
                                          "或删除 config.json 后重新打开程序使用默认配置。")
        return 2

    if args.drivercheck:
        import drivercheck
        r = drivercheck.run_all(cfg)
        icons = {"正常": "[ 正常 ]", "注意": "[ 注意 ]", "问题": "[ 问题 ]"}
        print("驱动与软件环境检测：%s（%d ms）" % (r["summary"], r["elapsed_ms"]))
        for it in r["items"]:
            print("  %s %s：%s" % (icons[it["level"]], it["name"], it["text"]))
            if it["fix"]:
                print("           处理：%s" % it["fix"])
        return 0 if r["ok"] else 1

    if args.check_security:
        print_security_report(cfg)
        return 0

    if args.check_transmission is not None:
        ep = args.check_transmission or _local_endpoint(cfg["endpoint"])
        return run_transmission_check(ep, cfg=cfg, duration_sec=args.check_seconds,
                                      do_send_test=args.send_test_data)

    if args.diagnose:
        return run_diagnose(cfg, duration_sec=args.check_seconds,
                            do_send_test=args.send_test_data)

    if args.list_serial:
        import nettest
        r = nettest.list_serial_ports()
        print(("[ 成功 ] " if r["ok"] else "[ 失败 ] ") + r["summary"])
        for line in r["details"]:
            print("    " + line)
        return 0 if r["ok"] else 1

    if args.diagnose_485:
        return run_rtu485_cli(args)

    if args.list:
        chrom_core.list_sources(args.list)
        return 0
    if args.extract:
        setup_logging(cfg, base_dir)
        chrom_core.extract(args.extract, args.out, last=args.last)
        return 0

    # 没有任何工具参数：打开桌面主窗口（--settings 时不自动启动服务）
    return run_desktop_app(cfg, base_dir, cfg_path, start_service=not args.settings)


def _backup_password_from_env():
    return os.environ.get("CHROMEXTRACT_BACKUP_PASSWORD") or None


def run_backup_cli(out, base_dir, cfg_path, encrypt=False, with_logs=False, password=None):
    """命令行导出备份：--backup 文件 [--encrypt] [--with-logs]。
    密码：环境变量 CHROMEXTRACT_BACKUP_PASSWORD（脚本用），否则提示输入两次。"""
    import backup_tools
    pw = None
    if encrypt:
        pw = password or _backup_password_from_env()
        if not pw:
            pw = getpass.getpass("设置备份密码（至少 8 位）：")
            if len(pw) < 8:
                print("密码太短：至少 8 个字符。")
                return 2
            if getpass.getpass("再输入一次：") != pw:
                print("两次输入不一致。")
                return 2
    try:
        plan = backup_tools.plan_export(base_dir, cfg_path, with_logs)
        if plan["private_keys"] and not pw:
            print("警告：备份里包含证书私钥（%s），而备份包没有加密。"
                  "请妥善保管，或加 --encrypt 用密码加密。" % "、".join(plan["private_keys"]))
        r = backup_tools.export_backup(out, base_dir, cfg_path, password=pw, include_logs=with_logs)
    except (backup_tools.BackupError, OSError, ValueError) as e:
        print("备份失败：%s" % e)
        return 2
    print("已导出备份：%s（%d 个文件，%s）" % (r["path"], r["files"], "已加密" if r["encrypted"] else "未加密"))
    for w in r["warnings"]:
        print("注意：%s" % w)
    return 0


def run_restore_cli(path, base_dir, cfg_path, with_logs=False, yes=False, password=None):
    """命令行还原：--restore 文件 [--with-logs] [--yes]。加密包的密码同上（环境变量或提示输入）。"""
    import backup_tools
    pw = password or _backup_password_from_env()
    validate = backup_tools.make_validator(sys.modules[__name__], base_dir)
    try:
        try:
            info = backup_tools.inspect_backup(path, base_dir, cfg_path, pw, with_logs)
        except backup_tools.PasswordRequired:
            pw = getpass.getpass("备份包已加密，请输入密码：")
            info = backup_tools.inspect_backup(path, base_dir, cfg_path, pw, with_logs)
        print(backup_tools.summary_text(info))
        if not yes:
            ans = input("确定还原吗？还原前会自动备份当前配置。输入 y 确认：").strip().lower()
            if ans not in ("y", "yes"):
                print("已取消。")
                return 1
        r = backup_tools.restore_backup(path, base_dir, cfg_path, pw, with_logs, validate=validate,
                                        keep=load_config(cfg_path).get("backup_keep", 10))
    except (backup_tools.BackupError, OSError, ValueError) as e:
        print("还原失败：%s" % e)
        return 2
    print("已还原 %d 个文件。还原前的配置已自动备份到：%s" % (len(r["written"]), r["auto_backup"] or "（无）"))
    if r["smtp_cleared"]:
        print("SMTP 密码来自另一台电脑，已清空：请在窗口“告警与通知”里重新填写。")
    print("请重启服务（在桌面窗口点“重启服务”，或关闭后重新打开程序）让证书和端口等设置生效。")
    return 0


def _wants_window(args):
    """这次启动是不是要开桌面窗口（而不是只跑某个命令行工具）。"""
    return not (args.drivercheck or args.check_security or args.check_transmission is not None
                or args.diagnose or args.list_serial or args.diagnose_485 or args.list or args.extract)


def show_error_dialog(title, text):
    """窗口版 EXE 没有控制台：启动阶段的错误用对话框告诉用户。无图形环境时静默跳过。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        _set_window_icon(r)
        messagebox.showerror(title, text, parent=r)
        r.destroy()
    except Exception:  # noqa
        pass


def show_info_dialog(title, text):
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        _set_window_icon(r)
        messagebox.showinfo(title, text, parent=r)
        r.destroy()
    except Exception:  # noqa
        pass


def run_desktop_app(cfg, base_dir, cfg_path, start_service=True):
    """桌面软件主流程：单实例检查 → 日志 → 启动提示 → 打开主窗口（窗口里控制服务启停）。
    服务只在窗口存在期间运行：关闭窗口 = 停止服务，没有任何后台/常驻进程。"""
    import instance_lock
    instance = instance_lock.SingleInstance(base_dir)
    if not instance.acquire():
        msg = ("色谱提取 OPC UA 服务已经打开了一个窗口（同一台电脑只运行一个，避免抢占端口 %s）。\n"
               "请在任务栏找到已打开的窗口。" % cfg.get("endpoint"))
        print(msg)
        show_info_dialog("程序已在运行", msg)
        return 0
    try:
        setup_logging(cfg, base_dir)
        get_log().info("色谱提取 OPC UA 服务 v%s 桌面窗口打开，配置：%s", __version__, cfg_path)
        for lv, msg in security_report(cfg):
            if lv != "正常":
                get_log().warning("安全提示[%s]：%s", lv, msg)
        for w in watch_dir_warnings(cfg):
            get_log().warning("数据目录提示：%s", w)
        # v3.0：“打开窗口后自动启动服务”“启动时最小化”两个桌面选项
        import desktop_features
        start_service, minimized = desktop_features.startup_behavior(cfg, start_service)
        if not start_service and cfg.get("desktop_auto_start_service", True) is False:
            get_log().info("已设置“打开窗口后不自动启动服务”：需要时点窗口上方“启动服务”")
        return run_gui(cfg, start_service=start_service, minimized=minimized)
    finally:
        instance.release()


# 打开窗口的参数（其余以 - 开头的参数都表示“只运行命令行工具”）
WINDOW_ARGS = ("--gui", "--desktop", "--settings", "--config")
# 需要控制台交互（输入密码 / Ctrl+C 结束）的工具：窗口版 EXE 没有控制台，提示改用命令行版
CONSOLE_ONLY_ARGS = ("--admin", "--harden", "--add-user", "--hash-password", "--modbus-slave", "--poll-seconds",
                     "--restore", "--encrypt")
CLI_EXE = "ChromExtractOPC-cli.exe"


def _is_tool_invocation(argv):
    return any(a.startswith("-") and a.split("=", 1)[0] not in WINDOW_ARGS for a in argv)


def _entry():
    """程序入口：统一兜住未预期的错误。
    - 窗口版 EXE（PyInstaller --windowed，没有控制台）：不带参数 = 打开桌面主窗口；
      带工具参数时把输出收集起来，结束后用对话框显示（脚本请用命令行版 ChromExtractOPC-cli.exe）。
    - 命令行版 EXE / 源码运行：输出照常打印；双击出错时停住窗口。"""
    rc = 0
    # 安装程序等自动调用时加 --no-pause：出错也不停住等回车（否则隐藏窗口会一直卡住）
    no_pause = "--no-pause" in sys.argv
    if no_pause:
        sys.argv = [x for x in sys.argv if x != "--no-pause"]
    argv = sys.argv[1:]
    windowed = sys.stdout is None
    captured = None
    if windowed:
        import io
        if _is_tool_invocation(argv):
            if any(a.split("=", 1)[0] in CONSOLE_ONLY_ARGS for a in argv):
                show_error_dialog("请使用命令行版",
                                  "这个功能需要在命令行窗口里输入或按 Ctrl+C 结束，窗口版程序无法使用。\n"
                                  "请在命令提示符里运行：%s %s\n（或双击 安全管理.bat）" % (CLI_EXE, " ".join(argv)))
                return 2
            captured = io.StringIO()
            sys.stdout = sys.stderr = captured
        else:
            sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        rc = main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except KeyboardInterrupt:
        rc = 0
    except Exception:  # noqa
        import traceback
        tb = traceback.format_exc()
        try:
            print("程序遇到未预期的错误：\n" + tb)
        except Exception:  # noqa  没有控制台
            pass
        try:
            logging.getLogger("chrom").error("程序遇到未预期的错误，已退出：%s", tb.strip().splitlines()[-1])
        except Exception:  # noqa
            pass
        try:
            d = os.path.join(program_dir(), "logs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "crash.log"), "a", encoding="utf-8") as f:
                f.write("%s\n%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), tb))
        except Exception:  # noqa
            pass
        if windowed and captured is None:
            show_error_dialog("程序出错", "程序遇到未预期的错误，已退出。详情见 logs\\crash.log：\n\n" +
                              tb.strip().splitlines()[-1])
        rc = 1
    if captured is not None:
        text = captured.getvalue().strip() or "（没有输出）"
        if len(text) > 3000:
            text = "……\n" + text[-3000:]
        text += "\n\n退出代码：%s。需要在脚本里使用时请改用 %s。" % (rc, CLI_EXE)
        (show_info_dialog if rc in (0, None) else show_error_dialog)("色谱提取 OPC UA 服务", text)
    elif not no_pause and not windowed:
        _pause_before_exit(rc)
    return rc


if __name__ == "__main__":
    sys.exit(_entry())
