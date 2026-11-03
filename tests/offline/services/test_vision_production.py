"""DEC-056：配方视觉步骤按「深度分割矩形 → 每框找圆」判定（离线；不接真机）。

覆盖：
- 命名检测保存了 ``circle_debug``（depth 段）时，``run_depth_rects_vision``
  走找圆调试同一条链：深度分割 → 每矩形找圆（每框第一个）→ 判定/目标；
- 判定分支：PASS / 框内缺圆 FAIL / 半径不合格 FAIL / 数量超范围 FAIL /
  无矩形与无圆 NO_TARGET / 缺深度、缺彩色、宿主不可用 ERROR / 取消 CANCELLED；
- ``ActionExecutor.run_vision`` 按 detection_id 取参数：有参数不调整图流水线，
  旧检测（无 depth 段）回落整图流水线；
- ``VisionRunNode`` 把 detection_ref 作为 detection_id 传给执行器。
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
from py_trees.common import Status

from application.algorithms.circle.detector import (
    CircleDecisionConfig,
    InProcessStubBackend,
    native_from_circles,
)
from application.algorithms.circle.params import CircleParams
from application.algorithms.circle.pipeline import CirclePipeline, CirclePipelineSpec
from application.algorithms.guidance.depth_guidance import DepthGuidanceParams
from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import DetectionStatus, ImageSpace
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
from application.contracts.results import DetectionResult
from application.services.actions import ActionExecutor
from application.services.vision_production import (
    VISION_MODE,
    depth_circle_debug,
    run_depth_rects_vision,
)
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.steps import VisionRunNode

W, H = 320, 240
RAW_DISC = 500
SCALE = 0.001


# ---------------------------------------------------------------- 帧与宿主
def _depth_with_discs(centers) -> np.ndarray:
    depth = np.zeros((H, W), dtype="uint16")
    yy, xx = np.mgrid[0:H, 0:W]
    for cx, cy, r in centers:
        depth[np.hypot(xx - cx, yy - cy) < r] = RAW_DISC
    return depth


def _frame(centers=((160, 120, 30),), *, color=True, depth=True) -> FrameBundle:
    kwargs = dict(
        camera_id="ds86_01", frame_id="f1", capture_id="c1", sequence=1,
        service_id="local", channel_id="ds86_01",
        depth_scale_m=SCALE,
        intrinsics=Intrinsics(fx=200.0, fy=200.0, cx=W / 2.0, cy=H / 2.0,
                              width=W, height=H,
                              intrinsics_space=ImageSpace.DEPTH,
                              calibration_version="t"))
    if color:
        kwargs["color"] = ImageRef.from_array(np.zeros((H, W, 3), dtype="uint8"))
    if depth:
        kwargs["depth"] = ImageRef.from_array(_depth_with_discs(centers))
    return FrameBundle(**kwargs)


def _debug_params(**overrides) -> dict:
    depth = DepthGuidanceParams(morph_op="none", thresh_method="binary_inv",
                                thresh_value=127, depth_min_mm=200.0,
                                depth_max_mm=3000.0,
                                rect_side_length=100.0).to_dict()
    depth.update(overrides)
    return {"schema_version": 1, "depth": depth, "circle": {}}


class RectHost:
    """假 cp38 宿主：以请求矩形的中心为圆心返回一个圆（模拟框内找圆）。"""

    def __init__(self, radius: float = 28.0, only_first: bool = False) -> None:
        self.radius = radius
        self.only_first = only_first
        self.calls: list = []

    def detect(self, images, rects, params) -> dict:
        self.calls.append((len(images), [list(r) for r in rects]))
        if self.only_first and len(self.calls) > 1:
            return native_from_circles([])
        x, y, w, h = rects[0][1], rects[0][2], rects[0][3], rects[0][4]
        return native_from_circles([(x + w / 2.0, y + h / 2.0, self.radius)])


class EmptyHost(RectHost):
    def detect(self, images, rects, params) -> dict:
        self.calls.append((len(images), [list(r) for r in rects]))
        return native_from_circles([])


def _run(**kwargs):
    opts = dict(circle_debug=_debug_params(), result_id="rid",
                decision=CircleDecisionConfig(radius_min_px=20.0,
                                              radius_max_px=60.0),
                pipeline_id="approved_ds86_circle_pipeline",
                pipeline_version="offline-v1", host="cp38-native")
    opts.update(kwargs)
    frame = opts.pop("frame", _frame())
    return run_depth_rects_vision(frame, **opts)


# ---------------------------------------------------------------- 判定分支
def test_passes_with_one_circle_per_box():
    frame = _frame(centers=((90, 120, 30), (230, 120, 30)))
    host = RectHost()
    result = _run(frame=frame, host_proxy=host)
    assert result.status is DetectionStatus.PASS
    assert result.quality["vision_mode"] == VISION_MODE
    assert result.metrics["boxes"] >= 2
    assert result.metrics["boxes_with_circle"] == result.metrics["boxes"]
    assert result.metrics["passing_count"] == result.metrics["candidate_count"]
    assert len(result.targets) == int(result.metrics["boxes"])
    assert len(host.calls) == int(result.metrics["boxes"])
    assert "框内圆均合格" in result.reason
    assert result.pipeline_id == "approved_ds86_circle_pipeline"


def test_no_rects_is_no_target():
    result = _run(frame=_frame(centers=()), host_proxy=RectHost())
    assert result.status is DetectionStatus.NO_TARGET
    assert "深度分割未找到候选矩形" in result.reason
    assert result.metrics["boxes"] == 0


def test_no_circles_in_any_box_is_no_target():
    result = _run(host_proxy=EmptyHost())
    assert result.status is DetectionStatus.NO_TARGET
    assert "均未找到圆" in result.reason


def test_missing_circle_in_one_box_fails():
    frame = _frame(centers=((90, 120, 30), (230, 120, 30)))
    host = RectHost(only_first=True)
    result = _run(frame=frame, host_proxy=host)
    assert result.status is DetectionStatus.FAIL
    assert result.metrics["boxes_without_circle"] >= 1
    assert "未找到圆" in result.reason
    assert len(host.calls) >= 2


def test_radius_out_of_range_fails():
    result = _run(host_proxy=RectHost(radius=28.0),
                  decision=CircleDecisionConfig(radius_min_px=100.0,
                                                radius_max_px=200.0))
    assert result.status is DetectionStatus.FAIL
    assert "不满足半径/完整度判据" in result.reason


def test_count_out_of_expected_range_fails():
    frame = _frame(centers=((90, 120, 30), (230, 120, 30)))
    result = _run(frame=frame, host_proxy=RectHost(),
                  decision=CircleDecisionConfig(expected_count_max=1,
                                                radius_min_px=20.0,
                                                radius_max_px=60.0))
    assert result.status is DetectionStatus.FAIL
    assert "超出期望范围" in result.reason


def test_missing_depth_is_error_not_fake():
    result = _run(frame=_frame(depth=False), host_proxy=RectHost())
    assert result.status is DetectionStatus.ERROR
    assert "深度分割失败" in result.reason
    assert result.errors and "深度" in result.errors[0]


def test_missing_color_is_error():
    result = _run(frame=_frame(color=False), host_proxy=RectHost())
    assert result.status is DetectionStatus.ERROR
    assert result.reason == "缺彩色原图"


def test_host_unavailable_is_error_not_fake():
    result = _run(host_proxy=None)
    assert result.status is DetectionStatus.ERROR
    assert "找圆不可用" in result.reason


def test_cancelled_wins_before_depth():
    result = _run(frame=_frame(depth=False), host_proxy=None, cancelled=True)
    assert result.status is DetectionStatus.CANCELLED
    assert result.reason == "取消"


def test_depth_circle_debug_requires_depth_section():
    assert depth_circle_debug(None) is None
    assert depth_circle_debug({}) is None
    assert depth_circle_debug({"circle_debug": {}}) is None
    assert depth_circle_debug({"circle_debug": {"circle": {}}}) is None
    debug = {"depth": {}, "circle": {}}
    assert depth_circle_debug({"circle_debug": debug}) is debug


# ---------------------------------------------------------------- 执行器接线
class _Store:
    def __init__(self, params) -> None:
        self._params = params

    def find(self, detection_id):
        if self._params is None:
            return None
        return SimpleNamespace(params=self._params)


class _BrokenStore:
    def find(self, detection_id):
        raise RuntimeError("存储读取失败")


def _pipeline(backend, decision=None) -> CirclePipeline:
    return CirclePipeline(
        CirclePipelineSpec(
            pipeline_id="approved_ds86_circle_pipeline", version="offline-v1",
            params=CircleParams(),
            decision=decision or CircleDecisionConfig(radius_min_px=20.0,
                                                      radius_max_px=60.0),
            host="cp38-native"),
        backend)


def _executor(backend, *, params, host_proxy=None) -> ActionExecutor:
    return ActionExecutor(interlocks=None, records=None, vision=_pipeline(backend),
                          vision_detections=_Store(params), host_proxy=host_proxy)


def _ctx() -> RunContext:
    return RunContext(run_id="r-dec056",
                      versions=VersionSnapshot("x", "v0", "-", "-"))


def test_run_vision_uses_depth_rects_when_detection_has_params():
    backend = InProcessStubBackend(native_from_circles([(10.0, 10.0, 5.0)]))
    host = RectHost()
    executor = _executor(backend, params={"circle_debug": _debug_params()},
                         host_proxy=host)
    result = executor.run_vision(_ctx(), _frame(), result_id="rid",
                                 detection_id="det")
    assert result.status is DetectionStatus.PASS
    assert result.quality["vision_mode"] == VISION_MODE
    assert backend.calls == []            # 没有走整图流水线
    assert host.calls                    # 每框都走了宿主找圆
    assert result.pipeline_id == "approved_ds86_circle_pipeline"
    assert result.frame_id == "f1" and result.capture_id == "c1"


def test_run_vision_falls_back_to_pipeline_for_legacy_detection():
    backend = InProcessStubBackend(native_from_circles([(160.0, 120.0, 28.0)]))
    executor = _executor(backend, params={}, host_proxy=RectHost())
    result = executor.run_vision(_ctx(), _frame(), result_id="rid",
                                 detection_id="det")
    assert result.status is DetectionStatus.PASS
    assert len(backend.calls) == 1        # 走了整图流水线
    assert result.quality.get("vision_mode") is None
    assert len(result.targets) == 1


def test_run_vision_without_detection_ref_stays_on_pipeline():
    backend = InProcessStubBackend(native_from_circles([(160.0, 120.0, 28.0)]))
    executor = _executor(backend, params={"circle_debug": _debug_params()},
                         host_proxy=RectHost())
    result = executor.run_vision(_ctx(), _frame(), result_id="rid")
    assert result.status is DetectionStatus.PASS
    assert len(backend.calls) == 1


def test_run_vision_survives_broken_detection_store():
    backend = InProcessStubBackend(native_from_circles([(160.0, 120.0, 28.0)]))
    executor = ActionExecutor(interlocks=None, records=None,
                              vision=_pipeline(backend),
                              vision_detections=_BrokenStore(),
                              host_proxy=RectHost())
    result = executor.run_vision(_ctx(), _frame(), result_id="rid",
                                 detection_id="det")
    assert result.status is DetectionStatus.PASS
    assert len(backend.calls) == 1


# ---------------------------------------------------------------- 节点接线
class _CaptureExecutor:
    vision = SimpleNamespace(pipeline_id=None)
    ledger = None

    def __init__(self, result: DetectionResult) -> None:
        self._result = result
        self.seen: dict = {}

    def detection_scheme(self, detection_id):
        return None

    def run_vision(self, ctx, frame, *, result_id, detection_id=None):
        self.seen["detection_id"] = detection_id
        return self._result


def _result() -> DetectionResult:
    return DetectionResult(result_id="rid", status=DetectionStatus.NO_TARGET,
                           reason="未检出圆/孔",
                           pipeline_id="approved_ds86_circle_pipeline")


def _node(executor, *, detection_ref="det") -> VisionRunNode:
    ctx = _ctx()
    outs = OutputStore()
    outs.set(ctx.station_instance_id or "", "cap", "frame", _frame())
    return VisionRunNode("vision:det", pipeline_id=None, input_step="cap",
                         detection_ref=detection_ref, step_id="det", ctx=ctx,
                         executor=executor, outputs=outs)


def test_vision_node_passes_detection_ref_to_executor():
    executor = _CaptureExecutor(_result())
    node = _node(executor)
    assert node.update() is Status.SUCCESS
    assert executor.seen["detection_id"] == "det"


def test_vision_node_without_detection_ref_omits_kwarg():
    executor = _CaptureExecutor(_result())
    node = _node(executor, detection_ref="")
    assert node.update() is Status.SUCCESS
    assert executor.seen["detection_id"] is None


# ---------------------------------------------------------------- 优先级（DEC-057）
class _PriorityExecutor(_CaptureExecutor):
    """同时具备 circle_debug_params / detection_scheme 两条链的执行器替身。"""

    def __init__(self, result, *, debug, scheme) -> None:
        super().__init__(result)
        self._debug = debug
        self._scheme = scheme
        self.seen.update(run_vision=0, run_scheme=0)

    def circle_debug_params(self, detection_id):
        return self._debug

    def detection_scheme(self, detection_id):
        return self._scheme

    def run_vision(self, ctx, frame, *, result_id, detection_id=None):
        self.seen["run_vision"] += 1
        return super().run_vision(ctx, frame, result_id=result_id,
                                  detection_id=detection_id)

    def run_detection_scheme(self, ctx, frame, *, detection_id, scheme, result_id):
        self.seen["run_scheme"] += 1
        return self._result


def test_vision_node_prefers_circle_debug_over_saved_scheme():
    """界面「另存为检测」永远带 scheme；有 circle_debug 时必须先走找圆链，
    否则作业台右卡永远拿不到框/圆（用户 2026-10-21 报告的根因）。"""
    executor = _PriorityExecutor(_result(), debug={"depth": {}, "circle": {}},
                                 scheme=object())
    node = _node(executor)
    assert node.update() is Status.SUCCESS
    assert executor.seen["run_vision"] == 1
    assert executor.seen["run_scheme"] == 0
    assert executor.seen["detection_id"] == "det"


def test_vision_node_uses_saved_scheme_without_circle_debug():
    """旧检测/无找圆调试参数（depth 段）时，仍按保存的「运行一次方案」执行。"""
    executor = _PriorityExecutor(_result(), debug=None, scheme=object())
    node = _node(executor)
    assert node.update() is Status.SUCCESS
    assert executor.seen["run_scheme"] == 1
    assert executor.seen["run_vision"] == 0


def test_vision_node_survives_broken_circle_debug_lookup():
    """查找圆参数失败时如实回落方案分支，不让探测把步骤打死。"""
    executor = _PriorityExecutor(_result(), debug=None, scheme=object())

    def _boom(detection_id):
        raise RuntimeError("存储读取失败")

    executor.circle_debug_params = _boom
    node = _node(executor)
    assert node.update() is Status.SUCCESS
    assert executor.seen["run_scheme"] == 1
