"""引导定位步骤 (vision.guided_position, DEC-033): 模型/校验/编辑器 + 执行节点闭环。

验证边界（离线）：
- 步骤 = **一个**独立步骤，内部粗→精依次执行；段选择只能是 (coarse, fine) 的非空子集；
- 每段**人工确认**后才按 Bolt 工具系 [X,0,Z] 偏移；拒绝/检测失败/超上限 → 步骤失败；
- 与手动「引导定位」按钮同一套闸（运动闸 + 已连接）；离线 NullMotionArm 一律拒绝；
- 引导服务只支持 DS86：步骤相机与引导服务绑定不一致时明确失败，不偷换。
硬件一律 NOT_RUN（用例只用 FakeArm/FakeGuidance，不接真机）。
"""
from __future__ import annotations

import time

import pytest
from py_trees.common import Status

from application.bootstrap.composition import build_offline_stack
from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import NodeType, TaskState
from application.services.guidance_service import CircleHit, GuidanceResult
from application.services.motion_config import MotionConfigStore
from application.services.motion_runner import MotionRunner
from application.services.recipes.editor import RecipeEditor
from application.services.recipes.models import RecipeModel, StepModel
from application.services.recipes.service import ReferenceCatalog, validate_recipe
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.steps import GuidedPositionNode, build_node

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


# ---------------------------------------------------------------- 假硬件（不接真机）
class FakeGuidance:
    def __init__(self, *, coarse=(10.0, -5.0), fine=(1.0, 0.5), ok=True,
                 message="") -> None:
        self.coarse, self.fine = coarse, fine
        self.ok, self.message = ok, message
        self.camera_id = "ds86_01"
        self.calls = 0

    def detect(self) -> GuidanceResult:
        self.calls += 1
        if not self.ok:
            return GuidanceResult(False, self.message)
        return GuidanceResult(
            True, "检测成功", cam_xyz=(12.0, 3.0, 500.0), coarse=self.coarse,
            fine=self.fine, circle=CircleHit(0, 12.0, 3.0, 25.0),
            used_depth_mm=500.0)


class FakeArm:
    def __init__(self) -> None:
        self.connected = True
        self.offsets: list[tuple] = []

    def move_offset(self, off, v=20, r=0, connect=0, block=1, stop_check=None,
                    tol=0.002, timeout=30.0):
        self.offsets.append((tuple(off), v))
        return 0

    def ensure_production_frames(self):
        return (True, [])

    def stop(self):
        return 0


def _gate_on(monkeypatch) -> None:
    """DEC-085：real 模式无环境变量闸，保留空函数避免改动大量调用点。"""
    return None


def _runner(tmp_path, *, guidance=None) -> MotionRunner:
    st = MotionConfigStore(str(tmp_path / "motion.yaml"), seed_if_missing=False)
    st.load()
    return MotionRunner(FakeArm(), st, guidance=guidance or FakeGuidance())


def _ctx() -> RunContext:
    return RunContext(run_id="r-guide", versions=VersionSnapshot("x", "v0", "-", "-"))


def _node(motion, stack, **kw) -> GuidedPositionNode:
    return GuidedPositionNode("guide:g", motion=motion, step_id="g", ctx=_ctx(),
                              executor=stack.executor, outputs=OutputStore(), **kw)


def _drive(node, *, answer="是", timeout=8.0):
    """替代运行页：等节点报 prompt 就提交 answer，直到节点结束。"""
    node.initialise()
    end = time.monotonic() + timeout
    status = node.update()
    while status is Status.RUNNING and time.monotonic() < end:
        p = node.pending_prompt
        if p:
            assert node.provide(answer, "测试员") is True
        time.sleep(0.02)
        status = node.update()
    return status


def _stack(tmp_path):
    return build_offline_stack(db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
                               recipe_path=RECIPE)


