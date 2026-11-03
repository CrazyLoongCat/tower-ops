"""RecordService: 最小持久化与关键证据 (12 §4.4, 02 §11, A11).

规则:
- 关键命令意图/执行账本需持久化确认后才新下发危险动作 (T20/T22);
- 记录失败阻止新动作, 但停止不得等待数据库队列 (T18);
- run/station/step/command/frame/result/events/config_versions 可追溯;
- 图像保存原图/uint16 深度/元数据及 hash, 只存引用不塞库;
- 事件缓冲有界 (StorageBackpressure), 低磁盘/写失败策略明确.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np

from application.contracts.frames import FrameBundle
from application.contracts.handles import CommandHandle
from application.contracts.results import DetectionResult
from application.infrastructure.events import Event, EventSequencer
from application.infrastructure.storage import (
    PENDING_COMMAND_STATES,
    SqliteStore,
    StorageError,
)


class StorageBackpressure(RuntimeError):
    """事件缓冲已满: 存储持续失败时不得无界积累, 应阻止新危险动作."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecordService:
    """记录服务. 单所有者串行访问; 不开启后台线程以保证离线可确定复现."""

    def __init__(
        self,
        store: SqliteStore,
        *,
        sequencer: EventSequencer | None = None,
        runtime_dir: str | None = None,
        max_pending_events: int = 1000,
        max_pending_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._store = store
        self._seq = sequencer or EventSequencer()
        self._runtime_dir = runtime_dir
        self._buffer: deque[dict] = deque()
        self._max_pending = max_pending_events
        self._max_pending_bytes = max_pending_bytes
        # DEC-068：事件写入会来自多个线程（运动/引导工作线程的日志经事件桥入 events 表），
        # 事件缓冲与 flush 必须串行，否则两个线程可能同一事件写两次/漏一次。
        self._lock = threading.RLock()
        # 领域事件镜像日志器（默认 None；由装配设成 towerops.event 写进文本日志）
        self._mirror_logger: logging.Logger | None = None

    def set_mirror_logger(self, logger: logging.Logger | None) -> None:
        """设领域事件镜像日志器：``append_event`` 入库存证后，**另**写一行文本日志。

        镜像日志器约定是 :data:`application.infrastructure.logging_setup.EVENT_LOGGER`
        （``towerops.event``）——事件桥会**跳过**它，故不会重复入库（DEC-029）。
        """
        self._mirror_logger = logger

    _MIRROR_LEVEL = {
        "DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING,
        "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
    }

    def _mirror(self, event: Event) -> None:
        logger = self._mirror_logger
        if logger is None:
            return
        try:
            severity = getattr(event.severity, "value", str(event.severity))
            level = self._MIRROR_LEVEL.get(severity, logging.INFO)
            tail = f"  ({event.reason})" if event.reason else ""
            logger.log(level, "[%s] %s%s", event.event_type, event.message or "", tail)
        except Exception:  # noqa: BLE001 - 镜像失败不得影响入库存证
            pass


    # ---------- 事件 ----------
    def append_event(self, event: Event, *, mirror: bool = True) -> dict:
        """分配 sequence 并入有界缓冲; 立即尝试落库.

        ``mirror`` 为真时（默认）另写一行到镜像日志（文本日志文件）；
        事件桥写库用 ``mirror=False``，避免与原始日志记录重复（DEC-029）。
        多线程安全：缓冲与 flush 由内部锁串行（DEC-068）。
        """
        with self._lock:
            event.sequence = self._seq.next()
            payload = event.as_dict()
            if len(self._buffer) >= self._max_pending:
                raise StorageBackpressure(
                    f"事件缓冲达到上限 {self._max_pending}, 存储持续失败"
                )
            self._buffer.append(payload)
            self.flush()
        if mirror:
            self._mirror(event)      # 镜像日志在锁外写（只写文本，不碰库）
        return payload

    def flush(self) -> int:
        """把缓冲写入库; 失败保留缓冲并抛出 (调用方可阻止新动作)."""
        with self._lock:
            written = 0
            while self._buffer:
                ev = self._buffer[0]
                try:
                    self._store.insert_event(ev)
                except StorageError:
                    raise  # 保留缓冲, 由调用方处理
                self._buffer.popleft()
                written += 1
            return written

    def append_event_nonblocking(self, event: Event, *,
                                 mirror: bool = True) -> None:
        """停止/关键路径用: 记录失败不阻塞停止 (T18)."""
        try:
            self.append_event(event, mirror=mirror)
        except (StorageError, StorageBackpressure):
            pass

    @property
    def pending_events(self) -> int:
        return len(self._buffer)

    # ---------- 运行/意图 ----------
    def start_run(self, run_id: str, *, recipe_id: str, recipe_version: str,
                  config_hash: str, calibration_hash: str) -> None:
        self._store.upsert_run(
            run_id=run_id, recipe_id=recipe_id, recipe_version=recipe_version,
            config_hash=config_hash, calibration_hash=calibration_hash,
            state="RUNNING", created_utc=_utc_now(), updated_utc=_utc_now(),
        )

    def finish_run(self, run_id: str, state: str) -> None:
        self._store.execute(
            "UPDATE runs SET state=?, updated_utc=? WHERE run_id=?",
            (state, _utc_now(), run_id),
        )

    def confirm_command_intent(self, handle: CommandHandle, *, run_id: str,
                               station_instance_id: str | None,
                               step_id: str | None) -> None:
        """危险动作下发前的持久化确认. 失败即抛出 -> 调用方不得下发."""
        self._store.upsert_command(
            command_id=handle.command_id, run_id=run_id,
            station_instance_id=station_instance_id, step_id=step_id,
            action_ref=handle.action_ref, board_id=handle.board_id,
            state=handle.state.value, done_ok=handle.done_ok,
            reason=None, updated_utc=_utc_now(),
        )

    def update_command(self, handle: CommandHandle, *, run_id: str | None = None,
                       station_instance_id: str | None = None,
                       step_id: str | None = None) -> None:
        self._store.upsert_command(
            command_id=handle.command_id, run_id=run_id,
            station_instance_id=station_instance_id, step_id=step_id,
            action_ref=handle.action_ref, board_id=handle.board_id,
            state=handle.state.value, done_ok=handle.done_ok,
            reason=handle.reject_reason or handle.error_reason,
            updated_utc=_utc_now(),
        )

    def checkpoint(self, run_id: str, key: str, payload: dict) -> None:
        self._store.put_checkpoint(run_id, key, payload, _utc_now())

    def get_checkpoint(self, run_id: str, key: str) -> dict | None:
        return self._store.get_checkpoint(run_id, key)

    # ---------- 帧/结果 ----------
    def save_capture_set(self, cs) -> None:
        """记录一次抓拍集合 (capture_id -> run/工位/步骤), 供报告与留存关联帧。"""
        req = cs.request
        for cid, frame in cs.frames.items():
            self._store.execute(
                "INSERT OR REPLACE INTO capture_sets(capture_id, run_id,"
                " station_instance_id, step_id, camera_ids, delta_quality, updated_utc)"
                " VALUES (?,?,?,?,?,?,?)",
                (frame.capture_id, req.run_id, req.station_instance_id, req.step_id,
                 json.dumps([cid], ensure_ascii=False), cs.delta_quality.value,
                 _utc_now()))

    def save_capture(self, frame: FrameBundle, subdir: str = "frames") -> str:
        """保存原图彩色与 uint16 深度 + 元数据; 返回帧记录 id."""
        if self._runtime_dir is None:
            raise StorageError("未配置 runtime_dir, 不能保存图像")
        target = os.path.join(self._runtime_dir, subdir)
        os.makedirs(target, exist_ok=True)
        color_path = depth_path = None
        sha = None
        if frame.color is not None:
            color_path = os.path.join(target, f"{frame.frame_id}_color.npy")
            np.save(color_path, np.asarray(frame.color.data))
            sha = _sha256_file(color_path)
        if frame.depth is not None:
            depth_path = os.path.join(target, f"{frame.frame_id}_depth.npy")
            np.save(depth_path, np.asarray(frame.depth.data).astype(np.uint16))
        self._store.upsert_frame(
            frame_id=frame.frame_id, capture_id=frame.capture_id,
            camera_id=frame.camera_id, sequence=frame.sequence,
            path=color_path, sha256=sha,
            dtype=frame.depth.dtype if frame.depth else None,
            shape=frame.depth.shape if frame.depth else None,
            depth_scale_m=frame.depth_scale_m,
            calibration_version=frame.calibration_version,
            updated_utc=_utc_now(),
        )
        return frame.frame_id

    def save_result(self, result: DetectionResult, detail: str | None = None) -> None:
        self._store.upsert_result(
            result_id=result.result_id, capture_id=result.capture_id,
            frame_id=result.frame_id, pipeline_id=result.pipeline_id,
            pipeline_version=result.pipeline_version, status=result.status.value,
            params_hash=result.params_hash, detail=detail or result.reason,
            updated_utc=_utc_now(),
        )

    def record_config_version(self, version_id: str, kind: str, ref_id: str,
                              revision: str, hash_: str, state: str,
                              operator: str) -> None:
        self._store.upsert_config_version(
            version_id=version_id, kind=kind, ref_id=ref_id, revision=revision,
            hash=hash_, state=state, operator=operator, created_utc=_utc_now(),
        )

    # ---------- 查询/回放 ----------
    def pending_unknown_commands(self) -> list[dict]:
        """重启后检出的未决动作 (T22): 需人工核对, 不自动续发."""
        return self._store.pending_commands()

    def list_runs(self, limit: int = 50) -> Iterable[dict]:
        return self._store.list_runs(limit)

    # ---------- 日志检索（「日志诊断」页，DEC-029） ----------
    def query_events(self, **kwargs: Any) -> list[dict]:
        """按条件检索**全部**日志事件（不限当前运行）。字段见 SqliteStore.query_events。"""
        return self._store.query_events(**kwargs)

    def list_event_types(self) -> list[str]:
        return self._store.distinct_event_types()

    def list_event_devices(self) -> list[str]:
        return self._store.distinct_event_devices()

    def list_event_runs(self) -> list[str]:
        return self._store.distinct_event_runs()

    def replay(self, run_id: str) -> Iterable[dict]:
        """只读回放: 返回该 run 的事件与结果; 不建立任何真实运动通道 (T23)."""
        rows = self._store.query(
            "SELECT * FROM events WHERE run_id=? ORDER BY sequence", (run_id,)
        )
        return [dict(r) for r in rows]

    def export_report(self, run_id: str, out_path: str) -> str:
        run = self._store.query_one(
            "SELECT * FROM runs WHERE run_id=?", (run_id,))
        events = self.replay(run_id)
        pending = self.pending_unknown_commands()
        missing = []
        if run is None:
            missing.append("run 记录缺失")
        lines = [f"# 运行报告 {run_id}", ""]
        if run:
            lines += [f"- 执行方案: {run['recipe_id']} @ {run['recipe_version']}",
                      f"- 状态: {run['state']}",
                      f"- 配置hash: {run['config_hash']}",
                      f"- 标定hash: {run['calibration_hash']}"]
        lines += ["", f"## 事件 ({len(events)})", ""]
        for ev in events:
            lines.append(f"- [{ev['severity']}] {ev['event_type']}: {ev['message']}")
        lines += ["", f"## 未决动作 ({len(pending)})", ""]
        for p in pending:
            lines.append(f"- {p['command_id']}: {p['state']}")
        if missing:
            lines += ["", "## 缺项", ""] + [f"- {m}" for m in missing]
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return out_path


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["RecordService", "StorageBackpressure"]
