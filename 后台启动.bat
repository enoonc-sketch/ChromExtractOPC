@echo off
cd /d "%~dp0"
rem Start the service silently in the background. Log: logs\server.log.
rem Does nothing if the service is already running.
set "EXE=ChromExtractOPC.exe"
if not exist "%EXE%" set "EXE=dist\ChromExtractOPC.exe"
if not exist "%EXE%" (
  echo ChromExtractOPC.exe not found.
  pause
  exit /b 1
)
start "" /b "%EXE%" --background
timeout /t 4 /nobreak >nul
"%EXE%" --status
echo.
echo Settings: open the desktop window (app icon / Start Menu shortcut).
echo The web page is still available at http://127.0.0.1:8840/ for remote access.
echo Stop the service from the Start Menu shortcut.
timeout /t 6 >nul
