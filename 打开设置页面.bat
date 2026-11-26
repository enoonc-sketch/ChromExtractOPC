@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 打开桌面主窗口，但不自动启动 OPC UA 服务（只改设置、做诊断时用；窗口里可随时点“启动服务”）。
if exist ChromExtractOPC.exe (
  start "" ChromExtractOPC.exe --settings
) else if exist dist\ChromExtractOPC.exe (
  start "" dist\ChromExtractOPC.exe --settings
) else if exist server_app.py (
  where pyw >nul 2>nul && (start "" pyw -3 server_app.py --settings & goto :eof)
  where pythonw >nul 2>nul && (start "" pythonw server_app.py --settings & goto :eof)
  echo 没有找到 Python，也没有打包好的 EXE。
  pause
) else (
  echo 找不到 ChromExtractOPC.exe。
  pause
)
