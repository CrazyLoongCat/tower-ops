"""单位与显式换算 (12 §4.1, 02 §1.1).

领域层: 长度 mm, 角度 deg, 时长 ms. SDK 边界显式转换, 不隐式混用.
深度保留 uint16 原始值, 乘 depth_scale_m 得米, 再由转换输出 mm.
"""
from __future__ import annotations

import math

LENGTH_UNIT = "mm"
ANGLE_UNIT = "deg"
DURATION_UNIT = "ms"


def mm_to_m(value_mm: float) -> float:
    """毫米 -> 米 (仅 SDK 边界使用)."""
    return float(value_mm) / 1000.0


def m_to_mm(value_m: float) -> float:
    """米 -> 毫米."""
    return float(value_m) * 1000.0


def deg_to_rad(value_deg: float) -> float:
    """角度 -> 弧度 (仅 SDK 边界使用)."""
    return math.radians(float(value_deg))


def rad_to_deg(value_rad: float) -> float:
    """弧度 -> 角度."""
    return math.degrees(float(value_rad))


def ms_to_s(value_ms: float) -> float:
    return float(value_ms) / 1000.0


def s_to_ms(value_s: float) -> float:
    return float(value_s) * 1000.0


def depth_raw_to_m(raw: float, depth_scale_m: float) -> float:
    """原始深度 (uint16) -> 米. depth_scale_m 必须为正的真实比例."""
    scale = float(depth_scale_m)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError(f"depth_scale_m 必须为有限正值, 得到 {depth_scale_m!r}")
    return float(raw) * scale


def depth_raw_to_mm(raw: float, depth_scale_m: float) -> float:
    """原始深度 (uint16) -> 毫米 (领域层输出单位)."""
    return m_to_mm(depth_raw_to_m(raw, depth_scale_m))


def depth_mm_to_raw(value_mm: float, depth_scale_m: float) -> float:
    """毫米 -> 原始深度值 (uint16 量纲, ``depth_raw_to_mm`` 的反向换算).

    用于把**按毫米给的**深度阈值/切分范围落回原始数组量纲
    (界面上深度切分上下限一律以 mm 给值, 见 DEC-025); 比例非法即拒绝, 不猜默认值。
    """
    scale = float(depth_scale_m)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError(f"depth_scale_m 必须为有限正值, 得到 {depth_scale_m!r}")
    return float(value_mm) / (scale * 1000.0)


__all__ = [
    "ANGLE_UNIT", "DURATION_UNIT", "LENGTH_UNIT", "deg_to_rad",
    "depth_mm_to_raw", "depth_raw_to_m", "depth_raw_to_mm", "m_to_mm", "mm_to_m",
    "ms_to_s",
    "rad_to_deg", "s_to_ms",
]
