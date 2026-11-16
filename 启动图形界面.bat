@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ChromExtractOPC.exe (
  start "" ChromExtractOPC.exe --gui
) else if exist dist\ChromExtractOPC.exe (
  start "" dist\ChromExtractOPC.exe --gui
) else (
  echo 找不到 ChromExtractOPC.exe。
  pause
)
