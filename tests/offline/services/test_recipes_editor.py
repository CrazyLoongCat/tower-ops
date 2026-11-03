"""配方草稿编辑器（E20 页的引擎，DEC-024）: 增删改 / 校验定位 / 发布闸。

纯 Python（无 Qt），故放在 services 下；页面只用它，不自己判业务。
"""
from __future__ import annotations

import os

import pytest

from application.contracts.enums import NodeType
from application.contracts.targets import JointTarget
from application.domain.actions import ActionCatalog, ActionDefinition, ParamSpec
from application.domain.resources import MOTION_GLOBAL
from application.services.recipes.editor import RecipeEditor, step_title
from application.services.recipes.service import (
    PublishError, ReferenceCatalog, RecipeService,
)
from application.services.methods import default_methods
from application.workflow.subflows import SubflowRegistry, SubflowTemplate

PLAN = "取件方案"
DET = "检测-A"
PIPE = "approved_ds86_circle_pipeline"


def _catalog() -> ReferenceCatalog:
    tpl = SubflowTemplate(template_id="capture_and_detect", version="v1",
                          steps=(),
                          allow_overrides=("deadline_ms",))
    return ReferenceCatalog(
        joint_targets={"teach_01": JointTarget(joint_deg=[0] * 6,
                                               waypoint_ref="taught_1")},
        pipelines={PIPE},
        actions=ActionCatalog([ActionDefinition(
            action_id="approved_single_board_action", device_id="stm32_01",
            resource_ids=(MOTION_GLOBAL,),
            parameters=(ParamSpec(name="force", minimum=0, maximum=100,
                                  required=False),), timeout_ms=1000)]),
        cameras={"ds86_01"},
        devices={"eco65_01", "ds86_01"},
        subflows=SubflowRegistry([tpl]),
        motion_plans={PLAN},
        detections={DET: PIPE},
        methods=default_methods(),
    )


def _editor() -> RecipeEditor:
    return RecipeEditor(RecipeService(_catalog()))


def _ready_recipe(ed: RecipeEditor, rid: str = "r1") -> str:
    ed.new_recipe(rid)
    ed.set_offline_draft(False)
    return rid


# ---------------------------------------------------------------- 新建/复制
def test_new_recipe_creates_empty_draft_and_refuses_duplicate_id():
    ed = _editor()
    ed.new_recipe("r1")
    assert ed.recipes() == ["r1"]
    assert ed.model.recipe_id == "r1"
    assert ed.model.state == "draft"
    assert not ed.model.sequence                       # 无工位：只有一条有序步骤表
    with pytest.raises(ValueError, match="已存在"):
        ed.new_recipe("r1")


def test_duplicate_keeps_offline_mark_so_placeholders_never_slip_into_real():
    """从离线示例复制出来的草稿**仍是离线示例**（占位值不许被"另存为真实"）。"""
    ed = _editor()
    _ready_recipe(ed, "base")
    ed.set_offline_draft(True)
    ed.add_step(NodeType.CAMERA_CAPTURE)
    ed.duplicate("base", "copy")
    assert ed.model.recipe_id == "copy"
    assert ed.model.is_offline_draft is True
    assert [s.id for s in ed.model.sequence] == ["capture_1"]
    assert ed.model.state == "draft"
    # 原地改过源草稿不影响副本
    ed.select("base")
    ed.set_version("v9")
    assert ed.service.get_draft("copy").version == "v1"


# ---------------------------------------------------------------- 步骤
def test_step_add_remove_reorder():
    ed = _editor()
    _ready_recipe(ed)
    a = ed.add_step(NodeType.CAMERA_CAPTURE)
    b = ed.add_step(NodeType.VISION_RUN)
    c = ed.add_step(NodeType.RECORD_EVIDENCE)
    assert [a, b, c] == ["capture_1", "detect_1", "evidence_1"]
    assert ed.model.sequence[1].input_from == {"step": "capture_1", "output": "frame"}
    assert ed.model.sequence[2].inputs == ("detect_1",)
    ed.move_step(c, -1)
    assert [s.id for s in ed.model.sequence] == ["capture_1", "evidence_1", "detect_1"]
    ed.remove_step("evidence_1")
    assert [s.id for s in ed.model.sequence] == ["capture_1", "detect_1"]


def test_add_step_rejects_duplicate_step_id():
    ed = _editor()
    _ready_recipe(ed)
    ed.add_step(NodeType.CAMERA_CAPTURE, "cap")
    with pytest.raises(ValueError, match="已存在"):
        ed.add_step(NodeType.CAMERA_CAPTURE, "cap")


