# -*- coding: utf-8 -*-
"""交互式标注图像组件 (E19) 的离线 Qt 用例。

对齐旧工程 `Ganta_Crtl_System/vision_monitor/ui/image_display_widget.py` 的**坐标约定**：
场景坐标 = 图像像素坐标（sceneRect = 图像尺寸，位图放 (0,0)，1:1）。本组用例专门盯住
这条约定 —— 鼠标事件经 `view.viewport()` 过滤，坐标必须原样落到**图像像素**上，
因为下游的深度分割/EdgeSAM 都是按原图像素算的（错位就会把提示点打到背景上）。

用合成的 `QMouseEvent`/`QKeyEvent` 走组件真正的事件入口（`eventFilter`/`keyPressEvent`），
不直接调私有方法，避免"测试通过但真实点击不工作"。无 PySide6 时整模块跳过。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402

from application.presentation.components.annot_image_view import AnnotImageView  # noqa: E402

W, H = 320, 240


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication（中途销毁会崩）。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def iv(qapp):
    """已显示一帧合成彩色图的组件（视口已布局，mapFromScene 才有意义）。"""
    view = AnnotImageView()
    view.resize(400, 300)
    img = np.zeros((H, W, 3), dtype=np.uint8)
    img[70:170, 90:230] = (200, 40, 40)
    assert view.show_array(img, kind="color") is True
    assert view.image_size.width() == W and view.image_size.height() == H
    view.show()
    qapp.processEvents()               # 布局 -> 视口尺寸确定
    yield view
    view.close()
    qapp.processEvents()


def _vp(iv, x, y):
    """图像像素 -> 视口坐标（组件事件入口用的就是视口坐标）。"""
    return iv.view.mapFromScene(QPointF(float(x), float(y)))


def _scene(iv, vp):
    """视口坐标 -> 场景(图像像素)坐标；组件内部就是这么反算的。"""
    return iv.view.mapToScene(vp)


def _press(iv, x, y, button=Qt.MouseButton.LeftButton):
    vp = _vp(iv, x, y)
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(vp), QPointF(vp),
                     button, button, Qt.KeyboardModifier.NoModifier)
    iv.eventFilter(iv.view.viewport(), ev)


def _move(iv, x, y, buttons=Qt.MouseButton.RightButton):
    vp = _vp(iv, x, y)
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(vp), QPointF(vp),
                     Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier)
    iv.eventFilter(iv.view.viewport(), ev)


def _release(iv, x, y, button):
    vp = _vp(iv, x, y)
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(vp), QPointF(vp),
                     button, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    iv.eventFilter(iv.view.viewport(), ev)


def _round_scene(iv, x, y):
    sp = _scene(iv, _vp(iv, x, y))
    return [int(round(sp.x())), int(round(sp.y()))]


def test_polygon_draw_and_close_emits_image_pixel_coords(iv):
    """左键逐个加点、右键闭合：顶点必须是**图像像素**（±1，仅取整误差）。"""
    got = []
    iv.polygon_drawn.connect(lambda pts: got.append(pts))
    iv.set_annotation_mode("polygon")
    want = [(100, 80), (220, 80), (220, 160), (100, 160)]
    for x, y in want:
        _press(iv, x, y)
    assert iv.annotation_mode() == "polygon"
    _press(iv, 0, 0, Qt.MouseButton.RightButton)     # 右键闭合
    assert len(got) == 1 and len(got[0]) == 4
    for (ex, ey), (x, y) in zip(got[0], want):
        # 视口坐标是整数 -> 反算回场景有 ≤1px 取整误差，这里直接按同一路径算期望值
        wx, wy = _round_scene(iv, x, y)
        assert abs(ex - wx) <= 1 and abs(ey - wy) <= 1, (got[0], want)
    # 坐标始终夹在图像内
    assert all(0 <= ex < W and 0 <= ey < H for ex, ey in got[0])


def test_polygon_needs_at_least_three_points(iv):
    """不足 3 点闭合 -> 不发 polygon_drawn（下游 run_scheme 也要求 ≥3）。"""
    got = []
    iv.polygon_drawn.connect(lambda pts: got.append(pts))
    iv.set_annotation_mode("polygon")
    _press(iv, 100, 80)
    _press(iv, 220, 80)
    _press(iv, 0, 0, Qt.MouseButton.RightButton)
    assert got == []
    assert iv.annotation_mode() == "polygon"      # 仍在绘制态，可继续加点


def test_polygon_draft_clamped_inside_image(iv):
    """超出图像的点击被夹到边界内（坐标是原图坐标，绝不能出界）。"""
    got = []
    iv.polygon_drawn.connect(lambda pts: got.append(pts))
    iv.set_annotation_mode("polygon")
    # 故意点到图像外（负坐标 / 超过宽高）
    for x, y in ((-50, -50), (W + 50, -50), (W + 50, H + 50), (-50, H + 50)):
        _press(iv, x, y)
    _press(iv, 0, 0, Qt.MouseButton.RightButton)
    assert len(got) == 1
    assert all(0 <= ex < W and 0 <= ey < H for ex, ey in got[0])


def test_roi_right_drag_emits_rect(iv):
    """ROI：右键拖拽画矩形，信号给的是图像像素的 (x1,y1,x2,y2)。"""
    got = []
    iv.roi_drawn.connect(lambda *a: got.append(tuple(a)))
    iv.set_annotation_mode("roi")
    _press(iv, 60, 40, Qt.MouseButton.RightButton)
    _move(iv, 200, 180)                                # 超过阈值 -> 进入拖拽
    _release(iv, 200, 180, Qt.MouseButton.RightButton)
    assert len(got) == 1
    x1, y1, x2, y2 = got[0]
    ex1, ey1 = _round_scene(iv, 60, 40)
    ex2, ey2 = _round_scene(iv, 200, 180)
    assert abs(x1 - ex1) <= 2 and abs(y1 - ey1) <= 2
    assert abs(x2 - ex2) <= 2 and abs(y2 - ey2) <= 2
    assert x2 > x1 and y2 > y1


def test_roi_left_drag_emits_rect(iv):
    """ROI：**左键**拖拽也能画矩形（比右键顺手；二者等价，都是图像像素坐标）。

    旧工程只有右键拖拽，用户反馈"画不了"——补上左键这条入口后，画框不再只靠右键。
    """
    got = []
    iv.roi_drawn.connect(lambda *a: got.append(tuple(a)))
    iv.set_annotation_mode("roi")
    _press(iv, 60, 40, Qt.MouseButton.LeftButton)
    _move(iv, 200, 180, buttons=Qt.MouseButton.LeftButton)      # 超过阈值 -> 进入拖拽
    _release(iv, 200, 180, Qt.MouseButton.LeftButton)
    assert len(got) == 1
    x1, y1, x2, y2 = got[0]
    ex1, ey1 = _round_scene(iv, 60, 40)
    ex2, ey2 = _round_scene(iv, 200, 180)
    assert abs(x1 - ex1) <= 2 and abs(y1 - ey1) <= 2
    assert abs(x2 - ex2) <= 2 and abs(y2 - ey2) <= 2
    assert x2 > x1 and y2 > y1


def test_roi_mode_left_click_without_drag_draws_nothing(iv):
    """ROI 模式下**左键单击**（没拖动）不画框 —— 别误触出一个零面积 ROI。"""
    got = []
    iv.roi_drawn.connect(lambda *a: got.append(tuple(a)))
    iv.set_annotation_mode("roi")
    _press(iv, 120, 100, Qt.MouseButton.LeftButton)
    _release(iv, 120, 100, Qt.MouseButton.LeftButton)
    assert got == []


def test_default_mode_left_drag_is_pan_not_roi(iv):
    """没进任何绘制模式时，左键拖拽仍是**平移**，不会冒出一个 ROI。"""
    got = []
    iv.roi_drawn.connect(lambda *a: got.append(tuple(a)))
    _press(iv, 60, 40, Qt.MouseButton.LeftButton)
    _move(iv, 200, 180, buttons=Qt.MouseButton.LeftButton)
    _release(iv, 200, 180, Qt.MouseButton.LeftButton)
    assert got == []


def test_edit_mode_drag_vertex_emits_polygon_edited(iv):
    """调整模式：拖动一个顶点 -> polygon_edited，且只动被拖的那个顶点。"""
    pts = [[100, 80], [220, 80], [220, 160], [100, 160]]
    iv.set_polygon_overlay(pts)
    got = []
    iv.polygon_edited.connect(lambda p: got.append(p))
    iv.set_edit_mode(True)
    vx, vy = _round_scene(iv, 100, 80)                 # 左上顶点
    _press(iv, 100, 80)                                # 命中顶点 -> 选中多边形
    _move(iv, 140, 60, buttons=Qt.MouseButton.LeftButton)
    _release(iv, 140, 60, Qt.MouseButton.LeftButton)
    assert got, "拖动顶点应发出 polygon_edited"
    after = got[-1]
    mov_x, mov_y = _round_scene(iv, 140, 60)
    assert abs(after[0][0] - mov_x) <= 3 and abs(after[0][1] - mov_y) <= 3
    # 其余顶点保持原位（未误伤）
    for k in (1, 2, 3):
        assert abs(after[k][0] - pts[k][0]) <= 1 and abs(after[k][1] - pts[k][1]) <= 1


def test_edit_mode_delete_removes_selected_polygon(iv):
    """调整模式选中多边形后按 Delete -> polygon_deleted，叠加被清空。"""
    pts = [[100, 80], [220, 80], [220, 160], [100, 160]]
    iv.set_polygon_overlay(pts)
    got = []
    iv.polygon_deleted.connect(lambda: got.append(True))
    iv.set_edit_mode(True)
    _press(iv, 160, 120)                               # 点多边形内部 -> 选中
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                   Qt.KeyboardModifier.NoModifier)
    iv.keyPressEvent(ev)
    assert got == [True]


def test_overlays_set_and_clear_roundtrip(iv):
    """四种叠加可设可清，且不清到别人头上（轮廓/交集/提示点各自独立）。"""
    iv.set_contours_overlay([np.array([[10, 10], [50, 10], [50, 50]])])
    iv.set_mark_overlay([np.array([[20, 20], [40, 20], [40, 40]])])
    iv.set_points_overlay([(30, 30), (35, 35)])
    iv.set_polygon_overlay([[100, 80], [220, 80], [220, 160]])
    iv.set_roi_overlay(60, 40, 200, 180)
    iv.clear_contours_overlay()
    iv.clear_mark_overlay()
    iv.clear_points_overlay()
    iv.clear_polygon_overlay()
    iv.clear_roi_overlay()
    # 清空后模式仍在，可继续绘制
    iv.set_annotation_mode("polygon")
    assert iv.annotation_mode() == "polygon"


def test_show_array_display_only_does_not_touch_frame(iv):
    """组件只做显示/交互：换一帧不改图像尺寸契约（下游据此把坐标映射回原图）。"""
    assert iv.image_size == iv.image_size.__class__(W, H)
    img2 = np.full((H, W, 3), 7, dtype=np.uint8)
    assert iv.show_array(img2, kind="color") is True
    assert iv.image_size.width() == W and iv.image_size.height() == H


# ---------------------------------------------------------- 可拖动检测圆 (DEC-042)
def test_circle_overlay_drag_moves_center_and_emits_pixel_coords(iv):
    """圆内拖动 -> 圆心跟手；松开发 circle_edited（图像像素坐标，半径不变）。"""
    got = []
    iv.circle_edited.connect(lambda cx, cy, r: got.append((cx, cy, r)))
    iv.set_circle_edit_mode(True)
    iv.set_circle_overlay(160, 120, 40)
    _press(iv, 160, 120)
    _move(iv, 180, 130, buttons=Qt.MouseButton.LeftButton)
    _release(iv, 180, 130, Qt.MouseButton.LeftButton)
    assert len(got) == 1
    cx, cy, r = got[0]
    ex, ey = _round_scene(iv, 180, 130)
    assert abs(cx - ex) <= 1 and abs(cy - ey) <= 1
    assert abs(r - 40) <= 1
    assert iv.circle_overlay()[0] == pytest.approx(ex, abs=1.5)
    assert iv.circle_overlay()[1] == pytest.approx(ey, abs=1.5)


def test_circle_handle_drag_resizes_radius(iv):
    """拖圆右侧白色手柄 -> 半径随鼠标距离变化；松开发 circle_edited。"""
    got = []
    iv.circle_edited.connect(lambda cx, cy, r: got.append((cx, cy, r)))
    iv.set_circle_edit_mode(True)
    iv.set_circle_overlay(160, 120, 40)
    _press(iv, 200, 120)                       # 手柄位置 (cx + r, cy)
    _move(iv, 230, 120, buttons=Qt.MouseButton.LeftButton)
    _release(iv, 230, 120, Qt.MouseButton.LeftButton)
    assert len(got) == 1
    cx, cy, r = got[0]
    assert abs(cx - 160) <= 1 and abs(cy - 120) <= 1
    assert 66 <= r <= 72                        # 新半径 ≈ 70px
    assert iv.circle_overlay()[2] == pytest.approx(r, abs=1)


def test_circle_edit_disabled_does_not_capture_click(iv):
    """未开启圆编辑（普通查看态）时点击圆附近不会发 circle_edited。"""
    got = []
    iv.circle_edited.connect(lambda cx, cy, r: got.append((cx, cy, r)))
    iv.set_circle_overlay(160, 120, 40)
    _press(iv, 160, 120)
    _move(iv, 180, 130, buttons=Qt.MouseButton.LeftButton)
    _release(iv, 180, 130, Qt.MouseButton.LeftButton)
    assert got == []
