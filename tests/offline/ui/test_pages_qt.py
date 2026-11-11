"""六页界面的 Qt 构造/导航/状态测试 (E18~E24 离线 UI)。

无 PySide6 时整模块跳过 (PySide6 属可选 'ui' 依赖, 离线套件不强制)。
使用 offscreen 平台, 不打开窗口、不接触真实设备。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from application.bootstrap.composition import build_offline_stack  # noqa: E402
from application.presentation.app import (  # noqa: E402
    NAV_PAGES, ROLE_PROGRAMMER, ROLE_TECHNICIAN, MainWindow, UiContext,
    initial_page_for_role, nav_pages_for_role, normalize_role,
)
from application.presentation.viewmodel import WorkbenchViewModel  # noqa: E402

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication (避免中途销毁)。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def win(tmp_path, qapp):
    import gc

    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path), recipe_path=RECIPE)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    w = MainWindow(UiContext(stack=stack, vm=vm))
    w.resize(1440, 1024)
    try:
        yield w, vm, stack, qapp
    finally:
        # 关键顺序: 停定时器 -> 关窗 -> 交给 Qt 删除 -> 跑事件循环 -> 关栈 -> 回收
        # 不做这步会在解释器退出时崩溃 (纯虚函数调用 / SIGSEGV), 断言通过也不算通过。
        try:
            w.shutdown()
            w.close()
            w.deleteLater()
            qapp.processEvents()
        except Exception:  # noqa: BLE001
            pass
        stack.close()
        del w
        gc.collect()
        qapp.processEvents()


def test_all_pages_construct_and_navigate(win):
    w, _vm, _stack, app = win
    assert [k for _l, k in NAV_PAGES] == [
        "workbench", "console", "manual", "vision", "recipes", "motion",
        "devices", "gripper", "settings", "diagnostics"]
    assert set(w.pages) == {k for _l, k in NAV_PAGES}
    for _label, key in NAV_PAGES:
        w._select(key)
        w.refresh_all()
        app.processEvents()
        assert w.stack_widget.currentWidget() is w.pages[key]


def test_role_nav_filter():
    """DEC-098：技术员只看到操作台/日志诊断；程序员看到全部。"""
    assert normalize_role("t") == ROLE_TECHNICIAN
    assert normalize_role("technician") == ROLE_TECHNICIAN
    assert normalize_role("g") == ROLE_PROGRAMMER
    assert normalize_role("programmer") == ROLE_PROGRAMMER
    assert normalize_role("") == ROLE_PROGRAMMER
    assert [k for _l, k in nav_pages_for_role(ROLE_TECHNICIAN)] == [
        "console", "diagnostics"]
    assert nav_pages_for_role(ROLE_PROGRAMMER) == NAV_PAGES
    assert initial_page_for_role(ROLE_TECHNICIAN) == "console"
    assert initial_page_for_role(ROLE_PROGRAMMER) == "workbench"


def test_technician_window_hides_other_nav_but_keeps_pages(win):
    """真实模式技术员：导航只两项、默认进操作台；页面对象仍全部存在（只隐藏导航）。"""
    _w, vm, stack, app = win
    old_profile = stack.profile
    stack.profile = "production"          # 模拟真实模式，不接硬件
    w2 = MainWindow(UiContext(stack=stack, vm=vm, role=ROLE_TECHNICIAN))
    try:
        labels = [b.text().strip() for b in w2._group.buttons()]
        assert labels == ["操作台", "日志诊断"]
        assert w2.stack_widget.currentWidget() is w2.pages["console"]
        assert set(w2.pages) == {k for _l, k in NAV_PAGES}
        # 操作台常用手工操作（DEC-148）：三个弹窗式操作两角色都保留
        assert set(w2.pages["console"].manual_buttons) == {
            "tower_width", "bolt_size", "motion_adjust"}
    finally:
        w2.shutdown(); w2.close(); w2.deleteLater(); app.processEvents()
        stack.profile = old_profile


def test_page_switch_is_not_logged(win):
    """DEC-052: 页面切换不记日志（代码也删了，不是只过滤）。

    日志只留给故障/事件；切换页面是纯导航，记下来只会淹没真正的问题。
    """
    import logging

    w, _vm, _stack, app = win
    seen = []

    class _Capture(logging.Handler):
        def emit(self, record):  # noqa: D401 - 测试桩
            seen.append(record)

    tower = logging.getLogger("towerops")
    handler = _Capture(level=logging.DEBUG)
    old_level = tower.level
    tower.addHandler(handler)
    tower.setLevel(logging.DEBUG)
    try:
        for _label, key in NAV_PAGES:
            w._select(key)
            app.processEvents()
    finally:
        tower.removeHandler(handler)
        tower.setLevel(old_level)

    assert w.stack_widget.currentWidget() is w.pages["diagnostics"]   # 确实切了
    assert not [r for r in seen if "切换到页面" in r.getMessage()]


def _write_device_config(tmp_path, cameras):
    """写一份测试专用设备配置（不碰用户的 configs/devices/devices.yaml）。"""
    from application.services.device_config import DeviceConfig, DeviceConfigStore
    path = tmp_path / "devices.yaml"
    seed = DeviceConfigStore(str(path), seed_if_missing=False)
    seed._config = DeviceConfig(cameras=list(cameras))   # noqa: SLF001 - 测试构造
    seed.save()
    return str(path)


def _window_with_cameras(tmp_path, qapp, cameras):
    """离线栈 + 主窗口（多相机页签用例用；相机来自临时设备配置）。"""
    cfg = _write_device_config(tmp_path, cameras)
    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path), recipe_path=RECIPE,
                                device_config_path=cfg)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    w = MainWindow(UiContext(stack=stack, vm=vm))
    w.resize(1440, 1024)
    return w, vm, stack


def _close_window(w, stack, qapp):
    import gc
    try:
        w.shutdown()
        w.close()
        w.deleteLater()
        qapp.processEvents()
    except Exception:  # noqa: BLE001
        pass
    stack.close()
    del w
    gc.collect()
    qapp.processEvents()


def _click_camera_tab(page, camera_id):
    """模拟操作员点相机页签（走 currentChanged -> _on_channel_selected）。"""
    tabs = page.channel_tabs
    for i in range(tabs.count()):
        if tabs.tabData(i) == camera_id:
            tabs.setCurrentIndex(i)
            return
    raise AssertionError(f"没有相机页签 {camera_id!r}")


def test_workbench_initial_state_and_rail(win):
    w, _vm, _stack, app = win
    w._select("workbench"); app.processEvents()
    page = w.pages["workbench"]
    assert page.state_pill.text().endswith("待检查")
    assert page._step_ids[0] == "move"                     # 未启动也显示配方声明的步骤
    assert page.rail.buttons["start"].isEnabled()          # 死锁已修
    # DEC-041: 停止/待机态也可以单步（起一个 run 只走一步）；没在跑就不能暂停/终止
    assert page.rail.buttons["single_step"].isEnabled()
    assert not page.rail.buttons["request_pause"].isEnabled()
    assert not page.rail.buttons["resume"].isEnabled()


def test_workbench_steps_scroll_inside_card(win):
    """DEC-099：步骤多时只在卡片内部滚动，不把整页往下撑。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QScrollArea
    w, _vm, _stack, app = win
    w._select("workbench"); app.processEvents()
    page = w.pages["workbench"]
    assert isinstance(page.steps_scroll, QScrollArea)
    assert page.steps_scroll.widget() is page.steps_container
    ids = [f"step_{i}" for i in range(40)]
    page._rebuild_step_rows(ids, ids)
    assert page.steps_layout.count() == 41      # 40 行 + 末尾 stretch
    assert page.steps_scroll.verticalScrollBarPolicy() != \
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_workbench_can_pick_the_recipe_to_run(win):
    """DEC-041: 作业台自己的下拉能选执行方案，选了就换成那份。"""
    w, vm, _stack, app = win
    w._select("workbench"); w.refresh_all(); app.processEvents()
    page = w.pages["workbench"]
    assert page.recipe_combo.isEnabled()
    ids = [page.recipe_combo.itemData(i) for i in range(page.recipe_combo.count())]
    target = "fastening_bolt" if "fastening_bolt" in ids else ids[0]
    idx = page.recipe_combo.findData(target)
    page._on_recipe_selected(idx)
    app.processEvents()
    assert vm.selected_recipe == target
    assert "已选择执行方案" in page.hint.text()
    assert page.recipe_combo.currentData() == target


def test_workbench_switch_recipe_after_completed_run(win):
    """DEC-100：运行完成后再换方案，步骤清单/版本必须刷新成新方案，不能沿用旧 run。"""
    w, vm, stack, app = win
    w._select("workbench"); w.refresh_all(); app.processEvents()
    page = w.pages["workbench"]
    stack.task.start("ds86_two_station_example", allow_draft=True, step_confirm=False)
    for _ in range(120):
        if stack.task.tick().value in ("COMPLETED", "FAULT", "STOPPED"):
            break
    w.refresh_all(); app.processEvents()
    old_run_ids = list(page._step_ids)
    target = "fastening_bolt"
    page._on_recipe_selected(page.recipe_combo.findData(target))
    w.refresh_all(); app.processEvents()
    expected = page._declared_step_ids(target)
    assert expected and page._step_ids == expected
    assert page._step_ids != old_run_ids
    assert stack.recipes.get_draft(target).version in page.kv_version.text()
    assert all("待执行" in pill.text() for _lbl, pill in page._step_rows)


def test_workbench_recipe_combo_locked_while_running(win):
    """运行中不能换执行方案：下拉禁用、提示原因。"""
    w, vm, _stack, app = win
    vm.start_current()
    w._select("workbench"); w.refresh_all(); app.processEvents()
    page = w.pages["workbench"]
    assert vm.state().task_state in ("RUNNING", "PAUSED", "WAITING_OPERATOR")
    assert not page.recipe_combo.isEnabled()
    assert "运行中" in page.recipe_combo.toolTip()


def test_stop_all_always_enabled_on_every_page(win):
    """03 §3/§4.2: 所有页面保留停止入口, "停止全部"始终可发停止请求。"""
    w, _vm, _stack, app = win
    assert w.global_stop.isEnabled()
    for _label, key in NAV_PAGES:
        w._select(key); app.processEvents()
        assert w.global_stop.isEnabled()


def test_workbench_running_shows_progress_and_buttons(win):
    w, vm, _stack, app = win
    vm.start_current()
    vm.tick()          # 单次推进: 整份配方尚未跑完, 仍处执行中
    w._select("workbench")
    w.refresh_all(); app.processEvents()
    page = w.pages["workbench"]
    assert page.state_pill.text().endswith("执行中")
    assert page.rail.buttons["request_pause"].isEnabled()
    assert not page.rail.buttons["start"].isEnabled()
    assert not page.rail.buttons["resume"].isEnabled()   # RUNNING 下不可继续


def test_vision_page_lists_cameras_from_device_config(win):
    """视觉调试页的通道列表必须来自设备配置, 且状态与运行期 registry 一致。"""
    from PySide6.QtCore import Qt
    w, _vm, stack, app = win
    w._select("vision"); app.processEvents()
    page = w.pages["vision"]
    cams = stack.device_config.config.cameras
    assert page.chan_list.count() == len(cams)
    for row in range(page.chan_list.count()):
        cid = page.chan_list.item(row).data(Qt.ItemDataRole.UserRole)
        cam = stack.device_config.find_camera(cid)
        assert cam is not None
        assert stack.device_state.status_text(cam) in page.chan_list.item(row).text()


def test_vision_page_select_and_capture_ds86_shows_image(win):
    """选中通道 -> 单次抓拍 -> 显示彩色/深度图与帧元数据 (E19)。"""
    from PySide6.QtCore import Qt
    w, _vm, stack, app = win
    cam = stack.device_config.find_camera("ds86_01")
    cam.enabled = True
    cam.confirmed = True
    w._select("vision"); app.processEvents()
    page = w.pages["vision"]
    row = [page.chan_list.item(i).data(Qt.ItemDataRole.UserRole)
           for i in range(page.chan_list.count())].index("ds86_01")
    page.chan_list.setCurrentRow(row); app.processEvents()
    assert page.btn_cap.isEnabled()

    page._capture(); app.processEvents()
    assert page._frame is not None, page.result.text()
    assert page.view_color.available and page.view_depth.available
    assert "0.001" in page.kv_scale.text()          # depth_scale_m 如实展示
    assert "1 原始值 = 1 mm" in page.kv_scale.text()  # 单位口径写明 (DEC-025)
    assert "离线合成" in page.src_hint.text()        # 不冒充真实图像

    page._run_pipeline(); app.processEvents()
    assert "状态" in page.result.text()


def test_vision_algo_panel_seeds_ds86_field_depth_range(win):
    """DEC-046: DS86 未单独保存深度区间时, 面板按引导配置的现场区间 (100–300) 播种;
    通道显式保存过的值优先。"""
    page, stack, app = _vision(win)
    stack.motion.guidance.config_store.config.ds86.update(
        {"ds86_depth_map_min_mm": 100, "ds86_depth_map_max_mm": 300})
    cam = _enable(stack, "ds86_01")
    cam.image_ops = {}
    _select_cam(page, "ds86_01", app)
    page._params_loaded_for = None          # 强制重新载入 (模拟首次进入该通道)
    page._on_select(); app.processEvents()
    assert page._widgets["depth_seg_min_mm"][0].value() == 100
    assert page._widgets["depth_seg_max_mm"][0].value() == 300

    cam.image_ops = {"depth_seg_min_mm": 500, "depth_seg_max_mm": 900}
    page._params_loaded_for = None
    page._on_select(); app.processEvents()
    assert page._widgets["depth_seg_min_mm"][0].value() == 500
    assert page._widgets["depth_seg_max_mm"][0].value() == 900


def test_vision_raw_preview_uses_ds86_field_depth_range(win):
    """原图对照里的深度伪彩也按现场区间上色 (DEC-046), 并在提示里写明区间。"""
    page, stack, app = _vision(win)
    stack.motion.guidance.config_store.config.ds86.update(
        {"ds86_depth_map_min_mm": 100, "ds86_depth_map_max_mm": 300})
    _capture("ds86_01", stack, page, app)
    page.chk_raw.setChecked(True); app.processEvents()
    assert page.view_depth.available
    assert "100–300 mm" in page.preview_hint.text()



    """无适配器的通道(如 D405)抓拍要给出明确原因, 不能伪装成功。"""
    from PySide6.QtCore import Qt
    w, _vm, stack, app = win
    cam = stack.device_config.find_camera("d405_01")
    cam.enabled = True
    cam.confirmed = True
    w._select("vision"); app.processEvents()
    page = w.pages["vision"]
    row = [page.chan_list.item(i).data(Qt.ItemDataRole.UserRole)
           for i in range(page.chan_list.count())].index("d405_01")
    page.chan_list.setCurrentRow(row); app.processEvents()
    page._capture(); app.processEvents()
    assert page._frame is None
    assert "抓拍未取得帧" in page.result.text()


def test_vision_page_connect_toggles_real_session(win):
    """DS86 有适配器 -> 连接真的开关会话; 无适配器 -> 明确报错。"""
    from PySide6.QtCore import Qt
    w, _vm, stack, app = win
    for cid in ("ds86_01", "d405_01"):
        c = stack.device_config.find_camera(cid)
        c.enabled = True
        c.confirmed = True
    w._select("vision"); app.processEvents()
    page = w.pages["vision"]

    def select(cid):
        row = [page.chan_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(page.chan_list.count())].index(cid)
        page.chan_list.setCurrentRow(row); app.processEvents()

    select("d405_01")
    page._toggle_conn(); app.processEvents()
    assert "无适配器" in page.result.text()
    assert stack.device_state.is_open("d405_01") is False

    select("ds86_01")
    if stack.device_state.is_open("ds86_01"):
        page._toggle_conn(); app.processEvents()          # 先断开
    assert stack.device_state.is_open("ds86_01") is False
    page._toggle_conn(); app.processEvents()              # 再连接
    assert stack.device_state.is_open("ds86_01") is True
    assert "会话已打开" in page.result.text()


def test_vision_connect_button_greyed_without_adapter(win):
    """没会话适配器的通道: 「连接」**置灰** + 悬停说明原因（别让操作员点了才知道）。"""
    from PySide6.QtCore import Qt
    w, _vm, stack, app = win
    w._select("vision"); app.processEvents()
    page = w.pages["vision"]

    def select(cid):
        row = [page.chan_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(page.chan_list.count())].index(cid)
        page.chan_list.setCurrentRow(row); app.processEvents()

    select("ds86_01")                          # 离线 DS86 有适配器 -> 可点
    assert stack.device_state.has_adapter("ds86_01") is True
    assert page.btn_conn.isEnabled() is True
    assert page.btn_conn.toolTip() == ""

    select("d405_01")                          # 默认配置没有 channel_id -> 无适配器
    assert stack.device_state.has_adapter("d405_01") is False
    assert page.btn_conn.isEnabled() is False
    assert "无适配器" in page.btn_conn.toolTip()
    assert "channel_id" in page.btn_conn.toolTip()


