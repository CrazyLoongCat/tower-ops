<#
.SYNOPSIS
  A03 跨解释器 IPC 验证: 3.12 主进程 spawn 独立 3.8 宿主 (真实原生件)。

.DESCRIPTION
  与 verify_circle_host.ps1 互补:
    - verify_circle_host.ps1  → 在 3.8 进程**内部**验证原生加载/结构/IPC/退出;
    - 本脚本                  → 验证**进程边界** (主 3.12 ↔ 宿主 3.8), 即真实架构。

  在 F:\jt-poletower\tower-ops 目录下运行:
    pwsh -File scripts\verify_circle_ipc.ps1
    pwsh -File scripts\verify_circle_ipc.ps1 -Stub      # 用 stub 联调进程边界 (不加载原生件)

  输出写入 doc\实施记录\_evidence\circle_ipc\<时间戳>\。
  只 spawn 本机子进程, 不接触任何设备。
#>
param(
  [string]$MainPython = 'E:\AnacondaEnvs\towerops\python.exe',
  [string]$HostPython = 'E:\AnacondaEnvs\towerops-circle\python.exe',
  [string]$ModuleDir = '',
  [switch]$Stub
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $root
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$outDir = Join-Path $repo "doc\实施记录\_evidence\circle_ipc\$stamp"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$log = Join-Path $outDir 'cross_ipc.txt'
$json = Join-Path $outDir 'cross_ipc_report.json'

Write-Host "== A03 跨解释器 IPC 验证 ==" -ForegroundColor Cyan
Write-Host "主解释器 (ENV-MAIN): $MainPython"
Write-Host "宿主解释器 (ENV-CIRCLE): $HostPython"
Write-Host "后端: $(if ($Stub) { 'stub (联调, 非真实证据)' } else { '真实原生件' })"
Write-Host "输出: $outDir`n"

foreach ($p in @($MainPython, $HostPython)) {
  if (-not (Test-Path -LiteralPath $p)) {
    Write-Host "找不到解释器: $p" -ForegroundColor Red
    Write-Host "用 -MainPython / -HostPython 指定实际路径"
    exit 3
  }
}

"# A03 跨解释器 IPC 验证`n日期: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`n主: $MainPython`n宿主: $HostPython`n主机: $env:COMPUTERNAME`n" |
  Set-Content -Path $log -Encoding utf8

$argList = @((Join-Path $root 'scripts\verify_circle_ipc_cross.py'),
             '--host-python', $HostPython, '--out', $json)
if ($ModuleDir -ne '') { $argList += @('--module-dir', $ModuleDir) }
if ($Stub) { $argList += '--stub' }

& $MainPython @argList 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE
Add-Content -Path $log -Value "[exit=$code] verify_circle_ipc_cross.py" -Encoding utf8

$verdict = 'UNKNOWN'
if (Test-Path -LiteralPath $json) {
  $verdict = (Get-Content -LiteralPath $json -Raw | ConvertFrom-Json).verdict
}
$summary = @"
# A03 跨解释器 IPC 汇总 ($stamp)

- 主解释器: $MainPython
- 宿主解释器: $HostPython
- 后端: $(if ($Stub) { 'stub (联调)' } else { '真实原生件' })
- 退出码: $code    判定: $verdict
- 明细: $log
- JSON: $json

覆盖: 正常往返 / 连续请求分帧 / 宿主崩溃 / 超时 / 干净退出。
"@
$summary | Set-Content -Path (Join-Path $outDir 'SUMMARY.md') -Encoding utf8
(Join-Path 'circle_ipc' $stamp) |
  Set-Content -Path (Join-Path (Split-Path -Parent $outDir) 'LATEST.txt') -Encoding ascii

Write-Host "`n判定: $verdict   退出码: $code" -ForegroundColor Green
Write-Host "汇总: $(Join-Path $outDir 'SUMMARY.md')"
Write-Host "结果已落在仓库里, 回一句“跑完了”即可。"
exit $code
