"""A17 Bootstrap/生命周期 + A18 作业台 ViewModel 测试 (T18/T19/T21/T23/T24/T25)."""
from __future__ import annotations

import pytest

from application.bootstrap.composition import build_offline_stack
from application.bootstrap.lifecycle import (
    LifecycleManager,
    ProfileError,
    assert_no_fake_in_production,
    assert_profile,
)
from application.contracts.enums import TaskState
from application.presentation.pages.workbench import GUI_REQUIRED
from application.presentation.theme import STATE_COLORS, THEME, state_colors
from application.presentation.viewmodel import WorkbenchViewModel

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


def _stack(tmp_path, **kw):
    return build_offline_stack(db_path=str(tmp_path / "r.db"),
                               runtime_dir=str(tmp_path), recipe_path=RECIPE, **kw)


# ---------------- A17 ----------------
def test_startup_has_no_motion(tmp_path):
    stack = _stack(tmp_path)
    lm = LifecycleManager(stack=stack, profile="offline")
    lm.start()
    assert lm.started
    assert stack.robot.read_state()["move_count"] == 0
    lm.shutdown()
    assert lm.shutdown_done and stack.closed


def test_shutdown_idempotent(tmp_path):
    stack = _stack(tmp_path)
    lm = LifecycleManager(stack=stack, profile="offline")
    lm.start()
    lm.shutdown()
    lm.shutdown()
    assert stack.closed


def test_offline_profile_creates_no_real_connections(tmp_path):
    stack = _stack(tmp_path)
    assert stack.profile == "offline"
    assert type(stack.robot).__name__ == "FakeRobot"
    assert type(stack.cameras["ds86_01"]).__name__ == "FakeCamera"
    lm = LifecycleManager(stack=stack, profile="offline")
    lm.start(); lm.shutdown()


def test_production_rejects_fake_components():
    with pytest.raises(ProfileError):
        assert_no_fake_in_production("production", [("robot", True), ("camera", False)])
    assert_no_fake_in_production("production", [("robot", False), ("camera", False)])


def test_unknown_profile_rejected():
    with pytest.raises(ProfileError):
        assert_profile("staging")


def test_shutdown_stops_active_task(tmp_path):
    stack = _stack(tmp_path)
    lm = LifecycleManager(stack=stack, profile="offline")
    lm.start()
    stack.task.start("ds86_two_station_example", allow_draft=True)
    stack.task.tick()
    lm.shutdown()
    assert "stop" in lm.shutdown_steps
    assert stack.task.state_machine.state in (TaskState.STOPPED, TaskState.FAULT)


# ---------------- A18 ViewModel ----------------
def test_recipe_auto_selected_and_draft_blocks_start(tmp_path):
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task)          # 默认不允许离线草稿
    st = vm.state()
    # 执行方案目录自动发现（DEC-035）：configs/recipes 里的方案都进下拉，按文件名序。
    # 目录里可能还有用户自存的方案（如 fastening_bolt），只断言示例在内、顺序有序。
    assert st.recipes == sorted(st.recipes)
    assert "ds86_two_station_example" in st.recipes
    assert st.selected_recipe == "ds86_two_station_example"
    assert st.task_state == "IDLE"
    assert any("未发布" in r for r in st.blocking_reasons)   # 明确阻止原因
    assert st.buttons["start"] is False                       # 有原因故不可启动
    stack.close()


def test_allow_offline_draft_enables_start(tmp_path):
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    st = vm.state()
    assert st.blocking_reasons == []
    assert st.buttons["start"] is True               # 死锁已修复: 可点“启动”
    stack.close()


def test_default_recipe_is_the_startup_recipe_not_alphabetical_first(tmp_path):
    """DEC-041: 作业台默认选中**启动时载入**的执行方案（--recipe 那份）。"""
    stack = _stack(tmp_path)
    options = WorkbenchViewModel(task=stack.task, allow_offline_draft=True).recipes()
    target = next((r for r in options if r != options[0]), options[0])
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True,
                            default_recipe=target)
    assert vm.selected_recipe == target
    vm2 = WorkbenchViewModel(task=stack.task, allow_offline_draft=True,
                             default_recipe="不存在的方案")
    assert vm2.selected_recipe == "ds86_two_station_example"   # 退回目录第一条
    stack.close()


