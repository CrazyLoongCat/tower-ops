"""并行执行步骤组（DEC-096）：编辑器默认分支、校验、编译/执行、失败取消。

首版范围：一个 robot.move_joint 分支 + 一个 mechanism.action 分支。
"""
from __future__ import annotations

import pytest
from py_trees.common import Status

from application.bootstrap.composition import build_offline_stack
from application.contracts.enums import NodeType, TaskState
from application.contracts.targets import JointTarget
from application.domain.actions import ActionCatalog, ActionDefinition, ParamSpec
from application.domain.resources import CAPTURE_STILLNESS, device_resource
from application.services.recipes.editor import RecipeEditor
from application.services.recipes.models import RecipeModel
from application.services.recipes.service import (
    ReferenceCatalog, RecipeService, validate_recipe,
)
from application.services.methods import default_methods
from application.workflow.nodes.parallel_group import ParallelGroupNode
from application.workflow.subflows import SubflowRegistry, SubflowTemplate

BASE_RECIPE = "configs/recipes/ds86_two_station_example.yaml"
TYPE_ROBOT = NodeType.ROBOT_MOVE_JOINT
TYPE_MECH = NodeType.MECHANISM_ACTION
ACTION = "approved_single_board_action"


# ---------------------------------------------------------------- 编辑器/校验
def _catalog() -> ReferenceCatalog:
    tpl = SubflowTemplate(template_id="capture_and_detect", version="v1",
                          steps=(), allow_overrides=())
    return ReferenceCatalog(
        joint_targets={"teach_01": JointTarget(joint_deg=[0] * 6,
                                               waypoint_ref="taught_1")},
        pipelines={"approved_ds86_circle_pipeline"},
        actions=ActionCatalog([ActionDefinition(
            action_id=ACTION, device_id="stm32_01",
            resource_ids=(device_resource("stm32_01"), CAPTURE_STILLNESS),
            parameters=(ParamSpec(name="force", minimum=0, maximum=100,
                                  required=False),), timeout_ms=1000)]),
        cameras={"ds86_01"},
        devices={"eco65_01", "ds86_01"},
        subflows=SubflowRegistry([tpl]),
        motion_plans={"取件方案"},
        detections={},
        methods=default_methods(),
    )


def _editor() -> RecipeEditor:
    ed = RecipeEditor(RecipeService(_catalog()))
    ed.new_recipe("r1")
    ed.set_offline_draft(False)
    return ed


def _parallel_step(**overrides) -> dict:
    step = {
        "id": "p",
        "type": NodeType.PARALLEL_GROUP.value,
        "branches": [
            {"id": "p_robot", "type": TYPE_ROBOT.value,
             "joint_target_ref": "teach_01"},
            {"id": "p_mech", "type": TYPE_MECH.value, "action_ref": ACTION},
        ],
    }
    step.update(overrides)
    return step


def _validate_sequence(*steps) -> list[str]:
    model = RecipeModel.model_validate(
        {"recipe_id": "r", "version": "v1", "sequence": list(steps)})
    return validate_recipe(model, _catalog()).errors


def test_editor_add_parallel_group_creates_robot_and_mechanism_branches():
    ed = _editor()
    sid = ed.add_step(NodeType.PARALLEL_GROUP)
    step = ed.model.sequence[0]
    assert sid == "parallel_1" and step.id == sid
    assert [b.type for b in step.branches] == [TYPE_ROBOT, TYPE_MECH]
    assert step.branches[0].id == "parallel_1_robot"
    assert step.branches[1].id == "parallel_1_mech"
    assert step.branches[0].joint_target_ref == "teach_01"
    assert step.branches[1].action_ref == ACTION
    assert ed.validate().ok


def test_editor_can_change_branch_fields():
    ed = _editor()
    sid = ed.add_step(NodeType.PARALLEL_GROUP)
    ed.set_parallel_branch_field(sid, TYPE_ROBOT, "motion_plan_ref", "取件方案")
    ed.set_parallel_branch_field(sid, TYPE_MECH, "action_ref", ACTION)
    step = ed.model.sequence[0]
    assert step.branches[0].motion_plan_ref == "取件方案"
    assert ed.validate().ok


