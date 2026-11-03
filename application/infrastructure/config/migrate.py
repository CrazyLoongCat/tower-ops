"""配置版本迁移与差异 (E16, 02 §10)。

- 迁移只产出**新副本**, 绝不就地修改 (在途运行快照不被自动改写);
- 缺迁移步骤即拒绝, 不猜测;
- 迁移后仍须走五层验证 (由调用方执行)。
"""
from __future__ import annotations

import copy
from typing import Callable

from application.infrastructure.config.base import (
    CONFIG_SCHEMA_VERSION,
    ConfigVersionError,
)
from application.infrastructure.config.compose import SECTIONS

# 旧键 -> 新节 (历史配置兼容)
LEGACY_KEY_MAP = {
    "camera": "devices", "cameras": "devices", "boards": "devices",
    "action": "actions", "vision": "vision_pipelines", "pipeline": "vision_pipelines",
    "calib": "calibrations",
}


def _v0_to_v1(data: dict) -> tuple[dict, list[str]]:
    """无版本 (0) -> 1: 建立 schema_version 与标准分节, 迁移历史键名。"""
    out = copy.deepcopy(data)
    changes: list[str] = []
    for legacy, new in list(LEGACY_KEY_MAP.items()):
        if legacy in out and new not in out:
            out[new] = out.pop(legacy)
            changes.append(f"节 {legacy!r} -> {new!r}")
    for sec in SECTIONS:
        if sec not in out:
            out[sec] = {}
            changes.append(f"补充缺省节 {sec!r}={{}}")
    out["schema_version"] = 1
    changes.append("写入 schema_version=1")
    return out, changes


MIGRATIONS: dict[int, Callable[[dict], tuple[dict, list[str]]]] = {0: _v0_to_v1}


def migrate_config(data: dict, *,
                   target: int = CONFIG_SCHEMA_VERSION) -> tuple[dict, list[str]]:
    """把配置迁移到 target 版本. 返回 (新副本, 变更说明); 不修改入参。"""
    if not isinstance(data, dict):
        raise ConfigVersionError(f"配置必须是映射, 得到 {type(data).__name__}")
    current = data.get("schema_version", 0)
    if not isinstance(current, int):
        raise ConfigVersionError(f"schema_version 非法: {current!r}")
    if current > target:
        raise ConfigVersionError(f"源版本 {current} 高于目标 {target} (拒绝降级)")
    out = copy.deepcopy(data)
    changes: list[str] = []
    while current < target:
        step = MIGRATIONS.get(current)
        if step is None:
            raise ConfigVersionError(f"缺少 {current} -> {current + 1} 的迁移步骤")
        out, c = step(out)
        changes += c
        nxt = out.get("schema_version")
        if not isinstance(nxt, int) or nxt <= current:
            raise ConfigVersionError(f"迁移 {current} 未正确提升 schema_version")
        current = nxt
    return out, changes


def flatten(data: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in data.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        elif isinstance(v, list):
            out[key] = tuple(map(str, v))
        else:
            out[key] = v
    return out


def diff_config(a: dict, b: dict) -> dict[str, tuple]:
    """返回 {路径: (旧值, 新值)}. 仅预览, 不修改任何一方。"""
    fa, fb = flatten(a), flatten(b)
    keys = set(fa) | set(fb)
    return {k: (fa.get(k, "<缺>"), fb.get(k, "<缺>")) for k in sorted(keys)
            if fa.get(k) != fb.get(k)}


__all__ = ["LEGACY_KEY_MAP", "MIGRATIONS", "diff_config", "flatten", "migrate_config"]
