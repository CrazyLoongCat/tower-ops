"""圆检测判定与执行 (12 §4.4, 02 §8.2, T14/T16/T38).

六态 PASS/FAIL/NO_TARGET/INDETERMINATE/ERROR/CANCELLED 不混;
不自动生成视觉运动目标; 迟到/取消结果由上层按来源作用域隔离.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from application.algorithms.circle.normalize import (
    NativeResultError,
    apply_roi_and_scale,
    parse_native_circles,
    to_image_space,
)
from application.algorithms.circle.params import CircleParams, RoiRect
from application.contracts.enums import DetectionStatus, ImageSpace
from application.contracts.frames import FrameBundle
from application.contracts.results import CirclePrimitive, DetectionResult, MetricMeasurement


class CircleBackend(Protocol):
    """圆检测后端: 进程内 stub / 独立 3.8 宿主代理 均实现此协议.

    ``rects`` 约定为**本帧扁平矩形列表**（每个矩形 ``[type,x,y,w,h,angle]``，
    见 ``RoiRect.to_native``）：
    - 空列表 = 本帧不限定区域（宿主侧不可用——DEC-055 起检测一律送真实矩形，
      无 ROI 时送**全图矩形**，与旧工程 ``detect_circle_imp([img], [[rect]])``
      的现场调用一致）；
    - 后端负责把它展开成原生 ``inputs['1']/['2']`` 的**按图包裹**形态
      （``NativeCircleBackend`` 加一层；``Circle38Proxy`` 原样放进报文，
      3.8 宿主端再加一层）。
    不能把 ``[[]]``（空矩形）当作"不限区域"——那在原生侧是**一个空矩形**，
    曾导致返回结构多包一层（DEC-055 现场根因）。
    """

    def detect(self, images: list[Any], rects: list[list[int]],
               params: CircleParams) -> dict: ...


def full_image_rect(shape: tuple[int, ...]) -> list[int]:
    """整幅图（含内参对齐图）对应的原生矩形 ``[2, 0, 0, w, h, 0]``。

    旧工程任何一次找圆都带真实矩形（如 ``[2, 328, 309, 150, 137, 0]`` 或
    整图 ``[2, 0, 0, w, h, 0]``）；不带矩形时 3.8 原生件的返回包裹层数会变
    （现场 ``circles_for_show`` 出现 ``[[[]]]``，DEC-055）。
    """
    h = int(shape[0]) if len(shape) > 0 else 0
    w = int(shape[1]) if len(shape) > 1 else 0
    return [2, 0, 0, w, h, 0]


@dataclass
class CircleDecisionConfig:
    """判定阈值. 默认值仅离线参考, 真实值由 P01/代表样本确认."""

    expected_count_min: int = 1
    expected_count_max: int = 10
    radius_min_px: float = 0.0
    radius_max_px: float = 1e9
    min_coverage: float = 0.0
    min_depth_valid_ratio: float = 0.0   # 高于此才判定, 否则 INDETERMINATE


def meets_decision(circle: CirclePrimitive, decision: CircleDecisionConfig) -> bool:
    """圆是否满足半径/完整度判据。

    流水线整图判定与「深度矩形逐框找圆」判定（DEC-056）共用这一份口径，
    不要各写一份阈值比较。
    """
    if not (decision.radius_min_px <= circle.radius_px <= decision.radius_max_px):
        return False
    if circle.coverage is not None and circle.coverage < decision.min_coverage:
        return False
    return True


class CircleDetector:
    """执行一次圆检测并规范化/判定. 无硬件运动副作用."""

    def __init__(self, backend: CircleBackend, *, image_space: ImageSpace = ImageSpace.COLOR,
                 pipeline_id: str = "circle_pipeline", pipeline_version: str = "v1",
                 coordinates_are_full_image: bool = True) -> None:
        self._backend = backend
        self._image_space = image_space
        self._pipeline_id = pipeline_id
        self._pipeline_version = pipeline_version
        self._coords_full = coordinates_are_full_image

    def run(
        self,
        frame: FrameBundle,
        params: CircleParams,
        decision: CircleDecisionConfig | None = None,
        *,
        result_id: str,
        roi: RoiRect | None = None,
        roi_offset_scale: tuple[tuple[int, int, int, int] | None, float] = (None, 1.0),
        cancelled: bool = False,
        host: str = "in-process",
    ) -> DetectionResult:
        decision = decision or CircleDecisionConfig()
        base = dict(
            result_id=result_id,
            capture_id=frame.capture_id,
            frame_id=frame.frame_id,
            run_id=frame.run_id,
            station_instance_id=frame.station_instance_id,
            step_id=frame.step_id,
            pipeline_id=self._pipeline_id,
            pipeline_version=self._pipeline_version,
            algorithm_version=params.algorithm_version,
            host=host,
            params_hash=params.params_hash(),
            image_space=self._image_space,
        )

        if cancelled:
            return DetectionResult(status=DetectionStatus.CANCELLED, reason="取消", **base)

        # 输入校验: 需要彩色原图.
        if frame.color is None:
            return DetectionResult(status=DetectionStatus.ERROR, reason="缺彩色原图",
                                   errors=("no_color",), **base)

        # 质量门: 深度有效比例过低 -> INDETERMINATE (不冒充通过, 也不当失败).
        if (decision.min_depth_valid_ratio > 0 and frame.depth_valid_ratio is not None
                and frame.depth_valid_ratio < decision.min_depth_valid_ratio):
            return DetectionResult(
                status=DetectionStatus.INDETERMINATE,
                reason=f"深度有效比例 {frame.depth_valid_ratio:.3f} 低于阈值",
                metrics={"depth_valid_ratio": frame.depth_valid_ratio},
                **base,
            )

        # DEC-055：**始终**送真实矩形。无 ROI 时送全图矩形（旧工程现场调用口径），
        # 不再送 ``[[]]``——空列表经宿主会被当成"一个空矩形"，原生返回多包一层。
        rects = ([roi.to_native()] if roi is not None
                 else [full_image_rect(frame.color.shape)])
        image = frame.color.data
        try:
            raw = self._backend.detect([image], rects, params)
        except Exception as exc:  # 后端异常 -> ERROR, 不伪造坐标
            return DetectionResult(status=DetectionStatus.ERROR,
                                   reason=f"圆检测后端异常: {exc}",
                                   errors=(str(exc),), **base)

        try:
            circles, metrics = parse_native_circles(raw)
        except NativeResultError as exc:
            return DetectionResult(status=DetectionStatus.ERROR,
                                   reason=f"原生返回畸形: {exc}",
                                   errors=(str(exc),), **base)

        offset, scale = roi_offset_scale
        circles = apply_roi_and_scale(circles, roi=offset, scale=scale,
                                      coordinates_are_full_image=self._coords_full)
        circles = to_image_space(circles, self._image_space)

        measurements = [
            MetricMeasurement(name="radius", value=c.radius_px, unit="px")
            for c in circles
        ]

        if not circles:
            return DetectionResult(status=DetectionStatus.NO_TARGET,
                                   reason="未检出圆/孔",
                                   measurements=[], metrics=metrics, **base)

        passing = [c for c in circles if self._meets(c, decision)]
        metrics = {**metrics, "candidate_count": len(circles), "passing_count": len(passing)}

        if not passing:
            return DetectionResult(status=DetectionStatus.FAIL,
                                   reason="检出圆均不满足半径/完整度判据",
                                   targets=circles, measurements=measurements,
                                   metrics=metrics, **base)

        if decision.expected_count_min <= len(passing) <= decision.expected_count_max:
            return DetectionResult(status=DetectionStatus.PASS,
                                   reason=f"合格圆数 {len(passing)} 在期望范围",
                                   targets=passing, measurements=measurements,
                                   metrics=metrics, **base)

        return DetectionResult(status=DetectionStatus.FAIL,
                               reason=f"合格圆数 {len(passing)} 超出期望范围 "
                                      f"[{decision.expected_count_min},{decision.expected_count_max}]",
                               targets=passing, measurements=measurements,
                               metrics=metrics, **base)

    @staticmethod
    def _meets(c: CirclePrimitive, d: CircleDecisionConfig) -> bool:
        return meets_decision(c, d)


class InProcessStubBackend:
    """离线测试用后端: 返回预置的原生结构; 不加载任何原生件."""

    def __init__(self, response: dict | None = None) -> None:
        self._response = response
        self.calls: list[tuple] = []

    def set_response(self, response: dict) -> None:
        self._response = response

    def detect(self, images, rects, params) -> dict:
        self.calls.append((len(images), rects, params.params_hash()))
        if self._response is None:
            raise RuntimeError("stub 后端未设置返回")
        return self._response


def native_from_circles(circles: list[tuple[float, float, float]],
                        *, statues=1, coverages: list[float] | None = None) -> dict:
    """便捷构造与原生同构的返回 (仅测试用)."""
    show = [[1, cx, cy, r] for (cx, cy, r) in circles]
    centers = [[[0, cx, cy] for (cx, cy, r) in circles]]  # circle_centers[0] = 中心列表
    radii = [[r for (cx, cy, r) in circles]]              # radius[0] = 半径列表
    cov = [coverages] if coverages else []
    return {
        "statues": [statues], "error_msg": [""],
        "circle_num": [[len(circles)]],
        "circles": [None], "circle_centers": centers, "radius": radii,
        "diameters": [None], "circle_points": [None],
        "circle_coverage": cov, "output_images": [None],
        "circles_for_show": [show],
    }


__all__ = [
    "CircleBackend", "CircleDecisionConfig", "CircleDetector",
    "InProcessStubBackend", "full_image_rect", "meets_decision",
    "native_from_circles",
]
