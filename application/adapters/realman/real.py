"""Jgt 基线真实 RobotAdapter (A22 离线实现; 真实 R1~R6 在 A25).

- 独占 SDK 句柄; 单位分型 (SDK 边界 m/rad <-> 领域 mm/deg);
- 非阻塞 movej (block=0) + 到位/稳定窗核验; 停止不排在长调用后;
- 安全导入: 未注入 SDK 时才惰性导入厂家包, 导入期不连接设备.
"""
from __future__ import annotations

import time
from typing import Any, Sequence

from application.adapters.realman.fake import JointLimits
from application.contracts.enums import CommandState
from application.contracts.handles import CommandHandle
from application.contracts.targets import (
    CartesianPose,
    JointTarget,
    require_joint_target,
)
from application.contracts.units import m_to_mm, rad_to_deg


class RobotSdkError(RuntimeError):
    pass


class RmRobotAdapter:
    """真实 RealMan 适配器. sdk 注入 Mock 或真实句柄; 句柄独占."""

    def __init__(self, *, sdk: Any = None, ip: str = "192.168.33.80",
                 port: int = 8080, model: str = "ECO_65",
                 limits: JointLimits | None = None,
                 frame_id: str = "base",
                 tolerance_deg: float = 0.05,
                 stable_window_ms: int = 100,
                 speed_cap_pct: int = 50) -> None:
        self._sdk = sdk
        self._ip = ip
        self._port = port
        self._model = model
        self.limits = limits or JointLimits()
        self._frame_id = frame_id
        self._tol = tolerance_deg
        self._stable_window_ms = stable_window_ms
        self._speed_cap = speed_cap_pct
        self._handle = None
        self._connected = False
        self._joints: list[float] = [0.0] * 6
        self._target: JointTarget | None = None
        self._active: CommandHandle | None = None
        self._within_since_ms: float | None = None
        self._stop_requested = False

    # ---------- 连接 ----------
    def _ensure_sdk(self) -> None:
        if self._sdk is not None:
            return
        try:
            from Robotic_Arm.rm_robot_interface import (  # type: ignore
                RoboticArm, rm_thread_mode_e,
            )
        except Exception as exc:  # noqa: BLE001
            from application.bootstrap.vendor_paths import describe_import_failure
            raise RobotSdkError(describe_import_failure(
                "Robotic_Arm", exc, note="需 A22/A24 环境登记")) from exc
        self._sdk = RoboticArm(rm_thread_mode_e(2))

    def connect(self) -> None:
        self._ensure_sdk()
        self._handle = self._sdk.create_robot_arm(self._ip, self._port, 3)
        if getattr(self._handle, "id", -1) == -1:
            raise RobotSdkError("连接失败 (句柄 id=-1)")
        self._connected = True

    def disconnect(self) -> None:
        if self._sdk is not None:
            try:
                self._sdk.delete_robot_arm()
            except Exception:  # noqa: BLE001
                pass
        self._handle = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ---------- 查询 ----------
    def read_joint(self) -> list[float] | None:
        res, state = self._sdk.get_current_arm_state()
        if res != 0 or not isinstance(state, dict):
            return None
        joint = state.get("joint")
        return [float(j) for j in joint] if joint else None

    def read_pose(self) -> CartesianPose | None:
        res, state = self._sdk.get_current_arm_state()
        if res != 0 or not isinstance(state, dict) or not state.get("pose"):
            return None
        p = state["pose"]  # [x,y,z(m),rx,ry,rz(rad)]
        return CartesianPose(
            frame_id=self._frame_id,
            translation_mm=(m_to_mm(p[0]), m_to_mm(p[1]), m_to_mm(p[2])),
            orientation_deg=(rad_to_deg(p[3]), rad_to_deg(p[4]), rad_to_deg(p[5])),
        )

    def read_state(self) -> dict:
        joint = self.read_joint()
        return {"connected": self._connected, "joint_deg": joint,
                "moving": self._target is not None}

    # ---------- 运动 ----------
    def move_joint(self, target: JointTarget, speed_pct: int = 20,
                   blend: int = 0) -> CommandHandle:
        require_joint_target(target)  # 拒绝非 JointTarget (分型)
        handle = CommandHandle(command_id=f"{self._model}#movej",
                               action_ref="robot.move_joint")
        handle.state = CommandState.VALIDATED
        if not self._connected:
            handle.state = CommandState.FAILED
            handle.error_reason = "机器人未连接"
            return handle
        goal = list(target.joint_deg)
        if not self.limits.within(goal):
            handle.state = CommandState.REJECTED
            handle.reject_reason = f"目标超关节限位: {goal}"
            return handle
        v = max(1, min(int(speed_pct), self._speed_cap))
        # 非阻塞下发 (block=0), 便于轮询暂停/停止
        res = self._sdk.movej(goal, v, blend, 1, 0)
        if res != 0:
            handle.state = CommandState.FAILED
            handle.error_reason = f"movej 下发返回 {res}"
            return handle
        self._target = target
        self._active = handle
        self._within_since_ms = None
        self._stop_requested = False
        handle.state = CommandState.ACCEPTED
        handle.accepted_monotonic_s = time.monotonic()
        return handle

    def poll(self, handle: CommandHandle) -> CommandState:
        """到位判定: 6 关节误差 + 稳定窗口; 接受不等于到位 (T05)."""
        if handle is not self._active:
            return handle.state
        now_ms = time.monotonic() * 1000.0
        joint = self.read_joint()
        if joint is None:
            handle.state = CommandState.UNKNOWN
            handle.error_reason = "读关节失败, 到位状态 UNKNOWN"
            return handle.state
        if self._stop_requested:
            handle.state = CommandState.CANCELLED
            self._target = None
            return handle.state
        within = all(abs(joint[i] - self._target.joint_deg[i]) <= self._tol
                     for i in range(6))
        if within:
            if self._within_since_ms is None:
                self._within_since_ms = now_ms
            elif now_ms - self._within_since_ms >= self._stable_window_ms:
                handle.state = CommandState.COMPLETED
                handle.done_ok = True
                handle.completed_monotonic_s = time.monotonic()
                self._target = None
        else:
            self._within_since_ms = None
        return handle.state

    def request_stop(self) -> None:
        """停止立即下发, 不排在长调用之后 (T17)."""
        self._stop_requested = True
        if self._sdk is not None:
            self._sdk.set_arm_stop()

    def set_io(self, io_num: int, state: bool) -> bool:
        return self._sdk.set_do_state(io_num, state) == 0

    def read_io(self) -> Sequence[bool]:
        res, arr = self._sdk.get_io_input()
        return arr if res == 0 else []

    def close(self) -> None:
        self.disconnect()


__all__ = ["RmRobotAdapter", "RobotSdkError"]
