"""统一相机与抓拍契约 (12 §4.1, 02 §6, T07/T12/T13/T21).

FrameBundle 必含来源/序号/epoch、dtype/shape/stride/nbytes、实际对齐空间、
匹配内参、深度比例、标定版本和新鲜度质量. 元数据必须匹配实际数组.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from application.contracts.enums import FreshnessQuality, ImageSpace
from application.contracts.ids import (
    CameraId,
    CaptureId,
    CaptureRequestId,
    ChannelId,
    FrameId,
    FrameSequence,
    RunId,
    SerialNumber,
    ServiceId,
    SessionEpoch,
    StationInstanceId,
)


class FreshnessPolicy(str, enum.Enum):
    """抓拍新鲜度策略 (02 §6.2). ARRIVAL_ONLY 仅在静止工艺明确允许时使用."""

    REQUIRE_DEVICE_TIME = "require_device_time"
    ALLOW_ARRIVAL_ONLY = "allow_arrival_only"


@dataclass(frozen=True)
class Intrinsics:
    """相机内参, 对应真实对齐空间和输出尺寸. cx/cy 统一命名 (ppx/ppy 由适配器转换)."""

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    intrinsics_space: ImageSpace
    calibration_version: str | None = None

    def __post_init__(self) -> None:
        if not (self.fx > 0 and self.fy > 0):
            raise ValueError("fx/fy 必须为正 (拒绝默认内参)")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("内参宽高必须为正")


@dataclass
class ImageRef:
    """只读图像引用 + 元数据. 不复制数据; producer 负责保证缓冲生命周期."""

    data: Any  # numpy.ndarray
    dtype: str
    shape: tuple[int, ...]
    stride: tuple[int, ...]
    nbytes: int
    encoding: str = "raw"
    generation: int = 0

    @classmethod
    def from_array(cls, array: Any, *, encoding: str = "raw", generation: int = 0) -> "ImageRef":
        """从 numpy 数组构造只读引用. 拒绝非 C 连续/非数组输入."""
        import numpy as np

        if not isinstance(array, np.ndarray):
            raise TypeError("ImageRef.from_array 只接受 numpy.ndarray")
        if not array.flags["C_CONTIGUOUS"]:
            raise ValueError("图像数组必须是 C 连续 (stride 与 shape 一致)")
        return cls(
            data=array,
            dtype=str(array.dtype),
            shape=tuple(int(x) for x in array.shape),
            stride=tuple(int(x) for x in array.strides),
            nbytes=int(array.nbytes),
            encoding=encoding,
            generation=generation,
        )

    def readonly_view(self) -> Any:
        """返回只读视图, 防止下游误改已发布帧 (T21)."""
        view = getattr(self.data, "view", None)
        if callable(view):
            v = view()
            try:
                v.flags.writeable = False
            except (AttributeError, ValueError):
                pass
            return v
        return self.data


@dataclass
class FrameBundle:
    """一帧彩色/深度 + 匹配内参 + 来源/时间/质量 (02 §6.1)."""

    # 身份
    camera_id: CameraId
    frame_id: FrameId
    capture_id: CaptureId
    sequence: FrameSequence
    session_epoch: SessionEpoch = 0
    service_id: ServiceId | None = None
    channel_id: ChannelId | None = None
    serial: SerialNumber | None = None
    # 来源
    run_id: RunId | None = None
    station_instance_id: StationInstanceId | None = None
    step_id: str | None = None
    capture_request_id: CaptureRequestId | None = None
    # 时间
    device_timestamp_ns: int | None = None
    device_time_domain: str | None = None
    producer_receive_monotonic_s: float | None = None
    capture_accepted_monotonic_s: float | None = None
    utc_recorded: str | None = None
    freshness_quality: FreshnessQuality = FreshnessQuality.UNKNOWN
    # 图像
    color: ImageRef | None = None
    depth: ImageRef | None = None
    # 度量
    depth_scale_m: float | None = None
    intrinsics: Intrinsics | None = None
    alignment_space: ImageSpace | None = None
    calibration_version: str | None = None
    # 质量
    depth_valid_ratio: float | None = None
    exposure_quality: str | None = None
    capture_errors: tuple[str, ...] = ()

    def has_required_streams(self, required: Sequence[str]) -> bool:
        for name in required:
            if name == "color" and self.color is None:
                return False
            if name == "depth" and self.depth is None:
                return False
            if name not in ("color", "depth"):
                raise ValueError(f"未知所需流: {name!r}")
        return True

    def require_depth_metrics(self) -> None:
        """度量算法前置: 缺原始深度/depth_scale 时明确拒绝, 不默认成功 (T07)."""
        if self.depth is None:
            raise ValueError(f"帧 {self.frame_id} 缺原始深度, 不能做度量算法")
        if self.depth_scale_m is None or not (self.depth_scale_m > 0):
            raise ValueError(f"帧 {self.frame_id} 缺有效 depth_scale_m")


@dataclass
class CaptureRequest:
    """抓拍请求: 单台或集合、期限、所需数据、新鲜度策略、allow_partial (02 §6.2)."""

    camera_ids: tuple[CameraId, ...]
    required_streams: tuple[str, ...] = ("color", "depth")
    deadline_ms: int = 2000
    allow_partial: bool = False
    freshness_policy: FreshnessPolicy = FreshnessPolicy.REQUIRE_DEVICE_TIME
    request_id: CaptureRequestId | None = None
    run_id: RunId | None = None
    station_instance_id: StationInstanceId | None = None
    step_id: str | None = None

    def __post_init__(self) -> None:
        if not self.camera_ids:
            raise ValueError("CaptureRequest 至少需要一台相机")
        if self.deadline_ms <= 0:
            raise ValueError("deadline_ms 必须为正")


@dataclass
class CaptureSet:
    """集合抓拍结果: 每通道 FrameBundle 或错误 + 时间差与比较质量 (02 §6.3)."""

    request: CaptureRequest
    frames: dict[CameraId, FrameBundle] = field(default_factory=dict)
    errors: dict[CameraId, str] = field(default_factory=dict)
    time_deltas_ms: Mapping[CameraId, float] = field(default_factory=dict)
    delta_quality: FreshnessQuality = FreshnessQuality.UNKNOWN

    @property
    def ok(self) -> bool:
        if not self.request.allow_partial:
            return not self.errors and all(
                cid in self.frames for cid in self.request.camera_ids
            )
        return bool(self.frames)


__all__ = [
    "CaptureRequest",
    "CaptureSet",
    "FrameBundle",
    "FreshnessPolicy",
    "ImageRef",
    "Intrinsics",
]
