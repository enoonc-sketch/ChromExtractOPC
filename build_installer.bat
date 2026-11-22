@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 生成安装程序 dist-installer\ChromExtractOPC-Setup-版本.exe
rem 需要：先运行 build_exe.bat；安装 Inno Setup 6.3 以上（自带简体中文） https://jrsoftware.org/isdl.php
if not exist dist\ChromExtractOPC.exe (
  echo 找不到 dist\ChromExtractOPC.exe，请先双击 build_exe.bat 打包。
  pause
  exit /b 1
)
set "ISCC="
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe") do (
  if exist "%%~p" set "ISCC=%%~p"
)
if not defined ISCC (
  where iscc >nul 2>nul && set "ISCC=iscc"
)
if not defined ISCC (
  echo 没有找到 Inno Setup。请安装 Inno Setup 6.3 以上：https://jrsoftware.org/isdl.php
  echo 或者运行：winget install JRSoftware.InnoSetup
  pause
  exit /b 1
)
set "VER=0"
for /f "tokens=2" %%v in ('dist\ChromExtractOPC.exe --version') do set "VER=%%v"
echo 生成安装程序 v%VER% ...
set "SIGNARGS="
powershell -NoProfile -ExecutionPolicy Bypass -File sign.ps1 -Check
if not errorlevel 2 (
  rem $f = Inno 传入的待签名文件（已带引号），$q = 引号。安装程序和卸载程序都会被签名。
  set SIGNARGS=/DSIGN "/Ssigntool=powershell.exe -NoProfile -ExecutionPolicy Bypass -File $q%~dp0sign.ps1$q $f"
)
"%ISCC%" /Qp /DMyAppVersion=%VER% %SIGNARGS% installer\ChromExtractOPC.iss
if errorlevel 1 (
  echo 生成安装程序失败。如果提示找不到 ChineseSimplified.isl，请升级到 Inno Setup 6.3 以上。
  pause
  exit /b 1
)
powershell -NoProfile -Command "Get-ChildItem dist-installer\*.exe | ForEach-Object { (Get-FileHash $_.FullName -Algorithm SHA256).Hash | Out-File -Encoding ascii ($_.FullName + '.sha256') }"
echo.
echo 完成：dist-installer\ChromExtractOPC-Setup-%VER%.exe
echo 拷到现场电脑双击运行即可（需要管理员权限）。
pause
