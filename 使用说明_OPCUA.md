# 色谱提取 OPC UA 服务（ChromExtractOPC）使用说明

把 GCP DataBrowser 日志数据库（`*_V3.db`）或 `.mbd` 谱图文件里的谱图曲线提取出来，
一方面照旧导出 **CSV 数据表**，另一方面作为 **OPC UA Server** 对外发布，供 DCS / SCADA /
实时数据库 / 组态软件直接订阅读取。

- 服务端基于标准 **OPC UA（IEC 62541）**，任何符合规范的客户端均可连接
  （KEPServerEX / UA Expert / WinCC / iFIX / Ignition / 各 DCS 的 OPC UA 客户端等）。
- 打包后是**单个 EXE**，目标电脑**无需安装 Python**，拷过去就能用。

---

## 1. 目录结构

| 文件 | 作用 |
|---|---|
| `server_app.py` | 主程序：OPC UA 服务 + 数据监控 + CSV 导出 + GUI 监控窗口 |
| `chrom_core.py` | 谱图解析核心库（BinTrc 解码、数据库/mbd 读取、CSV 输出） |
| `config.json` | 配置文件（端口、监控目录、扫描周期、安全模式等） |
| `build_exe.bat` | **一键打包为 EXE**（在有 Python 的电脑上运行一次即可） |
| `启动服务.bat` | 启动服务（优先用打包好的 EXE，其次用 Python 源码运行） |
| `启动图形界面.bat` | 以图形窗口方式启动（自动隐藏黑色命令行窗口） |
| `安装.bat` / `卸载.bat` | **一键安装 / 卸载**（自检 → 复制到 C:\ChromExtractOPC → 设数据目录 → 防火墙 → 开机自启 → 启动） |
| `安装开机自启.bat`、`install_autostart.ps1` | 只设置开机自启（不限时运行，崩溃后自动重启） |
| `demo_data.py` | 演示数据生成（打包进 EXE，供 `--selftest` 自检使用） |
| `requirements.txt` | 依赖清单 |
| `tools\make_demo_db.py` | 生成演示数据库，用于验收测试 |
| `tools\opcua_test_client.py` | OPC UA 验收测试客户端 |
| `dist\ChromExtractOPC.exe` | 打包产物（执行 build_exe.bat 后生成） |

## 2. 三步上手

**第 1 步：打包 EXE**（在一台装了 Python 3.10+ 的电脑上，只需做一次）

1. 双击 `build_exe.bat`，等待出现"打包完成，自检通过"。脚本会在打包后**真的启动一次服务并用客户端读取演示数据**，
   自检不通过就不会宣布成功，避免拿到一个不能用的 EXE。
2. 产物在 `dist\` 文件夹，里面已经包含 EXE、配置、安装/卸载脚本、防火墙脚本等全部文件。

**第 2 步：部署到现场电脑**

1. 把 `dist` 文件夹**整个**拷贝到现场电脑（不要只拷 EXE）。
2. 右键 `安装.bat` → **以管理员身份运行**，按提示操作：
   - 先自动自检，确认程序在这台电脑上能运行；
   - 输入色谱数据所在文件夹（存放 `*_V3.db` / `.mbd` 的目录），程序会写进配置并告诉你找到了几个数据文件；
   - 输入允许访问的客户端地址（防火墙，可跳过）；
   - 设置开机自启并立即启动。
3. 想先试一下、不安装：双击 `ChromExtractOPC.exe`，或双击 `启动图形界面.bat`。
   出错时窗口会停住显示原因，不会一闪而过。
4. 手动改配置的话，编辑 `config.json` 的 `watch_dirs`（路径里的反斜杠要写两个）：
   ```json
   "watch_dirs": ["D:\\GCP\\DataBrowser"]
   ```
   或者用命令：`ChromExtractOPC.exe --set-watch-dir "D:\GCP\DataBrowser"`。

**第 3 步：验收**

- 用任意 OPC UA 客户端连接 `opc.tcp://<本机IP>:4840/ChromExtract`，浏览
  `Objects → Chromatograph` 即可看到数据；或用本项目自带的测试客户端：
  ```
  python tools\opcua_test_client.py opc.tcp://127.0.0.1:4840/ChromExtract/
  ```
- 服务运行后会**自动扫描**监控目录（默认每 30 秒），色谱仪每出一个新周期，
  对应节点的数据自动刷新，无需人工干预。

## 3. config.json 配置项

