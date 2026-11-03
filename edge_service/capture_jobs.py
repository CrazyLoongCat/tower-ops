"""边缘侧采集任务管理 (E08)。

要点：
- `submit` **立即**返回 accepted（固化了新鲜度基线与上下文），**SDK 等待不占 REP**；
- 实际采集在 :meth:`CaptureJobManager.pump` 里做 —— 服务循环在两次请求之间调用它；
- 结果有 **期限 / 去重 / 取消 / 释放**；相同 `(service_epoch, client_id, request_id)`
  且内容一致返回**同一 `capture_id`**，内容不同则 `CONFLICT`；
- 队列**有界**：服务在途超 `MAX_PENDING_JOBS` 或单通道超 `MAX_QUEUE_DEPTH` → `BUSY`；
- 服务重启换 `service_epoch`，旧任务**不隐式继续**。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from application.contracts.camera_v2 import (
    DEDUP_TTL_MS,
    MAX_PENDING_JOBS,
    MAX_QUEUE_DEPTH,
    RESULT_TTL_MS,
    ErrorCode,
)
from edge_service.camera_registry import CameraRegistry
from edge_service.result_store import (
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_OK,
    JobResult,
    ResultStore,
)

STATE_ACCEPTED = "ACCEPTED"
STATE_DONE = "DONE"
STATE_CANCELLED = "CANCELLED"


class JobRejected(RuntimeError):
    """任务被拒绝（带 wire 错误码）。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class JobTicket:
    """已受理的采集任务。"""

    capture_id: str
    request_id: str
    client_id: str
    service_epoch: int
    channels: tuple[str, ...]
    deadline_ms: int
    payload_hash: str
    allow_partial: bool = True
    required_streams: tuple[str, ...] = ("color", "depth")
    state: str = STATE_ACCEPTED
    cancelled: bool = False            # 取消标记（不改 state, 由 pump 落结果）
    accepted_monotonic_s: float = field(default_factory=time.monotonic)
    done_monotonic_s: float | None = None

    def expired(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.accepted_monotonic_s) * 1000.0 > self.deadline_ms

    def as_dict(self) -> dict:
        return {"capture_id": self.capture_id, "request_id": self.request_id,
                "channels": list(self.channels), "state": self.state,
                "cancelled": self.cancelled,
                "service_epoch": self.service_epoch}


