"""逻辑板会话与命令账本 (12 §4.3, 02 §4.2, T02/T03/T04/T29).

物理链路与逻辑板分离: 一个 LinkSession 上可有多个 BoardSession, 按地址/身份路由.
ACK 仅接受; DONE 还需成功码、当前命令、时效及必要终态条件, 才 COMPLETED.
旧ID/错误board/重复/乱序不推进新动作; 超时 UNKNOWN 不自动重发.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from application.adapters.stm32.codec import FirmwareFrame
from application.adapters.stm32.session import LinkSession
from application.contracts.enums import CommandState
from application.contracts.handles import CommandHandle


@dataclass(frozen=True)
class FirmwareSemantics:
    """帧类型语义映射. 真实值来自 ICD; Fake 语义仅离线."""

    command_type: int
    ack_type: int
    done_type: int
    error_type: int
    stop_type: int
    is_fake: bool = True

    # 载荷约定 (可被真实 profile 覆盖): ACK/DONE 首字节为结果码.
    ok_code: int = 0x00
    reject_code: int = 0x01


class CommandTracker:
    """每板命令账本: command_id 分配/匹配/超时/回卷/重连基线."""

    ID_MOD = 0x10000  # 16 位 command_id

    def __init__(self, board_id: str, semantics: FirmwareSemantics) -> None:
        self.board_id = board_id
        self._semantics = semantics
        self._next_wire_id = 1
        self._board_epoch = 0
        self._pending: dict[int, CommandHandle] = {}
        self._issued: dict[int, CommandHandle] = {}   # 曾在本次会话发出的
        self.diagnostics: list[str] = []

    # ---------- 分配/登记 ----------
    def issue(self, action_id: str) -> CommandHandle:
        wire_id = self._next_wire_id
        self._next_wire_id = (self._next_wire_id + 1) % self.ID_MOD or 1
        handle = CommandHandle(
            command_id=f"{self.board_id}#{wire_id}#e{self._board_epoch}",
            action_ref=action_id,
            board_id=self.board_id,
        )
        handle.state = CommandState.VALIDATED
        self._pending[wire_id] = handle
        self._issued[wire_id] = handle
        return handle

    def wire_id_of(self, handle: CommandHandle) -> int:
        # command_id 形如 board#wire#epoch
        return int(handle.command_id.split("#")[1])

    def mark_queued(self, handle: CommandHandle) -> None:
        handle.state = CommandState.QUEUED

    def mark_sent(self, handle: CommandHandle) -> None:
        # 传输写成功只标 SENT, 不认为设备接受 (12 §4.2).
        if handle.state in (CommandState.QUEUED, CommandState.CREATED,
                            CommandState.VALIDATED):
            handle.state = CommandState.SENT

    def _note(self, msg: str) -> None:
        self.diagnostics.append(msg)

    # ---------- 接收匹配 ----------
    def on_frame(self, frame: FirmwareFrame) -> CommandHandle | None:
        """处理一帧, 返回受影响 handle (可能 None)."""
        sem = self._semantics
        wid = frame.command_id if frame.command_id is not None else 0
        handle = self._pending.get(wid)

        if handle is None:
            # 不在在途集合: 旧ID/迟到/未知 -> 只诊断, 不推进任何动作.
            prior = self._issued.get(wid)
            if prior is not None and prior.is_terminal:
                self._note(f"重复/迟到帧 cmd={wid} 已终态({prior.state.value})")
            else:
                self._note(f"未知/旧 command_id={wid} 帧, 丢弃控制用途")
            return None

        if frame.frame_type == sem.ack_type:
            return self._on_ack(handle, wid, frame)
        if frame.frame_type == sem.done_type:
            return self._on_done(handle, wid, frame)
        if frame.frame_type == sem.error_type:
            handle.state = CommandState.FAILED
            handle.error_reason = frame.payload.hex()
            self._retire(wid, handle)
            return handle
        # 其他类型(遥测)不在此处理.
        return None

    def _on_ack(self, handle: CommandHandle, wid: int, frame: FirmwareFrame) -> CommandHandle:
        if handle.state is CommandState.ACCEPTED:
            self._note(f"重复 ACK cmd={wid} 幂等")
            return handle
        if handle.is_terminal:
            self._note(f"迟到 ACK cmd={wid} 已 {handle.state.value}, 忽略")
            return handle
        code = frame.payload[0] if frame.payload else self._semantics.ok_code
        if code == self._semantics.reject_code:
            handle.state = CommandState.REJECTED
            handle.reject_reason = "设备拒绝"
            self._retire(wid, handle)
        else:
            handle.state = CommandState.ACCEPTED
            handle.accepted_monotonic_s = time.monotonic()
        return handle

    def _on_done(self, handle: CommandHandle, wid: int, frame: FirmwareFrame) -> CommandHandle:
        if handle.is_terminal:
            self._note(f"重复 DONE cmd={wid} 幂等(不重复下一步)")
            return handle
        ok_code = frame.payload[0] if frame.payload else self._semantics.ok_code
        absent_ack = handle.state is CommandState.SENT
        if ok_code != self._semantics.ok_code:
            handle.state = CommandState.FAILED
            handle.error_reason = f"DONE 失败码 0x{ok_code:02X}"
        else:
            # DONE 先于 ACK: 若协议允许且证据完整, 记录缺 ACK 并完成.
            handle.state = CommandState.COMPLETED
            handle.done_ok = True
            handle.completed_monotonic_s = time.monotonic()
            if absent_ack:
                handle.note("DONE 先于 ACK (协议允许, 记录缺 ACK)")
        self._retire(wid, handle)
        return handle

    def _retire(self, wid: int, handle: CommandHandle) -> None:
        self._pending.pop(wid, None)

    def retire_pending(self, wire_id: int) -> None:
        """移除未决命令 (意图持久化失败/撤销时)."""
        self._pending.pop(wire_id, None)

    # ---------- 超时 ----------
    def check_timeouts(self, now_monotonic_s: float | None = None) -> list[CommandHandle]:
        """非幂等超时: 进入 UNKNOWN, 不自动重发 (T04)."""
        now = time.monotonic() if now_monotonic_s is None else now_monotonic_s
        timed_out: list[CommandHandle] = []
        for wid, handle in list(self._pending.items()):
            if handle.state in (CommandState.QUEUED, CommandState.SENT,
                                CommandState.ACCEPTED):
                if (now - handle.created_monotonic_s) * 1000.0 > handle_timeout(handle):
                    handle.state = CommandState.UNKNOWN
                    self._note(f"命令 cmd={wid} 超时未见 DONE -> UNKNOWN (不自动重发)")
                    self._retire(wid, handle)
                    timed_out.append(handle)
        return timed_out

    # ---------- 重连基线 ----------
    def on_reconnect_baseline(self, observed_next_id: int | None = None) -> list[CommandHandle]:
        """重连先建状态基线; 未决动作 > UNKNOWN; 不恢复原发送队列 (02 §4.4)."""
        unknown: list[CommandHandle] = []
        for wid, handle in list(self._pending.items()):
            handle.state = CommandState.UNKNOWN
            handle.note("重连基线: 未决动作 UNKNOWN")
            unknown.append(handle)
        self._pending.clear()
        self._issued.clear()  # 旧 ID 不再与新动作绑定
        self._board_epoch += 1
        if observed_next_id is not None:
            self._next_wire_id = observed_next_id % self.ID_MOD or 1
        return unknown

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def next_wire_id(self) -> int:
        return self._next_wire_id


def handle_timeout(handle: CommandHandle) -> float:
    """默认命令超时 (ms). 真实值按动作/固件配置; 未确认为设计参考值 (02 §10.1)."""
    return 5000.0


class BoardSession:
    """单个逻辑板: 地址路由 + 命令账本 + 停止优先级, 与动作服务通过 Handle 协作."""

    def __init__(
        self,
        board_id: str,
        address: int,
        link: LinkSession,
        semantics: FirmwareSemantics,
        *,
        codec=None,
    ) -> None:
        self.board_id = board_id
        self.address = address
        self._link = link
        self._codec = codec if codec is not None else link.codec
        self._semantics = semantics
        self.tracker = CommandTracker(board_id, semantics)
        self.command_timeout_ms = 5000.0
        self._pending_payloads: dict[int, tuple[str, dict[str, Any]]] = {}

    # ---------- 接收 ----------
    def on_frames(self, frames: list[FirmwareFrame]) -> list[CommandHandle]:
        affected: list[CommandHandle] = []
        for frame in frames:
            if frame.address != self.address:
                continue  # 非本板地址: 交给其他 BoardSession
            handle = self.tracker.on_frame(frame)
            if handle is not None:
                self._link.note_response_received()
                affected.append(handle)
        return affected

    # ---------- 命令 ----------
    def issue_action(self, action_id: str, params: dict[str, Any] | None = None) -> CommandHandle:
        """创建账本条目并暂存待发 (不编码/不入队). 供服务先持久化意图再提交."""
        handle = self.tracker.issue(action_id)
        self._pending_payloads[self.tracker.wire_id_of(handle)] = (action_id, params or {})
        self.tracker.mark_queued(handle)
        return handle

    def submit(self, handle: CommandHandle) -> bool:
        """编码并入队 (写成功由链路回调标 SENT). 与 issue_action 配对."""
        wire_id = self.tracker.wire_id_of(handle)
        action_id, params = self._pending_payloads.pop(wire_id, (handle.action_ref, {}))
        payload = self._encode_payload(action_id, params)
        raw = self._codec.encode({
            "address": self.address,
            "frame_type": self._semantics.command_type,
            "command_id": wire_id,
            "payload": payload,
        })
        ok = self._link.send(raw, key=(self.board_id, wire_id),
                             on_written=lambda h=handle: self.tracker.mark_sent(h))
        if not ok:
            handle.state = CommandState.FAILED
            handle.error_reason = "链路未打开/队列满"
        return ok

    def retire(self, handle: CommandHandle) -> None:
        """撤销未提交的命令 (意图持久化失败时不得下发危险动作)."""
        wire_id = self.tracker.wire_id_of(handle)
        self._pending_payloads.pop(wire_id, None)
        self.tracker.retire_pending(wire_id)
        self._link.cancel((self.board_id, wire_id))

    def execute_action(self, action_id: str, params: dict[str, Any] | None = None) -> CommandHandle:
        """便捷组合: issue + submit (直接调用时使用, 无中间持久化)."""
        handle = self.issue_action(action_id, params)
        self.submit(handle)
        return handle

    def request_stop(self, command_id: str | None = None) -> None:
        """停止帧走优先队列; 不打断一帧中间发送, 不承诺零等待 (12 §4.3)."""
        raw = self._codec.encode({
            "address": self.address,
            "frame_type": self._semantics.stop_type,
            "command_id": 0,
            "payload": b"\xFE",
        })
        self._link.send_stop(raw)

    def check_timeouts(self, now: float | None = None) -> list[CommandHandle]:
        out = self.tracker.check_timeouts(now)
        for h in out:
            # 撤销尚未写出的帧: UNKNOWN 动作不能随后被写出 (T04).
            self._link.cancel((self.board_id, self.tracker.wire_id_of(h)))
            self._link.note_response_received()
        return out

    def _encode_payload(self, action_id: str, params: dict[str, Any]) -> bytes:
        # Fake 语义: 载荷 = action_id utf-8 + 参数按键序. 真实编码见 A21.
        parts = [action_id.encode("utf-8")]
        for key in sorted(params):
            parts.append(f"{key}={params[key]}".encode("utf-8"))
        return b"|".join(parts)


__all__ = ["BoardSession", "CommandTracker", "FirmwareSemantics", "handle_timeout"]
