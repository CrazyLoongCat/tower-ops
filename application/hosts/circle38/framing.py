"""长度前缀消息分帧 (12 §4.3, 02 §8.3, T21).

消息 = header_len(4B BE) + header_json(UTF-8) + payload(原始字节).
header 与 payload 都有上限; 半包增量缓存有界; 超限明确报错.
"""
from __future__ import annotations

import json
import struct

_HEADER_LEN_FMT = ">I"
_HEADER_LEN_BYTES = 4

# 报文上限 (02 §7 冻结前的保守值; 真实上限在 E05/A03 确认).
MAX_HEADER_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024


class FramingError(Exception):
    """分帧错误 (超限/结构非法)."""


def encode_message(header: dict, payload: bytes = b"") -> bytes:
    header_bytes = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise FramingError(f"头超过上限 {MAX_HEADER_BYTES}")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise FramingError(f"载荷超过上限 {MAX_PAYLOAD_BYTES}")
    return struct.pack(_HEADER_LEN_FMT, len(header_bytes)) + header_bytes + payload


class MessageStreamDecoder:
    """增量解码器: feed 半包/粘包, 返回完整 (header, payload) 列表; 缓存有界."""

    def __init__(self, *, max_buffer: int | None = None) -> None:
        self._buf = bytearray()
        self._max_buffer = max_buffer if max_buffer is not None else (
            _HEADER_LEN_BYTES + MAX_HEADER_BYTES + MAX_PAYLOAD_BYTES)

    @property
    def buffered_bytes(self) -> int:
        return len(self._buf)

    def feed(self, data: bytes) -> list[tuple[dict, bytes]]:
        self._buf.extend(data)
        out: list[tuple[dict, bytes]] = []
        while True:
            if len(self._buf) < _HEADER_LEN_BYTES:
                break
            (header_len,) = struct.unpack(_HEADER_LEN_FMT, self._buf[:_HEADER_LEN_BYTES])
            if header_len > MAX_HEADER_BYTES:
                raise FramingError(f"声明头长度 {header_len} 超过上限")
            total = _HEADER_LEN_BYTES + header_len
            if len(self._buf) < total:
                break
            header = json.loads(bytes(self._buf[_HEADER_LEN_BYTES:total]).decode("utf-8"))
            payload_len = int(header.get("_payload_bytes", 0))
            if payload_len > MAX_PAYLOAD_BYTES:
                raise FramingError(f"声明载荷长度 {payload_len} 超过上限")
            end = total + payload_len
            if len(self._buf) < end:
                break
            payload = bytes(self._buf[total:end])
            del self._buf[:end]
            out.append((header, payload))
        if len(self._buf) > self._max_buffer:
            raise FramingError("解码缓存超过上限 (无界积累)")
        return out


__all__ = [
    "MAX_HEADER_BYTES", "MAX_PAYLOAD_BYTES", "FramingError",
    "MessageStreamDecoder", "encode_message",
]
