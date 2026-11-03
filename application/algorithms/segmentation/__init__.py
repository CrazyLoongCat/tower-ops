# -*- coding: utf-8 -*-
"""视觉分割 / 运行一次的**算法层**（E19 增强）。

来自旧工程 `Ganta_Crtl_System/vision_monitor/ui/s100_detect_tab.py` 的检测流程：

    画多边形 → 深度分割（提示点）→ 智能分割（EdgeSAM 点提示 → 多边形）→ 运行一次（交集）

各层各司其职：

- :mod:`depth_seg` —— 深度分割：深度切分→归一化→二值化→形态学→轮廓过滤→
  最终轮廓筛选→轮廓内提示点。**判据复用** :mod:`application.algorithms.guidance.depth_guidance`
  （1:1 移植自旧程序的同一套函数），这里只做编排，不另写算法。
- :mod:`edge_sam` —— 智能分割：EdgeSAM 两段式 ONNX 推理（点 prompt → mask → 多边形），
  惰性导入 onnxruntime；离线栈用确定性 :class:`StubSegmenter` 顶替（明确标注，不冒充模型）。
- :mod:`intersect` —— 运行一次：智能分割多边形 mask ∩ 用户多边形 mask → 交集轮廓/面积。
- :mod:`scheme` —— :class:`RunOnceScheme`（把上面三步的参数固化成一份"方案"）与
  :func:`run_scheme`（在一帧上真实执行整条逻辑）。

**作用范围**：只读原始帧；产出提示点/多边形/交集轮廓供显示与判定，**不改原始帧**、
不写配置。长度/面积一律**像素**，不换算 mm（见设计基线 03 §7.2）。
"""
from __future__ import annotations

from .depth_seg import (
    OP_REGION_CODES,
    OP_REGION_LABELS,
    SOURCE_CODES,
    SOURCE_LABELS,
    build_encoder_image,
    clip_roi,
    offset_points,
    run_depth_segmentation,
)
from .edge_sam import (
    EdgeSamSegmenter,
    Segmenter,
    StubSegmenter,
    check_available as edge_sam_check_available,
    default_segmenter,
    mask_primary_component,
    mask_to_polygons,
    models_available as edge_sam_models_available,
    resolve_models_dir,
)
from .intersect import (
    contour_area_sum,
    intersection_result,
    mask_from_polygon,
    mask_to_contours,
)
from .scheme import (
    DEFAULT_MIN_AREA,
    RunOnceResult,
    RunOnceScheme,
    SCHEME_SCHEMA_VERSION,
    coerce_scheme,
    has_scheme,
    run_scheme,
    scheme_from_params,
)

__all__ = [
    # depth_seg
    "OP_REGION_CODES", "OP_REGION_LABELS", "SOURCE_CODES", "SOURCE_LABELS",
    "build_encoder_image", "clip_roi", "offset_points", "run_depth_segmentation",
    # edge_sam
    "EdgeSamSegmenter", "Segmenter", "StubSegmenter", "resolve_models_dir",
    "edge_sam_check_available", "edge_sam_models_available", "default_segmenter",
    "mask_primary_component", "mask_to_polygons",
    # intersect
    "contour_area_sum", "intersection_result", "mask_from_polygon", "mask_to_contours",
    # scheme
    "DEFAULT_MIN_AREA", "RunOnceResult", "RunOnceScheme", "SCHEME_SCHEMA_VERSION",
    "coerce_scheme", "has_scheme", "run_scheme", "scheme_from_params",
]