def test_two_valid_branches_validate():
    assert _validate_sequence(_parallel_step()) == []


def test_parallel_group_requires_exactly_two_branches():
    one = _parallel_step()
    one["branches"] = one["branches"][:1]
    errors = _validate_sequence(one)
    assert any("必须有 2 个分支" in e for e in errors)
    assert any("恰好是一个" in e for e in errors)


def test_parallel_group_requires_one_robot_and_one_mechanism():
    both_robot = _parallel_step()
    both_robot["branches"][1] = {"id": "p_robot2", "type": TYPE_ROBOT.value,
                                 "joint_target_ref": "teach_01"}
    assert any("恰好是一个" in e for e in _validate_sequence(both_robot))


def test_parallel_group_rejects_nested_group():
    nested = _parallel_step()
    nested["branches"][1] = {"id": "p_nested",
                             "type": NodeType.PARALLEL_GROUP.value,
                             "branches": []}
    assert any("不支持嵌套" in e for e in _validate_sequence(nested))


def test_parallel_branch_cannot_have_post_checks():
    bad = _parallel_step()
    bad["branches"][0]["post_checks"] = [{
        "kind": "robot_pose", "joint_target_ref": "teach_01"}]
    assert any("不支持判断属性" in e for e in _validate_sequence(bad))


def test_branches_are_rejected_on_non_parallel_step():
    step = {"id": "move", "type": TYPE_ROBOT.value,
            "joint_target_ref": "teach_01",
            "branches": [{"id": "b", "type": TYPE_MECH.value, "action_ref": ACTION}]}
    assert any("不是并行执行" in e for e in _validate_sequence(step))


def test_parallel_branch_reference_must_exist():
    bad = _parallel_step()
    bad["branches"][0]["joint_target_ref"] = "teach_missing"
    bad["branches"][1]["action_ref"] = "action_missing"
    errors = _validate_sequence(bad)
    assert any("未知示教点" in e for e in errors)
    assert any("未知动作" in e for e in errors)


def test_parallel_branch_id_cannot_collide_with_top_level_step():
    from pydantic import ValidationError
    bad = _parallel_step()
    bad["branches"][0]["id"] = "move"
    with pytest.raises(ValidationError, match="必须唯一"):
        RecipeModel.model_validate(
            {"recipe_id": "r", "version": "v1", "sequence": [
                {"id": "move", "type": TYPE_ROBOT.value,
                 "joint_target_ref": "teach_01"},
                bad]})


# ---------------------------------------------------------------- 行为树执行
def _stack_with_parallel(tmp_path):
    stack = build_offline_stack(
        db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
        recipe_path=BASE_RECIPE)
    stack.recipes.draft({"recipe_id": "parallel_case", "version": "v1",
                         "state": "draft", "sequence": [_parallel_step()]})
    return stack


def test_parallel_group_submits_both_branches_in_same_tick(tmp_path):
    stack = _stack_with_parallel(tmp_path)
    stack.task.start("parallel_case", allow_draft=True)
    assert stack.task.tick() is TaskState.RUNNING
    # 串行时第一 tick 只会提交机器人分支；两个都已提交 = 真正并行开始。
    assert stack.executor.ledger.submissions == 2
    node = stack.task.compiled.find_step("p")
    assert isinstance(node, ParallelGroupNode)
    stack.close()


def test_parallel_group_completes_only_after_both_branches_succeed(tmp_path):
    stack = _stack_with_parallel(tmp_path)
    stack.task.start("parallel_case", allow_draft=True)
    st = None
    for _ in range(200):
        st = stack.task.tick()
        if st in (TaskState.COMPLETED, TaskState.FAULT):
            break
    assert st is TaskState.COMPLETED
    node = stack.task.compiled.find_step("p")
    assert node.status is Status.SUCCESS
    stack.close()


