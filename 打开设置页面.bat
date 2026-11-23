@echo off
cd /d "%~dp0"
rem Open the desktop settings window (no service started).
rem v2.6: the desktop window replaces the browser settings page.
if exist ChromExtractOPC.exe (
  start "" ChromExtractOPC.exe --settings
) else if exist dist\ChromExtractOPC.exe (
  start "" dist\ChromExtractOPC.exe --settings
) else (
  python server_app.py --settings
)
