@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ---- 找一个真正能用的 Python 3.10+（Microsoft Store 的假 python.exe 会被自动排除）----
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto :nopy

set "VER=?"
for /f "delims=" %%v in ('%PY% -c "import chrom_core; print(chrom_core.__version__)"') do set "VER=%%v"

echo ============================================
echo   色谱提取 OPC UA 服务 —— 一键打包为 EXE
echo   ChromExtractOPC v%VER%
echo ============================================
echo.
echo 使用的 Python：
%PY% --version
echo.

if exist build_env\Scripts\python.exe (
  echo [1/4] 已有构建环境 build_env，直接复用...
) else (
  echo [1/4] 创建独立构建环境 build_env ...
  %PY% -m venv build_env
  if errorlevel 1 goto :fail
)

echo [2/4] 安装依赖 asyncua / cryptography / pyinstaller ...
build_env\Scripts\python -m pip install --disable-pip-version-check --upgrade pip -q
if errorlevel 1 goto :fail
build_env\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt -q
if errorlevel 1 goto :fail

echo      运行测试 ...
build_env\Scripts\python -m unittest discover -s tests
if errorlevel 1 goto :fail
echo [3/4] 使用 PyInstaller 打包（首次约需 1 到 3 分钟）...
if exist dist\ChromExtractOPC.exe del /q dist\ChromExtractOPC.exe
build_env\Scripts\python -m PyInstaller --noconfirm --clean --onefile --console --noupx ^
  --name ChromExtractOPC ^
  --icon assets\app.ico ^
  --add-data "assets\app.ico;assets" ^
  --collect-all asyncua ^
  --hidden-import tkinter ^
  --hidden-import demo_data ^
  --hidden-import web_settings ^
  --hidden-import desktop_ui ^
  --hidden-import config_tools ^
  --hidden-import alerts ^
  --hidden-import nettest ^
  --hidden-import stability ^
  --hidden-import probes ^
  --hidden-import drivercheck ^
  --hidden-import envwatch ^
  --hidden-import bgrun ^
  --hidden-import winservice ^
  --collect-submodules serial ^
  server_app.py
if errorlevel 1 goto :fail
if not exist dist\ChromExtractOPC.exe goto :fail

rem ---- 代码签名（可选）：配置了 SIGN_THUMBPRINT 或 SIGN_PFX 才签，否则跳过 ----
powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1 -Check
if not errorlevel 2 (
  echo      代码签名 ...
  powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1 dist\ChromExtractOPC.exe
  if errorlevel 1 goto :fail
)

echo [4/4] 验证打包结果（自检约需 10 秒）...
dist\ChromExtractOPC.exe --version
if errorlevel 1 goto :badexe
dist\ChromExtractOPC.exe --selftest
if errorlevel 1 goto :badexe

rem ---- 组装发布文件夹：这些文件缺一个，目标电脑上对应功能就用不了 ----
if not exist dist\config.json copy /y config.json dist\ >nul
for %%f in ("安装.bat" "卸载.bat" "启动服务.bat" "启动图形界面.bat" "安装开机自启.bat" "install_autostart.ps1" "设置防火墙.bat" "安全管理.bat" "打开设置页面.bat" "后台启动.bat" "停止后台服务.bat" "安装为Windows服务.bat" "卸载Windows服务.bat" "使用说明_OPCUA.md" "安全检查报告.md") do (
  if exist "%%~f" copy /y "%%~f" dist\ >nul
)
rem 生成 SHA256 校验值，拷到现场后可核对 EXE 没被篡改或损坏
powershell -NoProfile -Command "(Get-FileHash dist\ChromExtractOPC.exe -Algorithm SHA256).Hash | Out-File -Encoding ascii dist\ChromExtractOPC.exe.sha256"

echo.
echo ============================================
echo   打包完成，自检通过。
echo   把整个 dist 文件夹拷到目标电脑，右键
echo   "安装.bat" 选择"以管理员身份运行"即可。
echo   （只想试一下：双击 ChromExtractOPC.exe）
echo ============================================
pause
exit /b 0

:nopy
echo 没有找到可用的 Python 3.10 以上版本。
echo 请安装： https://www.python.org/downloads/
echo 安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
pause
exit /b 1

:badexe
echo.
echo 打包出了 EXE，但自检没通过，这个 EXE 不能用。
echo 请把上面的文字截图或复制给技术支持。
echo 如果提示被拦截或文件消失，多半是杀毒软件误删，请把本文件夹加入白名单后重新打包。
pause
exit /b 1

:fail
echo.
echo 打包失败，请把上面的报错信息截图或复制给技术支持。
pause
exit /b 1
