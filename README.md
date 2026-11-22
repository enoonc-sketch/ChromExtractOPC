# ChromExtractOPC — 色谱提取 OPC UA 服务

把 GCP DataBrowser 日志数据库（`*_V3.db`）或 `.mbd` 谱图文件里的谱图曲线提取出来，
一方面照旧导出 **CSV 数据表**，另一方面作为 **OPC UA Server** 对外发布，
供 DCS / SCADA / 实时数据库 / 组态软件直接订阅读取。

- 服务端基于标准 **OPC UA（IEC 62541）**，任何符合规范的客户端均可连接
  （KEPServerEX / UA Expert / WinCC / iFIX / Ignition / 各 DCS 的 OPC UA 客户端等）。
- 打包后是**单个 EXE**，目标电脑**无需安装 Python**，拷过去就能用。

完整使用说明见 [使用说明_OPCUA.md](使用说明_OPCUA.md)。

## 快速上手

### 方式一：在 GitHub 上打包（推荐，不用装 Python）

1. 打开本仓库的 **Actions** 页，选择 **Build EXE**，点 **Run workflow**。
2. 等跑完，在这次运行的页面底部下载产物 **ChromExtractOPC-Windows**，
   里面有 `ChromExtractOPC.exe`、`config.json`、`启动服务.bat` 和使用说明。
3. 把整个文件夹拷到现场电脑，双击 `ChromExtractOPC.exe` 即可启动。

推一个 `v*` 标签（例如 `v2.0`）也会自动触发打包。

### 方式二：在本机打包

在一台装了 Python 3.10+ 的电脑上，双击 `build_exe.bat`，产物在 `dist\` 文件夹。

## 安装程序（推荐给现场使用）

1. 先运行 `build_exe.bat` 生成 `dist\ChromExtractOPC.exe`。
2. 安装 [Inno Setup 6.3+](https://jrsoftware.org/isdl.php)（或 `winget install JRSoftware.InnoSetup`），双击 `build_installer.bat`。
3. 得到 `dist-installer\ChromExtractOPC-Setup-2.4.exe`（附 `.sha256`）。GitHub Actions 也会自动生成，产物名 **ChromExtractOPC-Setup**。

安装向导会：选数据文件夹、设置防火墙允许的地址、可选开机自启（SYSTEM 账号）、自检、创建开始菜单/桌面快捷方式。
- 升级：直接运行新版安装程序，会先停服务再替换，**保留 config.json、账号、证书和日志**。
- 卸载：控制面板 → 程序，或开始菜单里的"卸载"；会删除开机自启任务和防火墙规则，并询问是否删除配置和日志（默认保留）。
- 静默安装：`ChromExtractOPC-Setup-2.4.exe /VERYSILENT /SUPPRESSMSGBOXES /TASKS=autostart`
- 开机自启在安装程序里固定用 SYSTEM 账号；想用指定 Windows 账号运行，安装时不勾选，之后运行 `安装开机自启.bat`。

## 代码签名（可选）

签名能让 Windows 显示发布者名称，并大幅减少 SmartScreen 拦截和杀毒软件误报。**没配置证书时一切照旧，只是生成未签名的程序。**

1. 购买代码签名证书（OV 或 EV，向 DigiCert / Sectigo / GlobalSign 等 CA 购买；EV 一般在 USB 令牌里）。
   自签名证书只能内部测试流程（`tools\make_test_cert.ps1`），消除不了警告；公司内部可通过域组策略把它下发为受信任的发布者。
2. 安装 Windows SDK 的签名工具（signtool）：`winget install Microsoft.WindowsSDK.10.0.22621`。
3. 设置环境变量（三选一），然后照常运行 `build_exe.bat` 和 `build_installer.bat`：
   - `SIGN_THUMBPRINT=证书指纹`：证书已导入 Windows 证书存储或在 USB 令牌里（EV 推荐）
   - `SIGN_PFX=证书.pfx` + `SIGN_PFX_PASSWORD=密码`
   - `SIGN_PFX_BASE64` + `SIGN_PFX_PASSWORD`：给 GitHub Actions 用，放在仓库 Settings → Secrets
4. 会签名：`ChromExtractOPC.exe`、安装程序 `Setup.exe`、卸载程序 `unins000.exe`。全部 SHA-256 + 时间戳（证书过期后签名依然有效），签完自动校验。

注意：`.pfx` 和密码**不要**提交到仓库（`.gitignore` 已排除 `*.pfx`）。EV 令牌一般无法用于 GitHub 托管的构建机，需在本机或自建构建机上签名。

## 部署（不用安装程序时）

1. 把 `dist` 文件夹整个拷贝到现场电脑（例如 `D:\ChromExtractOPC\`）。
2. 编辑同目录的 `config.json`，把 `watch_dirs` 改成实际的日志数据库所在目录，例如：
   ```json
   "watch_dirs": ["D:\\GCP\\DataBrowser"]
   ```
3. 双击 `ChromExtractOPC.exe` 启动。窗口显示
   `OPC UA 服务已启动：opc.tcp://0.0.0.0:4840/ChromExtract` 即成功。