# ---------------------------------------------------------------- 模型/校验
def _catalog(cams=("ds86_01",)) -> ReferenceCatalog:
    return ReferenceCatalog(cameras=set(cams))


def _recipe(step: dict) -> RecipeModel:
    return RecipeModel.model_validate({
        "recipe_id": "r", "version": "v1", "state": "draft", "sequence": [step]})


def test_step_defaults_are_compile_ready():
    s = StepModel(id="g", type=NodeType.GUIDED_POSITION, camera_ref="ds86_01")
    assert s.guide_stages == ("coarse", "fine")
    assert s.guide_speed_pct is None          # None = 跟随引导配置
    assert s.guide_max_offset_mm == 0.0       # 0 = 不设上限


def test_valid_step_passes_validation():
    report = validate_recipe(_recipe(
        {"id": "g", "type": "vision.guided_position", "camera_ref": "ds86_01"}),
        _catalog())
    assert report.ok, report.entries


@pytest.mark.parametrize("stages,needle", [
    ([], "必须非空"),
    (["coarse", "nope"], "只含"),
    (["fine", "fine"], "重复"),
    (["fine", "coarse"], None),   # 顺序被接受（执行器固定粗→精），不算错
])
def test_stage_selection_value_layer(stages, needle):
    report = validate_recipe(_recipe(
        {"id": "g", "type": "vision.guided_position", "camera_ref": "ds86_01",
         "guide_stages": stages}), _catalog())
    if needle is None:
        assert report.ok, report.entries
    else:
        assert not report.ok
        assert any("guide_stages" == e.get("field_name") and needle in e["message"]
                   for e in report.entries)


def test_speed_and_limit_value_layer():
    bad_speed = validate_recipe(_recipe(
        {"id": "g", "type": "vision.guided_position", "camera_ref": "ds86_01",
         "guide_speed_pct": 101}), _catalog())
    assert not bad_speed.ok and any(e.get("field_name") == "guide_speed_pct"
                                    for e in bad_speed.entries)
    bad_limit = validate_recipe(_recipe(
        {"id": "g", "type": "vision.guided_position", "camera_ref": "ds86_01",
         "guide_max_offset_mm": -1.0}), _catalog())
    assert not bad_limit.ok and any(e.get("field_name") == "guide_max_offset_mm"
                                    for e in bad_limit.entries)


def test_structure_requires_camera_and_reference_must_exist():
    report = validate_recipe(_recipe(
        {"id": "g", "type": "vision.guided_position"}), _catalog())
    assert not report.ok                                  # 缺 camera_ref（结构层）
    report2 = validate_recipe(_recipe(
        {"id": "g", "type": "vision.guided_position", "camera_ref": "d405_01"}),
        _catalog())
    assert not report2.ok and any("相机" in e["message"] for e in report2.entries)


def test_editor_add_step_defaults_to_ds86():
    editor = RecipeEditor(_RecipeService(_catalog()))
    editor.new_recipe("r")
    sid = editor.add_step(NodeType.GUIDED_POSITION)
    step = next(s for s in editor.model.sequence if s.id == sid)
    assert step.camera_ref == "ds86_01"
    assert step.guide_stages == ("coarse", "fine")
    assert editor.validate().ok


# ------------------------------------------------- 相机引用以设备配置为准 (DEC-034)
def test_guided_step_rejects_non_ds86_even_if_in_catalog():
    """选了 D405 等其它相机：**校验期**就报「只能用 DS86」，不留到运行期。"""
    report = validate_recipe(_recipe(
        {"id": "g", "type": "vision.guided_position", "camera_ref": "d405_01"}),
        _catalog(("d405_01", "ds86_01")))
    assert not report.ok
    entry = next(e for e in report.entries if e.get("field_name") == "camera_ref")
    assert "只能用 DS86" in entry["message"]
    assert "d405_01" in entry["message"]


