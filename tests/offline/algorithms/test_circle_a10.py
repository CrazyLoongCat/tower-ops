"""A10 圆检测规范化/判定测试 (T14/T16/T21/T38 离线)."""
from __future__ import annotations

import numpy as np
import pytest

from application.algorithms.circle.detector import (
    CircleDecisionConfig,
    CircleDetector,
    InProcessStubBackend,
    native_from_circles,
)
from application.algorithms.circle.normalize import (
    NativeResultError,
    apply_roi_and_scale,
    parse_native_circles,
)
from application.algorithms.circle.params import CircleParams, RoiRect
from application.algorithms.circle.pipeline import CirclePipeline, CirclePipelineSpec
from application.contracts.enums import DetectionStatus, ImageSpace
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
from application.contracts.results import CirclePrimitive


def _frame(*, depth_valid=1.0):
    color = np.zeros((480, 640, 3), dtype=np.uint8)
    depth = np.full((480, 640), 1000, dtype=np.uint16)
    return FrameBundle(
        camera_id="ds86_01", frame_id="f1", capture_id="c1", sequence=1,
        run_id="run1", station_instance_id="st1", step_id="detect",
        color=ImageRef.from_array(color), depth=ImageRef.from_array(depth),
        depth_scale_m=0.001,
        intrinsics=Intrinsics(600, 600, 320, 240, 640, 480, ImageSpace.DEPTH),
        depth_valid_ratio=depth_valid,
    )


def _detector(response, **kw):
    return CircleDetector(InProcessStubBackend(response), **kw)


def test_pass_when_circle_within_criteria():
    det = _detector(native_from_circles([(300.0, 200.0, 40.0)]))
    r = det.run(_frame(), CircleParams(),
                CircleDecisionConfig(radius_min_px=20, radius_max_px=60),
                result_id="res1")
    assert r.status is DetectionStatus.PASS
    assert r.targets[0].radius_px == 40.0
    assert r.result_id == "res1" and r.frame_id == "f1" and r.run_id == "run1"


def test_no_target_when_zero_circles():
    det = _detector(native_from_circles([]))
    r = det.run(_frame(), CircleParams(), result_id="res")
    assert r.status is DetectionStatus.NO_TARGET
    assert r.is_terminal_negative


def test_fail_when_out_of_radius():
    det = _detector(native_from_circles([(300.0, 200.0, 5.0)]))
    r = det.run(_frame(), CircleParams(),
                CircleDecisionConfig(radius_min_px=20, radius_max_px=60),
                result_id="res")
    assert r.status is DetectionStatus.FAIL


def test_indeterminate_on_low_depth_quality():
    det = _detector(native_from_circles([(300, 200, 40)]))
    r = det.run(_frame(depth_valid=0.1), CircleParams(),
                CircleDecisionConfig(min_depth_valid_ratio=0.5),
                result_id="res")
    assert r.status is DetectionStatus.INDETERMINATE


def test_error_on_backend_exception():
    class Boom:
        def detect(self, images, rects, params):
            raise RuntimeError("native crashed")
    det = CircleDetector(Boom())
    r = det.run(_frame(), CircleParams(), result_id="res")
    assert r.status is DetectionStatus.ERROR
    assert not r.is_pass


def test_error_on_malformed_return():
    det = _detector({"statues": [1]})  # 缺 circle_centers 等
    r = det.run(_frame(), CircleParams(), result_id="res")
    assert r.status is DetectionStatus.ERROR


def test_cancelled_state_distinct():
    det = _detector(native_from_circles([(300, 200, 40)]))
    r = det.run(_frame(), CircleParams(), result_id="res", cancelled=True)
    assert r.status is DetectionStatus.CANCELLED
    assert not r.is_terminal_negative  # 取消不进入复核分支


def test_error_when_no_color():
    f = _frame()
    f.color = None
    det = _detector(native_from_circles([(1, 1, 1)]))
    r = det.run(f, CircleParams(), result_id="res")
    assert r.status is DetectionStatus.ERROR


def test_six_states_all_reachable_are_distinct():
    seen = {
        DetectionStatus.PASS, DetectionStatus.FAIL, DetectionStatus.NO_TARGET,
        DetectionStatus.INDETERMINATE, DetectionStatus.ERROR, DetectionStatus.CANCELLED,
    }
    assert len(seen) == 6


