# -*- mode: python ; coding: utf-8 -*-
# =============================================================================
# PyInstaller spec —— cp38 圆检测宿主（3.8 独立进程，tower-ops 的"第二块拼图"）
# =============================================================================
# 构建命令（在 tower-ops 目录下执行，**必须用 ENV-CIRCLE（3.8）**）：
#
#     E:\AnacondaEnvs\towerops-circle\python.exe -m PyInstaller --noconfirm --clean ^
#         --distpath dist_host --workpath build_host packaging\circle38_host.spec
#
# 产物： `dist_host\circle38_host\circle38_host.exe`
#
# 为什么不能并进主程序包
# -----------------------------------------------------------------------------
# `timesai_tools.cp38-win_amd64.pyd` 是 **Python 3.8 only** 的原生件（按包内说明为 3.8.10
# 编译，且 3.8.20+ 亦不兼容），而主程序跑在 **3.12**。C8/C18 明确"主程序不得导入 cp38 原生件"，
# 所以本宿主**单独打包、单独进程**，与主程序之间只走长度前缀 JSON 协议（`hosts/circle38`）。
#
# 用法（主程序侧，DEC-053）
# -----------------------------------------------------------------------------
#   **推荐**：把整个 `dist_host\circle38_host\` 目录拷到 `tower-ops.exe` 同目录
#   （即 exe 旁出现 `circle38_host\circle38_host.exe`）。`tower-ops.exe real` 启动时
#   会自动发现并**立刻拉起**它，无需手工双击、也无需 `--circle-host-cmd`。
#   把 exe 与 `_internal` 一起摊在 `tower-ops.exe` 同目录（`circle38_host.exe`）也认。
#
#   显式指定仍最优先：
#     tower-ops.exe real --circle-host-cmd "<...>\circle38_host.exe"
#   只给一个**存在的文件路径**时按整条路径处理（带空格也没问题）。
#
#   * 手工双击 `circle38_host.exe` **不会**被主程序使用：两者之间是子进程
#     stdin/stdout 管道，不是端口服务。请让主程序自己拉起（上面的自动发现）。
#   * 冻结后 `--module-dir` 的默认值自动指向**包内** `detect_circle\`（本 spec 已把原生件与
#     opencv DLL 一起收进去），所以**不用**再手工传 `--module-dir`。
#   * 用 `--circle-host-cmd` 传"解释器 + 参数"这种多段命令时仍按空格切分：路径里
#     不要有空格（推荐直接用自动发现，把它放在无空格目录）。
#
# 与旧工程的关系
# -----------------------------------------------------------------------------
# `detect_circle\` 里的 `timesai_tools.cp38-win_amd64.pyd` + `opencv_*450.dll` +
# `msvcp140/vcruntime140.dll` 是**旧工程原包**，本 spec **原样收进来、不修改、不重命名**；
# 目录名也必须保持 `detect_circle`，因为加载时靠 `os.add_dll_directory(该目录)` 找 DLL。
# =============================================================================

import os
import sys

try:
    _SPEC_DIR = os.path.abspath(SPECPATH)                      # noqa: F821 - PyInstaller 注入
except NameError:                                              # pragma: no cover
    try:
        _SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        _SPEC_DIR = os.getcwd()

PROJECT_ROOT = os.path.dirname(_SPEC_DIR)      # tower-ops\
REPO_ROOT = os.path.dirname(PROJECT_ROOT)      # F:\jt-poletower

APP_NAME = "circle38_host"
# DEC-078：原生找圆件统一收进 tower-ops/vendor/circle38/detect_circle
_DETECT_CIRCLE = os.path.join(PROJECT_ROOT, "vendor", "circle38", "detect_circle")

# --- 打包前自检 --------------------------------------------------------------
if sys.version_info[:2] != (3, 8):
    raise SystemExit(
        f"错误：圆宿主必须用 **ENV-CIRCLE（Python 3.8）** 打包，当前是 "
        f"{sys.version.split()[0]}。\n"
        "  timesai_tools.cp38-win_amd64.pyd 是 3.8 原生件；换解释器打出来的 exe 一定加载失败。\n"
        "  例: E:\\AnacondaEnvs\\towerops-circle\\python.exe -m PyInstaller packaging\\circle38_host.spec"
    )