4. Windows 防火墙首次弹窗时选择“允许访问”。

## 后台静默运行

服务适合在现场电脑上长期无人值守运行：

| 方式 | 说明 |
|---|---|
| **Windows 服务（推荐）** | 安装时选"注册为 Windows 服务"，或以管理员身份运行 `安装为Windows服务.bat`。在 `services.msc` 里显示为"色谱提取 OPC UA 服务"，可启动/停止/查看状态；开机**延迟自动启动**（等网络和网络盘就绪）；LocalSystem 账号；崩溃 1 分钟后自动重启（连续崩溃 5 分钟后）；关机时正常停止 |
| 计划任务自启 | 安装时勾选，或运行 `安装开机自启.bat`。计划任务以 `--background` 启动：开机即运行、不需要登录桌面、不弹任何窗口；运行时间不限，崩溃 1 分钟后自动重启；低优先级，不抢色谱工作站的 CPU |
| 手动后台启动 | 双击 `后台启动.bat`，或开始菜单"后台启动服务"；窗口几秒后自动关闭，服务留在后台 |
| 查看状态 | `ChromExtractOPC.exe --status`：是否在运行、进程号、启动时间、最近心跳、设置页面地址 |
| 停止 | `停止后台服务.bat` 或 `ChromExtractOPC.exe --stop`：请求正常停止（断开客户端、保存状态），20 秒没停才强制结束 |

- 服务相关命令（需管理员）：`--install-service` 注册并启动（已注册则更新；会删除旧的计划任务自启，避免两套自启并存）、`--uninstall-service`、`--start-service`、`--stop-service`；`--service-status` 查看。卸载服务用 `卸载Windows服务.bat`，程序和配置保留。
- 访问网络共享上的数据目录：LocalSystem 访问不了需要账号的共享盘，可在 services.msc → 属性 → 登录 里改为有权限的域/本地账号。
- **后台模式不显示任何窗口、不往控制台输出**，所有信息写入 `logs\server.log`（自动轮转），未预期的崩溃写入 `logs\crash.log`；出错时也不会停在"按回车关闭"等人处理。
- **单实例**：同一台电脑只运行一个服务（系统级互斥），重复启动会直接退出并记日志，不会抢端口、不会重复告警。后台服务已在运行时再打开"图形监控窗口"，会直接打开设置页面，不会另起一个服务。
- 运行状态写在 `run\status.json`（每 15 秒心跳）；心跳超过 45 秒没更新即视为已异常退出。
- 日常查看和修改设置用浏览器打开设置页面即可，不需要窗口程序。

## 网页设置（推荐，不用手改 config.json）

页面分为 4 个标签页（地址栏 `#status` / `#alerts` / `#tools` / `#system` 可直接打开对应页）：

| 标签页 | 内容 |
|---|---|
| 运行状态 | 运行告警与静默状态、设备检测、通道告警设置（各通道状态） |
| 告警与通知 | 告警阈值、群机器人、静默时段、邮件通知、各渠道级别 |
| 诊断工具 | 驱动与软件环境检测、连接测试、稳定性测试、多目标对比 |
| 系统设置 | 数据源、OPC UA 服务、CSV 导出、高级设置、登录账号、安全体检 |

- 底部固定"保存设置"栏：改过的输入框黄框标出，栏里显示有几项未保存、在哪些标签页（标签上有黄点）；有未保存修改时关闭页面会提醒。
- 标签上的红/黄数字：运行状态 = 当前告警数，系统设置 = 安全体检风险数。



服务启动后会同时打开一个**设置页面**：浏览器访问 <http://127.0.0.1:8840/>
（或双击 `打开设置页面.bat`，图形窗口里也有"打开设置页面…"按钮）。

- 可修改：监控目录、扫描周期、endpoint、安全模式、匿名开关、远程导出、日志等；添加/删除账号、一键加固。
- 保存前按服务启动时的同一套规则校验，不合法不会写入；写入前自动备份为 `config.json.bak`。
- 能热加载的项保存后自动生效，需要重启的项页面上标有"需重启"。
- 默认只监听 `127.0.0.1`（只有本机能打开）。若把 `web_host` 改为对外地址，需用 OPC UA 账号登录；
  此时自动启用 **HTTPS**（`web_https=auto`），密码加密传输。不需要时可设 `web_ui=false` 关闭。
