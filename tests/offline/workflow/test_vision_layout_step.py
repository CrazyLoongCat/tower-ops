"""视觉步骤多框/多圆布局与引导定位沿用（DEC-048，离线；不接真机）。

覆盖：
- ``VisionRunNode`` 初始布局按检测配置现算（无参数时退化为自动检测圆外接方框）；
- 人工画框/改圆/选择框：判定按选中框的圆重算、事件留证、发布引导目标；
- （伪 scheme）运行一次方案不参与框/圆调整；
- 编译器只在「前面最近的视觉步骤且场景未变」时给引导定位绑定视觉步骤；
- 引导定位节点的覆盖目标按 run/步骤/相机匹配，第一段沿用视觉帧与圆心，
  不匹配时回退正常闭环；
- ``GuidanceService.detect_with_circle`` 不重新抓图找圆，只按给定圆心+同帧深度算
  XYZ/粗精偏移，缺帧/缺深度/缺内参如实失败。
"""
from __future__ import annotations

import time

import numpy as np
import pytest
from py_trees.common import Status

from application.bootstrap.composition import build_offline_stack
from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import DetectionStatus, ImageSpace, NodeType
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
from application.contracts.results import CirclePrimitive, DetectionResult
from application.services.guidance_config import GuidanceConfigStore
from application.services.guidance_service import CircleHit, GuidanceResult, GuidanceService
from application.services.motion_config import MotionConfigStore
from application.services.motion_runner import MotionRunner
from application.services.recipes.models import RecipeModel
from application.services.vision_detections import VisionDetection
from application.workflow.compiler import compile_recipe
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.steps import GuidedPositionNode, VisionRunNode

RECIPE = "configs/recipes/ds86_two_station_example.yaml"
W, H = 640, 480
RAW = 500
SCALE = 0.001


# ---------------------------------------------------------------- 公共构造
def _ctx(run_id: str = "r-vl") -> RunContext:
    return RunContext(run_id=run_id, versions=VersionSnapshot("x", "v0", "-", "-"))


def _frame() -> FrameBundle:
    depth = np.zeros((H, W), dtype="uint16")
    yy, xx = np.mgrid[0:H, 0:W]
    depth[np.hypot(xx - 320.0, yy - 240.0) < 40] = RAW
    color = np.zeros((H, W, 3), dtype="uint8")
    return FrameBundle(
        camera_id="ds86_01", frame_id="f1", capture_id="c1", sequence=1,
        service_id="local", channel_id="ds86_01",
        color=ImageRef.from_array(color), depth=ImageRef.from_array(depth),
        depth_scale_m=SCALE,
        intrinsics=Intrinsics(fx=200.0, fy=200.0, cx=W / 2.0, cy=H / 2.0,
                              width=W, height=H,
                              intrinsics_space=ImageSpace.DEPTH,
                              calibration_version="t"))


def _result(*targets, pipeline_id="approved_ds86_circle_pipeline") -> DetectionResult:
    return DetectionResult(
        result_id="r1", status=DetectionStatus.PASS, capture_id="c1",
        frame_id="f1", run_id="r-vl", station_instance_id="", step_id="det",
        pipeline_id=pipeline_id, image_space=ImageSpace.COLOR,
        targets=list(targets), reason="自动判定")


def _stack(tmp_path):
    return build_offline_stack(db_path=str(tmp_path / "r.db"),
                               runtime_dir=str(tmp_path), recipe_path=RECIPE)


def _node(stack, frame, result, *, detection_ref="det", run_id="r-vl"):
    outs = OutputStore()
    outs.set("", "cap", "frame", frame)
    outs.set("", "det", "result", result)
    outs.set("", "det", "status", result.status.value)
    return VisionRunNode("vision:det", pipeline_id=None, input_step="cap",
                         detection_ref=detection_ref, step_id="det",
                         ctx=_ctx(run_id), executor=stack.executor, outputs=outs)


