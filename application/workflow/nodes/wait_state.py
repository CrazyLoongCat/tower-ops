"""wait.state 节点 (E13, 02 §9.1)。

等待**新鲜状态**满足**持续窗**: 条件必须在连续 stable_ticks 个 tick 都成立才算满足;
超时 (deadline_ms) 未满足则 FAILURE, 不伪造成成功。不阻塞 tick。
"""
from __future__ import annotations

import time
from typing import Any

from py_trees.common import Status

from application.contracts.enums import TelemetryFreshness
from application.workflow.nodes.base import BaseStepNode


class WaitStateNode(BaseStepNode):
    """等待设备/状态满足结构化条件并保持稳定窗."""

    def __init__(self, name: str, *, device_id: str, field_name: str | None = None,
                 expected: Any = None, max_age_ms: float = 1000.0,
                 stable_ticks: int = 1, deadline_ms: int | None = None,
                 **kw) -> None:
        super().__init__(name, **kw)
        self.device_id = device_id
        self.field_name = field_name
        self.expected = expected
        self.max_age_ms = float(max_age_ms)
        self.stable_ticks = max(1, int(stable_ticks))
        self.deadline_ms = deadline_ms
        self._held = 0
        self._started_ms: float | None = None
        self.satisfied = False

    def initialise(self) -> None:
        super().initialise()
        self._held = 0
        self._started_ms = None
        self.satisfied = False

    def _tick(self, entry) -> Status:
        store = getattr(self.executor, "state_store", None)
        if store is None:
            return self._mark_failure("无状态源 (state_store)")
        now_ms = time.monotonic() * 1000.0
        if self._started_ms is None:
            self._started_ms = now_ms
            self._held = 0

        rec = store.get(self.device_id)
        ok = (rec is not None
              and rec.validity is TelemetryFreshness.VALID
              and rec.is_fresh(self.max_age_ms)
              and (self.field_name is None
                   or rec.values.get(self.field_name) == self.expected))
        self._held = self._held + 1 if ok else 0

        if self._held >= self.stable_ticks:
            self.satisfied = True
            si = self.ctx.station_instance_id or ""
            self.outputs.set(si, self.step_id, "device", self.device_id)
            self.outputs.set(si, self.step_id, "held_ticks", self._held)
            return self._mark_success({"device": self.device_id, "held": self._held})

        if self.deadline_ms is not None and (now_ms - self._started_ms) > self.deadline_ms:
            return self._mark_failure(
                f"等待 {self.device_id}.{self.field_name} 超时 {self.deadline_ms}ms")
        return Status.RUNNING


__all__ = ["WaitStateNode"]
