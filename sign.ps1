# sign.ps1 —— 给 EXE / 安装程序做 Authenticode 代码签名（SHA-256 + RFC3161 时间戳）
#
# 证书来源（按优先级，用环境变量配置，不要把证书或密码写进仓库）：
#   1) SIGN_THUMBPRINT   证书指纹：证书已导入 Windows 证书存储，或在 USB 硬件令牌/HSM 里（EV/OV 证书常见）
#   2) SIGN_PFX + SIGN_PFX_PASSWORD   .pfx 文件路径和密码
#   3) SIGN_PFX_BASE64 + SIGN_PFX_PASSWORD   .pfx 的 Base64 内容（GitHub Actions 机密用）
# 可选：SIGN_TIMESTAMP_URL（默认 http://timestamp.digicert.com）、SIGN_DESC（签名描述）
#
# 用法：
#   powershell -File sign.ps1 文件1 [文件2 ...]      签名并校验
#   powershell -File sign.ps1 -Check                 只检查是否配置了证书（退出码 0=已配置，2=未配置）
#   powershell -File sign.ps1 -Verify 文件            只校验签名
param(
  [switch]$Check,
  [switch]$Verify,
  [Parameter(ValueFromRemainingArguments = $true)][string[]]$Files
)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

function Find-SignTool {
  $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $roots = @("${env:ProgramFiles(x86)}\Windows Kits\10\bin", "$env:ProgramFiles\Windows Kits\10\bin")
  foreach ($r in $roots) {
    if (Test-Path $r) {
      $hit = Get-ChildItem $r -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending | Select-Object -First 1
      if ($hit) { return $hit.FullName }
    }
  }
  throw '找不到 signtool.exe。请安装 Windows SDK（勾选 "Signing Tools for Desktop Apps"），或 winget install Microsoft.WindowsSDK.10.0.22621'
}

$tmpPfx = $null
function Get-CertArgs {
  if ($env:SIGN_THUMBPRINT) {
    $tp = ($env:SIGN_THUMBPRINT -replace '[^0-9A-Fa-f]', '')
    return @('/sha1', $tp)
  }
  $pfx = $env:SIGN_PFX
  if (-not $pfx -and $env:SIGN_PFX_BASE64) {
    $script:tmpPfx = Join-Path ([IO.Path]::GetTempPath()) ("sign_" + [guid]::NewGuid().ToString('N') + ".pfx")
    [IO.File]::WriteAllBytes($script:tmpPfx, [Convert]::FromBase64String($env:SIGN_PFX_BASE64))
    $pfx = $script:tmpPfx
  }
  if (-not $pfx) { return $null }
  if (-not (Test-Path -LiteralPath $pfx)) { throw "找不到证书文件：$pfx" }
  $a = @('/f', $pfx)
  if ($env:SIGN_PFX_PASSWORD) { $a += @('/p', $env:SIGN_PFX_PASSWORD) }
  return $a
}

$configured = [bool]($env:SIGN_THUMBPRINT -or $env:SIGN_PFX -or $env:SIGN_PFX_BASE64)
if ($Check) {
  if ($configured) { Write-Host '代码签名：已配置证书。'; exit 0 }
  Write-Host '代码签名：未配置证书（SIGN_THUMBPRINT / SIGN_PFX / SIGN_PFX_BASE64），将生成未签名的程序。'
  exit 2
}

try {
  $st = Find-SignTool
  if (-not $Files -or $Files.Count -eq 0) { throw '没有给出要签名的文件。' }
  if (-not $Verify) {
    $cert = Get-CertArgs
    if (-not $cert) { throw '未配置签名证书。' }
    $ts = if ($env:SIGN_TIMESTAMP_URL) { $env:SIGN_TIMESTAMP_URL } else { 'http://timestamp.digicert.com' }
    $desc = if ($env:SIGN_DESC) { $env:SIGN_DESC } else { '色谱提取 OPC UA 服务' }
  }
  foreach ($f in $Files) {
    if (-not (Test-Path -LiteralPath $f)) { throw "找不到文件：$f" }
    if (-not $Verify) {
      # 时间戳服务器偶尔超时，重试 3 次。时间戳让证书过期后签名依然有效。
      $ok = $false
      for ($i = 1; $i -le 3 -and -not $ok; $i++) {
        & $st sign /fd sha256 /td sha256 /tr $ts /d $desc @cert $f
        if ($LASTEXITCODE -eq 0) { $ok = $true } else { Write-Host "签名失败，第 $i 次，5 秒后重试..."; Start-Sleep 5 }
      }
      if (-not $ok) { throw "签名失败：$f" }
    }
    & $st verify /pa /q $f
    if ($LASTEXITCODE -ne 0) {
      $sig = Get-AuthenticodeSignature -LiteralPath $f
      # 自签名测试证书没装到受信任根时 verify 会失败，但签名本身是完整的
      if ($sig.Status -eq 'UnknownError' -and $sig.SignerCertificate) {
        Write-Host "已签名（证书不受本机信任，测试证书属正常）：$f"
        continue
      }
      throw "签名校验失败：$f（$($sig.Status)）"
    }
    Write-Host "已签名并校验通过：$f"
  }
  exit 0
} catch {
  Write-Host "代码签名出错：$($_.Exception.Message)"
  exit 1
} finally {
  if ($tmpPfx -and (Test-Path $tmpPfx)) { Remove-Item $tmpPfx -Force }
}
