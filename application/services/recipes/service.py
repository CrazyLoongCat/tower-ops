"""配方/配置服务: 五层校验、草稿/发布/冻结快照、override 保护、发布审计 (12 §4.4, 02 §10, T20/T34/T36).

五层: 结构类型 -> 值/单位/枚举 -> 引用存在 -> 能力/设备绑定 -> 资源/业务可执行性.
草稿/发布/运行快照严格分离; 设备寻址/互锁/停止/硬限值不可被配方覆盖.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from ruamel.yaml import YAML

from application.contracts.context import VersionSnapshot
# 引导定位的段名集合与运动方案执行器共用同一份定义（contracts 中性层，避免循环导入）。
from application.contracts.enums import ConditionOp, GUIDE_STAGES, NodeType
from application.domain.actions import ActionCatalog
from application.domain.resources import MOTION_GLOBAL
from application.contracts.targets import JointTarget
from application.services.recipes.models import ConditionModel, RecipeModel, StepModel
# 受保护路径单一定义在 infrastructure.config.base, 此处沿用同一份以避免两处漂移。
from application.infrastructure.config.atomic import atomic_write, rollback
from application.infrastructure.config.base import (
    PROTECTED_PATH_PREFIXES,
    is_protected as _is_protected,
)
from application.infrastructure import logging_setup

_log = logging_setup.get_logger("services.recipes")

# 每种 node type 必需字段 (结构层校验).
# vision.run 的检测来源是**二选一** (pipeline_ref 或 detection_ref), 单独校验, 见下方。
# robot.move_joint 的"必需"是 **joint_target_ref 或 motion_plan_ref 二选一**, 单独校验。
_REQUIRED_FIELDS: dict[NodeType, tuple[str, ...]] = {
    NodeType.ROBOT_MOVE_JOINT: (),
    NodeType.MECHANISM_ACTION: ("action_ref",),
    NodeType.CUSTOM_CALL: ("method_ref",),
    NodeType.CAMERA_CAPTURE: ("camera_ref",),
    NodeType.VISION_RUN: ("input_from",),
    NodeType.GUIDED_POSITION: ("camera_ref",),
    NodeType.CONDITION: ("predicate",),
    NodeType.WAIT_STATE: ("device_ref",),
    NodeType.OPERATOR_CONFIRM: (),
    NodeType.RECORD_EVIDENCE: ("inputs",),
    NodeType.SUBFLOW: ("subflow_ref",),
    NodeType.REPEAT: ("subflow_ref", "max_iterations"),
    NodeType.PARALLEL_GROUP: ("branches",),
}


class PublishError(RuntimeError):
    """发布被拒绝."""


@dataclass
class ValidationReport:
    """五层校验结果. ok 为 True 才允许发布.

    ``layers`` 保留旧的「层 -> 消息」形态供既有调用/测试使用；``entries`` 额外记录
    **可定位信息**（哪一步 / 哪个字段），供配方页把错误点回到具体控件上。
    """

    recipe_id: str
    layers: dict[str, list[str]] = field(default_factory=dict)
    entries: list[dict[str, Any]] = field(default_factory=list)

    def add(self, layer: str, message: str, *,
            step_id: str | None = None, field_name: str | None = None) -> None:
        self.layers.setdefault(layer, []).append(message)
        self.entries.append({"layer": layer, "message": message,
                             "step_id": step_id, "field_name": field_name})

    @property
    def errors(self) -> list[str]:
        return [f"[{layer}] {m}" for layer, msgs in self.layers.items() for m in msgs]

    @property
    def ok(self) -> bool:
        return not self.layers


@dataclass
class ReferenceCatalog:
    """校验引用所需的外部事实来源 (示教点/流水线/动作/相机/设备/子流程/自定义方法)."""

    joint_targets: dict[str, JointTarget] = field(default_factory=dict)
    pipelines: set[str] = field(default_factory=set)
    actions: ActionCatalog | None = None
    cameras: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    subflows: Any = None   # SubflowRegistry (E13); None 表示不接受 subflow 引用
    # 命名运动方案 (「运动方案」页) 的 plan_id 集合; robot.move_joint 可引用
    motion_plans: set[str] = field(default_factory=set)
    # 额外精定位偏移参数套 (DEC-065) 的 profile_id 集合
    fine_offset_profiles: set[str] = field(default_factory=set)
    # 命名视觉检测 (「视觉调试」页另存): detection_id -> pipeline_ref
    detections: dict[str, str] = field(default_factory=dict)
    # 自定义方法库 (application/services/methods.py); None 表示不接受 custom.call 引用
    methods: Any = None


def canonical_hash(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RecipeService:
    """配方服务: 草稿保内存 + 已发布历史; 运行快照冻结不可变."""

    def __init__(self, catalog: ReferenceCatalog, *, audit_sink=None,
                 authorization=None) -> None:
        self._catalog = catalog
        self._drafts: dict[str, RecipeModel] = {}
        # 草稿的来源文件（绝对路径）：页面「保存」按它直接覆盖回原文件（DEC-035）。
        # 只在载入 / 另存为时记录；编辑（draft）不会抹掉它。
        self._sources: dict[str, str] = {}
        self._published: dict[str, list[tuple[str, RecipeModel, str]]] = {}
        self._audit: list[dict] = []
        self._audit_sink = audit_sink
        self._authorization = authorization   # AuthorizationService (E17)

    # ---------- 草稿 ----------
    def draft(self, recipe: dict[str, Any] | RecipeModel, *,
              source: str | None = None) -> str:
        """载入/覆盖一条草稿；``source`` = 来源文件（给出时记录，供「保存」覆盖）。"""
        model = recipe if isinstance(recipe, RecipeModel) else RecipeModel.model_validate(recipe)
        self._drafts[model.recipe_id] = model
        if source:
            self._sources[model.recipe_id] = os.path.abspath(source)
        return model.recipe_id

    def source_of(self, recipe_id: str) -> str | None:
        """草稿的来源文件（未保存过/新建的草稿 = None）。"""
        return self._sources.get(recipe_id)

    def set_source(self, recipe_id: str, path: str | None) -> None:
        """记录/清除草稿的来源文件（「另存为/保存」落盘后调用）。"""
        if path:
            self._sources[recipe_id] = os.path.abspath(path)
        else:
            self._sources.pop(recipe_id, None)

    def delete(self, recipe_id: str, *, remove_file: bool = True) -> dict[str, Any]:
        """删除一条执行方案：草稿 + 来源文件（DEC-108）。

        - 仍被其他执行方案的 subflow 步骤引用时**拒绝**（先解除引用再删）；
        - 来源文件不直接删除，改名为 ``<文件>.deleted-<时间戳>.bak``：目录扫描
          跳过 ``.bak``，下次启动不再载入，误删可手工改回原名恢复。
        返回 ``{"recipe_id", "source", "backup"}``。
        """
        model = self._drafts.get(recipe_id)
        if model is None:
            raise PublishError(f"无草稿 {recipe_id!r}")
        referrers = sorted({
            other_id
            for other_id, other in self._drafts.items()
            if other_id != recipe_id
            and any(getattr(s, "subflow_ref", None) == recipe_id
                    for s in _all_steps(other))
        })
        if referrers:
            raise PublishError(
                f"执行方案 {recipe_id!r} 仍被 {', '.join(referrers)} 引用"
                "（子流程步骤），请先解除引用再删除")
        source = self._sources.pop(recipe_id, None)
        self._drafts.pop(recipe_id, None)
        self._published.pop(recipe_id, None)
        backup = None
        if remove_file and source and os.path.exists(source):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = f"{source}.deleted-{stamp}.bak"
            os.replace(source, backup)
        self._audit_event("delete", recipe_id, model.version, "")
        _log.info("删除执行方案 %s（来源 %s -> %s）", recipe_id, source, backup)
        return {"recipe_id": recipe_id, "source": source, "backup": backup}

    def get_draft(self, recipe_id: str) -> RecipeModel:
        if recipe_id not in self._drafts:
            raise PublishError(f"无草稿 {recipe_id!r}")
        return self._drafts[recipe_id]

    def drafts(self) -> list[str]:
        """当前可用草稿的 recipe_id 列表 (供 UI 选择)."""
        return sorted(self._drafts)

    def is_offline_draft(self, recipe_id: str) -> bool:
        model = self._drafts.get(recipe_id)
        return bool(model is not None and model.is_offline_draft)

    def detection_map(self) -> dict[str, str]:
        """命名视觉检测 detection_id -> pipeline_ref (供编译器解析 vision.run)。"""
        return dict(getattr(self._catalog, "detections", {}) or {})

    @property
    def catalog(self) -> "ReferenceCatalog":
        """引用目录（只读用途：配方页据此列出可选的方案/检测/相机/关节目标）。"""
        return self._catalog

    # ---------- 校验 ----------
    def validate(self, recipe_id: str) -> ValidationReport:
        model = self.get_draft(recipe_id)
        return validate_recipe(model, self._catalog)

    # ---------- 发布 ----------
    def publish(self, recipe_id: str, *,
                allow_offline_draft: bool = False,
                principal=None) -> str:
        """发布即生效（DEC-039 / DEC-040）。

        通过五层校验后冻结一个 ``state="published"`` 的 revision，并把**草稿本身**
        也标记为已发布——作业台启动校验据此放行（发布后不再报「执行方案未发布」）。
        **不询问、不记录操作人员**（DEC-040）；权限/审计（E17）仍按需生效。
        """
        model = self.get_draft(recipe_id)
        if model.is_offline_draft and not allow_offline_draft:
            raise PublishError("离线示例草稿不得用于真实生产发布 (is_offline_draft=True)")
        report = validate_recipe(model, self._catalog)
        if not report.ok:
            raise PublishError("校验未通过: " + "; ".join(report.errors))
        revision = f"{model.version}+{len(self._published.get(recipe_id, [])) + 1}"
        frozen = model.model_copy(deep=True)
        frozen.state = "published"
        hash_ = canonical_hash(frozen.model_dump(mode="json"))
        # E17: 服务层权限 + 追加审计 (绕过 UI 直接调用同样被拒绝)
        if self._authorization is not None:
            from application.services.authorization import Permission
            self._authorization.require(
                principal, Permission.PUBLISH_RECIPE,
                object_type="recipe", object_id=recipe_id,
                after_hash=hash_, reason=f"publish revision {revision}",
                detail={"revision": revision})
        self._published.setdefault(recipe_id, []).append((revision, frozen, hash_))
        model.state = "published"   # DEC-040: 草稿同步标记, 作业台据此放行
        self._audit_event("publish", recipe_id, revision, hash_)
        _log.info("发布执行方案 %s@%s hash=%s", recipe_id, revision, hash_[:12])
        return revision

    def history(self, recipe_id: str) -> list[tuple[str, str]]:
        return [(rev, h) for rev, _m, h in self._published.get(recipe_id, [])]

    def compare(self, recipe_id: str, revision_a: str, revision_b: str) -> dict[str, Any]:
        a = self._find(recipe_id, revision_a)
        b = self._find(recipe_id, revision_b)
        da, db = a.model_dump(mode="json"), b.model_dump(mode="json")
        changed = {k: {"a": da.get(k), "b": db.get(k)}
                   for k in set(da) | set(db) if da.get(k) != db.get(k)}
        return {"recipe_id": recipe_id, "a": revision_a, "b": revision_b,
                "changed": changed}

    def activate(self, recipe_id: str, revision: str) -> VersionSnapshot:
        model = self._find(recipe_id, revision)
        hash_ = canonical_hash(model.model_dump(mode="json"))
        return VersionSnapshot(
            recipe_id=recipe_id, recipe_version=revision,
            config_hash=canonical_hash({"recipe": model.model_dump(mode="json")}),
            calibration_hash="unset",
        )

    # ---------- 在途草稿不改运行 hash ----------
    def running_hash(self, recipe_id: str, revision: str) -> str:
        return canonical_hash(self._find(recipe_id, revision).model_dump(mode="json"))

    # ---------- 审计 ----------
    def _audit_event(self, action: str, recipe_id: str,
                     revision: str, hash_: str) -> None:
        # DEC-040: 发布不再记录操作人员
        entry = {
            "action": action, "recipe_id": recipe_id,
            "revision": revision, "hash": hash_,
            "utc": datetime.now(timezone.utc).isoformat(),
        }
        self._audit.append(entry)
        if self._audit_sink is not None:
            self._audit_sink(entry)

    @property
    def audit(self) -> list[dict]:
        return list(self._audit)

    def _find(self, recipe_id: str, revision: str) -> RecipeModel:
        for rev, model, _h in self._published.get(recipe_id, []):
            if rev == revision:
                return model
        raise PublishError(f"未找到发布版本 {recipe_id!r}@{revision!r}")


def _all_steps(model: RecipeModel) -> list[StepModel]:
    """顶层步骤 + 并行组分支（DEC-096）：结构/值/引用校验对两者同一套规则。

    分支也是执行账本键与节点键，不能因为「挂在一个组下面」就绕过示教点/动作/
    相机等引用存在性检查。
    """
    out: list[StepModel] = list(model.sequence)
    for step in model.sequence:
        out.extend(step.branches)
    return out


def validate_recipe(model: RecipeModel, catalog: ReferenceCatalog) -> ValidationReport:
    """五层校验. 任一错误即不可发布."""
    report = ValidationReport(recipe_id=model.recipe_id)
    all_steps = _all_steps(model)

    # 层1 结构/类型
    for step in all_steps:
        for field_name in _REQUIRED_FIELDS.get(step.type, ()):
            val = getattr(step, field_name)
            if val in (None, (), {}):
                report.add("structure", f"步骤 {step.id!r}({step.type.value}) 缺 {field_name}",
                           step_id=step.id, field_name=field_name)
        if step.type is NodeType.PARALLEL_GROUP:
            branches = tuple(step.branches)
            robots = [b for b in branches if b.type is NodeType.ROBOT_MOVE_JOINT]
            mechs = [b for b in branches if b.type is NodeType.MECHANISM_ACTION]
            if len(branches) != 2:
                report.add("structure",
                           f"并行执行步骤 {step.id!r} 必须有 2 个分支（当前 {len(branches)}）",
                           step_id=step.id, field_name="branches")
            if len(robots) != 1 or len(mechs) != 1 or len(branches) != 2:
                report.add("structure",
                           f"并行执行步骤 {step.id!r} 的分支必须恰好是一个 robot.move_joint "
                           f"与一个 mechanism.action（当前 "
                           f"{[b.type.value for b in branches]}）",
                           step_id=step.id, field_name="branches")
            for b in branches:
                if b.type in (NodeType.PARALLEL_GROUP, NodeType.SUBFLOW, NodeType.REPEAT):
                    report.add("structure",
                               f"并行执行步骤 {step.id!r} 的分支 {b.id!r}({b.type.value}) "
                               f"不支持嵌套（首版只允许机器人运动 + 机构动作）",
                               step_id=step.id, field_name="branches")
                if b.post_checks:
                    report.add("structure",
                               f"并行执行步骤 {step.id!r} 的分支 {b.id!r} 不支持判断属性"
                               f"（判断属性挂在整个并行步骤上）",
                               step_id=step.id, field_name="branches")
        elif step.branches:
            report.add("structure",
                       f"步骤 {step.id!r}({step.type.value}) 不是并行执行，不能带 branches",
                       step_id=step.id, field_name="branches")
        if step.type is NodeType.REPEAT:
            if not step.max_iterations or step.max_iterations <= 0:
                report.add("structure", f"步骤 {step.id!r}: repeat 必须有界 max_iterations>0",
                           step_id=step.id, field_name="max_iterations")
        if step.type is NodeType.ROBOT_MOVE_JOINT:
            # 运动步骤自带示教点：joint_target_ref 与 motion_plan_ref 二选一（至少一个）
            has_ref = bool((step.joint_target_ref or "").strip())
            has_plan = bool((step.motion_plan_ref or "").strip())
            if not has_ref and not has_plan:
                report.add("structure",
                           f"步骤 {step.id!r}(robot.move_joint) 需提供 joint_target_ref 或 motion_plan_ref",
                           step_id=step.id, field_name="joint_target_ref")
        if step.type is NodeType.ROBOT_MOVE_BY_BOLT_SIZE:
            # DEC-155：必须绑定运动方案，且 16/20/24 至少配一条路点映射
            if not (step.motion_plan_ref or "").strip():
                report.add("structure",
                           f"步骤 {step.id!r}(robot.move_by_bolt_size) 必须选择运动方案",
                           step_id=step.id, field_name="motion_plan_ref")
            mapping = dict(step.bolt_size_waypoints or {})
            if not mapping:
                report.add("structure",
                           f"步骤 {step.id!r} 未配置螺栓直径→路点映射（16/20/24）",
                           step_id=step.id, field_name="bolt_size_waypoints")
            for key, value in mapping.items():
                if str(key) not in ("16", "20", "24"):
                    report.add("structure",
                               f"步骤 {step.id!r} 的螺栓直径键 {key!r} 非法（只认 16/20/24）",
                               step_id=step.id, field_name="bolt_size_waypoints")
                try:
                    ok_val = int(value) >= 1
                except (TypeError, ValueError):
                    ok_val = False
                if not ok_val:
                    report.add("structure",
                               f"步骤 {step.id!r} 的螺栓直径 {key!r} 路点序号必须 ≥1",
                               step_id=step.id, field_name="bolt_size_waypoints")
            via = getattr(step, "bolt_size_via_waypoint", None)
            if via is not None and int(via) < 1:
                report.add("structure",
                           f"步骤 {step.id!r} 的先到路点序号必须 ≥1（或留空）",
                           step_id=step.id, field_name="bolt_size_via_waypoint")
        if step.type is NodeType.VISION_RUN:
            # 检测来源二选一: 直接引流水线, 或引一条命名视觉检测 (不能都给/都不给)
            has_pipe = bool((step.pipeline_ref or "").strip())
            has_det = bool((step.detection_ref or "").strip())
            if not has_pipe and not has_det:
                report.add("structure",
                           f"步骤 {step.id!r}(vision.run) 需提供 pipeline_ref 或 detection_ref",
                           step_id=step.id, field_name="pipeline_ref")
            if has_pipe and has_det:
                report.add("structure",
                           f"步骤 {step.id!r}(vision.run) pipeline_ref 与 detection_ref 只能给一个",
                           step_id=step.id, field_name="pipeline_ref")

    # 并行分支 ID 与顶层步骤同处一个账本命名空间，必须整体唯一（DEC-096）
    top_ids = {s.id for s in model.sequence}
    branch_ids: list[str] = []
    for s in model.sequence:
        for b in s.branches:
            branch_ids.append(b.id)
            if b.id in top_ids:
                report.add("structure",
                           f"并行执行步骤 {s.id!r} 的分支 ID {b.id!r} 与顶层步骤重复",
                           step_id=s.id, field_name="branches")
    if len(branch_ids) != len(set(branch_ids)):
        report.add("structure", "并行执行分支的 step.id 必须整体唯一",
                   field_name="branches")

    # 层2 值/单位/枚举 + 条件有界
    for step in all_steps:
        if step.predicate is not None:
            if step.predicate.depth() > step.predicate.max_depth:
                report.add("value", f"条件 {step.id!r} 嵌套超过深度上限",
                           step_id=step.id, field_name="predicate")
        if step.type is NodeType.CONDITION and step.on_false is None and step.on_true is None:
            report.add("value", f"条件步骤 {step.id!r} 未声明分支策略",
                       step_id=step.id, field_name="on_false")
        if step.type is NodeType.WAIT_STATE:
            if step.stable_ticks < 1:
                report.add("value", f"wait.state {step.id!r} 的 stable_ticks 必须 >=1",
                           step_id=step.id, field_name="stable_ticks")
            if step.deadline_ms is not None and step.deadline_ms <= 0:
                report.add("value", f"wait.state {step.id!r} 的 deadline_ms 必须为正",
                           step_id=step.id, field_name="deadline_ms")
        if step.type is NodeType.OPERATOR_CONFIRM and len(step.operator_options) < 2:
            report.add("value", f"operator.confirm {step.id!r} 至少需要 2 个允许选项",
                       step_id=step.id, field_name="operator_options")
        if step.type is NodeType.GUIDED_POSITION:
            stages = tuple(step.guide_stages or ())
            bad = [s for s in stages if s not in GUIDE_STAGES]
            if not stages or bad:
                report.add("value",
                           f"引导定位 {step.id!r} 的段选择必须非空且只含 "
                           f"{GUIDE_STAGES}，当前 {stages!r}",
                           step_id=step.id, field_name="guide_stages")
            elif len(set(stages)) != len(stages):
                report.add("value", f"引导定位 {step.id!r} 的段选择有重复 {stages!r}",
                           step_id=step.id, field_name="guide_stages")
            if step.guide_speed_pct is not None \
                    and not (1 <= int(step.guide_speed_pct) <= 100):
                report.add("value",
                           f"引导定位 {step.id!r} 的速度 {step.guide_speed_pct}% 越界 (1~100)",
                           step_id=step.id, field_name="guide_speed_pct")
            if float(step.guide_max_offset_mm or 0.0) < 0:
                report.add("value",
                           f"引导定位 {step.id!r} 的偏移上限不能为负 "
                           f"({step.guide_max_offset_mm}mm)",
                           step_id=step.id, field_name="guide_max_offset_mm")
    for path in model.overrides:
        if _is_protected(path):
            report.add("value", f"override {path!r} 命中受保护路径 (不许覆盖)",
                       field_name=path)

    # 层3 引用存在
    step_ids = {s.id for s in model.sequence}
    for step in all_steps:
        if step.type is NodeType.VISION_RUN and (step.pipeline_ref or "").strip() \
                and step.pipeline_ref not in catalog.pipelines:
            report.add("reference", f"步骤 {step.id!r} 引用未知流水线 {step.pipeline_ref!r}",
                       step_id=step.id, field_name="pipeline_ref")
        if step.type is NodeType.VISION_RUN and (step.detection_ref or "").strip():
            pipe = catalog.detections.get(step.detection_ref)
            if pipe is None:
                report.add("reference",
                           f"步骤 {step.id!r} 引用未知视觉检测 {step.detection_ref!r}",
                           step_id=step.id, field_name="detection_ref")
            elif pipe not in catalog.pipelines:
                report.add("reference",
                           f"步骤 {step.id!r} 的视觉检测 {step.detection_ref!r} 引用的流水线 "
                           f"{pipe!r} 未注册",
                           step_id=step.id, field_name="detection_ref")
        if step.type in (NodeType.ROBOT_MOVE_JOINT,
                         NodeType.ROBOT_MOVE_BY_BOLT_SIZE) \
                and (step.motion_plan_ref or "").strip() \
                and step.motion_plan_ref not in catalog.motion_plans:
            report.add("reference",
                       f"步骤 {step.id!r} 引用未知运动方案 {step.motion_plan_ref!r}",
                       step_id=step.id, field_name="motion_plan_ref")
        if step.type is NodeType.ROBOT_MOVE_JOINT and (step.joint_target_ref or "").strip() \
                and step.joint_target_ref not in catalog.joint_targets:
            report.add("reference",
                       f"步骤 {step.id!r} 引用未知示教点 {step.joint_target_ref!r}",
                       step_id=step.id, field_name="joint_target_ref")
        if step.type is NodeType.CUSTOM_CALL:
            method = None
            reg = catalog.methods
            if reg is None:
                report.add("reference",
                           f"步骤 {step.id!r} 引用自定义方法 {step.method_ref!r}，但未提供方法库",
                           step_id=step.id, field_name="method_ref")
            else:
                method = reg.find(step.method_ref or "")
                if method is None:
                    report.add("reference",
                               f"步骤 {step.id!r} 引用未知自定义方法 {step.method_ref!r}",
                               step_id=step.id, field_name="method_ref")
            for dev in step.device_refs:
                if dev not in catalog.devices:
                    report.add("reference",
                               f"步骤 {step.id!r} 自定义操作的设备 {dev!r} 未注册",
                               step_id=step.id, field_name="device_refs")
            if method is not None and not method.accepts_devices(len(step.device_refs)):
                report.add("capability",
                           f"步骤 {step.id!r} 自定义方法 {method.method_id!r} {method.device_hint()}，"
                           f"但选了 {len(step.device_refs)} 个设备",
                           step_id=step.id, field_name="device_refs")
        if step.type is NodeType.MECHANISM_ACTION:
            if catalog.actions is None or not catalog.actions.known(step.action_ref or ""):
                report.add("reference", f"步骤 {step.id!r} 引用未知动作 {step.action_ref!r}",
                           step_id=step.id, field_name="action_ref")
        if step.type is NodeType.CAMERA_CAPTURE and step.camera_ref not in catalog.cameras:
            report.add("reference", f"步骤 {step.id!r} 引用未知相机 {step.camera_ref!r}",
                       step_id=step.id, field_name="camera_ref")
        if step.type is NodeType.GUIDED_POSITION:
            # DEC-137：引导定位可用**任意**设备配置里已启用的相机（默认 ds86_01，
            # D405 USB 等同一条深度引导链）；引用了未启用的相机在校验期就报。
            if step.camera_ref not in catalog.cameras:
                report.add("reference",
                           f"引导定位 {step.id!r} 引用的相机 {step.camera_ref!r} 不在设备配置的"
                           f"已启用相机里（请在设备配置页启用该相机通道；未连接时执行方案仍可"
                           f"编辑，运行时该步骤会如实失败）",
                           step_id=step.id, field_name="camera_ref")
            ref = (getattr(step, "fine_profile_ref", None) or "").strip()
            if ref and ref not in catalog.fine_offset_profiles:
                report.add("reference",
                           f"引导定位 {step.id!r} 引用未知精定位参数套 {ref!r}"
                           f"（请在「参数配置」页添加）",
                           step_id=step.id, field_name="fine_profile_ref")
        if step.type is NodeType.WAIT_STATE and step.device_ref not in catalog.devices:
            report.add("reference", f"步骤 {step.id!r} 引用未知设备 {step.device_ref!r}",
                       step_id=step.id, field_name="device_ref")
        if step.input_from and step.input_from.get("step") not in step_ids:
            report.add("reference", f"步骤 {step.id!r} input_from 引用未知步骤",
                       step_id=step.id, field_name="input_from")
        src = getattr(step.predicate, "source", None) if step.predicate else None
        if src is not None and src.step not in step_ids:
            report.add("reference", f"步骤 {step.id!r} 条件引用未知步骤 {src.step!r}",
                       step_id=step.id, field_name="predicate")
        if step.type is NodeType.RECORD_EVIDENCE:
            for ref in step.inputs:
                if ref not in step_ids:
                    report.add("reference", f"步骤 {step.id!r} evidence 引用未知步骤 {ref!r}",
                               step_id=step.id, field_name="inputs")
        # E13: 子流程模板引用与 override 白名单
        if step.type in (NodeType.SUBFLOW, NodeType.REPEAT):
            ref = step.subflow_ref or ""
            registry = catalog.subflows
            if registry is None:
                report.add("reference",
                           f"步骤 {step.id!r} 引用子流程 {ref!r}，但未提供模板注册表",
                           step_id=step.id, field_name="subflow_ref")
            elif not getattr(registry, "known", lambda _x: False)(ref):
                report.add("reference", f"步骤 {step.id!r} 引用未知子流程模板 {ref!r}",
                           step_id=step.id, field_name="subflow_ref")
            else:
                tpl = registry.get(ref)
                extra = set(step.overrides) - set(getattr(tpl, "allow_overrides", ()))
                if extra:
                    report.add("value",
                               f"步骤 {step.id!r} override 不在模板白名单: {sorted(extra)}",
                               step_id=step.id, field_name="overrides")

    # 层3b 步骤后判断属性（DEC-064）；并行分支的判断按「随父步骤同序」处理（DEC-096）
    order: dict[str, int] = {}
    ordered: list[StepModel] = []
    for s in model.sequence:
        order[s.id] = len(ordered)
        ordered.append(s)
        for b in s.branches:
            order[b.id] = len(ordered)
            ordered.append(b)
    for i, step in enumerate(ordered):
        for n, chk in enumerate(getattr(step, "post_checks", ()) or (), start=1):
            label = f"步骤 {step.id!r} 判断#{n}"
            kind = str(getattr(chk, "kind", ""))
            if kind == "robot_pose":
                ref = (getattr(chk, "joint_target_ref", "") or "").strip()
                if not ref:
                    report.add("value", f"{label} 未选择示教点",
                               step_id=step.id, field_name="post_checks")
                elif ref not in catalog.joint_targets:
                    report.add("reference",
                               f"{label} 引用未知示教点 {ref!r}",
                               step_id=step.id, field_name="post_checks")
            elif kind == "device_field":
                dev = (getattr(chk, "device_ref", "") or "").strip()
                field = (getattr(chk, "field_name", "") or "").strip()
                if not dev:
                    report.add("value", f"{label} 未选择设备",
                               step_id=step.id, field_name="post_checks")
                elif dev not in catalog.devices:
                    report.add("reference",
                               f"{label} 引用未知设备 {dev!r}",
                               step_id=step.id, field_name="post_checks")
                if not field:
                    report.add("value", f"{label} 未填写字段名",
                               step_id=step.id, field_name="post_checks")
                if getattr(chk, "op", None) is None:
                    report.add("value", f"{label} 未选择比较算子",
                               step_id=step.id, field_name="post_checks")
            elif kind == "step_output":
                src = (getattr(chk, "source_step", "") or "").strip()
                field = (getattr(chk, "field_name", "") or "").strip()
                if not src:
                    report.add("value", f"{label} 未选择前序步骤",
                               step_id=step.id, field_name="post_checks")
                elif src not in order:
                    report.add("reference",
                               f"{label} 引用未知步骤 {src!r}",
                               step_id=step.id, field_name="post_checks")
                elif order[src] >= i:
                    report.add("value",
                               f"{label} 只能引用本步骤之前的步骤输出（{src!r} 在后面）",
                               step_id=step.id, field_name="post_checks")
                if not field:
                    report.add("value", f"{label} 未填写字段名",
                               step_id=step.id, field_name="post_checks")
                if getattr(chk, "op", None) is None:
                    report.add("value", f"{label} 未选择比较算子",
                               step_id=step.id, field_name="post_checks")
            else:
                report.add("value", f"{label} 类型非法: {kind!r}",
                           step_id=step.id, field_name="post_checks")

    # 层4 能力/设备绑定
    for step in all_steps:
        if step.type is NodeType.ROBOT_MOVE_JOINT and (step.joint_target_ref or "").strip():
            jt = catalog.joint_targets.get(step.joint_target_ref)
            if jt is not None and len(jt.joint_deg) != 6:
                report.add("capability", f"步骤 {step.id!r} 的示教点不是 6 项关节角",
                           step_id=step.id, field_name="joint_target_ref")
        if step.type is NodeType.PARALLEL_GROUP:
            # DEC-096：机构动作若仍占 motion_global，会和机器人分支互锁——发布前
            # 就拒绝，不在运行期表现成「第一个分支跑完才轮到第二个」。
            for b in step.branches:
                if b.type is not NodeType.MECHANISM_ACTION:
                    continue
                ref = (b.action_ref or "").strip()
                if catalog.actions is None or not catalog.actions.known(ref):
                    continue        # 引用错误已在层3报告
                defn = catalog.actions.get(ref)
                if MOTION_GLOBAL in tuple(defn.resource_ids or ()):
                    report.add(
                        "capability",
                        f"并行执行步骤 {step.id!r} 的机构动作 {ref!r} 占用 motion_global，"
                        f"无法与机器人分支并行（请改用板卡独立资源）",
                        step_id=step.id, field_name="branches")

    # 层5 资源/业务
    if not model.sequence:
        report.add("business", "sequence 为空（执行方案没有任何步骤）")

    return report


def load_recipes_dir(recipes: RecipeService, directory: str, *,
                     skip_paths: tuple[str, ...] = (),
                     log: Any = None) -> list[str]:
    """把目录里的 ``*.yaml`` 配方都载入为草稿（DEC-035）。

    这样「另存为」存到 ``configs/recipes/`` 的配方，**下次启动直接出现在配方下拉里**，
    不用再改 ``--recipe``。单个文件坏（旧 schema/手改坏 YAML）**只跳过它并记原因**，
    不拖垮启动；同一 recipe_id 已被载入时不覆盖（``--recipe`` 指定文件由调用方最后载入，
    从而优先）。``skip_paths`` 用于跳过调用方即将单独载入的文件（避免重复读）。
    """
    out: list[str] = []
    if not directory or not os.path.isdir(directory):
        return out
    skip = {os.path.abspath(p) for p in skip_paths if p}
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith((".yaml", ".yml")):
            continue
        path = os.path.abspath(os.path.join(directory, name))
        if path in skip or path.endswith(".bak"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = YAML(typ="safe").load(fh) or {}
            rid0 = str(data.get("recipe_id") or "") if isinstance(data, dict) else ""
            if rid0 and rid0 in recipes.drafts():
                continue        # 同一 recipe_id 先到先得（--recipe 随后载入 → 以它为准）
            rid = recipes.draft(data, source=path)
        except Exception as exc:  # noqa: BLE001 - 单个文件坏只跳过，不拖垮启动
            msg = f"执行方案目录载入跳过 {name}: {type(exc).__name__}: {exc}"
            (log or _log.warning)(msg)
            continue
        out.append(rid)
    return out


def preview_import(manifest: dict, base_dir: str) -> dict[str, Any]:
    """清单导入校验/差异预览; 不执行包内脚本 (02 §10)."""
    issues: list[str] = []
    files = manifest.get("files", [])
    for entry in files:
        rel = entry.get("path", "")
        if os.path.isabs(rel) or ".." in rel.split("/"):
            issues.append(f"非法路径: {rel!r}")
            continue
        full = os.path.join(base_dir, rel)
        if not os.path.exists(full):
            issues.append(f"清单文件缺失: {rel!r}")
    if manifest.get("schema_version") not in (1, "1"):
        issues.append(f"schema_version 不支持: {manifest.get('schema_version')!r}")
    return {
        "ok": not issues, "issues": issues,
        "planned_changes": [f.get("path") for f in files],
        "executed_scripts": [],  # 明确: 不执行包内脚本
    }
