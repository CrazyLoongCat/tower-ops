"""A12 配置/配方校验/发布测试 (T20/T34/T36 基础)."""
from __future__ import annotations

import os

import pytest
from ruamel.yaml import YAML

from application.contracts.targets import JointTarget
from application.domain.actions import ActionCatalog, ActionDefinition
from application.services.recipes.service import (
    PublishError,
    RecipeService,
    ReferenceCatalog,
    atomic_write,
    load_recipes_dir,
    preview_import,
    rollback,
    validate_recipe,
)
from application.services.methods import default_methods

_HERE = os.path.dirname(os.path.abspath(__file__))
_YAML = os.path.abspath(os.path.join(
    _HERE, "..", "..", "..", "configs", "recipes", "ds86_two_station_example.yaml"))


def _catalog():
    return ReferenceCatalog(
        joint_targets={
            "teach_01": JointTarget(joint_deg=[10, 20, 30, 0, 90, 0]),
            "teach_02": JointTarget(joint_deg=[-10, 20, 30, 0, 90, 0]),
        },
        pipelines={"approved_ds86_circle_pipeline"},
        actions=ActionCatalog([
            ActionDefinition(action_id="approved_single_board_action",
                             resource_ids=("motion_global",)),
        ]),
        cameras={"ds86_01"},
        devices={"stm32_01", "eco65_01", "ds86_01"},
        methods=default_methods(),
    )


def _recipe_dict():
    with open(_YAML, encoding="utf-8") as fh:
        return YAML(typ="safe").load(fh)


def test_sample_recipe_validates():
    svc = RecipeService(_catalog())
    rid = svc.draft(_recipe_dict())
    report = svc.validate(rid)
    assert report.ok, report.errors


def test_offline_draft_not_publishable_without_flag():
    svc = RecipeService(_catalog())
    rid = svc.draft(_recipe_dict())
    with pytest.raises(PublishError):
        svc.publish(rid)


def test_publish_and_frozen_snapshot():
    svc = RecipeService(_catalog())
    rid = svc.draft(_recipe_dict())
    rev = svc.publish(rid, allow_offline_draft=True)
    h1 = svc.running_hash(rid, rev)
    # 改草稿不影响已发布运行 hash
    d = _recipe_dict()
    d["sequence"][0]["id"] = "move2"
    svc.draft(d)
    assert svc.running_hash(rid, rev) == h1
    snap = svc.activate(rid, rev)
    assert snap.recipe_version == rev


def test_publish_marks_draft_published_so_start_gate_passes():
    """DEC-040: 发布即生效——草稿本身也标为 published，作业台不再报「未发布」。"""
    svc = RecipeService(_catalog())
    rid = svc.draft(_recipe_dict())
    assert svc.get_draft(rid).state == "draft"
    svc.publish(rid, allow_offline_draft=True)
    assert svc.get_draft(rid).state == "published"


def test_unknown_pipeline_rejected():
    svc = RecipeService(_catalog())
    d = _recipe_dict()
    d["sequence"][2]["pipeline_ref"] = "nope"
    rid = svc.draft(d)
    report = svc.validate(rid)
    assert not report.ok
    assert any("未知流水线" in e for e in report.errors)


def test_unknown_action_rejected():
    svc = RecipeService(_catalog())
    d = _recipe_dict()
    d["sequence"][4]["action_ref"] = "nope"
    rid = svc.draft(d)
    assert not svc.validate(rid).ok


def test_unknown_joint_target_rejected():
    svc = RecipeService(_catalog())
    d = _recipe_dict()
    d["sequence"][0]["joint_target_ref"] = "nope"
    rid = svc.draft(d)
    assert not svc.validate(rid).ok


def test_protected_override_rejected():
    svc = RecipeService(_catalog())
    d = _recipe_dict()
    d["overrides"] = {"interlocks.motion_global": "off"}
    rid = svc.draft(d)
    report = svc.validate(rid)
    assert not report.ok
    assert any("受保护" in e for e in report.errors)


def test_allowed_override_ok():
    svc = RecipeService(_catalog())
    d = _recipe_dict()
    d["overrides"] = {"vision.min_radius": 20}
    rid = svc.draft(d)
    assert svc.validate(rid).ok


def test_repeat_must_be_bounded():
    svc = RecipeService(_catalog())
    d = _recipe_dict()
    d["sequence"].append({"id": "loop", "type": "repeat", "max_iterations": 0})
    rid = svc.draft(d)
    assert not svc.validate(rid).ok


def test_unknown_node_type_rejected():
    d = _recipe_dict()
    d["sequence"].append({"id": "weird", "type": "python.eval"})
    with pytest.raises(Exception):
        RecipeService(_catalog()).draft(d)


def test_condition_depth_bounded():
    from application.services.recipes.models import ConditionModel, RecipeModel
    from application.services.recipes.service import validate_recipe
    # 深层嵌套 all (超过 max_depth=8)
    node = ConditionModel(source={"step": "move", "field": "f"}, op="eq", value=1)
    for _ in range(9):
        node = ConditionModel(all=[node])
    d = _recipe_dict()
    d["sequence"][3]["predicate"] = node.model_dump(mode="json")
    rep = validate_recipe(RecipeModel.model_validate(d), _catalog())
    assert any("嵌套" in e or "深度" in e for e in rep.errors)


