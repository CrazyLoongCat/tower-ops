"""全局日志设施测试 (DEC-029): 文本落盘 / 事件桥 / 镜像 / 结构化字段 / 幂等。

覆盖用户诉求「整体工程的日志 log，记录所有日志信息」的两个落点：
文本文件 + 事件库（供「日志诊断」页过滤）。
"""
from __future__ import annotations

import logging
import logging.handlers
import os

import pytest

from application.infrastructure import logging_setup
from application.infrastructure.events import Event, Severity
from application.infrastructure.storage import SqliteStore
from application.services.records.service import RecordService


@pytest.fixture()
def records(tmp_path):
    store = SqliteStore(str(tmp_path / "rec.db"))
    store.open()
    svc = RecordService(store, runtime_dir=str(tmp_path))
    yield svc, store
    logging_setup.reset()
    store.close()


def _read_log(tmp_path) -> str:
    path = logging_setup.log_path(str(tmp_path))
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_install_writes_text_log(tmp_path):
    logging_setup.reset()
    fac = logging_setup.install(str(tmp_path), to_console=False)
    logging_setup.get_logger("services.actions").info("动作下发 %s", "move")
    logging_setup.get_logger("bootstrap.composition").warning("装配有告警")
    text = _read_log(tmp_path)
    assert "动作下发 move" in text
    assert "装配有告警" in text
    assert fac.log_path.endswith(os.path.join("logs", "tower-ops.log"))
    logging_setup.reset()


def test_install_is_idempotent_per_dir(tmp_path):
    logging_setup.reset()
    a = logging_setup.install(str(tmp_path), to_console=False)
    b = logging_setup.install(str(tmp_path), to_console=False)
    assert a is b                                  # 同目录不叠加处理器
    logging_setup.reset()


def test_install_switching_dir_closes_old(tmp_path):
    logging_setup.reset()
    a = logging_setup.install(str(tmp_path / "one"), to_console=False)
    b = logging_setup.install(str(tmp_path / "two"), to_console=False)
    assert a is not b
    assert logging_setup.current() is b
    # 旧文件处理器已摘除：向新设施写一行，不该再出现在旧文件里
    logging_setup.get_logger("x").info("hello")
    assert "hello" in _read_log(tmp_path / "two")
    logging_setup.reset()


def test_rotation_handler_configured(tmp_path):
    logging_setup.reset()
    fac = logging_setup.install(str(tmp_path), backup_days=7, to_console=False)
    assert isinstance(fac.file_handler, logging.handlers.TimedRotatingFileHandler)
    assert fac.file_handler.when.upper() == "MIDNIGHT"
    assert fac.file_handler.backupCount == 7
    logging_setup.reset()


def test_bridge_puts_log_records_into_events(tmp_path, records):
    svc, store = records
    logging_setup.wire_records(svc, str(tmp_path), to_console=False)
    logging_setup.get_logger("services.device_state").info(
        "已连接 ds86_01", extra={"device_id": "ds86_01"})
    rows = svc.query_events()
    assert len(rows) == 1
    assert rows[0]["event_type"] == "services.device_state"
    assert rows[0]["severity"] == "INFO"
    assert rows[0]["device_id"] == "ds86_01"        # 结构化字段透传到事件
    assert "已连接" in rows[0]["message"]


def test_bridge_respects_level(tmp_path, records):
    svc, store = records
    logging_setup.wire_records(svc, str(tmp_path), level=logging.WARNING,
                               to_console=False)
    lg = logging_setup.get_logger("svc")
    lg.info("info 不该入库")
    lg.warning("warning 要入库")
    types = [r["message"] for r in svc.query_events()]
    assert "info 不该入库" not in types
    assert "warning 要入库" in types


def test_domain_event_mirrors_to_file_without_dupe(tmp_path, records):
    """领域事件：入库一条 + 文件里出现一行；**不**因镜像再次入库（不重复）。"""
    svc, store = records
    logging_setup.wire_records(svc, str(tmp_path), to_console=False)
    svc.append_event(Event(event_type="task.start", severity=Severity.INFO,
                           message="启动 r1", run_id="run-1"))
    rows = svc.query_events()
    assert len(rows) == 1                           # 镜像不外泄到事件桥（跳过 towerops.event）
    assert rows[0]["event_type"] == "task.start"
    text = _read_log(tmp_path)
    assert "task.start" in text and "启动 r1" in text
    # 文件里这条只出现一次（镜像一次，日志桥不重复写文件）
    assert text.count("启动 r1") == 1


def test_exception_traceback_recorded(tmp_path, records):
    svc, store = records
    logging_setup.wire_records(svc, str(tmp_path), to_console=False)
    try:
        raise ValueError("注入故障")
    except ValueError:
        logging_setup.get_logger("svc.bad").error("执行期异常", exc_info=True)
    row = svc.query_events()[0]
    assert row["severity"] == "ERROR"
    assert "ValueError" in (row["reason"] or "")
    text = _read_log(tmp_path)
    assert "Traceback" in text and "注入故障" in text


def test_severity_mapping(tmp_path, records):
    svc, store = records
    logging_setup.wire_records(svc, str(tmp_path), to_console=False)
    lg = logging_setup.get_logger("s")
    lg.debug("d"); lg.info("i"); lg.warning("w"); lg.error("e"); lg.critical("c")
    sevs = {r["message"]: r["severity"] for r in svc.query_events()}
    assert sevs == {"i": "INFO", "w": "WARNING", "e": "ERROR", "c": "CRITICAL"}
    # DEBUG 默认不入库（桥是 INFO）
    assert "d" not in sevs
    # 但 DEBUG 仍应落文件
    assert "d" in _read_log(tmp_path)


def test_error_logging_never_breaks_business(tmp_path, records, monkeypatch):
    """事件桥写库失败时只 handleError，绝不把异常抛回业务。"""
    svc, store = records
    logging_setup.wire_records(svc, str(tmp_path), to_console=False)

    def boom(*_a, **_k):
        raise RuntimeError("库坏了")
    monkeypatch.setattr(svc, "append_event_nonblocking", boom)
    logging_setup.get_logger("s").error("仍不应抛")  # 不 raise 即通过


def test_get_logger_namespaced():
    assert logging_setup.get_logger("a.b").name == "towerops.a.b"
    assert logging_setup.get_logger().name == "towerops"