def test_defaults_use_catalog_values_so_a_new_step_is_compilable():
    ed = _editor()
    _ready_recipe(ed)
    ed.add_step(NodeType.MECHANISM_ACTION)
    assert ed.model.sequence[0].action_ref == "approved_single_board_action"
    ed.add_step(NodeType.SUBFLOW)
    assert ed.model.sequence[1].subflow_ref == "capture_and_detect"
    ed.add_step(NodeType.REPEAT)
    assert ed.model.sequence[2].max_iterations == 3


# ---------------------------------------------------------------- 运动目标/方案绑定
def test_move_step_carries_its_own_joint_target():
    """无工位后，运动步骤**自带示教点**（joint_target_ref），新增即取目录里第一个。"""
    ed = _editor()
    _ready_recipe(ed)
    sid = ed.add_step(NodeType.ROBOT_MOVE_JOINT)
    assert ed.model.sequence[0].joint_target_ref == "teach_01"
    assert ed.validate().ok
    ed.set_step_joint_target(sid, None)
    report = ed.validate()
    assert not report.ok
    hit = [e for e in report.entries if e["step_id"] == sid
           and e["field_name"] == "joint_target_ref"]
    assert hit


def test_move_step_can_bind_and_unbind_a_named_motion_plan():
    ed = _editor()
    _ready_recipe(ed)
    sid = ed.add_step(NodeType.ROBOT_MOVE_JOINT)
    assert ed.model.sequence[0].motion_plan_ref is None
    ed.set_motion_plan(sid, PLAN)
    assert ed.model.sequence[0].motion_plan_ref == PLAN
    assert ed.validate().ok
    ed.set_motion_plan(sid, None)
    assert ed.model.sequence[0].motion_plan_ref is None
    assert ed.validate().ok


def test_unknown_motion_plan_is_located_on_that_step_and_field():
    ed = _editor()
    _ready_recipe(ed)
    sid = ed.add_step(NodeType.ROBOT_MOVE_JOINT)
    ed.set_motion_plan(sid, "不存在的方案")
    report = ed.validate()
    assert not report.ok
    hit = [e for e in report.entries if e["step_id"] == sid
           and e["field_name"] == "motion_plan_ref"]
    assert hit and "未知运动方案" in hit[0]["message"]


# ---------------------------------------------------------------- 自定义操作 custom.call
def test_custom_call_selects_method_and_devices():
    """自定义操作：选一个方法库里的方法 + 勾选要操作的设备（可多台）。"""
    ed = _editor()
    _ready_recipe(ed)
    sid = ed.add_step(NodeType.CUSTOM_CALL)
    assert ed.model.sequence[0].method_ref == "probe_devices"   # 目录里第一个方法
    ed.set_step_devices(sid, ["eco65_01", "ds86_01"])
    assert ed.model.sequence[0].device_refs == ("eco65_01", "ds86_01")
    assert ed.validate().ok
    ed.set_step_method(sid, "并不存在的办法")
    report = ed.validate()
    assert not report.ok
    hit = [e for e in report.entries if e["step_id"] == sid
           and e["field_name"] == "method_ref"]
    assert hit and "未知自定义方法" in hit[0]["message"]


def test_custom_call_device_count_must_fit_method_boundary():
    """方法的设备条数边界参与**校验**：many 也要至少 1 台，选 0 台发布前即被拒。"""
    ed = _editor()
    _ready_recipe(ed)
    sid = ed.add_step(NodeType.CUSTOM_CALL)
    ed.set_step_method(sid, "set_output")        # devices=many（至少 1 台）
    ed.set_step_devices(sid, [])
    report = ed.validate()
    assert not report.ok
    assert any(e["field_name"] == "device_refs" and e["step_id"] == sid
               for e in report.entries)
    ed.set_step_devices(sid, ["eco65_01"])
    assert ed.validate().ok


def test_custom_call_unregistered_device_is_located():
    ed = _editor()
    _ready_recipe(ed)
    sid = ed.add_step(NodeType.CUSTOM_CALL)
    ed.set_step_devices(sid, ["没有这台设备"])
    report = ed.validate()
    assert not report.ok
    assert any(e["field_name"] == "device_refs" and e["step_id"] == sid
               for e in report.entries)


