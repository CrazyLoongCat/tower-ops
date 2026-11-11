"""深度图统计脚本（scripts/depth_stats.py，DEC-127）的离线用例。

纯 numpy/cv2，无 Qt、不连设备、不碰仓库活配置；合成深度图验证统计口径与
逆深度 RANSAC 的平面法线/倾角换算。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
_SCRIPT = os.path.join(_ROOT, "scripts", "depth_stats.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("depth_stats", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["depth_stats"] = mod
    spec.loader.exec_module(mod)
    return mod


def _args(**over):
    base = dict(depth_scale=None, invalid_threshold=65535.0,
                fx=None, fy=None, cx=None, cy=None,
                planes=1, tol_mm=8.0, iterations=200, sample=20000,
                min_plane_ratio=0.01, seed=7, no_histogram=False)
    base.update(over)
    return SimpleNamespace(**base)


def _tilted_plane_depth(h=480, w=640, fx=800.0, fy=800.0, cx=320.0, cy=240.0,
                        normal=(0.2, 0.1), k=250.0, noise_mm=0.5, seed=0):
    """按已知平面 n·P=k 生成深度图（uint16，1 原始值=1mm）。返回 (深度, 单位法线)。"""
    n = np.array([normal[0], normal[1], np.sqrt(1.0 - normal[0] ** 2 - normal[1] ** 2)])
    u, v = np.meshgrid(np.arange(w, dtype=np.float64), np.arange(h, dtype=np.float64))
    den = n[0] * (u - cx) / fx + n[1] * (v - cy) / fy + n[2]
    z = k / den
    if noise_mm:
        z = z + np.random.default_rng(seed).normal(0, noise_mm, z.shape)
    return np.round(z).clip(0, 65535).astype(np.uint16), n


def test_tilted_plane_stats_and_normal(tmp_path):
    mod = _load_script()
    depth, n_true = _tilted_plane_depth()
    path = tmp_path / "plane_depth.npy"
    np.save(path, depth)

    res, _extras = mod.analyze_image(str(path), _args(
        fx=800.0, fy=800.0, cx=320.0, cy=240.0))

    d = res["depth"]
    assert res["valid_ratio"] == 1.0
    assert d["percentiles_mm"]["p01"] <= d["median_mm"] <= d["percentiles_mm"]["p99"]
    assert abs(d["min_mm"] - 229.0) <= 3.0 and abs(d["max_mm"] - 290.0) <= 3.0

    planes = res["planes"]
    assert len(planes) == 1
    p = planes[0]
    # 整帧都是一块平面：面积占比≈100%，RMS 小于噪声容差
    assert p["ratio_of_valid"] > 0.98
    assert p["rms_mm"] < 2.0
    # 法线/倾角与真值一致（DEC 换算：t=α·fx·x_n+…）
    tilt_true = float(np.degrees(np.arccos(n_true[2])))
    assert abs(p["tilt_deg_vs_axis"] - tilt_true) < 0.2
    got = np.array(p["normal_xyz"])
    assert np.allclose(got, n_true, atol=0.01)
    # 主点处光轴深度 = k/nz
    assert abs(p["depth_at_principal_mm"] - 250.0 / n_true[2]) < 2.0


def test_invalid_pixels_excluded(tmp_path):
    mod = _load_script()
    depth, _n = _tilted_plane_depth(noise_mm=0.0)
    h, w = depth.shape
    blocked = depth.copy()
    blocked[: h // 4, : w // 4] = 0            # 无回波
    blocked[-h // 4:, -w // 4:] = 65535        # 无效值
    path = tmp_path / "blocked_depth.npy"
    np.save(path, blocked)

    res, _extras = mod.analyze_image(str(path), _args())
    total = h * w
    assert res["zero_pixels"] == (h // 4) * (w // 4)
    assert res["saturated_pixels"] == (h // 4) * (w // 4)
    assert res["valid_ratio"] == pytest.approx(1.0 - 0.25 * 0.25 * 2, abs=1e-6)
    # 平面只在有效像素里找，占比按有效像素算
    assert res["planes"] and res["planes"][0]["ratio_of_valid"] > 0.95
    assert res["depth"]["min_mm"] > 0


def test_all_invalid_and_color_skip(tmp_path):
    mod = _load_script()
    bad = tmp_path / "empty_depth.npy"
    np.save(bad, np.full((10, 10), 65535, np.uint16))
    res, _extras = mod.analyze_image(str(bad), _args())
    assert res["valid_pixels"] == 0 and res["depth"] is None
    assert res["planes"] == []

    color = tmp_path / "cam-f1_color.npy"
    np.save(color, np.zeros((4, 4, 3), np.uint8))
    with pytest.raises(mod.NonDepthError):
        mod.load_depth_array(str(color))


def test_meta_pairing_and_summary_csv(tmp_path):
    mod = _load_script()
    depth, _n = _tilted_plane_depth(h=120, w=160)
    npy = tmp_path / "shot01_depth.npy"
    np.save(npy, depth)
    meta = {"depth_scale_m": 0.001,
            "intrinsics": {"fx": 200.0, "fy": 200.0, "cx": 80.0, "cy": 60.0}}
    (tmp_path / "shot01_depth.json").write_text(json.dumps(meta), encoding="utf-8")

    assert mod.find_meta_path(str(npy)) == str(tmp_path / "shot01_depth.json")
    res, _extras = mod.analyze_image(str(npy), _args())
    assert res["depth_scale_source"] == "JSON"
    assert res["intrinsics"]["fx"] == 200.0
    assert res["planes"] and "tilt_deg_vs_axis" in res["planes"][0]

    csv_path = mod.write_summary_csv([res, res], str(tmp_path / "summary.csv"),
                                     n_planes=2)
    lines = open(csv_path, encoding="utf-8-sig").read().strip().splitlines()
    assert len(lines) == 3
    assert lines[0].count(",") == lines[1].count(",") == lines[2].count(",")
    assert "plane2_pixels" in lines[0]
