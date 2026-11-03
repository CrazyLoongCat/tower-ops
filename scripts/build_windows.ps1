<#
.SYNOPSIS
  Windows 一键打包：主程序（ENV-MAIN, 3.12）+ 3.8 圆宿主（ENV-CIRCLE, 3.8.10）。

.DESCRIPTION
  在 F:\jt-poletower\tower-ops 目录下执行：

    # 一键打包（两包 + 组装发布目录 + 冒烟）
    pwsh -File scripts\build_windows.ps1

    # 只打包主程序（不重建圆宿主）
    pwsh -File scripts\build_windows.ps1 -SkipHost

    # 打完再打一个 zip，便于拷到现场机
    pwsh -File scripts\build_windows.ps1 -Zip

    # 解释器不在默认路径时显式指定
    pwsh -File scripts\build_windows.ps1 -Python D:\Envs\towerops\python.exe `
        -CirclePython D:\Envs\towerops-circle\python.exe

  产物：
    dist\tower-ops\tower-ops.exe                 主程序（onedir，单独存在）
    dist_host\circle38_host\circle38_host.exe    3.8 圆宿主（单独存在）
    dist\tower-ops-bundle\                       发布目录（整目录拷走即部署）
        tower-ops.exe
        _internal\...
        circle38_host\circle38_host.exe          放在主 exe 旁，real 启动自动拉起
        [可选] tower-ops-bundle.zip              加 -Zip 时生成

  安全边界：只在本机构建与冒烟（--help / 离屏 GUI 8 秒），不连接任何真实设备。
  首次运行 exe 时才会把包内 configs 种子释放到 exe 旁（只补缺失、不覆盖现场值）。
#>
param(
  [string]$Python = 'E:\AnacondaEnvs\towerops\python.exe',
  [string]$CirclePython = 'E:\AnacondaEnvs\towerops-circle\python.exe',
  [switch]$SkipHost,
  [switch]$Zip
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot          # ...\tower-ops
Set-Location $root
Write-Host "== 工作目录: $root =="

function Fail([string]$msg) {
  Write-Host "错误：$msg" -ForegroundColor Red
  exit 1
}

function Step([string]$msg) {
  Write-Host "== $msg ==" -ForegroundColor Cyan
}

# ---------------------------------------------------------------- 1. 源码预检
Step '源码预检（必须在完整的 tower-ops 目录里构建）'
$required = @(
  'main.py', 'pyproject.toml', 'application', 'configs',
  'scripts\check_packaging.py', 'scripts\build_windows.ps1',
  'packaging\tower-ops.spec', 'packaging\circle38_host.spec'
)
$missing = @($required | Where-Object { -not (Test-Path $_) })
if ($missing.Count -gt 0) {
  $missing | ForEach-Object { Write-Host "  - 缺少 $_" }
  Fail '缺少构建所需文件/目录，请把整个 tower-ops 目录拷到构建机'
}
if (Test-Path 'vendor\robot\Robotic_Arm') {
  Write-Host '  厂家机械臂 SDK: vendor\robot\Robotic_Arm（win_64 已收）'
} else {
  Write-Host '  提示：未找到 vendor\robot\Robotic_Arm —— 机械臂真机不可用' -ForegroundColor Yellow
}
if (Test-Path 'vendor\camera\scepter_sdk') {
  Write-Host '  厂家 DS86 SDK: vendor\camera\scepter_sdk（windows\x64 已收）'
} else {
  Write-Host '  提示：未找到 vendor\camera\scepter_sdk —— DS86 真机不可用' -ForegroundColor Yellow
}

# ---------------------------------------------------------------- 2. 解释器
Step '解释器与 PyInstaller'
if (-not (Test-Path $Python)) { Fail "找不到主程序解释器：$Python" }
& $Python -c "import sys; print('  python:', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { Fail "主程序解释器不可用：$Python" }
& $Python -c "import PySide6, cv2, numpy, pydantic, PyInstaller; print('  PySide6:', PySide6.__version__, '| cv2:', cv2.__version__, '| PyInstaller:', PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) { Fail "ENV-MAIN 依赖不全（需要 PySide6/cv2/numpy/pydantic/PyInstaller）：$Python" }

if (-not $SkipHost) {
  if (-not (Test-Path $CirclePython)) { Fail "找不到圆宿主解释器：$CirclePython" }
  & $CirclePython -c "import sys; print('  circle python:', sys.version.split()[0]); assert sys.version_info[:2] == (3, 8), '圆宿主必须是 Python 3.8（timesai_tools.cp38 原生件）'"
  if ($LASTEXITCODE -ne 0) { Fail "ENV-CIRCLE 不可用或不是 3.8：$CirclePython" }
  & $CirclePython -c "import PyInstaller; print('  circle PyInstaller:', PyInstaller.__version__)"
  if ($LASTEXITCODE -ne 0) { Fail "ENV-CIRCLE 未安装 PyInstaller： $CirclePython -m pip install pyinstaller" }
}

# ---------------------------------------------------------------- 3. spec 自检
Step 'spec 干跑校验（rc=0 才继续）'
& $Python scripts\check_packaging.py
if ($LASTEXITCODE -ne 0) { Fail 'check_packaging.py 未通过（spec 与仓库不一致）' }

# ---------------------------------------------------------------- 4. 构建主程序
Step '构建主程序（dist\tower-ops）'
if (Test-Path 'dist\tower-ops') { Remove-Item -Recurse -Force 'dist\tower-ops' }
& $Python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\tower-ops.spec
if ($LASTEXITCODE -ne 0) { Fail '主程序构建失败' }
if (-not (Test-Path 'dist\tower-ops\tower-ops.exe')) { Fail '构建结束但未找到 dist\tower-ops\tower-ops.exe' }

# ---------------------------------------------------------------- 5. 构建圆宿主
if (-not $SkipHost) {
  Step '构建 3.8 圆宿主（dist_host\circle38_host）'
  if (Test-Path 'dist_host\circle38_host') { Remove-Item -Recurse -Force 'dist_host\circle38_host' }
  & $CirclePython -m PyInstaller --noconfirm --clean --distpath dist_host --workpath build_host packaging\circle38_host.spec
  if ($LASTEXITCODE -ne 0) { Fail '圆宿主构建失败' }
  if (-not (Test-Path 'dist_host\circle38_host\circle38_host.exe')) { Fail '构建结束但未找到 dist_host\circle38_host\circle38_host.exe' }
}

# ---------------------------------------------------------------- 6. 组装发布目录
Step '组装发布目录（dist\tower-ops-bundle）'
$bundle = Join-Path $root 'dist\tower-ops-bundle'
if (Test-Path $bundle) { Remove-Item -Recurse -Force $bundle }
New-Item -ItemType Directory -Force -Path $bundle | Out-Null
Copy-Item -Path 'dist\tower-ops\*' -Destination $bundle -Recurse -Force
if (-not $SkipHost) {
  # DEC-053：整个 circle38_host 目录放在主 exe 旁，real 启动自动发现并拉起
  New-Item -ItemType Directory -Force -Path (Join-Path $bundle 'circle38_host') | Out-Null
  Copy-Item -Path 'dist_host\circle38_host\*' -Destination (Join-Path $bundle 'circle38_host') -Recurse -Force
  Write-Host '  模式: 主程序 + 独立圆宿主（real 启动自动拉起）'
} else {
  Write-Host '  模式: 仅主程序（未打包圆宿主；需要找圆时请另行放入 circle38_host）'
}

# ---------------------------------------------------------------- 7. 冒烟
Step '冒烟（不连接真实设备）'
$exe = Join-Path $bundle 'tower-ops.exe'
& $exe --help | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 'tower-ops.exe --help 返回非 0' }
Write-Host '  主程序 --help: OK'

$oldQt = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = 'offscreen'
try {
  $proc = Start-Process -FilePath $exe -ArgumentList 'gui' -PassThru -WindowStyle Hidden
  Start-Sleep -Seconds 8
  if ($proc.HasExited) {
    Write-Host "  警告：离屏 GUI 8 秒内退出（ExitCode=$($proc.ExitCode)）" -ForegroundColor Yellow
  } else {
    Stop-Process -Id $proc.Id -Force
    Write-Host '  GUI（offscreen）启动: OK（8 秒内正常进入事件循环）'
  }
} finally {
  if ($null -eq $oldQt) { Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue }
  else { $env:QT_QPA_PLATFORM = $oldQt }
}

# ---------------------------------------------------------------- 8. 可选 zip
if ($Zip) {
  Step '压缩发布目录'
  $zipPath = Join-Path $root 'dist\tower-ops-bundle.zip'
  if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
  Compress-Archive -Path $bundle -DestinationPath $zipPath -CompressionLevel Optimal
  Write-Host "  已生成 $zipPath"
}

Step '完成'
Write-Host "  发布目录: $bundle"
if ($Zip) { Write-Host "  压缩包:   $root\dist\tower-ops-bundle.zip" }
Write-Host '  部署:     把 tower-ops-bundle 整目录拷到现场机（路径不要有空格）'
Write-Host '  运行:     tower-ops.exe real'
Write-Host '  配置:     首次运行自动释放 configs\ 到 exe 旁（只补缺失，不覆盖现场值）'
exit 0
