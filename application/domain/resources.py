"""资源租约、全局运动互斥与 UNKNOWN 隔离 (12 §4.2, 02 §5.3/§9.2).

默认机器人运动经 motion_global 互斥；机构动作占用**板卡自己的 device 资源**，
以便「并行执行步骤组」里与机器人运动同时进行（DEC-096）。抓拍时取得
静止保护 (capture_stillness)，防止自动/手动移动破坏标定与视觉输入；机构动作
同时持有静止保护，抓拍与机构动作也不会叠在一起。
UNKNOWN 资源进入 quarantine, 不能重新分配.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field

from application.contracts.enums import SourceKind
from application.domain.errors import InterlockDenied, ResourceQuarantined

# 领域约定的具名资源.
MOTION_GLOBAL = "motion_global"
CAPTURE_STILLNESS = "capture_stillness"


def device_resource(device_id: str) -> str:
    return f"device:{device_id}"


def region_resource(region_id: str) -> str:
    """运动区域也是一种资源 (02 §9.2). 默认所有运动区域互斥由 motion_global 覆盖."""
    return f"region:{region_id}"


class LeaseState(enum.Enum):
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    RELEASED = "RELEASED"


@dataclass
class ResourceLease:
    """一次资源持有. 含物理设备与运动区域; 来源仅用于审计, 不改变硬条件."""

    resources: tuple[str, ...]
    holder: str
    source: SourceKind
    reason: str = ""
    acquired_monotonic_s: float = field(default_factory=time.monotonic)
    state: LeaseState = LeaseState.ACTIVE
    quarantine_reason: str | None = None

    @property
    def is_active(self) -> bool:
        return self.state is LeaseState.ACTIVE


@dataclass
class _Held:
    holder: str
    lease: ResourceLease


class ResourceManager:
    """单一所有者 (ControlCore) 串行管理资源. 互锁不因来源绕过 (02 §2).

    实现是纯内存、单线程所有者模型; 线程安全由调用方的控制执行单元保证.
    """

    def __init__(self) -> None:
        self._active: dict[str, _Held] = {}       # resource_id -> held
        self._quarantine: dict[str, str] = {}     # resource_id -> reason
        self._history: list[ResourceLease] = []

    # ---------- 查询 ----------
    def is_available(self, resource_id: str) -> bool:
        if resource_id in self._quarantine:
            return False
        return resource_id not in self._active

    def holder_of(self, resource_id: str) -> str | None:
        held = self._active.get(resource_id)
        return held.holder if held else None

    def quarantined(self) -> dict[str, str]:
        return dict(self._quarantine)

    # ---------- 获取 / 释放 ----------
    def acquire(
        self,
        resources: tuple[str, ...] | list[str],
        holder: str,
        source: SourceKind,
        reason: str = "",
    ) -> ResourceLease:
        """原子获取一组资源; 任一不可用则整体失败 (不部分占用)."""
        resources = tuple(resources)
        if not resources:
            raise ValueError("必须声明至少一个资源")
        # 先做全部检查, 避免部分占用.
        for rid in resources:
            if rid in self._quarantine:
                raise ResourceQuarantined(rid, self._quarantine[rid])
            held = self._active.get(rid)
            if held is not None and held.holder != holder:
                raise InterlockDenied(rid, held.holder, reason)
        lease = ResourceLease(
            resources=resources, holder=holder, source=source, reason=reason
        )
        for rid in resources:
            self._active[rid] = _Held(holder=holder, lease=lease)
        self._history.append(lease)
        return lease

    def release(self, lease: ResourceLease) -> None:
        """释放租约中仍归属本租约的资源. 已隔离资源不释放 (保持 quarantine)."""
        lease.state = LeaseState.RELEASED
        for rid in lease.resources:
            held = self._active.get(rid)
            if held is not None and held.lease is lease and rid not in self._quarantine:
                del self._active[rid]

    # ---------- UNKNOWN 隔离 ----------
    def quarantine(self, lease: ResourceLease, reason: str) -> None:
        """动作 UNKNOWN 时租约进入 quarantine, 人工核对前不可被另一动作取得."""
        lease.state = LeaseState.QUARANTINED
        lease.quarantine_reason = reason
        for rid in lease.resources:
            self._quarantine[rid] = reason
            # 保持占用, 防止 released 后又分配
            self._active[rid] = _Held(holder=lease.holder, lease=lease)

    def clear_quarantine(self, resource_id: str, operator_note: str) -> bool:
        """人工核对后解除隔离. 需显式提供核对说明 (02 §9.2)."""
        if resource_id not in self._quarantine:
            return False
        del self._quarantine[resource_id]
        held = self._active.get(resource_id)
        if held is not None and held.lease.state is LeaseState.QUARANTINED:
            held.lease.state = LeaseState.RELEASED
            del self._active[resource_id]
        self._history.append(
            ResourceLease(
                resources=(resource_id,), holder=f"operator:{operator_note}",
                source=SourceKind.RECOVERY, reason="clear_quarantine",
                state=LeaseState.RELEASED,
            )
        )
        return True

    # ---------- 相反动作互斥 ----------
    def check_opposes(
        self, requested: str, opposes: tuple[str, ...], holder: str
    ) -> None:
        """相反动作互斥: 同一通道上的相反动作不能被同时持有 (02 §4.3)."""
        for other in opposes:
            held = self._active.get(other)
            if held is not None and held.holder != holder:
                raise InterlockDenied(
                    other, held.holder, f"与 {requested!r} 相反/冲突"
                )


__all__ = [
    "CAPTURE_STILLNESS", "MOTION_GLOBAL", "LeaseState", "ResourceLease",
    "ResourceManager", "device_resource", "region_resource",
]
