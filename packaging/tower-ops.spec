# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# PyInstaller spec —— tower-ops 主程序（杆塔智能作业系统，操作台侧）
# =============================================================================
# 构建命令（在 tower-ops 目录下执行，用 **ENV-MAIN**）：
#
#     pyinstaller --noconfirm --clean --distpath dist --workpath build ^
#         packaging\tower-ops.spec
#
# 产物： `dist\tower-ops\tower-ops.exe`（单文件夹，整个 dist\tower-ops 目录一起拷贝即发布）
#
# ----------------------------------------------------------------------------
# 为什么必须是「单文件夹(one-folder)」而不是单文件(one-file)
# ----------------------------------------------------------------------------
# 界面会**写** `configs\devices\devices.yaml`（设备页增删改）、`configs\motion\motion.yaml`
# （运动方案页）与 `runtime\`（SQLite 记录库 + 帧文件）。这些必须在**可写、可见、可备份**的
# 地方。本 spec 配合 `application/bootstrap/app_paths.py`：冻结后应用根 = **exe 所在目录**，
# `configs\`、`runtime\` 就落在 exe 旁边；包内的 `configs\` 只作**首次启动的种子**，
# 且**只补缺失、绝不覆盖**现场改过的配置。详见 技术决定记录 DEC-016。
#
# 单文件模式会把资源解压到临时目录（每次启动新目录、退出即删），配置与记录库都会丢，**不可用**。
#
# ----------------------------------------------------------------------------
# 不打包进来的东西（有意为之，别"顺手补上"）
# ----------------------------------------------------------------------------
# 1. `timesai_tools.cp38-win_amd64.pyd` —— cp38(3.8) 原生件，**主程序不得导入**（C8/C18）。
#    它由**另一个** spec 打成独立宿主：`packaging/circle38_host.spec`（用 ENV-CIRCLE）。
#    主程序只通过 `--circle-host-cmd` 与宿主进程通信。
# 2. `edge_service/`（E08 的 S100 板端服务）—— 跑在边缘侧，不在操作台上；另行部署。
# 3. `runtime/`（记录库/帧）—— 运行期数据，不进包；否则会带着一份过期 records.db 发布。
# 4. `tests/`、`doc/`、四个旧子工程源码 —— 运行不需要。
#
# ----------------------------------------------------------------------------
# 厂家 SDK：**进包**，但按目录结构原样放，不复制成别的形态
# ----------------------------------------------------------------------------
# `Robotic_Arm`（含 `libs/win_64/api_c.dll`）与 `scepter_sdk`（含 `windows/x64/...`）按
# **原目录结构**收进包内 `vendor/robot` 与 `vendor/camera`；`application/bootstrap/vendor_paths.py`
# 冻结后默认就指这两个目录，所以 `main.py real` **不用**再传 `--robot-sdk-dir/--ds86-sdk-dir`。
# 用 datas（而不是 binaries）是刻意的：这些 DLL 由厂家封装用 ctypes 按**自身包目录**定位加载，
# 交给 bin 分析反而会打散目录结构。这条与旧工程 `ECO65_NutTighten.spec` 的做法一致。
# =============================================================================

import os
import sys

# --- 定位工程根 --------------------------------------------------------------
# PyInstaller 6.x 会把 SPECPATH 注入 spec 命名空间（已核实 6.22.3: build_main.py 注入）
# 但**不注入 `__file__`**。逐级兜底，避免"从哪儿跑都行"变成"从哪儿跑都不行"。
try:
    _SPEC_DIR = os.path.abspath(SPECPATH)                      # noqa: F821 - PyInstaller 注入
except NameError:                                              # pragma: no cover
    try:
        _SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        _SPEC_DIR = os.getcwd()

PROJECT_ROOT = os.path.dirname(_SPEC_DIR)      # tower-ops\
REPO_ROOT = os.path.dirname(PROJECT_ROOT)      # F:\jt-poletower（旧工程与厂家包在这里）

APP_NAME = "tower-ops"
DEDUP_SUBDIR_VC_RUNTIME = True                 # 见文末"VC 运行库去重"说明

# --- 打包前自检：宁可在这里报清楚，也不要产出一个跑不起来的包 -----------------
if not os.path.isfile(os.path.join(PROJECT_ROOT, "main.py")):
    raise SystemExit(
        "错误：找不到 tower-ops\\main.py。\n"
        f"  推断的工程根 PROJECT_ROOT = {PROJECT_ROOT}\n"
        f"  spec 目录 = {_SPEC_DIR}\n"
        "请确认 packaging\\tower-ops.spec 与 main.py 的相对位置没变。"
    )
if not ((3, 11) <= sys.version_info[:2] <= (3, 12)):
    raise SystemExit(
        f"错误：主程序须用 ENV-MAIN（Python 3.12 首选 / 3.11 兼容，见 DEC-001）打包，"
        f"当前是 {sys.version.split()[0]}。\n"
        "  3.8 解释器只能用来打包圆宿主：packaging\\circle38_host.spec"
    )