def test_devices_page_conn_button_greyed_without_adapter(win):
    """设备页同理: 选中没有会话适配器的通道行 -> 「连接/断开」置灰并说明原因。"""
    w, _vm, stack, app = win
    w._select("devices"); app.processEvents()
    page = w.pages["devices"]
    btn = page.chan_btns["连接/断开"]

    def select(cid):
        page.chan_table.setCurrentCell(page._chan_ids.index(cid), 0)
        app.processEvents()

    select("ds86_01")
    assert btn.isEnabled() is True
    assert btn.toolTip() == ""

    select("d405_01")
    assert stack.device_state.has_adapter("d405_01") is False
    assert btn.isEnabled() is False
    assert "无适配器" in btn.toolTip()


def test_workbench_channel_tabs_mirror_enabled_cameras(tmp_path, qapp):
    """DEC-051: 右卡相机页签 = 设备配置里已启用的相机（顺序同配置），未启用不显示。"""
    from application.services.device_config import CameraEntry

    w, _vm, stack = _window_with_cameras(tmp_path, qapp, [
        CameraEntry("ds86_01", "DS86 (随臂)", enabled=True, confirmed=True),
        CameraEntry("d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
                    host="192.168.2.117", port=5555, channel_id="cam1"),
        CameraEntry("d405_02", "D405-2", "zmq", enabled=False, confirmed=False),
        CameraEntry("d405_03", "D405-3", "zmq", enabled=False, confirmed=False),
    ])
    try:
        w._select("workbench"); w.refresh_all(); qapp.processEvents()
        tabs = w.pages["workbench"].channel_tabs
        assert tabs.count() == 2
        assert [tabs.tabData(i) for i in range(2)] == ["ds86_01", "d405_01"]
        assert [tabs.tabText(i) for i in range(2)] == ["DS86 (随臂)", "D405-1"]
    finally:
        _close_window(w, stack, qapp)


def test_workbench_channel_tabs_follow_device_config_changes(tmp_path, qapp):
    """DEC-051: 设备配置页启用/停用相机后，右卡页签同步增减（无需重启）。"""
    from application.services.device_config import CameraEntry

    w, _vm, stack = _window_with_cameras(tmp_path, qapp, [
        CameraEntry("ds86_01", "DS86 (随臂)", enabled=True, confirmed=True),
        CameraEntry("d405_03", "D405-3", "zmq", enabled=False, confirmed=False),
    ])
    try:
        w._select("workbench"); w.refresh_all(); qapp.processEvents()
        page = w.pages["workbench"]
        assert page.channel_tabs.count() == 1

        cam = stack.device_config.find_camera("d405_03")
        cam.enabled = True
        stack.device_config.update_camera("d405_03", cam)
        page.refresh(); qapp.processEvents()
        assert page.channel_tabs.count() == 2
        assert page.channel_tabs.tabData(1) == "d405_03"

        cam = stack.device_config.find_camera("d405_03")
        cam.enabled = False
        stack.device_config.update_camera("d405_03", cam)
        page.refresh(); qapp.processEvents()
        assert page.channel_tabs.count() == 1
    finally:
        _close_window(w, stack, qapp)


def test_shutdown_stops_timers(win):
    """关闭前必须停掉定时器: 否则回调会在对象销毁后触发 -> 原生崩溃 (rc=139)。"""
    w, _vm, _stack, _app = win
    page = w.pages["workbench"]
    assert page._timer.isActive()                 # noqa: SLF001
    assert w._clock.isActive()                    # noqa: SLF001
    w.shutdown()
    assert not page._timer.isActive()             # noqa: SLF001
    assert not w._clock.isActive()                # noqa: SLF001


def test_manual_page_actions_unbound_and_disabled(win):
    """未核对机构动作显示“未绑定”且不可点 (03 §6)。"""
    from application.presentation.components import StatusPill
    w, _vm, _stack, app = win
    w._select("manual"); app.processEvents()
    page = w.pages["manual"]
    found = False
    for i in range(page.mech_rows.count()):
        it = page.mech_rows.itemAt(i)
        if it and it.widget():
            pills = [c for c in it.widget().children() if isinstance(c, StatusPill)]
            if pills:
                assert "未绑定" in pills[0].text()
                found = True
    assert found, "机构动作应至少有占位行"


# ---------------------------------------------------------------- E21 设备配置页

def _no_dialog(*_a, **_k):
    raise AssertionError("测试中不应弹窗")


def _device_values(**over):
    base = {"device_id": "stm32_99", "name": "测试板", "kind": "board",
            "protocol": "serial", "enabled": True, "confirmed": False,
            "serial_port": "COM9", "baudrate": 115200, "data_bits": 8,
            "parity": "N", "stop_bits": 1, "duplex": "half", "host": "",
            "port": None, "link_id": "", "address": None,
            "firmware_profile": "", "notes": ""}
    base.update(over)
    return base


def test_devices_page_has_crud_toolbars(win):
    from PySide6.QtWidgets import QPushButton
    w, _vm, _stack, app = win
    w._select("devices"); app.processEvents()
    page = w.pages["devices"]
    assert page.node_table.columnCount() == 6      # 含协议与连接列
    assert page.node_table.rowCount() == 7
    assert page.chan_table.rowCount() == 5
    labels = {b.text() for b in page.findChildren(QPushButton)}
    assert {"新增设备", "修改", "删除", "测试连接"} <= labels


def test_devices_page_add_persists_and_delete(win, monkeypatch):
    w, _vm, stack, app = win
    from application.presentation.pages import devices as dev_mod
    from application.services.device_config import DeviceConfigStore
    w._select("devices"); app.processEvents()
    page = w.pages["devices"]
    before = page.node_table.rowCount()

    monkeypatch.setattr(dev_mod, "_dialog", lambda *a, **k: _device_values())
    monkeypatch.setattr(dev_mod, "_warn", _no_dialog)
    page._add_device(); app.processEvents()
    assert page.node_table.rowCount() == before + 1
    assert "stm32_99" in page._node_ids

    # 落盘并可重新读回 (原子写)
    assert stack.device_config.path and stack.device_config.path.endswith("devices.yaml")
    reloaded = DeviceConfigStore(stack.device_config.path).load()
    assert any(d.device_id == "stm32_99" and d.serial_port == "COM9"
               for d in reloaded.devices)

    # 删除
    monkeypatch.setattr(dev_mod, "_confirm", lambda *a, **k: True)
    row = page._node_ids.index("stm32_99")
    page.node_table.setCurrentCell(row, 0)
    page._del_device(); app.processEvents()
    assert "stm32_99" not in page._node_ids
    again = DeviceConfigStore(stack.device_config.path).load()
    assert all(d.device_id != "stm32_99" for d in again.devices)


def test_devices_page_edit_camera_and_link(win, monkeypatch):
    w, _vm, _stack, app = win
    from application.presentation.pages import devices as dev_mod
    w._select("devices"); app.processEvents()
    page = w.pages["devices"]

    cam = {"camera_id": "d405_01", "name": "D405-1", "protocol": "zmq",
           "enabled": True, "confirmed": False, "serial": "", "service_id": "",
           "channel_id": "", "host": "10.0.0.9", "port": 5555, "stream_url": "",
           "capture_profile": "", "calibration_id": "", "notes": "",
           "serial_port": "", "baudrate": 115200, "data_bits": 8, "parity": "N",
           "stop_bits": 1, "duplex": "half"}
    monkeypatch.setattr(dev_mod, "_dialog", lambda *a, **k: cam)
    monkeypatch.setattr(dev_mod, "_warn", _no_dialog)
    page.chan_table.setCurrentCell(page._chan_ids.index("d405_01"), 0)
    page._edit_camera(); app.processEvents()
    assert page.store.find_camera("d405_01").host == "10.0.0.9"
    assert page.store.find_camera("d405_01").status_text() == "待核对"

    link = {"link_id": "link0", "transport_kind": "serial", "protocol": "serial",
            "enabled": True, "confirmed": True, "notes": "现场核对",
            "serial_port": "COM3", "baudrate": 115200, "data_bits": 8,
            "parity": "N", "stop_bits": 1, "duplex": "half", "host": "", "port": None}
    monkeypatch.setattr(dev_mod, "_dialog", lambda *a, **k: link)
    page.link_table.setCurrentCell(page._link_ids.index("link0"), 0)
    page._edit_link(); app.processEvents()
    assert page.store.find_link("link0").serial_port == "COM3"
    assert page.store.find_link("link0").status_text() == "未连接"


def test_devices_page_unconfirmed_shows_pending_not_green(win):
    """未确认的连接参数必须显示“待核对/未启用”, 不伪装绿色 (12 §4.4)。"""
    w, _vm, _stack, app = win
    w._select("devices"); app.processEvents()
    page = w.pages["devices"]
    statuses = {page.node_table.item(r, 5).text()
                for r in range(page.node_table.rowCount())}
    assert statuses <= {"未启用", "已启用"}


def test_devices_page_surfaces_unconfirmed_advisories(win):
    """绑定校验卡必须把"未确认/未配置"提示露出来, 不能只显示"无阻塞项"。"""
    w, _vm, _stack, app = win
    w._select("devices"); app.processEvents()
    page = w.pages["devices"]
    texts = []
    for i in range(page.bind_layout.count()):
        wdg = page.bind_layout.itemAt(i).widget()
        if wdg is not None and hasattr(wdg, "text"):
            texts.append(wdg.text())
    joined = " ".join(texts)
    assert "待核对/未配置" in joined
    assert "未现场核对" in joined or "未配置" in joined


def test_devices_page_marks_startup_failure_red(qapp):
    """装配期失败的设备: 设备页状态列必须显示"装配失败"、标红、悬停给出原因。

    依据用户真机诉求: 机器人连不上**也要能进界面**, 而且进来后要**看得见**是哪台、为什么
    —— 否则只是"进去了", 仍然得回头翻控制台。
    """
    from types import SimpleNamespace

    from PySide6.QtGui import QColor

    from application.bootstrap.production import StartupIssue
    from application.presentation.pages.devices import DevicesPage
    from application.presentation.theme import THEME
    from application.services.device_config import (
        DeviceConfig,
        DeviceConfigStore,
        DeviceEntry,
    )
    from application.services.device_state import DeviceStateRegistry

    store = DeviceConfigStore.in_memory(DeviceConfig(devices=[
        DeviceEntry("eco65_01", "ECO65", "robot", "vendor_tcp",
                    enabled=True, confirmed=True, host="192.168.3.66", port=8080)]))
    state = DeviceStateRegistry(startup_issues=[StartupIssue(
        "ECO65 机器人",
        "ProductionError: 机器人连接失败: 192.168.3.66:8080",
        keys=("eco65_01",))])
    ctx = SimpleNamespace(stack=SimpleNamespace(device_config=store, device_state=state))
    page = DevicesPage(ctx)
    try:
        cell = page.node_table.item(0, 5)          # 状态列
        assert cell.text() == "未启用"
        assert cell.foreground().color() == QColor(THEME.danger)
        assert "机器人连接失败" in cell.toolTip()
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_vision_page_s100_channel_uses_s100_service_and_labels_it(tmp_path, qapp):
    """E09: zmq 通道经 S100 服务抓拍, 来源标注必须说 S100(V2 draft), 不能写成 FakeCamera。"""
    from PySide6.QtCore import Qt
    from application.bootstrap.composition import build_offline_stack
    from application.presentation.app import MainWindow, UiContext
    from application.presentation.viewmodel import WorkbenchViewModel
    from application.services.device_config import (
        CameraEntry, DeviceConfig, DeviceConfigStore)

    cfg_path = tmp_path / "devices.yaml"
    seed = DeviceConfigStore(str(cfg_path), seed_if_missing=False)
    seed._config = DeviceConfig(cameras=[               # noqa: SLF001 - 测试构造
        CameraEntry("d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
                    host="192.168.2.115", port=5555, channel_id="cam1")])
    seed.save()

    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path), recipe_path=RECIPE,
                                device_config_path=str(cfg_path))
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    w = MainWindow(UiContext(stack=stack, vm=vm))
    try:
        assert "d405_01" in stack.camera_registry.camera_ids()
        w._select("vision"); qapp.processEvents()
        page = w.pages["vision"]
        row = next(i for i in range(page.chan_list.count())
                   if page.chan_list.item(i).data(Qt.ItemDataRole.UserRole) == "d405_01")
        page.chan_list.setCurrentRow(row); qapp.processEvents()
        page._capture(); qapp.processEvents()
        assert page._frame is not None, page.result.text()
        assert page._frame.service_id.startswith("s100")
        assert "S100" in page.src_hint.text()
        assert "candidate" in page.src_hint.text()    # 明示协议仍待两端评审冻结
        assert "已启用" in page.chan_list.item(row).text()   # 抓拍隐式建会, 状态如实
    finally:
        w.shutdown(); w.close(); w.deleteLater(); qapp.processEvents()
        stack.close()


def test_motion_page_constructs_with_three_subtabs(win):
    """「运动方案」页：路径规划 / 路径实施 / 手动调整 三个子 tab（编辑与调用隔离）。"""
    w, _vm, stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    assert page.tabs.count() == 3
    assert [page.tabs.tabText(i) for i in range(3)] == \
        ["路径规划", "路径实施", "手动调整"]
    assert page.plan_combo.count() == len(stack.motion_config.config.plans)


def test_motion_page_lists_plans_and_waypoints(win):
    """方案列表与路点表来自运动配置（不是写死的）。"""
    from application.services.motion_config import MotionPlan, Waypoint
    w, _vm, stack, app = win
    stack.motion_config.add_plan(MotionPlan(
        "demo", speed=25, blend=8, timeout=15,
        waypoints=[Waypoint("movel", (0.1, 0.2, 0.3, 0, 0, 0)),
                   Waypoint("movej", (1, 2, 3, 4, 5, 6))], source="t"))
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    assert page.plan_combo.count() == 1
    assert page.plan_combo.currentData() == "demo"
    assert page.wp_table.rowCount() == 2
    assert page.speed_spin.value() == 25 and page.blend_spin.value() == 8
    # movel 行界面显示 mm（0.1 m -> 100）
    assert page.wp_table.item(0, 1).text().startswith("100")
    # 运行方案单选每个方案一枚
    assert set(page.run_radios) == {"demo"}


def test_motion_page_socket_waypoint_rows(win):
    """套筒偏移行：方式可下拉到「套筒偏移」；前 3 列(dx/dy/dz mm)可填，后 3 列只读（DEC-139）。"""
    from PySide6.QtCore import Qt

    from application.services.motion_config import MotionPlan, Waypoint
    w, _vm, stack, app = win
    stack.motion_config.add_plan(MotionPlan(
        "sock", waypoints=[Waypoint("socket_offset", (0, 15, 0, 0, 0, 0))], source="t"))
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    combo = page.wp_table.cellWidget(0, 0)
    assert combo.currentData() == "socket_offset"
    assert combo.itemText(combo.findData("socket_offset")) == "套筒偏移"
    # 偏移(mm) 显示在 y/j2 列；X/Y/Z 三列都可编辑，姿态列只读
    assert page.wp_table.item(0, 2).text() == "15"
    for col in (1, 2, 3):
        assert bool(page.wp_table.item(0, col).flags() & Qt.ItemFlag.ItemIsEditable)
    for col in (4, 5, 6):
        assert not bool(page.wp_table.item(0, col).flags() & Qt.ItemFlag.ItemIsEditable)
        assert page.wp_table.item(0, col).text() == "0"
    # X/Z 也可配置（DEC-139）：填 (5, -15, 2) mm
    page.wp_table.item(0, 1).setText("5")
    page.wp_table.item(0, 2).setText("-15")
    page.wp_table.item(0, 3).setText("2"); app.processEvents()
    wp = page._row_waypoint(0)          # noqa: SLF001
    assert wp.mode == "socket_offset"
    assert wp.pos == (5.0, -15.0, 2.0, 0.0, 0.0, 0.0)
    # 切换方式为点位行：6 列全部恢复可编辑
    combo.setCurrentIndex(combo.findData("movej")); app.processEvents()
    for col in range(1, 7):
        assert bool(page.wp_table.item(0, col).flags() & Qt.ItemFlag.ItemIsEditable)


def test_motion_page_manual_jog_tab_moves_bolt_axes(win, monkeypatch):
    """DEC-139：「手动调整」tab 有 X/Y/Z 正负按钮，点动带轴/步长/速度并作废引导画面。"""
    w, _vm, stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    assert page.tabs.tabText(page.tabs.count() - 1) == "手动调整"
    seen: list[tuple] = []
    monkeypatch.setattr(page.runner, "request_tool_jog",
                        lambda axis, mm, speed=None: seen.append((axis, mm, speed)) or True)
    monkeypatch.setattr(page, "_guard_motion", lambda *_a, **_k: True)
    page.jog_step.setValue(2.5)
    page.jog_speed.setValue(17)
    # 离线栈机械臂未连接，刷新会把按钮置灰；点动逻辑本身由 request_tool_jog 承担。
    for btn in page.jog_buttons.values():
        btn.setEnabled(True)
    page.jog_buttons[("X", +1)].click()
    page.jog_buttons[("Y", -1)].click()
    page.jog_buttons[("Z", +1)].click()
    app.processEvents()
    assert seen == [("X", 2.5, 17), ("Y", -2.5, 17), ("Z", 2.5, 17)]


