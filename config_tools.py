#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config_tools.py —— 配置文件与账号管理工具（命令行和图形窗口共用）

目标：让不懂 JSON 的人也能安全地开启加密、添加账号，而且不会把配置文件改坏。
  - 写入采用"先备份、写临时文件、再原子替换"，断电/出错不会留下写了一半的配置；
  - 只修改被要求修改的项，不会把默认值全部展开写进用户的 config.json；
  - 密码只以哈希形式保存。
"""
import hashlib
import hmac
import json
import os
import re
import shutil

PBKDF2_ITERATIONS = 200_000


class ConfigToolError(Exception):
    """给人看的错误信息（直接显示即可）。"""


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------

def hash_password(password, iterations=PBKDF2_ITERATIONS):
    """生成可写进 config.json 的密码哈希（带随机盐，PBKDF2-SHA256）。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt.hex(), dk.hex())


def verify_password(password, stored):
    """恒定时间比较；格式不对一律当作不匹配。"""
    try:
        algo, it, salt, h = stored.split("$")
        it = int(it)
        if algo != "pbkdf2_sha256" or not (1000 <= it <= 5_000_000):
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), it)
        return hmac.compare_digest(dk, bytes.fromhex(h))
    except Exception:  # noqa
        return False


_COMMON = {"password", "password1", "passw0rd", "12345678", "123456789", "1234567890",
           "qwertyui", "qwerty123", "admin123", "administrator", "11111111", "00000000",
           "abc12345", "iloveyou", "welcome1", "letmein1"}


def check_password_strength(password, username=""):
    """返回 None 表示通过，否则返回人话提示。故意不搞复杂规则，只拦最明显的弱口令。"""
    if len(password) < 8:
        return "密码太短：至少需要 8 个字符。"
    if len(password) > 128:
        return "密码太长：最多 128 个字符。"
    if len(set(password)) < 4:
        return "密码字符太单一，请至少混用 4 种不同的字符。"
    if password.lower() in _COMMON:
        return "这是最常见的弱密码之一，请换一个。"
    if username and username.lower() in password.lower():
        return "密码里不要包含用户名。"
    return None


_USER_RE = re.compile(r"^[A-Za-z0-9_.@\-\u4e00-\u9fff]{1,64}$")


def check_username(name):
    if not _USER_RE.match(name or ""):
        return "用户名只能包含字母、数字、中文和 _ . @ -，长度 1～64。"
    return None


# ---------------------------------------------------------------------------
# 配置文件读写
# ---------------------------------------------------------------------------

def read_raw(path):
    """读取用户 config.json 的原始内容（不合并默认值）。文件不存在返回空字典。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (ValueError, OSError) as e:
        raise ConfigToolError("配置文件 %s 无法读取（%s）。请先修正它再操作。" % (path, e))
    if not isinstance(data, dict):
        raise ConfigToolError("配置文件 %s 的内容不是 JSON 对象。" % path)
    return data


def write_atomic(path, data):
    """先备份为 .bak，再写临时文件，最后原子替换。任何一步失败，原文件都保持不变。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ConfigToolError("无法写入配置文件 %s：%s" % (path, e))


def list_users(path):
    users = read_raw(path).get("users") or {}
    return sorted(users) if isinstance(users, dict) else []


def add_user(path, name, password):
    """添加账号；已存在则视为重设密码。返回 'added' 或 'updated'。"""
    err = check_username(name) or check_password_strength(password, name)
    if err:
        raise ConfigToolError(err)
    data = read_raw(path)
    users = data.get("users")
    if not isinstance(users, dict):
        users = {}
    existed = name in users
    users[name] = hash_password(password)
    data["users"] = users
    write_atomic(path, data)
    return "updated" if existed else "added"


def remove_user(path, name):
    data = read_raw(path)
    users = data.get("users")
    if not isinstance(users, dict) or name not in users:
        raise ConfigToolError("没有这个账号：%s" % name)
    if len(users) == 1 and data.get("allow_anonymous", True) is False:
        raise ConfigToolError("这是最后一个账号，而且已关闭匿名访问：删除后将没有人能连接。\n"
                              "请先添加别的账号，或先允许匿名访问（allow_anonymous=true）。")
    del users[name]
    data["users"] = users
    write_atomic(path, data)


