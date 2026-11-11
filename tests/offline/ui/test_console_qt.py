"""操作台与监控离线验证：只使用 Fake、临时配置与 offscreen Qt。"""
from __future__ import annotations

import os
from concurrent.futures import Future

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFileDialog
from application.bootstrap.composition import build_offline_stack
from application.presentation.app import MainWindow, UiContext
from application.presentation.viewmodel import WorkbenchViewModel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def console(tmp_path, app):
    from application.services.device_config import CameraEntry, DeviceConfig, DeviceConfigStore
    path = str(tmp_path / "devices.yaml")
    store = DeviceConfigStore(path, seed_if_missing=False)
    store._config = DeviceConfig(cameras=[
        CameraEntry("ds86_01", "DS86 随臂相机", enabled=True, confirmed=True),
        CameraEntry("d405_01", "D405 相机", "zmq", enabled=True, confirmed=True,
                    host="127.0.0.1", port=5555, channel_id="cam1"),
    ])
    store.save()
    stack = build_offline_stack(db_path=str(tmp_path / "records.db"),
                                runtime_dir=str(tmp_path),
                                recipe_path="configs/recipes/ds86_two_station_example.yaml",
                                device_config_path=path)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records, allow_offline_draft=True)
    window = MainWindow(UiContext(stack, vm))
    window.resize(1440, 1024)
    window.pages["workbench"]._autotick = False
    window._select("console")
    try:
        yield window.pages["console"], window, stack, vm
    finally:
        window.shutdown()
        window.close()
        window.deleteLater()
        app.processEvents()
        stack.close()


def test_console_controls_share_task_and_pending_confirmation(console):
    page, window, stack, vm = console
    assert page.recipe_combo.currentData() == vm.selected_recipe
    assert page.progress.value() == 0
    page.rail.buttons["start"].click()
    assert vm.is_task_active()
    assert not page.recipe_combo.isEnabled()
    assert not page.manual_buttons["tower_width"].isEnabled()
    assert page.rail.buttons["request_pause"].isEnabled()
    # 不依赖页面导航；同一 run 的人工确认仍由作业台弹窗承接。
    for _ in range(50):
        vm.tick()
        page.refresh()
        if vm.pending_step_prompt():
            break
    assert vm.pending_step_prompt()
    assert window.pages["workbench"]._step_dialog.isVisible()
    assert "待人工确认" in page.step_detail.text()
    page.rail.buttons["stop_all"].click()
    assert vm.state().task_state in ("STOPPING", "STOPPED")


def test_console_shortcuts_and_configuration_status(console):
    page, window, stack, vm = console
    assert set(page.camera_buttons) == {"ds86_01", "d405_01"}
    # DEC-148：手工操作为三个弹窗——机械臂位置调整内嵌「手动调整」页签
    page.manual_buttons["motion_adjust"].click()
    dlg = page._motion_adjust_dlg
    assert dlg is not None and dlg.page.tabs.currentIndex() == 2
    dlg.close()
    window._select("console")
    page.manual_buttons["tower_width"].click()
    assert page._calc_dlgs.get("tower_width") is not None
    page._calc_dlgs["tower_width"].close()
    stack.device_config.config.cameras[0].enabled = False
    page.refresh()
    assert "ds86_01" not in page.camera_buttons
    assert any("未启用" in pill.text() for pill in page.device_pills)


def test_monitor_layout_snapshot_save_and_close(console, tmp_path, monkeypatch):
    page, _window, stack, _vm = console
    page.open_monitor("ds86_01")
    monitor = page.monitor
    monitor._timer.stop()
    monitor._future.result(timeout=5)
    monitor.refresh()
    tile = monitor.tiles[0]
    assert tile.view.available
    assert "离线合成" in tile.caption.text()
    for count in (2, 4, 1):
        monitor.layout_pick.setCurrentIndex(monitor.layout_pick.findData(count))
        assert len(monitor.visible_tiles()) == count
        assert sum(not t.isHidden() for t in monitor.tiles) == count
    tile.take_snapshot()
    snapshot = tile.snapshot.copy()
    tile.latest_image.fill(0)
    assert tile.snapshot == snapshot
    target = tmp_path / "snapshot.png"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(target), "PNG"))
    tile.save_snapshot()
    from PySide6.QtGui import QImage
    assert QImage(str(target)).convertToFormat(snapshot.format()) == snapshot
    # DEC-122/123：深度随抓拍一起存（原始 npy + 16位 PNG + JSON 元数据 + 伪彩预览）
    assert (tmp_path / "snapshot_depth.npy").exists()
    assert (tmp_path / "snapshot_depth16.png").exists()
    assert (tmp_path / "snapshot_depth.json").exists()
    assert (tmp_path / "snapshot_depth.png").exists()
    tile.close_button.click()
    assert tile.camera_id is None
    assert not tile.view.available
    assert not tile.save_button.isEnabled()
    monitor.close()
    assert not monitor._timer.isActive()
    assert not monitor.isVisible()


