"""增量帧切分与 CRC (12 §4.3, 02 §4.2, T01).

复用已核对的 Modbus CRC16 算法 (参见 Ganta_Crtl_System/jute_ui/maintenance/protocol.py).
FrameStreamSplitter 处理半包/粘包/噪声/超长: 不丢尾、不无界积累.
真实帧布局由 WireProfile 提供; 不得凭设计添加帧头/长度/字段.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


def crc16_modbus(data: bytes) -> int:
    """Modbus CRC16 (多项式 0xA001, 初值 0xFFFF). 返回小端 16 位值 (低字节在前)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


@runtime_checkable
class WireProfile(Protocol):
    """线路帧布局. 由真实 ICD/抓包或明确的 FakeProfile 实现."""

    @property
    def max_frame_bytes(self) -> int: ...

    def find_frame_start(self, buf: bytes | bytearray, offset: int) -> int:
        """从 offset 起定位可能的帧起点; 找不到返回 -1."""
        ...

    def frame_length(self, buf: bytes | bytearray, start: int) -> int | None:
        """返回整帧长度; 头部尚不完整返回 None (需要更多字节)."""
        ...

    def validate(self, frame: bytes) -> bool:
        """校验整帧 (长度/CRC 等)."""
        ...


class FrameSyntaxError(Exception):
    """帧语法错误 (长度/CRC 不合法)."""


class FrameStreamSplitter:
    """有界增量切分器: 处理半包/粘包/噪声/超长.

    - 半包: 缓存等待更多字节;
    - 粘包: 一次 feed 可返回多帧;
    - 噪声: 在帧起点前扫描并丢弃噪声字节 (计数);
    - 超长: 超过 max_frame/max_buffer 时丢弃并计数, 不无界积累.
    """

    def __init__(self, profile: WireProfile, *, max_buffer_bytes: int = 4096) -> None:
        self._profile = profile
        self._buf = bytearray()
        self._max_buffer = max(max_buffer_bytes, profile.max_frame_bytes + 1)
        self.dropped_bytes = 0
        self.overlong_count = 0
        self.crc_errors = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buf)

    @property
    def max_buffer_bytes(self) -> int:
        return self._max_buffer

    def feed(self, data: bytes) -> list[bytes]:
        """喂入字节, 返回本次解析出的完整合法帧列表."""
        if data:
            self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            start = self._profile.find_frame_start(self._buf, 0)
            if start < 0:
                # 无帧起点: 保留可能的残余 SOF 前缀, 其余丢弃.
                self._trim_noise()
                break
            if start > 0:
                self.dropped_bytes += start
                del self._buf[:start]
            length = self._profile.frame_length(self._buf, 0)
            if length is None:
                break  # 半包, 等待更多
            if length > self._profile.max_frame_bytes or length > self._max_buffer:
                # 超长: 丢弃 1 字节重新扫描, 保证前进.
                self.overlong_count += 1
                self.dropped_bytes += 1
                del self._buf[:1]
                continue
            if len(self._buf) < length:
                break  # 半包
            frame = bytes(self._buf[:length])
            if not self._profile.validate(frame):
                # 校验失败: 偏移 1 字节重新扫描 (CRC 失败恢复).
                self.crc_errors += 1
                self.dropped_bytes += 1
                del self._buf[:1]
                continue
            frames.append(frame)
            del self._buf[:length]
        self._enforce_bound()
        return frames

    def reset(self) -> None:
        self._buf.clear()

    def _trim_noise(self) -> None:
        # 若缓冲超过上限且仍无完整帧, 丢弃前面的字节避免无界增长.
        if len(self._buf) > self._max_buffer:
            drop = len(self._buf) - self._max_buffer
            self.dropped_bytes += drop
            del self._buf[:drop]

    def _enforce_bound(self) -> None:
        if len(self._buf) > self._max_buffer:
            drop = len(self._buf) - self._max_buffer
            self.dropped_bytes += drop
            del self._buf[:drop]


__all__ = [
    "FrameStreamSplitter", "FrameSyntaxError", "WireProfile", "crc16_modbus",
]
