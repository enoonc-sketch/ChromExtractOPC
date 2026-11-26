@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 打开桌面主窗口（OPC UA 服务随窗口自动启动，关闭窗口即停止）。等同于双击 ChromExtractOPC.exe。
if exist ChromExtractOPC.exe (
  start "" ChromExtractOPC.exe
) else if exist dist\ChromExtractOPC.exe (
  start "" dist\ChromExtractOPC.exe
) else if exist server_app.py (
  rem 源码运行：用 pythonw 不弹黑色窗口
  where pyw >nul 2>nul && (start "" pyw -3 server_app.py & goto :eof)
  where pythonw >nul 2>nul && (start "" pythonw server_app.py & goto :eof)
  echo 没有找到 Python，也没有打包好的 EXE。请先双击 build_exe.bat 打包。
  pause
) else (
  echo 找不到 ChromExtractOPC.exe。
  pause
)
