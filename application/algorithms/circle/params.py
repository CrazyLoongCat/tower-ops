"""圆/孔检测参数与 ROI (12 §4.1, 02 §8).

参数顺序/名称对应既有 detect_circle_imp, 但由项目定义 Schema 与 hash.
ROI 点回到原图时携带偏移与缩放; 缩放/ROI 映射只转换一次.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class RoiRect:
    """运算区域, 原生格式 [type, x, y, w, h, angle]."""

    type: int = 2
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    angle: int = 0

    def to_native(self) -> list[int]:
        return [self.type, self.x, self.y, self.w, self.h, self.angle]


@dataclass(frozen=True)
class CircleParams:
    """圆检测参数. 默认值仅离线参考, 真实阈值由 P01/样本确认."""

    output_type: str = "1轮廓图"
    circle_type: str = "最大圆"
    output_point: str = "是"
    min_radius: float = 0.0
    max_radius: float = 999999.9
    min_cover_ratio: float = 0.3
    max_cover_ratio: float = 1.0
    grad_thresh: float = 50.0
    anchor_thresh: float = 8.0
    min_path_len: float = 10.0
    guass_sigma: float = 1.0
    arc_fit_error: float = 1.0
    circle_fit_error: float = 2.0
    min_arc_points: int = 0
    max_arc_points: int = 999999
    circle_validation_pro: float = 0.1
    resize_factor: float = 1.0
    thread_num: int = 2
    algorithm_version: str = "detect_circle/legacy-port"

    def to_inputs(self) -> dict:
        """转成原生 inputs 字典 (0/1/2 为图像与 rects, 由调用方填充)."""
        return {
            "3": self.output_type, "4": self.circle_type, "5": self.output_point,
            "6": self.min_radius, "7": self.max_radius,
            "8": self.min_cover_ratio, "9": self.max_cover_ratio,
            "10": self.grad_thresh, "11": self.anchor_thresh,
            "12": self.min_path_len, "13": self.guass_sigma,
            "14": self.arc_fit_error, "15": self.circle_fit_error,
            "16": self.min_arc_points, "17": self.max_arc_points,
            "18": self.circle_validation_pro, "19": self.resize_factor,
            "20": self.thread_num,
        }

    def params_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = ["CircleParams", "RoiRect"]
