"""操作台：方案与设备状态、执行概览、常用操作、监控入口。"""
from __future__ import annotations

from application.presentation.components import ActionRail, Card, StatusPill
from application.presentation.components.video_monitor import monitor_channels
from application.presentation.pages import state_of, store_of
from application.presentation.theme import THEME

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QProgressBar,
        QPushButton, QScrollArea, QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except ImportError:  # pragma: no cover
    GUI_REQUIRED = False

from application.presentation.pages.workbench import _STATUS_CN


if GUI_REQUIRED:
    class CapturePopup(QDialog):
        """操作台执行到抓拍时弹出的**整块**作业台相机卡片（DEC-095）。

        直接复用作业台右卡 ``workbench.image_card``（页签/画框/画圆/定位圆/图像/脚注），
        打开时从作业台布局“摘下”放进弹窗，关闭时放回原位；卡片仍由作业台刷新驱动。
        """

        def __init__(self, workbench, parent=None) -> None:
            super().__init__(parent)
            self.workbench = workbench
            self.setWindowTitle("相机通道 · 抓拍与检测圆")
            self.setModal(False)
            self.resize(1200, 900)
            self._lay = QVBoxLayout(self)
            self._lay.setContentsMargins(10, 10, 10, 10)
            self._lay.setSpacing(8)
            self._attached = False
            self._placeholder = None
            self._index = -1
            self.attach()

        def attach(self) -> None:
            if self._attached:
                return
            card = getattr(self.workbench, "image_card", None)
            slot = getattr(self.workbench, "_image_slot", None)
            if card is None or slot is None:
                return
            self._index = slot.indexOf(card)
            slot.removeWidget(card)
            card.setParent(self)
            self._lay.addWidget(card, 1)
            self._placeholder = QLabel("抓拍窗口已弹出（关闭弹窗后返回作业台）")
            self._placeholder.setObjectName("SubLabel")
            self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slot.insertWidget(self._index, self._placeholder, 4)
            card.show()
            self._attached = True

        def detach(self) -> None:
            if not self._attached:
                return
            card = getattr(self.workbench, "image_card", None)
            slot = getattr(self.workbench, "_image_slot", None)
            if card is not None:
                self._lay.removeWidget(card)
                card.setParent(None)
            if slot is not None and card is not None:
                if self._placeholder is not None:
                    slot.removeWidget(self._placeholder)
                    self._placeholder.deleteLater()
                    self._placeholder = None
                slot.insertWidget(self._index, card, 4)
                card.show()
            self._attached = False

        def closeEvent(self, event):  # pragma: no cover - Qt
            self.detach()
            super().closeEvent(event)


    class ConsolePage(QWidget):
        def __init__(self, ctx, workbench):
            super().__init__()
            self.ctx = ctx
            self.vm = ctx.vm
            self.workbench = workbench
            self.monitor = None
            self._capture_popup = None
            self._capture_key = None
            self._device_signature = None
            self._camera_signature = None
            root = QVBoxLayout(self)
            root.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            content = QWidget()
            body = QVBoxLayout(content)
            body.setContentsMargins(14, 14, 14, 14)
            body.setSpacing(12)
            title = QLabel("操作台")
            title.setStyleSheet(f"font-size:20px; font-weight:700; color:{THEME.text_primary};")
            body.addWidget(title)
            subtitle = QLabel("选择方案、执行作业与常用操作")
            subtitle.setObjectName("SubLabel")
            body.addWidget(subtitle)
            body.addWidget(self._build_header())
            body.addWidget(self._build_execution())  # DEC-091：按内容高度，空白留给下方卡片
            bottom = QHBoxLayout()
            bottom.setSpacing(12)
            bottom.addWidget(self._build_manual(), 2)
            bottom.addWidget(self._build_monitor(), 1)
            body.addLayout(bottom, 2)
            scroll.setWidget(content)
            root.addWidget(scroll)
            self._timer = QTimer(self)
            self._timer.setInterval(500)
            self._timer.timeout.connect(self._refresh_visible)
            self._timer.start()
            self.refresh()

        @staticmethod
        def _label(text, kind="SubLabel"):
            label = QLabel(text)
            label.setObjectName(kind)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            return label

        def _build_header(self):
            card = Card()
            card.setMaximumHeight(190)
            row = QHBoxLayout()
            row.setSpacing(24)
            selection = QVBoxLayout()
            selection.addWidget(self._label("选择执行方案", "SectionTitle"))
            self.recipe_combo = QComboBox()
            self.recipe_combo.setMinimumWidth(260)
            self.recipe_combo.activated.connect(self._select_recipe)
            selection.addWidget(self.recipe_combo)
            selection.addStretch(1)
            row.addLayout(selection, 1)
            right = QVBoxLayout()
            heading = QHBoxLayout()
            heading.addWidget(self._label("设备状态", "SectionTitle"))
            heading.addStretch()
            right.addLayout(heading)
            self.devices_grid = QGridLayout()
            self.devices_grid.setHorizontalSpacing(16)
            self.devices_grid.setVerticalSpacing(4)
            self.devices_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
            right.addLayout(self.devices_grid)
            row.addLayout(right, 2)
            card.body().addLayout(row)
            return card

        def _build_execution(self):
            card = Card()
            head = QHBoxLayout()
            head.addWidget(self._label("作业执行", "SectionTitle"))
            head.addStretch()
            card.body().addLayout(head)

            # DEC-089/092/110：上一步 / 当前执行步骤 / 下一步三列平铺整行，同一行对齐、
            # 各自水平居中；颜色区分（当前=深青，上一步/下一步=次要色），字号相同。
            step_grid = QGridLayout()
            step_grid.setHorizontalSpacing(24)
            step_grid.setVerticalSpacing(2)
            step_grid.setColumnStretch(0, 2)
            step_grid.setColumnStretch(1, 3)
            step_grid.setColumnStretch(2, 2)
            prev_title = self._label("上一步")
            prev_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cur_title = self._label("当前执行步骤")
            cur_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            next_title = self._label("下一步")
            next_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.prev_step = self._label("—")
            self.prev_step.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.prev_step.setStyleSheet(
                f"font-size:24px; font-weight:700; color:{THEME.text_secondary}; background:transparent;")
            self.current_step = self._label("等待启动")
            self.current_step.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.current_step.setStyleSheet(
                f"font-size:24px; font-weight:700; color:{THEME.deep_teal}; background:transparent;")
            self.next_step = self._label("")
            self.next_step.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.next_step.setStyleSheet(
                f"font-size:24px; font-weight:700; color:{THEME.text_secondary}; background:transparent;")
            self.step_detail = self._label("选择执行方案后启动作业")
            self.step_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
            step_grid.addWidget(prev_title, 0, 0)
            step_grid.addWidget(cur_title, 0, 1)
            step_grid.addWidget(next_title, 0, 2)
            step_grid.addWidget(self.prev_step, 1, 0)
            step_grid.addWidget(self.current_step, 1, 1)
            step_grid.addWidget(self.next_step, 1, 2)
            step_grid.addWidget(self.step_detail, 2, 0, 1, 3)
            card.body().addLayout(step_grid)
            # DEC-090：动作栏紧跟步骤信息，多余高度放到卡片底部，避免按钮上方大空白。
            self.rail = ActionRail()
            self.rail.connect(self._on_action)
            card.add(self.rail)
            progress_head = QHBoxLayout()
            progress_head.addWidget(self._label("执行进度", "SectionTitle"))
            progress_head.addStretch()
            self.progress_text = self._label("0 / 0 步")
            progress_head.addWidget(self.progress_text)
            card.body().addLayout(progress_head)
            self.progress = QProgressBar()
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.progress.setFixedHeight(18)
            self.progress.setStyleSheet(
                f"QProgressBar {{ border:0; border-radius:5px; background:#E8EEF0;"
                f" text-align:center; color:{THEME.text_primary}; }}"
                f"QProgressBar::chunk {{ background:{THEME.deep_teal}; border-radius:5px; }}")
            card.add(self.progress)
            self.block_reason = self._label("")
            card.add(self.block_reason)
            self.hint = self._label("")
            card.add(self.hint)
            card.body().addStretch(1)
            return card

        def _build_manual(self):
            card = Card("常用手工操作")
            card.add(self._label("进入对应控制页操作，沿用当前参数、设备互锁与运动权限。"))
            grid = QGridLayout()
            grid.setSpacing(10)
            # 快捷入口不隐式采用上次参数下发运动；在原控制页核对后执行。
            specs = (
                ("manual", "机构手动控制", "manual", None),
                ("motion", "路径实施 / 引导定位", "motion", 1),
                ("socket", "套筒前进 / 后退", "motion", 1),
                ("vision", "相机抓拍 / 视觉检测", "vision", None),
                ("plans", "运动方案", "motion", 0),
                ("logs", "日志诊断", "diagnostics", None),
            )
            # DEC-098：技术员只保留操作台+日志诊断，操作台里的跨页快捷入口
            # 也只留日志诊断，避免从按钮绕到未展示页面。
            if getattr(self.ctx, "role", "programmer") == "technician":
                specs = tuple(s for s in specs if s[2] == "diagnostics")
            self.manual_buttons = {}
            for i, (key, text, page, tab) in enumerate(specs):
                button = QPushButton(text)
                button.setObjectName("Ghost")
                button.setMinimumHeight(48)
                button.clicked.connect(lambda _=False, p=page, t=tab: self._navigate(p, t))
                grid.addWidget(button, *divmod(i, 2))
                self.manual_buttons[key] = button
            card.body().addLayout(grid)
            card.body().addStretch(1)
            self.manual_note = self._label("未绑定的机构动作在手动控制页保持禁用。")
            card.add(self.manual_note)
            return card

        def _build_monitor(self):
            card = Card("视频监控")
            row = QHBoxLayout()
            row.addWidget(self._label("可用监控通道"))
            row.addStretch()
            self.monitor_button = QPushButton("打开监控 ›")
            self.monitor_button.setObjectName("Ghost")
            self.monitor_button.setMinimumHeight(32)
            self.monitor_button.clicked.connect(lambda: self.open_monitor())
            row.addWidget(self.monitor_button)
            card.body().addLayout(row)
            self.cameras_layout = QVBoxLayout()
            card.body().addLayout(self.cameras_layout)
            card.body().addStretch(1)
            card.add(self._label("支持单路、双路、四路画面 · 抓拍 · 保存图片"))
            return card

        def _navigate(self, key, tab=None):
            window = self.window()
            if not hasattr(window, "pages"):
                return
            if self.monitor is not None and key in ("manual", "motion", "vision", "devices"):
                if not self.monitor.yield_camera():
                    self.vm._set_hint("正在结束监控采集，请稍后再次进入控制页", "warn")
                    self.refresh()
                    return
            page = window.pages[key]
            if tab is not None and hasattr(page, "tabs"):
                page.tabs.setCurrentIndex(tab)
            window._select(key)

        def _select_recipe(self, index):
            if not self.vm.is_task_active():
                self.vm.select_recipe(self.recipe_combo.itemData(index))
            self.refresh()

        def _on_action(self, key):
            self.workbench._on_action(key)
            self.refresh()

        def open_monitor(self, camera_id=None):
            if self.monitor is None:
                from application.presentation.components.video_monitor import VideoMonitorDialog
                self.monitor = VideoMonitorDialog(self.ctx, self.window())
            self.monitor.open_channel(camera_id)

        @staticmethod
        def _clear(layout):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget():
                    item.widget().setParent(None)
                    item.widget().deleteLater()

        def _refresh_devices(self):
            cfg = store_of(self.ctx).config
            state = state_of(self.ctx)
            entries = list(cfg.devices)
            keys = {state.key_of(e) for e in entries}
            entries += [c for c in cfg.cameras if c.camera_id not in keys]
            signature = [(state.key_of(e), e.name, state.status_text(e), state.detail_of(e))
                         for e in entries]
            if signature != self._device_signature:
                self._device_signature = signature
                self._clear(self.devices_grid)
                self.device_pills = []
                for i, (key, name, status, detail) in enumerate(signature):
                    # DEC-088：设备状态只有两色——已启用绿、未启用红；一行 5 个。
                    pill = StatusPill(
                        f"{name or key} · {status}",
                        tone=(THEME.ok if status == "已启用" else THEME.danger))
                    pill.setToolTip(f"{key} · {detail}")
                    pill.setFixedHeight(24)
                    self.devices_grid.addWidget(pill, i // 5, i % 5)
                    self.device_pills.append(pill)
                if not entries:
                    self.devices_grid.addWidget(self._label("尚未配置设备"), 0, 0)
            channels = monitor_channels(self.ctx)
            signature = [(c.camera_id, c.name, state.status_text(c)) for c in channels]
            if signature != self._camera_signature:
                self._camera_signature = signature
                self._clear(self.cameras_layout)
                self.camera_buttons = {}
                for cid, name, status in signature:
                    button = QPushButton(f"{name or cid}  ·  {status}    ›")
                    button.setObjectName("Ghost")
                    button.setMinimumHeight(40)
                    button.setToolTip(cid)
                    button.setStyleSheet(
                        f"color:{THEME.ok if status == '已启用' else THEME.danger};")
                    button.clicked.connect(lambda _=False, camera=cid: self.open_monitor(camera))
                    self.cameras_layout.addWidget(button)
                    self.camera_buttons[cid] = button
                if not channels:
                    self.cameras_layout.addWidget(self._label("暂无启用的相机，请先到设备配置添加。"))
            self.monitor_button.setEnabled(bool(channels))

        def _refresh_visible(self):
            if self.isVisible():
                self.refresh()

        def refresh(self):
            # 作业台仍是唯一的流程推进和确认弹窗所有者；隐藏时也同步其确认状态。
            self.workbench.refresh()
            st = self.vm.state()
            ids = [self.recipe_combo.itemData(i) for i in range(self.recipe_combo.count())]
            if ids != st.recipes:
                self.recipe_combo.clear()
                for rid in st.recipes:
                    self.recipe_combo.addItem(rid, rid)
                if not st.recipes:
                    self.recipe_combo.addItem("（无可用执行方案）", None)
            self.recipe_combo.setCurrentIndex(max(0, self.recipe_combo.findData(st.selected_recipe)))
            active = self.vm.is_task_active()
            self.recipe_combo.setEnabled(bool(st.recipes) and not active)
            self.recipe_combo.setToolTip("任务进行中，停止后可切换方案" if active else "选择执行方案")
            # 当前终态对应上一次方案；空闲切换方案后展示新方案的步骤，不能沿用旧进度。
            use_run = bool(st.steps) and (active or st.recipe_id == st.selected_recipe)
            statuses = {s.step_id: s.status for s in st.steps} if use_run else {}
            step_ids = list(statuses) if use_run else self.workbench._declared_step_ids(st.selected_recipe)
            titles = self.workbench._step_titles(st.recipe_id if use_run else st.selected_recipe)
            done = sum(statuses.get(sid) == "SUCCESS" for sid in step_ids)
            total = len(step_ids)
            pending = self.vm.pending_step_prompt() if active else None
            current = (pending.get("step_id") if pending else None)
            current = current or next((sid for sid in step_ids if statuses.get(sid) in (
                "RUNNING", "FAILURE", "STOPPED")), None)
            current = current or next((sid for sid in step_ids if statuses.get(sid) != "SUCCESS"), None)
            if st.task_state == "COMPLETED" and use_run:
                self.prev_step.setText(titles.get(step_ids[-1], step_ids[-1])
                                       if step_ids else "—")
                self.current_step.setText("作业已完成")
                self.step_detail.setText("全部步骤已执行完成")
                self.next_step.setText("可重新启动或选择其他执行方案")
            elif current:
                index = step_ids.index(current)
                previous = step_ids[index - 1] if index > 0 else None
                self.prev_step.setText(
                    titles.get(previous, previous) if previous else "当前为第一步")
                self.current_step.setText(titles.get(current, current))
                status = "待人工确认" if pending else _STATUS_CN.get(statuses.get(current, "PENDING"), "待执行")
                self.step_detail.setText(f"第 {index + 1} / {total} 步 · {status}")
                following = step_ids[index + 1] if index + 1 < total else None
                self.next_step.setText(titles.get(following, following) if following else "当前为最后一步")
            else:
                self.prev_step.setText("—")
                self.current_step.setText("等待启动" if step_ids else "暂无执行步骤")
                self.step_detail.setText("选择执行方案后启动作业")
                self.next_step.setText("")
            self.current_step.setToolTip(f"运行记录：{st.run_id or '尚未启动'}")
            self.progress.setValue(int(done * 100 / total) if total else 0)
            self.progress_text.setText(f"{done} / {total} 步已完成")
            self.rail.set_enabled(st.buttons)
            reasons = st.blocking_reasons
            self.block_reason.setText("启动检查：" + "；".join(reasons) if reasons else "方案检查通过")
            self.block_reason.setStyleSheet(f"color:{THEME.warn if reasons else THEME.ok}; background:transparent;")
            self.hint.setText(st.hint)
            self.manual_note.setText(
                "任务执行中，请先停止流程再进行独立手动操作。" if active else
                "未绑定的机构动作在手动控制页保持禁用。")
            for key, button in self.manual_buttons.items():
                button.setEnabled(not active or key == "logs")
            self._refresh_devices()
            self._maybe_show_capture_popup()

        def _maybe_show_capture_popup(self) -> None:  # pragma: no cover - Qt
            """DEC-094：执行过程中出现抓拍帧时，主动弹出共用图像模块。"""
            if not self.isVisible():
                return
            try:
                frames = dict(self.vm.current_frames())
                latest = self.vm.current_frame()
            except Exception:  # noqa: BLE001
                return
            if not frames and latest is not None:
                frames = {getattr(latest, "camera_id", "camera"): latest}
            if not frames:
                return
            key = tuple(sorted((cid, getattr(f, "frame_id", None))
                               for cid, f in frames.items()))
            if self._capture_popup is None:
                self._capture_popup = CapturePopup(self.workbench, self)
            popup = self._capture_popup
            if key != self._capture_key:
                self._capture_key = key
                popup.attach()
                popup.show()
                popup.raise_()

        def hideEvent(self, event):  # pragma: no cover - Qt
            # 离开操作台就把抓拍弹窗收起，卡片放回作业台原位。
            if self._capture_popup is not None:
                self._capture_popup.close()
            super().hideEvent(event)

        def shutdown(self):
            self._timer.stop()
            if self._capture_popup is not None:
                self._capture_popup.close()
                self._capture_popup.deleteLater()
                self._capture_popup = None
            if self.monitor is not None:
                self.monitor.shutdown()