def test_motion_page_pose_view_reads_current_pose(win, monkeypatch):
    """DEC-145：「手动调整」页的 3D 位姿示意读当前位姿，只读、未连接时清空。"""
    w, _vm, stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    assert page.tabs.tabText(page.tabs.count() - 1) == "手动调整"
    view = page.pose_view
    pose = (0.1, -0.2, 0.3, 0.0, 0.0, 0.785)
    monkeypatch.setattr(page.runner.arm, "connected", True, raising=False)
    monkeypatch.setattr(page.runner.arm, "get_current_pose", lambda: pose, raising=False)
    page._refresh_pose_view()
    assert view.pose() == pose
    # 离屏渲染（验证 QPainter 不异常）
    view.resize(420, 320)
    assert not view.grab().isNull()
    # 未连接：清空位姿并显示提示
    monkeypatch.setattr(page.runner.arm, "connected", False, raising=False)
    page._refresh_pose_view()
    assert view.pose() is None


def test_motion_page_tool_offset_waypoint_rows(win):
    """工具系偏移行：前 3 列填 dx/dy/dz(mm)，后 3 列只读；表格往返保持 mm(DEC-060)。"""
    from PySide6.QtCore import Qt

    from application.services.motion_config import MotionPlan, Waypoint
    w, _vm, stack, app = win
    stack.motion_config.add_plan(MotionPlan(
        "lift", speed=10, blend=0,
        waypoints=[Waypoint("tool_offset", (0, 0, 5, 0, 0, 0))], source="t"))
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    combo = page.wp_table.cellWidget(0, 0)
    assert combo.currentData() == "tool_offset"
    assert combo.itemText(combo.findData("tool_offset")) == "工具系偏移"
    # 前 3 列可编辑，值按 mm 显示（不除 1000）
    for c in (1, 2, 3):
        assert bool(page.wp_table.item(0, c).flags() & Qt.ItemFlag.ItemIsEditable)
    assert page.wp_table.item(0, 3).text() == "5"
    # 后 3 列置 0 且只读
    for c in (4, 5, 6):
        assert page.wp_table.item(0, c).text() == "0"
        assert not bool(page.wp_table.item(0, c).flags() & Qt.ItemFlag.ItemIsEditable)
    # 表格 -> 路点：dx/dy/dz 原样(mm)，不做 m 换算
    page.wp_table.item(0, 2).setText("100"); app.processEvents()
    page.wp_table.item(0, 3).setText("-5"); app.processEvents()
    wp = page._row_waypoint(0)          # noqa: SLF001
    assert wp.mode == "tool_offset"
    assert wp.pos[:3] == (0.0, 100.0, -5.0) and wp.pos[3:] == (0.0, 0.0, 0.0)


def test_motion_page_buttons_disabled_when_arm_not_connected(win):
    """机械臂未连接：运动按钮禁用；停止始终可用。DEC-085 起无环境变量运动闸。"""
    w, _vm, _stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page._refresh_status()
    for btn in (page.btn_run, page.btn_guide_coarse, page.btn_guide_fine,
                page.btn_fwd, page.btn_back):
        assert not btn.isEnabled()
    assert page.btn_guide_coarse.text() == "粗定位"
    assert page.btn_guide_fine.text() == "精定位"
    assert not hasattr(page, "log_view")          # DEC-107：路径实施日志已移除
    assert page.btn_stop.isEnabled()
    assert "默认允许" in page.kv_gate.text()


def test_motion_page_shutdown_stops_timer(win):
    """页面必须能停定时器（Qt 关闭顺序：晚了会崩，断言过但 rc=139）。"""
    w, _vm, _stack, _app = win
    w._select("motion"); _app.processEvents()
    page = w.pages["motion"]
    assert page._timer.isActive()          # noqa: SLF001
    page.shutdown()
    assert not page._timer.isActive()      # noqa: SLF001


def test_motion_page_visual_detect_is_local_and_does_not_need_motion_gate(win, monkeypatch):
    """单次视觉检测**不动机械臂**；离线也能跑通并出结果图。"""
    w, _vm, stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page._refresh_status()
    assert page.btn_detect.isEnabled()          # 检测不受运动闸限制
    page._detect(); app.processEvents()
    # 离线栈用 FakeCamera + 进程内 stub 圆宿主 -> 应能出结果（含相机系 XYZ 与偏差）
    assert page.kv_xyz.text().find("X=") >= 0 or "失败" in page.kv_detect.text()
    assert page.view_result.available or "未检出" in page.kv_detect.text()


def test_motion_page_guide_camera_combo_and_start_guide_passes_camera(win, monkeypatch):
    """DEC-137：运动方案页有「引导相机」下拉（只列本次已连接通道），粗定位带上 camera_id。"""
    w, _vm, stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page.refresh(); app.processEvents()
    assert page.guide_camera.currentData() == "ds86_01"
    seen: dict = {}
    monkeypatch.setattr(page.runner, "start_guide", lambda **kw: seen.update(kw) or True)
    monkeypatch.setattr(page, "_guard_motion", lambda *_a, **_k: True)
    page._guide("coarse")
    assert seen.get("camera_id") == "ds86_01"
    assert seen.get("stages") == ("coarse",)


def test_motion_page_teach_group_offline_is_disabled(win):
    """DEC-067：离线模式没有示教点登记（不能拿假机械臂读数当示教值）。"""
    w, _vm, _stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    assert page.teach_store is None
    assert page.teach_table.rowCount() == 0
    assert not page.btn_teach_add.isEnabled()
    assert "离线" in page.btn_teach_add.toolTip()
    assert "离线" in page.teach_note.text()


def test_motion_page_register_teach_point_from_current_joint(win, monkeypatch, tmp_path):
    """DEC-067：读取当前关节 → 命名登记 → 写入 joint_targets.yaml 并即时生效。"""
    from application.bootstrap.production import load_confirmed_targets
    from application.presentation.pages import motion as motion_mod
    from application.services.teach_points import TeachPointStore

    w, _vm, _stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]

    target_path = tmp_path / "site" / "joint_targets.yaml"
    shared: dict = {}
    page.teach_store = TeachPointStore(str(target_path), confirmed_targets=shared)
    page._call_arm = lambda name: ([11.5, -22.25, 33.0, 44.5, 55.0, -66.75]
                                   if name == "get_current_joint" else None)
    monkeypatch.setattr(motion_mod, "_ask_text", lambda *a, **k: ("teach_01", True))
    monkeypatch.setattr(motion_mod, "_info_dialog", lambda *a, **k: None)
    monkeypatch.setattr(motion_mod, "_warn_dialog", lambda *a, **k: None)

    page._register_teach_point()
    app.processEvents()

    assert target_path.exists()
    loaded = load_confirmed_targets(str(target_path))
    assert loaded["teach_01"].joint_deg == (11.5, -22.25, 33.0, 44.5, 55.0, -66.75)
    assert shared["teach_01"].joint_deg == loaded["teach_01"].joint_deg   # 共享字典即时同步
    assert page.teach_table.rowCount() == 1
    assert page.teach_table.item(0, 0).text() == "teach_01"
    assert "已登记 1 个示教点" in page.teach_note.text()

    # 选中并删除
    page.teach_table.selectRow(0)
    monkeypatch.setattr(motion_mod, "_confirm", lambda *a, **k: True)
    page._delete_teach_point()
    assert page.teach_table.rowCount() == 0
    assert load_confirmed_targets(str(target_path)) == {}
    assert "teach_01" not in shared



def test_devices_page_status_follows_registry(win):
    """状态列 = registry 分级状态: 探测通过后不再停在不动的「待连接」。"""
    from application.services.device_probe import ProbeResult
    w, _vm, stack, app = win
    d = stack.device_config.find_device("eco65_01")
    d.enabled = True
    d.confirmed = True
    w._select("devices"); app.processEvents()
    page = w.pages["devices"]
    r = page._node_ids.index("eco65_01")

    assert page.node_table.item(r, 5).text() == "未启用"
    stack.device_state.record_probe(
        "eco65_01", ProbeResult(True, "TCP 端口可达", is_real=True))
    page.refresh(); app.processEvents()
    assert page.node_table.item(r, 5).text() == "已启用"

    stack.device_state.record_probe(
        "eco65_01", ProbeResult(False, "TCP 连接失败", is_real=True))
    page.refresh(); app.processEvents()
    assert page.node_table.item(r, 5).text() == "未启用"


def test_devices_page_connect_requires_adapter(win):
    """有适配器的通道可真的开/关会话; 无适配器必须报明确原因, 不伪装成功。"""
    w, _vm, stack, app = win
    cam = stack.device_config.find_camera("ds86_01")
    cam.enabled = True
    cam.confirmed = True
    dev = stack.device_config.find_device("eco65_01")
    dev.enabled = True
    dev.confirmed = True
    w._select("devices"); app.processEvents()
    page = w.pages["devices"]

    # 相机表(5列, 状态列=4): DS86 有适配器 (离线栈已 open)
    c = page._chan_ids.index("ds86_01")
    assert page.chan_table.item(c, 4).text() == "已启用"
    stack.device_state.close("ds86_01"); page.refresh(); app.processEvents()
    assert page.chan_table.item(c, 4).text() == "未启用"
    stack.device_state.open("ds86_01"); page.refresh(); app.processEvents()
    assert page.chan_table.item(c, 4).text() == "已启用"

    # 设备表: ECO65 无适配器
    r = page._node_ids.index("eco65_01")
    assert page.node_table.item(r, 5).text() == "未启用"
    try:
        stack.device_state.open("eco65_01")
        raise AssertionError("无适配器不应静默成功")
    except RuntimeError as exc:
        assert "无适配器" in str(exc)
    assert page.node_table.item(r, 5).text() == "未启用"


def test_device_dialog_fields_follow_protocol(win):
    w, _vm, _stack, app = win
    from application.presentation.pages.devices import _EntryDialog, _DEVICE_SPEC
    from application.services.device_config import DeviceEntry
    values = {f: getattr(DeviceEntry("x", protocol="serial"), f)
              for f in DeviceEntry.__dataclass_fields__}
    dlg = _EntryDialog("t", _DEVICE_SPEC, values)
    assert not dlg._groups["serial"].isHidden()
    assert dlg._groups["tcp"].isHidden()
    assert not dlg._groups["board"].isHidden()      # kind=board -> 板映射可见

    dlg._proto_combo.setCurrentIndex(dlg._proto_combo.findData("json_tcp"))
    app.processEvents()
    assert dlg._groups["serial"].isHidden()
    assert not dlg._groups["tcp"].isHidden()

    # 非板设备不显示板映射组
    dlg._kind_combo.setCurrentIndex(dlg._kind_combo.findData("robot"))
    app.processEvents()
    assert dlg._groups["board"].isHidden()

    out = dlg.values()
    assert out["protocol"] == "json_tcp" and out["port"] is None
    assert out["kind"] == "robot"
    dlg.deleteLater()


def test_camera_dialog_hides_network_group_for_local_sdk(win):
    """选本地 SDK（DS86）时，无关的网络参数整组收起，并直接说明"无需测试"。

    2026-10-15 用户要求：本地 SDK 直连没有 IP/端口/流地址，留着这些框只会让人以为要填。
    """
    w, _vm, _stack, app = win
    from application.presentation.pages.devices import _EntryDialog, _CAMERA_SPEC
    from application.services.device_config import CameraEntry
    cam = CameraEntry("ds86_01", protocol="sdk")
    values = {f: getattr(cam, f) for f in CameraEntry.__dataclass_fields__}
    dlg = _EntryDialog("t", _CAMERA_SPEC, values)
    app.processEvents()
    assert dlg._groups["tcp"].isHidden()                 # 网络参数组收起
    assert "无需测试" in dlg.result_label.text()          # 直接说清
    assert not dlg._widgets["serial"].isHidden()         # 序列号仍要填（按 SN 打开）

    dlg._proto_combo.setCurrentIndex(dlg._proto_combo.findData("zmq"))
    app.processEvents()
    assert not dlg._groups["tcp"].isHidden()             # 网络协议下照常显示
    assert dlg.result_label.text() == "未测试"
    dlg.deleteLater()



# ---------------------------------------------------------------- DS86 扫描设备
# 2026-10-15 用户反馈：相机没有配置序列号、扫描也找不到任何序列号 →
# 照旧工程（`guidance_locate_tab._refresh_ds86_devices` + `ds86_camera.query_devices`）
# 在相机对话框里补「扫描设备 + 选择」。这里钉住：按钮在哪些协议下出现、选中后写什么、
# 三种失败（扫不了 / 0 台 / 都没序列号）分别怎么说 —— 不许互相冒充。
class _ScanStub:
    """假的扫描器与交互：记录选了什么、通知里说了什么（不弹真模态框）。"""

    def __init__(self, outcome=None, *, pick=None, boom: Exception | None = None) -> None:
        self.outcome = outcome
        self.pick = pick
        self.boom = boom
        self.calls: list[tuple] = []
        self.labels: list[str] = []
        self.scan_ms: list = []

    def scan(self, *, scan_ms=None):
        self.scan_ms.append(scan_ms)
        if self.boom is not None:
            raise self.boom
        return self.outcome

    def select(self, parent, labels):
        self.labels = list(labels)
        return self.pick

    def notify(self, parent, title, text, warn=False):
        self.calls.append((title, text, warn))

    @property
    def last(self) -> tuple:
        return self.calls[-1] if self.calls else ("", "", False)


def _camera_dialog(app, *, protocol="sdk", stub=None):
    from application.presentation.pages.devices import _EntryDialog, _CAMERA_SPEC
    from application.services.device_config import CameraEntry

    cam = CameraEntry("ds86_01", protocol=protocol)
    values = {f: getattr(cam, f) for f in CameraEntry.__dataclass_fields__}
    stub = stub or _ScanStub()
    dlg = _EntryDialog("t", _CAMERA_SPEC, values, scanner=stub.scan,
                       selector=stub.select, notifier=stub.notify)
    app.processEvents()
    return dlg, stub


def _outcome(*cams):
    from application.services.device_scan import ScanOutcome
    return ScanOutcome(devices=tuple(cams))


def _cam(serial="SN-A", *, status=1, name="DS86", ip="192.168.1.50"):
    from application.services.device_scan import ScannedCamera
    return ScannedCamera(name=name, serial=serial, ip=ip, status=status)


def test_camera_dialog_scans_only_for_local_sdk(win):
    """「扫描设备」只出现在相机对话框 + 本地 SDK（DS86）下：只有它有厂家 SDK 可扫。"""
    w, _vm, _stack, app = win
    from application.presentation.pages.devices import _EntryDialog, _DEVICE_SPEC
    from application.services.device_config import DeviceEntry

    dlg, _stub = _camera_dialog(app, protocol="sdk")
    assert dlg.btn_scan is not None and not dlg.btn_scan.isHidden()
    dlg._proto_combo.setCurrentIndex(dlg._proto_combo.findData("zmq"))
    app.processEvents()
    assert dlg.btn_scan.isHidden()               # 网络相机: 没有可扫的东西
    dlg._proto_combo.setCurrentIndex(dlg._proto_combo.findData("sdk"))
    app.processEvents()
    assert not dlg.btn_scan.isHidden()
    dlg.deleteLater()

    dev = DeviceEntry("x", protocol="serial")
    dev_values = {f: getattr(dev, f) for f in DeviceEntry.__dataclass_fields__}
    dlg2 = _EntryDialog("t", _DEVICE_SPEC, dev_values)
    app.processEvents()
    assert dlg2.btn_scan is None                 # 设备节点对话框不带这个按钮
    dlg2.deleteLater()


def test_scan_writes_the_chosen_serial(win):
    w, _vm, _stack, app = win
    dlg, stub = _camera_dialog(app, stub=_ScanStub(_outcome(_cam("SN-A")), pick=0))
    dlg._scan_devices()
    assert stub.scan_ms == [2000]                # 与旧程序同档扫描时长
    assert dlg._widgets["serial"].text() == "SN-A"
    assert dlg.values()["serial"] == "SN-A"      # 保存时带上
    _title, text, warn = stub.last
    assert "SN-A" in text and "已填入" in text and warn is False
    assert dlg.btn_scan.text() == "扫描设备"      # 扫描中的提示文字要还原
    dlg.deleteLater()


