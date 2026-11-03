"""像素 → 相机系三维坐标（**1:1 移植**自旧工程 `guidance_locate_tab.py`）。

- :func:`pixel_to_xyz`：深度 + 像素 + 内参 → 相机系 (X,Y,Z) 米（兼容 ppx/ppy 与 cx/cy 命名）；
- :func:`compute_pixel_xyz`：先取该点深度，无效则**螺旋搜索**邻近有效深度，再换算为毫米。

移植原则：判据与搜索半径（1~7 像素）原样保留，勿改。
"""
from __future__ import annotations

from typing import Any, Mapping

SPIRAL_MAX_RADIUS = 7          # 旧实现固定半径上限(像素)


def pixel_to_xyz(depth_value_m: float, x: float, y: float,
                 intrinsics: Mapping[str, Any]) -> tuple[float, float, float]:
    """像素坐标 + 深度(米) → 相机系 (X,Y,Z) 米。

    兼容 D435I(`ppx/ppy`) 与 DS86(`cx/cy`) 两种内参字段命名。
    """
    if depth_value_m <= 0:
        return (0.0, 0.0, 0.0)
    fx = intrinsics["fx"]
    fy = intrinsics["fy"]
    ppx = intrinsics.get("ppx", intrinsics.get("cx", 0.0))
    ppy = intrinsics.get("ppy", intrinsics.get("cy", 0.0))
    x_m = (x - ppx) * depth_value_m / fx
    y_m = (y - ppy) * depth_value_m / fy
    return (x_m, y_m, depth_value_m)


def compute_pixel_xyz(x: float, y: float, depth_array: Any, intrinsics: Any,
                      depth_scale: float):
    """计算像素坐标的 XYZ（**毫米**），含螺旋搜索有效深度。

    返回 ``(x_mm, y_mm, z_mm, search_rect)`` 或 ``None``。
    """
    if depth_array is None or intrinsics is None:
        return None
    h, w = depth_array.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return None

    def _depth_at(px: int, py: int) -> float:
        try:
            return float(depth_array[py, px]) * depth_scale
        except Exception:  # noqa: BLE001
            return 0.0

    depth_m = _depth_at(int(x), int(y))
    search_rect = None
    if depth_m <= 0:
        found = 0.0
        found_r = 0
        for r in range(1, SPIRAL_MAX_RADIUS + 1):
            stop = False
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    nx, ny = int(x) + dx, int(y) + dy
                    if 0 <= nx < w and 0 <= ny < h and _depth_at(nx, ny) > 0:
                        found = _depth_at(nx, ny)
                        stop = True
                        break
                if stop:
                    break
            if stop:
                found_r = r
                break
        depth_m = found
        if found_r > 0:
            search_rect = (int(x) - found_r, int(y) - found_r,
                           int(x) + found_r, int(y) + found_r)
    if depth_m <= 0:
        return None
    cx, cy, cz = pixel_to_xyz(depth_m, x, y, intrinsics)
    return (cx * 1000.0, cy * 1000.0, cz * 1000.0, search_rect)


__all__ = ["SPIRAL_MAX_RADIUS", "compute_pixel_xyz", "pixel_to_xyz"]
