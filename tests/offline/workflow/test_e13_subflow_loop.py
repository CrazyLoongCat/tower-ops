"""E13 子流程模板 / 有限循环 / 等待 / 人工确认 测试 (T15/T16/T17/T20/T27/T28/T34 离线)。"""
from __future__ import annotations

import time

import pytest
from py_trees.common import Status

from application.bootstrap.composition import build_offline_stack, default_subflows
from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import NodeType, TaskState
from application.domain.errors import ValidationFailure
from application.services.recipes.models import RecipeModel, StepModel
from application.services.recipes.service import RecipeService, ReferenceCatalog
from application.workflow.bounded_loop import BoundedLoop, UnboundedLoop
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.operator_confirm import OperatorConfirmNode
from application.workflow.nodes.wait_state import WaitStateNode
from application.workflow.subflows import (
    OverrideNotAllowed, SubflowTemplate, UnknownSubflow, check_overrides, expand_subflow,
)

BASE_RECIPE = "configs/recipes/ds86_two_station_example.yaml"
LOOP_RECIPE_TEXT = """\
schema_version: 2
recipe_id: ds86_two_station_with_loop
version: 0.2-draft
state: draft
is_offline_draft: true
allow_partial: false
sequence:
  - {id: move, type: robot.move_joint, joint_target_ref: teach_01}
  - id: wait_ready
    type: wait.state
    device_ref: eco65_01
    field_name: state
    expected: READY
    stable_ticks: 2
    deadline_ms: 5000
  - {id: capture, type: camera.capture, camera_ref: ds86_01}
  - {id: detect, type: vision.run, input_from: {step: capture, output: frame}, pipeline_ref: approved_ds86_circle_pipeline}
  - id: gate
    type: condition
    predicate: {source: {step: detect, field: status}, op: eq, value: PASS}
    on_false: {policy: pause_for_review}
  - id: resample
    type: repeat
    subflow_ref: capture_and_detect
    max_iterations: 2
  - id: confirm
    type: operator.confirm
    inputs: [detect]
    operator_options: ["继续", "终止"]
  - {id: mechanism, type: mechanism.action, action_ref: approved_single_board_action}
  - {id: evidence, type: record.evidence, inputs: [capture, detect, mechanism]}
"""


def _loop_recipe(tmp_path) -> str:
    path = tmp_path / "loop_recipe.yaml"
    path.write_text(LOOP_RECIPE_TEXT, encoding="utf-8")
    return str(path)


def _stack(tmp_path, recipe=BASE_RECIPE, **kw):
    return build_offline_stack(db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
                               recipe_path=recipe, **kw)


def _ctx(run_id="r1", si="r1:run:0"):
    return RunContext(run_id=run_id,
                      versions=VersionSnapshot("x", "v", "c", "k"),
                      station_instance_id=si)


def _tick_until(node, limit=20):
    """注意: py_trees 的 tick_once() 返回 None, 必须读 node.status。"""
    for _ in range(limit):
        node.tick_once()
        if node.status in (Status.SUCCESS, Status.FAILURE):
            return node.status
    return node.status


def _drive_with_operator(stack, limit=600):
    """驱动任务; 每到 operator.confirm 就投一次"继续"。

    注意: initialise 会清空选择, 故每 tick 都投。
    """
    st = None
    for _ in range(limit):
        for node in stack.task.compiled.find_steps("confirm"):
            node.provide("继续", operator="tester")
        st = stack.task.tick()
        if st in (TaskState.COMPLETED, TaskState.FAULT, TaskState.WAITING_OPERATOR):
            return st
    return st


# ---------------- 有界循环 ----------------
def test_unbounded_loop_rejected():
    with pytest.raises(UnboundedLoop):
        BoundedLoop("l", factory=lambda c: [], max_iterations=0, ctx=_ctx())
    with pytest.raises(UnboundedLoop):
        BoundedLoop("l", factory=lambda c: [], max_iterations=None, ctx=_ctx())


def test_loop_runs_exactly_max_iterations(tmp_path):
    stack = _stack(tmp_path, _loop_recipe(tmp_path))
    stack.task.start("ds86_two_station_with_loop", allow_draft=True)
    compiled = stack.task.compiled
    loop = compiled.find_step("resample")
    assert isinstance(loop, BoundedLoop) and loop._max == 2      # noqa: SLF001
    # 驱动到循环结束
    _drive_with_operator(stack)
    assert loop.iterations_completed == 2
    assert loop.rebuilds == 2                                     # 每轮重建子节点
    stack.close()


def test_loop_iterations_have_distinct_ledger_keys(tmp_path):
    stack = _stack(tmp_path, _loop_recipe(tmp_path))
    stack.task.start("ds86_two_station_with_loop", allow_draft=True)
    _drive_with_operator(stack)
    keys = [e.key.as_tuple() for e in stack.task.ledger.all_entries()]
    sub = [k for k in keys if "capture_and_detect" in k[1]]
    assert sub, "子流程应产生账本条目"
    iters = {k[4] for k in sub}
    assert iters == {1, 2}, f"两轮迭代应有独立 iteration 键: {iters}"
    stack.close()


def test_loop_failure_is_failure_not_alternative_branch(tmp_path):
    """子流程失败 -> FAILURE, 不落入替代运动分支 (总纲 E13 步骤⑥)."""
    stack = _stack(tmp_path, _loop_recipe(tmp_path))

    def boom(iter_ctx):
        raise ValidationFailure("子流程构建失败")
    loop = BoundedLoop("l", factory=boom, max_iterations=2, ctx=_ctx())
    with pytest.raises(ValidationFailure):
        loop.update()
    stack.close()


