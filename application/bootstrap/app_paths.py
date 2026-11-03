"""应用资源与数据路径：源码运行 / PyInstaller 冻结 两种布局的统一解析。

**为什么需要这个模块**

界面会**写** `configs/devices/devices.yaml` 与 `configs/motion/motion.yaml`，程序还会写
`runtime/`（SQLite 记录库 + 帧文件）。冻结后若仍按 `__file__` 定位，这些会落进包内的
`_internal\\`（PyInstaller 6 的 onedir 布局）—— 升级/重装一覆盖就把现场填的设备值弄丢，
路径也不显眼、不好备份。所以这里把两种布局分开：

- **应用根 `app_root()`** —— 源码运行 = `tower-ops/`；冻结后 = **exe 所在目录**（可写）。
  `configs/`、`runtime/` 一律落在应用根下，也就是"就在 exe 旁边"。
- **包内资源根 `bundle_root()`** —— 冻结后 = `sys._MEIPASS`（只读，装的是种子与只读资源）；
  源码运行时与应用根相同。

包内的 `configs/` 只当**首次启动的种子**：:func:`seed_user_files` 只补齐**缺失**的文件，
**绝不覆盖**用户已经改过的配置（见 技术决定记录 DEC-016；约定"别覆盖用户的设备配置"）。

本模块**不导入** PySide6 / pydantic / 任何设备 SDK，也不做 I/O 之外的副作用，
故 3.8 宿主侧同样可以安全导入。
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Callable, Iterable

# application/bootstrap/app_paths.py -> 上溯 2 级 = tower-ops 工程根
_HERE = os.path.dirname(os.path.abspath(__file__))
_SOURCE_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

# 种子释放时要跳过的非运行期文件（备份与缓存不该进现场目录）
_SKIP_SUFFIXES = (".bak", ".pyc", ".pyo")
_SKIP_DIRS = {"__pycache__"}


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结产物里（`sys.frozen` 由 bootloader 设置）。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> str:
    """**可写**的应用根：源码运行 = tower-ops 工程根；冻结 = exe 所在目录。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return _SOURCE_ROOT


def bundle_root() -> str:
    """**只读**的包内资源根：冻结 = `sys._MEIPASS`；源码 = 与应用根相同。

    兼容 `sys._MEIPASS` 缺失的异常情形：退回应用根（宁可读不到种子，也不抛异常挡启动）。
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return os.path.abspath(meipass)
        return app_root()
    return _SOURCE_ROOT


def configs_dir() -> str:
    """配置目录（可写）：`<应用根>/configs`。"""
    return os.path.join(app_root(), "configs")


def runtime_dir() -> str:
    """运行数据目录（可写）：`<应用根>/runtime`。"""
    return os.path.join(app_root(), "runtime")


def recipes_dir() -> str:
    """配方目录（可写）：`<应用根>/configs/recipes`（另存为的默认位置，DEC-035）。"""
    return os.path.join(configs_dir(), "recipes")


def seed_configs_dir() -> str:
    """包内种子配置目录：`<包内资源根>/configs`。"""
    return os.path.join(bundle_root(), "configs")


def _iter_seed_files(seed_dir: str) -> Iterable[str]:
    """遍历种子目录里的文件，返回相对路径（POSIX 风格），跳过备份/缓存。"""
    for root, dirs, files in os.walk(seed_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in sorted(files):
            if name.endswith(_SKIP_SUFFIXES):
                continue
            rel = os.path.relpath(os.path.join(root, name), seed_dir)
            yield rel.replace("\\", "/")


def seed_user_files(*, log: Callable[[str], None] | None = None) -> list[str]:
    """把包内 `configs/` 中**本节缺失**的文件释放到 :func:`configs_dir`。

    返回实际拷出的相对路径列表（源码运行时通常为空 —— 文件本来就在那儿）。

    **只补缺失，不覆盖**：目标已存在就跳过。现场调好的 `devices.yaml` / `motion.yaml` /
    `joint_targets.yaml` 不会被打包产物覆盖。
    """
    seed_dir = seed_configs_dir()
    if not os.path.isdir(seed_dir):
        return []
    target_root = configs_dir()
    if os.path.abspath(seed_dir) == os.path.abspath(target_root):
        return []          # 源码运行：种子即现场，无需释放
    copied: list[str] = []
    for rel in _iter_seed_files(seed_dir):
        src = os.path.join(seed_dir, *rel.split("/"))
        dst = os.path.join(target_root, *rel.split("/"))
        if os.path.exists(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
        if log is not None:
            log(f"释放种子配置: {rel}")
    return copied


def describe() -> str:
    """一行说明当前布局（给启动打印用）。"""
    if is_frozen():
        return (f"冻结运行: 应用根(可写)={app_root()}  包内资源={bundle_root()}")
    return f"源码运行: 应用根={app_root()}"


__all__ = [
    "is_frozen", "app_root", "bundle_root", "configs_dir", "runtime_dir",
    "seed_configs_dir", "seed_user_files", "describe",
]
