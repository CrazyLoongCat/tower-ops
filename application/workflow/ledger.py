"""执行账本 (12 §4.4, 02 §9.2/§9.5, A16/T27).

每叶节点以 run/station_instance/step/attempt 取账本: 一次提交, 后续 tick 查同一 Handle.
已完成非幂等动作不因普通 tick 重做; UNKNOWN 不重置成 CREATED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from application.contracts.context import ExecutionKey


class LedgerError(RuntimeError):
    pass


@dataclass
class LedgerEntry:
    key: ExecutionKey
    submitted: bool = False
    handle: Any = None
    result: Any = None
    terminal: bool = False
    unknown: bool = False

    def reset_forbidden(self) -> bool:
        """UNKNOWN 不得重置成 CREATED (T27)."""
        return self.unknown


class ExecutionLedger:
    """运行期执行账本 + 持久检查点值 (内存; 持久化由 RecordService 负责)."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str, int], LedgerEntry] = {}
        self.submissions = 0

    def entry(self, key: ExecutionKey) -> LedgerEntry | None:
        return self._entries.get(key.as_tuple())

    def get_or_create(self, key: ExecutionKey) -> LedgerEntry:
        t = key.as_tuple()
        e = self._entries.get(t)
        if e is None:
            e = LedgerEntry(key=key)
            self._entries[t] = e
        return e

    def submit_once(self, key: ExecutionKey, handle: Any) -> LedgerEntry:
        """首次提交记录 Handle; 已提交则返回原条目 (不重复提交, T27)."""
        e = self.get_or_create(key)
        if e.submitted:
            return e
        e.submitted = True
        e.handle = handle
        self.submissions += 1
        return e

    def set_result(self, key: ExecutionKey, result: Any) -> None:
        e = self.get_or_create(key)
        e.result = result

    def mark_terminal(self, key: ExecutionKey) -> None:
        self.get_or_create(key).terminal = True

    def mark_unknown(self, key: ExecutionKey) -> None:
        """UNKNOWN 保持隔离: 不能重置为 CREATED, 防止重发非幂等动作."""
        e = self.get_or_create(key)
        e.unknown = True
        e.terminal = True

    def clear_unknown(self, key: ExecutionKey, operator_note: str) -> None:
        """人工核对后解除 UNKNOWN (需显式说明)."""
        e = self.get_or_create(key)
        if not e.unknown:
            raise LedgerError("该执行未处于 UNKNOWN")
        e.unknown = False
        e.terminal = False
        e.submitted = False
        e.handle = None
        e.result = {"cleared_by": operator_note}

    def all_entries(self) -> list[LedgerEntry]:
        return list(self._entries.values())

    def unknown_entries(self) -> list[LedgerEntry]:
        return [e for e in self._entries.values() if e.unknown]


__all__ = ["ExecutionLedger", "LedgerEntry", "LedgerError"]
