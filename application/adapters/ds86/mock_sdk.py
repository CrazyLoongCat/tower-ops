"""DS86 Scepter SDK 的 Mock (A23 离线接口测试用). 不打开真实设备."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class MockScFrame:
    depth: np.ndarray
    color: np.ndarray
    depth_scale_m: float
    seq: int
    timestamp_ns: int
    intrinsics: dict  # 含 ppx/ppy (以验证别名转换)


class MockDs86Sdk:
    """最小 SDK 仿真. reuse_buffer=True 时复用同一数组对象 (验证适配器复制)."""

    def __init__(self, *, width: int = 640, height: int = 480,
                 reuse_buffer: bool = False) -> None:
        self.width = width
        self.height = height
        self.reuse_buffer = reuse_buffer
        self.opened = False
        self.closed = False
        self._seq = 0
        self._depth = np.full((height, width), 1000, dtype=np.uint16)
        self._color = np.zeros((height, width, 3), dtype=np.uint8)
        self.import_side_effects = 0

    def enumerate_devices(self):
        return [{"serial": "mock-ds86", "model": "DS86"}]

    def open(self, serial: str):
        self.opened = True

    def close(self):
        self.closed = True
        self.opened = False

    def get_intrinsics(self):
        return {"fx": 600.0, "fy": 600.0, "ppx": 320.0, "ppy": 240.0}

    def read_frame(self) -> MockScFrame:
        self._seq += 1
        if self.reuse_buffer:
            depth, color = self._depth, self._color
        else:
            depth = self._depth.copy()
            color = self._color.copy()
        return MockScFrame(
            depth=depth, color=color, depth_scale_m=0.001, seq=self._seq,
            timestamp_ns=self._seq * 1_000_000, intrinsics=self.get_intrinsics(),
        )


__all__ = ["MockDs86Sdk", "MockScFrame"]
