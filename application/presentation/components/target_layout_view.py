# -*- coding: utf-8 -*-
"""作业台视觉步骤「多框 + 每框一个圆」标注视图（DEC-048）。

在 :class:`AnnotImageView` 的显示/缩放能力上叠加**本次运行**的框与圆：

- 显示：多个框，每个框内最多一个圆；选中框/圆高亮，圆带中心点与半径手柄；
- 画框：``set_target_mode("box")`` 后左键拖拽新框，松开发 ``box_requested``；
  页面按检测配置在框内找圆后把新布局回灌（本组件不自己找圆）；
- 画圆：``set_target_mode("circle")`` 后在**已有框内**拖拽画圆，发 ``circle_drawn``；
- 调整：编辑态下拖动圆移动圆心、拖右侧手柄改半径；拖动框移动（圆随动）、
  拖框边/角改框大小（圆夹在框内）；点击框/圆选中，Delete 删除选中项
  （选中圆只删圆、选中框删整个框）。
- 信号：``layout_changed`` 表示框/圆几何已在本地改完（页面据此重算判定并留证）。

坐标约定与 ``AnnotImageView`` 一致：场景坐标 = **原图像素**。
本组件不带定时器、不写配置、不动设备；布局是否保存由页面/节点决定。
"""
from __future__ import annotations

from typing import List, Optional

try:  # pragma: no cover - 与 annot_image_view 同一 Qt 依赖边界
    from PySide6.QtCore import QEvent, QRectF, Qt, Signal
    from PySide6.QtGui import QBrush, QColor, QFont, QPen
    from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem,
                                   QGraphicsRectItem, QGraphicsTextItem)
    from .annot_image_view import AnnotImageView
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    AnnotImageView = object  # type: ignore[assignment]

__all__ = ["GUI_REQUIRED", "TargetLayoutImageView"]

_BOX_COLOR = (0, 220, 90)        # 普通框：绿
_BOX_SEL_COLOR = (255, 190, 0)   # 选中框：琥珀
_CIRCLE_COLOR = (0, 210, 255)    # 普通圆：青
_CIRCLE_SEL_COLOR = (255, 80, 180)  # 选中圆：品红
_HANDLE_R = 6.0                  # 半径手柄命中半径（图像像素）
_EDGE_TOL = 6.0                  # 框边命中容差（图像像素）
_MIN_BOX = 5.0
_MIN_R = 3.0