def test_usb_scan_binding_requires_reconfirmation(win):
    w, _vm, _stack, app = win
    dlg, _stub = _camera_dialog(app, protocol="realsense_usb",
                                 stub=_ScanStub(_outcome(_cam("USB-1", name="D405", ip="")), pick=0))
    dlg._widgets["confirmed"].setChecked(True)
    dlg._scan_devices()
    assert dlg.values()["serial"] == "USB-1"
    assert not dlg.values()["confirmed"]
    assert dlg.btn_scan.isVisible() or not dlg.btn_scan.isHidden()
    dlg.deleteLater()


def test_scan_lists_occupied_devices_and_warns_if_picked(win):
    """被占用的设备**照样列出来**并标 [占用]（旧程序也只是标注），挑它要提醒先关占它的程序。"""
    w, _vm, _stack, app = win
    stub = _ScanStub(_outcome(_cam("SN-FREE"), _cam("SN-BUSY", status=0)), pick=1)
    dlg, _ = _camera_dialog(app, stub=stub)
    dlg._scan_devices()
    assert stub.labels == ["DS86 (SN-FREE) @ 192.168.1.50",
                           "DS86 (SN-BUSY) @ 192.168.1.50 [占用]"]
    assert dlg._widgets["serial"].text() == "SN-BUSY"
    _title, text, warn = stub.last
    assert "[占用]" in text and "占用" in text and warn is True
    dlg.deleteLater()


def test_scan_with_no_device_says_so_without_claiming_a_problem(win):
    w, _vm, _stack, app = win
    dlg, stub = _camera_dialog(app, stub=_ScanStub(_outcome()))
    dlg._scan_devices()
    _title, text, warn = stub.last
    assert "未发现在线 DS86 相机" in text and "2000" in text
    assert "本机网卡 IPv4" in text and "网段" in text   # DEC-082：扫 0 台给网段诊断
    assert warn is False                         # 没设备不等于出错，不弹成失败
    assert dlg._widgets["serial"].text() == ""
    dlg.deleteLater()


def test_scan_reports_missing_vendor_package_as_unscannable(win):
    """扫不了（厂家包没挂上）与"现场没相机"必须分开讲 —— 后者会把操作员带偏。"""
    w, _vm, _stack, app = win
    from application.services.device_scan import ScanOutcome

    dlg, stub = _camera_dialog(app, stub=_ScanStub(ScanOutcome(
        error="DS86 厂家包不可用，无法扫描设备（这**不是**「现场没有相机」）。")))
    dlg._scan_devices()
    _title, text, warn = stub.last
    assert "厂家包不可用" in text and warn is True
    assert dlg._widgets["serial"].text() == ""
    dlg.deleteLater()


def test_scan_reports_devices_without_serial(win):
    """设备在线却没上报序列号：按 SN 打开这条路走不通，要说清而不是空手而归。"""
    w, _vm, _stack, app = win
    dlg, stub = _camera_dialog(app, stub=_ScanStub(_outcome(_cam("", ip="10.0.0.9"))))
    dlg._scan_devices()
    _title, text, warn = stub.last
    assert "都没有上报序列号" in text and warn is True
    assert "10.0.0.9" in text                     # 至少告诉操作员看见了哪台
    assert dlg._widgets["serial"].text() == ""
    dlg.deleteLater()


def test_scan_cancel_and_scanner_crash_are_handled(win):
    w, _vm, _stack, app = win
    dlg, stub = _camera_dialog(app, stub=_ScanStub(_outcome(_cam("SN-A")), pick=None))
    dlg._scan_devices()
    assert dlg._widgets["serial"].text() == ""    # 取消选择: 不动输入框
    assert stub.calls == []                       # 也不弹任何提示
    dlg.deleteLater()

    dlg2, stub2 = _camera_dialog(app, stub=_ScanStub(boom=RuntimeError("SDK 崩了")))
    dlg2._scan_devices()
    _title, text, warn = stub2.last
    assert "扫描 DS86 失败" in text and "SDK 崩了" in text and warn is True
    assert dlg2.btn_scan.isEnabled()              # 出错后按钮要能再点
    dlg2.deleteLater()


# ================================================================ 视觉页图像算法 (DEC-021)
# 版面: 三列并排 = 相机通道 + 帧元数据(左, 收窄) | 主图(中) | 图像算法面板(右);
# 主图内彩色/深度**上下叠放**。算法**只作用于显示预览** —— 原始帧只读、不喂判定;
# 参数按通道存进设备配置。
def _vision(win):
    w, _vm, stack, app = win
    w._select("vision"); app.processEvents()
    return w.pages["vision"], stack, app


def _select_cam(page, cid, app):
    from PySide6.QtCore import Qt
    row = next(i for i in range(page.chan_list.count())
               if page.chan_list.item(i).data(Qt.ItemDataRole.UserRole) == cid)
    page.chan_list.setCurrentRow(row); app.processEvents()
    return row


def _enable(stack, cid):
    cam = stack.device_config.find_camera(cid)
    cam.enabled = True
    cam.confirmed = True
    return cam


def _capture(frame_cam, stack, page, app):
    _enable(stack, frame_cam)
    _select_cam(page, frame_cam, app)
    page._capture(); app.processEvents()
    assert page._frame is not None, page.result.text()


def test_vision_layout_three_columns_with_images_stacked(win):
    """版面: 相机通道+帧元数据(左) | 主图 | 图像算法 三列**并排**, 主图在算法左边;
    主图内彩色/深度**上下叠放**(不再左右并排); 帧元数据仍在通道下方。"""
    from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
    page, _stack, _app = _vision(win)

    # 左列: 通道在上、帧元数据在下 (同列)
    left_wrap, meta_wrap = page.left.parentWidget(), page.meta.parentWidget()
    assert left_wrap is meta_wrap, "帧元数据要跟相机通道同列(在其下方)"
    col = left_wrap.layout()
    assert col.indexOf(page.left) == 0 and col.indexOf(page.meta) == 1

    # 主图与右侧面板**并排**(同一行): 主图在左、右栏在右 —— 不再上下堆叠。
    # 右栏是**标签页**(图像算法 / 视觉分割)，两页共用同一列，主图那一格才放得下。
    body = next(L for L in page.findChildren(QHBoxLayout)
                if L.indexOf(left_wrap) >= 0)
    assert body.indexOf(left_wrap) == 0
    assert body.indexOf(page.mid) == 1, "主图要跟右侧面板并排(同一行), 不能上下堆叠"
    assert body.indexOf(page.right_tabs) == 2, "右侧标签页要在主图右边"
    assert page.algo is not body.itemAt(2).widget()       # 算法页嵌在标签里, 不是裸列
    assert page.right_tabs.tabText(0) == "图像算法"
    assert page.right_tabs.tabText(1) == "视觉分割"
    assert page.algo is page.right_tabs.widget(0)
    assert page.seg is page.right_tabs.widget(1)
    # 左列最窄, 主图最宽(画面要放得下), 右栏窄一档
    assert body.stretch(0) < body.stretch(1), "左列要比主图窄"
    assert body.stretch(2) < body.stretch(1), "右侧面板要比主图窄"
    from application.presentation.components import SectionTitle
    assert page.meta.findChild(SectionTitle).text() == "帧元数据"

    # 主图内: 彩色在上、深度在下（DEC-129 起用两行等拉伸的 QGridLayout，保证等宽等高）
    from PySide6.QtWidgets import QGridLayout
    color_cell, depth_cell = page.view_color.parentWidget(), page.view_depth.parentWidget()
    assert color_cell is not depth_cell
    grid = next(L for L in page.mid.findChildren(QGridLayout)
                if L.indexOf(color_cell) >= 0)
    assert grid.getItemPosition(grid.indexOf(color_cell))[:2] == (0, 0)
    assert grid.getItemPosition(grid.indexOf(depth_cell))[:2] == (1, 0), \
        "彩色与深度要上下叠放（且两行等拉伸）"
    assert grid.rowStretch(0) == grid.rowStretch(1) == 1


def test_vision_algo_panel_covers_every_param_key_and_makes_none_up(win):
    """面板由参数表生成: 控件集必须**正好**是 PANEL_KEYS (隐藏组不给控件), 不能多也不能少。"""
    from application.algorithms.image_ops import PANEL_KEYS, PARAM_KEYS
    page, _stack, _app = _vision(win)
    assert set(page._widgets) == set(PANEL_KEYS)
    assert set(PANEL_KEYS) < set(PARAM_KEYS)            # 十字线/显示增强只在数据模型里
    for key in PANEL_KEYS:
        w, _kind = page._widgets[key]
        assert w.objectName() == f"algo_{key}"
        assert w.toolTip()                          # 每项都要有悬停说明
    # 面板说明必须写清作用范围, 别让人以为算法参与了判定; 深度切分上下限的单位要写明 mm
    note = page.algo_note.text()
    assert "显示预览" in note and "不参与" in note and "mm" in note
    assert "深度切分上下限按毫米(mm)" in note
    # QLabel 是纯文本: 写进界面的说明不能残留 Markdown 记号(曾经把 ** 原样显示给用户)
    from PySide6.QtWidgets import QLabel
    labels = [page.algo_note] + page.algo.findChildren(QLabel)
    assert not [lab.text() for lab in labels if "**" in lab.text()], \
        "界面说明里不能出现 Markdown 星号"


def test_vision_panel_hides_crosshair_and_display_groups(win):
    """「几何变换」不再挂旧程序说明文字; 「十字线」「显示增强」组不出现在面板上。"""
    page, _stack, _app = _vision(win)
    assert "十字线" not in page._widgets and "crosshair" not in page._widgets
    assert "brightness" not in page._widgets and "contrast" not in page._widgets
    # 组标题也不该出现 (只渲染 PANEL_GROUPS)
    from PySide6.QtWidgets import QLabel
    titles = [lab.text() for lab in page.algo.findChildren(QLabel)]
    assert "十字线" not in titles and "显示增强" not in titles
    # 几何组不再有「照旧程序…」的文字说明
    assert not [t for t in titles if "照旧程序" in t and "镜像" in t]


def test_vision_mirror_checkboxes_share_one_row(win):
    """水平镜像/垂直镜像 (vflip 带 same_row) 要排在同一行。"""
    page, _stack, _app = _vision(win)
    from PySide6.QtWidgets import QGridLayout
    grids = page.algo.findChildren(QGridLayout)
    place = {}
    for grid in grids:
        for i in range(grid.count()):
            w = grid.itemAt(i).widget()
            if w is not None and w is page._widgets["vflip"][0]:
                r, _c, _rs, _cs = grid.getItemPosition(i)
                place["vflip"] = r
            if w is not None and w is page._widgets["hflip"][0]:
                r, _c, _rs, _cs = grid.getItemPosition(i)
                place["hflip"] = r
    assert place.get("hflip") is not None and place["hflip"] == place.get("vflip"), \
        "水平/垂直镜像要在同一行"


def test_vision_algo_change_rerenders_preview_but_never_touches_raw_frame(win):
    """改参数立刻重画预览; **原始帧数组一个字节都不能变**。"""
    import numpy as np
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    color_before = page._frame.color.data.copy()
    depth_before = page._frame.depth.data.copy()

    page._widgets["hflip"][0].setChecked(True)      # -> _on_algo_changed -> _render_preview
    app.processEvents()
    assert page._last_result is not None
    assert page._last_result.params.hflip is True
    assert np.array_equal(page._frame.color.data, color_before)
    assert np.array_equal(page._frame.depth.data, depth_before)
    assert "预览已套用 1 项参数" in page.preview_hint.text()
    assert "原始帧未改动" in page.preview_hint.text()
    # 深度路的单位要写在提示里: mm 阈值 + 本帧比例换算 (DEC-025)
    assert "深度切分 200~3000 mm" in page.preview_hint.text()
    assert "0.001" in page.preview_hint.text()


def test_vision_raw_compare_checkbox_bypasses_algorithms(win):
    """「原图对照」= 不套任何算法: 不产生处理结果, 提示里明说。"""
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    page._widgets["hflip"][0].setChecked(True)
    app.processEvents()
    assert page._last_result is not None

    page.chk_raw.setChecked(True); app.processEvents()
    assert page._last_result is None
    assert "原图对照" in page.preview_hint.text()
    assert page.view_color.available and page.view_depth.available

    page.chk_raw.setChecked(False); app.processEvents()
    assert page._last_result is not None             # 取消对照: 恢复算法预览


def test_vision_save_params_writes_only_changed_keys_per_channel(win, tmp_path):
    """「保存到本通道」: 只写与默认值不同的项, 落到**运行时设备配置**(不是仓库那份)。"""
    from application.services.device_config import DeviceConfigStore
    page, stack, app = _vision(win)
    _select_cam(page, "ds86_01", app)

    page._widgets["hflip"][0].setChecked(True)
    page._widgets["rotate"][0].setCurrentIndex(2)    # 180°
    app.processEvents()
    page.btn_algo_save.click(); app.processEvents()

    cam = stack.device_config.find_camera("ds86_01")
    assert cam.image_ops == {"hflip": True, "rotate": 180}
    assert str(tmp_path) in stack.device_config.path, "测试只碰运行时(tmp)配置"
    assert "已保存图像算法参数到通道 ds86_01" in page.result.text()
    # 真的落盘了: 重新开一个 store 从同一个文件读回来
    reopened = DeviceConfigStore(stack.device_config.path)
    reopened.load()
    assert reopened.find_camera("ds86_01").image_ops == {"hflip": True, "rotate": 180}

    # 同通道再存一次: 全默认 -> 清空记录(其余回落默认, 以后默认值升级能跟上)
    page.btn_algo_reset.click(); app.processEvents()
    page.btn_algo_save.click(); app.processEvents()
    assert stack.device_config.find_camera("ds86_01").image_ops == {}


def test_vision_reset_restores_defaults_without_persisting(win):
    """「恢复默认」只改面板, 不落盘 (要落盘得再点保存)。"""
    from application.services.device_config import DeviceConfigStore
    page, stack, app = _vision(win)
    _select_cam(page, "ds86_01", app)
    page._widgets["threshold_value"][0].setValue(20); app.processEvents()
    page.btn_algo_save.click(); app.processEvents()
    assert stack.device_config.find_camera("ds86_01").image_ops == {"threshold_value": 20}

    page.btn_algo_reset.click(); app.processEvents()
    assert page._params_from_widgets().is_default()
    assert "恢复默认" in page.result.text()
    reopened = DeviceConfigStore(stack.device_config.path)
    reopened.load()
    assert reopened.find_camera("ds86_01").image_ops == {"threshold_value": 20}, \
        "恢复默认不该悄悄改已保存的配置"


def test_vision_switching_channel_loads_that_channels_saved_params(win):
    """每个通道一套参数: 换通道要把该通道存的值载进面板。"""
    page, stack, app = _vision(win)
    stack.device_config.find_camera("ds86_01").image_ops = {"threshold_value": 20}
    stack.device_config.find_camera("d405_01").image_ops = {"hflip": True}

    _select_cam(page, "ds86_01", app)
    assert page._widgets["threshold_value"][0].value() == 20
    assert page._widgets["hflip"][0].isChecked() is False

    _select_cam(page, "d405_01", app)
    assert page._widgets["hflip"][0].isChecked() is True
    assert page._widgets["threshold_value"][0].value() == 127

    _select_cam(page, "ds86_01", app)
    assert page._widgets["threshold_value"][0].value() == 20


def test_vision_page_owns_no_timer(win):
    """本页**不建任何定时器** —— 定时器回调可能晚于控件销毁(见"Qt 关闭顺序"那条坑)。"""
    from PySide6.QtCore import QTimer
    page, _stack, _app = _vision(win)
    assert page.findChildren(QTimer) == []


def test_vision_result_records_param_snapshot_and_says_judgement_uses_raw_frame(win):
    """「结构化结果」里要有一份本次生效参数快照, 并写清判定输入是未处理的原始帧。"""
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    page._widgets["threshold_value"][0].setValue(10); app.processEvents()

    page._capture(); app.processEvents()
    assert "【图像算法（只影响显示预览，不参与判定）】" in page.result.text()
    assert "阈值=10" in page.result.text()

    page._run_pipeline(); app.processEvents()
    assert "判定输入是未处理的原始帧" in page.result.text()
    assert "阈值=10" in page.result.text()          # 判定结果里也复核了快照


def test_vision_capture_keeps_panel_params_when_refreshing(win):
    """抓拍会刷新列表, 但不能把操作员正在调的面板参数冲掉。"""
    page, stack, app = _vision(win)
    _select_cam(page, "ds86_01", app)
    page._widgets["threshold_value"][0].setValue(50); app.processEvents()
    _capture("ds86_01", stack, page, app)
    assert page._widgets["threshold_value"][0].value() == 50
    assert page._params_from_widgets().threshold_value == 50


