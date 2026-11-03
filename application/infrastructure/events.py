"""统一事件契约 (12 §4.4, 02 §11).

事件含 UTC 时间、本地单调、severity、event_type、各类 ID、版本、错误原因与处理建议.
快照有 revision, 事件有 sequence; UI 丢订阅后重取快照.
"""
from __future__ import annotations

import enum
import itertools
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


class Severity(str, enum.Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class Event:
    """一条可追溯事件. 生产/写库分离, 关键意图需持久化确认."""

    event_type: str
    severity: Severity = Severity.INFO
    message: str = ""
    run_id: str | None = None
    station_instance_id: str | None = None
    step_id: str | None = None
    command_id: str | None = None
    device_id: str | None = None
    capture_id: str | None = None
    frame_id: str | None = None
    version: str | None = None
    reason: str | None = None
    advice: str | None = None
    sequence: int = 0
    monotonic_s: float = field(default_factory=time.monotonic)
    utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict:
        return {
            "event_type": self.event_type, "severity": self.severity.value,
            "message": self.message, "run_id": self.run_id,
            "station_instance_id": self.station_instance_id, "step_id": self.step_id,
            "command_id": self.command_id, "device_id": self.device_id,
            "capture_id": self.capture_id, "frame_id": self.frame_id,
            "version": self.version, "reason": self.reason, "advice": self.advice,
            "sequence": self.sequence, "monotonic_s": self.monotonic_s, "utc": self.utc,
        }


class EventSequencer:
    """单调 sequence 分配 (每进程一份)."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)

    def next(self) -> int:
        return next(self._counter)


__all__ = ["Event", "EventSequencer", "Severity"]