# ---------------------------------------------------------------- 布局/判定
def test_circle_state_falls_back_and_publishes_guide_target(tmp_path):
    stack = _stack(tmp_path)
    frame = _frame()
    node = _node(stack, frame, _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0),
        CirclePrimitive(cx=120.0, cy=90.0, radius_px=12.0)))
    state = node.circle_state()
    assert state["available"] is True
    assert state["layout_source"] == "result_circle"
    assert len(state["boxes"]) == 2 and state["count"] == 2
    assert state["selected"] == 0
    assert "未保存找圆调试参数" in state["layout_message"]
    target = stack.executor.vision_guide_target
    assert target["run_id"] == "r-vl" and target["step_id"] == "det"
    assert target["camera_id"] == "ds86_01" and target["frame"] is frame
    assert target["circle"]["cx"] == pytest.approx(320.0)
    assert target["manual"] is False
    stack.close()


def test_apply_layout_recomputes_status_and_records_event(tmp_path):
    stack = _stack(tmp_path)
    node = _node(stack, _frame(), _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0)))
    node.circle_state()                       # 触发初始布局
    boxes = [
        {"x1": 260, "y1": 180, "x2": 380, "y2": 300,
         "circle": {"cx": 320, "cy": 240, "radius_px": 25}},
        {"x1": 60, "y1": 60, "x2": 160, "y2": 160,
         "circle": {"cx": 110, "cy": 110, "radius_px": 20}},
    ]
    info = node.apply_layout(boxes, 1, "张三")
    assert info["status"] == DetectionStatus.PASS.value
    assert info["boxes"] == 2 and info["circles"] == 2
    assert info["selected_box"] == 1
    state = node.circle_state()
    assert state["manual"] is True and state["layout_manual"] is True
    assert state["selected"] == 1
    assert state["circle"]["cx"] == pytest.approx(110.0)
    result = node._result()
    assert len(result.targets) == 2 and result.metrics["manual"] == 1.0
    assert stack.executor.vision_guide_target["circle"]["cx"] == pytest.approx(110.0)
    events = [e for e in stack.records.replay("r-vl")
              if e["event_type"] == "vision.manual_layout"]
    assert events and events[-1]["advice"] == "operator=张三"
    assert "2 个圆" in events[-1]["message"]
    stack.close()


def test_apply_layout_without_circles_is_no_target_and_clears_guide(tmp_path):
    stack = _stack(tmp_path)
    node = _node(stack, _frame(), _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0)))
    node.circle_state()
    assert stack.executor.vision_guide_target is not None
    info = node.apply_layout([{"x1": 260, "y1": 180, "x2": 380, "y2": 300,
                               "circle": None}], None, "张三")
    assert info["status"] == DetectionStatus.NO_TARGET.value
    assert "没有检测圆" in info["reason"]
    assert stack.executor.vision_guide_target is None
    state = node.circle_state()
    assert state["count"] == 0 and state["circle"] is None
    assert state["selected"] is None
    stack.close()


def test_add_box_finds_circle_with_host_and_selects_it(tmp_path):
    stack = _stack(tmp_path)
    node = _node(stack, _frame(), _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0)))
    node.circle_state()
    info = node.add_box(300, 220, 340, 260, "张三")
    assert info["boxes"] == 2
    assert info["selected_box"] == 1
    assert info["circle"] is not None
    state = node.circle_state()
    assert state["layout_manual"] is True
    assert state["boxes"][1]["circle"] is not None
    assert stack.executor.vision_guide_target["circle"]["cx"] == pytest.approx(
        info["circle"]["cx"])
    stack.close()


def test_set_box_circle_and_bounds(tmp_path):
    stack = _stack(tmp_path)
    node = _node(stack, _frame(), _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0)))
    node.circle_state()
    info = node.set_box_circle(0, 322, 238, 28, "张三")
    assert info["status"] == DetectionStatus.PASS.value
    assert node.circle_state()["circle"]["cx"] == pytest.approx(322.0)
    with pytest.raises(ValueError, match="超出范围"):
        node.set_box_circle(5, 10, 10, 10)
    with pytest.raises(ValueError, match="半径必须为正"):
        node.set_box_circle(0, 10, 10, 0)
    stack.close()