# ---------------------------------------------------------------- 视觉检测来源
def test_vision_source_is_either_or_and_pipeline_wins_only_when_alone():
    ed = _editor()
    _ready_recipe(ed)
    ed.add_step(NodeType.CAMERA_CAPTURE)
    sid = ed.add_step(NodeType.VISION_RUN)
    assert ed.model.sequence[1].pipeline_ref == PIPE
    ed.set_detection(sid, DET)
    step = ed.model.sequence[1]
    assert step.detection_ref == DET
    assert step.pipeline_ref is None              # 二选一：给检测就清流水线
    assert ed.validate().ok
    ed.set_pipeline(sid, PIPE)
    step = ed.model.sequence[1]
    assert step.pipeline_ref == PIPE
    assert step.detection_ref is None             # 反过来也一样
    assert ed.validate().ok


def test_unknown_detection_is_located_and_names_the_step():
    ed = _editor()
    _ready_recipe(ed)
    ed.add_step(NodeType.CAMERA_CAPTURE)
    sid = ed.add_step(NodeType.VISION_RUN)
    ed.set_detection(sid, "没有这条检测")
    report = ed.validate()
    assert not report.ok
    hit = [e for e in report.entries if e["step_id"] == sid
           and e["field_name"] == "detection_ref"]
    assert hit and "未知视觉检测" in hit[0]["message"]


def test_detection_pointing_at_unregistered_pipeline_is_reported():
    """检测存在，但它引用的流水线不在本栈注册 —— 也要报出来，别到运行期才炸。"""
    ed = _editor()
    ed.catalog.detections = {"检测-B": "some_other_pipeline"}
    _ready_recipe(ed)
    ed.add_step(NodeType.CAMERA_CAPTURE)
    sid = ed.add_step(NodeType.VISION_RUN)
    ed.set_detection(sid, "检测-B")
    report = ed.validate()
    assert not report.ok
    assert any("未注册" in e["message"] and e["step_id"] == sid
               for e in report.entries)


# ---------------------------------------------------------------- 发布闸
def test_can_publish_explains_each_refusal():
    ed = _editor()
    assert ed.can_publish() == (False, "未选择执行方案")
    _ready_recipe(ed, "r1")
    ed.set_offline_draft(True)
    ok, why = ed.can_publish()
    assert not ok and "离线示例" in why
    ed.set_offline_draft(False)
    sid = ed.add_step(NodeType.ROBOT_MOVE_JOINT)
    ed.set_motion_plan(sid, "不存在的方案")
    ok, why = ed.can_publish()
    assert not ok and "校验未通过" in why
    ed.set_motion_plan(sid, PLAN)
    ok, why = ed.can_publish()
    assert ok and "可发布" in why


def test_publish_records_revision_and_offline_is_refused_by_service():
    ed = _editor()
    _ready_recipe(ed, "r1")
    ed.add_step(NodeType.CAMERA_CAPTURE)
    rev = ed.publish()                    # DEC-040: 发布不询问/不记录操作人员
    assert rev == "v1+1"
    assert ed.history() == ["v1+1"]
    ed.publish()
    assert ed.history() == ["v1+1", "v1+2"]
    diff = ed.compare("v1+1", "v1+2")
    assert diff["changed"] == {}          # 内容没变，只是再发了一次
    assert ed.model.state == "published"  # DEC-040: 草稿同步标为已发布

    off = _editor()
    off.new_recipe("r2")
    off.set_offline_draft(True)
    with pytest.raises(PublishError, match="离线示例"):
        off.publish()


def test_validation_is_five_layer_and_still_says_why_when_recipe_is_empty():
    ed = _editor()
    ed.new_recipe("r1")
    report = ed.validate()
    assert not report.ok
    messages = " ".join(report.errors)
    assert "sequence 为空" in messages


# ---------------------------------------------------------------- 落盘
def test_save_writes_yaml_with_offline_note(tmp_path):
    ed = _editor()
    _ready_recipe(ed, "r1")
    ed.set_offline_draft(True)
    ed.add_step(NodeType.CAMERA_CAPTURE)
    out = tmp_path / "r1.yaml"
    ed.save(str(out))
    text = out.read_text(encoding="utf-8")
    assert "离线示例" in text
    assert "recipe_id: r1" in text
    assert "draft" in text


