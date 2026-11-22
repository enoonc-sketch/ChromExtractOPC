param(
  [string]$Exe,
  [switch]$Remove,
  [switch]$System
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$name = 'ChromExtractOPC'
try {
  if ($Remove) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
      Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
      Unregister-ScheduledTask -TaskName $name -Confirm:$false
      Write-Host "已删除开机自启任务 ${name}"
    } else {
      Write-Host "没有找到开机自启任务，无需删除。"
    }
    exit 0
  }
  if (-not (Test-Path -LiteralPath $Exe)) { throw "找不到程序：$Exe" }
  $dir = Split-Path -Parent $Exe
  # --background：后台静默运行，不弹窗口，日志写 logs\server.log；单实例，不会重复启动
  $action = New-ScheduledTaskAction -Execute $Exe -Argument '--background' -WorkingDirectory $dir
  $trigger = New-ScheduledTaskTrigger -AtStartup
  # 关键：默认的 schtasks 创建的任务运行满 72 小时会被系统强制结束；这里设为不限时，
  # 并且程序异常退出后每分钟自动重启一次。
  $settings = New-ScheduledTaskSettingsSet `
      -ExecutionTimeLimit ([TimeSpan]::Zero) `
      -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
      -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -MultipleInstances IgnoreNew -Hidden `
      -Priority 6
  if ($System) {
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Limited
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
  } else {
    $me = "$env:USERDOMAIN\$env:USERNAME"
    $cred = Get-Credential -UserName $me -Message '请输入该账号的 Windows 登录密码（用于开机时在后台运行服务）'
    if (-not $cred) { throw '已取消。' }
    $pw = $cred.GetNetworkCredential().Password
    if (-not $pw) { throw '该账号没有密码，Windows 不允许这样的账号运行计划任务。请重新运行并选择 SYSTEM 方式。' }
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -User $cred.UserName -Password $pw -RunLevel Limited -Force | Out-Null
  }
  Write-Host "已创建开机自启任务 ${name}（运行时间不限；异常退出后 1 分钟自动重启）。"
  exit 0
} catch {
  Write-Host "失败：$($_.Exception.Message)"
  exit 1
}
