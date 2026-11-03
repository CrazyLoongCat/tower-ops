"""边缘侧 CameraRegistry：按序列号绑定通道 (E07)。

- **按序列号绑定**，逻辑 ID 与设备身份绑定 —— 插拔或枚举顺序变化**不会**悄悄换掉 cam1/cam2；
- **重复序列号拒绝**（配置错误，直接抛错，不"取其一"）；
- **设备缺失只让该通道失败**，其余通道照常（不得误标"全部失败"）；
- 每通道独立 Actor（自带 epoch/sequence/health 与有界帧缓存）；
- **导入期不打开设备**：所有句柄在 ``start_all()`` 时才创建。

真实 SDK 版本由 E06/E10/E11 在板端落实；本模块离线用 Fake 源验证故障隔离与生命周期。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from edge_service.camera_actor import (
    STATE_FAILED,
    CameraActor,
    ChannelSource,
    FakeChannelSource,
)
from edge_service.frame_store import FrameStore

DEFAULT_CHANNEL_WIDTH = 848
DEFAULT_CHANNEL_HEIGHT = 480


class BindError(ValueError):
    """绑定配置错误（如序列号重复）。"""


@dataclass
class ChannelConfig:
    """一个通道的绑定配置。``serial`` 为空表示**待现场填写**（该通道保持失败）。"""

    channel_id: str
    serial: str = ""
    width: int = DEFAULT_CHANNEL_WIDTH
    height: int = DEFAULT_CHANNEL_HEIGHT
    alignment: bool = True
    enabled: bool = True

    def __post_init__(self) -> None:
        if not str(self.channel_id).strip():
            raise BindError("通道 channel_id 不能为空")
        if self.width <= 0 or self.height <= 0:
            raise BindError(f"通道 {self.channel_id!r} 宽高必须为正")


def _duplicate_serials(configs: Iterable[ChannelConfig]) -> list[str]:
    seen: dict[str, int] = {}
    for cfg in configs:
        serial = (cfg.serial or "").strip()
        if not serial:
            continue                      # 空序列号 = 待现场填, 不算重复
        seen[serial] = seen.get(serial, 0) + 1
    return sorted(s for s, n in seen.items() if n > 1)


class CameraRegistry:
    """通道注册表：绑定、启停、健康与逐通道 Actor。"""

    def __init__(self, configs: Sequence[ChannelConfig], *,
                 store: FrameStore | None = None,
                 source_factory: Callable[[ChannelConfig, bool], ChannelSource] | None = None,
                 enumerated: Iterable[str] | None = None) -> None:
        self._configs = list(configs)
        dupes = _duplicate_serials(self._configs)
        if dupes:
            raise BindError(f"序列号重复，拒绝绑定: {dupes}")
        self._store = store if store is not None else FrameStore()
        self._factory = source_factory or _default_source_factory
        # 已枚举到的设备序列号集合；None 表示"按配置视为全部在位"（离线）
        self._present: set[str] | None = (
            None if enumerated is None
            else {str(s).strip() for s in enumerated if str(s).strip()})
        self._actors: dict[str, CameraActor] = {}
        self._build()

    # ---------- 构建 ----------
    def _build(self) -> None:
        for cfg in self._configs:
            source = self._factory(cfg, self._is_present(cfg))
            self._actors[cfg.channel_id] = CameraActor(
                cfg.channel_id, source, store=self._store, align=cfg.alignment)

    def _is_present(self, cfg: ChannelConfig) -> bool:
        serial = (cfg.serial or "").strip()
        if not serial:
            return False                  # 序列号待现场填 -> 视为设备缺失
        if self._present is None:
            return True
        return serial in self._present

    # ---------- 生命周期 ----------
    def start_all(self) -> dict:
        """启动所有通道；返回各通道健康。**单通道失败不影响其它通道**。"""
        for cfg in self._configs:
            if not cfg.enabled:
                continue
            actor = self._actors[cfg.channel_id]
            if not self._is_present(cfg):
                # 设备缺失：不打开, 直接标该通道失败并说明原因
                actor.health().state = STATE_FAILED
                actor.health().last_error = (
                    f"设备缺失: 序列号 {cfg.serial!r} 未在枚举结果中")
                continue
            actor.start()
        return self.health()

    def stop_all(self) -> None:
        for actor in self._actors.values():
            actor.stop()

    def restart(self, channel: str) -> dict:
        """重启单通道：epoch+1、清本通道旧帧。"""
        self._actors[channel].restart()
        return self._actors[channel].health_dict()

    def rebind(self, enumerated: Iterable[str]) -> dict:
        """重新枚举后刷新在位情况；序列号消失的通道转失败，重新出现的通道重启换 epoch。

        **逻辑 ID 不变**：身份由序列号决定，因此枚举顺序变化不会互换 cam1/cam2。
        """
        self._present = {str(s).strip() for s in enumerated if str(s).strip()}
        for cfg in self._configs:
            actor = self._actors[cfg.channel_id]
            present = self._is_present(cfg)
            was_ok = actor.health().state != STATE_FAILED
            if present and not was_ok:
                actor.restart()           # 重新出现 -> 换 epoch 重新建立
            elif not present and was_ok:
                actor.health().state = STATE_FAILED
                actor.health().last_error = (
                    f"设备缺失: 序列号 {cfg.serial!r} 未在枚举结果中")
        return self.health()

    # ---------- 查询 ----------
    def channels(self) -> list[str]:
        return [cfg.channel_id for cfg in self._configs]

    def config(self, channel: str) -> ChannelConfig:
        for cfg in self._configs:
            if cfg.channel_id == channel:
                return cfg
        raise KeyError(f"未知通道 {channel!r}")

    def actor(self, channel: str) -> CameraActor:
        try:
            return self._actors[channel]
        except KeyError as exc:
            raise KeyError(f"未知通道 {channel!r}") from exc

    def serial_of(self, channel: str) -> str:
        return self.config(channel).serial

    def health(self) -> dict:
        return {cid: actor.health_dict() for cid, actor in self._actors.items()}

    def store_stats(self) -> dict:
        return self._store.stats()


def _default_source_factory(cfg: ChannelConfig, present: bool) -> ChannelSource:
    """离线默认：Fake 源；设备缺失时构造为"打开即失败"，以模拟真实缺失。"""
    return FakeChannelSource(cfg.serial, width=cfg.width, height=cfg.height,
                             fail_open=not present)


def load_edge_config(path: str) -> tuple[dict, list[ChannelConfig]]:
    """读取边缘通道配置（YAML）。返回 ``(service, channels)``。

    只解析，不打开任何设备。
    """
    from ruamel.yaml import YAML

    with open(path, encoding="utf-8") as fh:
        data = YAML(typ="safe").load(fh) or {}
    if data.get("schema_version") not in (1, "1"):
        raise BindError(f"schema_version 不支持: {data.get('schema_version')!r}")
    channels = [ChannelConfig(
        channel_id=str(c["channel_id"]),
        serial=str(c.get("serial", "")),
        width=int(c.get("width", DEFAULT_CHANNEL_WIDTH)),
        height=int(c.get("height", DEFAULT_CHANNEL_HEIGHT)),
        alignment=bool(c.get("alignment", True)),
        enabled=bool(c.get("enabled", True)),
    ) for c in (data.get("channels") or [])]
    return dict(data.get("service") or {}), channels


__all__ = ["BindError", "CameraRegistry", "ChannelConfig", "DEFAULT_CHANNEL_HEIGHT",
           "DEFAULT_CHANNEL_WIDTH", "load_edge_config"]
