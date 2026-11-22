#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drivercheck.py —— 驱动与软件环境检测（只读，不安装、不修改任何东西）

检查项：
  1. 串口设备与驱动：所有串口/USB 转串口设备、驱动厂商和版本、是否有黄色感叹号（驱动缺失/出错）、
     常见 USB 转 485/232 芯片识别（CH340/CH341、FTDI、CP210x、PL2303 等），PL2303 假芯片/老驱动提示；
  2. 未安装驱动的设备：设备管理器里"其他设备"中与串口相关的未知设备；
  3. 程序运行环境：Python 库 asyncua / cryptography / pyserial 是否可用及版本；
  4. 系统组件：Windows 防火墙服务、开机自启计划任务、监听端口是否被占用；
  5. 数据目录：监控目录是否可访问、是否有数据文件。

Windows 上通过系统自带 PowerShell 查询（Get-PnpDevice / Win32_PnPSignedDriver），参数固定，不拼接用户输入。
"""
import json
import os
import socket
import subprocess
import time

# 常见 USB 转串口芯片：VID -> (芯片名, 驱动下载提示)
USB_SERIAL = {
    "1A86": ("沁恒 CH340 / CH341 / CH9102", "驱动名一般为 CH341SER，官网 wch.cn 下载"),
    "0403": ("FTDI FT232 / FT2232 / FT4232", "驱动名 FTDI VCP，官网 ftdichip.com 下载"),
    "10C4": ("Silicon Labs CP210x", "驱动名 CP210x VCP，官网 silabs.com 下载"),
    "067B": ("Prolific PL2303", "驱动名 PL2303，官网 prolific.com.tw 下载；注意假芯片在新驱动下会报错代码 10"),
    "0483": ("ST 虚拟串口（STM32 CDC）", "Windows 10/11 一般自带驱动"),
    "2341": ("Arduino", "Windows 10/11 一般自带驱动"),
    "04E2": ("Exar / MaxLinear XR21V", "官网 maxlinear.com 下载"),
    "0557": ("ATEN UC232A（PL2303 方案）", "官网 aten.com 下载"),
    "1546": ("u-blox", "Windows 10/11 一般自带驱动"),
    "0B05": ("ASUS", ""),
}
SERIAL_HINTS = ("COM", "SERIAL", "UART", "串行", "串口", "CH340", "CH341", "CH910", "FTDI", "FT232", "CP210",
                "PL2303", "PROLIFIC", "RS-485", "RS485", "RS-232", "RS232", "USB-SERIAL", "USB SERIAL")
# 设备管理器错误代码（ConfigManagerErrorCode）
CM_ERRORS = {
    1: "设备没有正确配置", 3: "驱动可能已损坏，或内存不足", 10: "设备无法启动（PL2303 假芯片常见此错误）",
    18: "需要重新安装驱动", 19: "注册表中的配置信息损坏", 22: "设备已被禁用", 24: "设备不存在或未安装全部驱动",
    28: "没有安装驱动程序", 31: "设备工作不正常，Windows 无法加载驱动", 39: "驱动程序损坏或缺失",
    43: "设备报告了问题，已被停止", 45: "设备当前没有连接（以前插过）", 52: "驱动没有有效的数字签名",
}

_PS_DEVICES = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$drv = @{}
Get-CimInstance Win32_PnPSignedDriver | ForEach-Object { if ($_.DeviceID) { $drv[$_.DeviceID] = $_ } }
$out = Get-CimInstance Win32_PnPEntity | Where-Object {
  $_.PNPClass -in @('Ports','MultiportSerial') -or ($_.PNPClass -eq $null) -or ($_.ConfigManagerErrorCode -ne 0) -or
  ($_.Name -match 'COM\d|Serial|UART|CH34|CH910|FTDI|FT232|CP210|PL2303|Prolific|485|232')
} | ForEach-Object {
  $d = $drv[$_.DeviceID]
  [pscustomobject]@{
    name = $_.Name; cls = $_.PNPClass; id = $_.DeviceID; status = $_.Status; err = $_.ConfigManagerErrorCode;
    present = $_.Present; mfr = $_.Manufacturer;
    dver = $(if ($d) { $d.DriverVersion } else { '' }); ddate = $(if ($d -and $d.DriverDate) { $d.DriverDate.ToString('yyyy-MM-dd') } else { '' });
    dprov = $(if ($d) { $d.DriverProviderName } else { '' }); signed = $(if ($d) { $d.IsSigned } else { $null })
  }
}
@($out) | ConvertTo-Json -Depth 3 -Compress
"""