if GUI_REQUIRED:

    class TargetLayoutImageView(AnnotImageView):
        """多框/多圆标注视图（作业台视觉判定步骤右卡）。"""

        box_requested = Signal(int, int, int, int)     # (x1,y1,x2,y2) 原图像素
        circle_drawn = Signal(int, int, int, int)      # (框索引, cx, cy, r)
        layout_changed = Signal()                      # 本地几何调整完成
        selection_changed = Signal(str, int)           # ("box"/"circle"/"", 索引或-1)
        mode_changed = Signal(str)                     # "" / "box" / "circle"
        message = Signal(str)                          # 界面提示（纯文本）

        def __init__(self, placeholder: str = "尚未抓拍", parent=None) -> None:
            super().__init__(placeholder, parent)
            self._tl_boxes: List[dict] = []
            self._tl_selected: Optional[int] = None
            self._tl_kind: str = ""
            self._tl_mode: Optional[str] = None
            self._tl_edit = False
            self._tl_drag = None            # (kind, idx[, edges]) / ("new_box",) / ("new_circle", idx)
            self._tl_start = None
            self._tl_orig_box = None
            self._tl_orig_circle = None
            self._tl_box_items: list = []
            self._tl_circle_items: list = []
            self._tl_preview_rect: Optional[QGraphicsRectItem] = None
            self._tl_preview_circle: Optional[QGraphicsEllipseItem] = None

        # ------------------------------------------------------------------
        #  布局状态
        # ------------------------------------------------------------------
        def set_layout(self, boxes, selected: Optional[int] = None) -> None:
            """回灌布局（页面同步节点状态用）；坐标非法项原样保留显示。"""
            out: List[dict] = []
            for b in boxes or ():
                if not isinstance(b, dict):
                    continue
                try:
                    box = {"x1": float(b["x1"]), "y1": float(b["y1"]),
                           "x2": float(b["x2"]), "y2": float(b["y2"]),
                           "circle": None}
                except (KeyError, TypeError, ValueError):
                    continue
                c = b.get("circle")
                if isinstance(c, dict):
                    try:
                        box["circle"] = {"cx": float(c["cx"]), "cy": float(c["cy"]),
                                         "radius_px": float(c["radius_px"])}
                    except (KeyError, TypeError, ValueError):
                        box["circle"] = None
                out.append(box)
            prev_sel, prev_kind = self._tl_selected, self._tl_kind
            self._tl_boxes = out
            if selected is not None and 0 <= int(selected) < len(out):
                self._tl_selected = int(selected)
            elif self._tl_selected is not None and self._tl_selected < len(out):
                self._tl_selected = self._tl_selected
            else:
                self._tl_selected = None
            if self._tl_selected is None:
                self._tl_kind = ""
            elif self._tl_selected == prev_sel and prev_kind in ("box", "circle"):
                # 同一选中项刷新时保留用户点击的粒度（框 vs 圆），删除语义不漂移
                self._tl_kind = ("circle"
                                 if prev_kind == "circle"
                                 and out[self._tl_selected].get("circle")
                                 else "box")
            else:
                self._tl_kind = ("circle" if out[self._tl_selected].get("circle")
                                 else "box")
            self._tl_edit = bool(out)
            self._rebuild_items()

        def layout(self) -> dict:
            return {"boxes": [
                {"x1": b["x1"], "y1": b["y1"], "x2": b["x2"], "y2": b["y2"],
                 "circle": dict(b["circle"]) if b.get("circle") else None}
                for b in self._tl_boxes],
                "selected": self._tl_selected}

        def boxes(self) -> list:
            return self.layout()["boxes"]

        def selected(self) -> dict:
            return {"kind": self._tl_kind, "index": self._tl_selected}

        def clear_target_layout(self) -> None:
            self._tl_boxes = []
            self._tl_selected = None
            self._tl_kind = ""
            self._tl_drag = None
            self._tl_edit = False
            self._clear_previews()
            self._rebuild_items()

        def is_dragging(self) -> bool:
            return self._tl_drag is not None

        # ---- 与 DEC-042 旧接口的兼容（页面/旧用例读取）----
        def circle_overlay(self):
            sel = self._tl_selected
            if sel is None or not (0 <= sel < len(self._tl_boxes)):
                return super().circle_overlay()      # DEC-084：引导定位的只读圆
            c = self._tl_boxes[sel].get("circle")
            if not c:
                return super().circle_overlay()
            return (c["cx"], c["cy"], c["radius_px"])

        def circle_edit_mode(self) -> bool:
            return self._tl_edit

        def target_edit_mode(self) -> bool:
            return self._tl_edit

        # ------------------------------------------------------------------
        #  模式
        # ------------------------------------------------------------------
        def set_target_mode(self, mode: Optional[str]) -> None:
            if mode not in (None, "box", "circle"):
                raise ValueError(f"未知标注模式: {mode!r}")
            self._tl_mode = mode
            self._tl_drag = None
            self._clear_previews()
            # 与 ROI/多边形/单圆模式互斥
            super().set_annotation_mode(None)
            super().set_edit_mode(False)
            super().set_circle_edit_mode(False)
            self.view.viewport().setCursor(
                Qt.CursorShape.CrossCursor if mode
                else (Qt.CursorShape.SizeAllCursor if self._tl_edit
                      else Qt.CursorShape.CrossCursor))
            self.mode_changed.emit(mode or "")

        def target_mode(self) -> str:
            return self._tl_mode or ""

        def set_target_edit_mode(self, enabled: bool) -> None:
            self._tl_edit = bool(enabled)
            if not self._tl_edit:
                self._tl_drag = None
                self._clear_previews()

        # ------------------------------------------------------------------
        #  删除
        # ------------------------------------------------------------------
        def delete_selected(self) -> bool:
            """删除选中项：选中圆只删圆，选中框删整框。返回是否删除。"""
            idx = self._tl_selected
            if idx is None or not (0 <= idx < len(self._tl_boxes)):
                self.message.emit("请先点击选中一个框或圆，再删除")
                return False
            kind = self._tl_kind or ("circle" if self._tl_boxes[idx].get("circle")
                                     else "box")
            if kind == "circle" and self._tl_boxes[idx].get("circle"):
                self._tl_boxes[idx]["circle"] = None
                self._tl_kind = "box"
                self.message.emit(f"已删除框 {idx + 1} 内的圆（框保留）")
            else:
                self._tl_boxes.pop(idx)
                if not self._tl_boxes:
                    self._tl_selected = None
                    self._tl_kind = ""
                else:
                    self._tl_selected = min(idx, len(self._tl_boxes) - 1)
                    self._tl_kind = ("circle" if self._tl_boxes[
                        self._tl_selected].get("circle") else "box")
                self.message.emit(f"已删除框 {idx + 1}")
            self._rebuild_items()
            self.selection_changed.emit(self._tl_kind,
                                        -1 if self._tl_selected is None
                                        else self._tl_selected)
            self.layout_changed.emit()
            return True

        # ------------------------------------------------------------------
        #  绘制
        # ------------------------------------------------------------------
        def _clear_items(self) -> None:
            for group in (self._tl_box_items, self._tl_circle_items):
                for items in group:
                    for item in items:
                        try:
                            self.scene.removeItem(item)
                        except Exception:  # noqa: BLE001
                            pass
                group.clear()

        def _rebuild_items(self) -> None:
            self._clear_items()
            for i, box in enumerate(self._tl_boxes):
                selected = (i == self._tl_selected and self._tl_kind != "circle")
                color = _BOX_SEL_COLOR if selected else _BOX_COLOR
                rect = QGraphicsRectItem(QRectF(box["x1"], box["y1"],
                                                box["x2"] - box["x1"],
                                                box["y2"] - box["y1"]))
                rect.setPen(QPen(QColor(*color), 4 if selected else 2,
                                 Qt.PenStyle.DashLine if box.get("circle") is None
                                 else Qt.PenStyle.SolidLine))
                rect.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                rect.setZValue(9)
                self.scene.addItem(rect)
                label = QGraphicsTextItem(
                    f"框 {i + 1}" + ("（无圆）" if box.get("circle") is None else "")
                    + (" ·选中" if selected else ""))
                label.setDefaultTextColor(QColor(*color))
                label.setFont(QFont("Microsoft YaHei", 9))
                label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                label.setPos(box["x1"] + 2, max(0.0, box["y1"] - 2))
                label.setZValue(13)
                self.scene.addItem(label)
                self._tl_box_items.append([rect, label])

                circle = box.get("circle")
                if not circle:
                    self._tl_circle_items.append([])
                    continue
                cx, cy, r = circle["cx"], circle["cy"], circle["radius_px"]
                sel_circle = (i == self._tl_selected and self._tl_kind == "circle")
                ccolor = _CIRCLE_SEL_COLOR if sel_circle else _CIRCLE_COLOR
                ellipse = QGraphicsEllipseItem(QRectF(cx - r, cy - r, 2 * r, 2 * r))
                ellipse.setPen(QPen(QColor(*ccolor), 4 if sel_circle else 2))
                ellipse.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                ellipse.setZValue(11)
                self.scene.addItem(ellipse)
                center = QGraphicsEllipseItem(QRectF(cx - 4, cy - 4, 8, 8))
                center.setBrush(QBrush(QColor(255, 255, 255)))
                center.setPen(QPen(QColor(*ccolor), 1))
                center.setZValue(12)
                self.scene.addItem(center)
                items = [ellipse, center]
                if sel_circle:
                    handle = QGraphicsEllipseItem(
                        QRectF(cx + r - _HANDLE_R, cy - _HANDLE_R,
                               _HANDLE_R * 2, _HANDLE_R * 2))
                    handle.setBrush(QBrush(QColor(255, 255, 255)))
                    handle.setPen(QPen(QColor(*ccolor), 1))
                    handle.setZValue(12)
                    self.scene.addItem(handle)
                    items.append(handle)
                self._tl_circle_items.append(items)

        def _clear_previews(self) -> None:
            for attr in ("_tl_preview_rect", "_tl_preview_circle"):
                item = getattr(self, attr)
                if item is not None:
                    try:
                        self.scene.removeItem(item)
                    except Exception:  # noqa: BLE001
                        pass
                    setattr(self, attr, None)

        def _show_preview_rect(self, x1, y1, x2, y2) -> None:
            if self._tl_preview_rect is None:
                self._tl_preview_rect = QGraphicsRectItem()
                self._tl_preview_rect.setPen(QPen(QColor(*_BOX_SEL_COLOR), 2,
                                                  Qt.PenStyle.DashLine))
                self._tl_preview_rect.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                self._tl_preview_rect.setZValue(14)
                self.scene.addItem(self._tl_preview_rect)
            self._tl_preview_rect.setRect(QRectF(min(x1, x2), min(y1, y2),
                                                 abs(x2 - x1), abs(y2 - y1)))
            self._tl_preview_rect.show()

        def _show_preview_circle(self, cx, cy, r) -> None:
            if self._tl_preview_circle is None:
                self._tl_preview_circle = QGraphicsEllipseItem()
                self._tl_preview_circle.setPen(QPen(QColor(*_CIRCLE_SEL_COLOR), 2,
                                                    Qt.PenStyle.DashLine))
                self._tl_preview_circle.setBrush(QBrush(Qt.BrushStyle.NoBrush))
                self._tl_preview_circle.setZValue(14)
                self.scene.addItem(self._tl_preview_circle)
            self._tl_preview_circle.setRect(QRectF(cx - r, cy - r, 2 * r, 2 * r))
            self._tl_preview_circle.show()

        # ------------------------------------------------------------------
        #  命中
        # ------------------------------------------------------------------
        @staticmethod
        def _inside(box, sp) -> bool:
            return (box["x1"] <= sp.x() <= box["x2"]
                    and box["y1"] <= sp.y() <= box["y2"])

        def _box_at(self, sp) -> Optional[int]:
            for i in range(len(self._tl_boxes) - 1, -1, -1):
                if self._inside(self._tl_boxes[i], sp):
                    return i
            return None

        def _edges_at(self, idx: int, sp) -> list:
            b = self._tl_boxes[idx]
            edges = []
            if abs(sp.x() - b["x1"]) <= _EDGE_TOL:
                edges.append("l")
            if abs(sp.x() - b["x2"]) <= _EDGE_TOL:
                edges.append("r")
            if abs(sp.y() - b["y1"]) <= _EDGE_TOL:
                edges.append("t")
            if abs(sp.y() - b["y2"]) <= _EDGE_TOL:
                edges.append("b")
            if edges and (b["x1"] - _EDGE_TOL <= sp.x() <= b["x2"] + _EDGE_TOL
                          and b["y1"] - _EDGE_TOL <= sp.y() <= b["y2"] + _EDGE_TOL):
                return edges
            return []

        def _hit(self, sp):
            # 1) 选中圆的半径手柄优先
            idx = self._tl_selected
            if idx is not None and 0 <= idx < len(self._tl_boxes):
                c = self._tl_boxes[idx].get("circle")
                if c:
                    hx, hy = c["cx"] + c["radius_px"], c["cy"]
                    if ((sp.x() - hx) ** 2 + (sp.y() - hy) ** 2) ** 0.5 <= _HANDLE_R + 2:
                        return ("circle_resize", idx)
            # 2) 圆内（后画的框优先）
            order = list(range(len(self._tl_boxes) - 1, -1, -1))
            if idx is not None and idx in order:
                order.remove(idx)
                order.insert(0, idx)
            for i in order:
                c = self._tl_boxes[i].get("circle")
                if not c:
                    continue
                if ((sp.x() - c["cx"]) ** 2 + (sp.y() - c["cy"]) ** 2) ** 0.5 \
                        <= c["radius_px"] + 2:
                    return ("circle_move", i)
            # 3) 框边/框内
            for i in order:
                edges = self._edges_at(i, sp)
                if edges:
                    return ("box_resize", i, edges)
                if self._inside(self._tl_boxes[i], sp):
                    return ("box_move", i)
            return None

        # ------------------------------------------------------------------
        #  事件
        # ------------------------------------------------------------------
        def eventFilter(self, obj, event):
            if obj is not self.view.viewport():
                return super().eventFilter(obj, event)
            et = event.type()
            if et == QEvent.Type.Wheel:
                return super().eventFilter(obj, event)
            if et == QEvent.Type.MouseButtonDblClick and (self._tl_mode or self._tl_edit):
                return True
            if et == QEvent.Type.MouseButtonPress and (self._tl_mode or self._tl_edit):
                self.view.setFocus(Qt.FocusReason.MouseFocusReason)
                return self._tl_press(event)
            if et == QEvent.Type.MouseMove and self._tl_drag is not None:
                return self._tl_move(event)
            if et == QEvent.Type.MouseButtonRelease and self._tl_drag is not None:
                return self._tl_release(event)
            return super().eventFilter(obj, event)

        def keyPressEvent(self, event) -> None:
            if event.key() == Qt.Key.Key_Delete and self._tl_edit:
                if self.delete_selected():
                    return
            if event.key() == Qt.Key.Key_Escape and self._tl_mode:
                self.set_target_mode(None)
                return
            super().keyPressEvent(event)

        def _scene(self, event):
            return self.view.mapToScene(event.position().toPoint())

        def _tl_press(self, event) -> bool:
            if event.button() != Qt.MouseButton.LeftButton:
                return True
            sp = self._scene(event)
            if self._tl_mode == "box":
                self._tl_drag = ("new_box",)
                self._tl_start = sp
                return True
            if self._tl_mode == "circle":
                idx = self._box_at(sp)
                if idx is None:
                    self.message.emit("请在已有框内拖动画圆；框外请先点「画框」")
                    return True
                self._tl_selected = idx
                self._tl_kind = "circle"
                self._tl_drag = ("new_circle", idx)
                self._tl_start = sp
                self._rebuild_selection()
                return True
            if not self._tl_edit:
                return True
            hit = self._hit(sp)
            if hit is None:
                self._tl_selected = None
                self._tl_kind = ""
                self._tl_drag = None
                self._rebuild_items()
                self.selection_changed.emit("", -1)
                return True
            kind = "circle" if hit[0].startswith("circle") else "box"
            idx = hit[1]
            self._tl_selected = idx
            self._tl_kind = kind
            self._tl_start = sp
            self._tl_orig_box = dict(self._tl_boxes[idx])
            c = self._tl_boxes[idx].get("circle")
            self._tl_orig_circle = dict(c) if c else None
            if hit[0] == "circle_resize":
                self._tl_drag = ("circle_resize", idx)
            elif hit[0] == "circle_move":
                self._tl_drag = ("circle_move", idx)
            elif hit[0] == "box_resize":
                self._tl_drag = ("box_resize", idx, hit[2])
            else:
                self._tl_drag = ("box_move", idx)
            self._rebuild_selection()
            return True

        def _rebuild_selection(self) -> None:
            """选中态变化只重画本地叠加，不发 layout_changed（几何未变）。"""
            self._rebuild_items()
            self.selection_changed.emit(self._tl_kind,
                                        -1 if self._tl_selected is None
                                        else self._tl_selected)

        def _tl_move(self, event) -> bool:
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                return True
            sp = self._scene(event)
            kind = self._tl_drag[0]
            if kind == "new_box":
                self._show_preview_rect(self._tl_start.x(), self._tl_start.y(),
                                        sp.x(), sp.y())
                return True
            if kind == "new_circle":
                idx = self._tl_drag[1]
                b = self._tl_boxes[idx]
                cx = max(b["x1"], min(self._tl_start.x(), b["x2"]))
                cy = max(b["y1"], min(self._tl_start.y(), b["y2"]))
                r = ((sp.x() - cx) ** 2 + (sp.y() - cy) ** 2) ** 0.5
                r = min(r, min(cx - b["x1"], b["x2"] - cx,
                               cy - b["y1"], b["y2"] - cy))
                self._show_preview_circle(cx, cy, max(_MIN_R, r))
                return True
            idx = self._tl_drag[1]
            box = self._tl_boxes[idx]
            orig = self._tl_orig_box
            circle = box.get("circle")
            if kind == "circle_move" and circle:
                # 按**圆的半径**夹圆心（用框半边长会把圆心永远锁在框中心）
                r = float(circle["radius_px"])
                max_r = min(orig["x2"] - orig["x1"],
                            orig["y2"] - orig["y1"]) / 2.0
                r = max(1.0, min(r, max_r))
                circle["radius_px"] = r
                circle["cx"] = max(orig["x1"] + r, min(sp.x(), orig["x2"] - r))
                circle["cy"] = max(orig["y1"] + r, min(sp.y(), orig["y2"] - r))
            elif kind == "circle_resize" and circle:
                cx, cy = circle["cx"], circle["cy"]
                r = ((sp.x() - cx) ** 2 + (sp.y() - cy) ** 2) ** 0.5
                max_r = min(cx - box["x1"], box["x2"] - cx,
                            cy - box["y1"], box["y2"] - cy)
                circle["radius_px"] = max(_MIN_R, min(r, max_r))
            elif kind == "box_move":
                dx, dy = sp.x() - self._tl_start.x(), sp.y() - self._tl_start.y()
                w, h = orig["x2"] - orig["x1"], orig["y2"] - orig["y1"]
                x1 = min(max(0.0, orig["x1"] + dx), self._max_x() - w)
                y1 = min(max(0.0, orig["y1"] + dy), self._max_y() - h)
                box["x1"], box["y1"] = x1, y1
                box["x2"], box["y2"] = x1 + w, y1 + h
                if circle and self._tl_orig_circle:
                    circle["cx"] = self._tl_orig_circle["cx"] + (x1 - orig["x1"])
                    circle["cy"] = self._tl_orig_circle["cy"] + (y1 - orig["y1"])
            elif kind == "box_resize":
                edges = self._tl_drag[2]
                x1, y1, x2, y2 = orig["x1"], orig["y1"], orig["x2"], orig["y2"]
                if "l" in edges:
                    x1 = min(sp.x(), x2 - _MIN_BOX)
                if "r" in edges:
                    x2 = max(sp.x(), x1 + _MIN_BOX)
                if "t" in edges:
                    y1 = min(sp.y(), y2 - _MIN_BOX)
                if "b" in edges:
                    y2 = max(sp.y(), y1 + _MIN_BOX)
                x1, x2 = max(0.0, x1), min(self._max_x(), x2)
                y1, y2 = max(0.0, y1), min(self._max_y(), y2)
                box["x1"], box["y1"], box["x2"], box["y2"] = x1, y1, x2, y2
                if circle:
                    self._clamp_circle(box)
            self._rebuild_items()
            return True

        def _max_x(self) -> float:
            if self.image_size is None:
                return 1e9
            return float(max(1, self.image_size.width() - 1))

        def _max_y(self) -> float:
            if self.image_size is None:
                return 1e9
            return float(max(1, self.image_size.height() - 1))

        @staticmethod
        def _clamp_circle(box: dict) -> None:
            c = box.get("circle")
            if not c:
                return
            x1, y1, x2, y2 = box["x1"], box["y1"], box["x2"], box["y2"]
            max_r = min(x2 - x1, y2 - y1) / 2.0
            if max_r < 1.0:
                box["circle"] = None
                return
            c["radius_px"] = max(1.0, min(c["radius_px"], max_r))
            c["cx"] = max(x1 + c["radius_px"], min(c["cx"], x2 - c["radius_px"]))
            c["cy"] = max(y1 + c["radius_px"], min(c["cy"], y2 - c["radius_px"]))

        def _tl_release(self, event) -> bool:
            if event.button() != Qt.MouseButton.LeftButton:
                return True
            sp = self._scene(event)
            drag = self._tl_drag
            self._tl_drag = None
            kind = drag[0]
            if kind == "new_box":
                x1, y1, x2, y2 = (min(self._tl_start.x(), sp.x()),
                                  min(self._tl_start.y(), sp.y()),
                                  max(self._tl_start.x(), sp.x()),
                                  max(self._tl_start.y(), sp.y()))
                self._clear_previews()
                if x2 - x1 >= _MIN_BOX and y2 - y1 >= _MIN_BOX:
                    self.box_requested.emit(int(round(x1)), int(round(y1)),
                                            int(round(x2)), int(round(y2)))
                else:
                    self.message.emit("框太小，未创建（宽高需 ≥ 5px）")
                return True
            if kind == "new_circle":
                idx = drag[1]
                b = self._tl_boxes[idx]
                cx = max(b["x1"], min(self._tl_start.x(), b["x2"]))
                cy = max(b["y1"], min(self._tl_start.y(), b["y2"]))
                r = ((sp.x() - cx) ** 2 + (sp.y() - cy) ** 2) ** 0.5
                r = min(r, min(cx - b["x1"], b["x2"] - cx,
                               cy - b["y1"], b["y2"] - cy))
                self._clear_previews()
                if r >= _MIN_R:
                    self.circle_drawn.emit(idx, int(round(cx)), int(round(cy)),
                                           int(round(r)))
                else:
                    self.message.emit("圆太小，未应用（半径需 ≥ 3px）")
                return True
            self._tl_start = None
            self._tl_orig_box = None
            self._tl_orig_circle = None
            self._rebuild_items()
            self.layout_changed.emit()
            return True

else:  # pragma: no cover - 无 Qt 环境
    class TargetLayoutImageView:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")