- HTTPS 证书：默认自动生成设置页面专用的自签名证书 `certs\web_cert.pem`（与 OPC UA 证书分开）；
  浏览器首次会提示"不安全"，核对页面/日志里显示的 SHA-256 指纹一致后继续即可，
  或把 `web_cert.pem` 导入"受信任的根证书颁发机构"。有公司证书时填 `web_cert_file` / `web_key_file`（PEM）。
- 服务没运行时：`ChromExtractOPC.exe --settings` 只打开设置页面。

## 设备检测（网络 / 串口设备接入告警）

服务运行时按设定间隔自动检测关键设备，连不上就告警：

1. 在"连接测试"里选好类型和参数，先点"开始测试"测通；
2. 填名称、检测间隔（10 秒～1 天）、连续失败几次告警（默认 3 次，避免偶发丢包误报）、级别（警告 / 严重），点"加为设备检测"。

- 支持 Ping、TCP 端口、DNS、OPC UA、Modbus TCP、Modbus RTU、串口收发，最多 32 个。
- 告警走现有渠道：日志、网页、OPC UA `Alarms`、群机器人、邮件；遵守静默时段和渠道级别（例如设为"严重"的检测会发邮件）。一次成功自动恢复。
- "设备检测"表显示每项的状态、最近结果、最近 60 次的成功/失败条和可用率；可立即检测、编辑、停用、删除。停用或删除时告警直接移除，不发"恢复"。
- 改动立即生效，不用重启服务。OPC UA 登录密码以系统加密保存。
- 串口互斥：设备检测、单次测试、稳定性测试共用每个串口的锁，绝不同时打开同一个串口；串口被占用的那次检测不计入失败。

## 驱动与软件环境检测

设置页面"驱动与软件环境检测"或命令行 `ChromExtractOPC.exe --drivercheck`。只读检查，不安装、不修改：

| 检查项 | 内容 |
|---|---|
| 串口设备与驱动 | 每个 COM 口的芯片（CH340/CH341、FTDI、CP210x、PL2303 等）、驱动厂商、版本、日期、是否签名 |
| 有问题的设备 | 设备管理器里带黄色感叹号或未装驱动的串口设备，翻译错误代码（如 28 没装驱动、10 无法启动），按芯片给出驱动下载提示；PL2303 假芯片单独提示 |
| 运行库 | asyncua、cryptography、pyserial 是否可用及版本 |
| 系统 | 操作系统版本、Windows 防火墙服务、开机自启任务状态及上次运行结果、OPC UA 端口是否在监听 |
| 数据目录 | 监控目录是否存在、可读，里面有几个数据文件；提示 SYSTEM 账号看不到映射盘符 |

结果按"问题 / 注意 / 正常"排序，每项附处理办法。

**启动时自动检测**（默认开启）：服务启动约 8 秒后在后台做一次环境检测（不拖慢启动），之后每 `env_check_hours` 小时（默认 6，0 = 只在启动时）复查一次。
- "问题"级别的项（如串口驱动没装、设备报错、运行库缺失）→ 警告级告警，走现有渠道；复查时问题消失自动恢复，同一问题不重复通知。
- "注意"级别只记日志（每项只记一次），不打扰人。数据目录问题已有"没有数据"告警，不重复报。
- 设置页面显示上次自动检测的时间和结果；服务运行中点"开始检测"也会同步更新告警。
- 配置：`env_check_on_start`（需重启）、`env_check_hours`（立即生效）。

## 连接测试（网络 / RS-232 / RS-485）

现场排查用，在设置页面"连接测试"里操作（也可用命令行），测试在**运行本服务的电脑**上执行：

| 测试 | 用途 |
|---|---|
| Ping | 网络是否通、延迟；对方禁 ping 时改用 TCP 端口测试 |
| TCP 端口 | 端口是否开放；区分"超时（网络/防火墙）"和"拒绝（服务没开）"；可发一段文本看回应 |
| DNS 解析 | 主机名能否解析 |
| 本机网卡 | 列出本机 IP，确认和 DCS 是否同网段 |
| OPC UA | 连接任意 OPC UA 服务器（不加密），读状态并列出 Objects；可用来自测本服务 |
| Modbus TCP | 按站号/功能码（01～04）/地址读数据，显示原始报文和解析值 |
| 串口列表 | 列出 COM 口（含 USB 转 485/232 的厂商信息） |
| 串口收发 232/485 | 按波特率/数据位/校验/停止位打开串口，发送十六进制或文本，显示收到的数据 |
| 串口回环自检 | TX/RX 短接后自检，判断是串口本身坏了还是线缆/设备的问题 |
| Modbus RTU | 通过 RS-485/232 读从站寄存器，自动去掉 485 回显，CRC/异常码给出人话解释 |

