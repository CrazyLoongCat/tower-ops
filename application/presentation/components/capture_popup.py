"""相机图像卡片弹窗（DEC-119）。

把一块既有的**图像卡片**临时“摘下”放进独立窗口，关闭时放回原布局原位。
作业台相机卡片、运动方案引导画面、操作台抓拍弹窗共用同一个组件，
保证成像与画框/画圆能力一致，不各写一份。

用法：
    popup = CapturePopup(card, slot, parent=window)
    popup.attach(); popup.show()      # 打开
    popup.close()                     # 关闭 -> closeEvent 自动放回
"""
from __future__ import annotations

try:  # pragma: no cover - Qt
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QDialog = object  # type: ignore[assignment]

__all__ = ["CapturePopup", "GUI_REQUIRED"]


if GUI_REQUIRED:

    class CapturePopup(QDialog):
        """承载图像卡片的非模态弹窗；关闭时卡片自动回到 slot 原位。"""

        def __init__(self, card, slot, *, parent=None,
                     title: str = "相机画面",
                     placeholder_text: str = "画面窗口已弹出（关闭弹窗后返回原位）",
                     size: tuple[int, int] = (1200, 900)) -> None:
            super().__init__(parent)
            self._card = card
            self._slot = slot
            self._placeholder_text = placeholder_text
            self.setWindowTitle(title)
            self.setModal(False)
            # 可自由放大/缩小（DEC-130）：给最小尺寸 + 尺寸手柄 + 最大化按钮
            self.setMinimumSize(640, 480)
            try:
                self.setSizeGripEnabled(True)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.setWindowFlags(
                    self.windowFlags()
                    | Qt.WindowType.WindowMaximizeButtonHint
                    | Qt.WindowType.WindowMinimizeButtonHint)
            except Exception:  # noqa: BLE001
                pass
            self.resize(int(size[0]), int(size[1]))
            self._lay = QVBoxLayout(self)
            self._lay.setContentsMargins(10, 10, 10, 10)
            self._lay.setSpacing(8)
            self._attached = False
            self._placeholder = None
            self._index = -1

        # ---------- 摘/放 ----------
        def attach(self) -> None:
            if self._attached or self._card is None or self._slot is None:
                return
            self._index = self._slot.indexOf(self._card)
            self._slot.removeWidget(self._card)
            self._card.setParent(self)
            self._lay.addWidget(self._card, 1)
            self._placeholder = QLabel(self._placeholder_text)
            self._placeholder.setObjectName("SubLabel")
            self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._slot.insertWidget(max(self._index, 0), self._placeholder, 4)
            self._card.show()
            self._attached = True

        def detach(self) -> None:
            if not self._attached:
                return
            card, slot = self._card, self._slot
            if card is not None:
                self._lay.removeWidget(card)
                card.setParent(None)
            if slot is not None and card is not None:
                if self._placeholder is not None:
                    slot.removeWidget(self._placeholder)
                    self._placeholder.deleteLater()
                    self._placeholder = None
                slot.insertWidget(max(self._index, 0), card, 4)
                card.show()
            self._attached = False

        def is_attached(self) -> bool:
            return self._attached

        def closeEvent(self, event):  # pragma: no cover - Qt
            self.detach()
            super().closeEvent(event)

else:  # pragma: no cover - 无 Qt 环境
    class CapturePopup:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")
