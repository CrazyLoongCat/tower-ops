"""DEC-042: 步骤完成确认闸 + 手动检测圆判定（作业台人工确认）。

用户要求：作业台每个步骤执行结束后弹框确认，点「确认」才继续；点「不正确」暂停
作业（不记成设备/流程故障）；视觉步骤的抓拍原图显示在右卡，自动判定不准时可
手动拖动检测圆，**调整完毕后以手动圆为准**。

验证边界（离线）：
- 编译器默认不插闸（其它入口行为不变）；``step_confirm=True`` 时每个步骤后插一闸，
  子流程/循环展开的步骤同样有闸；
- 「不正确」→ 任务停在安全边界（PAUSED，非 FAULT）；继续后重新询问该步；
- 确认/拒绝都记录事件（含操作者）；
- 手动圆按流水线阈值重算判定并回写输出（下游 condition 读到手动结果）；
- 「运行一次」方案（无圆目标）明确拒绝，不伪造检测圆。
硬件一律 NOT_RUN（只用离线 Fake 栈）。
"""
from __future__ import annotations

import py_trees
import pytest
from py_trees.common import Status

from application.algorithms.circle.detector import CircleDecisionConfig
from application.algorithms.circle.manual import decide_manual_circle
from application.bootstrap.composition import build_offline_stack
from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import DetectionStatus, TaskState
from application.contracts.results import CirclePrimitive, DetectionResult
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.step_gate import StepConfirmGate
from application.workflow.nodes.steps import VisionRunNode

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


def _stack(tmp_path, **kw):
    return build_offline_stack(db_path=str(tmp_path / "r.db"),
                               runtime_dir=str(tmp_path), recipe_path=RECIPE, **kw)


def _pending_gate(stack) -> StepConfirmGate | None:
    for gate in stack.task.compiled.gates:
        p = gate.pending_prompt
        if p and p.get("options"):
            return gate
    return None


def _drive_confirm_all(stack, operator="张三", limit=200) -> list[dict]:
    """跑完整个作业：每次出现步骤确认就点「确认」，返回见到过的提示。"""
    seen: list[dict] = []
    st = stack.task.state_machine.state
    for _ in range(limit):
        st = stack.task.tick()
        if st in (TaskState.COMPLETED, TaskState.FAULT, TaskState.WAITING_OPERATOR):
            break
        gate = _pending_gate(stack)
        if gate is not None:
            seen.append(gate.pending_prompt)
            assert gate.provide("确认", operator) is True
    return seen


# ---------------------------------------------------------------- 编译期
def test_step_confirm_default_off(tmp_path):
    """默认关（其它入口/离线测试行为不变）：无闸、树结构不变。"""
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    assert stack.task.compiled.gates == []
    root = stack.task.compiled.tree.root
    assert len(root.children) == 6                    # 配方 6 步, 未插闸
    stack.close()


def test_step_confirm_inserts_gate_after_every_step(tmp_path):
    """step_confirm=True：每个步骤后插一个闸（含条件/证据步骤——用户要求所有步骤都弹）。"""
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True,
                     step_confirm=True)
    gates = stack.task.compiled.gates
    assert [g.target_step_id for g in gates] == [
        "move", "capture", "detect", "gate", "mechanism", "evidence"]
    assert len(stack.task.compiled.tree.root.children) == 12     # 6 步 + 6 闸
    assert all(isinstance(g, StepConfirmGate) for g in gates)
    stack.close()


def test_subflow_loop_steps_get_gates(tmp_path):
    """子流程/有限循环展开的步骤也有闸（循环体在运行时展开，闸随之产生）。"""
    stack = _stack(tmp_path)
    rid = "loop_gate_offline"
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "rep", "type": "repeat", "subflow_ref": "capture_and_detect",
             "max_iterations": 1},
        ],
    })
    stack.task.start(rid, allow_draft=True, step_confirm=True)
    assert [g.target_step_id for g in stack.task.compiled.gates] == ["rep"]

    targets: list[str] = []
    st = stack.task.state_machine.state
    for _ in range(60):
        st = stack.task.tick()
        if st in (TaskState.COMPLETED, TaskState.FAULT):
            break
        gate = _pending_gate(stack)
        if gate is not None:
            targets.append(gate.target_step_id)
            assert gate.provide("确认", "张三") is True
    assert st is TaskState.COMPLETED
    # 循环体两步各一闸，最后是 repeat 步骤自己的闸
    assert targets == ["sub_capture", "sub_detect", "rep"]
    stack.close()


# ---------------------------------------------------------------- 服务层闭环
def test_confirm_each_step_completes_and_records_operator(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True,
                     step_confirm=True)
    seen = _drive_confirm_all(stack)
    assert stack.task.state_machine.state is TaskState.COMPLETED
    assert len(seen) == 6
    # 提示里带序号/总数与步骤名（作业台弹框直接用这里的内容）
    assert seen[0]["index"] == 1 and seen[0]["total"] == 6
    assert seen[0]["options"] == ["确认", "不正确"]
    assert "步骤完成确认" in seen[0]["title"]
    run_id = stack.task.state_machine.run_id
    evs = [e for e in stack.records.replay(run_id)
           if e["event_type"] == "step.confirm"]
    assert len(evs) == 6
    assert all(e["advice"] == "operator=张三" for e in evs)
    stack.close()