- 没有自动收发切换的 485 转换器，勾选"RTS 控制收发方向"。
- 地址从 0 开始：手册上的 40001 填 0，40010 填 9。
- 串口被组态软件或串口助手占用时会明确提示。同一时间只运行一个测试。
- 只做读操作（Modbus 只支持读功能码），不会改写现场设备。

### 长时间稳定性测试

单次测试只能看"通不通"，间歇性故障（接头松动、485 干扰、交换机丢包）要靠长时间测试才能暴露。
在"连接测试"下方选好测试类型和参数，设置**间隔**（0.2～3600 秒，Ping 最小 0.5 秒）和**时长**（10 分钟～72 小时），点"开始稳定性测试"：

- 实时显示：进度、成功/失败次数、**失败率（丢包率）**、**最长连续失败**及发生时间、延迟**最小/平均/P95/P99/最大**、**抖动**。
- 趋势图：蓝线为每段平均延迟，红柱为该段失败比例，一眼看出故障集中在什么时间。
- 错误分布：同类错误合并计数（如"CRC 校验错误 12 次""无应答 3 次"），并给出**排查建议**（屏蔽接地、终端电阻、端子松动等）。
- 结论：稳定 / 基本稳定 / 不稳定（疑似间歇断线、延迟尖峰等）。
- 明细 CSV：每次测试一行，保存在 `logs\stability\`（保留最近 20 个），页面上可下载；运行中每 20 次写盘一次。
- 关掉浏览器页面测试也会在后台继续；同一时间只运行一个；正在测试的串口不能被单次测试占用。
- 支持：Ping、TCP 端口、DNS、OPC UA、Modbus TCP、Modbus RTU、串口收发（收到数据算成功）。Ping 自动改为每次 1 个包，按次统计丢包。

### 多目标并行测试

同时测多台设备，并对比**失败是否同时发生**，用来区分"公共部分坏了"还是"单台设备的问题"：

1. 在"连接测试"里选好一个目标的参数，填名称（如 GC-1），点"加入对比列表"；重复添加，最多 16 个。列表保存在浏览器里，下次打开还在。
2. 设置间隔和时长，点"开始多目标测试"。

结果是一张对比表，包括每个目标的失败率、最长连续失败、延迟和失败时间分布条（红色 = 失败，各行时间对齐），以及总体结论：

| 结论 | 含义 |
|---|---|
| 只有某一个目标失败 | 问题在这台设备本身或它的支路（端子、通讯模块、站号、那段线） |
| 多个目标在相同时段一起失败 | 问题在共用部分：交换机/网关、485 主干或终端电阻、本机网卡/USB 转换器、公共电源或接地 |
| 多个目标零散失败、时间不重合 | 各自支路的问题，从失败率最高的查起 |

- **同一串口上的多个站**（同一条 485 总线）自动**按顺序轮询**：485 是半双工，不能同时通信；这些目标的串口参数必须一致。不同串口、网络目标真正并行。
- 结束后生成对比汇总 `logs\stability\stability_multi_时间.csv`，每个目标另有明细 CSV。
- 命令行：`ChromExtractOPC.exe --stability-multi 目标.json interval=1 minutes=60`，目标文件格式见 `tools\stability_targets_example.json`。

命令行示例：
```
ChromExtractOPC.exe --stability modbus_rtu port=COM3 baud=9600 unit=1 interval=1 minutes=60
ChromExtractOPC.exe --stability ping 192.168.1.10 interval=1 minutes=1440
ChromExtractOPC.exe --nettest ping 192.168.1.10
ChromExtractOPC.exe --nettest tcp 192.168.1.10 4840
ChromExtractOPC.exe --nettest serial_list
ChromExtractOPC.exe --nettest modbus_rtu port=COM3 baud=9600 parity=N unit=1 addr=0 count=10
ChromExtractOPC.exe --nettest modbus_tcp 192.168.1.20 502 unit=1 func=3 addr=0 count=10
```

## 异常告警

服务运行时持续检查以下情况，**出现和恢复时各记录/推送一次**，不会刷屏：

| 告警 | 级别 | 触发条件 |
|---|---|---|
| 扫描卡死 | 严重 | 超过 3 个扫描周期（至少 2 分钟）没有完成一次扫描（独立定时检查，扫描线程卡住也能发现） |
| 扫描出错 | 严重 | 连续 `alert_scan_errors` 次（默认 3）扫描出错，例如数据库被锁、网络盘断开 |
| 没有数据 | 警告 | 监控目录里一个谱图都没找到 |
| 数据源丢失 | 警告 | 之前有的通道读不到了（对应节点 `Good=False`） |
| 通道不更新 | 警告 | 某通道超过 `alert_stale_minutes` 分钟（默认 120）没有新周期，分析仪可能停机 |

告警会出现在这些地方：
- 日志 `logs\server.log`
- 网页设置页顶部的"运行告警"（每 15 秒刷新，有告警时浏览器标签显示 ⚠）
- 图形窗口状态栏
- OPC UA 节点 `Chromatograph/Alarms`：`ActiveCount`、`Severity`（0 正常 / 1 警告 / 2 严重）、`Text`、`LastChange`，DCS 订阅 `Severity` 即可接入报警
- 可选邮件通知（见下）
- 可选推送 `alert_webhook`：企业微信 / 钉钉 / 飞书群机器人或任意 https 地址（只允许 https），网页上可"发送测试消息"

### 按级别分配渠道

每个渠道可以单独选择发送级别（设置页面"告警设置"里的下拉框，改完立即生效）：

| 设置值 | 含义 |
|---|---|
| `all` | 严重和警告都发 |
| `critical` | 只发严重（扫描卡死、连续扫描出错） |
| `off` | 不发 |

默认：群机器人 `all`，邮件 `critical`（邮件只在服务真出问题时打扰人）。
恢复通知只发给收到过该告警的渠道，不会出现"只收到已恢复、却不知道出过什么事"。
日志、网页、窗口和 OPC UA `Alarms` 节点始终显示全部告警，不受此设置影响。

### 按通道屏蔽告警

设置页面"通道告警设置"表里列出所有通道（最后周期、多久没更新、是否在告警），每个通道可以：

- **屏蔽告警**：长期停用或检修中的流路不再报"无新周期""数据源丢失"。屏蔽时该通道已有的告警直接移除，**不发"恢复"**，避免误以为恢复正常；可填**到期时间**，到期自动恢复告警。
- **单独设置无新周期分钟数**：轮换间隔长的流路设大一些，其它通道仍用全局 `alert_stale_minutes`。
- 屏蔽只影响通道类告警；扫描卡死、扫描出错、没有数据这类整体告警不受影响。
- 屏蔽记录会写入日志和"最近的告警记录"。OPC UA 节点数据照常更新（数据源丢失时仍是 `Good=False`）。

### 静默时段（计划停机 / 检修）

在设置页面"告警设置 → 静默时段"里管理，添加后立即生效：

- **临时静默**：一键静默 2 / 8 / 24 小时（最长 72 小时），可随时"立即结束静默"。
- **一次性时段**：如 `2026-11-23 08:00 ～ 18:00 大修`，最长 31 天，过期后自动从列表清除。
- **每天重复**：如每周一至周五 `22:00 ～ 06:00`，结束早于开始表示跨午夜；可只选部分星期。

规则：
- 静默只停**推送**（群机器人和邮件）；日志、网页、图形窗口、OPC UA `Alarms` 节点照常显示全部告警。
  OPC UA 新增 `Alarms.NotifySilenced`，DCS 可据此显示"推送已静默"。
- 静默结束时：仍未恢复的告警补发一次"出现"；静默前已通知、静默中已恢复的补发"恢复"；
  静默中出现又恢复的不再打扰。
- 时间按本机时间（现场电脑的系统时区）。

### 邮件通知

在设置页面"告警设置 → 邮件通知"里填写，保存后点"发送测试邮件"确认：

| 邮箱 | SMTP 服务器 | 端口 / 加密 | 密码 |
|---|---|---|---|
| 腾讯企业邮 | `smtp.exmail.qq.com` | 465 / SSL | 客户端专用密码 |
| QQ 邮箱 | `smtp.qq.com` | 465 / SSL | 授权码 |
| 163 邮箱 | `smtp.163.com` | 465 / SSL | 授权码 |
| Microsoft 365 | `smtp.office365.com` | 587 / STARTTLS | 需管理员开启 SMTP AUTH |
| 公司内网中继 | 问 IT | 25 / 不加密，用户名留空 | 无 |

- 告警出现、恢复时各发一封；每小时最多 20 封，超出的合并到下一封里说明。
- SMTP 密码不能只存哈希（发信时要用），Windows 上用系统 DPAPI 加密后写进 `config.json`，**拷到别的电脑无法解密**，换电脑需要重新设置；网页上永远不显示密码。
- 只要填了用户名，就必须用 SSL 或 STARTTLS，并校验服务器证书，密码不会明文传输。
- 现场电脑若不能上外网，需要防火墙放行到邮件服务器的 465/587 端口，或使用公司内网邮件中继。

`alert_stale_minutes` 请按分析周期设置（例如周期 10 分钟设 60）；流路轮换间隔较长的仪器要设大一些，避免误报。

## 配置（config.json）

| 键 | 默认值 | 说明 |
|---|---|---|
| `endpoint` | `opc.tcp://0.0.0.0:4840/ChromExtract` | 监听地址与端口 |
| `server_name` | 色谱提取 OPC UA 服务 | 客户端看到的服务器名称 |
| `watch_dirs` | `["data"]` | 要监控的数据目录列表，目录里的 `*_V3.db` 和 `*.mbd` 都会被解析 |
| `poll_seconds` | `30` | 扫描周期（秒），最小 5 |
| `last_n` | `5` | 每个流路取最近 N 个周期参与“最新值”比较 |
| `security` | `none` | 安全模式：`none` / `sign` / `sign_encrypt`，非 none 时首次启动自动生成自签名证书到 `certs\` |
| `csv_export_dir` | `谱图导出` | CSV 导出的默认输出目录 |
| `log_file` | `logs/server.log` | 日志文件 |
| `max_points` | `20000` | 单条曲线发布到 OPC UA 的最大点数（超出自动等间隔抽点） |
| `web_ui` | `true` | 是否开启网页设置页面 |
| `web_host` | `127.0.0.1` | 设置页面监听地址（非本机地址需要登录） |
| `web_port` | `8840` | 设置页面端口 |
| `alert_stale_minutes` | `120` | 通道多少分钟无新周期就告警，0 = 不检查 |
| `alert_scan_errors` | `3` | 连续扫描出错多少次告警，0 = 不检查 |
| `alert_webhook` | 空 | 告警推送地址（https） |
| `alert_email_to` | 空 | 告警邮件收件人，逗号隔开 |
| `smtp_host` / `smtp_port` / `smtp_security` | 空 / 465 / ssl | 邮件服务器，`ssl` / `starttls` / `none` |
| `alert_webhook_level` / `alert_email_level` | `all` / `critical` | 各渠道发送级别：`all` / `critical` / `off` |
| `alert_channel_rules` | `[]` | 按通道屏蔽 / 单独设置分钟数，请在设置页面通道表里设置 |
| `env_check_on_start` / `env_check_hours` | `true` / `6` | 启动时自动环境检测 / 复查间隔（小时，0 = 只启动时） |
| `alert_quiet` | `[]` | 静默时段，请在设置页面里添加，不建议手写 |
| `smtp_user` / `smtp_from` | 空 | 登录用户名 / 发件人（留空同用户名）；密码在设置页面里设置 |
| `web_https` | `auto` | `auto` 对外开放时自动 HTTPS / `on` 始终 / `off` 不用 |
| `web_cert_file` / `web_key_file` | 空 | 自有证书和私钥（PEM），留空自动生成 |

## OPC UA 地址空间

根节点：`Objects → Chromatograph`（命名空间 URI：`urn:chromextract:opcua`）

```
Chromatograph
├─ ServerVersion                 String   服务版本
├─ WatchDirs                     String   监控目录
├─ ExportCSV()                   方法     远程触发 CSV 导出，返回输出文件夹路径
├─ Alarms                      ActiveCount / Severity / Text / LastChange / NotifySilenced（异常告警）
└─ Station_<仪器序列号>
   ├─ Info
   │  ├─ SerialNumber            String   仪器序列号
   │  └─ SourceFile              String   数据来源文件名
   └─ App<应用号>_Stream<流路号>_Det<检测器号>
      ├─ LatestCycle             周期元数据（时间、编号、点数、采样间隔等）
      ├─ Trace                   时间 / 原始计数 / 微伏 / 显示值 四条数组
      └─ Status                  Revision（每次刷新 +1）、LastUpdate、Good
