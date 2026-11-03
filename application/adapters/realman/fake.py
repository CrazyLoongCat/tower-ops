"""FakeRobot: 离线机器人 (12 §4.4, 02 §5, T05/T06/T17/T32).

约束:
- movej 只接受 JointTarget (6 关节 deg); 缺6项/非法mode 拒绝;
- 接受不等到位: move_joint 同步返回 Handle, 由 tick 推进到位/稳定窗;
- 停止不排在长调用后: request_stop 立即生效, 不等待运动走完;
- 显式建模连接/限位/忙等错误路径.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field

from application.contracts.enums import CommandState
from application.contracts.handles import CommandHandle
from application.contracts.targets import JointTarget, require_joint_target


class RobotOpState(enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    READY = "READY"
    MOVING = "MOVING"
    STOPPING = "STOPPING"
    FAULT = "FAULT"


@dataclass
class JointLimits:
    """关节限位 (deg). 默认参考值, 真实限位由配置/现场确认, 非已验证结论."""

    minimum: tuple[float, ...] = (-170, -120, -170, -120, -170, -180)
    maximum: tuple[float, ...] = (170, 120, 170, 120, 170, 180)

    def within(self, joints: list[float]) -> bool:
        return all(self.minimum[i] <= joints[i] <= self.maximum[i]
                   for i in range(6))


@dataclass
class FakeRobotConfig:
    robot_id: str = "eco65_01"
    home_joint_deg: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    deg_per_tick: float = 6.0        # 每 tick 关节角速度上限 (模拟)
    tolerance_deg: float = 0.05      # 到位容差
    stable_window_ticks: int = 2     # 稳定窗
    stop_confirm_ticks: int = 1      # 停止确认所需 tick


class FakeRobot:
    """确定性离线机器人. 时间由 tick 显式推进 (不依赖真实时钟)."""

    def __init__(self, config: FakeRobotConfig | None = None,
                 limits: JointLimits | None = None) -> None:
        self.config = config or FakeRobotConfig()
        self.limits = limits or JointLimits()
        self.state = RobotOpState.DISCONNECTED
        self.joints: list[float] = list(self.config.home_joint_deg)
        self._target: list[float] | None = None
        self._handle: CommandHandle | None = None
        self._stable = 0
        self._stop_countdown = 0
        self._move_count = 0
        self.pose_deg: list[float] | None = None  # 法兰位姿 (供 DS86 姿态关联)

    # ---------- 连接 ----------
    def connect(self) -> None:
        self.state = RobotOpState.READY
        self.joints = list(self.config.home_joint_deg)
        self.pose_deg = [0.0] * 6
        self._target = None
        self._handle = None

    def disconnect(self) -> None:
        self.state = RobotOpState.DISCONNECTED
        self._target = None

    @property
    def connected(self) -> bool:
        return self.state is not RobotOpState.DISCONNECTED

    # ---------- 查询 ----------
    def read_joint(self) -> list[float] | None:
        return list(self.joints) if self.connected else None

    def read_pose(self) -> list[float] | None:
        return list(self.pose_deg) if self.pose_deg else None

    def read_state(self) -> dict:
        return {
            "connection": "ONLINE" if self.connected else "OFFLINE",
            "operational": self.state.value,
            "joints": list(self.joints),
            "moving": self.state is RobotOpState.MOVING,
            "move_count": self._move_count,
        }

    # ---------- 运动 ----------
    def move_joint(self, target: JointTarget, speed_pct: int = 20, blend: int = 0) -> CommandHandle:
        """同步返回 Handle. SENT->ACCEPTED 表示已接受, 不代表到位 (T05)."""
        require_joint_target(target)  # 拒绝非 JointTarget
        handle = CommandHandle(
            command_id=f"{self.config.robot_id}#move{self._move_count + 1}",
            action_ref="robot.move_joint",
        )
        handle.state = CommandState.VALIDATED
        if not self.connected:
            handle.state = CommandState.FAILED
            handle.error_reason = "机器人未连接"
            return handle
        if self.state is RobotOpState.MOVING:
            handle.state = CommandState.REJECTED
            handle.reject_reason = "已有在途运动 (每板/机器人一个在途动作)"
            return handle
        goal = list(target.joint_deg)
        if not self.limits.within(goal):
            handle.state = CommandState.REJECTED
            handle.reject_reason = f"目标超关节限位: {goal}"
            return handle
        self._move_count += 1
        self._target = goal
        self._handle = handle
        self._stable = 0
        self.state = RobotOpState.MOVING
        handle.state = CommandState.ACCEPTED
        handle.accepted_monotonic_s = time.monotonic()
        return handle

    def tick(self, dt_ms: float = 20.0) -> CommandHandle | None:
        """推进模拟. 到位且稳定窗满足后 COMPLETED."""
        if self.state is RobotOpState.STOPPING:
            self._stop_countdown -= 1
            if self._stop_countdown <= 0:
                self.state = RobotOpState.READY
                if self._handle is not None and not self._handle.is_terminal:
                    self._handle.state = CommandState.CANCELLED
                    self._handle.note("停止确认后取消在途运动")
                self._target = None
            return self._handle

        if self.state is not RobotOpState.MOVING or self._target is None:
            return self._handle

        step = self.config.deg_per_tick
        moved = False
        for i in range(6):
            diff = self._target[i] - self.joints[i]
            if abs(diff) > 1e-9:
                delta = max(-step, min(step, diff))
                self.joints[i] += delta
                moved = True
        # 位姿粗联动 (模拟工具在基座的运动), 供 DS86 姿态关联使用
        if self.pose_deg is not None:
            self.pose_deg[0] = sum(self.joints) * 0.1

        if not moved:
            self._stable += 1
            if self._stable >= self.config.stable_window_ticks:
                if self._handle is not None and not self._handle.is_terminal:
                    self._handle.state = CommandState.COMPLETED
                    self._handle.done_ok = True
                    self._handle.completed_monotonic_s = time.monotonic()
                self.state = RobotOpState.READY
                self._target = None
        else:
            self._stable = 0
        return self._handle

    def is_at_target(self, target: JointTarget) -> bool:
        return all(abs(self.joints[i] - target.joint_deg[i]) <= self.config.tolerance_deg
                   for i in range(6))

    # ---------- 停止 ----------
    def request_stop(self) -> None:
        """停止立即生效: 不等待运动走完, 不排在长调用后 (T17/T18)."""
        if not self.connected:
            return
        if self.state is RobotOpState.MOVING:
            self.state = RobotOpState.STOPPING
            self._stop_countdown = self.config.stop_confirm_ticks
        elif self.state is not RobotOpState.STOPPING:
            self.state = RobotOpState.READY

    def cancel(self, handle: CommandHandle) -> None:
        if handle is self._handle and not handle.is_terminal:
            handle.state = CommandState.CANCELLED
            self._target = None
            if self.state is RobotOpState.MOVING:
                self.state = RobotOpState.READY


__all__ = ["FakeRobot", "FakeRobotConfig", "JointLimits", "RobotOpState"]
