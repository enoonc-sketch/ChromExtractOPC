@echo off
cd /d "%~dp0"
if exist ChromExtractOPC.exe (
  start "" ChromExtractOPC.exe --settings
) else if exist dist\ChromExtractOPC.exe (
  start "" dist\ChromExtractOPC.exe --settings
) else (
  echo ChromExtractOPC.exe not found.
  pause
)