```

接入建议：订阅 `LatestCycle.CycleTime`（或 `Status.Revision`）判断新周期，
新周期到来时批量读取 `Trace.ValueArray`（时间对应用 `Trace.TimeArray`）。

## 命令行模式（原有 CSV 提取功能完整保留）

```
ChromExtractOPC.exe --extract 数据文件夹 -o 输出文件夹 --last 20
ChromExtractOPC.exe --list 数据文件夹          :: 查看里面有什么
ChromExtractOPC.exe --gui                      :: 打开图形监控窗口
```

- `--extract` 导出结果：`index.csv`（总清单）+ `csv\`（每条谱图一个 CSV：
  时间、原始计数、微伏、显示值）。
- 服务运行中也可**远程触发**导出：调用 `Chromatograph → ExportCSV` 方法即可。
- 读取数据库使用**副本**，不影响 DataBrowser 正常运行。

## 目录结构

| 文件 | 作用 |
|---|---|
| `server_app.py` | 主程序：OPC UA 服务 + 数据监控 + CSV 导出 + GUI 监控窗口 |
| `chrom_core.py` | 谱图解析核心库（BinTrc 解码、数据库/mbd 读取、CSV 输出） |
| `config.json` | 配置文件（端口、监控目录、扫描周期、安全模式等） |
| `build_exe.bat` | 一键打包为 EXE（在有 Python 的电脑上运行一次即可） |
| `启动服务.bat` | 启动服务（优先用打包好的 EXE，其次用 Python 源码运行） |
| `requirements.txt` | 依赖清单 |
| `tools\make_demo_db.py` | 生成演示数据库，用于验收测试 |
| `tools\opcua_test_client.py` | OPC UA 验收测试客户端 |
| `.github\workflows\build-exe.yml` | GitHub Actions 打包工作流（在 GitHub 上打 EXE） |

## 验收

- 用任意 OPC UA 客户端连接 `opc.tcp://<本机IP>:4840/ChromExtract`，浏览
  `Objects → Chromatograph` 即可看到数据；或用本项目自带的测试客户端：
  ```
  python tools\opcua_test_client.py opc.tcp://127.0.0.1:4840/ChromExtract/
  ```
