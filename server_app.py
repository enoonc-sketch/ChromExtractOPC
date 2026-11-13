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
import collections
import getpass
import json
import logging
import logging.handlers
import os
import queue
import sys
import tempfile
import threading
import time
from datetime import datetime

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
}


APP_URI = "urn:chromextract:opcua:server"   # 服务端应用 URI，必须与证书里一致

log_q = queue.Queue()


def get_log():
    return logging.getLogger("chrom")


class _SafeFormatter(logging.Formatter):
    """日志里可能带有来自数据文件的文本，换行一律压成空格，防止伪造日志行。"""

    def format(self, record):
        return super().format(record).replace("\r", " ").replace("\n", " ")


def setup_logging(cfg, base_dir):
    fmt = _SafeFormatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [sh]
    if cfg.get("log_file"):
        lf = os.path.join(base_dir, cfg["log_file"])
        os.makedirs(os.path.dirname(lf), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            lf, maxBytes=int(cfg.get("log_max_mb", 5)) * 1024 * 1024,
            backupCount=int(cfg.get("log_backups", 5)), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


# ---------------------------------------------------------------------------
# 数据快照：在后台线程扫描数据源，解析出最新谱图
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
            "watch_dirs", "last_n", "max_points", "poll_seconds")
RESTART_KEYS = ("endpoint", "security", "allow_insecure_fallback", "cert_dir",
                "trusted_client_certs_dir", "max_connections", "namespace_uri", "server_name")


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


def watcher_thread(snapshot_cb, stop_evt, cfg, beat_cb=None, reload_cb=None):
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
            if sig != last_sig:
                new_cnt = sum(1 for k, v in sig.items() if (last_sig or {}).get(k) != v)
                get_log().info("扫描到 %d 个检测器通道，其中 %d 个有更新", len(sig), new_cnt)
                last_sig = sig
                snapshot_cb(recs)
        except Exception as e:  # noqa
            get_log().error("扫描出错：%s", e)
        if beat_cb is not None:
            beat_cb()          # 心跳：即使没有新数据，客户端也能确认服务还活着
        stop_evt.wait(interval)


