# -*- coding: utf-8 -*-
"""DEC-048 作业台页面：视觉判定步骤右卡的多框/多圆显示与人工调整（离线 Qt）。

覆盖：
- 待确认视觉步骤：右卡同步节点布局（框/圆数量、定位圆、来源说明），
  「画框 / 画圆 / 删除选中」按钮可用且模式互斥；
- 页面画框（``_on_box_requested``）→ ViewModel/节点新增框并按检测参数找圆；
- 页面画圆（``_on_circle_drawn``）→ 指定框的圆被替换、判定重算；
- 本地几何调整（``_on_layout_changed``）→ 整个布局回灌节点并留证；
- 选择框（``_on_target_selected``）→ 定位圆切换并发布给引导定位；
- 图卡标题显示「框 N/圆 M」，说明栏显示定位圆与「引导定位将重新找圆」提示。

无 PySide6 时整模块跳过；关闭顺序按本工程惯例。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from application.bootstrap.composition import build_offline_stack  # noqa: E402
from application.presentation.viewmodel import WorkbenchViewModel  # noqa: E402

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _vision_recipe(stack, rid="ui_layout_offline"):
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "cap", "type": "camera.capture", "camera_ref": "ds86_01"},
            {"id": "det", "type": "vision.run",
             "input_from": {"step": "cap", "output": "frame"},
             "pipeline_ref": "approved_ds86_circle_pipeline"},
        ],
    })


@pytest.fixture()
def bench(qapp, tmp_path):
    """跑到视觉判定步骤待确认，返回 (stack, vm, page)。"""
    from application.presentation.pages.workbench import WorkbenchPage

    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path), recipe_path=RECIPE)
    _vision_recipe(stack)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.select_recipe("ui_layout_offline")
    page = WorkbenchPage(SimpleNamespace(stack=stack, vm=vm))
    page._autotick = False
    page.resize(1100, 780)
    page.show()
    qapp.processEvents()

    vm.start_current()
    vm.tick()                                          # 抓拍完成
    page.refresh()
    page._on_step_choice("确认")
    vm.tick()                                          # 判定完成
    page.refresh()
    qapp.processEvents()
    yield stack, vm, page
    page.shutdown()
    page.close()
    page.deleteLater()
    qapp.processEvents()
    stack.close()


def _boxes(state):
    return state.get("boxes") or []


# ---------------------------------------------------------------- 显示
def test_layout_is_shown_with_counts_and_buttons(bench, qapp):
    stack, vm, page = bench
    state = vm.step_circle_state()
    boxes = _boxes(state)
    assert state["available"] is True and boxes
    circles = [b for b in boxes if b.get("circle")]
    assert circles

    view = page.image_view
    assert view.layout()["boxes"] == [
        {k: b[k] for k in ("x1", "y1", "x2", "y2", "circle")} for b in boxes]
    assert view.selected()["index"] == state["selected"]

    assert page.draw_box_btn.isEnabled()
    assert page.draw_circle_btn.isEnabled()
    assert page.delete_target_btn.isEnabled()

    caption = page.image_caption.text()
    assert f"框 {len(boxes)}/圆 {len(circles)}" in caption
    status = page.target_status.text()
    assert f"框 {len(boxes)} 个 · 圆 {len(circles)} 个" in status
    assert "定位圆=框" in status                       # 默认选中第一个有圆的框


def test_draw_modes_are_exclusive_and_toggle_off(bench):
    _stack, _vm, page = bench
    page.draw_box_btn.click()
    assert page.image_view.target_mode() == "box"
    assert page.draw_circle_btn.isChecked() is False
    page.draw_circle_btn.click()
    assert page.image_view.target_mode() == "circle"
    assert page.draw_box_btn.isChecked() is False
    page.draw_circle_btn.click()                      # 再点一次退出画圆
    assert page.image_view.target_mode() == ""


def test_unselect_message_tells_guide_will_refind_circle(bench):
    _stack, vm, page = bench
    state = dict(vm.step_circle_state())
    state["selected"] = None                           # 模拟节点未选中定位圆
    page._sync_target_layout(state)
    assert "未选中定位圆" in page.target_status.text()
    assert "引导定位将重新找圆" in page.target_status.text()


# ---------------------------------------------------------------- 画框/画圆
def test_page_adds_box_finds_circle_and_updates_counts(bench, qapp):
    stack, vm, page = bench
    state = vm.step_circle_state()
    n = len(_boxes(state))
    circle = state["circle"]
    cx, cy = int(round(circle["cx"])), int(round(circle["cy"]))
    page._on_box_requested(max(0, cx - 40), max(0, cy - 40),
                           cx + 40, cy + 40)
    qapp.processEvents()
    after = vm.step_circle_state()
    assert len(_boxes(after)) == n + 1
    assert after["selected"] == n                       # 新框（并找到圆）成为定位框
    assert f"框 {n + 1}/圆" in page.image_caption.text()
    assert page.image_view.layout()["boxes"]

    # 新增框后按检测参数找圆，结果与节点一致（离线宿主固定圆）
    assert after["boxes"][n]["circle"] is not None
    target = stack.executor.vision_guide_target
    assert target is not None and target["step_id"] == "det"


def test_page_draw_circle_on_box_recomputes_and_records(bench, qapp):
    stack, vm, page = bench
    state = vm.step_circle_state()
    circle = state["circle"]
    page._on_circle_drawn(0, int(round(circle["cx"])) + 4,
                          int(round(circle["cy"])) + 2,
                          int(round(circle["radius_px"])))
    qapp.processEvents()
    after = vm.step_circle_state()
    assert after["manual"] is True
    c = after["boxes"][0]["circle"]
    assert c["cx"] == pytest.approx(circle["cx"] + 4, abs=1)
    node = stack.task.compiled.find_step("det")
    events = [e for e in stack.records.replay(node.ctx.run_id)
              if e["event_type"] == "vision.manual_layout"]
    assert events, "画圆调整必须留证"
    assert "operator=" in (events[-1]["advice"] or "")


# ---------------------------------------------------------------- 调整整个布局
def test_page_layout_changed_applies_whole_layout(bench, qapp):
    stack, vm, page = bench
    layout = page.image_view.layout()
    moved = [dict(b) for b in layout["boxes"]]
    for box in moved:                                   # 整框平移 +2px
        box["x1"] += 2
        box["x2"] += 2
    page.image_view.set_layout(moved, layout["selected"])
    page._on_layout_changed()
    qapp.processEvents()
    after = vm.step_circle_state()
    assert after["manual"] is True
    assert after["boxes"][0]["x1"] == pytest.approx(moved[0]["x1"], abs=1)
    assert vm.hint_tone != "bad"


def test_page_select_box_moves_guide_circle(bench, qapp):
    stack, vm, page = bench
    state = vm.step_circle_state()
    circle = state["circle"]
    cx, cy = int(round(circle["cx"])), int(round(circle["cy"]))
    if len(_boxes(state)) < 2:                          # 先补一个框，保证有可选对象
        page._on_box_requested(cx, cy, cx + 60, cy + 60)
        qapp.processEvents()
    state = vm.step_circle_state()
    assert len(_boxes(state)) >= 2
    page._on_target_selected("circle", 1)
    qapp.processEvents()
    after = vm.step_circle_state()
    assert after["selected"] == 1
    target = stack.executor.vision_guide_target
    assert target is not None
    assert target["circle"]["cx"] == pytest.approx(after["circle"]["cx"])


# ---------------------------------------------------------------- 定位圆下拉（DEC-054）
def test_target_pick_lists_circles_and_follows_selection(bench):
    _stack, vm, page = bench
    state = vm.step_circle_state()
    circles = [i for i, b in enumerate(_boxes(state)) if b.get("circle")]
    assert circles
    assert "不指定" in page.target_pick.itemText(0)
    assert page.target_pick.count() == 1 + len(circles)
    for row, box_index in enumerate(circles, start=1):
        assert f"框 {box_index + 1} ·" in page.target_pick.itemText(row)
    assert page.target_pick.currentData() == state["selected"]
    assert page.target_pick.isEnabled()


def test_target_pick_operator_selects_circle_and_records_event(bench, qapp):
    stack, vm, page = bench
    state = vm.step_circle_state()
    if len([b for b in _boxes(state) if b.get("circle")]) < 2:
        circle = state["circle"]
        cx, cy = int(round(circle["cx"])), int(round(circle["cy"]))
        page._on_box_requested(cx, cy, cx + 60, cy + 60)
        qapp.processEvents()
    state = vm.step_circle_state()
    circles = [i for i, b in enumerate(_boxes(state)) if b.get("circle")]
    assert len(circles) >= 2
    other = next(i for i in circles if i != state["selected"])
    row = page.target_pick.findData(other)
    assert row > 0
    page.target_pick.setCurrentIndex(row)               # 操作员在下拉里显式选择
    qapp.processEvents()
    after = vm.step_circle_state()
    assert after["selected"] == other
    target = stack.executor.vision_guide_target
    assert target is not None
    assert target["circle"]["cx"] == pytest.approx(after["circle"]["cx"])
    node = stack.task.compiled.find_step("det")
    events = [e for e in stack.records.replay(node.ctx.run_id)
              if e["event_type"] == "vision.select_target"]
    assert events and (events[-1]["advice"] or "").startswith("operator=")
    assert f"定位圆=框 {other + 1}" in page.target_status.text()


def test_target_pick_unspecified_clears_guide_target(bench, qapp):
    stack, vm, page = bench
    state = vm.step_circle_state()
    assert state["selected"] is not None
    page.target_pick.setCurrentIndex(0)                 # 首项 = 不指定
    qapp.processEvents()
    after = vm.step_circle_state()
    assert after["selected"] is None
    assert stack.executor.vision_guide_target is None
    assert "未选中定位圆" in page.target_status.text()
    assert "引导定位将重新找圆" in page.target_status.text()


# ---------------------------------------------------------------- 删除
def test_page_delete_selected_circle_keeps_box(bench, qapp):
    stack, vm, page = bench
    state = vm.step_circle_state()
    n = len(_boxes(state))
    page._on_target_selected("circle", 0)
    page.delete_target_btn.click()
    qapp.processEvents()
    after = vm.step_circle_state()
    assert len(_boxes(after)) == n                       # 框保留
    assert after["boxes"][0]["circle"] is None
    assert "框内未找到圆" not in page.target_status.text()  # 状态栏不误报
    node = stack.task.compiled.find_step("det")
    events = [e for e in stack.records.replay(node.ctx.run_id)
              if e["event_type"] == "vision.manual_layout"]
    assert events


def test_buttons_disabled_outside_vision_step(bench, qapp):
    _stack, vm, page = bench
    page._on_step_choice("确认")                        # 视觉步骤确认后继续
    vm.tick()
    page.refresh()
    qapp.processEvents()
    assert not page.draw_box_btn.isEnabled()
    assert not page.draw_circle_btn.isEnabled()
    assert not page.delete_target_btn.isEnabled()
    assert not page.target_pick.isEnabled()              # DEC-054 定位圆下拉同步禁用