def test_monitor_does_not_open_unconnected_camera(console, monkeypatch):
    page, _window, stack, _vm = console
    stack.camera_registry.close("ds86_01")
    monkeypatch.setattr(stack.camera_registry, "open", lambda *_: pytest.fail("不应自动连接"))
    monkeypatch.setattr(stack.camera_registry, "capture", lambda *_: pytest.fail("未连接不应抓拍"))
    page.open_monitor("ds86_01")
    monitor = page.monitor
    monitor._timer.stop()
    assert monitor._future is None
    assert not monitor.tiles[0].view.available
    assert "未启用" in monitor.tiles[0].caption.text()


def test_monitor_uses_task_frames_without_capture_and_rejects_stale_image(console, monkeypatch):
    from application.contracts.frames import CaptureRequest
    page, _window, stack, vm = console
    frames = stack.camera_registry.capture(CaptureRequest(camera_ids=("ds86_01",))).frames
    monkeypatch.setattr(vm, "is_task_active", lambda: True)
    monkeypatch.setattr(vm, "current_frames", lambda: frames)
    monkeypatch.setattr(stack.camera_registry, "capture", lambda *_: pytest.fail("任务运行中不采集"))
    page.open_monitor("ds86_01")
    monitor = page.monitor
    monitor._timer.stop()
    tile = monitor.tiles[0]
    assert tile.view.available
    assert "任务最近帧" in tile.caption.text()
    monkeypatch.setattr(vm, "current_frames", lambda: {})
    monitor.refresh()
    assert not tile.view.available
    assert not tile.capture_button.isEnabled()
    assert "尚未抓拍" in tile.caption.text()


def test_start_waits_for_inflight_monitor_capture(console):
    page, window, stack, vm = console
    page.open_monitor()
    monitor = page.monitor
    monitor._timer.stop()
    future = Future()
    monitor._future = future
    motion_nav = next(b for b in window._group.buttons() if b.property("page_key") == "motion")
    motion_nav.click()
    assert window.stack_widget.currentWidget() is page
    assert next(b for b in window._group.buttons() if b.isChecked()).property("page_key") == "console"
    page.rail.buttons["start"].click()
    assert vm.state().task_state == "IDLE"
    assert monitor._yielding
    assert "正在结束监控采集" in vm.state().hint
    future.set_result(None)
    page.rail.buttons["start"].click()
    assert vm.is_task_active()


def test_device_status_grid_is_five_per_row_with_two_colors(console):
    """DEC-088：设备状态一行 5 个；未启用红/已启用绿；不再有“设备配置”跳转按钮。"""
    from application.presentation.theme import THEME

    page, _window, _stack, _app = console
    assert not hasattr(page, "device_button")
    assert page.device_pills
    for i, pill in enumerate(page.device_pills):
        row, col, _rs, _cs = page.devices_grid.getItemPosition(i)
        assert (row, col) == divmod(i, 5)
        color = THEME.ok if "已启用" in pill.text() else THEME.danger
        assert f"color:{color}" in pill.styleSheet()


def test_execution_card_is_full_width_and_has_no_status_pill(console):
    """DEC-089：当前步骤/下一步左右平铺整行；去掉“作业详情/检测图”与“待检查”。"""
    page, _window, _stack, _app = console
    assert not hasattr(page, "state_pill")
    page.refresh()
    # 下一步文本不再带“下一步：”前缀（标题已单独在右侧）
    assert not page.next_step.text().startswith("下一步：")
    # 两个块在同一行：下一步的 x 在当前步骤块右侧（窗口已显示）
    if page.current_step.isVisible():
        assert page.next_step.pos().x() > page.current_step.pos().x()


def test_execution_rail_full_width_without_caption(console):
    """DEC-093：按钮布局回到整行栏（主组靠左、停止全部靠右）；无“停止结果…”文字。"""
    from PySide6.QtCore import QPoint

    page, window, _stack, _vm = console
    app = QApplication.instance()
    window.show()
    app.processEvents()
    page.refresh()
    app.processEvents()
    rail = page.rail
    assert not hasattr(rail, "caption")
    first = rail.buttons["start"].mapTo(rail, QPoint(0, 0)).x()
    last = rail.buttons["stop_all"]
    last_right = last.mapTo(rail, QPoint(0, 0)).x() + last.width()
    assert first < 100                         # 主操作组靠左
    assert last_right > rail.width() - 120     # 停止全部靠右
    # 动作栏紧跟在步骤信息下方（不是被一大段空白推到底部）
    step_bottom = page.step_detail.mapTo(page, QPoint(0, 0)).y() + page.step_detail.height()
    rail_top = rail.mapTo(page, QPoint(0, 0)).y()
    assert rail_top - step_bottom < 120


