"""领域错误类型. 失败分类见 enums.FailureClass; 不用一个 except 统一吞掉."""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类."""


class ValidationFailure(DomainError):
    """前置/结构/单位校验失败 (可预期, 不触发硬件)."""


class InterlockDenied(DomainError):
    """互锁拒绝. 互锁不因来源是 manual 而跳过硬条件 (02 §2)."""

    def __init__(self, resource_id: str, holder: str, reason: str = "") -> None:
        self.resource_id = resource_id
        self.holder = holder
        self.reason = reason
        super().__init__(
            f"资源 {resource_id!r} 已被 {holder!r} 占用"
            + (f": {reason}" if reason else "")
        )


class ResourceQuarantined(DomainError):
    """UNKNOWN 资源进入 quarantine, 不能重新分配 (12 §4.2)."""

    def __init__(self, resource_id: str, reason: str = "") -> None:
        self.resource_id = resource_id
        super().__init__(f"资源 {resource_id!r} 处于隔离状态, 不可分配: {reason}")


class StaleState(DomainError):
    """状态时效不满足: 陈旧/未验证反馈不能作为正常前置."""

    def __init__(self, device_id: str, validity: str, age_ms: float | None) -> None:
        self.device_id = device_id
        self.validity = validity
        self.age_ms = age_ms
        super().__init__(
            f"设备 {device_id!r} 状态有效性={validity}"
            + (f", 已过期 {age_ms:.0f}ms" if age_ms is not None else "")
        )


class UnknownAction(DomainError):
    """配方引用未注册动作/算法时在发布前拒绝 (12 §4.4)."""


class MotionRefused(DomainError):
    """回放/离线装配下拒绝真实运动 (E25, T23).

    回放只注入**拒绝运动适配器**, 服务 API 直接调用也拒绝, 不建立真实运动通道.
    """


__all__ = [
    "DomainError", "InterlockDenied", "MotionRefused", "ResourceQuarantined",
    "StaleState", "UnknownAction", "ValidationFailure",
]
