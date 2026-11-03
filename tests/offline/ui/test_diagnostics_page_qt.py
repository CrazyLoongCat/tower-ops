"""「日志诊断」页测试 (E24, DEC-029)。

用户诉求：诊断页过去**一个信息都没有**（只读当前 run 的事件）。现在必须列出**全部**
来源的日志（不限于当前 run），可按 等级/类型/设备/任务/关键字 过滤、自动刷新、并
能「打开日志文件」。这里钉住这些行为。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from application.bootstrap.composition import build_offline_stack  # noqa: E402
from application.infrastructure import logging_setup  # noqa: E402
from application.infrastructure.events import Event, Severity  # noqa: E402
from application.presentation.app import MainWindow, UiContext  # noqa: E402
from application.presentation.viewmodel import WorkbenchViewModel  # noqa: E402

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def win(tmp_path, qapp):
    import gc
    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path), recipe_path=RECIPE)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    w = MainWindow(UiContext(stack=stack, vm=vm))
    w.resize(1440, 1024)
    try:
        yield w, stack, qapp
    finally:
        try:
            w.shutdown(); w.close(); w.deleteLater()
            qapp.processEvents()
        except Exception:  # noqa: BLE001
            pass
        stack.close()
        logging_setup.reset()
        del w
        gc.collect()
        qapp.processEvents()


def _page(win):
    w, stack, app = win
    w._select("diagnostics"); app.processEvents()
    return w.pages["diagnostics"], stack, app


def _seed(stack):
    stack.records.append_event(Event(event_type="task.start", severity=Severity.INFO,
                                     message="启动 r1", run_id="run-1"))
    stack.records.append_event(Event(event_type="services.device_state",
                                     severity=Severity.INFO, message="已连接 ds86_01",
                                     device_id="ds86_01"))
    stack.records.append_event(Event(event_type="task.fault", severity=Severity.ERROR,
                                     message="抓拍集合失败", run_id="run-1",
                                     device_id="ds86_01", reason="相机未打开"))


def test_page_lists_all_logs_not_only_current_run(win):
    """没有跑任务时也要有内容：列出**全部**事件，而不是空提示。

    注：装配栈与开窗本身也会记日志，所以这里按「包含」判断，不钉死总行数。
    """
    page, stack, app = _page(win)
    _seed(stack)
    page.refresh(); app.processEvents()
    assert page.event_table.rowCount() >= 3
    messages = [page.event_table.item(r, 4).text()
                for r in range(page.event_table.rowCount())]
    assert any("启动 r1" in m for m in messages)
    assert any("抓拍集合失败" in m for m in messages)


def _column(page, col):
    return [page.event_table.item(r, col).text()
            for r in range(page.event_table.rowCount())]


def test_page_filters_by_severity_and_device(win):
    page, stack, app = _page(win)
    _seed(stack)
    page.refresh(); app.processEvents()

    page.severity_combo.setCurrentIndex(page.severity_combo.findData("ERROR"))
    page.refresh(); app.processEvents()
    assert _column(page, 1) and set(_column(page, 1)) == {"ERROR"}
    assert any("抓拍集合失败" in m for m in _column(page, 4))
    assert not any("已连接 ds86_01" in m for m in _column(page, 4))

    page.severity_combo.setCurrentIndex(0)               # 回到全部
    page.device_combo.setCurrentIndex(page.device_combo.findData("ds86_01"))
    page.refresh(); app.processEvents()
    assert set(_column(page, 3)) == {"ds86_01"}
    assert len([m for m in _column(page, 4) if "已连接 ds86_01" in m]) == 1


def test_page_filters_by_keyword(win):
    page, stack, app = _page(win)
    _seed(stack)
    page.keyword_edit.setText("启动 r1")
    page.refresh(); app.processEvents()
    assert page.event_table.rowCount() == 1
    assert "启动 r1" in page.event_table.item(0, 4).text()
    page.keyword_edit.setText("不存在的关键字")
    page.refresh(); app.processEvents()
    assert page.event_table.rowCount() == 0


def test_page_combo_options_come_from_events(win):
    page, stack, app = _page(win)
    _seed(stack)
    page.refresh(); app.processEvents()
    types = [page.type_combo.itemData(i) for i in range(page.type_combo.count())]
    assert "task.start" in types and "task.fault" in types
    devices = [page.device_combo.itemData(i) for i in range(page.device_combo.count())]
    assert "ds86_01" in devices


def test_page_shows_log_file_path_and_has_timer(win):
    page, stack, app = _page(win)
    assert page.path_label.text().endswith(os.path.join("logs", "tower-ops.log"))
    assert page._timer.isActive()
    page.shutdown()
    assert not page._timer.isActive()


def test_open_log_file_missing_shows_info(win, monkeypatch):
    """日志文件还没生成时不崩：弹一条说明即可。"""
    from PySide6.QtWidgets import QMessageBox
    page, stack, app = _page(win)
    calls = []
    monkeypatch.setattr(QMessageBox, "information",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(page, "_log_path", lambda: "/no/such/tower-ops.log")
    page._open_log_file()
    assert calls, "缺文件时应给出提示而不是静默"


def test_open_log_file_present_opens_it(win, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    page, stack, app = _page(win)
    logging_setup.get_logger("x").info("写一行让文件存在")
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    page._open_log_file()
    assert opened and opened[0].toLocalFile().endswith("tower-ops.log")


# ---------------------------------------------------------------- 北京时间 (DEC-038)
def test_times_are_shown_in_beijing_time(win):
    """列头写明北京时间；时间列 = 库存 UTC 换算成 UTC+8 的 YYYY-MM-DD HH:MM:SS。"""
    from application.presentation.timefmt import to_beijing_text
    page, stack, app = _page(win)
    _seed(stack)
    page.refresh(); app.processEvents()

    header = page.event_table.horizontalHeaderItem(0).text()
    assert "北京时间" in header

    times = _column(page, 0)
    assert times and all(len(t) == 19 for t in times)       # 无 T、无微秒、无 +00:00
    assert all("T" not in t and "+" not in t for t in times)

    events = list(stack.records.query_events(limit=200, newest_first=True))
    expect = {ev["message"]: to_beijing_text(ev["utc"]) for ev in events}
    rows = _column(page, 4)
    idx = rows.index("启动 r1")
    assert page.event_table.item(idx, 0).text() == expect["启动 r1"]


# ---------------------------------------------------------------- 刷新性能 (DEC-049)
def test_event_table_uses_fixed_width_columns(win):
    """列宽不许用 ResizeToContents：400 行时每次填充会逐项量宽，把界面卡死。"""
    from PySide6.QtWidgets import QHeaderView
    page, _stack, _app = _page(win)
    hh = page.event_table.horizontalHeader()
    for col in range(4):
        assert hh.sectionResizeMode(col) != QHeaderView.ResizeMode.ResizeToContents
    assert hh.sectionResizeMode(4) == QHeaderView.ResizeMode.Stretch


def test_refresh_without_new_events_does_not_rebuild_table(win, monkeypatch):
    page, stack, app = _page(win)
    _seed(stack)
    page.refresh(); app.processEvents()

    calls = []
    original = page._set_events_full
    monkeypatch.setattr(page, "_set_events_full",
                        lambda events: calls.append(len(events)) or original(events))
    page.refresh(); page.refresh(); app.processEvents()
    assert calls == [], "无新事件时不应整表重建"


def test_second_refresh_uses_incremental_min_sequence(win):
    page, stack, app = _page(win)
    _seed(stack)
    page.refresh(); app.processEvents()

    seen = []
    original = stack.records.query_events

    def spy(**kwargs):
        seen.append(kwargs)
        return original(**kwargs)

    stack.records.query_events = spy
    try:
        page.refresh()
    finally:
        stack.records.query_events = original
    assert seen, "自动刷新应查询事件库"
    assert seen[0].get("min_sequence") is not None


def test_incremental_refresh_prepends_and_keeps_selection(win):
    page, stack, app = _page(win)
    _seed(stack)
    page.refresh(); app.processEvents()
    idx = _column(page, 4).index("启动 r1")
    page.event_table.selectRow(idx)
    assert page.event_table.currentRow() == idx

    stack.records.append_event(Event(event_type="task.step", severity=Severity.INFO,
                                     message="后来 1", run_id="run-9"))
    stack.records.append_event(Event(event_type="task.step", severity=Severity.INFO,
                                     message="后来 2", run_id="run-9"))
    page.refresh(); app.processEvents()

    assert page.event_table.item(0, 4).text() == "后来 2"      # 最新在前
    assert _column(page, 4).index("启动 r1") == idx + 2
    cur = page.event_table.currentRow()
    assert cur >= 0 and page.event_table.item(cur, 4).text() == "启动 r1"


def test_incremental_refresh_respects_active_filter(win):
    page, stack, app = _page(win)
    _seed(stack)
    page.severity_combo.setCurrentIndex(page.severity_combo.findData("ERROR"))
    page.refresh(); app.processEvents()

    stack.records.append_event(Event(event_type="task.step", severity=Severity.INFO,
                                     message="不该出现", run_id="run-1"))
    stack.records.append_event(Event(event_type="task.fault", severity=Severity.ERROR,
                                     message="错误 2", run_id="run-1"))
    page.refresh(); app.processEvents()
    messages = _column(page, 4)
    assert "错误 2" in messages
    assert "不该出现" not in messages


def test_hidden_page_tick_does_nothing(win, monkeypatch):
    """页面不在前台时，2 秒定时器不查库、不重绘（窗口最小化同理）。"""
    page, _stack, _app = _page(win)
    assert page.isVisible() is False
    calls = []
    monkeypatch.setattr(page, "refresh", lambda *a, **k: calls.append(1))
    page._on_tick()
    assert calls == []


def test_large_table_fill_is_fast(win):
    """回归护栏：400 行填充必须远低于 2 秒（旧 ResizeToContents 实测约 8–17 秒）。"""
    import time

    page, _stack, app = _page(win)
    events = [{
        "sequence": i,
        "utc": "2026-10-19T02:00:00+00:00",
        "severity": "INFO",
        "event_type": "task.step",
        "device_id": f"dev-{i % 3}",
        "message": f"第 {i} 条比较长的日志消息，用于验证固定列宽下的填充速度",
    } for i in range(400)]
    times = []
    for _ in range(2):
        started = time.perf_counter()
        page._set_events_full(events)
        app.processEvents()
        times.append(time.perf_counter() - started)
    assert page.event_table.rowCount() == 400
    assert max(times) < 2.0, f"400 行填充耗时 {max(times):.2f}s，疑似又用回 ResizeToContents"
