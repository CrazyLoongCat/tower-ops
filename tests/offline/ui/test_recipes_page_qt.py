"""E20「配方流程」页 (DEC-024): 两栏编辑 / 运动步骤自带示教点与可选方案 / 自定义操作 / 引用命名视觉检测 / 发布闸。

离屏 Qt；页面逻辑走 RecipeEditor，改动一律经它与五层校验，不绕过去。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QComboBox, QGroupBox, QLabel  # noqa: E402

from application.bootstrap.composition import build_offline_stack  # noqa: E402
from application.contracts.enums import NodeType  # noqa: E402
from application.presentation.app import MainWindow, UiContext  # noqa: E402
from application.presentation.viewmodel import WorkbenchViewModel  # noqa: E402
from application.services.motion_config import (  # noqa: E402
    MotionConfigStore, MotionPlan, Waypoint,
)
from application.services.vision_detections import (  # noqa: E402
    VisionDetection, VisionDetectionStore,
)

RECIPE = "configs/recipes/ds86_two_station_example.yaml"
PLAN = "用例扫描方案"
DET = "检测-A"


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def win(tmp_path, qapp):
    """带**一份方案和一条命名检测**的离线栈：页面下拉里应当看得见它们。"""
    import gc
    mp = tmp_path / "motion.yaml"
    store = MotionConfigStore(str(mp)); store.load()
    store.config.plans.append(MotionPlan(
        plan_id=PLAN, default_mode="movej",
        waypoints=[Waypoint("movej", (1.0, 2.0, 3.0, 0.0, 90.0, 0.0))]))
    store.save()
    dp = tmp_path / "detections.yaml"
    dstore = VisionDetectionStore(str(dp)); dstore.load()
    dstore.upsert(VisionDetection(detection_id=DET, camera_ref="ds86_01",
                                  pipeline_ref="approved_ds86_circle_pipeline",
                                  source="test"))
    dstore.save()

    stack = build_offline_stack(db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
                                recipe_path=RECIPE, motion_config_path=str(mp),
                                vision_detections_path=str(dp))
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    w = MainWindow(UiContext(stack=stack, vm=vm))
    w.resize(1440, 1024)
    try:
        yield w, vm, stack, qapp
    finally:
        # 关闭顺序：停定时器 -> 关窗 -> deleteLater -> 跑事件 -> 关栈 -> 回收
        try:
            w.shutdown(); w.close(); w.deleteLater(); qapp.processEvents()
        except Exception:  # noqa: BLE001
            pass
        stack.close()
        del w
        gc.collect()
        qapp.processEvents()


def _page(win):
    w, _vm, _stack, app = win
    w._select("recipes")
    w.refresh_all(); app.processEvents()
    return w.pages["recipes"]


def _step_group(page) -> QGroupBox:
    for i in range(page.prop_lay.count()):
        w = page.prop_lay.itemAt(i).widget()
        if isinstance(w, QGroupBox) and w.title().startswith("步骤属性 —"):
            return w
    raise AssertionError("属性栏没有步骤表单")


def _prop_top_group(page, prefix: str) -> QGroupBox:
    """属性栏**顶层**分组（判断属性是独立分组，不再挂在步骤表单里，DEC-066）。"""
    for i in range(page.prop_lay.count()):
        w = page.prop_lay.itemAt(i).widget()
        if isinstance(w, QGroupBox) and w.title().startswith(prefix):
            return w
    raise AssertionError(f"属性栏顶层没有「{prefix}」分组")


def _prop_index(page, group: QGroupBox) -> int:
    for i in range(page.prop_lay.count()):
        if page.prop_lay.itemAt(i).widget() is group:
            return i
    raise AssertionError("分组不在属性栏里")


def _combo_by_row_label(group: QGroupBox, label: str) -> QComboBox:
    from PySide6.QtWidgets import QFormLayout
    form = group.layout()
    assert isinstance(form, QFormLayout)
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        if item is not None and item.widget() is not None \
                and item.widget().text() == label:
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
            assert isinstance(field, QComboBox)
            return field
    raise AssertionError(f"属性栏没有「{label}」下拉")


# ---------------------------------------------------------------- 呈现
def test_recipes_page_shows_draft_and_keeps_offline_publish_blocked(win):
    page = _page(win)
    assert page.recipe_combo.currentData() == "ds86_two_station_example"
    assert page.step_list.count() == 6
    assert page.result_label.text().startswith("校验通过")
    assert not page.btn_publish.isEnabled()
    assert "离线示例" in page.btn_publish.toolTip()


def test_recipes_page_selecting_a_recipe_also_points_the_workbench_at_it(win):
    _w, vm, _stack, _app = win
    page = _page(win)
    page.editor.new_recipe("r_new")
    page._reload()
    # DEC-041 起：只有**用户切换**下拉才同步作业台（重建下拉时的程序化选中不同步，
    # 否则配方页一刷新就覆盖作业台自己的选择）。先切走再切到新方案 = 真实的手动切换。
    page.recipe_combo.setCurrentIndex(
        page.recipe_combo.findData("ds86_two_station_example"))
    page.recipe_combo.setCurrentIndex(page.recipe_combo.findData("r_new"))
    assert vm.selected_recipe == "r_new"


# ---------------------------------------------------------------- 编辑
def test_recipes_page_can_add_step(win, monkeypatch):
    import application.presentation.pages.recipes as R
    from application.contracts.enums import NodeType
    page = _page(win)
    monkeypatch.setattr(R, "_ask_text", lambda *a, **k: ("r_edit", True))
    page._new_recipe()
    monkeypatch.setattr(R, "_ask_item", lambda *a, **k: (
        R.STEP_TYPE_LABELS[NodeType.ROBOT_MOVE_JOINT], True))
    page._add_step()
    assert [s.id for s in page.editor.model.sequence] == ["move_1"]
    assert page.editor.model.sequence[0].type is NodeType.ROBOT_MOVE_JOINT


def test_parallel_group_props_edit_both_branches(win, monkeypatch):
    """DEC-096：并行执行块 = 机器人分支（示教点/方案）+ 机构分支（动作）。"""
    import application.presentation.pages.recipes as R
    from application.contracts.enums import NodeType
    page = _page(win)
    monkeypatch.setattr(R, "_ask_text", lambda *a, **k: ("r_parallel", True))
    page._new_recipe()
    page.editor.set_offline_draft(False)
    monkeypatch.setattr(R, "_ask_item", lambda *a, **k: (
        R.STEP_TYPE_LABELS[NodeType.PARALLEL_GROUP], True))
    page._add_step()
    sid = page.editor.model.sequence[0].id
    page._sel_step = sid
    page._reload()

    step = page.editor.model.sequence[0]
    assert [b.type for b in step.branches] == [
        NodeType.ROBOT_MOVE_JOINT, NodeType.MECHANISM_ACTION]
    assert "∥" in page.step_list.item(0).text()

    group = _step_group(page)
    target = _combo_by_row_label(group, "机器人分支：关节目标")
    target.setCurrentIndex(target.findData("teach_02"))
    assert page.editor.model.sequence[0].branches[0].joint_target_ref == "teach_02"
    plan = _combo_by_row_label(group, "机器人分支：运动方案")
    assert plan.findData(PLAN) >= 0
    action = _combo_by_row_label(group, "机构分支：动作")
    assert action.findData("approved_single_board_action") >= 0
    assert page.editor.validate().ok


def test_custom_step_can_pick_method_and_multiple_devices(win, monkeypatch):
    """新增「自定义操作」步骤: 属性栏能选方法、勾选多个设备（同时下发、等全部完成）。"""
    import application.presentation.pages.recipes as R
    from application.contracts.enums import NodeType
    page = _page(win)
    monkeypatch.setattr(R, "_ask_text", lambda *a, **k: ("r_custom", True))
    page._new_recipe()
    page.editor.set_offline_draft(False)
    monkeypatch.setattr(R, "_ask_item", lambda *a, **k: (
        R.STEP_TYPE_LABELS[NodeType.CUSTOM_CALL], True))
    page._add_step()
    sid = page.editor.model.sequence[0].id
    page._sel_step = sid
    page._reload()
    assert page.editor.model.sequence[0].method_ref == "probe_devices"

    combo = _combo_by_row_label(_step_group(page), "自定义方法")
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert any(t.startswith("probe_devices") for t in texts)
    assert any(t.startswith("set_output") for t in texts)

    # 勾选两台设备
    from PySide6.QtWidgets import QCheckBox
    group = _step_group(page)
    boxes = group.findChildren(QCheckBox)
    assert {b.text() for b in boxes} >= {"eco65_01", "ds86_01"}
    for b in boxes:
        if b.text() in ("eco65_01", "ds86_01"):
            b.setChecked(True)
    assert set(page.editor.model.sequence[0].device_refs) == {"eco65_01", "ds86_01"}
    assert page.editor.validate().ok


def test_move_step_combo_binds_named_motion_plan(win, monkeypatch):
    """「运动方案」下拉选中一条方案 -> 该步骤按方案回放（写进 motion_plan_ref）。"""
    import application.presentation.pages.recipes as R  # noqa: F401
    from application.contracts.enums import NodeType
    page = _page(win)
    page.editor.new_recipe("r_bind")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.ROBOT_MOVE_JOINT)
    page._sel_step = sid
    page._reload()

    # 运动步骤自带示教点：新增即取目录里第一个关节目标
    tgt = _combo_by_row_label(_step_group(page), "关节目标")
    assert tgt.currentData() == "teach_01"

    combo = _combo_by_row_label(_step_group(page), "运动方案（可选）")
    assert [combo.itemText(i) for i in range(combo.count())] == \
        ["（不绑定：直接用上面的关节目标）", PLAN]
    combo.setCurrentIndex(1)
    assert page.editor.model.sequence[0].motion_plan_ref == PLAN
    assert page.editor.validate().ok
    # 再选回"不绑定" => 回到用关节目标
    combo2 = _combo_by_row_label(_step_group(page), "运动方案（可选）")
    combo2.setCurrentIndex(0)
    assert page.editor.model.sequence[0].motion_plan_ref is None


def test_vision_step_combo_can_point_at_saved_detection(win):
    """「检测来源」下拉能选到「视觉调试」页另存的检测，并清掉流水线（二选一）。"""
    from application.contracts.enums import NodeType
    page = _page(win)
    page.editor.new_recipe("r_det")
    page.editor.set_offline_draft(False)
    page.editor.add_step(NodeType.CAMERA_CAPTURE)
    sid = page.editor.add_step(NodeType.VISION_RUN)
    page._sel_step = sid
    page._reload()

    combo = _combo_by_row_label(_step_group(page), "检测来源")
    texts = [combo.itemText(i) for i in range(combo.count())]
    assert any(t.startswith("流水线：") for t in texts)
    assert any(t.startswith(f"视觉检测：{DET}") for t in texts)
    combo.setCurrentIndex(next(i for i, t in enumerate(texts)
                              if t.startswith(f"视觉检测：{DET}")))
    step = next(s for s in page.editor.model.sequence if s.id == sid)
    assert step.detection_ref == DET
    assert step.pipeline_ref is None
    assert page.editor.validate().ok


def test_step_list_shows_what_each_step_is_bound_to(win):
    from application.contracts.enums import NodeType
    page = _page(win)
    page.editor.new_recipe("r_show")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.ROBOT_MOVE_JOINT)
    page.editor.set_motion_plan(sid, PLAN)
    page.editor.add_step(NodeType.CAMERA_CAPTURE)
    d = page.editor.add_step(NodeType.VISION_RUN)
    page.editor.set_detection(d, DET)
    page._reload()
    texts = [page.step_list.item(i).text() for i in range(page.step_list.count())]
    assert any(PLAN in t for t in texts)          # 绑定写在这步的说明里
    assert any(DET in t for t in texts)


# ---------------------------------------------------------------- 引导定位 (DEC-033)
def test_guided_step_props_edit_camera_stages_speed_and_limit(win):
    """引导定位步骤：属性栏能选相机/段/速度/偏移上限；改动写回并通过校验。"""
    from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox
    from application.contracts.enums import NodeType
    page = _page(win)
    page.editor.new_recipe("r_guide")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.GUIDED_POSITION)
    page._sel_step = sid
    page._reload()
    step = next(s for s in page.editor.model.sequence if s.id == sid)
    assert step.camera_ref == "ds86_01"                    # 默认选 DS86
    assert step.guide_stages == ("coarse", "fine")
    assert page.editor.validate().ok

    group = _step_group(page)
    cam = _combo_by_row_label(group, "相机（引导定位）")
    assert cam.currentData() == "ds86_01"
    stages = _combo_by_row_label(group, "段选择")
    assert [stages.itemText(i) for i in range(stages.count())] == \
        ["粗 → 精（默认）", "仅粗", "仅精"]
    stages.setCurrentIndex(2)                              # 仅精
    step = next(s for s in page.editor.model.sequence if s.id == sid)
    assert step.guide_stages == ("fine",)

    sp_speed = next(w for w in group.findChildren(QSpinBox)
                    if w.suffix().strip() == "%")
    sp_speed.setValue(6)
    limit = next(w for w in group.findChildren(QDoubleSpinBox)
                 if w.suffix().strip() == "mm")
    limit.setValue(5.0)
    step = next(s for s in page.editor.model.sequence if s.id == sid)
    assert step.guide_speed_pct == 6
    assert step.guide_max_offset_mm == 5.0

    sp_speed2 = next(w for w in _step_group(page).findChildren(QSpinBox)
                     if w.suffix().strip() == "%")
    sp_speed2.setValue(0)                                  # 0 = 跟随引导配置
    step = next(s for s in page.editor.model.sequence if s.id == sid)
    assert step.guide_speed_pct is None
    assert page.editor.validate().ok
    texts = [page.step_list.item(i).text() for i in range(page.step_list.count())]
    assert any("段 fine" in t for t in texts)


# ---------------------------------------------------------------- 引导定位相机 (DEC-137)
def test_guide_camera_combo_lists_all_enabled_cameras(win, monkeypatch):
    """DEC-137：引导定位相机下拉列出**所有已启用相机**，DS86 排最前（默认）。"""
    page = _page(win)
    page.editor.new_recipe("r_guide_cam")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.GUIDED_POSITION)
    page.editor.set_step_field(sid, "camera_ref", "d405_01")
    page._sel_step = sid
    monkeypatch.setattr(page.editor, "cameras", lambda: ["d405_01", "ds86_01"])
    page._reload()
    group = _step_group(page)
    cam = _combo_by_row_label(group, "相机（引导定位）")
    assert [cam.itemData(i) for i in range(cam.count())] == ["ds86_01", "d405_01"]
    assert cam.currentData() == "d405_01"


def test_guide_camera_combo_flags_missing_reference(win, monkeypatch):
    """引用了未启用相机：下拉里仍列出来并标注「未启用」；目录为空时保留 DS86 占位。"""
    page = _page(win)
    page.editor.new_recipe("r_guide_legacy")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.GUIDED_POSITION)
    page.editor.set_step_field(sid, "camera_ref", "d405_01")
    page._sel_step = sid
    monkeypatch.setattr(page.editor, "cameras", lambda: ["ds86_01"])
    page._reload()
    cam = _combo_by_row_label(_step_group(page), "相机（引导定位）")
    assert [cam.itemData(i) for i in range(cam.count())] == ["ds86_01", "d405_01"]
    assert "未启用" in cam.itemText(1)
    assert cam.currentData() == "d405_01"

    monkeypatch.setattr(page.editor, "cameras", lambda: [])
    page._reload()
    cam = _combo_by_row_label(_step_group(page), "相机（引导定位）")
    assert cam.itemData(0) == "ds86_01" and "未启用" in cam.itemText(0)


def test_guided_props_hint_when_camera_not_open(win, monkeypatch):
    """本次运行没打开所选相机：属性栏提示「可编辑/保存，运行时如实失败」，不假装可用。"""
    from PySide6.QtWidgets import QLabel
    _w, _vm, stack, _app = win
    page = _page(win)
    page.editor.new_recipe("r_guide_hint")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.GUIDED_POSITION)
    page._sel_step = sid
    monkeypatch.delitem(stack.cameras, "ds86_01")                # 模拟未连接
    page._reload()
    labels = [lb.text() for lb in _step_group(page).findChildren(QLabel)]
    assert any("未打开相机" in t and "ds86_01" in t for t in labels)


def test_workbench_guide_confirm_dialog_submits_restricted_choice(win, monkeypatch):
    """作业台页：有引导定位确认提示时弹非模态弹窗；点选项 -> vm.provide_operator。"""
    w, _vm, _stack, app = win
    w._select("workbench"); w.refresh_all(); app.processEvents()
    page = w.pages["workbench"]
    prompt = {"title": "引导定位 · 粗定位",
              "message": "检测到偏差 X=10.00mm Z=-5.00mm\n是否按该偏移移动？",
              "options": ["是", "否"], "step_id": "g"}
    monkeypatch.setattr(page.vm, "pending_operator_prompt", lambda: prompt)
    page._sync_confirm()
    app.processEvents()
    dlg = page._guide_dialog
    assert dlg is not None and dlg.isVisible()
    assert dlg.title.text() == prompt["title"]
    btns = dlg.buttons()
    assert [b.text() for b in btns] == ["是", "否"]

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(page.vm, "provide_operator",
                        lambda choice, operator: seen.append((choice, operator)) or True)
    btns[1].click(); app.processEvents()
    assert seen and seen[0][0] == "否"

    monkeypatch.setattr(page.vm, "pending_operator_prompt", lambda: None)
    page._sync_confirm()
    assert not dlg.isVisible()


# ---------------------------------------------------------------- 校验定位
def test_error_click_selects_the_offending_step(win):
    from application.contracts.enums import NodeType
    page = _page(win)
    page.editor.new_recipe("r_bad")
    page.editor.set_offline_draft(False)
    page.editor.add_step(NodeType.CAMERA_CAPTURE)
    sid = page.editor.add_step(NodeType.VISION_RUN)
    page.editor.set_detection(sid, "没有这条检测")
    page._sel_step = None
    page._reload()

    assert page.err_list.count() >= 1
    item = next(page.err_list.item(i) for i in range(page.err_list.count())
                if item_step(page, i) == sid)
    page._goto_error(item)
    assert page._sel_step == sid
    title = _step_group(page).title()          # 中文类型名 + 步骤 ID（DEC-037）
    assert sid in title and "视觉判定" in title


def item_step(page, i):
    from PySide6.QtCore import Qt
    return page.err_list.item(i).data(Qt.ItemDataRole.UserRole)


# ---------------------------------------------------------------- 发布
def test_publish_is_gated_then_records_a_revision(win, monkeypatch):
    import application.presentation.pages.recipes as R
    page = _page(win)                      # 当前是离线示例：不许发布
    warned: list[str] = []
    monkeypatch.setattr(R, "_warn", lambda parent, title, text: warned.append(text))
    page._publish()
    assert warned and "离线示例" in warned[0]
    assert page.editor.history() == []

    page.editor.set_offline_draft(False)   # 认下"这是认真值"才放行
    # 示例来源是仓库文件；清掉来源=模拟未落盘草稿，别让测试写回仓库（DEC-040 会写回来源）
    page.editor.service.set_source(page.editor.recipe_id, None)
    page._reload()
    assert page.btn_publish.isEnabled()

    def _no_dialog(*a, **k):
        raise AssertionError("发布不应再弹操作员输入框（DEC-040）")
    monkeypatch.setattr(R, "_ask_text", _no_dialog)
    # DEC-041: 发布成功要弹窗（用户点名）；测试里截获弹窗内容，不阻塞
    popups: list[tuple[str, str]] = []
    monkeypatch.setattr(R, "_info", lambda parent, title, text: popups.append((title, text)))
    page._publish()
    revs = page.editor.history()
    assert len(revs) == 1 and revs[0].endswith("+1")   # revision = 版本号 + 序号
    text = page.result_label.text()
    assert "发布成功" in text and f"revision {revs[0]}" in text
    # DEC-039/040: 发布即生效, 没有"批准"环节, 也不再提示"审批矩阵/未获批"
    assert "发布即生效" in text
    assert "审批" not in text
    assert page.editor.model.state == "published"      # 作业台启动校验据此放行
    # DEC-041: 弹出「发布成功」窗口，内容与结果栏一致
    assert popups and popups[0][0] == "发布成功"
    assert f"revision {revs[0]}" in popups[0][1]


def test_publish_clears_the_unpublished_blocker_for_the_workbench(win, monkeypatch):
    """DEC-040: 发布后作业台校验不再报「执行方案未发布」（截图里的红字）。"""
    import application.presentation.pages.recipes as R
    _w, _vm, stack, _app = win
    page = _page(win)
    page.editor.set_offline_draft(False)
    page.editor.service.set_source(page.editor.recipe_id, None)
    page._reload()
    rid = page.editor.recipe_id
    # 发布前：真实模式启动校验明确被拦（与作业台红字同源）
    assert any("未发布" in p for p in stack.task.validate(rid))
    monkeypatch.setattr(R, "_info", lambda *a, **k: None)   # DEC-041 弹窗不阻塞测试
    page._publish()
    assert [p for p in stack.task.validate(rid) if "未发布" in p] == []


def test_publish_writes_published_state_back_to_source_file(win, tmp_path, monkeypatch):
    """DEC-040: 发布成功把 state: published 写回来源文件，重启后作业台仍放行。"""
    import application.presentation.pages.recipes as R
    page = _page(win)
    page.editor.set_offline_draft(False)
    target = tmp_path / "pub_recipe.yaml"
    monkeypatch.setattr(R, "_info", lambda *a, **k: None)   # DEC-041 弹窗不阻塞测试
    page.editor.save(str(target))          # 另存到临时文件 -> 来源 = 临时文件
    page._reload()
    page._publish()
    assert "state: published" in target.read_text(encoding="utf-8")
    assert "发布成功" in page.result_label.text()


def test_page_source_has_no_approval_step():
    """DEC-039: 发布是最后一步——页面代码/文案里不应再有"审批"字样。"""
    import inspect
    import application.presentation.pages.recipes as R
    assert "审批" not in inspect.getsource(R)


def test_save_as_writes_file_where_user_asked(win, monkeypatch, tmp_path):
    import application.presentation.pages.recipes as R
    page = _page(win)
    target = tmp_path / "out" / "saved.yaml"
    monkeypatch.setattr(R, "_ask_text", lambda *a, **k: (str(target), True))
    page._save_as()
    assert target.exists()
    assert "recipe_id: ds86_two_station_example" in target.read_text(encoding="utf-8")
    assert str(target) in page.result_label.text()


# ---------------------------------------------------------------- DEC-035 保存 / 另存为
def test_page_has_save_and_save_as_buttons(win):
    page = _page(win)
    assert page.btn_save.text() == "保存"
    assert page.btn_save_as.text() == "另存为"
    labels = {b.text() for b in (page.btn_new, page.btn_copy,
                                 page.btn_save, page.btn_save_as)}
    assert "另存草稿" not in labels          # 旧按钮名不再出现


def test_save_button_overwrites_source_without_dialog(win, monkeypatch, tmp_path):
    import application.presentation.pages.recipes as R
    page = _page(win)
    target = tmp_path / "saved.yaml"
    page.editor.service.set_source(page.editor.recipe_id, str(target))

    def _no_dialog(*_a, **_k):
        raise AssertionError("「保存」不应弹路径对话框")

    monkeypatch.setattr(R, "_ask_text", _no_dialog)
    page.btn_save.click()
    assert target.exists()
    assert "recipe_id: ds86_two_station_example" in target.read_text(encoding="utf-8")
    assert str(target) in page.result_label.text()
    page.btn_save.click()                    # 再点一次：覆盖同一文件 + 保留 .bak
    assert (tmp_path / "saved.yaml.bak").exists()


def test_save_new_recipe_goes_to_recipes_dir(win, monkeypatch, tmp_path):
    import application.presentation.pages.recipes as R
    page = _page(win)
    monkeypatch.setattr(R, "_recipes_dir", lambda: str(tmp_path / "recipes"))
    page.editor.new_recipe("r_saved")
    page._reload()
    page.btn_save.click()
    out = tmp_path / "recipes" / "r_saved.yaml"
    assert out.exists()
    assert "recipe_id: r_saved" in out.read_text(encoding="utf-8")
    assert str(out) in page.result_label.text()
    assert page.editor.source_path() == str(out)     # 「保存」之后就有来源了


# ---------------------------------------------------------------- DEC-036 属性栏下拉限宽
def test_long_plan_name_does_not_stretch_property_combo(win):
    """长方案名不把「运动方案」下拉撑宽；选中后也不变宽，tooltip 给全文。"""
    _w, _vm, stack, _app = win
    long_plan = "任意位置->拍照位（动作前请确保套筒已离开螺栓）"
    stack.motion_config.config.plans.append(MotionPlan(
        plan_id=long_plan, default_mode="movej",
        waypoints=[Waypoint("movej", (1.0, 2.0, 3.0, 0.0, 90.0, 0.0))]))
    page = _page(win)
    page.refresh()                                   # 目录同步进新方案
    page.editor.new_recipe("r_width")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.ROBOT_MOVE_JOINT)
    page._sel_step = sid
    page._reload()

    combo = _combo_by_row_label(_step_group(page), "运动方案（可选）")
    width_before = combo.sizeHint().width()
    full_width = combo.fontMetrics().horizontalAdvance(long_plan)
    assert width_before < full_width              # 不再按最长条目铺开
    assert combo.minimumSizeHint().width() <= combo.maximumWidth()   # 布局最小值被夹住

    idx = combo.findData(long_plan)
    assert idx >= 0
    combo.setCurrentIndex(idx)                    # 选中长方案 -> 触发重建
    combo2 = _combo_by_row_label(_step_group(page), "运动方案（可选）")
    assert combo2.currentData() == long_plan
    assert combo2.sizeHint().width() == width_before   # 选中前后一样宽，不再变长
    assert combo2.toolTip() == long_plan          # 全文在 tooltip 里


# ---------------------------------------------------------------- 步骤显示名 (DEC-037)
def _field_by_row_label(group, label):
    from PySide6.QtWidgets import QFormLayout
    form = group.layout()
    assert isinstance(form, QFormLayout)
    for row in range(form.rowCount()):
        item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        if item is not None and item.widget() is not None \
                and item.widget().text() == label:
            return form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
    raise AssertionError(f"属性栏没有「{label}」字段")


def test_step_name_sets_chinese_title_in_list_and_heading(win):
    """用户填「步骤名称」后，步骤列表与属性标题用它；清空则回退中文类型名。"""
    page = _page(win)
    page.editor.new_recipe("r_title")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.VISION_RUN)
    page._sel_step = sid
    page._reload()

    name = _field_by_row_label(_step_group(page), "步骤名称")
    name.setText("第二站视觉判定")
    name.editingFinished.emit()                 # 等价用户回车/移开焦点
    page._reload()

    texts = [page.step_list.item(i).text() for i in range(page.step_list.count())]
    assert any("第二站视觉判定" in t for t in texts)
    assert "第二站视觉判定" in _step_group(page).title()

    # 清空名称 -> 列表显示中文类型名（不再露英文 id）
    page.editor.set_step_title(sid, "")
    page._reload()
    text = page.step_list.item(0).text()
    assert "视觉判定" in text
    assert sid not in text                       # 英文步骤 ID 不再上屏


def test_step_name_field_shows_current_title(win):
    page = _page(win)
    page.editor.new_recipe("r_title2")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.CAMERA_CAPTURE)
    page.editor.set_step_title(sid, "拍照")
    page._sel_step = sid
    page._reload()
    assert _field_by_row_label(_step_group(page), "步骤名称").text() == "拍照"


def test_step_properties_can_add_post_checks(win):
    """DEC-064：步骤属性有可选「判断属性（步骤完成后）」，可添加并保存三类判断。

    DEC-066：判断属性从步骤表单里移出，是属性栏顶层的独立分组，排「步骤属性」之后。
    """
    page = _page(win)
    page.editor.new_recipe("r_check")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.CAMERA_CAPTURE)
    page.editor.set_step_field(sid, "camera_ref", "ds86_01")
    page._sel_step = sid
    page._reload()

    step_group = _step_group(page)
    sub = _prop_top_group(page, "判断属性")
    # 顶层独立分组：是步骤属性的**兄弟**（排在后面），不在步骤表单内部
    assert sub not in step_group.findChildren(QGroupBox)
    assert _prop_index(page, step_group) < _prop_index(page, sub)
    assert any("未设置" in lb.text() for lb in sub.findChildren(QLabel))

    # 通过编辑器写入三类判断（与界面保存走同一 editor API），再重载看属性栏能显示
    page.editor.set_step_checks(sid, [
        {"kind": "robot_pose", "joint_target_ref": "teach_01", "pos_tol_mm": 2.0},
        {"kind": "device_field", "device_ref": "eco65_01", "field_name": "state",
         "op": "eq", "value": "READY"},
        {"kind": "step_output", "source_step": "cap", "field_name": "capture_id",
         "op": "ne", "value": ""},
    ])
    page._sel_step = sid
    page._reload()
    step = page.editor.model.sequence[0]
    assert len(step.post_checks) == 3
    assert [c.kind for c in step.post_checks] == ["robot_pose", "device_field", "step_output"]
    # 界面里的判断区仍存在（不因渲染失败而丢）：三个判断行、类型下拉按顺序复原
    sub = _prop_top_group(page, "判断属性")
    from PySide6.QtWidgets import QFrame
    rows = [b for b in sub.findChildren(QFrame) if b.objectName() == "BlockBox"]
    kinds = [b.findChild(QComboBox).currentData() for b in rows]
    assert kinds == ["robot_pose", "device_field", "step_output"]


def test_prop_panel_fixed_width_no_horizontal_scroll(win):
    """DEC-066：属性栏宽度固定、不横向滚动；表单行空间不足时换行（WrapLongRows）。"""
    from PySide6.QtWidgets import QFormLayout

    page = _page(win)
    page.editor.new_recipe("r_width")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.CAMERA_CAPTURE)
    page.editor.set_step_field(sid, "camera_ref", "ds86_01")
    page._sel_step = sid
    page._reload()

    # 固定宽度：最小 = 最大 = 常量（窗口缩放/内容变化都不会把它撑宽或挤窄）
    assert page.prop_card.minimumWidth() == page.prop_card.maximumWidth() \
        == page._PROP_CARD_WIDTH
    # 不出现横向滚动条（内容放不下靠换行）
    assert page.prop_scroll.horizontalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    # 三个表单（执行方案属性/步骤属性/判断区行内表单）都允许行内换行
    for group in (_prop_top_group(page, "执行方案属性"), _step_group(page),
                  _prop_top_group(page, "判断属性")):
        form = group.layout()
        if isinstance(form, QFormLayout):
            assert form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.WrapLongRows


def test_guided_step_selects_fine_offset_profile(win):
    """DEC-065：引导定位步骤可选择参数配置页里的额外精定位参数套。"""
    from application.services.guidance_config import FineOffsetProfile
    w, _vm, stack, app = win
    gs = stack.motion.guidance.config_store
    gs.config.fine_profiles.append(FineOffsetProfile(
        profile_id="精定位B", tcp_x=12.0, desc="ui"))
    page = _page(win)
    page.refresh(); app.processEvents()
    page.editor.new_recipe("r_fine_profile")
    page.editor.set_offline_draft(False)
    sid = page.editor.add_step(NodeType.GUIDED_POSITION)
    page.editor.set_step_field(sid, "camera_ref", "ds86_01")
    page._sel_step = sid
    page._reload()
    combo = _combo_by_row_label(_step_group(page), "精定位参数")
    idx = combo.findData("精定位B")
    assert idx >= 0
    combo.setCurrentIndex(idx); app.processEvents()
    step = next(s for s in page.editor.model.sequence if s.id == sid)
    assert step.fine_profile_ref == "精定位B"


# ---------------------------------------------------------------- DEC-108 删除执行方案
def test_page_has_delete_button(win):
    page = _page(win)
    assert page.btn_delete.text() == "删除"


def test_delete_selected_recipe_removes_it_from_combo(win, monkeypatch):
    """新建后「删除」：确认后从下拉/草稿里消失，选择回退到其他方案。"""
    import application.presentation.pages.recipes as R
    page = _page(win)
    page.editor.new_recipe("r_del")
    page._reload()
    # 只删刚建的内存草稿：绝不碰仓库里的示例方案文件（来源路径为空）
    assert page.editor.recipe_id == "r_del"
    assert page.editor.source_path() is None
    monkeypatch.setattr(R, "_confirm", lambda *a, **k: True)
    page.btn_delete.click()
    ids = [page.recipe_combo.itemData(i)
           for i in range(page.recipe_combo.count())]
    assert "r_del" not in ids
    assert "r_del" not in page.editor.service.drafts()
    assert "已删除执行方案 r_del" in page.result_label.text()


def test_delete_is_guarded_while_recipe_is_running(win, monkeypatch):
    """运行中的方案拒绝删除（DEC-108），确认框都不弹。"""
    import application.presentation.pages.recipes as R
    _w, vm, _stack, _app = win
    page = _page(win)
    rid = page.editor.recipe_id
    assert rid in page.editor.service.drafts()
    monkeypatch.setattr(vm, "recipe_in_use", lambda r: r == rid)
    asked = {"confirm": False}
    monkeypatch.setattr(R, "_confirm",
                        lambda *a, **k: asked.__setitem__("confirm", True) or True)
    warned = []
    monkeypatch.setattr(R, "_warn", lambda _p, t, m: warned.append(m))
    page.btn_delete.click()
    assert rid in page.editor.service.drafts()
    assert warned and "正在执行中" in warned[0]
    assert not asked["confirm"]
