"""E16 配置合成/发布/版本迁移/配置包 测试 (T20/T34 + E16-附加 离线)。"""
from __future__ import annotations

import copy

import pytest

from application.infrastructure.config import (
    CONFIG_SCHEMA_VERSION,
    ComposeInputs,
    ConfigCatalog,
    ConfigRoot,
    ConfigVersionError,
    ProtectedPathError,
    compose_config,
    diff_config,
    freeze_config,
    is_protected,
    load_package,
    migrate_config,
    require_schema_version,
    validate_composed,
)
from application.services.recipes.service import PROTECTED_PATH_PREFIXES


# ---------------- 合成顺序与受保护路径 ----------------
def test_compose_order_later_layer_wins():
    merged = compose_config(ComposeInputs(
        site_defaults={"site": {"mode": "offline", "speed": 10}},
        published={"site": {"speed": 20}},
        recipe_allowed={"site": {"speed": 30}},
        station_overrides={"site": {"speed": 40}},
    ))
    assert merged["site"]["speed"] == 40
    assert merged["site"]["mode"] == "offline"      # 未被覆盖的键保留
    assert merged["schema_version"] == CONFIG_SCHEMA_VERSION


def test_published_layer_may_carry_device_baseline():
    """已发布设备/动作/标定属发布门控层, 不受 override 白名单限制。"""
    merged = compose_config(ComposeInputs(
        published={"devices": {"stm32_01": {"address": 1}}}))
    assert merged["devices"]["stm32_01"]["address"] == 1


@pytest.mark.parametrize("layer,key", [
    ("recipe_allowed", "devices.stm32_01.address"),
    ("recipe_allowed", "interlocks.motion_global"),
    ("station_overrides", "stop.limit_ms"),
    ("station_overrides", "hard_limits.max_speed"),
])
def test_override_layers_reject_protected_paths(layer, key):
    inputs = ComposeInputs(**{layer: {"site": {}}})
    setattr(inputs, layer, {"devices": {"stm32_01": {"address": 9}}}
            if key.startswith("devices") else _nested(key))
    with pytest.raises(ProtectedPathError):
        compose_config(inputs)


def _nested(dotted: str) -> dict:
    head, _, tail = dotted.partition(".")
    return {head: {tail: 1}}


def test_normal_override_allowed():
    merged = compose_config(ComposeInputs(
        station_overrides={"recipes": {"p1": {"params": {"radius": 12}}}}))
    assert merged["recipes"]["p1"]["params"]["radius"] == 12


def test_protected_prefixes_single_source():
    assert "devices." in PROTECTED_PATH_PREFIXES
    assert is_protected("devices.x.y") and is_protected("interlocks") \
        and not is_protected("recipes.params")


# ---------------- 冻结快照 ----------------
def test_frozen_snapshot_unaffected_by_later_edit():
    merged = compose_config(ComposeInputs(published={"actions": {"a": {"v": 1}}}))
    frozen = freeze_config(merged)
    before = frozen.hash
    merged["actions"]["a"]["v"] = 999            # 运行中编辑草稿
    assert frozen.data["actions"]["a"]["v"] == 1  # 快照不变
    assert frozen.hash == before


# ---------------- 五层验证 ----------------
def _good_config():
    return {
        "schema_version": 1,
        "site": {"mode": "offline"},
        "devices": {"stm32_01": {"channels": ["clamp", "release"]},
                    "eco65_01": {"channels": []}},
        "actions": {"clamp": {"device_id": "stm32_01", "channel": "clamp",
                              "resource_ids": ["motion_global"],
                              "parameters": [{"name": "force", "min": 0, "max": 100}]}},
        "interlocks": {"stop": {"resource_ids": ["motion_global"]}},
    }


def test_validation_ok():
    assert validate_composed(_good_config()).ok


def test_validation_unknown_section():
    cfg = _good_config(); cfg["weird"] = {}
    assert any("未知配置节" in e for e in validate_composed(cfg).errors)


def test_validation_bad_schema_version():
    cfg = _good_config(); cfg["schema_version"] = 99
    assert any("schema_version" in e for e in validate_composed(cfg).errors)
    with pytest.raises(ConfigVersionError):
        require_schema_version({})


def test_validation_unknown_device_reference():
    cfg = _good_config()
    cfg["actions"]["clamp"]["device_id"] = "nope"
    assert any("未知设备" in e for e in validate_composed(cfg).errors)


def test_validation_param_range_and_missing_resources():
    cfg = _good_config()
    cfg["actions"]["clamp"]["parameters"] = [{"name": "f", "min": 10, "max": 1}]
    del cfg["actions"]["clamp"]["resource_ids"]
    errs = " ".join(validate_composed(cfg).errors)
    assert "min>max" in errs and "resource_ids" in errs


