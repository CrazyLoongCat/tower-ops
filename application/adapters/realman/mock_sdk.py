"""RealMan SDK 的 Mock 实现 (A22 离线接口测试用).

不连接真实设备; 只模拟 SDK 方法签名/返回, 用于验证适配器的调用/取消/生命周期.
真实 SDK 由 A22 的 real.py 包装, 安全导入后不自动连接.
"""
from __future__ import annotations

import enum
import math


class MockOpState(enum.Enum):
    READY = "READY"
    MOVING = "MOVING"


class MockRmSdk:
    """最小 SDK 仿真. 关节/位姿单位: joint=deg, pose=[x,y,z(m),rx,ry,rz(rad)]."""

    def __init__(self) -> None:
        self.handle = None
        self.state = MockOpState.READY
        self.joint_deg = [0.0] * 6
        self.pose_m_rad = [0.0] * 6
        self.calls: list[str] = []
        self.stop_called = 0
        self.fail_next_movej = False
        self.import_side_effects = 0  # 构造时无副作用 (仅记录)

    # ---- 生命周期 ----
    def create_robot_arm(self, ip: str, port: int, level: int):
        self.calls.append("create")
        self.handle = type("H", (), {"id": 1})()
        return self.handle

    def delete_robot_arm(self):
        self.calls.append("delete")
        self.handle = None

    # ---- 状态 ----
    def get_current_arm_state(self):
        return 0, {"pose": list(self.pose_m_rad), "joint": list(self.joint_deg)}

    def get_robot_info(self):
        return 0, {"arm_model": "ECO_65", "six_force_sensor": 0}

    # ---- 运动 ----
    def movej(self, joint, v, r, connect, block):
        self.calls.append(f"movej:{block}")
        if self.fail_next_movej:
            self.fail_next_movej = False
            return -1
        self.joint_deg = [float(j) for j in joint]
        self.pose_m_rad = [j / 1000.0 for j in self.joint_deg]
        self.state = MockOpState.READY
        return 0

    def set_arm_stop(self):
        self.calls.append("stop")
        self.stop_called += 1
        return 0

    def set_arm_pause(self):
        self.calls.append("pause")
        return 0

    def set_arm_continue(self):
        self.calls.append("continue")
        return 0

    def set_arm_delete_trajectory(self):
        self.calls.append("delete_traj")
        return 0

    # ---- IO ----
    def set_do_state(self, io_num, state):
        self.calls.append(f"do:{io_num}={state}")
        return 0

    def get_io_input(self):
        return 0, [False] * 4


def rpy_to_mat_zyx(rx, ry, rz):
    """R = Rz*Ry*Rx (与项目约定一致), 供 Mock 生成位姿矩阵."""
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return [
        [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
        [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
        [-sy, cy * sx, cy * cx],
    ]


__all__ = ["MockOpState", "MockRmSdk", "rpy_to_mat_zyx"]
