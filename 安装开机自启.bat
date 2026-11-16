@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 让服务在电脑开机时自动在后台运行（不需要登录桌面）。右键 -> 以管理员身份运行。
rem 卸载： 安装开机自启.bat remove
net session >nul 2>nul
if errorlevel 1 (
  echo 需要管理员权限：请右键本文件，选择"以管理员身份运行"。
  pause
  exit /b 1
)
if not exist "%~dp0install_autostart.ps1" (
  echo 找不到 install_autostart.ps1，请和它放在同一个文件夹里。
  pause
  exit /b 1
)
if /i "%~1"=="remove" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" -Remove
  pause
  exit /b 0
)
set "EXE=%~dp0ChromExtractOPC.exe"
if not exist "%EXE%" set "EXE=%~dp0dist\ChromExtractOPC.exe"
if not exist "%EXE%" (
  echo 找不到 ChromExtractOPC.exe。请把本文件放在 exe 旁边。
  pause
  exit /b 1
)
echo 请选择运行身份：
echo   1 = 当前账号 %USERDOMAIN%\%USERNAME%（更安全，需要输入该账号的 Windows 密码）
echo   2 = SYSTEM 系统账号（不需要密码；账号没有设置密码时选这个）
choice /c 12 /n /m "请按 1 或 2："
if errorlevel 2 (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" -Exe "%EXE%" -System
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" -Exe "%EXE%"
)
if errorlevel 1 (
  pause
  exit /b 1
)
echo.
echo 立即试运行：  schtasks /Run /TN ChromExtractOPC
echo 卸载：  安装开机自启.bat remove
pause