def test_select_box_moves_guide_circle_without_new_event(tmp_path):
    stack = _stack(tmp_path)
    node = _node(stack, _frame(), _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0),
        CirclePrimitive(cx=120.0, cy=90.0, radius_px=12.0)))
    node.circle_state()
    before = len(stack.records.replay("r-vl"))
    assert node.select_box(1) is True
    assert stack.executor.vision_guide_target["circle"]["cx"] == pytest.approx(120.0)
    node.select_box(None)
    assert stack.executor.vision_guide_target is None
    assert len(stack.records.replay("r-vl")) == before     # 纯选择不留证
    stack.close()


def test_operator_explicit_selection_records_event_and_moves_guide(tmp_path):
    """DEC-054：作业台「定位圆」= 操作员显式指定，选择变化要留证并改引导目标。"""
    stack = _stack(tmp_path)
    node = _node(stack, _frame(), _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0),
        CirclePrimitive(cx=120.0, cy=90.0, radius_px=12.0)))
    node.circle_state()

    def picks():
        return [e for e in stack.records.replay("r-vl")
                if e["event_type"] == "vision.select_target"]

    assert node.select_box(1, "张三") is True
    assert stack.executor.vision_guide_target["circle"]["cx"] == pytest.approx(120.0)
    events = picks()
    assert len(events) == 1
    assert "框2" in events[0]["message"]
    assert events[0]["advice"] == "operator=张三"

    node.select_box(1, "张三")                 # 同一选择重复指定不重复留证
    assert len(picks()) == 1

    node.select_box(None, "张三")              # 显式取消指定也留证
    assert stack.executor.vision_guide_target is None
    events = picks()
    assert len(events) == 2 and "不指定" in events[1]["message"]
    stack.close()


def test_apply_manual_circle_syncs_multi_box_layout(tmp_path):
    stack = _stack(tmp_path)
    node = _node(stack, _frame(), _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0)))
    node.circle_state()
    node.apply_manual_circle(325.0, 236.0, 26.0, "张三")
    state = node.circle_state()
    assert state["manual"] is True and state["layout_manual"] is True
    assert state["circle"]["cx"] == pytest.approx(325.0)
    target = stack.executor.vision_guide_target
    assert target["circle"] == {"cx": pytest.approx(325.0),
                                "cy": pytest.approx(236.0),
                                "radius_px": pytest.approx(26.0)}
    stack.close()


def test_scheme_result_refuses_layout_adjustment(tmp_path):
    stack = _stack(tmp_path)
    node = _node(stack, _frame(), _result(pipeline_id="scheme:检测-ds86_01"))
    state = node.circle_state()
    assert state["available"] is False and "运行一次" in state["reason"]
    with pytest.raises(ValueError, match="运行一次"):
        node.apply_layout([], None)
    stack.close()


def test_detection_circle_debug_params_drive_initial_layout(tmp_path):
    """命名检测保存了找圆调试参数时，初始框按深度引导现算（不是自动圆外接方框）。"""
    stack = _stack(tmp_path)
    depth = {"morph_op": "none", "thresh_method": "binary_inv",
             "thresh_value": 127, "depth_min_mm": 200.0,
             "depth_max_mm": 3000.0, "rect_side_length": 100.0}
    stack.vision_detections.add(VisionDetection(
        detection_id="检测-ds86_01", camera_ref="ds86_01",
        pipeline_ref="approved_ds86_circle_pipeline",
        params={"circle_debug": {"depth": depth, "circle": {}}}))
    node = _node(stack, _frame(), _result(
        CirclePrimitive(cx=320.0, cy=240.0, radius_px=30.0)),
        detection_ref="检测-ds86_01")
    state = node.circle_state()
    assert state["layout_source"] == "circle_debug"
    assert state["boxes"] and "按检测配置现算" in state["layout_message"]
    box = state["boxes"][0]
    assert box["circle"] is not None
    assert abs(box["circle"]["cx"] - 320) < 15 and abs(box["circle"]["cy"] - 240) < 15
    stack.close()


