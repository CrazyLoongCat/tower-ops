"""配方草稿编辑（E20「配方流程」页的引擎；**纯 Python，无 Qt**）。

页面只把「意图」翻译成本模块的调用；真正的 schema 权威仍是 pydantic 模型与五层校验器：
每次改动都把所有字段重新 ``model_validate`` 一遍再写回草稿，改不对就当场报错，不静默吞掉。

要点（DEC-024 / DEC-030）：

- **无工位**：配方只有一条有序 ``sequence``。运动步骤**自带** ``joint_target_ref``
  （引用已登记示教点），可选再绑一条命名「运动方案」。
- **相机需求在步骤里**：抓拍步骤的 ``camera_ref``；配方级不再有 ``required_cameras``。
- **自定义操作 custom.call**：选一个方法库里注册的**自定义方法** + 1..N 个设备，
  对多设备同时下发、等全部完成。安全边界由方法库声明（见 services/methods.py）。
- **发布闸**：离线示例草稿 ``is_offline_draft=True`` **不允许**真实发布；
  发布走 ``RecipeService.publish``（权限 + 审计），本模块不绕过它。发布即生效：
  草稿同步标记为已发布（作业台据此放行），**不询问、不记录操作人员**（DEC-040）。
"""
from __future__ import annotations

from io import StringIO
from typing import Any, Callable

from ruamel.yaml import YAML

from application.contracts.enums import GUIDANCE_CAMERA_ID, NodeType
from application.services.recipes.models import SCHEMA_VERSION, RecipeModel
from application.services.recipes.service import (
    PublishError,
    RecipeService,
    ValidationReport,
    atomic_write,
    validate_recipe,
)

# 步骤类型的纯中文名（作业台/配方页展示用；DEC-037）
STEP_TYPE_CN: dict[NodeType, str] = {
    NodeType.ROBOT_MOVE_JOINT: "机器人运动",
    NodeType.ROBOT_MOVE_BY_BOLT_SIZE: "按螺栓直径运动",
    NodeType.CAMERA_CAPTURE: "抓拍",
    NodeType.VISION_RUN: "视觉判定",
    NodeType.GUIDED_POSITION: "引导定位",
    NodeType.MECHANISM_ACTION: "机构动作",
    NodeType.CUSTOM_CALL: "自定义操作",
    NodeType.WAIT_STATE: "等待状态",
    NodeType.CONDITION: "条件分支",
    NodeType.OPERATOR_CONFIRM: "人工确认",
    NodeType.RECORD_EVIDENCE: "记录证据",
    NodeType.SUBFLOW: "子流程",
    NodeType.REPEAT: "重复",
    NodeType.PARALLEL_GROUP: "并行执行",
}

# 步骤类型在下拉里的显示名（顺序 = 常见编排顺序）
STEP_TYPE_LABELS: dict[NodeType, str] = {
    t: f"{cn} {t.value}" for t, cn in STEP_TYPE_CN.items()
}


def step_title(step) -> str:
    """步骤在界面上的显示名（DEC-037）：设了中文名用中文名，否则回退中文类型名。"""
    return getattr(step, "title", None) or STEP_TYPE_CN.get(step.type, step.type.value)

_NO_PIPELINE = "（未选）"


def _dump_yaml(data: dict) -> str:
    yml = YAML(typ="safe")
    yml.default_flow_style = False
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.sort_base_mapping_type_on_output = False
    buf = StringIO()
    yml.dump(data, buf)
    return buf.getvalue()


