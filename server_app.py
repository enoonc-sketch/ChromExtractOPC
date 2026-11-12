#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
色谱提取 OPC UA 服务（ChromExtract OPC UA Server）

功能：
  1. 监控配置目录中的 DataBrowser 日志数据库（*_V3.db）和 .mbd 谱图文件；
  2. 解析最新谱图（BinTrc），通过 OPC UA Server 发布，供 DCS/SCADA/实时库订阅；
  3. 提供 OPC UA 方法 ExportCSV，可远程触发把最新数据导出为 CSV；
  4. 命令行模式：--extract 直接导出 CSV，--list 查看数据源，--gui 打开监控窗口。

用法：
  server_app.py                      # 按同目录 config.json 启动 OPC UA 服务
  server_app.py --config 配置.json   # 指定配置文件
  server_app.py --extract 数据文件夹 -o 输出文件夹 [--last 20]
  server_app.py --list 数据文件夹
  server_app.py --gui                # 图形监控窗口（服务同时启动）

OPC UA 地址空间（ns=<namespace index>，启动日志会打印实际值）：
  Objects/Chromatograph
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
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
from datetime import datetime

import chrom_core

__version__ = chrom_core.__version__

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
}

log_q = queue.Queue()


def get_log():
    return logging.getLogger("chrom")