- 生成测试数据：`python tools\make_demo_db.py data\demo_V3.db`。

## 常见问题

| 现象 | 处理 |
|---|---|
| 客户端连不上 | 检查防火墙是否放行 4840；`netstat -ano \| findstr 4840` 确认在监听 |
| 端口被占用 | 改 `config.json` 的 `endpoint` 端口（如 4841），客户端同步修改 |
| 看不到数据 | 确认 `watch_dirs` 路径正确且包含 `*_V3.db` 或 `*.mbd`；查看 `logs\server.log` |
| 改了 config 不生效 | 重启程序（配置只在启动时读取） |
| 加密模式客户端报证书错误 | 把 `certs\server_cert.der` 导入客户端受信任列表，或先用 `none` 模式排通 |

## 版本

- v2.0：OPC UA 服务版（监控 + 发布 + 远程导出 + GUI 监控窗口）。
- v2.0.1（修复）：
  - 修复 `security` 设为 `sign` / `sign_encrypt` 时服务启动即崩溃的问题（安全策略名称错误、证书/私钥未 await 加载）。
  - 证书里的应用 URI 与服务端对齐，旧版本生成的证书会自动重新生成；去掉 `utcnow` 弃用警告。
  - 修复 GUI 窗口关闭时服务停止信号不安全的问题（Python 3.8/3.9 会报错）；节点刷新加锁，避免并发重复建节点。
  - 修复 `启动服务.bat` 换行符为 LF 导致 Windows 下跳转失效的问题（改为 CRLF）。
  - 补回缺失的 `.github/workflows/build-exe.yml`；`tools/opcua_test_client.py` 的加密连接写法已更正。
