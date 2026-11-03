"""固件帧 Codec 接口与 Fake 实现 (12 §4.3, 02 §4.2).

FakeProfile/FakeFrameCodec 只服务离线测试 (T01 结构用例). 它不是金样本,
不能作为真实固件布局依据; 真实 Codec 见 A21 (以 ICD/抓包为输入).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from application.adapters.stm32.framing import (
    FrameStreamSplitter,
    WireProfile,
    crc16_modbus,
)


class FakeFrameType(enum.IntEnum):
    """Fake 帧类型. 与真实固件功能码无关."""

    COMMAND = 0x10
    ACK = 0x11
    DONE = 0x12
    ERROR = 0x13
    TELEMETRY = 0x20


@dataclass
class FirmwareFrame:
    """解析后的固件帧 (地址/类型/命令ID/载荷 + 原始字节)."""

    address: int
    frame_type: int
    command_id: int | None
    payload: bytes
    raw: bytes = b""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fake 线路布局 (仅离线). 布局为项目自定义的测试协议, 非厂家 ICD.
# ---------------------------------------------------------------------------
_FAKE_SOF = b"\xAA\x55"
_FAKE_HEADER = len(_FAKE_SOF) + 1 + 1 + 2 + 1  # sof + addr + type + cmd_id + len
_FAKE_CRC = 2


class FakeProfile(WireProfile):
    """离线测试用假协议布局 (必须显式标注 is_fake=True)."""

    is_fake = True

    def __init__(self, *, max_payload: int = 240) -> None:
        self._max_payload = max_payload

    @property
    def max_frame_bytes(self) -> int:
        return _FAKE_HEADER + self._max_payload + _FAKE_CRC

    def find_frame_start(self, buf: bytes | bytearray, offset: int) -> int:
        idx = bytes(buf).find(_FAKE_SOF, offset)
        return idx

    def frame_length(self, buf: bytes | bytearray, start: int) -> int | None:
        if len(buf) - start < _FAKE_HEADER:
            return None
        length_byte = buf[start + len(_FAKE_SOF) + 4]
        return _FAKE_HEADER + length_byte + _FAKE_CRC

    def validate(self, frame: bytes) -> bool:
        if len(frame) < _FAKE_HEADER + _FAKE_CRC:
            return False
        body = frame[:-2]
        crc = int.from_bytes(frame[-2:], "little")
        return crc == crc16_modbus(body)

    # ----- 编码 -----
    def encode(
        self,
        *,
        address: int,
        frame_type: int,
        command_id: int | None = 0,
        payload: bytes = b"",
    ) -> bytes:
        if len(payload) > self._max_payload:
            raise ValueError("载荷超过假协议上限")
        cid = 0 if command_id is None else command_id
        body = bytearray(_FAKE_SOF)
        body.append(address & 0xFF)
        body.append(int(frame_type) & 0xFF)
        body += cid.to_bytes(2, "little")
        body.append(len(payload))
        body += payload
        crc = crc16_modbus(bytes(body))
        body += crc.to_bytes(2, "little")
        return bytes(body)

    # ----- 解码 -----
    def decode(self, frame: bytes) -> FirmwareFrame:
        if not self.validate(frame):
            raise ValueError("假协议帧 CRC/长度校验失败")
        off = len(_FAKE_SOF)
        address = frame[off]
        frame_type = frame[off + 1]
        command_id = int.from_bytes(frame[off + 2:off + 4], "little")
        length = frame[off + 4]
        payload = frame[off + 5:off + 5 + length]
        return FirmwareFrame(
            address=address, frame_type=frame_type,
            command_id=command_id, payload=bytes(payload), raw=frame,
        )


class FakeFrameCodec:
    """FirmwareCodecPort 的离线实现: 增量 feed + 有界缓存."""

    def __init__(self, profile: FakeProfile | None = None) -> None:
        self._profile = profile or FakeProfile()
        self._splitter = FrameStreamSplitter(self._profile, max_buffer_bytes=4096)

    @property
    def profile_version(self) -> str:
        return "fake-v1-offline"

    @property
    def is_fake(self) -> bool:
        return True

    @property
    def splitter(self) -> FrameStreamSplitter:
        return self._splitter

    def encode(self, command: Any) -> bytes:
        """command: dict(address, frame_type, command_id, payload) 或 FirmwareFrame."""
        if isinstance(command, FirmwareFrame):
            return self._profile.encode(
                address=command.address, frame_type=command.frame_type,
                command_id=command.command_id, payload=command.payload,
            )
        return self._profile.encode(**command)

    def feed(self, data: bytes) -> list[FirmwareFrame]:
        return [self._profile.decode(f) for f in self._splitter.feed(data)]


__all__ = [
    "FakeFrameCodec", "FakeFrameType", "FakeProfile", "FirmwareFrame",
]
