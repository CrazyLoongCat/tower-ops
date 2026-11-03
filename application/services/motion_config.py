"""运动方案的模型、校验与存储 (E 阶段「运动方案」)。

对应旧工程 `RealMan_Robot/ECO65_NutTighten` 的 `motion_plans` / `paths` / `socket_map` /
`frames` / `guide` 等区块。**移植自真机实测配置**，因此每条记录带 `source` 来源标注：

    旧工程实测导入 —— 值来自已现场验证的工程，不等于本工程现场已复核。

约定（与旧工程一致，勿改）：
- 路点 ``mode`` 可为 ``movel`` / ``movej``（**同一方案内可混用**）、**套筒偏移**
  ``socket_offset``（旧工程「套筒偏移」与「取出套筒」两组按钮，见下），或
  **工具系相对偏移** ``tool_offset``（沿 Bolt 工具系 X/Y/Z 平移 dx/dy/dz mm，
  用一条直线 movel 到目标位姿；用于“先抬升再斜向下”这类多轴相对路径，DEC-060）；
- ``movel`` 的 pos = ``[x,y,z(m), rx,ry,rz(rad)]``；``movej`` 的 pos = ``[j1..j6](°)``；
- **套筒偏移路点**：偏移量(mm) 记在 **Y 分量** (``pos[1]``)，其余分量须为 0；
  **方向由数值正负号决定**——``+`` = Bolt 工具系 Y+（进入/前进），``−`` = Y−（脱离/后退）；
- **工具系偏移路点**：``pos[0..2]`` = Bolt 工具系 dx/dy/dz(mm，可正负)，``pos[3..5]`` 须为 0；
  执行时按当前位姿与工具系轴方向算出目标位姿，**一条 movel 直线**走过去（姿态不变）；
- 坐标系统一取 profile 的 ``active_work``（方案内不存坐标系，避免误导）；
- ``speed`` 是**示教器百分比**(1~100)，不是 mm/s；``blend`` 是交融半径百分比(0~100)。
"""
from __future__ import annotations

import math
import os
from dataclasses import asdict, dataclass, field
from io import StringIO
from typing import Any, Sequence

from ruamel.yaml import YAML

from application.services.recipes.service import atomic_write, rollback

CONFIG_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

# 点位方式（方案的 default_mode 只能在这两个里选）。
POINT_MODES = ("movel", "movej")
# 套筒偏移方式：把旧工程「套筒偏移」(前进/后退) 与「取出套筒」(进入/脱离) 两组按钮
# 整理成**一个方案路点方式**。它们底层是**同一个动作**——Bolt 工具系 Y 轴平移偏移
# (``RealMan_Robot/ECO65_NutTighten`` 的 ``_run_socket_move`` / ``_run_pickup``
#  -> ``_move_axis_offset`` -> ``robot.move_offset``)；旧界面的四个按钮名只是方向标签，
# 这里**不再逐一定义方式**，改用**带符号的毫米值**表达方向：
#   +mm = Y+（进入 / 前进），−mm = Y−（脱离 / 后退）。偏移量记在路点 **Y 分量**。
SOCKET_MODES = ("socket_offset",)
SOCKET_MODE_LABELS = {"socket_offset": "套筒偏移"}
# 工具系相对偏移（DEC-060）：沿 **Bolt 工具系** 的 X/Y/Z 平移 dx/dy/dz(mm)，
# 用一条**直线 movel** 到「当前位姿 + 偏移」的目标位姿（真斜线，不是逐轴走）。
# 与套筒偏移的区别：套筒偏移是旧工程验证过的「Y 轴 step 偏移」原语；
# 本方式用于“先抬升、再斜向下回到原位”这类**多轴相对路径**。
TOOL_OFFSET_MODES = ("tool_offset",)
TOOL_OFFSET_MODE_LABELS = {"tool_offset": "工具系偏移"}
# 全部合法路点方式：点位方式 + 套筒偏移 + 工具系相对偏移。
VALID_MODES = (*POINT_MODES, *SOCKET_MODES, *TOOL_OFFSET_MODES)
LEGACY_SOURCE = "旧工程实测导入（RealMan_Robot/ECO65_NutTighten/configs/config.default.yaml）"


def is_socket_mode(mode: str) -> bool:
    """该路点方式是否为套筒偏移（沿工具系 Y 轴平移偏移）。"""
    return mode in SOCKET_MODES


