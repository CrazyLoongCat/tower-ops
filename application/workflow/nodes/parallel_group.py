"""并行执行步骤组（DEC-096）。

一个 ``parallel.group`` 顶层步骤 = 一个 ``py_trees`` Parallel（SuccessOnAll）：
两个分支在同一 tick 内各自提交动作，互不等待；**两个分支都成功**整组才完成，
随后仍走本步骤的判断属性与人工确认闸（整组只弹一次确认）。

首版组合固定为一个 ``robot.move_joint`` + 一个 ``mechanism.action``
（发布校验保证）。失败语义：

- 任一分失败 -> 整组 FAILURE，另一分支**立即取消**（节点层面 stop）；
- 若被取消的是运动分支且机器人仍在动，先停运动方案线程（有则 ``motion.stop``），
  再向 ``executor.robot`` 发停止请求（``stop`` 优先，退回 ``request_stop``），
  并在失败原因里写明「停稳需现场核实」，不假装已停稳；被取消分支的在途租约
  按「停稳未核实」转隔离；
- 组内失败不沿用分支的「暂停复核」策略——半组已取消，原地继续不安全，
  统一按故障处理（TaskService 转 FAULT，操作员可停止全部/故障恢复）；
- 故障恢复「从失败步骤恢复」重试整组：成功分支靠账本 terminal 结果跳过，
  重放失败分支与**被取消分支**（后者可能停在半途，必须重新走到目标）。
"""
from __future__ import annotations

from typing import Any

import py_trees
from py_trees.common import Status

# 运动分支的节点类名：失败取消兄弟分支时，只有它还 RUNNING 才需要向机械臂发停止。
_MOTION_NODE_NAMES = ("MoveJointNode", "MovePlanNode")


class ParallelGroupNode(py_trees.composites.Parallel):
    """并行组节点：对外暴露 step_id/failure_reason，参与失败定位与故障恢复。"""

    def __init__(self, name: str, *, step_id: str, title: str,
                 executor: Any, children: list[Any], run_id: str = "",
                 motion: Any = None) -> None:
        super().__init__(name=name,
                         policy=py_trees.common.ParallelPolicy.SuccessOnAll())
        self.step_id = step_id
        self.title = title or step_id
        self.executor = executor
        self.motion = motion
        self.run_id = run_id
        self.failure_reason: str | None = None
        self.failure_policy = None       # 组内失败统一按故障处理（见模块说明）
        self._failed_branch: Any | None = None
        self._cancelled_branches: list[Any] = []
        self.children = list(children)

    # ---- py_trees 生命周期 ----
    def initialise(self) -> None:
        super().initialise()
        self.failure_reason = None
        self.failure_policy = None
        self._failed_branch = None
        self._cancelled_branches = []

    def update(self) -> Status:
        status = super().update()
        if status is Status.FAILURE and self.failure_reason is None:
            self._on_group_failure()
        return status

    # ---- 失败处理 ----
    def _on_group_failure(self) -> None:
        failed = [c for c in self.children if c.status is Status.FAILURE]
        reasons = []
        for child in failed:
            reason = getattr(child, "failure_reason", "") or "分支失败"
            reasons.append(f"{self._branch_label(child)}：{reason}")
        self._failed_branch = failed[0] if failed else None

        running = [c for c in self.children if c.status is Status.RUNNING]
        stopped = []
        for child in running:
            try:
                child.stop(Status.INVALID)
                stopped.append(self._branch_label(child))
                self._cancelled_branches.append(child)
            except Exception:  # noqa: BLE001 - 取消失败不掩盖原始失败
                pass

        detail = "；".join(reasons) or "分支失败"
        if stopped:
            detail += f"；已取消仍在执行的分支：{'、'.join(stopped)}"
            motion_running = any(type(c).__name__ in _MOTION_NODE_NAMES
                                 for c in running)
            if motion_running:
                detail += f"；{self._halt_motion()}"
            detail += f"；{self._quarantine_leases()}"
        self.failure_reason = f"并行执行「{self.title}」失败：{detail}"

    def _quarantine_leases(self) -> str:
        """被取消分支的在途租约按「停稳未核实」转隔离（不假装已停稳）。

        不结算的话，下一次「从失败步骤恢复」重新申请 motion_global 会被旧租约拒绝；
        结算成隔离后，故障恢复必须填写人工核对说明才能继续（TaskService 既有规则）。
        """
        fn = getattr(self.executor, "settle_run_leases", None)
        if not callable(fn) or not self.run_id:
            return "在途租约需人工核对"
        try:
            holders = fn(self.run_id, stop_confirmed=False)
        except Exception as exc:  # noqa: BLE001
            return f"在途租约隔离失败：{exc}（需人工核对）"
        if not holders:
            return "在途租约已结算"
        return f"{len(holders)} 个在途租约已隔离，需人工核对"

    def _halt_motion(self) -> str:
        """停止运动方案 + 向机器人发停止请求（尽力而为），返回给失败原因用的说明。"""
        notes: list[str] = []
        motion_stop = getattr(self.motion, "stop", None)
        if callable(motion_stop):
            try:
                motion_stop()
                notes.append("已停止运动方案")
            except Exception as exc:  # noqa: BLE001 - 停止失败必须如实写明
                notes.append(f"运动方案停止失败：{exc}")
        robot = getattr(self.executor, "robot", None)
        fn = getattr(robot, "stop", None)
        if not callable(fn):
            fn = getattr(robot, "request_stop", None)
        if callable(fn):
            try:
                fn()
                notes.append("已请求机械臂停止（停稳需现场核实）")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"机械臂停止请求失败：{exc}（停稳需现场核实）")
        else:
            notes.append("机械臂无停止接口，停稳需现场核实")
        return "；".join(notes)

    @staticmethod
    def _branch_label(node: Any) -> str:
        sid = getattr(node, "step_id", None) or getattr(node, "name", "分支")
        return str(sid)

    # ---- 故障恢复 ----
    def arm_retry(self, reason: str = "") -> int:
        """重试失败分支与**被取消分支**；已成功分支靠旧账本 terminal 结果跳过。

        被取消的运动分支可能停在半途，恢复时必须重新执行到目标；只重放失败分支
        会把「半途停下的机器人」留在原地。成功分支的账本已 terminal，不会重放动作。
        """
        targets = []
        if self._failed_branch is not None:
            targets.append(self._failed_branch)
        targets.extend(b for b in self._cancelled_branches
                       if b is not self._failed_branch)
        retryable = [t for t in targets if callable(getattr(t, "arm_retry", None))]
        if not retryable:
            raise RuntimeError("并行组内找不到可重试的失败分支")
        attempts = [t.arm_retry(reason) for t in retryable]
        self.failure_reason = None
        self.failure_policy = None
        self._cancelled_branches = []
        return attempts[0]


__all__ = ["ParallelGroupNode"]
