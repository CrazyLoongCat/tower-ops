"""运动方案：配置模型、存储、校验与旧工程导入（离线）。

覆盖：路点规范化（拒绝补零）/ 方案 CRUD / 路点增删改移 / 校验；
以及**仓库里那份从旧工程实测导入的 motion.yaml** 必须能加载、无校验问题、带来源标注。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from application.services.motion_config import (
    LEGACY_SOURCE,
    POINT_MODES,
    SOCKET_MODES,
    TOOL_OFFSET_MODES,
    MotionConfig,
    MotionConfigStore,
    MotionPlan,
    NamedPath,
    RobotMotionProfile,
    SocketSpec,
    VALID_MODES,
    Waypoint,
    is_socket_mode,
    is_tool_offset_mode,
    socket_signed_mm,
    tool_offset_mm,
    validate,
)
from application.services.motion_import import import_legacy_motion

ROOT = Path(__file__).resolve().parents[3]


def _store(tmp_path, *, seed=True) -> MotionConfigStore:
    return MotionConfigStore(str(tmp_path / "motion.yaml"), seed_if_missing=seed)


# ---------------------------------------------------------------- 路点
def test_waypoint_requires_exactly_six():
    assert Waypoint("movel", (1, 2, 3, 4, 5, 6)).pos == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    for bad in ((1, 2, 3), (1, 2, 3, 4, 5, 6, 7)):
        with pytest.raises(ValueError) as ei:
            Waypoint("movel", bad)
        assert "恰好 6 项" in str(ei.value)      # 拒绝补 0 / 缺项


def test_waypoint_rejects_unknown_mode_and_non_finite():
    with pytest.raises(ValueError):
        Waypoint("movel_x", (1, 2, 3, 4, 5, 6))
    with pytest.raises(ValueError):
        Waypoint("movel", (1, 2, 3, 4, 5, float("nan")))
    # 合法方式 = 点位方式(movel/movej) + 套筒偏移 + 工具系相对偏移，可混排在同一方案里。
    assert POINT_MODES == ("movel", "movej")
    assert SOCKET_MODES == ("socket_offset",)
    assert TOOL_OFFSET_MODES == ("tool_offset",)
    assert VALID_MODES == (*POINT_MODES, *SOCKET_MODES, *TOOL_OFFSET_MODES)


# ---------------------------------------------------------------- 套筒偏移路点
def test_socket_waypoint_mode_and_signed_mm():
    for mode in SOCKET_MODES:
        wp = Waypoint(mode, (0, 12, 0, 0, 0, 0))       # 偏移(mm) 记在 Y 分量
        assert is_socket_mode(wp.mode) is True
    # 方向 = Y 分量的正负号：+ = 进入/前进(Y+)，− = 脱离/后退(Y−)。
    assert socket_signed_mm(Waypoint("socket_offset", (0, 12, 0, 0, 0, 0))) == 12.0
    assert socket_signed_mm(Waypoint("socket_offset", (0, -12, 0, 0, 0, 0))) == -12.0
    assert is_socket_mode("movel") is False
    # 旧的四方式名已并入单一方式：不再是合法路点方式。
    for legacy in ("socket_fwd", "socket_back", "socket_enter", "socket_exit"):
        with pytest.raises(ValueError):
            Waypoint(legacy, (0, 12, 0, 0, 0, 0))


def test_validate_socket_waypoint_rules():
    def _issues(mode, pos):
        plan = MotionPlan("p", "d", "movel", 20, 10, 30, [Waypoint(mode, pos)])
        return validate(MotionConfig(plans=[plan]))

    assert _issues("socket_offset", (0, 10, 0, 0, 0, 0)) == []
    assert _issues("socket_offset", (0, -10, 0, 0, 0, 0)) == []       # 负向合法
    assert any("只应填 Y 分量" in s for s in _issues("socket_offset", (5, 10, 0, 0, 0, 0)))
    assert any("偏移为 0" in s for s in _issues("socket_offset", (0, 0, 0, 0, 0, 0)))
    # 点位行(movel)不受套筒规则约束。
    assert _issues("movel", (1, 2, 3, 0, 0, 0)) == []


def test_tool_offset_waypoint_mode_and_mm():
    """工具系偏移：前 3 项 = Bolt 工具系 dx/dy/dz(mm)，后 3 项应为 0。"""
    wp = Waypoint("tool_offset", (0, 0, 5, 0, 0, 0))
    assert is_tool_offset_mode(wp.mode) is True
    assert tool_offset_mm(wp) == (0.0, 0.0, 5.0)
    assert is_tool_offset_mode("movel") is False
    assert is_tool_offset_mode("socket_offset") is False


def test_validate_tool_offset_waypoint_rules():
    def _issues(pos, mode="tool_offset"):
        plan = MotionPlan("p", "d", "movel", 20, 10, 30, [Waypoint(mode, pos)])
        return validate(MotionConfig(plans=[plan]))

    assert _issues((0, 0, 5, 0, 0, 0)) == []           # 上升 5mm 合法
    assert _issues((0, 100, -5, 0, 0, 0)) == []        # 斜向回目标合法（多轴、可负）
    assert any("后 3 项须为 0" in s for s in _issues((0, 0, 5, 0, 0, 0.1)))
    assert any("偏移为 0" in s for s in _issues((0, 0, 0, 0, 0, 0)))


def test_tool_offset_waypoint_yaml_round_trip(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.add_plan(MotionPlan("lift5", "抬升5再斜回", "movel", 10, 0, 100,
                           [Waypoint("tool_offset", (0, 0, 5, 0, 0, 0)),
                            Waypoint("tool_offset", (0, 100, -5, 0, 0, 0))],
                           source="本工程新建"))
    st.save()
    again = MotionConfigStore(str(tmp_path / "motion.yaml"), seed_if_missing=False)
    again.load()
    wps = again.config.find_plan("lift5").waypoints
    assert [w.mode for w in wps] == ["tool_offset", "tool_offset"]
    assert [w.pos[:3] for w in wps] == [(0.0, 0.0, 5.0), (0.0, 100.0, -5.0)]


def test_socket_waypoint_yaml_round_trip(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.add_plan(MotionPlan("sock", "取套筒", "movel", 20, 10, 30,
                           [Waypoint("socket_offset", (0, 15, 0, 0, 0, 0)),
                            Waypoint("socket_offset", (0, -15, 0, 0, 0, 0))],
                           source="本工程新建"))
    st.save()
    again = MotionConfigStore(str(tmp_path / "motion.yaml"), seed_if_missing=False)
    again.load()
    wps = again.config.find_plan("sock").waypoints
    assert [w.mode for w in wps] == ["socket_offset", "socket_offset"]
    assert [w.pos[1] for w in wps] == [15.0, -15.0]


# ---------------------------------------------------------------- CRUD
def test_plan_crud_and_waypoint_ops(tmp_path):
    st = _store(tmp_path)
    st.load()
    plan = MotionPlan("p1", "demo", "movel", 30, 10, 30,
                      [Waypoint("movel", (1, 2, 3, 0, 0, 0))], source="本工程新建")
    st.add_plan(plan)
    with pytest.raises(ValueError):
        st.add_plan(MotionPlan("p1"))                     # ID 重复

    st.add_waypoint("p1", Waypoint("movej", (1, 2, 3, 4, 5, 6)))
    assert len(st.config.find_plan("p1").waypoints) == 2
    st.update_waypoint("p1", 0, Waypoint("movel", (9, 9, 9, 0, 0, 0)))
    assert st.config.find_plan("p1").waypoints[0].pos[0] == 9.0
    st.move_waypoint("p1", 0, 1)
    assert st.config.find_plan("p1").waypoints[0].mode == "movej"
    st.remove_waypoint("p1", 0)
    assert len(st.config.find_plan("p1").waypoints) == 1

    st.rename_plan("p1", "p2")
    assert st.config.find_plan("p2") is not None and st.config.find_plan("p1") is None
    st.remove_plan("p2")
    assert st.config.plans == []


def test_rename_to_existing_rejected(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.add_plan(MotionPlan("a"))
    st.add_plan(MotionPlan("b"))
    with pytest.raises(ValueError):
        st.rename_plan("a", "b")


def test_save_load_roundtrip(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.add_plan(MotionPlan("plan_x", "d", "movej", 40, 8, 20,
                           [Waypoint("movej", (1, 2, 3, 4, 5, 6))], source="本工程新建"))
    st.save()
    again = MotionConfigStore(st.path).load()
    plan = again.find_plan("plan_x")
    assert plan is not None
    assert plan.speed == 40 and plan.blend == 8 and plan.timeout == 20
    assert plan.waypoints[0].mode == "movej"
    assert "plan_x" in open(st.path, encoding="utf-8").read()


# ---------------------------------------------------------------- 校验
def test_validate_reports_range_and_duplicate():
    cfg = MotionConfig(plans=[
        MotionPlan("a", speed=200, blend=-1, timeout=0),
        MotionPlan("a"),
    ])
    issues = validate(cfg)
    joined = " | ".join(issues)
    assert "重复" in joined and "速度" in joined and "交融半径" in joined and "超时" in joined


def test_validate_clean_config_has_no_issues():
    assert validate(MotionConfig(plans=[MotionPlan("ok")])) == []


def test_socket_spec_and_path_normalize():
    s = SocketSpec("10", "M10", 0, (1, 2, 3, 0, 0, 0), (4, 5, 6, 0, 0, 0))
    assert s.size == "M10" and s.grip_pose[0] == 1.0
    p = NamedPath("path_to_view", [(1, 2, 3, 0, 0, 0)])
    assert len(p.poses) == 1 and len(p.poses[0]) == 6
    with pytest.raises(ValueError):
        NamedPath("bad", [(1, 2, 3)])              # 同样拒绝补零


# ---------------------------------------------------------------- 旧工程导入
def test_import_legacy_motion_shapes():
    legacy = {
        "motion_plans": {
            "demo": {"desc": "d", "mode": "movel", "speed": 30, "blend": 10,
                     "waypoints": [{"mode": "movel", "pos": [1, 2, 3, 0, 0, 0]},
                                   {"mode": "movej", "pos": [1, 2, 3, 4, 5, 6]}]},
            "legacy_list": {"mode": "movej",
                            "waypoints": [[1, 2, 3, 4, 5, 6]]},   # 旧版纯列表
        },
        "paths": {"p1": [[1, 2, 3, 0, 0, 0]]},
        "socket_map": {"10": {"size": "M10", "rack_station": 0,
                              "grip_pose": [1, 2, 3, 0, 0, 0],
                              "release_pose": [4, 5, 6, 0, 0, 0]}},
        "frames": {"active_tool": "Bolt", "active_work": "Ganta"},
        "guide": {"recv_timeout_s": 600, "move_speed": 10},
        "motion": {"speed_cap": 50, "point_timeout_s": 30},
        "global_speed": {"value": 35},
        "safety": {"collision": True},
    }
    cfg = import_legacy_motion(legacy)
    assert [p.plan_id for p in cfg.plans] == ["demo", "legacy_list"]
    assert cfg.plans[0].waypoints[1].mode == "movej"
    assert cfg.plans[1].waypoints[0].mode == "movej"     # 纯列表按方案默认方式包装
    assert cfg.profile.active_tool == "Bolt" and cfg.profile.speed_cap == 50
    assert cfg.profile.global_speed == 35
    assert cfg.sockets[0].size == "M10"
    assert all(p.source == LEGACY_SOURCE for p in cfg.plans)
    assert validate(cfg) == []


# ---------------------------------------------------------------- 仓库那份
def test_shipped_motion_config_loads_and_validates():
    """仓库 configs/motion/motion.yaml 必须能加载、无校验问题、带来源标注。"""
    path = ROOT / "configs" / "motion" / "motion.yaml"
    cfg = MotionConfigStore(str(path)).load()
    assert cfg.plans and cfg.paths and cfg.sockets
    assert validate(cfg) == []
    assert all(p.source for p in cfg.plans)          # 来源标注必须齐
    assert cfg.profile.active_tool == "Bolt" and cfg.profile.active_work == "Ganta"


def test_shipped_motion_config_matches_legacy_source():
    """仓库那份里**导入自旧工程**的方案应与旧工程 config.default.yaml 一致（数值逐点核对）。

    `configs/motion/motion.yaml` 是**用户的活配置**——用户会在界面里新建方案
    （``source: 本工程新建``），这里只核对导入项，不要求文件里没有用户自建方案。
    """
    from ruamel.yaml import YAML
    legacy_path = (ROOT.parent / "RealMan_Robot" / "ECO65_NutTighten" / "configs"
                   / "config.default.yaml")
    if not legacy_path.exists():                     # 旧工程被移走则跳过
        pytest.skip("旧工程配置不在")
    legacy = YAML(typ="safe").load(open(legacy_path, encoding="utf-8"))
    shipped = MotionConfigStore(str(ROOT / "configs" / "motion" / "motion.yaml")).load()
    legacy_plans = legacy.get("motion_plans") or {}
    imported = [p for p in shipped.plans if p.source == LEGACY_SOURCE]
    assert {p.plan_id for p in imported} == set(legacy_plans)
    for plan in imported:
        src = legacy_plans[plan.plan_id]
        assert plan.speed == int(src.get("speed", 20))
        assert plan.blend == int(src.get("blend", 12))
        legacy_wps = src.get("waypoints") or []
        assert len(plan.waypoints) == len(legacy_wps)
        for got, raw in zip(plan.waypoints, legacy_wps):
            pos = raw["pos"] if isinstance(raw, dict) else raw
            assert got.pos == tuple(float(x) for x in pos)
            if isinstance(raw, dict):
                assert got.mode == raw.get("mode", src.get("mode", "movel"))
