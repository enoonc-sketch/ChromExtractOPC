@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist dist\ChromExtractOPC.exe (
  dist\ChromExtractOPC.exe --admin
) else if exist ChromExtractOPC.exe (
  ChromExtractOPC.exe --admin
) else if exist server_app.py (
  where py >nul 2>nul && (py -3 server_app.py --admin & goto :end)
  where python >nul 2>nul && (python server_app.py --admin & goto :end)
  echo 没有找到 Python，也没有打包好的 EXE。请先双击 build_exe.bat 打包。
) else (
  echo 找不到 ChromExtractOPC.exe 或 server_app.py。
)
:end
pause
