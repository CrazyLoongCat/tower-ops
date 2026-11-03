"""从旧工程配置导入运动数据 (「运动方案」数据迁移)。

来源：`RealMan_Robot/ECO65_NutTighten/configs/config.*.yaml`（**真机实测**）。
导入只做**结构与范围规范化**，不改数值 —— 数值是现场验证过的，动它等于毁掉可信度。

每条记录写入 `source` 标注，便于日后区分"旧工程实测导入"与本工程新建。
"""
from __future__ import annotations

from typing import Any

from application.services.motion_config import (
    LEGACY_SOURCE,
    MotionConfig,
    MotionPlan,
    NamedPath,
    RobotMotionProfile,
    SocketSpec,
    Waypoint,
    _six,       # 与存储同一套规范化（拒绝补零）
)


def _plan_from_legacy(name: str, raw: dict) -> MotionPlan:
    default_mode = str(raw.get("mode", "movel"))
    if default_mode not in ("movel", "movej"):
        default_mode = "movel"
    waypoints: list[Waypoint] = []
    for item in (raw.get("waypoints") or []):
        if isinstance(item, dict) and "pos" in item:
            mode = str(item.get("mode", default_mode))
            pos = item.get("pos") or []
        else:                                  # 旧版纯列表路点
            mode = default_mode
            pos = item
        if mode not in ("movel", "movej"):
            mode = default_mode
        waypoints.append(Waypoint(mode, _six(pos)))
    return MotionPlan(
        plan_id=str(name), desc=str(raw.get("desc", "")),
        default_mode=default_mode, speed=int(raw.get("speed", 20)),
        blend=int(raw.get("blend", 12)), timeout=int(raw.get("timeout", 30)),
        waypoints=waypoints, source=LEGACY_SOURCE)


def import_legacy_motion(data: dict[str, Any]) -> MotionConfig:
    """把旧工程配置 dict 转成 :class:`MotionConfig`。"""
    if not isinstance(data, dict):
        raise ValueError("旧配置必须是映射")

    plans = [_plan_from_legacy(name, raw or {})
             for name, raw in (data.get("motion_plans") or {}).items()
             if isinstance(raw, dict) or raw is None]

    paths: list[NamedPath] = []
    for name, poses in (data.get("paths") or {}).items():
        paths.append(NamedPath(str(name), [_six(p) for p in (poses or [])]))

    sockets: list[SocketSpec] = []
    for code, spec in (data.get("socket_map") or {}).items():
        spec = spec or {}
        sockets.append(SocketSpec(
            code=str(code), size=str(spec.get("size", "")),
            rack_station=int(spec.get("rack_station", 0)),
            grip_pose=_six(spec.get("grip_pose")),
            release_pose=_six(spec.get("release_pose"))))

    frames = data.get("frames") or {}
    guide = data.get("guide") or {}
    motion = data.get("motion") or {}
    globals_ = data.get("global_speed") or {}
    profile = RobotMotionProfile(
        active_tool=str(frames.get("active_tool", "Bolt") or "Bolt"),
        active_work=str(frames.get("active_work", "Ganta") or "Ganta"),
        speed_cap=int(motion.get("speed_cap", 50)),
        point_timeout_s=int(motion.get("point_timeout_s", 30)),
        guide_recv_timeout_s=int(guide.get("recv_timeout_s", 600)),
        guide_move_speed=int(guide.get("move_speed", 10)),
        guide_move_blend=int(guide.get("move_blend", 0)),
        global_speed=int(globals_.get("value", 35)),
        safety=dict(data.get("safety") or {}),
        insert=dict(data.get("insert") or {}),
        tighten=dict(data.get("tighten") or {}))

    return MotionConfig(plans=plans, paths=paths, sockets=sockets, profile=profile)


__all__ = ["import_legacy_motion"]
