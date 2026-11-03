"""边缘侧通道 Actor (E07)。

每个通道**独立拥有**采集源、对齐/流水线开关、`epoch`、`sequence` 与健康状态：
一个通道失败**不得**被误标成"全部失败"，重启只影响本通道。

**禁止 import 时自动打开设备**：本模块不在导入期打开任何句柄；采集源由调用方显式创建并 ``start()``。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from application.contracts.enums import ImageSpace
from application.contracts.frames import Intrinsics
from edge_service.frame_store import FrameStore, StoredFrame

STATE_OK = "OK"
STATE_FAILED = "FAILED"
STATE_STOPPED = "STOPPED"

DEFAULT_DEPTH_SCALE_M = 0.001


class ChannelSource(Protocol):
    """采集源：板端真实 SDK 或离线 Fake 都实现这个最小接口。"""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def grab(self, deadline_ms: int) -> dict: ...


@dataclass
class ActorHealth:
    channel: str
    state: str = STATE_STOPPED
    epoch: int = 0
    sequence: int = 0
    last_error: str | None = None
    last_grab_monotonic_s: float | None = None

    def as_dict(self) -> dict:
        return {"channel": self.channel, "state": self.state, "epoch": self.epoch,
                "sequence": self.sequence, "last_error": self.last_error,
                "last_grab_monotonic_s": self.last_grab_monotonic_s}


class FakeChannelSource:
    """离线合成源（**不是**真机；上层必须据此标注来源）。"""

    def __init__(self, serial: str = "", *, width: int = 848, height: int = 480,
                 depth_scale_m: float = DEFAULT_DEPTH_SCALE_M,
                 fail_open: bool = False) -> None:
        self.serial = serial
        self.width = int(width)
        self.height = int(height)
        self.depth_scale_m = float(depth_scale_m)
        self.fail_open = fail_open
        self.opened = False

    def open(self) -> None:
        if self.fail_open:
            raise RuntimeError(f"Fake 源打开失败(注入): serial={self.serial!r}")
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def grab(self, deadline_ms: int) -> dict:
        import numpy as np
        if not self.opened:
            raise RuntimeError("通道未打开")
        h, w = self.height, self.width
        color = np.zeros((h, w, 3), dtype="uint8")
        depth = np.zeros((h, w), dtype="uint16")
        yy, xx = np.mgrid[0:h, 0:w]
        depth[np.hypot(xx - w / 2.0, yy - h / 2.0) < min(w, h) / 6.0] = 1000
        return {
            "color": color,
            "depth": depth,
            "depth_scale_m": self.depth_scale_m,
            "device_timestamp_ns": int(time.monotonic() * 1e9),
            "intrinsics": Intrinsics(
                fx=600.0, fy=600.0, cx=w / 2.0, cy=h / 2.0,
                width=w, height=h, intrinsics_space=ImageSpace.DEPTH,
                calibration_version="fake-edge-v1"),
        }


class CameraActor:
    """单通道采集 Actor。"""

    def __init__(self, channel: str, source: ChannelSource, *,
                 store: FrameStore | None = None, align: bool = True) -> None:
        self.channel = str(channel)
        self._source = source
        self.store = store if store is not None else FrameStore()
        self.align = bool(align)          # 该通道自己的对齐/流水线开关
        self._health = ActorHealth(channel=self.channel)

    # ---------- 生命周期 ----------
    def start(self) -> None:
        try:
            self._source.open()
        except Exception as exc:  # noqa: BLE001 - 单通道失败只影响本通道
            self._health.state = STATE_FAILED
            self._health.last_error = f"{type(exc).__name__}: {exc}"
            return
        self._health.state = STATE_OK
        self._health.last_error = None

    def stop(self) -> None:
        try:
            self._source.close()
        except Exception:  # noqa: BLE001
            pass
        self._health.state = STATE_STOPPED

    def restart(self) -> None:
        """重启：epoch+1 并清掉本通道旧帧 —— 旧 epoch 的帧不得当作当前帧。"""
        self.stop()
        self.store.clear(self.channel)
        self._health.epoch += 1
        self._health.sequence = 0
        self.start()

    # ---------- 采集 ----------
    def grab(self, deadline_ms: int = 2000) -> StoredFrame:
        if self._health.state != STATE_OK:
            raise RuntimeError(f"通道 {self.channel!r} 状态 {self._health.state}")
        try:
            raw = self._source.grab(int(deadline_ms))
        except Exception as exc:  # noqa: BLE001
            self._health.state = STATE_FAILED
            self._health.last_error = f"{type(exc).__name__}: {exc}"
            raise
        self._health.sequence += 1
        frame = self.store.put(
            channel=self.channel, sequence=self._health.sequence,
            epoch=self._health.epoch, color=raw.get("color"), depth=raw.get("depth"),
            depth_scale_m=raw.get("depth_scale_m"),
            intrinsics=raw.get("intrinsics"),
            device_timestamp_ns=raw.get("device_timestamp_ns"))
        self._health.last_grab_monotonic_s = frame.received_monotonic_s
        return frame

    # ---------- 健康 ----------
    def health(self) -> ActorHealth:
        return self._health

    def health_dict(self) -> dict:
        d = self._health.as_dict()
        d["align"] = self.align
        d["serial"] = getattr(self._source, "serial", None)
        return d


__all__ = ["ActorHealth", "CameraActor", "ChannelSource", "FakeChannelSource",
           "STATE_FAILED", "STATE_OK", "STATE_STOPPED"]
