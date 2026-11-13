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
