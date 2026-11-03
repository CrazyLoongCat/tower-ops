"""operator.confirm 节点 (E13, 02 §9.1)。

等待人工确认; 选项受限; **留证**(记录操作者与选择); 不绕过硬互锁
(服务层仍做互锁/权限校验, 本节点只控制流程推进)。
缺证据时不允许确认, 避免"点一下就继续"。
"""
from __future__ import annotations

from typing import Any, Sequence

from py_trees.common import Status

from application.infrastructure.events import Event, Severity
from application.workflow.nodes.base import BaseStepNode


class OperatorConfirmNode(BaseStepNode):
    """人工确认步骤: 需操作者选择**已声明选项**之一, 并满足所需的证据引用。"""

    def __init__(self, name: str, *, prompt: str,
                 options: Sequence[str] = ("继续", "终止"),
                 evidence_refs: Sequence[str] = (), **kw) -> None:
        super().__init__(name, **kw)
        self.prompt = prompt
        self.options = tuple(options)
        self.evidence_refs = tuple(evidence_refs)
        self.choice: str | None = None
        self.operator: str | None = None
        self.last_reject: str | None = None

    # ---- 供 UI 调用 ----
    def provide(self, choice: str, operator: str = "operator") -> bool:
        """提交选择. 仅接受已声明选项; 未满足证据前置则拒绝。"""
        if choice not in self.options:
            self.last_reject = f"选项 {choice!r} 不在允许列表 {self.options}"
            return False
        missing = self._missing_evidence()
        if missing:
            self.last_reject = f"缺证据: {missing}"
            return False
        self.choice = choice
        self.operator = operator
        self.last_reject = None
        return True

    def _missing_evidence(self) -> list[str]:
        si = self.ctx.station_instance_id or ""
        missing = []
        for ref in self.evidence_refs:
            if not self.outputs.step_outputs(si, ref):
                missing.append(ref)
        return missing

    # ---- 生命周期 ----
    def initialise(self) -> None:
        super().initialise()
        self.choice = None
        self.last_reject = None

    def _tick(self, entry) -> Status:
        if self.choice is None:
            return Status.RUNNING
        si = self.ctx.station_instance_id or ""
        self.outputs.set(si, self.step_id, "operator_choice", self.choice)
        self.outputs.set(si, self.step_id, "operator", self.operator)
        if self.executor.records is not None:
            try:
                self.executor.records.append_event_nonblocking(Event(
                    event_type="operator.confirm", severity=Severity.INFO,
                    message=f"{self.prompt} -> {self.choice}",
                    run_id=self.ctx.run_id,
                    station_instance_id=self.ctx.station_instance_id,
                    step_id=self.step_id, advice=f"operator={self.operator}",
                ))
            except Exception:  # noqa: BLE001 - 留证失败不影响流程, 但已尝试记录
                pass
        return self._mark_success({"operator_choice": self.choice,
                                   "operator": self.operator})


__all__ = ["OperatorConfirmNode"]