_PS_SYSTEM = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$fw = Get-Service -Name MpsSvc
$task = Get-ScheduledTask -TaskName 'ChromExtractOPC'
$info = $null; if ($task) { $info = $task | Get-ScheduledTaskInfo }
$os = Get-CimInstance Win32_OperatingSystem
[pscustomobject]@{
  fw = $(if ($fw) { [string]$fw.Status } else { '' });
  task = $(if ($task) { [string]$task.State } else { '' });
  task_last = $(if ($info -and $info.LastRunTime) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { '' });
  task_result = $(if ($info) { $info.LastTaskResult } else { $null });
  os = $(if ($os) { $os.Caption + ' ' + $os.Version } else { '' })
} | ConvertTo-Json -Compress
"""


def _item(name, level, text, fix=""):
    """level：正常 / 注意 / 问题"""
    return {"name": name, "level": level, "text": text, "fix": fix}


def _ps(script, runner=None, timeout=40):
    """运行固定的 PowerShell 脚本（不含任何用户输入），返回解析后的 JSON。"""
    if runner is not None:
        return runner(script)
    if os.name != "nt":
        return None
    p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", script], capture_output=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = (p.stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        return None
    return json.loads(text)


def _vid(dev_id):
    s = str(dev_id or "").upper()
    i = s.find("VID_")
    return s[i + 4:i + 8] if i >= 0 else ""


def _ftdi_vid(dev_id):
    s = str(dev_id or "").upper()
    return "0403" if s.startswith("FTDIBUS\\") else ""


def check_serial_drivers(runner=None):
    """串口设备与驱动。"""
    items = []
    try:
        devs = _ps(_PS_DEVICES, runner)
    except subprocess.TimeoutExpired:
        return [_item("串口驱动", "注意", "查询设备管理器超时（系统繁忙或 WMI 服务异常）。", "稍后重试，或在设备管理器里手动查看。")]
    except (OSError, ValueError) as e:
        return [_item("串口驱动", "注意", "无法查询设备管理器：%s" % e, "在设备管理器里手动查看“端口(COM 和 LPT)”。")]
    if devs is None:
        if os.name != "nt":
            return [_item("串口驱动", "注意", "非 Windows 系统，跳过设备管理器检查。")]
        devs = []
    if isinstance(devs, dict):
        devs = [devs]
    ports, unknown = [], []
    for d in devs:
        name = str(d.get("name") or "")
        cls = str(d.get("cls") or "")
        dev_id = str(d.get("id") or "")
        err = int(d.get("err") or 0)
        present = d.get("present")
        serial_like = cls in ("Ports", "MultiportSerial") or any(h in name.upper() for h in SERIAL_HINTS) \
            or _vid(dev_id) in USB_SERIAL or dev_id.upper().startswith("FTDIBUS\\")
        if not serial_like:
            continue
        if present is False and err in (0, 45):
            continue                            # 以前插过、现在没插的设备，不报
        vid = _vid(dev_id) or _ftdi_vid(dev_id)
        chip = USB_SERIAL.get(vid)
        entry = {"name": name or "（未命名设备）", "chip": chip[0] if chip else "", "vid": vid,
                 "driver": " ".join(x for x in (str(d.get("dprov") or ""), str(d.get("dver") or "")) if x),
                 "date": str(d.get("ddate") or ""), "err": err, "signed": d.get("signed")}
        if cls in ("Ports", "MultiportSerial") and err == 0:
            ports.append(entry)
        else:
            unknown.append(entry)
    for e in ports:
        desc = e["name"] + ("（%s）" % e["chip"] if e["chip"] else "")
        drv = "驱动 %s%s" % (e["driver"] or "未知", "，%s" % e["date"] if e["date"] else "")
        if e["signed"] is False:
            items.append(_item("串口：" + e["name"], "注意", "%s；%s；驱动没有数字签名。" % (desc, drv),
                               "建议从芯片厂商官网下载已签名的驱动。"))
        elif e["vid"] == "067B" and e["date"] and e["date"] < "2012-01-01":
            items.append(_item("串口：" + e["name"], "注意", "%s；%s；PL2303 老版本驱动。" % (desc, drv),
                               "老驱动在 Windows 10/11 上可能不稳定，若出现断线请更换为 CH340 / FTDI 方案的转换器。"))
        else:
            items.append(_item("串口：" + e["name"], "正常", "%s；%s。" % (desc, drv)))
    for e in unknown:
        chip = e["chip"]
        tip = USB_SERIAL.get(e["vid"], ("", ""))[1]
        reason = CM_ERRORS.get(e["err"], "驱动未安装或工作不正常（错误代码 %d）" % e["err"]) if e["err"] else "驱动未安装"
        fix = ("这是 %s 芯片，%s。" % (chip, tip) if chip else "在设备管理器里右键该设备 → 属性 → 详细信息 → 硬件 ID，"
               "按 VID 找到芯片厂商下载驱动。")
        if e["err"] == 10 and e["vid"] == "067B":
            fix = "PL2303 报错代码 10 多为假芯片被新驱动拒绝：更换正品转换器（推荐 FTDI / CH340），或安装旧版 3.3.2.102 驱动。"
        if e["err"] == 22:
            fix = "设备被禁用：设备管理器里右键 → 启用设备。"
        items.append(_item("设备：" + e["name"], "问题", "%s%s：%s。" % (e["name"], "（%s）" % chip if chip else "", reason), fix))
    if not ports and not unknown:
        items.append(_item("串口驱动", "注意", "没有找到任何串口设备。",
                           "使用 RS-485/232 时：确认 USB 转换器已插好；插上后设备管理器“端口(COM 和 LPT)”里应出现 COM 口。"
                           "不用串口可忽略。"))
    return items


def check_python_libs():
    items = []
    for mod, name, need, use in (("asyncua", "asyncua", True, "OPC UA 服务"),
                                 ("cryptography", "cryptography", True, "加密、证书、HTTPS"),
                                 ("serial", "pyserial", False, "串口测试（RS-232/485）")):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "") or getattr(m, "VERSION", "")
            items.append(_item("运行库：" + name, "正常", "%s %s 可用（%s）。" % (name, ver, use)))
        except Exception as e:  # noqa
            items.append(_item("运行库：" + name, "问题" if need else "注意", "%s 不可用：%s（影响：%s）。" % (name, e, use),
                               "打包版 EXE 应自带；源码运行请执行 pip install -r requirements.txt。"))
    return items


def check_system(cfg=None, runner=None):
    items = []
    cfg = cfg or {}
    try:
        info = _ps(_PS_SYSTEM, runner) or {}
    except Exception as e:  # noqa
        info = {}
        items.append(_item("系统信息", "注意", "无法查询系统服务：%s" % e))
    if info.get("os"):
        items.append(_item("操作系统", "正常", info["os"]))
    fw = info.get("fw")
    if fw:
        items.append(_item("Windows 防火墙服务", "正常" if fw == "Running" else "注意",
                           "状态：%s。" % fw, "" if fw == "Running" else "防火墙服务未运行，设置防火墙.bat 的来源限制不会生效。"))
    task = info.get("task")
    if os.name == "nt" or task:
        if task:
            res = info.get("task_result")
            ok_res = res in (0, 267009, None)          # 267009 = 正在运行
            items.append(_item("开机自启任务", "正常" if ok_res else "注意",
                               "已安装，状态 %s%s。" % (task, "，上次运行 %s" % info["task_last"] if info.get("task_last") else ""),
                               "" if ok_res else "上次运行结果代码 %s，请查看 logs\\server.log 和 logs\\crash.log。" % res))
        else:
            items.append(_item("开机自启任务", "注意", "没有安装开机自启。", "需要开机自动运行时，以管理员身份运行 安装开机自启.bat。"))
    ep = str(cfg.get("endpoint") or "")
    try:
        port = int(ep.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        port = None
    if port:
        s = socket.socket()
        s.settimeout(1)
        try:
            listening = s.connect_ex(("127.0.0.1", port)) == 0
        finally:
            s.close()
        items.append(_item("OPC UA 端口 %d" % port, "正常" if listening else "注意",
                           "本机端口 %d %s。" % (port, "正在监听（服务在运行，或被别的程序占用）" if listening else "没有程序监听"),
                           "" if listening else "服务没在运行时属正常；服务运行中却显示没有监听，请查看日志。"))
    return items


def check_data_dirs(cfg=None):
    items = []
    dirs = (cfg or {}).get("watch_dirs") or []
    if not dirs:
        return [_item("数据目录", "问题", "没有设置监控目录。", "在设置页面“数据源”里填写存放 *_V3.db / *.mbd 的文件夹。")]
    for d in dirs:
        if not os.path.isdir(d):
            items.append(_item("数据目录", "问题", "找不到：%s" % d, "检查路径；网络盘请确认已连接，服务以 SYSTEM 运行时看不到个人映射的盘符，请改用 \\\\服务器\\共享 路径。"))
            continue
        try:
            names = os.listdir(d)
        except OSError as e:
            items.append(_item("数据目录", "问题", "无法读取 %s：%s" % (d, e), "检查文件夹权限。"))
            continue
        n = sum(1 for f in names if f.lower().endswith(("_v3.db", ".mbd")))
        items.append(_item("数据目录", "正常" if n else "注意", "%s：%d 个数据文件。" % (d, n),
                           "" if n else "目录里暂时没有 *_V3.db 或 .mbd 文件，确认色谱工作站的数据存放位置。"))
    return items


def run_all(cfg=None, runner=None):
    t0 = time.perf_counter()
    items = []
    for fn in (lambda: check_serial_drivers(runner), check_python_libs,
               lambda: check_system(cfg, runner), lambda: check_data_dirs(cfg)):
        try:
            items += fn()
        except Exception as e:  # noqa  单项出错不影响其它项
            items.append(_item("检测出错", "注意", str(e)))
    bad = sum(1 for i in items if i["level"] == "问题")
    warn = sum(1 for i in items if i["level"] == "注意")
    summary = ("发现 %d 个问题、%d 个注意事项" % (bad, warn) if bad else
               ("%d 个注意事项，没有问题" % warn if warn else "全部正常"))
    return {"ok": bad == 0, "summary": summary, "items": items, "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
