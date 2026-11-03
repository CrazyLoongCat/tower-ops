"""DEC-061 故障恢复：FAULT 后从失败步骤原地重试，或重新执行整个任务。

用离线栈 + 注入抓拍超时制造真实 FAULT；验证：
- 只重试失败步骤（换 attempt），已完成步骤不重放；
- 同一 run 恢复（不换 run_id），完成后回到 COMPLETED；
- DEC-104：不再要求人工说明/勾选，选择恢复即自动清 UNKNOWN/隔离；
- 「重新执行整个任务」从 FAULT 起新 run，从头跑；
- 作业台按钮矩阵/故障信息。
"""
from __future__ import annotations

import pytest

from application.bootstrap.composition import build_offline_stack
from application.contracts.enums import TaskState
from application.presentation.viewmodel import WorkbenchViewModel
from application.services.task import TaskError

RECIPE_PATH = "configs/recipes/ds86_two_station_example.yaml"
RID = "fault_recovery_case"


def _stack(tmp_path):
    stack = build_offline_stack(
        db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
        recipe_path=RECIPE_PATH, capture_timeout=True)
    stack.recipes.draft({
        "recipe_id": RID, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "mech", "type": "mechanism.action",
             "action_ref": "approved_single_board_action"},
            {"id": "cap", "type": "camera.capture", "camera_ref": "ds86_01"},
            {"id": "ev", "type": "record.evidence", "inputs": ["mech", "cap"]},
        ],
    })
    return stack


def _run_to_end(stack, limit=200) -> TaskState:
    st = None
    for _ in range(limit):
        st = stack.task.tick()
        if st in (TaskState.COMPLETED, TaskState.FAULT,
                  TaskState.WAITING_OPERATOR, TaskState.STOPPED):
            return st
    return st


def _to_fault(stack) -> None:
    stack.task.start(RID, allow_draft=True)
    assert _run_to_end(stack) is TaskState.FAULT
    node = stack.task.compiled.find_failure()
    assert node is not None and node.step_id == "cap"


def test_resume_from_failed_step_same_run_without_replaying_completed_steps(tmp_path):
    stack = _stack(tmp_path)
    _to_fault(stack)
    run_id = stack.task.state_machine.run_id
    node = stack.task.compiled.find_failure()
    submissions = stack.executor.ledger.submissions
    assert submissions == 1                      # 只有机制步骤提交过

    stack.cameras["ds86_01"].faults.timeout = False   # 模拟人工处理后故障消除
    st = stack.task.resume_from_fault()
    assert st is TaskState.RUNNING
    assert node.attempt == 2
    assert stack.task.state_machine.run_id == run_id  # 同一 run

    assert _run_to_end(stack) is TaskState.COMPLETED
    # 机制步骤没有被重放（账本 terminal 结果命中），只补跑了失败步骤
    assert stack.executor.ledger.submissions == submissions
    stack.close()


def test_resume_clears_unknown_without_note(tmp_path):
    """DEC-104：不再要求人工说明；选择恢复即自动清 UNKNOWN 并继续。"""
    stack = _stack(tmp_path)
    _to_fault(stack)
    node = stack.task.compiled.find_failure()
    stack.task.ledger.mark_unknown(node.key)          # 模拟未核实执行

    stack.cameras["ds86_01"].faults.timeout = False
    st = stack.task.resume_from_fault()
    assert st is TaskState.RUNNING
    assert stack.task.ledger.unknown_entries() == []
    assert _run_to_end(stack) is TaskState.COMPLETED
    stack.close()


def test_restart_whole_task_from_fault_creates_new_run(tmp_path):
    stack = _stack(tmp_path)
    _to_fault(stack)
    old_run = stack.task.state_machine.run_id
    stack.cameras["ds86_01"].faults.timeout = False

    new_run = stack.task.start(RID, allow_draft=True, recovery=True)
    assert new_run != old_run
    assert stack.task.state_machine.state is TaskState.RUNNING
    assert _run_to_end(stack) is TaskState.COMPLETED
    stack.close()


def test_recovery_is_refused_outside_fault(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start(RID, allow_draft=True)
    with pytest.raises(TaskError):
        stack.task.resume_from_fault()
    stack.close()


def test_resume_does_not_reask_already_confirmed_step_gates(tmp_path):
    """DEC-061：整树复位重跑时，已完成并确认过的步骤闸直接跳过，不重复弹框。"""
    stack = _stack(tmp_path)
    stack.task.start(RID, allow_draft=True, step_confirm=True)
    gates = {g.target_step_id: g for g in stack.task.compiled.gates}
    mech_gate = gates["mech"]

    # 机制步完成 -> 确认闸；确认后继续，抓拍步失败。
    for _ in range(20):
        st = stack.task.tick()
        if mech_gate.pending_prompt is not None:
            break
        assert st not in (TaskState.FAULT, TaskState.COMPLETED)
    assert mech_gate.pending_prompt is not None
    assert mech_gate.provide("确认", "测试员")
    assert _run_to_end(stack) is TaskState.FAULT

    stack.cameras["ds86_01"].faults.timeout = False
    stack.task.resume_from_fault()
    stack.task.tick()                  # 复位后第一次 tick：旧确认闸不得再弹
    assert mech_gate.pending_prompt is None
    assert not stack.task.compiled.find_step("mech").status.name == "RUNNING"

    # 失败步骤重试成功 -> 它的确认闸才出现；确认后完成剩余步骤。
    cap_gate = gates["cap"]
    for _ in range(30):
        st = stack.task.tick()
        if cap_gate.pending_prompt is not None:
            break
    assert cap_gate.pending_prompt is not None
    cap_gate.provide("确认", "测试员")
    ev_gate = gates["ev"]
    for _ in range(30):
        st = stack.task.tick()
        if ev_gate.pending_prompt is not None:
            break
    if ev_gate.pending_prompt is not None:
        ev_gate.provide("确认", "测试员")
    assert _run_to_end(stack) is TaskState.COMPLETED
    stack.close()


def test_restart_without_note_is_refused_when_unknown_exists(tmp_path):
    """不允许绕过故障恢复：有 UNKNOWN 时直接「启动」必须被拒绝。"""
    stack = _stack(tmp_path)
    _to_fault(stack)
    node = stack.task.compiled.find_failure()
    stack.task.ledger.mark_unknown(node.key)
    with pytest.raises(TaskError):
        stack.task.start(RID, allow_draft=True)
    assert stack.task.state_machine.state is TaskState.FAULT
    stack.close()


def test_workbench_buttons_and_fault_info(tmp_path):
    stack = _stack(tmp_path)
    _to_fault(stack)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    vm.select_recipe(RID)
    assert vm.buttons()["fault_recover"] is True
    info = vm.fault_info()
    assert info["step_id"] == "cap"
    assert info["step_title"] == "抓拍"          # DEC-105：中文步骤名
    assert info["retryable"] is True
    assert info["run_id"] == stack.task.state_machine.run_id
    assert info["reason"]
    stack.close()
