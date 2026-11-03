"""线检测与测距纯函数 (E14, 02 §8)。

单位纪律: **只有具备深度+内参时才输出 mm**; 否则只给 px, 绝不把像素间距标成 mm。
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

from application.algorithms.depth.depth_ops import pixel_to_point_mm


def distance_px(p1: Sequence[float], p2: Sequence[float]) -> float:
    """两点像素距离 (px)."""
    dx = float(p2[0]) - float(p1[0])
    dy = float(p2[1]) - float(p1[1])
    return float((dx * dx + dy * dy) ** 0.5)


def px_to_mm(px: float, depth_raw: float, depth_scale_m: float, fx: float) -> float:
    """像素长度 -> 毫米 (小孔模型, 取该处深度). 深度无效即拒绝。"""
    if float(px) < 0:
        raise ValueError("像素长度不能为负")
    z_mm = float(depth_raw) * float(depth_scale_m) * 1000.0
    if z_mm <= 0:
        raise ValueError(f"深度无效 (raw={depth_raw!r}), 不输出 mm")
    if float(fx) <= 0:
        raise ValueError("fx 必须为正")
    return float(px) * z_mm / float(fx)


def measure_length(p1: Sequence[float], p2: Sequence[float], *,
                   depth_raw: float | None = None,
                   depth_scale_m: float | None = None,
                   fx: float | None = None) -> dict:
    """测距: 始终给 px; 仅当深度与内参齐全时附加 mm (显式单位)。"""
    px = distance_px(p1, p2)
    out = {"length_px": px, "length_mm": None,
           "unit_note": "px only (缺深度/内参)"}
    if depth_raw is not None and depth_scale_m is not None and fx is not None:
        out["length_mm"] = px_to_mm(px, depth_raw, depth_scale_m, fx)
        out["unit_note"] = "px + mm"
    return out


def point_mm(u: float, v: float, depth_raw: float, depth_scale_m: float, *,
             fx: float, fy: float, cx: float, cy: float):
    """端点三维坐标 (mm) —— 供测距做空间校验。"""
    return pixel_to_point_mm(u, v, depth_raw, depth_scale_m, fx=fx, fy=fy, cx=cx, cy=cy)


def fit_line(points: Sequence[Sequence[float]]) -> dict:
    """最小二乘拟合直线: 返回斜率/截距/rms (px). 少于 2 点即拒绝。"""
    pts = np.asarray(points, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2 or pts.shape[0] < 2:
        raise ValueError(f"拟合需要至少 2 个 (u,v) 点, 得到 {pts.shape}")
    x, y = pts[:, 0], pts[:, 1]
    if np.ptp(x) == 0:
        return {"slope": None, "intercept": float(x[0]), "vertical": True,
                "rms_px": float(np.std(y))}
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    return {"slope": float(slope), "intercept": float(intercept),
            "vertical": False, "rms_px": float(np.sqrt(np.mean((y - pred) ** 2)))}


__all__ = ["distance_px", "fit_line", "measure_length", "point_mm", "px_to_mm"]
