@echo off
chcp 65001 >nul
setlocal
rem 让服务在电脑开机时自动运行（无需登录桌面）。右键 → 以管理员身份运行。
rem 卸载请运行：安装开机自启.bat remove
net session >nul 2>nul
if errorlevel 1 (
  echo 需要管理员权限：请右键本文件，选择"以管理员身份运行"。
  pause
  exit /b 1
)
set "TASK=ChromExtractOPC"
if /i "%~1"=="remove" (
  schtasks /Delete /TN "%TASK%" /F
  pause
  exit /b 0
)
set "EXE=%~dp0ChromExtractOPC.exe"
if not exist "%EXE%" set "EXE=%~dp0dist\ChromExtractOPC.exe"
if not exist "%EXE%" (
  echo 找不到 ChromExtractOPC.exe。请先双击 build_exe.bat 打包，并把本文件放在 exe 旁边。
  pause
  exit /b 1
)
echo 将以账号 %USERDOMAIN%\%USERNAME% 在开机时自动运行服务。
echo 这是普通权限运行（不是 SYSTEM），更安全。下面需要输入该账号的 Windows 密码：
schtasks /Create /TN "%TASK%" /TR "\"%EXE%\"" /SC ONSTART /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL LIMITED /F
if errorlevel 1 (
  echo 创建失败。常见原因：该账号没有设置密码（Windows 不允许空密码账号运行计划任务）。
  pause
  exit /b 1
)
echo.
echo 已创建开机自启任务 "%TASK%"。立即试运行：schtasks /Run /TN "%TASK%"
echo 卸载：安装开机自启.bat remove
pause
