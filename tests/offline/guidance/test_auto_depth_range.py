"""自动深度范围（DEC-133）的离线用例。

公式：下限 = 本帧最小有效深度 − 偏移(默认10)；上限 = 最大平面平均深度 − 偏移(默认20)。
覆盖：公式数值、无有效像素回退、平面缺失回退、clamped、整链（自动模式下出矩形）。
"""
from __future__ import annotations

import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from application.algorithms.guidance.depth_auto_range import (  # noqa: E402
    resolve_auto_depth_range,
)
from application.algorithms.guidance.depth_guidance import (  # noqa: E402
    DepthGuidanceParams,
    run_depth_guidance,
)

H, W = 480, 640


def _scene(plane_mm=250, blobs_mm=(120, 126), seed=0) -> np.ndarray:
    """背景：整幅平面 250mm（加小噪声）；前景：两个 60×60 小块 120/126mm。"""
    rng = np.random.default_rng(seed)
    depth = np.full((H, W), plane_mm, dtype=np.float64)
    depth += rng.normal(0, 0.8, depth.shape)
    for i, mm in enumerate(blobs_mm):
        y = 100 + 160 * i
        depth[y:y + 60, 100 + 100 * i:160 + 100 * i] = mm
    return np.round(depth).astype(np.uint16)


def _params(**over) -> DepthGuidanceParams:
    base = dict(depth_range_mode="auto", auto_min_offset_mm=10.0,
                auto_plane_offset_mm=20.0, auto_plane_tol_mm=8.0,
                thresh_method="binary_inv", thresh_value=127.0,
                morph_op="none", morph_kernel_size=3,
                min_contour_area=200.0, max_contour_area=9999999.0,
                rect_mode="auto", rect_pad_px=20.0, rect_merge_gap_px=30.0)
    base.update(over)
    return DepthGuidanceParams(**base)


def test_auto_range_formula():
    """下限=最小深度−10；上限=最大平面平均−20。"""
    depth = _scene()
    out = resolve_auto_depth_range(depth, 1e-3)
    assert out["ok"], out
    assert abs(out["raw_min_mm"] - 120) <= 1
    assert abs(out["min_mm"] - (out["raw_min_mm"] - 10.0)) <= 1e-6
    assert abs(out["plane_mean_mm"] - 250) <= 1.5
    assert abs(out["max_mm"] - (out["plane_mean_mm"] - 20.0)) <= 1e-6
    assert out["plane_ratio"] > 0.8          # 背景占绝大部分


def test_auto_range_no_valid_pixels_fallback():
    """全无效帧：ok=False 如实给原因；run 时沿用手动上下限。"""
    depth = np.full((H, W), 65535, np.uint16)
    out = resolve_auto_depth_range(depth, 1e-3)
    assert not out["ok"] and "没有有效深度" in out["reason"]
    res = run_depth_guidance(depth, 1e-3, _params(depth_min_mm=200.0,
                                                  depth_max_mm=3000.0))
    assert res["auto_range"] is not None and not res["auto_range"]["ok"]
    assert res["effective_depth_range_mm"] == [200.0, 3000.0]
    assert res["n_contours"] == 0


def test_auto_range_clamped_when_plane_too_close():
    """平面离最近点太近（上限≤下限）时抬到下限之上并标记 clamped。"""
    depth = np.full((H, W), 100, np.uint16)
    depth[50:110, 50:110] = 95
    out = resolve_auto_depth_range(depth, 1e-3)
    assert out["ok"] and out["clamped"]
    assert out["max_mm"] == out["min_mm"] + 1.0


def test_run_depth_guidance_auto_mode_produces_rects():
    """整链：自动模式下切分用计算值，前景两块都出矩形。"""
    depth = _scene()
    res = run_depth_guidance(depth, 1e-3, _params())
    ar = res["auto_range"]
    assert ar is not None and ar["ok"]
    lo, hi = res["effective_depth_range_mm"]
    assert abs(lo - ar["min_mm"]) <= 1e-6 and abs(hi - ar["max_mm"]) <= 1e-6
    assert len(res["rects"]) == 2, res["rects"]
    assert res["n_contours"] == 2


def test_run_depth_guidance_manual_mode_unchanged():
    """manual 模式：不计算自动范围，行为与旧版一致。"""
    depth = _scene()
    p = _params(depth_range_mode="manual", depth_min_mm=100.0, depth_max_mm=180.0)
    res = run_depth_guidance(depth, 1e-3, p)
    assert res["auto_range"] is None
    assert res["effective_depth_range_mm"] == [100.0, 180.0]