def is_tool_offset_mode(mode: str) -> bool:
    """该路点方式是否为工具系相对偏移（多轴 mm，一条 movel 直线）。"""
    return mode in TOOL_OFFSET_MODES


def socket_signed_mm(wp: "Waypoint") -> float:
    """套筒偏移路点的**带符号**偏移量(mm)：+ = 进入/前进(Y+)，− = 脱离/后退(Y−)。

    方向直接取 Y 分量本身的正负号（与旧 ``move_offset`` 一致），不再由方式名决定。
    """
    return float(wp.pos[1])


def tool_offset_mm(wp: "Waypoint") -> tuple[float, float, float]:
    """工具系相对偏移路点的 (dx, dy, dz) mm（Bolt 工具系；位置分量前 3 项）。"""
    return float(wp.pos[0]), float(wp.pos[1]), float(wp.pos[2])

MIN_SPEED, MAX_SPEED = 1, 100
MIN_BLEND, MAX_BLEND = 0, 100


def _six(values: Sequence[Any] | None) -> tuple[float, ...]:
    """规范化为恰好 6 个 float；**不做补零**——长度不对直接报错（宁可拒绝不猜）。"""
    nums = list(values or [])
    if len(nums) != 6:
        raise ValueError(f"必须恰好 6 项，得到 {len(nums)} 项（拒绝补 0/缺项）")
    out = []
    for v in nums:
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"含非有限值: {v!r}")
        out.append(f)
    return tuple(out)


# ---------------------------------------------------------------- 模型
@dataclass
class Waypoint:
    """一个路点：方式 + 6 项坐标。

    ``movel``=``[x,y,z(m), rx,ry,rz(rad)]``；``movej``=``[j1..j6](°)``；
    **套筒偏移**=只用 Y 分量记带符号偏移(mm；+ = 进入/前进 Y+，− = 脱离/后退 Y−)，其余分量应为 0；
    **工具系偏移**=前 3 项记 Bolt 工具系 dx/dy/dz(mm，可正负)，后 3 项须为 0。
    """

    mode: str = "movel"
    pos: tuple[float, ...] = (0.0,) * 6

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ValueError(f"路点方式非法: {self.mode!r}；可选 {VALID_MODES}")
        self.pos = _six(self.pos)

    def as_dict(self) -> dict:
        return {"mode": self.mode, "pos": list(self.pos)}


@dataclass
class MotionPlan:
    """一个命名运动方案（示教回放）。"""

    plan_id: str
    desc: str = ""
    default_mode: str = "movel"
    speed: int = 20
    blend: int = 12
    timeout: int = 30
    waypoints: list[Waypoint] = field(default_factory=list)
    source: str = LEGACY_SOURCE

    def __post_init__(self) -> None:
        if not str(self.plan_id).strip():
            raise ValueError("plan_id 不能为空")
        if self.default_mode not in POINT_MODES:
            raise ValueError(f"默认方式非法: {self.default_mode!r}（只能 {POINT_MODES}）")
        self.speed = int(self.speed)
        self.blend = int(self.blend)
        self.timeout = int(self.timeout)

    def clone(self) -> "MotionPlan":
        return MotionPlan(plan_id=self.plan_id, desc=self.desc,
                          default_mode=self.default_mode, speed=self.speed,
                          blend=self.blend, timeout=self.timeout,
                          waypoints=[Waypoint(w.mode, w.pos) for w in self.waypoints],
                          source=self.source)


@dataclass
class NamedPath:
    """一条命名路径（顺序位姿列表，movel 语义）。"""

    path_id: str
    poses: list[tuple[float, ...]] = field(default_factory=list)
    source: str = LEGACY_SOURCE

    def __post_init__(self) -> None:
        if not str(self.path_id).strip():
            raise ValueError("path_id 不能为空")
        self.poses = [_six(p) for p in self.poses]


@dataclass
class SocketSpec:
    """套筒规格与取放位姿（取套筒用）。"""

    code: str                      # "10" / "12" / "14"
    size: str = ""                 # "M10" ...
    rack_station: int = 0
    grip_pose: tuple[float, ...] = (0.0,) * 6
    release_pose: tuple[float, ...] = (0.0,) * 6
    source: str = LEGACY_SOURCE

    def __post_init__(self) -> None:
        if not str(self.code).strip():
            raise ValueError("套筒 code 不能为空")
        self.rack_station = int(self.rack_station)
        self.grip_pose = _six(self.grip_pose)
        self.release_pose = _six(self.release_pose)


