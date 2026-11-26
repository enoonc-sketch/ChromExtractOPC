#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit.py —— 桌面窗口的用户权限与操作审计（v3.1 新增；不依赖 tkinter）

角色（从低到高）：
  operator 操作员：只看（状态、通道、谱图、历史、报表查看）；
  engineer 工程师：改配置、启停服务、诊断 / 通讯扫描、导出、Modbus 映射、报表生成；
  admin    管理员：账号与角色、安全设置、备份与还原、审计日志导出。

账号复用 config.json 的 users（PBKDF2 哈希，与 OPC UA 登录同一套），角色放在新配置项
user_roles {"用户名": "operator/engineer/admin"}；没有写角色的账号按操作员。
首次使用（没有任何管理员）时窗口会引导创建管理员；创建前窗口只读。

审计日志 logs/audit.log：每行一个 JSON（时间、用户、来源、操作、对象、修改前 / 后），
带哈希链：hash = SHA-256(上一行 hash + 本行内容)，任何一行被改、删、插都能检测出来（verify_chain）。
密码、哈希、私钥等敏感值一律打码。
"""
import csv
import hashlib
import hmac
import json
import os
import threading
import time
from datetime import datetime

from i18n import M

ROLES = ("operator", "engineer", "admin")
ROLE_NAMES = {"operator": "操作员", "engineer": "工程师", "admin": "管理员"}
ROLE_LEVEL = {r: i for i, r in enumerate(ROLES)}
# 操作 → 需要的最低角色
PERMISSIONS = {
    "view": "operator",
    "service": "engineer", "config": "engineer", "diagnose": "engineer", "export": "engineer",
    "modbus_map": "engineer", "report": "engineer", "peaks": "engineer",
    "users": "admin", "security": "admin", "backup": "admin", "restore": "admin", "audit_export": "admin",
    "roles": "admin",
}
SENSITIVE = ("password", "passwd", "pw", "secret", "token", "key", "hash", "users", "smtp_password",
             "password_enc")
GENESIS = "0" * 64
AUDIT_FILE = "logs/audit.log"
MAX_VALUE_LEN = 300


class AuthError(Exception):
    pass


def normalize_role(v):
    v = str(v or "").strip().lower()
    if v not in ROLES:
        raise ValueError("角色只能是 operator / engineer / admin，当前为：%r" % v)
    return v


def normalize_roles(roles, users=None):
    """user_roles 校验：字典，值为合法角色。users 给出时去掉已不存在的账号。"""
    if roles in (None, ""):
        return {}
    if not isinstance(roles, dict):
        raise ValueError("user_roles 必须是 {\"用户名\": \"角色\"} 的形式")
    out = {}
    for k, v in roles.items():
        if users is not None and k not in users:
            continue
        out[str(k)] = normalize_role(v)
    return out


def role_of(cfg, name):
    return (cfg.get("user_roles") or {}).get(name, "operator")


def allowed(role, perm):
    need = PERMISSIONS.get(perm, "admin")
    return ROLE_LEVEL.get(role or "", -1) >= ROLE_LEVEL[need]


def has_admin(cfg):
    users = cfg.get("users") or {}
    return any(r == "admin" and u in users for u, r in (cfg.get("user_roles") or {}).items())


def is_sensitive(key):
    k = str(key or "").lower()
    return any(s in k for s in SENSITIVE)


def mask(value, key=""):
    """敏感值打码：密码 / 哈希等只记“已修改（已隐藏）”；其它值截断到 300 字符。"""
    if is_sensitive(key):
        if value in (None, "", {}, []):
            return ""
        if isinstance(value, dict):
            return {k: "***" for k in value}       # 账号表：只留用户名
        return "***"
    if isinstance(value, dict):
        return {k: mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v, key) for v in value[:50]]
    if isinstance(value, str) and len(value) > MAX_VALUE_LEN:
        return value[:MAX_VALUE_LEN] + "…"
    return value


def diff(before, after, keys=None):
    """两份配置的差异：{键: [前, 后]}（已打码）。"""
    before, after = before or {}, after or {}
    ks = keys if keys is not None else sorted(set(before) | set(after))
    out = {}
    for k in ks:
        if before.get(k) != after.get(k):
            out[k] = [mask(before.get(k), k), mask(after.get(k), k)]
    return out


# ---------------------------------------------------------------------------
# 登录会话（窗口用）
# ---------------------------------------------------------------------------

class Session:
    """当前窗口的登录状态与空闲锁定。auth_enabled=False 时所有人都是管理员（不需要登录）。"""

    def __init__(self, cfg_reader, verify, idle_minutes=15, auth_enabled=True, clock=time.monotonic):
        self.cfg_reader, self.verify = cfg_reader, verify
        self.idle_minutes = int(idle_minutes or 0)
        self.auth_enabled = bool(auth_enabled)
        self.clock = clock
        self.user, self.role = None, None
        self.last_activity = clock()
        self.failures = {}

    @property
    def effective_role(self):
        if not self.auth_enabled:
            return "admin"
        return self.role or "operator"

    def can(self, perm):
        return allowed(self.effective_role, perm)

    def login(self, name, password):
        """成功返回角色；失败抛 AuthError（连续失败 5 次后 60 秒内拒绝该账号）。"""
        name = str(name or "").strip()
        f = self.failures.get(name)
        if f and f[0] >= 5 and self.clock() - f[1] < 60:
            raise AuthError("失败次数太多，请 1 分钟后再试。")
        cfg = self.cfg_reader()
        h = (cfg.get("users") or {}).get(name)
        if not h or not self.verify(password or "", h):
            cnt = (f[0] if f and self.clock() - f[1] < 60 else 0) + 1
            self.failures[name] = (cnt, self.clock())
            raise AuthError("用户名或密码不对。")
        self.failures.pop(name, None)
        self.user, self.role = name, role_of(cfg, name)
        self.touch()
        return self.role

    def logout(self):
        self.user, self.role = None, None

    def touch(self):
        self.last_activity = self.clock()

    def idle_expired(self):
        """登录状态下超过空闲时间返回 True（窗口据此锁定）。"""
        if not self.auth_enabled or self.user is None or self.idle_minutes <= 0:
            return False
        return self.clock() - self.last_activity >= self.idle_minutes * 60

    def actor(self):
        if not self.auth_enabled:
            return "desktop"
        return self.user or "operator(未登录)"


def create_admin(cfg_path, name, password, config_tools):
    """首次引导：创建（或把已有账号设为）管理员。已有账号必须密码正确。返回 'created'/'promoted'。"""
    raw = config_tools.read_raw(cfg_path)
    users = raw.get("users") or {}
    if name in users:
        if not config_tools.verify_password(password, users[name]):
            raise AuthError(M("账号 %s 已存在，密码不对。", name))
        res = "promoted"
    else:
        config_tools.add_user(cfg_path, name, password)
        res = "created"
    set_role(cfg_path, name, "admin", config_tools)
    return res


def set_role(cfg_path, name, role, config_tools):
    role = normalize_role(role)
    raw = config_tools.read_raw(cfg_path)
    if name not in (raw.get("users") or {}):
        raise AuthError(M("没有这个账号：%s", name))
    roles = dict(raw.get("user_roles") or {})
    if roles.get(name) == "admin" and role != "admin":
        others = [u for u, r in roles.items() if r == "admin" and u != name and u in (raw.get("users") or {})]
        if not others:
            raise AuthError("这是最后一个管理员，不能降级（请先把别的账号设为管理员）。")
    roles[name] = role
    raw["user_roles"] = roles
    config_tools.write_atomic(cfg_path, raw)
    return roles


# ---------------------------------------------------------------------------
# 审计日志（哈希链）
# ---------------------------------------------------------------------------

def _line_hash(prev, body):
    return hashlib.sha256((prev + "|" + body).encode("utf-8")).hexdigest()


def _canon(rec):
    return json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    """追加写入；进程内加锁。每行 = JSON，包含 prev 与 hash。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._last = None

    def _tail_hash(self):
        if self._last is not None:
            return self._last
        last = GENESIS
        if os.path.exists(self.path):
            with open(self.path, "rb") as f:
                try:
                    f.seek(-8192, 2)
                except OSError:
                    f.seek(0)
                lines = [l for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
            if lines:
                try:
                    last = json.loads(lines[-1]).get("hash") or GENESIS
                except ValueError:
                    last = GENESIS
        return last

    def record(self, user, action, target="", before=None, after=None, source="desktop", ok=True,
               detail="", now=None):
        rec = {"time": (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), "user": str(user or ""),
               "source": source, "action": action, "target": str(target or "")[:200],
               "before": mask(before, target), "after": mask(after, target), "ok": bool(ok),
               "detail": str(detail or "")[:500]}
        with self._lock:
            prev = self._tail_hash()
            rec["prev"] = prev
            body = _canon({k: v for k, v in rec.items() if k != "hash"})
            rec["hash"] = _line_hash(prev, body)
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(_canon(rec) + "\n")
                f.flush()
            self._last = rec["hash"]
        return rec

    def read(self):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    r = {"_bad": True, "line": i}
                r["_line"] = i
                out.append(r)
        return out

    def verify_chain(self):
        """返回 (是否完整, 问题列表 [(行号, 原因)])。"""
        prev = GENESIS
        problems = []
        for r in self.read():
            if r.get("_bad"):
                problems.append((r["_line"], "不是有效的 JSON"))
                continue
            ln = r.pop("_line")
            h = r.get("hash", "")
            if r.get("prev") != prev:
                problems.append((ln, "与上一行的链接断开（有行被删除、插入或改动）"))
            body = _canon({k: v for k, v in r.items() if k != "hash"})
            if not hmac.compare_digest(_line_hash(r.get("prev", ""), body), h):
                problems.append((ln, "内容被改动（哈希不一致）"))
            prev = h
        return (not problems), problems

    def query(self, start=None, end=None, user=None, action=None, text=None):
        out = []
        for r in self.read():
            if r.get("_bad"):
                continue
            t = r.get("time", "")
            if start and t < start:
                continue
            if end and t > end:
                continue
            if user and r.get("user") != user:
                continue
            if action and action not in str(r.get("action")):
                continue
            if text and text not in _canon(r):
                continue
            out.append(r)
        return out

    def export_csv(self, rows, path, headers=None):
        hdr = headers or ("时间", "用户", "来源", "操作", "对象", "修改前", "修改后", "结果", "说明", "哈希")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(list(hdr))
            for r in rows:
                vals = [r.get("time"), r.get("user"), r.get("source"), r.get("action"), r.get("target"),
                        _short(r.get("before")), _short(r.get("after")), "OK" if r.get("ok") else "FAIL",
                        r.get("detail"), r.get("hash", "")[:16]]
                w.writerow([_cell(v) for v in vals])
        return path


def _short(v):
    if v in (None, ""):
        return ""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)[:MAX_VALUE_LEN]


def _cell(v):
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def audit_path(cfg):
    base = cfg.get("_base_dir") or "."
    return os.path.join(base, AUDIT_FILE)


_LOGS = {}


def get_log(cfg_or_path):
    """同一个文件共用一个 AuditLog（同一进程里网页和窗口一起写也不会打乱哈希链）。"""
    p = cfg_or_path if isinstance(cfg_or_path, str) else audit_path(cfg_or_path)
    p = os.path.abspath(p)
    if p not in _LOGS:
        _LOGS[p] = AuditLog(p)
    return _LOGS[p]
