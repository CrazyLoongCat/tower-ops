"""动作定义与目录 (12 §4.1, 02 §1/§5).

ActionDefinition 声明资源、参数范围、前置条件、完成证据、超时与重试类别.
动作与 UI 按钮无关; 声明实际完成证据. 未知动作/算法在发布前拒绝.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from application.contracts.enums import RetryClass
from application.domain.errors import UnknownAction, ValidationFailure


class PreconditionKind(str, enum.Enum):
    TELEMETRY_FRESH = "telemetry_fresh"   # 设备状态在时效窗内
    RESOURCE_FREE = "resource_free"        # 指定资源当前空闲
    STATE_EQ = "state_eq"                  # 遥测字段 == 期望值


@dataclass(frozen=True)
class Precondition:
    """结构化前置条件 (无动态导入/表达式求值)."""

    kind: PreconditionKind
    device_id: str | None = None
    field_name: str | None = None
    value: Any = None
    resource_id: str | None = None
    max_age_ms: float = 1000.0


@dataclass(frozen=True)
class CompletionSpec:
    """完成证据: 有效 DONE 成功码 + 必需新鲜终态字段检查 (12 §4.2)."""

    requires_done: bool = True
    state_checks: tuple[tuple[str, str, Any], ...] = ()  # (field, op, value)
    stable_window_ms: int = 0


class ParamSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    required: bool = True

    def check(self, value: Any) -> None:
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationFailure(f"参数 {self.name!r} 不是数值: {value!r}") from exc
        if self.minimum is not None and num < self.minimum:
            raise ValidationFailure(f"参数 {self.name!r}={num} 低于下限 {self.minimum}")
        if self.maximum is not None and num > self.maximum:
            raise ValidationFailure(f"参数 {self.name!r}={num} 超过上限 {self.maximum}")


class ActionDefinition(BaseModel):
    """具名动作. 声明资源/参数/前置/完成/超时/重试/相反动作."""

    model_config = ConfigDict(extra="forbid")

    action_id: str
    device_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    parameters: tuple[ParamSpec, ...] = ()
    preconditions: tuple[Precondition, ...] = ()
    completion: CompletionSpec = Field(default_factory=CompletionSpec)
    timeout_ms: int = 5000
    retry_class: RetryClass = RetryClass.NON_IDEMPOTENT
    opposes: tuple[str, ...] = ()
    description: str = ""

    @field_validator("action_id")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValidationFailure("action_id 不能为空")
        return v.strip()

    def validate_parameters(self, params: dict[str, Any]) -> None:
        known = {p.name for p in self.parameters}
        extra = set(params) - known
        if extra:
            raise ValidationFailure(
                f"动作 {self.action_id!r} 收到未知参数: {sorted(extra)}"
            )
        for spec in self.parameters:
            if spec.name not in params:
                if spec.required:
                    raise ValidationFailure(
                        f"动作 {self.action_id!r} 缺少必需参数 {spec.name!r}"
                    )
                continue
            spec.check(params[spec.name])


class ActionCatalog:
    """动作目录: action_id -> ActionDefinition. 发布前拒绝未知引用."""

    def __init__(self, actions: Iterable[ActionDefinition] = ()) -> None:
        self._actions: dict[str, ActionDefinition] = {}
        for a in actions:
            self.register(a)

    def register(self, action: ActionDefinition) -> None:
        if action.action_id in self._actions:
            raise ValidationFailure(f"动作 {action.action_id!r} 已存在")
        self._actions[action.action_id] = action

    def get(self, action_id: str) -> ActionDefinition:
        act = self._actions.get(action_id)
        if act is None:
            raise UnknownAction(f"未注册动作: {action_id!r}")
        return act

    def known(self, action_id: str) -> bool:
        return action_id in self._actions

    def ids(self) -> list[str]:
        return sorted(self._actions)


__all__ = [
    "ActionCatalog", "ActionDefinition", "CompletionSpec", "ParamSpec",
    "Precondition", "PreconditionKind",
]
