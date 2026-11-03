"""示教点登记（「运动方案」页「读取当前关节并登记」，DEC-067）。

示教点 = 命名关节目标（``JointTarget``），执行方案运动步骤用 ``joint_target_ref``
引用它。文件格式与 ``main.py real`` 启动载入完全一致::

    targets:
      - target_id: teach_01            # 配方引用的键（旧键名 station_id 兼容保留）
        joint_deg: [j1, j2, j3, j4, j5, j6]   # 度，恰好 6 项
        waypoint_ref: taught_20261022_170000  # 现场可追溯标识（占位标记会被运动闸拒绝）
        confirmed: true
        taught_at: 2026-10-22T09:00:00+00:00  # UTC 存储；界面按北京时间显示
        source: 运动方案页-读取当前关节

约定（用户裁定 2026-10-22）：

- 示教值**只来自已连接机械臂的当前关节读数**（界面层负责在离线/未连接时拒绝）；
  本服务只做结构校验，不接收手填值。
- 登记条目一律 ``confirmed: true``（读到的就是现场实测值）；``waypoint_ref`` 自动生成，
  保证不是占位标记，真实运动闸才会放行。
- **只动 ``targets`` 列表**：文件其余内容与注释原样保留（ruamel round-trip；当前种子
  文件是纯注释、加载为 None，此时把原文作为**前导注释**保留，写回时放回文件开头）。
- 每次变更立即落盘：``atomic_write(..., backup=True)``（同目录 ``.bak``），失败可
  :meth:`rollback` 回滚。
- 写盘成功后把**共享的已确认字典**（配方引用目录与任务执行共用同一 dict 对象）就地
  同步 —— 无需重启即可在「执行方案」下拉里引用、启动校验与执行都可用。
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from application.contracts.targets import JointTarget
from application.infrastructure import logging_setup
from application.services.recipes.service import atomic_write, rollback

_log = logging_setup.get_logger("services.teach_points")

_SOURCE_UI = "运动方案页-读取当前关节"
# 北京时间 = UTC+8（与 presentation.timefmt 同一口径；服务层不反向依赖界面层）
_BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
# 文件不存在时首次写入用的说明头（已有文件的注释原样保留，不覆盖）
_DEFAULT_HEADER = (
    "# 示教点（「运动方案」页「读取当前关节并登记」维护；DEC-067）。\n"
    "# 数值来自现场真实机械臂的当前关节读数（deg，恰好 6 项），登记即 confirmed: true。\n"
    "# 执行方案运动步骤的 joint_target_ref 引用这里的 target_id。\n"
)


@dataclass(frozen=True)
class TeachPointEntry:
    """文件里的一条示教点（原样字段，不补默认值）。"""

    target_id: str
    joint_deg: tuple[float, ...]
    waypoint_ref: str
    confirmed: bool
    taught_at: str = ""
    source: str = ""

    def as_joint_target(self) -> JointTarget | None:
        """已确认且有 waypoint_ref 才转成运行/引用用的 JointTarget。"""
        if not (self.confirmed and self.waypoint_ref):
            return None
        return JointTarget(joint_deg=self.joint_deg, waypoint_ref=self.waypoint_ref)


class TeachPointStore:
    """示教点文件读写；`confirmed_targets` 是运行栈共享的已确认字典（就地同步）。"""

    def __init__(self, path: str | None,
                 *, confirmed_targets: dict[str, JointTarget] | None = None,
                 clock=None) -> None:
        self.path = path
        # 与 ReferenceCatalog / TaskService 共用**同一个 dict 对象**：add/remove 后就地更新
        self._shared = confirmed_targets if confirmed_targets is not None else {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._yaml = YAML()
        self._yaml.preserve_quotes = True
        self._doc = None
        self._preamble = ""
        self._entries: list[TeachPointEntry] = []
        self.load()

    # ------------------------------------------------------------- 读取
    def load(self) -> None:
        """重新读文件（不存在 = 空清单），并刷新共享字典。"""
        self._doc = None
        self._preamble = ""
        self._entries = []
        if self.path and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                text = fh.read()
            doc = self._yaml.load(text)
            if doc is None:
                # 纯注释文件（当前种子就是）：保留原文作前导注释
                self._preamble = text if text.strip() else ""
                doc = CommentedMap()
            self._doc = doc
            self._read_entries()
        elif self.path:
            self._preamble = _DEFAULT_HEADER     # 文件还没有：首次写入带上说明头
        if self._doc is None:
            self._doc = CommentedMap()
        self._refresh_shared()

    def entries(self) -> list[TeachPointEntry]:
        return list(self._entries)

    def find(self, target_id: str) -> TeachPointEntry | None:
        key = (target_id or "").strip()
        return next((e for e in self._entries if e.target_id == key), None)

    def _raw_targets(self) -> list:
        if self._doc is None:
            return []
        targets = self._doc.get("targets") if hasattr(self._doc, "get") else None
        return targets if isinstance(targets, list) else []

    def _read_entries(self) -> None:
        out: list[TeachPointEntry] = []
        for item in self._raw_targets():
            if not isinstance(item, dict):
                continue
            tid = str(item.get("target_id") or item.get("station_id") or "").strip()
            if not tid:
                continue
            raw = item.get("joint_deg")
            if not isinstance(raw, (list, tuple)) or len(raw) != 6:
                continue          # 畸形条目原样留在文件里，只是不进清单
            try:
                joint = tuple(float(v) for v in raw)
            except (TypeError, ValueError):
                continue
            out.append(TeachPointEntry(
                target_id=tid,
                joint_deg=joint,
                waypoint_ref=str(item.get("waypoint_ref") or "").strip(),
                confirmed=bool(item.get("confirmed", False)),
                taught_at=str(item.get("taught_at") or "").strip(),
                source=str(item.get("source") or "").strip(),
            ))
        self._entries = out

    def _refresh_shared(self) -> None:
        """就地同步共享字典（catalog 与 task 共用同一对象，不能整体替换）。"""
        fresh: dict[str, JointTarget] = {}
        for entry in self._entries:
            jt = entry.as_joint_target()
            if jt is not None:
                fresh[entry.target_id] = jt
        self._shared.clear()
        self._shared.update(fresh)

    # ------------------------------------------------------------- 写入
    def add(self, target_id: str, joint_deg, *, waypoint_ref: str | None = None,
            overwrite: bool = False) -> TeachPointEntry:
        """登记一条示教点并立即写盘。

        - ``joint_deg`` 必须恰好 6 个有限数（``JointTarget`` 同口径，不补 0）；
        - ``target_id`` 重复且 ``overwrite=False`` → ``ValueError``；
        - ``waypoint_ref`` 缺省按北京时间生成 ``taught_YYYYmmdd_HHMMSS``。
        """
        tid = (target_id or "").strip()
        if not tid:
            raise ValueError("示教点名称不能为空")
        if not self.path:
            raise RuntimeError("示教点文件路径未配置，无法登记")
        if any(ch in tid for ch in "\r\n\t"):
            raise ValueError("示教点名称不能包含换行/制表符")
        vals = JointTarget(joint_deg=tuple(joint_deg)).joint_deg   # 结构校验（6 项有限数）
        exists = self.find(tid) is not None
        if exists and not overwrite:
            raise ValueError(f"示教点 {tid!r} 已存在（需覆盖请确认覆盖）")
        ref = (waypoint_ref or self._default_waypoint_ref()).strip()
        if not ref:
            raise ValueError("waypoint_ref 不能为空")
        now = self._clock()
        taught_at = now.replace(microsecond=0).isoformat()
        entry = TeachPointEntry(target_id=tid, joint_deg=tuple(float(v) for v in vals),
                                waypoint_ref=ref, confirmed=True,
                                taught_at=taught_at, source=_SOURCE_UI)
        raw = self._make_raw(entry)
        targets = self._raw_targets()
        self._doc["targets"] = targets
        if exists:
            for i, item in enumerate(targets):
                item_id = str(item.get("target_id") or item.get("station_id") or "").strip() \
                    if isinstance(item, dict) else ""
                if item_id == tid:
                    targets[i] = raw
                    break
        else:
            targets.append(raw)
        self._save_and_reload()
        _log.info("示教点已登记 target=%s joint_deg=%s waypoint_ref=%s",
                  tid, list(entry.joint_deg), ref)
        return entry

    def remove(self, target_id: str) -> TeachPointEntry:
        """删除一条示教点并立即写盘；不存在抛 ``KeyError``。"""
        tid = (target_id or "").strip()
        entry = self.find(tid)
        if entry is None:
            raise KeyError(f"示教点 {tid!r} 不存在")
        if not self.path:
            raise RuntimeError("示教点文件路径未配置，无法删除")
        targets = self._raw_targets()
        kept = []
        for item in targets:
            item_id = str(item.get("target_id") or item.get("station_id") or "").strip() \
                if isinstance(item, dict) else ""
            if item_id != tid:
                kept.append(item)
        self._doc["targets"] = kept
        self._save_and_reload()
        _log.info("示教点已删除 target=%s", tid)
        return entry

    @staticmethod
    def _make_raw(entry: TeachPointEntry) -> CommentedMap:
        raw = CommentedMap()
        raw["target_id"] = entry.target_id
        raw["joint_deg"] = [float(v) for v in entry.joint_deg]
        raw["waypoint_ref"] = entry.waypoint_ref
        raw["confirmed"] = True
        raw["taught_at"] = entry.taught_at
        raw["source"] = entry.source
        return raw

    def _default_waypoint_ref(self) -> str:
        return "taught_" + self._clock().astimezone(_BEIJING_TZ).strftime("%Y%m%d_%H%M%S")

    def _dump_text(self) -> str:
        buf = io.StringIO()
        self._yaml.dump(self._doc, buf)
        body = buf.getvalue()
        if self._preamble.strip():
            return self._preamble.rstrip("\n") + "\n\n" + body
        return body

    def _save_and_reload(self) -> None:
        if not self.path:
            raise RuntimeError("示教点文件路径未配置，无法保存")
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        atomic_write(self.path, self._dump_text(), backup=True)
        self.load()

    def rollback(self) -> None:
        """回滚到上一份 ``.bak``（写盘失败/现场误操作后使用）。"""
        rollback(self.path)
        self.load()

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"TeachPointStore(path={self.path!r}, entries={len(self._entries)})"


__all__ = ["TeachPointEntry", "TeachPointStore"]
