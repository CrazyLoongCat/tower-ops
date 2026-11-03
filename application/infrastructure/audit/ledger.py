"""追加式审计账本 (E17, 02 §11, 10 F3)。

- 记录 用户/时间/动作/对象/前后 hash/原因/结果;
- **只追加**: 本模块不提供 update/delete 接口;
- 明确局限: 普通 SQLite 文件**不承诺防篡改** (tamper_proof=False), 需防篡改应另加
  外部机制; 文档与接口均不宣称防篡改;
- 秘密不入日志: 写入前按键名模式脱敏;
- 写失败**显式抛出** (AuditWriteError), 由调用方决定是否 fail-closed 拒绝该操作。
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

AUDIT_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  utc TEXT NOT NULL, user_id TEXT, role TEXT, action TEXT NOT NULL,
  object_type TEXT, object_id TEXT, before_hash TEXT, after_hash TEXT,
  reason TEXT, result TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS audit_meta (key TEXT PRIMARY KEY, value TEXT);
"""

# 命中即脱敏的键名片段 (秘密不进入日志)
SECRET_KEY_HINTS = ("password", "passwd", "secret", "token", "credential",
                    "private_key", "apikey", "api_key", "passphrase")

REDACTED = "<redacted>"


class AuditWriteError(RuntimeError):
    """审计写入失败 (调用方可据此拒绝敏感操作)."""


def redact(payload: object) -> object:
    """递归脱敏: 键名命中 SECRET_KEY_HINTS 的值替换为 <redacted>."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if any(h in str(k).lower() for h in SECRET_KEY_HINTS):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(payload, list):
        return [redact(v) for v in payload]
    return payload


@dataclass(frozen=True)
class AuditEntry:
    utc: str
    user_id: str
    role: str
    action: str
    object_type: str | None = None
    object_id: str | None = None
    before_hash: str | None = None
    after_hash: str | None = None
    reason: str | None = None
    result: str = "OK"
    detail: dict = field(default_factory=dict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditLedger:
    """SQLite 追加式审计账本. 单所有者串行访问。"""

    tamper_proof = False          # 诚实标注: 普通 SQLite 不防篡改
    schema_version = AUDIT_SCHEMA_VERSION

    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self.fail_writes = False   # 测试注入: 写失败策略

    def open(self) -> None:
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO audit_meta(key, value) VALUES('schema_version', ?)",
            (str(self.schema_version),))
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise AuditWriteError("审计账本未打开")
        return self._conn

    # ---- 追加 ----
    def append(self, entry: AuditEntry) -> None:
        if self.fail_writes:
            raise AuditWriteError("注入: 审计写失败")
        detail = json.dumps(redact(entry.detail), ensure_ascii=False, default=str)
        try:
            self.conn.execute(
                "INSERT INTO audit_log(utc, user_id, role, action, object_type,"
                " object_id, before_hash, after_hash, reason, result, detail)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (entry.utc or _utc_now(), entry.user_id, entry.role, entry.action,
                 entry.object_type, entry.object_id, entry.before_hash,
                 entry.after_hash, entry.reason, entry.result, detail))
            self.conn.commit()
        except sqlite3.Error as exc:
            raise AuditWriteError(f"审计写入失败: {exc}") from exc

    # ---- 查询/导出 ----
    def query(self, *, limit: int = 100, object_id: str | None = None,
              user_id: str | None = None, action: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit_log"
        where, args = [], []
        if object_id:
            where.append("object_id=?"); args.append(object_id)
        if user_id:
            where.append("user_id=?"); args.append(user_id)
        if action:
            where.append("action=?"); args.append(action)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def export(self, path: str, **filters) -> str:
        rows = self.query(limit=100000, **filters)
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        return path

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]


__all__ = [
    "AUDIT_SCHEMA_VERSION", "AuditEntry", "AuditLedger", "AuditWriteError",
    "REDACTED", "SECRET_KEY_HINTS", "redact",
]
