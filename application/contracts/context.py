"""运行上下文与作用域 (12 §4.1, 02 §1).

RunContext 只存本次运行结果, 不能混用上次图像/坐标.
每叶节点以 run/station_instance/step/attempt 取执行账本.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from application.contracts.ids import RecipeId, RunId, StationInstanceId, StepId


@dataclass(frozen=True)
class VersionSnapshot:
    """运行前冻结的版本快照 (T20). 运行中修改草稿不改运行 hash."""

    recipe_id: RecipeId
    recipe_version: str
    config_hash: str
    calibration_hash: str
    pipeline_hashes: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "recipe_version": self.recipe_version,
            "config_hash": self.config_hash,
            "calibration_hash": self.calibration_hash,
            "pipeline_hashes": dict(self.pipeline_hashes),
        }


@dataclass
class ExecutionKey:
    """执行账本键. attempt/iteration 参与键, 使重试/循环迭代各有独立作用域 (T27/E13)."""

    run_id: RunId
    station_instance_id: StationInstanceId
    step_id: StepId
    attempt: int = 1
    iteration: int = 0

    def as_tuple(self) -> tuple[str, str, str, int, int]:
        return (self.run_id, self.station_instance_id, self.step_id,
                self.attempt, self.iteration)


@dataclass
class RunContext:
    """一次生产任务的运行上下文. 作用域输出不跨 run 混用 (T15)."""

    run_id: RunId
    versions: VersionSnapshot
    station_instance_id: StationInstanceId | None = None
    iteration: int = 0
    scoped_outputs: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def child_for_station(
        self, station_instance_id: StationInstanceId, iteration: int = 0
    ) -> "RunContext":
        """派生一个带独立作用域的上下文.

        无工位后（配方一条 sequence），编译器用它建立**单一运行作用域**
        ``f"{run_id}:run:0"``（沿用 ``station_instance_id`` 字段作作用域键）；
        子流程/循环仍在此基础上叠加作用域，使账本与输出不跨实例串用。
        """
        return RunContext(
            run_id=self.run_id,
            versions=self.versions,
            station_instance_id=station_instance_id,
            iteration=iteration,
        )

    def for_iteration(self, iteration: int) -> "RunContext":
        """派生同一作用域的下一轮迭代上下文 (E13 有界循环)."""
        return replace(self, iteration=iteration)

    def for_subflow(self, scope_key: str) -> "RunContext":
        """派生子流程作用域; 作用域并入 station_instance_id, 使账本与输出不跨实例串用."""
        base = self.station_instance_id or ""
        scoped = f"{base}/{scope_key}" if base else scope_key
        return replace(self, station_instance_id=scoped)


__all__ = ["ExecutionKey", "RunContext", "VersionSnapshot"]
