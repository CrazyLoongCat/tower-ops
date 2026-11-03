"""边缘侧结果存储 (E08)。

**按 `(capture_id, 通道)` 存**：一次采集可含多个通道，每通道各有一条结果（帧或错误），
不会互相覆盖。

- `result` 查询**不隐式删除**（删除只走 TTL/容量淘汰或显式 `release`）；
- TTL 与容量**都有上限**；
- 记录 `service_epoch` 与通道 `epoch`，供上层做**旧任务隔离**。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from application.contracts.camera_v2 import MAX_RESULTS, RESULT_TTL_MS

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_EXPIRED = "EXPIRED"


@dataclass
class JobResult:
    """一次采集在**某一个通道**上的结果。"""

    capture_id: str
    request_id: str
    client_id: str
    service_epoch: int
    channel: str
    status: str = STATUS_OK
    error: str | None = None
    frame: Any | None = None           # StoredFrame
    channel_epoch: int = 0
    created_monotonic_s: float = field(default_factory=time.monotonic)
    ttl_ms: int = RESULT_TTL_MS

    @property
    def key(self) -> tuple[str, str]:
        return (self.capture_id, self.channel)

    def expired(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.created_monotonic_s) * 1000.0 > self.ttl_ms

    def as_dict(self) -> dict:
        return {"capture_id": self.capture_id, "request_id": self.request_id,
                "client_id": self.client_id, "service_epoch": self.service_epoch,
                "channel": self.channel, "status": self.status, "error": self.error,
                "channel_epoch": self.channel_epoch,
                "sequence": getattr(self.frame, "sequence", None)}


class ResultStore:
    """有界结果存储（键 = capture_id + 通道）。"""

    def __init__(self, *, max_results: int = MAX_RESULTS,
                 default_ttl_ms: int = RESULT_TTL_MS) -> None:
        if max_results <= 0:
            raise ValueError("max_results 必须为正")
        self._max = int(max_results)
        self._ttl = int(default_ttl_ms)
        self._items: dict[tuple[str, str], JobResult] = {}

    # ---------- 写 ----------
    def put(self, result: JobResult) -> JobResult:
        if result.ttl_ms <= 0:
            result.ttl_ms = self._ttl
        self._items[result.key] = result
        self._evict_over_capacity()
        return result

    def _evict_over_capacity(self) -> None:
        while len(self._items) > self._max:
            oldest = min(self._items.values(), key=lambda r: r.created_monotonic_s)
            self._items.pop(oldest.key, None)

    # ---------- 读（不删除） ----------
    def get(self, capture_id: str, channel: str | None = None) -> JobResult | None:
        if channel is not None:
            return self._items.get((capture_id, channel))
        for (cid, _ch), item in self._items.items():
            if cid == capture_id:
                return item
        return None

    def all_for(self, capture_id: str) -> list[JobResult]:
        return [item for (cid, _ch), item in self._items.items() if cid == capture_id]

    def expired(self, capture_id: str, channel: str | None = None) -> bool:
        items = (self.all_for(capture_id) if channel is None
                 else ([i for i in (self.get(capture_id, channel),) if i]))
        return bool(items) and all(i.expired() for i in items)

    def has(self, capture_id: str) -> bool:
        return any(cid == capture_id for cid, _ch in self._items)

    def keys(self) -> list[tuple[str, str]]:
        return list(self._items)

    # ---------- 显式删除 ----------
    def release(self, capture_id: str, channel: str | None = None) -> bool:
        if channel is not None:
            return self._items.pop((capture_id, channel), None) is not None
        removed = [k for k in self._items if k[0] == capture_id]
        for k in removed:
            self._items.pop(k, None)
        return bool(removed)

    def evict_expired(self, now: float | None = None) -> int:
        """显式清理过期项（由服务循环调用；**查询路径不会调它**）。"""
        now = time.monotonic() if now is None else now
        dead = [k for k, r in self._items.items() if r.expired(now)]
        for k in dead:
            self._items.pop(k, None)
        return len(dead)

    def stats(self) -> dict:
        captures = {cid for cid, _ch in self._items}
        return {"count": len(self._items), "captures": len(captures),
                "max_results": self._max, "default_ttl_ms": self._ttl}


__all__ = ["JobResult", "ResultStore", "STATUS_CANCELLED", "STATUS_EXPIRED",
           "STATUS_FAILED", "STATUS_OK"]
