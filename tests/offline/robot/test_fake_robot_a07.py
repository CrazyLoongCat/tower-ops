"""A07 FakeRobot 测试 (T05/T06/T17/T32 离线)."""
from __future__ import annotations

import pytest

from application.adapters.realman.fake import (
    FakeRobot,
    FakeRobotConfig,
    JointLimits,
    RobotOpState,
)
from application.contracts.enums import CommandState
from application.contracts.targets import CartesianPose, JointTarget


def _robot():
    r = FakeRobot(FakeRobotConfig(deg_per_tick=10.0, stable_window_ticks=2,
                                  stop_confirm_ticks=1))
    r.connect()
    return r


def test_move_requires_joint_target():
    r = _robot()
    with pytest.raises(TypeError):
        r.move_joint([0, 0, 0, 0, 0, 0])  # 裸列表
    with pytest.raises(TypeError):
        r.move_joint(CartesianPose(frame_id="base", translation_mm=[0, 0, 0],
                                   orientation_deg=[0, 0, 0]))


def test_move_when_disconnected_fails():
    r = FakeRobot()
    h = r.move_joint(JointTarget(joint_deg=[1, 2, 3, 4, 5, 6]))
    assert h.state is CommandState.FAILED


def test_out_of_limits_rejected():
    r = _robot()
    h = r.move_joint(JointTarget(joint_deg=[0, 0, 0, 0, 0, 999]))
    assert h.state is CommandState.REJECTED


def test_accept_does_not_wait_for_arrival():
    r = _robot()
    h = r.move_joint(JointTarget(joint_deg=[20, 0, 0, 0, 0, 0]))
    assert h.state is CommandState.ACCEPTED  # 立即返回, 未到位
    assert r.state is RobotOpState.MOVING
    assert not h.is_success


def test_tick_reaches_target_with_stable_window():
    r = _robot()
    tgt = JointTarget(joint_deg=[20, 0, 0, 0, 0, 0])
    h = r.move_joint(tgt)
    for _ in range(50):
        r.tick()
        if h.is_terminal:
            break
    assert h.state is CommandState.COMPLETED
    assert r.is_at_target(tgt)
    assert r.state is RobotOpState.READY


def test_second_move_rejected_while_moving():
    r = _robot()
    r.move_joint(JointTarget(joint_deg=[20, 0, 0, 0, 0, 0]))
    h2 = r.move_joint(JointTarget(joint_deg=[10, 0, 0, 0, 0, 0]))
    assert h2.state is CommandState.REJECTED


def test_stop_is_not_queued_behind_motion():
    r = _robot()
    tgt = JointTarget(joint_deg=[100, 0, 0, 0, 0, 0])
    h = r.move_joint(tgt)
    r.tick()  # 移动中
    assert r.state is RobotOpState.MOVING
    r.request_stop()
    assert r.state is RobotOpState.STOPPING  # 立即进入停止, 不等运动走完
    r.tick()  # 停止确认
    assert h.state is CommandState.CANCELLED
    assert r.state is RobotOpState.READY
    assert not r.is_at_target(tgt)  # 未到目标即停止


def test_cancel_clears_target():
    r = _robot()
    h = r.move_joint(JointTarget(joint_deg=[50, 0, 0, 0, 0, 0]))
    r.cancel(h)
    assert h.state is CommandState.CANCELLED


def test_joint_limits_within():
    lim = JointLimits(minimum=(-1,) * 6, maximum=(1,) * 6)
    assert lim.within([0] * 6)
    assert not lim.within([2, 0, 0, 0, 0, 0])