def test_no_recipe_gives_clean_reason_not_empty_id(tmp_path):
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.select_recipe(None)
    st = vm.state()
    assert st.blocking_reasons == ["未选择执行方案"]      # 不再是 "无草稿 ''"
    assert st.buttons["start"] is False
    stack.close()


def test_start_current_and_button_matrix_running(tmp_path):
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    run_id = vm.start_current()
    assert run_id.startswith("run-")
    st = vm.state()
    assert st.task_state == "RUNNING"
    assert st.buttons["request_pause"] and st.buttons["terminate"]
    assert st.buttons["stop_all"]
    assert not st.buttons["start"]
    assert not st.buttons["resume"]      # RUNNING 下不能继续 (仅 PAUSED/WAITING_OPERATOR)
    assert vm.hint and "已启动" in vm.hint
    stack.close()


def test_steps_and_devices_populated_after_start(tmp_path):
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.start_current()
    vm.tick()
    st = vm.state(image_source="CURRENT")
    assert st.stations == []                              # 无工位
    assert [s.step_id for s in st.steps] == [
        "move", "capture", "detect", "gate", "mechanism", "evidence"]
    assert st.image_source == "CURRENT"
    assert any(d.left == "eco65_01" for d in st.devices)
    stack.close()


def test_pause_resume_flow(tmp_path):
    """DEC-041: 点「暂停」即在安全边界落定为 PAUSED，「继续作业」随即可用。"""
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.start_current()
    vm.tick()
    vm.request_pause()
    st = vm.state()
    assert st.task_state == "PAUSED"                     # 不再是卡住的 PAUSE_REQUESTED
    assert st.buttons["resume"] and st.buttons["single_step"]
    assert not st.buttons["request_pause"]
    vm.resume(True)
    assert vm.state().task_state == "RUNNING"
    stack.close()


def test_stopped_state_can_single_step_and_it_pauses_again(tmp_path):
    """DEC-101: 停止态「单步」从当前未完成步骤继续（同一 run），再停在安全边界。"""
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.start_current()
    vm.tick()
    run_id = stack.task.run_ctx.run_id
    vm.terminate()
    st = vm.state()
    assert st.task_state == "STOPPED"
    assert st.buttons["single_step"]                    # 停止态可单步
    assert not st.buttons["request_pause"]              # 没在运行不能暂停
    assert not st.buttons["resume"]                     # 没暂停不能继续
    vm.single_step()
    assert vm.state().task_state in ("PAUSED", "WAITING_OPERATOR")
    assert stack.task.run_ctx.run_id == run_id          # 同一 run，不从头
    stack.close()


def test_running_buttons_pause_and_terminate_enabled(tmp_path):
    """DEC-041: 运行中「暂停」「终止流程」必须可点（用户点名）。"""
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.start_current()
    st = vm.state()
    assert st.task_state == "RUNNING"
    assert st.buttons["request_pause"] and st.buttons["terminate"]
    stack.close()


def test_terminate_returns_per_executor_and_hint(tmp_path):
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.start_current()
    vm.tick()
    rows = vm.terminate()
    # DEC-033 起「停止全部」也停运动方案/引导定位（motion 执行器进摘要）：
    # 当时无进行中的作业 -> CONFIRMED（无需停稳核实）。
    assert {r["executor"] for r in rows} == {"eco65_01", "ds86_01", "motion"}
    assert vm.hint_tone == "ok"                       # 全部确认
    st = vm.state()
    assert len(st.stop_summary) == 3                  # 停止摘要逐执行器
    assert st.buttons["terminate"] is False           # 已停止不再可终止
    stack.close()


def test_stop_summary_unknown_shows_bad_tone(tmp_path):
    stack = _stack(tmp_path, stop_verify_unknown_for={"eco65_01"})
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.start_current()
    vm.tick()
    vm.terminate()
    assert vm.hint_tone == "bad"                      # 未知不全绿
    tones = {r.left: r.tone for r in vm.state().stop_summary}
    assert tones["eco65_01"] == "bad"
    stack.close()


def test_no_step_shows_running_after_stop(tmp_path):
    """停止后界面不得仍显示某步骤“RUNNING”(不误报为运行中)."""
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.start_current()
    vm.tick()
    vm.terminate()
    st = vm.state()
    assert st.task_state == "STOPPED"
    assert all(s.status != "RUNNING" for s in st.steps)
    stack.close()


