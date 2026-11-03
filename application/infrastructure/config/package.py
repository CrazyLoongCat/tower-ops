"""配置包导入 (E16, 02 §10)。

- 只按**清单**加载; 校验路径/版本/资源引用;
- **不执行包内脚本** (清单里出现脚本一律拒绝, 并明确记录未执行);
- 未定义对象 (不在 catalog 的 id) 拒绝;
- 相对路径一律相对配置包根目录 (ConfigRoot), 不相对 cwd。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from application.infrastructure.config.base import ConfigRoot, is_protected
from application.infrastructure.config.compose import SECTIONS, ConfigCatalog

try:  # ruamel 为项目依赖
    from ruamel.yaml import YAML
except Exception:  # pragma: no cover
    YAML = None

_FORBIDDEN_SUFFIXES = (".py", ".pyd", ".so", ".dll", ".exe", ".bat", ".ps1", ".sh")


@dataclass
class PackageLoadResult:
    ok: bool
    data: dict = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    loaded_files: list[str] = field(default_factory=list)
    executed_scripts: list[str] = field(default_factory=list)


def _load_yaml(path: str) -> dict:
    if YAML is None:
        raise RuntimeError("未安装 ruamel.yaml, 无法读取配置包")
    yaml = YAML(typ="safe")
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh) or {}


def load_package(root: ConfigRoot, manifest: dict,
                 catalog: ConfigCatalog | None = None) -> PackageLoadResult:
    """按清单加载配置包. 任何非法即 ok=False, 不部分应用。"""
    issues: list[str] = []
    loaded: list[str] = []
    data: dict = {}

    if manifest.get("schema_version") not in (1, "1"):
        issues.append(f"schema_version 不支持: {manifest.get('schema_version')!r}")

    for entry in (manifest.get("files") or []):
        rel = str(entry.get("path", ""))
        if not rel:
            issues.append("清单条目缺 path")
            continue
        if rel.lower().endswith(_FORBIDDEN_SUFFIXES):
            issues.append(f"清单包含可执行/脚本文件, 拒绝加载: {rel!r}")
            continue
        try:
            full = root.resolve(rel)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"非法路径 {rel!r}: {exc}")
            continue
        if not os.path.exists(full):
            issues.append(f"清单文件缺失: {rel!r}")
            continue
        try:
            obj = _load_yaml(full)
        except Exception as exc:  # noqa: BLE001
            issues.append(f"解析失败 {rel!r}: {exc}")
            continue
        section = entry.get("section") or _infer_section(rel)
        if section not in SECTIONS:
            issues.append(f"未知配置节 {section!r} (来自 {rel!r})")
            continue
        if not isinstance(obj, dict):
            issues.append(f"{rel!r} 顶层必须是映射")
            continue
        data.setdefault(section, {}).update(obj)
        loaded.append(rel)

    if not issues:
        issues += _check_unknown_objects(data, catalog)
        issues += _check_no_protected_in_overrides(manifest)

    return PackageLoadResult(ok=not issues, data=data, issues=issues,
                             loaded_files=loaded, executed_scripts=[])


def _infer_section(rel: str) -> str:
    head = rel.replace("\\", "/").split("/")[0]
    return {"devices": "devices", "actions": "actions", "interlocks": "interlocks",
            "calibrations": "calibrations", "vision_pipelines": "vision_pipelines",
            "recipes": "recipes", "models": "models", "site": "site"}.get(head, head)


def _check_unknown_objects(data: dict, catalog: ConfigCatalog | None) -> list[str]:
    if catalog is None:
        return []
    issues: list[str] = []
    pairs = (("devices", catalog.device_ids, "设备"),
             ("actions", catalog.action_ids, "动作"),
             ("vision_pipelines", catalog.pipeline_ids, "流水线"),
             ("calibrations", catalog.calibration_ids, "标定"),
             ("models", catalog.model_ids, "模型"))
    for section, known, label in pairs:
        if not known:
            continue
        for oid in (data.get(section) or {}):
            if oid not in known:
                issues.append(f"未定义{label} {oid!r} (不在已注册目录中)")
    return issues


def _check_no_protected_in_overrides(manifest: dict) -> list[str]:
    issues = []
    for key in (manifest.get("overrides") or {}):
        if is_protected(key):
            issues.append(f"清单试图覆盖受保护路径 {key!r}")
    return issues


__all__ = ["PackageLoadResult", "load_package"]
