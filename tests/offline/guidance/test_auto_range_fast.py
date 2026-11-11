"""自动深度范围提速（DEC-136）：默认抽样/提前结束与高抽样参考结果一致。"""
from __future__ import annotations

import time

import numpy as np

from application.algorithms.guidance.depth_auto_range import resolve_auto_depth_range


def _synthetic_plane_frame():
    h, w = 240, 320
    yy, xx = np.mgrid[0:h, 0:w]
    plane = 200.0 + 0.05 * xx + 0.03 * yy          # 倾斜平面（mm）
    depth = plane.astype(np.uint16)
    depth[np.hypot(xx - 160, yy - 120) < 30] = 120  # 目标圆盘更近
    depth[0:10, :] = 65535                          # 无效区
    return depth


def test_fast_auto_range_matches_high_sample_reference():
    depth = _synthetic_plane_frame()
    fast = resolve_auto_depth_range(depth, 1e-3)
    ref = resolve_auto_depth_range(depth, 1e-3, iterations=200, sample_max=60000)
    assert fast["ok"] and ref["ok"]
    assert abs(fast["min_mm"] - ref["min_mm"]) <= 0.51
    assert abs(fast["max_mm"] - ref["max_mm"]) <= 0.51
    assert fast["min_mm"] < fast["max_mm"]


def test_fast_auto_range_is_not_slower_than_reference_path():
    """默认参数（15000 抽样、提前结束）不应比大抽样参考慢（宽松上限防抖）。"""
    depth = _synthetic_plane_frame()
    t0 = time.perf_counter(); resolve_auto_depth_range(depth, 1e-3)
    fast = time.perf_counter() - t0
    t0 = time.perf_counter()
    resolve_auto_depth_range(depth, 1e-3, iterations=200, sample_max=60000)
    slow = time.perf_counter() - t0
    assert fast <= slow * 1.2 + 0.02
