@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo   色谱提取 OPC UA 服务 —— 一键安装
echo ============================================
echo.
net session >nul 2>nul
if errorlevel 1 (
  echo 需要管理员权限：请右键本文件，选择"以管理员身份运行"。
  pause
  exit /b 1
)
if not exist "%~dp0ChromExtractOPC.exe" (
  echo 找不到 ChromExtractOPC.exe。请把整个文件夹拷过来后再运行，不要只拷这一个文件。
  pause
  exit /b 1
)

echo [1/6] 自检：确认程序在这台电脑上能正常运行...
"%~dp0ChromExtractOPC.exe" --selftest
if errorlevel 1 (
  echo.
  echo 自检没通过，已停止安装。请把上面的文字截图给技术支持。
  echo 常见原因：杀毒软件拦截或删除了文件。请把本文件夹加入白名单后重试。
  pause
  exit /b 1
)

set "DEST=C:\ChromExtractOPC"
echo.
echo [2/6] 安装到 %DEST% ...
schtasks /End /TN ChromExtractOPC >nul 2>nul
taskkill /IM ChromExtractOPC.exe /F >nul 2>nul
if /i not "%~dp0"=="%DEST%\" (
  robocopy "%~dp0." "%DEST%" /E /XF config.json /NFL /NDL /NJH /NJS /NP >nul
  if errorlevel 8 (
    echo 复制文件失败。
    pause
    exit /b 1
  )
  if not exist "%DEST%\config.json" copy /y "%~dp0config.json" "%DEST%\" >nul
)
icacls "%DEST%" /grant "%USERDOMAIN%\%USERNAME%:(OI)(CI)M" /T /C >nul

echo.
echo [3/6] 设置色谱数据所在的文件夹
echo   就是存放 *_V3.db 或 .mbd 文件的文件夹，例如 D:\Maxum\Data
echo   提示：在资源管理器里打开该文件夹，点地址栏复制路径，在这里单击右键即可粘贴。
set "WD="
set /p "WD=请输入文件夹路径（直接回车 = 稍后自己改 config.json）："
if defined WD (
  if "%WD:~-1%"=="\" set "WD=%WD:~0,-1%"
  "%DEST%\ChromExtractOPC.exe" --set-watch-dir "%WD%"
)

echo.
echo [4/6] 防火墙：只允许指定的客户端地址访问（强烈建议）
echo   例如 DCS 所在网段 192.168.1.0/24，多个地址用逗号隔开。
set "FW="
set /p "FW=请输入允许访问的地址（直接回车 = 跳过）："
if defined FW call "%DEST%\设置防火墙.bat" "%FW%"

echo.
echo [5/6] 设置开机自动运行...
echo   1 = 注册为 Windows 服务（推荐：services.msc 里可启停，崩溃自动重启）
echo   2 = 使用计划任务（兼容旧版本）
echo   3 = 不设置开机自启
choice /c 123 /n /m "请按 1、2 或 3："
set "AS=%errorlevel%"
if "%AS%"=="1" (
  echo.
  echo [6/6] 注册服务并启动...
  "%DEST%\ChromExtractOPC.exe" --no-pause --install-service
) else if "%AS%"=="2" (
  "%DEST%\ChromExtractOPC.exe" --no-pause --uninstall-service >nul 2>nul
  call "%DEST%\安装开机自启.bat"
  echo.
  echo [6/6] 现在启动服务...
  schtasks /Run /TN ChromExtractOPC >nul 2>nul
) else (
  echo.
  echo [6/6] 已跳过开机自启。需要时双击 后台启动.bat 启动服务。
)
echo.
echo ============================================
echo   安装完成。
echo   程序位置： %DEST%
echo   配置文件： %DEST%\config.json
echo   运行日志： %DEST%\logs\server.log
echo   OPC UA 地址： opc.tcp://本机IP:4840/ChromExtract
echo   卸载： 右键 "卸载.bat" 以管理员身份运行
echo ============================================
pause
