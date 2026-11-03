"""视觉检测（命名保存）的模型与存储 (E20 × E19 结合)。

视觉调试页把一次**调试结束的检测**命名保存下来，配方 ``vision.run`` 步骤就能
引用它（``StepModel.detection_ref``）。一条检测记录的是：

- ``camera_ref``   —— 在哪个相机通道上调的（信息性；判定时上游抓拍步骤已给帧）；
- ``pipeline_ref`` —— 用哪条检测流水线（如 ``approved_ds86_circle_pipeline``）；
- ``params``       —— 保存那一刻的参数快照（best-effort，可能为空）；
- ``params_hash``  —— 快照的哈希，便于日后核对"当时到底配了什么"；
- ``source``       —— 来源标注（在哪个页面、什么时候存的）。

**诚实边界**：离线时"参数快照"来自进程内 stub 流水线，不等于真机现场值。
保存的是**你调完的配置**，不代表本工程现场已复核。

存储风格与设备配置/运动方案一致：单文件 YAML、原子落盘（``atomic_write``）+
``.bak`` 备份、``rollback`` 可回退。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from io import StringIO
from typing import Any

from ruamel.yaml import YAML

from application.services.recipes.service import atomic_write, rollback
from application.infrastructure import logging_setup

_log = logging_setup.get_logger("services.vision_detections")

CONFIG_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)


def params_hash(params: dict[str, Any]) -> str:
    """参数快照的稳定哈希（排序键 + 非 ASCII 原样），用于日后核对。"""
    payload = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class VisionDetection:
    """一条命名视觉检测（通道 + 流水线 + 参数快照）。"""

    detection_id: str
    label: str = ""
    camera_ref: str = ""
    pipeline_ref: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    params_hash: str = ""
    source: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        self.detection_id = str(self.detection_id).strip()
        if not self.detection_id:
            raise ValueError("detection_id 不能为空")
        self.pipeline_ref = str(self.pipeline_ref).strip()
        if not self.pipeline_ref:
            raise ValueError("pipeline_ref 不能为空（检测必须绑定一条流水线）")
        self.camera_ref = str(self.camera_ref or "").strip()
        self.label = str(self.label or "").strip()
        self.params = dict(self.params or {})
        if not self.params_hash:
            self.params_hash = params_hash(self.params)

    def clone(self) -> "VisionDetection":
        return replace(self, params=dict(self.params))

    def as_dict(self) -> dict[str, Any]:
        return {
            "detection_id": self.detection_id,
            "label": self.label,
            "camera_ref": self.camera_ref,
            "pipeline_ref": self.pipeline_ref,
            "params": dict(self.params),
            "params_hash": self.params_hash,
            "source": self.source,
            "note": self.note,
        }


def _dump_yaml(data: dict) -> str:
    yml = YAML(typ="safe")
    yml.default_flow_style = False
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.sort_base_mapping_type_on_output = False
    buf = StringIO()
    yml.dump(data, buf)
    return buf.getvalue()


def _load_yaml(text: str) -> dict:
    return YAML(typ="safe").load(text) or {}


class VisionDetectionStore:
    """视觉检测的读写与增删改；单文件原子落盘。"""

    def __init__(self, path: str | None = None, *, seed_if_missing: bool = True) -> None:
        self.path = os.path.abspath(path) if path else None
        self._seed_if_missing = seed_if_missing
        self._items: list[VisionDetection] | None = None

    # ---------- 读/写 ----------
    def load(self) -> list[VisionDetection]:
        if self.path and os.path.exists(self.path):
            self._items = self.from_dict(_load_yaml(open(self.path, encoding="utf-8").read()))
        else:
            self._items = []
            if self.path and self._seed_if_missing:
                self.save()
        return self._items

    def save(self) -> None:
        if self._items is None:
            raise RuntimeError("尚未 load()")
        if self.path is None:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        header = ("# 视觉检测（命名保存，「视觉调试」页另存）。由程序维护。\n"
                  "# 离线参数快照来自进程内 stub 流水线，不代表现场已复核。\n")
        atomic_write(self.path, header + _dump_yaml(self.to_dict()), backup=True)

    def rollback(self) -> None:
        if self.path:
            rollback(self.path)
            self._items = None

    @property
    def detections(self) -> list[VisionDetection]:
        if self._items is None:
            self.load()
        assert self._items is not None
        return self._items

    # ---------- 查询 ----------
    def find(self, detection_id: str) -> VisionDetection | None:
        return next((d for d in self.detections if d.detection_id == detection_id), None)

    def ids(self) -> list[str]:
        return sorted(d.detection_id for d in self.detections)

    # ---------- CRUD ----------
    def add(self, det: VisionDetection) -> VisionDetection:
        if self.find(det.detection_id) is not None:
            raise ValueError(f"检测 ID {det.detection_id!r} 已存在")
        self.detections.append(det)
        return det

    def upsert(self, det: VisionDetection) -> VisionDetection:
        """存在则整条替换，否则追加（"另存"同名即覆盖，省得被迫先删）。"""
        for i, d in enumerate(self.detections):
            if d.detection_id == det.detection_id:
                self.detections[i] = det
                _log.info("视觉检测已更新(另存) %s pipeline=%s", det.detection_id,
                          det.pipeline_ref)
                return det
        self.detections.append(det)
        _log.info("视觉检测已新增 %s pipeline=%s", det.detection_id, det.pipeline_ref)
        return det

    def update(self, detection_id: str, det: VisionDetection) -> VisionDetection:
        for i, d in enumerate(self.detections):
            if d.detection_id == detection_id:
                if det.detection_id != detection_id and self.find(det.detection_id):
                    raise ValueError(f"检测 ID {det.detection_id!r} 已被占用")
                self.detections[i] = det
                return det
        raise KeyError(f"未找到检测 {detection_id!r}")

    def remove(self, detection_id: str) -> None:
        self._items = [d for d in self.detections if d.detection_id != detection_id]

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "detections": [d.as_dict() for d in self.detections],
        }

    @classmethod
    def from_dict(cls, data: dict) -> list[VisionDetection]:
        if not isinstance(data, dict):
            raise ValueError("视觉检测配置顶层必须是映射")
        version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"不支持的 schema_version={version!r}")
        out: list[VisionDetection] = []
        seen: set[str] = set()
        for raw in (data.get("detections") or []):
            det = VisionDetection(
                detection_id=str(raw["detection_id"]),
                label=str(raw.get("label", "")),
                camera_ref=str(raw.get("camera_ref", "")),
                pipeline_ref=str(raw.get("pipeline_ref", "")),
                params=dict(raw.get("params") or {}),
                params_hash=str(raw.get("params_hash", "")),
                source=str(raw.get("source", "")),
                note=str(raw.get("note", "")),
            )
            if det.detection_id in seen:
                raise ValueError(f"检测 ID 重复: {det.detection_id!r}")
            seen.add(det.detection_id)
            out.append(det)
        return out


__all__ = [
    "CONFIG_SCHEMA_VERSION", "SUPPORTED_SCHEMA_VERSIONS", "VisionDetection",
    "VisionDetectionStore", "params_hash",
]
