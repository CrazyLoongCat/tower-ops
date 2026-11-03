"""SQLite 持久化 (12 §4.4, 02 §11, A11).

表: runs、station_instances、step_executions、command_executions、capture_sets、
frames、vision_results、events、config_versions、checkpoints.
只存元数据与文件引用/hash, 不向数据库塞全部视频流.
写入失败须可被调用方感知 (阻止新危险动作), 停止不等待数据库队列.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from typing import Any, Iterable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, recipe_id TEXT, recipe_version TEXT,
  config_hash TEXT, calibration_hash TEXT, state TEXT, created_utc TEXT,
  updated_utc TEXT
);
CREATE TABLE IF NOT EXISTS station_instances (
  station_instance_id TEXT PRIMARY KEY, run_id TEXT, station_id TEXT,
  iteration INTEGER, state TEXT, created_utc TEXT
);
CREATE TABLE IF NOT EXISTS step_executions (
  step_execution_id TEXT PRIMARY KEY, run_id TEXT, station_instance_id TEXT,
  step_id TEXT, attempt INTEGER, state TEXT, updated_utc TEXT
);
CREATE TABLE IF NOT EXISTS command_executions (
  command_id TEXT PRIMARY KEY, run_id TEXT, station_instance_id TEXT, step_id TEXT,
  action_ref TEXT, board_id TEXT, state TEXT, done_ok INTEGER, reason TEXT,
  updated_utc TEXT
);
CREATE TABLE IF NOT EXISTS capture_sets (
  capture_id TEXT PRIMARY KEY, run_id TEXT, station_instance_id TEXT, step_id TEXT,
  camera_ids TEXT, delta_quality TEXT, updated_utc TEXT
);
CREATE TABLE IF NOT EXISTS frames (
  frame_id TEXT PRIMARY KEY, capture_id TEXT, camera_id TEXT, sequence INTEGER,
  path TEXT, sha256 TEXT, dtype TEXT, shape TEXT, depth_scale_m REAL,
  calibration_version TEXT, updated_utc TEXT
);
CREATE TABLE IF NOT EXISTS vision_results (
  result_id TEXT PRIMARY KEY, capture_id TEXT, frame_id TEXT, pipeline_id TEXT,
  pipeline_version TEXT, status TEXT, params_hash TEXT, detail TEXT, updated_utc TEXT
);
CREATE TABLE IF NOT EXISTS events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, severity TEXT,
  message TEXT, run_id TEXT, station_instance_id TEXT, step_id TEXT,
  command_id TEXT, device_id TEXT, capture_id TEXT, frame_id TEXT,
  version TEXT, reason TEXT, advice TEXT, monotonic_s REAL, utc TEXT
);
-- DEC-049: 诊断页按 类型/设备/任务 过滤与去重（DISTINCT）都走这些列
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_device_id ON events(device_id);
CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id);
CREATE TABLE IF NOT EXISTS config_versions (
  version_id TEXT PRIMARY KEY, kind TEXT, ref_id TEXT, revision TEXT,
  hash TEXT, state TEXT, operator TEXT, created_utc TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
  run_id TEXT, key TEXT, payload TEXT, updated_utc TEXT,
  PRIMARY KEY (run_id, key)
);
"""

# 这些命令状态表示"在途/未决": 重启后不得盲重放 (T22).
PENDING_COMMAND_STATES = ("SENT", "ACCEPTED", "UNKNOWN")


class StorageError(RuntimeError):
    """存储写失败 (调用方须阻止新危险动作)."""


class LowDiskError(StorageError):
    """磁盘空间不足."""


