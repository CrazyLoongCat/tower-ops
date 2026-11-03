"""运行/日志参数（DEC-065「参数配置」页）：少量全局运行设置，原子落盘。

这些参数**不涉及设备配置**（设备在 E21 页、运动/引导在各自配置），只控制日志与运行目录行为。
文件：``configs/runtime/settings.yaml``（源码/exe 旁的 configs 下）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from io import StringIO
import logging
import os

from ruamel.yaml import YAML

from application.services.recipes.service import atomic_write, rollback

SCHEMA_VERSION = 1
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass
class RuntimeSettings:
    schema_version: int = SCHEMA_VERSION
    log_level: str = "DEBUG"       # 文件日志级别（诊断页细粒度依赖 DEBUG）
    log_backup_days: int = 14
    log_to_console: bool = False   # 控制台另开一份日志（GUI 启动默认关）

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
                   log_to_console=bool(data.get("log_to_console", False)))


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


__all__ = ["LOG_LEVELS", "RuntimeSettings", "RuntimeSettingsStore", "level_value"]