def test_guided_step_ds86_must_be_enabled_in_device_config():
    """ds86_01 不在「设备配置里已启用的相机」→ 校验给出启用指引（不是含糊的未知相机）。"""
    report = validate_recipe(_recipe(
        {"id": "g", "type": "vision.guided_position", "camera_ref": "ds86_01"}),
        _catalog(("d405_01",)))
    assert not report.ok
    entry = next(e for e in report.entries if e.get("field_name") == "camera_ref")
    assert "已启用" in entry["message"] and "ds86_01" in entry["message"]


def test_editor_guided_default_keeps_ds86_without_live_catalog():
    """没接 DS86 也能建步骤/保存；错误只落在校验报告（运行时再如实失败）。"""
    editor = RecipeEditor(_RecipeService(_catalog(("d405_01",))))
    editor.new_recipe("r")
    sid = editor.add_step(NodeType.GUIDED_POSITION)
    step = next(s for s in editor.model.sequence if s.id == sid)
    assert step.camera_ref == "ds86_01"            # 绝不退到 D405
    report = editor.validate()
    assert not report.ok
    assert any(e.get("field_name") == "camera_ref" for e in report.entries)


def test_offline_catalog_follows_enabled_device_config(tmp_path):
    """引用目录 = 设备配置里**已启用**的相机 ∪ 本次会话；未启用的不进，未连接的照进
    （配方可编辑，运行期如实失败）。"""
    from application.services.device_config import (
        CameraEntry, DeviceConfig, DeviceConfigStore)

    cfg = tmp_path / "devices.yaml"
    store = DeviceConfigStore(str(cfg), seed_if_missing=False)
    store._config = DeviceConfig(cameras=[              # noqa: SLF001 - 测试构造
        CameraEntry("cam_new", "现场新增", "local_sdk", enabled=True, confirmed=True),
        CameraEntry("cam_off", "停用", "local_sdk", enabled=False),
    ])
    store.save()
    stack = build_offline_stack(db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
                                recipe_path=RECIPE, device_config_path=str(cfg))
    try:
        cams = set(stack.recipes.catalog.cameras)
        assert "cam_new" in cams                 # 启用即可引用（未连接也能编辑配方）
        assert "cam_off" not in cams             # 未启用不冒充可用
        assert "cam_new" not in stack.cameras    # 但本次会话确实没有它
    finally:
        stack.close()


class _RecipeService:
    """最小 RecipeService 外形（编辑器只需 drafts/get_draft/draft/publish 面）。"""

    def __init__(self, catalog) -> None:
        from application.services.recipes.service import RecipeService
        self._svc = RecipeService(catalog)
        self.catalog = catalog

    def __getattr__(self, name):
        return getattr(self._svc, name)