def test_roi_scale_applied_once():
    circles = [CirclePrimitive(cx=100.0, cy=50.0, radius_px=10.0)]
    # 局部坐标 -> 原图: /scale + offset
    full = apply_roi_and_scale(circles, roi=(10, 20, 100, 100), scale=2.0,
                               coordinates_are_full_image=False)
    assert full[0].cx == pytest.approx(100.0 / 2 + 10)
    assert full[0].cy == pytest.approx(50.0 / 2 + 20)
    assert full[0].radius_px == pytest.approx(5.0)
    # 已是全图坐标时不重复转换
    same = apply_roi_and_scale(circles, roi=(10, 20, 100, 100), scale=2.0,
                               coordinates_are_full_image=True)
    assert same[0].cx == 100.0 and same[0].radius_px == 10.0


def test_params_hash_changes_with_params():
    a = CircleParams(grad_thresh=50.0)
    b = CircleParams(grad_thresh=51.0)
    assert a.params_hash() != b.params_hash()
    assert a.params_hash() == CircleParams(grad_thresh=50.0).params_hash()


def test_late_result_scoped_out():
    det = _detector(native_from_circles([(300, 200, 40)]))
    r = det.run(_frame(), CircleParams(), result_id="res")
    assert r.belongs_to("run1", "st1")
    assert not r.belongs_to("run2", "st1")


def test_pipeline_binds_source_and_hash():
    spec = CirclePipelineSpec(pipeline_id="p1", version="v1", params=CircleParams(),
                              decision=CircleDecisionConfig())
    pipe = CirclePipeline(spec, InProcessStubBackend(native_from_circles([(300, 200, 40)])))
    r = pipe.run(_frame(), result_id="res")
    assert r.pipeline_id == "p1" and r.pipeline_version == "v1"
    assert r.params_hash == spec.params.params_hash()
    assert r.host == "in-process"


def test_parse_native_centers_fallback():
    raw = {"circle_num": [[1]], "circle_centers": [[[0, 11.0, 22.0]]],
           "radius": [[7.0]], "circles_for_show": [None]}
    circles, metrics = parse_native_circles(raw)
    assert circles[0].cx == 11.0 and circles[0].cy == 22.0
    assert circles[0].radius_px == 7.0


def test_parse_rejects_bad_structure():
    with pytest.raises(NativeResultError):
        parse_native_circles("not a dict")
    with pytest.raises(NativeResultError):
        parse_native_circles({"circle_num": [[0]]})  # 缺字段


# ---------------------------------------------------------------- DEC-054
# circles_for_show 的空占位（[] / [[]] / [[[]]]）是"没有圆"，不是畸形；分组要多下钻。
def _native_raw(show, *, centers=(), radii=(), coverages=()):
    return {"statues": [1], "error_msg": [""], "circle_num": [[1]],
            "circles": [None], "circle_centers": centers, "radius": radii,
            "diameters": [None], "circle_points": [None],
            "circle_coverage": coverages, "output_images": [None],
            "circles_for_show": show}


def test_parse_empty_nested_show_falls_back_to_centers():
    """现场日志的原始形状：circles_for_show=[[[]]] + 有效 circle_centers。"""
    raw = _native_raw([[[]]], centers=[[[4.0, 319.99, 240.0]]],
                      radii=[[62.8]])
    circles, metrics = parse_native_circles(raw)
    assert len(circles) == 1
    assert circles[0].cx == pytest.approx(319.99)
    assert circles[0].radius_px == pytest.approx(62.8)
    assert metrics["empty_show_entries"] >= 1


def test_parse_empty_show_without_centers_is_empty_not_malformed():
    for show in ([], [[]], [[[]]], [None], [[None]]):
        circles, metrics = parse_native_circles(_native_raw(show))
        assert circles == [], show
        assert metrics["reported_count"] == 0


def test_parse_grouped_show_flattens_per_rect_lists():
    circles, _ = parse_native_circles(
        _native_raw([[[1, 10, 20, 5], [1, 30, 40, 6]]]))
    assert [(c.cx, c.cy, c.radius_px) for c in circles] == \
        [(10.0, 20.0, 5.0), (30.0, 40.0, 6.0)]


def test_parse_mixed_empty_and_valid_entries_skips_empty():
    circles, metrics = parse_native_circles(
        _native_raw([[[]], [1, 10, 20, 5]]))
    assert len(circles) == 1 and circles[0].cx == pytest.approx(10.0)
    assert metrics["empty_show_entries"] == 1


