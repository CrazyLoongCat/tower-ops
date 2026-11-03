"""配置/配方数据模型 (12 §4.4, 02 §9.3/§10).

pydantic 校验的 YAML 数据; 固定 node type -> 已注册工厂, 不提供任意 Python/动态导入.
condition 用 eq/ne/gt/ge/lt/le/in 及有界 all/any 的结构化比较, 无字符串求值器.

**schema_version=2（2026-10-16 起）**：取消「工位」概念——配方只有**一条**有序步骤表
(``sequence``)，步骤串行执行。运动步骤自带 ``joint_target_ref``（引用已登记示教点），
相机需求在**步骤属性**里体现（抓拍步骤的 ``camera_ref`` / 自定义操作的 ``device_refs``），
不再有配方级 ``required_cameras``。旧版（工位版，schema_version=1）配方**不再兼容**，加载即拒绝。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from application.contracts.enums import ConditionOp, FailurePolicy, NodeType

# 本版本只认这一个 schema（旧工位版配方不兼容，加载即拒）。
SCHEMA_VERSION = 2


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    step: str
    field: str


class ConditionModel(BaseModel):
    """结构化条件: 单比较 或 有界 all/any."""

    model_config = ConfigDict(extra="forbid")

    source: SourceRef | None = None
    op: ConditionOp | None = None
    value: Any = None
    all: tuple["ConditionModel", ...] = ()
    any: tuple["ConditionModel", ...] = ()
    max_depth: int = 8  # 有界: 由校验器强制

    @model_validator(mode="after")
    def _check_shape(self) -> "ConditionModel":
        has_compare = self.source is not None or self.op is not None
        if self.all and self.any:
            raise ValueError("all 与 any 不能同时使用")
        if self.all or self.any:
            if has_compare:
                raise ValueError("组合条件不能同时带单比较")
            return self
        if self.source is None or self.op is None:
            raise ValueError("条件必须包含 source+op, 或 all/any")
        if self.op in (ConditionOp.ALL, ConditionOp.ANY):
            raise ValueError("all/any 应通过 all/any 字段表达")
        return self

    def depth(self) -> int:
        if not (self.all or self.any):
            return 1
        children = self.all or self.any
        return 1 + max((c.depth() for c in children), default=0)


class StepCheckModel(BaseModel):
    """步骤完成后的**可选判断属性**（DEC-064）。

    三类：
    - ``robot_pose``：当前末端位姿与**示教点到达时的实测位姿**比较（pos_tol_mm /
      可选 rot_tol_deg）。期望位姿来自本次运行中运动到该示教点完成时记录的位姿，
      不在配方里写死数值。
    - ``device_field``：设备遥测字段比较（``device_ref`` + ``field_name`` + op/value）。
    - ``step_output``：前序步骤输出比较（``source_step`` + ``field`` + op/value）。

    全部判断通过才进入下一步骤；不通过由 TaskService 停在安全边界（PAUSED）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["robot_pose", "device_field", "step_output"]
    # robot_pose
    joint_target_ref: str | None = None
    pos_tol_mm: float = 2.0
    check_rotation: bool = False
    rot_tol_deg: float = 2.0
    # device_field / step_output
    device_ref: str | None = None
    source_step: str | None = None
    field_name: str | None = None
    op: ConditionOp | None = None
    value: Any = None

    @model_validator(mode="after")
    def _check_shape(self) -> "StepCheckModel":
        """编辑器允许先加一行再填内容；**引用/字段完整性由发布校验报错**。"""
        if self.kind == "robot_pose":
            if float(self.pos_tol_mm) < 0 or float(self.rot_tol_deg) < 0:
                raise ValueError("位姿容差不能为负")
            return self
        if self.op in (ConditionOp.ALL, ConditionOp.ANY):
            raise ValueError(f"{self.kind} 判断不能使用 all/any 算子")
        return self


