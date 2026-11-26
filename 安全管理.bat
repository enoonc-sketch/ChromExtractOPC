@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 安全管理菜单（体检 / 一键加固 / 账号管理）需要在命令行里输入密码，所以用命令行版 ChromExtractOPC-cli.exe。
rem 同样的功能在桌面主窗口的“安全设置…”和“系统设置”页里也能做。
if exist ChromExtractOPC-cli.exe (
  ChromExtractOPC-cli.exe --admin
) else if exist dist\ChromExtractOPC-cli.exe (
  dist\ChromExtractOPC-cli.exe --admin
) else if exist server_app.py (
  where py >nul 2>nul && (py -3 server_app.py --admin & goto :end)
  where python >nul 2>nul && (python server_app.py --admin & goto :end)
  echo 没有找到 Python，也没有打包好的 EXE。请先双击 build_exe.bat 打包。
) else (
  echo 找不到 ChromExtractOPC-cli.exe 或 server_app.py。
)
:end
pause