- v2.1（安全加固与优化，详见 `安全检查报告.md`）：
  - **修复严重漏洞**：asyncua 默认登录管理器会让"用户名 admin + 任意密码"获得管理员权限、可改写数据；已替换为只读登录管理器。
  - 新增：账号登录（密码哈希）、错误登录锁定、客户端证书白名单、加密模式只开加密端点、连接数上限、`--check-security` 安全体检、`--hash-password`、防火墙脚本。
  - 输入防护：谱图点数/大小上限、先抽点再展开、MBD 头严格校验、数据库只读且禁止写入类 SQL、文本清洗、CSV 公式注入防护。
  - 可靠性：数据库未变化时不再重复拷贝；数据源丢失时节点 `Good=False`；新增 `Heartbeat`/`LastScanTime`；日志轮转；端口占用提示；相对路径按程序目录解析。
  - 新增 `tests/test_security.py`（49 条）；打包流程加入测试门禁和 SHA256 校验；依赖版本收紧并提供 `requirements-lock.txt`；Python 要求提高到 3.10。
- v2.2（易用性与可运维性升级）：
  - 新增 `安全管理.bat`（交互菜单）以及命令行 `--admin / --harden / --add-user / --remove-user / --list-users`：不用手改 JSON 就能开启加密、管理账号；密码强度检查，拦截弱口令；用户名只允许安全字符。
  - 配置**热加载**：账号、匿名开关、导出限制、监控目录、扫描间隔改完自动生效，无需重启；写坏或不合法的配置会被忽略并记录日志，不影响运行中的服务；需要重启的项会明确提示。
  - 配置文件写入改为"备份 → 临时文件 → 原子替换"，不会因断电或出错留下写了一半的文件；只修改被要求修改的项。
  - 窗口模式新增"安全设置…"面板和安全体检摘要。
  - 新增登录成功的审计日志；新增 `安装开机自启.bat`（未在 Windows 上实测）。
  - 密码相关函数拆到 `config_tools.py`；测试扩充到 76 条。
