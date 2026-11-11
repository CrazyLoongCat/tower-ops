# -*- coding: utf-8 -*-
"""交互式标注图像组件（E19 分割/绘制用）。

移植自旧工程 `Ganta_Crtl_System/vision_monitor/ui/image_display_widget.py` 的通道检测
交互（只取本工程用得到的那部分），保留其**坐标约定**：场景坐标 = 图像像素坐标
（sceneRect = 图像尺寸，位图放在 (0,0)，1:1 映射），故所有叠加/信号坐标都是**原图像素**。

提供：
- 显示：:meth:`show_array`（彩色/深度，深度伪彩委托 ``image_view.to_qimage``）；
- 绘制：:meth:`set_annotation_mode` ``'polygon'``（左键加点、右键闭合）/
  ``'roi'``（**左键或右键**拖拽画矩形，左键更顺手、右键沿用旧工程）；
- 调整：:meth:`set_edit_mode`（拖动/缩放 ROI、整体拖动多边形、拖顶点、双击边插点、
  右键删顶点、Delete 删除选中项、Esc 清除选中）；
- 叠加：:meth:`set_roi_overlay` / :meth:`set_polygon_overlay` /
  :meth:`set_contours_overlay` / :meth:`set_mark_overlay` / :meth:`set_points_overlay`。

**只做显示与交互**：组件不改算法、不写配置、不带定时器（视觉页刷新由页面驱动）。
颜色语义沿用旧工程：ROI=黄、用户多边形=品红、分割轮廓=绿、运行一次交集=红、提示点=黄十字。
"""
from __future__ import annotations

from typing import Optional

try:  # pragma: no cover
    from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
    from PySide6.QtGui import (QBrush, QColor, QFont, QPainter, QPainterPath,
                               QPen, QPixmap, QPolygonF)
    from PySide6.QtWidgets import (QGraphicsEllipseItem, QGraphicsItem,
                                   QGraphicsLineItem, QGraphicsPathItem,
                                   QGraphicsPixmapItem, QGraphicsPolygonItem,
                                   QGraphicsRectItem, QGraphicsScene,
                                   QGraphicsTextItem, QGraphicsView,
                                   QVBoxLayout, QWidget)
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

from .image_view import to_qimage

__all__ = ["GUI_REQUIRED", "AnnotImageView"]

# 叠加配色（与旧工程一致）
_ROI_COLOR = (255, 255, 0)          # 黄
_POLY_COLOR = (255, 0, 255)         # 品红
_CONTOUR_COLOR = (0, 255, 0)        # 绿
_MARK_COLOR = (255, 0, 0)           # 红
_POINT_COLOR = (255, 255, 0)        # 黄
_CIRCLE_COLOR = (0, 200, 255)       # 青（可拖动检测圆，DEC-042）

_HANDLE_SIZE = 6      # 手柄命中半径（图像像素）
_HIT_TOL = 6          # 顶点/边命中容差（图像像素）
_MIN_ROI = 5          # ROI 最小宽高
_CLICK_THRESHOLD = 5  # 右键"点击 vs 拖拽"判定阈值（视口像素）


def _point_segment_dist(px, py, a, b) -> float:
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-9:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** 0.5


def _point_in_polygon(px, py, pts) -> bool:
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = float(pts[i][0]), float(pts[i][1])
        xj, yj = float(pts[j][0]), float(pts[j][1])
        if (yi > py) != (yj > py):
            x_cross = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < x_cross:
                inside = not inside
        j = i
    return inside


