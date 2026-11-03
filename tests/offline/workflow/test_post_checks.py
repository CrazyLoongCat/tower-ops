"""DEC-064 步骤后判断属性：通过才进入下一步；不通过停在安全边界 PAUSED。

覆盖：
- 设备遥测字段判断通过 -> 完成；不通过 -> PAUSED，修好遥测后继续 -> 完成（重新判断）；
- 前序步骤输出判断；
- 机械臂末端位姿判断（引用示教点：期望位姿=本次运行到达该示教点时的实测位姿）；
- 判断属性的配方校验（未知设备/示教点/步骤、引用后面的步骤）。
"""
from __future__ import annotations

import pytest

from application.bootstrap.composition import build_offline_stack
from application.contracts.enums import ConditionOp, TaskState, TelemetryFreshness
from application.domain.telemetry import TelemetryRecord
from application.workflow.nodes.steps import PostCheckNode
from application.services.recipes.models import StepCheckModel
from application.services.recipes.service import ReferenceCatalog, RecipeService, validate_recipe
from application.services.recipes.models import RecipeModel

RECIPE_PATH = "configs/recipes/ds86_two_station_example.yaml"
RID = "post_check_case"


def _stack(tmp_path, *, post_checks, extra_steps=None):
    stack = build_offline_stack(
        db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
        recipe_path=RECIPE_PATH)
    sequence = [
        {"id": "mech", "type": "mechanism.action",
         "action_ref": "approved_single_board_action",
         "post_checks": post_checks},
    ]
    if extra_steps:
        sequence.extend(extra_steps)
    stack.recipes.draft({"recipe_id": RID, "version": "v1", "state": "draft",
                         "sequence": sequence})
    return stack


def _run_to(stack, wanted, limit=100):
    st = None
    for _ in range(limit):
        st = stack.task.tick()
        if st is wanted:
            return st
    return st


def _device_check(expected="READY", op="eq"):
    return [{"kind": "device_field", "device_ref": "eco65_01",
             "field_name": "state", "op": op, "value": expected}]


def test_device_field_check_passes_and_completes(tmp_path):
    stack = _stack(tmp_path, post_checks=_device_check())
    stack.task.start(RID, allow_draft=True)
    assert _run_to(stack, TaskState.COMPLETED) is TaskState.COMPLETED
    stack.close()


def test_device_field_check_failure_pauses_and_resumes(tmp_path):
    stack = _stack(tmp_path, post_checks=_device_check("MOVING"))
    stack.task.start(RID, allow_draft=True)
    assert _run_to(stack, TaskState.PAUSED) is TaskState.PAUSED
    assert any("判断不通过" in (t.reason or "")
               for t in stack.task.state_machine.history)
    # 人工处理：把遥测改成期望值 -> 继续 -> 判断重新执行并通过
    stack.state_store.update(TelemetryRecord(
        device_id="eco65_01", values={"state": "MOVING", "joints": [0.0] * 6},
        validity=TelemetryFreshness.VALID))
    stack.task.resume(reconciled=True)
    assert _run_to(stack, TaskState.COMPLETED) is TaskState.COMPLETED
    stack.close()


def test_step_output_check_uses_previous_step_output(tmp_path):
    checks = [{"kind": "step_output", "source_step": "cap",
               "field_name": "capture_id", "op": "ne", "value": ""}]
    stack = build_offline_stack(
        db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
        recipe_path=RECIPE_PATH)
    stack.recipes.draft({"recipe_id": RID, "version": "v1", "state": "draft",
                         "sequence": [
                             {"id": "cap", "type": "camera.capture",
                              "camera_ref": "ds86_01"},
                             {"id": "mech", "type": "mechanism.action",
                              "action_ref": "approved_single_board_action",
                              "post_checks": checks},
                         ]})
    stack.task.start(RID, allow_draft=True)
    assert _run_to(stack, TaskState.COMPLETED) is TaskState.COMPLETED
    stack.close()


class _PoseExecutor:
    """只给 PostCheckNode 用的最小执行器：位姿由测试控制。"""

    def __init__(self, expected, current):
        self._expected = expected
        self._current = current
        self.ledger = None

    def target_pose_mm_deg(self, ref, run_id=""):
        return self._expected if ref == "teach_01" else None

    def current_pose_mm_deg(self):
        return self._current


