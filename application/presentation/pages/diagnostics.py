"""日志诊断 / 报告页 (E24/E25, 03 §10)。

**全量日志**：默认列出**全部**来源的事件（不限于当前 run）——启动/装配、设备连接与探测、
抓拍、任务/步骤/命令、运动、视觉判定、界面操作、异常 traceback 都进同一条时间线。
可按 等级 / 事件类型 / 设备 / 任务 / 关键字 过滤，页面开着时**自动刷新**；另有
「打开日志文件」直达文本日志（``<runtime>/logs/tower-ops.log``，按天轮转）。

报警含影响与下一步; 逐设备停止摘要; 回放显式隔离真实运动 (回放不建立真实运动通道)。

性能（DEC-049）: 事件表用**固定列宽**（消息列拉伸），不用 ``ResizeToContents``——
后者每次填充都要逐项量宽，400 行即可把 GUI 线程卡住数秒。自动刷新默认**增量**：
按 ``min_sequence`` 只取上次之后的新事件，无新事件不碰表格；下拉选项限频刷新；
页面隐藏时定时器不做事；右栏只在内容变化时重绘。
"""
from __future__ import annotations

import os
import time

from application.infrastructure import logging_setup
from application.presentation.components import Card, EmptyHint, StatusPill
from application.presentation.theme import THEME
from application.presentation.timefmt import to_beijing_text