class SqliteStore:
    """基础 SQLite 封装. 单连接 + 内部锁串行访问.

    **跨线程（DEC-068）**：运动/引导等工作线程记日志会经事件桥写 ``events`` 表，
    连接却是在主线程建的 —— 默认 ``check_same_thread=True`` 会抛
    ``ProgrammingError: SQLite objects created in a thread can only be used in
    that same thread``。这里打开 ``check_same_thread=False``，并用一把可重入锁把
    **所有**语句（写 :meth:`execute`、读 :meth:`query`）串行化；不要再绕过这两个
    方法直接拿 :attr:`conn` 执行语句（并发读写会撞 cursor/事务）。
    """

    def __init__(self, path: str, *, min_free_bytes: int = 0) -> None:
        self.path = path
        self._min_free = min_free_bytes
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.fail_writes = False  # 测试注入: 写失败策略
        self.read_only = False

    def open(self, *, read_only: bool = False) -> None:
        """打开存储. read_only=True 用于回放: 只读且不建表, 任何写入会被 SQLite 拒绝。"""
        if read_only:
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                         check_same_thread=False)
        else:
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if not read_only:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.read_only = read_only

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("存储未打开")
        return self._conn

    # ---------- 前置 ----------
    def check_disk(self) -> None:
        """低磁盘告警: 关键证据不允许无界增长/静默丢弃 (E25)."""
        import os
        directory = os.path.dirname(self.path) or "."
        try:
            usage = shutil.disk_usage(directory)
        except OSError:
            return
        if usage.free < self._min_free:
            raise LowDiskError(
                f"可用空间 {usage.free} 低于阈值 {self._min_free}"
            )

    def _guard(self) -> None:
        if self.fail_writes:
            raise StorageError("注入: 写失败")

    # ---------- 读取（跨线程安全） ----------
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """查询全部行（内部锁串行；外部模块不要直接 ``store.conn.execute``）。"""
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    # ---------- 写入 ----------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        self._guard()
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    def upsert_run(self, **kw: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO runs(run_id, recipe_id, recipe_version,"
            " config_hash, calibration_hash, state, created_utc, updated_utc)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (kw["run_id"], kw.get("recipe_id"), kw.get("recipe_version"),
             kw.get("config_hash"), kw.get("calibration_hash"), kw.get("state"),
             kw.get("created_utc"), kw.get("updated_utc")),
        )

    def insert_event(self, ev: dict) -> None:
        self.execute(
            "INSERT INTO events(event_type, severity, message, run_id,"
            " station_instance_id, step_id, command_id, device_id, capture_id,"
            " frame_id, version, reason, advice, monotonic_s, utc)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev.get("event_type"), ev.get("severity"), ev.get("message"),
             ev.get("run_id"), ev.get("station_instance_id"), ev.get("step_id"),
             ev.get("command_id"), ev.get("device_id"), ev.get("capture_id"),
             ev.get("frame_id"), ev.get("version"), ev.get("reason"),
             ev.get("advice"), ev.get("monotonic_s"), ev.get("utc")),
        )

    def upsert_command(self, **kw: Any) -> None:
        done = kw.get("done_ok")
        self.execute(
            "INSERT OR REPLACE INTO command_executions(command_id, run_id,"
            " station_instance_id, step_id, action_ref, board_id, state, done_ok,"
            " reason, updated_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (kw["command_id"], kw.get("run_id"), kw.get("station_instance_id"),
             kw.get("step_id"), kw.get("action_ref"), kw.get("board_id"),
             kw.get("state"), None if done is None else int(bool(done)),
             kw.get("reason"), kw.get("updated_utc")),
        )

    def upsert_frame(self, **kw: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO frames(frame_id, capture_id, camera_id,"
            " sequence, path, sha256, dtype, shape, depth_scale_m,"
            " calibration_version, updated_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (kw["frame_id"], kw.get("capture_id"), kw.get("camera_id"),
             kw.get("sequence"), kw.get("path"), kw.get("sha256"), kw.get("dtype"),
             json.dumps(kw.get("shape")) if kw.get("shape") else None,
             kw.get("depth_scale_m"), kw.get("calibration_version"),
             kw.get("updated_utc")),
        )

    def upsert_result(self, **kw: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO vision_results(result_id, capture_id, frame_id,"
            " pipeline_id, pipeline_version, status, params_hash, detail, updated_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (kw["result_id"], kw.get("capture_id"), kw.get("frame_id"),
             kw.get("pipeline_id"), kw.get("pipeline_version"), kw.get("status"),
             kw.get("params_hash"), kw.get("detail"), kw.get("updated_utc")),
        )

    def upsert_config_version(self, **kw: Any) -> None:
        self.execute(
            "INSERT OR REPLACE INTO config_versions(version_id, kind, ref_id,"
            " revision, hash, state, operator, created_utc) VALUES (?,?,?,?,?,?,?,?)",
            (kw["version_id"], kw.get("kind"), kw.get("ref_id"), kw.get("revision"),
             kw.get("hash"), kw.get("state"), kw.get("operator"),
             kw.get("created_utc")),
        )

    def put_checkpoint(self, run_id: str, key: str, payload: dict, utc: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO checkpoints(run_id, key, payload, updated_utc)"
            " VALUES (?,?,?,?)",
            (run_id, key, json.dumps(payload, ensure_ascii=False), utc),
        )

    # ---------- 查询 ----------
    def get_checkpoint(self, run_id: str, key: str) -> dict | None:
        row = self.query_one(
            "SELECT payload FROM checkpoints WHERE run_id=? AND key=?",
            (run_id, key),
        )
        return json.loads(row["payload"]) if row else None

    def pending_commands(self) -> list[dict]:
        """重启后检出的未决动作 (不盲重放, 需人工核对)."""
        marks = ",".join("?" * len(PENDING_COMMAND_STATES))
        rows = self.query(
            f"SELECT * FROM command_executions WHERE state IN ({marks})",
            PENDING_COMMAND_STATES,
        )
        return [dict(r) for r in rows]

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self.query(
            "SELECT * FROM runs ORDER BY created_utc DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    def count_events(self) -> int:
        return self.query_one("SELECT COUNT(*) FROM events")[0]

    def query_events(self, *, limit: int = 500, severity: str | None = None,
                     event_type: str | None = None, device_id: str | None = None,
                     run_id: str | None = None, keyword: str | None = None,
                     min_sequence: int | None = None,
                     newest_first: bool = True) -> list[dict]:
        """按条件检索事件（「日志诊断」页用；默认最新在前）。

        过滤项都可空; 任一给出即做 ``AND`` 过滤。``keyword`` 对 message/event_type/reason
        做子串匹配。``min_sequence`` 用于“只取比某序号更新的”（增量刷新）。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if severity:
            clauses.append("severity=?")
            params.append(severity)
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if device_id:
            clauses.append("device_id=?")
            params.append(device_id)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        if keyword:
            like = f"%{keyword}%"
            clauses.append("(message LIKE ? OR event_type LIKE ? OR reason LIKE ?)")
            params += [like, like, like]
        if min_sequence is not None:
            clauses.append("sequence>?")
            params.append(int(min_sequence))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = "DESC" if newest_first else "ASC"
        rows = self.query(
            f"SELECT * FROM events{where} ORDER BY sequence {order} LIMIT ?",
            (*params, int(limit)),
        )
        return [dict(r) for r in rows]

    def distinct_event_types(self) -> list[str]:
        """出现过的事件类型（供过滤下拉）。"""
        rows = self.query(
            "SELECT DISTINCT event_type FROM events"
            " WHERE event_type IS NOT NULL AND event_type<>''"
            " ORDER BY event_type")
        return [r["event_type"] for r in rows]

    def distinct_event_devices(self) -> list[str]:
        """事件里出现过的 device_id（可能有空）。"""
        rows = self.query(
            "SELECT DISTINCT device_id FROM events"
            " WHERE device_id IS NOT NULL AND device_id<>''"
            " ORDER BY device_id")
        return [r["device_id"] for r in rows]

    def distinct_event_runs(self) -> list[str]:
        rows = self.query(
            "SELECT DISTINCT run_id FROM events"
            " WHERE run_id IS NOT NULL AND run_id<>'' ORDER BY run_id")
        return [r["run_id"] for r in rows]


__all__ = [
    "LowDiskError", "PENDING_COMMAND_STATES", "SqliteStore", "StorageError",
]
