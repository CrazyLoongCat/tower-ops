"""E25 运行记录/报告/留存/离线回放 测试 (T15/T20/T21/T23/T25; E25-附加)。"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from application.bootstrap.composition import build_offline_stack
from application.contracts.enums import TaskState
from application.domain.errors import MotionRefused
from application.infrastructure.retention import (
    RetentionPolicy,
    enforce_retention,
    protected_run_ids,
)
from application.services.records.replay import (
    RefusingBoard,
    RefusingRobot,
    build_replay_stack,
)
from application.services.records.reports import (
    build_run_report, export_report, render_markdown,
)

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


def _completed_stack(tmp_path):
    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path), recipe_path=RECIPE)
    stack.task.start("ds86_two_station_example", allow_draft=True)
    for _ in range(400):
        if stack.task.tick() is TaskState.COMPLETED:
            break
    return stack


# ---------------- 报告 ----------------
def test_report_of_completed_run(tmp_path):
    stack = _completed_stack(tmp_path)
    rep = build_run_report(stack.store, "run-1", runtime_dir=str(tmp_path))
    assert rep.present and rep.run["state"] == "COMPLETED"
    assert rep.versions["recipe_id"] == "ds86_two_station_example"
    assert rep.frames and rep.results and rep.commands and rep.events
    assert rep.pending_commands == []
    assert "缺项 / 限制" in render_markdown(rep)
    stack.close()


def test_report_missing_run_is_explicit(tmp_path):
    stack = _completed_stack(tmp_path)
    rep = build_run_report(stack.store, "nope")
    assert not rep.present and rep.missing == ["run 记录缺失"]
    assert "该运行不存在记录" in render_markdown(rep)
    stack.close()


def test_report_flags_missing_frame_file(tmp_path):
    stack = _completed_stack(tmp_path)
    rep = build_run_report(stack.store, "run-1")
    os.remove(rep.frames[0]["path"])
    rep2 = build_run_report(stack.store, "run-1")
    assert any("帧文件缺失" in m for m in rep2.missing)     # 缺证据显式列出
    stack.close()


def test_report_lists_pending_unknown_commands(tmp_path):
    stack = _completed_stack(tmp_path)
    stack.store.execute("UPDATE command_executions SET state='UNKNOWN'")
    rep = build_run_report(stack.store, "run-1")
    assert rep.pending_commands
    assert any("未决命令" in m for m in rep.missing)
    assert "需人工核对" in render_markdown(rep)
    stack.close()


def test_export_report_md_and_json(tmp_path):
    stack = _completed_stack(tmp_path)
    md = export_report(stack.store, "run-1", str(tmp_path / "out" / "r.md"))
    js = export_report(stack.store, "run-1", str(tmp_path / "out" / "r.json"), fmt="json")
    assert os.path.exists(md) and os.path.exists(js)
    assert "运行报告 run-1" in open(md, encoding="utf-8").read()
    import json
    assert json.load(open(js, encoding="utf-8"))["run_id"] == "run-1"
    stack.close()


# ---------------- 回放 ----------------
def test_replay_stack_refuses_motion(tmp_path):
    stack = _completed_stack(tmp_path)
    stack.close()                                   # 释放写句柄
    replay = build_replay_stack(str(tmp_path / "r.db"))
    replay.assert_no_motion_channel()
    assert isinstance(replay.robot, RefusingRobot)
    with pytest.raises(MotionRefused):
        replay.robot.move_joint()
    with pytest.raises(MotionRefused):
        replay.robot.request_stop()
    with pytest.raises(MotionRefused):
        replay.boards.get("stm32_01").execute_action("clamp", {})
    # 历史只读
    assert [e["event_type"] for e in replay.history("run-1")]
    replay.close()


def test_replay_database_is_read_only(tmp_path):
    stack = _completed_stack(tmp_path)
    stack.close()
    replay = build_replay_stack(str(tmp_path / "r.db"))
    assert replay.store.read_only is True
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):   # 只读库拒绝写入
        replay.store.execute("DELETE FROM frames")
    replay.close()


def test_replay_report_from_history(tmp_path):
    stack = _completed_stack(tmp_path)
    stack.close()
    replay = build_replay_stack(str(tmp_path / "r.db"))
    out = replay.export_report("run-1", str(tmp_path / "replay_report.md"))
    assert "运行报告 run-1" in open(out, encoding="utf-8").read()
    replay.close()


# ---------------- 留存 ----------------
def test_retention_protects_critical_runs(tmp_path):
    stack = _completed_stack(tmp_path)
    # 让 run-1 成为关键 (FAULT)
    stack.store.execute("UPDATE runs SET state='FAULT' WHERE run_id='run-1'")
    assert "run-1" in protected_run_ids(stack.store)
    # 把帧时间改老, 使其"过期"
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    stack.store.execute("UPDATE frames SET updated_utc=?", (old,))
    res = enforce_retention(stack.store, RetentionPolicy(keep_days=1),
                            runtime_dir=str(tmp_path))
    assert res.deleted_frames == 0                  # 关键证据未被删
    assert res.kept_frames >= 1 and "run-1" in res.protected_runs
    assert any("保护" in m for m in res.messages)
    stack.close()


def test_retention_rotates_non_critical_old_frames(tmp_path):
    stack = _completed_stack(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    stack.store.execute("UPDATE frames SET updated_utc=?", (old,))
    before = stack.store.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    res = enforce_retention(stack.store, RetentionPolicy(keep_days=1),
                            runtime_dir=str(tmp_path))
    after = stack.store.conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    assert before > 0 and after == 0 and res.deleted_frames == before
    assert res.deleted_bytes >= 0
    stack.close()


def test_retention_low_disk_reports_not_silent(tmp_path):
    stack = _completed_stack(tmp_path)
    res = enforce_retention(
        stack.store, RetentionPolicy(keep_days=1, min_free_bytes=10 ** 15),
        runtime_dir=str(tmp_path))
    assert res.low_disk and not res.ok
    assert any("低磁盘" in m and "不静默丢弃" in m for m in res.messages)
    stack.close()


def test_retention_capacity_pressure_only_non_critical(tmp_path):
    stack = _completed_stack(tmp_path)
    res = enforce_retention(
        stack.store, RetentionPolicy(keep_days=99999, max_bytes=0),
        runtime_dir=str(tmp_path))
    # 容量压力下轮转非关键帧
    assert res.deleted_frames >= 1
    stack.close()
