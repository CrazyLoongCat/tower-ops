"""A05 帧切分/Codec/传输测试 (T01 离线边界用例)."""
from __future__ import annotations

import pytest

from application.adapters.stm32.codec import (
    FakeFrameCodec,
    FakeFrameType,
    FakeProfile,
    FirmwareFrame,
)
from application.adapters.stm32.framing import (
    FrameStreamSplitter,
    crc16_modbus,
)
from application.adapters.stm32.transport import (
    FaultInjectingTransport,
    LoopbackTransport,
    ScriptedTransport,
)


def _frame(address=0x01, ftype=FakeFrameType.ACK, cid=7, payload=b"\x01\x02"):
    return FakeProfile().encode(address=address, frame_type=ftype,
                                command_id=cid, payload=payload)


def test_crc16_reference_vector():
    # Modbus CRC16 标准向量: "123456789" -> 0x4B37
    assert crc16_modbus(b"123456789") == 0x4B37


class TestSplitter:
    def test_half_packet_waits(self):
        sp = FrameStreamSplitter(FakeProfile())
        f = _frame()
        mid = len(f) // 2
        assert sp.feed(f[:mid]) == []
        assert sp.buffered_bytes == mid
        out = sp.feed(f[mid:])
        assert len(out) == 1 and out[0] == f

    def test_sticky_packet_two_frames(self):
        sp = FrameStreamSplitter(FakeProfile())
        out = sp.feed(_frame(cid=1) + _frame(cid=2))
        assert len(out) == 2

    def test_byte_by_byte(self):
        sp = FrameStreamSplitter(FakeProfile())
        f = _frame()
        got = []
        for b in f:
            got += sp.feed(bytes([b]))
        assert got == [f]

    def test_noise_recovery(self):
        sp = FrameStreamSplitter(FakeProfile())
        out = sp.feed(b"\x00\xff\x13" + _frame())
        assert len(out) == 1
        assert sp.dropped_bytes >= 3

    def test_crc_error_resync(self):
        sp = FrameStreamSplitter(FakeProfile())
        f = bytearray(_frame())
        f[-1] ^= 0xFF  # 破坏 CRC
        out = sp.feed(bytes(f) + _frame())
        assert len(out) == 1  # 只解出后面那帧
        assert sp.crc_errors >= 1

    def test_overlong_garbage_bounded(self):
        sp = FrameStreamSplitter(FakeProfile(), max_buffer_bytes=256)
        # 构造声明超长 length 的帧头 + 大量数据
        body = bytearray(b"\xAA\x55")
        body += bytes([0x01, 0x20, 0x00, 0x00, 240])  # length=240 (合法上限)
        sp.feed(bytes(body) + b"\x00" * 4000)
        assert sp.buffered_bytes <= sp.max_buffer_bytes

    def test_pure_noise_bounded(self):
        sp = FrameStreamSplitter(FakeProfile(), max_buffer_bytes=128)
        for _ in range(50):
            sp.feed(b"\x00" * 100)
        assert sp.buffered_bytes <= sp.max_buffer_bytes


class TestFakeCodec:
    def test_encode_decode_roundtrip(self):
        codec = FakeFrameCodec()
        raw = codec.encode({"address": 3, "frame_type": FakeFrameType.DONE,
                            "command_id": 9, "payload": b"\xAA"})
        frames = codec.feed(raw)
        assert len(frames) == 1
        fr = frames[0]
        assert isinstance(fr, FirmwareFrame)
        assert fr.address == 3 and fr.frame_type == FakeFrameType.DONE
        assert fr.command_id == 9 and fr.payload == b"\xAA"

    def test_fake_marked(self):
        codec = FakeFrameCodec()
        assert codec.is_fake is True
        assert codec.profile_version.startswith("fake")

    def test_payload_over_limit_rejected(self):
        with pytest.raises(ValueError):
            FakeProfile(max_payload=4).encode(address=1, frame_type=1,
                                             payload=b"12345")


class TestTransports:
    def test_loopback_pair(self):
        a, b = LoopbackTransport.pair()
        a.open(); b.open()
        assert a.write(b"hello")
        assert b.read_available() == b"hello"
        assert b.write(b"world")
        assert a.read_available() == b"world"

    def test_write_when_closed_fails(self):
        t = LoopbackTransport()
        assert t.write(b"x") is False

    def test_scripted_transport(self):
        t = ScriptedTransport(on_write=lambda d: [b"RESP:" + d])
        t.open()
        assert t.write(b"Q")
        assert t.read_available() == b"RESP:Q"

    def test_fault_injection(self):
        inner = LoopbackTransport(); inner.open()
        f = FaultInjectingTransport(inner)
        f.fail_writes = True
        assert f.write(b"x") is False
        f.fail_writes = False
        f.raise_on_write = True
        with pytest.raises(ConnectionError):
            f.write(b"y")