def test_no_recipe_blocks_without_noise(tmp_path):
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True,
                            recipes=[])          # 无可用配方
    st = vm.state()
    assert st.blocking_reasons == ["未选择执行方案"]
    assert not any("无草稿 ''" in r for r in st.blocking_reasons)
    stack.close()


def test_start_rejected_reason_surfaces_in_state(tmp_path):
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=False)
    with pytest.raises(Exception):
        vm.start_current()                            # 草稿未发布 -> 服务层拒绝
    stack.close()


def test_stop_no_confirmation_and_scope_note():
    vm = WorkbenchViewModel(task=_FakeTask(), allow_offline_draft=True)
    assert vm.stop_requires_no_confirmation
    assert "不自动回零" in vm.terminate_scope_note()


def test_theme_tokens_and_state_colors():
    from application.presentation.theme import qss
    assert THEME.deep_teal == "#0A5F5F"
    assert THEME.button_min_height_px >= 40
    assert THEME.font_family == "Microsoft YaHei"
    assert "FAULT" in STATE_COLORS
    fg, bg = state_colors("FAULT")
    assert fg == THEME.danger and bg.startswith("#")
    # 单选选中=实心黑（故障恢复/运动方案等所有 QRadioButton 共用全局样式）
    css = qss()
    assert "QRadioButton::indicator:checked" in css
    assert "#111111" in css


def test_qt_page_guarded_import():
    assert isinstance(GUI_REQUIRED, bool)


class _FakeTask:
    """不依赖完整栈的 ViewModel 边界测试用."""
    class _SM:
        run_id = ""
        state = TaskState.IDLE
        is_active = False
        stop_scope = None

        def allows_button(self, name):
            return False
    state_machine = _SM()
    run_ctx = None
    compiled = None
    ledger = None
    stop_coordinator = None
    executor = None
    recipe_service = None

    def available_recipes(self):
        return []

    def validate(self, recipe_id, allow_draft=False):
        return []

    def tick(self):
        return TaskState.IDLE

    def request_pause(self):
        return TaskState.IDLE

    def confirm_pause(self, safe):
        return TaskState.IDLE

    def resume(self, reconciled=True):
        return TaskState.IDLE

    def single_step(self, recipe_id=None, allow_draft=False, step_confirm=False):
        return TaskState.IDLE

    def start(self, recipe_id, allow_draft=False, step_confirm=False):
        return "run-x"

    def terminate(self, reason="", scope=None):
        from application.contracts.handles import StopSummary
        return StopSummary(scope=scope, reason=reason, request_id="r")


# ---------------- 引导定位每段确认 (DEC-033) ----------------
def test_viewmodel_guided_confirm_is_restricted_to_declared_options():
    """运行页确认只接受提示里**声明过的选项**（不提供自由文本通道）。"""
    from types import SimpleNamespace

    class _Node:
        def __init__(self):
            self.pending = {"title": "引导定位 · 粗定位",
                            "message": "偏差 X=10.00mm Z=-5.00mm",
                            "options": ["是", "否"], "step_id": "g"}
            self.got = []

        @property
        def pending_prompt(self):
            return self.pending

        def provide(self, choice, operator="operator"):
            if self.pending is None or choice not in self.pending["options"]:
                return False
            self.got.append((choice, operator))
            self.pending = None
            return True

    task = _FakeTask()
    node = _Node()
    task.compiled = SimpleNamespace(nodes={"run-x/g": node})
    vm = WorkbenchViewModel(task=task, allow_offline_draft=True)
    assert vm.pending_operator_prompt()["options"] == ["是", "否"]
    assert vm.provide_operator("移动吧", "甲") is False      # 未声明选项被拒
    assert node.got == []
    assert vm.provide_operator("是", "甲") is True
    assert node.got == [("是", "甲")]
    assert vm.pending_operator_prompt() is None             # 提供后提示消失


def test_stop_all_with_active_motion_reports_unknown(tmp_path):
    """有进行中的运动作业时停止：已下发停止但**停稳未核实** -> motion=UNKNOWN、任务 FAULT。"""
    stack = _stack(tmp_path)
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.start_current()
    vm.tick()
    stack.motion._state["mode"] = "guide"            # 模拟有进行中的作业（SLF001 测试用）
    rows = vm.terminate()
    states = {r["executor"]: r["state"] for r in rows}
    assert states["motion"] == "UNKNOWN"              # 不把「发了停止」当「已停稳」
    assert stack.task.state_machine.state is TaskState.FAULT
    stack.close()
