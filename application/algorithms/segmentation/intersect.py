# -*- coding: utf-8 -*-
"""运行一次：用户多边形 ∩ 智能分割多边形 → 交集轮廓/面积。

复刻旧工程 `s100_detect_tab.run_detect_once` 的判据：智能分割输出的多边形 mask（255）
与用户绘制多边形 mask（255）做**按位与**，两图均为 255 的交集区域取外轮廓标记；丢弃
``contourArea < 1`` 的单像素碎斑。面积一律**像素(px)**，不换算 mm。
"""
from __future__ import annotations

from typing import Iterable, Optional

import numpy as np


def mask_from_polygon(polygon, shape) -> np.ndarray:
    """把多边形顶点渲染成 uint8 mask（255=内部）。少于 3 点/空 -> 全 0。"""
    import cv2
    h, w = int(shape[0]), int(shape[1])
    mask = np.zeros((h, w), dtype=np.uint8)
    if polygon is None or len(polygon) < 3:
        return mask
    pts = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
    cv2.fillPoly(mask, [np.round(pts).astype(np.int32).reshape(-1, 1, 2)], 255)
    return mask


def mask_to_contours(mask, min_area: float = 1.0) -> list[np.ndarray]:
    """取 mask 的外轮廓；丢弃面积小于 ``min_area`` 的退化碎斑。"""
    import cv2
    if mask is None or np.asarray(mask).size == 0:
        return []
    contours, _ = cv2.findContours(np.asarray(mask), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    return [c for c in contours if cv2.contourArea(c) >= float(min_area)]


def contour_area_sum(contours: Iterable) -> float:
    """轮廓面积之和（像素²）。"""
    import cv2
    return float(sum(cv2.contourArea(c) for c in contours))


def intersection_result(smart_mask, polygon, shape,
                        min_area: float = 1.0) -> dict:
    """智能分割 mask ∩ 用户多边形 mask。

    Args:
        smart_mask: 智能分割输出的 uint8 mask（255=目标）；None/尺寸不符时按空处理。
        polygon: 用户绘制的多边形顶点 [(x,y), ...]（原图坐标）。
        shape: (H, W)（原图尺寸）。
        min_area: 交集轮廓最小面积（px²），小于者丢弃。

    Returns:
        dict: ``contours``（交集外轮廓列表）、``area_px``（交集像素数）、
        ``hit``（是否存在面积 >= min_area 的交集轮廓）。
    """
    import cv2
    h, w = int(shape[0]), int(shape[1])
    poly_mask = mask_from_polygon(polygon, (h, w))
    sm = None if smart_mask is None else np.asarray(smart_mask)
    if sm is None or sm.shape[:2] != (h, w):
        return {"contours": [], "area_px": 0, "hit": False}
    inter = cv2.bitwise_and(sm, poly_mask)
    contours = mask_to_contours(inter, min_area=min_area)
    return {
        "contours": contours,
        "area_px": int(cv2.countNonZero(inter)),
        "hit": bool(contours),
    }


__all__ = [
    "mask_from_polygon", "mask_to_contours", "contour_area_sum",
    "intersection_result",
]
