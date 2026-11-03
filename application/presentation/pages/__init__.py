"""页面 (A18 最小作业台; 其余页面在 E 阶段)。"""
from __future__ import annotations

from application.services.device_config import DeviceConfigStore
from application.services.device_state import DeviceStateRegistry


def store_of(ctx) -> DeviceConfigStore:
    """页面共用的设备配置；栈里没有时退回内存 store（不写盘）。"""
    store = getattr(getattr(ctx, "stack", None), "device_config", None)
    if isinstance(store, DeviceConfigStore):
        return store
    return DeviceConfigStore.in_memory()


def state_of(ctx) -> DeviceStateRegistry:
    """页面共用的运行期设备状态（最近探测 + 会话开关），两页共享同一份。"""
    state = getattr(getattr(ctx, "stack", None), "device_state", None)
    if isinstance(state, DeviceStateRegistry):
        return state
    return DeviceStateRegistry()


__all__ = ["state_of", "store_of"]
