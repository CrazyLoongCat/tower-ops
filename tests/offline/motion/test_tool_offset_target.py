"""RealmanMotionArm.tool_offset_target：Bolt 工具系相对偏移 → 基座系目标位姿（DEC-060）。

只验坐标变换与失败边界（不接真机、不建 TCP）：工具系三轴方向来自当前工具系姿态，
目标 = 当前位姿 + Σ d_i · 轴方向，姿态保持不变。
"""
from __future__ import annotations

import math

import pytest

from application.adapters.realman.motion import RealmanMotionArm


class _Sdk:
    """最小 SDK 替身：只实现 tool_offset_target 用到的查询/切换。"""

    def __init__(self, pose, euler, tool="Bolt") -> None:
        self._pose = list(pose)
        self._euler = list(euler)
        self.tool = tool
        self.switches: list[str] = []

    def rm_get_current_arm_state(self):
        return 0, {"pose": list(self._pose)}

    def rm_get_total_tool_frame(self):
        return {"return_code": 0, "tool_names": ["Bolt"]}

    def rm_get_current_tool_frame(self):
        return 0, {"name": self.tool, "pose": {"euler": list(self._euler)}}

    def rm_change_tool_frame(self, name):
        self.switches.append(name)
        self.tool = name
        return 0

    def rm_get_total_work_frame(self):
        return {"return_code": 0, "work_names": []}

    def rm_get_current_work_frame(self):
        return 0, {"name": None}


def _arm(sdk: _Sdk) -> RealmanMotionArm:
    arm = RealmanMotionArm(sdk=sdk)
    arm.connected = True
    return arm


def test_tool_offset_identity_axes():
    """工具系与基座同姿态：偏移直接叠加到基座位姿，姿态不变。"""
    sdk = _Sdk([0.1, 0.2, 0.3, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
    arm = _arm(sdk)
    target = arm.tool_offset_target([0.0, 0.1, -0.005])
    assert target == pytest.approx([0.1, 0.3, 0.295, 0.0, 0.0, 0.0])
    assert sdk.switches == []                     # 当前已是 Bolt，无需切换


def test_tool_offset_uses_tool_axes_not_base_axes():
    """工具系绕基座 Z 转 90°：工具 X → 基座 Y，工具 Y → 基座 −X。"""
    sdk = _Sdk([0.0, 0.0, 0.0, 0.0, 0.0, math.pi / 2],
               [0.0, 0.0, math.pi / 2])
    arm = _arm(sdk)
    target = arm.tool_offset_target([0.1, 0.0, 0.0])
    assert target[:3] == pytest.approx([0.0, 0.1, 0.0])
    target2 = arm.tool_offset_target([0.0, 0.1, 0.0])
    assert target2[:3] == pytest.approx([-0.1, 0.0, 0.0])


def test_tool_offset_switches_to_bolt_and_restores_previous_tool():
    sdk = _Sdk([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0], tool="Other")
    arm = _arm(sdk)
    target = arm.tool_offset_target([0.0, 0.0, 0.005])
    assert target == pytest.approx([0.0, 0.0, 0.005, 0.0, 0.0, 0.0])
    assert sdk.switches == ["Bolt", "Other"]      # 算完方向后还原


def test_tool_offset_without_pose_returns_none():
    sdk = _Sdk([0.0] * 6, [0.0] * 3)
    sdk.rm_get_current_arm_state = lambda: (1, {})     # 读位姿失败
    assert _arm(sdk).tool_offset_target([0, 0, 0.005]) is None