# ---------------------------------------------------------------- DEC-035 落盘来源
def test_save_records_source_so_save_button_can_overwrite(tmp_path):
    ed = _editor()
    _ready_recipe(ed, "r1")
    assert ed.source_path() is None                      # 新建草稿：还没落过盘
    target = tmp_path / "out" / "r1.yaml"
    ed.save(str(target))
    assert ed.source_path() == str(target)
    ed.save(str(target))                                 # 第二次保存覆盖同一文件
    assert (tmp_path / "out" / "r1.yaml.bak").exists()   # 原子写保留上一版


# ---------------------------------------------------------------- 步骤显示名 (DEC-037)
def test_step_title_is_optional_display_only():
    """步骤可设中文名；留空/全空白 = 未设；名称不参与引用，步骤 ID 仍是唯一引用键。"""
    ed = _editor()
    _ready_recipe(ed, "r1")
    sid = ed.add_step(NodeType.CAMERA_CAPTURE)
    assert ed.model.sequence[0].title is None            # 默认不设
    ed.set_step_title(sid, "  拍照位 A  ")               # 首尾空白去掉
    assert ed.model.sequence[0].title == "拍照位 A"
    assert ed.model.sequence[0].id == sid                # ID 不受影响
    ed.set_step_title(sid, "   ")                        # 全空白 -> 回到未设
    assert ed.model.sequence[0].title is None
    ed.set_step_title(sid, "拍照位 A")
    assert step_title(ed.model.sequence[0]) == "拍照位 A"


def test_step_title_falls_back_to_chinese_type_name():
    """未设名称时界面显示中文类型名（DEC-037），不再显示英文 id。"""
    ed = _editor()
    _ready_recipe(ed, "r1")
    ed.add_step(NodeType.ROBOT_MOVE_JOINT)
    ed.add_step(NodeType.VISION_RUN)
    assert [step_title(s) for s in ed.model.sequence] == ["机器人运动", "视觉判定"]


def test_step_title_roundtrips_through_yaml(tmp_path):
    """中文名随草稿落盘、再载入还在（只做展示，不影响 schema 校验）。"""
    ed = _editor()
    _ready_recipe(ed, "r1")
    sid = ed.add_step(NodeType.CAMERA_CAPTURE)
    ed.set_step_title(sid, "拍照")
    out = tmp_path / "r1.yaml"
    ed.save(str(out))
    assert "title: 拍照" in out.read_text(encoding="utf-8")

    from application.services.recipes.models import RecipeModel
    from ruamel.yaml import YAML
    data = YAML(typ="safe").load(out.read_text(encoding="utf-8"))
    model = RecipeModel.model_validate(data)
    assert step_title(model.sequence[0]) == "拍照"


# ---------------------------------------------------------------- 删除执行方案 (DEC-108)
def test_delete_recipe_removes_draft_and_backs_up_source_file(tmp_path):
    """删除 = 草稿移除 + 来源文件改名 .deleted-<时间>.bak（目录扫描跳过 .bak）。"""
    ed = _editor()
    _ready_recipe(ed, "r1")
    _ready_recipe(ed, "r2")
    out = tmp_path / "r1.yaml"
    ed.select("r1")
    ed.save(str(out))
    assert out.exists()

    info = ed.delete_recipe("r1")
    assert ed.service.drafts() == ["r2"]
    assert not out.exists()
    assert info["backup"] and info["backup"].endswith(".bak")
    assert os.path.exists(info["backup"])
    assert os.path.dirname(info["backup"]) == str(tmp_path)
    assert ed.recipe_id is None          # 删的是当前选中 -> 选择清空


def test_delete_recipe_unsaved_draft_only_leaves_memory(tmp_path):
    """新建没落过盘的草稿：删除只从内存移除，不动磁盘。"""
    ed = _editor()
    _ready_recipe(ed, "r1")
    info = ed.delete_recipe("r1")
    assert ed.service.drafts() == []
    assert info["source"] is None and info["backup"] is None


def test_delete_recipe_refuses_when_referenced_by_other_subflow():
    """被其他执行方案的子流程步骤引用时拒绝删除，并指出引用者。"""
    ed = _editor()
    _ready_recipe(ed, "r1")
    _ready_recipe(ed, "r2")
    ed.select("r2")
    sid = ed.add_step(NodeType.SUBFLOW)
    ed.set_step_field(sid, "subflow_ref", "r1")

    with pytest.raises(PublishError, match="r2"):
        ed.delete_recipe("r1")
    assert "r1" in ed.service.drafts()   # 引用未解除 -> 原样保留


def test_delete_recipe_unknown_id_raises():
    ed = _editor()
    with pytest.raises(PublishError, match="无草稿"):
        ed.delete_recipe("nope")
