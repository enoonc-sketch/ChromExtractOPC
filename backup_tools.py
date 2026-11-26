#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backup_tools.py —— 配置备份与还原（v3.1 新增；不依赖 tkinter，可单独测试）

备份包是一个 .zip：
    manifest.json        版本、导出时间、主机名、本机标识（哈希）、文件清单与 SHA-256
    config.json          配置文件（账号的密码哈希就在里面）
    certs/...            cert_dir 下的 OPC UA 服务端证书/私钥、网页证书/私钥（含 trusted 子目录）
    trusted/...          trusted_client_certs_dir 不在 cert_dir 里面时单独放这里
    logs/...             可选：日志目录

加密（可选，强烈建议：备份里有私钥）：
    外层 zip 只有 manifest.json（不含文件清单）+ payload.bin；
    payload.bin = AES-256-GCM(密钥 = PBKDF2-HMAC-SHA256(密码, 随机盐, 30 万次), 随机 nonce, 内层 zip)，
    附加认证数据（AAD）是外层 manifest 的规范化 JSON —— 改动外层 manifest 或密文都会解密失败。

还原：
    1. 校验：zip 结构、路径（拒绝绝对路径、盘符、..、反斜杠、未知目录）、条目数/大小上限、
       manifest 与实际文件一一对应、每个文件 SHA-256、版本兼容；
    2. 预览：列出将覆盖 / 新增 / 删除（信任列表以备份为准）的文件；
    3. 自动备份当前配置到 backups/（只保留最近 N 个自动备份）；
    4. 先用 normalize_config 校验备份里的配置，不合法直接拒绝（一个文件都不动）；
    5. 写入文件后再按磁盘上的结果校验一次；任何一步失败都回滚到还原前的状态；
    6. smtp_password 是 Windows DPAPI 本机加密的：还原到别的电脑时自动清空，并提示重新填写。
"""
import base64
import hashlib
import io
import json
import os
import shutil
import socket
import tempfile
import zipfile
from datetime import datetime

from i18n import M

FORMAT = 1
APP_NAME = "ChromExtractOPC"
MANIFEST = "manifest.json"
PAYLOAD = "payload.bin"
SECTIONS = ("config.json", "certs/", "trusted/", "logs/")
MAX_ENTRIES = 5000
MAX_FILE_BYTES = 200 * 1024 * 1024          # 单个文件解压后上限
MAX_TOTAL_BYTES = 512 * 1024 * 1024         # 全部解压后上限
MAX_ARCHIVE_BYTES = 600 * 1024 * 1024       # 备份包文件本身上限
MAX_RATIO = 200                             # 压缩比上限（防 zip 炸弹）
LIMITS = {"entries": MAX_ENTRIES, "file": MAX_FILE_BYTES, "total": MAX_TOTAL_BYTES}   # 可调（测试用）
KDF_ITERATIONS = 300_000
AUTO_PREFIX = "自动备份_"
DEFAULT_KEEP = 10
AAD_PREFIX = b"ChromExtractOPC-backup-v1|"


class BackupError(Exception):
    """给人看的错误信息（直接显示即可）。"""


class PasswordRequired(BackupError):
    """备份包已加密，需要密码。"""


class WrongPassword(BackupError):
    """密码错误或密文被改动。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def app_version():
    try:
        import chrom_core
        return str(chrom_core.__version__ or "")
    except Exception:  # noqa
        return ""


def parse_version(v):
    out = []
    for part in str(v or "0").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def machine_identity(reader=None):
    """本机标识：Windows 用注册表 MachineGuid（只保存它的 SHA-256，不泄露原值），
    其它系统用 /etc/machine-id，都没有时用主机名。reader 可注入（测试用）。"""
    raw = ""
    try:
        if reader is not None:
            raw = reader() or ""
        elif os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
                raw = winreg.QueryValueEx(k, "MachineGuid")[0]
        else:
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if os.path.isfile(p):
                    with open(p, "r") as f:
                        raw = f.read().strip()
                    break
    except Exception:  # noqa
        raw = ""
    if not raw:
        raw = "host:" + socket.gethostname().lower()
    return hashlib.sha256(("ChromExtractOPC|" + str(raw)).encode("utf-8")).hexdigest()[:32]


