"""A13 应用服务调用链测试 (T03/T04/T06/T14/T16/T21 离线)."""
from __future__ import annotations

import pytest

from application.bootstrap.composition import build_offline_stack
from application.contracts.context import ExecutionKey, RunContext, VersionSnapshot
from application.contracts.enums import SourceKind
from application.contracts.targets import JointTarget
from application.domain.errors import InterlockDenied, ValidationFailure
from application.infrastructure.storage import StorageError

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


def _stack(tmp_path):
    return build_offline_stack(
        db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
        recipe_path=RECIPE)


def _ctx(run_id="run-x", si="run-x:st:0"):
    return RunContext(run_id=run_id, versions=VersionSnapshot(
        recipe_id="r", recipe_version="v", config_hash="c", calibration_hash="k"),
        station_instance_id=si)


def test_manual_cannot_preempt_auto_motion(tmp_path):
    stack = _stack(tmp_path)
    key = ExecutionKey("run-x", "run-x:st:0", "move")
    stack.executor.submit_move_joint(key, _ctx(), JointTarget(joint_deg=[1, 2, 3, 0, 0, 0]),
                                     SourceKind.WORKFLOW, "workflow")
    # 手动来源不能抢已在自动任务持有的运动资源
    with pytest.raises(InterlockDenied):
        stack.executor.submit_move_joint(
            ExecutionKey("run-x", "run-x:st:0", "move-manual"), _ctx(),
            JointTarget(joint_deg=[1, 2, 3, 0, 0, 0]),
            SourceKind.MANUAL, "operator")
    stack.close()


def test_mechanism_can_run_while_robot_moves(tmp_path):
    """DEC-096：机构动作占板卡资源，可与机器人运动同时进行（并行组前提）。"""
    stack = _stack(tmp_path)
    key = ExecutionKey("run-x", "run-x:st:0", "move")
    stack.executor.submit_move_joint(key, _ctx(), JointTarget(joint_deg=[1, 2, 3, 0, 0, 0]),
                                     SourceKind.WORKFLOW, "workflow")
    mech = stack.executor.submit_mechanism(
        ExecutionKey("run-x", "run-x:st:0", "mech"), _ctx(),
        "approved_single_board_action", {}, SourceKind.WORKFLOW, "workflow")
    assert mech is not None
    stack.close()


def test_service_rejects_bad_param(tmp_path):
    stack = _stack(tmp_path)
    key = ExecutionKey("run-x", "run-x:st:0", "mech")
    with pytest.raises(ValidationFailure):
        stack.executor.submit_mechanism(
            key, _ctx(), "approved_single_board_action", {"force": 999},
            SourceKind.WORKFLOW, "workflow")
    stack.close()


def test_unknown_action_rejected(tmp_path):
    stack = _stack(tmp_path)
    key = ExecutionKey("run-x", "run-x:st:0", "mech")
    with pytest.raises(Exception):
        stack.executor.submit_mechanism(
            key, _ctx(), "nonexistent_action", {}, SourceKind.WORKFLOW, "workflow")
    stack.close()


def test_storage_failure_blocks_dangerous_action(tmp_path):
    stack = _stack(tmp_path)
    stack.store.fail_writes = True
    key = ExecutionKey("run-x", "run-x:st:0", "mech")
    with pytest.raises(StorageError):
        stack.executor.submit_mechanism(
            key, _ctx(), "approved_single_board_action", {},
            SourceKind.WORKFLOW, "workflow")
    # 记录失败 -> 未下发危险动作 (帧未写出, 命令已撤销)
    assert stack.boards.link("link0").sent_count == 0
    assert stack.boards.board("stm32_01").tracker.pending_count == 0
    stack.close()


def test_repeated_submit_same_key_uses_single_handle(tmp_path):
    stack = _stack(tmp_path)
    key = ExecutionKey("run-x", "run-x:st:0", "mech")
    h1 = stack.executor.submit_mechanism(
        key, _ctx(), "approved_single_board_action", {}, SourceKind.WORKFLOW, "workflow")
    # 同一 key 再提交: 账本只记一次
    stack.executor.submit_mechanism(
        key, _ctx(), "approved_single_board_action", {}, SourceKind.WORKFLOW, "workflow")
    assert stack.executor.ledger.submissions == 1
    stack.close()


def test_capture_records_frames(tmp_path):
    stack = _stack(tmp_path)
    from application.contracts.frames import CaptureRequest
    cs = stack.executor.capture(_ctx(), CaptureRequest(
        camera_ids=("ds86_01",), request_id="rq1",
        run_id="run-x", station_instance_id="run-x:st:0", step_id="capture"))
    assert cs.ok
    assert stack.store.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 1
    stack.close()


def test_vision_result_recorded(tmp_path):
    stack = _stack(tmp_path)
    from application.contracts.frames import CaptureRequest
    cs = stack.executor.capture(_ctx(), CaptureRequest(
        camera_ids=("ds86_01",), request_id="rq2",
        run_id="run-x", station_instance_id="run-x:st:0", step_id="capture"))
    frame = next(iter(cs.frames.values()))
    result = stack.executor.run_vision(_ctx(), frame, result_id="res-1")
    assert result.status.value == "PASS"
    assert stack.store.conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0] == 1
    stack.close()
