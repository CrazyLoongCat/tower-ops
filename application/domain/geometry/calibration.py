"""标定模型、适用性校验与变换服务 (12 §4.1, 02 §5.4, T07/T31/T32).

- 标定带版本、单位、适用尺寸/工具/安装; 不适用即拒绝, 不复用错误标定;
- DS86 随臂与固定相机变换分别实现, 不能把随臂矩阵复制给固定相机;
- 未知标定/单位不符/尺寸不符 -> 明确拒绝 (不返回默认成功坐标).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from application.contracts.enums import OrientationConvention
from application.contracts.targets import CartesianPose
from application.domain.geometry.transforms import (
    GeometryError,
    ds86_target_in_base,
    fixed_camera_target_in_base,
    matrix_to_pose,
    pose_to_matrix,
    require_homogeneous,
)


class CalibrationKind(str, enum.Enum):
    HAND_EYE = "hand_eye"          # 随臂: T_flange_camera
    FIXED_CAMERA = "fixed_camera"  # 固定: T_base_camera
    TOOL = "tool"                  # 工具系
    WORK = "work"                  # 工件系


@dataclass
class Calibration:
    """一份标定. matrix 为 4x4, 平移单位由 units 声明 (默认 mm)."""

    calibration_id: str
    kind: CalibrationKind
    matrix: Sequence[Sequence[float]]
    version: str
    units: str = "mm"
    camera_id: str | None = None
    resolution: tuple[int, int] | None = None
    tool_id: str | None = None
    install_revision: str | None = None
    valid_range_mm: tuple[float, float] | None = None
    verified: bool = False

    def as_matrix(self) -> np.ndarray:
        if self.units != "mm":
            raise GeometryError(
                f"标定 {self.calibration_id!r} 单位为 {self.units!r}, "
                f"领域层只接受 mm; 请在导入时显式换算, 不要静默混用"
            )
        return require_homogeneous(np.asarray(self.matrix, dtype=float))


class CalibrationStore:
    """标定登记. 未登记 -> 拒绝 (不默认成功)."""

    def __init__(self, calibrations: Sequence[Calibration] = ()) -> None:
        self._items: dict[str, Calibration] = {}
        for c in calibrations:
            self.add(c)

    def add(self, cal: Calibration) -> None:
        if cal.calibration_id in self._items:
            raise GeometryError(f"标定 {cal.calibration_id!r} 重复")
        self._items[cal.calibration_id] = cal

    def get(self, calibration_id: str) -> Calibration:
        cal = self._items.get(calibration_id)
        if cal is None:
            raise GeometryError(f"未知标定: {calibration_id!r} (拒绝默认坐标)")
        return cal

    def known(self, calibration_id: str) -> bool:
        return calibration_id in self._items


def check_applicability(
    cal: Calibration,
    *,
    camera_id: str | None = None,
    resolution: tuple[int, int] | None = None,
    tool_id: str | None = None,
    install_revision: str | None = None,
    require_verified: bool = False,
) -> None:
    """尺寸/工具/安装/相机变化使旧标定不适用 (T32)."""
    if require_verified and not cal.verified:
        raise GeometryError(f"标定 {cal.calibration_id!r} 未经独立验证, 不适用")
    if cal.camera_id is not None and camera_id is not None and cal.camera_id != camera_id:
        raise GeometryError(
            f"标定 {cal.calibration_id!r} 属于相机 {cal.camera_id!r}, 不适用于 {camera_id!r}"
        )
    if cal.resolution is not None and resolution is not None and cal.resolution != resolution:
        raise GeometryError(
            f"标定 {cal.calibration_id!r} 尺寸 {cal.resolution} != {resolution} (需重标定)"
        )
    if cal.tool_id is not None and tool_id is not None and cal.tool_id != tool_id:
        raise GeometryError(
            f"标定 {cal.calibration_id!r} 工具 {cal.tool_id!r} != {tool_id!r}"
        )
    if (cal.install_revision is not None and install_revision is not None
            and cal.install_revision != install_revision):
        raise GeometryError(
            f"标定 {cal.calibration_id!r} 安装版本 {cal.install_revision!r} "
            f"!= {install_revision!r}"
        )


class TransformService:
    """坐标变换服务 (TransformPort 实现). 未知标定/不满足条件即拒绝."""

    def __init__(self, store: CalibrationStore) -> None:
        self._store = store

    def ds86_to_base(
        self,
        target_in_camera: CartesianPose,
        T_base_flange: Sequence[Sequence[float]],
        calibration_id: str,
        *,
        frame_id: str = "base",
        camera_id: str = "ds86_01",
    ) -> CartesianPose:
        cal = self._store.get(calibration_id)
        if cal.kind is not CalibrationKind.HAND_EYE:
            raise GeometryError(
                f"标定 {calibration_id!r} 不是随臂手眼标定, 不能用于 DS86 变换"
            )
        check_applicability(cal, camera_id=camera_id)
        T_base_target = ds86_target_in_base(
            require_homogeneous(np.asarray(T_base_flange, dtype=float)),
            cal.as_matrix(),
            pose_to_matrix(list(target_in_camera.translation_mm)
                           + list(target_in_camera.orientation_deg)),
        )
        return _to_pose(T_base_target, frame_id, cal)

    def fixed_camera_to_base(
        self,
        target_in_camera: CartesianPose,
        calibration_id: str,
        *,
        frame_id: str = "base",
        camera_id: str | None = None,
        resolution: tuple[int, int] | None = None,
    ) -> CartesianPose:
        cal = self._store.get(calibration_id)
        if cal.kind is not CalibrationKind.FIXED_CAMERA:
            raise GeometryError(
                f"标定 {calibration_id!r} 不是固定相机外参, 不能用于固定相机变换"
            )
        check_applicability(cal, camera_id=camera_id, resolution=resolution)
        T_base_target = fixed_camera_target_in_base(
            cal.as_matrix(),
            pose_to_matrix(list(target_in_camera.translation_mm)
                           + list(target_in_camera.orientation_deg)),
        )
        return _to_pose(T_base_target, frame_id, cal)

    def transform_target(
        self,
        source: CartesianPose,
        target_frame: str,
        calibration_id: str,
        *,
        T_base_flange: Sequence[Sequence[float]] | None = None,
    ) -> CartesianPose:
        """通用入口: 按标定类型分派. 缺所需输入即拒绝."""
        cal = self._store.get(calibration_id)
        if cal.kind is CalibrationKind.HAND_EYE:
            if T_base_flange is None:
                raise GeometryError("随臂变换需要抓拍时的 T_base_flange(t_capture)")
            return self.ds86_to_base(source, T_base_flange, calibration_id,
                                     frame_id=target_frame)
        if cal.kind is CalibrationKind.FIXED_CAMERA:
            return self.fixed_camera_to_base(source, calibration_id,
                                             frame_id=target_frame)
        raise GeometryError(f"标定类型 {cal.kind.value!r} 不用于目标坐标变换")


def _to_pose(T: np.ndarray, frame_id: str, cal: Calibration) -> CartesianPose:
    pose = matrix_to_pose(T)
    return CartesianPose(
        frame_id=frame_id,
        translation_mm=(pose[0], pose[1], pose[2]),
        orientation_deg=(pose[3], pose[4], pose[5]),
        orientation_convention=OrientationConvention.RPY_ZYX_DEG,
        calibration_version=cal.version,
        calibration_id=cal.calibration_id,
        tool_id=cal.tool_id,
    )


__all__ = [
    "Calibration", "CalibrationKind", "CalibrationStore", "TransformService",
    "check_applicability",
]
