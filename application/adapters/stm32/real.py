"""真实固件 Codec 与 profile (A21 离线实现; 真实收发/动作在 A25).

- FirmwareProfile 描述真实帧布局 (以版本化 ICD/抓包为输入), 不凭设计添加帧头字段;
- ProfileCodec 实现 FirmwareCodecPort: 增量 feed + 有界缓存;
- 未确认 profile 标记 confirmed=False, 不得用于真实生产.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from application.adapters.stm32.codec import FirmwareFrame
from application.adapters.stm32.framing import FrameStreamSplitter, crc16_modbus


@dataclass
class FirmwareProfile:
    """真实帧布局参数. 默认字段仅为占位, 未被 ICD 确认前 confirmed=False."""

    profile_version: str
    sof: bytes = b""
    address_offset: int = 0
    address_len: int = 1
    type_offset: int = 1
    type_len: int = 1
    command_id_offset: int = 2
    command_id_len: int = 2
    command_id_byteorder: str = "little"
    length_offset: int | None = 4
    length_len: int = 1
    payload_offset: int = 5
    crc_len: int = 2
    crc_byteorder: str = "little"
    max_frame_bytes: int = 260
    confirmed: bool = False
    is_fake: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.confirmed and not self.is_fake:
            # 明确记录风险, 不静默当真实使用
            if not self.notes:
                self.notes = "未确认 ICD; 不得用于真实生产收发"

    # ---- WireProfile 接口 ----
    def find_frame_start(self, buf: bytes | bytearray, offset: int) -> int:
        if self.sof:
            return bytes(buf).find(self.sof, offset)
        return offset if offset < len(buf) else -1

    def frame_length(self, buf: bytes | bytearray, start: int) -> int | None:
        if self.length_offset is None:
            return None  # 无显式长度字段则需靠定长/其他规则 (待 ICD)
        off = start + self.length_offset
        if len(buf) < off + self.length_len:
            return None
        payload_len = int.from_bytes(bytes(buf[off:off + self.length_len]), "little")
        return self.payload_offset + payload_len + self.crc_len

    def validate(self, frame: bytes) -> bool:
        if self.crc_len == 0:
            return True
        if len(frame) < self.payload_offset + self.crc_len:
            return False
        body = frame[:-self.crc_len]
        crc = int.from_bytes(frame[-self.crc_len:], self.crc_byteorder)
        return crc == crc16_modbus(body)

    def decode(self, frame: bytes) -> FirmwareFrame:
        if not self.validate(frame):
            raise ValueError("CRC 校验失败")
        addr = int.from_bytes(
            frame[self.address_offset:self.address_offset + self.address_len], "big")
        ftype = int.from_bytes(
            frame[self.type_offset:self.type_offset + self.type_len], "big")
        cid = None
        if self.command_id_len:
            cid = int.from_bytes(
                frame[self.command_id_offset:self.command_id_offset + self.command_id_len],
                self.command_id_byteorder)
        payload_len = 0
        if self.length_offset is not None:
            payload_len = int.from_bytes(
                frame[self.length_offset:self.length_offset + self.length_len], "little")
        payload = frame[self.payload_offset:self.payload_offset + payload_len]
        return FirmwareFrame(address=addr, frame_type=ftype, command_id=cid,
                             payload=payload, raw=frame)

    def encode(self, *, address: int, frame_type: int, command_id: int | None,
               payload: bytes = b"") -> bytes:
        out = bytearray()
        if self.sof:
            out += self.sof
        # 头部字段按"相对帧起点的绝对 offset"填充; sof 已占 len(sof) 字节.
        head_len = self.payload_offset - len(self.sof)
        if head_len < 0:
            raise ValueError("payload_offset 小于帧头(sof)长度")
        head = bytearray(head_len)
        base = len(self.sof)  # sof 之后, head 内索引 = 绝对offset - base

        def _put(off: int, value: bytes) -> None:
            i = off - base
            head[i:i + len(value)] = value

        _put(self.address_offset, (address & 0xFF).to_bytes(self.address_len, "big"))
        _put(self.type_offset, (int(frame_type) & 0xFF).to_bytes(self.type_len, "big"))
        if self.command_id_len:
            _put(self.command_id_offset,
                 int(command_id or 0).to_bytes(self.command_id_len,
                                               self.command_id_byteorder))
        if self.length_offset is not None:
            _put(self.length_offset, len(payload).to_bytes(self.length_len, "little"))
        out += head
        out += payload
        if self.crc_len:
            out += crc16_modbus(bytes(out)).to_bytes(self.crc_len, self.crc_byteorder)
        return bytes(out)


class ProfileCodec:
    """FirmwareCodecPort 实现: 按 FirmwareProfile 解码/编码."""

    def __init__(self, profile: FirmwareProfile) -> None:
        self._profile = profile
        self._splitter = FrameStreamSplitter(profile, max_buffer_bytes=4096)

    @property
    def profile_version(self) -> str:
        return self._profile.profile_version

    @property
    def is_fake(self) -> bool:
        return False

    @property
    def confirmed(self) -> bool:
        return self._profile.confirmed

    def encode(self, command: Any) -> bytes:
        return self._profile.encode(**command)

    def feed(self, data: bytes) -> list[FirmwareFrame]:
        return [self._profile.decode(f) for f in self._splitter.feed(data)]


def load_firmware_profile(data: dict) -> FirmwareProfile:
    """从 ICD/配置加载 profile. 未提供 confirmed 时视为未确认."""
    known = {f for f in FirmwareProfile.__dataclass_fields__}  # type: ignore[attr-defined]
    kwargs = {k: v for k, v in data.items() if k in known}
    if isinstance(kwargs.get("sof"), str):
        kwargs["sof"] = bytes.fromhex(kwargs["sof"])
    return FirmwareProfile(**kwargs)


__all__ = ["FirmwareProfile", "ProfileCodec", "load_firmware_profile"]