def test_reject_pauses_not_fault_and_resume_moves_on(tmp_path):
    """点「不正确」：停在安全边界（PAUSED），不是 FAULT；继续后放行到下一步。"""
    stack = _stack(tmp_path)
    rid = "reject_offline"
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "cap", "type": "camera.capture", "camera_ref": "ds86_01"},
        ],
    })
    stack.task.start(rid, allow_draft=True, step_confirm=True)
    assert stack.task.tick() is TaskState.RUNNING          # 抓拍完成, 闸在等
    gate = _pending_gate(stack)
    assert gate is not None and gate.target_step_id == "cap"
    assert gate.provide("是", "张三") is False             # 未声明选项被拒
    assert gate.provide("不正确", "张三") is True

    assert stack.task.tick() is TaskState.PAUSED           # 安全边界暂停, 非故障
    run_id = stack.task.state_machine.run_id
    evs = stack.records.replay(run_id)
    rej = [e for e in evs if e["event_type"] == "step.confirm.rejected"]
    assert len(rej) == 1 and rej[0]["advice"] == "operator=张三"
    assert any(e["event_type"] == "task.paused" for e in evs)

    # DEC-102：继续后不再重问已完成的这一步，直接放行；本例只有一步 -> 完成。
    assert stack.task.resume(True) is TaskState.RUNNING
    assert stack.task.tick() is TaskState.COMPLETED
    assert gate.pending_prompt is None
    stack.close()


def test_gate_provide_requires_pending_prompt(tmp_path):
    stack = _stack(tmp_path)
    gate = StepConfirmGate("gate:x", target_step_id="x", title="抓拍",
                           ctx=RunContext(run_id="r",
                                          versions=VersionSnapshot("x", "v0", "-", "-")),
                           executor=stack.executor, outputs=OutputStore(),
                           step_id="x")
    assert gate.pending_prompt is None                     # 未 initialise, 不伪造提示
    assert gate.provide("确认", "甲") is False
    gate.initialise()
    assert gate.pending_prompt["options"] == ["确认", "不正确"]
    assert gate.provide("确认", "甲") is True
    assert gate.pending_prompt is None                     # 已选择, 提示关闭
    assert gate.provide("确认", "甲") is False             # 不能重复提交
    stack.close()


# ---------------------------------------------------------------- 手动检测圆
def _manual_circle(status=DetectionStatus.PASS, pipeline_id="approved_ds86_circle_pipeline"):
    return CirclePrimitive(cx=120.0, cy=90.0, radius_px=40.0, coverage=0.8, flag=0), status, pipeline_id


def _vision_node_with_result(stack, *, pipeline_id="approved_ds86_circle_pipeline"):
    ctx = RunContext(run_id="r-manual", versions=VersionSnapshot("x", "v0", "-", "-"))
    outs = OutputStore()
    node = VisionRunNode("vision:detect", pipeline_id=pipeline_id,
                         input_step="capture", step_id="detect", ctx=ctx,
                         executor=stack.executor, outputs=outs)
    circle, status, pid = _manual_circle()
    result = DetectionResult(result_id="res-1", status=status, pipeline_id=pid,
                             frame_id="f-1", capture_id="c-1", targets=[circle],
                             metrics={"count": 1.0})
    si = ctx.station_instance_id or ""
    outs.set(si, "detect", "result", result)
    outs.set(si, "detect", "status", status.value)
    return node, outs, ctx, si, result


def test_manual_circle_decision_rules():
    cfg = CircleDecisionConfig(radius_min_px=20, radius_max_px=60)
    ok, why = decide_manual_circle(CirclePrimitive(10, 20, 40), cfg)
    assert ok is DetectionStatus.PASS and "满足判据" in why
    bad, why = decide_manual_circle(CirclePrimitive(10, 20, 5), cfg)
    assert bad is DetectionStatus.FAIL and "不在允许范围" in why
    bad2, why2 = decide_manual_circle(
        CirclePrimitive(10, 20, 40),
        CircleDecisionConfig(expected_count_min=2, expected_count_max=3))
    assert bad2 is DetectionStatus.FAIL and "超出期望范围" in why2
    # 手动圆没有覆盖率数据: 不伪造覆盖率判据（coverage=None 也 PASS）
    assert decide_manual_circle(CirclePrimitive(1, 2, 40, coverage=None),
                                cfg)[0] is DetectionStatus.PASS


def test_circle_state_reports_available_circle(tmp_path):
    stack = _stack(tmp_path)
    node, _outs, _ctx, _si, _result = _vision_node_with_result(stack)
    state = node.circle_state()
    assert state["available"] is True
    assert state["status"] == "PASS"
    assert state["circle"] == {"cx": 120.0, "cy": 90.0, "radius_px": 40.0}
    assert state["count"] == 1 and state["manual"] is False
    stack.close()


