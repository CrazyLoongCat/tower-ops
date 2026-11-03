"""真实设备模式的安全约束。

**DEC-085 起不再有环境变量闸**：`python main.py real` 就是真实设备模式，
会连接全部已配置设备并允许运动。唯一保留的硬约束是：

**未确认/占位的示教点一律拒绝运动** —— 总纲 §1041"输入不全时由任务状态保留等待，
不以示例参数凑出真实配置"。真实运动需要目标是已确认的示教点（非离线占位）。

`GatedRobot` 仍包住真实机器人：对 `move_joint` 的关节目标做确认检查，其它方法透明代理。
"""
from __future__ import annotations

from typing import Any, Callable

TARGET_HINT = ("拒绝运动到未确认的示教点：目标 waypoint_ref={ref!r} 属占位/未确认；"
               "真实示教点须由现场用示教器取得并登记（INPUT-PATH）")

# waypoint_ref 里出现这些片段即视为"占位/未确认"
_PLACEHOLDER_MARKERS = ("offline", "placeholder", "unconfirmed", "待核对",
                        "待确认", "todo", "tbd")


class AuthorizationError(PermissionError):
    """运动目标未确认等安全拒绝。"""


class MotionBlocked(AuthorizationError):
    """运动被拒（目标未确认）。"""


def is_confirmed_waypoint(waypoint_ref: str | None) -> bool:
    """目标是否是**已确认**的示教点（而不是离线占位）。"""
    ref = (waypoint_ref or "").strip()
    if not ref:
        return False
    low = ref.lower()
    return not any(marker in low for marker in _PLACEHOLDER_MARKERS)


def require_confirmed_target(target: Any) -> None:
    """运动目标必须是已确认示教点。"""
    ref = getattr(target, "waypoint_ref", None)
    if not is_confirmed_waypoint(ref):
        raise MotionBlocked(TARGET_HINT.format(ref=ref))


class GatedRobot:
    """包住真实机器人：只拦截**未确认示教点**的 `move_joint`，其余透明代理。"""

    MOTION_METHODS = (
        "move_joint", "set_io", "movel", "movej", "movej_p", "move_waypoints",
        "move_offset", "grip_socket", "release_socket", "pause", "continue_",
        "stop", "delete_trajectory",
    )

    def __init__(self, inner: Any, *,
                 on_block: Callable[[str, str], None] | None = None) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_on_block", on_block)
        object.__setattr__(self, "blocked_calls", [])

    def _guard(self, name: str, *args: Any) -> None:
        if name == "move_joint" and args:
            # 只对「关节目标对象」做示教点确认；movel/movej 收的是数值数组（运动方案
            # 的实测路点），其合法性由配置来源保证，不在此判 waypoint_ref
            try:
                require_confirmed_target(args[0])
            except MotionBlocked as exc:
                self.blocked_calls.append((name, str(exc)))
                if self._on_block:
                    self._on_block(name, str(exc))
                raise

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)
        if name in type(self).MOTION_METHODS and callable(attr):
            def guarded(*args: Any, **kwargs: Any) -> Any:
                self._guard(name, *args)
                return attr(*args, **kwargs)
            return guarded
        return attr

    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def motion_allowed(self) -> bool:
        """real 模式恒为 True；保留属性供界面/日志读取。"""
        return True


__all__ = [
    "AuthorizationError", "GatedRobot", "MotionBlocked", "TARGET_HINT",
    "is_confirmed_waypoint", "require_confirmed_target",
]
