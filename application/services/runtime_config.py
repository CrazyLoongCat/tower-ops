"""运行/日志参数（DEC-065「参数配置」页）：少量全局运行设置，原子落盘。

这些参数**不涉及设备配置**（设备在 E21 页、运动/引导在各自配置），只控制日志与运行目录行为。
文件：``configs/runtime/settings.yaml``（源码/exe 旁的 configs 下）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from io import StringIO
import logging
import os

from ruamel.yaml import YAML

from application.services.recipes.service import atomic_write, rollback

SCHEMA_VERSION = 1
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# 全局快捷键（DEC-115）：动作 id -> 界面名称。空字符串 = 未绑定。
# 顺序即「参数配置 → 快捷键」卡片的显示顺序。
SHORTCUT_ACTIONS: tuple[tuple[str, str], ...] = (
    ("stop_all", "停止全部 / 急停"),
    ("start", "作业启动"),
    ("pause", "暂停"),
    ("resume", "继续"),
    ("single_step", "单步"),
    ("page_workbench", "切到「作业台」"),
    ("page_console", "切到「操作台」"),
    ("page_motion", "切到「运动方案」"),
    ("page_vision", "切到「视觉调试」"),
    ("page_diagnostics", "切到「日志诊断」"),
)
# 默认键：功能键用于作业控制，Ctrl+数字用于页面切换；不与文本编辑常用组合冲突。
DEFAULT_SHORTCUTS: dict[str, str] = {
    "stop_all": "F12",
    "start": "F5",
    "pause": "F6",
    "resume": "F7",
    "single_step": "F8",
    "page_workbench": "Ctrl+1",
    "page_console": "Ctrl+2",
    "page_motion": "Ctrl+3",
    "page_vision": "Ctrl+4",
    "page_diagnostics": "Ctrl+5",
}


def normalize_shortcuts(raw: object) -> dict[str, str]:
    """把配置里的快捷键表归一成完整映射：缺省项用默认键，未知键忽略，空串=未绑定。"""
    out = dict(DEFAULT_SHORTCUTS)
    if isinstance(raw, dict):
        for key, _label in SHORTCUT_ACTIONS:
            if key in raw:
                value = raw.get(key)
                out[key] = "" if value is None else str(value).strip()
    return out


def duplicate_shortcuts(shortcuts: dict[str, str]) -> list[str]:
    """返回重复绑定的键（规范化后比较；空串不参与）。"""
    seen: dict[str, str] = {}
    dup: list[str] = []
    for key, _label in SHORTCUT_ACTIONS:
        seq = str(shortcuts.get(key, "") or "").strip()
        if not seq:
            continue
        if seq in seen:
            dup.append(seq)
        else:
            seen[seq] = key
    return sorted(set(dup))


@dataclass
class RuntimeSettings:
    schema_version: int = SCHEMA_VERSION
    log_level: str = "DEBUG"       # 文件日志级别（诊断页细粒度依赖 DEBUG）
    log_backup_days: int = 14
    log_to_console: bool = False   # 控制台另开一份日志（GUI 启动默认关）
    # DEC-115：全局快捷键（动作 id -> QKeySequence 字符串；空=未绑定）
    shortcuts: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SHORTCUTS))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "RuntimeSettings":
        data = dict(data or {})
        level = str(data.get("log_level", "DEBUG")).upper()
        if level not in LOG_LEVELS:
            level = "DEBUG"
        return cls(schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
                   log_level=level,
                   log_backup_days=int(data.get("log_backup_days", 14)),
                   log_to_console=bool(data.get("log_to_console", False)),
                   shortcuts=normalize_shortcuts(data.get("shortcuts")))


def level_value(name: str) -> int:
    return getattr(logging, str(name or "DEBUG").upper(), logging.DEBUG)


def _dump(data: dict) -> str:
    yml = YAML(typ="safe")
    yml.default_flow_style = False
    yml.indent(mapping=2, sequence=4, offset=2)
    buf = StringIO()
    yml.dump(data, buf)
    return buf.getvalue()


class RuntimeSettingsStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = os.path.abspath(path) if path else None
        self._settings: RuntimeSettings | None = None

    def load(self) -> RuntimeSettings:
        if self.path and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self._settings = RuntimeSettings.from_dict(
                    YAML(typ="safe").load(fh) or {})
        else:
            self._settings = RuntimeSettings()
        return self._settings

    def save(self) -> None:
        if self._settings is None:
            raise RuntimeError("尚未 load()")
        if self.path is None:
            return
        header = ("# 运行/日志参数（「参数配置」页维护）。改文件日志级别/保留天数需重启生效。\n")
        atomic_write(self.path, header + _dump(self._settings.to_dict()), backup=True)

    @property
    def settings(self) -> RuntimeSettings:
        if self._settings is None:
            self.load()
        assert self._settings is not None
        return self._settings

    def rollback(self) -> None:
        if self.path:
            rollback(self.path)
            self._settings = None


__all__ = ["DEFAULT_SHORTCUTS", "LOG_LEVELS", "RuntimeSettings",
           "RuntimeSettingsStore", "SHORTCUT_ACTIONS", "duplicate_shortcuts",
           "level_value", "normalize_shortcuts"]
