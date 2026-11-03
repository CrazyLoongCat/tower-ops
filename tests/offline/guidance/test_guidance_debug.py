"""找圆调试服务（离线；不接真机、不碰网络）。

覆盖：深度引导阶段图（合成深度图）/ 逐矩形找圆（全图坐标、跳过小矩形、单矩形异常不中断、
宿主缺失不给假圆）/ 圆心 XYZ（均值深度与螺旋搜索兜底）/ 整条调试链的四种落点
（depth / circle / xyz / done）与粗精偏移映射。
"""
from __future__ import annotations

import numpy as np
import pytest

from application.algorithms.circle.params import CircleParams
from application.algorithms.guidance.depth_guidance import DepthGuidanceParams
from application.algorithms.guidance.offsets import AxisMapping, OffsetParams
from application.contracts.enums import ImageSpace
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
from application.services.guidance_debug import (
    CircleDebugResult,
    STAGE_CHOICES,
    circle_xyz,
    depth_guidance_stage,
    find_circles_in_rects,
    run_circle_debug,
)
from application.services.guidance_service import CircleHit

W, H = 320, 240
RAW_DISC = 500
SCALE = 0.001
FX = FY = 200.0
PPX, PPY = W / 2.0, H / 2.0


def _depth_with_disc(cx: int = 160, cy: int = 120, radius: int = 30) -> np.ndarray:
    depth = np.zeros((H, W), dtype="uint16")
    yy, xx = np.mgrid[0:H, 0:W]
    depth[np.hypot(xx - cx, yy - cy) < radius] = RAW_DISC
    return depth


def _frame(depth: np.ndarray | None = None) -> FrameBundle:
    depth = _depth_with_disc() if depth is None else depth
    color = np.zeros((H, W, 3), dtype="uint8")
    return FrameBundle(
        camera_id="ds86_01", frame_id="f1", capture_id="c1", sequence=1,
        service_id="local", channel_id="ds86_01",
        color=ImageRef.from_array(color), depth=ImageRef.from_array(depth),
        depth_scale_m=SCALE,
        intrinsics=Intrinsics(fx=FX, fy=FY, cx=PPX, cy=PPY, width=W, height=H,
                              intrinsics_space=ImageSpace.DEPTH,
                              calibration_version="t"))


def _depth_params(**over) -> DepthGuidanceParams:
    """合成图判据：与出厂配置同口径（binary_inv/127），形态学关掉。"""
    base = dict(morph_op="none", thresh_method="binary_inv", thresh_value=127,
                depth_min_mm=200.0, depth_max_mm=3000.0, rect_side_length=100.0)
    base.update(over)
    return DepthGuidanceParams(**base)


def _rect(x1=110, y1=70, x2=210, y2=170, **extra) -> dict:
    return {"cx": (x1 + x2) / 2.0, "cy": (y1 + y2) / 2.0,
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "w": x2 - x1, "h": y2 - y1, **extra}


class RecordHost:
    """记录调用参数的假 cp38 宿主。"""

    def __init__(self, circles=None) -> None:
        self.circles = circles if circles is not None else [(160.0, 120.0, 28.0)]
        self.calls: list = []

    def detect(self, images, rects, params) -> dict:
        self.calls.append((images, rects, params))
        show = [[1, cx, cy, r] for (cx, cy, r) in self.circles]
        return {"statues": [1], "error_msg": [""], "circle_num": [[len(show)]],
                "circles_for_show": [show], "circle_centers": [], "radius": []}


# ---------------------------------------------------------------- 深度阶段
def test_depth_stage_returns_stage_images_and_rects():
    stage = depth_guidance_stage(_frame(), _depth_params())
    assert set(stage.stages) == {"depth_mm", "gray", "binary", "filtered",
                                 "contour_image", "debug_image"}
    assert stage.n_contours >= 1
    assert stage.rects, "合成圆盘上应找到正外接矩形"
    r = stage.rects[0]
    assert abs(int(r["cx"]) - 160) <= 8 and abs(int(r["cy"]) - 120) <= 8
    assert stage.stages["depth_mm"].max() == pytest.approx(500.0)


