"""E20×运动方案/视觉调试 接线 (DEC-024): 步骤绑定运动方案 + 引用命名视觉检测。

- ``robot.move_joint`` 带 ``motion_plan_ref`` -> :class:`MovePlanNode` 由 ``MotionRunner``
  回放方案路点; **离线一律拒绝**(与「运动方案」页手动运行同一闸), 按失败结束, 不留假动作。
- ``vision.run`` 带 ``detection_ref`` -> 编译期解析成流水线; 本栈只有一条流水线,
  要求切换时明确失败, 不拿另一条冒充。
"""
from __future__ import annotations

import pytest
from py_trees.common import Status

from application.bootstrap.composition import build_offline_stack
from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import TaskState
from application.services.motion_config import (
    MotionConfigStore, MotionPlan, Waypoint,
)
from application.services.vision_detections import VisionDetection, VisionDetectionStore
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.steps import MovePlanNode, VisionRunNode

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


def _node(compiled, step_id):
    return next(n for k, n in compiled.nodes.items() if k.endswith("/" + step_id))


def _ctx() -> RunContext:
    return RunContext(run_id="r", versions=VersionSnapshot("x", "v0", "-", "-"))


def _motion_file(tmp_path, plan_id: str, n_points: int = 2) -> str:
    """自建一份运动配置（**不动用户的活配置**）：含一个可绑定的命名方案。"""
    path = str(tmp_path / "motion.yaml")
    store = MotionConfigStore(path)
    store.load()
    store.config.plans.append(MotionPlan(
        plan_id=plan_id, desc="离线用例方案", default_mode="movej",
        waypoints=[Waypoint("movej", (10.0, 20.0, 30.0, 0.0, 90.0, 0.0))
                   for _ in range(n_points)]))
    store.save()
    return path


# ---------------------------------------------------------------- 运动方案绑定
def test_move_step_bound_to_motion_plan_offline_refuses_motion(tmp_path):
    """绑定运动方案的 move 步骤离线被拒: 编译成 MovePlanNode, 运行按失败结束。"""
    plan_id = "用例方案-离线"
    stack = _stack(tmp_path, motion_config_path=_motion_file(tmp_path, plan_id))
    rid = "bound_plan_offline"
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "mv", "type": "robot.move_joint",
             "joint_target_ref": "teach_01",
             "motion_plan_ref": plan_id},
        ],
    })
    stack.task.start(rid, allow_draft=True)

    node = _node(stack.task.compiled, "mv")
    assert isinstance(node, MovePlanNode)
    assert node.plan_id == plan_id

    assert _run_to_end(stack) is TaskState.FAULT
    # 失败原因要说清「被拒」以及为什么（离线未开运动闸/未连接），不能含糊
    assert plan_id in (node.failure_reason or "")
    assert "拒" in (node.failure_reason or "")
    stack.close()


def _socket_plan_file(tmp_path, plan_id: str) -> str:
    """自建一份**只含套筒偏移**的运动配置（不动用户的活配置）。"""
    path = str(tmp_path / "motion.yaml")
    store = MotionConfigStore(path)
    store.load()
    store.config.plans.append(MotionPlan(
        plan_id=plan_id, desc="套筒偏移方案", default_mode="movel",
        waypoints=[Waypoint("socket_offset", (0, 10, 0, 0, 0, 0)),
                   Waypoint("socket_offset", (0, -10, 0, 0, 0, 0))]))
    store.save()
    return path


def test_move_step_bound_to_socket_plan_is_referenceable(tmp_path):
    """配方步骤可引用**只含套筒偏移的方案**(无需关节目标): 编译成 MovePlanNode；
    离线执行按失败结束并给出可读原因（与手动运行同一闸, 不留假动作）。"""
    plan_id = "取套筒-离线"
    stack = _stack(tmp_path, motion_config_path=_socket_plan_file(tmp_path, plan_id))
    rid = "socket_plan_offline"
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "pickup", "type": "robot.move_joint", "motion_plan_ref": plan_id},
        ],
    })
    stack.task.start(rid, allow_draft=True)

    node = _node(stack.task.compiled, "pickup")
    assert isinstance(node, MovePlanNode)
    assert node.plan_id == plan_id
    assert _run_to_end(stack) is TaskState.FAULT
    assert plan_id in (node.failure_reason or "")
    assert "拒" in (node.failure_reason or "")
    stack.close()