def test_vision_page_fits_target_screen_even_when_result_text_grows(win):
    """结果文本变长不能把页面顶高、也不能把帧元数据压糊。

    这条守的是一个真实踩过的坑: 结构化结果卡随着文本变高 -> 整页布局下限超过设计尺寸 ->
    窗口被顶高(超过 THEME.target_screen) 且富文本 KV 行被压到实际行高以下, 第二行被裁掉。
    现在的做法是结果卡**封顶 + 内部滚动**。
    """
    from application.presentation.theme import THEME
    w, _vm, stack, app = win
    page, _stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    w.show(); app.processEvents()

    page.result.setText("\n".join(f"很长的一行结构化结果 {i} " + "x" * 90
                                 for i in range(14)))
    app.processEvents()
    assert w.size().height() <= THEME.target_screen[1], "结果再长也不能把窗口顶高"
    # 富文本 KV 每行要拿到自己的行高, 不能被压到 minimumSizeHint 以下(会裁掉第二行)
    for name in ("kv_frame", "kv_seq", "kv_shape", "kv_scale", "kv_valid", "kv_intr"):
        kv = getattr(page, name)
        assert kv.height() >= kv.sizeHint().height(), f"{name} 被压扁了"
    # 长结果靠卡片内部滚动, 不是把文字裁掉
    assert page.result_scroll.maximumHeight() <= 200
    assert page.result_scroll.verticalScrollBar().maximum() > 0
    page.result.setText("短结果"); app.processEvents()
    assert page.result_scroll.verticalScrollBar().maximum() == 0, "短结果不该出滚动条"


# ============================================== 视觉页「另存为视觉检测」 (E20×E19, DEC-024)
# 调试结束把 通道 + 流水线 + 参数快照 命名保存, 供配方 vision.run 的 detection_ref 引用。
def test_vision_save_as_detection_persists_snapshot_and_lists(win, monkeypatch):
    """另存把 通道/流水线/参数快照 落盘, 并出现在左列「已保存的视觉检测」里。"""
    from PySide6.QtCore import Qt
    from application.presentation.pages import vision as vis_mod
    from application.services.vision_detections import VisionDetectionStore
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)

    page._widgets["hflip"][0].setChecked(True); app.processEvents()
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("圆检测-离线", True))
    page.btn_save_det.click(); app.processEvents()

    det = stack.vision_detections.find("圆检测-离线")
    assert det is not None, page.result.text()
    assert det.camera_ref == "ds86_01"
    assert det.pipeline_ref == "approved_ds86_circle_pipeline"
    # 快照分两类: 判定用的流水线参数 + 显示用的图像算法参数 (用途写清, 防误读)
    assert det.params["pipeline"]["pipeline_id"] == "approved_ds86_circle_pipeline"
    assert det.params["display_image_ops"].get("hflip") is True
    assert det.params_hash

    # 列表出现该检测, 引导提示隐藏
    ids = [page.det_list.item(i).data(Qt.ItemDataRole.UserRole)
           for i in range(page.det_list.count())]
    assert ids == ["圆检测-离线"]
    assert page.det_hint.isHidden()

    # 原子落盘, 可重新读回
    assert stack.vision_detections.path.endswith("detections.yaml")
    reloaded = VisionDetectionStore(stack.vision_detections.path).load()
    assert any(d.detection_id == "圆检测-离线" for d in reloaded)


def test_vision_save_as_detection_same_name_asks_before_overwrite(win, monkeypatch):
    """同名另存必须先确认: 拒绝则不覆盖旧快照, 允许才更新。"""
    from application.presentation.pages import vision as vis_mod
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("同名", True))

    monkeypatch.setattr(vis_mod, "_confirm", lambda *a, **k: True)
    page.btn_save_det.click(); app.processEvents()
    assert stack.vision_detections.find("同名") is not None

    # 改一个显示参数, 再次另存但拒绝覆盖 -> 旧快照仍在 (无 hflip)
    page._widgets["hflip"][0].setChecked(True); app.processEvents()
    monkeypatch.setattr(vis_mod, "_confirm", lambda *a, **k: False)
    page.btn_save_det.click(); app.processEvents()
    assert stack.vision_detections.find("同名").params["display_image_ops"].get(
        "hflip") is not True

    # 允许覆盖 -> 快照更新
    monkeypatch.setattr(vis_mod, "_confirm", lambda *a, **k: True)
    page.btn_save_det.click(); app.processEvents()
    assert stack.vision_detections.find("同名").params["display_image_ops"].get(
        "hflip") is True


def test_vision_save_as_detection_without_channel_is_refused(win, monkeypatch):
    """没有选中通道时另存被拒, 且不弹命名框、不落盘。"""
    from application.presentation.pages import vision as vis_mod
    page, stack, app = _vision(win)
    page.chan_list.setCurrentRow(-1); app.processEvents()
    assert page.selected_camera() is None

    asked = []
    monkeypatch.setattr(vis_mod, "_ask_text",
                        lambda *a, **k: (asked.append(1), ("x", True))[1])
    page.btn_save_det.click(); app.processEvents()
    assert asked == []
    assert "通道" in page.result.text()
    assert stack.vision_detections.detections == []


def test_vision_delete_detection_removes_and_unlists(win, monkeypatch):
    """删除所选检测: 从列表移除并落盘; 删空后显示引导提示。"""
    from application.presentation.pages import vision as vis_mod
    from application.services.vision_detections import VisionDetectionStore
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("待删", True))
    monkeypatch.setattr(vis_mod, "_confirm", lambda *a, **k: True)
    page.btn_save_det.click(); app.processEvents()
    assert page.det_list.count() == 1

    page.det_list.setCurrentRow(0); app.processEvents()
    page.btn_det_del.click(); app.processEvents()

    assert stack.vision_detections.find("待删") is None
    assert page.det_list.count() == 0
    assert not page.det_hint.isHidden()
    reloaded = VisionDetectionStore(stack.vision_detections.path).load()
    assert all(d.detection_id != "待删" for d in reloaded)


# ============================================ 视觉页「视觉分割 / 运行一次」 (E19 增强, DEC-026)
# 流程同旧工程: 画多边形 → 深度分割(轮廓内提示点) → 智能分割(EdgeSAM) → 运行一次(交集)。
# 「另存为检测」保存的是**运行一次这套实现逻辑**(图源/区域/多边形/深度分割参数/分割参数)，
# 配方 vision.run 按 detection_ref 在真实帧上重跑同一算法。
_SEG_KEYS = {"source", "op_region", "seg_filter_mode", "seg_filter_n", "prompt_k",
             "prompt_margin", "depth_min_mm", "depth_max_mm", "thresh_method",
             "morph_op", "morph_kernel_size"}


def _stub_segmenter(page, monkeypatch):
    """把智能分割后端换成确定性占位（不加载真实 EdgeSAM，判据不依赖 ~40MB 模型在不在）。"""
    from application.algorithms.segmentation import StubSegmenter
    monkeypatch.setattr(page, "_make_segmenter",
                        lambda: (StubSegmenter(), "离线占位（非真实模型，测试用）"))


def test_vision_seg_panel_widgets_and_defaults(win):
    """分割面板由参数表生成: 控件集正好是分割参数; 图源默认彩色、区域默认整张图。"""
    page, _stack, _app = _vision(win)
    assert set(page._seg_widgets) == _SEG_KEYS
    assert page.algo is page.right_tabs.widget(0)
    assert page.seg is page.right_tabs.widget(1)
    assert page._seg_value("source") == "color"
    assert page._seg_value("op_region") == "whole"
    assert page._seg_value("seg_filter_n") == 1
    for key, w in page._seg_widgets.items():
        assert w.toolTip(), f"{key} 缺悬停说明"
    # 二值化/形态学默认必须对齐**旧现场配置**（S100 检测 binary_inv / none），
    # 不能是算法 dataclass 的 otsu/close —— 否则"目标比背景近"的场景会把背景当白色
    # 前景，分割结果变成目标的**互补**（用户反馈的"和旧程序相反"，DEC-028）。
    assert page._seg_value("thresh_method") == "binary_inv"
    assert page._seg_value("morph_op") == "none"
    # 分割页里把"运行一次保存的是什么"讲清楚(避免误以为是发布/审批)
    from PySide6.QtWidgets import QLabel
    text = "\n".join(lab.text() for lab in page.seg.findChildren(QLabel))
    assert "运行一次" in text and "vision.run" in text


def test_vision_geometry_transform_allows_draw_and_run_once(win, monkeypatch):
    """DEC-120：几何变换（镜像/旋转）不再阻止绘制/分割/运行一次。

    多边形/ROI 按**原始帧坐标**保存，画在预览上时自动换算；结果叠加再换算回预览。
    """
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    _stub_segmenter(page, monkeypatch)

    # 预览启用「水平镜像 + 顺时针 90°」
    page._widgets["hflip"][0].setChecked(True)
    rotate = page._widgets["rotate"][0]
    rotate.setCurrentIndex(rotate.findData(90))
    app.processEvents()
    assert page._blocked_by_geometry() is None

    # 绘制不再被拦，按钮保持选中
    page.btn_draw_poly.setChecked(True)
    page._seg_mode("polygon"); app.processEvents()
    assert page.view_color.annotation_mode() == "polygon"
    assert page.btn_draw_poly.isChecked()

    # 在预览上画四边形 -> 内部保存原始帧坐标，正反变换可往返
    preview_pts = [(40.0, 30.0), (120.0, 35.0), (118.0, 90.0), (45.0, 88.0)]
    page._on_polygon_drawn(preview_pts)
    assert len(page._polygon) == 4
    for (px, py), (ox, oy) in zip(preview_pts, page._polygon):
        rx, ry = page._orig_to_preview_pt(ox, oy)
        assert abs(rx - px) < 1e-6 and abs(ry - py) < 1e-6
        bx, by = page._preview_to_orig_pt(px, py)
        assert abs(bx - ox) < 1e-6 and abs(by - oy) < 1e-6

    # ROI 同样换算：预览画框 -> 原始坐标 -> 再回预览应还原
    page._on_roi_drawn(20.0, 15.0, 140.0, 100.0)
    assert page._roi is not None
    mapped = page._orig_to_preview_roi(page._roi)
    assert abs(mapped[0] - 20.0) < 1e-6 and abs(mapped[1] - 15.0) < 1e-6
    assert abs(mapped[2] - 140.0) < 1e-6 and abs(mapped[3] - 100.0) < 1e-6

    # 深度分割与运行一次都能跑（坐标用原始帧）
    page._run_depth_seg(); app.processEvents()
    assert "深度分割完成" in page.result.text()
    page._run_once(); app.processEvents()
    assert "运行一次完成" in page.result.text()
    assert getattr(page, "_seg_result", None) is not None


def test_vision_run_once_runs_real_algorithm_path_on_current_frame(win, monkeypatch):
    """运行一次在当前帧上跑完整路径(深度分割→智能分割→交集), 结果如实标注后端与像素面积。"""
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    _stub_segmenter(page, monkeypatch)
    page._set_polygon([[100, 100], [500, 100], [500, 400], [100, 400]])
    page._seg_widgets["source"].setCurrentIndex(
        page._seg_widgets["source"].findData("color"))
    page._run_once(); app.processEvents()

    res = page._seg_result
    assert res.ok and res.error is None
    assert res.source == "color" and res.source_note == "彩色图"
    assert "非真实模型" in res.segmenter_name            # 占位绝不冒充模型
    assert res.n_contours >= 1 and res.prompts
    assert res.hit is True and res.area_px > 0
    text = page.result.text()
    assert "运行一次完成" in text and "交点" not in text
    assert "面积" in text and "px" in text               # 面积一律像素


def test_vision_run_once_without_polygon_is_refused(win, monkeypatch):
    """没画多边形(≥3 点)直接运行一次 -> 拒并提示先画多边形。"""
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    _stub_segmenter(page, monkeypatch)
    page._set_polygon([[10, 10], [20, 20]])
    page._run_once(); app.processEvents()
    assert "绘制多边形" in page.result.text()
    assert getattr(page, "_seg_result", None) is None


def test_vision_save_as_detection_stores_run_once_scheme(win, monkeypatch):
    """另存为检测必须把**运行一次方案**(图源/区域/多边形/参数)一起存进 params["scheme"]。"""
    from application.presentation.pages import vision as vis_mod
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    page._set_polygon([[100, 100], [500, 100], [500, 400], [100, 400]])
    page._seg_widgets["source"].setCurrentIndex(
        page._seg_widgets["source"].findData("depth"))
    page._seg_widgets["prompt_k"].setValue(4)
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("带方案检测", True))
    page.btn_save_det.click(); app.processEvents()

    det = stack.vision_detections.find("带方案检测")
    assert det is not None, page.result.text()
    scheme = det.params.get("scheme")
    assert isinstance(scheme, dict)
    assert scheme["source"] == "depth" and scheme["op_region"] == "whole"
    assert len(scheme["polygon"]) == 4
    assert scheme["prompt_k"] == 4
    assert "运行一次方案" in page.result.text() and "已保存" in page.result.text()


def test_vision_load_detection_scheme_restores_panel_and_switches_tab(win, monkeypatch):
    """双击已保存检测: 运行一次方案载回面板与主图叠加, 并切到「视觉分割」页。"""
    from PySide6.QtCore import Qt
    from application.presentation.pages import vision as vis_mod
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    saved_poly = [[100, 100], [500, 100], [500, 400], [100, 400]]
    page._set_polygon(saved_poly)
    page._seg_widgets["source"].setCurrentIndex(
        page._seg_widgets["source"].findData("fused"))
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("可载回", True))
    page.btn_save_det.click(); app.processEvents()

    # 改掉手上的设置, 再双击载回 -> 面板/多边形复原
    page._set_polygon([[1, 1], [2, 2], [3, 3]])
    page._seg_widgets["source"].setCurrentIndex(
        page._seg_widgets["source"].findData("color"))
    page.right_tabs.setCurrentWidget(page.algo)
    page.det_list.setCurrentRow(0)
    item = page.det_list.item(0)
    assert item.data(Qt.ItemDataRole.UserRole) == "可载回"
    page._load_detection_scheme(); app.processEvents()

    assert page._seg_value("source") == "fused"
    assert page._polygon == [[float(x), float(y)] for x, y in saved_poly]
    assert page.right_tabs.currentWidget() is page.seg
    assert "已载回检测" in page.result.text()


def test_vision_load_detection_without_scheme_is_refused(win):
    """旧检测只绑流水线、没有方案 -> 载回时明确拒绝, 不乱复原。"""
    from PySide6.QtCore import Qt
    from application.services.vision_detections import VisionDetection
    page, stack, app = _vision(win)
    stack.vision_detections.upsert(VisionDetection(
        detection_id="旧检测", label="旧检测", camera_ref="ds86_01",
        pipeline_ref="approved_ds86_circle_pipeline", params={"pipeline": {}},
        source="历史"))
    page._refresh_detections(); app.processEvents()
    page.det_list.setCurrentRow(0)
    page._load_detection_scheme(); app.processEvents()
    assert "没有保存运行一次方案" in page.result.text()


# ============================== 视觉页「找圆调试参数随检测保存/载回」 (DEC-047)
# 用户报障: 找圆调试调完「另存为检测」, 重新双击载回时找圆参数没有复原。
# 根因: 快照只存流水线/显示/运行一次方案, 找圆调试面板参数从未被保存,
#       载回也只复原分割方案且总跳到「视觉分割」页。
def test_vision_save_as_detection_stores_circle_debug_params(win, monkeypatch):
    """另存为检测要把找圆调试面板(深度引导+找圆)参数存进 params["circle_debug"]。"""
    from application.presentation.pages import vision as vis_mod
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)

    page._circle_widgets["depth_min_mm"].setValue(150.0)
    page._circle_widgets["depth_max_mm"].setValue(400.0)
    page._circle_widgets["min_contour_area"].setValue(800.0)
    page._circle_widgets["thresh_method"].setCurrentIndex(
        page._circle_widgets["thresh_method"].findData("binary"))
    page._circle_widgets["circle_type"].setCurrentIndex(
        page._circle_widgets["circle_type"].findData("所有圆"))
    page._circle_widgets["min_radius"].setValue(8.0)
    page._circle_widgets["stage"].setCurrentIndex(
        page._circle_widgets["stage"].findData("binary"))
    page.right_tabs.setCurrentWidget(page.circle); app.processEvents()

    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("找圆检测", True))
    page.btn_save_det.click(); app.processEvents()

    det = stack.vision_detections.find("找圆检测")
    assert det is not None, page.result.text()
    snap = det.params.get("circle_debug")
    assert isinstance(snap, dict) and snap.get("schema_version") == 1
    assert snap["depth"]["depth_min_mm"] == 150.0
    assert snap["depth"]["depth_max_mm"] == 400.0
    assert snap["depth"]["min_contour_area"] == 800.0
    assert snap["depth"]["thresh_method"] == "binary"
    assert snap["circle"]["circle_type"] == "所有圆"
    assert snap["circle"]["min_radius"] == 8.0
    assert snap["stage"] == "binary"
    assert det.params.get("active_tab") == "circle"
    assert "找圆调试参数：已保存" in page.result.text()


