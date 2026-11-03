"""字节传输实现 (12 §4.3, 02 §4.1).

ByteTransport 只管"把字节发到物理链路", 不含帧语义.
- LoopbackTransport / ScriptedTransport: 离线测试用, 不打开真实串口;
- FaultInjectingTransport: 包装器, 用于离线故障注入;
- SerialTransport: pyserial, 惰性导入, 无 pyserial 时明确报错 (不静默回退).
"""
from __future__ import annotations

from collections import deque
from typing import Callable


class ByteTransport:
    """字节传输基类. 单所有者读写 (每物理链路一个)."""

    def open(self) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def write(self, data: bytes) -> bool:  # pragma: no cover - 抽象
        raise NotImplementedError

    def read_available(self) -> bytes:  # pragma: no cover - 抽象
        raise NotImplementedError

    @property
    def is_open(self) -> bool:  # pragma: no cover - 抽象
        raise NotImplementedError


class LoopbackTransport(ByteTransport):
    """内存回环: 两个端点互通, 用于离线联调 LinkSession/Codec, 不接触真实串口."""

    def __init__(self) -> None:
        self._open = False
        self._inbox: bytearray = bytearray()
        self._peer: LoopbackTransport | None = None
        self.written: list[bytes] = []
        self.fail_writes = False

    @classmethod
    def pair(cls) -> tuple["LoopbackTransport", "LoopbackTransport"]:
        a, b = cls(), cls()
        a._peer = b
        b._peer = a
        return a, b

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> bool:
        if not self._open or self.fail_writes:
            return False
        self.written.append(bytes(data))
        if self._peer is not None:
            self._peer._inbox.extend(data)
        return True

    def inject_received(self, data: bytes) -> None:
        """测试辅助: 直接向本端点注入"收到的"字节."""
        self._inbox.extend(data)

    def read_available(self) -> bytes:
        if not self._inbox:
            return b""
        out = bytes(self._inbox)
        self._inbox.clear()
        return out

    @property
    def is_open(self) -> bool:
        return self._open


class ScriptedTransport(ByteTransport):
    """按脚本应答的传输: write 触发预置响应, 便于离线契约测试."""

    def __init__(self, on_write: Callable[[bytes], list[bytes]] | None = None) -> None:
        self._open = False
        self._inbox: deque[bytes] = deque()
        self._on_write = on_write
        self.written: list[bytes] = []

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> bool:
        if not self._open:
            return False
        self.written.append(bytes(data))
        if self._on_write is not None:
            for chunk in self._on_write(bytes(data)):
                self._inbox.append(chunk)
        return True

    def queue_incoming(self, data: bytes) -> None:
        self._inbox.append(bytes(data))

    def read_available(self) -> bytes:
        if not self._inbox:
            return b""
        return b"".join(self._inbox.popleft() for _ in range(len(self._inbox)))

    @property
    def is_open(self) -> bool:
        return self._open


class FaultInjectingTransport(ByteTransport):
    """包装器: 离线注入写失败/断连/丢弃接收, 不改动被包装对象代码."""

    def __init__(self, inner: ByteTransport) -> None:
        self._inner = inner
        self.fail_writes = False
        self.drop_reads = False
        self.raise_on_write = False

    def open(self) -> None:
        self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def write(self, data: bytes) -> bool:
        if self.raise_on_write:
            raise ConnectionError("注入: 写异常")
        if self.fail_writes:
            return False
        return self._inner.write(data)

    def read_available(self) -> bytes:
        if self.drop_reads:
            self._inner.read_available()
            return b""
        return self._inner.read_available()

    @property
    def is_open(self) -> bool:
        return self._inner.is_open


class SerialTransport(ByteTransport):
    """pyserial 串口传输 (真实). 未安装 pyserial 时明确拒绝, 不静默回退.

    真实接线/端口/波特率等在 A21/A24 与现场 D1 确认后使用; 本类不在离线测试打开真实串口.
    """

    def __init__(self, port: str, baudrate: int = 115200,
                 bytesize: int = 8, parity: str = "N", stopbits: int = 1,
                 read_timeout_s: float = 0.05) -> None:
        self._port = port
        self._baud = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._read_timeout = read_timeout_s
        self._serial = None

    def open(self) -> None:
        try:
            import serial  # pyserial
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise RuntimeError(
                "未安装 pyserial, 无法打开真实串口; 离线请使用 LoopbackTransport"
            ) from exc
        self._serial = serial.Serial(
            self._port, self._baud, bytesize=self._bytesize,
            parity=self._parity, stopbits=self._stopbits,
            timeout=self._read_timeout,
        )

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def write(self, data: bytes) -> bool:
        if self._serial is None:
            return False
        self._serial.write(data)
        return True

    def read_available(self) -> bytes:
        if self._serial is None:
            return b""
        n = self._serial.in_waiting
        return self._serial.read(n) if n else b""

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open


__all__ = [
    "ByteTransport", "FaultInjectingTransport", "LoopbackTransport",
    "ScriptedTransport", "SerialTransport",
]
