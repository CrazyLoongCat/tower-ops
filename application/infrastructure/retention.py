"""证据留存与轮转 (E25, 02 §11, 10 F4)。

规则:
- 按批准留存规则轮转 (保留天数 / 容量上限);
- **关键证据保护**: 处于 FAULT 或含未决 (SENT/ACCEPTED/UNKNOWN) 命令的运行不自动删除;
- 低磁盘只报告 + 保护关键证据, **不静默丢弃**关键证据 (也不无界增长);
- 删除文件失败显式记录, 不吞掉。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from application.services.records.reports import PENDING_STATES


@dataclass(frozen=True)
class RetentionPolicy:
    keep_days: int = 30
    max_bytes: int = 2_000_000_000
    protect_critical: bool = True     # 关键证据不自动删除
    min_free_bytes: int = 500_000_000


@dataclass
class RetentionResult:
    ok: bool = True
    deleted_frames: int = 0
    deleted_bytes: int = 0
    kept_frames: int = 0
    protected_runs: list[str] = field(default_factory=list)
    low_disk: bool = False
    free_bytes: int | None = None
    messages: list[str] = field(default_factory=list)


def protected_run_ids(store) -> set[str]:
    """FAULT 运行 + 含未决命令的运行 (不自动轮转其证据)."""
    out: set[str] = set()
    for r in store.query("SELECT run_id FROM runs WHERE state='FAULT'"):
        out.add(r["run_id"])
    marks = ",".join("?" * len(PENDING_STATES))
    for r in store.query(
            f"SELECT DISTINCT run_id FROM command_executions WHERE state IN ({marks})",
            PENDING_STATES):
        if r["run_id"]:
            out.add(r["run_id"])
    return out


def _parse_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _free_bytes(directory: str) -> int | None:
    try:
        return shutil.disk_usage(directory).free
    except OSError:
        return None


def enforce_retention(store, policy: RetentionPolicy, *,
                      runtime_dir: str | None = None,
                      now: datetime | None = None) -> RetentionResult:
    """执行留存轮转。只读逻辑 + 删除过期帧; 保护关键证据; 低磁盘显式报告。"""
    now = now or datetime.now(timezone.utc)
    res = RetentionResult()
    cutoff = now - timedelta(days=policy.keep_days)

    protected = protected_run_ids(store) if policy.protect_critical else set()
    res.protected_runs = sorted(protected)

    dir_for_space = runtime_dir or os.path.dirname(os.path.abspath(store.path)) or "."
    res.free_bytes = _free_bytes(dir_for_space)
    if res.free_bytes is not None and res.free_bytes < policy.min_free_bytes:
        res.low_disk = True
        res.ok = False
        res.messages.append(
            f"低磁盘: 可用 {res.free_bytes} < 阈值 {policy.min_free_bytes}; "
            f"只轮转非关键证据, 不静默丢弃关键证据")

    # 该 run 是否可轮转: 取其 capture 归属的 run
    capture_run: dict[str, str] = {}
    for row in store.query("SELECT capture_id, run_id FROM capture_sets"):
        capture_run[row["capture_id"]] = row["run_id"]

    total_bytes = int(store.query_one(
        "SELECT COALESCE(SUM(COALESCE(LENGTH(sha256),0)),0) FROM frames")[0] or 0)
    rows = list(store.query(
        "SELECT frame_id, capture_id, path, updated_utc FROM frames"))

    deleted_ids: list[str] = []
    for row in rows:
        run_id = capture_run.get(row["capture_id"])
        if run_id and run_id in protected:
            res.kept_frames += 1
            continue
        ts = _parse_utc(row["updated_utc"])
        age_expired = ts is not None and ts < cutoff
        # 容量压力: 仅当超上限且非关键时才轮转
        capacity_pressure = total_bytes > policy.max_bytes
        if not (age_expired or capacity_pressure):
            res.kept_frames += 1
            continue
        path = row["path"]
        size = 0
        if path and os.path.exists(path):
            try:
                size = os.path.getsize(path)
                os.remove(path)
            except OSError as exc:
                res.messages.append(f"删除帧文件失败(保留记录): {path}: {exc}")
                res.kept_frames += 1
                continue
        try:
            store.execute("DELETE FROM frames WHERE frame_id=?", (row["frame_id"],))
        except Exception as exc:  # noqa: BLE001
            res.messages.append(f"删除帧记录失败: {row['frame_id']}: {exc}")
            continue
        deleted_ids.append(row["frame_id"])
        res.deleted_frames += 1
        res.deleted_bytes += size
        total_bytes -= size

    if res.deleted_frames:
        res.messages.append(
            f"已轮转 {res.deleted_frames} 帧 / {res.deleted_bytes} 字节"
            f"({policy.keep_days} 天保留, 上限 {policy.max_bytes} 字节)")
    if protected:
        res.messages.append(f"保护 {len(protected)} 个关键运行 (FAULT/未决) 的证据")
    return res


__all__ = ["RetentionPolicy", "RetentionResult", "enforce_retention",
           "protected_run_ids"]
