"""检测结果契约 (12 §4.1, 02 §8.2, T14/T16).

六态 PASS/FAIL/NO_TARGET/INDETERMINATE/ERROR/CANCELLED 不混.
结果绑定 frame/capture 与来源作用域; 取消/换工位后的旧结果只存档 (T16).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from application.contracts.enums import DetectionStatus, ImageSpace

# 注意: ids 依赖 pydantic (3.9+ 的 typing.Annotated)。本模块在 **3.8 圆检测宿主**
# 的导入路径上, 绝不能引入 pydantic —— 故只在类型检查期 import, 运行期用裸 str。
# (文件已 `from __future__ import annotations`, 注解是惰性字符串, 运行期无需这些名字。)
if TYPE_CHECKING:
    from application.contracts.ids import (
        CaptureId,
        FrameId,
        ResultId,
        RunId,
        StationInstanceId,
    )


@dataclass(frozen=True)
class CirclePrimitive:
    """圆图元. 坐标在 image_space 内, 半径标 px (非 mm)."""

    cx: float
    cy: float
    radius_px: float
    image_space: ImageSpace = ImageSpace.COLOR
    coverage: float | None = None
    flag: int | None = None

    def __post_init__(self) -> None:
        if self.radius_px < 0:
            raise ValueError("半径 px 不能为负")


@dataclass(frozen=True)
class PointPrimitive:
    x: float
    y: float
    image_space: ImageSpace = ImageSpace.COLOR


@dataclass(frozen=True)
class MetricMeasurement:
    """度量结果. 明确单位, 像素指标标 px, 不冒充 mm (02 §8.2)."""

    name: str
    value: float
    unit: Literal["px", "mm", "deg", "ratio"]


@dataclass
class DetectionResult:
    """检测结果: 目标列表 + 单位 + 质量 + 判定原因 + 来源/版本 (02 §8.2)."""

    result_id: str
    status: DetectionStatus
    # 来源绑定
    capture_id: str | None = None
    frame_id: str | None = None
    run_id: str | None = None
    station_instance_id: str | None = None
    step_id: str | None = None
    # 版本/宿主
    pipeline_id: str | None = None
    pipeline_version: str | None = None
    algorithm_version: str | None = None
    host: str | None = None
    params_hash: str | None = None
    # 内容
    image_space: ImageSpace = ImageSpace.COLOR
    targets: list[Any] = field(default_factory=list)
    measurements: list[MetricMeasurement] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    overlays: list[Any] = field(default_factory=list)
    reason: str | None = None
    errors: tuple[str, ...] = ()

    @property
    def is_pass(self) -> bool:
        return self.status is DetectionStatus.PASS

    @property
    def is_terminal_negative(self) -> bool:
        """FAIL/NO_TARGET/INDETERMINATE 需进入已批准复核/重抓拍/终止策略 (12 §4.4)."""
        return self.status in (
            DetectionStatus.FAIL,
            DetectionStatus.NO_TARGET,
            DetectionStatus.INDETERMINATE,
        )

    def belongs_to(self, run_id: str | None, station_instance_id: str | None) -> bool:
        """来源作用域检查: 迟到/换工位结果不得触发新动作 (T16)."""
        return self.run_id == run_id and self.station_instance_id == station_instance_id


__all__ = [
    "CirclePrimitive",
    "DetectionResult",
    "MetricMeasurement",
    "PointPrimitive",
]