def test_parse_flag_zero_is_ignored_and_falls_back():
    circles, metrics = parse_native_circles(
        _native_raw([[[0, 1, 2, 3]]], centers=[[[0, 7, 8]]],
                    radii=[[4.0]]))
    assert [(c.cx, c.cy, c.radius_px) for c in circles] == [(7.0, 8.0, 4.0)]
    assert metrics["show_rejected"] == 1


def test_parse_coverage_from_nested_lists():
    circles, _ = parse_native_circles(
        _native_raw([[[1, 10, 20, 5], [1, 30, 40, 6]]],
                    coverages=[[[0.9], [0.8]]]))
    assert [c.coverage for c in circles] == [pytest.approx(0.9),
                                             pytest.approx(0.8)]


def test_parse_still_rejects_truly_malformed_leaf():
    with pytest.raises(NativeResultError, match="结构非法"):
        parse_native_circles(_native_raw([[1, 2]]))
    with pytest.raises(NativeResultError, match="数值非法"):
        parse_native_circles(_native_raw([[1, "x", 2, 3]]))


def test_detector_user_reported_empty_show_is_pass_not_error():
    """回归用户 2026-10-21 现场日志：空占位曾被判"原生返回畸形"，整步失败。"""
    det = _detector(_native_raw([[[]]], centers=[[[0, 300.0, 200.0]]],
                                radii=[[40.0]]))
    r = det.run(_frame(), CircleParams(), result_id="res")
    assert r.status is DetectionStatus.PASS
    assert r.targets[0].cx == pytest.approx(300.0)


def test_detector_empty_show_without_centers_is_no_target_not_error():
    det = _detector(_native_raw([[[]]]))
    r = det.run(_frame(), CircleParams(), result_id="res")
    assert r.status is DetectionStatus.NO_TARGET
    assert r.status is not DetectionStatus.ERROR


# ---------------------------------------------------------------- DEC-055
# 现场同一帧的返回会整体多包一层（宿主曾把"不限区域"当成一个空矩形）：
# circle_centers / radius / circle_num / statues 都必须按任意层数解析。
def test_parse_extra_layer_centers_and_radius_fallback():
    raw = _native_raw([[[]]],
                      centers=[[[[4.0, 319.99, 240.0]]]],
                      radii=[[[62.8]]])
    circles, _ = parse_native_circles(raw)
    assert len(circles) == 1
    assert circles[0].cx == pytest.approx(319.99)
    assert circles[0].cy == pytest.approx(240.0)
    assert circles[0].radius_px == pytest.approx(62.8)


def test_parse_deep_circle_num_and_statues_do_not_crash():
    raw = _native_raw([[[1, 10, 20, 5]]])
    raw["circle_num"] = [[[3]]]
    raw["statues"] = [[0]]
    raw["error_msg"] = [[""]]
    circles, metrics = parse_native_circles(raw)
    assert len(circles) == 1
    assert metrics["raw_circle_num"] == 3
    assert metrics["statues"] == 0


def test_parse_centers_placeholder_layers_are_empty_not_malformed():
    raw = _native_raw([[[]]], centers=[[[[]]]], radii=[[[]]])
    circles, metrics = parse_native_circles(raw)
    assert circles == []
    assert metrics["empty_center_entries"] >= 1


def test_parse_malformed_centers_reports_native_shape():
    raw = _native_raw([[[]]], centers=[[[[4.0]]]], radii=[[[1.0]]])
    with pytest.raises(NativeResultError) as exc:
        parse_native_circles(raw)
    msg = str(exc.value)
    assert "circle_centers[0] 结构非法" in msg
    # 现场排障：报错必须带原生真实形状，别再靠猜包裹层数
    assert "原生形状" in msg and "circles_for_show=" in msg


def test_detector_sends_full_image_rect_when_no_roi():
    """DEC-055：不再送 [[]]（那是"一个空矩形"），改送全图矩形。"""
    backend = InProcessStubBackend(native_from_circles([(300.0, 200.0, 40.0)]))
    CircleDetector(backend).run(_frame(), CircleParams(), result_id="res")
    _, rects, _ = backend.calls[0]
    assert rects == [[2, 0, 0, 640, 480, 0]]


def test_detector_roi_rect_kept_as_flat_list():
    backend = InProcessStubBackend(native_from_circles([(300.0, 200.0, 40.0)]))
    CircleDetector(backend).run(
        _frame(), CircleParams(), result_id="res",
        roi=RoiRect(x=10, y=20, w=100, h=80))
    _, rects, _ = backend.calls[0]
    assert rects == [[2, 10, 20, 100, 80, 0]]
