"""子流程模板 (E13, 02 §9.1/§9.3)。

- 模板带**版本**; 引用时必须存在, 否则发布前拒绝;
- 展开为带**独立作用域**的步骤序列 (账本与输出不跨实例/迭代串用);
- override 仅限模板声明的白名单键, 其余拒绝 (硬限值/互锁/寻址不可覆盖)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import py_trees

from application.contracts.context import RunContext
from application.domain.errors import ValidationFailure
from application.services.recipes.models import StepModel
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.step_gate import make_step_gate
from application.workflow.nodes.steps import build_node


class UnknownSubflow(ValidationFailure):
    """引用了未注册的子流程模板."""


class OverrideNotAllowed(ValidationFailure):
    """override 键不在模板白名单内."""


@dataclass(frozen=True)
class SubflowTemplate:
    """可复用的步骤模板. version 参与作用域与校验, 发布后不可变。"""

    template_id: str
    version: str
    steps: tuple[StepModel, ...]
    allow_overrides: tuple[str, ...] = ()
    description: str = ""

    def scope_key(self) -> str:
        return f"{self.template_id}@{self.version}"


class SubflowRegistry:
    """模板注册表. 未注册即拒绝 (发布前校验)."""

    def __init__(self, templates: list[SubflowTemplate] | None = None) -> None:
        self._items: dict[str, SubflowTemplate] = {}
        for t in (templates or []):
            self.register(t)

    def register(self, template: SubflowTemplate) -> None:
        if template.template_id in self._items:
            raise ValidationFailure(f"子流程模板 {template.template_id!r} 已存在")
        self._items[template.template_id] = template

    def get(self, template_id: str) -> SubflowTemplate:
        t = self._items.get(template_id)
        if t is None:
            raise UnknownSubflow(f"未注册子流程模板: {template_id!r}")
        return t

    def known(self, template_id: str) -> bool:
        return template_id in self._items

    def ids(self) -> list[str]:
        return sorted(self._items)


def check_overrides(template: SubflowTemplate, overrides: dict[str, Any]) -> None:
    """override 白名单校验: 未声明键一律拒绝。"""
    extra = set(overrides) - set(template.allow_overrides)
    if extra:
        raise OverrideNotAllowed(
            f"模板 {template.template_id!r} 不允许覆盖: {sorted(extra)}; "
            f"允许: {list(template.allow_overrides)}")


def expand_subflow(template: SubflowTemplate, *, ctx: RunContext, executor: Any,
                   outputs: OutputStore, joint_targets: dict | None = None,
                   overrides: dict[str, Any] | None = None,
                   motion: Any = None, detections: dict[str, str] | None = None,
                   methods: Any = None, step_confirm: bool = False,
                   gates: list | None = None) -> list[Any]:
    """把模板展开为带独立作用域的步骤节点列表 (供 Sequence/有界循环使用)。

    ``step_confirm`` 为真时，每个步骤后追加一个确认闸（DEC-042）；循环每轮重建
    子节点时也会重建闸，闸列表随之增长（旧闸已终止，不会再产生提示）。
    """
    check_overrides(template, overrides or {})
    sub_ctx = ctx.for_subflow(template.scope_key())
    nodes = []
    total = len(template.steps)
    for i, step in enumerate(template.steps):
        nodes.append(build_node(step, ctx=sub_ctx, executor=executor,
                                outputs=outputs, joint_targets=joint_targets or {},
                                motion=motion, detections=detections,
                                methods=methods))
        if step_confirm:
            nodes.append(make_step_gate(
                step, ctx=sub_ctx, executor=executor, outputs=outputs,
                index=i + 1, total=total, gates=gates))
    return nodes


def expand_subflow_sequence(template: SubflowTemplate, *, name: str, ctx: RunContext,
                            executor: Any, outputs: OutputStore,
                            joint_targets: dict | None = None,
                            overrides: dict[str, Any] | None = None,
                            motion: Any = None, detections: dict[str, str] | None = None,
                            methods: Any = None, step_confirm: bool = False,
                            gates: list | None = None):
    """展开为 memory Sequence (子流程完成才继续)."""
    children = expand_subflow(template, ctx=ctx, executor=executor, outputs=outputs,
                              joint_targets=joint_targets, overrides=overrides,
                              motion=motion, detections=detections, methods=methods,
                              step_confirm=step_confirm, gates=gates)
    return py_trees.composites.Sequence(name=name, memory=True, children=children)


__all__ = [
    "OverrideNotAllowed", "SubflowRegistry", "SubflowTemplate", "UnknownSubflow",
    "check_overrides", "expand_subflow", "expand_subflow_sequence",
]
