"""齐次变换与欧拉角 (12 §4.1, 02 §1.1/§5.4, T07/T31/T32).

约定: T_A_B 把 B 坐标变换到 A; 平移单位 mm; 旋转约定 R = Rz(z) * Ry(y) * Rx(x) [deg].
与既有 Jgt handeye.py 的约定一致 (同构, 便于核对).
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np


class GeometryError(Exception):
    """几何/单位/矩阵方向错误 (拒绝而非默认成功)."""


def rpy_to_matrix(rx_deg: float, ry_deg: float, rz_deg: float) -> np.ndarray:
    """欧拉角(deg, R=Rz*Ry*Rx) -> 3x3 旋转矩阵."""
    rx, ry, rz = map(math.radians, (rx_deg, ry_deg, rz_deg))
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return np.array([
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy,     cy * sx,                cy * cx],
    ], dtype=float)


def matrix_to_rpy(R: np.ndarray) -> tuple[float, float, float]:
    """3x3 旋转矩阵 -> 欧拉角(deg, R=Rz*Ry*Rx)."""
    R = np.asarray(R, dtype=float)
    if R.shape != (3, 3):
        raise GeometryError(f"旋转矩阵必须是 3x3, 得到 {R.shape}")
    sy = -R[2, 0]
    sy = max(-1.0, min(1.0, sy))
    ry = math.asin(sy)
    if abs(math.cos(ry)) > 1e-9:
        rz = math.atan2(R[1, 0], R[0, 0])
        rx = math.atan2(R[2, 1], R[2, 2])
    else:  # 奇异: 万向锁
        rz = 0.0
        rx = math.atan2(-R[1, 2], R[1, 1])
    return (math.degrees(rx), math.degrees(ry), math.degrees(rz))


def pose_to_matrix(pose_mm_deg: Sequence[float]) -> np.ndarray:
    """位姿 [x,y,z(mm),rx,ry,rz(deg)] -> 4x4 齐次矩阵."""
    if len(pose_mm_deg) != 6:
        raise GeometryError(f"位姿必须是 6 项 [mm|deg], 得到 {len(pose_mm_deg)} 项")
    x, y, z, rx, ry, rz = (float(v) for v in pose_mm_deg)
    if not all(math.isfinite(v) for v in (x, y, z, rx, ry, rz)):
        raise GeometryError("位姿含非有限值")
    T = np.eye(4)
    T[:3, :3] = rpy_to_matrix(rx, ry, rz)
    T[:3, 3] = (x, y, z)
    return T


def matrix_to_pose(T: np.ndarray) -> list[float]:
    """4x4 齐次矩阵 -> [x,y,z(mm),rx,ry,rz(deg)]."""
    T = require_homogeneous(T)
    rx, ry, rz = matrix_to_rpy(T[:3, :3])
    return [float(T[0, 3]), float(T[1, 3]), float(T[2, 3]), rx, ry, rz]


def require_homogeneous(T: np.ndarray) -> np.ndarray:
    """校验 4x4 齐次矩阵 (末行 [0,0,0,1], 有限值)."""
    arr = np.asarray(T, dtype=float)
    if arr.shape != (4, 4):
        raise GeometryError(f"必须是 4x4 齐次矩阵, 得到 {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise GeometryError("矩阵含非有限值")
    if not np.allclose(arr[3], (0, 0, 0, 1), atol=1e-9):
        raise GeometryError(f"齐次矩阵末行非法: {arr[3].tolist()}")
    return arr


def mat_mul(*mats: np.ndarray) -> np.ndarray:
    """顺序相乘 T = T1 @ T2 @ ..."""
    if not mats:
        raise GeometryError("mat_mul 需要至少一个矩阵")
    out = require_homogeneous(mats[0])
    for m in mats[1:]:
        out = out @ require_homogeneous(m)
    return out


def invert(T: np.ndarray) -> np.ndarray:
    """齐次矩阵求逆 (闭式, 不用通用求逆)."""
    T = require_homogeneous(T)
    R = T[:3, :3]
    t = T[:3, 3]
    if not np.allclose(R @ R.T, np.eye(3), atol=1e-6):
        raise GeometryError("旋转部分非正交, 不能作为刚体变换求逆")
    out = np.eye(4)
    out[:3, :3] = R.T
    out[:3, 3] = -R.T @ t
    return out


def translation_of(T: np.ndarray) -> tuple[float, float, float]:
    T = require_homogeneous(T)
    return (float(T[0, 3]), float(T[1, 3]), float(T[2, 3]))


def transform_point(T: np.ndarray, point_mm: Sequence[float]) -> tuple[float, float, float]:
    """把 B 系点 (mm) 变换到 A 系."""
    T = require_homogeneous(T)
    p = np.array([float(point_mm[0]), float(point_mm[1]), float(point_mm[2]), 1.0])
    q = T @ p
    return (float(q[0]), float(q[1]), float(q[2]))


def ds86_target_in_base(
    T_base_flange: np.ndarray,
    T_flange_ds86: np.ndarray,
    T_ds86_target: np.ndarray,
) -> np.ndarray:
    """DS86 随臂: T_base_target = T_base_flange(t_capture) · T_flange_ds86 · T_ds86_target."""
    return mat_mul(T_base_flange, T_flange_ds86, T_ds86_target)


def fixed_camera_target_in_base(
    T_base_d405_i: np.ndarray,
    T_d405_i_target: np.ndarray,
) -> np.ndarray:
    """固定 D405: T_base_target = T_base_d405_i · T_d405_i_target (每台独立外参)."""
    return mat_mul(T_base_d405_i, T_d405_i_target)


__all__ = [
    "GeometryError", "ds86_target_in_base", "fixed_camera_target_in_base",
    "invert", "mat_mul", "matrix_to_pose", "matrix_to_rpy", "pose_to_matrix",
    "require_homogeneous", "rpy_to_matrix", "transform_point", "translation_of",
]
