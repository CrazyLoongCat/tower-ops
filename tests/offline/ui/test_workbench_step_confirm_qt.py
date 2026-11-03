# -*- coding: utf-8 -*-
"""DEC-042 作业台：步骤确认弹框 + 右卡抓拍图/手动检测圆（离线 Qt 用例）。

覆盖：
- ViewModel：启动即带步骤确认闸；确认/拒绝/继续重询问；待确认视觉步骤可取
  抓拍帧与检测圆状态，并按手动圆重算判定；跨 run 的旧帧/旧结果不显示；
- 组件/页面：确认弹框非模态且等待期间关不掉；右卡显示抓拍原图并叠加可拖动
  检测圆；页面把拖动结果提交给 ViewModel。

无 PySide6 时整模块跳过；关闭顺序按本工程惯例（定时器→窗口→deleteLater→事件）。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from application.bootstrap.composition import build_offline_stack  # noqa: E402
from application.contracts.enums import TaskState  # noqa: E402
from application.presentation.viewmodel import WorkbenchViewModel  # noqa: E402

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _stack(tmp_path):
    return build_offline_stack(db_path=str(tmp_path / "r.db"),
                               runtime_dir=str(tmp_path), recipe_path=RECIPE)


def _vision_recipe(stack, rid="ui_confirm_offline"):
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "cap", "type": "camera.capture", "camera_ref": "ds86_01"},
            {"id": "det", "type": "vision.run",
             "input_from": {"step": "cap", "output": "frame"},
             "pipeline_ref": "approved_ds86_circle_pipeline"},
        ],
    })
    return rid


def _new_vm(stack):
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    return vm


def _tick_until_prompt(vm, *, step_id=None, limit=60):
    """推进一步直到出现（指定步骤的）确认提示。"""
    for _ in range(limit):
        st = vm.tick()
        if st in ("COMPLETED", "FAULT", "WAITING_OPERATOR"):
            return None, st
        p = vm.pending_step_prompt()
        if p and (step_id is None or p.get("step_id") == step_id):
            return p, st
    return None, vm.tick()


# ---------------------------------------------------------------- ViewModel
def test_vm_start_prompts_confirm_after_every_step(tmp_path):
    stack = _stack(tmp_path)
    vm = _new_vm(stack)
    vm.select_recipe("ds86_two_station_example")
    vm.start_current()

    seen = []
    st = None
    for _ in range(200):
        st = vm.tick()
        if st in ("COMPLETED", "FAULT", "WAITING_OPERATOR"):
            break
        p = vm.pending_step_prompt()
        if p:
            seen.append(p["step_id"])
            assert vm.confirm_step("确认", "张三") is True
    assert st == "COMPLETED"
    assert seen == ["move", "capture", "detect", "gate", "mechanism", "evidence"]
    assert vm.pending_step_prompt() is None
    stack.close()


def test_vm_confirm_rejects_undeclared_choice(tmp_path):
    stack = _stack(tmp_path)
    vm = _new_vm(stack)
    vm.select_recipe("ds86_two_station_example")
    vm.start_current()
    assert _tick_until_prompt(vm)[0] is not None
    assert vm.confirm_step("是", "张三") is False
    assert vm.hint_tone == "bad"
    assert vm.pending_step_prompt() is not None          # 提示仍在等
    assert vm.confirm_step("确认", "张三") is True
    stack.close()


def test_vm_reject_pauses_then_resume_moves_to_next_step(tmp_path):
    stack = _stack(tmp_path)
    rid = _vision_recipe(stack)
    vm = _new_vm(stack)
    vm.select_recipe(rid)
    vm.start_current()
    vm.tick()
    p = vm.pending_step_prompt()
    assert p and p["step_id"] == "cap"
    assert vm.confirm_step("不正确", "张三") is True
    assert vm.tick() == "PAUSED"                        # 安全边界暂停, 非故障
    assert not vm.buttons()["request_pause"]
    assert vm.buttons()["resume"]
    vm.resume(True)
    assert vm.state().task_state == "RUNNING"
    vm.tick()                                            # DEC-102: 放行到下一步
    p2 = vm.pending_step_prompt()
    assert p2 and p2["step_id"] == "det"                 # 不再重问 cap
    stack.close()


def test_vm_manual_circle_on_pending_vision_step(tmp_path):
    """待确认的视觉步骤：右卡帧/检测圆可取，手动圆判定生效（以手动圆为准）。"""
    stack = _stack(tmp_path)
    rid = _vision_recipe(stack)
    vm = _new_vm(stack)
    vm.select_recipe(rid)
    vm.start_current()

    assert _tick_until_prompt(vm, step_id="cap")[0] is not None
    assert vm.confirm_step("确认", "张三") is True
    prompt, _st = _tick_until_prompt(vm, step_id="det")
    assert prompt is not None

    frame = vm.current_frame()
    assert frame is not None and frame.color is not None
    det = vm.current_detection()
    assert det is not None and det.status.value == "PASS"

    state = vm.step_circle_state()
    assert state["available"] is True and state["manual"] is False
    circle = state["circle"]
    assert circle is not None

    info = vm.apply_step_circle(circle["cx"] + 5.0, circle["cy"] + 3.0,
                                circle["radius_px"], "张三")
    assert info["status"] == "PASS"
    after = vm.step_circle_state()
    assert after["manual"] is True
    assert after["circle"]["cx"] == pytest.approx(circle["cx"] + 5.0)
    assert vm.hint_tone == "ok"

    # 手动结果写进步骤输出（下游条件步骤按它判定）
    node = stack.task.compiled.find_step("det")
    si = node.ctx.station_instance_id or ""
    assert stack.task.compiled.outputs.get(si, "det", "status") == "PASS"
    out = stack.task.compiled.outputs.get(si, "det", "result")
    assert out.metrics.get("manual") == 1.0
    stack.close()


def test_vm_current_frames_per_camera_capture(tmp_path):
    """DEC-051: 多相机抓拍后按 camera_id 取各自最近帧（右卡页签用）。"""
    from application.services.device_config import (
        CameraEntry, DeviceConfig, DeviceConfigStore)

    cfg_path = tmp_path / "devices.yaml"
    seed = DeviceConfigStore(str(cfg_path), seed_if_missing=False)
    seed._config = DeviceConfig(cameras=[                  # noqa: SLF001 - 测试构造
        CameraEntry("ds86_01", "DS86 (随臂)", enabled=True, confirmed=True),
        CameraEntry("d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
                    host="192.168.2.117", port=5555, channel_id="cam1"),
    ])
    seed.save()
    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path), recipe_path=RECIPE,
                                device_config_path=str(cfg_path))
    rid = "ui_multi_camera_offline"
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "cap_ds86", "type": "camera.capture", "camera_ref": "ds86_01"},
            {"id": "cap_d405", "type": "camera.capture", "camera_ref": "d405_01"},
        ],
    })
    vm = _new_vm(stack)
    vm.select_recipe(rid)
    vm.start_current()

    assert _tick_until_prompt(vm, step_id="cap_ds86")[0] is not None
    assert vm.current_frame("ds86_01") is not None
    assert vm.current_frame("d405_01") is None            # 还没抓 D405
    vm.confirm_step("确认", "张三")

    assert _tick_until_prompt(vm, step_id="cap_d405")[0] is not None
    f405 = vm.current_frame("d405_01")
    assert f405 is not None and f405.camera_id == "d405_01"
    f86 = vm.current_frame("ds86_01")
    assert f86 is not None and f86.camera_id == "ds86_01"
    # 两台相机各自的帧都还在（不是只留最近一次抓拍的那一台）
    assert set(vm.current_frames()) == {"ds86_01", "d405_01"}
    stack.close()


def test_vm_current_frame_ignores_previous_run():
    """跨 run 的旧帧/旧检测不显示（防止新任务右卡还挂着上次的图）。"""
    from types import SimpleNamespace

    class _SM:
        run_id = "run-new"
        state = TaskState.IDLE
        is_active = False

    task = SimpleNamespace(
        state_machine=_SM(), compiled=None,
        executor=SimpleNamespace(last_capture_run_id="run-old",
                                 last_frame=object(),
                                 camera_frames={"d405_01": SimpleNamespace(
                                     run_id="run-old")},
                                 last_detection_run_id="run-old",
                                 last_detection=object()),
        available_recipes=lambda: [],
    )
    vm = WorkbenchViewModel(task=task, allow_offline_draft=True)
    assert vm.current_frame() is None
    assert vm.current_frame("d405_01") is None
    assert vm.current_frames() == {}
    assert vm.current_detection() is None


# ---------------------------------------------------------------- 弹框（Qt）
def test_step_confirm_dialog_is_modeless_and_blocks_close(qapp):
    from application.presentation.pages.workbench import StepConfirmDialog

    got: list[str] = []
    dlg = StepConfirmDialog(on_choice=got.append)
    dlg.set_prompt({"title": "步骤完成确认 · 抓拍", "message": "第 2/6 步已执行完成",
                    "options": ["确认", "不正确"], "step_id": "cap"}, circle_available=True)
    assert "拖动" in dlg.circle_hint.text()
    dlg.mark_active(True)
    dlg.show()
    qapp.processEvents()
    dlg.close()                     # 等待期间点叉无效, 必须显式选择
    qapp.processEvents()
    dlg.reject()                    # Esc 同样无效
    qapp.processEvents()
    assert dlg.isVisible()

    dlg.ok_btn.click()
    assert got == ["确认"]
    dlg.mark_active(False)
    dlg.close()
    dlg.deleteLater()
    qapp.processEvents()


def test_step_confirm_dialog_without_circle_hint(qapp):
    from application.presentation.pages.workbench import StepConfirmDialog

    dlg = StepConfirmDialog()
    dlg.set_prompt({"title": "步骤完成确认 · 运动", "message": "",
                    "options": ["确认", "不正确"]}, circle_available=False)
    assert "原图" in dlg.circle_hint.text()
    dlg.deleteLater()
    qapp.processEvents()


def test_guide_confirm_dialog_is_modeless_and_blocks_close(qapp):
    """DEC-059：引导定位确认改为弹窗；非模态、等待期间叉/Esc 无效、只回受限选项。"""
    from application.presentation.pages.workbench import GuideConfirmDialog

    got: list[str] = []
    dlg = GuideConfirmDialog(on_choice=got.append)
    dlg.set_prompt({"title": "引导定位 · 粗定位",
                    "message": "检测到偏差 X=10.00mm Z=-5.00mm\n是否按该偏移移动？",
                    "options": ["是", "否"]})
    dlg.mark_active(True)
    dlg.show()
    qapp.processEvents()
    dlg.close()                     # 等待期间点叉无效, 必须显式选择
    qapp.processEvents()
    dlg.reject()                    # Esc 同样无效
    qapp.processEvents()
    assert dlg.isVisible()

    btns = dlg.buttons()
    assert [b.text() for b in btns] == ["是", "否"]
    btns[0].click()
    assert got == ["是"]
    dlg.mark_active(False)
    dlg.close()
    dlg.deleteLater()
    qapp.processEvents()


# ---------------------------------------------------------------- 页面接线
def test_fault_recovery_dialog_offers_resume_or_restart(qapp):
    """DEC-061：故障恢复弹窗——可原地重试时默认“从失败步骤恢复”，否则只能重启。"""
    from application.presentation.pages.workbench import FaultRecoveryDialog

    from PySide6.QtWidgets import QLabel
    dlg = FaultRecoveryDialog(info={
        "run_id": "run-9", "step_id": "cap", "step_title": "抓拍",
        "reason": "抓拍超时",
        "unknown_count": 0, "quarantine": {}, "retryable": True})
    assert dlg.choice() == "resume"
    assert dlg.resume_radio.isEnabled()
    assert not hasattr(dlg, "note_edit")          # DEC-104：人工说明已移除
    assert not hasattr(dlg, "confirm_check")      # DEC-104：确认勾选已移除
    texts = [w.text() for w in dlg.findChildren(QLabel)]
    assert any("失败步骤：抓拍" in t for t in texts)   # DEC-105：中文步骤名
    dlg.deleteLater()
    qapp.processEvents()

    dlg2 = FaultRecoveryDialog(info={
        "run_id": "run-9", "step_id": "loop", "reason": "循环内失败",
        "unknown_count": 0, "quarantine": {}, "retryable": False})
    assert dlg2.choice() == "restart"
    assert not dlg2.resume_radio.isEnabled()
    dlg2.deleteLater()
    qapp.processEvents()


def test_workbench_has_fault_recovery_button(qapp, tmp_path):
    """DEC-061：作业台固定操作栏有「故障恢复」按钮（FAULT 时可用）。"""
    from types import SimpleNamespace

    from application.presentation.pages.workbench import WorkbenchPage

    stack = _stack(tmp_path)
    vm = _new_vm(stack)
    page = WorkbenchPage(SimpleNamespace(stack=stack, vm=vm))
    page._autotick = False
    try:
        assert "fault_recover" in page.rail.buttons
        assert page.rail.buttons["fault_recover"].text() == "故障恢复"
    finally:
        page.shutdown(); page.close(); page.deleteLater()
        qapp.processEvents()
        stack.close()


def test_workbench_page_shows_dialog_frame_and_applies_circle(qapp, tmp_path):
    from types import SimpleNamespace

    from application.presentation.pages.workbench import WorkbenchPage

    stack = _stack(tmp_path)
    rid = _vision_recipe(stack, rid="ui_page_confirm")
    vm = _new_vm(stack)
    vm.select_recipe(rid)
    ctx = SimpleNamespace(stack=stack, vm=vm)
    page = WorkbenchPage(ctx)
    page._autotick = False
    page.resize(1100, 780)
    page.show()
    qapp.processEvents()

    vm.start_current()
    vm.tick()                                    # 抓拍完成 -> 抓拍闸
    page.refresh()
    dlg = page._step_dialog
    assert dlg is not None and dlg.isVisible()
    assert "抓拍" in dlg.title.text()
    assert page.image_view.available
    # 抓拍帧显示在右卡
    assert page._shown_frame_id is not None
    assert "抓拍步骤" in page.image_caption.text()

    page._on_step_choice("确认")
    vm.tick()                                    # 判定完成 -> 视觉闸
    page.refresh()
    assert "视觉判定" in page.image_caption.text()
    assert page.image_view.circle_edit_mode()
    assert page.image_view.circle_overlay() is not None

    state = vm.step_circle_state()
    circle = state["circle"]
    page._on_circle_edited(int(round(circle["cx"])) + 6,
                           int(round(circle["cy"])),
                           int(round(circle["radius_px"])))
    qapp.processEvents()
    assert "手动圆" in dlg.status.text() and "PASS" in dlg.status.text()
    assert vm.step_circle_state()["manual"] is True
    overlay = page.image_view.circle_overlay()
    assert overlay is not None and overlay[0] == pytest.approx(circle["cx"] + 6, abs=1)

    page._on_step_choice("确认")
    vm.tick()
    page.refresh()
    assert vm.state().task_state == "COMPLETED"
    assert not dlg.isVisible()

    page.shutdown()
    page.close()
    page.deleteLater()
    qapp.processEvents()
    stack.close()


# ---------------------------------------------------------------- 引导定位画圆（DEC-084）
def test_vm_guide_circle_state_exposes_circle_and_frame():
    """粗/精定位检测完成时，VM 要给出引导用的帧与检测圆（作业台右卡画圆）。"""
    from types import SimpleNamespace

    from application.services.guidance_service import CircleHit, GuidanceResult

    frame = SimpleNamespace(run_id="run-1", camera_id="ds86_01")
    result = GuidanceResult(True, "ok", circle=CircleHit(0, 100.0, 120.0, 30.0))
    motion = SimpleNamespace(state={
        "mode": "guide", "guide_phase": "confirm", "guide_stage": "coarse",
        "last_guide": result, "last_guide_frame": frame})
    task = SimpleNamespace(state_machine=SimpleNamespace(run_id="run-1"),
                           motion=motion, executor=None, compiled=None)
    vm = WorkbenchViewModel(task=task)

    info = vm.guide_circle_state()
    assert info["available"] and info["active"]
    assert info["stage"] == "coarse" and info["frame"] is frame
    assert info["circle"] == {"cx": 100.0, "cy": 120.0, "radius_px": 30.0}

    motion.state = {**motion.state, "mode": "idle", "guide_phase": ""}
    assert vm.guide_circle_state()["active"] is False      # 引导结束后不再显示旧圆

    motion.state = {**motion.state, "last_guide_frame": SimpleNamespace(
        run_id="old-run", camera_id="ds86_01")}
    assert vm.guide_circle_state()["available"] is False   # 跨 run 的旧帧不显示


def test_workbench_right_card_draws_guide_circle(qapp, tmp_path):
    """引导定位（粗/精）期间右卡必须显示检测圆，而不是只有原始图。"""
    from types import SimpleNamespace

    from application.contracts.frames import CaptureRequest
    from application.presentation.pages.workbench import WorkbenchPage

    stack = _stack(tmp_path)
    vm = _new_vm(stack)
    ctx = SimpleNamespace(stack=stack, vm=vm)
    frame = stack.cameras["ds86_01"].capture(CaptureRequest(
        camera_ids=("ds86_01",), request_id="guide-ui")).frames["ds86_01"]
    vm.guide_circle_state = lambda: {
        "available": True, "active": True, "frame": frame, "stage": "fine",
        "circle": {"cx": 100.0, "cy": 120.0, "radius_px": 30.0}}
    page = WorkbenchPage(ctx)
    page._autotick = False
    try:
        page.refresh()
        assert "引导定位·精定位" in page.image_caption.text()
        cx, cy, r = page.image_view.circle_overlay()
        assert (cx, cy, r) == pytest.approx((100.0, 120.0, 30.0), abs=0.1)
    finally:
        page.shutdown()
        page.close()
        page.deleteLater()
        qapp.processEvents()
        stack.close()