def _resolve(base_dir, p, default):
    p = str(p or default)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(base_dir, p))


def _inside(child, parent):
    child, parent = os.path.normcase(os.path.abspath(child)), os.path.normcase(os.path.abspath(parent))
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def layout(cfg_raw, base_dir, cfg_path):
    """备份涉及的本机位置：{"config": 文件, "certs": 目录, "trusted": 目录或 None, "logs": 目录}。
    trusted 在 certs 里面时为 None（已包含在 certs/ 里）。"""
    cfg_raw = cfg_raw or {}
    certs = _resolve(base_dir, cfg_raw.get("cert_dir"), "certs")
    trusted = _resolve(base_dir, cfg_raw.get("trusted_client_certs_dir"), "certs/trusted")
    lf = _resolve(base_dir, cfg_raw.get("log_file"), "logs/server.log")
    return {"config": cfg_path, "certs": certs,
            "trusted": None if _inside(trusted, certs) else trusted,
            "trusted_real": trusted,
            "logs": os.path.dirname(lf)}


def _read_cfg(cfg_path):
    if not os.path.exists(cfg_path):
        return {}
    with open(cfg_path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise BackupError(M("配置文件不是 JSON 对象：%s", cfg_path))
    return data


def _walk_files(root):
    out = []
    if not root or not os.path.isdir(root):
        return out
    for dp, dns, fns in os.walk(root):
        dns.sort()
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out.append((rel, full))
    return out


def is_private_key(name, data=b""):
    n = name.lower()
    if n.endswith((".key",)) or n.endswith("key.pem") or "private" in n:
        return True
    return b"PRIVATE KEY" in (data[:4096] if data else b"")


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

def plan_export(base_dir, cfg_path, include_logs=False):
    """列出将要备份的文件：[(包内路径, 本机路径)] 和提示信息。不写任何文件。"""
    raw = _read_cfg(cfg_path)
    lay = layout(raw, base_dir, cfg_path)
    items = []
    if os.path.isfile(cfg_path):
        items.append(("config.json", cfg_path))
    for rel, full in _walk_files(lay["certs"]):
        items.append(("certs/" + rel, full))
    if lay["trusted"]:
        for rel, full in _walk_files(lay["trusted"]):
            items.append(("trusted/" + rel, full))
    if include_logs:
        backups_dir = os.path.join(base_dir, "backups")
        for rel, full in _walk_files(lay["logs"]):
            if _inside(full, backups_dir):
                continue
            items.append(("logs/" + rel, full))
    warnings = []
    keys = [a for a, full in items if a.startswith(("certs/", "trusted/")) and is_private_key(a, _peek(full))]
    if str(raw.get("smtp_password") or "").startswith("dpapi:"):
        warnings.append("SMTP 密码是用本机 Windows DPAPI 加密的：还原到别的电脑后无法解密，会被清空，需要重新填写。")
    for k in ("web_cert_file", "web_key_file"):
        v = str(raw.get(k) or "")
        if v and not _inside(_resolve(base_dir, v, ""), lay["certs"]):
            warnings.append(M("%s（%s）不在证书目录里，没有包含在备份中，换电脑时请另行复制。", k, v))
    return {"items": items, "private_keys": keys, "warnings": warnings, "layout": lay}


def _peek(path):
    try:
        with open(path, "rb") as f:
            return f.read(4096)
    except OSError:
        return b""


def _derive_key(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)


def _aad(outer_manifest):
    return AAD_PREFIX + json.dumps(outer_manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _build_zip(entries, manifest):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        for arc, data in entries:
            z.writestr(arc, data)
    return buf.getvalue()


def export_backup(out_path, base_dir, cfg_path, password=None, include_logs=False,
                  now=None, hostname=None, machine=None, note=""):
    """导出备份包。password 非空 = 加密。返回 {"path","files","encrypted","private_keys","warnings","bytes"}。"""
    plan = plan_export(base_dir, cfg_path, include_logs)
    if not plan["items"]:
        raise BackupError("没有找到可备份的文件（配置文件不存在）。")
    entries, files, total = [], [], 0
    for arc, full in plan["items"]:
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError as e:
            if arc.startswith("logs/"):
                continue          # 正在写的日志读不了就跳过
            raise BackupError(M("无法读取 %s：%s", full, e))
        total += len(data)
        if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise BackupError(M("备份内容太大（%s）：请不要包含日志，或先清理日志目录。", arc))
        entries.append((arc, data))
        files.append({"path": arc, "size": len(data), "sha256": sha256_bytes(data)})
    created = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    host = hostname if hostname is not None else socket.gethostname()
    mid = machine if machine is not None else machine_identity()
    manifest = {"app": APP_NAME, "format": FORMAT, "app_version": app_version(), "created": created,
                "hostname": host, "machine_id": mid, "encrypted": False,
                "include_logs": bool(include_logs), "note": str(note or "")[:200],
                "smtp_dpapi": bool(plan["warnings"] and any("DPAPI" in w for w in plan["warnings"])),
                "files": files}
    inner = _build_zip(entries, manifest)
    if password:
        import secrets
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
        outer = {"app": APP_NAME, "format": FORMAT, "app_version": manifest["app_version"],
                 "created": created, "hostname": host, "machine_id": mid, "encrypted": True,
                 "include_logs": bool(include_logs),
                 "kdf": {"name": "pbkdf2_sha256", "iterations": KDF_ITERATIONS,
                         "salt": base64.b64encode(salt).decode("ascii")},
                 "cipher": {"name": "aes-256-gcm", "nonce": base64.b64encode(nonce).decode("ascii")}}
        ct = AESGCM(_derive_key(password, salt, KDF_ITERATIONS)).encrypt(nonce, inner, _aad(outer))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr(MANIFEST, json.dumps(outer, ensure_ascii=False, indent=2))
            z.writestr(PAYLOAD, ct)
        blob = buf.getvalue()
    else:
        blob = inner
    d = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(d, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, out_path)
    return {"path": out_path, "files": len(files), "encrypted": bool(password),
            "private_keys": plan["private_keys"], "warnings": plan["warnings"], "bytes": len(blob)}


def default_backup_name(now=None, prefix="配置备份_"):
    return "%s%s_%s.zip" % (prefix, socket.gethostname()[:20] or "pc", (now or datetime.now()).strftime("%Y%m%d_%H%M%S"))


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def check_member_name(name):
    """包内路径安全检查：返回 None 表示合法，否则返回原因。"""
    if not name or "\x00" in name:
        return "空文件名"
    if "\\" in name:
        return "包含反斜杠"
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return "绝对路径"
    parts = name.split("/")
    if any(p in ("..", ".") for p in parts):
        return "包含 .. 或 ."
    if name.endswith("/"):
        return None                           # 目录条目：允许但忽略
    if name in (MANIFEST, PAYLOAD, "config.json"):
        return None
    if not name.startswith(("certs/", "trusted/", "logs/")) or len(parts) < 2 or not parts[-1]:
        return "不在允许的目录（config.json / certs/ / trusted/ / logs/）里"
    return None


def _open_zip(path_or_bytes):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        if len(path_or_bytes) > MAX_ARCHIVE_BYTES:
            raise BackupError("备份包太大。")
        src = io.BytesIO(path_or_bytes)
    else:
        if not os.path.isfile(path_or_bytes):
            raise BackupError(M("找不到备份文件：%s", path_or_bytes))
        if os.path.getsize(path_or_bytes) > MAX_ARCHIVE_BYTES:
            raise BackupError(M("备份包太大（超过 %d MB）。", MAX_ARCHIVE_BYTES // 1048576))
        src = path_or_bytes
    try:
        return zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as e:
        raise BackupError(M("不是有效的备份包（zip 无法打开）：%s", e))


def _safe_read_all(z):
    """校验所有条目名与大小后读出：{名称: bytes}。"""
    infos = z.infolist()
    if len(infos) > LIMITS["entries"]:
        raise BackupError(M("备份包里的文件太多（%d 个）。", len(infos)))
    total = 0
    out = {}
    for info in infos:
        why = check_member_name(info.filename)
        if why:
            raise BackupError(M("备份包里有不安全的路径（%s）：%r，已拒绝还原。", why, info.filename[:120]))
        if info.filename.endswith("/"):
            continue
        if info.filename in out:
            raise BackupError(M("备份包里有重复的文件：%s", info.filename))
        if info.file_size > LIMITS["file"]:
            raise BackupError(M("备份包里的文件太大：%s", info.filename))
        total += info.file_size
        if total > LIMITS["total"]:
            raise BackupError("备份包解压后太大，已拒绝（可能是损坏或恶意文件）。")
        if info.compress_size and info.file_size / float(info.compress_size) > MAX_RATIO and info.file_size > 1048576:
            raise BackupError(M("备份包压缩比异常（%s），已拒绝。", info.filename))
        with z.open(info) as f:
            data = f.read(LIMITS["file"] + 1)
        if len(data) > LIMITS["file"] or len(data) != info.file_size:
            raise BackupError(M("备份包里的文件大小与记录不符：%s", info.filename))
        out[info.filename] = data
    return out


def _parse_manifest(data):
    try:
        m = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("manifest.json 不是有效的 JSON。")
    if not isinstance(m, dict) or m.get("app") != APP_NAME:
        raise BackupError("这不是本程序导出的备份包（manifest 不对）。")
    try:
        fmt = int(m.get("format"))
    except (TypeError, ValueError):
        raise BackupError("备份包格式版本不对。")
    if fmt > FORMAT:
        raise BackupError(M("备份包格式版本 %d 比本程序支持的 %d 新：请先升级程序再还原。", fmt, FORMAT))
    cur = app_version()
    if cur and parse_version(m.get("app_version")) > parse_version(cur):
        raise BackupError(M("备份来自更新的程序版本 %s（当前 %s）：请先把程序升级到 %s 或以上再还原。", m.get("app_version"), cur, m.get("app_version")))
    if parse_version(m.get("app_version")) < (2, 0, 0):
        raise BackupError(M("备份来自过旧的版本 %r，无法还原。", m.get("app_version")))
    return m


def read_manifest(path_or_bytes):
    """只读外层 manifest（不需要密码）：用于显示备份信息、判断是否加密。"""
    with _open_zip(path_or_bytes) as z:
        try:
            info = z.getinfo(MANIFEST)
        except KeyError:
            raise BackupError("备份包里没有 manifest.json。")
        if info.file_size > 4 * 1024 * 1024:
            raise BackupError("manifest.json 太大。")
        return _parse_manifest(z.read(MANIFEST))


def _decrypt(outer, payload, password):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        kdf, ciph = outer["kdf"], outer["cipher"]
        if kdf.get("name") != "pbkdf2_sha256" or ciph.get("name") != "aes-256-gcm":
            raise BackupError("不支持的加密方式。")
        it = int(kdf["iterations"])
        if not 100_000 <= it <= 10_000_000:
            raise BackupError("加密参数异常。")
        salt, nonce = base64.b64decode(kdf["salt"]), base64.b64decode(ciph["nonce"])
    except (KeyError, TypeError, ValueError):
        raise BackupError("加密参数缺失或损坏。")
    try:
        return AESGCM(_derive_key(password, salt, it)).decrypt(nonce, payload, _aad(outer))
    except InvalidTag:
        raise WrongPassword("密码错误，或备份包已被改动（解密校验失败）。")


def load_backup(path_or_bytes, password=None):
    """完整校验并读出备份：返回 (manifest, {包内路径: bytes})。
    加密包没给密码抛 PasswordRequired；密码错抛 WrongPassword；其它问题抛 BackupError。"""
    with _open_zip(path_or_bytes) as z:
        data = _safe_read_all(z)
    if MANIFEST not in data:
        raise BackupError("备份包里没有 manifest.json。")
    m = _parse_manifest(data[MANIFEST])
    if m.get("encrypted"):
        if set(data) != {MANIFEST, PAYLOAD}:
            raise BackupError("加密备份包的结构不对（多了或少了文件）。")
        if not password:
            raise PasswordRequired("这个备份包已加密，请输入导出时设置的密码。")
        inner = _decrypt(m, data[PAYLOAD], password)
        with _open_zip(inner) as z:
            data = _safe_read_all(z)
        if MANIFEST not in data:
            raise BackupError("备份包内层没有 manifest.json。")
        inner_m = _parse_manifest(data[MANIFEST])
        for k in ("created", "hostname", "machine_id", "app_version"):
            if inner_m.get(k) != m.get(k):
                raise BackupError(M("备份包内外层信息不一致（%s），已拒绝。", k))
        m = inner_m
    if PAYLOAD in data:
        raise BackupError("备份包结构不对。")
    files = m.get("files")
    if not isinstance(files, list):
        raise BackupError("manifest 里没有文件清单。")
    listed = {}
    for it in files:
        if not isinstance(it, dict) or not isinstance(it.get("path"), str):
            raise BackupError("manifest 文件清单格式不对。")
        why = check_member_name(it["path"])
        if why or it["path"] in (MANIFEST, PAYLOAD):
            raise BackupError(M("manifest 里有不安全的路径（%s）：%r", why or "保留名", it["path"][:120]))
        listed[it["path"]] = it
    actual = {k: v for k, v in data.items() if k != MANIFEST}
    extra = sorted(set(actual) - set(listed))
    lost = sorted(set(listed) - set(actual))
    if extra:
        raise BackupError(M("备份包里有清单之外的文件：%s", "、".join(extra[:5])))
    if lost:
        raise BackupError(M("备份包缺少文件：%s", "、".join(lost[:5])))
    for name, blob in actual.items():
        want = str(listed[name].get("sha256") or "").lower()
        if sha256_bytes(blob) != want:
            raise BackupError(M("文件校验失败（SHA-256 不一致，可能被改动或损坏）：%s", name))
    if "config.json" not in actual:
        raise BackupError("备份包里没有 config.json。")
    try:
        cfg = json.loads(actual["config.json"].decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        raise BackupError("备份里的 config.json 不是有效的 JSON。")
    if not isinstance(cfg, dict):
        raise BackupError("备份里的 config.json 不是 JSON 对象。")
    return m, actual


# ---------------------------------------------------------------------------
# 还原
# ---------------------------------------------------------------------------

def _targets(files, cfg_new, base_dir, cfg_path, include_logs, now):
    """包内路径 → 本机目标路径。日志还原到 logs/restored_时间/，不覆盖正在写的日志。"""
    lay = layout(cfg_new, base_dir, cfg_path)
    out = {}
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    for name in files:
        if name == "config.json":
            out[name] = cfg_path
        elif name.startswith("certs/"):
            out[name] = os.path.join(lay["certs"], *name[6:].split("/"))
        elif name.startswith("trusted/"):
            out[name] = os.path.join(lay["trusted_real"], *name[8:].split("/"))
        elif name.startswith("logs/") and include_logs:
            out[name] = os.path.join(lay["logs"], "restored_" + stamp, *name[5:].split("/"))
    for name, dst in out.items():
        if not _inside(dst, base_dir) and name != "config.json":
            # 证书目录配置成程序目录外的绝对路径也允许，但必须在对应的目录内
            root = lay["certs"] if name.startswith("certs/") else lay["trusted_real"] if name.startswith("trusted/") else lay["logs"]
            if not _inside(dst, root):
                raise BackupError(M("还原目标路径异常：%s", dst))
    return out, lay


def _trusted_removals(files, lay):
    """备份里有信任列表时，以备份为准：本机多出来的客户端证书会被删除（预览里列出）。"""
    tdir = lay["trusted_real"]
    if lay["trusted"]:
        prefix, inbak = "trusted/", [n for n in files if n.startswith("trusted/")]
        names = {n[len(prefix):] for n in inbak}
    else:
        rel = os.path.relpath(tdir, lay["certs"]).replace(os.sep, "/")
        prefix = "certs/" + rel + "/"
        inbak = [n for n in files if n.startswith(prefix)]
        names = {n[len(prefix):] for n in inbak}
    if not inbak or not os.path.isdir(tdir):
        return []          # 备份里没有信任证书：不动本机的白名单（避免误删导致白名单失效）
    rem = []
    for rel, full in _walk_files(tdir):
        if rel not in names and "/" not in rel:
            rem.append(full)
    return rem


def same_machine(manifest, machine=None):
    mid = machine if machine is not None else machine_identity()
    return bool(manifest.get("machine_id")) and manifest.get("machine_id") == mid


def inspect_backup(path, base_dir, cfg_path, password=None, include_logs=False, machine=None, now=None):
    """完整校验并生成预览（不写任何文件）。返回 dict：
    manifest、actions [(动作, 包内路径, 本机路径)]、warnings、cross_machine、smtp_cleared。"""
    m, files = load_backup(path, password)
    cfg_new = json.loads(files["config.json"].decode("utf-8-sig"))
    targets, lay = _targets(files, cfg_new, base_dir, cfg_path, include_logs, now)
    actions = []
    for name in sorted(targets):
        dst = targets[name]
        actions.append(("覆盖" if os.path.exists(dst) else "新增", name, dst))
    for full in _trusted_removals(files, lay):
        actions.append(("删除", "", full))
    cross = not same_machine(m, machine)
    warnings = []
    smtp = str(cfg_new.get("smtp_password") or "")
    smtp_clear = cross and smtp.startswith("dpapi:")
    if smtp_clear:
        warnings.append(M("备份来自另一台电脑（%s）：SMTP 密码是那台电脑加密的，这里无法解密，还原后会清空，"
                          "请在“监控 › 告警 → 通知设置”里重新填写。", m.get("hostname") or "?"))
    elif cross:
        warnings.append(M("备份来自另一台电脑（%s）。", m.get("hostname") or "?"))
    if any(a[1].startswith(("certs/", "trusted/")) and is_private_key(a[1], files.get(a[1], b"")) for a in actions):
        warnings.append("将还原证书私钥：还原后 OPC UA 客户端看到的服务端证书会变成备份里的那一个。")
    if m.get("include_logs") and not include_logs:
        warnings.append("备份里含日志，本次不还原日志。")
    lv = parse_version(m.get("app_version"))
    if lv < parse_version(app_version() or "0"):
        warnings.append(M("备份来自旧版本 %s，缺少的新配置项会使用默认值。", m.get("app_version")))
    return {"manifest": m, "actions": actions, "warnings": warnings, "cross_machine": cross,
            "smtp_cleared": smtp_clear, "files": files, "targets": targets, "config": cfg_new}


def list_auto_backups(base_dir):
    d = os.path.join(base_dir, "backups")
    if not os.path.isdir(d):
        return []
    names = sorted(n for n in os.listdir(d) if n.startswith(AUTO_PREFIX) and n.endswith(".zip"))
    return [os.path.join(d, n) for n in names]


def prune_auto_backups(base_dir, keep=DEFAULT_KEEP):
    """只保留最近 keep 个自动备份，返回删除的文件。"""
    files = list_auto_backups(base_dir)
    removed = []
    for p in files[:max(0, len(files) - max(1, int(keep)))]:
        try:
            os.remove(p)
            removed.append(p)
        except OSError:
            pass
    return removed


def auto_backup(base_dir, cfg_path, keep=DEFAULT_KEEP, now=None, reason="还原前"):
    """把当前配置另存为一次自动备份（不加密：只存在本机程序目录，证书本来就以明文存放在本机）。"""
    t = now or datetime.now()
    name = "%s%s_%s.zip" % (AUTO_PREFIX, t.strftime("%Y%m%d_%H%M%S_%f")[:-3], reason)
    path = os.path.join(base_dir, "backups", name)
    if not os.path.exists(cfg_path):
        return None
    export_backup(path, base_dir, cfg_path, password=None, include_logs=False, now=t, note=reason)
    prune_auto_backups(base_dir, keep)
    return path


def _validate_cfg(validate, cfg):
    if validate is None:
        return
    try:
        validate(dict(cfg))
    except BackupError:
        raise
    except Exception as e:  # noqa
        raise BackupError(M("备份里的配置校验不通过：%s", e))


def restore_backup(path, base_dir, cfg_path, password=None, include_logs=False, validate=None,
                   keep=DEFAULT_KEEP, machine=None, now=None, _after_write=None):
    """还原备份。validate(cfg_dict) 抛异常 = 配置不合法（通常传 server_app 的 normalize 校验）。
    返回 {"auto_backup","written","removed","warnings","smtp_cleared","restart_needed"}。
    任何一步失败抛 BackupError，且磁盘上的文件恢复到还原前的状态。"""
    info = inspect_backup(path, base_dir, cfg_path, password, include_logs, machine, now)
    cfg_new = dict(info["config"])
    if info["smtp_cleared"]:
        cfg_new["smtp_password"] = ""
    _validate_cfg(validate, cfg_new)                      # 先校验，不合法一个文件都不动
    files, targets = info["files"], info["targets"]
    payload = {}
    for name, dst in targets.items():
        if name == "config.json":
            payload[dst] = (json.dumps(cfg_new, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        else:
            payload[dst] = files[name]
    removals = [a[2] for a in info["actions"] if a[0] == "删除"]
    ab = auto_backup(base_dir, cfg_path, keep=keep, now=now)
    # 回滚快照：记录每个目标的原始内容（不存在记 None）
    snap_dir = tempfile.mkdtemp(prefix="restore_", dir=_ensure(os.path.join(base_dir, "backups")))
    snapshot = []
    try:
        for i, dst in enumerate(list(payload) + removals):
            if os.path.isfile(dst):
                keep_p = os.path.join(snap_dir, "%05d" % i)
                shutil.copy2(dst, keep_p)
                snapshot.append((dst, keep_p))
            else:
                snapshot.append((dst, None))
        written = []
        try:
            for dst, data in payload.items():
                _ensure(os.path.dirname(dst))
                tmp = dst + ".restore_tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dst)
                written.append(dst)
                if _after_write is not None:
                    _after_write(dst)
            for p in removals:
                os.remove(p)
            # 写完再按磁盘上的配置校验一次（与服务启动时读取的完全一样）
            with open(cfg_path, "r", encoding="utf-8-sig") as f:
                disk = json.load(f)
            _validate_cfg(validate, disk)
        except Exception as e:  # noqa
            _rollback(snapshot)
            if isinstance(e, BackupError):
                raise BackupError(M("%s（已回滚，配置保持还原前的状态）", e))
            raise BackupError(M("还原失败，已回滚到还原前的状态：%s", e))
    finally:
        shutil.rmtree(snap_dir, ignore_errors=True)
    return {"auto_backup": ab, "written": written, "removed": removals, "warnings": info["warnings"],
            "smtp_cleared": info["smtp_cleared"], "restart_needed": True, "manifest": info["manifest"]}


def _ensure(d):
    os.makedirs(d, exist_ok=True)
    return d


def _rollback(snapshot):
    for dst, keep_p in reversed(snapshot):
        try:
            if keep_p is None:
                if os.path.exists(dst):
                    os.remove(dst)
                for t in (dst + ".restore_tmp",):
                    if os.path.exists(t):
                        os.remove(t)
            else:
                _ensure(os.path.dirname(dst))
                shutil.copy2(keep_p, dst)
        except OSError:
            pass


def summary_text(info, tr=None):
    """预览的文字版（窗口和命令行共用）。tr 为翻译函数（可选，传 i18n.tr）。"""
    from i18n import tr_msg
    t = tr or (lambda s: s)
    m = info["manifest"]
    lines = [t("备份时间：%s　来源电脑：%s　程序版本：%s") % (m.get("created"), m.get("hostname"), m.get("app_version")),
             t("将要执行的操作（%d 项）：") % len(info["actions"])]
    for act, name, dst in info["actions"]:
        lines.append("  [%s] %s" % (t(act), dst))
    for w in info["warnings"]:
        lines.append("! " + (tr_msg(w) if tr else w))
    return "\n".join(lines)


def make_validator(sa, base_dir):
    """用 server_app 的 load_config(None) + normalize_config 校验（与服务启动时规则相同）。"""
    def validate(data):
        cfg = sa.load_config(None)
        cfg.update(data)
        cfg["_base_dir"] = base_dir
        sa.normalize_config(cfg, base_dir)
        return cfg
    return validate
