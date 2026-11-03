"""深度纯函数 (E14, 02 §8): 范围分割 / 有效比例 / ROI / 图元 / 像素->XYZ(mm)。

约定:
- 深度保持 uint16 原始值, 用 depth_scale_m 换算; 输出长度单位 mm;
- 空深度/无目标/低质量分别返回明确结果, 不用默认坐标冒充;
- ROI 映射只做一次, 保留原图偏移。
"""
from __future__ import annotations

from typing import Any

import numpy as np

from application.contracts.units import depth_raw_to_mm


def depth_valid_ratio(depth: np.ndarray, *, zero_is_invalid: bool = True) -> float:
    """深度有效比例 (0 视为无效)."""
    arr = np.asarray(depth)
    if arr.size == 0:
        return 0.0
    if zero_is_invalid:
        return float(np.count_nonzero(arr)) / float(arr.size)
    return 1.0


def roi_slice(roi: tuple[int, int, int, int], shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    """(x,y,w,h) -> (y0,y1,x0,x1), 并裁剪到图像边界 (越界即夹取, 不静默失败)。"""
    x, y, w, h = (int(v) for v in roi)
    if w <= 0 or h <= 0:
        raise ValueError(f"ROI 宽高必须为正: {roi!r}")
    height, width = int(shape[0]), int(shape[1])
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"ROI 完全越界: {roi!r} vs {shape}")
    return y0, y1, x0, x1


def depth_range_mask(depth: np.ndarray, depth_scale_m: float, *,
                     min_mm: float, max_mm: float) -> np.ndarray:
    """深度范围掩码. 0 值(无效)一律不进掩码。"""
    if min_mm > max_mm:
        raise ValueError(f"min_mm({min_mm}) > max_mm({max_mm})")
    arr = np.asarray(depth)
    if arr.dtype != np.uint16:
        raise ValueError(f"深度必须是 uint16 原始值, 得到 {arr.dtype}")
    mm = arr.astype(np.float64) * float(depth_scale_m) * 1000.0
    mask = (mm >= float(min_mm)) & (mm <= float(max_mm))
    mask &= arr > 0
    return mask


def mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """掩码非零区域包围盒 (x0,y0,x1,y1, 半开). 无目标返回 None。"""
    arr = np.asarray(mask).astype(bool)
    if not arr.any():
        return None
    ys, xs = np.where(arr)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def pixel_to_point_mm(u: float, v: float, depth_raw: float, depth_scale_m: float, *,
                      fx: float, fy: float, cx: float, cy: float) -> tuple[float, float, float]:
    """像素 + 原始深度 -> 相机系点 (mm). 深度为 0 或无效即拒绝 (不返回默认坐标)。"""
    z_mm = depth_raw_to_mm(depth_raw, depth_scale_m)
    if z_mm <= 0:
        raise ValueError(f"像素 ({u},{v}) 深度无效 (raw={depth_raw!r}), 拒绝生成坐标")
    if fx <= 0 or fy <= 0:
        raise ValueError("fx/fy 必须为正")
    x_mm = (float(u) - float(cx)) * z_mm / float(fx)
    y_mm = (float(v) - float(cy)) * z_mm / float(fy)
    return (x_mm, y_mm, z_mm)


def summarize_depth(depth: np.ndarray, depth_scale_m: float | None) -> dict[str, Any]:
    """深度摘要 (供质量评估/报告): 有效比例、有效范围内 min/max (mm)。"""
    arr = np.asarray(depth)
    valid = arr[arr > 0]
    out: dict[str, Any] = {
        "shape": tuple(int(s) for s in arr.shape),
        "dtype": str(arr.dtype),
        "valid_ratio": depth_valid_ratio(arr),
        "nonzero_count": int(valid.size),
    }
    if depth_scale_m and valid.size:
        mm = valid.astype(np.float64) * float(depth_scale_m) * 1000.0
        out["min_mm"] = float(mm.min())
        out["max_mm"] = float(mm.max())
    return out


__all__ = [
    "depth_range_mask", "depth_valid_ratio", "mask_bbox", "pixel_to_point_mm",
    "roi_slice", "summarize_depth",
]
