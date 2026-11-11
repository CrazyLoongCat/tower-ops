"""配方编译为 py_trees 行为树 (A16/E13, 12 §4.4, 02 §9)。

**无工位（schema_version=2）**：整份配方 = 一条有序步骤表，编译成**一个** memory Sequence，
步骤**串行**执行（一个完了下一个）。运动步骤自带示教点（``joint_target_ref``），
由编译器按步解析为具体关节目标；自定义操作（``custom.call``）由叶子节点处理（内部可对
多台设备同时下发、等全部完成）。

- 固定 node type -> 已注册工厂；
- **subflow** 展开为带独立作用域的子序列；**repeat** 用 BoundedLoop 有界迭代（无限循环在发布前即被拒绝）；
- **parallel.group**（DEC-096）编译为 Parallel（SuccessOnAll）：一个机器人运动分支与一个
  机构动作分支同时执行，两个都成功本步才完成；
- 全部步骤成功 -> 本次作业 COMPLETED；任一步 FAILURE -> 整次失败（除非该步策略是暂停复核）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import py_trees

from application.contracts.context import RunContext
from application.contracts.enums import NodeType
from application.domain.errors import ValidationFailure
from application.services.recipes.models import RecipeModel
from application.workflow.bounded_loop import BoundedLoop
from application.workflow.nodes.base import BaseStepNode, OutputStore
from application.workflow.nodes.parallel_group import ParallelGroupNode
from application.workflow.nodes.step_gate import make_step_gate
from application.workflow.nodes.steps import build_node, make_post_check_node
from application.workflow.subflows import (
    SubflowRegistry,
    expand_subflow,
    expand_subflow_sequence,
)

# 无工位后，整份配方共用一个「运行作用域」id（沿用 station_instance_id 字段作为
# 账本/输出/记录/日志的通用作用域键，值为单一 run 作用域）。
_RUN_SCOPE = "run"


@dataclass
class CompiledRun:
    tree: py_trees.trees.BehaviourTree
    outputs: OutputStore
    nodes: dict[str, Any] = field(default_factory=dict)
    station_sequences: dict[str, Any] = field(default_factory=dict)
    loops: dict[str, BoundedLoop] = field(default_factory=dict)
    # 步骤完成确认闸 (DEC-042); 只在 step_confirm=True 的编译里非空
    gates: list[Any] = field(default_factory=list)
    # 步骤后判断节点 (DEC-064)；不进入 nodes（不是配方步骤），供 TaskService 取暂停原因
    checks: list[Any] = field(default_factory=list)

    def find_failure(self) -> Any | None:
        from py_trees.common import Status
        # 优先返回叶子步骤节点 (带 failure_reason/policy)
        for node in self.nodes.values():
            if node.status is Status.FAILURE and hasattr(node, "failure_reason"):
                return node
        for node in self.nodes.values():
            if node.status is Status.FAILURE:
                return node
        return None

    def find_step(self, step_id: str) -> Any | None:
        """按 step_id 找节点. 仅用于唯一步骤; 重名请用 find_steps."""
        found = self.find_steps(step_id)
        return found[0] if found else None

    def find_steps(self, step_id: str) -> list[Any]:
        """按 step_id 找全部匹配的节点 (子流程/循环展开后可能有多个)."""
        return [node for name, node in self.nodes.items()
                if name.endswith(f"/{step_id}")]


def _run_scope_id(run_id: str) -> str:
    """整份配方的单一运行作用域 id（无工位）。"""
    return f"{run_id}:{_RUN_SCOPE}:0"


def _build_step(step, *, ctx: RunContext, executor: Any, outputs: OutputStore,
                joint_targets: dict, subflows: SubflowRegistry | None,
                motion: Any = None, detections: dict[str, str] | None = None,
                methods: Any = None, step_confirm: bool = False,
                gates: list | None = None, vision_step_ref: str | None = None):
    """按 node type 构造节点; subflow/repeat 展开为复合结构。"""
    if step.type is NodeType.SUBFLOW:
        template = _require_template(step.subflow_ref, subflows)
        return expand_subflow_sequence(template, name=f"subflow:{step.id}",
                                       ctx=ctx, executor=executor, outputs=outputs,
                                       joint_targets=joint_targets,
                                       motion=motion, detections=detections,
                                       methods=methods,
                                       step_confirm=step_confirm, gates=gates)
    if step.type is NodeType.REPEAT:
        template = _require_template(step.subflow_ref, subflows)
        if not step.max_iterations or step.max_iterations <= 0:
            raise ValidationFailure(f"步骤 {step.id}: repeat 必须有界")
        return BoundedLoop(
            f"repeat:{step.id}",
            factory=lambda ictx, t=template: expand_subflow(
                t, ctx=ictx, executor=executor, outputs=outputs,
                joint_targets=joint_targets, motion=motion, detections=detections,
                methods=methods, step_confirm=step_confirm, gates=gates),
            max_iterations=step.max_iterations, ctx=ctx)
    if step.type is NodeType.PARALLEL_GROUP:
        # DEC-096：首版只允许一个机器人运动分支 + 一个机构动作分支；
        # 分支在**同一 tick** 内各自提交，整组 SuccessOnAll 才算本步完成。
        branches = tuple(step.branches)
        robots = [b for b in branches if b.type is NodeType.ROBOT_MOVE_JOINT]
        mechs = [b for b in branches if b.type is NodeType.MECHANISM_ACTION]
        if len(branches) != 2 or len(robots) != 1 or len(mechs) != 1:
            raise ValidationFailure(
                f"并行执行步骤 {step.id}: 必须恰好一个 robot.move_joint 分支"
                f"与一个 mechanism.action 分支（当前 "
                f"{[b.type.value for b in branches]}）")
        children = [
            _build_step(b, ctx=ctx, executor=executor, outputs=outputs,
                        joint_targets=joint_targets, subflows=subflows,
                        motion=motion, detections=detections, methods=methods,
                        step_confirm=False, gates=None)
            for b in branches
        ]
        return ParallelGroupNode(
            f"parallel:{step.id}", step_id=step.id,
            title=(getattr(step, "title", None) or "并行执行"),
            executor=executor, children=children, run_id=ctx.run_id,
            motion=motion)
    return build_node(step, ctx=ctx, executor=executor, outputs=outputs,
                      joint_targets=joint_targets, motion=motion,
                      detections=detections, methods=methods,
                      vision_step_ref=vision_step_ref)


def _require_template(ref: str | None, subflows: SubflowRegistry | None):
    if subflows is None:
        raise ValidationFailure("执行方案引用了子流程, 但未提供模板注册表")
    return subflows.get(ref or "")


def compile_recipe(recipe: RecipeModel, run_ctx: RunContext, executor,
                   joint_targets: dict, outputs: OutputStore | None = None,
                   subflows: SubflowRegistry | None = None,
                   motion: Any = None,
                   detections: dict[str, str] | None = None,
                   methods: Any = None,
                   step_confirm: bool = False) -> CompiledRun:
    """把配方编译为一条串行执行的行为树. 未知 node type/缺示教点/未知模板即拒绝。

    ``joint_targets``: 已登记示教点 (关节目标键 -> JointTarget)，运动步骤按步解析；
    ``motion``: 运动方案执行器 (绑定 ``motion_plan_ref`` 的 move 步骤走它);
    ``detections``: 命名视觉检测 ``detection_id -> pipeline_ref`` (解析 ``detection_ref``);
    ``methods``: 自定义方法库 (CustomMethodRegistry)，``custom.call`` 步骤据此解析。
    ``step_confirm``: 为真时在**每个步骤之后**插入人工确认闸（DEC-042，作业台开启；
    默认关，保持既有离线/测试行为不变）。
    """
    outputs = outputs or OutputStore()
    nodes: dict[str, Any] = {}
    loops: dict[str, BoundedLoop] = {}
    gates: list[Any] = []
    checks: list[Any] = []
    scope = _run_scope_id(run_ctx.run_id)
    ctx = run_ctx.child_for_station(scope, 0)

    step_nodes = []
    total = len(recipe.sequence)
    # DEC-048：引导定位沿用**前面最近的视觉判定步骤**所抓的那一帧与人工确认的圆。
    # 中间若有抓拍/运动/机构/自定义动作（场景或位姿已变），旧帧不再可信，不沿用。
    last_vision: str | None = None
    scene_dirty = True
    for i, step in enumerate(recipe.sequence):
        vision_ref = None
        if (step.type is NodeType.GUIDED_POSITION and last_vision
                and not scene_dirty):
            vision_ref = last_vision
        node = _build_step(step, ctx=ctx, executor=executor, outputs=outputs,
                           joint_targets=joint_targets, subflows=subflows,
                           motion=motion, detections=detections, methods=methods,
                           step_confirm=step_confirm, gates=gates,
                           vision_step_ref=vision_ref)
        if step.type is NodeType.VISION_RUN:
            last_vision, scene_dirty = step.id, False
        elif step.type in (NodeType.CAMERA_CAPTURE,
                           NodeType.ROBOT_MOVE_JOINT,
                           NodeType.ROBOT_MOVE_BY_BOLT_SIZE,
                           NodeType.MECHANISM_ACTION, NodeType.CUSTOM_CALL,
                           NodeType.SUBFLOW, NodeType.REPEAT,
                           NodeType.PARALLEL_GROUP,
                           NodeType.GUIDED_POSITION):
            scene_dirty = True
        nodes[f"{scope}/{step.id}"] = node
        if isinstance(node, BoundedLoop):
            loops[f"{scope}/{step.id}"] = node
        step_nodes.append(node)
        # DEC-064：步骤完成后先跑判断属性，全部通过才进入后续（含人工确认闸）。
        check_node = make_post_check_node(step, ctx=ctx, executor=executor,
                                          outputs=outputs)
        if check_node is not None:
            checks.append(check_node)
            step_nodes.append(check_node)
        if step_confirm:
            step_nodes.append(make_step_gate(
                step, ctx=ctx, executor=executor, outputs=outputs,
                index=i + 1, total=total, gates=gates))

    seq = py_trees.composites.Sequence(name=f"run:{run_ctx.run_id}", memory=True,
                                       children=step_nodes)
    tree = py_trees.trees.BehaviourTree(seq)
    # station_sequences 保留为「作用域 key -> 序列」，无工位时只有单一 run 作用域。
    return CompiledRun(tree=tree, outputs=outputs, nodes=nodes,
                       station_sequences={_RUN_SCOPE: seq}, loops=loops,
                       gates=gates, checks=checks)


__all__ = ["CompiledRun", "compile_recipe"]
