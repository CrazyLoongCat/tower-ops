"""自定义方法库 (E20+, DEC-030)。

配方里的 **custom.call（自定义操作）** 步骤可选择一个这里注册的**自定义方法**。
方法由**程序代码**定义（一个 Python callable + 一段声明式的**安全边界**元数据），
界面只负责「选一个方法 + 选它要操作的设备」，**不提供任意代码/动态导入**——
与 node type 固定工厂同一条约束。

安全边界（每一项都参与校验与执行期拒绝，不是注释）：

- ``devices``: 该方法需要几个设备 —— ``none`` / ``one`` / ``many``；配方步骤的
  ``device_refs`` 个数必须落在其区间内，否则发布前即被拒。
- ``needs_motion``: 是否驱动**运动**（关节/机器人）。为 True 时执行期必须走运动闸，
  离线栈（无运动授权）一律**拒绝执行**（宁缺勿假）。
- ``needs_board``: 是否下发**机构/板卡**动作。为 True 时若执行栈没有板卡适配器，拒绝执行。
- ``offline_ok``: 是否允许在离线演示档位执行。为 False 的方法离线一律拒绝。
- ``requires_confirm``: 执行前是否需要人工确认（界面/流程据此提示；不绕过互锁）。

本版本内置方法偏**只读探测**（离线可跑通、真实也安全）；凡声明 ``needs_board`` /
``needs_motion`` 或 ``offline_ok=False`` 的方法，在离线栈里都会被**如实拒绝**，
用于演示边界确实生效，而不是伪造成功。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class MethodError(RuntimeError):
    """自定义方法相关错误（未注册 / 边界不符 / 执行失败）。"""


@dataclass(frozen=True)
class MethodParam:
    """一个自定义方法参数（供界面渲染/校验；本版本仅做存在与类型粗校验）。"""

    name: str
    title: str
    kind: str = "str"          # str / int / float / bool
    default: Any = None
    required: bool = False


@dataclass(frozen=True)
class MethodOutcome:
    """一次自定义方法执行的结果（如实汇报，不掩饰部分失败）。"""

    ok: bool
    detail: str = ""
    per_device: tuple[tuple[str, str], ...] = ()   # (device_id, 该设备结果)


# 执行函数签名: (env, device_refs, params) -> MethodOutcome
#   env 由 ActionExecutor.run_custom_method 组装, 至少含: executor / ctx / device_state
Runner = Callable[..., MethodOutcome]

# 设备个数语义
DEVICE_COUNTS = ("none", "one", "many")


@dataclass(frozen=True)
class CustomMethod:
    method_id: str
    title: str
    description: str = ""
    devices: str = "none"                       # none / one / many
    needs_motion: bool = False
    needs_board: bool = False
    offline_ok: bool = True
    requires_confirm: bool = False
    params: tuple[MethodParam, ...] = ()
    runner: Runner | None = None

    def accepts_devices(self, n: int) -> bool:
        if self.devices == "none":
            return n == 0
        if self.devices == "one":
            return n == 1
        return n >= 1   # many

    def device_hint(self) -> str:
        return {"none": "不需要设备", "one": "需要 1 个设备", "many": "可选多个设备"}[
            self.devices]


class MethodRegistry:
    """自定义方法注册表. 未注册即拒绝（发布前校验）。"""

    def __init__(self, methods: list[CustomMethod] | None = None) -> None:
        self._items: dict[str, CustomMethod] = {}
        for m in (methods or []):
            self.register(m)

    def register(self, method: CustomMethod) -> None:
        if method.devices not in DEVICE_COUNTS:
            raise MethodError(f"方法 {method.method_id!r} 的 devices 非法: {method.devices!r}")
        if method.method_id in self._items:
            raise MethodError(f"自定义方法 {method.method_id!r} 已存在")
        self._items[method.method_id] = method

    def get(self, method_id: str) -> CustomMethod:
        m = self._items.get(method_id)
        if m is None:
            raise MethodError(f"未注册的自定义方法: {method_id!r}")
        return m

    def find(self, method_id: str) -> CustomMethod | None:
        return self._items.get(method_id)

    def known(self, method_id: str) -> bool:
        return method_id in self._items

    def ids(self) -> list[str]:
        return sorted(self._items)

    def items(self) -> list[CustomMethod]:
        return [self._items[k] for k in sorted(self._items)]


# ---------------------------------------------------------------- 内置方法
def _probe_devices(env, device_refs: tuple[str, ...], params: dict) -> MethodOutcome:
    """只读探测所选设备当前状态；全部可读即成功（不驱动任何运动/机构）。

    这是**离线可跑通**的安全方法：只调用执行栈里已有的状态读取接口
    （机器人 ``read_state`` / 相机 ``status``），不改任何设备状态。
    """
    executor = env.get("executor")
    per: list[tuple[str, str]] = []
    ok = True
    for dev in device_refs:
        state = _read_device_state(executor, dev)
        if state is None:
            per.append((dev, "未知设备/无状态接口"))
            ok = False
        else:
            per.append((dev, state))
    detail = "；".join(f"{d}={s}" for d, s in per) or "无设备"
    return MethodOutcome(ok=ok, detail=detail, per_device=tuple(per))


def _read_device_state(executor: Any, dev: str) -> str | None:
    if executor is None:
        return None
    robot = getattr(executor, "robot", None)
    if robot is not None and hasattr(robot, "read_state"):
        st = robot.read_state() or {}
        # 机器人在设备名里通常以 eco65 前缀登记；这里不猜名字，仅当名字吻合才取
        if dev.startswith("eco65") or dev == getattr(robot, "device_id", None):
            return f"operational={st.get('operational', '?')}"
    stores = getattr(executor, "state_store", None)
    if stores is not None and hasattr(stores, "get"):
        try:
            v = stores.get(dev)
            if v is not None:
                return str(v)
        except Exception:  # noqa: BLE001 - 状态不可读按未知处理
            pass
    cams = getattr(executor, "cameras", None) or {}
    if dev in cams:
        s = cams[dev].status() if hasattr(cams[dev], "status") else {}
        return "已打开" if s.get("open") else "未打开"
    return None


def _set_output(env, device_refs: tuple[str, ...], params: dict) -> MethodOutcome:
    """置位一个机构输出（**需板卡**）。本版本执行栈未接入真实输出映射，一律如实拒绝。"""
    return MethodOutcome(
        ok=False,
        detail="置位输出需要已接线的板卡输出映射；当前执行栈未提供，拒绝执行（不伪造成功）",
    )


def default_methods() -> MethodRegistry:
    """内置方法库。只读探测离线可用；需要机构/运动的一律声明边界并在执行期拒绝。"""
    return MethodRegistry([
        CustomMethod(
            method_id="probe_devices",
            title="探测设备状态（只读）",
            description="读取所选设备当前状态并汇总；不驱动任何运动/机构，离线可跑通。",
            devices="many",
            needs_motion=False,
            needs_board=False,
            offline_ok=True,
            params=(),
            runner=_probe_devices,
        ),
        CustomMethod(
            method_id="set_output",
            title="置位机构输出（需板卡）",
            description="对所选设备置位一个机构输出；需要真实板卡输出映射。",
            devices="many",
            needs_motion=False,
            needs_board=True,
            offline_ok=False,
            requires_confirm=True,
            params=(MethodParam(name="channel", title="输出通道", kind="int", required=True),),
            runner=_set_output,
        ),
    ])


__all__ = [
    "CustomMethod", "DEVICE_COUNTS", "MethodError", "MethodOutcome", "MethodParam",
    "MethodRegistry", "default_methods",
]
