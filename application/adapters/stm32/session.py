"""物理链路会话 (12 §4.3, 02 §4.1/§4.4, T02/T18).

一个 LinkSession 独占一条物理链路的读写与缓存:
- 持续接收、缓存有界、按确认地址路由 (交给 BoardSession);
- 每物理链路单发送者; 停止帧优先级高于普通命令, 在完整帧边界调度;
- 重连生成 host_link_epoch, 丢弃旧待发运动队列, 先建立状态基线.

普通帧带 key (board_id, wire_id): 命令超时/取消时按 key 撤销未发出的帧,
避免 UNKNOWN 动作仍在后面被写出 (T04).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

from application.adapters.stm32.codec import FirmwareFrame
from application.adapters.stm32.transport import ByteTransport


@dataclass
class _QueuedFrame:
    raw: bytes
    key: tuple[str, int] | None = None
    on_written: Callable[[], None] | None = None


class LinkSession:
    """单物理链路所有者. 不决定业务分支, 只做收发/路由/优先级/撤销."""

    def __init__(
        self,
        link_id: str,
        transport: ByteTransport,
        codec,
        *,
        max_frames_per_flush: int = 1,
        max_queue: int = 256,
    ) -> None:
        self.link_id = link_id
        self._transport = transport
        self._codec = codec
        self._epoch = 0
        self._stop_queue: deque[bytes] = deque()
        self._normal_queue: deque[_QueuedFrame] = deque()
        self._max_frames_per_flush = max_frames_per_flush
        self._max_queue = max_queue
        self._awaiting_key: tuple[str, int] | None = None  # 单事务: 在途响应
        self.sent_count = 0
        self.stop_sent_count = 0
        self.dropped_on_reconnect = 0

    # ---------- 生命周期 ----------
    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def codec(self):
        return self._codec

    @property
    def is_open(self) -> bool:
        return self._transport.is_open

    def open(self) -> None:
        self._transport.open()
        self._epoch += 1  # 新 host_link_epoch
        self._stop_queue.clear()
        self._normal_queue.clear()
        self._awaiting_key = None
        self._codec.splitter.reset()

    def close(self) -> None:
        self._transport.close()

    def on_reconnect(self) -> None:
        """重连: 新 epoch, 丢弃旧待发运动队列, 不恢复原发送队列 (02 §4.4)."""
        self.dropped_on_reconnect += len(self._normal_queue)
        self._normal_queue.clear()
        self._stop_queue.clear()
        self._awaiting_key = None
        self._epoch += 1
        self._codec.splitter.reset()

    # ---------- 发送 ----------
    def send(self, raw: bytes, key: tuple[str, int] | None = None,
             on_written: Callable[[], None] | None = None) -> bool:
        """入普通队列. 返回是否入队 (不代表设备接受)."""
        if not self.is_open:
            return False
        if len(self._normal_queue) >= self._max_queue:
            return False
        self._normal_queue.append(_QueuedFrame(raw=raw, key=key, on_written=on_written))
        return True

    def send_stop(self, raw: bytes) -> bool:
        """停止帧进优先队列; 不被普通命令拖住 (T18)."""
        if not self.is_open:
            return False
        self._stop_queue.append(raw)
        return True

    def cancel(self, key: tuple[str, int]) -> int:
        """按 key 撤销未发出的普通帧; 若在途响应帧匹配则解除单事务锁."""
        before = len(self._normal_queue)
        self._normal_queue = deque(f for f in self._normal_queue if f.key != key)
        removed = before - len(self._normal_queue)
        if self._awaiting_key == key:
            self._awaiting_key = None
        return removed

    def flush_outgoing(self) -> int:
        """发送队列: 先清停止队列, 再按节拍发普通帧 (单事务)."""
        sent = 0
        while self._stop_queue:
            raw = self._stop_queue.popleft()
            if self._transport.write(raw):
                self.stop_sent_count += 1
                sent += 1
            else:
                # 写失败: 停止优先级保留在队列前端, 下次重试 (不静默丢弃)
                self._stop_queue.appendleft(raw)
                return sent
        if self._awaiting_key is not None:
            return sent
        for _ in range(self._max_frames_per_flush):
            if not self._normal_queue:
                break
            item = self._normal_queue[0]
            if not self._transport.write(item.raw):
                break
            self._normal_queue.popleft()
            self.sent_count += 1
            sent += 1
            self._awaiting_key = item.key
            if item.on_written is not None:
                item.on_written()  # 传输写成功 -> 标 SENT (仍非完成)
        return sent

    def note_response_received(self) -> None:
        """BoardSession 收到匹配响应/完成时调用, 解除单事务锁."""
        self._awaiting_key = None

    # ---------- 接收 ----------
    def pump(self) -> list[FirmwareFrame]:
        """读传输 -> 增量解码 -> 返回本次完整帧 (先 flush 发送队列)."""
        self.flush_outgoing()
        data = self._transport.read_available()
        if not data:
            return []
        return self._codec.feed(data)

    @property
    def pending_normal(self) -> int:
        return len(self._normal_queue)

    @property
    def pending_stop(self) -> int:
        return len(self._stop_queue)


__all__ = ["LinkSession"]
