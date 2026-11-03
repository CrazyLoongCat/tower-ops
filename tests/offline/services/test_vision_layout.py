"""视觉步骤「框 + 框内圆」初始布局服务（DEC-048，离线；不接真机）。

覆盖：
- 有找圆调试参数（``circle_debug``）时按检测配置在**当前帧**现算：深度引导矩形
  → 每框找圆（同一 cp38 宿主）→ 每框最多一个圆；
- 宿主不可用/深度框为空/旧检测无参数时**不伪造圆**：保留深度框或退化为自动检测
  圆的外接方框，并在 message 里如实说明来源；
- 人工提交的框/圆坐标校验（非数字/退化框拒绝，越界夹取）；
- 人工新画框后的框内找圆（找不到圆如实说明）。

算法本身由 guidance_debug 用例覆盖，这里只测编排与诚实边界。
"""
from __future__ import annotations

import numpy as np
import pytest

from application.algorithms.guidance.depth_guidance import DepthGuidanceParams
from application.contracts.enums import ImageSpace
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
from application.contracts.results import CirclePrimitive
from application.services.vision_layout import (
    SOURCE_CIRCLE_DEBUG,
    SOURCE_NONE,
    SOURCE_RESULT_CIRCLE,
    box_around_circle,
    compute_initial_layout,
    find_circle_in_rect,
    image_size,
    normalize_boxes,
)

W, H = 320, 240
RAW_DISC = 500
SCALE = 0.001


def _depth_with_discs(centers=((160, 120, 30),)) -> np.ndarray:
    depth = np.zeros((H, W), dtype="uint16")
    yy, xx = np.mgrid[0:H, 0:W]
    for cx, cy, r in centers:
        depth[np.hypot(xx - cx, yy - cy) < r] = RAW_DISC
    return depth


def _frame(depth=None) -> FrameBundle:
    depth = _depth_with_discs() if depth is None else depth
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


def _depth_params_dict() -> dict:
    return DepthGuidanceParams(morph_op="none", thresh_method="binary_inv",
                               thresh_value=127, depth_min_mm=200.0,
                               depth_max_mm=3000.0,
                               rect_side_length=100.0).to_dict()


def _params(**circle_debug) -> dict:
    dbg = {"depth": _depth_params_dict(), "circle": {}}
    dbg.update(circle_debug)
    return {"circle_debug": dbg}


class Host:
    """假 cp38 宿主：每次调用都返回同一个圆（全图坐标）。"""

    def __init__(self, circles=None) -> None:
        self.circles = circles if circles is not None else [(160.0, 120.0, 28.0)]
        self.calls = 0

    def detect(self, images, rects, params) -> dict:
        self.calls += 1
        show = [[1, cx, cy, r] for (cx, cy, r) in self.circles]
        return {"statues": [1], "error_msg": [""], "circle_num": [[len(show)]],
                "circles_for_show": [show], "circle_centers": [], "radius": []}


class EmptyHost(Host):
    def detect(self, images, rects, params) -> dict:
        self.calls += 1
        return {"statues": [1], "error_msg": [""], "circle_num": [[0]],
                "circles_for_show": [[]], "circle_centers": [], "radius": []}


# ---------------------------------------------------------------- 几何助手
def test_image_size_prefers_color_then_depth():
    assert image_size(_frame()) == (W, H)
    bare = _frame()
    bare.color = None
    assert image_size(bare) == (W, H)
    assert image_size(None) == (None, None)


def test_normalize_boxes_rejects_bad_and_clamps_overflow():
    with pytest.raises(ValueError, match="格式非法"):
        normalize_boxes(["nope"], W, H)
    with pytest.raises(ValueError, match="坐标非法"):
        normalize_boxes([{"x1": "a", "y1": 1, "x2": 10, "y2": 10}], W, H)
    with pytest.raises(ValueError, match="太小"):
        normalize_boxes([{"x1": 10, "y1": 10, "x2": 11, "y2": 40}], W, H)
    out = normalize_boxes([{"x1": -20, "y1": -5, "x2": 9999, "y2": 9999,
                            "circle": {"cx": 9999, "cy": 9999,
                                       "radius_px": 9999}}], W, H)
    box = out[0]
    assert box["x1"] == 0 and box["y1"] == 0
    assert box["x2"] == W - 1 and box["y2"] == H - 1
    c = box["circle"]
    assert box["x1"] <= c["cx"] <= box["x2"]
    assert box["y1"] <= c["cy"] <= box["y2"]
    assert c["cx"] - c["radius_px"] >= box["x1"] - 1e-6