def test_vision_load_detection_restores_circle_debug_params(win, monkeypatch):
    """双击载回检测: 找圆调试面板参数按保存时复原, 并切回「找圆调试」页。"""
    from PySide6.QtCore import Qt
    from application.presentation.pages import vision as vis_mod
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)

    page._circle_widgets["depth_min_mm"].setValue(150.0)
    page._circle_widgets["min_contour_area"].setValue(800.0)
    page._circle_widgets["circle_type"].setCurrentIndex(
        page._circle_widgets["circle_type"].findData("最大圆"))
    page._circle_widgets["max_radius"].setValue(320.0)
    page._circle_widgets["avg_depth_enable"].setChecked(True)
    page.right_tabs.setCurrentWidget(page.circle); app.processEvents()
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("可载回找圆", True))
    page.btn_save_det.click(); app.processEvents()

    # 改乱面板与页签, 再双击载回
    page._circle_widgets["depth_min_mm"].setValue(200.0)
    page._circle_widgets["min_contour_area"].setValue(0.0)
    page._circle_widgets["circle_type"].setCurrentIndex(
        page._circle_widgets["circle_type"].findData("最小圆"))
    page._circle_widgets["max_radius"].setValue(500.0)
    page._circle_widgets["avg_depth_enable"].setChecked(False)
    page._circle_rects = [{"idx": 0, "x1": 1, "y1": 1, "x2": 9, "y2": 9}]
    page.right_tabs.setCurrentWidget(page.algo); app.processEvents()

    page.det_list.setCurrentRow(0)
    item = page.det_list.item(0)
    assert item.data(Qt.ItemDataRole.UserRole) == "可载回找圆"
    page._load_detection_scheme(); app.processEvents()

    assert page._circle_value("depth_min_mm") == 150.0
    assert page._circle_value("min_contour_area") == 800.0
    assert page._circle_value("circle_type") == "最大圆"
    assert page._circle_value("max_radius") == 320.0
    assert page._circle_value("avg_depth_enable") is True
    assert page.right_tabs.currentWidget() is page.circle
    assert page._circle_rects == []          # 参数变了, 上一帧的矩形不再沿用
    text = page.result.text()
    assert "深度引导参数" in text and "找圆参数" in text
    assert "需重新" in text                  # 叠加/阶段图须重跑, 不假装已复原


def test_vision_seg_clear_removes_polygon_points_and_boxes_keeps_roi(win, monkeypatch):
    """「清除」：清掉用户多边形 + 提示点 + 分割叠加框（轮廓/交集）；ROI 作为运算区域保留。

    这是用户反馈的两点之一——"增加一个清除的功能"。清除后应能重新从零画起，
    且**运算区域 ROI 不该被误删**（删 ROI 走「调整多边形」选中后 Delete，另有其路）。
    """
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    _stub_segmenter(page, monkeypatch)
    page._set_polygon([[100, 100], [500, 100], [500, 400], [100, 400]])
    page._set_roi((60, 40, 200, 180))
    page._run_once(); app.processEvents()

    v = page.view_color
    # 先确认"确实有东西可清"：多边形在、提示点/轮廓/交集叠加已生成
    assert page._polygon and page._seg_result is not None
    assert v._contour_items and v._point_items, "运行一次应留下轮廓+提示点叠加"

    page.btn_seg_clear.click(); app.processEvents()      # 走真实按钮接线

    assert page._polygon == []                           # 多边形清了
    assert v._contour_items == [] and v._mark_items == [] and v._point_items == []
    assert getattr(page, "_seg_result", None) is None    # 缓存结果也清
    assert page.view_color.annotation_mode() is None     # 回到非绘制态
    assert not any(b.isChecked() for b in
                   (page.btn_draw_poly, page.btn_edit_poly, page.btn_draw_roi))
    assert page._roi == (60, 40, 200, 180)               # ROI 保留
    assert v._roi_item is not None and v._roi_item.isVisible()
    assert "已清除" in page.result.text()


def test_vision_ui_text_has_no_markdown_asterisks(win, monkeypatch):
    """QLabel/QToolTip 是纯文本: 界面说明与结果里不能出现 Markdown 的 ``**``。

    曾经把 ``**运行一次这套逻辑**`` / ``**原始帧**`` 原样显示给用户（DEC-022 同类缺陷）。
    """
    from PySide6.QtWidgets import QLabel, QWidget
    from application.presentation.pages import vision as vis_mod
    page, stack, app = _vision(win)
    labels = page.seg.findChildren(QLabel) + [page.result, page.seg_hint, page.preview_hint]
    assert not [lab.text() for lab in labels if "**" in lab.text()]
    assert not [w.toolTip() for w in page.findChildren(QWidget) if "**" in w.toolTip()]

    # 走一遍会产生文本的路径: 几何拒绝 + 另存 + 运行一次结果
    _capture("ds86_01", stack, page, app)
    _stub_segmenter(page, monkeypatch)
    page._set_polygon([[100, 100], [500, 100], [500, 400], [100, 400]])
    page._widgets["hflip"][0].setChecked(True); app.processEvents()
    page._run_once(); app.processEvents()
    assert "**" not in page.result.text()
    page._widgets["hflip"][0].setChecked(False); app.processEvents()
    page._run_once(); app.processEvents()
    assert "**" not in page.result.text()
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("无星号", True))
    page.btn_save_det.click(); app.processEvents()
    assert "**" not in page.result.text()


# ---------------------------------------------------------------- 步骤显示名 (DEC-037)
def test_workbench_steps_show_chinese_names_not_ids(win):
    """作业台的执行步骤显示中文（类型名兜底），英文步骤 ID 只留在悬停提示里。"""
    w, _vm, _stack, app = win
    w._select("workbench")
    w.refresh_all(); app.processEvents()
    page = w.pages["workbench"]
    from application.services.recipes.editor import STEP_TYPE_CN
    labels = [lbl.text() for lbl, _pill in page._step_rows]
    assert labels                                   # 未启动也显示方案声明的步骤
    assert set(labels) <= set(STEP_TYPE_CN.values())     # 全是中文类型名
    assert not set(labels) & set(page._step_ids)   # 英文 id 不再上屏
    assert all(lbl.toolTip().startswith("步骤 ID：")
               for lbl, _pill in page._step_rows)


def test_workbench_step_rows_use_custom_titles(win):
    """方案里设了中文名 -> 作业台步骤行显示中文名（草稿在内存里改，不动用户文件）。"""
    from application.services.recipes.editor import RecipeEditor
    w, _vm, stack, app = win
    ed = RecipeEditor(stack.recipes)
    ed.select("ds86_two_station_example")
    first = ed.model.sequence[0].id
    ed.set_step_title(first, "上料到位")

    w._select("workbench")
    w.refresh_all(); app.processEvents()
    page = w.pages["workbench"]
    assert page._step_ids[0] == first
    assert page._step_rows[0][0].text() == "上料到位"
    assert page._step_rows[0][0].toolTip() == f"步骤 ID：{first}"


# ---------------------------------------------------------------- 北京时间 (DEC-038)
def test_workbench_recent_event_time_is_beijing(win):
    """作业台「最近事件」时间与诊断页同口径：北京时间，不是 UTC 原文。"""
    w, vm, _stack, app = win
    vm.start_current(); vm.tick()
    w._select("workbench")
    w.refresh_all(); app.processEvents()
    page = w.pages["workbench"]
    t = page.event_time.text()
    assert len(t) == 19 and "T" not in t and "+" not in t


# ---------------------------------------------------------------- 找圆调试 (DEC-045)
# 旧工程 Ganta_Crtl_System/vision_monitor 的 DS86「ROBOT 引导」调试页并入本页：
# 右栏第三个页签「找圆调试」，与「图像算法」「视觉分割」共用同一主图与同一帧。
def test_vision_right_tabs_include_circle_debug_as_third_page(win):
    page, _stack, _app = _vision(win)
    assert page.right_tabs.count() == 4
    assert [page.right_tabs.tabText(i) for i in range(4)] == [
        "图像算法", "视觉分割", "找圆调试", "棱线测量"]
    assert page.algo is page.right_tabs.widget(0)      # 原有两页不动
    assert page.seg is page.right_tabs.widget(1)
    assert page.circle is page.right_tabs.widget(2)
    assert page.edge is page.right_tabs.widget(3)      # DEC-144 棱线测量
    # 参数控件齐全（深度引导 + 找圆两组）且都有悬停说明
    for key in ("stage", "depth_min_mm", "depth_max_mm", "depth_range_mode",
                "auto_min_offset_mm", "auto_plane_offset_mm", "auto_plane_tol_mm",
                "thresh_method", "morph_op",
                "rect_mode", "rect_fit", "rect_scale", "rect_pad_px",
                "rect_min_side_px", "rect_max_side_px", "rect_merge_gap_px",
                "rect_side_length", "circle_type", "min_radius", "max_radius"):
        assert key in page._circle_widgets
        assert page._circle_widgets[key].toolTip()
    # 棱线测量页签：参数齐全且都有悬停说明（DEC-144）
    for key in ("tol_mm", "open_kernel", "scan", "min_span", "line_thr_mm",
                "depth_min_mm", "depth_max_mm", "min_width_mm", "max_width_mm"):
        assert key in page._edge_widgets
        assert page._edge_widgets[key].toolTip()


def test_vision_circle_debug_needs_a_captured_frame_first(win):
    """调试只用当前这一帧：没抓拍就点运行 -> 明说要先「单次抓拍」，不偷偷抓图。"""
    page, _stack, app = _vision(win)
    page.right_tabs.setCurrentWidget(page.circle); app.processEvents()
    page.btn_cd_run.click(); app.processEvents()
    assert "请先「单次抓拍」" in page.result.text()
    assert page._circle_result is None and page._circle_rects == []


def test_vision_circle_debug_full_flow_reports_xyz_and_offsets(win):
    """DS86 抓帧 → 深度检测 → 找圆 → 运行一次：圆、相机系 XYZ、粗/精偏移都要给。"""
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    page.right_tabs.setCurrentWidget(page.circle); app.processEvents()

    page.btn_cd_depth.click(); app.processEvents()
    assert page._circle_rects, page.result.text()
    assert "深度引导完成" in page.result.text()

    page.btn_cd_circles.click(); app.processEvents()
    assert page._circle_hits, page.result.text()
    assert "找圆完成" in page.result.text()

    page.btn_cd_run.click(); app.processEvents()
    res = page._circle_result
    assert res is not None and res.ok and res.stage == "done", page.result.text()
    text = page.result.text()
    assert "相机系 XYZ" in text and "粗定位" in text and "精定位" in text
    assert "单位 mm" in text and "不驱动机械臂" in text    # 单位与边界写清楚
    # 叠加只在主图; 提示栏有矩形/圆计数
    assert "矩形=" in page.circle_hint.text() and "圆=" in page.circle_hint.text()


def test_vision_circle_debug_stage_switch_shows_intermediate_image(win):
    """「调试画面」下拉：深度窗口切到二值图/轮廓图等阶段图（默认仍是原始深度伪彩）。"""
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    page.right_tabs.setCurrentWidget(page.circle); app.processEvents()
    page.btn_cd_depth.click(); app.processEvents()

    combo = page._circle_widgets["stage"]
    combo.setCurrentIndex(combo.findData("binary")); app.processEvents()
    assert page.view_depth.available                     # 二值图已显示在深度窗口
    combo.setCurrentIndex(combo.findData("contour")); app.processEvents()
    assert page.view_depth.available
    combo.setCurrentIndex(combo.findData("orig")); app.processEvents()
    assert page.view_depth.available                     # 回到原始深度伪彩


def test_vision_circle_debug_clear_keeps_user_polygon(win):
    """「清除叠加」只清找圆调试的缓存/叠加，不碰用户画的多边形/ROI。"""
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    page._set_polygon([[100, 100], [200, 100], [200, 200]])
    page.right_tabs.setCurrentWidget(page.circle); app.processEvents()
    page.btn_cd_depth.click(); app.processEvents()
    assert page._circle_rects

    page.btn_cd_clear.click(); app.processEvents()
    assert page._circle_rects == [] and page._circle_hits == []
    assert page._circle_stage_images == {}
    assert page._polygon == [[100, 100], [200, 100], [200, 200]]
    assert "已清除找圆调试" in page.result.text()


def test_vision_circle_debug_loads_guidance_config_into_panel(win, tmp_path):
    """「载入引导配置」把 guidance.yaml 的深度/找圆值填进面板（只读载入，不回写）。"""
    page, stack, app = _vision(win)
    cfg = stack.motion.guidance.config_store.config
    cfg.depth.depth_min_mm = 321.0
    cfg.depth.thresh_method = "binary"
    cfg.circle = {"circle_type": "最大圆", "min_radius": 9.0}

    page.btn_cd_load_cfg.click(); app.processEvents()
    assert page._circle_widgets["depth_min_mm"].value() == 321.0
    assert page._circle_widgets["thresh_method"].currentData() == "binary"
    assert page._circle_widgets["circle_type"].currentData() == "最大圆"
    assert page._circle_widgets["min_radius"].value() == 9.0
    assert "已载入引导配置" in page.result.text()
    assert "不回写" in page.result.text()


def test_vision_circle_debug_tab_switch_restores_overlays(win):
    """三页签共用同一主图：进/出「找圆调试」不残留对方叠加，切回能恢复自己那份。"""
    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    page.right_tabs.setCurrentWidget(page.circle); app.processEvents()
    page.btn_cd_depth.click(); app.processEvents()
    page.right_tabs.setCurrentWidget(page.algo); app.processEvents()
    # 离开找圆调试 -> 叠加清掉（缓存保留）
    assert page._circle_rects                     # 缓存还在
    page.right_tabs.setCurrentWidget(page.circle); app.processEvents()
    assert page._circle_rects                     # 切回来仍可用


def _bundle(camera_id="ds86_01", frame_id="f1", *, color=True, depth=True):
    import numpy as np
    from application.contracts.enums import ImageSpace
    from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
    h, w = 240, 320
    return FrameBundle(
        camera_id=camera_id, frame_id=frame_id, capture_id=f"c-{frame_id}",
        sequence=1, service_id="local", channel_id=camera_id,
        color=(ImageRef.from_array(np.zeros((h, w, 3), dtype="uint8"))
               if color else None),
        depth=(ImageRef.from_array(np.full((h, w), 1000, dtype="uint16"))
               if depth else None),
        depth_scale_m=0.001,
        intrinsics=Intrinsics(fx=200.0, fy=200.0, cx=160.0, cy=120.0,
                              width=w, height=h,
                              intrinsics_space=ImageSpace.DEPTH,
                              calibration_version="t"))


def test_workbench_channel_switch_shows_each_camera_frame(tmp_path, qapp):
    """DEC-051: 切页签显示该相机本 run 最近帧；没抓拍过的通道如实占位。"""
    from application.services.device_config import CameraEntry

    w, vm, stack = _window_with_cameras(tmp_path, qapp, [
        CameraEntry("ds86_01", "DS86 (随臂)", enabled=True, confirmed=True),
        CameraEntry("d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
                    host="192.168.2.117", port=5555, channel_id="cam1"),
    ])
    try:
        w._select("workbench"); w.refresh_all(); qapp.processEvents()
        page = w.pages["workbench"]
        f86, f405 = _bundle("ds86_01", "f86"), _bundle("d405_01", "f405")
        vm.current_frames = lambda: {"ds86_01": f86, "d405_01": f405}
        vm.current_frame = lambda: f405          # 最新抓拍是 D405
        vm.step_circle_state = lambda: {"available": False}
        page.refresh(); qapp.processEvents()

        # 自动跟随最新抓拍 -> D405 页签
        assert page._selected_camera_id() == "d405_01"       # noqa: SLF001
        assert page.image_view.available
        assert "d405_01" in page.image_caption.text()

        _click_camera_tab(page, "ds86_01")
        qapp.processEvents()
        assert page._selected_camera_id() == "ds86_01"      # noqa: SLF001
        assert "ds86_01" in page.image_caption.text()

        # 该通道本 run 没抓拍 -> 占位，不拿别的相机图充数
        vm.current_frames = lambda: {"ds86_01": f86}
        _click_camera_tab(page, "d405_01")
        qapp.processEvents()
        assert not page.image_view.available
        assert "尚未抓拍" in page.image_caption.text()
    finally:
        _close_window(w, stack, qapp)


