"""离线校验 `packaging/*.spec` 与仓库实情是否一致 —— **不构建 exe**。

用途：改过 spec 或移动过目录之后，先跑这个再打包。它把 spec 用桩类（Analysis/PYZ/EXE/
COLLECT）"干跑"一遍，检查：

- 关键不变式（主程序包**不含** cp38 原生件、不含 runtime/、不含 edge_service）；
- 每个 datas 的源路径真实存在（漏收/写错路径会被抓到）；
- 每个 hiddenimport 在源码树里找得到（拼错模块名会被抓到）；
- 厂家包（以 data 进包、PyInstaller 不分析）import 的每个模块都已登记（漏 `ctypes.util` 这类会被抓到）；
- 两个 spec 的**前置自检会真的拦住**（版本不对 / 位数不对）；
- 没有使用 PyInstaller 6 已移除的写法（`cipher=` 等 —— 照抄旧工程 5.x spec 会踩）。

用法：
    python scripts/check_packaging.py          # Windows + Linux 四个 spec 全查（Windows 机器）
    python scripts/check_packaging.py --linux  # 只查 Linux 两个 spec（Linux 构建机用；
                                               #   不要求仓库兄弟目录里的 Windows 厂家 SDK）

**它证明的是"spec 与仓库对得上"，不是"exe 能跑起来"** —— 真机构建与运行仍须在
Windows + ENV-MAIN / ENV-CIRCLE 上做（见 doc/实施记录/_evidence/打包_离线证据.txt）。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_ROOT)
_PACKAGING = os.path.join(_ROOT, "packaging")

MAIN_SPEC = os.path.join(_PACKAGING, "tower-ops.spec")
HOST_SPEC = os.path.join(_PACKAGING, "circle38_host.spec")
MAIN_LINUX_SPEC = os.path.join(_PACKAGING, "tower-ops-linux.spec")
HOST_LINUX_SPEC = os.path.join(_PACKAGING, "circle38_host_linux.spec")

# PyInstaller 6 已移除的 spec 参数（传了会抛错或无声失效）
_REMOVED_KWARGS = ("cipher", "win_no_prefer_redirects", "win_private_assemblies")

_failures: list[str] = []
_notes: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _failures.append(msg)


# ------------------------------------------------------------------ 桩

class _StubAnalysis:
    """记录 spec 传给 Analysis 的东西；`pure` 用 hiddenimports 冒充（不真做分析）。"""

    def __init__(self, scripts=None, pathex=None, binaries=None, datas=None,
                 hiddenimports=None, hookspath=None, hooksconfig=None,
                 runtime_hooks=None, excludes=None, noarchive=None, **extra):
        self.scripts = list(scripts or [])
        self.pathex = list(pathex or [])
        self.binaries = [tuple(b) for b in (binaries or [])]
        self.datas = [tuple(d) for d in (datas or [])]
        self.hiddenimports = list(hiddenimports or [])
        self.excludes = list(excludes or [])
        self.runtime_hooks = list(runtime_hooks or [])
        self.call_kwargs = dict(scripts=scripts, pathex=pathex)
        self.extra_kwargs = extra                     # 未预期的参数（cipher= 之类）会落到这里
        self.pure = [(m.split(".")[0], m, "PYMODULE") for m in self.hiddenimports]
        self.zipped_data = []


class _StubTarget:
    def __init__(self, *tocs, **kwargs):
        self.tocs = tocs
        self.kwargs = kwargs


def _run_spec(path: str, fake_version: tuple[int, int, int]) -> _StubAnalysis:
    """在"伪装成目标解释器版本"的条件下执行 spec，返回桩 Analysis。"""
    ns = {
        "__name__": "spec",
        "__file__": path,
        "SPECPATH": os.path.dirname(path),
        "Analysis": _StubAnalysis,
        "PYZ": _StubTarget,
        "EXE": _StubTarget,
        "COLLECT": _StubTarget,
        "BUNDLE": _StubTarget,
        "MERGE": _StubTarget,
        "TOC": list,
        "Tree": _StubTarget,
    }
    real = sys.version_info
    sys.version_info = fake_version          # type: ignore[assignment]
    try:
        with open(path, encoding="utf-8") as fh:
            code = compile(fh.read(), path, "exec")
        exec(code, ns)                       # noqa: S102 - 校验脚本，输入是本仓库的 spec
    finally:
        sys.version_info = real              # type: ignore[assignment]
    return ns["a"]


def _expect_exit(path: str, fake_version: tuple[int, int, int], needle: str,
                 label: str) -> None:
    """前置自检必须拦住：期望执行期抛 SystemExit 且消息里有 needle。"""
    try:
        _run_spec(path, fake_version)
    except SystemExit as exc:
        check(needle in str(exc), f"{label}: 自检报了错但消息里没有 {needle!r}: {exc}")
        return
    _failures.append(f"{label}: 前置自检没有拦住（期望 SystemExit，实际跑过去了）")


# ------------------------------------------------------------------ 检查项

def _module_exists(mod: str) -> bool:
    """`application.x.y` 必须在源码树里有对应文件。"""
    return os.path.isfile(os.path.join(_ROOT, *mod.split(".")) + ".py") or os.path.isdir(
        os.path.join(_ROOT, *mod.split(".")))


# 厂家 SDK 以 **data** 进包 → PyInstaller **不分析**它 → 它 import 的模块不会自动进包，
# 必须显式写进 hiddenimports，否则真机上报 ImportError/ModuleNotFoundError。
# 2026-10-15 实机踩过一次：漏了 `ctypes.util`，真机 exe 报 "No module named 'ctypes.util'"
# （`ctypes` 本体只因本工程自己也 import 才侥幸进了包）。
# 下列**顶层名**由标准库/冻结引导自身带进来，无需登记（已在实机 exe 归档里核实存在）。
_VENDOR_ALWAYS_BUNDLED = {"os", "sys", "re", "enum", "typing"}


def _vendor_sdk_imports() -> dict[str, list[str]]:
    """扫描 data 方式进包的厂家源码，收集它**任何位置**（含函数内）import 的模块名。

    DEC-078：厂家包统一在 `tower-ops/vendor/{robot,camera}`。
    """
    import ast
    roots = {
        "robot": os.path.join(_ROOT, "vendor", "robot", "Robotic_Arm"),
        "camera": os.path.join(_ROOT, "vendor", "camera", "scepter_sdk"),
    }
    found: dict[str, list[str]] = {}
    for label, root in roots.items():
        names: set[str] = set()
        for dirpath, _dirs, files in os.walk(root):
            if "libs" in dirpath or "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                full = os.path.join(dirpath, fn)
                with open(full, encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read(), full)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names.update(al.name for al in node.names)
                    elif isinstance(node, ast.ImportFrom) and not node.level:  # 相对=包内自己
                        names.add(node.module or "")
        found[label] = sorted(n for n in names
                              if n.split(".")[0] not in _VENDOR_ALWAYS_BUNDLED)
    return found


def check_vendor_deps(hiddenimports: list[str]) -> None:
    """厂家包（data 进包）用到的每个模块都要在 hiddenimports 里有对应项。

    覆盖判据：存在 hiddenimport ``h`` 使 ``h == m`` 或 ``h.startswith(m + ".")``。
    注意**父模块不覆盖子模块** —— 正是"只登记了 ctypes、没登记 ctypes.util"才踩的坑。
    """
    found = _vendor_sdk_imports()
    for label, mods in found.items():
        need = [m for m in mods
                if not any(h == m or h.startswith(m + ".") for h in hiddenimports)]
        check(not need, f"厂家包({label})用到但 hiddenimports 里没有: {need}"
                        " —— data 进包的 SDK 不被 PyInstaller 分析，漏了会在真机上 ImportError")
    print("  厂家包依赖自检: " + "，".join(f"{k} {v}" for k, v in found.items()))


def check_main_spec() -> None:
    print("=== packaging/tower-ops.spec ===")
    a = _run_spec(MAIN_SPEC, (3, 12, 15))

    # 1) 入口与 pathex
    check(a.scripts == [os.path.join(_ROOT, "main.py")],
          f"入口应为 tower-ops\\main.py，实际 {a.scripts}")
    check(a.pathex == [_ROOT], f"pathex 应为工程根 {_ROOT}，实际 {a.pathex}")

    # 2) 没有用 PyInstaller 6 已移除的参数
    for kw in _REMOVED_KWARGS:
        check(kw not in a.extra_kwargs,
              f"传了 PyInstaller 6 已移除的参数 {kw}=（会报错或无声失效）")

    # 3) datas 源都存在，且结构正确
    missing = [(s, d) for s, d in a.datas if not os.path.exists(s)]
    check(not missing, f"datas 里有不存在的源路径: {missing[:5]}")
    dests = {d for _s, d in a.datas}
    for want in ("configs/devices", "configs/motion", "configs/recipes", "configs/site",
                 "vendor/robot/Robotic_Arm", "vendor/camera/scepter_sdk"):
        check(want in dests, f"datas 少了 {want}（改过目录结构？）")

    # 4) **关键不变式**：主程序包里不许有 cp38 原生件 / runtime / edge_service
    bad = [s for s, _d in a.datas
           if s.endswith(".pyd") or "timesai_tools" in os.path.basename(s)
           or os.path.basename(s).startswith("opencv_")]
    check(not bad, f"主程序包混进了原生件（须由 circle38_host.spec 单独打包）: {bad[:3]}")
    check(not any(d.startswith("runtime") for d in dests),
          "datas 里带了 runtime/（会把过期记录库一起发布）")
    check("timesai_tools" in a.excludes,
          "excludes 必须挡住 timesai_tools（主程序不得导入 cp38 原生件）")
    check("edge_service" in a.excludes, "excludes 应挡住 edge_service（边缘侧另外部署）")

    # 5) hiddenimports 拼写
    for mod in a.hiddenimports:
        if mod.startswith("application."):
            check(_module_exists(mod), f"hiddenimports 里的 {mod} 在源码树里找不到")
    third_party = [m for m in a.hiddenimports if not m.startswith(("application.", "PySide6"))]
    print(f"  第三方 hiddenimports: {', '.join(third_party)}")

    # 5b) 厂家包（data 进包、不被分析）import 的模块必须显式登记
    check_vendor_deps(a.hiddenimports)

    print(f"  datas {len(a.datas)} 条 / hiddenimports {len(a.hiddenimports)} 条 / "
          f"excludes {len(a.excludes)} 条 —— 结构检查通过"
          if not _failures else "  发现问题（见末尾汇总）")

    # 6) 前置自检必须拦住"用 3.8 打主程序"
    _expect_exit(MAIN_SPEC, (3, 8, 10), "ENV-MAIN", "tower-ops.spec 的版本自检")


def check_host_spec() -> None:
    print("=== packaging/circle38_host.spec ===")
    a = _run_spec(HOST_SPEC, (3, 8, 10))

    check(a.scripts == [os.path.join(_ROOT, "application", "hosts", "circle38", "entry.py")],
          f"宿主入口不对: {a.scripts}")
    for kw in _REMOVED_KWARGS:
        check(kw not in a.extra_kwargs, f"传了已移除参数 {kw}=")

    pyds = [os.path.basename(s) for s, _d in a.datas if s.endswith(".pyd")]
    check(pyds == ["timesai_tools.cp38-win_amd64.pyd"],
          f"detect_circle 的 .pyd 没被收进来: {pyds}")
    dests = {d for _s, d in a.datas}
    check("detect_circle" in dests,
          "DLL 与 pyd 必须落在同一目录 detect_circle/（加载靠 add_dll_directory）")
    for name in ("libEGL", ):
        pass
    missing = [(s, d) for s, d in a.datas if not os.path.exists(s)]
    check(not missing, f"datas 里有不存在的源路径: {missing[:3]}")
    print(f"  datas {len(a.datas)} 条（含 {len(pyds)} 个 .pyd），"
          f"最大的是 opencv DLL（约 29MB，来自原包，不改动）")

    for mod in ("pydantic", "PySide6", "py_trees"):
        check(mod in a.excludes, f"excludes 应挡住 {mod}（防 3.8 链路被污染）")
    check(a.hiddenimports[0] == "numpy", f"宿主首个必需依赖应是 numpy: {a.hiddenimports}")

    # 前置自检必须拦住"用主环境打宿主"
    _expect_exit(HOST_SPEC, (3, 12, 15), "ENV-CIRCLE", "circle38_host.spec 的版本自检")


def check_repo_facts(*, require_windows_vendor: bool = True) -> None:
    print("=== 仓库实情 ===")
    robot = os.path.join(_ROOT, "vendor", "robot", "Robotic_Arm")
    camera = os.path.join(_ROOT, "vendor", "camera", "scepter_sdk")
    if require_windows_vendor:
        api = os.path.join(robot, "libs", "win_64", "api_c.dll")
        scepter = os.path.join(camera, "windows", "x64", "Scepter_api.dll")
        for path, label in ((api, "Robotic_Arm/libs/win_64/api_c.dll"),
                            (scepter, "scepter_sdk/windows/x64/Scepter_api.dll")):
            check(os.path.isfile(path), f"厂家包缺文件 {label}（进包后真实模式会加载失败）")
    else:
        print("  （--linux：跳过 Windows 厂家 DLL 检查）")
    # DEC-078：Linux 两套库与 DS86 Linux 库也必须随工程携带
    for path, label in (
            (os.path.join(robot, "libs", "linux_arm", "libapi_c.so"),
             "Robotic_Arm/libs/linux_arm/libapi_c.so"),
            (os.path.join(robot, "libs", "linux_x86", "libapi_c.so"),
             "Robotic_Arm/libs/linux_x86/libapi_c.so"),
            (os.path.join(camera, "linux", "x64", "libScepter_api.so"),
             "scepter_sdk/linux/x64/libScepter_api.so"),
            (os.path.join(camera, "linux", "aarch64", "libScepter_api.so"),
             "scepter_sdk/linux/aarch64/libScepter_api.so")):
        check(os.path.isfile(path), f"厂家包缺 Linux 库 {label}")
    check(os.path.isdir(os.path.join(_ROOT, "configs", "devices")),
          "configs/devices 不在（种子会缺）")
    if require_windows_vendor:
        print(f"  厂家包: robot={'有' if os.path.isdir(robot) else '无'} "
              f"camera={'有' if os.path.isdir(camera) else '无'}")
    edge_sam = os.path.join(_ROOT, "vendor", "models", "edge_sam-r20231213")
    print(f"  EdgeSAM 模型（vendor/models，可选，进主程序包）: "
          f"{'有' if os.path.isdir(edge_sam) else '无（智能分割不可用，会给明确报错）'}")


def check_linux_specs() -> None:
    """DEC-072：Ubuntu（onedir）两包的 spec 与仓库实情一致。"""
    print("=== packaging/*-linux.spec（Ubuntu）===")
    a = _run_spec(MAIN_LINUX_SPEC, (3, 12, 15))
    check(a.scripts == [os.path.join(_ROOT, "main.py")],
          f"Linux 主程序入口应为 tower-ops/main.py，实际 {a.scripts}")
    check(a.pathex == [_ROOT], f"Linux 主程序 pathex 应为工程根 {_ROOT}，实际 {a.pathex}")
    for kw in _REMOVED_KWARGS:
        check(kw not in a.extra_kwargs,
              f"Linux 主程序传了 PyInstaller 6 已移除的参数 {kw}=")
    missing = [(s, d) for s, d in a.datas if not os.path.exists(s)]
    check(not missing, f"Linux 主程序 datas 里有不存在的源路径: {missing[:5]}")
    dests = {d for _s, d in a.datas}
    for want in ("configs/devices", "configs/motion", "configs/recipes", "configs/site",
                 "vendor/robot/Robotic_Arm", "vendor/camera/scepter_sdk"):
        check(want in dests, f"Linux 主程序 datas 少了 {want}（DEC-078）")
    check("models/edge_sam-r20231213" in dests,
          "Linux 主程序 datas 缺 models/edge_sam-r20231213")
    bad = [s for s, _d in a.datas
           if (s.endswith((".pyd", ".so")) or "timesai_tools" in os.path.basename(s)
               or os.path.basename(s).startswith("opencv_"))
           and not _d.replace("\\", "/").startswith("vendor/")]
    check(not bad, f"Linux 主程序包不许带原生找圆件: {bad[:3]}")
    # DEC-077/078：厂家 Linux 库必须进包
    for rel in ("vendor/robot/Robotic_Arm/libs/linux_arm/libapi_c.so",
                "vendor/robot/Robotic_Arm/libs/linux_x86/libapi_c.so",
                "vendor/camera/scepter_sdk/linux/x64/libScepter_api.so",
                "vendor/camera/scepter_sdk/linux/aarch64/libScepter_api.so"):
        found = [s for s, _d in a.datas
                 if s.replace("\\", "/").endswith(rel)]
        check(bool(found), f"Linux 包未收入 {rel}（DEC-078）")
    check("timesai_tools" in a.excludes,
          "Linux 主程序 excludes 必须挡住 timesai_tools")
    check("edge_service" in a.excludes,
          "Linux 主程序 excludes 应挡住 edge_service")
    for mod in ("cv2", "numpy", "PySide6.QtWidgets", "onnxruntime",
                "application.hosts.circle38.opencv_backend",
                # DEC-076：厂家 SDK 以 data/外部目录进包、不被分析，其模块级 import 的
                # 模块必须登记（Linux 冻结包曾报 No module named 'ctypes.util'）
                "ctypes", "ctypes.util", "atexit", "platform", "glob"):
        check(mod in a.hiddenimports, f"Linux 主程序 hiddenimports 缺 {mod}")
    # DEC-078：厂家包依赖同样必须登记（扫描 tower-ops/vendor 下源码的 import）
    check_vendor_deps(a.hiddenimports)
    # 版本自检必须拦住低于 3.10 的解释器
    _expect_exit(MAIN_LINUX_SPEC, (3, 9, 18), "Python 3.10",
                 "Linux 主程序版本自检")

    h = _run_spec(HOST_LINUX_SPEC, (3, 12, 15))
    check(h.scripts == [os.path.join(_ROOT, "application", "hosts", "circle38",
                                     "entry.py")],
          f"Linux 宿主入口错误: {h.scripts}")
    check(h.pathex == [_ROOT], f"Linux 宿主 pathex 应为工程根 {_ROOT}，实际 {h.pathex}")
    for kw in _REMOVED_KWARGS:
        check(kw not in h.extra_kwargs,
              f"Linux 宿主传了 PyInstaller 6 已移除的参数 {kw}=")
    check("cv2" in h.hiddenimports and "numpy" in h.hiddenimports,
          "Linux 宿主 hiddenimports 必须含 cv2/numpy（OpenCV 后端）")
    check("application.hosts.circle38.opencv_backend" in h.hiddenimports,
          "Linux 宿主 hiddenimports 缺 opencv_backend")
    for banned in ("pydantic", "PySide6", "py_trees", "edge_service"):
        check(banned in h.excludes, f"Linux 宿主 excludes 必须挡住 {banned}")
    check(not h.datas, "Linux 宿主不应带原生 datas（OpenCV 后端无原生件）")
    print("  Linux 主程序/宿主 spec：入口、datas、hiddenimports、excludes 一致")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    linux_only = "--linux" in args
    print(f"工程根: {_ROOT}")
    print(f"仓库根: {_REPO}")
    print(f"模式: {'Linux 构建机（--linux）' if linux_only else '全量（Windows + Linux）'}\n")
    check_repo_facts(require_windows_vendor=not linux_only)
    print()
    if not linux_only:
        check_main_spec()
        print()
        check_host_spec()
        print()
    check_linux_specs()
    print()

    if _failures:
        print(f"===== 不一致 {len(_failures)} 处 =====")
        for f in _failures:
            print(f"  ✗ {f}")
        return 1
    print("===== 全部一致 =====")
    print("（提醒：这**不**等于包能跑；Windows 包须在 Windows、"
          "Ubuntu 包须在 Ubuntu 上构建并冒烟）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