def setup_logging(cfg, base_dir):
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [sh]
    if cfg.get("log_file"):
        lf = os.path.join(base_dir, cfg["log_file"])
        os.makedirs(os.path.dirname(lf), exist_ok=True)
        fh = logging.FileHandler(lf, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


# ---------------------------------------------------------------------------
# 数据快照：在后台线程扫描数据源，解析出最新谱图
# ---------------------------------------------------------------------------

def gather_snapshot(watch_dirs, last_n, max_points):
    """扫描所有数据源，返回 [dict] 每个检测器一条最新谱图记录。"""
    records = []
    tmp = None
    try:
        srcs = chrom_core.find_inputs(watch_dirs)
    except FileNotFoundError as e:
        get_log().warning("监控路径不存在：%s", e)
        return records
    for src in srcs:
        try:
            if src.lower().endswith(".mbd"):
                items = list(chrom_core.iter_mbd(src))
            else:
                if tmp is None:
                    tmp = tempfile.mkdtemp(prefix="chrom_opc_")
                items = list(chrom_core.iter_db(src, tmp, last=last_n))
        except Exception as e:  # noqa
            get_log().warning("读取 %s 失败：%s", src, e)
            continue
        latest = {}
        for it in items:
            key = (it.source, it.app, it.stream, it.det)
            old = latest.get(key)
            if old is None or (it.cycle_id or 0) > (old.cycle_id or 0):
                latest[key] = it
        for it in latest.values():
            try:
                info = chrom_core.parse_trc(it.blob)
            except chrom_core.TraceError as e:
                get_log().warning("跳过无法解析的谱图（%s det=%s）：%s", it.source, it.det, e)
                continue
            if not info["data"]:
                continue
            t, tx, raw, uv, val = chrom_core.to_series(info)
            if len(val) > max_points:  # 等间隔抽点，控制 OPC UA 报文大小
                step = len(val) / float(max_points)
                idx = [int(k * step) for k in range(max_points)]
                t = [t[i] for i in idx]
                tx = [tx[i] for i in idx]
                raw = [raw[i] for i in idx]
                uv = [uv[i] for i in idx]
                val = [val[i] for i in idx]
            records.append(dict(
                source=it.source, analyzer=it.analyzer, app=it.app, app_name=it.app_name,
                stream=it.stream, stream_name=it.stream_name, det=it.det,
                det_name=info["det_name"] or info["trace_name"],
                cycle_id=it.cycle_id, cycle_time=str(it.cycle_time),
                is_validation=it.is_validation, cal_level=it.cal_level,
                num_points=len(val), sample_period=info["sample_period"],
                duration=t[-1] if t else 0.0, min_raw=min(raw), max_raw=max(raw),
                y_title=info["y_title"], x_title=info["x_title"] or "s",
                time=[float(v) for v in t], raw=[float(v) for v in raw],
                uv=[float(v) for v in uv], value=[float(v) for v in val],
            ))
    if tmp:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return records


def watcher_thread(snapshot_cb, stop_evt, cfg):
    """循环扫描数据源；发现新周期即通过回调推给 OPC UA 循环。"""
    last_sig = {}
    interval = max(5, int(cfg.get("poll_seconds", 30)))
    while not stop_evt.is_set():
        try:
            recs = gather_snapshot(cfg.get("watch_dirs") or [], cfg.get("last_n", 5),
                                   cfg.get("max_points", 20000))
            sig = {r["source"] + "|%s|%s|%s" % (r["app"], r["stream"], r["det"]): r["cycle_id"]
                   for r in recs}
            if sig != last_sig:
                new_cnt = sum(1 for k, v in sig.items() if last_sig.get(k) != v)
                get_log().info("扫描到 %d 个检测器通道，其中 %d 个有更新", len(sig), new_cnt)
                last_sig = sig
                snapshot_cb(recs)
        except Exception as e:  # noqa
            get_log().error("扫描出错：%s", e)
        stop_evt.wait(interval)


# ---------------------------------------------------------------------------
# OPC UA 服务
# ---------------------------------------------------------------------------

def _safe_node(s):
    import re as _re
    return _re.sub(r"[^0-9A-Za-z_.-]+", "_", str(s)).strip("_") or "x"


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
        get_log().warning("未安装 cryptography（%s），降级为不加密连接", e)
        return False

    cert_dir = os.path.join(base_dir, cfg.get("cert_dir") or "certs")
    os.makedirs(cert_dir, exist_ok=True)
    cert_p = os.path.join(cert_dir, "server_cert.der")
    key_p = os.path.join(cert_dir, "server_key.pem")
    if not (os.path.exists(cert_p) and os.path.exists(key_p)):
        get_log().info("生成自签名证书到 %s ...", cert_dir)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = [x509.NameAttribute(NameOID.COMMON_NAME, u"ChromExtractOPC")]
        san = [x509.DNSName(u"localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
               x509.UniformResourceIdentifier(cfg["endpoint"])]
        try:
            host = socket.gethostname()
            san.append(x509.DNSName(host))
            san.append(x509.IPAddress(ipaddress.IPv4Address(socket.gethostbyname(host))))
        except Exception:  # noqa
            pass
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name(name)).issuer_name(x509.Name(name))
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
                .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
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
    cfg["_cert_path"], cfg["_key_path"] = cert_p, key_p
    return True


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

    async def start(self):
        from asyncua import Server, ua
        cfg = self.cfg
        self.server = Server()
        await self.server.init()
        self.server.set_endpoint(cfg["endpoint"])
        self.server.set_server_name(cfg.get("server_name") or "ChromExtract OPC UA Server")
        self.idx = await self.server.register_namespace(cfg.get("namespace_uri") or
                                                        "urn:chromextract:opcua")

        sec = str(cfg.get("security", "none")).lower()
        if sec in ("sign", "sign_encrypt"):
            if await make_cert(cfg, cfg["_base_dir"]):
                self.server.load_certificate(cfg["_cert_path"])
                self.server.load_private_key(cfg["_key_path"])
                policies = [ua.SecurityPolicyType.NoSecurity,
                            ua.SecurityPolicyType.Sign if sec == "sign"
                            else ua.SecurityPolicyType.SignAndEncrypt]
                self.server.set_security_policy(policies)
                get_log().info("安全模式：%s（证书在 %s）",
                               sec, os.path.join(cfg["_base_dir"], cfg.get("cert_dir", "certs")))
            else:
                self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        else:
            self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

        objects = self.server.nodes.objects
        self.root = await objects.add_object(self.idx, "Chromatograph")
        # 服务器级节点
        self.n_server_info = await self.root.add_variable(
            self.idx, "ServerVersion", __version__)
        self.n_status = await self.root.add_variable(self.idx, "WatchDirs",
                                                     ",".join(cfg.get("watch_dirs") or []))
        # 远程方法：ExportCSV -> 导出最新数据为 CSV，返回输出路径
        self.n_export = await self.root.add_method(
            self.idx, "ExportCSV", self._export_csv, [],
            [ua.VariantType.String])
        await self.server.start()
        get_log().info("OPC UA 服务已启动：%s", cfg["endpoint"])

    @staticmethod
    def _export_csv(parent=None):
        """OPC UA 方法调用（在事件循环里执行）：导出当前数据为 CSV。"""
        from asyncua import ua
        cfg = OpcUaApp.current.cfg
        outdir = os.path.join(cfg["_base_dir"], cfg.get("csv_export_dir") or "谱图导出")
        n = 0
        recs = OpcUaApp.current.latest_records
        if recs:
            n = _write_records_csv(recs, outdir)
        get_log().info("远程调用 ExportCSV：%d 条 -> %s", n, outdir)
        return [ua.Variant(os.path.abspath(outdir), ua.VariantType.String)]

    async def apply_snapshot(self, records):
        from asyncua import ua
        self.latest_records = records
        self.revision += 1
        for r in records:
            try:
                await self._upsert(r)
            except Exception as e:  # noqa
                get_log().error("更新节点失败（%s）：%s", r["source"], e)
        try:
            await self.n_server_info.write_value(__version__)
        except Exception:  # noqa
            pass

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
        rows.append([r["source"], r["analyzer"], r["app"], r["app_name"], r["stream"],
                     r["stream_name"], r["det"], r["det_name"], r["cycle_id"], r["cycle_time"],
                     r["is_validation"], r["cal_level"], r["num_points"], r["sample_period"],
                     r["duration"], r["min_raw"], r["max_raw"],
                     os.path.join("csv", label + ".csv")])
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
    q = queue.Queue()

    def snapshot_cb(recs):
        loop.call_soon_threadsafe(lambda: asyncio.create_task(app.apply_snapshot(recs)))

    stop_flag = threading.Event()
    th = threading.Thread(target=watcher_thread, args=(snapshot_cb, stop_flag, cfg), daemon=True)
    th.start()
    get_log().info("数据监控已启动，监控目录：%s（每 %s 秒扫描）",
                   cfg.get("watch_dirs") or ["（空）"], cfg.get("poll_seconds", 30))

    try:
        await stop_evt.wait()
    finally:
        stop_flag.set()
        await app.server.stop()
        get_log().info("服务已停止")


# ---------------------------------------------------------------------------
# GUI 监控窗口（可选）
# ---------------------------------------------------------------------------

def run_gui(cfg):
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("色谱提取 OPC UA 服务 v%s" % __version__)
    root.geometry("760x520")

    status = tk.StringVar(value="启动中…")
    tk.Label(root, textvariable=status, anchor="w", fg="blue",
             font=("Microsoft YaHei", 11)).pack(fill="x", padx=10, pady=(10, 2))
    box = scrolledtext.ScrolledText(root, state="disabled")
    box.pack(fill="both", expand=True, padx=10, pady=6)

    def pump_log():
        try:
            while True:
                rec = log_q.get_nowait()
                box.configure(state="normal")
                box.insert("end", rec + "\n")
                box.see("end")
                box.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(300, pump_log)

    class QHandler(logging.Handler):
        def emit(self, record):
            log_q.put(self.format(record))

    setup_logging(cfg, cfg["_base_dir"])
    logging.getLogger().addHandler(QHandler())

    th_stop = asyncio.Event()

    def server_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_server(cfg, th_stop))
        except Exception as e:  # noqa
            status.set("服务出错：" + str(e))
            get_log().error("服务线程退出：%s", e)

    threading.Thread(target=server_worker, daemon=True).start()
    status.set("运行中：%s" % cfg["endpoint"])
    pump_log()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    th_stop.set()


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