# ---------------- 校验 (模板引用/override/有界) ----------------
def _catalog_with_subflows():
    reg = default_subflows()
    return ReferenceCatalog(
        joint_targets={}, pipelines={"approved_ds86_circle_pipeline"},
        actions=None, cameras={"ds86_01"}, devices={"eco65_01"}, subflows=reg)


def _recipe_with(step: dict) -> RecipeModel:
    return RecipeModel(recipe_id="r", version="v", sequence=[step])


def test_repeat_requires_bound_and_template():
    from application.services.recipes.service import validate_recipe
    rep = validate_recipe(
        _recipe_with({"id": "loop", "type": "repeat"}), _catalog_with_subflows())
    msgs = " ".join(rep.errors)
    assert "subflow_ref" in msgs and "max_iterations" in msgs


def test_unknown_subflow_rejected():
    from application.services.recipes.service import validate_recipe
    rep = validate_recipe(
        _recipe_with({"id": "sf", "type": "subflow", "subflow_ref": "nope"}),
        _catalog_with_subflows())
    assert any("未知子流程模板" in e for e in rep.errors)


def test_override_whitelist_enforced():
    tpl = SubflowTemplate(template_id="t", version="v1", steps=(), allow_overrides=("a",))
    check_overrides(tpl, {"a": 1})                     # 允许
    with pytest.raises(OverrideNotAllowed):
        check_overrides(tpl, {"b": 2})                  # 不在白名单
    from application.services.recipes.service import validate_recipe
    rep = validate_recipe(
        _recipe_with({"id": "sf", "type": "subflow", "subflow_ref": "capture_and_detect",
                      "overrides": {"deadline_ms": 100, "hard_limits": 1}}),
        _catalog_with_subflows())
    assert any("override 不在模板白名单" in e for e in rep.errors)


def test_subflow_scope_isolation(tmp_path):
    """子流程展开使用独立作用域, 不污染外层运行作用域输出 (E13 步骤③)."""
    stack = _stack(tmp_path, _loop_recipe(tmp_path))
    stack.task.start("ds86_two_station_with_loop", allow_draft=True)
    _drive_with_operator(stack)
    outputs = stack.task.compiled.outputs
    si = stack.task.compiled.find_step("capture").ctx.station_instance_id  # 外层运行作用域
    assert outputs.step_outputs(si, "capture").get("capture_id")
    scoped = f"{si}/capture_and_detect@v1"
    assert outputs.step_outputs(scoped, "sub_capture").get("capture_id")  # 子流程作用域
    stack.close()


# ---------------- wait.state ----------------
def test_wait_state_stable_window(tmp_path):
    stack = _stack(tmp_path)
    node = WaitStateNode("w", step_id="w", ctx=_ctx(), executor=stack.executor,
                         outputs=OutputStore(), device_id="eco65_01",
                         field_name="state", expected="READY",
                         stable_ticks=2, max_age_ms=10000)
    node.setup()
    node.tick_once()
    assert node.status is Status.RUNNING      # 第 1 次: 未达持续窗
    assert _tick_until(node) is Status.SUCCESS
    assert node.satisfied
    stack.close()


def test_wait_state_deadline_fails(tmp_path):
    stack = _stack(tmp_path)
    node = WaitStateNode("w", step_id="w", ctx=_ctx(), executor=stack.executor,
                         outputs=OutputStore(), device_id="eco65_01",
                         field_name="state", expected="NEVER",
                         stable_ticks=1, deadline_ms=30, max_age_ms=10000)
    node.setup()
    st = Status.RUNNING
    for _ in range(60):
        node.tick_once()
        st = node.status
        if st is Status.FAILURE:
            break
        time.sleep(0.005)
    assert st is Status.FAILURE and "超时" in (node.failure_reason or "")
    stack.close()


# ---------------- operator.confirm ----------------
def test_operator_confirm_option_and_evidence(tmp_path):
    stack = _stack(tmp_path)
    out = OutputStore()
    ctx = _ctx()
    node = OperatorConfirmNode("c", step_id="confirm", ctx=ctx, executor=stack.executor,
                               outputs=out, prompt="确认", options=("继续", "终止"),
                               evidence_refs=("detect",))
    node.setup()
    node.tick_once()                                  # 先 initialise (会清空 choice)
    assert node.provide("非法选项") is False          # 选项受限
    assert node.provide("继续") is False              # 缺证据不允许确认
    assert "缺证据" in (node.last_reject or "")
    out.set(ctx.station_instance_id, "detect", "status", "PASS")
    assert node.provide("继续", operator="alice") is True
    assert _tick_until(node) is Status.SUCCESS
    assert out.get(ctx.station_instance_id, "confirm", "operator") == "alice"
    # 留证: 事件已写入
    events = list(stack.records.replay("r1"))
    assert any(e["event_type"] == "operator.confirm" for e in events)
    stack.close()


def test_full_loop_recipe_completes_with_operator(tmp_path):
    stack = _stack(tmp_path, _loop_recipe(tmp_path))
    stack.task.start("ds86_two_station_with_loop", allow_draft=True)
    st = _drive_with_operator(stack)
    assert st is TaskState.COMPLETED
    assert stack.records.pending_unknown_commands() == []
    stack.close()
