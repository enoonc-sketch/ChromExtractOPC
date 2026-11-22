@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 服务已在运行：直接打开浏览器；否则单独启动设置页面
powershell -NoProfile -Command "try{(New-Object Net.Sockets.TcpClient('127.0.0.1',8840)).Close();exit 0}catch{exit 1}" >nul 2>nul
if not errorlevel 1 (
  start "" http://127.0.0.1:8840/
  exit /b 0
)
if exist ChromExtractOPC.exe (
  ChromExtractOPC.exe --settings
) else if exist dist\ChromExtractOPC.exe (
  dist\ChromExtractOPC.exe --settings
) else (
  python server_app.py --settings
)
