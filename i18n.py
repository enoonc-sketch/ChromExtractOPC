#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
i18n.py —— 中英文界面切换（v3.1 新增；不依赖 tkinter，可单独测试）

用法：
    from i18n import tr
    tr("启动服务")          # 中文界面返回原文；英文界面返回 "Start service"
    tr("已 %d 分钟") % 5     # 带占位符的句子：先翻译再格式化，英文译文的占位符与原文一致
    tr("app.title")         # 也可以用英文键（KEYS 里定义），缺失时回退中文

规则：
  - 翻译以“中文原文”为键（EN 字典），这样代码里仍然写中文，没翻译到的字符串自动回退中文；
  - 只翻译界面文字。日志内容、OPC UA 节点名、配置键名、CSV 列名中的字段键一律不翻译，
    避免影响 DCS 组态和排障（日志始终是中文，便于与历史日志对照）；
  - 语言设置 desktop_language："zh" / "en" / "auto"（auto 按 Windows 系统界面语言：中文系统用中文，其它用英文）。
  - 切换语言时窗口用 retranslate_text() 把“旧语言的显示文字”换成“新语言的显示文字”，即时刷新。
"""
import locale
import os
import re
import threading

LANGS = ("zh", "en")
LANG_CHOICES = ("auto", "zh", "en")
LANG_NAMES = {"auto": "跟随系统 / Auto", "zh": "中文", "en": "English"}

_state = {"lang": "zh"}
_lock = threading.Lock()
_listeners = []

# 英文键：给少量需要固定标识的文字使用（tr("app.title")）
KEYS = {
    "app.title": {"zh": "色谱提取 OPC UA 服务", "en": "ChromExtract OPC UA Server"},
    "lang.restart_hint": {"zh": "部分文字会在下一次刷新时更新；如有个别文字没变，重新打开窗口即可。",
                          "en": "Some texts update on the next refresh; reopen the window if anything is left untranslated."},
}

# 中文原文 -> English（1054 条；桌面窗口与网页设置页共用）
EN = {
    "\n\n服务原来没有运行，已按新设置启动。": "\n\nThe service was not running; it has been started with the new settings.",
    "\n\n正在重启服务（客户端会短暂断开几秒）……": "\n\nRestarting the service (clients will disconnect for a few seconds)…",
    "\n\n现在重启服务让这些设置生效吗？（客户端会短暂断开几秒）": "\n\nRestart the service now to apply these settings? (Clients will disconnect for a few seconds.)",
    "\n……另有 %d 项，请看窗口“运行状态”页": "\n…and %d more; see the \"Status\" tab",
    "\n注意：暂时没有 *_V3.db 或 .mbd 文件，请确认路径是否正确。": "\nNote: no *_V3.db or .mbd files found yet; please check the path.",
    "    %d 次：%s": "    %d times: %s",
    "    处理：%s": "    Fix: %s",
    "   ← 找不到这个文件夹": "   ← folder not found",
    "  ✓ %d,%s,%g  %s（%.0f ms）": "  ✓ %d,%s,%g  %s (%.0f ms)",
    "  日期": "  Date",
    "  显示：": "  Show:",
    "  班次": "  Shift",
    "  空闲自动锁定（分钟，0 = 不锁定）": "  Auto-lock when idle (minutes, 0 = never)",
    "  站号 %3d  %s  %s（%.0f ms）": "  Slave %3d  %s  %s (%.0f ms)",
    "  通道": "  Channel",
    " 共 %d 个，告警中 %d 个": " %d in total, %d alerting",
    "%.1f s（%.1f 分钟）": "%.1f s (%.1f min)",
    "%d 分钟前": "%d min ago",
    "%d 次：%s": "%d times: %s",
    "%s %d,%s,%g：扫描站号 %d～%d（%d/%d），发现 %d 个从站%s": "%s %d,%s,%g: scanned slave IDs %d–%d (%d/%d), found %d slave(s)%s",
    "%s %s · %s · %s / %s · 每 %s 秒": "%s %s · %s · %s / %s · every %s s",
    "%s %s：%s": "%s %s: %s",
    "%s · 绘制 %d 点（按像素抽点，保留峰值）": "%s · %d points drawn (min/max per pixel, peaks kept)",
    "%s · 耗时 %s ms%s": "%s · took %s ms%s",
    "%s 不能为空": "%s must not be empty",
    "%s 不能包含换行或制表符": "%s must not contain line breaks or tabs",
    "%s 只能选：%s": "%s can only be: %s",
    "%s 必须在 %d～%d 之间，当前：%d": "%s must be between %d and %d, current: %d",
    "%s 必须是数字": "%s must be a number",
    "%s 必须是整数，当前填的是：%r": "%s must be an integer, you entered: %r",
    "%s 正被其它测试或设备检测使用，请稍后再试。": "%s is in use by another test or device check; please try again later.",
    "%s 范围 %d～%d": "%s range %d–%d",
    "%s%%（%d 次）": "%s%% (%d times)",
    "%s（%s %s）：%d 次，失败率 %s%%，最长连续失败 %s，平均/P95/最大 %s/%s/%s ms": "%s (%s %s): %d times, failure rate %s%%, longest failure run %s, avg/P95/max %s/%s/%s ms",
    "%s（%s）": "%s (%s)",
    "%s（%s）不在证书目录里，没有包含在备份中，换电脑时请另行复制。": "%s (%s) is not inside the certificate folder and is not included in the backup; copy it separately when moving to another PC.",
    "%s（%s）接线：%s": "%s (%s) wiring: %s",
    "%s（已回滚，配置保持还原前的状态）": "%s (rolled back; configuration unchanged)",
    "%s：扫描 %d/%d 个端口，开放 %d 个%s": "%s: scanned %d/%d ports, %d open%s",
    "%s：新告警 %d 项%s": "%s: %d new alert(s)%s",
    "0 = 不检查": "0 = don't check",
    "0 = 只在启动时检查；改完点底部“保存设置”": "0 = only at start-up; click \"Save settings\" at the bottom after changing",
    "0 = 只在启动时检查；改完点页面底部“保存设置”": "0 = only at start-up; click \"Save settings\" at the bottom of the page after changing",
    "01 读线圈": "01 Read coils",
    "02 读离散输入": "02 Read discrete inputs",
    "03 读保持寄存器": "03 Read holding registers",
    "04 读输入寄存器": "04 Read input registers",
    "05 写单线圈": "05 Write single coil",
    "06 写单寄存器": "06 Write single register",
    "0基地址": "Address (0-based)",
    "1 小时": "1 hour",
    "10 分钟": "10 minutes",
    "127.0.0.1 = 只有本机能打开（推荐）；改成其它地址后必须用账号登录": "127.0.0.1 = only this PC can open it (recommended); any other address requires an account login",
    "127.0.0.1 = 只有本机能读（默认）；0.0.0.0 或网卡地址 = 对外开放（Modbus 没有认证，务必配白名单）": "127.0.0.1 = only this PC can read (default); 0.0.0.0 or a NIC address = open to the network (Modbus has no authentication — always set an allow list)",
    "15 写多线圈": "15 Write multiple coils",
    "16 写多寄存器": "16 Write multiple registers",
    "2 小时": "2 hours",
    "24 小时": "24 hours",
    "32 位数据字节序": "32-bit byte order",
    "485配型诊断": "RS-485 setup diagnosis",
    "72 小时": "72 hours",
    "8 小时": "8 hours",
    "CSV 导出": "CSV export",
    "CSV 导出目录": "CSV export folder",
    "ChromExtract 设置网页": "ChromExtract settings page",
    "DNS 解析": "DNS lookup",
    "EtherNet/IP 隐式": "EtherNet/IP implicit",
    "EtherNet/IP（CIP）": "EtherNet/IP (CIP)",
    "FTDI（FT232/FT2232 等）": "FTDI (FT232/FT2232 etc.)",
    "FTP 数据": "FTP data",
    "HTTPS（备用）": "HTTPS (alternate)",
    "HTTP（备用）": "HTTP (alternate)",
    "IP 扫描": "IP scan",
    "IP 扫描只支持 IPv4：%s": "IP scan supports IPv4 only: %s",
    "IP 扫描时同时扫的端口最多 64 个（大量端口请用“端口扫描”逐台扫）": "At most 64 ports can be scanned together with an IP scan (use \"Port scan\" host by host for many ports)",
    "IP 扫描（在线主机）": "IP scan (online hosts)",
    "MQTT（TLS）": "MQTT (TLS)",
    "Modbus Poll 主站测试": "Modbus Poll master test",
    "Modbus Slave 从站模拟器": "Modbus Slave simulator",
    "Modbus TCP 只读从站": "Modbus TCP read-only slave",
    "Modbus TCP 没有任何认证和加密：监听 %s 会让网络上的设备都能读取数据。": "Modbus TCP has no authentication or encryption: listening on %s lets every device on the network read the data.",
    "Modbus TCP（备用）": "Modbus TCP (alternate)",
    "Modbus TCP（常见替代）": "Modbus TCP (common alternative)",
    "Modbus 转发": "Modbus forwarding",
    "Modbus 转发的启用 / 地址 / 端口 / 白名单需要重启服务才生效。现在重启吗？": "Enabling Modbus forwarding / address / port / allow list take effect after a service restart. Restart now?",
    "Modbus地址": "Modbus address",
    "OPC UA 客户端连接数": "OPC UA client connections",
    "OPC UA 服务": "OPC UA service",
    "OPC UA 端口": "OPC UA port",
    "OPC UA（TLS）": "OPC UA (TLS)",
    "Ping 超时": "Ping timeout",
    "Ping 超时(秒)": "Ping timeout (s)",
    "QQ/163/企业邮箱请填“授权码”，不是网页登录密码。": "For QQ/163/corporate mail use the \"authorization code\", not the web login password.",
    "RS-232（全双工，点对点，≤15 m）：本机 TX(DB9 第 3 脚) → 对方 RX，本机 RX(第 2 脚) ← 对方 TX，GND(第 5 脚) 必须相连；两端都是电脑/DTE 时 TX/RX 要交叉（用交叉线 / 零调制解调器线）。": "RS-232 (full duplex, point-to-point, ≤15 m): local TX (DB9 pin 3) → remote RX, local RX (pin 2) ← remote TX, GND (pin 5) must be connected; between two PCs/DTEs TX/RX must be crossed (crossover / null-modem cable).",
    "RS-422（四线全双工）：本机 TX+ → 对方 RX+，TX- → 对方 RX-，本机 RX+ ← 对方 TX+，RX- ← 对方 TX-，建议连 GND；正负接反时收不到或乱码。": "RS-422 (4-wire full duplex): local TX+ → remote RX+, TX- → remote RX-, local RX+ ← remote TX+, RX- ← remote TX-; connecting GND is recommended. Swapped polarity gives no data or garbage.",
    "RS-485 是半双工，不能做 TX/RX 短接环回": "RS-485 is half duplex; a TX/RX loopback test is not possible",
    "RS-485（两线半双工，可多站）：A 接 A（D+ / T/R+），B 接 B（D- / T/R-），建议同时连 GND；总线两端各接一个 120 Ω 终端电阻。转换器没有自动收发切换时勾选“RTS 控制收发方向”。完全没反应时最常见的原因是 A/B 接反。": "RS-485 (2-wire half duplex, multi-drop): A to A (D+ / T/R+), B to B (D- / T/R-), GND recommended; put a 120 Ω terminator at both ends of the bus. Tick \"RTS controls direction\" if the converter has no automatic direction control. If nothing responds at all, A/B swapped is the most common cause.",
    "RTS 控制收发方向": "RTS controls direction",
    "RTS 控制收发方向（仅 RS-485 半双工）": "RTS controls direction (RS-485 half duplex only)",
    "RTS 控制收发方向（无自动切换的 485 转换器）": "RTS controls direction (485 converters without auto switching)",
    "RTU 站号范围 1～247": "RTU slave ID range 1–247",
    "S7 / ISO-TSAP（西门子）": "S7 / ISO-TSAP (Siemens)",
    "SMB 文件共享": "SMB file sharing",
    "SMTP 密码是用本机 Windows DPAPI 加密的：还原到别的电脑后无法解密，会被清空，需要重新填写。": "The SMTP password is encrypted with this PC's Windows DPAPI: it cannot be decrypted on another PC and will be cleared there; re-enter it after restoring.",
    "SMTP 服务器": "SMTP server",
    "SSL（一般 465）": "SSL (usually 465)",
    "STARTTLS（一般 587）": "STARTTLS (usually 587)",
    "STMicroelectronics（虚拟串口）": "STMicroelectronics (virtual COM port)",
    "Shell_NotifyIcon(NIM_ADD) 失败": "Shell_NotifyIcon(NIM_ADD) failed",
    "Shell_NotifyIcon(NIM_MODIFY) 失败": "Shell_NotifyIcon(NIM_MODIFY) failed",
    "TCP 端口": "TCP port",
    "TCP端口": "TCP port ",
    "WCH 沁恒 CH340/CH341/CH343": "WCH CH340/CH341/CH343",
    "Windows 通知": "Windows notification",
    "[ 失败 ]": "[ FAIL ]",
    "[ 失败 ] ": "[ FAIL ] ",
    "[ 成功 ] ": "[ OK ] ",
    "[ 注意 ]": "[ NOTE ]",
    "[ 通过 ]": "[ PASS ]",
    "[%s] %s 站号 %s 功能码 %s 请求 %s 应答 %s%s": "[%s] %s unit %s function %s request %s response %s%s",
    "[%s] %s（自 %s）": "[%s] %s (since %s)",
    "[%s] %s：%s": "[%s] %s: %s",
    "[推送静默至 %s]    ": "[notifications muted until %s]    ",
    "[正常] 没有告警": "[OK] no alerts",
    "[错误] %s": "[Error] %s",
    "auto（对外开放时自动启用，推荐）": "auto (enabled automatically when open to the network, recommended)",
    "endpoint 里的端口，DCS/SCADA 用它连接，默认 4840": "Port in the endpoint; DCS/SCADA connect to it, default 4840",
    "manifest 文件清单格式不对。": "The file list in the manifest is malformed.",
    "manifest 里有不安全的路径（%s）：%r": "The manifest contains an unsafe path (%s): %r",
    "manifest 里没有文件清单。": "The manifest has no file list.",
    "manifest.json 不是有效的 JSON。": "manifest.json is not valid JSON.",
    "manifest.json 太大。": "manifest.json is too large.",
    "none（不加密）": "none (no encryption)",
    "off（不启用）": "off (disabled)",
    "on（始终启用）": "on (always enabled)",
    "sign_encrypt（签名+加密，推荐）": "sign_encrypt (sign + encrypt, recommended)",
    "sign（签名）": "sign (signature)",
    "“%s”不支持长时间测试。": "\"%s\" does not support long-running tests.",
    "“%s”不支持长时间测试，请先选 Ping / TCP / DNS / OPC UA / Modbus / 串口收发。": "\"%s\" does not support long-running tests; choose Ping / TCP / DNS / OPC UA / Modbus / Serial send-receive.",
    "“%s”不能做设备检测，请选 Ping / TCP / DNS / OPC UA / Modbus / 串口收发。": "\"%s\" cannot be used as a device check; choose Ping / TCP / DNS / OPC UA / Modbus / Serial send-receive.",
    "“最近 N 个周期”请填正整数，留空表示全部导出。": "\"Last N cycles\" must be a positive integer; leave empty to export all.",
    "“登录后自动打开窗口”只能在 Windows 上使用。": "\"Open window after login\" is only available on Windows.",
    "“登录后自动打开窗口”本来就没有开启。": "\"Open window after login\" was not enabled.",
    "■ 停止服务": "■ Stop service",
    "■ 服务已停止：客户端读不到数据。点“启动服务”开始发布。": "■ Service stopped: clients cannot read data. Click \"Start service\" to publish.",
    "■ 服务未运行：启动失败（%s）。处理后点“启动服务”重试。": "■ Service not running: start-up failed (%s). Fix it and click \"Start service\" to retry.",
    "▶ 启动服务": "▶ Start service",
    "● 服务运行中：%s（DCS/SCADA 可连接；关闭窗口会停止服务）": "● Service running: %s (DCS/SCADA can connect; closing the window stops the service)",
    "⚠ %s：%s": "⚠ %s: %s",
    "✓ 恢复": "✓ Recovered",
    "、": ", ",
    "一个都没找到：先用“波特率探测”确认串口参数，再检查 A/B 接线和 RTS 方向控制。": "Nothing found: confirm the serial settings with \"Baud rate detection\" first, then check A/B wiring and RTS direction control.",
    "一次性时段": "One-time window",
    "一键加固会开启加密、关闭匿名访问。现有 DCS/OPC 客户端需要同步改成加密+账号登录，否则会连不上。继续吗？": "Hardening enables encryption and disables anonymous access. Existing DCS/OPC clients must switch to encryption + account login or they will not connect. Continue?",
    "一键加固（开启加密 + 关闭匿名 + 用上面的账号）": "Harden in one click (enable encryption + disable anonymous + use the account above)",
    "三菱 MELSEC": "Mitsubishi MELSEC",
    "上一次扫描还在进行，请等它结束或点“停止”。": "The previous scan is still running; wait for it to finish or click \"Stop\".",
    "上执行。": ".",
    "上次自动检测：%s · %s": "Last automatic check: %s · %s",
    "下载明细 CSV": "Download detail CSV",
    "不加密（仅内网免登录中继，25）": "No encryption (only for an internal relay without login, 25)",
    "不发": "Don't send",
    "不在允许的目录（config.json / certs/ / trusted/ / logs/）里": "not in an allowed folder (config.json / certs/ / trusted/ / logs/)",
    "不支持的功能码：%s": "Unsupported function code: %s",
    "不支持的加密方式。": "Unsupported encryption method.",
    "不是 Windows": "Not Windows",
    "不是有效的 IP 地址：%r": "Not a valid IP address: %r",
    "不是有效的 JSON": "not valid JSON",
    "不是有效的备份包（zip 无法打开）：%s": "Not a valid backup package (zip cannot be opened): %s",
    "不能还原：%s": "Cannot restore: %s",
    "不自动生成": "Do not generate automatically",
    "与上一行的链接断开（有行被删除、插入或改动）": "chain broken from the previous line (a line was deleted, inserted or changed)",
    "与群机器人 / 邮件使用同一份静默时段（告警与通知页设置）": "Uses the same quiet periods as the group robot / e-mail (set on the Alerts tab)",
    "丢失": "Lost",
    "两次输入的备份密码不一致。": "The two backup passwords do not match.",
    "两次输入的密码不一致。": "The two passwords do not match.",
    "严重": "Critical",
    "严重 = 扫描卡死、连续扫描出错；警告 = 无数据、数据源丢失、通道长时间不更新。恢复通知只发给收到过该告警的渠道": "Critical = scan stuck or repeated scan errors; Warning = no data, data source lost, channel not updated for a long time. Recovery notices go only to channels that received the alert",
    "严重和警告都发": "Send critical and warning",
    "严重和警告都提醒": "Notify critical and warning",
    "中文": "中文",
    "串口": "Serial port",
    "串口列表": "Serial ports",
    "串口回环自检": "Serial loopback self-test",
    "串口扫描与诊断（RS-232 / RS-485 / RS-422）": "Serial scan & diagnosis (RS-232 / RS-485 / RS-422)",
    "串口收发 232/485": "Serial send/receive 232/485",
    "串口（例如 COM3）：": "Serial port (e.g. COM3):",
    "临时静默": "Temporary mute",
    "主机": "Host",
    "主机名": "Host name",
    "主板 / 内置串口": "Motherboard / built-in serial port",
    "仅排障用，用完请关闭": "Troubleshooting only; turn it off afterwards",
    "从": "From",
    "从备份还原": "Restore from backup",
    "任务栏闪烁 + Windows 通知气泡；失败时在窗口里弹出小提示框": "Flash the taskbar + Windows notification balloon; a small pop-up inside the window if that fails",
    "企业微信 / 钉钉 / 飞书群机器人的 Webhook 地址，或任意接收 JSON 的 https 地址；告警出现和恢复时各推送一次。钉钉机器人请在安全设置里加关键词\"色谱\"": "Webhook URL of a WeCom / DingTalk / Feishu group robot, or any https URL accepting JSON; one push when an alert appears and one when it recovers. For DingTalk, add the keyword \"色谱\" in the robot's security settings",
    "企业微信 / 钉钉 / 飞书群机器人的 Webhook 地址，或任意接收 JSON 的 https 地址；告警出现和恢复时各推送一次。钉钉机器人请在安全设置里加关键词“色谱”": "Webhook URL of a WeCom / DingTalk / Feishu group robot, or any https URL that accepts JSON; one push when an alert appears and one when it recovers. For DingTalk add the keyword \"色谱\" in the robot's security settings",
    "会话名": "Session name",
    "位": "bits",
    "位置：%s": "Location: %s",
    "例如 白班=08:00, 夜班=20:00；班次属于它开始的那一天": "e.g. Day=08:00, Night=20:00; a shift belongs to the day it starts",
    "依次填：用户名、密码（至少 8 位）、再输入一次": "Enter in order: user name, password (at least 8 characters), password again",
    "保存": "Save",
    "保存为默认参数": "Save as default parameters",
    "保存备份包": "Save backup package",
    "保存失败，配置文件没有改动：\n%s": "Save failed, the configuration file was not changed:\n%s",
    "保存失败：%s": "Save failed: %s",
    "保存密码": "Save password",
    "保存并重启服务": "Save and restart service",
    "保存报表设置": "Save report settings",
    "保存映射表": "Save map",
    "保存设置": "Save settings",
    "保存转发设置": "Save forwarding settings",
    "保持寄存器": "Holding registers",
    "保留不加密备用入口": "Keep an unencrypted fallback endpoint",
    "保留历史日志个数": "Number of old log files kept",
    "保留名": "reserved name",
    "保留时间(s)": "Retention time (s)",
    "倍福 ADS": "Beckhoff ADS",
    "值": "Value",
    "值（%s）": "Value (%s)",
    "停止": "Stop",
    "停止从站": "Stop slave",
    "停止位": "Stop bits",
    "停止位只能是 1 / 1.5 / 2": "Stop bits can only be 1 / 1.5 / 2",
    "停止位必须是数字": "Stop bits must be a number",
    "停止服务后 DCS/SCADA 将读不到数据。确定停止吗？": "After stopping the service DCS/SCADA cannot read data. Stop it?",
    "停止轮询": "Stop polling",
    "停用": "Disable",
    "偶 E": "Even E",
    "像 Modbus Poll 一样读/写设备数据：TCP 或 RTU，读 01/02/03/04，写 05/06/15/16（写之前会弹窗确认目标和数值）。寄存器按数据类型+字节序解码显示，每次通讯显示请求/响应报文和耗时。": "Read/write device data like Modbus Poll: TCP or RTU, read 01/02/03/04, write 05/06/15/16 (a confirmation shows target and values before writing). Registers are decoded by data type + byte order; every exchange shows request/response frames and time.",
    "像 Modbus Slave 一样模拟一台从站设备：TCP 或 RTU，四区数据（线圈 / 离散输入 / 保持寄存器 / 输入寄存器）可编辑，带通讯日志。只在点“启动从站”后才监听，默认不启动；监听地址见状态行。": "Simulate a slave device like Modbus Slave: TCP or RTU, four editable areas (coils / discrete inputs / holding registers / input registers) with a communication log. Listens only after \"Start slave\"; off by default; the listen address is shown in the status line.",
    "允许匿名只读连接": "Allow anonymous read-only connections",
    "允许的来源 IP / 网段": "Allowed source IPs / networks",
    "允许远程调用 ExportCSV": "Allow remote ExportCSV calls",
    "先把串口 TX 和 RX 短接（DB9 的 2、3 脚），用于确认串口和转换器本身是好的。RS-485 半双工不适用。": "Short TX and RX of the serial port first (DB9 pins 2 and 3) to confirm the port and converter work. Not applicable to RS-485 half duplex.",
    "先点下面的“保存设置”，再测试": "Click \"Save settings\" below first, then test",
    "先点底部“保存设置”，再测试": "Click \"Save settings\" at the bottom first, then test",
    "免登录的内网中继可留空": "May be left empty for an internal relay without login",
    "全双工": "Full duplex",
    "全部通道": "All channels",
    "共 %d 个峰": "%d peaks",
    "共 %d 条（显示最近 1000 条）": "%d entries (latest 1000 shown)",
    "共 %d 项、%d 个寄存器。地址列“Modbus地址”按 4xxxx（功能码 03）写法；用 04 读时对应 3xxxx。": "%d items, %d registers. \"Modbus address\" uses the 4xxxx notation (function 03); with function 04 it is 3xxxx.",
    "关闭": "Close",
    "关闭后打开窗口时不自动启动服务，需手动点“启动服务”（--settings 方式打开一律不自动启动）": "When off, the service does not start automatically when the window opens; click \"Start service\" manually (opening with --settings never starts it)",
    "关闭窗口会停止 OPC UA 服务，DCS/SCADA 将读不到数据。\n确定退出吗？": "Closing the window stops the OPC UA service and DCS/SCADA cannot read data.\nExit anyway?",
    "内容被改动（哈希不一致）": "content changed (hash mismatch)",
    "再输一次": "Repeat",
    "再输入一次": "Repeat password",
    "再输入一次密码：": "Enter the password again:",
    "写入值": "Value to write",
    "写入数据区": "Write data area",
    "写入（确认后执行）": "Write (after confirmation)",
    "出错：": "Error: ",
    "切换后立即生效并保存（desktop_language）。日志内容和 OPC UA 节点名不翻译，以免影响 DCS 组态；后台返回的个别诊断细节仍为中文。": "Takes effect immediately and is saved (desktop_language). Log contents and OPC UA node names are not translated so DCS configurations are unaffected; a few diagnostic details returned by the back end remain in Chinese.",
    "刚刚": "just now",
    "创建快捷方式失败（返回 %s）：%s\n可以手动操作：Win+R 输入 shell:startup，把桌面上的程序快捷方式复制进去。": "Failed to create the shortcut (returned %s): %s\nYou can do it manually: press Win+R, type shell:startup and copy the program shortcut from the desktop into it.",
    "创建快捷方式失败：%s": "Failed to create the shortcut: %s",
    "创建管理员": "Create administrator",
    "创建管理员…": "Create administrator…",
    "删除": "Delete",
    "删除快捷方式失败：%s\n%s": "Failed to delete the shortcut: %s\n%s",
    "删除所选": "Delete selected",
    "删除设备检测「%s」？": "Delete device check \"%s\"?",
    "删除选中账号": "Delete selected account",
    "删除选中静默时段": "Delete selected quiet period",
    "到": "To",
    "到期": "Expires",
    "到期时间（YYYY-MM-DD HH:MM，留空=不自动解除）": "Expiry (YYYY-MM-DD HH:MM, empty = never)",
    "刷新": "Refresh",
    "刷新串口列表": "Refresh serial ports",
    "功能码": "Function code",
    "功能码不对": "Invalid function code",
    "加为设备检测": "Add as device check",
    "加为设备检测：": "Add as device check:",
    "加入对比列表": "Add to comparison list",
    "加密参数异常。": "Abnormal encryption parameters.",
    "加密参数缺失或损坏。": "Encryption parameters missing or damaged.",
    "加密备份包的结构不对（多了或少了文件）。": "The encrypted backup has an invalid structure (extra or missing files).",
    "加密方式": "Encryption",
    "勾选后在当前用户的“启动”文件夹（shell:startup）放一个快捷方式，登录后自动打开本窗口；取消勾选即删除。这只是登录后打开可见的桌面窗口，不是后台服务，也不是计划任务。": "When ticked, a shortcut is placed in the current user's Startup folder (shell:startup) so this window opens after login; untick to delete it. It only opens the visible desktop window after login — not a background service and not a scheduled task.",
    "包含 .. 或 .": "contains .. or .",
    "包含反斜杠": "contains a backslash",
    "包含文字": "Contains text",
    "包含日志": "Include logs",
    "匿名（只读）": "Anonymous (read-only)",
    "十六进制": "Hexadecimal",
    "半双工": "Half duplex",
    "半峰宽(s)": "FWHM (s)",
    "单个 .mbd 上限（MB）": "Max size of one .mbd (MB)",
    "单个日志大小（MB）": "Size of one log file (MB)",
    "单曲线最大发布点数": "Max points published per curve",
    "单条谱图最大采样点": "Max samples per trace",
    "单次执行": "Run once",
    "厂商": "Vendor",
    "历史对比": "History",
    "原始值": "Raw",
    "原始值 %s～%s": "Raw %s–%s",
    "原始值（计数）": "Raw (counts)",
    "原始发送": "Raw send",
    "原始收发": "Raw send/receive",
    "反向解析主机名": "Reverse-resolve host names",
    "发件人地址": "Sender address",
    "发现方式": "Found by",
    "发送一条测试提醒": "Send a test notification",
    "发送内容（留空 = 只监听）": "Data to send (empty = listen only)",
    "发送文本（可选）": "Text to send (optional)",
    "发送测试数据": "Send test data",
    "发送测试消息": "Send test message",
    "发送测试邮件": "Send test e-mail",
    "叠加对比所选周期": "Overlay selected cycles",
    "只发严重告警": "Send critical alerts only",
    "只提醒严重告警": "Notify critical alerts only",
    "只能取到客户端地址（会话名、用户无法获取）": "Only client addresses are available (session names and users cannot be read)",
    "只读探测：Ping、TCP 连接后立即断开、Modbus 读功能码 01～04，不会写任何寄存器。扫描别人的网段前请先征得网络管理员同意（部分工控防火墙会把扫描当成攻击告警）。": "Read-only probing: Ping, TCP connect-and-close, Modbus read functions 01–04; no register is ever written. Ask the network administrator before scanning other networks (some industrial firewalls treat scans as attacks).",
    "只读检查，不会安装或修改任何东西：串口设备和驱动（CH340 / FTDI / CP210x / PL2303 等，含黄色感叹号设备）、运行库、防火墙服务、OPC UA 端口、数据目录、旧版本遗留的后台自启。现场装好后、或串口突然不能用时先跑一遍。": "Read-only check, installs or changes nothing: serial devices and drivers (CH340 / FTDI / CP210x / PL2303 etc., including devices with a yellow warning sign), runtimes, firewall service, OPC UA port, data folder, leftover auto-start entries of old versions. Run it after installation or when a serial port suddenly stops working.",
    "可用率": "Availability",
    "同时扫端口：": "Also scan ports:",
    "同时还原日志（放到 logs\\restored_时间）": "Also restore logs (into logs\\restored_<time>)",
    "名称": "Name",
    "名称，如 GC-1": "Name, e.g. GC-1",
    "否": "No",
    "启动与登录": "Start-up & login",
    "启动从站": "Start slave",
    "启动失败": "Start-up failed",
    "启动时最小化窗口": "Start the window minimized",
    "启动检测还没开始（服务启动约 8 秒后进行，或已在设置里关闭）。": "The start-up check has not run yet (it runs about 8 s after the service starts, or it is disabled in settings).",
    "启用": "Enable",
    "启用 Modbus TCP 转发": "Enable Modbus TCP forwarding",
    "启用/停用": "Enable/disable",
    "启用设置网页": "Enable settings web page",
    "告警": "Alerts",
    "告警与通知": "Alerts & notifications",
    "告警推送地址（可选）": "Alert push URL (optional)",
    "告警桌面提醒": "Desktop alert notifications",
    "告警设置": "Alert settings",
    "告警：%d 项（%s）    ": "Alerts: %d (%s)    ",
    "告警：无    ": "Alerts: none    ",
    "周一": "Mon",
    "周三": "Wed",
    "周二": "Tue",
    "周五": "Fri",
    "周六": "Sat",
    "周四": "Thu",
    "周日": "Sun",
    "周期": "cycles",
    "周期号": "Cycle ID",
    "周期号 %s": "Cycle %s",
    "周期时长（秒）": "Cycle duration (s)",
    "周期时间": "Cycle time",
    "周期时间 %s": "Cycle time %s",
    "噪声倍数": "Noise factor",
    "在上面依次选好每个目标的参数，点“加入对比列表”，最多 16 个；同一串口上的多个站会自动按顺序轮询（RS-485 不能同时通信），不同串口和网络目标真正并行。结束后会分析各目标的失败是否同时发生。": "Choose each target's parameters above and click \"Add to comparison list\", up to 16; several slaves on one serial port are polled in turn (RS-485 cannot talk simultaneously), different ports and network targets run in parallel. Afterwards the tool analyses whether failures happened at the same time.",
    "在曲线上标注峰": "Mark peaks on the curve",
    "在谱图预览中查看": "View in Chromatogram preview",
    "地址": "Address",
    "地址太多：最多一次扫描 %d 个": "Too many addresses: at most %d per scan",
    "地址太多：最多一次扫描 %d 个地址，请缩小范围（例如 /24 = 254 个）": "Too many addresses: at most %d per scan; narrow the range (e.g. /24 = 254 addresses)",
    "填入常用工业端口": "Fill in common industrial ports",
    "备份与审计": "Backup & audit",
    "备份内容太大（%s）：请不要包含日志，或先清理日志目录。": "Backup content too large (%s): leave out the logs or clean the log folder first.",
    "备份包": "Backup package",
    "备份包内外层信息不一致（%s），已拒绝。": "Inner and outer information of the backup do not match (%s); rejected.",
    "备份包内层没有 manifest.json。": "The inner backup has no manifest.json.",
    "备份包压缩比异常（%s），已拒绝。": "Abnormal compression ratio in the backup (%s); rejected.",
    "备份包太大。": "The backup package is too large.",
    "备份包太大（超过 %d MB）。": "The backup package is too large (over %d MB).",
    "备份包已加密": "The backup is encrypted",
    "备份包格式版本 %d 比本程序支持的 %d 新：请先升级程序再还原。": "Backup format version %d is newer than the supported %d: upgrade the program before restoring.",
    "备份包格式版本不对。": "Invalid backup format version.",
    "备份包结构不对。": "Invalid backup structure.",
    "备份包缺少文件：%s": "File missing from the backup: %s",
    "备份包解压后太大，已拒绝（可能是损坏或恶意文件）。": "The backup is too large when extracted; rejected (damaged or malicious file).",
    "备份包里有不安全的路径（%s）：%r，已拒绝还原。": "The backup contains an unsafe path (%s): %r; restore refused.",
    "备份包里有清单之外的文件：%s": "The backup contains files not in the manifest: %s",
    "备份包里有重复的文件：%s": "The backup contains a duplicate file: %s",
    "备份包里没有 config.json。": "The backup has no config.json.",
    "备份包里没有 manifest.json。": "The backup has no manifest.json.",
    "备份包里的文件大小与记录不符：%s": "File size in the backup does not match the record: %s",
    "备份包里的文件太多（%d 个）。": "Too many files in the backup (%d).",
    "备份包里的文件太大：%s": "File in the backup is too large: %s",
    "备份密码至少 8 个字符。": "The backup password must be at least 8 characters.",
    "备份时间：%s　来源电脑：%s　程序版本：%s": "Backup time: %s　Source PC: %s　Program version: %s",
    "备份来自另一台电脑（%s）。": "The backup comes from another PC (%s).",
    "备份来自另一台电脑（%s）：SMTP 密码是那台电脑加密的，这里无法解密，还原后会清空，请在“告警与通知”里重新填写。": "The backup comes from another PC (%s): its SMTP password was encrypted on that PC and cannot be decrypted here; it will be cleared — re-enter it on the \"Alerts & notifications\" tab.",
    "备份来自另一台电脑：SMTP 密码无法在本机解密，已清空，请在“告警与通知”里重新填写。": "The backup comes from another PC: the SMTP password cannot be decrypted here and has been cleared; re-enter it on the \"Alerts & notifications\" tab.",
    "备份来自旧版本 %s，缺少的新配置项会使用默认值。": "The backup comes from older version %s; new settings missing in it will use defaults.",
    "备份来自更新的程序版本 %s（当前 %s）：请先把程序升级到 %s 或以上再还原。": "The backup comes from a newer program version %s (current %s): upgrade to %s or later before restoring.",
    "备份来自过旧的版本 %r，无法还原。": "The backup comes from a version that is too old (%r) and cannot be restored.",
    "备份里包含证书私钥（%s），而你没有设置密码：\n拿到这个文件的人可以冒充本服务器。\n\n确定不加密导出吗？（建议勾选“用密码加密”）": "The backup contains certificate private keys (%s) and you did not set a password:\nanyone who gets this file can impersonate this server.\n\nExport without encryption anyway? (Ticking \"Encrypt with password\" is recommended)",
    "备份里含日志，本次不还原日志。": "The backup contains logs; logs are not restored this time.",
    "备份里的 config.json 不是 JSON 对象。": "config.json in the backup is not a JSON object.",
    "备份里的 config.json 不是有效的 JSON。": "config.json in the backup is not valid JSON.",
    "备份里的配置校验不通过：%s": "The configuration in the backup failed validation: %s",
    "备注": "Note",
    "备注，如 夜班": "Note, e.g. night shift",
    "备注，如 大修": "Note, e.g. overhaul",
    "多个用逗号隔开；留空 = 不发邮件。告警出现和恢复时各发一封，每小时最多 20 封": "Separate several with commas; empty = no e-mail. One e-mail when an alert appears and one when it recovers, at most 20 per hour",
    "多久扫描一次数据目录": "How often the data folder is scanned",
    "多目标对比": "Multi-target comparison",
    "多目标对比至少需要 2 个目标（只有 1 个请直接点“开始稳定性测试”）。": "A multi-target comparison needs at least 2 targets (for one target click \"Start stability test\").",
    "多目标对比：在上面依次选好每个目标的参数，点“加入对比列表”，最多 16 个；同一串口上的多个站会自动按顺序轮询（RS-485 不能同时通信），不同串口和网络目标真正并行。结束后会分析各目标的失败是否同时发生。": "Multi-target comparison: choose each target's parameters above and click \"Add to comparison list\", up to 16; several slaves on one serial port are polled in turn (RS-485 cannot talk simultaneously), different ports and network targets run in parallel. Afterwards the tool analyses whether failures happened at the same time.",
    "多目标稳定性测试 · %d 个目标 · %s · %s / %s · 每 %s 秒": "Multi-target stability test · %d targets · %s · %s / %s · every %s s",
    "失败": "Failed",
    "失败 %d/%d": "Failed %d/%d",
    "失败次数太多，请 1 分钟后再试。": "Too many failed attempts; try again in 1 minute.",
    "失败率": "Failure rate",
    "失败：%s": "Failed: %s",
    "奇 O": "Odd O",
    "如 DCS 服务器": "e.g. DCS server",
    "字段": "Field",
    "字节序": "Byte order",
    "字节序只能是：%s": "Byte order can only be: %s",
    "存放 *_V3.db / *.mbd 的文件夹（色谱工作站 DataBrowser 的数据目录）。选择后立即保存；服务运行中会在下一轮扫描（默认 30 秒内）自动生效，无需重启。": "Folder holding *_V3.db / *.mbd (the DataBrowser data folder of the chromatography workstation). Saved immediately when chosen; while the service runs it takes effect at the next scan (within 30 s by default), no restart needed.",
    "存放 *_V3.db / *.mbd 的文件夹，必须真实存在，例如 D:\\GCP\\DataBrowser": "Folder holding *_V3.db / *.mbd; it must exist, e.g. D:\\GCP\\DataBrowser",
    "安全体检": "Security check",
    "安全提示": "Security notice",
    "安全警告": "Security warning",
    "安全设置…": "Security settings…",
    "完成": "Done",
    "定期复查（小时）": "Re-check every (hours)",
    "审计日志完整：哈希链校验通过（%d 条）。": "The audit log is intact: hash chain verified (%d entries).",
    "审计日志被改动过！": "The audit log has been altered!",
    "审计日志：logs\\audit.log（每行带哈希链，删改任何一行都能被“校验防篡改”发现）": "Audit log: logs\\audit.log (every line is hash-chained; \"Verify integrity\" detects any deleted or edited line)",
    "客户端地址": "Client address",
    "客户端连接": "Clients",
    "客户端连接：%s": "Client connections: %s",
    "寄存器": "Registers",
    "寄存器映射表": "Register map",
    "密码": "Password",
    "密码 / 授权码": "Password / authorization code",
    "密码已加密保存在配置文件里。QQ/163/企业邮箱请填“授权码”。": "The password is stored encrypted in the configuration file. For QQ/163/corporate mail use the \"authorization code\".",
    "密码错误，或备份包已被改动（解密校验失败）。": "Wrong password, or the backup has been altered (decryption check failed).",
    "密码（可选）": "Password (optional)",
    "密码（至少 8 位）": "Password (at least 8 characters)",
    "密码（至少 8 位）：": "Password (at least 8 characters):",
    "密码：": "Password:",
    "对比汇总 CSV：logs/stability/%s": "Comparison summary CSV: logs/stability/%s",
    "对象 / 修改": "Target / change",
    "导出 CSV": "Export CSV",
    "导出中…": "Exporting…",
    "导出到": "Export to",
    "导出备份…": "Export backup…",
    "导出失败：%s": "Export failed: %s",
    "导出峰表 CSV": "Export peak table CSV",
    "导出所选周期 CSV": "Export selected cycles to CSV",
    "导出文件夹还不存在（先导出一次）：%s": "The export folder does not exist yet (export once first): %s",
    "导出映射表 CSV（给 DCS 组态）": "Export map CSV (for DCS configuration)",
    "导出目录": "Export folder",
    "导出配置备份": "Export configuration backup",
    "将向 %s 站号 %d 地址 %d 执行%s：\n值 %s\n\n确认写入吗？": "About to write to %s unit %d address %d using %s:\nvalues %s\n\nWrite now?",
    "将要执行的操作（%d 项）：": "Operations to perform (%d):",
    "将覆盖 / 新增 / 删除 %d 个文件（见预览）。还原前会自动备份当前配置。\n确定还原吗？": "%d files will be overwritten / added / deleted (see preview). The current configuration is backed up automatically first.\nRestore now?",
    "将还原证书私钥：还原后 OPC UA 客户端看到的服务端证书会变成备份里的那一个。": "Certificate private keys will be restored: OPC UA clients will then see the server certificate from the backup.",
    "尚未完成": "Not finished yet",
    "屏蔽告警": "Mute alerts",
    "屏蔽该通道的告警": "Mute alerts for this channel",
    "峰个数": "Peak count",
    "峰表已导出：%s": "Peak table exported: %s",
    "峰识别与峰表": "Peak detection & peak table",
    "峰识别参数已保存（%d 项改动）。服务运行中下一轮扫描起按新参数计算。": "Peak detection parameters saved (%d changes). While the service runs, the new parameters apply from the next scan.",
    "峰面积": "Peak area",
    "峰面积合计": "Total peak area",
    "峰高": "Peak height",
    "工程师": "Engineer",
    "工程师（改配置、诊断、导出）": "Engineer (change settings, diagnose, export)",
    "已保存": "Saved",
    "已保存 %d 项。": "Saved %d item(s).",
    "已保存，立即生效。": "Saved; effective immediately.",
    "已停止（默认不监听）": "Stopped (not listening by default)",
    "已写入%s 地址 %d 起 %d 个位：%s": "Wrote %s at address %d, %d bit(s): %s",
    "已写入%s 地址 %d 起 %d 个寄存器：%s": "Wrote %s at address %d, %d register(s): %s",
    "已创建管理员 %s 并登录。\n同一个账号也可以登录 OPC UA（只读）。": "Administrator %s created and logged in.\nThe same account can also log in to OPC UA (read-only).",
    "已加入对比列表，共 %d 个。": "Added to the comparison list, %d in total.",
    "已加密": "encrypted",
    "已发送测试提醒（方式：%s）。没看到的话，检查 Windows“通知”设置里是否允许通知。": "Test notification sent (method: %s). If you did not see it, check that notifications are allowed in Windows settings.",
    "已取消“登录后自动打开窗口”，已删除：%s": "\"Open window after login\" cancelled; removed: %s",
    "已启用，服务未运行或端口被占用（看运行日志）": "Enabled; service not running or port in use (see the log)",
    "已完成": "Finished",
    "已导出 %d 个周期：%s": "Exported %d cycle(s): %s",
    "已导出 %d 条谱图到：\n%s": "Exported %d trace(s) to:\n%s",
    "已导出 %d 条：%s": "Exported %d entries: %s",
    "已导出 %d 行：\n%s": "Exported %d rows:\n%s",
    "已导出备份：%s\n%d 个文件，%s。": "Backup exported: %s\n%d files, %s.",
    "已屏蔽告警": "Alerts muted",
    "已开启“登录后自动打开窗口”：登录 Windows 后会自动打开本程序的桌面窗口（不是后台服务）。\n快捷方式：%s": "\"Open window after login\" enabled: the desktop window of this program opens automatically after you log in to Windows (not a background service).\nShortcut: %s",
    "已手动停止": "Stopped manually",
    "已把「%s」的参数填到连接测试里，改完点“加为设备检测”保存%s。": "The parameters of \"%s\" were filled into the connection test; adjust and click \"Add as device check\" to save%s.",
    "已设置（不显示）": "Set (hidden)",
    "已还原 %d 个文件。还原前的配置已备份到：%s": "Restored %d file(s). The previous configuration was backed up to: %s",
    "已连接": "Connected",
    "已锁定（%s）：请重新登录。": "Locked (%s): please log in again.",
    "常用设置": "Quick settings",
    "常用配置": "Common settings",
    "常见服务": "Common service",
    "平均": "Avg",
    "平滑点数": "Smoothing points",
    "并发数": "Concurrency",
    "序号": "No.",
    "应用": "App",
    "应答": "Responded",
    "应答正确": "Correct response",
    "应答正确（%s）": "Correct response (%s)",
    "应答（异常）": "Response (exception)",
    "延迟(ms)": "Latency (ms)",
    "开始/结束填 HH:MM；结束早于开始表示跨午夜，例如 22:00～06:00": "Start/end as HH:MM; an end before the start means across midnight, e.g. 22:00–06:00",
    "开始多目标测试": "Start multi-target test",
    "开始导出": "Start export",
    "开始扫描": "Start scan",
    "开始检测": "Run check",
    "开始测试": "Start test",
    "开始稳定性测试": "Start stability test",
    "开始轮询": "Start polling",
    "开放端口": "Open ports",
    "异常码 %d：%s": "Exception code %d: %s",
    "当前 endpoint 格式不对（%r），请在“系统设置”里直接改 endpoint。": "The current endpoint has an invalid format (%r); edit the endpoint directly on the \"System settings\" tab.",
    "当前告警数": "Active alerts",
    "当前数据目录：\n  ": "Current data folders:\n  ",
    "当前数据目录：（未设置）—— 请点“选择数据目录…”": "Current data folder: (not set) — click \"Choose data folder…\"",
    "当前没有客户端连接": "No clients connected",
    "当前用户：%s（%s）": "Current user: %s (%s)",
    "当前谱图没有峰可导出。": "The current chromatogram has no peaks to export.",
    "当前连接数：%d / 上限 %s": "Connections: %d / limit %s",
    "待检测": "Pending",
    "微伏": "Microvolts",
    "微伏（µV）": "Microvolts (µV)",
    "快捷方式位置：%s": "Shortcut location: %s",
    "恢复": "Recovered",
    "恢复为已保存的值": "Revert to saved values",
    "成功": "Succeeded",
    "手动锁定": "locked manually",
    "打包 config.json（含账号密码哈希）、certs 下的 OPC UA 与网页证书和私钥、信任的客户端证书，可选日志。备份里有私钥，强烈建议设置密码加密（AES-256-GCM）。": "Packs config.json (including account password hashes), the OPC UA and web certificates and private keys under certs, trusted client certificates and optionally the logs. The backup contains private keys — encrypting it with a password (AES-256-GCM) is strongly recommended.",
    "打开导出文件夹": "Open export folder",
    "打开报表文件夹": "Open report folder",
    "打开数据目录": "Open data folder",
    "打开日志文件": "Open log file",
    "打开日志文件夹": "Open log folder",
    "打开明细 CSV": "Open detail CSV",
    "打开窗口后自动启动服务": "Start the service when the window opens",
    "打开网页设置页": "Open settings web page",
    "托盘气泡": "Tray balloon",
    "扫描 %d/%d 个地址，发现 %d 台在线主机%s": "Scanned %d/%d addresses, found %d online host(s)%s",
    "扫描串口": "Scan serial ports",
    "扫描周期（秒）": "Scan interval (s)",
    "扫描站号从": "Scan slave IDs from",
    "找不到备份文件：%s": "Backup file not found: %s",
    "找不到当前用户的“启动”文件夹（没有 APPDATA 环境变量）。": "Cannot find the current user's Startup folder (no APPDATA environment variable).",
    "找不到数据目录：%s": "Data folder not found: %s",
    "找到 %d 个周期（库中共 %d 个，%.1f MB，最早 %s）": "Found %d cycle(s) (%d in the database, %.1f MB, earliest %s)",
    "找到串口参数：%d 波特，校验 %s（%s），停止位 %g，站号 %d 有应答": "Serial settings found: %d baud, parity %s (%s), stop bits %g, slave %d responded",
    "找峰失败：%s": "Peak detection failed: %s",
    "把数据目录里的谱图导出为 CSV：index.csv（总清单）+ csv\\（每条谱图一个文件：时间、原始计数、微伏、显示值）。每次导出放在带时间的子文件夹里，不覆盖上次结果。读取的是数据库副本，不影响 DataBrowser 运行；服务开着或停着都可以导出。": "Export the chromatograms in the data folder as CSV: index.csv (overview) + csv\\ (one file per trace: time, raw counts, microvolts, display value). Each export goes into a time-stamped sub-folder and never overwrites the previous one. A copy of the database is read, so DataBrowser is not affected; works whether the service is running or not.",
    "把每个通道的周期号、点数、心跳、告警数、峰面积等写成 Modbus 寄存器，供只支持 Modbus 的 DCS 读取。只支持功能码 03 / 04（读），其它功能码返回异常码 01，DCS 无法写入。随服务启停。": "Writes each channel's cycle ID, point count, heartbeat, alert count, peak areas etc. into Modbus registers for DCS that only speak Modbus. Only functions 03 / 04 (read) are supported; any other function returns exception 01, so the DCS cannot write. Starts and stops with the service.",
    "抖动": "Jitter",
    "报表目录": "Report folder",
    "报表设置已保存，立即生效。": "Report settings saved; effective immediately.",
    "报表：%s（%s ～ %s）": "Report: %s (%s – %s)",
    "按参数重新找峰": "Re-detect with these parameters",
    "按当前通道自动生成": "Generate from current channels",
    "按日": "Daily",
    "按日或按班统计：通道状态、周期数、告警次数与持续时长、服务在线时长、客户端连接数峰值。数据来自服务运行期间记录的历史库。导出 .xlsx（Excel / WPS 直接打开）和 .html（浏览器打印成 PDF）。": "Daily or per-shift statistics: channel status, cycle counts, alert count and duration, service uptime, peak number of clients. Data come from the history database recorded while the service runs. Exports .xlsx (opens in Excel / WPS) and .html (print to PDF from the browser).",
    "按班": "Per shift",
    "排查建议": "Troubleshooting tips",
    "排查建议：": "Troubleshooting tips:",
    "排查：站号是否正确（不确定可先用“站号扫描”）、RS-485 A/B 是否接反、是否需要勾选 RTS 方向控制、设备是不是 Modbus RTU 协议；收到乱码多半是波特率或校验位不对。": "Check: is the slave ID right (use \"Slave scan\" if unsure), are RS-485 A/B swapped, is RTS direction control needed, is the device really Modbus RTU; garbage usually means wrong baud rate or parity.",
    "接口类型": "Interface type",
    "接收等待(秒)": "Receive wait (s)",
    "推送静默中": "Notifications muted",
    "推送静默中，至 %s%s": "Notifications muted until %s%s",
    "描述": "Description",
    "提示": "Information",
    "提示：": "Tip: ",
    "提醒哪些级别": "Levels to notify",
    "操作": "Action",
    "操作员": "Operator",
    "操作员（只看）": "Operator (view only)",
    "操作审计日志": "Operation audit log",
    "收件人": "Recipients",
    "收到乱码 / CRC 错": "Garbage / CRC error",
    "放弃修改": "Discard changes",
    "数据": "Data",
    "数据与导出": "Data & export",
    "数据传输检测": "Transmission check",
    "数据位": "Data bits",
    "数据区": "Data area",
    "数据有效（1 有效 / 0 数据源消失）": "Data valid (1 valid / 0 source lost)",
    "数据源": "Data source",
    "数据源消失或读不了": "Data source missing or unreadable",
    "数据目录": "Data folder",
    "数据目录已保存，共找到 %d 个数据文件。%s": "Data folder saved, %d data file(s) found. %s",
    "数据类型": "Data type",
    "数据类型只能是：%s": "Data type can only be: %s",
    "数量": "Count",
    "文件校验失败（SHA-256 不一致，可能被改动或损坏）：%s": "File verification failed (SHA-256 mismatch, possibly altered or damaged): %s",
    "文件：": "Files:",
    "新告警时桌面提醒": "Desktop notification for new alerts",
    "新增": "New",
    "方式": "Method",
    "无 N": "None N",
    "无周期": "No cycles",
    "无应答": "No response",
    "无新周期告警（分钟）": "No-new-cycle alert (minutes)",
    "无新周期告警（分钟，留空=默认）": "No-new-cycle alert (minutes, empty = default)",
    "无法完成": "Cannot complete",
    "无法获取（%s）": "Unavailable (%s)",
    "无法获取（当前 asyncua 版本不提供会话列表）": "Unavailable (this asyncua version provides no session list)",
    "无法读取 %s：%s": "Cannot read %s: %s",
    "无需参数，直接点“开始测试”。": "No parameters needed; just click \"Start test\".",
    "日志文件还没有生成：%s": "The log file has not been created yet: %s",
    "日期从": "Date from",
    "日期格式不对（写成 2026-11-26）：%s": "Invalid date format (use 2026-11-26): %s",
    "日期格式不对：%s（写成 2026-11-26）": "Invalid date format: %s (use 2026-11-26)",
    "时长": "Duration",
    "时长 %s": "Duration %s",
    "时长(s)": "Duration (s)",
    "时间": "Time",
    "时间（%s）": "Time (%s)",
    "明细": "Detail",
    "明细 CSV 在：%s": "Detail CSV at: %s",
    "明细 CSV：logs/stability/%s（%s）": "Detail CSV: logs/stability/%s (%s)",
    "星期（都不勾 = 每天）：": "Weekdays (none ticked = every day):",
    "映射表已保存（%d 项）。服务运行中下一轮扫描起按新映射输出。": "Register map saved (%d items). While the service runs, the new map is used from the next scan.",
    "映射表已导出：%s": "Register map exported: %s",
    "映射表是空的：先“按当前通道自动生成”或添加。": "The register map is empty: \"Generate from current channels\" or add items first.",
    "是": "Yes",
    "智能诊断": "Smart diagnosis",
    "最后周期": "Last cycle",
    "最后更新": "Last update",
    "最多 16 个目标。": "At most 16 targets.",
    "最大": "Max",
    "最大连接数": "Max connections",
    "最小": "Min",
    "最小 %s，平均 %s，P95 %s，P99 %s，最大 %s，抖动 %s": "min %s, avg %s, P95 %s, P99 %s, max %s, jitter %s",
    "最小半峰宽(s)": "Min FWHM (s)",
    "最小峰高": "Min peak height",
    "最常改的几项集中在这里。保存时用与服务启动完全相同的规则校验，不合法不会写入并说明原因；保存后会告诉你哪些自动生效、哪些需要重启服务。其余设置在“告警与通知”“系统设置”页。": "The most frequently changed settings are collected here. Saving validates with exactly the same rules as service start-up; invalid values are not written and the reason is shown. After saving you are told what applies automatically and what needs a service restart. Other settings are on the \"Alerts & notifications\" and \"System settings\" tabs.",
    "最新周期时间": "Latest cycle time",
    "最近扫描 %s · %s 个通道": "Last scan %s · %s channel(s)",
    "最近的告警记录": "Recent alert history",
    "最近结果": "Last result",
    "最长 31 天": "at most 31 days",
    "最长连续失败": "Longest failure run",
    "最高告警级别（0 正常 / 1 警告 / 2 严重）": "Highest alert level (0 OK / 1 warning / 2 critical)",
    "有 %d 项修改未保存": "%d unsaved change(s)",
    "有告警": "Alerting",
    "服务": "Service",
    "服务启动中……": "Service starting…",
    "服务启动后约 8 秒自动检测；“问题”级别的项会告警（警告级），复查时消失自动恢复；“注意”只记日志": "Checked automatically about 8 s after the service starts; \"problem\" items raise a warning alert that clears when a re-check passes; \"note\" items are only logged",
    "服务启动时自动检测": "Check automatically when the service starts",
    "服务器名称": "Server name",
    "服务器地址": "Server address",
    "服务在线 %s（%.1f%%），启动 %d 次；周期 %d 个；告警 %d 次，累计 %s；客户端连接数峰值 %d": "Service online %s (%.1f%%), started %d time(s); %d cycle(s); %d alert(s), total %s; peak clients %d",
    "服务心跳（每轮扫描 +1，0～65535 循环）": "Service heartbeat (+1 every scan, wraps 0–65535)",
    "服务未运行": "Service not running",
    "服务未运行（直接读自数据目录，未发布）": "Service not running (read directly from the data folder, not published)",
    "服务未运行，也还没有设置数据目录（“数据与导出”页里选择）": "The service is not running and no data folder is set yet (choose one on the \"Data & export\" tab)",
    "服务未运行，看不到通道。启动服务后这里会列出所有通道。": "The service is not running, so no channels are visible. Start the service and all channels will be listed here.",
    "服务未运行：没有客户端连接": "Service not running: no client connections",
    "服务未运行：没有客户端连接（点窗口上方“启动服务”）": "Service not running: no client connections (click \"Start service\" at the top)",
    "服务未运行：直接从数据目录读取（%s，%d 个通道）": "Service not running: reading directly from the data folder (%s, %d channel(s))",
    "服务未运行：自动检测只在服务运行时进行，可以点“开始检测”手动检查。": "Service not running: automatic checks only run while the service is running; click \"Run check\" to check manually.",
    "服务运行中：显示服务正在发布的最新快照（已更新 %s 次）": "Service running: showing the latest snapshot being published (updated %s times)",
    "服务运行中：等待第一轮扫描完成……": "Service running: waiting for the first scan to finish…",
    "服务运行时按间隔自动检测关键设备（DCS、网关、分析仪等），连续失败达到次数就告警，一次成功自动恢复；告警走上面设置的群机器人 / 邮件，遵守静默时段和渠道级别。添加方法：在下面“连接测试”里选好类型和参数、测通后，点“加为设备检测”。": "While the service runs, key devices (DCS, gateways, analysers…) are checked at intervals; an alert is raised after the configured number of consecutive failures and clears after one success. Alerts use the robot / e-mail set above and respect quiet periods and levels. To add one: pick type and parameters in \"Connection test\" below, test it, then click \"Add as device check\".",
    "服务运行时按间隔自动检测关键设备（DCS、网关、分析仪等），连续失败达到次数就告警，一次成功自动恢复；告警走告警与通知页设置的群机器人 / 邮件，遵守静默时段和渠道级别。添加方法：在“诊断工具”的连接测试里选好类型和参数、测通后，点“加为设备检测”。": "While the service runs, key devices (DCS, gateways, analysers…) are checked at intervals; an alert is raised after the configured number of consecutive failures and clears after one success. Alerts use the robot / e-mail settings of the Alerts tab and respect quiet periods and levels. To add one: pick type and parameters in the connection test on the \"Diagnostics\" tab, test it, then click \"Add as device check\".",
    "服务运行期间每个新周期都会存入 history\\history.db；在这里按日期和通道查询。": "Every new cycle is stored in history\\history.db while the service runs; query it here by date and channel.",
    "未加密": "not encrypted",
    "未启用": "Disabled",
    "未指定证书时自动生成自签名证书；浏览器首次会提示\"不安全\"，核对页面顶部显示的指纹一致后继续即可": "Without a certificate a self-signed one is generated; the browser warns \"not secure\" the first time — continue after checking that it matches the fingerprint shown at the top of the page",
    "未指定证书时自动生成自签名证书；浏览器首次会提示“不安全”，核对指纹一致后继续即可": "Without a certificate a self-signed one is generated; the browser warns \"not secure\" the first time — continue after checking that the fingerprint matches",
    "未登录：操作员（只读）。修改配置、诊断、导出请先登录。": "Not logged in: operator (read-only). Log in to change settings, diagnose or export.",
    "未知": "Unknown",
    "未知芯片": "Unknown chip",
    "未设置": "Not set",
    "本机网卡": "Local network adapters",
    "本页面已使用 HTTPS 加密": "This page uses HTTPS",
    "本页面未使用 HTTPS（仅本机访问时无影响）": "This page does not use HTTPS (harmless for local access only)",
    "来源": "Source",
    "查看告警": "View alerts",
    "查看数据源": "View data sources",
    "查询": "Query",
    "校验": "Parity",
    "校验 N/E/O": "Parity N/E/O",
    "校验位只能是 N（无）/ E（偶）/ O（奇）": "Parity can only be N (none) / E (even) / O (odd)",
    "校验防篡改": "Verify integrity",
    "格式": "Format",
    "格式：开始/结束填 YYYY-MM-DD HH:MM，第三框备注（如 大修）；最长 31 天": "Format: start/end as YYYY-MM-DD HH:MM, the third box is a note (e.g. overhaul); at most 31 days",
    "桌面登录已关闭：所有操作都可用（可在“备份与审计 → 账号与角色”开启）": "Desktop login is off: every operation is available (turn it on under \"Backup & audit → Accounts & roles\")",
    "桌面窗口是本机默认入口；网页仍可开启供远程访问": "The desktop window is the local entry point; the web page can still be enabled for remote access",
    "桌面窗口是本机默认入口；网页供浏览器/远程查看": "The desktop window is the local entry point; the web page is for browser/remote viewing",
    "桌面窗口设置，已立即生效：%s。": "Desktop window settings, effective immediately: %s.",
    "桌面窗口需要登录（推荐）": "Require login for the desktop window (recommended)",
    "检测": "Check",
    "检测中（约 10～30 秒）…": "Checking (about 10–30 s)…",
    "检测器": "Detector",
    "检测失败": "Check failed",
    "次": "times",
    "次告警": "alerts",
    "次数": "Times",
    "欧姆龙 FINS": "Omron FINS",
    "正在停止……": "Stopping…",
    "正在停止服务……": "Stopping the service…",
    "正在导出到 %s …": "Exporting to %s …",
    "正在读取……": "Reading…",
    "正在读取数据源（数据库较大时需要几十秒）…": "Reading data sources (large databases may take tens of seconds)…",
    "正在进行启动检测…": "Running the start-up check…",
    "正常": "OK",
    "每 %s 秒，连续 %s 次失败→%s": "every %s s, %s consecutive failures → %s",
    "每 20 次写盘一次": "flushed to disk every 20 tests",
    "每 5 秒自动刷新。服务运行中显示正在发布的数据和告警；服务没运行时直接读数据目录（不发布）。双击一行打开该通道的谱图曲线。": "Refreshed every 5 s. While the service runs, the published data and alerts are shown; otherwise the data folder is read directly (not published). Double-click a row to open that channel's chromatogram.",
    "每 5 秒自动刷新。连接时间为本窗口第一次看到该连接的时刻；连接数达到上限（系统设置 → 最大连接数）后新的客户端会被拒绝。会话信息取自 OPC UA 服务端内部，个别 asyncua 版本取不到时显示“无法获取”。": "Refreshed every 5 s. Connected time is when this window first saw the connection; when the limit (System settings → Max connections) is reached new clients are refused. Session details come from the OPC UA server internals; \"Unavailable\" is shown if this asyncua version does not expose them.",
    "每个流路只导出最近 N 个周期": "Export only the last N cycles per stream",
    "每区大小": "Size per area",
    "每天自动生成": "Generate daily",
    "每天重复时段": "Daily window",
    "每次超时(秒)": "Timeout per attempt (s)",
    "每流路最近周期数": "Recent cycles per stream",
    "每流路读取最近周期数": "Recent cycles read per stream",
    "每班自动生成": "Generate per shift",
    "每通道寄存器数": "Registers per channel",
    "每通道峰数": "Peaks per channel",
    "没有保存，请改正：\n": "Not saved, please correct:\n",
    "没有发现开放端口": "No open ports found",
    "没有可画的数据": "No data to plot",
    "没有导出任何谱图（数据目录里没有可解析的谱图）。详情见导出区的过程信息。": "No traces exported (no parsable chromatograms in the data folder). See the progress text in the export area.",
    "没有找到串口：USB 转换器是否插好、驱动是否安装（设备管理器 → 端口）": "No serial port found: is the USB converter plugged in and its driver installed (Device Manager → Ports)?",
    "没有找到可备份的文件（配置文件不存在）。": "Nothing to back up (the configuration file does not exist).",
    "没有找到峰（可调低噪声倍数或最小峰高）": "No peaks found (try a lower noise factor or minimum peak height)",
    "没有改动。": "No changes.",
    "没有未保存的修改": "No unsaved changes",
    "没有这个账号：%s": "No such account: %s",
    "没有选择文件夹。": "No folder selected.",
    "没有通道数据：%s": "No channel data: %s",
    "波特率": "Baud rate",
    "波特率探测": "Baud rate detection",
    "波特率探测中（%s，站号 %s）……": "Detecting baud rate (%s, slave %s)…",
    "波特率探测依次尝试 1200～115200 × 无/偶/奇校验，收到 CRC 正确的应答（含 Modbus 异常应答）即判定参数正确；站号扫描按上面的串口参数逐个站号发读请求（1～247 全扫约需 1～2 分钟）。": "Baud rate detection tries 1200–115200 × none/even/odd parity; a response with a correct CRC (including a Modbus exception) means the parameters are right. Slave scan sends a read request to each slave ID with the serial settings above (a full 1–247 scan takes about 1–2 minutes).",
    "波特率探测完成": "Baud rate detection finished",
    "波特率探测：%d,%s,%g（%d/%d）": "Baud rate detection: %d,%s,%g (%d/%d)",
    "注意": "Attention",
    "注销": "Log out",
    "活动": "Active",
    "流路": "Stream",
    "测试中…": "Testing…",
    "测试失败": "Test failed",
    "测试提醒没有发出去，详见运行日志。": "The test notification could not be delivered; see the log.",
    "测试次数": "Tests",
    "测试次数 %s，成功 %s，失败 %s，失败率 %s，最长连续失败 %s%s": "%s tests, %s succeeded, %s failed, failure rate %s, longest failure run %s%s",
    "浏览…": "Browse…",
    "添加": "Add",
    "添加 / 重设": "Add / reset",
    "添加目录…": "Add folder…",
    "清空列表": "Clear list",
    "清空显示": "Clear display",
    "清除": "Clear",
    "点数": "Points",
    "点数 %s": "Points %s",
    "状态": "Status",
    "状态 / 告警": "Status / alerts",
    "环回测试": "Loopback test",
    "现在重启服务……": "Restarting the service now…",
    "现场排查用：网络（Ping / TCP 端口 / DNS / OPC UA / Modbus TCP）和串口（RS-232 / RS-485 / RS-422，Modbus RTU）。测试在": "For on-site troubleshooting: network (Ping / TCP port / DNS / OPC UA / Modbus TCP) and serial (RS-232 / RS-485 / RS-422, Modbus RTU). Tests run on",
    "现场排查用：网络（Ping / TCP 端口 / DNS / OPC UA / Modbus TCP）和串口（RS-232 / RS-485 / RS-422，Modbus RTU）。测试在本机执行。": "For on-site troubleshooting: network (Ping / TCP port / DNS / OPC UA / Modbus TCP) and serial (RS-232 / RS-485 / RS-422, Modbus RTU). Tests run on this PC.",
    "班次（名称=开始时间）": "Shifts (name=start time)",
    "生成报表": "Generate report",
    "用上面同样的参数反复测试，统计丢包率、延迟和错误。支持 Ping / TCP 端口 / DNS / OPC UA / Modbus TCP / Modbus RTU / 串口收发；关掉网页不影响，关闭桌面主窗口会停止测试。": "Repeat the test above with the same parameters and collect loss rate, latency and errors. Supports Ping / TCP port / DNS / OPC UA / Modbus TCP / Modbus RTU / serial send-receive; closing the web page does not matter, closing the desktop window stops the test.",
    "用上面同样的参数反复测试，统计丢包率、延迟和错误。支持 Ping / TCP 端口 / DNS / OPC UA / Modbus TCP / Modbus RTU / 串口收发；测试期间请保持窗口打开（关闭窗口会停止测试）。": "Repeat the test above with the same parameters and collect loss rate, latency and errors. Supports Ping / TCP port / DNS / OPC UA / Modbus TCP / Modbus RTU / serial send-receive; keep the window open during the test (closing it stops the test).",
    "用密码加密": "Encrypt with password",
    "用户": "User",
    "用户名": "User name",
    "用户名或密码不对。": "Wrong user name or password.",
    "用户名（可选）": "User name (optional)",
    "用户名：": "User name:",
    "由本窗口里的定时器完成（每分钟检查一次，上一个日 / 班结束后生成），不建 Windows 计划任务；窗口关闭期间不会生成。": "Done by a timer inside this window (checked every minute; generated after the previous day / shift ends); no Windows scheduled task is created, and nothing is generated while the window is closed.",
    "界面语言": "Interface language",
    "留空 = 全部导出（数据多时较慢）": "Empty = export everything (slow with a lot of data)",
    "留空 = 用户名": "Empty = user name",
    "留空 = 自动生成": "Empty = generate automatically",
    "登录": "Log in",
    "登录 Windows 后自动打开窗口": "Open the window after logging in to Windows",
    "登录账号": "Login accounts",
    "监听地址": "Listen address",
    "监听地址 endpoint": "Listen endpoint",
    "监听端口": "Listen port",
    "监控目录（每行一个）": "Watched folders (one per line)",
    "目录里没有找到 *_V3.db 或 .mbd 数据文件：%s": "No *_V3.db or .mbd data file found in: %s",
    "目标": "Target",
    "目标%d": "Target %d",
    "目标地址": "Target address",
    "相对路径按程序目录解析": "Relative paths are resolved against the program folder",
    "确定删除账号 %s 吗？": "Delete account %s?",
    "确定清除 SMTP 密码吗？": "Clear the SMTP password?",
    "确定要对外开放吗？": "Open it to the network anyway?",
    "确认写入": "Confirm write",
    "确认还原": "Confirm restore",
    "离散输入": "Discrete inputs",
    "积分终点(s)": "Integration end (s)",
    "积分起点(s)": "Integration start (s)",
    "移除": "Remove",
    "空文件名": "empty file name",
    "空闲 %d 分钟自动锁定": "auto-locked after %d idle minutes",
    "窗口内提示框": "In-window pop-up",
    "窗口太小": "Window too small",
    "立即刷新": "Refresh now",
    "立即检测": "Check now",
    "立即结束静默": "End mute now",
    "站号": "Slave ID",
    "站号 %s 收到乱码或不匹配的应答：可能多个设备同站号冲突、线路干扰或终端电阻问题。": "Slave(s) %s returned garbage or mismatched responses: possibly duplicate slave IDs, line noise or termination problems.",
    "站号扫描": "Slave scan",
    "站号扫描 %d/%d（已发现 %d 个）": "Slave scan %d/%d (%d found)",
    "站号扫描中（%s %s,%s,%s，站号 %s～%s）……": "Scanning slave IDs (%s %s,%s,%s, IDs %s–%s)…",
    "站号范围 1～247，且起始不能大于结束": "Slave ID range 1–247 and the start must not exceed the end",
    "站号范围必须是数字": "Slave ID range must be numeric",
    "站号（0 = 任意）": "Unit ID (0 = any)",
    "站点": "Station",
    "站点 %s": "Station %s",
    "端口": "Port",
    "端口 / 加密方式": "Port / encryption",
    "端口写法：502,4840,8000-8010；一次最多 %d 个。只做 TCP 连接，连上立即断开。": "Port syntax: 502,4840,8000-8010; at most %d per scan. TCP connect only, closed immediately.",
    "端口太多：最多一次扫描 %d 个": "Too many ports: at most %d per scan",
    "端口太多：最多一次扫描 %d 个，请缩小范围": "Too many ports: at most %d per scan; narrow the range",
    "端口扫描": "Port scan",
    "端口扫描（单个主机）": "Port scan (single host)",
    "端口格式不对：%r（例如 502,4840,8000-8010）": "Invalid port format: %r (e.g. 502,4840,8000-8010)",
    "端口范围 1～65535：%r": "Port range 1–65535: %r",
    "端口超时": "Port timeout",
    "第 %d 行：%s": "Line %d: %s",
    "第 %s 个峰的保留时间（秒）": "Retention time of peak %s (s)",
    "第 %s 个峰的峰高": "Height of peak %s",
    "第 %s 个峰的面积": "Area of peak %s",
    "第 %s 个峰的面积%%": "Area%% of peak %s",
    "管理员": "Administrator",
    "管理员用户名：": "Administrator user name:",
    "管理员（账号、安全、备份还原）": "Administrator (accounts, security, backup/restore)",
    "类型": "Type",
    "系统设置": "System settings",
    "线圈": "Coils",
    "终点(s)": "End (s)",
    "结果": "Result",
    "绝对路径": "absolute path",
    "编辑": "Edit",
    "编辑选中通道的规则…": "Edit rule of selected channel…",
    "缺少 pyserial 库，无法使用串口功能（源码运行时请执行 pip install pyserial）": "The pyserial library is missing, serial functions are unavailable (when running from source: pip install pyserial)",
    "网段 %s 有 %d 个地址，超过上限 %d，请缩小范围": "Network %s has %d addresses, more than the limit %d; narrow the range",
    "网段格式不对：%r（例如 192.168.1.0/24）": "Invalid network format: %r (e.g. 192.168.1.0/24)",
    "网页设置页已关闭：在“系统设置 → 高级设置”勾选“启用设置网页”并保存后再试。": "The settings web page is turned off: tick \"Enable settings web page\" under \"System settings → Advanced\", save and try again.",
    "网页设置页没能打开：%s\n详见下方运行日志（常见原因：端口 %s 被占用）。": "The settings web page could not be opened: %s\nSee the log below (common cause: port %s in use).",
    "群机器人发送级别": "Robot notification level",
    "耗时 %s ms": "took %s ms",
    "自动生成": "Auto generate",
    "自动生成（窗口运行期间）": "Automatic generation (while the window runs)",
    "自动生效（服务运行中，下一轮扫描内自动加载）：%s。": "Applied automatically (loaded at the next scan while the service runs): %s.",
    "自定义端口（如 5020,9600）": "Custom ports (e.g. 5020,9600)",
    "自有私钥文件（PEM）": "Own private key file (PEM)",
    "自有证书文件（PEM）": "Own certificate file (PEM)",
    "自检": "Self-test",
    "自检中（约 10～30 秒）……": "Self-test running (about 10–30 s)…",
    "自检没有通过：%s\n\n请把日志截图发给技术支持。": "Self-test failed: %s\n\nPlease send a screenshot of the log to technical support.",
    "自检通过：%s\n\n程序本身工作正常。如果现场仍读不到数据，问题在数据目录或网络/防火墙。": "Self-test passed: %s\n\nThe program itself works. If data still cannot be read on site, the problem is the data folder or network/firewall.",
    "自检需要先暂停正在运行的服务（约 10～30 秒），完成后自动重新启动。继续吗？": "The self-test pauses the running service (about 10–30 s) and restarts it afterwards. Continue?",
    "色谱提取 OPC UA 服务": "ChromExtract OPC UA Server",
    "色谱提取 OPC UA 服务 v%s": "ChromExtract OPC UA Server v%s",
    "色谱提取 OPC UA 服务 · 设置": "ChromExtract OPC UA Server · Settings",
    "色谱提取 OPC UA 服务：测试提醒": "ChromExtract OPC UA Server: test notification",
    "范围": "Range",
    "范围写法：192.168.1.0/24、192.168.1.10-192.168.1.50、192.168.1.10-50，多段用逗号隔开；一次最多 %d 个地址。对方禁 ping 时，勾选的端口开放也算在线。": "Range syntax: 192.168.1.0/24, 192.168.1.10-192.168.1.50, 192.168.1.10-50; separate several with commas; at most %d addresses per scan. A host that blocks ping still counts as online if a ticked port is open.",
    "蓝线 = 平均延迟，红柱 = 失败比例（每段 %s 秒）": "Blue line = average latency, red bars = failure ratio (per %s s)",
    "覆盖": "Overwrite",
    "角色": "Role",
    "警告": "Warning",
    "设备地址": "Device address",
    "设备检测": "Device checks",
    "设置所选账号的角色": "Set role of selected account",
    "设置网页监听地址": "Settings web page listen address",
    "设置网页端口": "Settings web page port",
    "证书、端口等设置要重启服务后才生效（可点“重启服务”）。": "Certificates, ports and similar settings take effect after a service restart (click \"Restart service\").",
    "诊断工具": "Diagnostics",
    "试了 %d 组参数都没有正确应答%s": "Tried %d parameter sets, none responded correctly%s",
    "该通道的告警数": "Alerts of this channel",
    "说明": "Details",
    "请先“选择备份文件并预览”。": "Click \"Choose backup file and preview\" first.",
    "请先在列表里选中要对比的周期（可按住 Ctrl 多选）。": "Select the cycles to compare in the list first (hold Ctrl to select several).",
    "请先在表格里选中一个通道。": "Select a channel in the table first.",
    "请先选中一个自动备份。": "Select an automatic backup first.",
    "请先选中一个设备检测。": "Select a device check first.",
    "请先选中一个通道。": "Select a channel first.",
    "请先选中一条静默时段。": "Select a quiet period first.",
    "请先选中要删除的账号。": "Select the account to delete first.",
    "请先选中要导出的周期。": "Select the cycles to export first.",
    "请先选中账号。": "Select an account first.",
    "请在浏览器打开：%s": "Open in a browser: %s",
    "请填写串口，例如 COM3": "Enter the serial port, e.g. COM3",
    "请填写扫描范围，例如 192.168.1.0/24 或 192.168.1.10-192.168.1.50": "Enter the scan range, e.g. 192.168.1.0/24 or 192.168.1.10-192.168.1.50",
    "请填写用户名。": "Enter a user name.",
    "请填写目标设备地址": "Enter the target device address",
    "请填写要扫描的端口，例如 1-1024 或 502,4840,102": "Enter the ports to scan, e.g. 1-1024 or 502,4840,102",
    "请填写设备检测的名称。": "Enter a name for the device check.",
    "请换成 RS-232/422 方式，或用另一个 485 转换器对接、用“原始收发”互发。": "Switch to RS-232/422, or connect another 485 converter and exchange data with \"Raw send/receive\".",
    "请确认": "Please confirm",
    "请输入密码或授权码。": "Enter the password or authorization code.",
    "请输入导出时设置的密码：": "Enter the password set when exporting:",
    "请选择班次": "Choose a shift",
    "请选择通道（没有数据时请先启动服务或设置数据目录）": "Choose a channel (if there is no data, start the service or set the data folder first)",
    "读到 %d 个值": "read %d value(s)",
    "读到：%s": "read: %s",
    "读取数据区": "Read data area",
    "读取谱图数据失败：%s": "Failed to read chromatogram data: %s",
    "读取配置失败：%s": "Failed to read the configuration: %s",
    "谱图预览": "Chromatogram",
    "账号 %s 已存在，密码不对。": "Account %s already exists and the password is wrong.",
    "账号与 OPC UA 登录账号是同一套（在“系统设置 → 登录账号”添加）。没有指定角色的账号按操作员。": "Accounts are the same as the OPC UA login accounts (add them under \"System settings → Login accounts\"). Accounts without a role are operators.",
    "账号与角色": "Accounts & roles",
    "起始地址": "Start address",
    "起始地址（从 0 开始）": "Start address (0-based)",
    "起点(s)": "Start (s)",
    "超时": "Timeout",
    "超时(秒)": "Timeout (s)",
    "超过就抽点后再发布": "Longer curves are down-sampled before publishing",
    "超过这么久没有新周期就告警（分析仪停机、数据库不再更新）；按分析周期设，例如周期 10 分钟可设 60；0 = 不检查": "Alert when no new cycle arrives for this long (analyser stopped, database no longer updated); set according to the analysis cycle, e.g. 60 for a 10-minute cycle; 0 = don't check",
    "距最新周期的分钟数": "Minutes since the latest cycle",
    "跟随系统": "Follow system",
    "轮询只用于读功能码；写操作请用“写入（确认后执行）”。": "Polling is for read functions only; use \"Write (after confirmation)\" to write.",
    "轮询间隔(秒)": "Poll interval (s)",
    "轮询间隔必须是数字（秒）": "Poll interval must be a number (seconds)",
    "轮询间隔至少 0.05 秒": "Poll interval must be at least 0.05 s",
    "输入寄存器": "Input registers",
    "运行中": "Running",
    "运行中：%s:%d，已应答 %d 次，拒绝连接 %d 次": "Running: %s:%d, %d request(s) answered, %d connection(s) refused",
    "运行中：监听 %s（手动启动，关窗自动停）": "Running: listening on %s (started manually, stops when the window closes)",
    "运行告警": "Current alerts",
    "运行报表": "Reports",
    "运行日志": "Log",
    "运行本服务的这台电脑": "the PC running this service",
    "运行状态": "Service status",
    "运行状态 · 告警 · 诊断 · 设置": "Status · Alerts · Diagnostics · Settings",
    "近 7 天": "Last 7 days",
    "还原": "Restore",
    "还原前": "before restore",
    "还原前会自动把当前配置另存到 backups\\自动备份_*.zip（保留最近 N 个，N = backup_keep）。校验不通过或写入失败会自动回滚。": "Before restoring, the current configuration is saved to backups\\自动备份_*.zip automatically (the last N are kept, N = backup_keep). If validation or writing fails, everything is rolled back.",
    "还原失败，已回滚到还原前的状态：%s": "Restore failed and was rolled back: %s",
    "还原并重启服务": "Restore and restart service",
    "还原目标路径异常：%s": "Unexpected restore target path: %s",
    "还没有扫描到通道。": "No channels scanned yet.",
    "还没有扫描结果可以导出。": "No scan results to export yet.",
    "还没有明细 CSV 文件。": "No detail CSV file yet.",
    "还没有管理员账号：开启登录前请先“创建管理员…”，否则没人能修改设置。": "There is no administrator yet: \"Create administrator…\" before turning login on, otherwise nobody can change settings.",
    "还没有设备检测。": "No device checks yet.",
    "还没有设置数据目录：请先点“选择数据目录…”。": "No data folder set yet: click \"Choose data folder…\" first.",
    "还没有设置有效的数据目录。": "No valid data folder has been set yet.",
    "还没有通道数据：请先启动服务或设置数据目录，等通道出现后再生成。": "No channel data yet: start the service or set the data folder, and generate after channels appear.",
    "这不是本程序导出的备份包（manifest 不对）。": "This is not a backup exported by this program (wrong manifest).",
    "这个备份包已加密，请输入导出时设置的密码。": "This backup is encrypted; enter the password set when exporting.",
    "这个操作需要“%s”或更高权限。\n请点窗口上方的“登录”，用有权限的账号登录。": "This operation requires \"%s\" or higher.\nClick \"Log in\" at the top of the window and log in with an authorized account.",
    "这是一条测试提醒。新告警出现时会这样提醒你（任务栏同时闪烁）。": "This is a test notification. New alerts will be announced like this (the taskbar flashes too).",
    "这是最后一个管理员，不能降级（请先把别的账号设为管理员）。": "This is the last administrator and cannot be demoted (make another account an administrator first).",
    "这条谱图没有可显示的数据点": "This chromatogram has no points to display",
    "进度 %s%%": "Progress %s%%",
    "进行中……": "In progress…",
    "远程导出最短间隔（秒）": "Minimum interval between remote exports (s)",
    "远程桌面": "Remote Desktop",
    "连接不到服务：服务可能已停止或正在重启。请确认桌面窗口仍然打开、服务在运行（窗口顶部显示“服务运行中”），然后刷新页面。": "Cannot reach the service: it may have stopped or be restarting. Make sure the desktop window is still open and the service is running (\"Service running\" at the top of the window), then refresh the page.",
    "连接时间": "Connected since",
    "连接测试": "Connection test",
    "连接耗时(ms)": "Connect time (ms)",
    "连续失败": "Consecutive failures",
    "连续扫描出错告警（次）": "Alert after consecutive scan errors (times)",
    "退出": "Exit",
    "适合配合“登录后自动打开窗口”使用": "Useful together with \"Open the window after logging in\"",
    "选中周期后点“叠加对比所选周期”": "Select cycles, then click \"Overlay selected cycles\"",
    "选择 CSV 导出位置": "Choose CSV export location",
    "选择备份包": "Choose backup package",
    "选择备份文件并预览…": "Choose backup file and preview…",
    "选择存放 *_V3.db / *.mbd 的数据文件夹": "Choose the data folder holding *_V3.db / *.mbd",
    "选择数据目录…": "Choose data folder…",
    "逐组结果：": "Result per parameter set:",
    "逗号分隔，例如 192.168.1.20, 10.0.0.0/24；留空 = 不限制": "Comma separated, e.g. 192.168.1.20, 10.0.0.0/24; empty = no restriction",
    "通信安全模式": "Communication security mode",
    "通讯日志": "Communication log",
    "通讯诊断": "Comms diagnostics",
    "通道": "Channel",
    "通道告警规则": "Channel alert rules",
    "通道告警设置": "Channel alert settings",
    "通道心跳（每出一个新周期 +1）": "Channel heartbeat (+1 for every new cycle)",
    "通道数": "Channel count",
    "通道数据": "Channels",
    "通道无新周期告警（分钟）": "Channel no-new-cycle alert (minutes)",
    "通道（站点 / 应用 / 流路 / 检测器）": "Channel (station / app / stream / detector)",
    "通道：": "Channel:",
    "邮件发送级别": "E-mail notification level",
    "邮件通知（可选）": "E-mail notification (optional)",
    "配置文件不是 JSON 对象：%s": "The configuration file is not a JSON object: %s",
    "配置文件：%s": "Configuration file: %s",
    "配置有误": "Configuration error",
    "配置有误，服务没有启动：\n%s\n\n请在“系统设置”里改正并保存后再启动。": "Configuration error, the service was not started:\n%s\n\nCorrect it on the \"System settings\" tab, save and start again.",
    "采样间隔": "Sample interval",
    "采样间隔 %s": "Sample interval %s",
    "重启服务": "Restart service",
    "重启服务会让客户端短暂断开（几秒），用于让“需重启”的设置生效。继续吗？": "Restarting disconnects clients briefly (a few seconds) to apply settings that need a restart. Continue?",
    "锁定": "Lock",
    "错误分布": "Error distribution",
    "错误分布：": "Error distribution:",
    "长时间稳定性测试": "Long-running stability test",
    "长期停用或检修中的流路可以屏蔽告警；轮换间隔长的流路可以单独加大\"无新周期\"分钟数。屏蔽时该通道的告警直接移除，不发\"恢复\"；可设到期时间自动解除。": "Alerts of streams that are out of service or under maintenance can be muted; streams with long rotation intervals can get a larger \"no new cycle\" limit. Muted alerts are removed without a \"recovered\" notice; an expiry can lift the mute automatically.",
    "长期停用或检修中的流路可以屏蔽告警；轮换间隔长的流路可以单独加大“无新周期”分钟数。屏蔽时该通道的告警直接移除，不发“恢复”；可设到期时间自动解除。选中一行点“编辑规则”修改。": "Alerts of streams that are out of service or under maintenance can be muted; streams with long rotation intervals can get a larger no-new-cycle limit. Muted alerts are removed without a \"recovered\" notice; an expiry can lift the mute automatically. Select a row and click \"Edit rule\".",
    "问题": "Problem",
    "间隔 / 告警条件": "Interval / alert condition",
    "间隔(秒)": "Interval (s)",
    "需要重启服务": "Service restart required",
    "需要重启服务才生效：%s%s。可点“保存并重启服务”或窗口上方“重启服务”。": "Effective after a service restart: %s%s. Click \"Save and restart service\" or \"Restart service\" at the top.",
    "需要重新登录：请刷新页面。": "Login required again: please refresh the page.",
    "需重启": "restart needed",
    "静默时段内不提醒": "No notifications during quiet periods",
    "静默时段（计划停机 / 检修时不推送）": "Quiet periods (no pushes during planned shutdowns / maintenance)",
    "静默期间群机器人和邮件都不发；日志、本窗口和 OPC UA 告警节点照常显示。静默结束时仍未恢复的告警会补发一次；静默期间出现又恢复的不再打扰。时间按本机时间，添加后立即生效。": "During a quiet period neither the robot nor e-mail is sent; the log, this window and the OPC UA alarm nodes still show alerts. Alerts still active when the quiet period ends are sent once; alerts that came and went during it are not. Times are local; effective immediately.",
    "静默期间群机器人和邮件都不发；日志、本页面和 OPC UA 告警节点照常显示。静默结束时仍未恢复的告警会补发一次；静默期间出现又恢复的不再打扰。时间按本机时间，添加后立即生效。": "During a quiet period neither the robot nor e-mail is sent; the log, this page and the OPC UA alarm nodes still show alerts. Alerts still active when the quiet period ends are sent once; alerts that came and went during it are not. Times are local; effective immediately.",
    "面积%": "Area %",
    "页面已过期（服务重启过）：请刷新页面后重试。": "The page has expired (the service was restarted): refresh the page and try again.",
    "预览所选自动备份": "Preview selected automatic backup",
    "风险": "Risk",
    "驱动与软件环境检测": "Driver & software environment check",
    "驱动芯片": "Driver chip",
    "验证周期": "validation cycle",
    "高级设置": "Advanced",
    "默认": "Default",
    "（%s 分钟前更新）": " (updated %s min ago)",
    "（%s 起）": " (since %s)",
    "（%s）": " (%s)",
    "（01 线圈 / 02 离散输入 / 03 保持寄存器 / 04 输入寄存器）": "(01 coils / 02 discrete inputs / 03 holding registers / 04 input registers)",
    "（严重 %d）": " (%d critical)",
    "（全局）": "(global)",
    "（列表为空）": "(list is empty)",
    "（另有 %d 组也有应答）": " (%d other set(s) also responded)",
    "（密码留空 = 保留原密码）": "(empty password = keep the old one)",
    "（尚未保存）": " (not saved yet)",
    "（尚未建立会话）": "(no session yet)",
    "（已中途停止）": " (stopped early)",
    "（找不到启动文件夹）": "(Startup folder not found)",
    "（按住 Ctrl / Shift 多选，最多叠加 10 条）": "(hold Ctrl / Shift to select several, up to 10 curves)",
    "（无账号）": "(no accounts)",
    "（无）": "(none)",
    "（服务未运行，只能查看和立即检测）": "(service not running: you can only view and check now)",
    "（服务未运行：只打开了设置页面）": "(service not running: only the settings page is open)",
    "（服务未运行：点窗口上方“启动服务”后这里显示实时告警）": "(service not running: live alerts appear here after \"Start service\")",
    "（未知）": "(unknown)",
    "（桌面“数据与导出”页立即使用新目录）": " (the desktop \"Data & export\" tab uses the new folder immediately)",
    "（没有静默时段）": "(no quiet periods)",
    "（读取中…）": "(reading…)",
    "（逗号分隔；寄存器按数据类型解释，如 float32 填 123.45）": "(comma separated; registers are interpreted by data type, e.g. 123.45 for float32)",
    "（需重启）": " (restart needed)",
    "，其中 %d 台开放了所选端口": ", %d of them have the selected ports open",
    "：": ": ",
    "；": "; ",
    "；全部无响应，可能主机不在线或防火墙丢弃了连接": "; nothing responded, the host may be offline or a firewall drops the connections",
}


def get_language():
    return _state["lang"]


def system_ui_language(getter=None):
    """Windows 系统界面语言：中文（简/繁）返回 "zh"，其它返回 "en"。
    getter 可注入（测试用），返回 LANGID 整数或语言代码字符串。"""
    try:
        if getter is not None:
            v = getter()
        elif os.name == "nt":
            import ctypes
            v = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        else:
            v = (locale.getlocale()[0] or os.environ.get("LANG") or "")
    except Exception:  # noqa
        return "zh"
    if isinstance(v, int):
        return "zh" if (v & 0x3FF) == 0x04 else "en"      # PRIMARYLANGID: LANG_CHINESE = 0x04
    v = str(v or "").lower()
    if not v or v in ("c", "posix") or v.startswith("c."):
        return "zh"
    return "zh" if v.startswith("zh") or "chinese" in v else "en"


def resolve_language(setting, getter=None):
    """配置值 → 实际语言（"zh"/"en"）。不认识的值按中文。"""
    s = str(setting or "auto").strip().lower()
    if s in LANGS:
        return s
    if s == "auto":
        return system_ui_language(getter)
    return "zh"


def normalize_language(value):
    """配置校验：只能是 auto / zh / en（大小写不敏感），其它值抛 ValueError。"""
    s = str(value if value is not None else "auto").strip().lower()
    if s not in LANG_CHOICES:
        raise ValueError("desktop_language 只能是 auto / zh / en，当前为：%r" % (value,))
    return s


def set_language(lang):
    """设置当前语言（"zh"/"en"，也接受 "auto"），返回实际生效的语言。通知已注册的监听者。"""
    lang = resolve_language(lang)
    with _lock:
        old = _state["lang"]
        _state["lang"] = lang
        listeners = list(_listeners)
    if old != lang:
        for fn in listeners:
            try:
                fn(old, lang)
            except Exception:  # noqa
                pass
    return lang


def add_listener(fn):
    with _lock:
        _listeners.append(fn)


def remove_listener(fn):
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


def tr(text, lang=None):
    """翻译。text 是中文原文或 KEYS 里的英文键；缺失时回退中文（原文）。非字符串原样返回。"""
    if not isinstance(text, str):
        return text
    lang = lang or _state["lang"]
    k = KEYS.get(text)
    if k is not None:
        return k.get(lang) or k.get("zh") or text
    if lang == "en":
        return EN.get(text, text)
    return text


class Msg(str):
    """可翻译的格式化消息：值等于 中文模板 % 参数（日志/异常里照常是中文），
    同时记住模板和参数，界面显示时用 tr_msg() 翻译成当前语言。"""

    def __new__(cls, tmpl, *args):
        try:
            text = tmpl % args if args else tmpl
        except (TypeError, ValueError):
            text = tmpl
        obj = str.__new__(cls, text)
        obj.tmpl, obj.args = tmpl, args
        return obj


def M(tmpl, *args):
    return Msg(tmpl, *args)


def tr_msg(x, lang=None):
    """翻译 Msg / 异常 / 普通字符串（异常取第一个参数）。"""
    if isinstance(x, BaseException):
        return tr_msg(x.args[0], lang) if x.args else str(x)
    if isinstance(x, Msg):
        args = tuple(tr_msg(a, lang) if isinstance(a, (str, BaseException)) else a for a in x.args)
        try:
            return tr(x.tmpl, lang) % args if args else tr(x.tmpl, lang)
        except (TypeError, ValueError):
            return str(x)
    if isinstance(x, str):
        return tr(x, lang)
    return str(x)


def tr_list(items, lang=None):
    return [tr(x, lang) for x in items]


_REV = {}


def _reverse():
    if len(_REV) != len(EN) + 1:
        _REV.clear()
        for zh, en in EN.items():
            _REV.setdefault(en, zh)
        for k, v in KEYS.items():
            _REV.setdefault(v.get("en"), v.get("zh"))
        _REV[None] = None
    return _REV


def untr(text):
    """显示文字 → 中文原文（用于把下拉框里选中的英文显示值还原成代码里用的中文键）。"""
    if not isinstance(text, str):
        return text
    return _reverse().get(text, text)


def retranslate_text(text, old, new):
    """把用 old 语言显示的整段文字换成 new 语言；不是已知译文就返回 None（表示不用改）。
    依次尝试：整句完全匹配 → 带“（需重启）”之类后缀 → 按带占位符的模板匹配（例如“快捷方式位置：%s”）。"""
    if not isinstance(text, str) or not text or old == new:
        return None
    out = _exact(text, old, new)
    if out is not None:
        return out
    for suf in SUFFIXES:                                  # 标签 + 固定后缀
        s_old = tr(suf, old)
        if s_old and text.endswith(s_old) and len(text) > len(s_old):
            head = _exact(text[:-len(s_old)], old, new)
            if head is not None:
                return head + tr(suf, new)
    m = _template_match(text, old)
    if m is not None:
        key, groups = m
        tmpl_new = _PH_RE.sub("%s", tr(key, new).replace("%%", "\x00"))
        vals = tuple(_exact(g, old, new) or g for g in groups)
        try:
            return (tmpl_new % vals).replace("\x00", "%")
        except (TypeError, ValueError):
            return None
    return None


SUFFIXES = ("（需重启）",)


def _exact(text, old, new):
    key = untr(text) if old == "en" else text
    if old == "en" and key == text and text not in EN:
        return None
    if old == "zh" and text not in EN and text not in KEYS:
        return None
    out = tr(key, new)
    return out if out != text else None


_PH_RE = re.compile(r"%(?:\([^)]*\))?[-#0 +]*(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrsa]")
_TEMPLATES = {}


def _templates(lang):
    """lang 语言下所有带占位符的模板：[(按前缀分组的正则, 中文键)]。"""
    if lang in _TEMPLATES and _TEMPLATES[lang][0] == len(EN):
        return _TEMPLATES[lang][1]
    index = {}
    for key in EN:
        if "%" not in key or not _PH_RE.search(key):
            continue
        shown = tr(key, lang)
        parts = _PH_RE.split(shown.replace("%%", "\x00"))
        if not "".join(parts).strip():
            continue
        pat = "^" + "(.*?)".join(re.escape(p.replace("\x00", "%")) for p in parts) + "$"
        try:
            rx = re.compile(pat, re.S)
        except re.error:
            continue
        index.setdefault(parts[0][:2], []).append((rx, key))
    _TEMPLATES[lang] = (len(EN), index)
    return index


def _template_match(text, lang):
    idx = _templates(lang)
    for pref in (text[:2], text[:1], ""):
        for rx, key in idx.get(pref, ()):
            m = rx.match(text)
            if m:
                return key, m.groups()
    return None


_FMT_RE = re.compile(r"%(?:\([^)]*\))?[-#0 +]*(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrsa%]")


def placeholders(text):
    """字符串里的 % 占位符（按顺序），用于测试检查译文与原文占位符一致。"""
    return [m.group(0) for m in _FMT_RE.finditer(text or "") if m.group(0) != "%%"]


def missing_keys(keys):
    """给定的中文原文里，哪些没有英文翻译（测试用）。"""
    return sorted(k for k in set(keys) if k not in EN and k not in KEYS)


_CJK = re.compile(r"[\u4e00-\u9fff]")


def has_cjk(text):
    return bool(_CJK.search(text or ""))


def init_from_config(cfg, getter=None):
    """按配置设置语言（窗口启动时调用）。返回实际语言。"""
    return set_language(resolve_language((cfg or {}).get("desktop_language", "auto"), getter))