if GUI_REQUIRED:

    class AnnotImageView(QWidget):
        """可绘制/调整多边形与 ROI 的图像视图，带常驻叠加图层。"""

        polygon_drawn = Signal(list)          # 顶点 [[x, y], ...]（图像像素）
        polygon_edited = Signal(list)         # 编辑后顶点；空=清空
        polygon_deleted = Signal()
        roi_drawn = Signal(int, int, int, int)     # (x1, y1, x2, y2)
        roi_edited = Signal(int, int, int, int)
        roi_deleted = Signal()
        circle_edited = Signal(int, int, int)      # (cx, cy, radius_px)，DEC-042

        _HANDLE_KEYS = ("tl", "tr", "bl", "br", "t", "b", "l", "r")

        def __init__(self, placeholder: str = "尚未抓拍", parent=None) -> None:
            super().__init__(parent)
            self.scene = QGraphicsScene(self)
            self.view = QGraphicsView(self.scene, self)
            self.pixmap_item: Optional[QGraphicsPixmapItem] = None
            self.image_size = None
            self._user_zoomed = False
            self._placeholder = placeholder

            # 交互状态
            self._annotation_mode = None       # None / 'roi' / 'polygon'
            self._edit_mode = False
            self._poly_draft: list[list[int]] = []
            self._poly_draft_items: list = []
            self._panning = False
            self._pan_last = None
            self._draw_press_pos = None        # 画 ROI 拖拽起点（左键/右键，视口坐标）
            self._draw_dragging = False
            self._preview_rect: Optional[QGraphicsRectItem] = None
            self._preview_start = None
            self._edit_drag = None             # ('resize', key)/('move',)/('vertex', i)/('poly_move',)
            self._edit_drag_start = None
            self._edit_orig_geom = None
            self._edit_selection = None        # 'roi' / 'polygon' / None
            self._edit_handles: list = []

            # 常驻叠加
            self._roi_item: Optional[QGraphicsRectItem] = None
            self._poly_item: Optional[QGraphicsPolygonItem] = None
            self._poly_dots: list = []
            self._poly_color = QColor(*_POLY_COLOR)
            self._contour_items: list = []
            self._mark_items: list = []
            self._point_items: list = []
            self._status_item: Optional[QGraphicsTextItem] = None

            # 可拖动检测圆（DEC-042）: 圆心拖动/边缘手柄改半径
            self._circle_item: Optional[QGraphicsEllipseItem] = None
            self._circle_center_item: Optional[QGraphicsEllipseItem] = None
            self._circle_handle_item: Optional[QGraphicsEllipseItem] = None
            self._circle = None                # (cx, cy, r) 图像像素
            self._circle_edit = False
            self._circle_drag = None           # 'circle_move' / 'circle_resize'
            self._circle_drag_start = None
            self._circle_orig = None
            # DEC-119：只旋转显示（图像与叠加一起转，View 变换保证鼠标坐标自动换算回原图）
            self._display_rotation = 0

            self.setFocusPolicy(Qt.StrongFocus)
            self._init_ui()
            self.clear_image()

        # ------------------------------------------------------------------
        def _init_ui(self) -> None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            self.view.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            self.view.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
            self.view.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
            self.view.setStyleSheet("border:1px solid #24323C; border-radius:6px;")
            self.view.viewport().installEventFilter(self)
            self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self.view.setMouseTracking(True)
            self.view.viewport().setMouseTracking(True)
            self.view.setFocusPolicy(Qt.StrongFocus)
            self.scene.setBackgroundBrush(QColor("#0E1A22"))
            layout.addWidget(self.view)

        # ------------------------------------------------------------------
        #  显示旋转（DEC-119）
        # ------------------------------------------------------------------
        def set_display_rotation(self, degrees: int) -> None:
            """顺时针旋转显示画面（0/90/180/270）；场景坐标仍是原图像素。"""
            deg = int(degrees) % 360
            if deg == self._display_rotation:
                return
            self._display_rotation = deg
            if self.pixmap_item is not None:
                self._user_zoomed = False
                self._fit_image()

        def display_rotation(self) -> int:
            return self._display_rotation

        # ------------------------------------------------------------------
        #  显示
        # ------------------------------------------------------------------
        def show_array(self, array, *, kind: str = "color") -> bool:
            if array is None:
                self.clear_image()
                return False
            img = to_qimage(array, kind=kind)
            if img is None:
                self.clear_image()
                return False
            self._set_qimage(img)
            return True

        def _set_qimage(self, qimage) -> None:
            new_size = qimage.size()
            first = self.pixmap_item is None
            size_changed = self.image_size != new_size
            self.image_size = new_size
            pixmap = QPixmap.fromImage(qimage)
            self._clear_status()
            if self.pixmap_item:
                self.pixmap_item.setPixmap(pixmap)
            else:
                self.pixmap_item = QGraphicsPixmapItem(pixmap)
                self.scene.addItem(self.pixmap_item)
            if size_changed or first:
                self.scene.setSceneRect(0, 0, qimage.width(), qimage.height())
            if first:
                self._fit_image()

        def clear_image(self, text: Optional[str] = None) -> None:
            if self.pixmap_item is not None:
                self.scene.removeItem(self.pixmap_item)
                self.pixmap_item = None
            self.image_size = None
            self._user_zoomed = False
            self.view.resetTransform()
            self._set_status(text if text is not None else self._placeholder)

        def set_placeholder(self, text: str) -> None:
            self._placeholder = text
            if self.pixmap_item is None:
                self._set_status(text)

        @property
        def available(self) -> bool:
            return self.pixmap_item is not None

        def _set_status(self, text: str) -> None:
            self._clear_status()
            if not text:
                return
            self._status_item = QGraphicsTextItem(text)
            self._status_item.setFlag(
                QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            self._status_item.setDefaultTextColor(QColor("#93A6B2"))
            self._status_item.setFont(QFont("Microsoft YaHei", 11))
            self.scene.addItem(self._status_item)
            self._fit_status()

        def _clear_status(self) -> None:
            if self._status_item is not None:
                self.scene.removeItem(self._status_item)
                self._status_item = None

        def _fit_status(self) -> None:
            if self._status_item is None:
                return
            r = self.view.viewport().rect()
            if r.isEmpty():
                return
            c = self.view.mapToScene(r.center())
            br = self._status_item.boundingRect()
            self._status_item.setPos(c.x() - br.width() / 2.0, c.y() - br.height() / 2.0)

        def _fit_image(self) -> None:
            if self.pixmap_item is None or self.image_size is None:
                return
            vr = self.view.viewport().rect()
            if vr.isEmpty():
                return
            iw, ih = self.image_size.width(), self.image_size.height()
            if iw <= 0 or ih <= 0:
                return
            if self._display_rotation % 180:      # 90/270：显示宽高互换
                iw, ih = ih, iw
            scale = min(vr.width() / iw, vr.height() / ih)
            self.view.resetTransform()
            if self._display_rotation:
                self.view.rotate(self._display_rotation)   # 顺时针
            self.view.scale(scale, scale)
            self.view.centerOn(self.pixmap_item)

        def resizeEvent(self, event) -> None:  # pragma: no cover - Qt
            super().resizeEvent(event)
            if not self._user_zoomed and self.pixmap_item is not None:
                self._fit_image()
            self._fit_status()

        # ------------------------------------------------------------------
        #  模式
        # ------------------------------------------------------------------
        def set_annotation_mode(self, mode: Optional[str]) -> None:
            if mode not in (None, "roi", "polygon"):
                mode = None
            if mode is not None:
                self._edit_mode = False
                self._circle_edit = False
                self._hide_handles()
                self._edit_drag = None
                self._edit_selection = None
                self._apply_selection_visual()
            self._annotation_mode = mode
            # 切模式时清掉画框拖拽态，避免带着上一模式的起点进来误画
            self._draw_press_pos = None
            self._draw_dragging = False
            if mode != "polygon":
                self._clear_poly_draft()
            if mode is not None:
                self._deselect_all()

        def annotation_mode(self) -> Optional[str]:
            return self._annotation_mode

        def set_edit_mode(self, enabled: bool) -> None:
            enabled = bool(enabled)
            if enabled == self._edit_mode:
                return
            self._edit_mode = enabled
            self._edit_drag = None
            self._edit_orig_geom = None
            self._edit_selection = None
            if enabled:
                self._annotation_mode = None
                self._circle_edit = False
                self._clear_poly_draft()
                self._show_handles()
            else:
                self._hide_handles()
                self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self._apply_selection_visual()

        def edit_mode(self) -> bool:
            return self._edit_mode

        # ------------------------------------------------------------------
        #  可拖动检测圆（DEC-042）
        # ------------------------------------------------------------------
        def set_circle_overlay(self, cx, cy, r, color=_CIRCLE_COLOR) -> None:
            """显示一个可调整的检测圆（图像像素坐标）。"""
            if self._circle_item is None:
                self._circle_item = QGraphicsEllipseItem()
                self._circle_item.setZValue(11)
                self.scene.addItem(self._circle_item)
            self._circle = (float(cx), float(cy), float(r))
            pen = QPen(QColor(int(color[0]), int(color[1]), int(color[2])),
                       4 if self._circle_edit else 2)
            self._circle_item.setPen(pen)
            self._circle_item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            self._circle_item.show()
            self._sync_circle_items()

        def clear_circle_overlay(self) -> None:
            self._circle = None
            self._circle_drag = None
            for item in (self._circle_item, self._circle_center_item,
                         self._circle_handle_item):
                if item is not None:
                    item.hide()

        def circle_overlay(self):
            return self._circle

        def set_circle_edit_mode(self, enabled: bool) -> None:
            """开启后：拖动圆内移动圆心，拖边缘白色手柄改半径；松开即发 circle_edited。"""
            enabled = bool(enabled)
            if enabled == self._circle_edit:
                return
            self._circle_edit = enabled
            self._circle_drag = None
            if enabled:
                self._annotation_mode = None
                self._edit_mode = False
                self._hide_handles()
                self._clear_poly_draft()
                self._edit_selection = None
                self._apply_selection_visual()
                self.view.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
            else:
                self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
            self._sync_circle_items()

        def circle_edit_mode(self) -> bool:
            return self._circle_edit

        def _sync_circle_items(self) -> None:
            if self._circle is None or self._circle_item is None:
                return
            cx, cy, r = self._circle
            self._circle_item.setRect(QRectF(cx - r, cy - r, r * 2, r * 2))
            if not self._circle_edit:
                if self._circle_center_item is not None:
                    self._circle_center_item.hide()
                if self._circle_handle_item is not None:
                    self._circle_handle_item.hide()
                return
            if self._circle_center_item is None:
                self._circle_center_item = QGraphicsEllipseItem(0, 0, 8, 8)
                self._circle_center_item.setBrush(QBrush(QColor(255, 255, 255)))
                self._circle_center_item.setPen(QPen(QColor(*_CIRCLE_COLOR), 1))
                self._circle_center_item.setZValue(12)
                self.scene.addItem(self._circle_center_item)
            if self._circle_handle_item is None:
                self._circle_handle_item = QGraphicsEllipseItem(0, 0, 12, 12)
                self._circle_handle_item.setBrush(QBrush(QColor(255, 255, 255)))
                self._circle_handle_item.setPen(QPen(QColor(*_CIRCLE_COLOR), 1))
                self._circle_handle_item.setZValue(12)
                self.scene.addItem(self._circle_handle_item)
            self._circle_center_item.setRect(cx - 4, cy - 4, 8, 8)
            self._circle_handle_item.setRect(cx + r - 6, cy - 6, 12, 12)
            self._circle_center_item.show()
            self._circle_handle_item.show()

        # ------------------------------------------------------------------
        #  事件
        # ------------------------------------------------------------------
        def eventFilter(self, obj, event):
            if obj is not self.view.viewport():
                return super().eventFilter(obj, event)
            et = event.type()
            if et == QEvent.Type.Wheel:
                self._on_wheel(event)
                return True
            if et == QEvent.Type.ContextMenu:
                return True
            if et == QEvent.Type.MouseButtonDblClick:
                if self._edit_mode and event.button() == Qt.MouseButton.LeftButton:
                    hit = self._hit_test(self.view.mapToScene(event.position().toPoint()))
                    if hit and hit[0] == "edge":
                        pts = self._poly_pts()
                        if pts is not None:
                            sp = self.view.mapToScene(event.position().toPoint())
                            pts.insert(hit[1] + 1, [sp.x(), sp.y()])
                            self.set_polygon_overlay(pts)
                            self.polygon_edited.emit(self._round_pts(pts))
                            return True
                return False
            if et == QEvent.Type.MouseButtonPress:
                self.view.setFocus(Qt.FocusReason.MouseFocusReason)
                return self._on_press(event)
            if et == QEvent.Type.MouseMove:
                return self._on_move(event)
            if et == QEvent.Type.MouseButtonRelease:
                return self._on_release(event)
            return super().eventFilter(obj, event)

        def _on_press(self, event) -> bool:
            if self._circle_edit and event.button() == Qt.MouseButton.LeftButton:
                sp = self.view.mapToScene(event.position().toPoint())
                hit = self._hit_test(sp)
                if hit and hit[0] in ("circle_move", "circle_resize"):
                    self._circle_drag = hit[0]
                    self._circle_drag_start = sp
                    self._circle_orig = self._circle
                    return True
            if self._edit_mode:
                sp = self.view.mapToScene(event.position().toPoint())
                if event.button() == Qt.MouseButton.LeftButton:
                    hit = self._hit_test(sp)
                    if hit and hit[0] == "handle":
                        self._edit_selection = "roi"
                        self._apply_selection_visual()
                        self._edit_drag = ("resize", hit[1])
                        self._edit_drag_start = sp
                        self._edit_orig_geom = self._roi_geom()
                        return True
                    if hit and hit[0] == "vertex":
                        self._edit_selection = "polygon"
                        self._apply_selection_visual()
                        self._edit_drag = ("vertex", hit[1])
                        self._edit_drag_start = sp
                        self._edit_orig_geom = self._poly_pts()[hit[1]]
                        return True
                    if hit and hit[0] == "polygon":
                        self._edit_selection = "polygon"
                        self._apply_selection_visual()
                        self._edit_drag = ("poly_move",)
                        self._edit_drag_start = sp
                        self._edit_orig_geom = self._poly_pts()
                        return True
                    if hit and hit[0] == "edge":
                        self._edit_selection = "polygon"
                        self._apply_selection_visual()
                    elif hit and hit[0] == "roi":
                        self._edit_selection = "roi"
                        self._apply_selection_visual()
                        self._edit_drag = ("move",)
                        self._edit_drag_start = sp
                        self._edit_orig_geom = self._roi_geom()
                        return True
                elif event.button() == Qt.MouseButton.RightButton:
                    hit = self._hit_test(sp)
                    if hit and hit[0] == "vertex":
                        pts = self._poly_pts()
                        pts.pop(hit[1])
                        if len(pts) >= 3:
                            self.set_polygon_overlay(pts)
                            self.polygon_edited.emit(self._round_pts(pts))
                        else:
                            self.clear_polygon_overlay()
                            self.polygon_edited.emit([])
                        return True
            if self._annotation_mode == "polygon":
                if event.button() == Qt.MouseButton.LeftButton:
                    self._add_poly_point(self.view.mapToScene(event.position().toPoint()))
                    return True
                if event.button() == Qt.MouseButton.RightButton:
                    self._finish_polygon()
                    return True
            # 画 ROI 模式：**左键或右键**拖拽都能画（左键更顺手；右键沿用旧工程）。
            # 该模式下左键不再用来平移，避免与画框抢手势。
            if self._annotation_mode == "roi" and event.button() in (
                    Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
                self._draw_press_pos = event.position().toPoint()
                self._draw_dragging = False
                return True
            if event.button() == Qt.MouseButton.RightButton:
                self._draw_press_pos = event.position().toPoint()
                self._draw_dragging = False
                return True
            if event.button() == Qt.MouseButton.LeftButton:
                self._panning = True
                self._pan_last = event.position().toPoint()
                self.view.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                return True
            return False

        def _on_move(self, event) -> bool:
            if self._circle_edit and self._circle_drag is not None:
                self._update_circle_drag(self.view.mapToScene(event.position().toPoint()))
                return True
            if self._edit_mode and self._edit_drag is not None:
                self._update_drag(self.view.mapToScene(event.position().toPoint()))
                return True
            if self._panning and (event.buttons() & Qt.MouseButton.LeftButton):
                delta = event.position().toPoint() - self._pan_last
                self._pan_last = event.position().toPoint()
                self.view.horizontalScrollBar().setValue(
                    self.view.horizontalScrollBar().value() - delta.x())
                self.view.verticalScrollBar().setValue(
                    self.view.verticalScrollBar().value() - delta.y())
                return True
            if self._draw_press_pos is not None and (event.buttons() & (
                    Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton)):
                dx = event.position().toPoint().x() - self._draw_press_pos.x()
                dy = event.position().toPoint().y() - self._draw_press_pos.y()
                draw_active = self._annotation_mode == "roi"
                if not self._draw_dragging and (abs(dx) > _CLICK_THRESHOLD
                                                or abs(dy) > _CLICK_THRESHOLD):
                    self._draw_dragging = True
                    if draw_active:
                        self._start_preview(self._draw_press_pos)
                if self._draw_dragging and draw_active:
                    self._update_preview(self.view.mapToScene(event.position().toPoint()))
                return True
            if self._annotation_mode == "polygon" and self._poly_draft:
                self._update_rubber(self.view.mapToScene(event.position().toPoint()))
                return True
            return False

        def _on_release(self, event) -> bool:
            if event.button() == Qt.MouseButton.LeftButton and self._circle_drag is not None:
                self._finish_circle_drag()
                return True
            if event.button() == Qt.MouseButton.LeftButton and self._edit_drag is not None:
                self._finish_drag()
                return True
            if event.button() == Qt.MouseButton.LeftButton and self._panning:
                self._panning = False
                self._pan_last = None
                self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
                return True
            # 画 ROI：左键/右键松开都收尾（拖拽出矩形才算，单击不画）
            if self._draw_press_pos is not None and event.button() in (
                    Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
                if self._draw_dragging and self._annotation_mode == "roi":
                    self._finish_roi(self.view.mapToScene(event.position().toPoint()))
                self._draw_press_pos = None
                self._draw_dragging = False
                return True
            return False

        def keyPressEvent(self, event) -> None:
            if self._edit_mode and event.key() == Qt.Key.Key_Delete:
                if self._edit_selection == "roi" and self._roi_geom() is not None:
                    self._edit_drag = None
                    self.clear_roi_overlay()
                    self._hide_handles()
                    self._edit_selection = None
                    self._apply_selection_visual()
                    self.roi_deleted.emit()
                    return
                if self._edit_selection == "polygon" and self._poly_pts() is not None:
                    self._edit_drag = None
                    self.clear_polygon_overlay()
                    self._edit_selection = None
                    self._apply_selection_visual()
                    self.polygon_deleted.emit()
                    return
            if self._edit_mode and event.key() == Qt.Key.Key_Escape:
                self._edit_selection = None
                self._apply_selection_visual()
                return
            if self._annotation_mode == "polygon" and event.key() == Qt.Key.Key_Escape:
                self._clear_poly_draft()
                return
            super().keyPressEvent(event)

        # ------------------------------------------------------------------
        #  多边形草稿
        # ------------------------------------------------------------------
        def _add_poly_point(self, sp) -> None:
            x = int(round(sp.x()))
            y = int(round(sp.y()))
            if self.image_size:
                x = max(0, min(x, self.image_size.width() - 1))
                y = max(0, min(y, self.image_size.height() - 1))
            self._poly_draft.append([x, y])
            dot = QGraphicsEllipseItem(x - 3, y - 3, 6, 6)
            dot.setBrush(QBrush(QColor(*_POLY_COLOR)))
            dot.setPen(QPen(Qt.PenStyle.NoPen))
            dot.setZValue(8)
            self.scene.addItem(dot)
            self._poly_draft_items.append(dot)
            if len(self._poly_draft) >= 2:
                px, py = self._poly_draft[-2]
                line = QGraphicsLineItem(px, py, x, y)
                line.setPen(QPen(QColor(*_POLY_COLOR), 2))
                line.setZValue(8)
                self.scene.addItem(line)
                self._poly_draft_items.append(line)

        def _update_rubber(self, sp) -> None:
            items = self._poly_draft_items
            if items and isinstance(items[-1], QGraphicsLineItem) \
                    and getattr(items[-1], "_is_rubber", False):
                rubber = items[-1]
            else:
                rubber = QGraphicsLineItem()
                pen = QPen(QColor(*_POLY_COLOR), 1)
                pen.setStyle(Qt.PenStyle.DashLine)
                rubber.setPen(pen)
                rubber.setZValue(8)
                rubber._is_rubber = True
                self.scene.addItem(rubber)
                items.append(rubber)
            px, py = self._poly_draft[-1]
            rubber.setLine(px, py, sp.x(), sp.y())

        def _finish_polygon(self) -> None:
            pts = [list(p) for p in self._poly_draft]
            self._clear_poly_draft()
            if len(pts) >= 3:
                self.polygon_drawn.emit(pts)

        def _clear_poly_draft(self) -> None:
            for it in self._poly_draft_items:
                try:
                    self.scene.removeItem(it)
                except Exception:  # noqa: BLE001
                    pass
            self._poly_draft_items = []
            self._poly_draft = []

        # ------------------------------------------------------------------
        #  ROI 预览
        # ------------------------------------------------------------------
        def _start_preview(self, viewport_pos) -> None:
            self._clear_preview()
            pen = QPen(QColor(255, 165, 0), 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            self._preview_rect = QGraphicsRectItem()
            self._preview_rect.setPen(pen)
            self.scene.addItem(self._preview_rect)
            self._preview_start = self.view.mapToScene(viewport_pos)

        def _update_preview(self, sp) -> None:
            if self._preview_rect is None or self._preview_start is None:
                return
            x = min(self._preview_start.x(), sp.x())
            y = min(self._preview_start.y(), sp.y())
            w = abs(sp.x() - self._preview_start.x())
            h = abs(sp.y() - self._preview_start.y())
            self._preview_rect.setRect(x, y, w, h)

        def _finish_roi(self, sp) -> None:
            if self._preview_start is None:
                self._clear_preview()
                return
            x1 = min(self._preview_start.x(), sp.x())
            y1 = min(self._preview_start.y(), sp.y())
            x2 = max(self._preview_start.x(), sp.x())
            y2 = max(self._preview_start.y(), sp.y())
            self._clear_preview()
            if self.image_size is None:
                return
            x1 = max(0, min(int(x1), self.image_size.width() - 1))
            y1 = max(0, min(int(y1), self.image_size.height() - 1))
            x2 = max(0, min(int(x2), self.image_size.width() - 1))
            y2 = max(0, min(int(y2), self.image_size.height() - 1))
            if x2 - x1 < 3 or y2 - y1 < 3:
                return
            self.roi_drawn.emit(x1, y1, x2, y2)

        def _clear_preview(self) -> None:
            if self._preview_rect is not None:
                try:
                    self.scene.removeItem(self._preview_rect)
                except Exception:  # noqa: BLE001
                    pass
                self._preview_rect = None
            self._preview_start = None

        # ------------------------------------------------------------------
        #  常驻叠加
        # ------------------------------------------------------------------
        def set_roi_overlay(self, x1, y1, x2, y2) -> None:
            if self._roi_item is None:
                self._roi_item = QGraphicsRectItem()
                self._roi_item.setZValue(9)
                self.scene.addItem(self._roi_item)
            w = 4 if (self._edit_mode and self._edit_selection == "roi") else 2
            self._roi_item.setPen(QPen(QColor(*_ROI_COLOR), w))
            self._roi_item.setRect(QRectF(QPointF(float(x1), float(y1)),
                                          QPointF(float(x2), float(y2))))
            self._roi_item.show()
            if self._edit_mode:
                self._show_handles()

        def clear_roi_overlay(self) -> None:
            if self._roi_item is not None:
                self._roi_item.hide()
            self._hide_handles()

        def set_polygon_overlay(self, pts, color=None) -> None:
            if not pts or len(pts) < 3:
                self.clear_polygon_overlay()
                return
            if color is not None:
                self._poly_color = QColor(color) if not isinstance(color, (tuple, list)) \
                    else QColor(int(color[0]), int(color[1]), int(color[2]))
            if self._poly_item is None:
                self._poly_item = QGraphicsPolygonItem()
                self._poly_item.setZValue(9)
                self.scene.addItem(self._poly_item)
            sel = self._edit_mode and self._edit_selection == "polygon"
            self._poly_item.setPen(QPen(self._poly_color, 4 if sel else 2))
            self._poly_item.setPolygon(
                QPolygonF([QPointF(float(p[0]), float(p[1])) for p in pts]))
            self._poly_item.show()
            self._render_poly_dots(pts, 5 if sel else 3)

        def _render_poly_dots(self, pts, r) -> None:
            for d in self._poly_dots:
                try:
                    self.scene.removeItem(d)
                except Exception:  # noqa: BLE001
                    pass
            self._poly_dots = []
            for p in pts:
                dot = QGraphicsEllipseItem(float(p[0]) - r, float(p[1]) - r, r * 2, r * 2)
                dot.setBrush(QBrush(self._poly_color))
                dot.setPen(QPen(Qt.PenStyle.NoPen))
                dot.setZValue(9)
                self.scene.addItem(dot)
                self._poly_dots.append(dot)

        def clear_polygon_overlay(self) -> None:
            if self._poly_item is not None:
                self._poly_item.hide()
            for d in self._poly_dots:
                try:
                    self.scene.removeItem(d)
                except Exception:  # noqa: BLE001
                    pass
            self._poly_dots = []

        def _make_contours(self, contours, color, width) -> list:
            pen = QPen(QColor(int(color[0]), int(color[1]), int(color[2])), width)
            items = []
            for c in contours:
                pts = []
                for p in c:
                    if hasattr(p, "ravel"):
                        v = p.ravel()
                        if v.size < 2:
                            continue
                        pts.append(QPointF(float(v[0]), float(v[1])))
                    else:
                        pts.append(QPointF(float(p[0]), float(p[1])))
                if len(pts) < 2:
                    continue
                path = QPainterPath()
                path.addPolygon(QPolygonF(pts))
                path.closeSubpath()
                item = QGraphicsPathItem()
                item.setPath(path)
                item.setPen(pen)
                item.setZValue(9)
                self.scene.addItem(item)
                items.append(item)
            return items

        def _remove_items(self, items) -> None:
            for it in items:
                try:
                    self.scene.removeItem(it)
                except Exception:  # noqa: BLE001
                    pass

        def set_contours_overlay(self, contours, color=_CONTOUR_COLOR, width=2) -> None:
            self.clear_contours_overlay()
            self._contour_items = self._make_contours(contours, color, width)

        def clear_contours_overlay(self) -> None:
            self._remove_items(self._contour_items)
            self._contour_items = []

        def set_mark_overlay(self, contours, color=_MARK_COLOR, width=2) -> None:
            self.clear_mark_overlay()
            self._mark_items = self._make_contours(contours, color, width)

        def clear_mark_overlay(self) -> None:
            self._remove_items(self._mark_items)
            self._mark_items = []

        def set_points_overlay(self, points, color=_POINT_COLOR, r=5) -> None:
            """逐点画十字标记。``points`` 元素可为 ``(x, y)`` 或 ``(x, y, (r,g,b))``
            指定该点颜色（DEC-144：棱线测量左右棱用不同颜色）。"""
            self.clear_points_overlay()
            default = QColor(int(color[0]), int(color[1]), int(color[2]))
            for p in points:
                x, y = float(p[0]), float(p[1])
                c = default
                if len(p) >= 3 and p[2] is not None:
                    pc = p[2]
                    c = QColor(int(pc[0]), int(pc[1]), int(pc[2]))
                dot = QGraphicsEllipseItem(x - r, y - r, r * 2, r * 2)
                dot.setBrush(QBrush(c))
                dot.setPen(QPen(Qt.PenStyle.NoPen))
                dot.setZValue(10)
                self.scene.addItem(dot)
                self._point_items.append(dot)
                for x1, y1, x2, y2 in ((x - 2 * r, y, x + 2 * r, y),
                                       (x, y - 2 * r, x, y + 2 * r)):
                    line = QGraphicsLineItem(x1, y1, x2, y2)
                    line.setPen(QPen(c, 1))
                    line.setZValue(10)
                    self.scene.addItem(line)
                    self._point_items.append(line)

        def clear_points_overlay(self) -> None:
            self._remove_items(self._point_items)
            self._point_items = []

        def clear_all_overlays(self) -> None:
            self.clear_roi_overlay()
            self.clear_polygon_overlay()
            self.clear_contours_overlay()
            self.clear_mark_overlay()
            self.clear_points_overlay()
            self.clear_circle_overlay()

        # ------------------------------------------------------------------
        #  编辑态
        # ------------------------------------------------------------------
        _HANDLE_CURSORS = {
            "tl": Qt.CursorShape.SizeFDiagCursor,
            "br": Qt.CursorShape.SizeFDiagCursor,
            "tr": Qt.CursorShape.SizeBDiagCursor,
            "bl": Qt.CursorShape.SizeBDiagCursor,
        }

        def _handle_points(self, x1, y1, x2, y2):
            xm, ym = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            return {"tl": (x1, y1), "tr": (x2, y1), "bl": (x1, y2), "br": (x2, y2),
                    "t": (xm, y1), "b": (xm, y2), "l": (x1, ym), "r": (x2, ym)}

        def _show_handles(self) -> None:
            geom = self._roi_geom()
            if geom is None:
                self._hide_handles()
                return
            if not self._edit_handles:
                for _ in self._HANDLE_KEYS:
                    h = QGraphicsEllipseItem(0, 0, _HANDLE_SIZE * 2, _HANDLE_SIZE * 2)
                    h.setBrush(QBrush(QColor(255, 255, 255)))
                    h.setPen(QPen(QColor(*_ROI_COLOR), 1))
                    h.setZValue(10)
                    self.scene.addItem(h)
                    self._edit_handles.append(h)
            self._sync_handles()
            for h in self._edit_handles:
                h.show()

        def _sync_handles(self) -> None:
            geom = self._roi_geom()
            if geom is None:
                return
            pts = self._handle_points(*geom)
            for h, key in zip(self._edit_handles, self._HANDLE_KEYS):
                hx, hy = pts[key]
                h.setRect(hx - _HANDLE_SIZE, hy - _HANDLE_SIZE,
                          _HANDLE_SIZE * 2, _HANDLE_SIZE * 2)

        def _hide_handles(self) -> None:
            for h in self._edit_handles:
                h.hide()

        def _roi_geom(self):
            if self._roi_item is None or not self._roi_item.isVisible():
                return None
            r = self._roi_item.rect()
            return (r.left(), r.top(), r.right(), r.bottom())

        def _poly_pts(self):
            if self._poly_item is None or not self._poly_item.isVisible():
                return None
            return [[p.x(), p.y()] for p in self._poly_item.polygon()]

        def _apply_selection_visual(self) -> None:
            if self._roi_item is not None:
                w = 4 if (self._edit_mode and self._edit_selection == "roi") else 2
                self._roi_item.setPen(QPen(QColor(*_ROI_COLOR), w))
            if self._poly_item is not None:
                sel = self._edit_mode and self._edit_selection == "polygon"
                self._poly_item.setPen(QPen(self._poly_color, 4 if sel else 2))
                if self._poly_item.isVisible():
                    pts = [[p.x(), p.y()] for p in self._poly_item.polygon()]
                    if pts:
                        self._render_poly_dots(pts, 5 if sel else 3)

        def _hit_test(self, sp):
            x, y = sp.x(), sp.y()
            if self._circle_edit and self._circle is not None:
                cx, cy, r = self._circle
                if (x - (cx + r)) ** 2 + (y - cy) ** 2 <= (_HANDLE_SIZE + 3) ** 2:
                    return ("circle_resize",)
                d2 = (x - cx) ** 2 + (y - cy) ** 2
                if d2 <= max(r, _HIT_TOL) ** 2:
                    return ("circle_move",)
            geom = self._roi_geom()
            if geom is not None and self._edit_handles:
                pts = self._handle_points(*geom)
                tol2 = (_HANDLE_SIZE + 2) ** 2
                for key in self._HANDLE_KEYS:
                    hx, hy = pts[key]
                    if (x - hx) ** 2 + (y - hy) ** 2 <= tol2:
                        return ("handle", key)
            pts = self._poly_pts()
            if pts:
                for i, (px, py) in enumerate(pts):
                    if (x - px) ** 2 + (y - py) ** 2 <= _HIT_TOL ** 2:
                        return ("vertex", i)
                n = len(pts)
                for i in range(n):
                    if _point_segment_dist(x, y, pts[i], pts[(i + 1) % n]) <= _HIT_TOL:
                        return ("edge", i)
                if len(pts) >= 3 and _point_in_polygon(x, y, pts):
                    return ("polygon",)
            if geom is not None:
                x1, y1, x2, y2 = geom
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return ("roi",)
            return None

        def _update_drag(self, sp) -> None:
            mode = self._edit_drag[0]
            if mode == "vertex":
                i = self._edit_drag[1]
                pts = self._poly_pts()
                if pts is None or not (0 <= i < len(pts)):
                    return
                x, y = sp.x(), sp.y()
                if self.image_size:
                    x = max(0.0, min(x, self.image_size.width() - 1))
                    y = max(0.0, min(y, self.image_size.height() - 1))
                geom = self._roi_geom()
                if geom is not None:
                    x = max(geom[0], min(x, geom[2]))
                    y = max(geom[1], min(y, geom[3]))
                pts[i] = [x, y]
                self.set_polygon_overlay(pts)
                return
            if mode == "poly_move":
                orig = self._edit_orig_geom
                if not orig:
                    return
                dx = sp.x() - self._edit_drag_start.x()
                dy = sp.y() - self._edit_drag_start.y()
                xs = [float(p[0]) for p in orig]
                ys = [float(p[1]) for p in orig]
                if self.image_size:
                    dx = max(dx, -min(xs))
                    dx = min(dx, (self.image_size.width() - 1) - max(xs))
                    dy = max(dy, -min(ys))
                    dy = min(dy, (self.image_size.height() - 1) - max(ys))
                geom = self._roi_geom()
                if geom is not None:
                    dx = max(dx, geom[0] - min(xs))
                    dx = min(dx, geom[2] - max(xs))
                    dy = max(dy, geom[1] - min(ys))
                    dy = min(dy, geom[3] - max(ys))
                self.set_polygon_overlay([[p[0] + dx, p[1] + dy] for p in orig])
                return
            # ROI move / resize
            dx = sp.x() - self._edit_drag_start.x()
            dy = sp.y() - self._edit_drag_start.y()
            x1, y1, x2, y2 = self._edit_orig_geom
            W = self.image_size.width() if self.image_size else None
            H = self.image_size.height() if self.image_size else None
            if mode == "move":
                nx1, ny1, nx2, ny2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
                if W is not None:
                    if nx1 < 0:
                        nx2, nx1 = nx2 - nx1, 0.0
                    elif nx2 > W - 1:
                        nx1, nx2 = nx1 - (nx2 - (W - 1)), float(W - 1)
                if H is not None:
                    if ny1 < 0:
                        ny2, ny1 = ny2 - ny1, 0.0
                    elif ny2 > H - 1:
                        ny1, ny2 = ny1 - (ny2 - (H - 1)), float(H - 1)
            else:
                handle = self._edit_drag[1]
                nx1, ny1, nx2, ny2 = x1, y1, x2, y2
                if "l" in handle:
                    nx1 = x1 + dx
                if "r" in handle:
                    nx2 = x2 + dx
                if "t" in handle:
                    ny1 = y1 + dy
                if "b" in handle:
                    ny2 = y2 + dy
                if W is not None:
                    nx1 = max(0.0, nx1)
                    nx2 = min(float(W - 1), nx2)
                if H is not None:
                    ny1 = max(0.0, ny1)
                    ny2 = min(float(H - 1), ny2)
                if nx2 - nx1 < _MIN_ROI:
                    if "l" in handle:
                        nx1 = nx2 - _MIN_ROI
                    else:
                        nx2 = nx1 + _MIN_ROI
                if ny2 - ny1 < _MIN_ROI:
                    if "t" in handle:
                        ny1 = ny2 - _MIN_ROI
                    else:
                        ny2 = ny1 + _MIN_ROI
            self.set_roi_overlay(nx1, ny1, nx2, ny2)
            self._sync_handles()

        def _finish_drag(self) -> None:
            if self._edit_drag is None:
                return
            drag = self._edit_drag
            self._edit_drag = None
            self._edit_drag_start = None
            self._edit_orig_geom = None
            if drag[0] in ("vertex", "poly_move"):
                pts = self._poly_pts()
                if pts is not None:
                    self.polygon_edited.emit(self._round_pts(pts))
            else:
                geom = self._roi_geom()
                if geom is not None:
                    x1, y1, x2, y2 = geom
                    self.roi_edited.emit(int(round(x1)), int(round(y1)),
                                         int(round(x2)), int(round(y2)))

        def _update_circle_drag(self, sp) -> None:
            if self._circle_orig is None:
                return
            cx, cy, r = self._circle_orig
            if self._circle_drag == "circle_resize":
                r = ((sp.x() - cx) ** 2 + (sp.y() - cy) ** 2) ** 0.5
                if self.image_size is not None:
                    r = min(r, float(max(self.image_size.width(),
                                         self.image_size.height())))
                r = max(3.0, r)
            else:
                cx, cy = sp.x(), sp.y()
                if self.image_size is not None:
                    cx = max(0.0, min(cx, self.image_size.width() - 1.0))
                    cy = max(0.0, min(cy, self.image_size.height() - 1.0))
            self.set_circle_overlay(cx, cy, r)

        def _finish_circle_drag(self) -> None:
            self._circle_drag = None
            self._circle_drag_start = None
            self._circle_orig = None
            if self._circle is not None:
                cx, cy, r = self._circle
                self.circle_edited.emit(int(round(cx)), int(round(cy)),
                                        max(1, int(round(r))))

        def _round_pts(self, pts):
            return [[int(round(px)), int(round(py))] for px, py in pts]

        def _deselect_all(self) -> None:
            self._edit_selection = None
            self._apply_selection_visual()

        # ------------------------------------------------------------------
        #  滚轮
        # ------------------------------------------------------------------
        def _on_wheel(self, event) -> None:
            if self.pixmap_item is None:
                return
            delta = event.angleDelta().y()
            if delta == 0:
                return
            factor = 1.2 if delta > 0 else 1 / 1.2
            # DEC-132：显示旋转（DEC-119）后变换矩阵的 m11 旋转到 0，直接用 m11
            # 判断缩放会让滚轮失效；按矩阵的缩放幅度 sqrt(m11^2 + m12^2) 判断。
            t = self.view.transform()
            cur = (t.m11() ** 2 + t.m12() ** 2) ** 0.5
            if cur * factor < 0.05 or cur * factor > 60:
                return
            self._user_zoomed = True
            self.view.scale(factor, factor)

else:  # pragma: no cover - 无 Qt 环境
    class AnnotImageView:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")
