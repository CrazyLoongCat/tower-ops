<#
.SYNOPSIS
  生成 tower-ops 最小源码包 tower-ops-min-src.tar.gz（DEC-112，Windows/pwsh 版）。

.DESCRIPTION
  在 tower-ops 目录（或任意目录）执行：

    pwsh -File scripts\make_min_src.ps1
    pwsh -File scripts\make_min_src.ps1 -NoVendor
    pwsh -File scripts\make_min_src.ps1 -OutFile D:\tmp\tower-ops-min-src.tar.gz -Verify

  包含：main.py、pyproject.toml、conftest.py、README.md、application/、configs/、
        packaging/、scripts/、edge_service/、（默认）vendor/
  排除：runtime/、tests/、__pycache__、*.pyc、*.bak、build*/dist*、
        .pytest_cache、.git

  产物：默认 <仓库根>\tower-ops-min-src.tar.gz 与同名 .sha256；
        压缩包顶层目录为 tower-ops/，解包即得完整工程。

  注意：Windows 10/11 自带 tar.exe（bsdtar）即可；-Verify 需要 python 才能跑
        check_packaging.py --linux，默认用 E:\AnacondaEnvs\towerops\python.exe。
#>
param(
  [string]$OutFile = '',
  [switch]$NoVendor,
  [switch]$Verify,
  [string]$Python = 'E:\AnacondaEnvs\towerops\python.exe'
)

$ErrorActionPreference = 'Stop'

function Fail([string]$msg) {
  Write-Host "错误：$msg" -ForegroundColor Red
  exit 1
}

$root = Split-Path -Parent $PSScriptRoot          # ...\tower-ops
$parent = Split-Path -Parent $root                # 仓库根
$name = Split-Path -Leaf $root                    # tower-ops（目录改名时随实际名字）
if (-not $OutFile) { $OutFile = Join-Path $parent 'tower-ops-min-src.tar.gz' }

Write-Host "== 源目录: $root =="
foreach ($rel in @('main.py', 'pyproject.toml', 'application', 'configs', 'packaging',
                   'scripts\check_packaging.py', 'scripts\build_linux.sh',
                   'scripts\make_min_src.ps1')) {
  if (-not (Test-Path (Join-Path $root $rel))) { Fail "缺少 $rel（请在完整 tower-ops 目录里运行）" }
}
if ($NoVendor) {
  Write-Host '  模式: 不含 vendor（构建机需另行提供 SDK/模型）'
} else {
  foreach ($d in @('vendor\robot\Robotic_Arm', 'vendor\camera\scepter_sdk',
                   'vendor\circle38\detect_circle')) {
    if (-not (Test-Path (Join-Path $root $d))) { Write-Host "  提示：未找到 $d" -ForegroundColor Yellow }
  }
  Write-Host '  模式: 含 vendor（可直接在构建机 pyinstaller 打包）'
}

if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) {
  Fail '找不到 tar.exe（Windows 10/11 自带；或用 Git Bash 跑 scripts/make_min_src.sh）'
}

Write-Host '== 打包 =='
if (Test-Path $OutFile) { Remove-Item -Force $OutFile }
$shaFile = "$OutFile.sha256"
if (Test-Path $shaFile) { Remove-Item -Force $shaFile }

$tarArgs = @(
  '-czf', $OutFile,
  "--exclude=$name/runtime",
  "--exclude=$name/tests",
  "--exclude=$name/.pytest_cache",
  "--exclude=$name/.git",
  "--exclude=$name/build",
  "--exclude=$name/build_host",
  "--exclude=$name/build_linux",
  "--exclude=$name/build_linux_host",
  "--exclude=$name/dist",
  "--exclude=$name/dist_host",
  "--exclude=$name/dist_linux",
  '--exclude=*/__pycache__',
  '--exclude=*.pyc',
  '--exclude=*.bak'
)
if ($NoVendor) { $tarArgs += "--exclude=$name/vendor" }
$tarArgs += @('-C', $parent, $name)
& tar.exe @tarArgs
if ($LASTEXITCODE -ne 0) { Fail 'tar 打包失败' }

Write-Host '== 校验内容 =='
$list = & tar.exe -tzf $OutFile
if ($LASTEXITCODE -ne 0) { Fail '读取压缩包列表失败' }
foreach ($rel in @('main.py', 'pyproject.toml', 'application/presentation/app.py',
                   'configs/devices/devices.yaml', 'packaging/tower-ops.spec',
                   'packaging/circle38_host.spec', 'packaging/tower-ops-linux.spec',
                   'scripts/check_packaging.py', 'scripts/build_linux.sh',
                   'scripts/build_windows.ps1')) {
  if (-not ($list -contains "$name/$rel")) { Fail "压缩包缺少 $rel" }
}
if ($list | Where-Object { $_ -like "$name/runtime/*" }) { Fail '压缩包混入了 runtime/' }
if ($list | Where-Object { $_ -like "$name/tests/*" }) { Fail '压缩包混入了 tests/' }
if ($list | Where-Object { $_ -like '*.pyc' }) { Fail '压缩包混入了 .pyc' }
if ($NoVendor -and ($list | Where-Object { $_ -like "$name/vendor/*" })) {
  Fail '-NoVendor 但包里仍有 vendor/'
}

$sizeMB = [math]::Round((Get-Item $OutFile).Length / 1MB, 1)
$sha = (Get-FileHash -Algorithm SHA256 -Path $OutFile).Hash.ToLower()
[System.IO.File]::WriteAllText($shaFile, "$sha  $(Split-Path -Leaf $OutFile)`n")
Write-Host "  文件数: $($list.Count)"
Write-Host "  大小:   $sizeMB MB"
Write-Host "  sha256: $sha"
Write-Host "  已写:   $shaFile"

if ($Verify) {
  Write-Host '== 解包验证（临时目录 + check_packaging --linux）=='
  if (-not (Test-Path $Python)) { Fail "找不到 python：$Python（-Verify 需要）" }
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("min-src-" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    & tar.exe -xzf $OutFile -C $tmp
    if ($LASTEXITCODE -ne 0) { Fail '解包失败' }
    $src = Join-Path $tmp $name
    & $Python (Join-Path $src 'scripts\check_packaging.py') --linux
    if ($LASTEXITCODE -ne 0) { Fail 'check_packaging.py --linux 未通过（包内缺件）' }
  } finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  }
}

Write-Host '== 完成 =='
Write-Host "  压缩包: $OutFile"
Write-Host "  使用:   tar -xzf $(Split-Path -Leaf $OutFile); cd $name; bash scripts/build_linux.sh"
exit 0
