"""边缘侧有界帧缓存 (E07)。

要点：
- **复制持有**（或受控持有），不依赖 SDK 缓冲生命周期 —— SDK 复用缓冲不得改写已发布帧（T21）；
- 保留**原始深度单位**与**对齐内参**（不换算成毫米后丢掉系数）；
- 容量与字节数**都有上限**，超出按最旧淘汰，不做无界增长。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from application.contracts.frames import Intrinsics

DEFAULT_CAPACITY_PER_CHANNEL = 4
DEFAULT_MAX_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class StoredFrame:
    """缓存中的一帧（数组只读）。"""

    channel: str
    sequence: int
    epoch: int
    color: Any | None
    depth: Any | None
    depth_scale_m: float | None
    intrinsics: Intrinsics | None
    device_timestamp_ns: int | None
    received_monotonic_s: float

    @property
    def nbytes(self) -> int:
        n = 0
        for a in (self.color, self.depth):
            if a is not None:
                n += int(a.nbytes)
        return n

    def as_arrays(self) -> tuple[Any | None, Any | None]:
        return self.color, self.depth


def _freeze(array):
    """复制成独立只读数组；None 原样返回。"""
    if array is None:
        return None
    import numpy as np
    if not isinstance(array, np.ndarray):
        raise TypeError("帧数组必须是 numpy.ndarray")
    out = np.ascontiguousarray(array).copy()
    out.setflags(write=False)          # 只读: 防止下游改写已发布帧
    return out


class FrameStore:
    """按通道分桶的有界帧缓存。"""

    def __init__(self, *, capacity_per_channel: int = DEFAULT_CAPACITY_PER_CHANNEL,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if capacity_per_channel <= 0:
            raise ValueError("capacity_per_channel 必须为正")
        self._capacity = int(capacity_per_channel)
        self._max_bytes = int(max_bytes)
        self._buckets: dict[str, list[StoredFrame]] = {}
        self._bytes = 0

    # ---------- 写 ----------
    def put(self, *, channel: str, sequence: int, epoch: int,
            color: Any = None, depth: Any = None, depth_scale_m: float | None = None,
            intrinsics: Intrinsics | None = None,
            device_timestamp_ns: int | None = None) -> StoredFrame:
        frame = StoredFrame(
            channel=channel, sequence=int(sequence), epoch=int(epoch),
            color=_freeze(color), depth=_freeze(depth),
            depth_scale_m=depth_scale_m, intrinsics=intrinsics,
            device_timestamp_ns=device_timestamp_ns,
            received_monotonic_s=time.monotonic())
        bucket = self._buckets.setdefault(channel, [])
        bucket.append(frame)
        self._bytes += frame.nbytes
        self._evict(channel)
        return frame

    def _evict(self, channel: str) -> None:
        bucket = self._buckets[channel]
        while len(bucket) > self._capacity:
            self._bytes -= bucket.pop(0).nbytes
        while self._bytes > self._max_bytes and len(bucket) > 1:
            self._bytes -= bucket.pop(0).nbytes

    # ---------- 读 ----------
    def latest(self, channel: str) -> StoredFrame | None:
        bucket = self._buckets.get(channel)
        return bucket[-1] if bucket else None

    def history(self, channel: str) -> list[StoredFrame]:
        return list(self._buckets.get(channel, ()))

    def clear(self, channel: str | None = None) -> None:
        if channel is None:
            self._buckets.clear()
            self._bytes = 0
            return
        for f in self._buckets.pop(channel, []):
            self._bytes -= f.nbytes

    def stats(self) -> dict:
        return {
            "channels": {c: len(v) for c, v in self._buckets.items()},
            "bytes": self._bytes,
            "capacity_per_channel": self._capacity,
            "max_bytes": self._max_bytes,
        }


__all__ = ["DEFAULT_CAPACITY_PER_CHANNEL", "DEFAULT_MAX_BYTES", "FrameStore",
           "StoredFrame"]