def test_ui_saved_detection_runs_circle_chain_and_shows_boxes(tmp_path):
    """界面另存为检测的快照**永远带 scheme**（哪怕没画多边形）。有找圆调试参数时
    视觉步骤必须先走「深度分割 → 每框找圆」链，作业台右卡才能显示框/圆
    （DEC-057：用户 2026-10-21 报告"判定后框/圆不显示、运行一次却有两框两圆"）。

    这里模拟真实快照：``params`` 同时有 ``scheme`` 与 ``circle_debug``。
    """
    from application.algorithms.segmentation import RunOnceScheme

    stack = _stack(tmp_path)
    depth = {"morph_op": "none", "thresh_method": "binary_inv",
             "thresh_value": 127, "depth_min_mm": 200.0,
             "depth_max_mm": 3000.0, "rect_side_length": 100.0}
    scheme = RunOnceScheme(
        source="color", op_region="whole", polygon=[],
        depth_params=depth,
    ).to_dict()
    stack.vision_detections.add(VisionDetection(
        detection_id="检测-带方案", camera_ref="ds86_01",
        pipeline_ref="approved_ds86_circle_pipeline",
        params={"scheme": scheme,
                "circle_debug": {"depth": depth, "circle": {}}}))
    outs = OutputStore()
    outs.set("", "cap", "frame", _frame())
    node = VisionRunNode("vision:det", pipeline_id=None, input_step="cap",
                         detection_ref="检测-带方案", step_id="det",
                         ctx=_ctx("r-scheme-order"), executor=stack.executor,
                         outputs=outs)
    assert node.update() is Status.SUCCESS
    result = node.outputs.get("", "det", "result")
    # 走了找圆链，不是 scheme 分支
    assert not str(result.pipeline_id).startswith("scheme:")
    assert result.quality.get("vision_mode") == "depth_rects"
    # 作业台右卡可显示框/圆
    state = node.circle_state()
    assert state["available"] is True
    assert state["layout_source"] == "circle_debug"
    assert state["boxes"]
    assert any(b.get("circle") for b in state["boxes"])
    stack.close()


# ---------------------------------------------------------------- 编译器绑定
def _recipe(sequence) -> RecipeModel:
    return RecipeModel.model_validate({
        "recipe_id": "r", "version": "v1", "state": "draft",
        "sequence": sequence})


_CAP = {"id": "cap", "type": "camera.capture", "camera_ref": "ds86_01"}
_DET = {"id": "det", "type": "vision.run",
        "input_from": {"step": "cap", "output": "frame"},
        "pipeline_ref": "approved_ds86_circle_pipeline"}
_GUIDE = {"id": "g", "type": "vision.guided_position", "camera_ref": "ds86_01"}


def _compile(tmp_path, sequence, step_ids=("g",)):
    stack = _stack(tmp_path)
    compiled = compile_recipe(
        _recipe(sequence), _ctx("r-compile"), stack.executor, joint_targets={},
        detections={"det": "approved_ds86_circle_pipeline"}, motion=None)
    out = [compiled.find_step(sid) for sid in step_ids]
    stack.close()
    return out


def test_compiler_binds_nearest_vision_step(tmp_path):
    guides = _compile(tmp_path, [_CAP, _DET, _GUIDE])
    assert guides[0].vision_step_ref == "det"


def test_compiler_does_not_bind_across_scene_change(tmp_path):
    cap2 = {"id": "cap2", "type": "camera.capture", "camera_ref": "ds86_01"}
    guides = _compile(tmp_path, [_CAP, _DET, cap2, _GUIDE])
    assert guides[0].vision_step_ref == ""


