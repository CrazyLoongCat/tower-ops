"""离线回放脚本（scripts/debug_saved_frames.py，DEC-125）的配对/回放用例。

纯 numpy/cv2，无 Qt、不连设备、不碰仓库活配置。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_ROOT, "scripts", "debug_saved_frames.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("debug_saved_frames", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["debug_saved_frames"] = mod     # dataclass 需要模块已注册
    spec.loader.exec_module(mod)
    return mod


class _FakeHost:
    def __init__(self, circles=((160.0, 120.0, 28.0),)):
        self.circles = circles

    def detect(self, images, rects, params):
        show = [[1, cx, cy, r] for cx, cy, r in self.circles]
        return {"statues": [1], "error_msg": [""], "circle_num": [[len(show)]],
                "circles_for_show": [show], "circle_centers": [], "radius": []}


def _args(**over):
    base = dict(fx=None, fy=None, cx=None, cy=None, width=None, height=None,
                depth_scale=None, circle_host=None, scheme=None, detection=None,
                real_segmenter=False, display_rotate=270, no_depth_preview=True,
                guidance=None, set=[])
    base.update(over)
    return SimpleNamespace(**base)


def _write_frame(tmp_path):
    h, w = 240, 320
    yy, xx = np.mgrid[0:h, 0:w]
    depth = np.zeros((h, w), dtype="uint16")
    depth[np.hypot(xx - 160, yy - 120) < 30] = 500
    color = np.zeros((h, w, 3), dtype="uint8")
    color[...] = (40, 40, 40)
    cv2.circle(color, (160, 120), 28, (230, 230, 230), 3)
    base = tmp_path / "ds86_01_f1"
    cv2.imwrite(str(base) + ".png", color[:, :, ::-1])       # cv2 写 BGR
    np.save(str(base) + "_depth.npy", depth)
    (tmp_path / "ds86_01_f1_depth.json").write_text(json.dumps({
        "camera_id": "ds86_01", "width": 320, "height": 240, "dtype": "uint16",
        "depth_scale_m": 0.001,
        "intrinsics": {"fx": 200.0, "fy": 200.0, "cx": 160.0, "cy": 120.0,
                       "width": 320, "height": 240, "space": "depth"},
    }, ensure_ascii=False), encoding="utf-8")
    return base


def test_discover_and_load_four_file_set(tmp_path):
    mod = _load_script()
    base = _write_frame(tmp_path)
    entries = mod.discover_frames([str(tmp_path)], _args())
    assert len(entries) == 1
    entry = mod.load_frame(entries[0], _args())
    assert entry.color is not None and entry.color.shape == (240, 320, 3)
    assert entry.depth is not None and entry.depth.dtype == np.uint16
    assert entry.depth_scale_m == 0.001
    assert entry.intrinsics is not None and entry.intrinsics.fx == 200.0
    assert os.path.basename(entry.name) == base.name


def test_guidance_replay_gives_xyz_and_offsets(tmp_path):
    mod = _load_script()
    _write_frame(tmp_path)
    entry = mod.load_frame(mod.discover_frames([str(tmp_path)], _args())[0],
                           _args())
    bundle = mod.to_bundle(entry, 0)
    cfg = mod._guidance_config(None)
    out = mod.run_guidance_replay(
        bundle, cfg, _FakeHost(),
        {"depth.depth_min_mm": 200.0, "depth.depth_max_mm": 3000.0,
         "depth.thresh_method": "binary_inv", "depth.thresh_value": 127,
         "depth.morph_op": "none", "depth.rect_side_length": 100.0}, None)
    assert not out.get("error"), out.get("error")
    assert out["rects"], "深度引导应至少给一个矩形"
    assert out["circles"], "假宿主应返回一个圆"
    circle = out["circles"][0]
    assert circle["cx"] == 160.0 and circle["radius_px"] == 28.0
    assert "xyz_mm" in circle and "coarse_mm" in circle and "fine_mm" in circle
    # 圆心在光心附近 -> 偏移很小
    assert abs(circle["coarse_mm"][0]) < 20 and abs(circle["coarse_mm"][1]) < 20


# ---------------------------------------------------------------- DEC-128 中文路径
def test_frame_import_handles_chinese_paths_without_cv_imread(tmp_path, monkeypatch):
    """Windows 上 cv2.imread 对中文路径会失败；导入必须走 np.fromfile+imdecode。"""
    from application.services import frame_import

    folder = tmp_path / "配置及数据" / "runtime" / "captures"
    folder.mkdir(parents=True)
    h, w = 24, 32
    color = np.full((h, w, 3), 90, dtype="uint8")
    depth = np.full((h, w), 500, dtype="uint16")
    base = folder / "ds86_01_20261105_160709"
    assert frame_import.imwrite_unicode(str(base) + ".png", color)
    np.save(str(base) + "_depth.npy", depth)
    (folder / "ds86_01_20261105_160709_depth.json").write_text(
        json.dumps({"depth_scale_m": 0.001, "width": w, "height": h,
                    "intrinsics": {"fx": 20.0, "fy": 20.0, "cx": 16.0,
                                   "cy": 12.0, "width": w, "height": h}}),
        encoding="utf-8")

    # 模拟 Windows OpenCV：imread 不可用
    def _boom(*_a, **_k):
        raise AssertionError("不应调用 cv2.imread（中文路径会失败）")

    monkeypatch.setattr(frame_import.cv2, "imread", _boom)
    entries = frame_import.discover_frames([str(folder)])
    assert len(entries) == 1
    entry = frame_import.load_frame(entries[0])
    assert entry.color is not None and entry.color.shape == (h, w, 3)
    assert entry.depth is not None and int(entry.depth[0, 0]) == 500
    assert entry.intrinsics is not None

    out = folder / "写出.png"
    assert frame_import.imwrite_unicode(str(out), color)
    assert out.exists() and out.stat().st_size > 0