def harden(path, username, password):
    """一键加固：开启加密 + 关闭匿名 + 建一个账号。返回改动说明列表。"""
    err = check_username(username) or check_password_strength(password, username)
    if err:
        raise ConfigToolError(err)
    data = read_raw(path)
    changes = []
    if data.get("security") != "sign_encrypt":
        changes.append("通信加密：已开启（sign_encrypt）")
    data["security"] = "sign_encrypt"
    if data.get("allow_anonymous", True) is not False:
        changes.append("匿名访问：已关闭（必须登录）")
    data["allow_anonymous"] = False
    if data.get("allow_insecure_fallback"):
        changes.append("不加密备用入口：已关闭")
    data["allow_insecure_fallback"] = False
    users = data.get("users")
    if not isinstance(users, dict):
        users = {}
    changes.append("账号 %s：%s" % (username, "已重设密码" if username in users else "已创建"))
    users[username] = hash_password(password)
    data["users"] = users
    write_atomic(path, data)
    return changes


def set_watch_dirs(path, dirs):
    """把监控目录改成给定的文件夹（可多个）。目录必须真实存在；保存为绝对路径。
    返回保存后的列表。只改 watch_dirs 这一项。"""
    cleaned = []
    for d in dirs:
        d = (d or "").strip().strip('"').strip()
        if not d:
            continue
        d = os.path.abspath(os.path.expanduser(d))
        if not os.path.isdir(d):
            raise ConfigToolError("找不到这个文件夹：%s\n请检查路径是否输入正确（可以在资源管理器地址栏复制）。" % d)
        if d not in cleaned:
            cleaned.append(d)
    if not cleaned:
        raise ConfigToolError("没有给出有效的文件夹路径。")
    data = read_raw(path)
    data["watch_dirs"] = cleaned
    write_atomic(path, data)
    return cleaned


# ---------------------------------------------------------------------------
# 网页设置页用：批量修改普通配置项
# ---------------------------------------------------------------------------

# 允许在网页里修改的项及类型（账号/密码不走这里，走 add_user/remove_user）
EDITABLE = {
    "endpoint": str, "server_name": str, "namespace_uri": str,
    "watch_dirs": list, "poll_seconds": int, "last_n": int, "max_points": int,
    "security": str, "allow_anonymous": bool, "allow_insecure_fallback": bool,
    "remote_export": bool, "export_min_interval_sec": int, "max_connections": int,
    "csv_export_dir": str, "max_trace_points": int, "max_file_mb": int,
    "log_max_mb": int, "log_backups": int,
    "web_ui": bool, "web_host": str, "web_port": int,
    "web_https": str, "web_cert_file": str, "web_key_file": str,
    "alert_stale_minutes": int, "alert_scan_errors": int, "alert_webhook": str,
    "alert_email_to": str, "smtp_host": str, "smtp_port": int, "smtp_security": str,
    "smtp_user": str, "smtp_from": str,
    "alert_webhook_level": str, "alert_email_level": str,
    "env_check_on_start": bool, "env_check_hours": int,
}


def _coerce(key, val):
    typ = EDITABLE[key]
    try:
        if typ is bool:
            if isinstance(val, str):
                return val.strip().lower() in ("1", "true", "yes", "on")
            return bool(val)
        if typ is int:
            return int(str(val).strip())
        if typ is list:
            if isinstance(val, str):
                val = [x for x in val.replace("\r", "").split("\n")]
            return [str(x).strip().strip('"').strip() for x in val if str(x).strip()]
        return str(val).strip()
    except (TypeError, ValueError):
        raise ConfigToolError("配置项 %s 的值不对：%r" % (key, val))


