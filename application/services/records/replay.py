"""离线回放装配 (E25, T23, 02 §12)。

回放只读取历史记录并**只注入拒绝运动适配器**: 不建立真实运动通道,
服务 API 直接调用也会被拒绝 (MotionRefused)。数据库以只读方式打开。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from application.domain.errors import MotionRefused
from application.infrastructure.storage import SqliteStore
from application.services.records.reports import export_report
from application.services.records.service import RecordService


def _refuse(what: str):
    raise MotionRefused(f"回放装配禁止真实运动/设备操作: {what}")


class RefusingRobot:
    """回放用机器人端口: 读为空, 任何运动/停止一律拒绝。"""

    connected = False
    replay = True

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def read_joint(self):
        return None

    def read_pose(self):
        return None

    def read_state(self) -> dict:
        return {"connection": "OFFLINE", "operational": "REPLAY",
                "joints": None, "moving": False}

    def move_joint(self, *a: Any, **k: Any):
        _refuse("robot.move_joint")

    def request_stop(self) -> None:
        _refuse("robot.request_stop")

    def set_io(self, *a: Any, **k: Any):
        _refuse("robot.set_io")

    def read_io(self):
        return []


class RefusingBoard:
    """回放用板卡端口: 任何动作/停止一律拒绝。"""

    def __init__(self, board_id: str = "replay") -> None:
        self.board_id = board_id
        self.address = -1

    def execute_action(self, *a: Any, **k: Any):
        _refuse(f"board({self.board_id}).execute_action")

    def request_stop(self, *a: Any, **k: Any) -> None:
        _refuse(f"board({self.board_id}).request_stop")


class RefusingBoardRegistry:
    def __init__(self) -> None:
        self._boards: dict[str, RefusingBoard] = {}

    def get(self, board_id: str) -> RefusingBoard:
        return self._boards.setdefault(board_id, RefusingBoard(board_id))

    def ids(self) -> list[str]:
        return sorted(self._boards)


@dataclass
class ReplayStack:
    """只读回放装配. 有记录能力, 无运动能力。"""

    profile: str
    store: SqliteStore
    records: RecordService
    robot: RefusingRobot
    boards: RefusingBoardRegistry
    names: list[str] = field(default_factory=lambda: [
        "RefusingRobot", "RefusingBoardRegistry"])

    def history(self, run_id: str) -> list[dict]:
        return list(self.records.replay(run_id))

    def export_report(self, run_id: str, out_path: str, *, fmt: str = "md") -> str:
        return export_report(self.store, run_id, out_path, fmt=fmt)

    def assert_no_motion_channel(self) -> None:
        """自检: 回放装配不得存在可运动的端口 (T23)。"""
        assert getattr(self.robot, "replay", False) is True
        assert isinstance(self.robot, RefusingRobot)
        for bid in self.boards.ids():
            assert isinstance(self.boards.get(bid), RefusingBoard)

    def close(self) -> None:
        self.store.close()


def build_replay_stack(db_path: str, *, runtime_dir: str | None = None) -> ReplayStack:
    """以只读方式打开历史记录, 装配拒绝运动的回放栈。"""
    store = SqliteStore(db_path)
    store.open(read_only=True)
    records = RecordService(store, runtime_dir=runtime_dir)
    stack = ReplayStack(profile="replay", store=store, records=records,
                        robot=RefusingRobot(), boards=RefusingBoardRegistry())
    stack.assert_no_motion_channel()
    return stack


__all__ = [
    "RefusingBoard", "RefusingBoardRegistry", "RefusingRobot", "ReplayStack",
    "build_replay_stack",
]
