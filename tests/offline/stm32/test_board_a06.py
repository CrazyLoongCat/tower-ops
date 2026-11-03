"""A06 Link/Board 会话与 CommandTracker 测试 (T02/T03/T04/T29 离线)."""
from __future__ import annotations

import pytest

from application.adapters.stm32.board import BoardSession, FirmwareSemantics
from application.adapters.stm32.codec import FakeFrameCodec
from application.adapters.stm32.registry import Stm32Registry
from application.adapters.stm32.session import LinkSession
from application.adapters.stm32.transport import LoopbackTransport
from application.contracts.enums import CommandState

SEM = FirmwareSemantics(command_type=0x10, ack_type=0x11, done_type=0x12,
                        error_type=0x13, stop_type=0x14, is_fake=True)
ADDR = 0x01


def _setup(*, addresses=(ADDR,)):
    a, b = LoopbackTransport.pair()
    a.open(); b.open()
    codec = FakeFrameCodec()
    link = LinkSession("link0", a, codec, max_frames_per_flush=8)
    link.open()
    boards = []
    reg = Stm32Registry()
    reg.add_link(link)
    for i, addr in enumerate(addresses):
        bs = BoardSession(f"stm32_{i+1:02d}", addr, link, SEM)
        reg.add_board(bs, "link0")
        boards.append(bs)
    return reg, link, boards, a, b, codec


def _reply(codec, addr, ftype, cid, code=0x00):
    return codec.encode({"address": addr, "frame_type": ftype,
                         "command_id": cid, "payload": bytes([code])})


def test_write_success_is_not_completion():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    h = bs.execute_action("clamp")
    assert h.state is CommandState.QUEUED   # 仅入队, 未写
    reg.dispatch()                           # 写出命令帧
    assert h.state is CommandState.SENT      # 已写 != 完成
    assert not h.is_success


def test_ack_then_done_completes():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    h = bs.execute_action("clamp")
    wid = bs.tracker.wire_id_of(h)
    a.inject_received(_reply(codec, ADDR, SEM.ack_type, wid))
    reg.dispatch()
    assert h.state is CommandState.ACCEPTED
    a.inject_received(_reply(codec, ADDR, SEM.done_type, wid, 0x00))
    reg.dispatch()
    assert h.state is CommandState.COMPLETED and h.is_success


def test_done_before_ack_allowed_with_note():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    h = bs.execute_action("clamp")
    wid = bs.tracker.wire_id_of(h)
    a.inject_received(_reply(codec, ADDR, SEM.done_type, wid, 0x00))
    reg.dispatch()
    assert h.state is CommandState.COMPLETED
    assert any("缺 ACK" in d for d in h.diagnostics)


def test_ack_reject_code():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    h = bs.execute_action("clamp")
    wid = bs.tracker.wire_id_of(h)
    a.inject_received(_reply(codec, ADDR, SEM.ack_type, wid, SEM.reject_code))
    reg.dispatch()
    assert h.state is CommandState.REJECTED


def test_stale_done_does_not_complete_new_action():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    h1 = bs.execute_action("clamp")
    w1 = bs.tracker.wire_id_of(h1)
    a.inject_received(_reply(codec, ADDR, SEM.done_type, w1, 0x00))
    reg.dispatch()
    assert h1.state is CommandState.COMPLETED
    # 新动作使用不同 ID
    h2 = bs.execute_action("release")
    w2 = bs.tracker.wire_id_of(h2)
    assert w2 != w1
    # 旧的 DONE (w1) 迟到, 不能完成 h2
    a.inject_received(_reply(codec, ADDR, SEM.done_type, w1, 0x00))
    reg.dispatch()
    assert h2.state is CommandState.SENT
    assert any("迟到" in d or "重复" in d for d in bs.tracker.diagnostics)


def test_duplicate_done_idempotent():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    h = bs.execute_action("clamp")
    wid = bs.tracker.wire_id_of(h)
    a.inject_received(_reply(codec, ADDR, SEM.done_type, wid))
    reg.dispatch()
    a.inject_received(_reply(codec, ADDR, SEM.done_type, wid))
    reg.dispatch()
    assert h.state is CommandState.COMPLETED
    assert len([d for d in bs.tracker.diagnostics if "重复" in d]) >= 1


def test_unknown_id_ignored():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    bs.execute_action("clamp")
    a.inject_received(_reply(codec, ADDR, SEM.done_type, 0x7777))
    reg.dispatch()
    assert bs.tracker.pending_count == 1  # 未决命令未被伪造 DONE 完成
    assert any("未知" in d or "旧" in d for d in bs.tracker.diagnostics)


def test_timeout_unknown_no_resend():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    h = bs.execute_action("clamp")
    sent_before = link.sent_count
    out = bs.check_timeouts(now=h.created_monotonic_s + 100.0)
    assert h in out and h.state is CommandState.UNKNOWN
    assert bs.tracker.pending_count == 0
    # 不自动重发
    reg.dispatch()
    assert link.sent_count == sent_before


def test_wrong_board_ack_not_applied():
    reg, link, boards, a, b, codec = _setup(addresses=(0x01, 0x02))
    bs1, bs2 = boards
    h1 = bs1.execute_action("clamp")
    w1 = bs1.tracker.wire_id_of(h1)
    # ACK 地址属于板2, 不应完成板1
    a.inject_received(_reply(codec, 0x02, SEM.ack_type, w1))
    reg.dispatch()
    assert h1.state is CommandState.SENT
    assert reg.unrouted == []  # 板2 存在, 不算未路由


def test_unknown_address_recorded():
    reg, link, boards, a, b, codec = _setup()
    a.inject_received(_reply(codec, 0x99, SEM.ack_type, 1))
    reg.dispatch()
    assert reg.unrouted == ["link0:153"]


def test_stop_priority_ahead_of_normal():
    a, b = LoopbackTransport.pair()
    a.open(); b.open()
    link = LinkSession("lk", a, FakeFrameCodec(), max_frames_per_flush=8)
    link.open()
    for _ in range(5):
        link.send(b"NORMAL")
    link.send_stop(b"STOP!")
    link.flush_outgoing()
    # 停止字节最先到达对端
    received = b.read_available()
    assert received.startswith(b"STOP!")


def test_reconnect_baseline_marks_unknown_and_drops_queue():
    reg, link, boards, a, b, codec = _setup()
    bs = boards[0]
    h = bs.execute_action("clamp")
    epoch0 = link.epoch
    link.send(b"PENDING")
    unknown = bs.tracker.on_reconnect_baseline()
    link.on_reconnect()
    assert h in unknown and h.state is CommandState.UNKNOWN
    assert link.pending_normal == 0
    assert link.epoch > epoch0


def test_stop_when_closed_rejected():
    reg, link, boards, a, b, codec = _setup()
    link.close()
    h = boards[0].execute_action("clamp")
    assert h.state is CommandState.FAILED