def test_apply_manual_circle_overrides_result_and_outputs(tmp_path):
    """调整后**以手动圆为准**：结果目标/判定/输出全部按手动圆更新，并留证。"""
    stack = _stack(tmp_path)
    node, outs, ctx, si, result = _vision_node_with_result(stack)
    info = node.apply_manual_circle(200, 150, 40, operator="张三")
    assert info["status"] == "PASS"
    assert info["auto_count"] == 1
    assert (info["cx"], info["cy"], info["radius_px"]) == (200.0, 150.0, 40.0)
    assert result.targets[0].cx == 200.0 and result.targets[0].cy == 150.0
    assert result.targets[0].coverage is None               # 手动圆无覆盖率, 不伪造
    assert result.metrics["manual"] == 1.0
    assert result.reason.startswith("手动微调")
    assert outs.get(si, "detect", "status") == "PASS"        # 下游 condition 读这里
    assert outs.get(si, "detect", "result") is result
    assert node.circle_state()["manual"] is True

    evs = [e for e in stack.records.replay(ctx.run_id)
           if e["event_type"] == "vision.manual_circle"]
    assert len(evs) == 1
    assert evs[0]["advice"] == "operator=张三"
    assert "自动圆" in evs[0]["message"] and "手动" in evs[0]["message"]
    stack.close()


def test_apply_manual_circle_fails_out_of_range_and_updates_status(tmp_path, monkeypatch):
    """手动半径超阈值 -> FAIL 并回写输出（结果跟随手动圆，不是保留自动 PASS）。"""
    stack = _stack(tmp_path)
    node, outs, _ctx, si, result = _vision_node_with_result(stack)
    monkeypatch.setattr(node, "_decision",
                        lambda: CircleDecisionConfig(radius_min_px=20, radius_max_px=60))
    info = node.apply_manual_circle(200, 150, 5, operator="张三")
    assert info["status"] == "FAIL"
    assert "不在允许范围" in info["reason"]
    assert outs.get(si, "detect", "status") == "FAIL"
    assert result.status is DetectionStatus.FAIL
    stack.close()


def test_scheme_result_refuses_manual_circle(tmp_path):
    """「运行一次」方案没有检测圆：明确拒绝，不伪造圆。"""
    stack = _stack(tmp_path)
    node, _outs, _ctx, _si, result = _vision_node_with_result(
        stack, pipeline_id="scheme:检测-X")
    result.pipeline_id = "scheme:检测-X"
    state = node.circle_state()
    assert state["available"] is False
    assert "运行一次" in state["reason"]
    with pytest.raises(ValueError, match="运行一次"):
        node.apply_manual_circle(1, 2, 3)
    stack.close()


def test_manual_circle_requires_existing_result(tmp_path):
    stack = _stack(tmp_path)
    ctx = RunContext(run_id="r-empty", versions=VersionSnapshot("x", "v0", "-", "-"))
    node = VisionRunNode("vision:detect", pipeline_id="approved_ds86_circle_pipeline",
                         input_step="capture", step_id="detect", ctx=ctx,
                         executor=stack.executor, outputs=OutputStore())
    assert node.circle_state()["available"] is False
    with pytest.raises(ValueError, match="没有视觉结果"):
        node.apply_manual_circle(1, 2, 3)
    with pytest.raises(ValueError, match="半径"):
        # 有结果才走到半径校验
        _n, _o, _c, _s, _r = _vision_node_with_result(stack)
        _n.apply_manual_circle(1, 2, 0)
    stack.close()


# ---------------------------------------------------------------- 循环跨 tick 回归
def test_bounded_loop_last_iteration_may_span_ticks():
    """回归（DEC-042 暴露）：确认闸让循环体跨 tick；最后一轮也必须继续推进。

    旧实现 ``_iter >= _max`` 时即使子节点还在 RUNNING 也直接返回 RUNNING 且不再
    tick 子节点 -> 整棵树永远"运行中"（任何跨 tick 的循环体步骤都会踩到）。
    """
    from application.workflow.bounded_loop import BoundedLoop

    class _Slow(py_trees.behaviour.Behaviour):
        def __init__(self):
            super().__init__(name="slow")
            self.n = 0

        def update(self):
            self.n += 1
            return Status.SUCCESS if self.n >= 3 else Status.RUNNING

    ctx = RunContext(run_id="r-loop", versions=VersionSnapshot("x", "v0", "-", "-"))
    loop = BoundedLoop("loop", factory=lambda _ictx: [_Slow()],
                       max_iterations=2, ctx=ctx)
    loop.initialise()
    status = Status.RUNNING
    for _ in range(50):
        status = loop.update()
        if status is not Status.RUNNING:
            break
    assert status is Status.SUCCESS
    assert loop.iterations_completed == 2
