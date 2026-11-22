@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 后台静默启动：不显示窗口，日志在 logs\server.log。已在运行时不会重复启动。
set "EXE=ChromExtractOPC.exe"
if not exist "%EXE%" set "EXE=dist\ChromExtractOPC.exe"
if not exist "%EXE%" (
  echo 找不到 ChromExtractOPC.exe。
  pause
  exit /b 1
)
start "" /b "%EXE%" --background
timeout /t 4 /nobreak >nul
"%EXE%" --status
echo.
echo 设置页面：http://127.0.0.1:8840/    停止服务：双击 停止后台服务.bat
timeout /t 6 >nul
