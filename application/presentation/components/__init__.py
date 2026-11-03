"""通用界面组件包 (12 §5, 03 §4.1/§11)。

按 V0.2 视觉语言: 卡片、状态胶囊、分区标题、设备条、固定操作栏、阶段步进条。
所有组件只负责呈现, 不做业务判断。

本目录是**组件包**: 通用组件在本文件; 专用组件分文件
(如 :mod:`application.presentation.components.image_view` 的图像显示)。
"""
from __future__ import annotations

try:  # pragma: no cover
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import (
        QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout, QLabel,
        QPushButton, QSizePolicy, QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

from application.presentation.theme import THEME
from .capture_gallery import CaptureGalleryView

__all__ = [
    "ActionRail", "CaptureGalleryView", "Card", "DeviceStrip", "EmptyHint",
    "GUI_REQUIRED", "KV", "SectionTitle", "StatusPill", "Stepper",
]


def _card_shadow(widget) -> None:  # pragma: no cover - Qt
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(10)
    eff.setOffset(0, 1)
    eff.setColor(QColor(23, 43, 58, 28))
    widget.setGraphicsEffect(eff)


if GUI_REQUIRED:

    class Card(QFrame):
        """白底卡片 + 标题。"""

        def __init__(self, title: str | None = None, parent=None) -> None:
            super().__init__(parent)
            self.setObjectName("Card")
            self._lay = QVBoxLayout(self)
            self._lay.setContentsMargins(14, 12, 14, 14)
            self._lay.setSpacing(10)
            if title:
                self._lay.addWidget(SectionTitle(title))
            _card_shadow(self)

        def body(self) -> QVBoxLayout:
            return self._lay

        def add(self, w) -> None:
            self._lay.addWidget(w)

    class SectionTitle(QLabel):
        def __init__(self, text: str, parent=None) -> None:
            super().__init__(text, parent)
            self.setObjectName("SectionTitle")

    class StatusPill(QLabel):
        """状态胶囊: 圆点 + 文字, 颜色与文字同时表达 (不只靠颜色)."""

        _MAP = {
            "待执行": "#5A6B78", "待检查": THEME.warn, "待确认": THEME.warn,
            "待核对": THEME.warn, "待连接": THEME.warn,
            "已连接": THEME.ok, "正常": THEME.ok, "完成": THEME.ok,
            "SUCCESS": THEME.ok, "CONFIRMED": THEME.ok,
            "执行中": THEME.deep_teal, "RUNNING": THEME.deep_teal,
            "异常": THEME.danger, "失败": THEME.danger, "FAILURE": THEME.danger,
            "UNKNOWN": THEME.danger, "未知": THEME.danger,
            "禁用": THEME.text_disabled, "未绑定": THEME.text_disabled,
            "已启用": THEME.ok, "未启用": THEME.danger,
        }

        def __init__(self, text: str, tone: str | None = None, parent=None) -> None:
            super().__init__(parent)
            self.setObjectName("Pill")
            self.set_text(text, tone)

        def set_text(self, text: str, tone: str | None = None) -> None:
            color = tone or self._MAP.get(text, THEME.text_secondary)
            self.setText(f"● {text}")
            self.setStyleSheet(
                f"color:{color}; background:transparent; padding:2px 4px;"
                f" font-weight:600;")

    class KV(QLabel):
        """键值行: 主值 + 可选副标题。"""

        def __init__(self, key: str, value: str, value_color: str | None = None,
                     parent=None) -> None:
            super().__init__(parent)
            self.setObjectName("KV")
            self.set_key(key, value, value_color)

        def set_key(self, key: str, value: str, value_color: str | None = None) -> None:
            c = value_color or THEME.text_primary
            self.setText(
                f"<span style='color:{THEME.text_secondary}'>{key}</span>"
                f"<br><span style='color:{c};font-size:15px;font-weight:600'>"
                f"{value}</span>")

    class EmptyHint(QLabel):
        def __init__(self, text: str, parent=None) -> None:
            super().__init__(text, parent)
            self.setObjectName("EmptyHint")
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setWordWrap(True)

    class Stepper(QWidget):
        """阶段步进条: 编号圆 + 名称 + 当前/完成/待执行态。无工位后只剩「执行」一段。"""

        def __init__(self, stages: list[str], parent=None) -> None:
            super().__init__(parent)
            self.setObjectName("Stepper")
            self._lay = QHBoxLayout(self)
            self._lay.setContentsMargins(8, 10, 8, 10)
            self._lay.setSpacing(0)
            self._items: list[tuple[QLabel, QLabel]] = []
            self._cells: list[QWidget] = []
            self.set_stages(stages)

        def set_stages(self, stages: list[str]) -> None:
            stages = list(stages) or ["执行"]
            if getattr(self, "_stages", None) == stages:
                return                      # 不变则不重建, 避免旧控件残留重叠
            self._stages = stages
            while self._lay.count():
                item = self._lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)       # 立即脱离, 不能仅 deleteLater (会短暂残留)
            self._items.clear()
            self._cells.clear()
            for i, name in enumerate(stages):
                cell = QWidget()
                v = QVBoxLayout(cell)
                v.setContentsMargins(0, 0, 0, 0)
                v.setSpacing(4)
                num = QLabel(f"{i + 1:02d}")
                num.setAlignment(Qt.AlignmentFlag.AlignCenter)
                num.setFixedSize(34, 34)
                lab = QLabel(name)
                lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
                v.addWidget(num, alignment=Qt.AlignmentFlag.AlignHCenter)
                v.addWidget(lab, alignment=Qt.AlignmentFlag.AlignHCenter)
                self._lay.addWidget(cell, 1)
                self._items.append((num, lab))
                self._cells.append(cell)
                if i < len(stages) - 1:
                    bar = QFrame()
                    bar.setFixedHeight(2)
                    bar.setStyleSheet(f"background:{THEME.border};")
                    self._lay.addWidget(bar, 1)

        def set_current(self, index: int, completed: int = 0) -> None:
            """编号圆的背景/前景与其下方文字分开取色: 文字在浅色底上, 不能用白色。"""
            for i, (num, lab) in enumerate(self._items):
                if i < completed:
                    bg, fg, border, lab_color = THEME.ok, "#FFFFFF", THEME.ok, THEME.ok
                    cell_bg = "transparent"
                elif i == index:
                    bg, fg, border = THEME.deep_teal, "#FFFFFF", THEME.deep_teal
                    lab_color, cell_bg = THEME.deep_teal, "#E4EFEF"
                else:
                    bg, fg, border = "#FFFFFF", THEME.text_secondary, THEME.border
                    lab_color, cell_bg = THEME.text_secondary, "transparent"
                num.setStyleSheet(
                    f"background:{bg}; color:{fg}; border:1px solid {border};"
                    f" border-radius:17px; font-weight:700;")
                lab.setStyleSheet(
                    f"color:{lab_color};"
                    f" font-weight:{'700' if i == index else '500'};"
                    f" background:transparent;")
                if i < len(self._cells):
                    self._cells[i].setStyleSheet(
                        f"background:{cell_bg}; border-radius:6px;")

    class DeviceStrip(QWidget):
        """设备/依赖条: 名称 + 状态胶囊 (未验证显示待核对/待连接, 不显虚假绿灯)。"""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.setObjectName("DeviceStrip")
            self._lay = QHBoxLayout(self)
            self._lay.setContentsMargins(12, 8, 12, 8)
            self._lay.setSpacing(18)
            self._cells: list[tuple[QLabel, StatusPill]] = []

        def set_items(self, items) -> None:
            while self._lay.count():
                it = self._lay.takeAt(0)
                if it.widget():
                    it.widget().setParent(None)
                elif it.layout():
                    while it.layout().count():
                        sub = it.layout().takeAt(0)
                        if sub.widget():
                            sub.widget().setParent(None)
            self._cells.clear()
            for left, right in items:
                cell = QWidget()
                h = QHBoxLayout(cell)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(6)
                lab = QLabel(str(left))
                lab.setStyleSheet(f"color:{THEME.text_primary}; background:transparent;")
                pill = StatusPill(str(right))
                h.addWidget(lab)
                h.addWidget(pill)
                self._lay.addWidget(cell)
                self._cells.append((lab, pill))
            self._lay.addStretch(1)

    class ActionRail(QFrame):
        """固定操作栏：整行白色底+边框；主操作组靠左、停止全部靠右（始终可发停止）。"""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.setObjectName("ActionRail")
            self._lay = QHBoxLayout(self)
            self._lay.setContentsMargins(14, 10, 14, 10)
            self._lay.setSpacing(12)
            self.buttons: dict[str, QPushButton] = {}
            self._specs = (
                ("start", "启动作业", "Primary"),
                ("single_step", "单步", "Ghost"),
                ("fault_recover", "故障恢复", "Primary"),
                ("request_pause", "暂停", "Ghost"),
                ("resume", "继续作业", "Ghost"),
                ("terminate", "终止流程", "Neutral"),
                ("stop_all", "停止全部", "Danger"),
            )
            for key, text, kind in self._specs:
                btn = QPushButton(text)
                btn.setObjectName(kind)
                btn.setMinimumHeight(
                    THEME.button_key_height_px if kind in ("Primary", "Danger")
                    else THEME.button_min_height_px)
                self.buttons[key] = btn
                self._lay.addWidget(btn)
                if key == "terminate":
                    self._lay.addStretch(1)

        def set_enabled(self, matrix: dict[str, bool]) -> None:
            for key, btn in self.buttons.items():
                # “停止全部”始终可发停止请求 (03 §4.2)
                if key == "stop_all":
                    btn.setEnabled(True)
                else:
                    btn.setEnabled(bool(matrix.get(key)))

        def connect(self, handler) -> None:  # pragma: no cover - Qt
            for key, btn in self.buttons.items():
                btn.clicked.connect(lambda _=False, k=key: handler(k))

else:  # pragma: no cover - 无 Qt 环境
    class Card:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")

    class StatusPill:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")