| 键 | 默认值 | 说明 |
|---|---|---|
| `endpoint` | `opc.tcp://0.0.0.0:4840/ChromExtract` | 监听地址与端口。只监听本机可改成 `opc.tcp://127.0.0.1:4840/...`；改端口直接换数字 |
| `server_name` | 色谱提取 OPC UA 服务 | 客户端看到的服务器名称 |
| `namespace_uri` | `urn:chromextract:opcua` | 命名空间 URI（一般不用改） |
| `watch_dirs` | `["data"]` | **要监控的数据目录列表**，可写多个；目录里的 `*_V3.db` 和 `*.mbd` 都会被解析 |
| `poll_seconds` | `30` | 扫描周期（秒），最小 5 |
| `last_n` | `5` | 每个流路取最近 N 个周期参与"最新值"比较 |
| `security` | `none` | 安全模式：`none` / `sign`（签名）/ `sign_encrypt`（签名+加密）。非 none 时首次启动自动生成自签名证书到 `certs\` |
| `cert_dir` | `certs` | 证书目录 |
| `csv_export_dir` | `谱图导出` | CSV 导出的默认输出目录 |
| `log_file` | `logs/server.log` | 日志文件 |
| `max_points` | `20000` | 单条曲线发布到 OPC UA 的最大点数（超出自动等间隔抽点） |

## 4. OPC UA 地址空间（节点表）

根节点：`Objects → Chromatograph`（命名空间 URI：`urn:chromextract:opcua`）

```
Chromatograph
├─ ServerVersion                 String   服务版本
├─ WatchDirs                     String   监控目录
├─ ExportCSV()                   方法     远程触发 CSV 导出，返回输出文件夹路径
└─ Station_<仪器序列号>
   ├─ Info
   │  ├─ SerialNumber            String   仪器序列号
   │  └─ SourceFile              String   数据来源文件名
   └─ App<应用号>_Stream<流路号>_Det<检测器号>
      ├─ AppName / StreamName    String   应用名 / 流路名
      ├─ LatestCycle
      │  ├─ CycleTime            String   周期时间（判断新数据的依据）
      │  ├─ CycleId              Int32    周期编号
      │  ├─ IsValidation         Boolean  是否验证周期
      │  ├─ CalibrationLevel     Int32    标定级别
      │  ├─ NumPoints            Int32    点数
      │  ├─ SamplePeriod         Double   采样间隔（秒）
      │  ├─ Duration             Double   总时长（秒）
      │  ├─ DetName              String   检测器名（如 FID1）
      │  ├─ YTitle / XTitle      String   信号轴 / 时间轴名称
      │  └─ MinRaw / MaxRaw      Double   原始值范围
      ├─ Trace
      │  ├─ TimeArray            Double[] 时间序列（秒）
      │  ├─ RawArray             Double[] 原始计数
      │  ├─ MicrovoltArray       Double[] 微伏值
      │  └─ ValueArray           Double[] 显示值（工程量）
      └─ Status
         ├─ Revision             Int32    每次刷新 +1，可用来判断数据更新
         ├─ LastUpdate           String   最后刷新时间
         └─ Good                 Boolean  数据质量位