@dataclass
class RobotMotionProfile:
    """机器人运动相关全局参数（坐标系/速度上限/引导/工艺/安全）。"""

    active_tool: str = "Bolt"
    active_work: str = "Ganta"
    speed_cap: int = 50
    point_timeout_s: int = 30
    guide_recv_timeout_s: int = 600
    # DEC-058: 引导定位「准备+检测」阶段超时（秒）。正常 1~3 秒返回；超过即判定
    # 相机读帧 / 找圆宿主卡住，步骤如实失败，而不是永远停在"执行中"。
    # <=0 关闭看门狗（调试用）。
    guide_detect_timeout_s: int = 60
    guide_move_speed: int = 10
    guide_move_blend: int = 0
    global_speed: int = 35
    safety: dict = field(default_factory=dict)
    insert: dict = field(default_factory=dict)
    tighten: dict = field(default_factory=dict)
    source: str = LEGACY_SOURCE


@dataclass
class MotionConfig:
    """整份运动配置。"""

    schema_version: int = CONFIG_SCHEMA_VERSION
    plans: list[MotionPlan] = field(default_factory=list)
    paths: list[NamedPath] = field(default_factory=list)
    sockets: list[SocketSpec] = field(default_factory=list)
    profile: RobotMotionProfile = field(default_factory=RobotMotionProfile)

    # ---------- 查询 ----------
    def find_plan(self, plan_id: str) -> MotionPlan | None:
        return next((p for p in self.plans if p.plan_id == plan_id), None)

    def find_path(self, path_id: str) -> NamedPath | None:
        return next((p for p in self.paths if p.path_id == path_id), None)

    def find_socket(self, code: str) -> SocketSpec | None:
        return next((s for s in self.sockets if s.code == str(code)), None)


# ---------------------------------------------------------------- 校验
def validate(cfg: MotionConfig) -> list[str]:
    """运动配置校验；返回问题列表（空 = 通过）。**不修值，只报问题**。"""
    issues: list[str] = []
    seen: set[str] = set()
    for plan in cfg.plans:
        if plan.plan_id in seen:
            issues.append(f"方案 ID 重复: {plan.plan_id!r}")
        seen.add(plan.plan_id)
        if plan.default_mode not in POINT_MODES:
            issues.append(f"方案 {plan.plan_id!r} 默认方式非法: {plan.default_mode!r}")
        if not (MIN_SPEED <= plan.speed <= MAX_SPEED):
            issues.append(f"方案 {plan.plan_id!r} 速度 {plan.speed} 越界 "
                          f"({MIN_SPEED}~{MAX_SPEED})")
        if not (MIN_BLEND <= plan.blend <= MAX_BLEND):
            issues.append(f"方案 {plan.plan_id!r} 交融半径 {plan.blend} 越界 "
                          f"({MIN_BLEND}~{MAX_BLEND})")
        if plan.timeout <= 0:
            issues.append(f"方案 {plan.plan_id!r} 单点超时必须为正")
        for i, wp in enumerate(plan.waypoints, 1):
            if wp.mode not in VALID_MODES:
                issues.append(f"方案 {plan.plan_id!r} 第 {i} 点方式非法: {wp.mode!r}")
            try:
                pos = _six(wp.pos)
            except ValueError as exc:
                issues.append(f"方案 {plan.plan_id!r} 第 {i} 点坐标: {exc}")
                continue
            if is_socket_mode(wp.mode):
                # 套筒偏移只用 Y 分量（带符号偏移 mm）；其余分量应为 0，否则是**填错了列**。
                others = [pos[k] for k in (0, 2, 3, 4, 5)]
                if any(abs(v) > 1e-9 for v in others):
                    issues.append(
                        f"方案 {plan.plan_id!r} 第 {i} 点({SOCKET_MODE_LABELS.get(wp.mode, wp.mode)})"
                        f" 只应填 Y 分量(偏移 mm，带符号)，其余分量须为 0")
                if abs(pos[1]) <= 1e-9:
                    issues.append(
                        f"方案 {plan.plan_id!r} 第 {i} 点({SOCKET_MODE_LABELS.get(wp.mode, wp.mode)})"
                        f" 偏移为 0（无动作）")
            elif is_tool_offset_mode(wp.mode):
                # 工具系偏移：前 3 项 = dx/dy/dz(mm)，后 3 项须为 0。
                if any(abs(pos[k]) > 1e-9 for k in (3, 4, 5)):
                    issues.append(
                        f"方案 {plan.plan_id!r} 第 {i} 点"
                        f"({TOOL_OFFSET_MODE_LABELS.get(wp.mode, wp.mode)})"
                        f" 只用前 3 项填 dx/dy/dz(mm)，后 3 项须为 0")
                if all(abs(pos[k]) <= 1e-9 for k in (0, 1, 2)):
                    issues.append(
                        f"方案 {plan.plan_id!r} 第 {i} 点"
                        f"({TOOL_OFFSET_MODE_LABELS.get(wp.mode, wp.mode)})"
                        f" 偏移为 0（无动作）")
    for path in cfg.paths:
        for i, pose in enumerate(path.poses, 1):
            try:
                _six(pose)
            except ValueError as exc:
                issues.append(f"路径 {path.path_id!r} 第 {i} 点: {exc}")
    for sock in cfg.sockets:
        if not sock.size.strip():
            issues.append(f"套筒 {sock.code!r} 缺规格名(如 M10)")
    if cfg.profile.speed_cap <= 0:
        issues.append("speed_cap 必须为正")
    return issues


