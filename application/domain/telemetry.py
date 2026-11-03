"""遥测与状态时效 (12 §4.2, 02 §3.3).

连接状态、设备操作状态、时效分别保存. 失效后数值保留显示,
但不能继续作为正常反馈作为动作前置.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from application.contracts.enums import (
    ConnectionState,
    DeviceOperationalState,
    TelemetryFreshness,
)
from application.contracts.ids import DeviceId, SessionEpoch
from application.domain.errors import StaleState


@dataclass
class TelemetryRecord:
    """一次设备状态采样. 含来源/序号/epoch/时间/时效."""

    device_id: DeviceId
    values: Mapping[str, Any]
    session_epoch: SessionEpoch = 0
    sequence: int = 0
    source_time_ns: int | None = None
    received_monotonic_s: float = field(default_factory=time.monotonic)
    validity: TelemetryFreshness = TelemetryFreshness.UNVERIFIED
    connection: ConnectionState = ConnectionState.OFFLINE
    operational: DeviceOperationalState = DeviceOperationalState.UNKNOWN

    def age_ms(self, now_monotonic_s: float | None = None) -> float:
        now = time.monotonic() if now_monotonic_s is None else now_monotonic_s
        return (now - self.received_monotonic_s) * 1000.0

    def is_fresh(self, max_age_ms: float, now_monotonic_s: float | None = None) -> bool:
        """时效为 VALID 且在允许年龄内, 才可作为正常前置."""
        if self.validity is not TelemetryFreshness.VALID:
            return False
        return self.age_ms(now_monotonic_s) <= max_age_ms

    def mark_stale(self) -> None:
        """断开/超时后使相关遥测 STALE (02 §4.4)."""
        self.validity = TelemetryFreshness.STALE


class StateStore:
    """按设备保存最近一份遥测; 提供时效门. 单所有者串行访问."""

    def __init__(self) -> None:
        self._records: dict[DeviceId, TelemetryRecord] = {}
        self._max_age_ms: dict[DeviceId, float] = {}

    def set_max_age(self, device_id: DeviceId, max_age_ms: float) -> None:
        self._max_age_ms[device_id] = max_age_ms

    def update(self, record: TelemetryRecord) -> None:
        self._records[record.device_id] = record

    def get(self, device_id: DeviceId) -> TelemetryRecord | None:
        return self._records.get(device_id)

    def require_fresh(self, device_id: DeviceId, now_monotonic_s: float | None = None) -> TelemetryRecord:
        """前置检查: 不满足则抛 StaleState (禁止用陈旧反馈放行动作)."""
        rec = self._records.get(device_id)
        if rec is None:
            raise StaleState(device_id, "UNVERIFIED", None)
        max_age = self._max_age_ms.get(device_id, 1000.0)
        if not rec.is_fresh(max_age, now_monotonic_s):
            raise StaleState(device_id, rec.validity.value, rec.age_ms(now_monotonic_s))
        return rec

    def invalidate(self, device_id: DeviceId) -> None:
        rec = self._records.get(device_id)
        if rec is not None:
            rec.mark_stale()


__all__ = ["StateStore", "TelemetryRecord"]
