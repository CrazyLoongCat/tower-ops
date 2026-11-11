"""找圆矩形生成算法（DEC-131）的离线用例。

纯 numpy/cv2：固定正方形（旧算法兼容）、自动贴合（正方形/外接框）、
碎块合并、最小/最大边长钳制、画面边界裁剪，以及一条「深度二值图→矩形」的
整链用例（5 个碎块合成图）。
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from application.algorithms.guidance.depth_guidance import (  # noqa: E402
    DepthGuidanceParams,
    contours_to_rects,
    run_depth_guidance,
)


def _rect_contour(x: int, y: int, w: int, h: int) -> np.ndarray:
    pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.int32)
    return pts.reshape(-1, 1, 2)


def _auto(**over) -> DepthGuidanceParams:
    base = dict(rect_mode="auto", rect_fit="square", rect_scale=1.0,
                rect_pad_px=0.0, rect_min_side_px=0.0, rect_max_side_px=0.0,
                rect_merge_gap_px=0.0)
    base.update(over)
    return DepthGuidanceParams(**base)


def test_fixed_mode_keeps_old_behavior():
    """fixed 模式（默认）：中心点+固定边长正方形，与旧算法一致。"""
    c = _rect_contour(300, 200, 80, 60)
    rects = contours_to_rects([c], DepthGuidanceParams(rect_side_length=100.0),
                              640, 480)
    assert len(rects) == 1
    r = rects[0]
    assert r["w"] == r["h"] == 100
    # 旧行为：方框中心 = 轮廓外接框中心 (340, 230)
    assert abs(r["cx"] - 340.0) <= 1.0 and abs(r["cy"] - 230.0) <= 1.0
    assert (r["x1"], r["y1"], r["x2"], r["y2"]) == (290, 180, 390, 280)
    assert r["n_contours"] == 1


def test_auto_square_fits_blob_with_padding():
    """自动贴合·正方形：边长 = 较大边×系数 + 2×外扩。"""
    c = _rect_contour(300, 200, 80, 60)
    rects = contours_to_rects([c], _auto(rect_pad_px=20.0), 640, 480)
    assert len(rects) == 1
    r = rects[0]
    # cv2.boundingRect 宽高为闭区间（81×61），边长 = 81 + 2×20
    assert r["w"] == r["h"] == 121
    assert (r["x1"], r["y1"], r["x2"], r["y2"]) == (280, 170, 401, 291)


def test_auto_bbox_keeps_aspect():
    """自动贴合·按外接框：宽高各自 × 系数 + 2×外扩（可为长方形）。"""
    c = _rect_contour(300, 200, 80, 60)
    rects = contours_to_rects([c], _auto(rect_fit="bbox", rect_pad_px=10.0),
                              640, 480)
    r = rects[0]
    assert (r["w"], r["h"]) == (101, 81)     # 81×61 外接框各 +2×10
    assert (r["x1"], r["y1"], r["x2"], r["y2"]) == (290, 190, 391, 271)


def test_auto_scale_and_side_clamps():
    """尺寸系数放大；最小/最大边长钳制生效。"""
    c = _rect_contour(300, 200, 80, 60)
    r = contours_to_rects([c], _auto(rect_scale=1.5), 640, 480)[0]
    assert r["w"] == 121                     # 81×1.5
    r = contours_to_rects([c], _auto(rect_min_side_px=140.0), 640, 480)[0]
    assert r["w"] == 140                     # 下限抬起
    r = contours_to_rects([c], _auto(rect_max_side_px=70.0), 640, 480)[0]
    assert r["w"] == 70                      # 上限压住


def test_merge_gap_joins_fragments():
    """碎块合并：间距 ≤ 合并间距的轮廓只出一个矩形，间距=0 时各出一个。"""
    a = _rect_contour(100, 100, 40, 40)
    b = _rect_contour(150, 105, 40, 40)      # 与 a 的水平间隔 10px
    merged = contours_to_rects([a, b],
                               _auto(rect_merge_gap_px=15.0, rect_fit="bbox"),
                               640, 480)
    assert len(merged) == 1
    m = merged[0]
    assert m["n_contours"] == 2
    assert (m["x1"], m["y1"], m["x2"], m["y2"]) == (100, 100, 191, 146)
    split = contours_to_rects([a, b], _auto(), 640, 480)
    assert len(split) == 2
    assert all(s["n_contours"] == 1 for s in split)


def test_border_clipping():
    """贴边物体：矩形裁剪到画面内，不越界。"""
    c = _rect_contour(0, 0, 30, 30)
    r = contours_to_rects([c], _auto(rect_pad_px=20.0), 100, 100)[0]
    assert r["x1"] == 0 and r["y1"] == 0
    assert r["x2"] <= 99 and r["y2"] <= 99


def _five_blob_depth(h: int = 400, w: int = 400) -> np.ndarray:
    """5 个 60×60 近物体（150mm）贴在 180mm 背景上（原始值=mm）。"""
    depth = np.full((h, w), 180, dtype=np.uint16)
    for x, y in ((60, 60), (280, 60), (60, 280), (280, 280), (170, 170)):
        depth[y:y + 60, x:x + 60] = 150
    return depth


def test_depth_to_rects_five_blobs_auto_mode():
    """整链：深度图 → 切分/二值化 → 5 个 blob → 自动贴合出 5 个矩形。"""
    depth = _five_blob_depth()
    params = _auto(rect_pad_px=20.0)
    params.depth_min_mm = 140.0
    params.depth_max_mm = 200.0
    params.thresh_method = "binary_inv"
    params.thresh_value = 127.0
    params.morph_op = "none"
    params.min_contour_area = 500.0
    params.max_contour_area = 9999.0
    res = run_depth_guidance(depth, 1e-3, params, include_rects=True)
    rects = res["rects"]
    assert len(rects) == 5, f"应为 5 个矩形，实际 {len(rects)}"
    for r in rects:
        # 60×60 blob + 2×20 外扩 = 100×100 方框
        assert (r["w"], r["h"]) == (100, 100)
        assert r["n_contours"] == 1


def test_depth_to_rects_five_blobs_fixed_mode_old():
    """同一合成图用旧 fixed 模式：仍是 5 个固定 100px 方框（兼容回归）。"""
    depth = _five_blob_depth()
    params = DepthGuidanceParams(rect_side_length=100.0)
    params.depth_min_mm = 140.0
    params.depth_max_mm = 200.0
    params.thresh_method = "binary_inv"
    params.thresh_value = 127.0
    params.morph_op = "none"
    params.min_contour_area = 500.0
    params.max_contour_area = 9999.0
    res = run_depth_guidance(depth, 1e-3, params, include_rects=True)
    assert len(res["rects"]) == 5
    assert all(r["w"] == r["h"] == 100 for r in res["rects"])
