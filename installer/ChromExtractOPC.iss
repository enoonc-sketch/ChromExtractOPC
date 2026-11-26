; ChromExtractOPC 安装程序（Inno Setup 6.3+）
; 编译：iscc /DMyAppVersion=3.1 installer\ChromExtractOPC.iss
; 需要先运行 build_exe.bat 生成 dist\ChromExtractOPC.exe 和 dist\ChromExtractOPC-cli.exe
; v2.9 起为桌面软件：不再注册 Windows 服务或开机自启计划任务；安装/卸载时会清理旧版本留下的这两项。
; v3.0：可选任务“登录后自动打开窗口”= 在当前用户的启动文件夹 {userstartup} 放一个指向窗口版的快捷方式
;       （登录 Windows 后打开可见的桌面窗口，不是后台服务/计划任务），卸载时删除。程序窗口“常用设置”页也能开关。
; 本文件必须保存为 UTF-8 带 BOM，否则中文会乱码。

#ifndef MyAppVersion
  #define MyAppVersion "3.1"
#endif
#define MyAppName "色谱提取 OPC UA 服务"
#define MyAppExe "ChromExtractOPC.exe"
#define MyCliExe "ChromExtractOPC-cli.exe"
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
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："
Name: "openatlogin"; Description: "登录 Windows 后自动打开程序窗口（可见的桌面窗口，不是后台服务）"; GroupDescription: "启动："; Flags: unchecked

[Dirs]
; 允许本机普通用户修改配置/日志（与原 安装.bat 的 icacls 一致）
Name: "{app}"; Permissions: users-modify
Name: "{app}\data"
Name: "{app}\logs"

[Files]
; ChromExtractOPC.exe = 桌面软件（窗口版）；ChromExtractOPC-cli.exe = 命令行版（自检/导出/安全管理菜单）
Source: "{#Dist}\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\{#MyCliExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#Dist}\ChromExtractOPC.exe.sha256"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#Dist}\ChromExtractOPC-cli.exe.sha256"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
; 升级时保留现场的配置文件；卸载时也不删（除非用户选择删除全部数据）
Source: "..\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "..\启动图形界面.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\打开设置页面.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\安全管理.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\设置防火墙.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\使用说明_OPCUA.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\安全检查报告.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"
Name: "{group}\打开窗口（不自动启动服务）"; Filename: "{app}\{#MyAppExe}"; Parameters: "--settings"; WorkingDir: "{app}"
Name: "{group}\安全管理"; Filename: "{app}\安全管理.bat"; WorkingDir: "{app}"; IconFilename: "{app}\{#MyAppExe}"
Name: "{group}\运行日志"; Filename: "{app}\logs"
Name: "{group}\使用说明"; Filename: "{app}\使用说明_OPCUA.md"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; Tasks: desktopicon
; 与程序窗口“常用设置 → 登录后自动打开窗口”创建的快捷方式同名（色谱提取 OPC UA 服务.lnk），两边开关的是同一个文件
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; Comment: "登录后打开色谱提取 OPC UA 服务窗口"; Tasks: openatlogin

[UninstallDelete]
; 用户在程序窗口里自己勾选创建的“登录后自动打开窗口”快捷方式，卸载时一并删除
Type: files; Name: "{userstartup}\{#MyAppName}.lnk"

[Run]
Filename: "{app}\{#MyAppExe}"; WorkingDir: "{app}"; Description: "立即打开 {#MyAppName}（窗口打开即启动服务）"; Flags: postinstall nowait skipifsilent

[Code]
const
  TaskName = 'ChromExtractOPC';
  FwRule = 'ChromExtractOPC-4840';

var
  { 用 InputQuery 而非 InputDir：InputDir 页内置校验不许留空（静默安装必失败），
    数据文件夹允许留空稍后设置，故用普通文本框，路径存在性由 NextButtonClick 查 }
  DataPage: TInputQueryWizardPage;
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

{ 关闭正在运行的程序窗口（服务随窗口停止），否则 EXE 被占用无法替换 }
procedure CloseRunningApp();
begin
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/IM ChromExtractOPC.exe /F');
  RunHidden(ExpandConstant('{sys}\taskkill.exe'), '/IM ChromExtractOPC-cli.exe /F');
end;

{ 清理 v2.8 及以前版本的后台运行方式：开机自启计划任务、Windows 服务。新版本只以桌面窗口运行，
  残留的后台实例会占用 4840 端口，导致窗口里的服务启动失败。这里只删除，不创建任何后台项。 }
procedure RemoveLegacyBackground();
begin
  RunHidden(ExpandConstant('{sys}\schtasks.exe'), '/End /TN ' + TaskName);
  RunHidden(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN ' + TaskName + ' /F');
  RunHidden(ExpandConstant('{sys}\sc.exe'), 'stop ' + TaskName);
  Sleep(2000);
  RunHidden(ExpandConstant('{sys}\sc.exe'), 'delete ' + TaskName);
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
  DataPage := CreateInputQueryPage(wpSelectTasks, '色谱数据所在文件夹',
    '存放 *_V3.db 或 .mbd 文件的文件夹',
    '服务会监控这个文件夹里的谱图数据并通过 OPC UA 发布。' + #13#10 +
    '留空 = 稍后在桌面设置窗口里再选。升级安装时留空会保留原来的设置。');
  DataPage.Add('数据文件夹：', False);
  DataPage.Values[0] := '';

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

{ 覆盖安装/升级前先关闭正在运行的程序，并清理旧版后台运行残留 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  RemoveLegacyBackground();
  CloseRunningApp();
  Sleep(1000);
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Cli, Dir, Fw: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  { 自检、设置数据目录用命令行版（有返回码、不弹窗口）}
  Cli := ExpandConstant('{app}\{#MyCliExe}');

  WizardForm.StatusLabel.Caption := '自检：确认程序在这台电脑上能正常运行...';
  SelftestFailed := RunHidden(Cli, '--no-pause --selftest') <> 0;
  if SelftestFailed then
    MsgBox('程序自检没有通过，服务可能无法正常运行。' + #13#10 +
           '常见原因：杀毒软件拦截或删除了文件，请把 ' + ExpandConstant('{app}') + ' 加入白名单。' + #13#10 +
           '安装会继续完成。可在程序窗口点“自检”，或在命令行运行 ChromExtractOPC-cli.exe --selftest 查看原因。',
           mbError, MB_OK);

  Dir := Trim(DataPage.Values[0]);
  if Dir <> '' then
  begin
    WizardForm.StatusLabel.Caption := '设置数据文件夹...';
    if RunHidden(Cli, '--no-pause --set-watch-dir ' + Q(Dir)) <> 0 then
      MsgBox('数据文件夹没设置成功，请稍后在程序窗口“数据与导出”页里选择。', mbInformation, MB_OK);
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
end;

{ ---------------- 卸载 ---------------- }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  App: String;
begin
  App := ExpandConstant('{app}');
  if CurUninstallStep = usUninstall then
  begin
    CloseRunningApp();
    RemoveLegacyBackground();
    RunHidden(ExpandConstant('{sys}\netsh.exe'), 'advfirewall firewall delete rule name=' + Q(FwRule));
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    { 静默卸载时一律保留数据；交互卸载时询问，默认"否" }
    if DirExists(App) and not UninstallSilent then
      if MsgBox('是否同时删除配置文件、账号、证书、日志、历史数据、备份和报表？' + #13#10 + App + #13#10#13#10 +
             '选"否"则保留，下次安装时自动沿用原来的配置。', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(App, True, True, True);
  end;
end;