def _check_node(executor, **chk):
    from types import SimpleNamespace

    from application.contracts.context import RunContext
    from application.contracts.context import VersionSnapshot
    from application.workflow.nodes.base import OutputStore

    ctx = RunContext(run_id="r", versions=VersionSnapshot(
        recipe_id="x", recipe_version="v", config_hash="h", calibration_hash="c"))
    model = StepCheckModel(kind="robot_pose", joint_target_ref="teach_01", **chk)
    return PostCheckNode("check:s", checks=[model], target_step_id="s",
                         ctx=ctx, executor=executor, outputs=OutputStore())


def test_robot_pose_check_uses_recorded_teach_point_pose():
    node = _check_node(_PoseExecutor((10.0, 20.0, 30.0, 0, 0, 0),
                                     (10.5, 20.0, 30.0, 0, 0, 0)))
    from py_trees.common import Status
    assert node._tick(None) is Status.SUCCESS                   # noqa: SLF001
    assert node.consume_pause_request() is None

    node2 = _check_node(_PoseExecutor((10.0, 20.0, 30.0, 0, 0, 0),
                                      (13.0, 20.0, 30.0, 0, 0, 0)), pos_tol_mm=2.0)
    assert node2._tick(None) is Status.RUNNING                   # noqa: SLF001
    reason = node2.consume_pause_request()
    assert reason and "偏离示教点" in reason and "3.00mm" in reason
    assert node2.consume_pause_request() is None                 # 只取一次


def test_robot_pose_check_passes_after_move_to_teach_point(tmp_path):
    """集成：运动到示教点后记录实测位姿，后置判断引用该示教点可通过。"""
    stack = build_offline_stack(
        db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
        recipe_path=RECIPE_PATH)
    stack.recipes.draft({"recipe_id": RID, "version": "v1", "state": "draft",
                         "sequence": [
                             {"id": "mv", "type": "robot.move_joint",
                              "joint_target_ref": "teach_01",
                              "post_checks": [{
                                  "kind": "robot_pose",
                                  "joint_target_ref": "teach_01",
                                  "pos_tol_mm": 2.0}]},
                         ]})
    stack.task.start(RID, allow_draft=True)
    assert _run_to(stack, TaskState.COMPLETED) is TaskState.COMPLETED
    assert (stack.task.state_machine.run_id, "teach_01") in stack.executor.pose_by_target
    stack.close()


def test_robot_pose_check_without_recorded_pose_fails_clearly():
    class _NoRecord(_PoseExecutor):
        def target_pose_mm_deg(self, ref, run_id=""):
            return None

    node = _check_node(_NoRecord(None, (0, 0, 0, 0, 0, 0)))
    from py_trees.common import Status
    assert node._tick(None) is Status.RUNNING                     # noqa: SLF001
    reason = node.consume_pause_request()
    assert reason and "还没有实测位姿" in reason


def _recipe(post_checks):
    return RecipeModel.model_validate({
        "recipe_id": "pc", "version": "v1", "state": "draft",
        "sequence": [
            {"id": "mech", "type": "mechanism.action",
             "action_ref": "approved_single_board_action",
             "post_checks": post_checks},
        ]})


def _catalog():
    return ReferenceCatalog(
        devices={"eco65_01"},
        actions=None,
    )


def test_validation_rejects_unknown_check_references():
    bad = [
        {"kind": "device_field", "device_ref": "nope", "field_name": "state",
         "op": "eq", "value": 1},
        {"kind": "robot_pose", "joint_target_ref": "teach_missing"},
        {"kind": "step_output", "source_step": "missing", "field_name": "x",
         "op": "eq", "value": 1},
    ]
    report = validate_recipe(_recipe(bad), _catalog())
    text = " | ".join(report.errors)
    assert "未知设备 'nope'" in text
    assert "未知示教点 'teach_missing'" in text
    assert "未知步骤 'missing'" in text


def test_validation_rejects_step_output_from_later_step():
    model = RecipeModel.model_validate({
        "recipe_id": "pc2", "version": "v1", "state": "draft",
        "sequence": [
            {"id": "a", "type": "mechanism.action",
             "action_ref": "approved_single_board_action",
             "post_checks": [{"kind": "step_output", "source_step": "b",
                              "field_name": "x", "op": "eq", "value": 1}]},
            {"id": "b", "type": "mechanism.action",
             "action_ref": "approved_single_board_action"},
        ]})
    report = validate_recipe(model, _catalog())
    assert any("只能引用本步骤之前" in e for e in report.errors)
