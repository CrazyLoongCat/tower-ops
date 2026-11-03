"""FakeCamera (DS86) 与帧来源/缓冲/时效 (12 §4.1, 02 §6, T07/T12/T21/T31).

约束:
- 每次抓拍产生新序号; 旧缓存不能冒充本次帧 (T12);
- 内参匹配实际对齐空间与输出尺寸; 深度保留 uint16 + depth_scale_m;
- 发布只读/受控持有数据; 模拟 SDK 下一帧覆盖不改变已发布帧 (T21);
- 记录静止前后姿态, 移动使旧手眼结果失效 (T31).
"""
from __future__ import annotations

import enum
import itertools
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from application.contracts.enums import FreshnessQuality, ImageSpace
from application.contracts.frames import (
    CaptureRequest,
    CaptureSet,
    FrameBundle,
    FreshnessPolicy,
    ImageRef,
    Intrinsics,
)


class ExposureQuality(str, enum.Enum):
    GOOD = "GOOD"
    DARK = "DARK"
    SATURATED = "SATURATED"
    UNKNOWN = "UNKNOWN"


@dataclass
class FakeCameraConfig:
    camera_id: str = "ds86_01"
    width: int = 640
    height: int = 480
    fx: float = 600.0
    fy: float = 600.0
    cx: float = 320.0
    cy: float = 240.0
    depth_scale_m: float = 0.001
    calibration_version: str = "fake-cal-v1"
    alignment_space: ImageSpace = ImageSpace.DEPTH
    exposure: ExposureQuality = ExposureQuality.GOOD


@dataclass
class _Faults:
    """离线故障注入开关 (不含真实设备)."""

    stale_cache: bool = False     # 返回上一帧序号 (冒充旧缓存)
    fail_capture: bool = False    # 直接报错
    low_valid_depth: bool = False # 深度有效比例过低
    timeout: bool = False         # 超期未返回


class CameraFaultError(RuntimeError):
    """相机抓拍故障 (离线注入)."""


class FakeCamera:
    """确定性离线相机. 序号/姿态由内部状态与显式调用决定."""

    def __init__(
        self,
        config: FakeCameraConfig | None = None,
        pose_provider: Callable[[], list[float] | None] | None = None,
    ) -> None:
        self.config = config or FakeCameraConfig()
        self._pose_provider = pose_provider
        self._open = False
        self._seq = itertools.count(1)
        self._last_seq = 0
        self._epoch = 0
        self.faults = _Faults()
        self._held: list[FrameBundle] = []  # 已发布帧 (防 SDK 复用改写)
        self._pose_at_capture: list[float] | None = None

    # ---------- 生命周期 ----------
    def enumerate(self) -> list[str]:
        return [self.config.camera_id]

    def open(self, camera_id: str) -> None:
        if camera_id != self.config.camera_id:
            raise ValueError(f"未知相机 {camera_id!r}")
        self._open = True
        self._epoch += 1

    def close(self, camera_id: str) -> None:
        self._open = False

    def status(self) -> dict:
        return {
            "camera_id": self.config.camera_id,
            "open": self._open,
            "epoch": self._epoch,
            "last_sequence": self._last_seq,
            "alignment_space": self.config.alignment_space.value,
        }

    # ---------- 抓拍 ----------
    def capture(self, request: CaptureRequest) -> CaptureSet:
        cs = CaptureSet(request=request)
        if self.faults.timeout:
            for cid in request.camera_ids:
                cs.errors[cid] = "抓拍超时 (注入)"
            return cs
        if self.faults.fail_capture:
            for cid in request.camera_ids:
                cs.errors[cid] = "抓拍失败 (注入)"
            return cs
        for cid in request.camera_ids:
            if cid != self.config.camera_id:
                cs.errors[cid] = f"未配置相机 {cid!r}"
                continue
            if not self._open:
                cs.errors[cid] = "相机未打开"
                continue
            frame = self._make_frame(request)
            cs.frames[cid] = frame
            self._held.append(frame)
            # 连续监控只需保留最近帧；消费者持有的 FrameBundle 自身拥有数组。
            del self._held[:-8]
        cs.delta_quality = (
            FreshnessQuality.DEVICE_TIME_VERIFIED if not cs.errors
            else FreshnessQuality.UNKNOWN
        )
        return cs

    def _make_frame(self, request: CaptureRequest) -> FrameBundle:
        if self.faults.stale_cache:
            seq = self._last_seq  # 冒充: 老序号 (T12 应被上层察觉)
        else:
            seq = next(self._seq)
        self._last_seq = seq
        w, h = self.config.width, self.config.height
        color = np.zeros((h, w, 3), dtype=np.uint8)
        depth = np.full((h, w), 1000, dtype=np.uint16)  # 1.0 m
        if self.faults.low_valid_depth:
            depth[:] = 0
        now = time.monotonic()
        if self._pose_provider is not None:
            self._pose_at_capture = self._pose_provider()
        depth_valid = float(np.count_nonzero(depth)) / depth.size
        fresh = (
            FreshnessQuality.ARRIVAL_ONLY
            if request.freshness_policy is FreshnessPolicy.ALLOW_ARRIVAL_ONLY
            else FreshnessQuality.DEVICE_TIME_VERIFIED
        )
        return FrameBundle(
            camera_id=self.config.camera_id,
            frame_id=f"{self.config.camera_id}-f{seq}",
            capture_id=f"{self.config.camera_id}-c{seq}",
            sequence=seq,
            session_epoch=self._epoch,
            service_id="local",
            channel_id=self.config.camera_id,
            serial="fake-serial",
            run_id=request.run_id,
            station_instance_id=request.station_instance_id,
            step_id=request.step_id,
            capture_request_id=request.request_id,
            device_timestamp_ns=int(now * 1e9),
            device_time_domain="fake_monotonic",
            producer_receive_monotonic_s=now,
            capture_accepted_monotonic_s=now,
            utc_recorded=None,
            freshness_quality=fresh,
            color=ImageRef.from_array(color),
            depth=ImageRef.from_array(depth),
            depth_scale_m=self.config.depth_scale_m,
            intrinsics=Intrinsics(
                fx=self.config.fx, fy=self.config.fy,
                cx=self.config.cx, cy=self.config.cy,
                width=w, height=h,
                intrinsics_space=self.config.alignment_space,
                calibration_version=self.config.calibration_version,
            ),
            alignment_space=self.config.alignment_space,
            calibration_version=self.config.calibration_version,
            depth_valid_ratio=depth_valid,
            exposure_quality=self.config.exposure.value,
            capture_errors=(),
        )

    # ---------- 姿态关联 (T31) ----------
    def pose_at_last_capture(self) -> list[float] | None:
        return self._pose_at_capture

    def invalidates_on_move(self, current_pose: list[float] | None) -> bool:
        """抓拍后移动 -> 旧手眼结果失效 (02 §5.4)."""
        if self._pose_at_capture is None or current_pose is None:
            return False
        return any(abs(a - b) > 1e-6
                   for a, b in zip(self._pose_at_capture, current_pose))

    # ---------- 测试辅助 ----------
    def held_frames(self) -> list[FrameBundle]:
        return self._held


__all__ = [
    "CameraFaultError", "ExposureQuality", "FakeCamera", "FakeCameraConfig",
]
