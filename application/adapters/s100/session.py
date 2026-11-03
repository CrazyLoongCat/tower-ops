"""唯一 REQ owner 会话 (E09 步骤①②⑤)。

一个连接只由一个 :class:`SingleReqSession` 持有，页面/服务不得各开私有 socket：

- **有界排队**：按 (优先级, 期限, 入队序) 出队，超出上限直接拒绝（不无限堆积）；
- **期限与超时**：超时即**关闭并重建**连接，`epoch += 1`，并把重建原因记下来；
- **request_id 去重**：已作答的 id 直接返回缓存，**迟到响应不当新任务**；
- **epoch 隔离**：请求发出前后 epoch 变化，则本次结果作废（旧任务的响应不落到新会话）。

传输层可注入：真实用 :class:`ZmqReqTransport`，离线/测试用 :class:`InProcessTransport`。
"""
from __future__ import annotations

import heapq
import itertools
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

DEFAULT_TIMEOUT_MS = 2000
DEFAULT_MAX_PENDING = 8
DEFAULT_DEDUP_SIZE = 16


class SessionTimeout(RuntimeError):
    """一次请求超时（触发重建）。"""


class QueueFull(RuntimeError):
    """在途请求已达上限。"""


class ReqTransport(Protocol):
    """一次一问一答的传输。超时须抛 TimeoutError。"""

    def request(self, payload: bytes) -> list[bytes]: ...

    def close(self) -> None: ...


@dataclass
class Reply:
    """一次应答（或失败）。``epoch`` 为该次请求所属的连接代。"""

    request_id: str
    parts: list[bytes] = field(default_factory=list)
    epoch: int = 0
    from_cache: bool = False
    error: str | None = None
    stale: bool = False            # 因 epoch 变化作废
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.stale


@dataclass(order=True)
class _Pending:
    priority: int
    deadline: int
    seq: int
    request_id: str = field(compare=False)
    payload: bytes = field(compare=False)


