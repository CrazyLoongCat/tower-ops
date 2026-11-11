"""平面棱线间距测量脚本（scripts/measure_plane_width.py）的离线用例。

合成平面矩形（宽×高 = 250×400mm 与 400×250mm 两种摆放）在 800mm 深度处，
验证「逐行/逐列取轮廓端点 → 三维距离」在两种朝向下都量出 250mm。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_ROOT, "scripts", "measure_plane_width.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("measure_plane_width", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["measure_plane_width"] = mod
    spec.loader.exec_module(mod)
    return mod


_H, _W = 720, 1280
_INTR = {"fx": 650.0, "fy": 650.0, "cx": 640.0, "cy": 360.0}


def _plane_depth(half_x_mm: float, half_y_mm: float, z_mm: float = 800.0):
    """生成一块朝向相机的平面矩形深度图（矩形外为无效 0）。"""
    u = np.arange(_W)[None, :]
    v = np.arange(_H)[:, None]
    x = (u - _INTR["cx"]) / _INTR["fx"] * z_mm
    y = (v - _INTR["cy"]) / _INTR["fy"] * z_mm
    inside = (np.abs(x) <= half_x_mm) & (np.abs(y) <= half_y_mm)
    depth = np.zeros((_H, _W), np.float32)
    depth[inside] = z_mm
    return depth


def _measure(mod, depth):
    valid = depth > 0
    plane = mod.fit_largest_plane(depth, valid, tol_mm=8.0)
    assert plane is not None
    return mod.measure_edge_distance(plane, depth, valid, _INTR)


def test_tall_rect_row_scan():
    """竖放的 250×400 面：按行扫左右，两侧间距 = 250mm。"""
    mod = _load_script()
    info = _measure(mod, _plane_depth(125.0, 200.0))
    assert info is not None and info["scan_mode"] == "row"
    assert abs(info["width_mm"] - 250.0) <= 12.0
    assert info["tilt_deg"] <= 1.0
    assert info["scan_lines"] > 50


def test_wide_rect_col_scan():
    """横放的 400×250 面（画面旋转 90° 的情形）：按列扫上下，短边 = 250mm。"""
    mod = _load_script()
    info = _measure(mod, _plane_depth(200.0, 125.0))
    assert info is not None and info["scan_mode"] == "col"
    assert abs(info["width_mm"] - 250.0) <= 12.0
