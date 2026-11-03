"""A16 py_trees 编译/账本/整配方执行测试 (T15/T16/T17/T27/T28)."""
from __future__ import annotations

import pytest

from application.algorithms.circle.detector import CircleDecisionConfig
from application.bootstrap.composition import build_offline_stack
from application.contracts.enums import FailureClass, StopScope, TaskState
from application.workflow.ledger import ExecutionLedger
from application.contracts.context import ExecutionKey

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


def _stack(tmp_path, **kw):
    return build_offline_stack(
        db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
        recipe_path=RECIPE, **kw)


def _run_to_end(stack, limit=500):
    st = None
    for _ in range(limit):
        st = stack.task.tick()
        if st in (TaskState.COMPLETED, TaskState.FAULT, TaskState.WAITING_OPERATOR):
            return st
    return st


def test_recipe_completes_in_a_single_serial_pass(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    assert _run_to_end(stack) is TaskState.COMPLETED
    # 无工位后整份配方只跑一遍: 1 movej + 1 机构 = 2 次提交
    assert stack.executor.ledger.submissions == 2
    assert stack.records.pending_unknown_commands() == []
    stack.close()


def test_nodes_submit_once_across_ticks(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    _run_to_end(stack)
    subs = stack.executor.ledger.submissions
    # 再次 tick (已完成) 不重复提交
    stack.task.tick()
    assert stack.executor.ledger.submissions == subs
    stack.close()


def test_condition_false_goes_waiting_operator(tmp_path):
    stack = _stack(tmp_path, circles=[])  # NO_TARGET
    stack.task.start("ds86_two_station_example", allow_draft=True)
    assert _run_to_end(stack) is TaskState.WAITING_OPERATOR
    stack.close()


def test_low_quality_indeterminate_waits(tmp_path):
    stack = _stack(tmp_path, camera_low_depth=True,
                   decision=CircleDecisionConfig(min_depth_valid_ratio=0.9))
    stack.task.start("ds86_two_station_example", allow_draft=True)
    assert _run_to_end(stack) is TaskState.WAITING_OPERATOR
    stack.close()


def test_pause_stops_ticking_and_resume_continues(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()
    stack.task.request_pause()          # DEC-041: 请求即在安全边界落定 (PAUSED)
    assert stack.task.state_machine.state is TaskState.PAUSED
    before = stack.robot.read_state()["move_count"]
    for _ in range(5):
        stack.task.tick()
    assert stack.robot.read_state()["move_count"] == before  # 暂停不推进
    stack.task.resume(reconciled=True)
    assert _run_to_end(stack) is TaskState.COMPLETED
    stack.close()


def test_single_step_executes_one_step_then_pauses(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    st = stack.task.single_step()
    assert stack.task.state_machine.state in (TaskState.PAUSED, TaskState.WAITING_OPERATOR)
    stack.close()


def test_single_step_from_stopped_continues_same_run_current_step(tmp_path):
    """DEC-101：停止后单步从当前未完成步骤继续，不从头新起 run。"""
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()                       # 第一步已提交、仍在执行
    run_id = stack.task.run_ctx.run_id
    stack.task.terminate("先停下再单步")
    assert stack.task.state_machine.state is TaskState.STOPPED
    move = stack.task.compiled.find_step("move")
    assert move.status.name == "RUNNING"
    before_attempt = move.attempt
    stack.task.single_step("ds86_two_station_example", allow_draft=True)
    assert stack.task.state_machine.state in (TaskState.PAUSED, TaskState.WAITING_OPERATOR)
    assert stack.task.run_ctx.run_id == run_id          # 同一 run，不从头
    assert move.attempt == before_attempt + 1           # 当前步骤换新 attempt
    stack.close()


def test_single_step_from_clean_fault_continues_failed_step(tmp_path):
    """DEC-101：无 UNKNOWN/隔离的 FAULT，单步从失败步骤继续（同一 run）。"""
    from py_trees.common import Status
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()
    run_id = stack.task.run_ctx.run_id
    move = stack.task.compiled.find_step("move")
    move.status = Status.FAILURE
    move.failure_reason = "模拟设备失败"
    # 模拟设备已停止并结算在途租约（真实失败路径由 poll/_release 完成）。
    stack.executor.settle_run_leases(run_id, stop_confirmed=True)
    stack.task.state_machine.fail(FailureClass.DEVICE, "模拟设备失败")
    stack.task.single_step("ds86_two_station_example", allow_draft=True)
    assert stack.task.state_machine.state in (TaskState.PAUSED, TaskState.WAITING_OPERATOR)
    assert stack.task.run_ctx.run_id == run_id
    stack.close()


def test_single_step_from_stopped_still_respects_the_publish_gate(tmp_path):
    """DEC-041：停止态单步同样过发布闸——未发布草稿且未 allow_draft 就如实拒绝。"""
    from application.services.task import TaskError
    stack = _stack(tmp_path)
    with pytest.raises(TaskError):
        stack.task.single_step("ds86_two_station_example", allow_draft=False)
    stack.close()


def _advance_to_gate(task, step_id: str, limit: int = 40):
    """推进到指定步骤的确认闸出现，返回该闸。"""
    for _ in range(limit):
        task.tick()
        for gate in task.compiled.gates:
            prompt = getattr(gate, "pending_prompt", None)
            if prompt and prompt.get("step_id") == step_id:
                return gate
    raise AssertionError(f"没有等到步骤 {step_id!r} 的确认闸")


def test_single_step_after_step_rejected_moves_to_next_step(tmp_path):
    """DEC-102：第 3 步完成但判「不正确」后，单步应从下一步继续，不重问该步。"""
    stack = _stack(tmp_path)
    task = stack.task
    task.start("ds86_two_station_example", allow_draft=True, step_confirm=True)
    gate = _advance_to_gate(task, "move")
    gate.provide("不正确", operator="tester")
    assert task.tick() is TaskState.PAUSED
    assert task.compiled.find_step("move").status.name == "SUCCESS"
    assert task.compiled.find_step("capture").status.name == "INVALID"
    # 单步：不应再次弹 move 的确认，而是直接执行下一步 capture
    task.single_step("ds86_two_station_example", allow_draft=True, step_confirm=True)
    assert task.state_machine.state in (TaskState.PAUSED, TaskState.WAITING_OPERATOR)
    prompts = [g.pending_prompt for g in task.compiled.gates if g.pending_prompt]
    assert all(p.get("step_id") != "move" for p in prompts)
    assert task.compiled.find_step("capture").status.name != "INVALID"
    stack.close()


def test_single_step_after_rejected_then_stopped_moves_to_next_step(tmp_path):
    """DEC-102：判「不正确」后先停止，再单步也从下一步继续。"""
    stack = _stack(tmp_path)
    task = stack.task
    task.start("ds86_two_station_example", allow_draft=True, step_confirm=True)
    gate = _advance_to_gate(task, "move")
    gate.provide("不正确", operator="tester")
    assert task.tick() is TaskState.PAUSED
    task.terminate("操作员先停止")
    assert task.state_machine.state is TaskState.STOPPED
    task.single_step("ds86_two_station_example", allow_draft=True, step_confirm=True)
    assert task.state_machine.state in (TaskState.PAUSED, TaskState.WAITING_OPERATOR)
    prompts = [g.pending_prompt for g in task.compiled.gates if g.pending_prompt]
    assert all(p.get("step_id") != "move" for p in prompts)
    assert task.compiled.find_step("capture").status.name != "INVALID"
    stack.close()


def test_terminate_all_confirmed_stops(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()
    summary = stack.task.terminate("操作员终止")
    assert summary.all_confirmed
    assert stack.task.state_machine.state is TaskState.STOPPED
    stack.close()


def test_terminate_unknown_goes_fault(tmp_path):
    stack = _stack(tmp_path, stop_verify_unknown_for={"eco65_01"})
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()
    summary = stack.task.terminate("终止")
    assert not summary.all_confirmed
    assert stack.task.state_machine.state is TaskState.FAULT
    stack.close()


def test_terminate_does_not_auto_home_or_release(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()
    before = stack.robot.state
    stack.task.terminate("终止流程")
    # 不自动回零/松夹: 机器人只进入停止/就绪, 未回到 home 关节
    assert stack.robot.state.name in ("READY", "STOPPING", "DISCONNECTED")
    stack.close()


def test_unknown_ledger_not_reset():
    led = ExecutionLedger()
    key = ExecutionKey(run_id="r", station_instance_id="s", step_id="x")
    led.get_or_create(key)
    led.mark_unknown(key)
    assert led.entry(key).reset_forbidden()      # UNKNOWN 不得重置成 CREATED
    assert led.unknown_entries() and led.entry(key).terminal
    led.clear_unknown(key, "operator_checked")   # 人工核对后才解除
    assert not led.entry(key).unknown and not led.entry(key).terminal
    with pytest.raises(Exception):
        led.clear_unknown(key, "again")          # 非 UNKNOWN 不能再解除
