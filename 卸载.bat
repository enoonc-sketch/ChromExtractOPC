@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
net session >nul 2>nul
if errorlevel 1 (
  echo 需要管理员权限：请右键本文件，选择"以管理员身份运行"。
  pause
  exit /b 1
)
echo 将关闭程序窗口（停止服务），删除快捷方式（含“登录后自动打开窗口”）和防火墙规则，并清理旧版本留下的开机自启任务 / Windows 服务。
echo 程序文件夹、配置、日志和证书不会被删除，需要的话请手动删。
pause
taskkill /IM ChromExtractOPC.exe /F >nul 2>nul
taskkill /IM ChromExtractOPC-cli.exe /F >nul 2>nul
rem v2.8 及以前版本的后台运行残留
schtasks /End /TN ChromExtractOPC >nul 2>nul
schtasks /Delete /TN ChromExtractOPC /F >nul 2>nul
sc stop ChromExtractOPC >nul 2>nul
sc delete ChromExtractOPC >nul 2>nul
netsh advfirewall firewall delete rule name="ChromExtractOPC-4840" >nul 2>nul
rem 桌面 / 开始菜单快捷方式，以及 v3.0“登录后自动打开窗口”放在当前用户启动文件夹里的快捷方式
powershell -NoProfile -ExecutionPolicy Bypass -Command "foreach($d in @([Environment]::GetFolderPath('CommonDesktopDirectory'),[Environment]::GetFolderPath('CommonPrograms'),[Environment]::GetFolderPath('Startup'))){ Remove-Item -LiteralPath (Join-Path $d '色谱提取 OPC UA 服务.lnk') -ErrorAction SilentlyContinue }"
echo.
echo 已卸载。
pause
