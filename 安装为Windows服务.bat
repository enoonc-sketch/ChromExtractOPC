@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 注册为 Windows 服务：开机自动启动（延迟启动）、崩溃自动重启、在 services.msc 里可启停。右键 -> 以管理员身份运行。
net session >nul 2>nul
if errorlevel 1 (
  echo 需要管理员权限：请右键本文件，选择"以管理员身份运行"。
  pause
  exit /b 1
)
set "EXE=%~dp0ChromExtractOPC.exe"
if not exist "%EXE%" set "EXE=%~dp0dist\ChromExtractOPC.exe"
if not exist "%EXE%" (
  echo 找不到 ChromExtractOPC.exe。
  pause
  exit /b 1
)
"%EXE%" --no-pause --install-service
echo.
echo 管理服务：Win+R 输入 services.msc，找到"色谱提取 OPC UA 服务"。
echo 设置页面：http://127.0.0.1:8840/      卸载服务：卸载Windows服务.bat
pause
