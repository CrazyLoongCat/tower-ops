"""启动/关闭生命周期与 profile 校验 (12 §4.4, 02 §3.1, A17)。

- startup 只校验接口/能力, 不执行运动;
- 关闭顺序: 停止 -> 采集/tick/队列 -> 设备/宿主 -> 记录/执行器 -> (Qt);
- 拒绝把 Fake 配置冒充生产 fallback (profile 必须显式).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from application.infrastructure import logging_setup

VALID_PROFILES = ("offline", "production")

_log = logging_setup.get_logger("bootstrap.lifecycle")


class ProfileError(RuntimeError):
    """profile 非法: 不允许 Fake 冒充生产."""


def assert_profile(profile: str) -> None:
    if profile not in VALID_PROFILES:
        raise ProfileError(f"未知 profile: {profile!r}")


def assert_no_fake_in_production(profile: str, fake_flags: list[tuple[str, bool]]) -> None:
    """production 中任何组件标记为 fake 即拒绝启动."""
    assert_profile(profile)
    if profile != "production":
        return
    fakes = [name for name, is_fake in fake_flags if is_fake]
    if fakes:
        raise ProfileError(
            f"production profile 含 Fake 组件: {fakes}; 不允许静默回退到 Fake"
        )


@dataclass
class LifecycleManager:
    """持有离线/生产栈并管理启动/关闭顺序. 可重复调用 shutdown (幂等)."""

    stack: Any
    profile: str = "offline"
    _started: bool = False
    _shutdown_done: bool = False
    startup_steps: list[str] = field(default_factory=list)
    shutdown_steps: list[str] = field(default_factory=list)

    def start(self) -> None:
        assert_profile(self.profile)
        # startup 无动作: 离线栈装配后 move_count 必须为 0
        robot = getattr(self.stack, "robot", None)
        if robot is not None and robot.read_state().get("move_count", 0) != 0:
            _log.error("startup 阶段发生运动, 违反无动作约束 profile=%s", self.profile)
            raise RuntimeError("startup 阶段发生运动, 违反无动作约束")
        self.startup_steps.append("validate")
        self.startup_steps.append("build_offline_stack")
        self.startup_steps.append("subscribe_state")
        self._started = True
        _log.info("生命周期启动完成 profile=%s（startup 无动作）", self.profile)

    def shutdown(self) -> None:
        """严格顺序关闭; 不把进程退出当硬件停稳."""
        if self._shutdown_done:
            return
        # 1) 停止 (若任务活跃)
        self.shutdown_steps.append("stop")
        try:
            task = getattr(self.stack, "task", None)
            if task is not None and task.state_machine.is_active:
                _log.warning("关闭时任务仍活跃, 请求终止 profile=%s", self.profile)
                task.terminate("应用关闭")
        except Exception:  # noqa: BLE001
            _log.debug("关闭阶段 terminate 异常（忽略）", exc_info=True)
        # 2) 设备/宿主/记录/存储
        self.shutdown_steps.append("close_devices")
        try:
            self.stack.close()
        except Exception:  # noqa: BLE001
            _log.debug("关闭阶段 close 异常（忽略）", exc_info=True)
        self._shutdown_done = True
        _log.info("生命周期关闭完成 profile=%s 步骤=%s", self.profile,
                  "->".join(self.shutdown_steps))

    @property
    def started(self) -> bool:
        return self._started

    @property
    def shutdown_done(self) -> bool:
        return self._shutdown_done


__all__ = [
    "LifecycleManager", "ProfileError", "VALID_PROFILES", "assert_no_fake_in_production",
    "assert_profile",
]