class _StubChild:
    """最小分支节点：可控状态、记录 stop、可选 arm_retry。"""

    def __init__(self, name, step_id, status, *, arm_retry=None):
        self.name = name
        self.step_id = step_id
        self.status = status
        self.failure_reason = ""
        self.stopped_with = None
        self.arm_retried = 0
        self._arm_retry = arm_retry

    def stop(self, new_status=Status.INVALID):
        self.stopped_with = new_status

    def arm_retry(self, reason=""):
        self.arm_retried += 1
        return self.arm_retried


class _RobotStub:
    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class _MotionStub:
    def __init__(self):
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class _ExecutorStub:
    def __init__(self):
        self.robot = _RobotStub()
        self.settled: list[tuple[str, bool]] = []

    def settle_run_leases(self, run_id, *, stop_confirmed):
        self.settled.append((run_id, stop_confirmed))
        return ["run-x/0/p_robot#1"]


class MoveJointNode(_StubChild):
    """与真实运动分支同名：组失败时据此判定需向机械臂发停止请求。"""


def test_group_failure_cancels_running_sibling_and_requests_robot_stop():
    robot = MoveJointNode("move:p_robot", "p_robot", Status.RUNNING)
    mech = _StubChild("mech:p_mech", "p_mech", Status.FAILURE)
    mech.failure_reason = "机构动作 REJECTED"
    executor = _ExecutorStub()
    motion = _MotionStub()
    group = ParallelGroupNode("parallel:p", step_id="p", title="并行执行",
                              executor=executor, children=[robot, mech],
                              run_id="run-x", motion=motion)
    group._on_group_failure()
    assert robot.stopped_with is Status.INVALID
    assert executor.robot.stop_calls == 1
    assert motion.stop_calls == 1                    # 运动方案线程也要停
    assert executor.settled == [("run-x", False)]      # 停稳未核实 -> 隔离
    assert "机构动作 REJECTED" in group.failure_reason
    assert "已请求机械臂停止" in group.failure_reason
    assert "需人工核对" in group.failure_reason
    assert group.failure_policy is None


def test_group_retry_rearms_failed_and_cancelled_branches():
    robot = MoveJointNode("move:p_robot", "p_robot", Status.RUNNING)
    mech = _StubChild("mech:p_mech", "p_mech", Status.FAILURE)
    mech.failure_reason = "机构动作 REJECTED"
    group = ParallelGroupNode("parallel:p", step_id="p", title="并行执行",
                              executor=_ExecutorStub(), children=[robot, mech],
                              run_id="run-x")
    group._on_group_failure()
    assert group.arm_retry("现场已处理") == 1
    assert mech.arm_retried == 1
    assert robot.arm_retried == 1          # 被取消的运动分支重新走到目标


def test_group_retry_does_not_rearm_successful_branch():
    robot = MoveJointNode("move:p_robot", "p_robot", Status.SUCCESS)
    mech = _StubChild("mech:p_mech", "p_mech", Status.FAILURE)
    mech.failure_reason = "机构动作 REJECTED"
    group = ParallelGroupNode("parallel:p", step_id="p", title="并行执行",
                              executor=_ExecutorStub(), children=[robot, mech],
                              run_id="run-x")
    group._on_group_failure()
    assert group.arm_retry("现场已处理") == 1
    assert mech.arm_retried == 1
    assert robot.arm_retried == 0


def test_parallel_group_rejected_when_action_still_holds_motion_global():
    cat = _catalog()
    cat.actions = ActionCatalog([ActionDefinition(
        action_id=ACTION, device_id="stm32_01", resource_ids=("motion_global",),
        timeout_ms=1000)])
    model = RecipeModel.model_validate(
        {"recipe_id": "r", "version": "v1", "sequence": [_parallel_step()]})
    errors = validate_recipe(model, cat).errors
    assert any("占用 motion_global" in e for e in errors)
