#!/usr/bin/env bash
# =============================================================================
# Linux 构建环境一键准备（DEC-113）
# =============================================================================
# 用途：在 Ubuntu 构建机上创建虚拟环境并安装打包所需依赖（含 PyInstaller）。
#       之后用该环境的 Python 跑 build_linux.sh 即可。
#
# 用法（tower-ops 目录；缺 python3-venv 时先 sudo apt install python3-venv）：
#     bash scripts/setup_linux_env.sh
#     source ../.venv/bin/activate && bash scripts/build_linux.sh
#   # 或者不激活：
#     PYTHON=../.venv/bin/python bash scripts/build_linux.sh
#
# 虚拟环境默认建在 **tower-ops 的同级目录**（../.venv），不放进工程目录：
# 这样源码包/同步/复制 tower-ops 时不会把几百 MB 的 .venv 带进去。
#
# 选项（环境变量）：
#     PYTHON=python3.11        用指定解释器建 venv（默认 python3；需 3.10–3.12）
#     VENV=../.venv            虚拟环境目录（默认 ../.venv，相对 tower-ops）
#     SKIP_INSTALL=1           只建 venv，不装依赖（排障/离线机用）
#
# 说明：PyInstaller 不支持交叉编译 —— 在什么架构的机器上建环境，就产出什么架构的包。
#       依赖清单 packaging/requirements-linux.txt（PySide6==6.6.3、numpy<2、pyinstaller）。
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."          # tower-ops 根
ROOT="$(pwd)"

BASE_PY="${PYTHON:-python3}"
# DEC-117：默认放在 tower-ops 同级目录，避免虚拟环境混进工程/源码包
VENV="${VENV:-../.venv}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"

fail() { echo "错误：$*" >&2; exit 1; }

echo "== 检查基础解释器: $BASE_PY =="
command -v "$BASE_PY" >/dev/null 2>&1 || fail "找不到 $BASE_PY（请先安装 Python 3.10–3.12）"
"$BASE_PY" - <<'PY' || exit 1
import sys
v = sys.version_info
print(f"  python: {v.major}.{v.minor}.{v.micro}")
if not ((3, 10) <= (v.major, v.minor) <= (3, 12)):
    sys.exit("错误：需要 Python 3.10–3.12（Ubuntu 22.04 自带 3.10；3.12 需另装）")
PY

VENV_ABS="$(cd "$(dirname "$VENV")" 2>/dev/null && pwd || echo "")/$(basename "$VENV")"
echo "== 创建虚拟环境: $VENV（${VENV_ABS}）=="
if [ "$VENV" == "../.venv" ] && [ -d "$ROOT/.venv" ] && [ ! -x "$ROOT/../.venv/bin/python" ]; then
  echo "  提示：检测到旧位置 $ROOT/.venv（以前建在工程目录里）；"
  echo "        新环境在 $ROOT/../.venv，旧目录确认无用后可删除。"
fi
if [ -x "$VENV/bin/python" ]; then
  echo "  已存在，复用"
else
  if ! "$BASE_PY" -m venv "$VENV"; then
    fail "创建虚拟环境失败：Ubuntu 可能缺 python3-venv，请先 sudo apt install python3-venv"
  fi
  echo "  已创建"
fi
PY="$VENV/bin/python"
"$PY" -c 'import sys; print("  venv python:", sys.version.split()[0])'

if [ "$SKIP_INSTALL" == "1" ]; then
  echo "== 跳过依赖安装（SKIP_INSTALL=1）=="
else
  echo "== 安装依赖（packaging/requirements-linux.txt）=="
  "$PY" -m pip install --upgrade pip
  if ! "$PY" -m pip install -r packaging/requirements-linux.txt; then
    echo "安装失败：请检查网络和 pip 错误。如果提示找不到 pyrealsense2，" >&2
    echo "请检查 CPU 架构与 Python 版本；ARM64 可尝试 Python 3.12 并新建 venv。" >&2
    exit 1
  fi
fi

echo "== 自检 =="
if [ "$SKIP_INSTALL" == "1" ]; then
  echo "  已跳过依赖自检（依赖未安装）"
else
  "$PY" - <<'PY' || exit 1
import sys
from importlib.metadata import version
import numpy, cv2, pydantic, py_trees, ruamel.yaml
import serial, zmq, onnxruntime, PyInstaller, PySide6, pyrealsense2
print(f"  PySide6 {PySide6.__version__} | numpy {numpy.__version__} | "
      f"cv2 {cv2.__version__} | pydantic {pydantic.VERSION} | "
      f"PyInstaller {PyInstaller.__version__} | pyrealsense2 {version('pyrealsense2')}")
assert int(numpy.__version__.split(".")[0]) < 2, \
    "numpy 必须是 1.x（PySide6 6.6.x 的二进制按 NumPy 1.x 编译，DEC-076）"
PY
  echo "  依赖齐全"
fi

echo "== 完成 =="
echo "  下一步（二选一）："
echo "    source $VENV/bin/activate && bash scripts/build_linux.sh"
echo "    PYTHON=$VENV/bin/python bash scripts/build_linux.sh"
echo "  注意：PyInstaller 不能交叉编译，产物架构 = 本机架构（$(uname -m)）"