def test_compiler_does_not_reuse_vision_after_guide(tmp_path):
    g2 = {"id": "g2", "type": "vision.guided_position", "camera_ref": "ds86_01"}
    guides = _compile(tmp_path, [_CAP, _DET, _GUIDE, g2], ("g", "g2"))
    assert [g.vision_step_ref for g in guides] == ["det", ""]


# ---------------------------------------------------------------- 引导定位沿用
class FakeGuidanceAuto:
    """只有正常闭环 detect() 的引导服务（旧假执行器）。"""

    def __init__(self) -> None:
        self.camera_id = "ds86_01"
        self.calls = 0

    def detect(self) -> GuidanceResult:
        self.calls += 1
        return GuidanceResult(True, "检测成功", cam_xyz=(12.0, 3.0, 500.0),
                              coarse=(10.0, -5.0), fine=(1.0, 0.5),
                              circle=CircleHit(0, 12.0, 3.0, 25.0),
                              used_depth_mm=500.0)


class FakeGuidanceWithCircle(FakeGuidanceAuto):
    def __init__(self) -> None:
        super().__init__()
        self.override_calls: list[dict] = []

    def detect_with_circle(self, *, frame, cx, cy, radius_px=0.0):
        self.override_calls.append({"frame": frame, "cx": cx, "cy": cy,
                                    "radius_px": radius_px})
        return GuidanceResult(True, "按视觉步骤圆心检测成功",
                              cam_xyz=(15.0, 4.0, 500.0), coarse=(2.0, 0.5),
                              fine=(0.1, 0.0),
                              circle=CircleHit(0, cx, cy, radius_px),
                              used_depth_mm=500.0)


class FakeArm:
    def __init__(self) -> None:
        self.connected = True
        self.offsets: list[tuple] = []

    def move_offset(self, off, v=20, r=0, connect=0, block=1, stop_check=None,
                    tol=0.002, timeout=30.0):
        self.offsets.append((tuple(off), v))
        return 0

    def ensure_production_frames(self):
        return (True, [])

    def stop(self):
        return 0


def _gate_on(monkeypatch) -> None:
    """DEC-085：real 模式无环境变量闸，保留空函数避免改动大量调用点。"""
    return None


def _runner(tmp_path, guidance) -> MotionRunner:
    st = MotionConfigStore(str(tmp_path / "motion.yaml"), seed_if_missing=False)
    st.load()
    return MotionRunner(FakeArm(), st, guidance=guidance)


def _target(frame_obj, *, run_id="r-guide", step_id="det",
            camera_id="ds86_01", circle=None):
    return {
        "run_id": run_id, "step_id": step_id, "camera_id": camera_id,
        "frame": frame_obj if frame_obj is not _NO_FRAME else None,
        "circle": circle if circle is not None else
        {"cx": 345.0, "cy": 260.0, "radius_px": 30.0},
        "manual": True, "source": "manual",
    }


_NO_FRAME = object()


def _guide_node(runner, stack, **kw) -> GuidedPositionNode:
    return GuidedPositionNode("guide:g", motion=runner, step_id="g",
                              ctx=_ctx("r-guide"), executor=stack.executor,
                              outputs=OutputStore(), **kw)


def _drive(node, *, answer="是", timeout=8.0):
    node.initialise()
    end = time.monotonic() + timeout
    status = node.update()
    while status is Status.RUNNING and time.monotonic() < end:
        p = node.pending_prompt
        if p:
            assert node.provide(answer, "测试员") is True
        time.sleep(0.02)
        status = node.update()
    return status


