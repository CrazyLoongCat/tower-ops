"""命令句柄与停止摘要 (12 §4.2, 02 §2).

Handle 是异步方法返回的可查询对象: UI 不阻塞等待.
命令生命周期 CREATED->VALIDATED->QUEUED->SENT->ACCEPTED->COMPLETED
或 REJECTED/FAILED/CANCELLED/UNKNOWN. 传输写成功只标 SENT.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from application.contracts.enums import CommandState, StopScope, StopState
from application.contracts.ids import CommandId, RequestId


@dataclass
class CommandHandle:
    """单一命令的账本句柄. 后续 tick 只查询同一 Handle, 不重新运动."""

    command_id: CommandId
    action_ref: str
    created_monotonic_s: float = field(default_factory=time.monotonic)
    state: CommandState = CommandState.CREATED
    board_id: str | None = None
    accepted_monotonic_s: float | None = None
    completed_monotonic_s: float | None = None
    done_ok: bool | None = None
    reject_reason: str | None = None
    error_reason: str | None = None
    # 迟到/旧ID/重复事件只诊断, 不推进动作.
    diagnostics: list[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            CommandState.COMPLETED,
            CommandState.REJECTED,
            CommandState.FAILED,
            CommandState.CANCELLED,
            CommandState.UNKNOWN,
        )

    @property
    def is_success(self) -> bool:
        return self.state is CommandState.COMPLETED

    def elapsed_ms(self) -> float:
        end = self.completed_monotonic_s or time.monotonic()
        return (end - self.created_monotonic_s) * 1000.0

    def note(self, message: str) -> None:
        self.diagnostics.append(message)


@dataclass
class ExecutorStopReport:
    """单个执行器的停止结果. 未知不全绿 (T18)."""

    executor_id: str
    state: StopState = StopState.STOPPING
    detail: str | None = None
    confirmed_monotonic_s: float | None = None


@dataclass
class StopSummary:
    """停止摘要. 逐执行器确认, 未知进任务 FAULT 摘要, 不另造任务态."""

    scope: StopScope
    reason: str
    request_id: RequestId
    requested_monotonic_s: float = field(default_factory=time.monotonic)
    executors: dict[str, ExecutorStopReport] = field(default_factory=dict)

    def report(self, executor_id: str) -> ExecutorStopReport:
        rep = self.executors.get(executor_id)
        if rep is None:
            rep = ExecutorStopReport(executor_id=executor_id)
            self.executors[executor_id] = rep
        return rep

    @property
    def all_confirmed(self) -> bool:
        return bool(self.executors) and all(
            r.state is StopState.CONFIRMED for r in self.executors.values()
        )

    @property
    def has_unknown(self) -> bool:
        return any(
            r.state in (StopState.UNKNOWN, StopState.FAILED)
            for r in self.executors.values()
        )


__all__ = ["CommandHandle", "ExecutorStopReport", "StopSummary"]
