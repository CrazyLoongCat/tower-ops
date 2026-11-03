"""A11 持久化/关键证据测试 (T20/T21/T22 基础)."""
from __future__ import annotations

import numpy as np
import pytest

from application.contracts.enums import DetectionStatus
from application.contracts.frames import FrameBundle, ImageRef
from application.contracts.handles import CommandHandle
from application.contracts.results import DetectionResult
from application.infrastructure.events import Event, Severity
from application.infrastructure.storage import LowDiskError, SqliteStore, StorageError
from application.services.records.service import RecordService, StorageBackpressure


def _svc(tmp_path, *, min_free=0, max_pending=3):
    store = SqliteStore(str(tmp_path / "rec.db"), min_free_bytes=min_free)
    store.open()
    return RecordService(store, runtime_dir=str(tmp_path), max_pending_events=max_pending), store


def test_start_run_and_events(tmp_path):
    svc, store = _svc(tmp_path)
    svc.start_run("run1", recipe_id="r", recipe_version="v1",
                  config_hash="cfg", calibration_hash="cal")
    svc.append_event(Event(event_type="task.start", run_id="run1"))
    assert store.count_events() == 1
    assert list(svc.list_runs())[0]["run_id"] == "run1"


def test_pending_unknown_detected(tmp_path):
    svc, store = _svc(tmp_path)
    h = CommandHandle(command_id="cmd1", action_ref="clamp")
    h.state = h.state.QUEUED
    svc.confirm_command_intent(h, run_id="run1", station_instance_id="st1", step_id="s1")
    from application.contracts.enums import CommandState
    h.state = CommandState.SENT
    svc.update_command(h, run_id="run1")
    pending = svc.pending_unknown_commands()
    assert any(p["command_id"] == "cmd1" for p in pending)  # 未决, 重启不盲重放
    h.state = CommandState.COMPLETED
    svc.update_command(h, run_id="run1")
    assert svc.pending_unknown_commands() == []


def test_write_failure_blocks_and_backpressures(tmp_path):
    svc, store = _svc(tmp_path, max_pending=3)
    store.fail_writes = True
    with pytest.raises(StorageError):
        svc.append_event(Event(event_type="x"))
    # 缓冲保留并累积
    for i in range(2):
        with pytest.raises(StorageError):
            svc.append_event(Event(event_type=f"y{i}"))
    assert svc.pending_events == 3
    with pytest.raises(StorageBackpressure):
        svc.append_event(Event(event_type="z"))
    # 恢复后可落库
    store.fail_writes = False
    svc.flush()
    assert svc.pending_events == 0


def test_stop_path_never_blocks(tmp_path):
    svc, store = _svc(tmp_path)
    store.fail_writes = True
    svc.append_event_nonblocking(Event(event_type="stop.requested"))  # 不抛
    assert True


def test_low_disk_raises(tmp_path):
    svc, store = _svc(tmp_path, min_free=10**15)
    with pytest.raises(LowDiskError):
        store.check_disk()


def test_save_capture_writes_files_and_hash(tmp_path):
    svc, store = _svc(tmp_path)
    frame = FrameBundle(
        camera_id="ds86_01", frame_id="f1", capture_id="c1", sequence=1,
        color=ImageRef.from_array(np.zeros((4, 4, 3), np.uint8)),
        depth=ImageRef.from_array(np.full((4, 4), 1000, np.uint16)),
        depth_scale_m=0.001,
    )
    svc.save_capture(frame)
    row = store.conn.execute("SELECT * FROM frames WHERE frame_id='f1'").fetchone()
    assert row["sha256"] and len(row["sha256"]) == 64
    assert row["shape"] == "[4, 4]"


def test_checkpoint_roundtrip(tmp_path):
    svc, store = _svc(tmp_path)
    svc.checkpoint("run1", "step3", {"index": 2})
    assert svc.get_checkpoint("run1", "step3")["index"] == 2


def test_save_result_and_replay(tmp_path):
    svc, store = _svc(tmp_path)
    svc.start_run("run1", recipe_id="r", recipe_version="v1",
                  config_hash="c", calibration_hash="k")
    svc.append_event(Event(event_type="a", run_id="run1"))
    svc.append_event(Event(event_type="b", run_id="run1"))
    svc.save_result(DetectionResult(result_id="res1",
                                    status=DetectionStatus.PASS))
    events = svc.replay("run1")
    assert [e["event_type"] for e in events] == ["a", "b"]


def test_export_report_lists_missing(tmp_path):
    svc, store = _svc(tmp_path)
    out = tmp_path / "report.md"
    svc.export_report("nope", str(out))
    text = out.read_text(encoding="utf-8")
    assert "run 记录缺失" in text


def test_config_version_recorded(tmp_path):
    svc, store = _svc(tmp_path)
    svc.record_config_version("v1", "recipe", "r1", "rev1", "hash", "published", "op")
    row = store.conn.execute("SELECT * FROM config_versions WHERE version_id='v1'").fetchone()
    assert row["state"] == "published"