def test_guide_first_stage_uses_vision_frame_and_circle(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    guide = FakeGuidanceWithCircle()
    runner = _runner(tmp_path, guide)
    frame = _frame()
    stack.executor.vision_guide_target = _target(frame)
    node = _guide_node(runner, stack, camera_ref="ds86_01",
                       stages=("coarse",), vision_step_ref="det")
    assert _drive(node) is Status.SUCCESS
    assert guide.calls == 0                       # 不重新走闭环抓图找圆
    assert len(guide.override_calls) == 1
    call = guide.override_calls[0]
    assert call["frame"] is frame
    assert (call["cx"], call["cy"], call["radius_px"]) == (345.0, 260.0, 30.0)
    rows = node.outputs.get("", "g", "guide")
    assert rows[0]["source"] == "vision_step"
    stack.close()


@pytest.mark.parametrize("patch", [
    {"step_id": "other"},
    {"camera_id": "d405_01"},
    {"run_id": "r-old"},
    {"frame_obj": _NO_FRAME},
    {"circle": {}},
])
def test_guide_falls_back_to_auto_on_mismatch(tmp_path, monkeypatch, patch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    guide = FakeGuidanceWithCircle()
    runner = _runner(tmp_path, guide)
    target = _target(*([_NO_FRAME] if patch.get("frame_obj") is _NO_FRAME
                       else [_frame()]),
                     **{k: v for k, v in patch.items() if k != "frame_obj"})
    stack.executor.vision_guide_target = target
    node = _guide_node(runner, stack, camera_ref="ds86_01",
                       stages=("coarse",), vision_step_ref="det")
    assert _drive(node) is Status.SUCCESS
    assert guide.calls == 1 and guide.override_calls == []
    assert node.outputs.get("", "g", "guide")[0]["source"] == "auto"
    stack.close()


def test_guide_without_vision_step_ref_never_overrides(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    guide = FakeGuidanceWithCircle()
    runner = _runner(tmp_path, guide)
    stack.executor.vision_guide_target = _target(_frame())
    node = _guide_node(runner, stack, camera_ref="ds86_01", stages=("coarse",))
    assert _drive(node) is Status.SUCCESS
    assert guide.calls == 1 and guide.override_calls == []
    stack.close()


# ---------------------------------------------------------------- 按圆心定位
def _guidance_store(tmp_path) -> GuidanceConfigStore:
    st = GuidanceConfigStore(str(tmp_path / "g.yaml"), seed_if_missing=False)
    st.load()
    return st


def test_detect_with_circle_uses_frame_depth_without_recapture(tmp_path):
    st = _guidance_store(tmp_path)
    svc = GuidanceService(st, camera=None, host_proxy=None)
    res = svc.detect_with_circle(frame=_frame(), cx=320.0, cy=240.0,
                                 radius_px=30.0)
    assert res.ok is True
    assert res.cam_xyz[2] == pytest.approx(500.0, abs=2.0)
    assert res.cam_xyz[0] == pytest.approx(0.0, abs=0.5)
    assert res.circle.cx == pytest.approx(320.0)
    assert res.rects == []
    assert "视觉步骤圆心" in res.message


def test_detect_with_circle_off_center_gives_offsets(tmp_path):
    st = _guidance_store(tmp_path)
    svc = GuidanceService(st, camera=None, host_proxy=None)
    res = svc.detect_with_circle(frame=_frame(), cx=340.0, cy=240.0,
                                 radius_px=30.0)
    assert res.ok is True
    assert abs(res.cam_xyz[0]) > 1.0
    assert res.coarse is not None and any(abs(v) > 0.1 for v in res.coarse)


def test_detect_with_circle_reports_missing_inputs(tmp_path):
    st = _guidance_store(tmp_path)
    svc = GuidanceService(st, camera=None, host_proxy=None)
    assert svc.detect_with_circle(frame=None, cx=1, cy=1).ok is False
    frame = _frame()
    frame.depth = None
    r = svc.detect_with_circle(frame=frame, cx=1, cy=1)
    assert r.ok is False and "深度" in r.message
    frame2 = _frame()
    frame2.intrinsics = None
    r2 = svc.detect_with_circle(frame=frame2, cx=1, cy=1)
    assert r2.ok is False and "内参" in r2.message
    r3 = svc.detect_with_circle(frame=_frame(), cx=float("nan"), cy=1)
    assert r3.ok is False and "非法" in r3.message


# ---------------------------------------------------------------- DEC-063 精定位选圆
class FakeGuidanceFine(FakeGuidanceWithCircle):
    """提供 detect_fine 的服务：验证精定位走“光心最近”通道。"""

    def __init__(self) -> None:
        super().__init__()
        self.fine_calls = 0

    def detect_fine(self, fine_profile_ref=None):
        self.fine_calls += 1
        self.last_fine_profile = fine_profile_ref
        return GuidanceResult(True, "精定位检测成功", cam_xyz=(0.5, 0.2, 500.0),
                              coarse=(0.0, 0.0), fine=(0.1, 0.0),
                              circle=CircleHit(0, 160.0, 120.0, 28.0),
                              used_depth_mm=500.0)


class FakeGuidanceOverrideBoom(FakeGuidanceAuto):
    """按视觉圆心检测直接抛错：不得静默回退到自动找圆。"""

    def detect_with_circle(self, *, frame, cx, cy, radius_px=0.0):
        raise RuntimeError("注入: 按圆心检测失败")


def test_guide_fine_stage_uses_selected_fine_profile(tmp_path, monkeypatch):
    """DEC-065：引导定位步骤选择的精定位参数套传给 detect_fine。"""
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    guide = FakeGuidanceFine()
    runner = _runner(tmp_path, guide)
    stack.executor.vision_guide_target = _target(_frame())
    node = _guide_node(runner, stack, camera_ref="ds86_01",
                       stages=("coarse", "fine"), vision_step_ref="det",
                       fine_profile_ref="第二套")
    assert _drive(node) is Status.SUCCESS
    assert guide.last_fine_profile == "第二套"
    stack.close()


def test_guide_fine_stage_uses_detect_fine_not_first_auto_circle(tmp_path, monkeypatch):
    """DEC-063：粗定位沿用操作员选圆；精定位重新检测时走 detect_fine（离光心最近）。"""
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    guide = FakeGuidanceFine()
    runner = _runner(tmp_path, guide)
    stack.executor.vision_guide_target = _target(_frame())
    node = _guide_node(runner, stack, camera_ref="ds86_01",
                       stages=("coarse", "fine"), vision_step_ref="det")
    assert _drive(node) is Status.SUCCESS
    assert len(guide.override_calls) == 1         # 粗定位用操作员选的圆
    assert guide.fine_calls == 1                  # 精定位走 detect_fine
    assert guide.calls == 0                       # 没有走普通 detect()（第一个自动圆）
    stack.close()


def test_guide_override_detection_failure_never_falls_back_to_auto(tmp_path, monkeypatch):
    """操作员选了圆后，按圆检测失败必须如实失败，不能改按自动圆移动（DEC-063）。"""
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    guide = FakeGuidanceOverrideBoom()
    runner = _runner(tmp_path, guide)
    stack.executor.vision_guide_target = _target(_frame())
    node = _guide_node(runner, stack, camera_ref="ds86_01",
                       stages=("coarse",), vision_step_ref="det")
    assert _drive(node) is Status.FAILURE
    assert guide.calls == 0                       # 没有回退自动找圆
    assert runner.arm.offsets == []               # 也没有产生动作
    assert "操作员选择的圆心" in (node.failure_reason or "")
    stack.close()


def test_validation_rejects_unknown_fine_offset_profile(monkeypatch):
    """DEC-065：引导定位步骤引用未知精定位参数套要在校验期报错。"""
    from application.services.recipes.models import RecipeModel
    from application.services.recipes.service import ReferenceCatalog, validate_recipe
    model = RecipeModel.model_validate({
        "recipe_id": "r", "version": "v", "state": "draft",
        "sequence": [{"id": "g", "type": "vision.guided_position",
                      "camera_ref": "ds86_01", "fine_profile_ref": "不存在"}]})
    report = validate_recipe(model, ReferenceCatalog(cameras={"ds86_01"}))
    assert any("未知精定位参数套" in e for e in report.errors)
