"""互锁服务 (12 §4.2, 02 §2/§9.2).

手动窗口和流程步骤调用同一 execute_action, 都携带 source/操作者/上下文;
互锁服务不因来源是 manual 而跳过硬条件. 相反动作/自动手动竞争/陈旧状态/
抓拍期间移动被拒绝; 未知资源不能再分配.
"""
from __future__ import annotations

import operator
from typing import Any

from application.contracts.enums import SourceKind
from application.domain.actions import (
    ActionCatalog,
    ActionDefinition,
    PreconditionKind,
)
from application.domain.errors import InterlockDenied, StaleState, ValidationFailure
from application.domain.resources import ResourceLease, ResourceManager
from application.domain.telemetry import StateStore

_OPS = {
    "eq": operator.eq, "ne": operator.ne,
    "gt": operator.gt, "ge": operator.ge,
    "lt": operator.lt, "le": operator.le,
}


class InterlockService:
    """前置条件 + 资源租约 + 相反动作的统一校验入口."""

    def __init__(
        self,
        resources: ResourceManager,
        state_store: StateStore,
        catalog: ActionCatalog,
    ) -> None:
        self._resources = resources
        self._state = state_store
        self._catalog = catalog

    # ---------- 只读检查 ----------
    def check_preconditions(
        self, action: ActionDefinition, now_monotonic_s: float | None = None
    ) -> None:
        for pre in action.preconditions:
            if pre.kind is PreconditionKind.TELEMETRY_FRESH:
                assert pre.device_id is not None
                self._state.require_fresh(pre.device_id, now_monotonic_s)
            elif pre.kind is PreconditionKind.RESOURCE_FREE:
                assert pre.resource_id is not None
                if not self._resources.is_available(pre.resource_id):
                    holder = self._resources.holder_of(pre.resource_id) or "unknown"
                    raise InterlockDenied(pre.resource_id, holder, "前置: 资源须空闲")
            elif pre.kind is PreconditionKind.STATE_EQ:
                assert pre.device_id is not None and pre.field_name is not None
                rec = self._state.require_fresh(pre.device_id, now_monotonic_s)
                actual = rec.values.get(pre.field_name)
                if actual != pre.value:
                    raise ValidationFailure(
                        f"设备 {pre.device_id!r}.{pre.field_name} 期望 {pre.value!r}, "
                        f"实际 {actual!r}"
                    )

    def check_state_checks(
        self,
        device_id: str,
        checks: tuple[tuple[str, str, Any], ...],
        now_monotonic_s: float | None = None,
    ) -> None:
        """完成证据中的终态字段检查 (DONE 只声明固件完成, 互锁仍核验真实新状态)."""
        rec = self._state.require_fresh(device_id, now_monotonic_s)
        for field_name, op, expected in checks:
            fn = _OPS.get(op)
            if fn is None:
                raise ValidationFailure(f"未知比较算子: {op!r}")
            actual = rec.values.get(field_name)
            if not fn(actual, expected):
                raise ValidationFailure(
                    f"完成终态不满足: {field_name} {op} {expected!r}, 实际 {actual!r}"
                )

    # ---------- 获取授权 ----------
    def action_definition(self, action_id: str) -> ActionDefinition:
        return self._catalog.get(action_id)

    def acquire_resources(self, resources, holder: str, source: SourceKind,
                          reason: str = "") -> ResourceLease:
        """按来源获取一组具名资源 (供动作/抓拍服务复用同一互锁硬条件)."""
        return self._resources.acquire(tuple(resources), holder=holder,
                                       source=source, reason=reason)

    def authorize(
        self,
        action_id: str,
        params: dict[str, Any],
        holder: str,
        source: SourceKind,
        now_monotonic_s: float | None = None,
    ) -> ResourceLease:
        """校验参数/前置/相反动作并原子获取资源. 任一不满足即拒绝 (来源不绕过)."""
        action = self._catalog.get(action_id)
        action.validate_parameters(params)
        self.check_preconditions(action, now_monotonic_s)
        if action.opposes:
            self._resources.check_opposes(action_id, action.opposes, holder)
        return self._resources.acquire(
            action.resource_ids, holder=holder, source=source,
            reason=f"action:{action_id}",
        )

    # ---------- 停止/隔离 ----------
    def quarantine(self, lease: ResourceLease, reason: str) -> None:
        self._resources.quarantine(lease, reason)

    def release(self, lease: ResourceLease) -> None:
        self._resources.release(lease)

    # ---------- 人工核对（故障恢复） ----------
    def quarantined(self) -> dict[str, str]:
        """当前被隔离的资源 ``{resource_id: reason}``（只读快照）。"""
        return self._resources.quarantined()

    def clear_quarantine(self, resource_id: str, operator_note: str) -> bool:
        """人工核对后解除某个资源的隔离（需显式说明）。返回是否确有其事。"""
        return self._resources.clear_quarantine(resource_id, operator_note)


__all__ = ["InterlockService"]
