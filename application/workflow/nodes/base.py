"""步骤节点基类与输出作用域 (E13 抽出, 供 steps/wait_state/operator_confirm 共用).

约束 (12 §4.4, 02 §9.5):
- setup 校验接口/能力, 不运动;
- initialise 取执行账本, 只对未提交实例一次提交;
- update 快速返回 RUNNING/SUCCESS/FAILURE/INVALID, 不阻塞采集/运动/线程 join;
- 已 COMPLETED 的实例再入节点直接返回已登记结果 (不重复非幂等动作);
- terminate(INVALID/FAILURE) 申请取消, 不标示物理停稳; UNKNOWN 保持隔离.
"""
from __future__ import annotations

from typing import Any

import py_trees
from py_trees.common import Status

from application.contracts.context import ExecutionKey, RunContext
from application.contracts.enums import FailurePolicy
from application.domain.errors import ValidationFailure


class OutputStore:
    """按 station_instance 作用域保存步骤输出 (T15/E13). 不跨工位/子流程/迭代串用."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    def set(self, station_instance_id: str, step_id: str, field_name: str,
            value: Any) -> None:
        self._data.setdefault((station_instance_id, step_id), {})[field_name] = value

    def get(self, station_instance_id: str, step_id: str, field_name: str) -> Any:
        return self._data.get((station_instance_id, step_id), {}).get(field_name)

    def step_outputs(self, station_instance_id: str, step_id: str) -> dict[str, Any]:
        return dict(self._data.get((station_instance_id, step_id), {}))


class BaseStepNode(py_trees.behaviour.Behaviour):
    """步骤节点基类: 账本一次提交 + 非阻塞 update."""

    def __init__(self, name: str, *, step_id: str, ctx: RunContext,
                 executor: Any, outputs: OutputStore, attempt: int = 1) -> None:
        super().__init__(name=name)
        self.step_id = step_id
        self.ctx = ctx
        self.executor = executor
        self.outputs = outputs
        self.attempt = attempt
        self.key = ExecutionKey(
            run_id=ctx.run_id,
            station_instance_id=ctx.station_instance_id or "",
            step_id=step_id, attempt=attempt, iteration=ctx.iteration,
        )
        self.last_status = Status.RUNNING
        self.failure_reason: str | None = None
        self.failure_policy: FailurePolicy | None = None
        self.initialised_count = 0

    # ---- py_trees 生命周期 ----
    def setup(self, **kwargs: Any) -> None:
        if self.executor is None:
            raise ValidationFailure(f"节点 {self.name} 缺少执行器")

    def initialise(self) -> None:
        self.initialised_count += 1
        self.failure_reason = None

    def update(self) -> Status:
        entry = self.executor.ledger.entry(self.key) if self.executor.ledger else None
        if entry is not None and entry.terminal and entry.result is not None:
            return Status.SUCCESS          # 已登记结果, 不重复动作
        self.last_status = self._tick(entry)
        return self.last_status

    def _tick(self, entry) -> Status:  # pragma: no cover - 抽象
        raise NotImplementedError

    def terminate(self, new_status: Status) -> None:
        if new_status in (Status.INVALID, Status.FAILURE):
            self._on_terminate()

    def _on_terminate(self) -> None:
        pass

    # ---- 结果 ----
    def _mark_success(self, result: Any = None) -> Status:
        if self.executor.ledger:
            if result is not None:
                self.executor.ledger.set_result(self.key, result)
            self.executor.ledger.mark_terminal(self.key)
        return Status.SUCCESS

    def _mark_failure(self, reason: str, policy: FailurePolicy | None = None) -> Status:
        self.failure_reason = reason
        self.failure_policy = policy
        return Status.FAILURE

    # ---- 故障恢复 ----
    def arm_retry(self, reason: str = "") -> int:
        """为「从失败步骤恢复」准备下一次尝试（DEC-061）。

        只把**本节点**换到新的 attempt 账本键（已完成步骤仍命中旧键的 terminal 结果，
        不会重放）；节点自身在下次 tick 会重新 ``initialise``。返回新的 attempt 号。
        """
        self.attempt += 1
        self.key = ExecutionKey(
            run_id=self.ctx.run_id,
            station_instance_id=self.ctx.station_instance_id or "",
            step_id=self.step_id,
            attempt=self.attempt,
            iteration=self.ctx.iteration,
        )
        self.last_status = Status.RUNNING
        self.failure_reason = None
        self.failure_policy = None
        return self.attempt


__all__ = ["BaseStepNode", "OutputStore"]