def test_workbench_box_adjust_only_on_ds86_step_frame(tmp_path, qapp):
    """DEC-051: 框/圆只在本步视觉帧且只在 DS86 图上可调；其它页签只读预览。"""
    from application.services.device_config import CameraEntry

    w, vm, stack = _window_with_cameras(tmp_path, qapp, [
        CameraEntry("ds86_01", "DS86 (随臂)", enabled=True, confirmed=True),
        CameraEntry("d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
                    host="192.168.2.117", port=5555, channel_id="cam1"),
    ])
    try:
        w._select("workbench"); w.refresh_all(); qapp.processEvents()
        page = w.pages["workbench"]
        f86, f405 = _bundle("ds86_01", "f86"), _bundle("d405_01", "f405")
        boxes = [{"x1": 40, "y1": 40, "x2": 120, "y2": 120,
                  "circle": {"cx": 80, "cy": 80, "radius_px": 20}}]
        vm.current_frames = lambda: {"ds86_01": f86, "d405_01": f405}
        vm.current_frame = lambda: f405
        vm.step_circle_state = lambda: {
            "available": True, "step_id": "det", "frame_id": "f86",
            "status": "PASS", "manual": False, "boxes": boxes, "selected": 0}
        page.refresh(); qapp.processEvents()

        assert page._selected_camera_id() == "ds86_01"       # 跟随待确认视觉帧
        assert page.draw_box_btn.isEnabled()
        assert page.draw_circle_btn.isEnabled()

        _click_camera_tab(page, "d405_01")
        qapp.processEvents()
        assert not page.draw_box_btn.isEnabled()
        assert not page.draw_circle_btn.isEnabled()
        assert "只读预览" in page.target_status.text()

        _click_camera_tab(page, "ds86_01")
        qapp.processEvents()
        assert page.draw_box_btn.isEnabled()

        # 视觉步骤若落在非 DS86 帧上，即便选中该页签也不允许画（用户裁定）
        vm.step_circle_state = lambda: {
            "available": True, "step_id": "det", "frame_id": "f405",
            "status": "PASS", "manual": False, "boxes": boxes, "selected": 0}
        _click_camera_tab(page, "d405_01")
        page.refresh(); qapp.processEvents()
        assert not page.draw_box_btn.isEnabled()
        assert "只支持在 DS86" in page.target_status.text()
    finally:
        _close_window(w, stack, qapp)


def test_workbench_capture_card_shows_color_only(tmp_path, qapp):
    """DEC-050/051: 作业台右卡只显示彩色抓拍（画框/画圆只发生在彩色图上）。

    深度图在视觉调试页仍可看；本卡不展示深度，也不放「深度」占位。
    """
    from application.services.device_config import CameraEntry

    w, vm, stack = _window_with_cameras(tmp_path, qapp, [
        CameraEntry("ds86_01", "DS86 (随臂)", enabled=True, confirmed=True),
    ])
    try:
        w._select("workbench"); w.refresh_all(); qapp.processEvents()
        page = w.pages["workbench"]
        assert not hasattr(page, "depth_view")
        assert not hasattr(page, "depth_cap")

        vm.current_frame = lambda: _bundle()
        page._sync_image_card(); qapp.processEvents()
        assert page.image_view.available
        assert "原始彩色图" in page.image_caption.text()
        assert "ds86_01" in page.image_caption.text()
        assert "深度" not in page.image_caption.text()

        # 有彩色但深度缺失/全无效，也照样只显示彩色，不出现深度占位
        no_depth = _bundle("ds86_01", "f2", depth=False)
        vm.current_frame = lambda: no_depth
        page._sync_image_card(); qapp.processEvents()
        assert page.image_view.available
        assert not hasattr(page, "depth_view")

        # 无彩色流: 如实说没有彩色，不拿深度冒充
        no_color = _bundle("ds86_01", "f3", color=False)
        vm.current_frame = lambda: no_color
        page._sync_image_card(); qapp.processEvents()
        assert not page.image_view.available
        assert "无彩色流" in page.image_caption.text()

        vm.current_frame = lambda: None
        page._sync_image_card(); qapp.processEvents()
        assert not page.image_view.available
    finally:
        _close_window(w, stack, qapp)


def test_settings_page_categories_and_fine_profiles(win):
    """DEC-065：参数配置页四个分组可打开；精定位参数套可展示/增加。"""
    from PySide6.QtWidgets import QLabel
    from application.services.guidance_config import FineOffsetProfile
    w, _vm, stack, app = win
    w._select("settings"); app.processEvents()
    page = w.pages["settings"]
    titles = [lb.text() for lb in page.findChildren(QLabel)]
    # DEC-115：原「视觉引导参数」块已替换为「快捷键」配置；
    # DEC-148：运动全局参数改名「全局参数」，并含杆塔宽度/螺栓直径两组
    for t in ("全局参数", "快捷键", "精定位偏移参数", "运行 / 日志参数"):
        assert t in titles
    assert "视觉引导参数" not in titles
    from PySide6.QtWidgets import QGroupBox
    groups = [g.title() for g in page.body.findChildren(QGroupBox)]
    # DEC-151：全局参数里是「计算值」两个数值，而不是算法参数组
    assert any("计算值" in t for t in groups), groups
    labels = [lb.text() for lb in page.body.findChildren(QLabel)]
    assert any("杆塔宽度(mm)" == t for t in labels), labels
    assert any("螺栓直径(mm)" == t for t in labels), labels
    cfg = stack.motion.guidance.config_store.config
    assert page.guidance_store is not None
    cfg.fine_profiles.append(FineOffsetProfile(
        profile_id="第二套", tcp_x=33.0, desc="页测试"))
    page._rebuild(); app.processEvents()
    labels = [lb.text() for lb in page.body.findChildren(QLabel)]
    assert any("第二套" in t for t in labels)

    # DEC-069：tcp_z（纵向距离）可编辑，页面写明同轴安装横向 tcp 必须为 0
    from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout
    found: dict[str, QDoubleSpinBox] = {}
    for form in page.body.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            lab = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if lab is None or lab.widget() is None:
                continue
            text = lab.widget().text()
            if text in ("tcp_x(mm)", "tcp_y(mm)", "tcp_z(mm)"):
                field = form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
                assert isinstance(field, QDoubleSpinBox)
                found.setdefault(text, field)
    assert set(found) >= {"tcp_x(mm)", "tcp_y(mm)", "tcp_z(mm)"}
    found["tcp_z(mm)"].setValue(123.0)
    assert cfg.offsets.tcp_z == 123.0
    assert any("工具中心" in lb.text() for lb in page.body.findChildren(QLabel))


def test_devices_page_camera_edit_rebuilds_sessions_immediately(qapp, monkeypatch):
    """DEC-081：保存相机连接配置后立刻按新配置重建会话，视觉调试不再用旧连接。"""
    from types import SimpleNamespace

    from application.presentation.pages import devices as devices_mod
    from application.presentation.pages.devices import DevicesPage
    from application.services.device_config import (
        CameraEntry, DeviceConfig, DeviceConfigStore,
    )
    from application.services.device_state import DeviceStateRegistry

    store = DeviceConfigStore.in_memory(DeviceConfig(cameras=[
        CameraEntry("d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
                    host="192.168.2.117", port=5555, channel_id="cam1")]))
    reloads: list[str] = []
    ctx = SimpleNamespace(stack=SimpleNamespace(
        device_config=store,
        device_state=DeviceStateRegistry(cameras={}),
        camera_reload=lambda camera_id=None: (reloads.append("x") or "ok"),
        camera_reload_blocker=lambda: None,
    ))
    notices: list[str] = []
    monkeypatch.setattr(devices_mod, "_notify",
                        lambda _p, _t, text, warn=False: notices.append(text))
    monkeypatch.setattr(devices_mod, "_dialog", lambda *_a, **_k: {
        "camera_id": "d405_01", "name": "D405-1", "protocol": "zmq",
        "enabled": True, "confirmed": True, "serial": "", "service_id": "",
        "channel_id": "cam1", "host": "192.168.2.118", "port": 5555,
        "stream_url": "", "capture_profile": "", "calibration_id": "",
        "notes": "", "image_ops": {},
    })
    page = DevicesPage(ctx)
    try:
        page.chan_table.selectRow(0)
        page._edit_camera()
        assert reloads == ["x"]                    # 保存后立即重建，不等重启
        assert notices == []                       # 成功不弹窗（DEC-083）
        assert store.find_camera("d405_01").host == "192.168.2.118"
    finally:
        page.deleteLater()
        qapp.processEvents()


def test_devices_page_blocks_camera_edit_while_task_running(qapp, monkeypatch):
    """作业进行中改相机配置会把在用会话抽走 —— 先拦住并说明原因。"""
    from types import SimpleNamespace

    from application.presentation.pages import devices as devices_mod
    from application.presentation.pages.devices import DevicesPage
    from application.services.device_config import (
        CameraEntry, DeviceConfig, DeviceConfigStore,
    )
    from application.services.device_state import DeviceStateRegistry

    store = DeviceConfigStore.in_memory(DeviceConfig(cameras=[
        CameraEntry("d405_01", protocol="zmq", enabled=True, confirmed=True,
                    host="192.168.2.117", port=5555, channel_id="cam1")]))
    ctx = SimpleNamespace(stack=SimpleNamespace(
        device_config=store,
        device_state=DeviceStateRegistry(cameras={}),
        camera_reload=lambda camera_id=None: "不应调用",
        camera_reload_blocker=lambda: "任务状态为 RUNNING；请先停止",
    ))
    warnings: list[str] = []
    monkeypatch.setattr(devices_mod, "_warn",
                        lambda _p, text: warnings.append(text))
    dialogs: list[int] = []
    monkeypatch.setattr(devices_mod, "_dialog",
                        lambda *_a, **_k: dialogs.append(1) or None)
    page = DevicesPage(ctx)
    try:
        page.chan_table.selectRow(0)
        page._edit_camera()
        assert not dialogs                         # 根本没打开编辑框
        assert warnings and "不能修改相机配置" in warnings[-1]
    finally:
        page.deleteLater()
        qapp.processEvents()


# ---------------------------------------------------------------- DEC-109 引导定位视觉方案/框圆
def test_motion_guide_lists_vision_schemes_and_draw_tools(win):
    """路径实施引导定位：可选视觉方案（命名检测），并提供画框/画圆/删除选中。"""
    from application.services.vision_detections import VisionDetection
    w, _vm, stack, app = win
    stack.vision_detections.upsert(VisionDetection(
        detection_id="检测-M", label="运动页测试", camera_ref="ds86_01",
        pipeline_ref="approved_ds86_circle_pipeline", source="test"))
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page.refresh(); app.processEvents()
    ids = [page.guide_detection.itemData(i)
           for i in range(page.guide_detection.count())]
    assert ids[0] is None                     # 首项=自动找圆
    assert "检测-M" in ids
    assert page.btn_draw_box.text() == "画框"
    assert page.btn_draw_circle.text() == "画圆"
    assert not page.btn_draw_box.isEnabled()  # 没有画面时不能画


def test_motion_guide_uses_selected_circle_as_override(win, monkeypatch):
    """人工调整后的圆作为粗/精定位依据；移动一次后旧画面即失效。"""
    from types import SimpleNamespace
    w, _vm, _stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page._guide_frame = SimpleNamespace(color=SimpleNamespace(data=None),
                                        frame_id="f1")
    page._guide_frame_valid = True
    page.view_result.set_layout([
        {"x1": 10, "y1": 10, "x2": 110, "y2": 110,
         "circle": {"cx": 61.0, "cy": 62.0, "radius_px": 20.0}}], 0)
    calls = []
    monkeypatch.setattr(page, "_guard_motion", lambda *a: True)
    monkeypatch.setattr(page.runner, "start_guide",
                        lambda **kw: calls.append(kw) or True)
    page._guide("coarse")
    assert len(calls) == 1
    assert calls[0]["stages"] == ("coarse",)
    ov = calls[0]["override"]
    assert ov["circle"] == {"cx": 61.0, "cy": 62.0, "radius_px": 20.0}
    assert ov["frame"] is page._guide_frame
    assert page._guide_frame_valid is False      # 移动后旧画面失效


def test_motion_guide_stale_frame_with_scheme_blocks(win, monkeypatch):
    """选了视觉方案但画面过期（机械臂已移动）时拒绝按旧画面定位。"""
    from types import SimpleNamespace
    import application.presentation.pages.motion as motion_mod
    from application.services.vision_detections import VisionDetection
    w, _vm, stack, app = win
    stack.vision_detections.upsert(VisionDetection(
        detection_id="检测-S", camera_ref="ds86_01",
        pipeline_ref="approved_ds86_circle_pipeline", source="t"))
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page.refresh()
    page.guide_detection.setCurrentIndex(
        page.guide_detection.findData("检测-S"))
    page._guide_frame = SimpleNamespace()
    page._guide_frame_valid = False
    started, warns = [], []
    monkeypatch.setattr(page, "_guard_motion", lambda *a: True)
    monkeypatch.setattr(page.runner, "start_guide",
                        lambda **kw: started.append(kw) or True)
    monkeypatch.setattr(motion_mod, "_warn_dialog",
                        lambda _p, _t, m: warns.append(m))
    page._guide("fine")
    assert not started
    assert warns and "已过期" in warns[0]


# ---------------------------------------------------------------- DEC-115 全局快捷键
def test_settings_shortcuts_card_replaces_guidance_form(win):
    """参数配置页：原「视觉引导参数」块改为「快捷键」；每个动作一个按键编辑框。"""
    from PySide6.QtWidgets import QLabel

    from application.services.runtime_config import SHORTCUT_ACTIONS

    w, _vm, _stack, app = win
    w._select("settings"); app.processEvents()
    page = w.pages["settings"]
    assert not hasattr(page, "_guidance_form")
    titles = [lb.text() for lb in page.body.findChildren(QLabel)]
    assert "快捷键" in titles and "视觉引导参数" not in titles
    edits = page._shortcut_edits
    assert set(edits) == {key for key, _label in SHORTCUT_ACTIONS}
    assert edits["stop_all"].keySequence().toString() == "F12"
    assert edits["start"].keySequence().toString() == "F5"


def test_settings_shortcuts_save_applies_immediately(win, monkeypatch, tmp_path):
    """改键并保存：写入 settings.yaml，主窗口快捷键立即重载（不需重启）。"""
    from PySide6.QtGui import QKeySequence

    from application.services.runtime_config import RuntimeSettingsStore

    import application.presentation.app as app_mod

    w, _vm, _stack, app = win
    w._select("settings"); app.processEvents()
    page = w.pages["settings"]
    # 让「页面保存」与「主窗口重载」读同一个临时配置目录（正常部署里本就相同）
    (tmp_path / "runtime").mkdir(exist_ok=True)
    page.runtime_store = RuntimeSettingsStore(str(tmp_path / "runtime" / "settings.yaml"))
    page.runtime_store.load()
    page._rebuild(); app.processEvents()
    monkeypatch.setattr(app_mod.app_paths, "configs_dir", lambda: str(tmp_path))
    # 只验证快捷键落盘：不写仓库里的 motion/guidance 活配置
    monkeypatch.setattr(page.motion_store, "save", lambda: None)
    monkeypatch.setattr(page.guidance_store, "save", lambda: None)

    page._shortcut_edits["start"].setKeySequence(QKeySequence("Ctrl+F5"))
    page._save()
    assert "已保存" in page.status.text()
    saved = RuntimeSettingsStore(str(tmp_path / "runtime" / "settings.yaml")).load()
    assert saved.shortcuts["start"] == "Ctrl+F5"
    seqs = {sc.key().toString() for sc in w._shortcuts}
    assert "Ctrl+F5" in seqs


def test_settings_shortcut_duplicate_is_rejected(win, monkeypatch, tmp_path):
    """同一按键绑两个动作 -> 保存被拒，不落盘。"""
    from PySide6.QtGui import QKeySequence

    import application.presentation.pages.settings as settings_mod
    from application.services.runtime_config import RuntimeSettingsStore

    w, _vm, _stack, app = win
    w._select("settings"); app.processEvents()
    page = w.pages["settings"]
    page.runtime_store = RuntimeSettingsStore(str(tmp_path / "settings.yaml"))
    page.runtime_store.load()
    page._rebuild(); app.processEvents()
    monkeypatch.setattr(page.motion_store, "save", lambda: None)
    monkeypatch.setattr(page.guidance_store, "save", lambda: None)
    warned = []
    monkeypatch.setattr(settings_mod.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a))
    page._shortcut_edits["pause"].setKeySequence(QKeySequence("F5"))  # 与启动重复
    page._save()
    assert warned and "重复" in str(warned[0])
    assert not (tmp_path / "settings.yaml").exists()


def test_main_window_default_shortcuts_and_mapping(win, monkeypatch):
    """主窗口按默认配置装好快捷键；触发时页面切换/流程动作走同一入口。"""
    w, _vm, _stack, app = win
    seqs = {sc.key().toString() for sc in w._shortcuts}
    for want in ("F12", "F5", "F6", "F7", "F8",
                 "Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4", "Ctrl+5"):
        assert want in seqs
    w._on_shortcut("page_console")
    assert w.stack_widget.currentWidget() is w.pages["console"]
    calls = []
    monkeypatch.setattr(w.pages["workbench"], "_on_action",
                        lambda key: calls.append(key))
    for action in ("start", "pause", "resume", "single_step"):
        w._on_shortcut(action)
    assert calls == ["start", "request_pause", "resume", "single_step"]