def test_depth_stage_missing_depth_raises():
    frame = _frame()
    frame.depth = None
    with pytest.raises(ValueError, match="深度"):
        depth_guidance_stage(frame, _depth_params())


def test_depth_stage_missing_scale_raises():
    frame = _frame()
    frame.depth_scale_m = 0.0
    with pytest.raises(ValueError, match="depth_scale"):
        depth_guidance_stage(frame, _depth_params())


# ---------------------------------------------------------------- 逐矩形找圆
def test_find_circles_passes_algo_rect_and_keeps_full_image_coords():
    host = RecordHost()
    image = np.zeros((H, W, 3), dtype="uint8")
    hits, errors = find_circles_in_rects(
        image, [_rect(110, 70, 210, 170)], CircleParams(), host)
    assert errors == [] and len(hits) == 1
    h = hits[0]
    assert (h.rect_index, h.cx, h.cy, h.radius) == (0, 160.0, 120.0, 28.0)
    images, rects, _params = host.calls[0]
    assert rects == [[2, 110, 70, 100, 100, 0]]      # 原生约定 [2,x,y,w,h,0]
    assert images[0] is image


def test_find_circles_uses_rect_index_when_present():
    host = RecordHost()
    hits, _ = find_circles_in_rects(np.zeros((H, W, 3), dtype="uint8"),
                                    [_rect(idx=7)], CircleParams(), host)
    assert hits[0].rect_index == 7


def test_find_circles_skips_small_rects_with_reason():
    host = RecordHost()
    hits, errors = find_circles_in_rects(
        np.zeros((H, W, 3), dtype="uint8"),
        [_rect(10, 10, 13, 40)], CircleParams(), host)
    assert hits == [] and host.calls == []
    assert any("< 5px" in e for e in errors)


def test_find_circles_continues_after_one_rect_failure():
    class FlakyHost(RecordHost):
        def detect(self, images, rects, params) -> dict:
            if rects[0][1] == 0:                     # 第一块矩形注入异常
                raise RuntimeError("注入: 宿主失败")
            return super().detect(images, rects, params)

    host = FlakyHost()
    hits, errors = find_circles_in_rects(
        np.zeros((H, W, 3), dtype="uint8"),
        [_rect(0, 0, 100, 100), _rect(110, 70, 210, 170)], CircleParams(), host)
    assert len(hits) == 1 and hits[0].rect_index == 1
    assert any("找圆异常" in e and "注入" in e for e in errors)


def test_find_circles_without_host_raises_instead_of_faking():
    with pytest.raises(RuntimeError, match="cp38"):
        find_circles_in_rects(np.zeros((H, W, 3), dtype="uint8"),
                              [_rect()], CircleParams(), None)


def test_find_circles_without_image_raises():
    with pytest.raises(ValueError, match="图像"):
        find_circles_in_rects(None, [_rect()], CircleParams(), RecordHost())


# ---------------------------------------------------------------- 圆心 XYZ
def test_circle_xyz_mean_depth():
    frame = _frame()
    stage = depth_guidance_stage(frame, _depth_params())
    params = _depth_params(avg_depth_enable=True, avg_depth_diameter=11,
                           depth_valid_min_mm=1.0)
    xyz, used = circle_xyz(frame, CircleHit(0, 160.0, 120.0, 28.0),
                           stage.stages["depth_mm"], params)
    assert xyz is not None and used == pytest.approx(500.0, abs=1.0)
    assert xyz[2] == pytest.approx(500.0, abs=1.0)
    assert xyz[0] == pytest.approx(0.0, abs=0.5)


def test_circle_xyz_spiral_fallback_when_center_invalid():
    depth = _depth_with_disc()
    yy, xx = np.mgrid[0:H, 0:W]
    depth[np.hypot(xx - 160.0, yy - 120.0) < 3] = 0     # 圆心挖成无效
    frame = _frame(depth)
    stage = depth_guidance_stage(frame, _depth_params())
    xyz, used = circle_xyz(frame, CircleHit(0, 160.0, 120.0, 28.0),
                           stage.stages["depth_mm"], _depth_params())
    assert xyz is not None and used == pytest.approx(500.0, abs=1.0)


