#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bgrun.py —— 后台静默运行支持

  - 后台模式（--background）：不显示任何窗口、不往控制台输出，日志只写 logs/server.log；
    双击/命令行启动时会自动脱离控制台窗口，开机自启（计划任务）时本来就没有窗口；
  - 单实例：同一台电脑只允许运行一个服务（Windows 命名互斥量；其它系统用锁文件），
    重复启动会直接退出并记日志，不会抢端口；
  - 状态文件 run/status.json：进程号、启动时间、版本、OPC UA 地址、设置页面地址、最近心跳；
  - 优雅停止（--stop）：写停止请求文件，服务在 1～2 秒内正常关闭（保存状态、断开客户端），
    超时才强制结束；--status 查看是否在运行。
"""
import json
import os
import sys
import threading
import time
from datetime import datetime

MUTEX_NAME = "Global\\ChromExtractOPC_SingleInstance"
HEARTBEAT_SEC = 15


def run_dir(base_dir):
    d = os.path.join(base_dir, "run")
    os.makedirs(d, exist_ok=True)
    return d


def status_path(base_dir):
    return os.path.join(run_dir(base_dir), "status.json")


def stop_path(base_dir):
    return os.path.join(run_dir(base_dir), "stop.request")


# ---------------------------------------------------------------------------
# 静默：脱离控制台、屏蔽输出
# ---------------------------------------------------------------------------

def go_silent():
    """把标准输出/错误指向空设备，并在 Windows 上脱离（隐藏）控制台窗口。"""
    try:
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stderr = devnull
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            hwnd = k32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)    # 先隐藏，避免窗口一闪
            k32.FreeConsole()
        except Exception:  # noqa
            pass


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------

class SingleInstance:
    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.handle = None
        self.lock_file = None

    def acquire(self):
        """拿到返回 True；已有实例在运行返回 False。"""
        if os.name == "nt":
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                k32.CreateMutexW.restype = ctypes.c_void_p
                h = k32.CreateMutexW(None, False, MUTEX_NAME)
                if not h:
                    # Global\ 需要权限时退回本会话范围
                    h = k32.CreateMutexW(None, False, MUTEX_NAME.replace("Global\\", "Local\\"))
                if h and k32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
                    k32.CloseHandle(ctypes.c_void_p(h))
                    return False
                self.handle = h
                return True
            except Exception:  # noqa  ctypes 不可用时退回锁文件
                pass
        p = os.path.join(run_dir(self.base_dir), "instance.lock")
        try:
            f = open(p, "a+")
        except OSError:
            return True                 # 连锁文件都建不了：不阻止启动（端口占用时服务自己会报错）
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            self.lock_file = f          # 系统不支持文件锁：只能放行
            return True
        except OSError:
            f.close()
            return False                # 锁被别的进程占着：已有实例在运行
        self.lock_file = f
        return True

    def release(self):
        if self.handle and os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
            except Exception:  # noqa
                pass
            self.handle = None
        if self.lock_file:
            try:
                self.lock_file.close()
            except OSError:
                pass
            self.lock_file = None


# ---------------------------------------------------------------------------
# 状态文件与停止请求
# ---------------------------------------------------------------------------

def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class StatusKeeper:
    """服务运行期间：定期写心跳，监视停止请求。stop_cb 在收到停止请求时被调用（只调一次）。"""

    def __init__(self, base_dir, info, stop_cb, poll=1.0, clock=time.time):
        self.base_dir, self.info, self.stop_cb = base_dir, dict(info), stop_cb
        self.poll, self.clock = poll, clock
        self.evt = threading.Event()
        self.started = clock()

    def start(self):
        try:
            os.remove(stop_path(self.base_dir))           # 清掉上次遗留的停止请求
        except OSError:
            pass
        self._beat()
        threading.Thread(target=self._loop, daemon=True, name="bgrun").start()
        return self

    def _beat(self, state="running"):
        data = dict(self.info, pid=os.getpid(), state=state,
                    started=datetime.fromtimestamp(self.started).strftime("%Y-%m-%d %H:%M:%S"),
                    heartbeat=datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d %H:%M:%S"),
                    heartbeat_ts=self.clock())
        try:
            _write_json(status_path(self.base_dir), data)
        except OSError:
            pass

    def _loop(self):
        last = 0.0
        while not self.evt.wait(self.poll):
            if os.path.exists(stop_path(self.base_dir)):
                try:
                    os.remove(stop_path(self.base_dir))
                except OSError:
                    pass
                self._beat("stopping")
                try:
                    self.stop_cb()
                finally:
                    return
            now = self.clock()
            if now - last >= HEARTBEAT_SEC:
                last = now
                self._beat()

    def close(self):
        self.evt.set()
        self._beat("stopped")


def read_status(base_dir):
    """返回 (是否在运行, 状态字典或 None, 说明)。心跳超过 3 个周期没更新视为没在运行。"""
    p = status_path(base_dir)
    try:
        with open(p, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return False, None, "没有在运行（找不到状态文件）"
    if st.get("state") == "stopped":
        return False, st, "已停止（%s）" % st.get("heartbeat", "")
    age = time.time() - float(st.get("heartbeat_ts") or 0)
    if age > HEARTBEAT_SEC * 3:
        return False, st, "没有在运行（最后心跳 %s，可能异常退出或被强制结束）" % st.get("heartbeat", "")
    return True, st, "正在运行（进程 %s，启动于 %s）" % (st.get("pid"), st.get("started"))


def request_stop(base_dir, timeout=20, force=True):
    """请求后台服务停止。返回 (成功, 说明)。"""
    running, st, _ = read_status(base_dir)
    if not running:
        return True, "服务没有在运行。"
    if (st or {}).get("mode") == "service":
        # 作为 Windows 服务运行：优先通过服务管理器停止（状态显示才对，也不会被当成崩溃）
        try:
            import winservice
            if winservice.is_admin():
                return True, winservice.stop()
        except Exception:  # noqa  没有权限等：退回停止请求文件（服务会以正常退出码结束，不会被自动重启）
            pass
    with open(stop_path(base_dir), "w", encoding="utf-8") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        r, st2, _ = read_status(base_dir)
        if not r or (st2 and st2.get("state") == "stopped"):
            return True, "服务已正常停止。"
    if force and st and st.get("pid"):
        try:
            if os.name == "nt":
                import subprocess
                subprocess.run(["taskkill", "/PID", str(int(st["pid"])), "/F"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                import signal
                os.kill(int(st["pid"]), signal.SIGTERM)
            return True, "服务 %d 秒内没有响应，已强制结束（进程 %s）。" % (timeout, st["pid"])
        except Exception as e:  # noqa
            return False, "停止失败：%s" % e
    try:
        os.remove(stop_path(base_dir))
    except OSError:
        pass
    return False, "服务 %d 秒内没有停止。" % timeout
