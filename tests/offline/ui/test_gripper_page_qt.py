"""夹爪板调试页 UI 测试（DEC-097）。离屏 Qt，不连接真实板。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from application.bootstrap.composition import build_offline_stack  # noqa: E402
from application.presentation.app import MainWindow, UiContext  # noqa: E402
from application.presentation.pages.gripper import GripperPage, StepDialog  # noqa: E402
from application.presentation.viewmodel import WorkbenchViewModel  # noqa: E402
from application.services.gripper_debug import GripperDebugStore, SEQUENCE_FILE  # noqa: E402

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture()
def win(tmp_path, qapp):
    import gc
    stack = build_offline_stack(db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
                                recipe_path=RECIPE)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    w = MainWindow(UiContext(stack=stack, vm=vm))
    w.resize(1440, 1024)
    try:
        yield w, qapp, tmp_path
    finally:
        try:
            w.shutdown(); w.close(); w.deleteLater(); qapp.processEvents()
        except Exception:  # noqa: BLE001
            pass
        stack.close()
        del w
        gc.collect()
        qapp.processEvents()


def _page(win) -> GripperPage:
    w, app, _tmp = win
    w._select("gripper")
    w.refresh_all(); app.processEvents()
    page = w.pages["gripper"]
    assert isinstance(page, GripperPage)
    return page


def test_nav_has_gripper_debug_page(win):
    w, _app, _tmp = win
    assert "gripper" in w.pages
    labels = [b.text().strip() for b in w._group.buttons()]
    assert "夹爪调试" in labels


def test_page_loads_default_sequence_without_connecting(win):
    page = _page(win)
    assert page.seq_combo.currentText() == "开合测试"
    assert page.step_table.rowCount() == 2
    assert page.step_table.item(0, 2).text().startswith("夹爪打开")
    assert page.connect_btn.isEnabled()
    assert not page.send_btn.isEnabled()      # 未连接不能下发


def test_step_dialog_builds_valid_step(win):
    page = _page(win)
    dlg = StepDialog(page)
    dlg.name_edit.setText("夹紧到 250")
    idx = dlg.cmd_combo.findData(0x02)
    dlg.cmd_combo.setCurrentIndex(idx)
    dlg.params["jj_pos"].setText("250")
    dlg.wait_combo.setCurrentIndex(dlg.wait_combo.findData("done"))
    dlg.timeout_spin.setValue(20000)
    step = dlg._build()
    step.validate()
    assert step.command == 0x02
    assert step.params["jj_pos"] == 250
    assert step.wait == "done"
    assert step.timeout_ms == 20000


def test_save_config_and_sequences_to_runtime_yaml(win):
    page = _page(win)
    page._save_all()
    path = os.path.join(str(win[2]), SEQUENCE_FILE)
    assert os.path.exists(path)
    cfg, seqs = GripperDebugStore(path).load()
    assert cfg.port == 502
    assert [s.name for s in seqs] == ["开合测试"]


def test_sequence_edit_marks_and_runs_only_when_connected(win):
    page = _page(win)
    seq = page._current_sequence()
    assert seq is not None
    seq.steps.append(seq.steps[0])
    page._refresh_step_table()
    assert page.step_table.rowCount() == 3
    # 未连接时运行按钮被禁用（不弹窗、不下发）
    page._sync_enabled()
    assert not page.run_seq_btn.isEnabled()
