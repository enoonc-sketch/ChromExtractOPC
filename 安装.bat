@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo   色谱提取 OPC UA 服务 —— 一键安装（桌面软件）
echo ============================================
echo.
net session >nul 2>nul
if errorlevel 1 (
  echo 需要管理员权限：请右键本文件，选择"以管理员身份运行"。
  pause
  exit /b 1
)
if not exist "%~dp0ChromExtractOPC.exe" goto :missing
if not exist "%~dp0ChromExtractOPC-cli.exe" goto :missing

echo [1/5] 自检：确认程序在这台电脑上能正常运行...
"%~dp0ChromExtractOPC-cli.exe" --no-pause --selftest
if errorlevel 1 (
  echo.
  echo 自检没通过，已停止安装。请把上面的文字截图给技术支持。
  echo 常见原因：杀毒软件拦截或删除了文件。请把本文件夹加入白名单后重试。
  pause
  exit /b 1
)

set "DEST=C:\ChromExtractOPC"
echo.
echo [2/5] 安装到 %DEST% ...
tasklist /FI "IMAGENAME eq ChromExtractOPC.exe" 2>nul | find /I "ChromExtractOPC.exe" >nul
if not errorlevel 1 (
  echo 检测到程序窗口正在运行。请先关闭"色谱提取 OPC UA 服务"窗口（会停止服务），再按任意键继续。
  pause
)
taskkill /IM ChromExtractOPC.exe /F >nul 2>nul
taskkill /IM ChromExtractOPC-cli.exe /F >nul 2>nul
rem 清理 v2.8 及以前版本的后台运行方式（开机自启计划任务 / Windows 服务），新版本只以桌面窗口运行
schtasks /End /TN ChromExtractOPC >nul 2>nul
schtasks /Delete /TN ChromExtractOPC /F >nul 2>nul
sc stop ChromExtractOPC >nul 2>nul
sc delete ChromExtractOPC >nul 2>nul
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
echo [3/5] 设置色谱数据所在的文件夹
echo   就是存放 *_V3.db 或 .mbd 文件的文件夹，例如 D:\Maxum\Data
echo   也可以稍后在程序窗口"数据与导出"页点"选择数据目录…"。
set "WD="
set /p "WD=请输入文件夹路径（直接回车 = 稍后在窗口里选）："
if defined WD (
  if "%WD:~-1%"=="\" set "WD=%WD:~0,-1%"
  "%DEST%\ChromExtractOPC-cli.exe" --no-pause --set-watch-dir "%WD%"
)

echo.
echo [4/5] 防火墙：只允许指定的客户端地址访问（强烈建议）
echo   例如 DCS 所在网段 192.168.1.0/24，多个地址用逗号隔开。
set "FW="
set /p "FW=请输入允许访问的地址（直接回车 = 跳过）："
if defined FW call "%DEST%\设置防火墙.bat" "%FW%"

echo.
echo [5/5] 创建桌面和开始菜单快捷方式...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; foreach($d in @([Environment]::GetFolderPath('CommonDesktopDirectory'),[Environment]::GetFolderPath('CommonPrograms'))){ $s=$w.CreateShortcut((Join-Path $d '色谱提取 OPC UA 服务.lnk')); $s.TargetPath='%DEST%\ChromExtractOPC.exe'; $s.WorkingDirectory='%DEST%'; $s.IconLocation='%DEST%\ChromExtractOPC.exe,0'; $s.Save() }"
if errorlevel 1 echo 快捷方式没有创建成功，可直接双击 %DEST%\ChromExtractOPC.exe 使用。

echo.
echo ============================================
echo   安装完成（桌面软件，不在后台运行）。
echo   使用：双击桌面"色谱提取 OPC UA 服务"，窗口打开即启动服务；
echo         关闭窗口即停止服务，DCS/SCADA 需要数据时请保持窗口打开。
echo   程序位置： %DEST%
echo   配置文件： %DEST%\config.json
echo   运行日志： %DEST%\logs\server.log
echo   OPC UA 地址： opc.tcp://本机IP:4840/ChromExtract
echo   卸载： 右键 "卸载.bat" 以管理员身份运行
echo ============================================
choice /c YN /n /m "现在打开程序窗口吗？(Y/N)："
if not errorlevel 2 start "" "%DEST%\ChromExtractOPC.exe"
exit /b 0

:missing
echo 找不到 ChromExtractOPC.exe 或 ChromExtractOPC-cli.exe。请把整个文件夹拷过来后再运行，不要只拷一个文件。
pause
exit /b 1
