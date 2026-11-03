"""最小 Modbus TCP 客户端（MBAP + PDU，无 CRC）。

只实现调试页用到的功能码：
- 0x03 读保持寄存器
- 0x06 写单个寄存器
- 0x10 写多个寄存器

标准 Modbus TCP 端口默认 502；`unit_id` 对应板端 ``DEV_ADDR``（默认 1）。
不使用第三方库：请求/响应都有明确长度与错误码，便于离线测试和打包。
"""
from __future__ import annotations

import socket
import struct
import threading

DEFAULT_PORT = 502
MAX_READ_REGISTERS = 125
MAX_WRITE_REGISTERS = 123

_EXCEPTION_TEXT = {
    0x01: "非法功能码",
    0x02: "非法数据地址",
    0x03: "非法数据值",
    0x04: "从站设备故障",
    0x05: "确认（处理中）",
    0x06: "从站忙",
    0x07: "存储奇偶校验错误",
    0x08: "网关路径不可用",
    0x0A: "网关目标无响应",
    0x0B: "网关目标响应失败",
}


class ModbusTcpError(RuntimeError):
    """Modbus TCP 通信/协议/异常响应错误。"""


class ModbusTcpTimeout(ModbusTcpError):
    """请求超时（未收到完整响应）。"""


class ModbusTcpClient:
    """单连接、串行请求的 Modbus TCP 客户端（线程安全）。"""

    def __init__(self, host: str, port: int = DEFAULT_PORT, unit_id: int = 1,
                 timeout_s: float = 1.0) -> None:
        self.host = (host or "").strip() or "127.0.0.1"
        self.port = int(port or DEFAULT_PORT)
        if not (1 <= self.port <= 65535):
            raise ValueError(f"端口越界: {self.port}")
        self.unit_id = int(unit_id or 1)
        if not (1 <= self.unit_id <= 247):
            raise ValueError(f"从站地址越界（1~247）: {self.unit_id}")
        self.timeout_s = max(0.05, float(timeout_s or 1.0))
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self._transaction_id = 0
        self.last_error: str | None = None

    # ---------- 连接 ----------
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        with self._lock:
            self.close()
            try:
                sock = socket.create_connection((self.host, self.port),
                                                timeout=self.timeout_s)
            except OSError as exc:
                raise ModbusTcpError(
                    f"连接失败 {self.host}:{self.port}：{exc}") from exc
            sock.settimeout(self.timeout_s)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self._sock = sock
            self.last_error = None

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    # ---------- 功能码 ----------
    def read_holding(self, address: int, count: int) -> list[int]:
        if not (1 <= count <= MAX_READ_REGISTERS):
            raise ValueError(f"读取数量越界（1~{MAX_READ_REGISTERS}）: {count}")
        pdu = struct.pack(">BHH", 0x03, int(address), int(count))
        body = self._request(pdu)
        if len(body) < 1:
            raise ModbusTcpError("读寄存器响应为空")
        byte_count = body[0]
        if byte_count != count * 2 or len(body) < 1 + byte_count:
            raise ModbusTcpError(
                f"读寄存器响应长度不符：期望 {count * 2} 字节，得到 {byte_count}")
        return list(struct.unpack(f">{count}H", body[1:1 + byte_count]))

    def write_register(self, address: int, value: int) -> None:
        pdu = struct.pack(">BHH", 0x06, int(address), int(value) & 0xFFFF)
        body = self._request(pdu)
        if len(body) < 4:
            raise ModbusTcpError("写单寄存器响应过短")

    def write_registers(self, address: int, values: list[int]) -> None:
        if not (1 <= len(values) <= MAX_WRITE_REGISTERS):
            raise ValueError(f"写入数量越界（1~{MAX_WRITE_REGISTERS}）: {len(values)}")
        payload = b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in values)
        pdu = struct.pack(">BHHB", 0x10, int(address), len(values),
                          len(values) * 2) + payload
        body = self._request(pdu)
        if len(body) < 4:
            raise ModbusTcpError("写多寄存器响应过短")

    # ---------- MBAP ----------
    def _next_transaction_id(self) -> int:
        self._transaction_id = (self._transaction_id + 1) & 0xFFFF
        if self._transaction_id == 0:
            self._transaction_id = 1
        return self._transaction_id

    def _request(self, pdu: bytes) -> bytes:
        with self._lock:
            sock = self._sock
            if sock is None:
                raise ModbusTcpError("未连接夹爪板")
            tid = self._next_transaction_id()
            frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, self.unit_id) + pdu
            try:
                sock.sendall(frame)
                header = self._recv_exact(sock, 7)
                r_tid, proto, length, unit = struct.unpack(">HHHB", header)
                if proto != 0:
                    raise ModbusTcpError(f"响应协议号非 0：{proto}")
                if length < 2:
                    raise ModbusTcpError(f"响应长度非法：{length}")
                body = self._recv_exact(sock, length - 1)
            except socket.timeout as exc:
                self.last_error = f"超时（{self.host}:{self.port}）"
                self.close()
                raise ModbusTcpTimeout(
                    f"Modbus TCP 超时：{self.host}:{self.port} "
                    f"unit={self.unit_id}（{self.timeout_s:.1f}s）") from exc
            except OSError as exc:
                self.last_error = f"通信失败：{exc}"
                self.close()
                raise ModbusTcpError(
                    f"Modbus TCP 通信失败 {self.host}:{self.port}：{exc}") from exc

            if r_tid != tid:
                raise ModbusTcpError(
                    f"事务号不匹配：期望 {tid}，收到 {r_tid}")
            if body and (body[0] & 0x80):
                code = body[1] if len(body) > 1 else 0
                text = _EXCEPTION_TEXT.get(code, "未知异常")
                raise ModbusTcpError(
                    f"Modbus 异常响应 0x{code:02X}：{text}")
            return body[1:] if body else b""

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise ModbusTcpError("连接被对端关闭")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


__all__ = [
    "DEFAULT_PORT", "MAX_READ_REGISTERS", "MAX_WRITE_REGISTERS",
    "ModbusTcpClient", "ModbusTcpError", "ModbusTcpTimeout",
]
