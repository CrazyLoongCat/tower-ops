<#
.SYNOPSIS
  在 ENV-CIRCLE (Python 3.8) 上验证 cp38 圆检测宿主: 原生加载 / 合法输出 / 正常退出 / IPC 端到端 (A03)。

.DESCRIPTION
  在 F:\jt-poletower\tower-ops 目录下运行:

    # 基本: 只做自检 (需 3.8 环境已装 numpy + opencv-python)
    pwsh -File scripts\verify_circle_host.ps1

    # 首次: 顺带安装依赖
    pwsh -File scripts\verify_circle_host.ps1 -Install

    # 原生件不在默认位置时
    pwsh -File scripts\verify_circle_host.ps1 -ModuleDir "D:\path\to\detect_circle"

  输出写入 doc\实施记录\_evidence\env_circle\<时间戳>\ ，并更新 LATEST.txt。

  安全边界: 只用合成图; 不连接任何设备; 不修改旧环境。
#>
param(
  [string]$Python = 'E:\AnacondaEnvs\towerops-circle\python.exe',
  [string]$ModuleDir = '',
  [string]$Conda = 'E:\software\miniconda3\Scripts\conda.exe',
  [switch]$Install
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot          # ...\tower-ops
$repo = Split-Path -Parent $root                  # ...\jt-poletower
$stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$outDir = Join-Path $repo "doc\实施记录\_evidence\env_circle\$stamp"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$envDir = Split-Path -Parent $Python

# 关键: conda 环境里的 numpy(MKL) / _ssl(OpenSSL) 依赖 <env>\Library\bin 下的 DLL,
# 而该目录只在**激活环境**时才进 PATH。直接用 python.exe 会报
# "DLL load failed ... 找不到指定的模块"。所以优先用 `conda run -p <env>` 调用。
$useCondaRun = Test-Path -LiteralPath $Conda
function Invoke-EnvPython {
  param([string[]]$ArgList)
  if ($useCondaRun) {
    & $Conda run -p $envDir --no-capture-output python @ArgList
  } else {
    & $Python @ArgList
  }
}
$invokeLabel = if ($useCondaRun) { "conda run -p `"$envDir`" python" } else { $Python }

Write-Host "== ENV-CIRCLE (cp38 圆检测宿主) 验证 ==" -ForegroundColor Cyan
Write-Host "解释器: $Python"
Write-Host "调用方式: $invokeLabel"
Write-Host "输出目录: $outDir`n"

if (-not (Test-Path -LiteralPath $Python)) {
  Write-Host "找不到解释器: $Python" -ForegroundColor Red
  Write-Host "请用 -Python 指定 ENV-CIRCLE 的实际 python.exe 路径"
  exit 3
}

$log = Join-Path $outDir 'circle_host_report.txt'
"# ENV-CIRCLE 验证记录`n日期: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`n解释器: $Python`n调用: $invokeLabel`n主机: $env:COMPUTERNAME`n" |
  Set-Content -Path $log -Encoding utf8

# 0) 健康检查: 先确认 conda 环境本身可用 (numpy 需 MKL, ssl 需 OpenSSL, 均在 Library\bin)
Write-Host "[0/2] 环境健康检查 (numpy + ssl) ..."
Invoke-EnvPython @('-c', "import numpy, ssl; print('numpy', numpy.__version__, '| ssl', ssl.OPENSSL_VERSION)") 2>&1 |
  Tee-Object -FilePath $log -Append
$health = $LASTEXITCODE
Add-Content -Path $log -Value "[exit=$health] 健康检查" -Encoding utf8
if ($health -ne 0) {
  Write-Host "环境不健康 (numpy/ssl 无法导入)。请先确认用 conda run 调用, 或重建环境:" -ForegroundColor Yellow
  Write-Host "  conda create -p `"$envDir`" -c conda-forge `"python=3.8.10`" numpy -y" -ForegroundColor Yellow
}

if ($Install) {
  if (Test-Path -LiteralPath $Conda) {
    # 关键: 必须**锁住 python=3.8.10** —— timesai_tools.pyd 编译于 3.8.10,
    # 且其自带 opencv_*450.dll; 只装 numpy, 不装 opencv/cv2 (避免同进程两套 OpenCV 冲突)。
    Write-Host "[1/2] conda install python=3.8.10 + numpy (锁定 ABI 版本) ..."
    & $Conda install -p $envDir "python=3.8.10" numpy -y 2>&1 | Tee-Object -FilePath $log -Append
    Add-Content -Path $log -Value "[exit=$LASTEXITCODE] conda install -p $envDir python=3.8.10 numpy -y" -Encoding utf8
  } else {
    Write-Host "未找到 conda ($Conda); 请手动安装 numpy (并确保 python 为 3.8.10):" -ForegroundColor Yellow
    Write-Host "  conda install -p `"$envDir`" `"python=3.8.10`" numpy -y" -ForegroundColor Yellow
  }
} else {
  Write-Host "[1/2] 跳过安装 (加 -Install 可先装; 会锁定 python=3.8.10)"
}

Write-Host "[2/2] 原生自检 + IPC 端到端 ..."
$json = Join-Path $outDir 'circle_host_report.json'
$argList = @((Join-Path $root 'scripts\check_native_circle.py'), '--out', $json)
if ($ModuleDir -ne '') { $argList += @('--module-dir', $ModuleDir) }
Invoke-EnvPython $argList 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE
Add-Content -Path $log -Value "[exit=$code] check_native_circle.py" -Encoding utf8

$verdict = 'UNKNOWN'
if (Test-Path -LiteralPath $json) {
  $verdict = (Get-Content -LiteralPath $json -Raw | ConvertFrom-Json).verdict
}
$summary = @"
# ENV-CIRCLE 验证汇总 ($stamp)

- 解释器: $Python
- 调用方式: $invokeLabel   (conda run 会把 <env>\Library\bin 加入 DLL 搜索路径)
- 环境健康检查 (numpy/ssl) 退出码: $health
- 原生件目录: $(if ($ModuleDir -ne '') { $ModuleDir } else { '默认 (仓库内 detect_circle)' })
- 自检退出码: $code
- 判定: $verdict
- 明细: $log
- JSON: $json

说明: 合成图只验证 加载/结构/IPC; 真实工艺代表样本与误检漏检归 T38, 本脚本不覆盖。
"@
$summaryPath = Join-Path $outDir 'SUMMARY.md'
$summary | Set-Content -Path $summaryPath -Encoding utf8
(Join-Path 'env_circle' $stamp) |
  Set-Content -Path (Join-Path (Split-Path -Parent $outDir) 'LATEST.txt') -Encoding ascii

Write-Host "`n== 完成 ==" -ForegroundColor Green
Write-Host "判定: $verdict   退出码: $code"
Write-Host "汇总: $summaryPath"
Write-Host "结果已落在仓库里, 回一句“跑完了”即可。"
exit $code