def test_box_around_circle_is_square_with_margin():
    box = box_around_circle({"cx": 160, "cy": 120, "radius_px": 30}, W, H)
    assert box["circle"] == {"cx": pytest.approx(160), "cy": pytest.approx(120),
                             "radius_px": pytest.approx(30)}
    side_x = box["x2"] - box["x1"]
    side_y = box["y2"] - box["y1"]
    assert side_x == pytest.approx(side_y)
    assert side_x >= 2 * 30


# ---------------------------------------------------------------- 初始布局
def test_circle_debug_computes_boxes_and_circles_on_current_frame():
    layout = compute_initial_layout(_frame(), _params(), Host())
    assert layout.source == SOURCE_CIRCLE_DEBUG
    assert len(layout.boxes) >= 1
    box = layout.boxes[0]
    assert box["circle"] is not None
    assert box["x1"] < box["circle"]["cx"] < box["x2"]
    assert layout.selected == 0
    assert "按检测配置现算" in layout.message


def test_circle_debug_keeps_boxes_when_host_unavailable():
    """宿主不可用：保留深度框但**不造圆**，选中为空并说明原因。"""
    layout = compute_initial_layout(_frame(), _params(), None)
    assert layout.source == SOURCE_CIRCLE_DEBUG
    assert layout.boxes and all(b["circle"] is None for b in layout.boxes)
    assert layout.selected is None
    assert "找圆不可用" in layout.message


def test_circle_debug_empty_depth_falls_back_to_result_circles():
    blank = _frame(np.zeros((H, W), dtype="uint16"))
    fallback = [CirclePrimitive(cx=160.0, cy=120.0, radius_px=25.0)]
    layout = compute_initial_layout(blank, _params(), Host(), fallback_circles=fallback)
    assert layout.source == SOURCE_RESULT_CIRCLE
    assert len(layout.boxes) == 1 and layout.boxes[0]["circle"] is not None
    assert "深度引导未找到候选矩形" in layout.message
    assert "外接方框" in layout.message


def test_legacy_detection_without_circle_debug_uses_result_circles():
    fallback = [{"cx": 160, "cy": 120, "radius_px": 25},
                CirclePrimitive(cx=60.0, cy=60.0, radius_px=12.0)]
    layout = compute_initial_layout(_frame(), {}, Host(), fallback_circles=fallback)
    assert layout.source == SOURCE_RESULT_CIRCLE
    assert len(layout.boxes) == 2
    assert layout.selected == 0
    assert all(b["circle"] for b in layout.boxes)
    assert "未保存找圆调试参数" in layout.message


def test_no_params_and_no_results_is_honestly_empty():
    layout = compute_initial_layout(_frame(), None, Host())
    assert layout.boxes == [] and layout.selected is None
    assert layout.source == SOURCE_NONE
    assert "没有可显示的框与圆" in layout.message


def test_circle_debug_stage_exception_reports_and_falls_back():
    bad = {"circle_debug": {"depth": {"depth_min_mm": "x"}, "circle": {}}}
    fallback = [CirclePrimitive(cx=160.0, cy=120.0, radius_px=20.0)]
    layout = compute_initial_layout(_frame(), bad, Host(), fallback_circles=fallback)
    assert layout.source == SOURCE_RESULT_CIRCLE
    assert "按检测配置现算失败" in layout.message


# ---------------------------------------------------------------- 人工新框找圆
def test_find_circle_in_rect_uses_same_host_and_params():
    circle, why = find_circle_in_rect(_frame(), _params(), Host(),
                                      {"x1": 110, "y1": 70, "x2": 210, "y2": 170})
    assert circle == {"cx": pytest.approx(160.0), "cy": pytest.approx(120.0),
                      "radius_px": pytest.approx(28.0)}
    assert "找到圆" in why


def test_find_circle_in_rect_without_hit_explains_without_faking():
    circle, why = find_circle_in_rect(_frame(), _params(), EmptyHost(),
                                      {"x1": 110, "y1": 70, "x2": 210, "y2": 170})
    assert circle is None and "框内未找到圆" in why


def test_find_circle_in_rect_without_host_explains():
    circle, why = find_circle_in_rect(_frame(), _params(), None,
                                      {"x1": 110, "y1": 70, "x2": 210, "y2": 170})
    assert circle is None and "找圆不可用" in why
