#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
winservice.py —— 注册为真正的 Windows 服务（services.msc 里可启停），只用标准库 ctypes，不需要 pywin32。

  ChromExtractOPC.exe --install-service     注册服务（管理员）：开机自动启动（延迟启动，等网络就绪）、
                                            LocalSystem 账号、崩溃后 1 分钟自动重启；同时删除旧的计划任务自启
  ChromExtractOPC.exe --uninstall-service   停止并删除服务（管理员）
  ChromExtractOPC.exe --start-service       启动服务（管理员）
  ChromExtractOPC.exe --stop-service        停止服务（管理员；普通用户可用 --stop）
  ChromExtractOPC.exe --service-status      查看服务状态
  ChromExtractOPC.exe --service             仅供 Windows 服务管理器调用

服务内部就是后台静默模式（--background）：单实例、日志写 logs\\server.log、正常停止流程完全一样。
"""
import os
import re
import subprocess
import threading
import time

SERVICE_NAME = "ChromExtractOPC"
DISPLAY_NAME = "色谱提取 OPC UA 服务"
DESCRIPTION = ("ChromExtractOPC：监控色谱数据（*_V3.db / *.mbd），通过 OPC UA 发布最新谱图；"
               "含网页设置页（默认 http://127.0.0.1:8840/）、异常告警与设备检测。")
LEGACY_TASK = "ChromExtractOPC"

STATES = {1: "已停止", 2: "正在启动", 3: "正在停止", 4: "正在运行", 5: "正在继续", 6: "正在暂停", 7: "已暂停"}
START_TYPES = {0: "引导", 1: "系统", 2: "自动", 3: "手动", 4: "已禁用"}
SC_ERRORS = {
    5: "需要管理员权限：请右键“以管理员身份运行”。",
    1053: "服务没有及时响应启动请求（多为杀毒软件拦截或磁盘很慢），请查看 logs\\crash.log 和 logs\\server.log。",
    1056: "服务已经在运行。",
    1058: "服务已被禁用：在 services.msc 里把启动类型改为“自动”。",
    1060: "没有注册这个服务。",
    1062: "服务没有在运行。",
    1066: "服务因程序错误退出，请查看 logs\\server.log 和 logs\\crash.log。",
    1072: "服务已标记为删除：请关闭 services.msc 窗口后重试（必要时重启电脑）。",
    1073: "服务已经存在。",
}


class ServiceError(Exception):
    """给人看的错误。"""


# ---------------------------------------------------------------------------
# sc.exe 封装（参数以列表传入，不经过命令行解释器）
# ---------------------------------------------------------------------------

def _decode(b):
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", "replace")


def _sc(args, runner=None):
    if runner is None:
        if os.name != "nt":
            raise ServiceError("只有 Windows 支持注册为服务。")

        def runner(a):
            return subprocess.run(a, capture_output=True, timeout=60,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    p = runner(["sc.exe"] + list(args))
    return p.returncode, _decode((p.stdout or b"") + (p.stderr or b""))


def _err(rc, text=""):
    return SC_ERRORS.get(rc, "sc.exe 返回错误 %d：%s" % (rc, " ".join(str(text).split())[:200]))


def bin_path(exe):
    return '"%s" --service' % exe


def create_args(exe):
    return ["create", SERVICE_NAME, "binPath=", bin_path(exe), "start=", "delayed-auto",
            "DisplayName=", DISPLAY_NAME]


def config_args(exe):
    return ["config", SERVICE_NAME, "binPath=", bin_path(exe), "start=", "delayed-auto",
            "DisplayName=", DISPLAY_NAME]


def failure_args():
    # 第 1、2 次崩溃 1 分钟后重启，之后每次 5 分钟后重启；24 小时无崩溃则计数清零
    return ["failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/60000/restart/60000/restart/300000"]


def is_admin():
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa
        return False


def query(runner=None):
    """返回 None（没注册）或 {"state", "state_name", "start", "start_name", "path", "account", "delayed"}。"""
    rc, text = _sc(["query", SERVICE_NAME], runner)
    if rc == 1060:
        return None
    if rc != 0:
        raise ServiceError(_err(rc, text))
    m = re.search(r"STATE\s*:\s*(\d+)", text)
    state = int(m.group(1)) if m else 0
    info = {"state": state, "state_name": STATES.get(state, "未知（%d）" % state),
            "start": None, "start_name": "", "path": "", "account": "", "delayed": False}
    rc2, qc = _sc(["qc", SERVICE_NAME], runner)
    if rc2 == 0:
        m = re.search(r"START_TYPE\s*:\s*(\d+)", qc)
        if m:
            info["start"] = int(m.group(1))
            info["start_name"] = START_TYPES.get(info["start"], "")
        info["delayed"] = "DELAYED" in qc.upper()
        m = re.search(r"BINARY_PATH_NAME\s*:\s*(.+)", qc)
        info["path"] = m.group(1).strip() if m else ""
        m = re.search(r"SERVICE_START_NAME\s*:\s*(.+)", qc)
        info["account"] = m.group(1).strip() if m else ""
    return info


def _wait_state(target, timeout, runner=None):
    end = time.time() + timeout
    info = None
    while time.time() < end:
        info = query(runner)
        if info is None or info["state"] == target:
            return info
        time.sleep(1)
    return info


def install(exe, runner=None):
    """注册（或更新）服务。返回给人看的步骤列表。"""
    if not os.path.isfile(exe):
        raise ServiceError("找不到程序：%s" % exe)
    steps = []
    existing = query(runner)
    rc, text = _sc(config_args(exe) if existing else create_args(exe), runner)
    if rc != 0:
        raise ServiceError(_err(rc, text))
    steps.append("已%s服务 %s（%s）" % ("更新" if existing else "注册", SERVICE_NAME, DISPLAY_NAME))
    _sc(["description", SERVICE_NAME, DESCRIPTION], runner)
    rc, text = _sc(failure_args(), runner)
    if rc == 0:
        _sc(["failureflag", SERVICE_NAME, "1"], runner)
        steps.append("崩溃后自动重启：1 分钟后（连续崩溃时 5 分钟后）")
    else:
        steps.append("注意：自动重启设置失败（%s），可在 services.msc → 恢复 里手动设置" % _err(rc, text))
    steps.append("启动类型：自动（延迟启动，开机约 1～2 分钟后启动，等网络和网络盘就绪）")
    steps.append("运行账号：LocalSystem（可在 services.msc → 登录 里改为指定账号，以访问网络共享）")
    # 旧版本用计划任务自启：和服务同时存在没有意义（单实例也只会运行一个），删掉
    if runner is None and os.name == "nt":
        p = subprocess.run(["schtasks.exe", "/Delete", "/TN", LEGACY_TASK, "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode == 0:
            steps.append("已删除旧的开机自启计划任务（改由服务负责）")
    return steps


def start(runner=None, wait=40):
    info = query(runner)
    if info is None:
        raise ServiceError("还没有注册服务：先运行 --install-service（或 安装为Windows服务.bat）。")
    if info["state"] == 4:
        return "服务已经在运行。"
    rc, text = _sc(["start", SERVICE_NAME], runner)
    if rc not in (0, 1056):
        raise ServiceError(_err(rc, text))
    info = _wait_state(4, wait, runner)
    if info and info["state"] == 4:
        return "服务已启动。"
    raise ServiceError("服务没有进入运行状态（当前：%s）。请查看 logs\\server.log 和 logs\\crash.log。"
                       % (info["state_name"] if info else "未注册"))


def stop(runner=None, wait=40):
    info = query(runner)
    if info is None:
        return "没有注册服务。"
    if info["state"] == 1:
        return "服务没有在运行。"
    rc, text = _sc(["stop", SERVICE_NAME], runner)
    if rc not in (0, 1062):
        raise ServiceError(_err(rc, text))
    info = _wait_state(1, wait, runner)
    if info is None or info["state"] == 1:
        return "服务已停止。"
    raise ServiceError("服务 %d 秒内没有停止（当前：%s）。" % (wait, info["state_name"]))


def uninstall(runner=None):
    info = query(runner)
    if info is None:
        return "没有注册服务，无需删除。"
    if info["state"] != 1:
        try:
            stop(runner)
        except ServiceError:
            pass
    rc, text = _sc(["delete", SERVICE_NAME], runner)
    if rc not in (0, 1072):
        raise ServiceError(_err(rc, text))
    return "服务已删除。"


def describe(info):
    if info is None:
        return "没有注册为 Windows 服务。"
    return "Windows 服务 %s：%s；启动类型 %s%s；账号 %s" % (
        SERVICE_NAME, info["state_name"], info["start_name"] or "未知", "（延迟）" if info["delayed"] else "",
        info["account"] or "未知")


# ---------------------------------------------------------------------------
# 服务宿主：由 Windows 服务管理器（SCM）以 --service 启动时运行
# ---------------------------------------------------------------------------

SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN = 0x1, 0x4
SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_SHUTDOWN = 1, 4, 5
ERROR_SERVICE_SPECIFIC_ERROR = 1066
ERROR_CALL_NOT_IMPLEMENTED = 120
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063


def run_as_service(body, on_stop, started_evt, log=None, stop_timeout=90):
    """body() 在工作线程里运行服务并返回退出码；on_stop() 请求它正常停止；
    started_evt 在服务真正开始运行时被置位（之后才向 SCM 报告“正在运行”）。"""
    if os.name != "nt":
        raise ServiceError("只有 Windows 支持服务模式。")
    import ctypes
    from ctypes import wintypes

    adv = ctypes.WinDLL("advapi32", use_last_error=True)

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    HANDLER_EX = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
    SERVICE_MAIN = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))

    class SERVICE_TABLE_ENTRY(ctypes.Structure):
        _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", SERVICE_MAIN)]

    adv.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, HANDLER_EX, ctypes.c_void_p]
    adv.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
    adv.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(SERVICE_STATUS)]
    adv.SetServiceStatus.restype = wintypes.BOOL
    adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRY)]
    adv.StartServiceCtrlDispatcherW.restype = wintypes.BOOL

    status = SERVICE_STATUS()
    status.dwServiceType = SERVICE_WIN32_OWN_PROCESS
    box = {"h": None, "rc": 0, "stop_at": None}
    lock = threading.Lock()
    stop_evt = threading.Event()

    def report(state, exit_rc=0, wait_ms=0):
        with lock:
            status.dwCurrentState = state
            status.dwControlsAccepted = (SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN) if state == SERVICE_RUNNING else 0
            if state == SERVICE_STOPPED and exit_rc:
                # 报告“程序错误退出”：SCM 的“恢复”设置才会生效（自动重启）
                status.dwWin32ExitCode = ERROR_SERVICE_SPECIFIC_ERROR
                status.dwServiceSpecificExitCode = int(exit_rc) & 0xFFFFFFFF
            else:
                status.dwWin32ExitCode = 0
                status.dwServiceSpecificExitCode = 0
            status.dwWaitHint = wait_ms
            status.dwCheckPoint = 0 if state in (SERVICE_RUNNING, SERVICE_STOPPED) else status.dwCheckPoint + 1
            if box["h"]:
                adv.SetServiceStatus(box["h"], ctypes.byref(status))

    def handler(ctrl, event_type, event_data, context):
        if ctrl in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            if not stop_evt.is_set():
                stop_evt.set()
                box["stop_at"] = time.time()
                report(SERVICE_STOP_PENDING, wait_ms=30000)
                if log:
                    log.info("收到 Windows 服务%s请求，正在正常停止……", "停止" if ctrl == SERVICE_CONTROL_STOP else "关机")
                try:
                    on_stop()
                except Exception as e:  # noqa
                    if log:
                        log.error("请求停止时出错：%s", e)
            return 0
        if ctrl == SERVICE_CONTROL_INTERROGATE:
            return 0
        return ERROR_CALL_NOT_IMPLEMENTED

    handler_cb = HANDLER_EX(handler)

    def service_main(argc, argv):
        h = adv.RegisterServiceCtrlHandlerExW(SERVICE_NAME, handler_cb, None)
        if not h:
            return
        box["h"] = h
        report(SERVICE_START_PENDING, wait_ms=30000)

        def work():
            try:
                box["rc"] = int(body() or 0)
            except SystemExit as e:
                box["rc"] = e.code if isinstance(e.code, int) else 1
            except BaseException as e:  # noqa
                box["rc"] = 1
                if log:
                    log.exception("服务线程异常退出：%s", e)

        th = threading.Thread(target=work, daemon=True, name="service-body")
        th.start()
        end = time.time() + 25
        while th.is_alive() and not started_evt.is_set() and not stop_evt.is_set() and time.time() < end:
            th.join(1)
            report(SERVICE_START_PENDING, wait_ms=30000)
        if th.is_alive() and not stop_evt.is_set():
            report(SERVICE_RUNNING)
        while th.is_alive():
            th.join(2)
            if stop_evt.is_set():
                if time.time() - box["stop_at"] > stop_timeout:
                    if log:
                        log.error("服务 %d 秒内没有正常停止，强制结束。", stop_timeout)
                    break
                report(SERVICE_STOP_PENDING, wait_ms=30000)
        # 由服务管理器要求停止的：一律算正常停止；自己退出的：按退出码（非 0 触发自动重启）
        report(SERVICE_STOPPED, exit_rc=0 if stop_evt.is_set() else box["rc"])

    main_cb = SERVICE_MAIN(service_main)
    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = main_cb
    if not adv.StartServiceCtrlDispatcherW(table):
        err = ctypes.get_last_error()
        if err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            raise ServiceError("--service 只能由 Windows 服务管理器启动。注册服务：ChromExtractOPC.exe --install-service")
        raise ServiceError("连接 Windows 服务管理器失败（错误 %d）" % err)
    return box["rc"]
