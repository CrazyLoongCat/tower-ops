"""步骤完成确认闸 (DEC-042, 作业台人工确认)。

用户要求: **每个步骤执行结束后**弹框让操作者确认动作是否正确，点确认才继续。
本节点由编译器插在**每个步骤之后**（含子流程/循环展开的步骤），语义:

- 到达即进入等待（``pending_prompt`` 供作业台弹框），流程不再前进；
- 选项仅 ``确认`` / ``不正确``（受限选项，无自由文本）；
- ``不正确``: 节点**不返回 FAILURE**，而是标记 ``_rejected`` 并保持 RUNNING；
  ``TaskService.tick`` 检测到后把任务停在安全边界（PAUSED）。DEC-102 起，
  操作者处理后继续时**不再重问该已完成步骤**，直接放行到下一步；
- 两个选择都记录事件（含操作者），便于追溯。

注: 运行一次方案/条件步骤等也一视同仁地弹（用户确认「所有步骤都弹」）。
"""
from __future__ import annotations

from typing import Sequence

from py_trees.common import Status

from application.infrastructure.events import Event, Severity
from application.workflow.nodes.base import BaseStepNode, OutputStore

CONFIRM_OK = "确认"
CONFIRM_NOT_OK = "不正确"


class StepConfirmGate(BaseStepNode):
    """一步执行完后的确认闸: 等操作者点「确认」才放行。"""

    OPTIONS: tuple[str, ...] = (CONFIRM_OK, CONFIRM_NOT_OK)

    def __init__(self, name: str, *, target_step_id: str, title: str = "",
                 index: int = 0, total: int = 0, **kw) -> None:
        # 闸用**独立账本键**（不能与所确认步骤同键，否则会命中去重逻辑直接 SUCCESS）
        kw["step_id"] = f"{kw['step_id']}#confirm"
        super().__init__(name, **kw)
        self.target_step_id = target_step_id
        self.title = title or target_step_id
        self.index = int(index or 0)
        self.total = int(total or 0)
        self._prompt: dict | None = None
        self._choice: str | None = None
        self._operator: str | None = None
        self._rejected = False
        self._handled = False
        self.last_reject: str | None = None

    # ---- 供作业台调用 ----
    @property
    def pending_prompt(self) -> dict | None:
        """等待人工确认时的提示（title/message/options）；无则 None。"""
        p = self._prompt
        return dict(p) if p else None

    def provide(self, choice: str, operator: str = "operator") -> bool:
        """提交选择；仅接受本提示**已声明**的选项，且必须正在等待。"""
        p = self._prompt
        if p is None or choice not in p["options"]:
            self.last_reject = f"选项 {choice!r} 不在允许列表 {self.OPTIONS}"
            return False
        self._choice = choice
        self._operator = operator
        self._prompt = None
        self.last_reject = None
        return True

    def consume_rejection(self) -> dict | None:
        """TaskService 用: 取出「不正确」并标记已处理（只取一次）。"""
        if self._rejected and not self._handled:
            self._handled = True
            return {"step_id": self.target_step_id, "title": self.title,
                    "operator": self._operator}
        return None

    # ---- 生命周期 ----
    def initialise(self) -> None:
        super().initialise()
        self._choice = None
        if not self._rejected:
            self._operator = None
        self._handled = False
        self.last_reject = None
        # DEC-102：「不正确」已经让任务停过一次；停止/暂停后继续时**不再重问**
        # 这个已完成的步骤，直接把闸放行到下一步。保留 _rejected 让下一次
        # _tick 标记通过（新 run 会重新编译出新的闸对象，不受影响）。
        if self._rejected:
            self._prompt = None
            return
        # DEC-061 故障恢复：整树复位重跑时，**已完成并确认过**的步骤闸应直接跳过，
        # 不再给操作员弹一遍旧步骤的确认框（账本里已有 terminal 结果）。
        ledger = getattr(self.executor, "ledger", None)
        entry = ledger.entry(self.key) if ledger is not None else None
        if entry is not None and entry.terminal and entry.result is not None:
            self._prompt = None
            return
        self._arm()

    def _on_terminate(self) -> None:
        self._prompt = None

    def _arm(self) -> None:
        seq = (f"第 {self.index}/{self.total} 步" if self.total else "当前步骤")
        self._prompt = {
            "kind": "step_confirm",
            "title": f"步骤完成确认 · {self.title}",
            "message": (f"{seq}「{self.title}」已执行完成。请确认动作是否正确；"
                        f"点「确认」后继续，点「不正确」将暂停作业。"),
            "options": list(self.OPTIONS),
            "step_id": self.target_step_id,
            "step_title": self.title,
            "index": self.index, "total": self.total,
        }

    def _tick(self, entry) -> Status:
        if self._choice is None:
            if self._rejected:
                # DEC-102：操作员已判定「不正确」并让任务停过；继续时直接放行
                # 到下一步（第三步完成、第四步未执行 → 下一步从第四步开始），
                # 不重复询问已完成的这一步。
                self._rejected = False
                self._handled = True
                return self._mark_success({
                    "step_confirm": CONFIRM_NOT_OK,
                    "operator": self._operator,
                    "target_step_id": self.target_step_id,
                })
            return Status.RUNNING
        choice = self._choice
        self._choice = None
        self._record(choice)
        if choice == CONFIRM_NOT_OK:
            # 不报 FAILURE: 由 TaskService 在安全边界暂停, 避免把人工判定记成故障
            self._rejected = True
            self._handled = False
            return Status.RUNNING
        return self._mark_success({"step_confirm": CONFIRM_OK,
                                   "operator": self._operator,
                                   "target_step_id": self.target_step_id})

    def _record(self, choice: str) -> None:
        records = getattr(self.executor, "records", None)
        if records is None:
            return
        if choice == CONFIRM_NOT_OK:
            event_type = "step.confirm.rejected"
            message = f"步骤「{self.title}」判定不正确，暂停作业"
            severity = Severity.WARNING
        else:
            event_type = "step.confirm"
            message = f"步骤「{self.title}」确认正确，继续"
            severity = Severity.INFO
        try:
            records.append_event_nonblocking(Event(
                event_type=event_type, severity=severity, message=message,
                run_id=self.ctx.run_id,
                station_instance_id=self.ctx.station_instance_id,
                step_id=self.target_step_id,
                advice=f"operator={self._operator}",
            ))
        except Exception:  # noqa: BLE001 - 留证失败不影响流程
            pass


def make_step_gate(step, *, ctx, executor, outputs: OutputStore,
                   index: int = 0, total: int = 0,
                   gates: list | None = None) -> StepConfirmGate:
    """按步骤构造确认闸（名称/显示名/账本键都从该步骤派生）。"""
    title = _step_title(step)
    gate = StepConfirmGate(
        f"gate:{step.id}", target_step_id=step.id, title=title,
        index=index, total=total,
        ctx=ctx, executor=executor, outputs=outputs, step_id=step.id,
    )
    if gates is not None:
        gates.append(gate)
    return gate


def _step_title(step) -> str:
    try:
        from application.services.recipes.editor import step_title
        return step_title(step)
    except Exception:  # noqa: BLE001 - 显示名不可得时退回 id, 不影响确认
        return getattr(step, "title", None) or getattr(step, "id", "")


__all__ = [
    "CONFIRM_NOT_OK", "CONFIRM_OK", "StepConfirmGate", "make_step_gate",
]
