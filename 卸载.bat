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
echo 将停止服务，并删除开机自启任务和防火墙规则。
echo 程序文件夹、配置、日志和证书不会被删除，需要的话请手动删。
pause
schtasks /End /TN ChromExtractOPC >nul 2>nul
if exist "%~dp0ChromExtractOPC.exe" "%~dp0ChromExtractOPC.exe" --no-pause --uninstall-service
sc stop ChromExtractOPC >nul 2>nul
sc delete ChromExtractOPC >nul 2>nul
if exist "%~dp0ChromExtractOPC.exe" "%~dp0ChromExtractOPC.exe" --no-pause --stop
taskkill /IM ChromExtractOPC.exe /F >nul 2>nul
if exist "%~dp0install_autostart.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" -Remove
)
netsh advfirewall firewall delete rule name="ChromExtractOPC-4840" >nul 2>nul
echo.
echo 已卸载。
pause
