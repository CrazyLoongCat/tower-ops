"""相机通道抓拍展示（作业台右卡与操作台弹窗共用，DEC-094）。

只负责“通道页签 + 图像 + 脚注”的展示骨架；是否允许画框/改圆由使用方通过
``view`` 的交互信号自行接线。作业台用它承载可编辑的检测框/圆，操作台弹窗以只读方式
显示同一 run 的最近抓拍。
"""
from __future__ import annotations

from typing import Iterable, Sequence

try:  # pragma: no cover - Qt
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QTabBar, QVBoxLayout, QWidget

    from .target_layout_view import TargetLayoutImageView
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

    class CaptureGalleryView:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")


if GUI_REQUIRED:

    class CaptureGalleryView(QWidget):
        """通道页签 + 彩色图 + 脚注（抓拍步骤/时间）。"""

        def __init__(self, placeholder: str = "尚未抓拍", parent=None) -> None:
            super().__init__(parent)
            self._lay = QVBoxLayout(self)
            self._lay.setContentsMargins(0, 0, 0, 0)
            self._lay.setSpacing(6)

            self.tabs = QTabBar()
            self.tabs.setObjectName("ChannelTabs")
            self.tabs.setExpanding(False)
            self.tabs.setDrawBase(False)
            self.tabs.setUsesScrollButtons(True)
            self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
            self.tabs.setVisible(False)
            self._lay.addWidget(self.tabs)

            self.view = TargetLayoutImageView(placeholder)
            self.view.setMinimumHeight(220)
            self._lay.addWidget(self.view, 1)

            foot = QHBoxLayout()
            self.caption = QLabel("此图未用于当前任务")
            self.caption.setObjectName("SubLabel")
            self.time = QLabel("-")
            self.time.setObjectName("SubLabel")
            foot.addWidget(self.caption, 1)
            foot.addWidget(self.time)
            self._lay.addLayout(foot)

            # 供作业台把“画框/定位圆/状态”等工具条插到页签与图像之间。
            self._toolbar_index = 1

        # ---------- 工具条 ----------
        def add_toolbar(self, layout) -> None:
            self._lay.insertLayout(self._toolbar_index, layout)
            self._toolbar_index += 1

        def add_widget_toolbar(self, widget: QWidget) -> None:
            self._lay.insertWidget(self._toolbar_index, widget)
            self._toolbar_index += 1

        # ---------- 通道页签 ----------
        def set_channels(self, channels: Sequence[tuple[str, str]],
                         current: str | None = None) -> None:
            current = current or self.selected_camera()
            self.tabs.blockSignals(True)
            while self.tabs.count():
                self.tabs.removeTab(0)
            for cid, name in channels:
                idx = self.tabs.addTab(name)
                self.tabs.setTabData(idx, cid)
                self.tabs.setTabToolTip(idx, f"相机通道：{cid}")
            if current:
                for i in range(self.tabs.count()):
                    if self.tabs.tabData(i) == current:
                        self.tabs.setCurrentIndex(i)
                        break
            self.tabs.blockSignals(False)
            self.tabs.setVisible(bool(channels))

        def selected_camera(self) -> str | None:
            idx = self.tabs.currentIndex()
            return (self.tabs.tabData(idx) or None) if idx >= 0 else None

        def select_camera(self, camera_id: str) -> bool:
            for i in range(self.tabs.count()):
                if self.tabs.tabData(i) == camera_id:
                    self.tabs.blockSignals(True)
                    self.tabs.setCurrentIndex(i)
                    self.tabs.blockSignals(False)
                    return True
            return False

        # ---------- 图像 ----------
        def show_frame(self, frame, caption: str, time_text: str = "-") -> bool:
            color = getattr(frame, "color", None) if frame is not None else None
            if color is None:
                self.view.clear_image("该帧无彩色流")
                self.caption.setText(caption or "该帧无彩色流")
                self.time.setText(time_text or "-")
                return False
            try:
                self.view.show_array(color.readonly_view(), kind="color")
            except Exception:  # noqa: BLE001
                self.view.show_array(color.data, kind="color")
            self.caption.setText(caption)
            self.time.setText(time_text or "-")
            return True

        def clear_image(self, text: str = "") -> None:
            self.view.clear_image(text or None)
            self.caption.setText(text or "此图未用于当前任务")
            self.time.setText("-")

        def circle_outline(self, cx: float, cy: float, r: float, n: int = 48):
            import numpy as _np
            angles = _np.linspace(0.0, 2.0 * _np.pi, n, endpoint=False)
            pts = _np.stack([cx + r * _np.cos(angles), cy + r * _np.sin(angles)], axis=1)
            return pts.reshape(-1, 1, 2).astype("float32")

        def draw_readonly_targets(self, boxes: Iterable[dict] | None = None,
                                  guide_circle: dict | None = None) -> None:
            """只读叠加：框=红矩形、圆=红圈、圆心=黄点、引导圆=青圈。"""
            marks = []
            points = []
            for box in (boxes or []):
                try:
                    x1, y1 = float(box["x1"]), float(box["y1"])
                    x2, y2 = float(box["x2"]), float(box["y2"])
                except (KeyError, TypeError, ValueError):
                    continue
                marks.append([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
                circle = box.get("circle")
                if isinstance(circle, dict):
                    try:
                        cx, cy = float(circle["cx"]), float(circle["cy"])
                        r = max(float(circle.get("radius_px") or 1.0), 1.0)
                    except (KeyError, TypeError, ValueError):
                        continue
                    marks.append(self.circle_outline(cx, cy, r))
                    points.append((cx, cy))
            if marks:
                self.view.set_mark_overlay(marks)
            else:
                self.view.clear_mark_overlay()
            if points:
                self.view.set_points_overlay(points)
            else:
                self.view.clear_points_overlay()
            if guide_circle:
                try:
                    cx, cy = float(guide_circle["cx"]), float(guide_circle["cy"])
                    r = max(float(guide_circle.get("radius_px") or 1.0), 1.0)
                except (KeyError, TypeError, ValueError):
                    self.view.clear_circle_overlay()
                else:
                    self.view.set_circle_overlay(cx, cy, r)
                    self.view.set_points_overlay([(cx, cy)])
            else:
                self.view.clear_circle_overlay()


__all__ = ["CaptureGalleryView", "GUI_REQUIRED"]
