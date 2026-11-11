"""DEC-157：机械臂动作防重复下发（执行中再次点击不得二次下发）。

修复点：
- 套筒偏移/工具系点动此前**不置 playing**，执行期间 busy=False，连点会并发起
  两个运动线程（真实重复下发）；
- 所有入口统一走 _spawn：忙态或上一线程未退出即拒绝；通过检查后**同步**置
  playing，堵住"线程已起、模式未定"的竞态（双击）。
"""
from __future__ import annotations

import tempfile
import threading
import os

from application.services.motion_config import MotionPlan, MotionConfigStore, Waypoint
from application.services.motion_runner import MotionRunner


class _SlowArm:
    """阻塞式假臂：进入动作后等 release，统计下发次数。"""

    connected = True

    def __init__(self) -> None:
        self.enter = threading.Event()
        self.release = threading.Event()
        self.moves = 0
        self._pos = (0.0,) * 6
        self._joint = (0.0,) * 6

    def _block(self):
        self.moves += 1
        self.enter.set()
        self.release.wait(5.0)
        return 0

    def move_offset(self, off, v=0, stop_check=None):
        return self._block()

    def movel(self, target, speed, blend, a, b):
        self._block()
        self._pos = tuple(target)

    def movej(self, target, speed, blend, a, b):
        self._block()
        self._joint = tuple(target)

    def get_current_pose(self):
        return self._pos

    def get_current_joint(self):
        return self._joint

    def stop(self):
        return None


def _store(tmp: str) -> MotionConfigStore:
    store = MotionConfigStore(os.path.join(tmp, "motion.yaml"), seed_if_missing=False)
    store.load()
    store.add_plan(MotionPlan(plan_id="p", waypoints=[
        Waypoint("movel", (0.1, 0.2, 0.3, 0.0, 0.0, 0.0)),
    ]))
    return store


def test_socket_move_marks_busy_and_rejects_second_submit():
    with tempfile.TemporaryDirectory() as tmp:
        arm = _SlowArm()
        runner = MotionRunner(arm, _store(tmp))
        assert runner.request_socket_move(+5.0, axis="Y", speed=10) is True
        assert arm.enter.wait(2.0)
        # 执行中必须 busy；第二次点击被拒绝且不产生第二条动作
        assert runner.busy is True
        assert runner.request_socket_move(+5.0, axis="Y", speed=10) is False
        arm.release.set()
        assert runner.wait(5.0)
        assert arm.moves == 1


def test_tool_jog_rejects_second_submit_while_moving():
    with tempfile.TemporaryDirectory() as tmp:
        arm = _SlowArm()
        runner = MotionRunner(arm, _store(tmp))
        assert runner.request_tool_jog("X", 5.0, speed=23) is True
        assert arm.enter.wait(2.0)
        assert runner.request_tool_jog("X", 5.0, speed=23) is False
        arm.release.set()
        assert runner.wait(5.0)
        assert arm.moves == 1


def test_start_plan_immediately_double_click_rejected():
    """双击竞态：第二次点击发生在运动线程进入 movel 之前也必须被拒绝。"""
    with tempfile.TemporaryDirectory() as tmp:
        arm = _SlowArm()
        runner = MotionRunner(arm, _store(tmp))
        assert runner.start_plan("p") is True
        assert runner.start_plan("p") is False      # 同步置 playing 后立刻拒绝
        assert arm.enter.wait(2.0)
        arm.release.set()
        assert runner.wait(5.0)
        assert arm.moves == 1
        assert runner.busy is False
