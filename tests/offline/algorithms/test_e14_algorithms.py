"""E14 算法注册 / 深度 / 线测距 / 组合流水线 测试 (T07/T14/T15/T16/T21/T25 离线)。"""
from __future__ import annotations

import numpy as np
import pytest

from application.algorithms.depth import (
    depth_range_mask, depth_valid_ratio, mask_bbox, pixel_to_point_mm, roi_slice,
    summarize_depth,
)
from application.algorithms.line import distance_px, fit_line, measure_length, px_to_mm
from application.algorithms.pipeline import AlgorithmPipeline, PipelineStep
from application.algorithms.registry import (
    AlgorithmDescriptor, AlgorithmRegistry, UnknownAlgorithm, check_frame_for,
    default_registry,
)
from application.contracts.enums import DetectionStatus, ImageSpace
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
from application.domain.errors import ValidationFailure


def _frame(*, depth=None, color=None, depth_scale=0.001, with_intr=True):
    h, w = 480, 640
    depth = np.full((h, w), 1000, np.uint16) if depth is None else depth
    color = np.zeros((h, w, 3), np.uint8) if color is None else color
    return FrameBundle(
        camera_id="ds86_01", frame_id="f1", capture_id="c1", sequence=1,
        color=ImageRef.from_array(color), depth=ImageRef.from_array(depth),
        depth_scale_m=depth_scale,
        intrinsics=(Intrinsics(600, 600, 320, 240, w, h, ImageSpace.DEPTH)
                    if with_intr else None),
        depth_valid_ratio=depth_valid_ratio(depth),
        calibration_version="cal-v1",
    )


# ---------------- 注册表 ----------------
def test_descriptor_validation():
    with pytest.raises(ValidationFailure):
        AlgorithmRegistry([AlgorithmDescriptor(algorithm_id="", version="v",
                                               output_schema={"a": 1})])
    with pytest.raises(ValidationFailure):
        AlgorithmRegistry([AlgorithmDescriptor(algorithm_id="a", version="",
                                               output_schema={"a": 1})])
    with pytest.raises(ValidationFailure):
        AlgorithmRegistry([AlgorithmDescriptor(algorithm_id="a", version="v",
                                               requires=("imu",),
                                               output_schema={"a": 1})])
    with pytest.raises(ValidationFailure):
        AlgorithmRegistry([AlgorithmDescriptor(algorithm_id="a", version="v")])


def test_registry_unknown_and_duplicate():
    reg = default_registry()
    assert reg.known("circle.detect") and reg.known("depth.range_segment")
    assert reg.get("circle.detect").host == "circle38"      # 圆检测仍在 3.8 宿主
    with pytest.raises(UnknownAlgorithm):
        reg.get("nope")
    with pytest.raises(ValidationFailure):
        reg.register(reg.get("circle.detect"))


def test_check_frame_requires_raw_depth_and_metrics():
    reg = default_registry()
    d = reg.get("depth.range_segment")
    f = _frame()
    f.depth = ImageRef.from_array(np.zeros((4, 4), np.float32))
    with pytest.raises(ValidationFailure):
        check_frame_for(d, f)                                # dtype 必须是 uint16
    f2 = _frame(with_intr=False)
    with pytest.raises(ValidationFailure):
        check_frame_for(d, f2)                               # 缺内参
    f3 = _frame(depth_scale=None)
    with pytest.raises(ValidationFailure):
        check_frame_for(d, f3)                               # 缺 depth_scale


# ---------------- 深度 ----------------
def test_depth_valid_ratio():
    assert depth_valid_ratio(np.array([[0, 0], [1, 2]], np.uint16)) == 0.5
    assert depth_valid_ratio(np.zeros((0, 0), np.uint16)) == 0.0


def test_roi_slice_and_bounds():
    assert roi_slice((10, 20, 30, 40), (480, 640)) == (20, 60, 10, 40)
    assert roi_slice((600, 400, 100, 100), (480, 640)) == (400, 480, 600, 640)
    with pytest.raises(ValueError):
        roi_slice((0, 0, 0, 10), (480, 640))
    with pytest.raises(ValueError):
        roi_slice((1000, 1000, 10, 10), (480, 640))


def test_depth_range_mask_and_bbox():
    depth = np.full((4, 4), 1000, np.uint16)
    depth[0, 0] = 0
    depth[1, 1] = 5000
    mask = depth_range_mask(depth, 0.001, min_mm=900, max_mm=1100)
    assert mask.sum() == 14                                  # 0 与 5000 都排除
    assert mask_bbox(mask) == (0, 0, 4, 4)
    assert mask_bbox(np.zeros((4, 4), bool)) is None
    with pytest.raises(ValueError):
        depth_range_mask(depth, 0.001, min_mm=2000, max_mm=1000)
    with pytest.raises(ValueError):
        depth_range_mask(depth.astype(np.float32), 0.001, min_mm=1, max_mm=2)


def test_pixel_to_point_mm_maths_and_zero_depth():
    x, y, z = pixel_to_point_mm(320, 240, 1000, 0.001, fx=600, fy=600, cx=320, cy=240)
    assert (x, y, z) == (0.0, 0.0, 1000.0)
    x2, y2, _ = pixel_to_point_mm(620, 240, 1000, 0.001, fx=600, fy=600, cx=320, cy=240)
    assert abs(x2 - 500.0) < 1e-6                            # (620-320)*1000/600
    with pytest.raises(ValueError):
        pixel_to_point_mm(1, 1, 0, 0.001, fx=600, fy=600, cx=320, cy=240)  # 0 深度拒绝
    with pytest.raises(ValueError):
        pixel_to_point_mm(1, 1, 1000, 0.001, fx=0, fy=600, cx=0, cy=0)


