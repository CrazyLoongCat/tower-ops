"""启动执行方案（``--recipe`` 指名文件）的容错载入与旧版自愈（DEC-044）。

**起因（2026-10-19 现场）**：exe 旁边的 ``configs/recipes/`` 里还留着旧版
schema_version=1「工位版」执行方案；本版只认 schema 2，``--recipe`` 指名载入没有兜底，
ValidationError 直接抛到装配层 → ``[real] 装配失败``、整栈拒绝启动。

**裁定**：

1. **载入失败绝不致命**：记通知、跳过这条方案、装配继续（界面照常打开；作业台默认不选它，
   可在下拉里选别的方案，或用 ``--recipe`` 指向可用文件）。
   —— 口径与配方目录扫描一致（``load_recipes_dir`` 对坏文件本来就是"跳过不拖垮"）。
2. **只对启动配方做"旧版自愈"**：失败文件在**可写配置目录**内、且包内种子目录里有
   **同相对路径且自己能载入**的新版副本时：把现场旧文件**改名备份**为
   ``<原名>.incompatible-<时间戳>.bak``（内容原样保留，绝不删掉），再把包内新版释放回原路径
   并载入。其它路径 / 其它配方文件一律不动。
   —— 不搞"种子整体覆盖"：``devices.yaml`` / ``motion.yaml`` / 用户自己的配方都是活配置
   （DEC-016 / "别覆盖用户的设备配置"），全局覆盖会把现场调好的值冲掉。
3. 自愈过程写日志 + 返回 :class:`RecipeStartupNotice`，由 ``main.py`` 在控制台打印、
   并挂在 ``AppStack.startup_notices`` 上供界面/诊断查看。
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil
from dataclasses import dataclass
from typing import Any, Callable

from ruamel.yaml import YAML

from application.services.recipes.models import RecipeModel

_YAML = YAML(typ="safe")

# 通知动作（测试与界面按它分支）
ACTION_REPLACED = "replaced"    # 旧版已备份、包内新版已放回并载入
ACTION_SKIPPED = "skipped"      # 跳过未载入（不影响启动）


@dataclass(frozen=True)
class RecipeStartupNotice:
    """启动配方的问题说明（**不致命**）。"""

    path: str
    action: str          # ACTION_REPLACED / ACTION_SKIPPED
    message: str         # 给控制台/界面看的中文说明


def _emit(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        try:
            log(message)
        except Exception:  # noqa: BLE001 - 日志本身失败不能反过来挡启动
            pass


def _read_recipe(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = _YAML.load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError("执行方案 YAML 顶层必须是映射（dict）")
    return data


def _load_into(recipes, path: str) -> None:
    """按 ``--recipe`` 语义把文件载入为草稿（校验在 ``RecipeService.draft`` 里做）。"""
    recipes.draft(_read_recipe(path), source=path)


def _relative_inside(root: str, path: str) -> str | None:
    """path 在 root 之内则返回相对路径，否则 None（不同盘符也按"不在里面"处理）。"""
    try:
        rel = os.path.relpath(path, root)
    except ValueError:      # Windows: 不同盘符
        return None
    if rel == os.curdir or rel.startswith("..") or os.path.isabs(rel):
        return None
    return rel


def _unique_backup_path(path: str) -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = f"{path}.incompatible-{stamp}.bak"
    n = 1
    while os.path.exists(candidate):
        n += 1
        candidate = f"{path}.incompatible-{stamp}-{n}.bak"
    return candidate


def _reseed_from_bundle(recipes, path: str, *, seed_dir: str | None,
                        configs_root: str | None) -> RecipeStartupNotice | None:
    """旧文件改名备份 + 包内新版放回原路径并载入；任何一步不满足就原样碰都不碰。"""
    from application.bootstrap import app_paths

    seed_dir = seed_dir or app_paths.seed_configs_dir()
    configs_root = configs_root or app_paths.configs_dir()
    if not seed_dir or not os.path.isdir(seed_dir):
        return None
    rel = _relative_inside(configs_root, path)
    if rel is None:
        return None                     # 不在可写配置目录里：绝不碰
    seed_path = os.path.join(seed_dir, rel)
    if not os.path.isfile(seed_path):
        return None                     # 包内没有同相对路径的副本：没有"新版"可用
    if os.path.abspath(seed_path) == os.path.abspath(path):
        return None                     # 源码运行：种子目录即现场目录，不存在换新
    seed_data = _read_recipe(seed_path)
    RecipeModel.model_validate(seed_data)   # 包内副本必须自己能载入才敢用它

    backup = _unique_backup_path(path)
    os.replace(path, backup)            # 先保住现场旧文件（保留内容，不删）
    try:
        shutil.copy2(seed_path, path)
        recipes.draft(seed_data, source=path)
    except Exception:                   # noqa: BLE001 - 半路失败要回滚，不留"换了一半"
        try:
            os.replace(backup, path)
        except OSError:
            pass
        return None
    return RecipeStartupNotice(
        path=path,
        action=ACTION_REPLACED,
        message=(
            f"启动执行方案是旧版、当前版本载入不了，已自动换新：{os.path.basename(path)}"
            f"（现场旧文件保留为 {os.path.basename(backup)}，包内新版已放回原路径）。"))


def load_startup_recipe(recipes, path: str, *,
                        log: Callable[[str], None] | None = None,
                        seed_dir: str | None = None,
                        configs_root: str | None = None,
                        ) -> RecipeStartupNotice | None:
    """载入 ``--recipe`` 指名文件：成功返回 None；坏了就跳过（并尽量自愈），绝不抛异常。

    :param recipes: :class:`application.services.recipes.service.RecipeService`
    :param path: ``--recipe`` 路径（空串 = 没指定，直接返回 None）
    :param log: 通知落点（给日志设施；异常由本函数吞掉，绝不反过来挡启动）
    :param seed_dir: 包内种子配置目录（默认 :func:`app_paths.seed_configs_dir`）
    :param configs_root: 可写配置目录（默认 :func:`app_paths.configs_dir`）
    """
    if not path:
        return None
    path = os.path.abspath(path)
    if not os.path.exists(path):
        notice = RecipeStartupNotice(
            path=path, action=ACTION_SKIPPED,
            message=(f"启动执行方案文件不存在，已跳过（不影响启动）：{path}。"
                     "可在「执行方案」页另存一份，或启动时用 --recipe 指定。"))
        _emit(log, notice.message)
        return notice

    try:
        _load_into(recipes, path)
        return None
    except Exception as exc:            # noqa: BLE001 - 坏配方绝不拖垮整栈启动
        first = str(exc).strip().splitlines()
        reason = f"{type(exc).__name__}: {first[0]}" if first else type(exc).__name__
        reason = reason[:300]

    try:
        replaced = _reseed_from_bundle(recipes, path, seed_dir=seed_dir,
                                       configs_root=configs_root)
    except Exception as exc:            # noqa: BLE001 - 自愈失败退化成"跳过"
        replaced = None
        reason += f"；自动换新失败: {type(exc).__name__}"
    if replaced is not None:
        _emit(log, replaced.message)
        return replaced

    notice = RecipeStartupNotice(
        path=path, action=ACTION_SKIPPED,
        message=(f"启动执行方案载入失败，已跳过（不影响启动，界面照常打开）："
                 f"{os.path.basename(path)}；原因: {reason}。"
                 "可在「执行方案」页重存为当前版本，或用 --recipe 换一份可用方案。"))
    _emit(log, notice.message)
    return notice


__all__ = [
    "ACTION_REPLACED", "ACTION_SKIPPED", "RecipeStartupNotice", "load_startup_recipe",
]
