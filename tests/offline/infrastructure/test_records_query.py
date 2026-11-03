"""事件库检索 API 测试（「日志诊断」页数据源，DEC-029）。

覆盖 :meth:`RecordService.query_events` 的 等级/类型/设备/任务/关键字/序号 过滤与
``list_event_*`` 去重下拉，以及存储层边界（limit / newest_first / min_sequence）。
"""
from __future__ import annotations

from application.infrastructure.events import Event, Severity
from application.infrastructure.storage import SqliteStore
from application.services.records.service import RecordService


def _svc(tmp_path):
    store = SqliteStore(str(tmp_path / "rec.db"))
    store.open()
    return RecordService(store, runtime_dir=str(tmp_path)), store


def _seed(svc):
    svc.append_event(Event(event_type="task.start", severity=Severity.INFO,
                           message="启动 r1", run_id="run-1"))
    svc.append_event(Event(event_type="services.device_state", severity=Severity.INFO,
                           message="已连接 ds86_01", device_id="ds86_01"))
    svc.append_event(Event(event_type="services.device_state", severity=Severity.INFO,
                           message="已连接 s100", device_id="s100"))
    svc.append_event(Event(event_type="task.fault", severity=Severity.ERROR,
                           message="抓拍集合失败", run_id="run-1", device_id="ds86_01",
                           reason="相机未打开"))
    svc.append_event(Event(event_type="task.start", severity=Severity.INFO,
                           message="启动 r2", run_id="run-2"))


def test_query_all_newest_first(tmp_path):
    svc, store = _svc(tmp_path)
    _seed(svc)
    rows = svc.query_events()
    assert len(rows) == 5
    assert rows[0]["message"] == "启动 r2"          # 最新在前
    assert [r["sequence"] for r in rows] == sorted((r["sequence"] for r in rows),
                                                   reverse=True)
    store.close()


def test_query_filters_combine_with_and(tmp_path):
    svc, store = _svc(tmp_path)
    _seed(svc)
    assert len(svc.query_events(severity="ERROR")) == 1
    assert len(svc.query_events(event_type="task.start")) == 2
    assert len(svc.query_events(device_id="ds86_01")) == 2
    assert len(svc.query_events(run_id="run-1")) == 2
    # 组合：run-1 且 ERROR -> 只剩 fault
    rows = svc.query_events(run_id="run-1", severity="ERROR")
    assert [r["event_type"] for r in rows] == ["task.fault"]
    store.close()


def test_query_keyword_searches_message_type_reason(tmp_path):
    svc, store = _svc(tmp_path)
    _seed(svc)
    assert len(svc.query_events(keyword="启动")) == 2
    assert len(svc.query_events(keyword="device_state")) == 2   # 命中 event_type
    assert len(svc.query_events(keyword="相机未打开")) == 1       # 命中 reason
    store.close()


def test_query_limit_and_min_sequence(tmp_path):
    svc, store = _svc(tmp_path)
    _seed(svc)
    assert len(svc.query_events(limit=2)) == 2
    rows = svc.query_events(limit=1, newest_first=False)
    first_seq = rows[0]["sequence"]
    newer = svc.query_events(min_sequence=first_seq)
    assert all(r["sequence"] > first_seq for r in newer)
    assert len(newer) == 4
    store.close()


def test_distinct_lists_for_dropdowns(tmp_path):
    svc, store = _svc(tmp_path)
    _seed(svc)
    assert svc.list_event_types() == ["services.device_state", "task.fault",
                                      "task.start"]
    assert svc.list_event_devices() == ["ds86_01", "s100"]
    assert svc.list_event_runs() == ["run-1", "run-2"]
    store.close()


def test_query_on_empty_db_returns_empty(tmp_path):
    svc, store = _svc(tmp_path)
    assert svc.query_events() == []
    assert svc.list_event_types() == []
    assert svc.list_event_devices() == []
    assert svc.list_event_runs() == []
    store.close()