- v2.3（修复"打包后装不上、用不了"）：
  - **修复**：`build_exe.bat` 之前只复制 3 个文件到 `dist`，缺了安装开机自启、防火墙、安全管理脚本，导致打出来的文件夹没法安装；现在补全，并有测试防止再漏。
  - **修复**：开机自启改用 PowerShell 注册（`install_autostart.ps1`）。旧的 `schtasks` 方式默认会在运行满 72 小时后强制结束服务，且崩溃后不重启；新任务不限时、崩溃 1 分钟后自动重启。
  - **修复**：双击 EXE 出错（端口占用、配置错）时窗口不再一闪而过，会停住显示原因；未预期的崩溃写入 `logs/crash.log`。
  - **修复**：`设置防火墙.bat` 传多个地址（逗号分隔）时第二个地址被当成端口的问题；`build_exe.bat` 能识别并跳过 Microsoft Store 的假 python.exe；横幅版本号不再写死。
  - 新增 `--selftest`：在临时目录造演示数据、启动真实服务、用客户端读取，一条命令判断 EXE 本身有没有问题。`build_exe.bat` 和 CI 打包后都会自动跑，不通过就报失败。
  - 新增 `安装.bat` / `卸载.bat` 一键安装卸载；`--set-watch-dir` 不用手改 JSON 设置数据目录；启动时检查监控目录并给出明确提示；`启动图形界面.bat`（自动隐藏黑窗口）；`--version`。
  - 测试扩充到 89 条。
- v2.4（网页设置 + HTTPS）：
  - 新增浏览器设置页面（`web_settings.py`），可修改全部常用配置、管理账号、一键加固，不用再手改 JSON；`打开设置页面.bat`、`--settings`。
  - 设置页面支持 HTTPS：对外开放时自动启用，自签名证书自动生成并在地址变化时更新，也可用自有证书；安全体检会提示对外明文访问的风险。
  - 新增 Inno Setup 安装程序（`installer\ChromExtractOPC.iss`、`build_installer.bat`）：向导式安装、开始菜单/桌面快捷方式、升级保留配置、卸载清理计划任务和防火墙；CI 自动生成并做静默安装/卸载测试。
  - 新增程序图标 `assets\app.ico`（16～256 多尺寸）：EXE、安装程序、开始菜单/桌面快捷方式、监控窗口、设置网页标签页都使用它。
  - 新增可选代码签名（`sign.ps1`）：EXE、安装程序、卸载程序均可签名并加时间戳；支持证书存储/USB 令牌、.pfx 文件、GitHub 机密三种方式；未配置证书时自动跳过。
  - 新增异常告警（`alerts.py`）：扫描卡死、扫描出错、无数据、数据源丢失、通道长时间不更新；日志、网页、窗口、OPC UA `Alarms` 节点、企业微信/钉钉/飞书推送和邮件通知（SMTP 密码 DPAPI 加密保存、发送限流），各渠道可按级别分别设置（全部 / 只发严重 / 不发）；支持静默时段（临时、一次性、每天重复），结束后补发未恢复的告警；可按通道屏蔽告警或单独设置无新周期分钟数（可设到期时间）。
  - 新增连接测试（`nettest.py`）：Ping、TCP 端口、DNS、本机网卡、OPC UA、Modbus TCP；串口列表、RS-232/485 收发、回环自检、Modbus RTU；网页和命令行 `--nettest` 均可使用。新增依赖 pyserial；长时间稳定性测试（`stability.py`）：成功率/丢包率、延迟百分位、抖动、最长连续失败、错误分布与排查建议、趋势图和 CSV 明细，网页和命令行 `--stability`；多目标并行测试（最多 16 个，同一 485 总线自动轮询，失败时间关联分析判断公共故障还是单台故障，对比汇总 CSV，`--stability-multi`）。
  - 新增设备检测（`probes.py`）：定时检测 Ping / 端口 / OPC UA / Modbus / 串口设备，连续失败达到次数走告警，自动恢复，可用率统计；串口全局互斥锁。
  - 新增驱动与软件环境检测（`drivercheck.py`、`--drivercheck`）：串口驱动和芯片识别、异常设备与错误代码、运行库、防火墙、自启任务、端口、数据目录；服务启动时自动检测并定期复查（`envwatch.py`），问题项接入告警。
  - 后台静默运行（`bgrun.py`）：`--background` 无窗口运行、日志只写文件；单实例互斥；`--status` / `--stop` 正常停止；开机自启任务改为后台模式、低优先级；新增 `后台启动.bat`、`停止后台服务.bat`。
  - 注册为真正的 Windows 服务（`winservice.py`，纯 ctypes，无新依赖）：services.msc 可启停、延迟自动启动、崩溃自动重启、关机时正常停止；安装程序和 `安装.bat` 可选服务或计划任务；CI 增加服务注册、启停、卸载测试。
  - 打包：本地 `build_exe.bat` 和 GitHub Actions 都会先跑测试再打包，打包后自检（OPC UA + HTTPS 设置页面），生成 `ChromExtractOPC.exe.sha256` 校验文件。
