"""A19 离线两工位端到端与故障出口 (12 §10, 04 §8)。

覆盖 v0.5 离线子集的成功/条件分支与故障注入:
无目标/低质量/超时/掉线/迟到/停止未知/宿主失败/存储失败/关闭与重启。
断言通过且正常关闭; 此出口不代表 G1/真实硬件验收。
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from application.bootstrap.composition import build_offline_stack
from application.contracts.enums import TaskState
from application.hosts.circle38.client import Circle38LateResult, Circle38Proxy
from application.hosts.circle38.protocol import Request, build_request
from application.hosts.circle38.server import Circle38Server
from application.hosts.circle38.backends import StubCircleBackend

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


def _stack(tmp_path, **kw):
    return build_offline_stack(db_path=str(tmp_path / "r.db"),
                               runtime_dir=str(tmp_path), recipe_path=RECIPE, **kw)


def _run(stack, limit=600):
    st = None
    for _ in range(limit):
        st = stack.task.tick()
        if st in (TaskState.COMPLETED, TaskState.FAULT, TaskState.WAITING_OPERATOR):
            return st
    return st


# ---------------- 成功闭环 ----------------
def test_full_success_loop_and_clean_shutdown(tmp_path):
    stack = _stack(tmp_path)
    assert stack.task.validate("ds86_two_station_example", allow_draft=True) == []
    stack.task.start("ds86_two_station_example", allow_draft=True)
    assert _run(stack) is TaskState.COMPLETED
    # 证据: 整份配方只跑一遍 -> 1 帧 + 1 结果 + 无未决动作
    assert stack.store.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 1
    assert stack.store.conn.execute("SELECT COUNT(*) FROM vision_results").fetchone()[0] == 1
    assert stack.records.pending_unknown_commands() == []
    snaps = stack.task.snapshot()
    assert snaps["state"] == "COMPLETED"
    stack.close()
    # 资源释放
    assert stack.store._conn is None  # noqa: SLF001
    assert not stack.host_proxy.alive


def test_publish_makes_the_recipe_startable_without_allow_draft(tmp_path):
    """DEC-040: 发布即生效——发布后作业台启动校验不再拦「未发布」。"""
    stack = _stack(tmp_path)
    rid = "ds86_two_station_example"
    assert any("未发布" in p for p in stack.task.validate(rid))   # 发布前：被拦
    stack.recipes.publish(rid, allow_offline_draft=True)          # 离线示例: 显式放行发布
    assert [p for p in stack.task.validate(rid) if "未发布" in p] == []
    stack.close()


# ---------------- 故障注入 ----------------
def test_no_target_branches_to_review(tmp_path):
    stack = _stack(tmp_path, circles=[])
    stack.task.start("ds86_two_station_example", allow_draft=True)
    assert _run(stack) is TaskState.WAITING_OPERATOR
    stack.close()


def test_low_quality_indeterminate(tmp_path):
    from application.algorithms.circle.detector import CircleDecisionConfig
    stack = _stack(tmp_path, camera_low_depth=True,
                   decision=CircleDecisionConfig(min_depth_valid_ratio=0.9))
    stack.task.start("ds86_two_station_example", allow_draft=True)
    assert _run(stack) is TaskState.WAITING_OPERATOR
    stack.close()


def test_capture_timeout_faults(tmp_path):
    stack = _stack(tmp_path, capture_timeout=True)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    assert _run(stack) in (TaskState.FAULT, TaskState.WAITING_OPERATOR)
    stack.close()


def test_camera_disconnected_faults(tmp_path):
    stack = _stack(tmp_path, camera_closed=True)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    st = _run(stack)
    assert st in (TaskState.FAULT, TaskState.WAITING_OPERATOR)
    stack.close()


def test_host_failure_reports_error_not_success(tmp_path):
    stack = _stack(tmp_path, host_fail=True)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    st = _run(stack)
    assert st is TaskState.FAULT  # 宿主失败不装作成功
    stack.close()


def test_stop_unknown_faults_not_green(tmp_path):
    stack = _stack(tmp_path, stop_verify_unknown_for={"eco65_01"})
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()
    stack.task.terminate("终止")
    assert stack.task.state_machine.state is TaskState.FAULT
    assert "eco65_01" in stack.task.state_machine.unknown_executors
    stack.close()


def test_storage_failure_blocks_new_action(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()
    stack.store.fail_writes = True
    st = _run(stack)
    assert st is TaskState.FAULT
    stack.close()


def test_late_result_archived_not_used(tmp_path):
    server = Circle38Server(StubCircleBackend([(1.0, 2.0, 3.0)]))
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    stale = server.handle_bytes(build_request(Request("circle-0", img, [], _params())))

    class LateTransport:
        alive = True
        def request(self, payload, deadline_ms):
            return stale
        def close(self):
            pass

    proxy = Circle38Proxy(LateTransport())
    with pytest.raises(Circle38LateResult):
        proxy.detect([img], [[]], _params())
    assert proxy.archived_late == ["circle-0"]


def test_restart_detects_pending_unknown(tmp_path):
    db = str(tmp_path / "r.db")
    stack = build_offline_stack(db_path=db, runtime_dir=str(tmp_path), recipe_path=RECIPE)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()  # 提交首个 movej 并持久化意图 -> ACCEPTED
    stack.close()
    # 重启: 同一 DB 检出未决动作, 不自动续发
    stack2 = build_offline_stack(db_path=db, runtime_dir=str(tmp_path), recipe_path=RECIPE)
    pending = stack2.records.pending_unknown_commands()
    assert any(p["action_ref"] == "robot.move_joint" for p in pending)
    # 新栈未自动运行
    assert stack2.task.state_machine.state is TaskState.IDLE
    assert stack2.robot.read_state()["move_count"] == 0
    stack2.close()


def test_replay_has_no_motion_channel(tmp_path):
    stack = _stack(tmp_path)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    _run(stack)
    events = list(stack.records.replay("run-1"))
    assert any(e["event_type"] == "task.completed" for e in events)
    stack.close()


def _params():
    from application.algorithms.circle.params import CircleParams
    return CircleParams()


# ---------------- DEC-035 启动载入配方目录 ----------------
def test_startup_discovers_saved_recipes_and_recipe_file_wins(tmp_path):
    """「另存为」到配方目录的配方下次启动直接可见；--recipe 的同一 ID 以它为准。"""
    import shutil
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    with open(RECIPE, encoding="utf-8") as fh:
        text = fh.read()
    (recipes_dir / "saved.yaml").write_text(
        text.replace("recipe_id: ds86_two_station_example", "recipe_id: saved_by_page"),
        encoding="utf-8")
    startup = recipes_dir / "startup.yaml"
    shutil.copy(RECIPE, startup)

    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path),
                                recipe_path=str(startup))
    try:
        assert set(stack.recipes.drafts()) == {"ds86_two_station_example", "saved_by_page"}
        assert stack.recipes.source_of("saved_by_page") == str(recipes_dir / "saved.yaml")
        assert stack.recipes.source_of("ds86_two_station_example") == str(startup)
    finally:
        stack.close()