def test_compare_reports_changes():
    svc = RecipeService(_catalog())
    rid = svc.draft(_recipe_dict())
    rev1 = svc.publish(rid, allow_offline_draft=True)
    d = _recipe_dict()
    d["version"] = "0.3-draft"
    svc.draft(d)
    rev2 = svc.publish(rid, allow_offline_draft=True)
    diff = svc.compare(rid, rev1, rev2)
    assert "version" in diff["changed"]


def test_audit_recorded():
    svc = RecipeService(_catalog())
    rid = svc.draft(_recipe_dict())
    svc.publish(rid, allow_offline_draft=True)
    assert svc.audit[-1]["action"] == "publish"
    # DEC-040: 发布不再记录操作人员
    assert "operator" not in svc.audit[-1]


def test_atomic_write_and_rollback(tmp_path):
    p = tmp_path / "cfg.yaml"
    atomic_write(str(p), "v1")
    assert p.read_text() == "v1"
    atomic_write(str(p), "v2")   # 生成 .bak=v1
    assert p.read_text() == "v2"
    rollback(str(p))
    assert p.read_text() == "v1"


def test_preview_import_no_script_execution(tmp_path):
    (tmp_path / "a.yaml").write_text("x: 1")
    manifest = {"schema_version": 1,
                "files": [{"path": "a.yaml"}, {"path": "../evil.yaml"}]}
    result = preview_import(manifest, str(tmp_path))
    assert not result["ok"]
    assert any("非法路径" in i for i in result["issues"])
    assert result["executed_scripts"] == []


def test_import_missing_file(tmp_path):
    manifest = {"schema_version": 1, "files": [{"path": "missing.yaml"}]}
    result = preview_import(manifest, str(tmp_path))
    assert not result["ok"]


# ---------------------------------------------------------------- DEC-035 保存/目录发现
def _mini_recipe(rid: str, **over):
    data = _recipe_dict()
    data["recipe_id"] = rid
    data.update(over)
    return data


def _write_yaml(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        YAML(typ="safe").dump(data, fh)


def test_draft_records_source_file_and_edit_keeps_it(tmp_path):
    svc = RecipeService(_catalog())
    target = str(tmp_path / "r1.yaml")
    rid = svc.draft(_mini_recipe("r1"), source=target)
    assert svc.source_of(rid) == os.path.abspath(target)
    # 再次 draft（编辑/重新校验）默认**不抹掉**来源 —— 「保存」仍覆盖原文件
    svc.draft(_mini_recipe("r1"))
    assert svc.source_of(rid) == os.path.abspath(target)
    # 新建的草稿没有来源
    assert svc.source_of(svc.draft(_mini_recipe("r2"))) is None


def test_load_recipes_dir_scans_sorted_and_skips_bad_files(tmp_path):
    _write_yaml(tmp_path / "b_recipe.yaml", _mini_recipe("b_rec"))
    _write_yaml(tmp_path / "a_recipe.yaml", _mini_recipe("a_rec"))
    (tmp_path / "broken.yaml").write_text("schema_version: 1\nrecipe_id: old\n",
                                          encoding="utf-8")   # 旧 schema：跳过
    (tmp_path / "notes.txt").write_text("不是配方", encoding="utf-8")
    (tmp_path / "a_recipe.yaml.bak").write_text("备份，不该扫", encoding="utf-8")

    svc = RecipeService(_catalog())
    skipped: list[str] = []
    loaded = load_recipes_dir(svc, str(tmp_path), log=skipped.append)

    assert loaded == ["a_rec", "b_rec"]                    # 按文件名序
    assert svc.drafts() == ["a_rec", "b_rec"]
    assert svc.source_of("a_rec") == os.path.abspath(str(tmp_path / "a_recipe.yaml"))
    assert len(skipped) == 1 and "broken.yaml" in skipped[0]   # 坏文件只跳过并记原因


def test_load_recipes_dir_does_not_override_already_drafted_id(tmp_path):
    svc = RecipeService(_catalog())
    svc.draft(_mini_recipe("keep", version="memory-v1"))
    _write_yaml(tmp_path / "keep.yaml", _mini_recipe("keep", version="file-v9"))
    load_recipes_dir(svc, str(tmp_path))
    assert svc.get_draft("keep").version == "memory-v1"    # 先到先得，--recipe 随后最终生效


def test_load_recipes_dir_finds_shipped_examples():
    svc = RecipeService(_catalog())
    loaded = load_recipes_dir(svc, os.path.dirname(_YAML))
    assert "ds86_two_station_example" in loaded
    assert svc.source_of("ds86_two_station_example") == _YAML
    # 目录里的其它配方（含旧版/现场文件）坏或旧只跳过，不让断言绑死用户配置