def test_move_plan_node_without_runner_fails_clearly(tmp_path):
    """没接运动方案执行器时, 绑定步明确报错, 不静默跳过。"""
    stack = _stack(tmp_path)
    node = MovePlanNode("move:x", motion=None, plan_id="whatever",
                        step_id="x", ctx=_ctx(), executor=stack.executor,
                        outputs=OutputStore())
    assert node.update() is Status.FAILURE
    assert "运动方案执行器" in (node.failure_reason or "")
    stack.close()


# ---------------------------------------------------------------- 视觉检测引用
def _detections_file(tmp_path, detection_id: str, pipeline: str) -> str:
    path = str(tmp_path / "detections.yaml")
    store = VisionDetectionStore(path)
    store.load()
    store.upsert(VisionDetection(detection_id=detection_id, camera_ref="ds86_01",
                                 pipeline_ref=pipeline, source="test"))
    store.save()
    return path


def test_vision_step_resolves_named_detection_to_pipeline(tmp_path):
    """vision.run 引用命名检测: 编译解析出流水线, 运行完成并记下用的检测名。"""
    det_path = _detections_file(tmp_path, "检测-A", "approved_ds86_circle_pipeline")
    stack = _stack(tmp_path, vision_detections_path=det_path)
    rid = "det_ref_offline"
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "capture", "type": "camera.capture", "camera_ref": "ds86_01"},
            {"id": "detect", "type": "vision.run",
             "input_from": {"step": "capture", "output": "frame"},
             "detection_ref": "检测-A"},
            {"id": "gate", "type": "condition",
             "predicate": {"source": {"step": "detect", "field": "status"},
                           "op": "eq", "value": "PASS"},
             "on_false": {"policy": "pause_for_review"}},
        ],
    })
    stack.task.start(rid, allow_draft=True)

    node = _node(stack.task.compiled, "detect")
    assert isinstance(node, VisionRunNode)
    assert node.detection_ref == "检测-A"
    assert node.pipeline_id == "approved_ds86_circle_pipeline"

    assert _run_to_end(stack) is TaskState.COMPLETED
    si = node.ctx.station_instance_id
    assert stack.task.compiled.outputs.get(si, "detect", "detection") == "检测-A"
    stack.close()


def test_vision_node_refuses_pipeline_switch(tmp_path):
    """配方要求的流水线与本栈唯一流水线不符时明确失败, 不拿另一条冒充。"""
    stack = _stack(tmp_path)
    ctx = _ctx()
    outs = OutputStore()
    outs.set(ctx.station_instance_id or "", "cap", "frame", object())
    node = VisionRunNode("vision:x", pipeline_id="other_pipeline", input_step="cap",
                         step_id="x", ctx=ctx, executor=stack.executor, outputs=outs)
    assert node.update() is Status.FAILURE
    assert "流水线" in (node.failure_reason or "")
    stack.close()


# ----------------------------------------------- 视觉检测携带「运行一次方案」(DEC-026)
def _scheme_dict():
    from application.algorithms.guidance.depth_guidance import DepthGuidanceParams
    from application.algorithms.segmentation import RunOnceScheme
    return RunOnceScheme(
        source="color", op_region="whole",
        polygon=[[100.0, 100.0], [500.0, 100.0], [500.0, 400.0], [100.0, 400.0]],
        depth_params=DepthGuidanceParams().to_dict(),
        seg_filter_mode="所有轮廓", seg_filter_n=1, prompt_k=3, prompt_margin=5.0,
    ).to_dict()


