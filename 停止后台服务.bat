@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "EXE=ChromExtractOPC.exe"
if not exist "%EXE%" set "EXE=dist\ChromExtractOPC.exe"
rem 开机自启任务在运行时，先结束任务，避免被计划任务立刻重新拉起
schtasks /End /TN ChromExtractOPC >nul 2>nul
rem 注册成 Windows 服务时：通过服务管理器停止（需要管理员）；没有权限时下面的 --stop 也能正常停止
sc stop ChromExtractOPC >nul 2>nul
"%EXE%" --stop
pause
