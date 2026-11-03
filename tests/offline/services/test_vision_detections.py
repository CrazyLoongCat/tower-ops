"""命名视觉检测的模型与存储（E20 × E19 结合，DEC-024）。

纯 Python（无 Qt）：一条检测 = 通道 + 流水线 + 参数快照；单文件原子落盘 + `.bak` 可回退。
这里守的是存储契约本身（往返/哈希/重复/回退），页面级行为在 `tests/offline/ui/test_recipes_page_qt.py`。
"""
from __future__ import annotations

import os

import pytest

from application.services.vision_detections import (
    VisionDetection, VisionDetectionStore, params_hash,
)

PIPE = "approved_ds86_circle_pipeline"


def _det(det_id: str = "检测-A", **kw) -> VisionDetection:
    base = dict(camera_ref="ds86_01", pipeline_ref=PIPE, source="test")
    base.update(kw)
    return VisionDetection(detection_id=det_id, **base)


# ---------------------------------------------------------------- 基础校验
def test_detection_needs_id_and_pipeline():
    with pytest.raises(ValueError, match="detection_id"):
        VisionDetection(detection_id="  ", pipeline_ref=PIPE)
    with pytest.raises(ValueError, match="pipeline_ref"):
        VisionDetection(detection_id="检测-A", pipeline_ref="   ")


def test_params_hash_ignores_key_order_and_is_blank_automatically():
    a = _det(params={"threshold_value": 100, "mode": "detect"})
    b = _det(params={"mode": "detect", "threshold_value": 100})
    assert a.params_hash == b.params_hash == params_hash(a.params)
    assert params_hash({"中文": "轮廓"}) == params_hash({"中文": "轮廓"})
    assert params_hash({}) != params_hash({"threshold_value": 0})


# ---------------------------------------------------------------- 落盘/往返
def test_missing_file_seeds_empty_and_is_written_back(tmp_path):
    path = str(tmp_path / "sub" / "detections.yaml")
    store = VisionDetectionStore(path)
    assert store.load() == []
    assert os.path.exists(path)                 # 缺文件时补一份中性种子（目录也会建）
    text = open(path, encoding="utf-8").read()
    assert "schema_version: 1" in text and "detections: []" in text
    assert "不代表现场已复核" in text            # 头部如实写明离线快照的边界


def test_upsert_round_trips_every_field_through_yaml(tmp_path):
    path = str(tmp_path / "detections.yaml")
    store = VisionDetectionStore(path)
    store.load()
    store.upsert(_det(label="圆孔", params={"threshold_value": 88}, note="调完的配置"))
    store.save()

    again = VisionDetectionStore(path)
    again.load()
    (d,) = again.detections
    assert d.detection_id == "检测-A" and d.label == "圆孔"
    assert d.camera_ref == "ds86_01" and d.pipeline_ref == PIPE
    assert d.params == {"threshold_value": 88} and d.note == "调完的配置"
    assert d.params_hash == params_hash({"threshold_value": 88})   # 快照哈希如实重算
    assert again.ids() == ["检测-A"]


def test_upsert_replaces_same_name_but_add_refuses_duplicate(tmp_path):
    store = VisionDetectionStore(str(tmp_path / "d.yaml"))
    store.load()
    store.upsert(_det(label="第一版"))
    store.upsert(_det(label="第二版"))          # "另存"同名即覆盖，不追加第二条
    assert [d.label for d in store.detections] == ["第二版"]
    with pytest.raises(ValueError, match="已存在"):
        store.add(_det())
    store.add(_det("检测-B"))
    with pytest.raises(ValueError, match="已被占用"):
        store.update("检测-B", _det("检测-A"))
    assert store.ids() == ["检测-A", "检测-B"]


def test_remove_and_rollback_go_back_to_the_previous_file(tmp_path):
    path = str(tmp_path / "detections.yaml")
    store = VisionDetectionStore(path)
    store.load()
    store.add(_det("检测-A"))
    store.save()
    store.add(_det("检测-B"))
    store.save()                                # 这一步把"只有 A"的版本留成 .bak
    assert store.ids() == ["检测-A", "检测-B"]

    store.rollback()
    assert store.ids() == ["检测-A"]             # 回退到上一版，不是清空
    store.remove("检测-A")
    store.save()
    assert VisionDetectionStore(path).load() == []


# ---------------------------------------------------------------- 坏输入
def test_bad_schema_and_duplicate_ids_are_refused(tmp_path):
    with pytest.raises(ValueError, match="schema_version"):
        VisionDetectionStore.from_dict({"schema_version": 9, "detections": []})
    with pytest.raises(ValueError, match="顶层必须是映射"):
        VisionDetectionStore.from_dict(["不是映射"])
    dup = {"schema_version": 1, "detections": [
        _det("同名").as_dict(), _det("同名").as_dict()]}
    with pytest.raises(ValueError, match="重复"):
        VisionDetectionStore.from_dict(dup)
    # 坏文件照样在 load() 时报出来，不静默当空
    bad = tmp_path / "d.yaml"
    bad.write_text("schema_version: 7\ndetections: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        VisionDetectionStore(str(bad)).load()
