@echo off
chcp 65001 >nul
setlocal
rem 给 OPC UA 服务开放防火墙端口，并且只允许指定的客户端地址访问。
rem 用法（右键 → 以管理员身份运行）：
rem     设置防火墙.bat 192.168.1.0/24            只允许这个网段
rem     设置防火墙.bat 192.168.1.20,192.168.1.21  只允许这两台
rem     设置防火墙.bat remove                     删除这条规则
net session >nul 2>nul
if errorlevel 1 (
  echo 需要管理员权限：请右键本文件，选择"以管理员身份运行"。
  pause
  exit /b 1
)
set "RULE=ChromExtractOPC-4840"
if /i "%~1"=="remove" (
  netsh advfirewall firewall delete rule name="%RULE%"
  pause
  exit /b 0
)
if "%~1"=="" (
  echo 请带上允许访问的客户端地址，例如：
  echo     设置防火墙.bat 192.168.1.0/24
  echo 不限制来源是不安全的，所以本脚本不提供"全部放行"。
  pause
  exit /b 1
)
set "PORT=4840"
if not "%~2"=="" set "PORT=%~2"
netsh advfirewall firewall delete rule name="%RULE%" >nul 2>nul
netsh advfirewall firewall add rule name="%RULE%" dir=in action=allow protocol=TCP localport=%PORT% remoteip=%~1 profile=any
if errorlevel 1 (echo 设置失败。& pause & exit /b 1)
echo.
echo 已设置：只允许 %~1 访问本机 TCP %PORT% 端口。
echo 提示：如果之前弹窗时选过"允许所有网络访问"，请到 Windows 防火墙里删除那条旧规则，否则限制不生效。
pause