def update_settings(path, updates, validate=None):
    """只修改 updates 里给出的、且在 EDITABLE 中的项。
    validate(合并后的字典) 抛异常即拒绝写入（原文件不变）。返回实际改动的键列表。"""
    if not isinstance(updates, dict):
        raise ConfigToolError("提交的数据格式不对。")
    unknown = [k for k in updates if k not in EDITABLE]
    if unknown:
        raise ConfigToolError("这些项不允许在网页里修改：%s" % "、".join(unknown))
    data = read_raw(path)
    changed = []
    for k, v in updates.items():
        v = _coerce(k, v)
        if k == "watch_dirs":
            for d in v:
                if not os.path.isdir(os.path.abspath(os.path.expanduser(d))):
                    raise ConfigToolError("找不到这个文件夹：%s" % d)
        if data.get(k) != v:
            data[k] = v
            changed.append(k)
    if not changed:
        return []
    if validate is not None:
        try:
            validate(dict(data))
        except Exception as e:  # noqa
            raise ConfigToolError(str(e))
    write_atomic(path, data)
    return changed


# ---------------------------------------------------------------------------
# 需要"取回原文"的密码（例如发邮件用的 SMTP 密码）
# 不能像登录密码那样只存哈希。Windows 上用系统 DPAPI（本机范围）加密后再写进 config.json：
# 配置文件被拷到别的电脑上无法解密。非 Windows 只做 Base64 混淆，并在体检里提示。
# ---------------------------------------------------------------------------

def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    CRYPTPROTECT_UI_FORBIDDEN, CRYPTPROTECT_LOCAL_MACHINE = 0x1, 0x4
    flags = CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE   # 本机任何账号（含 SYSTEM 服务）都能解密
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, flags, ctypes.byref(out)):
        raise ConfigToolError("系统加密/解密失败（错误码 %d）" % ctypes.GetLastError())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect_secret(text):
    import base64
    raw = (text or "").encode("utf-8")
    if os.name == "nt":
        return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
    return "b64:" + base64.b64encode(raw).decode("ascii")


def unprotect_secret(stored):
    import base64
    s = str(stored or "")
    if not s:
        return ""
    if s.startswith("dpapi:"):
        if os.name != "nt":
            raise ConfigToolError("这个密码是在 Windows 上加密的，只能在原来那台电脑上使用。")
        try:
            return _dpapi(base64.b64decode(s[6:]), False).decode("utf-8")
        except ConfigToolError:
            raise ConfigToolError("无法解密 SMTP 密码：配置文件可能是从别的电脑拷过来的，请重新设置密码。")
    if s.startswith("b64:"):
        return base64.b64decode(s[4:]).decode("utf-8")
    raise ConfigToolError("smtp_password 格式不对：请不要手写，在设置页面里设置。")


def set_smtp_password(path, password):
    """设置/清除 SMTP 密码（空字符串 = 清除）。"""
    if password and len(password) > 256:
        raise ConfigToolError("密码太长。")
    data = read_raw(path)
    data["smtp_password"] = protect_secret(password) if password else ""
    write_atomic(path, data)


def set_quiet(path, items, normalize):
    """整体替换静默时段列表。normalize 校验（alerts.normalize_quiet），不合法不写入。"""
    try:
        items = normalize(items)
    except ValueError as e:
        raise ConfigToolError(str(e))
    data = read_raw(path)
    data["alert_quiet"] = items
    write_atomic(path, data)
    return items


def set_channel_rule(path, rule, normalize):
    """新增/替换/删除某通道的规则。rule 里只有 channel（其余为空）表示删除。"""
    data = read_raw(path)
    rules = [r for r in (data.get("alert_channel_rules") or []) if r.get("channel") != rule.get("channel")]
    if rule.get("mute") or rule.get("stale_minutes") not in (None, ""):
        rules.append(rule)
    try:
        rules = normalize(rules)
    except ValueError as e:
        raise ConfigToolError(str(e))
    data["alert_channel_rules"] = rules
    write_atomic(path, data)
    return rules


def set_probes(path, items, normalize):
    """整体替换设备检测列表（normalize = probes.normalize_probes），不合法不写入。"""
    try:
        items = normalize(items)
    except ValueError as e:
        raise ConfigToolError(str(e))
    data = read_raw(path)
    data["alert_probes"] = items
    write_atomic(path, data)
    return items
