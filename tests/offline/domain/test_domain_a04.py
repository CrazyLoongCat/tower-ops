"""A04 领域资源/互锁/时效测试 (T06/T15/T31 基础)."""
from __future__ import annotations

import time

import pytest

from application.contracts.enums import SourceKind, TelemetryFreshness
from application.domain.actions import (
    ActionCatalog,
    ActionDefinition,
    ParamSpec,
    Precondition,
    PreconditionKind,
)
from application.domain.errors import (
    InterlockDenied,
    ResourceQuarantined,
    StaleState,
    UnknownAction,
    ValidationFailure,
)
from application.domain.interlocks import InterlockService
from application.domain.resources import (
    CAPTURE_STILLNESS,
    MOTION_GLOBAL,
    ResourceManager,
)
from application.domain.telemetry import StateStore, TelemetryRecord


def _catalog():
    return ActionCatalog([
        ActionDefinition(
            action_id="clamp", device_id="stm32_01",
            resource_ids=(MOTION_GLOBAL,), timeout_ms=3000,
        ),
        ActionDefinition(
            action_id="release", device_id="stm32_01",
            resource_ids=(MOTION_GLOBAL,), opposes=("clamp",),
        ),
        ActionDefinition(
            action_id="move_joint", device_id="eco65_01",
            resource_ids=(MOTION_GLOBAL,),
            parameters=(ParamSpec(name="speed_pct", minimum=1, maximum=50),),
            preconditions=(
                Precondition(kind=PreconditionKind.TELEMETRY_FRESH,
                             device_id="eco65_01", max_age_ms=500),
            ),
        ),
        ActionDefinition(
            action_id="capture_ds86", device_id="ds86_01",
            # 抓拍占有静止保护, 同时占 motion_global 冻结运动 (02 §5.3)
            resource_ids=(MOTION_GLOBAL, CAPTURE_STILLNESS),
        ),
    ])


def _svc():
    res = ResourceManager()
    st = StateStore()
    cat = _catalog()
    return InterlockService(res, st, cat), res, st, cat


def test_unknown_action_rejected():
    svc, *_ = _svc()
    with pytest.raises(UnknownAction):
        svc.authorize("nope", {}, holder="h", source=SourceKind.WORKFLOW)


def test_param_limits_enforced():
    svc, *_ = _svc()
    with pytest.raises(ValidationFailure):
        svc.authorize("move_joint", {"speed_pct": 999}, holder="h",
                      source=SourceKind.MANUAL)


def test_opposite_action_mutual_exclusion():
    svc, res, *_ = _svc()
    lease = svc.authorize("clamp", {}, holder="taskA", source=SourceKind.WORKFLOW)
    # release 与 clamp 相反, 且共用 motion_global -> 拒绝
    with pytest.raises(InterlockDenied):
        svc.authorize("release", {}, holder="taskB", source=SourceKind.WORKFLOW)
    svc.release(lease)
    svc.authorize("release", {}, holder="taskB", source=SourceKind.WORKFLOW)


def test_manual_does_not_bypass_interlock():
    svc, *_ = _svc()
    svc.authorize("clamp", {}, holder="auto_task", source=SourceKind.WORKFLOW)
    # 手动来源同样被拒 (02 §2)
    with pytest.raises(InterlockDenied):
        svc.authorize("clamp", {}, holder="manual", source=SourceKind.MANUAL)


def test_capture_blocks_motion_and_vice_versa():
    svc, res, st, cat = _svc()
    # 机器人状态新鲜, 使拒绝原因唯一地是资源互锁而非时效
    st.update(TelemetryRecord(device_id="eco65_01", values={"state": "READY"},
                              validity=TelemetryFreshness.VALID))
    cap = svc.authorize("capture_ds86", {}, holder="taskA",
                        source=SourceKind.WORKFLOW)
    # 抓拍期间移动被拒绝
    with pytest.raises(InterlockDenied):
        svc.authorize("move_joint", {"speed_pct": 10}, holder="manual",
                      source=SourceKind.MANUAL)
    svc.release(cap)


def test_stale_state_rejected_as_precondition():
    svc, res, st, cat = _svc()
    st.set_max_age("eco65_01", 100.0)
    st.update(TelemetryRecord(
        device_id="eco65_01", values={"state": "READY"},
        validity=TelemetryFreshness.STALE,
    ))
    with pytest.raises(StaleState):
        svc.authorize("move_joint", {"speed_pct": 10}, holder="h",
                      source=SourceKind.WORKFLOW)


def test_fresh_state_allows():
    svc, res, st, cat = _svc()
    st.set_max_age("eco65_01", 500.0)
    st.update(TelemetryRecord(
        device_id="eco65_01", values={"state": "READY"},
        validity=TelemetryFreshness.VALID,
    ))
    lease = svc.authorize("move_joint", {"speed_pct": 10}, holder="h",
                          source=SourceKind.WORKFLOW)
    assert lease.is_active


def test_unknown_resource_quarantined_not_reallocated():
    svc, res, *_ = _svc()
    lease = svc.authorize("clamp", {}, holder="taskA", source=SourceKind.WORKFLOW)
    svc.quarantine(lease, "DONE 未确认, 状态 UNKNOWN")
    with pytest.raises(ResourceQuarantined):
        svc.authorize("clamp", {}, holder="taskB", source=SourceKind.WORKFLOW)
    assert MOTION_GLOBAL in res.quarantined()
    # 人工核对后解除
    assert res.clear_quarantine(MOTION_GLOBAL, "operator_checked_off")
    svc.authorize("clamp", {}, holder="taskB", source=SourceKind.WORKFLOW)


def test_atomic_acquire_no_partial():
    svc, res, *_ = _svc()
    res.acquire((CAPTURE_STILLNESS,), holder="other", source=SourceKind.MANUAL)
    with pytest.raises(InterlockDenied):
        svc.authorize("capture_ds86", {}, holder="taskA",
                      source=SourceKind.WORKFLOW)
    # 原子性: motion_global 不应被部分占用
    assert res.is_available(MOTION_GLOBAL)


def test_state_check_completion_fields():
    svc, res, st, cat = _svc()
    st.set_max_age("stm32_01", 500.0)
    st.update(TelemetryRecord(
        device_id="stm32_01", values={"clamped": True},
        validity=TelemetryFreshness.VALID,
    ))
    svc.check_state_checks("stm32_01", (("clamped", "eq", True),))
    with pytest.raises(ValidationFailure):
        svc.check_state_checks("stm32_01", (("clamped", "eq", False),))


def test_telemetry_record_age():
    rec = TelemetryRecord(
        device_id="d", values={}, validity=TelemetryFreshness.VALID,
        received_monotonic_s=time.monotonic() - 2.0,
    )
    assert rec.age_ms() >= 2000.0
    assert not rec.is_fresh(500.0)
    assert rec.is_fresh(5000.0)
