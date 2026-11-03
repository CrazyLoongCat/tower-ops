# -*- coding: utf-8 -*-
"""深度分割（步骤编排）—— 把深度图切成目标、给出轮廓内提示点。

判据**不在这里**：调用 :func:`application.algorithms.guidance.depth_guidance.run_depth_guidance`
（1:1 移植自旧工程 `vision_monitor/vision/depth_guidance_pipeline.py` 的步骤 1），
再叠加**最终轮廓筛选** :func:`~...filter_contours_final` 与**轮廓内提示点**
:func:`~...contour_interior_points`。本模块只负责"裁剪运算区域 + 串流程 + 坐标换算"。

处理图源（智能分割编码图）三选一：

- ``color`` 彩色图 —— 直接用彩色帧；
- ``depth`` 深度图 —— 深度伪彩（:func:`image_ops.depth_to_rgb`）；
- ``fused`` 融合图 —— 彩色图 × 深度分割的形态学白色掩码（掩码外置黑），
  用于过滤远距离重叠目标的干扰（旧工程「融合图辅助」）。

长度/面积一律**像素**，深度切分上下限按**毫米(mm)**（见 DEC-025）。
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np

from application.algorithms.guidance.depth_guidance import (
    DepthGuidanceParams,
    contour_interior_points,
    filter_contours_final,
    run_depth_guidance,
)
from application.algorithms.image_ops import depth_to_rgb

# 处理图源（智能分割编码图）选项：code -> 中文标签（顺序即界面显示顺序）
SOURCE_LABELS = (
    ("color", "彩色图"),
    ("depth", "深度图"),
    ("fused", "融合图"),
)
SOURCE_CODES = tuple(c for c, _ in SOURCE_LABELS)

# 运算区域选项
OP_REGION_LABELS = (
    ("whole", "整张图"),
    ("roi", "ROI裁剪图"),
)
OP_REGION_CODES = tuple(c for c, _ in OP_REGION_LABELS)


def clip_roi(roi, height: int, width: int) -> Optional[tuple[int, int, int, int]]:
    """把 (x1,y1,x2,y2) 夹到图像范围内（x2/y2 开界）；无效返回 None。"""
    if not roi:
        return None
    x1, y1, x2, y2 = (int(round(float(v))) for v in roi[:4])
    x1 = max(0, min(x1, width - 1))
    x2 = max(x1 + 1, min(x2, width))
    y1 = max(0, min(y1, height - 1))
    y2 = max(y1 + 1, min(y2, height))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    return (x1, y1, x2, y2)


def run_depth_segmentation(
    depth_array: np.ndarray,
    depth_scale: float,
    params: DepthGuidanceParams,
    *,
    filter_mode: str = "所有轮廓",
    top_n: int = 1,
    k: int = 3,
    margin: float = 5.0,
    roi=None,
) -> dict[str, Any]:
    """深度分割：裁剪运算区域 → 步骤1 → 最终轮廓筛选 → 轮廓内提示点。

    Args:
        depth_array: 原始深度数组 (uint16)；None/空时返回空结果（不抛）。
        depth_scale: 米/原始单位。
        params: :class:`DepthGuidanceParams`（步骤 1 参数，含深度切分上下限 mm）。
        filter_mode: 最终轮廓筛选模式（:data:`SEG_FILTER_MODES` 之一）。
        top_n: 筛选保留的轮廓个数（>=1）。
        k: 每个轮廓最多取的提示点数（>=1）。
        margin: 除首点外提示点到边缘的最小距离（像素）。
        roi: (x1,y1,x2,y2) 运算区域；None 或 ``filter_mode`` 之外的整张图。

    Returns:
        dict:
            - contours: 最终轮廓列表（**运算区域坐标系**，np.ndarray）
            - prompts: 提示点分组（每组一个轮廓的 [(x,y), ...]，运算区域坐标系）
            - n_contours: 步骤 1 参数过滤后的轮廓数（筛选前）
            - gray / binary / filtered: 各阶段图（运算区域坐标系，便于调试预览）
            - roi_offset: (x1, y1)，运算区域左上角在原图中的偏移；整图时为 (0, 0)
            - depth_run: 实际参与运算的深度裁剪（原始值）
    """
    empty = {
        "contours": [], "prompts": [], "n_contours": 0,
        "gray": None, "binary": None, "filtered": None,
        "roi_offset": (0, 0), "depth_run": None,
    }
    if depth_array is None or np.asarray(depth_array).size == 0:
        return empty

    depth = np.asarray(depth_array)
    h, w = depth.shape[:2]
    box = clip_roi(roi, h, w)
    if roi and box is None:
        # 给了 ROI 但太小/越界：按整图处理（不静默失败，交给调用方判断）——这里如实返回空
        return empty
    if box is None:
        depth_run = depth
        offset = (0, 0)
    else:
        x1, y1, x2, y2 = box
        depth_run = depth[y1:y2, x1:x2]
        offset = (x1, y1)

    res = run_depth_guidance(depth_run, float(depth_scale), params,
                             color_image=None, include_rects=False)
    contours = filter_contours_final(res.get("contours", []),
                                     filter_mode, top_n=top_n)
    prompts: list[list[tuple[float, float]]] = []
    for c in contours:
        pts = contour_interior_points(c, k=int(k), margin=float(margin))
        if pts:
            prompts.append(pts)
    return {
        "contours": list(contours),
        "prompts": prompts,
        "n_contours": int(res.get("n_contours", 0)),
        "gray": res.get("gray"),
        "binary": res.get("binary"),
        "filtered": res.get("filtered"),
        "roi_offset": offset,
        "depth_run": depth_run,
    }


def offset_points(points, offset) -> list[tuple[float, float]]:
    """把运算区域坐标系的点整体平移到原图坐标系。"""
    ox, oy = int(offset[0]), int(offset[1])
    return [(float(x) + ox, float(y) + oy) for x, y in points]


def build_encoder_image(source: str, color_run, depth_run, depth_scale: float,
                        filtered) -> tuple[Optional[np.ndarray], str]:
    """按处理图源构造智能分割的编码图（RGB）。

    Args:
        source: ``color`` / ``depth`` / ``fused``（:data:`SOURCE_CODES` 之一）。
        color_run: 运算区域内的彩色图（RGB，可 None）。
        depth_run: 运算区域内的原始深度（uint16，可 None）。
        depth_scale: 米/原始单位。
        filtered: 深度分割的形态学滤波二值图（融合图用，可 None）。

    Returns:
        (image, note)：image 为 3 通道 RGB uint8；无法构造时 image=None，
        note 说明原因（供界面如实提示，不静默降级）。
    """
    code = source if source in SOURCE_CODES else "color"
    if code == "color":
        if color_run is None or np.asarray(color_run).size == 0:
            return None, "该帧无彩色图，处理图源=彩色图不可用"
        return np.ascontiguousarray(np.asarray(color_run)[:, :, :3]), "彩色图"
    if code == "depth":
        if depth_run is None or np.asarray(depth_run).size == 0:
            return None, "该帧无深度图，处理图源=深度图不可用"
        rgb = depth_to_rgb(np.asarray(depth_run), depth_scale_m=depth_scale)
        return np.ascontiguousarray(rgb), "深度伪彩图"
    # fused：彩色图 × 形态学白色掩码（掩码外置黑）
    if color_run is None or filtered is None:
        return None, "融合图需要彩色图 + 深度分割掩码：请先执行「深度分割」并确保有彩色帧"
    import cv2
    color = np.asarray(color_run)[:, :, :3]
    mask = np.asarray(filtered)
    if mask.shape[:2] != color.shape[:2]:
        mask = cv2.resize(mask, (color.shape[1], color.shape[0]),
                          interpolation=cv2.INTER_NEAREST)
    fused = np.ascontiguousarray(color.copy())
    fused[mask <= 0] = 0
    return fused, "融合图（彩色 × 深度分割掩码）"


__all__ = [
    "SOURCE_LABELS", "SOURCE_CODES", "OP_REGION_LABELS", "OP_REGION_CODES",
    "clip_roi", "run_depth_segmentation", "offset_points", "build_encoder_image",
]