def _detections_file_with_scheme(tmp_path, detection_id, pipeline, scheme) -> str:
    path = str(tmp_path / "detections.yaml")
    store = VisionDetectionStore(path)
    store.load()
    store.upsert(VisionDetection(
        detection_id=detection_id, label=detection_id, camera_ref="ds86_01",
        pipeline_ref=pipeline, source="test",
        params={"scheme": scheme, "pipeline": {"pipeline_id": pipeline}}))
    store.save()
    return path


def _recipe_with_detection(rid: str, detection_id: str) -> dict:
    return {
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "capture", "type": "camera.capture", "camera_ref": "ds86_01"},
            {"id": "detect", "type": "vision.run",
             "input_from": {"step": "capture", "output": "frame"},
             "detection_ref": detection_id},
            {"id": "gate", "type": "condition",
             "predicate": {"source": {"step": "detect", "field": "status"},
                           "op": "eq", "value": "PASS"},
             "on_false": {"policy": "pause_for_review"}},
        ],
    }


def test_vision_step_with_saved_scheme_runs_run_once(monkeypatch, tmp_path):
    """检测带「运行一次方案」时, vision.run 在真实帧上重跑算法(非流水线), 结果供条件判定。"""
    from application.algorithms.segmentation import StubSegmenter
    # 不加载真实 EdgeSAM: 注入确定性占位(判据不依赖 ~40MB 模型在不在), 结果如实标注
    monkeypatch.setattr("application.algorithms.segmentation.default_segmenter",
                        lambda *, offline: (StubSegmenter(), "离线占位（测试）"))
    det_path = _detections_file_with_scheme(
        tmp_path, "检测-方案", "approved_ds86_circle_pipeline", _scheme_dict())
    stack = _stack(tmp_path, vision_detections_path=det_path)
    rid = "scheme_ref_offline"
    stack.recipes.draft(_recipe_with_detection(rid, "检测-方案"))
    stack.task.start(rid, allow_draft=True)

    node = _node(stack.task.compiled, "detect")
    assert isinstance(node, VisionRunNode)
    assert node.detection_ref == "检测-方案"

    assert _run_to_end(stack) is TaskState.COMPLETED
    si = node.ctx.station_instance_id
    result = stack.task.compiled.outputs.get(si, "detect", "result")
    assert result.status.value == "PASS"
    assert result.host == "run_once"                     # 走的是运行一次路径, 不是流水线
    assert result.pipeline_id == "scheme:检测-方案"
    assert result.metrics["intersection_area_px"] > 0
    assert "非真实模型" in result.quality["segmenter"]   # 后端如实标注, 不冒充模型
    assert result.quality["source"] == "color"
    stack.close()


def test_vision_step_with_scheme_and_no_segmenter_reports_error(monkeypatch, tmp_path):
    """智能分割不可用(真机缺 onnxruntime/模型)时: 运行一次方案返回 ERROR, 步骤失败, 不冒充。"""
    monkeypatch.setattr("application.algorithms.segmentation.default_segmenter",
                        lambda *, offline: (None, "onnxruntime 未安装"))
    det_path = _detections_file_with_scheme(
        tmp_path, "检测-无后端", "approved_ds86_circle_pipeline", _scheme_dict())
    stack = _stack(tmp_path, vision_detections_path=det_path)
    rid = "scheme_no_backend"
    stack.recipes.draft(_recipe_with_detection(rid, "检测-无后端"))
    stack.task.start(rid, allow_draft=True)

    node = _node(stack.task.compiled, "detect")
    assert _run_to_end(stack) is TaskState.FAULT
    # DEC-052: 失败原因带底层原因，不再是一句笼统的"视觉 ERROR"
    assert "视觉判定失败" in (node.failure_reason or "")
    assert "智能分割不可用" in (node.failure_reason or "")
    result = stack.task.compiled.outputs.get(
        node.ctx.station_instance_id, "detect", "result")
    assert result.status.value == "ERROR"
    assert "智能分割不可用" in (result.reason or "")
    stack.close()
