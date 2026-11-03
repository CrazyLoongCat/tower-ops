"""有界循环 (E13, 12 §4.4, 02 §9.1)。

规则:
- **禁止无限循环**: max_iterations 必须是正整数, 缺省即拒绝;
- 每次迭代用**独立上下文**重建子节点, 保证 run/station/iteration/step/attempt 账本互不串用;
- 子流程失败**不落入替代运动分支**, 直接 FAILURE 交上层策略;
- 暂停/重入/取消复用原账本 (节点自身查账本), 不重发非幂等动作。
"""
from __future__ import annotations

from typing import Any, Callable

import py_trees
from py_trees.common import Status

from application.contracts.context import RunContext
from application.domain.errors import ValidationFailure


class UnboundedLoop(ValidationFailure):
    """请求了无界循环 (非法)."""


class BoundedLoop(py_trees.behaviour.Behaviour):
    """有界迭代一个子步骤序列. 每轮迭代重建子节点并使用迭代作用域上下文。"""

    def __init__(self, name: str, *, factory: Callable[[RunContext], list[Any]],
                 max_iterations: int, ctx: RunContext) -> None:
        super().__init__(name=name)
        if not isinstance(max_iterations, int) or max_iterations <= 0:
            raise UnboundedLoop(
                f"有界循环需要正整数 max_iterations, 得到 {max_iterations!r}")
        self._factory = factory
        self._max = max_iterations
        self._base_ctx = ctx
        self._iter = 0
        self._child: py_trees.behaviour.Behaviour | None = None
        self.iterations_completed = 0
        self.rebuilds = 0

    # ---- 生命周期 ----
    def setup(self, **kwargs: Any) -> None:
        pass  # 子节点在每轮迭代时按需构建; setup 不运动

    def initialise(self) -> None:
        self._iter = 0
        self._child = None
        self.iterations_completed = 0

    def update(self) -> Status:
        # 注意: 最后一轮迭代可能**跨多个 tick**(确认闸/等待类步骤), 不能因为
        # _iter 已达到上限就放弃 tick 在途子节点——否则树永远 RUNNING。
        if self._child is None:
            if self._iter >= self._max:
                return Status.SUCCESS
            return self._begin_iteration()
        self._child.tick_once()
        return self._after_tick(self._child.status)

    def terminate(self, new_status: Status) -> None:
        # INVALID/FAILURE: 交给子节点处理取消; 不宣称硬件已停
        if self._child is not None:
            self._child.stop(new_status)

    # ---- 内部 ----
    def _begin_iteration(self) -> Status:
        self._iter += 1
        iter_ctx = self._base_ctx.for_iteration(self._iter)
        children = self._factory(iter_ctx)
        if not children:
            raise ValidationFailure(f"{self.name}: 循环体为空")
        self._child = py_trees.composites.Sequence(
            name=f"{self.name}#iter{self._iter}", memory=True, children=children)
        self._child.setup()
        self.rebuilds += 1
        self._child.tick_once()
        return self._after_tick(self._child.status)

    def _after_tick(self, status: Status) -> Status:
        if status is Status.SUCCESS:
            self.iterations_completed += 1
            self._child = None
            if self._iter >= self._max:
                return Status.SUCCESS
            return Status.RUNNING          # 下一轮
        if status is Status.FAILURE:
            return Status.FAILURE          # 不落入替代分支
        return Status.RUNNING


__all__ = ["BoundedLoop", "UnboundedLoop"]