def test_validation_calibration_units_must_be_mm():
    cfg = _good_config(); cfg["calibrations"] = {"c1": {"units": "m"}}
    assert any("非 mm" in e for e in validate_composed(cfg).errors)


def test_validation_capability_channel_binding():
    cfg = _good_config()
    cfg["actions"]["clamp"]["channel"] = "not_a_channel"
    assert any("不在设备" in e for e in validate_composed(cfg).errors)


def test_validation_resource_conflict_without_interlock():
    cfg = _good_config()
    cfg["actions"]["release"] = {"device_id": "stm32_01", "channel": "release",
                                 "resource_ids": ["motion_global"]}
    del cfg["interlocks"]["stop"]              # 去掉互锁声明
    errs = " ".join(validate_composed(cfg).errors)
    assert "共用资源" in errs and "停止规则" in errs


# ---------------- 版本迁移 ----------------
def test_migrate_v0_to_v1_renames_and_versions():
    legacy = {"camera": {"c1": {}}, "action": {"a1": {}}}
    snapshot = copy.deepcopy(legacy)
    out, changes = migrate_config(legacy)
    assert out["schema_version"] == 1
    assert "devices" in out and "camera" not in out
    assert "actions" in out and "action" not in out
    assert legacy == snapshot                  # 入参未被就地修改
    assert any("schema_version=1" in c for c in changes)


def test_migrate_rejects_downgrade_and_missing_step():
    with pytest.raises(ConfigVersionError):
        migrate_config({"schema_version": 5}, target=1)
    with pytest.raises(ConfigVersionError):
        migrate_config({"schema_version": 1}, target=3)   # 无 1->2 步骤
    with pytest.raises(ConfigVersionError):
        migrate_config({"schema_version": "x"})


def test_diff_config():
    a = {"site": {"speed": 1, "mode": "offline"}}
    b = {"site": {"speed": 2, "mode": "offline"}}
    d = diff_config(a, b)
    assert d == {"site.speed": (1, 2)}


# ---------------- 配置根与配置包 ----------------
def test_config_root_resolution_and_escape(tmp_path):
    root = ConfigRoot(str(tmp_path))
    assert root.resolve("devices/boards.yaml").startswith(str(tmp_path))
    with pytest.raises(ProtectedPathError):
        root.resolve("../outside.yaml")
    with pytest.raises(ProtectedPathError):
        root.resolve("/etc/passwd")


def test_load_package_ok(tmp_path):
    (tmp_path / "devices").mkdir()
    (tmp_path / "devices" / "boards.yaml").write_text(
        "stm32_01:\n  device_id: stm32_01\n", encoding="utf-8")
    res = load_package(ConfigRoot(str(tmp_path)),
                       {"schema_version": 1,
                        "files": [{"path": "devices/boards.yaml", "section": "devices"}]},
                       ConfigCatalog(device_ids={"stm32_01"}))
    assert res.ok and res.data["devices"]["stm32_01"]["device_id"] == "stm32_01"
    assert res.executed_scripts == []          # 绝不执行包内脚本


def test_load_package_rejects_scripts_and_bad_paths(tmp_path):
    (tmp_path / "devices").mkdir()
    (tmp_path / "devices" / "evil.py").write_text("print('x')", encoding="utf-8")
    res = load_package(ConfigRoot(str(tmp_path)),
                       {"schema_version": 1,
                        "files": [{"path": "devices/evil.py", "section": "devices"},
                                  {"path": "../x.yaml", "section": "devices"},
                                  {"path": "devices/missing.yaml", "section": "devices"}]})
    assert not res.ok
    errs = " ".join(res.issues)
    assert "脚本文件" in errs and "非法路径" in errs and "缺失" in errs
    assert res.executed_scripts == []


def test_load_package_rejects_unknown_object(tmp_path):
    (tmp_path / "devices").mkdir()
    (tmp_path / "devices" / "b.yaml").write_text("ghost:\n  device_id: ghost\n",
                                                 encoding="utf-8")
    res = load_package(ConfigRoot(str(tmp_path)),
                       {"schema_version": 1,
                        "files": [{"path": "devices/b.yaml", "section": "devices"}]},
                       ConfigCatalog(device_ids={"stm32_01"}))
    assert not res.ok and any("未定义设备" in i for i in res.issues)


def test_load_package_rejects_unknown_section(tmp_path):
    (tmp_path / "weird").mkdir()
    (tmp_path / "weird" / "a.yaml").write_text("x: 1", encoding="utf-8")
    res = load_package(ConfigRoot(str(tmp_path)),
                       {"schema_version": 1,
                        "files": [{"path": "weird/a.yaml", "section": "weird"}]})
    assert not res.ok and any("未知配置节" in i for i in res.issues)