def main(argv=None):
    ap = argparse.ArgumentParser(description="色谱提取 OPC UA 服务")
    ap.add_argument("--config", default=None, help="配置文件（默认：程序目录 config.json）")
    ap.add_argument("--gui", action="store_true", help="打开图形监控窗口")
    ap.add_argument("--extract", nargs="+", metavar="源",
                    help="命令行导出 CSV（源可以是 *_V3.db / *.mbd / 文件夹）")
    ap.add_argument("-o", "--out", default="谱图导出", help="导出文件夹")
    ap.add_argument("--last", type=int, default=None, help="每个流路只导出最近 N 个周期")
    ap.add_argument("--list", nargs="+", metavar="源", help="只查看数据源里有什么")
    args = ap.parse_args(argv)

    base_dir = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, "frozen", False) \
        else os.path.dirname(sys.executable)
    cfg_path = args.config or os.path.join(base_dir, "config.json")
    cfg = load_config(cfg_path)
    cfg["_base_dir"] = base_dir

    if args.list:
        chrom_core.list_sources(args.list)
        return 0
    if args.extract:
        setup_logging(cfg, base_dir)
        chrom_core.extract(args.extract, args.out, last=args.last)
        return 0

    setup_logging(cfg, base_dir)
    get_log().info("色谱提取 OPC UA 服务 v%s 启动，配置：%s", __version__, cfg_path)
    if args.gui:
        run_gui(cfg)
        return 0

    async def _run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        import signal
        for sig in ("SIGINT", "SIGTERM"):
            if hasattr(signal, sig):
                try:
                    loop.add_signal_handler(getattr(signal, sig), stop.set)
                except NotImplementedError:
                    pass
        await run_server(cfg, stop)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
