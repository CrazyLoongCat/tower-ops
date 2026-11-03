"""任务状态机 - 单一权威 (12 §4.2, 02 §3.2, A14).

规则:
- PAUSED 不能由点击"暂停"即时设置, 必须经 PAUSE_REQUESTED 且安全边界确认;
- STOPPED 需停止反馈确认; 反馈缺失时进入 FAULT 并保留未知设备清单;
- 错误/取消不进入默认运动分支; 重抓拍淘汰旧依赖;
- 恢复需显式 reconcile, 不是简单改状态.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from application.contracts.enums import FailureClass, StopScope, TaskState


class IllegalTransition(RuntimeError):
    """非法状态迁移."""


# 允许的迁移表 (与 02 §3.2 状态图一致)
_ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.IDLE: {TaskState.VALIDATING},
    TaskState.VALIDATING: {TaskState.READY, TaskState.IDLE},
    TaskState.READY: {TaskState.RUNNING, TaskState.VALIDATING, TaskState.IDLE},
    TaskState.RUNNING: {
        TaskState.PAUSE_REQUESTED, TaskState.WAITING_OPERATOR,
        TaskState.COMPLETED, TaskState.STOPPING, TaskState.FAULT,
    },
    TaskState.PAUSE_REQUESTED: {TaskState.PAUSED, TaskState.STOPPING, TaskState.FAULT},
    TaskState.PAUSED: {TaskState.RUNNING, TaskState.STOPPING, TaskState.FAULT},
    TaskState.WAITING_OPERATOR: {TaskState.RUNNING, TaskState.STOPPING, TaskState.FAULT},
    TaskState.STOPPING: {TaskState.STOPPED, TaskState.FAULT},
    TaskState.STOPPED: {TaskState.VALIDATING, TaskState.IDLE},
    TaskState.FAULT: {TaskState.VALIDATING, TaskState.IDLE},
    TaskState.COMPLETED: {TaskState.IDLE, TaskState.VALIDATING},
}


@dataclass
class StateTransition:
    from_state: TaskState
    to_state: TaskState
    reason: str = ""
    failure_class: FailureClass | None = None


@dataclass
class TaskStateMachine:
    """单一任务状态权威. 外部状态只在控制执行单元迁移."""

    run_id: str = ""
    state: TaskState = TaskState.IDLE
    history: list[StateTransition] = field(default_factory=list)
    failure_class: FailureClass | None = None
    stop_scope: StopScope | None = None
    unknown_executors: list[str] = field(default_factory=list)

    # ---------- 基础迁移 ----------
    def _to(self, new: TaskState, reason: str = "",
            failure_class: FailureClass | None = None) -> TaskState:
        if new not in _ALLOWED.get(self.state, set()):
            raise IllegalTransition(
                f"不允许 {self.state.value} -> {new.value} ({reason})"
            )
        self.history.append(StateTransition(self.state, new, reason, failure_class))
        self.state = new
        if failure_class is not None:
            self.failure_class = failure_class
        return self.state

    # ---------- 业务动作 ----------
    def begin_validation(self, reason: str = "") -> TaskState:
        return self._to(TaskState.VALIDATING, reason)

    def mark_ready(self, reason: str = "校验通过") -> TaskState:
        return self._to(TaskState.READY, reason)

    def validation_failed(self, reason: str = "") -> TaskState:
        return self._to(TaskState.IDLE, reason or "校验失败")

    def start(self, reason: str = "") -> TaskState:
        if self.state is not TaskState.READY:
            raise IllegalTransition(f"仅 READY 可启动, 当前 {self.state.value}")
        return self._to(TaskState.RUNNING, reason)

    def request_pause(self, reason: str = "") -> TaskState:
        """请求暂停: 只进入 PAUSE_REQUESTED, 不立即 PAUSED."""
        if self.state is not TaskState.RUNNING:
            raise IllegalTransition(f"仅 RUNNING 可请求暂停, 当前 {self.state.value}")
        return self._to(TaskState.PAUSE_REQUESTED, reason)

    def confirm_pause(self, safe_boundary: bool, reason: str = "") -> TaskState:
        """安全边界确认后才 PAUSED."""
        if self.state is not TaskState.PAUSE_REQUESTED:
            raise IllegalTransition("仅 PAUSE_REQUESTED 可确认暂停")
        if not safe_boundary:
            raise IllegalTransition("安全边界未确认, 不能进入 PAUSED")
        return self._to(TaskState.PAUSED, reason or "到达安全边界")

    def resume(self, reconciled: bool, reason: str = "") -> TaskState:
        """恢复需显式 reconcile (重检状态/位姿/互锁)."""
        if self.state not in (TaskState.PAUSED, TaskState.WAITING_OPERATOR):
            raise IllegalTransition(f"当前 {self.state.value} 不能继续")
        if not reconciled:
            raise IllegalTransition("未通过 reconcile, 不能继续")
        return self._to(TaskState.RUNNING, reason or "复核通过")

    def single_step(self, reason: str = "") -> TaskState:
        if self.state not in (TaskState.READY, TaskState.PAUSED):
            raise IllegalTransition("单步仅在 READY/PAUSED 可用")
        return self._to(TaskState.RUNNING, reason or "单步")

    def request_operator(self, reason: str = "") -> TaskState:
        if self.state is not TaskState.RUNNING:
            raise IllegalTransition("仅 RUNNING 可进入 WAITING_OPERATOR")
        return self._to(TaskState.WAITING_OPERATOR, reason)

    def request_stop(self, scope: StopScope, reason: str = "") -> TaskState:
        if self.state not in (TaskState.RUNNING, TaskState.PAUSED,
                              TaskState.PAUSE_REQUESTED, TaskState.WAITING_OPERATOR):
            raise IllegalTransition(f"当前 {self.state.value} 不能请求停止")
        self.stop_scope = scope
        return self._to(TaskState.STOPPING, reason)

    def confirm_stopped(self, all_confirmed: bool, unknown_executors=(),
                        reason: str = "") -> TaskState:
        if self.state is not TaskState.STOPPING:
            raise IllegalTransition("仅 STOPPING 可确认停止")
        if all_confirmed:
            return self._to(TaskState.STOPPED, reason or "停止已确认")
        self.unknown_executors = list(unknown_executors)
        return self._to(TaskState.FAULT, reason or "停止未确认", FailureClass.DEVICE)

    def complete(self, reason: str = "") -> TaskState:
        if self.state is not TaskState.RUNNING:
            raise IllegalTransition("仅 RUNNING 可完成")
        return self._to(TaskState.COMPLETED, reason or "全部步骤完成")

    def fail(self, failure_class: FailureClass, reason: str = "") -> TaskState:
        if self.state is TaskState.COMPLETED:
            raise IllegalTransition("已完成任务不能失败")
        return self._to(TaskState.FAULT, reason, failure_class)

    # ---------- 查询 ----------
    def allows_button(self, name: str) -> bool:
        """按钮矩阵 (12 §5): 各状态下合法操作."""
        table = {
            TaskState.IDLE: {"select_recipe", "connect", "manual"},
            TaskState.VALIDATING: {"cancel_start"},
            TaskState.READY: {"start", "single_step"},
            TaskState.RUNNING: {"request_pause", "terminate", "stop_all"},
            TaskState.PAUSE_REQUESTED: {"stop_all"},
            TaskState.PAUSED: {"resume", "single_step", "terminate", "stop_all"},
            TaskState.WAITING_OPERATOR: {"operator_option", "stop_all"},
            TaskState.STOPPING: {"view_stop_progress"},
            TaskState.STOPPED: {"revalidate", "end_task"},
            TaskState.FAULT: {"fault_handling"},
            TaskState.COMPLETED: {"view_report", "new_task"},
        }
        return name in table.get(self.state, set())

    @property
    def is_active(self) -> bool:
        return self.state in (TaskState.RUNNING, TaskState.PAUSE_REQUESTED,
                              TaskState.PAUSED, TaskState.WAITING_OPERATOR,
                              TaskState.STOPPING)


__all__ = ["IllegalTransition", "StateTransition", "TaskStateMachine"]