if sys.maxsize <= 2 ** 32:
    raise SystemExit(
        "错误：必须用 **64 位** Python 打包 —— 厂家 DLL 是 win_64、原生件是 amd64。"
    )

# --- 目录收集小工具 ----------------------------------------------------------
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".bak")


def _collect(src_dir, dst_dir):
    """把 src_dir 按**原目录结构**收成 datas；跳过缓存/备份。"""
    out = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(_SKIP_SUFFIXES):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(root, src_dir).replace("\\", "/")
            dst = dst_dir if rel == "." else f"{dst_dir}/{rel}"
            out.append((src, dst))
    return out


# --- 随包携带的数据 ----------------------------------------------------------
configs_datas = _collect(os.path.join(PROJECT_ROOT, "configs"), "configs")

# 厂家包源目录（DEC-078：统一收进 tower-ops/vendor/，迁移/打包只需本工程）
_ROBOT_SDK_PKG = os.path.join(PROJECT_ROOT, "vendor", "robot", "Robotic_Arm")
_CAMERA_SDK_PKG = os.path.join(PROJECT_ROOT, "vendor", "camera", "scepter_sdk")

vendor_datas = []
if os.path.isdir(_ROBOT_SDK_PKG):
    # Windows 包只带 win_*，不把 Linux .so 塞进 exe
    vendor_datas += [d for d in _collect(_ROBOT_SDK_PKG, "vendor/robot/Robotic_Arm")
                     if "/libs/linux_" not in d[0].replace("\\", "/")]
else:
    print(f"[spec] 警告: 未找到 {_ROBOT_SDK_PKG} —— 真实模式的机器人 SDK 不会进包，"
          "届时须在命令行显式 --robot-sdk-dir")

if os.path.isdir(_CAMERA_SDK_PKG):
    vendor_datas += [d for d in _collect(_CAMERA_SDK_PKG, "vendor/camera/scepter_sdk")
                     if "/linux/" not in d[0].replace("\\", "/")]
else:
    print(f"[spec] 警告: 未找到 {_CAMERA_SDK_PKG} —— 真实模式的 DS86 SDK 不会进包，"
          "届时须在命令行显式 --ds86-sdk-dir")

# --- EdgeSAM 智能分割模型（「视觉调试」页 运行一次 / 配方 vision.run 用）--------
# DEC-078：模型统一收进 tower-ops/vendor/models/edge_sam-r20231213；
# 冻结后 resolve_models_dir 会找到 `<包内资源根>/models/edge_sam-r20231213`。
# 两个模型合计约 40 MB，**必须进包**：否则真机 exe 上「运行一次」直接报"未找到 EdgeSAM 模型"。
# 可用环境变量 TOWER_OPS_EDGE_SAM_DIR 覆盖。
_EDGE_SAM_MODELS = os.path.join(PROJECT_ROOT, "vendor", "models",
                                "edge_sam-r20231213")
model_datas = []
if os.path.isdir(_EDGE_SAM_MODELS):
    model_datas += _collect(_EDGE_SAM_MODELS, "models/edge_sam-r20231213")
else:
    print(f"[spec] 警告: 未找到 {_EDGE_SAM_MODELS} —— 「运行一次」的智能分割在真机上"
          "将不可用（会给明确报错，不会用占位冒充）")

print(f"[spec] datas: configs={len(configs_datas)} 文件, vendor={len(vendor_datas)} 文件, "
      f"models={len(model_datas)} 文件")

added_datas = configs_datas + vendor_datas + model_datas

