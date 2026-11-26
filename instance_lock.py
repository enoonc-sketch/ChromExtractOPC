#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
instance_lock.py —— 桌面程序单实例（v2.9 起替代 bgrun.py 中的同名功能）

同一台电脑只允许一个桌面窗口运行 OPC UA 服务，避免两个窗口抢 4840 端口、重复告警。
Windows 上用命名互斥量（Global\\，无权限时退回 Local\\）；其它系统用锁文件。
本模块不含任何后台运行、状态文件或停止请求逻辑。
"""
import os

MUTEX_NAME = "Global\\ChromExtractOPC_SingleInstance"


def _lock_dir(base_dir):
    d = os.path.join(base_dir, "run")
    os.makedirs(d, exist_ok=True)
    return d


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
        try:
            p = os.path.join(_lock_dir(self.base_dir), "instance.lock")
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