class RecipeEditor:
    """在 :class:`RecipeService` 的草稿上做增删改；不碰设备、不发运动。"""

    def __init__(self, service: RecipeService, *, recipe_id: str | None = None) -> None:
        self.service = service
        self.recipe_id = recipe_id

    # ---------------------------------------------------------------- 目录来源
    @property
    def catalog(self):  # noqa: ANN201 - ReferenceCatalog
        return self.service.catalog

    def motion_plans(self) -> list[str]:
        return sorted(getattr(self.catalog, "motion_plans", set()) or set())

    def fine_profiles(self) -> list[str]:
        """额外精定位参数套（「参数配置」页维护，DEC-065）。"""
        return sorted(getattr(self.catalog, "fine_offset_profiles", set()) or set())

    def detections(self) -> list[tuple[str, str]]:
        """(detection_id, pipeline_ref) 列表，来自「视觉调试」页另存的命名检测。"""
        return sorted((getattr(self.catalog, "detections", {}) or {}).items())

    def pipelines(self) -> list[str]:
        return sorted(getattr(self.catalog, "pipelines", set()) or set())

    def cameras(self) -> list[str]:
        return sorted(getattr(self.catalog, "cameras", set()) or set())

    def joint_targets(self) -> list[str]:
        return sorted(getattr(self.catalog, "joint_targets", {}) or {})

    def methods(self) -> list[str]:
        """自定义方法库里的方法 id 列表（供 custom.call 步骤选择）。"""
        reg = getattr(self.catalog, "methods", None)
        if reg is None:
            return []
        ids = getattr(reg, "ids", None)
        return sorted(list(ids() if callable(ids) else ids or []))

    def method_info(self, method_id: str):
        """取一个自定义方法的元数据（title/边界/参数）；没有则 None。"""
        reg = getattr(self.catalog, "methods", None)
        find = getattr(reg, "find", None) if reg is not None else None
        return find(method_id) if callable(find) else None

    def devices(self) -> list[str]:
        return sorted(getattr(self.catalog, "devices", set()) or set())

    def actions(self) -> list[str]:
        cat = getattr(self.catalog, "actions", None)
        ids = getattr(cat, "action_ids", None) or getattr(cat, "ids", None)
        return sorted(list(ids() if callable(ids) else ids or []))

    def subflows(self) -> list[str]:
        reg = getattr(self.catalog, "subflows", None)
        ids = getattr(reg, "template_ids", None) or getattr(reg, "ids", None)
        return sorted(list(ids() if callable(ids) else ids or []))

    # ---------------------------------------------------------------- 选择/新建
    def recipes(self) -> list[str]:
        return self.service.drafts()

    def select(self, recipe_id: str | None) -> None:
        self.recipe_id = recipe_id

    def exists(self, recipe_id: str) -> bool:
        return recipe_id in self.service.drafts()

    @property
    def model(self) -> RecipeModel:
        if not self.recipe_id:
            raise PublishError("未选择执行方案")
        return self.service.get_draft(self.recipe_id)

    def is_offline_draft(self) -> bool:
        return bool(self.recipe_id and self.model.is_offline_draft)

    def new_recipe(self, recipe_id: str, *, version: str = "v1",
                   is_offline_draft: bool = False) -> str:
        rid = (recipe_id or "").strip()
        if not rid:
            raise ValueError("执行方案 ID 不能为空")
        if self.exists(rid):
            raise ValueError(f"执行方案 {rid!r} 已存在（换个 ID 或先复制）")
        model = RecipeModel.model_validate({
            "schema_version": SCHEMA_VERSION, "recipe_id": rid, "version": version,
            "state": "draft", "sequence": [],
            "is_offline_draft": bool(is_offline_draft),
        })
        self.service.draft(model)
        self.recipe_id = rid
        return rid

    def duplicate(self, source_id: str, new_id: str) -> str:
        """复制一份**新草稿**。离线示例复制出来**仍是离线示例**（不让占位值混进真实发布）。"""
        rid = (new_id or "").strip()
        if not rid:
            raise ValueError("新执行方案 ID 不能为空")
        if self.exists(rid):
            raise ValueError(f"执行方案 {rid!r} 已存在")
        src = self.service.get_draft(source_id)
        data = src.model_dump(mode="json")
        data["recipe_id"] = rid
        data["state"] = "draft"
        model = RecipeModel.model_validate(data)
        self.service.draft(model)
        self.recipe_id = rid
        return rid

    def delete_recipe(self, recipe_id: str | None = None, *,
                      remove_file: bool = True) -> dict:
        """删除执行方案（默认当前选中的那条；DEC-108）。

        ``remove_file`` 为 True 时来源文件改名备份（见 ``RecipeService.delete``）。
        """
        rid = recipe_id or self.recipe_id
        if not rid:
            raise ValueError("未选择执行方案")
        info = self.service.delete(rid, remove_file=remove_file)
        if self.recipe_id == rid:
            self.recipe_id = None
        return info

    # ---------------------------------------------------------------- 配方级字段
    def set_version(self, version: str) -> None:
        self._mutate(lambda d: d.update(version=version.strip() or "v1"))

    def set_offline_draft(self, flag: bool) -> None:
        self._mutate(lambda d: d.update(is_offline_draft=bool(flag)))

    def set_allow_partial(self, flag: bool) -> None:
        self._mutate(lambda d: d.update(allow_partial=bool(flag)))

    # ---------------------------------------------------------------- 步骤
    def add_step(self, node_type: NodeType | str, step_id: str | None = None, *,
                 index: int | None = None) -> str:
        ntype = node_type if isinstance(node_type, NodeType) else NodeType(node_type)
        sid = (step_id or "").strip() or self._next_step_id(ntype)
        if any(s.id == sid for s in self.model.sequence):
            raise ValueError(f"步骤 ID {sid!r} 已存在")
        step = self._default_step(ntype, sid)

        def fn(d: dict) -> None:
            seq = d.setdefault("sequence", [])
            seq.insert(len(seq) if index is None else max(0, index), step)
        self._mutate(fn)
        return sid

    def remove_step(self, step_id: str) -> None:
        self._mutate(lambda d: d.update(
            sequence=[s for s in d.get("sequence", []) if s.get("id") != step_id]))

    def move_step(self, step_id: str, delta: int) -> None:
        self._move("sequence", "id", step_id, delta)

    def set_step_field(self, step_id: str, field_name: str, value: Any) -> None:
        def fn(d: dict) -> None:
            for s in d.get("sequence", []):
                if s.get("id") == step_id:
                    s[field_name] = value
        self._mutate(fn)

    def set_motion_plan(self, step_id: str, plan_id: str | None) -> None:
        """绑定/解绑命名运动方案（**按步骤绑定**）。"""
        pid = (plan_id or "").strip()
        self.set_step_field(step_id, "motion_plan_ref", pid or None)

    def set_step_joint_target(self, step_id: str, joint_target_ref: str | None) -> None:
        """设置运动步骤**自带的示教点**（引用已登记的关节目标键）。"""
        ref = (joint_target_ref or "").strip()
        self.set_step_field(step_id, "joint_target_ref", ref or None)

    def set_step_method(self, step_id: str, method_id: str | None) -> None:
        """选择 custom.call 步骤要执行的**自定义方法**。"""
        mid = (method_id or "").strip()
        self.set_step_field(step_id, "method_ref", mid or None)

    def set_step_devices(self, step_id: str, device_refs: list[str]) -> None:
        """设置 custom.call 步骤要操作的**设备集合**（可多个；执行时同时下发、等全部完成）。"""
        refs = [d.strip() for d in device_refs if d and d.strip()]
        self.set_step_field(step_id, "device_refs", refs)

    def set_step_title(self, step_id: str, title: str | None) -> None:
        """设置步骤的**显示名称**（DEC-037，可留空；只影响界面展示，不参与引用/执行）。"""
        text = (title or "").strip()
        self.set_step_field(step_id, "title", text or None)

    def set_step_checks(self, step_id: str, checks: list[dict]) -> None:
        """设置步骤完成后的**判断属性**（DEC-064；空列表 = 不判断）。"""
        self.set_step_field(step_id, "post_checks", list(checks or []))

    def set_parallel_branch_field(self, step_id: str, branch_type: NodeType | str,
                                  field_name: str, value: Any) -> None:
        """设置**并行执行步骤组**某个分支的字段（DEC-096）。

        ``branch_type`` 用分支类型定位（首版只有 robot.move_joint / mechanism.action
        各一个分支），避免再暴露一套分支编辑引擎；分支不存在时静默不动作
        （界面只在对应分支存在时才给出控件）。
        """
        target = branch_type if isinstance(branch_type, NodeType) else NodeType(branch_type)

        def fn(d: dict) -> None:
            for s in d.get("sequence", []):
                if s.get("id") != step_id:
                    continue
                for b in s.get("branches", []) or []:
                    if b.get("type") == target.value:
                        b[field_name] = value
        self._mutate(fn)

    def set_detection(self, step_id: str, detection_id: str | None) -> None:
        """引用命名视觉检测；**同时清掉 pipeline_ref**（二选一）。"""
        det = (detection_id or "").strip()
        self.set_step_field(step_id, "detection_ref", det or None)
        self.set_step_field(step_id, "pipeline_ref", None)

    def set_pipeline(self, step_id: str, pipeline_id: str | None) -> None:
        """直接引流水线；**同时清掉 detection_ref**（二选一）。"""
        pid = (pipeline_id or "").strip()
        self.set_step_field(step_id, "pipeline_ref", pid or None)
        self.set_step_field(step_id, "detection_ref", None)

    # ---------------------------------------------------------------- 校验/发布
    def validate(self) -> ValidationReport:
        return validate_recipe(self.model, self.catalog)

    def can_publish(self) -> tuple[bool, str]:
        """能否发布：先看离线示例闸，再看五层校验。返回 (可否, 说明)。"""
        if not self.recipe_id or not self.exists(self.recipe_id):
            return False, "未选择执行方案"
        if self.is_offline_draft():
            return False, ("离线示例草稿不得真实发布；如内容是认真值，"
                           "请取消「离线示例」并重新校验")
        report = self.validate()
        if not report.ok:
            return False, "校验未通过：" + "；".join(report.errors[:3])
        return True, "校验通过，可发布"

    def publish(self, *, principal=None,
                allow_offline_draft: bool = False) -> str:
        """发布（发布即生效；不询问、不记录操作人员，DEC-040）。"""
        if not self.recipe_id:
            raise PublishError("未选择执行方案")
        return self.service.publish(self.recipe_id,
                                    allow_offline_draft=allow_offline_draft,
                                    principal=principal)

    def history(self) -> list[str]:
        if not self.recipe_id:
            return []
        return [rev for rev, _h in self.service.history(self.recipe_id)]

    def compare(self, revision_a: str, revision_b: str) -> dict:
        if not self.recipe_id:
            raise PublishError("未选择执行方案")
        return self.service.compare(self.recipe_id, revision_a, revision_b)

    # ---------------------------------------------------------------- 落盘
    def to_yaml(self) -> str:
        header = ("# 执行方案草稿（E20「执行方案」页导出）。由程序按 schema 生成。\n"
                  "# is_offline_draft=true 的草稿是离线示例，不得用于真实生产发布。\n")
        return header + _dump_yaml(self.model.model_dump(mode="json"))

    def save(self, path: str) -> None:
        """原子写入文件，并把该文件记为草稿来源（DEC-035：「保存」直接覆盖它）。"""
        atomic_write(path, self.to_yaml())
        if self.recipe_id and self.exists(self.recipe_id):
            self.service.set_source(self.recipe_id, path)

    def source_path(self) -> str | None:
        """当前配方对应的文件（新建/未落盘过的草稿 = None）。"""
        if not self.recipe_id:
            return None
        return self.service.source_of(self.recipe_id)

    # ---------------------------------------------------------------- 内部
    def _mutate(self, fn: Callable[[dict], Any]) -> None:
        """取当前草稿的纯 dict -> 改 -> 重新 pydantic 校验 -> 写回草稿。"""
        data = self.model.model_dump(mode="json")
        out = fn(data)
        if isinstance(out, dict):
            data = out
        self.service.draft(RecipeModel.model_validate(data))

    def _move(self, coll: str, key: str, ident: str, delta: int) -> None:
        """在同一集合里把某项与相邻项交换（越界即不动，不报错）。"""
        def fn(d: dict) -> None:
            seq = d.get(coll, [])
            i = next((k for k, it in enumerate(seq) if it.get(key) == ident), None)
            if i is None:
                return
            j = i + delta
            if not (0 <= j < len(seq)):
                return
            seq[i], seq[j] = seq[j], seq[i]
        self._mutate(fn)

    def _next_step_id(self, ntype: NodeType) -> str:
        base = {"robot.move_joint": "move", "camera.capture": "capture",
                "vision.run": "detect", "vision.guided_position": "guide",
                "mechanism.action": "action",
                "custom.call": "custom",
                "wait.state": "wait", "condition": "gate",
                "operator.confirm": "confirm", "record.evidence": "evidence",
                "subflow": "subflow", "repeat": "repeat",
                "parallel.group": "parallel"}.get(ntype.value, "step")
        existing = {s.id for s in self.model.sequence}
        i = 1
        while f"{base}_{i}" in existing:
            i += 1
        return f"{base}_{i}"

    def _prev_step_id(self) -> str | None:
        seq = self.model.sequence
        return seq[-1].id if seq else None

    def _default_step(self, ntype: NodeType, sid: str) -> dict:
        """按类型给一份**可编译的起点**（引用的东西从目录里取，取不到就留空由校验报出）。"""
        prev = self._prev_step_id()
        step: dict[str, Any] = {"id": sid, "type": ntype.value}
        if ntype is NodeType.ROBOT_MOVE_JOINT:
            jts = self.joint_targets()
            if jts:
                step["joint_target_ref"] = jts[0]
        elif ntype is NodeType.CUSTOM_CALL:
            ms = self.methods()
            if ms:
                step["method_ref"] = ms[0]
        elif ntype is NodeType.CAMERA_CAPTURE:
            cams = self.cameras()
            if cams:
                step["camera_ref"] = cams[0]
        elif ntype is NodeType.VISION_RUN:
            if prev:
                step["input_from"] = {"step": prev, "output": "frame"}
            pipes = self.pipelines()
            if pipes:
                step["pipeline_ref"] = pipes[0]
        elif ntype is NodeType.GUIDED_POSITION:
            # DEC-137：引导定位不再只认 DS86。默认优先 DS86（现场既有配法）；
            # DS86 未启用时退到目录里第一台已启用相机（如 D405），不写必然校验失败的默认值。
            cams = self.cameras()
            step["camera_ref"] = (GUIDANCE_CAMERA_ID if GUIDANCE_CAMERA_ID in cams
                                  else (cams[0] if cams else GUIDANCE_CAMERA_ID))
            step["guide_stages"] = ["coarse", "fine"]
            step["guide_max_offset_mm"] = 0.0
        elif ntype is NodeType.CONDITION:
            # 起点条件：引用上一步的 status；没有上一步就留空，由校验定位出来让用户改
            step["predicate"] = {"source": {"step": prev or "", "field": "status"},
                                 "op": "eq", "value": "PASS"}
            step["on_false"] = {"policy": "pause_for_review"}
        elif ntype is NodeType.MECHANISM_ACTION:
            acts = self.actions()
            if acts:
                step["action_ref"] = acts[0]
        elif ntype is NodeType.WAIT_STATE:
            devs = self.devices()
            if devs:
                step["device_ref"] = devs[0]
            step.update(field_name="state", expected="READY",
                        stable_ticks=3, deadline_ms=5000)
        elif ntype is NodeType.RECORD_EVIDENCE:
            step["inputs"] = [prev] if prev else []
        elif ntype is NodeType.SUBFLOW:
            subs = self.subflows()
            if subs:
                step["subflow_ref"] = subs[0]
        elif ntype is NodeType.REPEAT:
            subs = self.subflows()
            if subs:
                step["subflow_ref"] = subs[0]
            step["max_iterations"] = 3
        elif ntype is NodeType.PARALLEL_GROUP:
            # DEC-096：默认生成两个分支 = 一个机器人运动 + 一个机构动作。
            # 引用从目录里取第一项；取不到就留空，由发布校验明确报出。
            taken = {s.id for s in self.model.sequence}
            taken |= {b.id for s in self.model.sequence for b in s.branches}
            robot_id = self._unique_branch_id(sid, "robot", taken)
            mech_id = self._unique_branch_id(sid, "mech", taken | {robot_id})
            robot: dict[str, Any] = {"id": robot_id,
                                     "type": NodeType.ROBOT_MOVE_JOINT.value}
            jts = self.joint_targets()
            if jts:
                robot["joint_target_ref"] = jts[0]
            mech: dict[str, Any] = {"id": mech_id,
                                    "type": NodeType.MECHANISM_ACTION.value}
            acts = self.actions()
            if acts:
                mech["action_ref"] = acts[0]
            step["branches"] = [robot, mech]
        return step

    @staticmethod
    def _unique_branch_id(parent_id: str, suffix: str, taken: set[str]) -> str:
        """并行分支 ID：``<父步骤>_<robot|mech>``，已被占用时追加序号。"""
        base = f"{parent_id}_{suffix}"
        candidate, i = base, 2
        while candidate in taken:
            candidate = f"{base}_{i}"
            i += 1
        return candidate


__all__ = ["STEP_TYPE_CN", "STEP_TYPE_LABELS", "RecipeEditor", "step_title"]
