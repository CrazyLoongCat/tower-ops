"""关节目标与笛卡尔位姿 - 严格分型 (12 §4.1, 02 §1.1/§5.1, T32).

关键约束:
- JointTarget 恰好 6 个有限 joint_deg; 拒绝非法 mode / 缺项 / 补 0;
- CartesianPose 明确 frame_id / translation_mm / orientation_convention /
  calibration_version;
- 二者是不同类型, 禁止将视觉 XYZ+三欧拉角直接当 6 关节值.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from application.contracts.enums import OrientationConvention
from application.contracts.ids import CalibrationId, ToolId


def _ensure_finite(name: str, values: Sequence[float]) -> list[float]:
    out: list[float] = []
    for i, v in enumerate(values):
        try:
            fv = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}[{i}] 不是数值: {v!r}") from exc
        if not math.isfinite(fv):
            raise ValueError(f"{name}[{i}] 不是有限值: {v!r}")
        out.append(fv)
    return out


class JointTarget(BaseModel):
    """6 关节角目标 (deg). movej 输入, 恰好 6 项, 不补 0."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    joint_deg: tuple[float, float, float, float, float, float]
    robot_model: str = "ECO_65"
    joint_constraint_version: str = "unset"
    waypoint_ref: str | None = None

    @field_validator("joint_deg", mode="before")
    @classmethod
    def _check_six(cls, v: Any) -> tuple[float, ...]:
        # 拒绝缺失/补零: 长度必须恰好 6, 非 list/tuple 也拒绝.
        if isinstance(v, (str, bytes)) or not isinstance(v, (list, tuple)):
            raise ValueError("joint_deg 必须是长度恰为 6 的序列")
        if len(v) != 6:
            raise ValueError(
                f"joint_deg 必须恰好 6 项, 得到 {len(v)} 项 (拒绝补 0/缺项)"
            )
        vals = _ensure_finite("joint_deg", v)
        return tuple(vals)

    @property
    def joint_deg_list(self) -> list[float]:
        return list(self.joint_deg)


class CartesianPose(BaseModel):
    """笛卡尔目标位姿. 平移 mm, 姿态 deg, 首版约定 R=Rz*Ry*Rx."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    frame_id: str
    translation_mm: tuple[float, float, float]
    orientation_deg: tuple[float, float, float]
    orientation_convention: OrientationConvention = OrientationConvention.RPY_ZYX_DEG
    calibration_version: str | None = None
    tool_id: ToolId | None = None
    calibration_id: CalibrationId | None = None

    @field_validator("translation_mm", "orientation_deg", mode="before")
    @classmethod
    def _check_triple(cls, v: Any, info: Any) -> tuple[float, ...]:
        name = info.field_name
        if isinstance(v, (str, bytes)) or not isinstance(v, (list, tuple)):
            raise ValueError(f"{name} 必须是长度 3 的序列")
        if len(v) != 3:
            raise ValueError(f"{name} 必须恰好 3 项, 得到 {len(v)} 项")
        return tuple(_ensure_finite(name, v))

    @field_validator("frame_id", mode="before")
    @classmethod
    def _check_frame(cls, v: Any) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("frame_id 必须是非空字符串 (禁止默认坐标系)")
        return v.strip()


def require_joint_target(obj: Any) -> JointTarget:
    """类型守卫: 只接受 JointTarget, 拒绝把 CartesianPose/裸列表当关节目标."""
    if not isinstance(obj, JointTarget):
        raise TypeError(
            f"movej 只接受 JointTarget, 得到 {type(obj).__name__} (分型错误)"
        )
    return obj


__all__ = ["CartesianPose", "JointTarget", "require_joint_target"]