class SingleReqSession:
    """唯一 REQ owner：队列 / 期限 / 重建 / 去重 / epoch。"""

    def __init__(self, transport_factory: Callable[[], ReqTransport], *,
                 timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 max_pending: int = DEFAULT_MAX_PENDING,
                 dedup_size: int = DEFAULT_DEDUP_SIZE) -> None:
        if timeout_ms <= 0:
            raise ValueError("timeout_ms 必须为正")
        self._factory = transport_factory
        self._timeout_ms = int(timeout_ms)
        self._max_pending = int(max_pending)
        self._transport: ReqTransport | None = None
        self._pending: list[_Pending] = []
        self._answers: "OrderedDict[str, Reply]" = OrderedDict()
        self._dedup_size = int(dedup_size)
        self._seq = itertools.count()
        self._epoch = 0
        self._opened = False
        self.rebuild_count = 0
        self.last_error: str | None = None

    # ---------- 状态 ----------
    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def is_open(self) -> bool:
        return self._opened and self._transport is not None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def timing(self) -> dict:
        return {"timeout_ms": self._timeout_ms, "max_pending": self._max_pending,
                "epoch": self._epoch, "rebuild_count": self.rebuild_count,
                "pending": len(self._pending), "last_error": self.last_error}

    # ---------- 连接 ----------
    def open(self) -> None:
        if self._opened:
            return
        self._transport = self._factory()
        self._opened = True
        self.last_error = None

    def close(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass
        self._transport = None
        self._opened = False

    def _rebuild(self, reason: str) -> None:
        """超时/异常后重建: 关旧连接, 开新连接, epoch+1 (旧任务结果作废)。"""
        self.close()
        self.open()
        self._epoch += 1
        self.rebuild_count += 1
        self.last_error = reason

    # ---------- 请求 ----------
    def submit(self, payload: bytes, *, request_id: str, deadline_ms: int,
               priority: int = 0) -> None:
        """入队（有界）。已作答的 id 不入队（去重在 call 层直接命中缓存）。"""
        if len(self._pending) >= self._max_pending:
            raise QueueFull(
                f"在途请求已达上限 {self._max_pending}，拒绝 {request_id!r}")
        heapq.heappush(self._pending, _Pending(
            priority=-int(priority),           # 数值大者先出（小顶堆取负）
            deadline=int(deadline_ms), seq=next(self._seq),
            request_id=request_id, payload=payload))

    def call(self, payload: bytes, *, request_id: str,
             deadline_ms: int = DEFAULT_TIMEOUT_MS, priority: int = 0) -> Reply:
        """submit + pump 的便捷组合。"""
        cached = self._answers.get(request_id)
        if cached is not None:
            return Reply(request_id=request_id, parts=list(cached.parts),
                         epoch=cached.epoch, from_cache=True,
                         latency_ms=cached.latency_ms)
        self.submit(payload, request_id=request_id, deadline_ms=deadline_ms,
                    priority=priority)
        replies = self.pump()
        for r in replies:
            if r.request_id == request_id:
                return r
        return Reply(request_id=request_id, error="请求未产生应答")

    def pump(self) -> list[Reply]:
        """按优先级/期限出队执行（同步）。返回本次全部应答。"""
        out: list[Reply] = []
        while self._pending:
            item = heapq.heappop(self._pending)
            out.append(self._issue(item))
        return out

    def _issue(self, item: _Pending) -> Reply:
        cached = self._answers.get(item.request_id)
        if cached is not None:                 # 迟到/重复: 用缓存, 不重新发
            return Reply(request_id=item.request_id, parts=list(cached.parts),
                         epoch=cached.epoch, from_cache=True,
                         latency_ms=cached.latency_ms)
        if not self.is_open:
            self.open()
        epoch_before = self._epoch
        started = time.perf_counter()
        assert self._transport is not None
        try:
            parts = self._transport.request(item.payload)
        except TimeoutError as exc:
            self._rebuild(f"超时重建: {exc}")
            return Reply(request_id=item.request_id, epoch=epoch_before,
                         error=f"超时（已重建连接, epoch={self._epoch}）: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._rebuild(f"传输异常重建: {exc}")
            return Reply(request_id=item.request_id, epoch=epoch_before,
                         error=f"传输异常（已重建连接）: {exc}")
        latency = (time.perf_counter() - started) * 1000.0
        if self._epoch != epoch_before:        # epoch 变化 -> 本次结果作废
            return Reply(request_id=item.request_id, epoch=epoch_before,
                         stale=True, error="连接代已变化, 结果作废")
        reply = Reply(request_id=item.request_id, parts=list(parts),
                      epoch=epoch_before, latency_ms=latency)
        self._remember(item.request_id, reply)
        return reply

    def _remember(self, request_id: str, reply: Reply) -> None:
        self._answers[request_id] = reply
        while len(self._answers) > self._dedup_size:
            self._answers.popitem(last=False)


# ---------------------------------------------------------------- 传输实现
class ZmqReqTransport:
    """真实 REQ 传输（**默认不启用**：真实设备连接受 D1 授权约束）。"""

    def __init__(self, endpoint: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 context: Any = None) -> None:
        import zmq                          # 惰性导入: 未装 pyzmq 不影响其它协议
        self._zmq = zmq
        self._endpoint = endpoint
        self._timeout_ms = int(timeout_ms)
        self._ctx = context or zmq.Context.instance()
        self._sock = None
        self._connect()

    def _connect(self) -> None:
        sock = self._ctx.socket(self._zmq.REQ)
        sock.setsockopt(self._zmq.LINGER, 0)
        sock.setsockopt(self._zmq.SNDTIMEO, self._timeout_ms)
        sock.setsockopt(self._zmq.RCVTIMEO, self._timeout_ms)
        sock.connect(self._endpoint)
        self._sock = sock

    def request(self, payload: bytes) -> list[bytes]:
        if self._sock is None:
            raise RuntimeError("socket 已关闭")
        try:
            self._sock.send_multipart([bytes(payload)])
            return [bytes(p) for p in self._sock.recv_multipart()]
        except self._zmq.Again as exc:      # 发送/接收超时
            raise TimeoutError(f"ZMQ 超时({self._timeout_ms}ms): {exc}") from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close(0)
            except Exception:  # noqa: BLE001
                pass
            self._sock = None


class InProcessTransport:
    """进程内传输：把请求交给服务对象处理（离线 Fake / 契约测试用，不碰网络）。"""

    def __init__(self, service: Callable[[bytes], Sequence[bytes]], *,
                 latency_ms: float = 0.0, timeout_times: int = 0) -> None:
        self._service = service
        self._latency_ms = float(latency_ms)
        self._timeout_times = int(timeout_times)
        self.calls = 0
        self.closed = False

    def request(self, payload: bytes) -> list[bytes]:
        self.calls += 1
        if self._timeout_times > 0:
            self._timeout_times -= 1
            raise TimeoutError("注入超时")
        if self._latency_ms:
            time.sleep(self._latency_ms / 1000.0)
        return [bytes(p) for p in self._service(bytes(payload))]

    def close(self) -> None:
        self.closed = True


__all__ = [
    "DEFAULT_DEDUP_SIZE", "DEFAULT_MAX_PENDING", "DEFAULT_TIMEOUT_MS",
    "InProcessTransport", "QueueFull", "Reply", "ReqTransport", "SessionTimeout",
    "SingleReqSession", "ZmqReqTransport",
]
