#!/usr/bin/env bash
# =============================================================================
# Ubuntu 22.04 x86_64 一键打包（DEC-072/073）
# =============================================================================
# 用法（tower-ops 目录）：
#     bash scripts/build_linux.sh              # 默认：单程序（进程内 OpenCV 找圆）
#     BUILD_HOST=1 bash scripts/build_linux.sh # 另带独立圆宿主（子进程隔离版）
#
# 环境要求：Ubuntu 22.04 x86_64，Python 3.11/3.12（验证可用 3.10），并已安装：
#     pip install -r <本项目依赖> PySide6 opencv-python-headless onnxruntime pyinstaller
#
# 产物：
#   dist_linux/tower-ops/            主程序 onedir（tower-ops 可执行 + _internal/）
#   dist_linux/tower-ops-bundle/     发布目录（单程序：找圆跑在主程序进程内，opencv-linux）
#   [BUILD_HOST=1 时] dist_linux/circle38_host/ + bundle/circle38_host/（子进程隔离）
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
DIST="dist_linux"
BUNDLE="$DIST/tower-ops-bundle"
BUILD_HOST="${BUILD_HOST:-0}"

echo "== 源码预检（构建必须在完整的 tower-ops 目录里执行）=="
_missing=()
for _f in main.py pyproject.toml application configs \
          scripts/check_packaging.py scripts/build_linux.sh \
          packaging/tower-ops-linux.spec packaging/circle38_host_linux.spec \
          packaging/requirements-linux.txt; do
  [[ -e "$_f" ]] || _missing+=("$_f")
done
if (( ${#_missing[@]} > 0 )); then
  echo "错误：缺少构建所需文件/目录：" >&2
  printf '  - %s\n' "${_missing[@]}" >&2
  echo "请把**整个 tower-ops 目录**拷到构建机（见 doc/实施记录/Ubuntu打包说明.md §2.2）。" >&2
  exit 1
fi
if [[ ! -d vendor/robot/Robotic_Arm ]]; then
  echo "提示：未找到 vendor/robot/Robotic_Arm —— 机械臂真机不可用（可运行时 --robot-sdk-dir 指定）。"
else
  echo "  厂家机械臂 SDK: vendor/robot/Robotic_Arm（含 linux_arm/linux_x86 .so）"
fi
if [[ ! -d vendor/camera/scepter_sdk ]]; then
  echo "提示：未找到 vendor/camera/scepter_sdk —— DS86 真机不可用（可运行时 --ds86-sdk-dir 指定）。"
else
  echo "  厂家 DS86 SDK: vendor/camera/scepter_sdk（含 linux/x64、linux/aarch64 库）"
fi
if [[ ! -d vendor/models/edge_sam-r20231213 ]]; then
  echo "提示：未找到 vendor/models/edge_sam-r20231213（可选）——"
  echo "      「运行一次」的智能分割会不可用（运行时会给明确报错，不会用占位冒充）。"
fi
echo "  必需文件齐全"

echo "== 环境 =="
"$PY" -c 'import sys; print("python:", sys.version.split()[0])'
echo "  arch: $(uname -m) | $(ldd --version 2>/dev/null | head -1 || true)"
echo "  注意：PyInstaller 不能交叉编译 —— 本机是什么架构，产物就是什么架构"
"$PY" -c 'import PySide6, cv2, numpy, pydantic; print("PySide6:", PySide6.__version__, "| cv2:", cv2.__version__)'
if ! "$PY" -c 'import pyrealsense2; from importlib.metadata import version; print("pyrealsense2:", version("pyrealsense2"))'; then
  echo "错误：当前构建环境无法导入 pyrealsense2；请先运行 bash scripts/setup_linux_env.sh。" >&2
  exit 1
fi
if ! "$PY" -m PyInstaller --version >/dev/null 2>&1; then
  echo "错误：当前解释器（$PY）未安装 PyInstaller。建议先做构建环境（DEC-113）：" >&2
  echo "  bash scripts/setup_linux_env.sh" >&2
  echo "  source ../.venv/bin/activate && bash scripts/build_linux.sh   # 或 PYTHON=../.venv/bin/python bash scripts/build_linux.sh" >&2
  exit 1
fi
"$PY" -c 'import onnxruntime; print("onnxruntime:", onnxruntime.__version__)' || \
  echo "警告：未安装 onnxruntime —— 智能分割（运行一次）在包里不可用（会明确报错）"

echo "== 干跑校验 spec（Linux 模式：不要求 Windows 厂家 SDK）=="
"$PY" scripts/check_packaging.py --linux

echo "== 构建主程序 =="
rm -rf build_linux
"$PY" -m PyInstaller --noconfirm --clean \
    --distpath "$DIST" --workpath build_linux packaging/tower-ops-linux.spec

if ! find "$DIST/tower-ops/_internal" -type f -name 'pyrealsense2*.so' -print -quit | grep -q .; then
  echo "错误：打包结果未找到 pyrealsense2 原生库，D405 USB 不可用；请检查 PyInstaller 日志。" >&2
  exit 1
fi
echo "  D405 USB 原生库已收集（仍需在目标机验证导入与实机采集）"

if [[ "$BUILD_HOST" == "1" ]]; then
  echo "== 构建圆宿主（子进程隔离版，OpenCV 后端）=="
  rm -rf build_linux_host "$DIST/circle38_host"
  "$PY" -m PyInstaller --noconfirm --clean \
      --distpath "$DIST" --workpath build_linux_host packaging/circle38_host_linux.spec
fi

echo "== 组装发布目录 =="
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE"
cp -a "$DIST/tower-ops/." "$BUNDLE/"
if [[ "$BUILD_HOST" == "1" ]]; then
  mkdir -p "$BUNDLE/circle38_host"
  cp -a "$DIST/circle38_host/." "$BUNDLE/circle38_host/"
  echo "  模式: 主程序 + 独立圆宿主（auto 会优先用宿主）"
else
  echo "  模式: 单程序（进程内 OpenCV 找圆，opencv-linux；不启动宿主）"
fi

echo "== 冒烟 =="
"$BUNDLE/tower-ops" --help >/dev/null 2>&1
echo "  主程序 --help: OK"
if [[ "$BUILD_HOST" == "1" ]]; then
  printf '' | "$BUNDLE/circle38_host/circle38_host" >/dev/null 2>"$DIST/host_stderr.txt" || true
  grep -q "后端: opencv-linux" "$DIST/host_stderr.txt" && \
    echo "  宿主后端: opencv-linux" || { echo "  宿主后端行缺失："; cat "$DIST/host_stderr.txt"; exit 1; }
fi

if command -v timeout >/dev/null 2>&1; then
  QT_QPA_PLATFORM=offscreen timeout 8 "$BUNDLE/tower-ops" gui >/dev/null 2>&1 || rc=$?
  rc="${rc:-0}"
  if [[ "$rc" == "0" || "$rc" == "124" ]]; then
    echo "  GUI（offscreen）启动: OK（8s 内正常进入事件循环）"
  else
    echo "  警告：GUI 离屏启动返回 rc=$rc（可能缺系统 Qt/EGL 库）" >&2
  fi
fi

echo "== 完成 =="
echo "  发布目录: $BUNDLE"
echo "  配置:     首次运行自动释放到 $BUNDLE/configs/（只补缺失，不覆盖现场值）"