```

**DCS / 上位机接入建议**：订阅 `LatestCycle.CycleTime`（或 `Status.Revision`）判断新周期，
新周期到来时批量读取 `Trace.ValueArray`（时间对应用 `Trace.TimeArray`），并把
`LatestCycle` 下的元数据一并入库。也可以直接对数组变量建订阅，按死区/周期刷新。

## 5. 安全模式（可选）

- 默认 `none`：适合隔离的工控网段，连接最简单。
- 内网要求加密时，把 `security` 改为 `"sign_encrypt"` 并重启：首次启动会在
  `certs\` 下自动生成自签名证书（有效期 10 年）。客户端侧需信任该证书
  （把 `certs\server_cert.der` 导入客户端的受信任证书库）。
- 更严格的场合可由企业 CA 签发证书后替换 `certs\` 下的文件。

## 6. 开机自启

**推荐：`安装.bat`（或只设自启：右键 `安装开机自启.bat` 以管理员身份运行）。**

- 任务设为**运行时间不限**，程序异常退出后 **1 分钟自动重启**。
  （注意：用 `schtasks /Create` 手工创建的任务，Windows 默认会在运行满 72 小时后强制结束它，服务会莫名停掉，所以不要手工创建。）
- 可选以当前账号运行（更安全，需 Windows 密码）或 SYSTEM 账号运行（不需要密码）。
- 卸载：`卸载.bat`，或 `安装开机自启.bat remove`。

**备选：NSSM 注册为 Windows 服务**

1. 下载 NSSM（https://nssm.cc），执行：
   ```
   nssm install ChromExtractOPC C:\ChromExtractOPC\ChromExtractOPC.exe
   nssm start ChromExtractOPC
   ```
2. 卸载服务：`nssm remove ChromExtractOPC confirm`。

## 7. 原有 CSV 提取功能（完整保留）

```
ChromExtractOPC.exe --extract 数据文件夹 -o 输出文件夹 --last 20
ChromExtractOPC.exe --list 数据文件夹          :: 查看里面有什么
ChromExtractOPC.exe --gui                      :: 打开图形监控窗口
```

- `--extract` 导出结果：`index.csv`（总清单）+ `csv\`（每条谱图一个 CSV：
  时间、原始计数、微伏、显示值）。
- 服务运行中也可**远程触发**导出：调用 `Chromatograph → ExportCSV` 方法即可，
  导出位置为程序目录下 `谱图导出\`。
- 读取数据库使用**副本**，不影响 DataBrowser 正常运行。

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| 客户端连不上 | 检查防火墙是否放行 4840；`netstat -ano | findstr 4840` 确认在监听 |
| 端口被占用 | 改 `config.json` 的 `endpoint` 端口（如 4841），客户端同步修改 |
| 看不到数据 | 确认 `watch_dirs` 路径正确且包含 `*_V3.db` 或 `*.mbd`；查看 `logs\server.log` |
| 改了 config 不生效 | 账号、匿名开关、监控目录、扫描间隔等会在下一轮扫描内自动生效；端口、加密方式、证书需重启 |
| 不确定 EXE 本身有没有问题 | 命令行运行 `ChromExtractOPC.exe --selftest`：通过说明程序正常，问题在配置/网络/防火墙 |
| 双击 EXE 窗口一闪就没了 | 新版出错时会停住并显示原因；日志在 `logs\server.log`，未预期的崩溃记在 `logs\crash.log` |
| 杀毒软件删了 EXE / 提示危险 | PyInstaller 打包的程序常被误报。把程序文件夹加入白名单后重新打包；`安装.bat` 的自检能发现文件被破坏 |
| 加密模式客户端报证书错误 | 把 `certs\server_cert.der` 导入客户端受信任列表，或先用 `none` 模式排通 |

## 9. 从源码重新打包 / 开发调试

- 源码模式直接运行：`python server_app.py`（需 `pip install -r requirements.txt`）。
- 重新打包：再次双击 `build_exe.bat`。
- 生成测试数据：`python tools\make_demo_db.py data\demo_V3.db`。
- 验证打包结果：`ChromExtractOPC.exe --selftest`（在临时目录起真实服务并读取，不影响现有配置）。

---

## 安全设置

**最省事的做法：双击 `安全管理.bat`**，出现菜单：

| 选项 | 作用 |
|---|---|
| 1 安全体检 | 用"正常 / 注意 / 风险"告诉你当前哪里不安全 |
| 2 一键加固 | 一步完成：开启加密 + 关闭匿名 + 创建登录账号 |
| 3 添加账号 / 重设密码 | 密码输入时不显示，两次确认，会拦截"12345678"这类弱密码 |
| 4 删除账号 | 不允许删掉最后一个账号（否则没人能连） |
| 5 查看账号 | |

- **不需要重启：** 账号、匿名开关、导出限制、监控目录、扫描间隔，改完下一轮扫描（默认 30 秒内）自动生效；**加密方式、端口、证书**这些需要重启服务。
- **窗口模式**（`ChromExtractOPC.exe --gui`）里有"安全设置…"按钮，可以在窗口里做同样的事，状态栏会显示安全体检摘要。
- 配置文件改坏了不用怕：每次保存前都会备份成 `config.json.bak`，而且服务运行中如果配置文件写坏或不合法，会**忽略这次修改、继续用原来的设置**，并在日志里告诉你。
- 命令行方式（高级）：`--check-security`、`--harden`、`--add-user 用户名`、`--remove-user 用户名`、`--list-users`。
- `设置防火墙.bat 192.168.1.0/24`（右键以管理员身份运行）：只允许指定地址访问。
- `安装开机自启.bat`（右键以管理员身份运行）：让服务开机自动运行；`安装开机自启.bat remove` 卸载。**这个脚本我没有在 Windows 上实际试过**，首次使用请重启一次电脑验证。
- 登录成功、登录失败、被拒绝的客户端都会记在 `logs\server.log`。
- 完整的检查结果见 **安全检查报告.md**。
- 新增节点：`Heartbeat`（每轮扫描 +1）和 `LastScanTime`，用来判断服务是否还活着；数据源丢失时，对应通道的 `Status.Good` 会变成 `False`。
- 运行自动测试：`python -m unittest discover -s tests`（约 1 分钟）。
- Python 版本要求是 **3.10 及以上**。