if sys.maxsize <= 2 ** 32:
    raise SystemExit("错误：必须用 64 位 Python（原生件是 amd64）。")
if not os.path.isfile(os.path.join(PROJECT_ROOT, "application", "hosts", "circle38",
                                  "entry.py")):
    raise SystemExit(f"错误：找不到宿主入口，PROJECT_ROOT 推断有误：{PROJECT_ROOT}")
if not os.path.isdir(_DETECT_CIRCLE):
    raise SystemExit(f"错误：原生件目录不存在：{_DETECT_CIRCLE}")
if not any(f.endswith(".pyd") for f in os.listdir(_DETECT_CIRCLE)):
    raise SystemExit(f"错误：{_DETECT_CIRCLE} 里没有 .pyd（缺 timesai_tools.cp38-win_amd64.pyd）")


def _collect(src_dir, dst_dir):
    """按原目录结构收集（跳缓存），datas 形式 —— 原生件与 DLL 要留在同一目录。"""
    out = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".pytest_cache"}]
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(root, src_dir).replace("\\", "/")
            out.append((src, dst_dir if rel == "." else f"{dst_dir}/{rel}"))
    return out


added_datas = _collect(_DETECT_CIRCLE, "detect_circle")

# 宿主的真实依赖只有 numpy（`backends.detect()` 收发 ndarray）。
# 这里的 `detect_circle.py`（旧工程的算法封装）本身 `import cv2`，但 tower-ops **不走它**
# （宿主直接调 timesai_tools.detect_circle），所以 cv2 不是必需；装了才带上，免得将来
# 有人改回走 detect_circle.py 时缺模块。
hiddenimports = ["numpy"]
try:
    import cv2  # noqa: F401
    hiddenimports.append("cv2")
    print("[spec] 环境里有 cv2，一并收进宿主（非必需，仅为兼容 detect_circle.py）")
except ImportError:
    print("[spec] 环境无 cv2：宿主直接调 timesai_tools，不受影响")

excludes = [
    # 宿主**绝不能**被这些污染：
    #  - pydantic/py_trees/PySide6 是主程序侧依赖，曾把 typing.Annotated(3.9+) 带进 3.8 链路，
    #    导致宿主起不来（历史 bug，见 交接说明 §8 第 4 条）；
    #  - 主程序的界面/服务/适配器与宿主无关。
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
    "pytest",
    "_pytest",
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, "application", "hosts", "circle38", "entry.py")],
    pathex=[PROJECT_ROOT],          # 让 `import application...` 在分析期可解析
    binaries=[],
    datas=added_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # 同主程序 spec：不要传 cipher=（PyInstaller 6 已移除，传了直接报错）
)

# --- 构建期硬校验：宿主包里**不许**出现主程序侧的重依赖 ------------------------
# 这是把"3.8 宿主没被 pydantic 污染"从"希望如此"变成"构建时挡住"。
_banned = ("pydantic", "PySide6", "shiboken6", "py_trees", "edge_service")
_leaked = sorted({name.split(".")[0] for name, *_ in a.pure
                  if name.split(".")[0] in _banned})
if _leaked:
    raise SystemExit(
        "错误：宿主包里混进了不该有的依赖: " + ", ".join(_leaked)
        + "\n  宿主只应有 stdlib + numpy + application.hosts.circle38 的闭包。"
          "请检查是否有新的模块级 import 把主程序侧依赖带了进来。"
    )
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
    # 绝不压：这里都是原生 pyd/dll，UPX 压坏后是"加载失败"或直接闪退
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # 控制台程序：走 stdin/stdout 长度前缀协议，诊断（原生加载失败原因）打到 stderr，
    # 由主程序的 SubprocessTransport 收集进日志。
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
