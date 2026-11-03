"""圆检测流水线 (12 §4.4, 02 §8.1, A10).

Frame -> 质量检查 -> ROI -> 圆检测 -> 判定. 绑定算法版本/宿主/输入Schema/参数hash,
结果携带完整来源 (run/station/step/frame/capture), 供迟到隔离 (T16).
"""
from __future__ import annotations

from dataclasses import dataclass

from application.algorithms.circle.detector import (
    CircleBackend,
    CircleDecisionConfig,
    CircleDetector,
)
from application.algorithms.circle.params import CircleParams, RoiRect
from application.contracts.enums import ImageSpace
from application.contracts.frames import FrameBundle
from application.contracts.results import DetectionResult


@dataclass
class CirclePipelineSpec:
    """可发布的流水线定义 (草稿/发布前的数据)."""

    pipeline_id: str
    version: str
    params: CircleParams
    decision: CircleDecisionConfig
    roi: RoiRect | None = None
    image_space: ImageSpace = ImageSpace.COLOR
    host: str = "in-process"


class CirclePipeline:
    """绑定后端与规范的可执行流水线. 无硬件运动副作用."""

    def __init__(self, spec: CirclePipelineSpec, backend: CircleBackend) -> None:
        self.spec = spec
        self._detector = CircleDetector(
            backend,
            image_space=spec.image_space,
            pipeline_id=spec.pipeline_id,
            pipeline_version=spec.version,
            coordinates_are_full_image=True,
        )

    @property
    def pipeline_id(self) -> str:
        return self.spec.pipeline_id

    @property
    def version(self) -> str:
        return self.spec.version

    def params_hash(self) -> str:
        return self.spec.params.params_hash()

    def run(
        self,
        frame: FrameBundle,
        *,
        result_id: str,
        cancelled: bool = False,
        host: str | None = None,
    ) -> DetectionResult:
        return self._detector.run(
            frame, self.spec.params, self.spec.decision,
            result_id=result_id, roi=self.spec.roi,
            cancelled=cancelled, host=host or self.spec.host,
        )


__all__ = ["CirclePipeline", "CirclePipelineSpec"]
