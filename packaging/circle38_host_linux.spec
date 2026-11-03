# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# PyInstaller spec —— 圆检测宿主（Ubuntu 22.04 x86_64，DEC-072）
# =============================================================================
# 构建（tower-ops 目录、Ubuntu 22.04）：
#
#     python3 -m PyInstaller --noconfirm --clean \
#         --distpath dist_linux --workpath build_linux_host \
#         packaging/circle38_host_linux.spec
#
# 或直接用封装脚本： `bash scripts/build_linux.sh`
#
# 产物：`dist_linux/circle38_host/circle38_host`（onedir）。
# 用法：整个目录摊到主程序同目录 `circle38_host/`（build_linux.sh 会自动做），
# 主程序 real 模式启动时自动发现并拉起（DEC-053/072）。
#
# 说明
# -----------------------------------------------------------------------------
# * cp38 原生件是 Windows-only（timesai_tools.cp38-win_amd64.pyd），Linux 无 .so；
#   本包默认带 **OpenCV 自研后端**（host_version=opencv-linux）：Canny 梯度→弧段→
#   圆拟合→弧段合并→半径/完整度/拟合误差筛选，参数语义与旧工程一致，
#   **不保证与原生逐位一致**（界面上/日志里如实标注后端）。
# * 若以后拿到 Linux 版 `timesai_tools*.so`，把它放进 `detect_circle/` 再构建，
#   或运行时用 `--module-dir` 指过去；入口 `--backend auto` 会优先原生。
# * 宿主依赖只有 stdlib + numpy + cv2；显式排除主程序侧重依赖（历史 bug：
#   pydantic 曾污染 3.8 宿主，见 交接说明 §8）。
# =============================================================================

import os
import sys

try:
    _SPEC_DIR = os.path.abspath(SPECPATH)                      # noqa: F821
except NameError:                                              # pragma: no cover
    _SPEC_DIR = os.path.dirname(os.path.abspath(__file__))

PROJECT_ROOT = os.path.dirname(_SPEC_DIR)      # tower-ops/

APP_NAME = "circle38_host"

if not os.path.isfile(os.path.join(PROJECT_ROOT, "application", "hosts", "circle38",
                                   "entry.py")):
    raise SystemExit(f"错误：找不到宿主入口，PROJECT_ROOT 推断有误：{PROJECT_ROOT}")
if sys.version_info[:2] < (3, 8):
    raise SystemExit(f"错误：宿主需 Python >=3.8（Linux 建议 3.11/3.12），"
                     f"当前 {sys.version.split()[0]}")
if sys.maxsize <= 2 ** 32:
    raise SystemExit("错误：必须用 64 位 Python 打包。")

hiddenimports = [
    "numpy",
    "cv2",
    "application.hosts.circle38.entry",
    "application.hosts.circle38.server",
    "application.hosts.circle38.backends",
    "application.hosts.circle38.opencv_backend",
    "application.hosts.circle38.protocol",
    "application.hosts.circle38.framing",
]

excludes = [
    # 宿主绝不能混入主程序侧重依赖（历史 bug：pydantic/py_trees/PySide6）
    "pydantic",
    "py_trees",
    "PySide6",
    "shiboken6",
    "edge_service",
    "application.presentation",
    "application.services",
    "application.adapters",
    "application.infrastructure",
    "tkinter",
    "matplotlib",
    "pytest",
    "_pytest",
    # 原生件只属于 Windows 宿主包；Linux 后端不需要它
    "timesai_tools",
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, "application", "hosts", "circle38", "entry.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

# --- 构建期硬校验：宿主包里不许出现主程序侧重依赖 ---------------------------
_banned = ("pydantic", "PySide6", "shiboken6", "py_trees", "edge_service")
_leaked = sorted({name.split(".")[0] for name, *_ in a.pure
                  if name.split(".")[0] in _banned})
if _leaked:
    raise SystemExit(
        "错误：宿主包里混进了不该有的依赖: " + ", ".join(_leaked)
        + "\n  宿主只应有 stdlib + numpy + cv2 + application.hosts.circle38 的闭包。")
print(f"[spec] 宿主纯模块 {len(a.pure)} 个，未混入 {len(_banned)} 类重依赖（已校验）")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
