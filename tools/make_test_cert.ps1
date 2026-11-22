# make_test_cert.ps1 —— 生成"自签名测试证书"，仅用于内部测试签名流程。
# 注意：自签名证书【不能】消除 SmartScreen 警告和杀毒误报，只有向 CA 购买的 OV/EV 代码签名证书才行。
# 若只在自己公司内部用：可把生成的 .cer 通过域组策略下发到"受信任的发布者"和"受信任的根证书颁发机构"。
param([string]$Out = 'certs\codesign_test', [string]$Password)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
if (-not $Password) {
  $sec = Read-Host '为 .pfx 设置密码' -AsSecureString
} else {
  $sec = ConvertTo-SecureString $Password -AsPlainText -Force
}
New-Item -ItemType Directory -Force -Path (Split-Path $Out -Parent) | Out-Null
$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject 'CN=ChromExtractOPC Test Signing' `
          -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 -NotAfter (Get-Date).AddYears(3) `
          -CertStoreLocation Cert:\CurrentUser\My
Export-PfxCertificate -Cert $cert -FilePath "$Out.pfx" -Password $sec | Out-Null
Export-Certificate -Cert $cert -FilePath "$Out.cer" | Out-Null
Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)"
Write-Host "已生成：$Out.pfx（私钥，妥善保管，不要提交到仓库）和 $Out.cer（公钥，可分发）"
Write-Host "指纹：$($cert.Thumbprint)"
Write-Host "使用：set SIGN_PFX=$Out.pfx  和  set SIGN_PFX_PASSWORD=密码，然后运行 build_exe.bat"
