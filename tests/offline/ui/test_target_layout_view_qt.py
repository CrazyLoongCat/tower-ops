# -*- coding: utf-8 -*-
"""作业台多框/多圆标注组件（DEC-048，离线 Qt 用例）。

用合成鼠标/键盘事件走组件真正的事件入口，盯住：
- 画框：框外拖拽 → ``box_requested``，坐标按**原图像素**（±1 取整）；
- 画圆：只在已有框内拖拽 → ``circle_drawn``；框外给提示不发信号；
- 调整：拖圆心移动（按圆半径夹在框内，不锁死在框中心）、拖半径手柄改半径、
  拖框移动（圆随动）、拖框边改大小（圆夹在框内）；
- 删除：选中圆 Delete 只删圆、选中框 Delete 删整框；刷新布局不改变用户选择的
  框/圆粒度；
- 与旧单圆接口兼容：``circle_overlay`` / ``circle_edit_mode``。

无 PySide6 时整模块跳过。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402

from application.presentation.components.target_layout_view import (  # noqa: E402
    TargetLayoutImageView,
)

W, H = 320, 240

BOXES = [
    {"x1": 100, "y1": 80, "x2": 220, "y2": 200,
     "circle": {"cx": 160, "cy": 140, "radius_px": 40}},
    {"x1": 240, "y1": 20, "x2": 300, "y2": 80, "circle": None},
]


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def iv(qapp):
    view = TargetLayoutImageView()
    view.resize(400, 300)
    img = np.zeros((H, W, 3), dtype=np.uint8)
    assert view.show_array(img, kind="color") is True
    view.set_layout(BOXES, 0)
    view.show()
    qapp.processEvents()
    # 合成事件按视口像素量化；把缩放还原成 1:1，避免 fit 缩放吃掉精度
    view.view.resetTransform()
    qapp.processEvents()
    yield view
    view.close()
    qapp.processEvents()


def _vp(iv, x, y):
    return iv.view.mapFromScene(QPointF(float(x), float(y)))


def _press(iv, x, y, button=Qt.MouseButton.LeftButton):
    vp = _vp(iv, x, y)
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(vp), QPointF(vp),
                     button, button, Qt.KeyboardModifier.NoModifier)
    iv.eventFilter(iv.view.viewport(), ev)


def _move(iv, x, y, buttons=Qt.MouseButton.LeftButton):
    vp = _vp(iv, x, y)
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(vp), QPointF(vp),
                     Qt.MouseButton.NoButton, buttons, Qt.KeyboardModifier.NoModifier)
    iv.eventFilter(iv.view.viewport(), ev)


def _release(iv, x, y, button=Qt.MouseButton.LeftButton):
    vp = _vp(iv, x, y)
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(vp), QPointF(vp),
                     button, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    iv.eventFilter(iv.view.viewport(), ev)


def _round_scene(iv, x, y):
    sp = iv.view.mapToScene(_vp(iv, x, y))
    return int(round(sp.x())), int(round(sp.y()))


def _drag(iv, start, end, button=Qt.MouseButton.LeftButton):
    _press(iv, *start, button=button)
    _move(iv, *end)
    _release(iv, *end, button=button)


# ---------------------------------------------------------------- 画框/画圆
def test_draw_box_emits_image_pixel_rect(iv):
    got = []
    iv.box_requested.connect(lambda *a: got.append(a))
    iv.set_target_mode("box")
    _drag(iv, (30, 30), (90, 90))
    assert len(got) == 1
    x1, y1 = _round_scene(iv, 30, 30)
    x2, y2 = _round_scene(iv, 90, 90)
    assert got[0] == (x1, y1, x2, y2)
    assert iv.target_mode() == "box"              # 可连续画多个框


def test_tiny_box_is_rejected_with_message(iv):
    got, msgs = [], []
    iv.box_requested.connect(lambda *a: got.append(a))
    iv.message.connect(msgs.append)
    iv.set_target_mode("box")
    _drag(iv, (30, 30), (32, 31))
    assert got == [] and any("太小" in m for m in msgs)


def test_draw_circle_inside_existing_box(iv):
    got = []
    iv.circle_drawn.connect(lambda *a: got.append(a))
    iv.set_target_mode("circle")
    _drag(iv, (160, 140), (198, 140))
    assert len(got) == 1
    idx, cx, cy, r = got[0]
    assert idx == 0
    assert cx == pytest.approx(160, abs=2)
    assert cy == pytest.approx(140, abs=2)
    assert r == pytest.approx(38, abs=2)


def test_draw_circle_outside_boxes_asks_for_box_first(iv):
    got, msgs = [], []
    iv.circle_drawn.connect(lambda *a: got.append(a))
    iv.message.connect(msgs.append)
    iv.set_target_mode("circle")
    _drag(iv, (40, 40), (70, 40))
    assert got == []
    assert any("框内" in m and "画框" in m for m in msgs)


def test_escape_exits_draw_mode(iv):
    iv.set_target_mode("box")
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                   Qt.KeyboardModifier.NoModifier)
    iv.keyPressEvent(ev)
    assert iv.target_mode() == ""


# ---------------------------------------------------------------- 调整
def test_drag_circle_moves_center_not_locked_to_box_center(iv):
    changed = []
    iv.layout_changed.connect(lambda: changed.append(True))
    iv.set_target_edit_mode(True)
    iv.set_target_mode(None)
    _drag(iv, (160, 140), (180, 150))
    assert changed
    c = iv.layout()["boxes"][0]["circle"]
    assert c["cx"] == pytest.approx(180, abs=2)
    assert c["cy"] == pytest.approx(150, abs=2)
    assert c["radius_px"] == pytest.approx(40, abs=2)


def test_drag_radius_handle_resizes_circle(iv):
    changed = []
    iv.layout_changed.connect(lambda: changed.append(True))
    iv.set_target_edit_mode(True)
    iv.set_target_mode(None)
    # 半径手柄在 (cx + r, cy) = (200, 140)
    _drag(iv, (200, 140), (218, 140))
    assert changed
    c = iv.layout()["boxes"][0]["circle"]
    assert c["radius_px"] == pytest.approx(58, abs=3)
    assert c["radius_px"] <= 60 + 1e-6           # 夹在框内（半边长 60）


def test_drag_box_moves_circle_with_it(iv):
    changed = []
    iv.layout_changed.connect(lambda: changed.append(True))
    iv.set_target_edit_mode(True)
    iv.set_target_mode(None)
    _drag(iv, (120, 95), (130, 100))             # 框内、圆外、非边上
    assert changed
    box = iv.layout()["boxes"][0]
    c = box["circle"]
    assert box["x1"] == pytest.approx(110, abs=2)
    assert box["y1"] == pytest.approx(85, abs=2)
    assert c["cx"] == pytest.approx(170, abs=2)
    assert c["cy"] == pytest.approx(145, abs=2)


def test_drag_box_edge_resizes_and_clamps_circle(iv):
    changed = []
    iv.layout_changed.connect(lambda: changed.append(True))
    iv.set_target_edit_mode(True)
    iv.set_target_mode(None)
    _drag(iv, (220, 140), (240, 140))            # 右边中段
    assert changed
    box = iv.layout()["boxes"][0]
    assert box["x2"] == pytest.approx(240, abs=2)
    c = box["circle"]
    assert c["cx"] + c["radius_px"] <= box["x2"] + 1e-6


# ---------------------------------------------------------------- 选中/删除
def _select_circle(iv):
    _drag(iv, (160, 140), (160, 140))            # 点一下圆心（无位移）
    assert iv.selected() == {"kind": "circle", "index": 0}


def _select_box(iv):
    _drag(iv, (120, 95), (120, 95))              # 框内、圆外、非边上
    assert iv.selected() == {"kind": "box", "index": 0}


def test_delete_selected_circle_keeps_box(iv):
    changed = []
    iv.layout_changed.connect(lambda: changed.append(True))
    _select_circle(iv)
    assert iv.delete_selected() is True
    layout = iv.layout()
    assert len(layout["boxes"]) == 2               # 只删圆，框还在
    assert layout["boxes"][0]["circle"] is None
    assert changed


def test_delete_selected_box_removes_box_and_circle(iv):
    changed = []
    iv.layout_changed.connect(lambda: changed.append(True))
    _select_box(iv)
    assert iv.delete_selected() is True
    layout = iv.layout()
    assert len(layout["boxes"]) == 1
    assert layout["boxes"][0]["x1"] == 240
    assert changed


def test_delete_key_removes_selection(iv):
    _select_circle(iv)
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                   Qt.KeyboardModifier.NoModifier)
    iv.keyPressEvent(ev)
    assert iv.layout()["boxes"][0]["circle"] is None


def test_delete_without_selection_only_hints(iv):
    iv.set_layout([], None)                      # 空布局 → 无选中项
    msgs = []
    iv.message.connect(msgs.append)
    assert iv.delete_selected() is False
    assert any("选中" in m for m in msgs)


def test_refresh_layout_keeps_selected_kind(iv):
    _select_circle(iv)
    moved = [dict(box) for box in BOXES]
    moved[0] = dict(moved[0], circle={"cx": 162, "cy": 142, "radius_px": 38})
    iv.set_layout(moved, 0)
    assert iv.selected()["kind"] == "circle"     # 5Hz 刷新不把"选圆"漂成"选框"
    # 圆被删掉的刷新退化为选框
    no_circle = [dict(BOXES[0], circle=None), BOXES[1]]
    iv.set_layout(no_circle, 0)
    assert iv.selected()["kind"] == "box"


# ---------------------------------------------------------------- 兼容/模式
def test_legacy_single_circle_interface(iv):
    assert iv.circle_edit_mode() is True
    assert iv.circle_overlay() == (160, 140, 40)
    iv.set_layout(BOXES, 1)                      # 选中无圆的框 2
    assert iv.circle_overlay() is None


def test_target_mode_is_exclusive_with_roi_annotation(iv):
    iv.set_annotation_mode("polygon")
    iv.set_target_mode("box")
    assert iv.annotation_mode() is None
    assert iv.target_mode() == "box"
    iv.set_target_mode(None)
    assert iv.target_mode() == ""


def test_clear_target_layout_disables_edit_and_overlays(iv):
    iv.clear_target_layout()
    assert iv.layout() == {"boxes": [], "selected": None}
    assert iv.circle_edit_mode() is False
    assert iv.circle_overlay() is None