# ---------------------------------------------------------------------------
# OPC UA 服务
# ---------------------------------------------------------------------------

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

    async def start(self):
        from asyncua import Server, ua
        cfg = self.cfg
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
        # 远程方法：ExportCSV -> 导出最新数据为 CSV，返回输出路径
        self.n_export = await self.root.add_method(
            self.idx, "ExportCSV", self._export_csv, [],
            [ua.VariantType.String])
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

    async def beat(self):
        self._beats += 1
        try:
            await self.n_beat.write_value(self._beats)
            await self.n_scan.write_value(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa
            pass

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
            for r in records:
                seen.add((r["analyzer"], r["app"], r["stream"], r["det"]))
                try:
                    await self._upsert(r)
                except Exception as e:  # noqa
                    get_log().error("更新节点失败（%s）：%s", r["source"], e)
            # 数据源丢失/读不了时，旧数据仍留在节点里；必须明确标记为"不可信"，不能让 DCS 把陈旧值当真
            for key, nodes in self.devices.items():
                if key not in seen:
                    try:
                        await nodes["good"].write_value(False)
                    except Exception:  # noqa
                        pass
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
    th = threading.Thread(target=watcher_thread,
                          args=(snapshot_cb, stop_flag, cfg, beat_cb, app.user_manager.reload),
                          daemon=True)
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


def run_gui(cfg):
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("色谱提取 OPC UA 服务 v%s" % __version__)
    root.geometry("760x520")

    status = tk.StringVar(value="启动中…")
    tk.Label(root, textvariable=status, anchor="w", fg="blue",
             font=("Microsoft YaHei", 11)).pack(fill="x", padx=10, pady=(10, 2))
    sec_var = tk.StringVar(value=security_summary(cfg))
    bar = tk.Frame(root)
    bar.pack(fill="x", padx=10)
    tk.Label(bar, textvariable=sec_var, anchor="w").pack(side="left")
    tk.Button(bar, text="安全设置…",
              command=lambda: SecurityWindow(root, cfg, _TkDialogs(root))).pack(side="right")
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

    def refresh_sec():
        sec_var.set(security_summary(cfg))       # 配置热加载后摘要随之更新
        root.after(5000, refresh_sec)

    class QHandler(logging.Handler):
        def emit(self, record):
            log_q.put(self.format(record))

    setup_logging(cfg, cfg["_base_dir"])
    logging.getLogger().addHandler(QHandler())

    holder = {}   # loop / stop 事件都在服务线程的事件循环里创建，关闭时线程安全地置位
    worker_done = threading.Event()

    def server_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _main():
            holder["loop"] = loop
            holder["stop"] = asyncio.Event()
            await run_server(cfg, holder["stop"])

        try:
            loop.run_until_complete(_main())
        except Exception as e:  # noqa
            status.set("服务出错：" + str(e))
            get_log().error("服务线程退出：%s", e)
        finally:
            worker_done.set()

    threading.Thread(target=server_worker, daemon=True).start()
    status.set("运行中：%s" % cfg["endpoint"])
    pump_log()
    refresh_sec()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    lp, ev = holder.get("loop"), holder.get("stop")
    if lp is not None and ev is not None:
        try:
            lp.call_soon_threadsafe(ev.set)
        except RuntimeError:
            pass
    worker_done.wait(timeout=5)


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
    for key, lo, hi in (("poll_seconds", 5, 3600), ("last_n", 1, 100),
                        ("max_points", 100, 200000), ("max_connections", 1, 200),
                        ("export_min_interval_sec", 0, 3600), ("log_max_mb", 1, 100),
                        ("log_backups", 0, 20), ("max_trace_points", 1000, 5000000),
                        ("max_file_mb", 1, 2048)):
        _clamp_int(cfg, key, lo, hi)
    dirs = cfg.get("watch_dirs") or []
    if isinstance(dirs, str):
        dirs = [dirs]
    # 相对路径按"程序所在目录"解析，而不是启动时的当前目录
    # （从快捷键/计划任务/服务启动时，当前目录常常是 C:\Windows\System32，会找不到数据）
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
    chrom_core.set_limits(max_points=cfg["max_trace_points"], max_file_mb=cfg["max_file_mb"])
    return cfg


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
    if cfg.get("remote_export", True):
        out.append(("注意", "允许远程调用 ExportCSV（已限流 %s 秒/次）。不需要可设 remote_export=false。"
                    % cfg.get("export_min_interval_sec")))
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


def main(argv=None):
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(description="色谱提取 OPC UA 服务")
    ap.add_argument("--config", default=None, help="配置文件（默认：程序目录 config.json）")
    ap.add_argument("--gui", action="store_true", help="打开图形监控窗口")
    ap.add_argument("--extract", nargs="+", metavar="源",
                    help="命令行导出 CSV（源可以是 *_V3.db / *.mbd / 文件夹）")
    ap.add_argument("-o", "--out", default="谱图导出", help="导出文件夹")
    ap.add_argument("--last", type=int, default=None, help="每个流路只导出最近 N 个周期")
    ap.add_argument("--list", nargs="+", metavar="源", help="只查看数据源里有什么")
    ap.add_argument("--hash-password", action="store_true",
                    help="生成登录密码的哈希（粘贴到 config.json 的 users 里）")
    ap.add_argument("--check-security", action="store_true", help="安全体检：检查当前配置的安全状况")
    ap.add_argument("--admin", action="store_true", help="安全管理菜单（体检/一键加固/账号管理）")
    ap.add_argument("--harden", action="store_true", help="一键加固：开启加密、关闭匿名、创建登录账号")
    ap.add_argument("--add-user", metavar="用户名", help="添加账号或重设密码（会提示输入密码）")
    ap.add_argument("--remove-user", metavar="用户名", help="删除账号")
    ap.add_argument("--list-users", action="store_true", help="列出账号")
    args = ap.parse_args(argv)

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
        print("配置有误（%s）：\n  %s" % (cfg_path, e))
        return 2

    if args.check_security:
        print_security_report(cfg)
        return 0

    if args.list:
        chrom_core.list_sources(args.list)
        return 0
    if args.extract:
        setup_logging(cfg, base_dir)
        chrom_core.extract(args.extract, args.out, last=args.last)
        return 0

    setup_logging(cfg, base_dir)
    get_log().info("色谱提取 OPC UA 服务 v%s 启动，配置：%s", __version__, cfg_path)
    for lv, msg in security_report(cfg):
        if lv != "正常":
            get_log().warning("安全提示[%s]：%s", lv, msg)
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
    except OSError as e:
        # 最常见：端口已被占用（上一个实例没关，或别的程序在用 4840）
        get_log().error("服务无法启动：%s。请检查 %s 的端口是否被占用（可在 config.json 改 endpoint 端口）。",
                        e, cfg["endpoint"])
        return 3
    except RuntimeError as e:
        get_log().error("服务无法启动：%s", e)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
