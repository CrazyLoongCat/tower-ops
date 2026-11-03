"""A14 任务状态机测试 (T04/T14/T16/T17/T19/T22 基础)."""
from __future__ import annotations

import pytest

from application.contracts.enums import FailureClass, StopScope, TaskState
from application.workflow.state import IllegalTransition, TaskStateMachine


def _sm_ready():
    sm = TaskStateMachine()
    sm.begin_validation()
    sm.mark_ready()
    return sm


def test_pause_is_not_instant():
    sm = _sm_ready()
    sm.start()
    sm.request_pause()
    assert sm.state is TaskState.PAUSE_REQUESTED  # 不是 PAUSED
    with pytest.raises(IllegalTransition):
        sm.confirm_pause(safe_boundary=False)  # 安全边界未确认
    sm.confirm_pause(safe_boundary=True)
    assert sm.state is TaskState.PAUSED


def test_cannot_jump_to_paused_directly():
    sm = _sm_ready()
    with pytest.raises(IllegalTransition):
        sm._to(TaskState.PAUSED)


def test_cannot_jump_to_stopped_directly():
    sm = _sm_ready()
    sm.start()
    with pytest.raises(IllegalTransition):
        sm._to(TaskState.STOPPED)


def test_stop_unknown_goes_fault():
    sm = _sm_ready()
    sm.start()
    sm.request_stop(StopScope.TASK_TERMINATE)
    sm.confirm_stopped(all_confirmed=False, unknown_executors=["eco65_01"])
    assert sm.state is TaskState.FAULT
    assert sm.unknown_executors == ["eco65_01"]


def test_stop_confirmed_goes_stopped():
    sm = _sm_ready()
    sm.start()
    sm.request_stop(StopScope.STOP_ALL)
    sm.confirm_stopped(all_confirmed=True)
    assert sm.state is TaskState.STOPPED


def test_resume_requires_reconcile():
    sm = _sm_ready()
    sm.start(); sm.request_pause(); sm.confirm_pause(True)
    with pytest.raises(IllegalTransition):
        sm.resume(reconciled=False)
    sm.resume(reconciled=True)
    assert sm.state is TaskState.RUNNING


def test_complete_then_new_task():
    sm = _sm_ready()
    sm.start(); sm.complete()
    assert sm.state is TaskState.COMPLETED
    sm.begin_validation()
    assert sm.state is TaskState.VALIDATING


def test_fail_records_class():
    sm = _sm_ready()
    sm.start()
    sm.fail(FailureClass.COMMUNICATION, "板卡失联")
    assert sm.state is TaskState.FAULT and sm.failure_class is FailureClass.COMMUNICATION


def test_button_matrix():
    sm = _sm_ready()
    assert sm.allows_button("start") and not sm.allows_button("request_pause")
    sm.start()
    assert sm.allows_button("request_pause") and not sm.allows_button("start")
    sm.request_pause()
    # PAUSE_REQUESTED 不提供继续/手动
    assert not sm.allows_button("resume") and not sm.allows_button("manual")
    assert sm.allows_button("stop_all")


def test_history_recorded():
    sm = _sm_ready()
    sm.start()
    states = [(t.from_state.value, t.to_state.value) for t in sm.history]
    assert ("IDLE", "VALIDATING") in states and ("READY", "RUNNING") in states


def test_terminate_not_allowed_when_idle():
    sm = TaskStateMachine()
    with pytest.raises(IllegalTransition):
        sm.request_stop(StopScope.STOP_ALL)