# ---------------------------------------------------------------- 节点闭环
def test_node_runs_two_stages_with_confirmed_offsets(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    guide = FakeGuidance()
    runner = _runner(tmp_path, guidance=guide)
    node = _node(runner, stack, camera_ref="ds86_01")

    assert _drive(node) is Status.SUCCESS
    assert guide.calls == 2
    assert [o[0] for o in runner.arm.offsets] == [
        (0.010, 0.0, -0.005, 0.0, 0.0, 0.0),      # 粗：X=10mm Z=-5mm
        (0.001, 0.0, 0.0005, 0.0, 0.0, 0.0),      # 精：X=1mm Z=0.5mm
    ]
    rows = node.outputs.get("", "g", "guide")
    assert [r["stage"] for r in rows] == ["coarse", "fine"]
    assert all(r["confirmed"] and r["applied"] for r in rows)
    stack.close()


def test_node_refusal_fails_without_motion(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    runner = _runner(tmp_path)
    node = _node(runner, stack, camera_ref="ds86_01")

    assert _drive(node, answer="否") is Status.FAILURE
    assert runner.arm.offsets == []
    assert "未确认" in (node.failure_reason or "")
    stack.close()


def test_node_over_limit_fails_before_confirm(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    runner = _runner(tmp_path)
    node = _node(runner, stack, camera_ref="ds86_01", max_offset_mm=8.0)

    # 粗段 |X|=10mm > 8mm：直接失败，**不出确认、不移动**
    assert _drive(node) is Status.FAILURE
    assert runner.arm.offsets == []
    assert "超上限" in (node.failure_reason or "")
    assert runner.state["guide_results"][0]["applied"] is False
    stack.close()


def test_node_single_fine_stage(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    runner = _runner(tmp_path)
    node = _node(runner, stack, camera_ref="ds86_01", stages=("fine",))

    assert _drive(node) is Status.SUCCESS
    assert runner.arm.offsets == [((0.001, 0.0, 0.0005, 0.0, 0.0, 0.0), 10)]
    stack.close()


def test_node_detection_failure_is_step_failure(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    runner = _runner(tmp_path, guidance=FakeGuidance(ok=False, message="未找到候选目标"))
    node = _node(runner, stack, camera_ref="ds86_01")

    assert _drive(node) is Status.FAILURE
    assert "未找到候选目标" in (node.failure_reason or "")
    stack.close()


# -------------------------------------------- 检测阶段抛异常/卡死 (DEC-058)
class RaisingGuidance(FakeGuidance):
    """检测阶段抛异常（例如相机读帧异常没有被引导服务兜住）。"""

    def detect(self) -> GuidanceResult:
        self.calls += 1
        raise RuntimeError("模拟：读帧异常（未兜底）")


class StalledGuidance(FakeGuidance):
    """检测阶段卡住（例如 SDK read_frame / 圆宿主不返回）。"""

    def __init__(self, *, stall_s: float = 3.0) -> None:
        super().__init__()
        self.stall_s = stall_s

    def detect(self) -> GuidanceResult:
        time.sleep(self.stall_s)
        return super().detect()


def test_node_detection_exception_fails_instead_of_hanging(tmp_path, monkeypatch):
    """线程内未捕获异常必须让步骤**失败并说明原因**，不能停在 RUNNING（现场症状）。"""
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    runner = _runner(tmp_path, guidance=RaisingGuidance())
    node = _node(runner, stack, camera_ref="ds86_01")

    status = _drive(node, timeout=5.0)
    assert status is Status.FAILURE
    assert "检测异常" in (node.failure_reason or "")
    assert "读帧异常" in (node.failure_reason or "")
    assert runner.state.get("mode") == "error"
    assert runner.arm.offsets == []                 # 绝不下发动作
    stack.close()


def test_node_detect_stall_times_out_and_stops_without_motion(tmp_path, monkeypatch):
    """检测超过 guide_detect_timeout_s：步骤失败 + 停止引导，不再永远"执行中"。"""
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    runner = _runner(tmp_path, guidance=StalledGuidance(stall_s=5.0))
    runner.store.config.profile.guide_detect_timeout_s = 0.4
    node = _node(runner, stack, camera_ref="ds86_01")

    t0 = time.monotonic()
    status = _drive(node, timeout=6.0)
    waited = time.monotonic() - t0
    assert status is Status.FAILURE
    assert "没有返回" in (node.failure_reason or "")
    assert waited < 3.0                             # 按超时返回, 不是等检测自己结束
    assert runner.state.get("mode") == "idle"       # 看门狗已 stop(), 不留"执行中"
    assert runner.arm.offsets == []
    stack.close()


def test_node_confirm_wait_is_not_killed_by_detect_watchdog(tmp_path, monkeypatch):
    """等人工确认时不看门狗（否则操作员思考久一点就被判超时）。"""
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    runner = _runner(tmp_path)
    runner.store.config.profile.guide_detect_timeout_s = 0.2
    node = _node(runner, stack, camera_ref="ds86_01")

    node.initialise()
    assert node.update() is Status.RUNNING
    end = time.monotonic() + 3.0
    while node.pending_prompt is None and time.monotonic() < end:
        time.sleep(0.02)
        node.update()
    assert node.pending_prompt is not None          # 已进入确认等待
    time.sleep(0.5)                                 # 远超 0.2s
    assert node.update() is Status.RUNNING
    assert node.provide("是", "测试员") is True
    stack.close()


def test_node_offline_null_arm_is_refused(tmp_path):
    """离线栈默认 NullMotionArm：与手动「引导定位」同一闸 -> 步骤失败, 不留假动作。"""
    stack = _stack(tmp_path)
    node = _node(stack.motion, stack, camera_ref="ds86_01")
    assert node.update() is Status.FAILURE
    assert "拒" in (node.failure_reason or "")
    stack.close()


def test_node_camera_mismatch_refused(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    runner = _runner(tmp_path)                # 引导服务 camera_id=ds86_01
    node = _node(runner, stack, camera_ref="d405_01")
    assert node.update() is Status.FAILURE
    assert "ds86_01" in (node.failure_reason or "")
    assert "d405_01" in (node.failure_reason or "")
    stack.close()


# ---------------------------------------------------------------- 配料->执行器全链路
def test_recipe_guided_step_end_to_end_with_operator_confirm(tmp_path, monkeypatch):
    """TaskService 编译 guided 步骤；ViewModel 提交确认；事件记录操作者与偏移。"""
    _gate_on(monkeypatch)
    stack = _stack(tmp_path)
    rid = "guided_e2e"
    stack.recipes.draft({
        "recipe_id": rid, "version": "v1", "state": "draft",
        "sequence": [
            {"id": "g", "type": "vision.guided_position", "camera_ref": "ds86_01",
             "guide_stages": ["coarse", "fine"], "guide_speed_pct": 6,
             "guide_max_offset_mm": 20.0},
        ],
    })
    runner = _runner(tmp_path)
    stack.task.motion = runner              # 用可动的假臂替代离线 NullMotionArm
    from application.presentation.viewmodel import WorkbenchViewModel
    vm = WorkbenchViewModel(task=stack.task, allow_offline_draft=True)
    vm.select_recipe(rid)
    assert vm.can_start()
    vm.start_current()

    end = time.monotonic() + 15.0
    prompts: list[dict] = []
    state = stack.task.state_machine.state
    while state in (TaskState.VALIDATING, TaskState.READY, TaskState.RUNNING) \
            and time.monotonic() < end:
        p = vm.pending_operator_prompt()
        if p:
            prompts.append(p)
            assert vm.provide_operator("是", "张三") is True
        # DEC-042: 作业台启动的作业每步之后有确认闸（此处自动确认）
        if vm.pending_step_prompt():
            assert vm.confirm_step("确认", "张三") is True
        vm.tick()
        state = stack.task.state_machine.state
        time.sleep(0.01)

    assert state is TaskState.COMPLETED
    assert len(prompts) == 2
    assert "粗定位" in prompts[0]["title"] and "精定位" in prompts[1]["title"]
    assert "X=10.00mm" in prompts[0]["message"]
    assert [o[0] for o in runner.arm.offsets] == [
        (0.010, 0.0, -0.005, 0.0, 0.0, 0.0), (0.001, 0.0, 0.0005, 0.0, 0.0, 0.0)]
    assert all(o[1] == 6 for o in runner.arm.offsets)      # 步骤速度覆盖引导默认
    node = next(n for k, n in stack.task.compiled.nodes.items() if k.endswith("/g"))
    assert node.status is Status.SUCCESS
    # 留证：每段一条事件，带操作者
    run_id = stack.task.state_machine.run_id
    evs = [e for e in stack.records.replay(run_id) if e["event_type"] == "guide.position"]
    assert len(evs) == 2 and all(e["advice"] == "operator=张三" for e in evs)
    stack.close()