def test_execution_steps_aligned_centered_and_single_action_border(console):
    """DEC-092：当前/下一步对齐居中、颜色区分；动作栏是**一个大边框**包住两组。"""
    from PySide6.QtCore import QPoint, Qt
    from application.presentation.theme import THEME

    page, window, _stack, _vm = console
    app = QApplication.instance()
    window.show()
    app.processEvents()
    page.refresh()
    app.processEvents()

    # 版式：同一行、水平居中
    assert page.current_step.alignment() & Qt.AlignmentFlag.AlignHCenter
    assert page.next_step.alignment() & Qt.AlignmentFlag.AlignHCenter
    assert (page.current_step.mapTo(page, QPoint(0, 0)).y()
            == page.next_step.mapTo(page, QPoint(0, 0)).y())
    # 区分靠颜色，不靠字号
    assert "font-size:24px" in page.current_step.styleSheet()
    assert "font-size:24px" in page.next_step.styleSheet()
    assert THEME.deep_teal in page.current_step.styleSheet()
    assert THEME.text_secondary in page.next_step.styleSheet()
    assert page.current_step.styleSheet() != page.next_step.styleSheet()

    # 整行一个白底边框；主组靠左、停止全部靠右
    rail = page.rail
    assert rail.objectName() == "ActionRail"
    assert not hasattr(rail, "box")
    for key in ("start", "terminate", "stop_all"):
        assert rail.buttons[key].parent() is rail
    assert rail.buttons["start"].x() < rail.width() / 2
    last = rail.buttons["stop_all"]
    assert last.x() + last.width() > rail.width() / 2


def test_console_pops_capture_gallery_when_capture_occurs(console):
    """DEC-094：操作台执行到抓拍时，主动弹出与作业台共用的相机通道图像模块。"""
    page, window, _stack, vm = console
    app = QApplication.instance()
    window.show()
    app.processEvents()
    assert page._capture_popup is None
    vm.start_current()
    for _ in range(80):
        vm.tick()
        if vm.current_frames():
            break
        while vm.pending_step_prompt():
            vm.confirm_step("确认", "tester")
            vm.tick()
    page.refresh()
    app.processEvents()
    popup = page._capture_popup
    assert popup is not None and popup.isVisible()
    card = page.workbench.image_card
    assert card.parent() is popup
    assert page.workbench.image_view.available
    assert page.workbench.channel_tabs.count() >= 1
    assert page.workbench.target_pick.isVisible()


# ---------------------------------------------------------------- DEC-110 上一步/当前步/下一步
def test_execution_shows_previous_step_left_of_current(console):
    """上一步 / 当前执行步骤 / 下一步 三列同排：上一步在左、下一步在右。"""
    from PySide6.QtCore import QPoint, Qt
    from application.presentation.theme import THEME

    page, window, _stack, _app = console
    window.show(); QApplication.instance().processEvents()
    page.refresh(); QApplication.instance().processEvents()
    assert page.prev_step.alignment() & Qt.AlignmentFlag.AlignHCenter
    assert "font-size:24px" in page.prev_step.styleSheet()
    assert THEME.text_secondary in page.prev_step.styleSheet()
    if page.current_step.isVisible():
        assert page.prev_step.pos().x() < page.current_step.pos().x()
        assert page.next_step.pos().x() > page.current_step.pos().x()
        assert (page.prev_step.mapTo(page, QPoint(0, 0)).y()
                == page.current_step.mapTo(page, QPoint(0, 0)).y())


def test_execution_previous_current_next_texts(console, monkeypatch):
    """第 2 步执行为当前步时：上一步=第 1 步、下一步=第 3 步。"""
    from types import SimpleNamespace
    from application.presentation.viewmodel import StepView

    page, _window, _stack, vm = console
    page.refresh()
    rid = page.recipe_combo.currentData()
    ids = page.workbench._declared_step_ids(rid)
    assert len(ids) >= 3
    titles = page.workbench._step_titles(rid)
    steps = [StepView("", sid, "SUCCESS" if i == 0 else
                      ("RUNNING" if i == 1 else "PENDING"))
             for i, sid in enumerate(ids)]
    st = SimpleNamespace(recipes=[rid], selected_recipe=rid, steps=steps,
                         recipe_id=rid, task_state="RUNNING", run_id="run-x",
                         buttons={}, blocking_reasons=[], hint="")
    monkeypatch.setattr(vm, "state", lambda *a, **k: st)
    monkeypatch.setattr(vm, "is_task_active", lambda: True)
    monkeypatch.setattr(vm, "pending_step_prompt", lambda: None)
    monkeypatch.setattr(page.workbench, "refresh", lambda: None)
    page.refresh()
    assert page.prev_step.text() == titles.get(ids[0], ids[0])
    assert page.current_step.text() == titles.get(ids[1], ids[1])
    assert page.next_step.text() == titles.get(ids[2], ids[2])
    assert "第 2 /" in page.step_detail.text()

    # 第一步：上一步显示「当前为第一步」
    steps[0].status = "RUNNING"; steps[1].status = "PENDING"
    page.refresh()
    assert page.prev_step.text() == "当前为第一步"