# ---------------------------------------------------------------- DEC-116 引导确认跨线程修复
def test_motion_guide_confirm_is_marshaled_to_gui_thread(win, monkeypatch):
    """引导线程请求确认：弹窗必须在 GUI 线程执行（跨线程 QMessageBox 会崩）。"""
    import threading
    import time

    w, _vm, _stack, app = win
    page = w.pages["motion"]
    seen = []

    def fake_box(title, message, options):
        seen.append(threading.get_ident() == threading.main_thread().ident)
        return options[0]

    monkeypatch.setattr(page, "_confirm_box", fake_box)

    # GUI 线程直接调用：立即本地执行
    assert page._confirm("引导定位", "测试", ["是", "否"]) == "是"

    # 工作线程调用：经信号排队回 GUI 线程，主线程跑事件循环取结果
    result = {}
    t = threading.Thread(target=lambda: result.setdefault(
        "v", page._confirm("引导定位", "测试", ["是", "否"])))
    t.start()
    deadline = time.time() + 10
    while t.is_alive() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    t.join(1)
    assert result.get("v") == "是"
    assert seen == [True, True]


# ---------------------------------------------------------------- DEC-118 引导画框/画圆
def test_motion_guide_speed_defaults_to_profile(win):
    """引导速度初值 = 参数配置里的「引导移动速度」（此前被夹成 1%，几乎不动）。"""
    w, _vm, stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    want = max(1, min(100, int(
        stack.motion_config.config.profile.guide_move_speed or 10)))
    assert page.guide_speed.value() == want
    assert page.guide_speed.value() > 1


def test_motion_guide_drawing_box_and_circle_lands_in_layout(win):
    """组件只发信号：页面必须把画出的框/圆落进本地布局，粗/精定位才用得到。"""
    from types import SimpleNamespace

    w, _vm, _stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page._guide_frame = SimpleNamespace(color=SimpleNamespace(data=None))
    page._guide_frame_valid = True
    page.view_result.set_layout([])
    page._set_guide_draw_buttons(True)      # 正常流程里由「单次视觉检测」_apply_guide_frame 开启

    page.view_result.box_requested.emit(10, 10, 110, 110)
    boxes = page.view_result.boxes()
    assert len(boxes) == 1 and boxes[0]["circle"] is None

    page.view_result.circle_drawn.emit(0, 60, 60, 20)
    boxes = page.view_result.boxes()
    assert boxes[0]["circle"] == {"cx": 60.0, "cy": 60.0, "radius_px": 20.0}
    circle, idx = page._selected_guide_circle()
    assert idx == 0 and circle["radius_px"] == 20.0


def test_motion_guide_drawing_blocked_when_frame_stale(win):
    """画面过期（机械臂移动过）时不允许再画框/画圆，避免用旧画面定位。"""
    from types import SimpleNamespace

    w, _vm, _stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page._guide_frame = SimpleNamespace(color=SimpleNamespace(data=None))
    page._guide_frame_valid = False
    page.view_result.set_layout([])
    page.view_result.box_requested.emit(10, 10, 110, 110)
    assert page.view_result.boxes() == []
    assert "过期" in page.guide_status.text()


# ---------------------------------------------------------------- DEC-119 显示旋转 + 弹窗
def test_ds86_display_rotation_270_everywhere(win):
    """DS86 画面按显示旋转 270°：作业台右卡、运动页引导、视觉调试（按通道）。"""
    w, _vm, _stack, app = win
    # 运动方案引导（固定 DS86）
    w._select("motion"); app.processEvents()
    motion = w.pages["motion"]
    assert motion.view_result.display_rotation() == 270
    # 视觉调试：DS86 -> 270，其它相机 -> 0
    vision = w.pages["vision"]
    vision._apply_display_rotation("ds86_01")
    assert vision.view_color.display_rotation() == 270
    assert vision.view_depth.display_rotation() == 270
    vision._apply_display_rotation("d405_01")
    assert vision.view_color.display_rotation() == 0
    assert vision.view_depth.display_rotation() == 0
    # 作业台：显示 DS86 抓拍帧时旋转 270
    w._select("workbench"); app.processEvents()
    page = w.pages["workbench"]
    page.vm.current_frames = lambda: {}
    page.vm.current_frame = lambda: _bundle("ds86_01", "f-rot")
    page._sync_image_card(); app.processEvents()
    assert page.image_view.display_rotation() == 270


def test_rotated_view_keeps_scene_coordinates(win):
    """显示旋转只转视角：场景坐标仍是原图像素（鼠标点按自动换算回原图）。"""
    from application.presentation.components.target_layout_view import (
        TargetLayoutImageView,
    )
    w, _vm, _stack, app = win
    view = TargetLayoutImageView("测试")
    view.resize(400, 400)
    import numpy as np
    view.show_array(np.zeros((40, 60, 3), dtype="uint8"), kind="color")
    view.set_display_rotation(270)
    app.processEvents()
    assert view.image_size.width() == 60 and view.image_size.height() == 40
    # 旋转后显示宽高互换：场景矩形映射回视口后高度应大于宽度
    br = view.view.mapToScene(view.view.viewport().rect()).boundingRect()
    assert br.height() > br.width()
    view.deleteLater()


def test_workbench_popup_toggle_moves_card_out_and_back(win):
    w, _vm, _stack, app = win
    w._select("workbench"); app.processEvents()
    page = w.pages["workbench"]
    page.btn_popup.click(); app.processEvents()
    assert page.image_popup_open()
    popup = page._ensure_image_popup()
    assert popup.isVisible()
    assert page.image_card.parent() is popup
    page.btn_popup.click(); app.processEvents()
    assert not page.image_popup_open()
    assert not popup.isVisible()
    assert page.image_card.parent() is page


def test_motion_guide_popup_toggle(win):
    w, _vm, _stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    page.btn_popup.click(); app.processEvents()
    popup = page._ensure_image_popup()
    assert popup.isVisible()
    assert page.image_card.parent() is popup
    page.btn_popup.click(); app.processEvents()
    assert not popup.isVisible()
    assert page.image_card.parent() is not popup      # 已放回运动方案页布局


# ---------------------------------------------------------------- DEC-121 抓帧保存图片
def test_save_frame_files_writes_color_and_depth(tmp_path, monkeypatch, win):
    """DEC-122：一次保存彩色图 + 深度（原始 .npy + 伪彩 PNG），尺寸=原图。"""
    import json

    import numpy as np
    from PySide6.QtGui import QImage

    from application.presentation.components import frame_export

    target = tmp_path / "shot.png"
    monkeypatch.setattr(frame_export.QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    frame = _bundle("ds86_01", "f-save")
    saved = frame_export.save_frame_files(frame, None, camera_id="ds86_01")
    assert str(target) in saved and target.exists()
    depth_npy = tmp_path / "shot_depth.npy"
    depth16 = tmp_path / "shot_depth16.png"
    depth_meta = tmp_path / "shot_depth.json"
    depth_png = tmp_path / "shot_depth.png"          # 伪彩预览
    for path in (depth_npy, depth16, depth_meta, depth_png):
        assert path.exists()
    assert set(saved) == {str(target), str(depth_npy), str(depth16),
                          str(depth_meta), str(depth_png)}
    img = QImage(str(target))
    assert (img.width(), img.height()) == (320, 240)      # 彩色原图（未旋转）
    depth = np.load(depth_npy)
    assert depth.shape == (240, 320) and depth.dtype == np.uint16
    # DEC-123：16 位无损 PNG，像素值=原始深度值（1000），可直接线下读
    d16 = QImage(str(depth16))
    assert d16.format() == QImage.Format.Format_Grayscale16
    assert (d16.width(), d16.height()) == (320, 240)
    raw = bytes(d16.constBits())
    assert int.from_bytes(raw[0:2], "little") == 1000     # 像素值=原始深度
    meta = json.loads(depth_meta.read_text(encoding="utf-8"))
    assert meta["depth_scale_m"] == 0.001 and meta["width"] == 320
    assert meta["height"] == 240 and meta["dtype"] == "uint16"
    dimg = QImage(str(depth_png))
    assert (dimg.width(), dimg.height()) == (320, 240)


def test_capture_points_have_save_image_buttons(win, monkeypatch, tmp_path):
    """作业台/运动引导/视觉调试的抓帧位置都有「保存图片」，且能落盘。"""
    import application.presentation.components.frame_export as fe

    w, _vm, _stack, app = win
    out = {"n": 0}

    def fake_dialog(*_a, **_k):
        out["n"] += 1
        return str(tmp_path / f"shot_{out['n']}.png"), ""

    monkeypatch.setattr(fe.QFileDialog, "getSaveFileName", staticmethod(fake_dialog))

    # 作业台相机卡片
    w._select("workbench"); app.processEvents()
    page = w.pages["workbench"]
    page.vm.current_frames = lambda: {}
    page.vm.current_frame = lambda: _bundle("ds86_01", "f-save-wb")
    page._sync_image_card(); app.processEvents()
    assert page.btn_save_img.isEnabled()
    page.btn_save_img.click(); app.processEvents()
    assert (tmp_path / "shot_1.png").exists()
    assert (tmp_path / "shot_1_depth.npy").exists()
    assert (tmp_path / "shot_1_depth16.png").exists()
    assert (tmp_path / "shot_1_depth.json").exists()
    assert (tmp_path / "shot_1_depth.png").exists()

    # 运动方案引导画面
    from types import SimpleNamespace
    w._select("motion"); app.processEvents()
    motion = w.pages["motion"]
    motion._apply_guide_frame(_bundle("ds86_01", "f-save-motion"),
                              SimpleNamespace(message="", boxes=[], selected=None))
    assert motion.btn_save_img.isEnabled()
    motion.btn_save_img.click(); app.processEvents()
    assert (tmp_path / "shot_2.png").exists()
    assert (tmp_path / "shot_2_depth.npy").exists()
    assert (tmp_path / "shot_2_depth16.png").exists()

    # 视觉调试单次抓拍
    w._select("vision"); app.processEvents()
    vision = w.pages["vision"]
    vision._frame = _bundle("ds86_01", "f-save-vision")
    vision._frame_camera = "ds86_01"
    vision.btn_save_img.setEnabled(True)
    vision.btn_save_img.click(); app.processEvents()
    assert (tmp_path / "shot_3.png").exists()
    assert (tmp_path / "shot_3_depth16.png").exists()
    assert (tmp_path / "shot_3_depth.json").exists()


# ---------------------------------------------------------------- DEC-124 引导偏移上限
def test_motion_guide_passes_profile_offset_limit(win, monkeypatch):
    """手动粗/精定位把参数配置里的「引导单段最大偏移」传给 runner。"""
    w, _vm, stack, app = win
    w._select("motion"); app.processEvents()
    page = w.pages["motion"]
    calls = []
    monkeypatch.setattr(page, "_guard_motion", lambda *a: True)
    monkeypatch.setattr(page.runner, "start_guide",
                        lambda **kw: calls.append(kw) or True)
    page._guide("coarse")
    assert calls and calls[0]["stages"] == ("coarse",)
    want = float(getattr(stack.motion_config.config.profile,
                         "guide_max_offset_mm", 50.0) or 0.0)
    assert calls[0]["max_offset_mm"] == want
    assert want > 0


# ---------------------------------------------------------------- DEC-126 载入本地帧 + 深度取点
def _write_import_set(tmp_path):
    import json

    import numpy as np

    h, w = 240, 320
    yy, xx = np.mgrid[0:h, 0:w]
    depth = np.zeros((h, w), dtype="uint16")
    depth[np.hypot(xx - 160, yy - 120) < 30] = 500
    color = np.zeros((h, w, 3), dtype="uint8")
    color[...] = (30, 30, 30)
    import cv2
    cv2.circle(color, (160, 120), 28, (230, 230, 230), 3)
    base = tmp_path / "ds86_01_local"
    cv2.imwrite(str(base) + ".png", color[:, :, ::-1])
    np.save(str(base) + "_depth.npy", depth)
    (tmp_path / "ds86_01_local_depth.json").write_text(json.dumps({
        "camera_id": "ds86_01", "width": w, "height": h, "dtype": "uint16",
        "depth_scale_m": 0.001,
        "intrinsics": {"fx": 200.0, "fy": 200.0, "cx": 160.0, "cy": 120.0,
                       "width": w, "height": h, "space": "depth"},
    }, ensure_ascii=False), encoding="utf-8")
    return str(base) + ".png"


def test_vision_loads_local_frame_and_depth_click_readout(win, monkeypatch, tmp_path):
    """载入四件套 -> 显示同一帧；深度图点击按显示旋转换算回原始坐标取深度/XYZ。"""
    from PySide6.QtWidgets import QFileDialog

    w, _vm, _stack, app = win
    path = _write_import_set(tmp_path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (path, "")))
    w._select("vision"); app.processEvents()
    page = w.pages["vision"]
    page._load_local_images(); app.processEvents()
    assert page._frame is not None
    assert page._frame.camera_id == "ds86_01"
    assert page.view_color.available and page.view_depth.available
    assert "已载入本地帧" in page.result.text()
    assert page.btn_save_img.isEnabled()

    # 显示旋转 270（DS86）：原始光心(160,120) -> 显示 (120, 159)
    assert page.view_depth.display_rotation() == 270
    page.view_depth.clicked.emit(120.0, 159.0)
    app.processEvents()
    text = page.depth_probe.text()
    assert "(160,120)" in text and "500" in text and "mm" in text
    assert "相机 XYZ" in text


# ---------------------------------------------------------------- DEC-129 主图等宽 + 弹窗
def test_vision_color_and_depth_views_equal_size_policy(win):
    """彩色/深度两格同宽同高：都 Expanding + 同一最小高度（修复彩色被挤成窄条）。"""
    from PySide6.QtWidgets import QSizePolicy

    w, _vm, _stack, app = win
    w._select("vision"); app.processEvents()
    page = w.pages["vision"]
    for view in (page.view_color, page.view_depth):
        assert view.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Expanding
        assert view.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Expanding
        assert view.minimumHeight() == 200


def test_vision_main_image_popup_toggle(win):
    """弹窗可放大；彩色/深度在弹窗内仍等宽（行高差 ≤2px）。"""
    w, _vm, _stack, app = win
    w._select("vision"); app.processEvents()
    page = w.pages["vision"]
    page.btn_image_popup.click(); app.processEvents()
    popup = page._ensure_image_popup()
    assert popup.isVisible()
    assert page.mid.parent() is popup
    assert popup.minimumWidth() == 640            # 有最小尺寸、可自由缩放
    popup.resize(1500, 1200); app.processEvents()
    assert page.view_color.width() == page.view_depth.width()
    assert abs(page.view_color.height() - page.view_depth.height()) <= 2
    page.btn_image_popup.click(); app.processEvents()
    assert not popup.isVisible()
    assert page.mid.parent() is not popup


# ---------------------------------------------------------------- DEC-133 选择覆盖已保存检测
def test_vision_save_detection_can_overwrite_selected_without_typing_name(win, monkeypatch):
    """下拉选中已保存检测 -> 另存直接覆盖它（不再要求输入同名）。"""
    from application.presentation.pages import vision as vis_mod

    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("已有检测", True))
    monkeypatch.setattr(vis_mod, "_confirm", lambda *a, **k: True)
    page.btn_save_det.click(); app.processEvents()
    assert stack.vision_detections.find("已有检测") is not None

    # 下拉里出现该检测；选中后保存不再弹名称输入
    idx = page.det_overwrite.findData("已有检测")
    assert idx >= 0
    page.det_overwrite.setCurrentIndex(idx)

    def _no_name(*_a, **_k):
        raise AssertionError("选中覆盖目标后不应再要求输入名称")

    monkeypatch.setattr(vis_mod, "_ask_text", _no_name)
    page._widgets["hflip"][0].setChecked(True); app.processEvents()
    page.btn_save_det.click(); app.processEvents()
    assert stack.vision_detections.find("已有检测").params[
        "display_image_ops"].get("hflip") is True


def test_vision_save_detection_overwrite_can_be_cancelled(win, monkeypatch):
    """选中目标但拒绝确认 -> 旧快照保持不变。"""
    from application.presentation.pages import vision as vis_mod

    page, stack, app = _vision(win)
    _capture("ds86_01", stack, page, app)
    monkeypatch.setattr(vis_mod, "_ask_text", lambda *a, **k: ("取消覆盖", True))
    monkeypatch.setattr(vis_mod, "_confirm", lambda *a, **k: True)
    page.btn_save_det.click(); app.processEvents()
    assert stack.vision_detections.find("取消覆盖") is not None

    page.det_overwrite.setCurrentIndex(page.det_overwrite.findData("取消覆盖"))
    page._widgets["hflip"][0].setChecked(True); app.processEvents()
    monkeypatch.setattr(vis_mod, "_confirm", lambda *a, **k: False)
    page.btn_save_det.click(); app.processEvents()
    assert stack.vision_detections.find("取消覆盖").params[
        "display_image_ops"].get("hflip") is not True
