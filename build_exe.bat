@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   色谱提取 OPC UA 服务 —— 一键打包为 EXE
echo   ChromExtractOPC v2.0
echo ============================================
echo.

where py >nul 2>nul && (set "PY=py -3" & goto :havepy)
where python >nul 2>nul && (set "PY=python" & goto :havepy)
echo 没有找到 Python 3。请先安装 Python 3.8 以上版本：
echo   https://www.python.org/downloads/
echo 安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
pause
exit /b 1

:havepy
echo 找到 Python：
%PY% --version
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
if errorlevel 1 (
  echo Python 版本过低，需要 3.8 以上。请升级 Python 后再试。
  pause
  exit /b 1
)

if exist build_env\Scripts\python.exe (
  echo [1/3] 已有构建环境 build_env，直接复用...
) else (
  echo [1/3] 创建独立构建环境（build_env）...
  %PY% -m venv build_env
  if errorlevel 1 goto :fail
)

echo [2/3] 安装依赖（asyncua / cryptography / pyinstaller）...
build_env\Scripts\python -m pip install --upgrade pip -q
if errorlevel 1 goto :fail
build_env\Scripts\pip install -r requirements.txt -q
if errorlevel 1 goto :fail

echo [3/3] 使用 PyInstaller 打包（首次打包约需 1~3 分钟）...
build_env\Scripts\pyinstaller --noconfirm --clean --onefile --console ^
  --name ChromExtractOPC ^
  --collect-all asyncua ^
  --hidden-import tkinter ^
  server_app.py
if errorlevel 1 goto :fail

if not exist dist\ChromExtractOPC.exe (
  echo 打包失败：未生成 dist\ChromExtractOPC.exe，请把上面的报错截图或复制下来寻求支持。
  pause
  exit /b 1
)

copy /y config.json dist\ >nul
copy /y "启动服务.bat" dist\ >nul
copy /y "使用说明_OPCUA.md" dist\ >nul
echo.
echo ============================================
echo   打包完成！
echo   程序：dist\ChromExtractOPC.exe
echo   配置：dist\config.json（部署前按实际目录改 watch_dirs）
echo   启动：dist\启动服务.bat
echo   使用：把 dist 文件夹整个拷到目标电脑，
echo         双击 ChromExtractOPC.exe 即可启动 OPC UA 服务
echo ============================================
pause
exit /b 0

:fail
echo.
echo 打包失败，请把上面的报错信息截图或复制下来寻求支持。
pause
exit /b 1
