"""配置根、schema 版本与受保护路径 (E16, 02 §10)。

- 相对路径以**配置包根目录**解析 (不相对用户 cwd) —— 总纲 §4.4 明确要求;
- 受保护路径 (设备寻址/互锁/停止/硬限值/固件) 不能被 override 覆盖;
- schema_version 必须存在且受支持, 否则拒绝。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from application.domain.errors import ValidationFailure

CONFIG_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

# 受保护路径前缀: 任何 override/合成都不得触及 (设备寻址/互锁/停止/硬限值/固件)。
PROTECTED_PATH_PREFIXES = (
    "devices.", "device.", "links.", "link.", "boards.", "board.",
    "interlocks.", "interlock.", "stop.", "stops.", "limits.", "hard_limits.",
    "addressing.", "firmware.", "firmware_profiles.", "profiles.",
)


class ProtectedPathError(ValidationFailure):
    """命中受保护路径."""


class OverrideNotAllowed(ValidationFailure):
    """override 不在白名单内."""


class ConfigVersionError(ValidationFailure):
    """schema_version 缺失或不支持."""


def is_protected(path: str) -> bool:
    p = path.strip().lower()
    return any(p == pre.rstrip(".") or p.startswith(pre)
               for pre in PROTECTED_PATH_PREFIXES)


def require_schema_version(data: dict) -> int:
    """校验 schema_version 存在且受支持."""
    if not isinstance(data, dict) or "schema_version" not in data:
        raise ConfigVersionError("配置缺少 schema_version")
    v = data["schema_version"]
    if v not in SUPPORTED_SCHEMA_VERSIONS:
        raise ConfigVersionError(
            f"不支持的 schema_version={v!r}; 支持 {SUPPORTED_SCHEMA_VERSIONS}")
    return int(v)


@dataclass(frozen=True)
class ConfigRoot:
    """配置包根目录. 所有相对路径相对它解析, 并禁止越出根目录。"""

    root: str

    def resolve(self, relative: str) -> str:
        if os.path.isabs(relative):
            raise ProtectedPathError(f"配置路径必须是相对路径: {relative!r}")
        full = os.path.abspath(os.path.join(self.root, relative))
        base = os.path.abspath(self.root)
        if not (full == base or full.startswith(base + os.sep)):
            raise ProtectedPathError(f"配置路径越出包根目录: {relative!r}")
        return full


__all__ = [
    "CONFIG_SCHEMA_VERSION", "PROTECTED_PATH_PREFIXES", "ConfigRoot",
    "ConfigVersionError", "OverrideNotAllowed", "ProtectedPathError",
    "SUPPORTED_SCHEMA_VERSIONS", "is_protected", "require_schema_version",
]
