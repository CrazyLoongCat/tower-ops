"""操作台计算配置 manual_ops.yaml（DEC-148）的读写用例。

覆盖：缺文件用默认（两操作各一套「默认」算法）、选择持久化后可重载、
算法参数合并默认项、非法/未知算法名回退默认。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from application.services.operation_config import (  # noqa: E402
    BOLT, DEFAULTS, TWR, OperationConfigStore,
)


def test_defaults_when_file_missing(tmp_path):
    store = OperationConfigStore(str(tmp_path / "manual_ops.yaml"))
    cfg = store.load()
    assert set(cfg) == {TWR, BOLT}
    assert store.algorithm_names(TWR) == ["默认"]
    assert store.algorithm_names(BOLT) == ["默认"]
    p = store.algorithm_params(TWR)
    assert p["tol_mm"] == DEFAULTS[TWR]["tol_mm"]
    assert p["scan"] == "auto"
    assert store.algorithm_params(BOLT)["circle_type"] == "最大圆"


def test_selection_persists_roundtrip(tmp_path):
    path = str(tmp_path / "manual_ops.yaml")
    store = OperationConfigStore(path)
    store.load()
    store.set_selection(TWR, camera="d405_01", algorithm="检测-塔A")
    store.set_selection(BOLT, camera="ds86_01")
    store.save()
    assert os.path.isfile(path)

    again = OperationConfigStore(path)
    again.load()
    # DEC-149：算法名可以是视觉检测 id（不要求在 algorithms 里）
    assert again.selection(TWR) == {"camera": "d405_01", "algorithm": "检测-塔A"}
    assert again.selection(BOLT)["camera"] == "ds86_01"


def test_value_mm_roundtrip(tmp_path):
    """DEC-151：全局参数里的计算值（杆塔宽度/螺栓直径）可存可读。"""
    path = str(tmp_path / "manual_ops.yaml")
    store = OperationConfigStore(path)
    store.load()
    assert store.value(TWR) == 0.0
    store.set_value(TWR, 246.6)
    store.set_value(BOLT, 21.7)
    store.save()
    again = OperationConfigStore(path)
    again.load()
    assert again.value(TWR) == pytest_approx(246.6)
    assert again.value(BOLT) == pytest_approx(21.7)


def pytest_approx(v):  # 不依赖 pytest 的近似比较
    return type("A", (), {"__eq__": lambda self, x: abs(x - v) <= 1e-6})()


def test_nearest_bolt_size(tmp_path):
    """DEC-154：螺栓直径取 16/20/24 三档里与测量值最接近的一项。"""
    from application.services.operation_config import BOLT_SIZES, nearest_bolt_size
    assert BOLT_SIZES == (16.0, 20.0, 24.0)
    assert nearest_bolt_size(21.7) == 20.0
    assert nearest_bolt_size(23.4) == 24.0
    assert nearest_bolt_size(10.0) == 16.0
    assert nearest_bolt_size(100.0) == 24.0
    assert nearest_bolt_size(None) == 16.0


def test_params_defaults_merged_and_unknown_algo_falls_back(tmp_path):
    path = str(tmp_path / "manual_ops.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("tower_width:\n  camera: x\n  algorithm: 高塔\n"
                 "  algorithms:\n    高塔:\n      tol_mm: 12.0\n")
    store = OperationConfigStore(path)
    store.load()
    p = store.algorithm_params(TWR)                     # 选中的「高塔」
    assert p["tol_mm"] == 12.0
    assert p["min_span"] == DEFAULTS[TWR]["min_span"]    # 未写的项用默认补齐
    store.set_selection(TWR, algorithm="检测-螺栓A")      # DEC-149：任意名字可存
    assert store.selection(TWR)["algorithm"] == "检测-螺栓A"
    # 名字不在 algorithms 里时，参数回退「默认」（无「默认」则第一套）——
    # 本测试文件只有「高塔」，因此回退到它的参数
    assert store.algorithm_params(TWR)["tol_mm"] == 12.0
