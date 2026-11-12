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

在一台装了 Python 3.8+ 的电脑上，双击 `build_exe.bat`，产物在 `dist\` 文件夹。

## 部署

1. 把 `dist` 文件夹整个拷贝到现场电脑（例如 `D:\ChromExtractOPC\`）。
2. 编辑同目录的 `config.json`，把 `watch_dirs` 改成实际的日志数据库所在目录，例如：
   ```json
   "watch_dirs": ["D:\\GCP\\DataBrowser"]
   ```
3. 双击 `ChromExtractOPC.exe` 启动。窗口显示
   `OPC UA 服务已启动：opc.tcp://0.0.0.0:4840/ChromExtract` 即成功。
4. Windows 防火墙首次弹窗时选择“允许访问”。

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

## OPC UA 地址空间

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