def test_summarize_depth():
    d = np.array([[0, 1000], [2000, 3000]], np.uint16)
    s = summarize_depth(d, 0.001)
    assert s["valid_ratio"] == 0.75 and s["min_mm"] == 1000.0 and s["max_mm"] == 3000.0


# ---------------- 线测距 ----------------
def test_distance_and_px_to_mm():
    assert distance_px((0, 0), (3, 4)) == 5.0
    assert abs(px_to_mm(60, 1000, 0.001, 600) - 100.0) < 1e-6
    with pytest.raises(ValueError):
        px_to_mm(10, 0, 0.001, 600)                          # 深度无效不输出 mm
    with pytest.raises(ValueError):
        px_to_mm(-1, 1000, 0.001, 600)


def test_measure_length_px_only_without_depth():
    m = measure_length((0, 0), (3, 4))
    assert m["length_px"] == 5.0 and m["length_mm"] is None
    assert "px only" in m["unit_note"]                       # 不得把 px 冒充 mm
    m2 = measure_length((0, 0), (3, 4), depth_raw=1000, depth_scale_m=0.001, fx=600)
    assert m2["length_mm"] is not None and m2["unit_note"] == "px + mm"


def test_fit_line():
    r = fit_line([(0, 0), (1, 1), (2, 2)])
    assert abs(r["slope"] - 1.0) < 1e-9 and r["rms_px"] < 1e-9 and not r["vertical"]
    v = fit_line([(5, 0), (5, 10)])
    assert v["vertical"] is True
    with pytest.raises(ValueError):
        fit_line([(0, 0)])


# ---------------- 组合流水线 ----------------
def test_pipeline_depth_range_pass_and_no_target():
    pipe = AlgorithmPipeline("p", "v1", [PipelineStep("depth.range_segment",
                                                      {"min_mm": 900, "max_mm": 1100})])
    r = pipe.run(_frame(), result_id="r1")
    assert r.status is DetectionStatus.PASS
    assert r.quality["depth.range_segment"]["mask_ratio"] == 1.0

    far = np.full((480, 640), 9000, np.uint16)
    r2 = pipe.run(_frame(depth=far), result_id="r2")
    assert r2.status is DetectionStatus.NO_TARGET


def test_pipeline_pixel_xyz_units_mm():
    pipe = AlgorithmPipeline("p", "v1", [PipelineStep("depth.pixel_to_xyz",
                                                      {"u": 320, "v": 240})])
    r = pipe.run(_frame(), result_id="r")
    units = {m.name: m.unit for m in r.measurements}
    assert units["z"] == "mm" and all(m.unit == "mm" for m in r.measurements)


def test_pipeline_line_px_only_without_depth():
    f = _frame()
    f.depth = None                                           # 无深度
    pipe = AlgorithmPipeline("p", "v1", [PipelineStep("line.measure",
                                                      {"points": [(0, 0), (3, 4)]})])
    r = pipe.run(f, result_id="r")
    assert r.status is DetectionStatus.PASS
    assert [m.unit for m in r.measurements] == ["px"]        # 只有 px, 不显示 mm
    assert r.quality["line.measure"]["depth_available"] is False


def test_pipeline_unknown_algorithm_is_error():
    pipe = AlgorithmPipeline("p", "v1", [PipelineStep("nope", {})])
    r = pipe.run(_frame(), result_id="r")
    assert r.status is DetectionStatus.ERROR and "未知算法" in (r.reason or "")


def test_pipeline_host_algorithm_without_runner_is_error_not_fake():
    """需要隔离宿主的算法 (circle.detect) 未注入运行器 -> ERROR, 不假装成功。"""
    pipe = AlgorithmPipeline("p", "v1", [PipelineStep("circle.detect", {})])
    r = pipe.run(_frame(), result_id="r")
    assert r.status is DetectionStatus.ERROR
    assert "需要宿主" in (r.reason or "") and "假装成功" in (r.reason or "")


def test_pipeline_host_algorithm_with_injected_runner():
    calls = {}

    def fake_host(frame, step):
        calls["n"] = calls.get("n", 0) + 1
        return {"no_target": False, "quality": {"circle_num": 1}, "measurements": []}

    pipe = AlgorithmPipeline("p", "v1", [PipelineStep("circle.detect", {})],
                             host_runners={"circle.detect": fake_host})
    r = pipe.run(_frame(), result_id="r")
    assert r.status is DetectionStatus.PASS and calls["n"] == 1


def test_pipeline_cancelled():
    pipe = AlgorithmPipeline("p", "v1", [PipelineStep("depth.range_segment",
                                                      {"min_mm": 900, "max_mm": 1100})])
    r = pipe.run(_frame(), result_id="r", cancelled=True)
    assert r.status is DetectionStatus.CANCELLED and not r.is_terminal_negative


def test_pipeline_input_validation_error():
    pipe = AlgorithmPipeline("p", "v1", [PipelineStep("depth.range_segment",
                                                      {"min_mm": 900, "max_mm": 1100})])
    f = _frame(with_intr=False)
    r = pipe.run(f, result_id="r")
    assert r.status is DetectionStatus.ERROR and "输入不满足" in (r.reason or "")
