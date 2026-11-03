"""StopCoordinator: 冻结调度、清待发、独立发停止、逐设备核验新鲜实际状态 (12 §4.2/§4.3, 02 §5.3, A15).

规则:
- 未知不全绿; BT 失败/退出不当物理停稳; 停止不被图像/DB 阻塞;
- scope 区分任务终止与停止全部; 重复请求去重;
- 不宣称主进程已崩时仍能自发停止 (硬件 watchdog/急停另验).
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Callable

from application.contracts.enums import StopScope, StopState
from application.contracts.handles import StopSummary


@dataclass
class StopTarget:
    """一个执行器的停止能力. request_stop 独立发出; verify_stopped 返回新鲜实际状态."""

    executor_id: str
    request_stop: Callable[[], None]
    verify_stopped: Callable[[], StopState]
    task_scoped: bool = True


@dataclass
class StopCoordinator:
    freeze_scheduler: Callable[[], None] = lambda: None
    clear_pending: Callable[[], None] = lambda: None
    _targets: list[StopTarget] = field(default_factory=list)
    _active: dict[str, StopSummary] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def register(self, target: StopTarget) -> None:
        self._targets.append(target)

    def targets(self, scope: StopScope) -> list[StopTarget]:
        if scope is StopScope.STOP_ALL:
            return list(self._targets)
        return [t for t in self._targets if t.task_scoped]

    def request_stop(self, scope: StopScope, reason: str,
                     request_id: str | None = None) -> StopSummary:
        rid = request_id or f"stop-{next(self._ids)}"
        # 去重: 同 request_id 已完成则直接返回
        if rid in self._active and self._active[rid].all_confirmed:
            return self._active[rid]

        summary = StopSummary(scope=scope, reason=reason, request_id=rid)
        self._active[rid] = summary

        # 1) 冻结调度 + 2) 清待发动作 (不等待图像/DB)
        try:
            self.freeze_scheduler()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.clear_pending()
        except Exception:  # noqa: BLE001
            pass

        # 3) 逐执行器独立发停止 + 4) 核验新鲜状态
        for target in self.targets(scope):
            rep = summary.report(target.executor_id)
            rep.state = StopState.STOPPING
            try:
                target.request_stop()
            except Exception as exc:  # noqa: BLE001
                rep.state = StopState.FAILED
                rep.detail = f"停止请求失败: {exc}"
                continue
            try:
                state = target.verify_stopped()
            except Exception as exc:  # noqa: BLE001
                rep.state = StopState.UNKNOWN
                rep.detail = f"停止核验失败: {exc}"
                continue
            rep.state = state
            if state is StopState.CONFIRMED:
                rep.confirmed_monotonic_s = time.monotonic()
            elif state is StopState.UNKNOWN:
                rep.detail = rep.detail or "未获新鲜停止证据"
        return summary

    def get_stop_summary(self, request_id: str) -> StopSummary | None:
        return self._active.get(request_id)


__all__ = ["StopCoordinator", "StopTarget"]