try:  # pragma: no cover
    from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
    from PySide6.QtGui import QColor, QDesktopServices
    from PySide6.QtWidgets import (
        QAbstractItemView, QCheckBox, QComboBox, QHeaderView, QHBoxLayout, QLabel,
        QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
        QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

# 级别 -> 颜色（文字本身也标级别, 不只靠颜色）
_SEV_COLORS = {
    "DEBUG": THEME.text_secondary,
    "INFO": THEME.text_primary,
    "WARNING": THEME.warn,
    "ERROR": THEME.danger,
    "CRITICAL": THEME.danger,
}
_SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_ALL = "（全部）"
# 事件类型按模块前缀归类; 这里给出给人看的短名（找不到前缀就原样显示）
_TYPE_LABELS = {
    "app": "应用", "main": "入口", "ui": "界面", "event": "领域事件",
    "bootstrap": "装配", "services": "服务", "workflow": "流程", "motion": "运动",
    "guidance": "引导", "vision": "视觉", "records": "记录",
}

# DEC-049: 下拉选项（DISTINCT 扫描）限频；表格只保留最近 N 条
_OPTIONS_REFRESH_S = 15.0
# 单次增量相对上一次刷新最多接受的新事件数；超过则整表重载（固定列宽下也是毫秒级）
_INCREMENT_MAX = 200


def _short_type(event_type: str) -> str:
    if not event_type:
        return ""
    head = event_type.split(".", 1)[0]
    return _TYPE_LABELS.get(head, event_type)


if GUI_REQUIRED:

    class DiagnosticsPage(QWidget):
        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.vm = ctx.vm
            self._limit = 400
            # 增量刷新状态（DEC-049）
            self._rows: list[dict] = []
            self._last_seq: int | None = None
            self._filter_sig: tuple | None = None
            self._options_at = 0.0
            self._options_seq: int | None = None
            self._alarm_fault: bool | None = None
            self._stop_sig: tuple | None = None
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            # ---------- 过滤栏 ----------
            bar = QHBoxLayout()
            bar.setSpacing(8)

            self.severity_combo = QComboBox()
            self.severity_combo.addItem(_ALL, None)
            for sev in _SEVERITIES:
                self.severity_combo.addItem(sev, sev)
            bar.addWidget(QLabel("等级"))
            bar.addWidget(self.severity_combo)

            self.type_combo = QComboBox()
            self.type_combo.addItem(_ALL, None)
            bar.addWidget(QLabel("类型"))
            bar.addWidget(self.type_combo)

            self.device_combo = QComboBox()
            self.device_combo.addItem(_ALL, None)
            bar.addWidget(QLabel("设备"))
            bar.addWidget(self.device_combo)

            self.run_combo = QComboBox()
            self.run_combo.addItem(_ALL, None)
            bar.addWidget(QLabel("任务"))
            bar.addWidget(self.run_combo)

            bar.addWidget(QLabel("关键字"))
            self.keyword_edit = QLineEdit()
            self.keyword_edit.setPlaceholderText("消息/类型/原因 子串…")
            self.keyword_edit.setClearButtonEnabled(True)
            self.keyword_edit.setMinimumWidth(180)
            bar.addWidget(self.keyword_edit, 1)

            for widget in (self.severity_combo, self.type_combo, self.device_combo,
                           self.run_combo):
                widget.currentIndexChanged.connect(lambda _=0: self.refresh())
            self.keyword_edit.returnPressed.connect(self.refresh)

            refresh_btn = QPushButton("刷新")
            refresh_btn.setObjectName("Ghost")
            refresh_btn.clicked.connect(lambda: self.refresh())
            bar.addWidget(refresh_btn)

            self.auto_check = QCheckBox("自动刷新")
            self.auto_check.setChecked(True)
            self.auto_check.toggled.connect(self._on_auto_toggled)
            bar.addWidget(self.auto_check)

            self.open_btn = QPushButton("打开日志文件")
            self.open_btn.setObjectName("Ghost")
            self.open_btn.clicked.connect(self._open_log_file)
            bar.addWidget(self.open_btn)
            root.addLayout(bar)

            path_row = QHBoxLayout()
            self.path_label = QLabel()
            self.path_label.setObjectName("SubLabel")
            self.path_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            path_row.addWidget(QLabel("文本日志"))
            path_row.addWidget(self.path_label, 1)
            root.addLayout(path_row)

            # ---------- 事件表 + 右栏 ----------
            mid = QHBoxLayout(); mid.setSpacing(10)
            self.event_card = Card("事件（全部来源）")
            self.event_table = QTableWidget(0, 5)
            self.event_table.setHorizontalHeaderLabels(
                ["时间（北京时间）", "级别", "类型", "设备", "消息"])
            self.event_table.verticalHeader().setVisible(False)
            self.event_table.setEditTriggers(
                QAbstractItemView.EditTrigger.NoEditTriggers)
            self.event_table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows)
            self.event_table.setWordWrap(True)
            self.event_table.setVerticalScrollMode(
                QAbstractItemView.ScrollMode.ScrollPerPixel)
            # DEC-049: 固定列宽（消息列拉伸）。ResizeToContents 会为每个单元格
            # 反复求宽，400 行时一次刷新就能卡住界面数秒。
            hh = self.event_table.horizontalHeader()
            for col, width in ((0, 165), (1, 64), (2, 84), (3, 96)):
                hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
                self.event_table.setColumnWidth(col, width)
            hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
            self.event_card.add(self.event_table)
            mid.addWidget(self.event_card, 3)

            right = QVBoxLayout(); right.setSpacing(10)
            self.alarm_card = Card("报警（含影响与下一步）")
            self.alarm_layout = QVBoxLayout(); self.alarm_card.body().addLayout(self.alarm_layout)
            right.addWidget(self.alarm_card, 1)
            self.stop_card = Card("逐设备停止摘要")
            self.stop_layout = QVBoxLayout(); self.stop_card.body().addLayout(self.stop_layout)
            right.addWidget(self.stop_card, 1)
            mid.addLayout(right, 2)
            root.addLayout(mid, 1)

            self._timer = QTimer(self)
            self._timer.setInterval(2000)
            self._timer.timeout.connect(self._on_tick)
            self._timer.start()
            self.refresh()

        # ---------- 数据 ----------
        def _records(self):
            return getattr(self.ctx.stack, "records", None)

        def _log_path(self) -> str:
            facility = logging_setup.current()
            if facility is not None:
                return facility.log_path
            return logging_setup.log_path()

        def _sync_combo(self, combo: QComboBox, options: list,
                        labeler=None) -> None:
            """重建下拉项但**保留当前选择**（选项没变则不动，免得打断用户）。"""
            desired = [_ALL, *options]
            current = combo.currentData()
            existing = [combo.itemData(i) for i in range(combo.count())]
            if existing == desired:
                return
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(_ALL, None)
            for opt in options:
                text = labeler(str(opt)) if labeler else str(opt)
                combo.addItem(text, opt)
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

        def _filters(self) -> tuple:
            """当前过滤条件（含关键字）；也是增量刷新的缓存键。"""
            severity = self.severity_combo.currentData()
            event_type = self.type_combo.currentData()
            device_id = self.device_combo.currentData()
            run_id = self.run_combo.currentData()
            keyword = self.keyword_edit.text().strip()
            return severity, event_type, device_id, run_id, keyword

        @staticmethod
        def _query_kwargs(filters: tuple, **extra) -> dict:
            severity, event_type, device_id, run_id, keyword = filters
            kwargs: dict = dict(extra)
            if severity:
                kwargs["severity"] = severity
            if event_type:
                kwargs["event_type"] = event_type
            if device_id:
                kwargs["device_id"] = device_id
            if run_id is not None:
                kwargs["run_id"] = run_id
            if keyword:
                kwargs["keyword"] = keyword
            return kwargs

        def _refresh_options(self, records, *, force: bool = False) -> None:
            """下拉选项来自 DISTINCT 扫描：首次/有新事件时刷新，且**限频**。"""
            now = time.monotonic()
            if not force:
                if self._options_at and now - self._options_at < _OPTIONS_REFRESH_S:
                    return
                if self._options_at and self._options_seq == self._last_seq:
                    return
            try:
                self._sync_combo(self.type_combo, records.list_event_types(),
                                 labeler=_short_type)
                self._sync_combo(self.device_combo, records.list_event_devices())
                self._sync_combo(self.run_combo, records.list_event_runs())
            except Exception:  # noqa: BLE001 - 过滤项刷新失败不阻断主列表
                pass
            self._options_at = now
            self._options_seq = self._last_seq

        def refresh(self, rate_limit: bool = False) -> None:  # pragma: no cover - Qt
            """重载/增量刷新。

            ``rate_limit=True`` 只给 2 秒自动刷新用（下拉选项限频）；手动刷新/切换
            过滤/切页面传入默认 False，立即使下拉选项跟上。
            """
            self.path_label.setText(self._log_path())
            records = self._records()
            if records is None or not hasattr(records, "query_events"):
                self._rows = []
                self._last_seq = None
                self._filter_sig = None
                self._set_events_full([])
                self._render_alarms()
                self._render_stop()
                return

            filters = self._filters()
            if filters != self._filter_sig or self._last_seq is None:
                # 过滤条件变化 / 首次加载：整表重载
                self._filter_sig = filters
                try:
                    events = list(records.query_events(
                        limit=self._limit, newest_first=True,
                        **self._query_kwargs(filters)))
                except Exception:  # noqa: BLE001 - 检索失败保留旧表，不崩
                    self._render_alarms()
                    self._render_stop()
                    return
                self._set_events_full(events)
                self._refresh_options(records, force=True)
            else:
                # 增量：只取比上次更新的（DISTINCT 下拉不用每 2 秒扫全表）
                try:
                    new = list(records.query_events(
                        limit=_INCREMENT_MAX, newest_first=True,
                        min_sequence=self._last_seq,
                        **self._query_kwargs(filters)))
                except Exception:  # noqa: BLE001 - 检索失败保留旧表，不崩
                    new = []
                if len(new) >= _INCREMENT_MAX:
                    # 一次涌入太多（已截断）：退回整表重载，避免丢中间的条目
                    try:
                        new = list(records.query_events(
                            limit=self._limit, newest_first=True,
                            **self._query_kwargs(filters)))
                    except Exception:  # noqa: BLE001
                        new = []
                    self._set_events_full(new)
                elif new:
                    merged = new + self._rows
                    self._set_events_full(merged[: self._limit])
                self._refresh_options(records, force=not rate_limit)
            self._render_alarms()
            self._render_stop()

        def _fill_row(self, row: int, ev: dict) -> None:
            severity = str(ev.get("severity") or "")
            cells = [
                to_beijing_text(ev.get("utc")),
                severity,
                _short_type(str(ev.get("event_type") or "")),
                str(ev.get("device_id") or ""),
                str(ev.get("message") or ""),
            ]
            for c, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if c == 1:
                    item.setForeground(QColor(
                        _SEV_COLORS.get(severity, THEME.text_primary)))
                if c == 4:
                    reason = ev.get("reason")
                    tip = str(reason) if reason else str(ev.get("event_type") or "")
                    item.setToolTip(tip)
                self.event_table.setItem(row, c, item)

        def _selected_sequence(self):
            row = self.event_table.currentRow()
            if 0 <= row < len(self._rows):
                return self._rows[row].get("sequence")
            return None

        def _top_sequence(self):
            idx = self.event_table.indexAt(QPoint(0, 0))
            if idx.isValid() and 0 <= idx.row() < len(self._rows):
                return self._rows[idx.row()].get("sequence")
            return None

        def _row_of_sequence(self, sequence) -> int:
            if sequence is None:
                return -1
            for r, ev in enumerate(self._rows):
                if ev.get("sequence") == sequence:
                    return r
            return -1

        def _set_events_full(self, events: list) -> None:
            """整表重填（固定列宽下 ~10ms/400 行）；保留选中行与滚动位置。"""
            sel_seq = self._selected_sequence()
            top_seq = self._top_sequence()
            self._rows = list(events)
            seqs = [ev.get("sequence") for ev in events
                    if ev.get("sequence") is not None]
            self._last_seq = max(seqs) if seqs else 0
            table = self.event_table
            table.setUpdatesEnabled(False)
            try:
                table.setRowCount(0)
                table.setRowCount(len(events))
                for r, ev in enumerate(events):
                    self._fill_row(r, ev)
            finally:
                table.setUpdatesEnabled(True)
            row = self._row_of_sequence(sel_seq)
            if row >= 0:
                table.selectRow(row)
            else:
                row = self._row_of_sequence(top_seq)
                if row >= 0:
                    table.scrollToItem(
                        table.item(row, 0),
                        QAbstractItemView.ScrollHint.PositionAtTop)

        # ---------- 自动刷新 ----------
        def _on_tick(self) -> None:
            # 页面隐藏（或窗口最小化）时不做任何检索/重绘；切回页面时 _select 会刷新
            if self.isVisible():
                self.refresh(rate_limit=True)

        def _on_auto_toggled(self, checked: bool) -> None:
            if checked:
                self._timer.start()
            else:
                self._timer.stop()

        def _open_log_file(self) -> None:  # pragma: no cover - Qt
            path = self._log_path()
            if not os.path.exists(path):
                QMessageBox.information(
                    self, "日志文件",
                    f"暂未生成文本日志文件：\n{path}\n\n运行离线/真实模式后会产生。")
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

        # ---------- 右栏 ----------
        def _clear_layout(self, layout) -> None:
            while layout.count():
                it = layout.takeAt(0)
                w = it.widget()
                if w is not None:
                    w.setParent(None)

        def _render_alarms(self) -> None:
            fault = self.vm.state().task_state == "FAULT"
            if fault == self._alarm_fault:
                return                       # 内容没变，不重建控件
            self._alarm_fault = fault
            self._clear_layout(self.alarm_layout)
            if fault:
                lab = QLabel("故障：存在未决动作或未确认停止")
                lab.setObjectName("SubLabel"); lab.setWordWrap(True)
                self.alarm_layout.addWidget(lab)
                nxt = QLabel("下一步：核对设备/查看命令，未决资源隔离，不能直接重放动作")
                nxt.setObjectName("SubLabel"); nxt.setWordWrap(True)
                self.alarm_layout.addWidget(nxt)
            else:
                self.alarm_layout.addWidget(EmptyHint("无活动报警"))

        def _render_stop(self) -> None:
            rows = self.vm.state().stop_summary
            sig = tuple((row.left, row.right) for row in rows)
            if sig == self._stop_sig:
                return                       # 内容没变，不重建控件
            self._stop_sig = sig
            self._clear_layout(self.stop_layout)
            for row in rows:
                line = QWidget(); h = QHBoxLayout(line)
                h.setContentsMargins(0, 2, 0, 2); h.setSpacing(8)
                name = QLabel(row.left); name.setObjectName("StepTitle")
                pill = StatusPill(row.right)
                h.addWidget(name, 1); h.addWidget(pill)
                self.stop_layout.addWidget(line)
            if not rows:
                self.stop_layout.addWidget(EmptyHint("尚无停止记录"))
            self.stop_layout.addStretch(1)

        # ---------- 退出 ----------
        def shutdown(self) -> None:  # pragma: no cover - Qt
            try:
                self._timer.stop()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["GUI_REQUIRED", "DiagnosticsPage"]
