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
set "EXE=%~dp0ChromExtractOPC.exe"
if not exist "%EXE%" set "EXE=%~dp0dist\ChromExtractOPC.exe"
if exist "%EXE%" (
  "%EXE%" --no-pause --uninstall-service
) else (
  sc stop ChromExtractOPC >nul 2>nul
  sc delete ChromExtractOPC
)
echo 程序文件、配置和日志都保留。需要开机自启可再运行 安装为Windows服务.bat 或 安装开机自启.bat。
pause