# ---------------------------------------------------------------- 读写
def _dump_yaml(data: dict) -> str:
    yml = YAML(typ="safe")
    yml.default_flow_style = False
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.sort_base_mapping_type_on_output = False
    buf = StringIO()
    yml.dump(data, buf)
    return buf.getvalue()


def _load_yaml(text: str) -> dict:
    return YAML(typ="safe").load(text) or {}


def _plan_to_dict(plan: MotionPlan) -> dict:
    return {"plan_id": plan.plan_id, "desc": plan.desc,
            "default_mode": plan.default_mode, "speed": plan.speed,
            "blend": plan.blend, "timeout": plan.timeout,
            "source": plan.source,
            "waypoints": [w.as_dict() for w in plan.waypoints]}


class MotionConfigStore:
    """运动配置的读写与增删改；单文件原子落盘。"""

    def __init__(self, path: str | None = None, *, seed_if_missing: bool = True) -> None:
        self.path = os.path.abspath(path) if path else None
        self._seed_if_missing = seed_if_missing
        self._config: MotionConfig | None = None

    # ---------- 读/写 ----------
    def load(self) -> MotionConfig:
        if self.path and os.path.exists(self.path):
            self._config = self.from_dict(_load_yaml(open(self.path, encoding="utf-8").read()))
        elif self._seed_if_missing:
            self._config = MotionConfig()
            self.save()
        else:
            self._config = MotionConfig()
        return self._config

    def save(self) -> None:
        if self._config is None:
            raise RuntimeError("尚未 load()")
        if self.path is None:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        header = ("# 运动方案配置 (「运动方案」页). 由程序维护。\n"
                  "# source 标注来源：旧工程实测导入 的值来自已现场验证的工程，\n"
                  "# 不代表本工程现场已复核。\n")
        atomic_write(self.path, header + _dump_yaml(self.to_dict()), backup=True)

    def rollback(self) -> None:
        if self.path:
            rollback(self.path)
            self._config = None

    @property
    def config(self) -> MotionConfig:
        if self._config is None:
            self.load()
        assert self._config is not None
        return self._config

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        cfg = self.config
        return {
            "schema_version": cfg.schema_version,
            "plans": [_plan_to_dict(p) for p in cfg.plans],
            "paths": [{"path_id": p.path_id, "poses": [list(x) for x in p.poses],
                       "source": p.source} for p in cfg.paths],
            "sockets": [{"code": s.code, "size": s.size,
                         "rack_station": s.rack_station,
                         "grip_pose": list(s.grip_pose),
                         "release_pose": list(s.release_pose),
                         "source": s.source} for s in cfg.sockets],
            "profile": {**asdict(cfg.profile)},
        }

    @classmethod
    def from_dict(cls, data: dict) -> MotionConfig:
        if not isinstance(data, dict):
            raise ValueError("运动配置顶层必须是映射")
        version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"不支持的 schema_version={version!r}")
        plans = []
        for raw in (data.get("plans") or []):
            wps = [Waypoint(str(w.get("mode", "movel")), tuple(w.get("pos") or ()))
                   for w in (raw.get("waypoints") or [])]
            plans.append(MotionPlan(
                plan_id=str(raw["plan_id"]), desc=str(raw.get("desc", "")),
                default_mode=str(raw.get("default_mode", "movel")),
                speed=raw.get("speed", 20), blend=raw.get("blend", 12),
                timeout=raw.get("timeout", 30), waypoints=wps,
                source=str(raw.get("source", ""))))
        paths = [NamedPath(str(p["path_id"]),
                           [tuple(x) for x in (p.get("poses") or [])],
                           str(p.get("source", "")))
                 for p in (data.get("paths") or [])]
        sockets = [SocketSpec(str(s["code"]), str(s.get("size", "")),
                              s.get("rack_station", 0),
                              tuple(s.get("grip_pose") or (0.0,) * 6),
                              tuple(s.get("release_pose") or (0.0,) * 6),
                              str(s.get("source", "")))
                   for s in (data.get("sockets") or [])]
        prof_raw = dict(data.get("profile") or {})
        known = set(RobotMotionProfile.__dataclass_fields__)
        prof = RobotMotionProfile(**{k: v for k, v in prof_raw.items() if k in known})
        return MotionConfig(schema_version=int(version), plans=plans, paths=paths,
                            sockets=sockets, profile=prof)

    # ---------- 方案 CRUD ----------
    def add_plan(self, plan: MotionPlan) -> MotionPlan:
        if self.config.find_plan(plan.plan_id) is not None:
            raise ValueError(f"方案 ID {plan.plan_id!r} 已存在")
        self.config.plans.append(plan)
        return plan

    def update_plan(self, plan_id: str, plan: MotionPlan) -> MotionPlan:
        if plan.plan_id != plan_id and self.config.find_plan(plan.plan_id) is not None:
            raise ValueError(f"方案 ID {plan.plan_id!r} 已被占用")
        for i, p in enumerate(self.config.plans):
            if p.plan_id == plan_id:
                self.config.plans[i] = plan
                return plan
        raise KeyError(f"未找到方案 {plan_id!r}")

    def rename_plan(self, old: str, new: str) -> MotionPlan:
        plan = self.config.find_plan(old)
        if plan is None:
            raise KeyError(f"未找到方案 {old!r}")
        if new == old:
            return plan
        if self.config.find_plan(new) is not None:
            raise ValueError(f"方案 ID {new!r} 已存在")
        plan.plan_id = str(new)
        return plan

    def remove_plan(self, plan_id: str) -> None:
        self.config.plans = [p for p in self.config.plans if p.plan_id != plan_id]

    # ---------- 路点级 ----------
    def add_waypoint(self, plan_id: str, wp: Waypoint, index: int | None = None) -> None:
        plan = self._require(plan_id)
        if index is None or index >= len(plan.waypoints):
            plan.waypoints.append(wp)
        else:
            plan.waypoints.insert(index, wp)

    def update_waypoint(self, plan_id: str, index: int, wp: Waypoint) -> None:
        plan = self._require(plan_id)
        if not (0 <= index < len(plan.waypoints)):
            raise IndexError(f"路点下标越界: {index}")
        plan.waypoints[index] = wp

    def remove_waypoint(self, plan_id: str, index: int) -> None:
        plan = self._require(plan_id)
        if not (0 <= index < len(plan.waypoints)):
            raise IndexError(f"路点下标越界: {index}")
        plan.waypoints.pop(index)

    def move_waypoint(self, plan_id: str, src: int, dst: int) -> None:
        plan = self._require(plan_id)
        n = len(plan.waypoints)
        if not (0 <= src < n and 0 <= dst < n):
            raise IndexError(f"路点下标越界: {src}->{dst}")
        plan.waypoints.insert(dst, plan.waypoints.pop(src))

    def _require(self, plan_id: str) -> MotionPlan:
        plan = self.config.find_plan(plan_id)
        if plan is None:
            raise KeyError(f"未找到方案 {plan_id!r}")
        return plan


__all__ = [
    "CONFIG_SCHEMA_VERSION", "LEGACY_SOURCE", "MAX_BLEND", "MAX_SPEED", "MIN_BLEND",
    "MIN_SPEED", "MotionConfig", "MotionConfigStore", "MotionPlan", "NamedPath",
    "POINT_MODES", "RobotMotionProfile", "SOCKET_MODE_LABELS", "SOCKET_MODES",
    "SocketSpec", "TOOL_OFFSET_MODE_LABELS", "TOOL_OFFSET_MODES", "VALID_MODES",
    "Waypoint", "is_socket_mode", "is_tool_offset_mode", "socket_signed_mm",
    "tool_offset_mm", "validate",
]
