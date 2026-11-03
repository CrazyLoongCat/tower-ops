"""夹爪板 Modbus TCP 客户端测试（DEC-097）。

用本机假 Modbus TCP 服务验证 MBAP、03/06/16、异常码与超时；不连真实板。
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from application.adapters.gripper_board.modbus_tcp import (
    ModbusTcpClient, ModbusTcpError, ModbusTcpTimeout,
)


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = conn.recv(remaining)
        if not chunk:
            raise ConnectionError("closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class FakeModbusServer(threading.Thread):
    """最小 Modbus TCP 服务：03/06/16；可指定某地址返回异常 02。"""

    def __init__(self, registers: dict[int, int], *,
                 exception_address: int | None = None,
                 timeout_reply: bool = False) -> None:
        super().__init__(daemon=True)
        self.registers = dict(registers)
        self.exception_address = exception_address
        self.timeout_reply = timeout_reply
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[bytes] = []

    def run(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            while True:
                try:
                    header = _recv_exact(conn, 7)
                except Exception:
                    return
                tid, _pid, length, _unit = struct.unpack(">HHHB", header)
                try:
                    pdu = _recv_exact(conn, length - 1)
                except Exception:
                    return
                self.received.append(pdu)
                if self.timeout_reply:
                    time.sleep(0.5)
                    continue
                reply = self._handle(pdu)
                conn.sendall(struct.pack(">HHHB", tid, 0, len(reply) + 1, 1) + reply)

    def _handle(self, pdu: bytes) -> bytes:
        fc = pdu[0]
        if fc == 0x03:
            addr, count = struct.unpack(">HH", pdu[1:5])
            if self.exception_address == addr:
                return bytes([fc | 0x80, 0x02])
            values = [self.registers.get(addr + i, 0) for i in range(count)]
            return bytes([0x03, count * 2]) + b"".join(
                struct.pack(">H", v & 0xFFFF) for v in values)
        if fc == 0x06:
            addr, value = struct.unpack(">HH", pdu[1:5])
            self.registers[addr] = value
            return pdu[:5]
        if fc == 0x10:
            addr, count = struct.unpack(">HH", pdu[1:5])
            values = list(struct.unpack(f">{count}H", pdu[6:6 + count * 2]))
            for i, value in enumerate(values):
                self.registers[addr + i] = value
            return struct.pack(">BHH", 0x10, addr, count)
        return bytes([fc | 0x80, 0x01])

    def stop(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def test_modbus_tcp_reads_and_writes_registers():
    server = FakeModbusServer({0x0000: 2, 0x0001: 0x0100, 0x0002: 0x0105,
                               0x0003: 1, 0x0004: 0x0009})
    server.start()
    client = ModbusTcpClient("127.0.0.1", server.port, unit_id=1, timeout_s=1.0)
    try:
        client.connect()
        assert client.read_holding(0x0000, 5) == [2, 0x0100, 0x0105, 1, 9]
        client.write_register(0x008A, 7)
        assert server.registers[0x008A] == 7
        client.write_registers(0x0801, [11, 22, 33])
        assert [server.registers[0x0801 + i] for i in range(3)] == [11, 22, 33]
    finally:
        client.close()
        server.stop()
        server.join(timeout=1)


def test_modbus_exception_response_is_reported():
    server = FakeModbusServer({}, exception_address=0x0999)
    server.start()
    client = ModbusTcpClient("127.0.0.1", server.port, timeout_s=1.0)
    try:
        client.connect()
        with pytest.raises(ModbusTcpError, match="非法数据地址"):
            client.read_holding(0x0999, 1)
    finally:
        client.close()
        server.stop()
        server.join(timeout=1)


def test_modbus_timeout_closes_connection():
    server = FakeModbusServer({}, timeout_reply=True)
    server.start()
    client = ModbusTcpClient("127.0.0.1", server.port, timeout_s=0.15)
    try:
        client.connect()
        with pytest.raises(ModbusTcpTimeout):
            client.read_holding(0x0000, 1)
        assert not client.connected
    finally:
        client.close()
        server.stop()
        server.join(timeout=1)