hiddenimports = [
    # --- 主依赖 -------------------------------------------------------------
    "pydantic",
    "ruamel.yaml",
    "py_trees",
    "numpy",
    "cv2",            # opencv-python：引导定位深度算法（DEC-011 升为主依赖）
    # onnxruntime：智能分割（EdgeSAM 两段式 ONNX）的推理后端，模块内**惰性导入**
    # 以便未安装时界面给出明确提示；但打包必须收进来，否则真机上「运行一次」不可用。
    # 原生件与 provider 的收集交给 PyInstaller 自带的 onnxruntime hook。
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "serial",         # pyserial：真实串口链路，函数内惰性导入
    "zmq",            # pyzmq：S100 探测与旧协议(pickle/v1)采集，函数内惰性导入（DEC-005）
    # --- 界面 ---------------------------------------------------------------
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    # 六个页面在 app.py 里是函数内导入，显式列出以防漏收
    "application.presentation.pages.workbench",
    "application.presentation.pages.manual",
    "application.presentation.pages.recipes",
    "application.presentation.pages.motion",
    "application.presentation.pages.devices",
    "application.presentation.pages.gripper",
    "application.presentation.pages.vision",
    "application.presentation.pages.diagnostics",
    "application.presentation.components.image_view",
    "application.presentation.components.annot_image_view",
    # --- 视觉分割 / 运行一次（函数内惰性导入，显式列出防漏收）----------------
    "application.algorithms.segmentation",
    "application.algorithms.segmentation.depth_seg",
    "application.algorithms.segmentation.edge_sam",
    "application.algorithms.segmentation.intersect",
    "application.algorithms.segmentation.scheme",
    "application.algorithms.guidance.depth_guidance",
    # --- S100 采集（离线 Fake 与真实 legacy/V2 两条路）----------------------
    "application.adapters.s100.client",
    "application.adapters.s100.legacy",
    "application.adapters.s100.real",
    "application.adapters.s100.session",
    "application.adapters.s100.framing",
    "application.adapters.s100.multipart_decoder",
    "application.adapters.s100.fake_service",
    "application.adapters.camera_mirror",   # DEC-043 D405-2/4 采集层自动双镜像
    # --- 厂家适配器（真实模式；SDK 本身由 vendor_paths 在运行时挂 sys.path）--
    "application.adapters.ds86.real",
    "application.adapters.ds86.scepter",
    "application.adapters.realman.real",
    "application.adapters.realman.motion",
    # DEC-097：夹爪板 Modbus TCP 调试页（页面/服务/适配层）
    "application.adapters.gripper_board",
    "application.adapters.gripper_board.modbus_tcp",
    "application.adapters.gripper_board.registers",
    "application.services.gripper_debug",
    # --- data 方式进包的厂家 SDK 在**模块级** import 的模块 ------------------
    # 厂家包按 data 收进包（见上面 vendor_datas），PyInstaller **不分析**它，
    # 故它模块级 import 的模块不会自动进包。缺 ctypes.util 的实机症状是：
    #   MotionArmError: 未注入 SDK 且无法导入厂家包 ... No module named 'ctypes.util'
    # 2026-10-15 在实机 exe 上复现并定位（ctypes 本体只因本工程自己也 import 才侥幸进包）。
    "ctypes",
    "ctypes.util",
    "atexit",         # scepter_sdk/ScepterDS_api.py 模块级 import
    "platform",       # 两个厂家包模块级都 import
    "glob",           # Robotic_Arm/rm_ctypes_wrap.py 模块级 import
    # --- 圆宿主客户端（主程序只与宿主进程通信，不导入原生件）---------------
    "application.hosts.circle38.client",
    "application.hosts.circle38.protocol",
    "application.hosts.circle38.framing",
    "application.hosts.circle38.server",
    "application.hosts.circle38.backends",
]

excludes = [
    # cp38 原生件与它的加载器：主程序包**绝不能**含有它（C8/C18）
    "timesai_tools",
    "detect_circle",
    # S100 板端服务：边缘侧另外部署，不在操作台包内
    "edge_service",
    # 明确用不到的重型/无关依赖（避免被间接拖进来）
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
    # 注意：**不要**传 cipher=/win_no_prefer_redirects= 之类。
    # PyInstaller 6 已移除字节码加密，传 cipher 会直接抛 RemovedCipherFeatureError
    # （旧工程 `visual_guidance_project/*.spec` 就是 5.x 写法，照抄会炸）。
)

# --- VC 运行库去重（旧工程踩过的坑，照搬）------------------------------------
# PySide6/shiboken6、cv2、厂家 SDK 各自带了一份 MSVCP140/VCRUNTIME140，版本不一致时
# 会出现"初始化了两个 C++ 运行库" -> 原生模块互相踩，表现为 0xC0000005 闪退。
# 做法：丢掉**子目录里**的重名副本，只留根目录那一份，让所有原生模块共用一个运行库。
# （若现场实测某模块需要自带副本，把 DEDUP_SUBDIR_VC_RUNTIME 置 False 再打包。）
if DEDUP_SUBDIR_VC_RUNTIME:
    _VC_RUNTIME = {"msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
                   "concrt140.dll"}
    _kept, _dropped = [], []
    for _entry in a.binaries:
        _dest = _entry[0]
        if os.path.basename(_dest).lower() in _VC_RUNTIME and os.path.dirname(_dest):
            _dropped.append(_dest)
            continue
        _kept.append(_entry)
    a.binaries = _kept
    if _dropped:
        print(f"[spec] 去重子目录 VC 运行库 {len(_dropped)} 个: "
              + ", ".join(sorted(_dropped)[:6])
              + (" ..." if len(_dropped) > 6 else ""))

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # 二进制交给 COLLECT 放进文件夹（onedir）
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 压坏 Qt / OpenCV / 厂家 DLL 会导致双击闪退且没有输出 —— 关闭（旧工程同结论）
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 保留控制台：real 模式的门闸状态、装配失败原因、会话通道、S100 协议都靠它打印。
    # 想要"纯界面无黑框"就改成 False（代价是现场排错看不到那些诊断）。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # EXE 图标：需要 .ico（png 不行）。项目暂无图标，备好 packaging\tower-ops.ico 后取消注释：
    # icon=os.path.join(_SPEC_DIR, "tower-ops.ico"),
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
