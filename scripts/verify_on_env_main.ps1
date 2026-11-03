<#
.SYNOPSIS
  在 ENV-MAIN (Python 3.12) 上执行 环境自检 / 离线测试 / 离线闭环 / 界面截图,
  并把全部输出写入仓库, 便于直接读取分析 (不需要人工复制粘贴)。

.DESCRIPTION
  在 F:\jt-poletower\tower-ops 目录下运行:

    # 基本: 环境自检 + 离线测试 + 离线闭环
    pwsh -File scripts\verify_on_env_main.ps1

    # 首次/依赖变更后: 先装依赖
    pwsh -File scripts\verify_on_env_main.ps1 -Install

    # 额外: 离屏渲染六页界面截图
    pwsh -File scripts\verify_on_env_main.ps1 -WithGui

  输出写入 doc\实施记录\_evidence\env_main\<时间戳>\ ，并更新 LATEST.txt 指向最新一次。

  安全边界: 只跑离线套件与 Fake 硬件 (main.py offline / render_ui.py)，
  不打开真实串口/SDK/相机，不下发任何真实动作。
#>
param(
  [string]$Python = 'E:\AnacondaEnvs\towerops\python.exe',
  [switch]$Install,
  [switch]$WithGui
)

$ErrorActionPreference = 'Continue'

# tower-ops 与 仓库根
$root = Split-Path -Parent $PSScriptRoot          # ...\tower-ops
$repo = Split-Path -Parent $root                  # ...\jt-poletower
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$outDir = Join-Path $repo "doc\实施记录\_evidence\env_main\$stamp"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

function Write-StepHeader([string]$file, [string]$title) {
  Add-Content -Path $file -Value "`n===== $title =====" -Encoding utf8
}
function Record([string]$file, [string]$cmd, [int]$code) {
  Add-Content -Path $file -Value "[exit=$code] $cmd" -Encoding utf8
}

Write-Host "== ENV-MAIN 验证 ==" -ForegroundColor Cyan
Write-Host "解释器: $Python"
Write-Host "输出目录: $outDir`n"

if (-not (Test-Path -LiteralPath $Python)) {
  Write-Host "找不到解释器: $Python" -ForegroundColor Red
  Write-Host "请用 -Python 指定实际路径 (例如 E:\AnacondaEnvs\towerops\python.exe)"
  exit 3
}

$report = Join-Path $outDir 'env_main_report.txt'
"# ENV-MAIN 验证记录`n日期: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`n解释器: $Python`n主机: $env:COMPUTERNAME`n" |
  Set-Content -Path $report -Encoding utf8

# ---- 1) 版本 + 环境自检 ----
Write-Host "[1/5] 版本与环境自检 ..."
Write-StepHeader $report 'python --version'
& $Python --version 2>&1 | Tee-Object -FilePath $report -Append
Record $report "$Python --version" $LASTEXITCODE

Write-StepHeader $report 'check_env.py'
& $Python (Join-Path $root 'scripts\check_env.py') 2>&1 |
  Tee-Object -FilePath $report -Append
Record $report "check_env.py" $LASTEXITCODE

# ---- 2) 可选装依赖 ----
if ($Install) {
  Write-Host "[2/5] pip install -e . (装主程序依赖) ..."
  Write-StepHeader $report 'pip install -e .'
  Push-Location $root
  & $Python -m pip install -e . 2>&1 | Tee-Object -FilePath $report -Append
  Record $report "pip install -e ." $LASTEXITCODE
  Pop-Location
} else {
  Write-Host "[2/5] 跳过安装 (加 -Install 可先装依赖)"
}

# ---- 3) 离线测试套件 ----
Write-Host "[3/5] 离线测试套件 tests\offline ..."
$pytestLog = Join-Path $outDir 'pytest_offline.txt'
Push-Location $root
& $Python -m pytest tests\offline -q 2>&1 | Tee-Object -FilePath $pytestLog
$pytestCode = $LASTEXITCODE
Pop-Location
Add-Content -Path $pytestLog -Value "[exit=$pytestCode] pytest tests\offline" -Encoding utf8

# ---- 4) 离线闭环 (main.py offline) ----
Write-Host "[4/5] 离线两工位闭环 main.py offline ..."
$mainLog = Join-Path $outDir 'main_offline.txt'
$runDir = Join-Path $outDir 'runtime'
Push-Location $root
& $Python 'main.py' 'offline' --db (Join-Path $runDir 'records.db') --runtime $runDir 2>&1 |
  Tee-Object -FilePath $mainLog
$mainCode = $LASTEXITCODE
Pop-Location
Add-Content -Path $mainLog -Value "[exit=$mainCode] main.py offline" -Encoding utf8

# ---- 5) 可选: 六页界面截图 ----
$guiNote = 'skipped'
if ($WithGui) {
  Write-Host "[5/5] 离屏渲染六页界面 ..."
  $shotDir = Join-Path $repo 'doc\界面验证\E阶段六页'
  New-Item -ItemType Directory -Force -Path $shotDir | Out-Null
  $env:QT_QPA_PLATFORM = 'offscreen'
  Push-Location $root
  & $Python (Join-Path $root 'scripts\render_ui.py') $shotDir 2>&1 |
    Tee-Object -FilePath (Join-Path $outDir 'render_ui.txt') -Append
  $guiCode = $LASTEXITCODE
  Pop-Location
  $guiNote = "exit=$guiCode -> $shotDir"
} else {
  Write-Host "[5/5] 跳过界面截图 (加 -WithGui 可渲染)"
}

# ---- 汇总 ----
$summary = @"
# ENV-MAIN 验证汇总 ($stamp)

- 解释器: $Python
- 环境自检: $report
- 离线测试: $pytestLog  -> exit=$pytestCode
- 离线闭环: $mainLog    -> exit=$mainCode
- 界面截图: $guiNote

结论(自动判定): $(if ($pytestCode -eq 0 -and $mainCode -eq 0) { '两项均正常退出' } else { '存在非零退出码, 需查看日志' })
"@
$summaryPath = Join-Path $outDir 'SUMMARY.md'
$summary | Set-Content -Path $summaryPath -Encoding utf8
$latest = Join-Path (Split-Path -Parent $outDir) 'LATEST.txt'
$outDir | Set-Content -Path $latest -Encoding utf8

Write-Host "`n== 完成 ==" -ForegroundColor Green
Write-Host "汇总: $summaryPath"
Write-Host "离线测试 exit=$pytestCode, 离线闭环 exit=$mainCode"
Write-Host "把这段贴给 AI 即可 (或直接说“跑完了”), 结果已落在仓库里。"
exit $(if ($pytestCode -eq 0 -and $mainCode -eq 0) { 0 } else { 1 })