class StepModel(BaseModel):
    """配方步骤. 类型特定字段可选, 由校验器按 type 检查必需品."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: NodeType
    # 显示名称（DEC-037）: 中文名只用于界面展示（配方页列表/作业台执行步骤），
    # 不参与引用与执行；留空 = 界面按中文类型名显示（如「机器人运动」）。
    title: str | None = None
    # robot.move_joint: 引用一个**已登记示教点**(关节目标) 或 绑定一条命名「运动方案」。
    # 二者可并存: 绑定方案时按方案回放路点, 未绑定则移动到本步自带的示教点。
    joint_target_ref: str | None = None
    motion_plan_ref: str | None = None
    # camera.capture
    camera_ref: str | None = None
    # vision.run
    input_from: dict[str, str] | None = None
    # vision.run: 二选一 —— 直接引流水线, 或引用一条命名「视觉检测」
    # (来自「视觉调试」页「另存为检测」, 内含通道 + 流水线 + 参数快照)。
    pipeline_ref: str | None = None
    detection_ref: str | None = None
    # vision.guided_position（引导定位, DEC-033）: 相机复用上面的 camera_ref。
    # 段选择 = 引导定位的子集（默认粗→精两段，按 (coarse, fine) 顺序执行）；
    # 速度 None = 跟随引导配置 guide_move_speed（默认 10%）；
    # 偏移上限 mm, 0 = 不设上限（与手动「引导定位」按钮行为一致）。
    guide_stages: tuple[str, ...] = ("coarse", "fine")
    guide_speed_pct: int | None = None
    guide_max_offset_mm: float = 0.0
    # DEC-065: 选用哪套额外精定位偏移参数（空=默认 offsets.fine/tcp）
    fine_profile_ref: str | None = None
    # condition
    predicate: ConditionModel | None = None
    on_false: dict[str, Any] | None = None
    on_true: dict[str, Any] | None = None
    # 步骤完成后的可选判断属性（DEC-064）：全部通过才进入下一步骤。
    post_checks: tuple[StepCheckModel, ...] = ()
    # 并行执行步骤组（DEC-096，type=parallel.group）：分支步骤同时执行。
    # 首版只允许**恰好两个**分支，且一个是 robot.move_joint、一个是 mechanism.action；
    # 分支不能是并行/子流程/重复（不支持嵌套）。分支字段与普通步骤同构，
    # 由编辑器/五层校验保证组合合法；此处只承载数据，不在此处做分支数量校验
    # （草稿编辑过程中允许暂时不完整，发布前由 validate_recipe 拒绝）。
    branches: tuple["StepModel", ...] = ()
    # mechanism.action
    action_ref: str | None = None
    # custom.call（自定义操作）: 选一个**已注册的自定义方法**（安全边界由方法库声明），
    # 可对**多个设备** device_refs 同时下发指令、等全部完成后本步才算完成。
    method_ref: str | None = None
    device_refs: tuple[str, ...] = ()
    params: dict[str, Any] = Field(default_factory=dict)
    # wait.state / record.evidence / subflow
    inputs: tuple[str, ...] = ()
    subflow_ref: str | None = None
    device_ref: str | None = None
    max_iterations: int | None = None
    # wait.state 细节 (E13): 持续窗与超时
    field_name: str | None = None
    expected: Any = None
    stable_ticks: int = 1
    deadline_ms: int | None = None
    # operator.confirm (E13): 允许选项
    operator_options: tuple[str, ...] = ("继续", "终止")
    # subflow 允许的 override 参数 (不得含硬限值/互锁/寻址)
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("step.id 不能为空")
        return v.strip()

    @field_validator("title")
    @classmethod
    def _title_optional(cls, v: str | None) -> str | None:
        """显示名称去首尾空白；空串按「未设」处理（界面回退中文类型名）。"""
        text = (v or "").strip()
        return text or None

    def failure_policy(self) -> FailurePolicy | None:
        if self.on_false is None:
            return None
        policy = self.on_false.get("policy")
        return FailurePolicy(policy) if policy else None


class RecipeModel(BaseModel):
    """配方 = 一条有序步骤表（无工位）。步骤串行执行。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    recipe_id: str
    version: str
    state: str = "draft"
    sequence: tuple[StepModel, ...] = ()
    failure_policy: dict[str, Any] | None = None
    allow_partial: bool = False
    # 配方可覆盖的普通参数 (白名单路径 -> 值)
    overrides: dict[str, Any] = Field(default_factory=dict)
    is_offline_draft: bool = False  # 离线示例必须为 True, 不得用于真实生产发布

    @field_validator("schema_version")
    @classmethod
    def _check_schema(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"不支持的执行方案 schema_version={v}：本版本只认 {SCHEMA_VERSION}。"
                f"旧「工位版」执行方案（schema_version=1）不再兼容，请按新结构重建。")
        return v

    @model_validator(mode="after")
    def _unique_ids(self) -> "RecipeModel":
        # 顶层步骤与并行分支同为账本/节点键，ID 必须整体唯一（DEC-096）。
        ids = [s.id for s in self.sequence]
        ids += [b.id for s in self.sequence for b in s.branches]
        if len(ids) != len(set(ids)):
            raise ValueError("sequence/branches 中 step.id 必须唯一")
        return self


StepModel.model_rebuild()
ConditionModel.model_rebuild()

__all__ = ["ConditionModel", "RecipeModel", "SCHEMA_VERSION", "SourceRef", "StepModel"]
