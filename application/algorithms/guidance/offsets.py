"""相机系圆心 → 机械手 (x,z) 偏差（**1:1 移植**自旧工程 `_compute_robot_offsets`）。

旧说明（保留）：找圆后自动计算粗定位/精定位偏差(mm)。

- **粗定位**：识别点相对**相机中心点** XY 偏差 → 按轴映射转换得机械手 (x, z)；
- **精定位**：识别点相对**工具中心点**(tcp_x/tcp_y) XY 偏差 → 按轴映射转换。

轴映射（相机 X/Y → 机械手哪个轴）与符号都**可配置**，旧工程默认
`X→Z(-)`、`Y→X(-)`（见 configs 里的 `robot_coarse_x_axis` 等）。

移植原则：分支结构与"后写的映射覆盖先写的"这一行为**原样保留** —— 改了就可能改变现场方向。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

VALID_AXES = ("X", "Z")


@dataclass(frozen=True)
class AxisMapping:
    """一组轴映射：相机 X/Y 各映射到机械手哪个轴、符号如何。"""

    x_axis: str = "Z"
    x_sign: int = -1
    y_axis: str = "X"
    y_sign: int = -1

    def __post_init__(self) -> None:
        for name, axis in (("x_axis", self.x_axis), ("y_axis", self.y_axis)):
            if axis not in VALID_AXES:
                raise ValueError(f"{name} 只能是 {VALID_AXES}，得到 {axis!r}")
        for name, sign in (("x_sign", self.x_sign), ("y_sign", self.y_sign)):
            if int(sign) not in (-1, 1):
                raise ValueError(f"{name} 只能是 +1/-1，得到 {sign!r}")


@dataclass(frozen=True)
class OffsetParams:
    """粗/精定位的轴映射与工具中心点（单位 mm）。

    ``tcp_x``/``tcp_y`` 是工具中心相对相机的**横向**（相机系 X/Y）错位，参与精定位
    横向补偿；``tcp_z``（DEC-069）是沿相机光轴的**纵向**距离，只登记/展示，
    **不参与** 2D 横向偏移 —— 相机与工作位同轴（相机在套筒正上方）时横向偏移应为 0。
    """

    coarse: AxisMapping = AxisMapping()
    fine: AxisMapping = AxisMapping()
    tcp_x: float = 0.0
    tcp_y: float = 0.0
    tcp_z: float = 0.0


def _map(cam_x: float, cam_y: float, mapping: AxisMapping) -> Tuple[float, float]:
    """把相机系 XY 按映射转成机械手 (x, z)。**保持旧分支结构**（后写覆盖先写）。"""
    robot_x, robot_z = 0.0, 0.0
    if mapping.x_axis == "Z":
        robot_z = mapping.x_sign * cam_x
    else:
        robot_x = mapping.x_sign * cam_x
    if mapping.y_axis == "X":
        robot_x = mapping.y_sign * cam_y
    else:
        robot_z = mapping.y_sign * cam_y
    return robot_x, robot_z


def compute_robot_offsets(cam_xyz: Sequence[float], params: OffsetParams | None = None
                          ) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """返回 ``(粗定位(x,z), 精定位(x,z))``，单位 mm。

    ``cam_xyz``：相机系圆心 (X, Y, Z) mm（相对光心主点，**未经显示旋转折算**）。

    DEC-069：精定位只减**横向** tcp_x/tcp_y；tcp_z（沿光轴距离）不参与横向偏移。
    """
    if len(cam_xyz) < 3:
        raise ValueError("cam_xyz 至少需要 X/Y/Z 三项")
    params = params or OffsetParams()
    cam_x, cam_y = float(cam_xyz[0]), float(cam_xyz[1])
    coarse = _map(cam_x, cam_y, params.coarse)
    fine = _map(cam_x - float(params.tcp_x), cam_y - float(params.tcp_y), params.fine)
    return coarse, fine


__all__ = ["AxisMapping", "OffsetParams", "VALID_AXES", "compute_robot_offsets"]