def payload_hash(*, channels: Sequence[str], required_streams: Sequence[str],
                 allow_partial: bool) -> str:
    body = json.dumps({"cams": sorted(channels),
                       "required_streams": sorted(required_streams),
                       "allow_partial": bool(allow_partial)},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class CaptureJobManager:
    """受理 / 收集 / 查询 / 取消 采集任务。"""

    def __init__(self, registry: CameraRegistry, results: ResultStore | None = None, *,
                 service_epoch: int = 1, max_pending: int = MAX_PENDING_JOBS,
                 queue_depth: int = MAX_QUEUE_DEPTH,
                 dedup_ttl_ms: int = DEDUP_TTL_MS,
                 result_ttl_ms: int = RESULT_TTL_MS) -> None:
        self.registry = registry
        self.results = results if results is not None else ResultStore()
        self.service_epoch = int(service_epoch)
        self._max_pending = int(max_pending)
        self._queue_depth = int(queue_depth)
        self._dedup_ttl_ms = int(dedup_ttl_ms)
        self._result_ttl_ms = int(result_ttl_ms)
        self._accepted: dict[str, JobTicket] = {}
        # (epoch, client, request_id) -> (payload_hash, capture_id)
        self._dedup: dict[tuple[int, str, str], tuple[str, str]] = {}

    # ---------- 受理 ----------
    def submit(self, *, request_id: str, client_id: str, channels: Iterable[str],
               deadline_ms: int = 2000, required_streams: Sequence[str] = ("color", "depth"),
               allow_partial: bool = True) -> JobTicket:
        chans = tuple(str(c) for c in channels)
        if not chans:
            raise JobRejected(ErrorCode.BAD_ENVELOPE, "submit 至少需要一个通道")
        if deadline_ms <= 0:
            raise JobRejected(ErrorCode.BAD_ENVELOPE, "deadline_ms 必须为正")
        phash = payload_hash(channels=chans, required_streams=required_streams,
                             allow_partial=allow_partial)
        key = (self.service_epoch, client_id, request_id)
        existing = self._dedup.get(key)
        if existing is not None:
            old_hash, old_id = existing
            if old_hash != phash:
                raise JobRejected(
                    ErrorCode.CONFLICT,
                    f"request_id {request_id!r} 已用于不同内容（拒绝静默覆盖）")
            return self._accepted[old_id]          # 去重命中: 同一 capture_id
        # 有界：只统计**在途**（ACCEPTED）任务，已完成的不再占队列
        if self._pending_count() >= self._max_pending:
            raise JobRejected(ErrorCode.BUSY, f"在途任务已达上限 {self._max_pending}")
        for ch in chans:
            if self._channel_pending(ch) >= self._queue_depth:
                raise JobRejected(ErrorCode.BUSY,
                                  f"通道 {ch!r} 在途已达上限 {self._queue_depth}")
        capture_id = self._make_capture_id(key, phash)
        ticket = JobTicket(
            capture_id=capture_id, request_id=request_id, client_id=client_id,
            service_epoch=self.service_epoch, channels=chans,
            deadline_ms=int(deadline_ms), payload_hash=phash,
            allow_partial=bool(allow_partial),
            required_streams=tuple(required_streams))
        self._accepted[capture_id] = ticket
        self._dedup[key] = (phash, capture_id)
        return ticket

    def _make_capture_id(self, key: tuple[int, str, str], phash: str) -> str:
        epoch, client, request_id = key
        return f"cap-{epoch}-{client}-{request_id}-{phash[:8]}"

    def _pending_count(self) -> int:
        return sum(1 for t in self._accepted.values() if t.state == STATE_ACCEPTED)

    def _channel_pending(self, channel: str) -> int:
        return sum(1 for t in self._accepted.values()
                   if channel in t.channels and t.state == STATE_ACCEPTED)

    # ---------- 收集 ----------
    def pump(self, now: float | None = None) -> int:
        """执行未完成任务；返回本次处理的任务数。服务循环在请求之间调用。"""
        now = time.monotonic() if now is None else now
        processed = 0
        for ticket in list(self._accepted.values()):
            if ticket.state == STATE_DONE:
                continue
            if ticket.cancelled:
                self._finish_all(ticket, STATUS_CANCELLED, "已取消", now)
            elif ticket.expired(now):
                self._finish_all(ticket, STATUS_EXPIRED, "任务超期", now)
            else:
                self._collect(ticket, now)
            processed += 1
        self.results.evict_expired(now)
        return processed

    def _collect(self, ticket: JobTicket, now: float) -> None:
        errors: dict[str, str] = {}
        frames = {}
        for ch in ticket.channels:
            try:
                frames[ch] = self.registry.actor(ch).grab(ticket.deadline_ms)
            except KeyError:
                errors[ch] = f"未知通道 {ch!r}"
            except Exception as exc:  # noqa: BLE001
                errors[ch] = f"{type(exc).__name__}: {exc}"
        if errors and not ticket.allow_partial:
            # 不允许部分成功 -> 整个集合失败（逐通道都记失败原因）
            for ch in ticket.channels:
                self._store(ticket, ch, STATUS_FAILED,
                            errors.get(ch) or "集合中存在失败通道", None, now)
        else:
            for ch in ticket.channels:
                if ch in frames:
                    self._store(ticket, ch, STATUS_OK, None, frames[ch], now)
                else:
                    self._store(ticket, ch, STATUS_FAILED, errors.get(ch, "未知失败"),
                                None, now)
        ticket.state = STATE_DONE
        ticket.done_monotonic_s = now

    def _finish_all(self, ticket: JobTicket, status: str, reason: str, now: float) -> None:
        for ch in ticket.channels:
            self._store(ticket, ch, status, reason, None, now)
        ticket.state = STATE_DONE
        ticket.done_monotonic_s = now

    def _store(self, ticket: JobTicket, channel: str, status: str,
               error: str | None, frame, now: float) -> None:
        self.results.put(JobResult(
            capture_id=ticket.capture_id, request_id=ticket.request_id,
            client_id=ticket.client_id, service_epoch=ticket.service_epoch,
            channel=channel, status=status, error=error, frame=frame,
            channel_epoch=(getattr(frame, "epoch", 0) if frame else 0),
            created_monotonic_s=now, ttl_ms=self._result_ttl_ms))

    # ---------- 查询 / 取消 / 释放 ----------
    def ticket(self, capture_id: str) -> JobTicket | None:
        return self._accepted.get(capture_id)

    def cancel(self, capture_id: str) -> bool:
        ticket = self._accepted.get(capture_id)
        if ticket is None or ticket.state == STATE_DONE or ticket.cancelled:
            return False
        ticket.cancelled = True
        return True

    def release(self, capture_id: str) -> bool:
        return self.results.release(capture_id)

    def bump_epoch(self) -> int:
        """服务重启：换 service_epoch；旧任务不再继续（留在结果里等 TTL 过期）。"""
        self.service_epoch += 1
        self._accepted.clear()
        self._dedup.clear()
        return self.service_epoch

    def stats(self) -> dict:
        return {"service_epoch": self.service_epoch,
                "pending": self._pending_count(),
                "max_pending": self._max_pending, "queue_depth": self._queue_depth,
                "results": self.results.stats()}


__all__ = ["CaptureJobManager", "JobRejected", "JobTicket", "STATE_ACCEPTED",
           "STATE_CANCELLED", "STATE_DONE", "payload_hash"]
