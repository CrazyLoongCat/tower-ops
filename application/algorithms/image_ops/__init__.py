# -*- coding: utf-8 -*-
"""图像算法纯函数包 (E19 增强, DEC-021)。

两层, 各司其职:

- :mod:`params` —— **唯一事实源**: 参数表 (:data:`PARAM_GROUPS`) + 取值对象
  (:class:`ImageOpsParams`)。界面控件、设备配置持久化、离线用例都从它派生。
- :mod:`ops` —— 纯函数实现 (ndarray -> ndarray)。不碰 Qt、不读配置、不改入参。

用法::

    from application.algorithms.image_ops import apply_chain, coerce_params

    params = coerce_params(camera_entry.image_ops)   # 按通道取回上次保存的参数
    result = apply_chain(color_frame, depth_frame, params,
                         depth_scale_m=frame.depth_scale_m)   # 深度切分上下限按 mm 给
    result.color   # 显示用 RGB 图 (原帧未被改动)
    result.detections, result.params, result.notes

**作用范围**: 只影响**显示预览**; 不改变原始帧、不喂给判定链路。
"""
from __future__ import annotations

from .ops import (
    DEFAULT_DEPTH_SCALE_M,
    DetectionItem,
    DetectionSummary,
    ImageOpsResult,
    apply_brightness_contrast,
    apply_chain,
    apply_geometry,
    as_rgb,
    binarize_image,
    canny,
    denoise,
    depth_to_rgb,
    describe,
    detect,
    draw_crosshair,
    draw_detections,
    format_detections,
    morphology_image,
    param_label,
    to_gray,
)
from .params import (
    PANEL_GROUPS,
    PANEL_KEYS,
    PARAM_GROUPS,
    PARAM_KEYS,
    PARAM_SPECS,
    SCOPES,
    ImageOpsParams,
    coerce_params,
    dataclass_field_names,
    default_params,
    depth_display_range_mm,
    group_of,
    keys_in_scope,
    param_spec,
)

__all__ = [
    # params (唯一事实源)
    "PARAM_GROUPS", "PARAM_KEYS", "PARAM_SPECS", "PANEL_GROUPS", "PANEL_KEYS",
    "SCOPES", "ImageOpsParams", "coerce_params", "dataclass_field_names",
    "default_params", "depth_display_range_mm", "group_of", "keys_in_scope",
    "param_spec",
    # ops (纯函数)
    "DEFAULT_DEPTH_SCALE_M", "DetectionItem", "DetectionSummary", "ImageOpsResult",
    "apply_brightness_contrast", "apply_chain", "apply_geometry", "as_rgb",
    "binarize_image", "canny", "denoise", "depth_to_rgb", "describe", "detect",
    "draw_crosshair", "draw_detections", "format_detections", "morphology_image",
    "param_label", "to_gray",
]
