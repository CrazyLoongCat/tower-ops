"""全局日志设施: 文本日志文件 + 控制台 + **事件桥**(日志 → records.db 事件表)。

- 单一应用日志器根 ``"towerops"``；各模块用 :func:`get_logger` 取子日志器
  （``get_logger("services.actions")`` → ``towerops.services.actions``）。
- **两个落点，一处来源**：
  * 文本文件 —— ``<runtime>/logs/tower-ops.log``，``TimedRotatingFileHandler`` **按天轮转**、
    保留 ``backup_days`` 天（``tower-ops.log.2026-10-15`` 之类）。所有 ``towerops.*`` 记录都进它。
  * 事件库 —— 通过 :class:`EventStoreHandler` 把 **INFO 及以上**的日志记录桥进
    :class:`~application.services.records.service.RecordService` 的 ``events`` 表，
    于是「日志诊断」页既能看文件、也能按级别/类型/设备/任务过滤的**全部**日志。
- **不重复、不递归**：领域事件（``records.append_event``）除了入库，还**镜像**一行到文件
  （镜像日志器 :data:`EVENT_LOGGER`）；而事件桥**忽略**该镜像日志器，免得同一行又入库/又再镜像。
  事件桥写库走 ``mirror=False``，所以一条 :mod:`logging` 记录只会在文件里出现一次、库里出现一次。
- **纯净**：不导入 PySide6 / pydantic / 任何设备 SDK；只碰标准库与事件契约。
  幂等：重复 :func:`install` 返回同一设施，不叠加处理器。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import traceback
from dataclasses import dataclass, field
from typing import Any

from application.infrastructure.events import Event, Severity

# 应用日志器根 / 领域事件镜像日志器（事件桥需**跳过**它，见模块 docstring）
LOGGER_ROOT = "towerops"
EVENT_LOGGER = "towerops.event"

# 文件与事件里单条消息的截断上限（避免异常堆栈/巨串把库和页面撑爆）
_MAX_MESSAGE = 4000

_FACILITY_ATTR = "_towerops_logging_facility"


def log_dir(runtime_dir: str | None = None) -> str:
    """日志目录：``<runtime>/logs``。``runtime_dir`` 缺省用应用运行目录。"""
    if runtime_dir is None:
        from application.bootstrap import app_paths

        runtime_dir = app_paths.runtime_dir()
    return os.path.join(runtime_dir, "logs")


def log_path(runtime_dir: str | None = None) -> str:
    """当前（活动）文本日志文件路径：``<runtime>/logs/tower-ops.log``。"""
    return os.path.join(log_dir(runtime_dir), "tower-ops.log")


def get_logger(name: str = "") -> logging.Logger:
    """取应用子日志器：``get_logger("services.actions")`` → ``towerops.services.actions``。"""
    full = LOGGER_ROOT if not name else f"{LOGGER_ROOT}.{name}"
    return logging.getLogger(full)


def _severity_for(levelno: int) -> Severity:
    if levelno >= logging.CRITICAL:
        return Severity.CRITICAL
    if levelno >= logging.ERROR:
        return Severity.ERROR
    if levelno >= logging.WARNING:
        return Severity.WARNING
    if levelno >= logging.INFO:
        return Severity.INFO
    return Severity.DEBUG


def _event_type_for(logger_name: str) -> str:
    """把日志器名压成事件类型：``towerops.services.actions`` → ``services.actions``。"""
    if logger_name == LOGGER_ROOT:
        return "app"
    prefix = LOGGER_ROOT + "."
    return logger_name[len(prefix):] if logger_name.startswith(prefix) else logger_name


def _format_exception(record: logging.LogRecord) -> str | None:
    if not record.exc_info:
        return None
    text = "".join(traceback.format_exception(*record.exc_info))
    return text.strip() or None


class EventStoreHandler(logging.Handler):
    """把日志记录桥进 ``RecordService`` 事件表（`events`）。

    - 只处理 ``level`` 及以上（默认 INFO，与诊断页的“常规日志”口径一致）；
    - 跳过 :data:`EVENT_LOGGER` 及其子（领域事件镜像），避免重复入库/递归；
    - 写库以 ``mirror=False`` 调用，**不再镜像到文件**（原记录已由文件处理器写过）。
    """

    def __init__(self, records: Any, *, level: int = logging.INFO,
                 skip_prefixes: tuple[str, ...] = (EVENT_LOGGER,)) -> None:
        super().__init__(level)
        self._records = records
        self._skip = tuple(skip_prefixes)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if any(record.name == p or record.name.startswith(p + ".")
                   for p in self._skip):
                return
            try:
                message = record.getMessage()
            except Exception:  # noqa: BLE001 - 格式化失败不该吞掉整条链路
                message = str(record.msg)
            event = Event(
                event_type=_event_type_for(record.name),
                severity=_severity_for(record.levelno),
                message=message[:_MAX_MESSAGE],
                reason=_format_exception(record),
                # 结构化字段：调用方用 ``extra={"device_id": ...}`` 带进来（缺省为 None）。
                # 于是「日志诊断」页能按设备/任务/步骤过滤**日志行**（不只领域事件）。
                device_id=getattr(record, "device_id", None) or None,
                run_id=getattr(record, "run_id", None) or None,
                station_instance_id=getattr(record, "station_instance_id", None) or None,
                step_id=getattr(record, "step_id", None) or None,
                command_id=getattr(record, "command_id", None) or None,
            )
            self._records.append_event_nonblocking(event, mirror=False)
        except Exception:  # noqa: BLE001 - 记录日志失败绝不打断业务
            self.handleError(record)


@dataclass
class LoggingFacility:
    """已安装的日志设施句柄。"""

    log_dir: str
    log_path: str
    root: logging.Logger
    file_handler: logging.Handler
    console_handler: logging.Handler | None = None
    bridge: EventStoreHandler | None = None
    _extra_handlers: list[logging.Handler] = field(default_factory=list)

    def attach_event_bridge(self, records: Any, *,
                            level: int = logging.INFO) -> EventStoreHandler:
        """把日志桥接进 ``records`` 事件表（幂等：已有桥则替换）。"""
        if self.bridge is not None:
            self.root.removeHandler(self.bridge)
        self.bridge = EventStoreHandler(records, level=level)
        self.root.addHandler(self.bridge)
        return self.bridge

    def detach_event_bridge(self) -> None:
        if self.bridge is not None:
            self.root.removeHandler(self.bridge)
            self.bridge = None

    def close(self) -> None:
        for handler in (self.bridge, self.console_handler, self.file_handler):
            if handler is not None:
                try:
                    self.root.removeHandler(handler)
                    handler.close()
                except Exception:  # noqa: BLE001
                    pass
        self.bridge = None
        if getattr(self.root, _FACILITY_ATTR, None) is self:
            try:
                delattr(self.root, _FACILITY_ATTR)
            except AttributeError:
                pass


def _current_facility() -> LoggingFacility | None:
    root = logging.getLogger(LOGGER_ROOT)
    facility = getattr(root, _FACILITY_ATTR, None)
    return facility if isinstance(facility, LoggingFacility) else None


def install(runtime_dir: str | None = None, *, level: int = logging.INFO,
            backup_days: int = 14, to_console: bool = True,
            file_level: int = logging.DEBUG) -> LoggingFacility:
    """安装全局日志设施（幂等，按 ``runtime_dir`` 归属）。

    同一目录内重复调用返回**同一**设施（不叠加处理器）；请求的目录与已安装的不同时，
    先关旧设施再建新的（便于测试与多栈隔离 —— 每个栈把日志落到自己的 runtime）。
    """
    directory = log_dir(runtime_dir)
    existing = _current_facility()
    if existing is not None:
        if os.path.abspath(existing.log_dir) == os.path.abspath(directory):
            return existing
        existing.close()

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "tower-ops.log")

    root = logging.getLogger(LOGGER_ROOT)
    root.setLevel(logging.DEBUG)      # 级别交给处理器；避免拦掉 DEBUG 落文件
    root.propagate = False            # 不往 Python 根日志器冒泡（防重复/污染）

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=int(backup_days), encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(int(file_level))
    root.addHandler(file_handler)

    console_handler: logging.Handler | None = None
    if to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(level)
        root.addHandler(console_handler)

    facility = LoggingFacility(log_dir=directory, log_path=path, root=root,
                               file_handler=file_handler,
                               console_handler=console_handler)
    setattr(root, _FACILITY_ATTR, facility)
    return facility


def current() -> LoggingFacility | None:
    """已安装的设施（未安装返回 ``None``）。"""
    return _current_facility()


def reset() -> None:
    """关闭并卸载当前设施（测试隔离用；生产无需调用）。"""
    facility = _current_facility()
    if facility is not None:
        facility.close()


def wire_records(records: Any, runtime_dir: str | None = None, *,
                 level: int = logging.INFO,
                 to_console: bool = True) -> LoggingFacility:
    """把日志设施接到一个 ``RecordService`` 上：装设施 + 挂事件桥 + 设领域事件镜像。

    装配栈（离线/生产）调用它，于是一条日志既进文本文件、又进 ``events`` 表（DEC-029）。
    """
    facility = install(runtime_dir, level=level, to_console=to_console)
    records.set_mirror_logger(get_logger("event"))
    facility.attach_event_bridge(records, level=level)
    return facility


__all__ = [
    "EVENT_LOGGER", "LOGGER_ROOT", "EventStoreHandler", "LoggingFacility",
    "current", "get_logger", "install", "log_dir", "log_path", "reset",
    "wire_records",
]

