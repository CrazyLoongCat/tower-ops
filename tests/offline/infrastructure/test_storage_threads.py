"""DEC-068 跨线程写库：事件桥从工作线程写 events 表不再抛 ProgrammingError。

真机现场（2026-10-22）：运动线程 `_run_guide` → `guidance_service.detect_with_circle`
记 INFO 日志 → 日志设施把记录桥进事件表 → sqlite3 报
``SQLite objects created in a thread can only be used in that same thread``。
本用例在主线程建库、工作线程写/读，复现并守住修复。
"""
from __future__ import annotations

import logging
import threading

from application.infrastructure.events import Event, Severity
from application.infrastructure.logging_setup import EventStoreHandler
from application.infrastructure.storage import SqliteStore
from application.services.records.service import RecordService


def _open_store(tmp_path):
    store = SqliteStore(str(tmp_path / "r.db"))
    store.open()
    return store


def test_worker_thread_can_write_and_read_same_store(tmp_path):
    store = _open_store(tmp_path)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            store.insert_event({"event_type": "thread.x", "severity": "INFO",
                                "message": "来自工作线程"})
            assert store.query_one("SELECT COUNT(*) FROM events")[0] == 1
        except BaseException as exc:  # noqa: BLE001 - 失败要带回主线程断言
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive()
    assert errors == []                       # 修复前这里是 ProgrammingError
    assert store.query_one(
        "SELECT COUNT(*) FROM events WHERE event_type='thread.x'")[0] == 1
    store.close()


def test_event_bridge_from_worker_thread_persists_log(tmp_path):
    """复现真机路径：工作线程里 logger.info → EventStoreHandler → events 表。"""
    store = _open_store(tmp_path)
    records = RecordService(store)
    handler = EventStoreHandler(records)
    logger = logging.getLogger("towerops.test.thread_bridge")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        def worker() -> None:
            for i in range(5):
                logger.info("[Guide] 工作线程日志 %d", i)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10)
        assert not t.is_alive()
        count = store.query_one(
            "SELECT COUNT(*) FROM events WHERE event_type='test.thread_bridge'")[0]
        assert count == 5                     # 修复前 0 条（每条都吞进 handleError）
    finally:
        logger.removeHandler(handler)
        store.close()


def test_concurrent_append_events_no_duplicates(tmp_path):
    store = _open_store(tmp_path)
    records = RecordService(store)

    def add(n: int) -> None:
        for i in range(25):
            records.append_event(
                Event(event_type="concurrent", severity=Severity.INFO,
                      message=f"{n}-{i}"), mirror=False)

    threads = [threading.Thread(target=add, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
        assert not t.is_alive()

    rows = store.query("SELECT sequence, message FROM events")
    assert len(rows) == 100
    assert len({r["sequence"] for r in rows}) == 100     # 不重不漏
    assert records.pending_events == 0
    store.close()
