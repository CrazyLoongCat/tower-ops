"""操作台监控弹窗：复用已打开的相机会话，有界后台采样，不建立私有连接。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from application.contracts.frames import CaptureRequest, FreshnessPolicy
from application.presentation.components import Card
from application.presentation.components.image_view import GUI_REQUIRED, ImageView, to_qimage
from application.presentation.pages import state_of, store_of
from application.presentation.theme import qss


def monitor_channels(ctx):
    return [c for c in store_of(ctx).config.cameras if c.enabled]


if GUI_REQUIRED:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
        QPushButton, QVBoxLayout,
    )

    class MonitorTile(Card):
        def __init__(self, ctx, index, parent=None):
            super().__init__(parent=parent)
            self.ctx = ctx
            self.latest_image = None
            self.snapshot = None
            self._channel_signature = None
            self._frame_id = None
            head = QHBoxLayout()
            self.pick = QComboBox()
            self.pick.setMinimumWidth(140)
            head.addWidget(self.pick, 1)
            self.close_button = QPushButton("关闭画面")
            self.close_button.setObjectName("Ghost")
            self.close_button.clicked.connect(lambda: self.pick.setCurrentIndex(0))
            head.addWidget(self.close_button)
            self.body().addLayout(head)
            self.view = ImageView(f"画面 {index + 1} · 请选择监控通道")
            self.view.setWordWrap(True)
            self.add(self.view)
            self.caption = QLabel("未选择通道")
            self.caption.setObjectName("SubLabel")
            self.caption.setWordWrap(True)
            self.caption.setTextFormat(Qt.TextFormat.PlainText)
            self.add(self.caption)
            buttons = QHBoxLayout()
            self.capture_button = QPushButton("抓拍")
            self.save_button = QPushButton("保存图片")
            for button in (self.capture_button, self.save_button):
                button.setObjectName("Ghost")
                button.setMinimumHeight(32)
                buttons.addWidget(button)
            self.body().addLayout(buttons)
            self.capture_button.clicked.connect(self.take_snapshot)
            self.save_button.clicked.connect(self.save_snapshot)
            self.pick.currentIndexChanged.connect(self.clear_channel)
            self.sync_channels()
            self.clear_channel()

        def sync_channels(self):
            channels = monitor_channels(self.ctx)
            signature = [(c.camera_id, c.name, c.confirmed) for c in channels]
            if signature == self._channel_signature:
                return
            self._channel_signature = signature
            selected = self.pick.currentData()
            self.pick.blockSignals(True)
            self.pick.clear()
            self.pick.addItem("（关闭 / 未选择）", None)
            for channel in channels:
                self.pick.addItem(channel.name or channel.camera_id, channel.camera_id)
            self.pick.setCurrentIndex(max(0, self.pick.findData(selected)))
            self.pick.blockSignals(False)
            if self.pick.currentData() != selected:
                self.clear_channel()

        def clear_channel(self, *_):
            self.latest_image = self.snapshot = None
            self._frame_id = None
            self.view.clear_image("等待画面" if self.camera_id else "请选择监控通道")
            self.caption.setText("等待相机画面" if self.camera_id else "未选择通道")
            self.capture_button.setEnabled(False)
            self.save_button.setEnabled(False)

        @property
        def camera_id(self):
            return self.pick.currentData()

        def show_error(self, message):
            self.latest_image = None
            self._frame_id = None
            self.view.clear_image(message)
            self.caption.setText(message)
            self.capture_button.setEnabled(False)
            self.save_button.setEnabled(self.snapshot is not None)

        def show_frame(self, frame, source):
            if frame.color is None:
                self.show_error("此通道无彩色流")
                return
            if frame.frame_id != self._frame_id:
                image = to_qimage(frame.color.readonly_view())
                if image is None:
                    self.show_error("无法显示此帧")
                    return
                self.latest_image = image
                self._frame_id = frame.frame_id
                self.view.show_array(frame.color.readonly_view())
            self.caption.setText(f"{source} · {frame.camera_id} · 帧 {frame.sequence}")
            self.capture_button.setEnabled(self.latest_image is not None)
            self.save_button.setEnabled(self.snapshot is not None)

        def take_snapshot(self):
            if self.latest_image is None:
                return
            self.snapshot = self.latest_image.copy()
            self.save_button.setEnabled(True)
            self.caption.setText(f"已抓拍 {self.camera_id} · 保存图片可导出原始分辨率")

        def save_snapshot(self):
            if self.snapshot is None:
                return
            # 抓拍时复制，后续视频刷新不能替换待保存的图片。
            image = self.snapshot.copy()
            path, _ = QFileDialog.getSaveFileName(
                self, "保存监控抓拍", f"capture_{datetime.now():%Y%m%d_%H%M%S}.png",
                "PNG 图片 (*.png);;JPEG 图片 (*.jpg)")
            if path:
                ok = image.save(path)
                self.caption.setText(f"已保存：{path}" if ok else f"保存失败：{path}")

    class VideoMonitorDialog(QDialog):
        def __init__(self, ctx, parent=None):
            super().__init__(parent)
            self.ctx = ctx
            self.setWindowTitle("视频监控")
            self.setStyleSheet(qss())
            self.resize(1120, 780)
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="console-monitor")
            self._future = None
            self._closed = False
            self._yielding = False
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)
            head = QHBoxLayout()
            title = QLabel("视频监控")
            title.setObjectName("SectionTitle")
            head.addWidget(title)
            head.addStretch()
            self.layout_pick = QComboBox()
            for label, count in (("单路画面", 1), ("双路画面", 2), ("四路画面", 4)):
                self.layout_pick.addItem(label, count)
            head.addWidget(self.layout_pick)
            self.preview_button = QPushButton("暂停预览")
            self.preview_button.setObjectName("Ghost")
            self.preview_button.setMinimumHeight(36)
            self.preview_button.clicked.connect(self._toggle_preview)
            head.addWidget(self.preview_button)
            close = QPushButton("关闭监控")
            close.setObjectName("Ghost")
            close.setMinimumHeight(36)
            close.clicked.connect(self.close)
            head.addWidget(close)
            root.addLayout(head)
            self.note = QLabel("")
            self.note.setObjectName("SubLabel")
            self.note.setWordWrap(True)
            root.addWidget(self.note)
            self.grid = QGridLayout()
            self.grid.setSpacing(10)
            self.tiles = [MonitorTile(ctx, i) for i in range(4)]
            root.addLayout(self.grid, 1)
            self.layout_pick.currentIndexChanged.connect(self._layout_changed)
            self._layout_changed()
            self._timer = QTimer(self)
            self._timer.setInterval(400)
            self._timer.timeout.connect(self.refresh)

        def visible_tiles(self):
            return self.tiles[:self.layout_pick.currentData()]

        def _layout_changed(self, *_):
            while self.grid.count():
                self.grid.takeAt(0)
            count = self.layout_pick.currentData()
            for i, tile in enumerate(self.tiles):
                if i < count:
                    row, column = divmod(i, 2) if count == 4 else (0, i)
                    self.grid.addWidget(tile, row, column)
                tile.setVisible(i < count)
            # 清除四路布局残留的行权重，单路/双路占满剩余高度。
            for row in range(2):
                self.grid.setRowStretch(row, 1 if row == 0 or count == 4 else 0)

        def open_channel(self, camera_id=None):
            if self._closed:
                return
            self._yielding = False
            for tile in self.tiles:
                tile.sync_channels()
            if camera_id:
                self.tiles[0].pick.setCurrentIndex(
                    max(0, self.tiles[0].pick.findData(camera_id)))
            self.show()
            self.raise_()
            self._timer.start()
            self.refresh()

        def _source(self):
            return ("离线合成（Fake 相机）" if self.ctx.stack.profile != "production"
                    else "真实相机")

        def _toggle_preview(self):
            self._yielding = not self._yielding
            self.refresh()

        def _sample(self, ids):
            return self.ctx.stack.camera_registry.capture(CaptureRequest(
                camera_ids=tuple(ids), required_streams=("color",), deadline_ms=400,
                allow_partial=True, freshness_policy=FreshnessPolicy.ALLOW_ARRIVAL_ONLY))

        def refresh(self):
            if self._closed or not self.isVisible():
                return
            for tile in self.tiles:
                tile.sync_channels()
            future = self._future
            result = None
            failure = None
            if future is not None and future.done():
                self._future = None
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    failure = str(exc)
            active = self.ctx.vm.is_task_active() or bool(self.ctx.stack.motion.busy)
            self.preview_button.setEnabled(not active)
            self.preview_button.setText("恢复预览" if self._yielding else "暂停预览")
            self.note.setText(
                "任务 / 运动执行期间显示本次任务最近帧；空闲后恢复连续预览。"
                if active else
                "连续预览已暂停，点击恢复预览继续。" if self._yielding else
                "连续预览已连接的彩色通道；未连接时请到设备配置连接。抓拍后可保存原始分辨率图片。")
            frames = self.ctx.vm.current_frames() if active else {}
            entries = {c.camera_id: c for c in monitor_channels(self.ctx)}
            state = state_of(self.ctx)
            ids = []
            for tile in self.visible_tiles():
                cid = tile.camera_id
                if not cid:
                    continue
                entry = entries.get(cid)
                if entry is None or not entry.confirmed or not state.is_open(cid):
                    tile.show_error("通道已移除" if entry is None else
                                    f"{state.status_text(entry)} · {state.detail_of(entry)}")
                    continue
                if active:
                    frame = frames.get(cid)
                    if frame is None:
                        tile.show_error("本次任务尚未抓拍此通道")
                    else:
                        tile.show_frame(frame, f"{self._source()} · 任务最近帧")
                    continue
                if result is not None:
                    frame = result.frames.get(cid)
                    if frame is not None:
                        tile.show_frame(frame, self._source())
                    elif cid in result.errors:
                        tile.show_error(result.errors[cid])
                elif failure:
                    tile.show_error(f"预览失败：{failure}")
                if cid not in ids:
                    ids.append(cid)
            if active:
                self._yielding = False
            if not active and not self._yielding and ids and self._future is None:
                self._future = self._pool.submit(self._sample, ids)

        def yield_camera(self):
            """任务启动前停止提交新预览，正在采集时不允许 UI 下发任务。"""
            self._yielding = True
            return not self.capturing

        @property
        def capturing(self):
            return self._future is not None and not self._future.done()

        def closeEvent(self, event):
            self._timer.stop()
            super().closeEvent(event)

        def reject(self):
            self._timer.stop()
            super().reject()

        def shutdown(self):
            self._closed = True
            self._timer.stop()
            self._pool.shutdown(wait=True, cancel_futures=True)
            self.close()
