; ChromExtractOPC 安装程序（Inno Setup 6.3+）
; 编译：iscc /DMyAppVersion=2.4 installer\ChromExtractOPC.iss
; 需要先运行 build_exe.bat 生成 dist\ChromExtractOPC.exe
; 本文件必须保存为 UTF-8 带 BOM，否则中文会乱码。

#ifndef MyAppVersion
  #define MyAppVersion "2.4"
#endif
#define MyAppName "色谱提取 OPC UA 服务"
#define MyAppExe "ChromExtractOPC.exe"
#define Dist "..\dist"

[Setup]
AppId={{6E3B1C52-7A0D-4F0B-9C8E-2B1D5C0A4F71}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=ChromExtractOPC
; 程序把配置、日志、证书写在自己目录里，所以默认不装到 Program Files（普通账号写不进去）
DefaultDirName=C:\ChromExtractOPC
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist-installer
OutputBaseFilename=ChromExtractOPC-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
CloseApplications=no
SetupLogging=yes
#ifdef SIGN
; 由 build_installer.bat 传入 /DSIGN 和 /Ssigntool=...（见 sign.ps1）；安装程序和卸载程序都签名
SignTool=signtool
SignedUninstaller=yes
#endif

[Languages]
; 语言文件随仓库走（installer/Languages/）：运行器上 Inno Setup 6.7.1 自带的 Languages 目录里没有 ChineseSimplified.isl，写 compiler: 路径会编译失败
Name: "chs"; MessagesFile: "Languages\ChineseSimplified.isl"

[Tasks]
Name: "autostart"; Description: "开机自动在后台运行服务"; GroupDescription: "运行方式："
Name: "autostart\service"; Description: "注册为 Windows 服务（推荐：services.msc 里可启停，崩溃自动重启）"; GroupDescription: "运行方式："; Flags: exclusive
Name: "autostart\task"; Description: "使用计划任务（兼容旧版本）"; GroupDescription: "运行方式："; Flags: exclusive unchecked
Name: "desktopicon"; Description: "创建桌面快捷方式（图形监控窗口）"; GroupDescription: "快捷方式："
Name: "desktopsettings"; Description: "创建桌面快捷方式（设置页面）"; GroupDescription: "快捷方式："

[Dirs]
; 允许本机普通用户修改配置/日志（与原 安装.bat 的 icacls 一致）
Name: "{app}"; Permissions: users-modify
Name: "{app}\data"
Name: "{app}\logs"

[Files]
Source: "{#Dist}\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\ChromExtractOPC.exe.sha256"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 升级时保留现场的配置文件；卸载时也不删（除非用户选择删除全部数据）
Source: "..\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "..\install_autostart.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\启动服务.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\启动图形界面.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\打开设置页面.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\后台启动.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\停止后台服务.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\安装为Windows服务.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\卸载Windows服务.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\安全管理.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\设置防火墙.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\安装开机自启.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\使用说明_OPCUA.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\安全检查报告.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\图形监控窗口"; Filename: "{app}\{#MyAppExe}"; Parameters: "--gui"; WorkingDir: "{app}"
Name: "{group}\设置页面"; Filename: "{app}\打开设置页面.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExe}"
Name: "{group}\后台启动服务"; Filename: "{app}\{#MyAppExe}"; Parameters: "--background"; WorkingDir: "{app}"; Flags: runminimized
Name: "{group}\Windows 服务管理"; Filename: "{sys}\services.msc"
Name: "{group}\停止后台服务"; Filename: "{app}\停止后台服务.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExe}"
Name: "{group}\安全管理"; Filename: "{app}\安全管理.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExe}"
Name: "{group}\运行日志"; Filename: "{app}\logs"
Name: "{group}\使用说明"; Filename: "{app}\使用说明_OPCUA.md"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\色谱提取 OPC UA"; Filename: "{app}\{#MyAppExe}"; Parameters: "--gui"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{autodesktop}\色谱提取 OPC UA 设置"; Filename: "{app}\打开设置页面.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExe}"; Tasks: desktopsettings

[Run]
Filename: "http://127.0.0.1:8840/"; Description: "打开设置页面"; Flags: postinstall shellexec nowait skipifsilent; Tasks: autostart
Filename: "{app}\{#MyAppExe}"; Parameters: "--gui"; WorkingDir: "{app}"; Description: "启动图形监控窗口"; Flags: postinstall nowait skipifsilent unchecked; Tasks: not autostart

[Code]
const
  TaskName = 'ChromExtractOPC';
  FwRule = 'ChromExtractOPC-4840';

var
  DataPage: TInputDirWizardPage;
  FwPage: TInputQueryWizardPage;
  SelftestFailed: Boolean;

function Q(const S: String): String;
begin
  Result := '"' + S + '"';
end;

function RunHidden(const Exe, Params: String): Integer;
var
  Code: Integer;
begin
  Log('运行：' + Exe + ' ' + Params);
  if not Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  Log('退出代码：' + IntToStr(Code));
  Result := Code;
end;

procedure StopService();
var
  Exe: String;
begin
  RunHidden(ExpandConstant('{sys}\schtasks.exe'), '/End /TN ' + TaskName);
  { Windows 服务方式：先通过服务管理器停止（等它真正停下），否则 EXE 被占用无法替换 }
  RunHidden(ExpandConstant('{sys}\sc.exe'), 'stop ' + TaskName);
  Sleep(2000);
  { 先请求正常停止（保存状态、断开客户端），20 秒没停才强制结束 }
  Exe := ExpandConstant('{app}\ChromExtractOPC.exe');
  if FileExists(Exe) then
    RunHidden(Exe, '--no-pause --stop');
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/IM ChromExtractOPC.exe /F');
end;

{ 防火墙地址只允许 IP / 网段 / 逗号 / 连字符，防止拼进命令行的内容被当成别的参数 }
function FwAddrValid(const S: String): Boolean;
var
  i: Integer;
  c: Char;
begin
  Result := Length(S) > 0;
  for i := 1 to Length(S) do
  begin
    c := S[i];
    if not (((c >= '0') and (c <= '9')) or ((c >= 'a') and (c <= 'f')) or
            ((c >= 'A') and (c <= 'F')) or (c = '.') or (c = '/') or (c = ',') or
            (c = '-') or (c = ':')) then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

procedure InitializeWizard();
begin
  DataPage := CreateInputDirPage(wpSelectTasks, '色谱数据所在文件夹',
    '存放 *_V3.db 或 .mbd 文件的文件夹',
    '服务会监控这个文件夹里的谱图数据并通过 OPC UA 发布。' + #13#10 +
    '留空 = 稍后在设置页面里再选。升级安装时留空会保留原来的设置。', False, '');
  DataPage.Add('数据文件夹：');
  DataPage.Values[0] := '';
  { DIAG #10: silent install only - give the data page an existing default path to test whether the empty value is what the wizard validation rejects; interactive installs still start empty }
  if WizardSilent then
    DataPage.Values[0] := 'C:\Windows\Temp';

  FwPage := CreateInputQueryPage(DataPage.ID, '防火墙',
    '只允许指定的客户端访问 OPC UA 端口 4840（强烈建议）',
    '填 DCS / SCADA 所在的地址或网段，多个用英文逗号隔开，例如：' + #13#10 +
    '  192.168.1.0/24' + #13#10 + '  192.168.1.20,192.168.1.21' + #13#10 +
    '留空 = 不修改防火墙（之后可运行 设置防火墙.bat）。');
  FwPage.Add('允许访问的地址：', False);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  v: String;
begin
  Result := True;
  if CurPageID = DataPage.ID then
  begin
    v := Trim(DataPage.Values[0]);
    if (v <> '') and not DirExists(v) then
    begin
      MsgBox('找不到这个文件夹：' + #13#10 + v + #13#10#13#10 + '请检查路径，或留空稍后再设。', mbError, MB_OK);
      Result := False;
    end;
  end
  else if CurPageID = FwPage.ID then
  begin
    v := Trim(FwPage.Values[0]);
    if (v <> '') and not FwAddrValid(v) then
    begin
      MsgBox('地址格式不对：只能包含数字、点、/、逗号和连字符，例如 192.168.1.0/24', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

{ 覆盖安装/升级前先停掉正在运行的服务，否则 EXE 被占用无法替换 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopService();
  Sleep(1000);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Exe, Dir, Fw, Ps: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  Exe := ExpandConstant('{app}\ChromExtractOPC.exe');

  WizardForm.StatusLabel.Caption := '自检：确认程序在这台电脑上能正常运行...';
  SelftestFailed := RunHidden(Exe, '--no-pause --selftest') <> 0;
  if SelftestFailed then
    MsgBox('程序自检没有通过，服务可能无法正常运行。' + #13#10 +
           '常见原因：杀毒软件拦截或删除了文件，请把 ' + ExpandConstant('{app}') + ' 加入白名单。' + #13#10 +
           '安装会继续完成，但不会设置开机自启。可在命令行运行 ChromExtractOPC.exe --selftest 查看原因。',
           mbError, MB_OK);

  Dir := Trim(DataPage.Values[0]);
  if Dir <> '' then
  begin
    WizardForm.StatusLabel.Caption := '设置数据文件夹...';
    if RunHidden(Exe, '--no-pause --set-watch-dir ' + Q(Dir)) <> 0 then
      MsgBox('数据文件夹没设置成功，请稍后在设置页面里修改。', mbInformation, MB_OK);
  end;

  Fw := Trim(FwPage.Values[0]);
  if Fw <> '' then
  begin
    WizardForm.StatusLabel.Caption := '设置防火墙...';
    RunHidden(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name=' + Q(FwRule));
    if RunHidden(ExpandConstant('{sys}\netsh.exe'),
        'advfirewall firewall add rule name=' + Q(FwRule) +
        ' dir=in action=allow protocol=TCP localport=4840 remoteip=' + Fw + ' profile=any') <> 0 then
      MsgBox('防火墙规则没设置成功，请稍后以管理员身份运行 设置防火墙.bat。', mbInformation, MB_OK);
  end;

  if WizardIsTaskSelected('autostart\service') and not SelftestFailed then
  begin
    WizardForm.StatusLabel.Caption := '注册 Windows 服务并启动...';
    { --install-service 会顺带删除旧的计划任务自启，并立即启动服务 }
    if RunHidden(Exe, '--no-pause --install-service') = 0 then
      Sleep(3000)
    else
      MsgBox('注册 Windows 服务没有成功，请稍后以管理员身份运行 安装为Windows服务.bat，' + #13#10 +
             '或查看 logs\server.log。', mbInformation, MB_OK);
  end
  else if WizardIsTaskSelected('autostart\task') and not SelftestFailed then
  begin
    WizardForm.StatusLabel.Caption := '设置开机自启并启动服务...';
    RunHidden(Exe, '--no-pause --uninstall-service');      { 从服务方式切回计划任务 }
    Ps := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
    if RunHidden(Ps, '-NoProfile -ExecutionPolicy Bypass -File ' +
        Q(ExpandConstant('{app}\install_autostart.ps1')) + ' -Exe ' + Q(Exe) + ' -System') = 0 then
    begin
      RunHidden(ExpandConstant('{sys}\schtasks.exe'), '/Run /TN ' + TaskName);
      Sleep(3000);   { 给设置页面一点启动时间，完成页上的"打开设置页面"才能打开 }
    end
    else
      MsgBox('开机自启没设置成功，请稍后以管理员身份运行 安装开机自启.bat。', mbInformation, MB_OK);
  end;
end;

{ ---------------- 卸载 ---------------- }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
begin
  App := ExpandConstant('{app}');
  if CurUninstallStep = usUninstall then
  begin
    StopService();
    if FileExists(App + '\ChromExtractOPC.exe') then
      RunHidden(App + '\ChromExtractOPC.exe', '--no-pause --uninstall-service');
    RunHidden(ExpandConstant('{sys}\sc.exe'), 'delete ' + TaskName);
    RunHidden(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -ExecutionPolicy Bypass -File ' + Q(App + '\install_autostart.ps1') + ' -Remove');
    RunHidden(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN ' + TaskName + ' /F');
    RunHidden(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name=' + Q(FwRule));
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    { 静默卸载时一律保留数据；交互卸载时询问，默认"否" }
    if DirExists(App) and not UninstallSilent then
      if MsgBox('是否同时删除配置文件、账号、证书和日志？' + #13#10 + App + #13#10#13#10 +
             '选"否"则保留，下次安装时自动沿用原来的配置。', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(App, True, True, True);
  end;
end;
