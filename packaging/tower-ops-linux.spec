# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# PyInstaller spec —— 主程序（Ubuntu 22.04 x86_64 onedir，DEC-072）
# =============================================================================
# 构建（在 tower-ops 目录、**Ubuntu 22.04** 上执行）：
#
#     python3 -m PyInstaller --noconfirm --clean \
#         --distpath dist_linux --workpath build_linux \
#         packaging/tower-ops-linux.spec
#
# 或直接用封装脚本： `bash scripts/build_linux.sh`
#
# 产物：`dist_linux/tower-ops/tower-ops`（onedir，带 `_internal/`）。
# 运行：`./tower-ops gui` / `./tower-ops real ...`（与源码入口一致）。
#
# 说明
# -----------------------------------------------------------------------------
# * Python 版本：正式包建议 **3.11/3.12**（DEC-001）；沙箱/临时验证允许 3.10
#   （打印警告，不挡构建）。低于 3.10 拒绝。
# * 厂家 SDK（Robotic_Arm / scepter_sdk）是 **Windows DLL 包**，Linux 版不含；
#   真实设备模式在 Ubuntu 上若要用，需要厂家提供 Linux 版并通过
#   `--robot-sdk-dir / --ds86-sdk-dir` 指定。configs 与 EdgeSAM ONNX 模型跨平台，照常进包。
# * 圆宿主不是本包：`packaging/circle38_host_linux.spec` 单独构建，
#   由 `scripts/build_linux.sh` 一起摊到 `tower-ops-bundle/circle38_host/`。
# =============================================================================

import os
import sys

try:
    _SPEC_DIR = os.path.abspath(SPECPATH)                      # noqa: F821 - PyInstaller 注入
except NameError:                                              # pragma: no cover
    _SPEC_DIR = os.path.dirname(os.path.abspath(__file__))

PROJECT_ROOT = os.path.dirname(_SPEC_DIR)      # tower-ops/
REPO_ROOT = os.path.dirname(PROJECT_ROOT)      # 仓库根（旧工程/模型在这里）

APP_NAME = "tower-ops"

# --- 打包前自检 -----------------------------------------------------------------
if not os.path.isfile(os.path.join(PROJECT_ROOT, "main.py")):
    raise SystemExit(
        "错误：找不到 tower-ops/main.py。\n"
        f"  推断的工程根 PROJECT_ROOT = {PROJECT_ROOT}")
if sys.version_info[:2] < (3, 10) or sys.version_info[:2] > (3, 12):
    raise SystemExit(
        f"错误：Linux 包需 Python 3.10–3.12（正式建议 3.11/3.12），"
        f"当前 {sys.version.split()[0]}")
if sys.version_info[:2] < (3, 11):
    print(f"[spec] 警告：当前 Python {sys.version.split()[0]} < 3.11 —— "
          "只建议用于沙箱/验证构建；正式包请在 3.11/3.12 环境构建。")
if sys.maxsize <= 2 ** 32:
    raise SystemExit("错误：必须用 64 位 Python 打包。")

# --- 目录收集 -------------------------------------------------------------------
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".bak")


def _collect(src_dir, dst_dir):
    out = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(_SKIP_SUFFIXES):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(root, src_dir).replace("\\", "/")
            out.append((src, dst_dir if rel == "." else f"{dst_dir}/{rel}"))
    return out


configs_datas = _collect(os.path.join(PROJECT_ROOT, "configs"), "configs")

# EdgeSAM 智能分割模型（ONNX，跨平台）。DEC-078：统一在 tower-ops/vendor/models。
model_datas = []
_edge_sam = os.path.join(PROJECT_ROOT, "vendor", "models", "edge_sam-r20231213")
if os.path.isdir(_edge_sam):
    model_datas += _collect(_edge_sam, "models/edge_sam-r20231213")
else:
    print(f"[spec] 警告: 未找到 {_edge_sam} —— 「运行一次」的智能分割将不可用"
          "（会给明确报错，不会用占位冒充）")

print(f"[spec] datas: configs={len(configs_datas)} 文件, models={len(model_datas)} 文件")