def test_circle_xyz_none_when_all_depth_invalid():
    frame = _frame(np.zeros((H, W), dtype="uint16"))
    depth_mm = np.zeros((H, W), dtype="float32")
    xyz, used = circle_xyz(frame, CircleHit(0, 160.0, 120.0, 28.0),
                           depth_mm, _depth_params())
    assert xyz is None and used == 0.0


# ---------------------------------------------------------------- 整条链
def test_stage_choices_cover_five_views():
    keys = [k for k, _label in STAGE_CHOICES]
    assert keys == ["orig", "gray", "binary", "filtered", "contour"]


def test_run_circle_debug_only_depth():
    res = run_circle_debug(_frame(), depth_params=_depth_params(),
                           circle_params=CircleParams(), only_depth=True)
    assert res.ok and res.stage == "depth" and res.rects
    assert res.message.startswith("深度引导完成")


def test_run_circle_debug_missing_depth_stops_at_depth():
    frame = _frame()
    frame.depth = None
    res = run_circle_debug(frame, depth_params=_depth_params(),
                           circle_params=CircleParams(), host_proxy=RecordHost())
    assert not res.ok and res.stage == "depth" and "深度" in res.message


def test_run_circle_debug_no_candidate_stops_at_depth():
    res = run_circle_debug(_frame(np.zeros((H, W), dtype="uint16")),
                           depth_params=_depth_params(),
                           circle_params=CircleParams(), host_proxy=RecordHost())
    assert not res.ok and res.stage == "depth"
    assert "未找到候选" in res.message
    assert res.coarse is None and res.fine is None      # 不给假偏移


def test_run_circle_debug_without_host_stops_at_circle():
    res = run_circle_debug(_frame(), depth_params=_depth_params(),
                           circle_params=CircleParams(), host_proxy=None)
    assert not res.ok and res.stage == "circle"
    assert "找圆不可用" in res.message and res.hits == []
    assert res.coarse is None and res.fine is None


def test_run_circle_debug_no_hits_stops_at_circle():
    res = run_circle_debug(_frame(), depth_params=_depth_params(),
                           circle_params=CircleParams(),
                           host_proxy=RecordHost(circles=[]))
    assert not res.ok and res.stage == "circle"
    assert "未找到圆" in res.message and res.coarse is None


def test_run_circle_debug_hit_outside_valid_depth_stops_at_xyz():
    res = run_circle_debug(_frame(), depth_params=_depth_params(),
                           circle_params=CircleParams(),
                           host_proxy=RecordHost(circles=[(10.0, 10.0, 3.0)]))
    assert not res.ok and res.stage == "xyz"
    assert "深度无效" in res.message and res.cam_xyz is None
    assert res.coarse is None and res.fine is None


def test_run_circle_debug_full_chain_ok():
    res = run_circle_debug(_frame(), depth_params=_depth_params(),
                           circle_params=CircleParams(),
                           host_proxy=RecordHost())
    assert res.ok and res.stage == "done", res.message
    assert isinstance(res, CircleDebugResult)
    assert res.selected is not None and res.hits
    assert res.cam_xyz is not None
    assert res.cam_xyz[2] == pytest.approx(500.0, abs=2.0)
    assert res.coarse is not None and res.fine is not None
    assert res.stages.get("debug_image") is not None


def test_run_circle_debug_applies_offset_axis_mapping():
    mapping = AxisMapping("Z", -1, "X", -1)
    offsets = OffsetParams(coarse=mapping, fine=mapping, tcp_x=0.0, tcp_y=0.0)
    # 圆心偏离光心 10px → X≈+25mm（深度500mm, fx=200）
    res = run_circle_debug(_frame(), depth_params=_depth_params(),
                           circle_params=CircleParams(),
                           host_proxy=RecordHost(circles=[(170.0, 120.0, 10.0)]),
                           offsets=offsets)
    assert res.ok and res.cam_xyz[0] == pytest.approx(25.0, abs=2.0)
    assert res.cam_xyz[1] == pytest.approx(0.0, abs=1.0)
    assert res.coarse == pytest.approx((0.0, -25.0), abs=2.0)   # (robot_x=-y, robot_z=-x)
    assert res.fine == pytest.approx((0.0, -25.0), abs=2.0)
