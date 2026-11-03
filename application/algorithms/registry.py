"""算法注册与描述子 (E14, 02 §8.1)。

Descriptor 声明: 算法 id/版本、输入要求、参数 Schema、输出 Schema、执行宿主、
取消方式与模型依赖。运行前校验 FrameBundle 的原始深度/对齐空间/颜色格式与标定,
不只检查“数组是否存在”。未知算法在发布前拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from application.domain.errors import ValidationFailure


@dataclass(frozen=True)
class AlgorithmDescriptor:
    """算法能力描述。宿主为 'in-process' 或隔离宿主 (如 'circle38')。"""

    algorithm_id: str
    version: str
    requires: tuple[str, ...] = ("color",)      # color / depth
    params_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    host: str = "in-process"
    cancellable: bool = True
    model_refs: tuple[str, ...] = ()
    requires_depth_metrics: bool = False         # 需要 depth_scale_m + 内参
    requires_calibration: bool = False
    notes: str = ""

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.algorithm_id.strip():
            problems.append("algorithm_id 不能为空")
        if not self.version.strip():
            problems.append(f"{self.algorithm_id}: version 不能为空")
        for req in self.requires:
            if req not in ("color", "depth"):
                problems.append(f"{self.algorithm_id}: 未知输入要求 {req!r}")
        if not self.output_schema:
            problems.append(f"{self.algorithm_id}: 未声明输出 Schema")
        return problems


class UnknownAlgorithm(ValidationFailure):
    """引用了未注册算法 (发布前拒绝)。"""


class AlgorithmRegistry:
    """算法目录. 未注册即拒绝; 能力发现不隐式安装依赖。"""

    def __init__(self, descriptors: list[AlgorithmDescriptor] | None = None) -> None:
        self._items: dict[str, AlgorithmDescriptor] = {}
        for d in (descriptors or []):
            self.register(d)

    def register(self, descriptor: AlgorithmDescriptor) -> None:
        problems = descriptor.validate()
        if problems:
            raise ValidationFailure("; ".join(problems))
        if descriptor.algorithm_id in self._items:
            raise ValidationFailure(f"算法 {descriptor.algorithm_id!r} 已注册")
        self._items[descriptor.algorithm_id] = descriptor

    def get(self, algorithm_id: str) -> AlgorithmDescriptor:
        d = self._items.get(algorithm_id)
        if d is None:
            raise UnknownAlgorithm(f"未注册算法: {algorithm_id!r}")
        return d

    def known(self, algorithm_id: str) -> bool:
        return algorithm_id in self._items

    def ids(self) -> list[str]:
        return sorted(self._items)

    def descriptors(self) -> list[AlgorithmDescriptor]:
        return [self._items[k] for k in self.ids()]


def check_frame_for(descriptor: AlgorithmDescriptor, frame) -> None:
    """运行前检查输入: 原始深度/对齐空间/颜色格式/内参与 depth_scale。"""
    if "color" in descriptor.requires and frame.color is None:
        raise ValidationFailure(f"{descriptor.algorithm_id}: 缺彩色图")
    if "depth" in descriptor.requires:
        if frame.depth is None:
            raise ValidationFailure(f"{descriptor.algorithm_id}: 缺原始深度")
        if frame.depth.dtype != "uint16":
            raise ValidationFailure(
                f"{descriptor.algorithm_id}: 深度必须是 uint16 原始值, 得到 {frame.depth.dtype}")
    if descriptor.requires_depth_metrics:
        try:
            frame.require_depth_metrics()      # 缺 depth_scale_m 即拒绝, 不默认成功
        except ValueError as exc:
            raise ValidationFailure(f"{descriptor.algorithm_id}: {exc}") from exc
        if frame.intrinsics is None:
            raise ValidationFailure(f"{descriptor.algorithm_id}: 缺内参(未对齐/未匹配)")
    if descriptor.requires_calibration and not frame.calibration_version:
        raise ValidationFailure(f"{descriptor.algorithm_id}: 缺标定版本")


def default_registry() -> AlgorithmRegistry:
    """内置算法描述 (E14): 圆/孔(3.8宿主)、深度范围、线测距。"""
    return AlgorithmRegistry([
        AlgorithmDescriptor(
            algorithm_id="circle.detect", version="legacy-port/1",
            requires=("color",), host="circle38", cancellable=True,
            params_schema={"min_radius": "float>=0", "max_radius": "float",
                           "grad_thresh": "float", "resize_factor": "float>0"},
            output_schema={"targets": "list[CirclePrimitive]", "status": "DetectionStatus"},
            notes="必须经独立 3.8 宿主调用, 主进程不导入原生件"),
        AlgorithmDescriptor(
            algorithm_id="depth.range_segment", version="v1",
            requires=("depth",), host="in-process",
            params_schema={"min_mm": "float", "max_mm": "float"},
            output_schema={"mask_ref": "ndarray[bool]", "bbox_px": "tuple|None",
                           "valid_ratio": "float"},
            requires_depth_metrics=True),
        AlgorithmDescriptor(
            algorithm_id="depth.pixel_to_xyz", version="v1",
            requires=("depth",), host="in-process",
            params_schema={"u": "int", "v": "int"},
            output_schema={"point_mm": "tuple[float,float,float]"},
            requires_depth_metrics=True),
        AlgorithmDescriptor(
            algorithm_id="line.measure", version="v1",
            requires=("color",), host="in-process",
            params_schema={"points": "list[(u,v)]"},
            output_schema={"length_px": "float", "length_mm": "float|None"},
            requires_depth_metrics=False,
            notes="有深度+内参时才给 mm; 否则只给 px, 不冒充 mm"),
    ])


__all__ = [
    "AlgorithmDescriptor", "AlgorithmRegistry", "UnknownAlgorithm",
    "check_frame_for", "default_registry",
]