# --- 厂家 SDK（Linux）-----------------------------------------------------------
# DEC-078：厂家包统一收进 tower-ops/vendor/（迁移/打包只需本工程）：
#   robot/Robotic_Arm   —— 自带 libs/linux_arm（aarch64）、linux_x86（x86_64）、win_64，
#                          `rm_ctypes_wrap.py` 按 platform.machine() 自动选
#   camera/scepter_sdk  —— 含 linux/x64、linux/aarch64（libScepter_api.so 26.03.1）与
#                          windows/x64；ScepterDS_api.py 按平台加载
vendor_datas = []
for _rel, _dst in (("robot/Robotic_Arm", "vendor/robot/Robotic_Arm"),
                   ("camera/scepter_sdk", "vendor/camera/scepter_sdk")):
    _src = os.path.join(PROJECT_ROOT, "vendor", *_rel.split("/"))
    if os.path.isdir(_src):
        _items = _collect(_src, _dst)
        if _rel.startswith("robot"):
            # Linux 包不塞 Windows DLL（linux_arm/linux_x86 保留）
            _items = [d for d in _items
                      if "/libs/win_" not in d[0].replace("\\", "/")]
        else:
            _items = [d for d in _items
                      if "/windows/" not in d[0].replace("\\", "/")]
        vendor_datas += _items
        print(f"[spec] 厂家 SDK 进包: {_rel}")
    else:
        print(f"[spec] 提示: 未找到 {_src} —— 对应设备真机不可用"
              "（可用 --robot-sdk-dir / --ds86-sdk-dir 运行时指定）")

added_datas = configs_datas + model_datas + vendor_datas

hiddenimports = [
    # --- 主依赖 -------------------------------------------------------------
    "pydantic",
    "ruamel.yaml",
    "py_trees",
    "numpy",
    "cv2",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "serial",
    "zmq",
    "pyrealsense2",
    "pyrealsense2.pyrealsense2",
    # --- 界面（app.py 函数内导入，显式列出防漏收）---------------------------
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "application.presentation.pages.workbench",
    "application.presentation.pages.console",
    "application.presentation.pages.manual",
    "application.presentation.pages.recipes",
    "application.presentation.pages.motion",
    "application.presentation.pages.devices",
    "application.presentation.pages.gripper",
    "application.presentation.pages.settings",
    "application.presentation.pages.vision",
    "application.presentation.pages.diagnostics",
    "application.presentation.components.image_view",
    "application.presentation.components.annot_image_view",
    "application.presentation.components.video_monitor",
    # --- 视觉分割 / 引导算法 ------------------------------------------------
    "application.algorithms.segmentation",
    "application.algorithms.segmentation.depth_seg",
    "application.algorithms.segmentation.edge_sam",
    "application.algorithms.segmentation.intersect",
    "application.algorithms.segmentation.scheme",
    "application.algorithms.guidance.depth_guidance",
    # --- S100 / 相机适配器 ---------------------------------------------------
    "application.adapters.s100.client",
    "application.adapters.s100.legacy",
    "application.adapters.s100.real",
    "application.adapters.s100.session",
    "application.adapters.s100.framing",
    "application.adapters.s100.multipart_decoder",
    "application.adapters.s100.fake_service",
    "application.adapters.camera_mirror",
    "application.adapters.realsense_usb",
    "application.adapters.ds86.real",
    "application.adapters.ds86.scepter",
    "application.adapters.realman.real",
    "application.adapters.realman.motion",
    # DEC-097：夹爪板 Modbus TCP 调试页（页面/服务/适配层）
    "application.adapters.gripper_board",
    "application.adapters.gripper_board.modbus_tcp",
    "application.adapters.gripper_board.registers",
    "application.services.gripper_debug",
    # --- 圆宿主客户端（主程序只与宿主进程通信，不导入原生件）---------------
    "application.hosts.circle38.client",
    "application.hosts.circle38.protocol",
    "application.hosts.circle38.framing",
    "application.hosts.circle38.server",
    "application.hosts.circle38.backends",
    "application.hosts.circle38.entry",
    "application.hosts.circle38.opencv_backend",
    # --- 厂家 SDK（按 --robot-sdk-dir / --ds86-sdk-dir 以 data 或外部目录挂载）---
    # 以 data/外部路径进包的 SDK **不被 PyInstaller 分析**，它模块级 import 的模块必须
    # 显式登记（Windows spec 同样登记过；Linux 冻结包曾因此报
    # `No module named 'ctypes.util'`，见 DEC-076）。
    "ctypes",
    "ctypes.util",
    "atexit",         # scepter_sdk 模块级 import
    "platform",       # 两个厂家包模块级都 import
    "glob",           # Robotic_Arm/rm_ctypes_wrap.py 模块级 import
]

excludes = [
    # 原生找圆件与其加载器只属于宿主包（Windows）——主程序包不得含有
    "timesai_tools",
    "detect_circle",
    "edge_service",
    "tkinter",
    "matplotlib",
    "IPython",
    "pytest",
    "_pytest",
    "py",
    "PIL",
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, "main.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=added_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

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
    # 保留控制台：real 模式门闸/装配/宿主后端信息都从终端看
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
