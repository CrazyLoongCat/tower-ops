"""厂家 SDK 路径挂载（**原包不修改**；DEC-078 起统一收进 `tower-ops/vendor/`）。

真实适配器要用的厂家包**都在本工程内**（迁移/打包只需 tower-ops 目录）::

    tower-ops/vendor/
    ├── robot/Robotic_Arm/        # Realman 机械臂（libs/linux_arm、linux_x86、win_64…）
    ├── camera/scepter_sdk/       # Vzense DS86（linux/x64、linux/aarch64、windows/x64）
    ├── models/edge_sam-r20231213/# EdgeSAM ONNX（"运行一次"智能分割）
    └── circle38/detect_circle/   # Windows cp38 原生找圆件（仅宿主打包用）

按项目约定"无更改地保留原包"，这里**只把包目录加入 `sys.path`**，不拷贝二进制、
不改厂家代码。默认按 `tower-ops/vendor` 自动探测；可用显式参数覆盖。

**打包后**（PyInstaller）没有源码相对位置，默认位置是包内 `vendor/robot` 与
`vendor/camera`（spec 按目录结构收进 `_internal/vendor/...`）。

注意：**只在启动真实设备前调用**，离线路径不需要、也不应调用（避免把旧包带进离线运行）。
"""
from __future__ import annotations

import os
import sys
from typing import Iterable

from application.bootstrap import app_paths

# 本文件在 tower-ops/application/bootstrap/ → 上溯 2 级 = tower-ops/
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))


def default_sdk_dirs() -> tuple[str, str]:
    """返回 (含 `Robotic_Arm` 的目录, 含 `scepter_sdk` 的目录)。

    - **源码运行**：`tower-ops/vendor/robot` 与 `tower-ops/vendor/camera`；
    - **打包运行**：包内的 `vendor/robot` 与 `vendor/camera`。
    """
    if app_paths.is_frozen():
        vendor = os.path.join(app_paths.bundle_root(), "vendor")
        return os.path.join(vendor, "robot"), os.path.join(vendor, "camera")
    vendor = os.path.join(_PROJECT_ROOT, "vendor")
    return os.path.join(vendor, "robot"), os.path.join(vendor, "camera")


# 在**导入时**求值即可：冻结后 bootloader 先于任何用户代码设好 `sys.frozen`/`sys._MEIPASS`，
# 故此处拿到的就是本进程实际使用的布局。
DEFAULT_ROBOT_SDK_DIR, DEFAULT_DS86_SDK_PARENT = default_sdk_dirs()


def add_vendor_paths(*, robot_sdk_dir: str | None = None,
                     ds86_sdk_dir: str | None = None) -> list[str]:
    """把厂家包目录加入 ``sys.path``（幂等）。返回**实际加入**的路径列表。

    - ``robot_sdk_dir``：**包含 `Robotic_Arm` 包**的目录（默认 `tower-ops/vendor/robot`）；
    - ``ds86_sdk_dir``：**包含 `scepter_sdk` 包**的目录（默认 `tower-ops/vendor/camera`）。
    """
    added: list[str] = []
    for raw in (_pick(robot_sdk_dir, DEFAULT_ROBOT_SDK_DIR, "Robotic_Arm"),
                _pick(ds86_sdk_dir, DEFAULT_DS86_SDK_PARENT, "scepter_sdk")):
        if raw is None:
            continue
        path = os.path.abspath(raw)
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
            added.append(path)
    return added


def _pick(explicit: str | None, default: str, required_pkg: str) -> str | None:
    """显式路径优先；否则用默认位置（**必须真的有那个包目录**才算数）。"""
    if explicit:
        return explicit
    candidate = default
    if os.path.isdir(os.path.join(candidate, required_pkg)):
        return candidate
    return None


def describe_missing() -> list[str]:
    """报告哪些厂家包没找到（给启动时提示用）。"""
    missing: list[str] = []
    if not os.path.isdir(os.path.join(DEFAULT_ROBOT_SDK_DIR, "Robotic_Arm")):
        missing.append(f"Robotic_Arm（期望在 {DEFAULT_ROBOT_SDK_DIR}）")
    if not os.path.isdir(os.path.join(DEFAULT_DS86_SDK_PARENT, "scepter_sdk")):
        missing.append(f"scepter_sdk（期望在 {DEFAULT_DS86_SDK_PARENT}）")
    return missing


def importable(name: str) -> bool:
    """检查某个包此刻能否导入（不触发副作用地探测）。"""
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def describe_import_failure(pkg_name: str, exc: BaseException,
                            *, note: str | None = None) -> str:
    """把"厂家包导入失败"写成一份能照着查的**诊断报告**（而非只回显一句异常）。

    真实模式这条信息常常要隔着网线念给现场的人听，所以宁可长一点：路径找没找到、
    包解析到哪、是不是解释器本身缺件（冻结包少模块的典型症状），一次说清。
    """
    import traceback

    lines: list[str] = [f"【厂家包 {pkg_name} 导入失败】{type(exc).__name__}: {exc}"]
    if note:
        lines.append(f"  说明: {note}")

    # 1) 期望位置与实际解析位置
    home = (DEFAULT_ROBOT_SDK_DIR if pkg_name == "Robotic_Arm"
            else DEFAULT_DS86_SDK_PARENT)
    lines.append(f"  期望包目录: {os.path.join(home, pkg_name)} "
                 f"（{'存在' if os.path.isdir(os.path.join(home, pkg_name)) else '**不存在**'}）")
    try:
        import importlib.util
        spec = importlib.util.find_spec(pkg_name)
    except Exception as sub:  # noqa: BLE001
        spec, lines = None, lines + [f"  find_spec 自身失败: {type(sub).__name__}: {sub}"]
    lines.append(f"  实际解析到: {getattr(spec, 'origin', None) or '**未找到**'}")
    on_path = any(isinstance(entry, str) and os.path.isdir(os.path.join(entry, pkg_name))
                  for entry in sys.path)
    lines.append("  该包目录是否已在 sys.path 上: "
                 + ("是" if on_path else
                    "**否**（真实模式须先经 vendor_paths.add_vendor_paths() 挂载）"))

    # 2) 解释器自身缺件自检（冻结包少了模块时，逐个子模块点出来）
    for mod in ("ctypes", "ctypes.util"):
        try:
            __import__(mod)
            lines.append(f"  解释器自检 {mod}: 可导入")
        except Exception as sub:  # noqa: BLE001
            lines.append(f"  解释器自检 {mod}: **导入失败** -> {type(sub).__name__}: {sub}")
    ct = sys.modules.get("ctypes")
    origin = getattr(ct, "__file__", None)
    if origin:  # 打出来一眼能看出是否被同名文件/目录遮蔽
        lines.append(f"  ctypes 来自: {origin}")

    # 3) 冻结包特有：厂家包按 data 进包时不被 PyInstaller 分析
    if app_paths.is_frozen():
        lines.append("  提示: 冻结后厂家包按 data 进包，PyInstaller **不分析**它，"
                     "故其模块级 import 的模块必须写进 packaging/tower-ops.spec 的 "
                     "hiddenimports（ctypes.util 已补）。")

    lines.append("  完整回溯:")
    lines.extend("    " + ln for ln in traceback.format_exception(exc))
    return "\n".join(lines)


__all__ = ["DEFAULT_DS86_SDK_PARENT", "DEFAULT_ROBOT_SDK_DIR", "add_vendor_paths",
           "default_sdk_dirs", "describe_import_failure", "describe_missing",
           "importable"]
