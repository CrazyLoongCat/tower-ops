"""DEC-072 OpenCV 圆检测后端（Linux 宿主）与宿主后端选择。

覆盖：合成图找圆（中心/半径/完整度）、circle_type 选择、半径过滤、ROI、
空图、与 ``parse_native_circles`` 的契约、``--backend`` 选择逻辑、跨进程 IPC、
主程序侧 ``circle38_host``（Linux 无扩展名）自动发现。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import cv2
import pytest

from application.algorithms.circle.normalize import parse_native_circles
from application.algorithms.circle.params import CircleParams
from application.bootstrap.circle_host import find_circle_host, resolve_circle_host_cmd
from application.hosts.circle38.client import Circle38Proxy, SubprocessTransport
from application.hosts.circle38.entry import build_backend
from application.hosts.circle38.opencv_backend import OpenCVCircleBackend

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _image_with(circles: list[tuple[int, int, int]], w: int = 640, h: int = 480):
    img = np.full((h, w, 3), 255, np.uint8)
    for cx, cy, r in circles:
        cv2.circle(img, (cx, cy), r, (0, 0, 0), 2)
    return img


@pytest.fixture(scope="module")
def backend():
    b = OpenCVCircleBackend()
    b.load()
    return b


def test_detects_synthetic_circle_and_parses_like_native(backend):
    raw = backend.detect([_image_with([(320, 240, 40)])], [[2, 0, 0, 640, 480, 0]],
                         CircleParams(circle_type="所有圆"))
    circles, _metrics = parse_native_circles(raw)
    assert len(circles) == 1
    c = circles[0]
    assert abs(c.cx - 320) <= 2.0 and abs(c.cy - 240) <= 2.0
    assert abs(c.radius_px - 40) <= 2.5
    assert c.coverage is not None and c.coverage > 0.95


def test_circle_type_selection_and_radius_filter(backend):
    img = _image_with([(200, 240, 30), (450, 240, 60)])
    rect = [[2, 0, 0, 640, 480, 0]]

    def rs(ct, **kw):
        raw = backend.detect([img], rect, CircleParams(circle_type=ct, **kw))
        return sorted(round(c[3], 0) for c in raw["circles_for_show"][0])

    assert rs("最大圆") == [60.0]
    assert rs("最小圆") == [30.0]
    assert rs("所有圆") == [30.0, 60.0]
    assert rs("最大圆和最小圆") == [30.0, 60.0]
    assert rs("所有圆", min_radius=40, max_radius=80) == [60.0]
    with pytest.raises(ValueError):
        backend.detect([img], rect, CircleParams(circle_type="不存在的类型"))


def test_roi_limits_search_and_blank_has_no_circle(backend):
    img = _image_with([(320, 240, 40)])
    miss = backend.detect([img], [[2, 0, 0, 200, 150, 0]], CircleParams(circle_type="所有圆"))
    assert parse_native_circles(miss)[0] == []
    hit = backend.detect([img], [[2, 250, 170, 150, 150, 0]], CircleParams(circle_type="所有圆"))
    assert len(parse_native_circles(hit)[0]) == 1
    blank = np.full((480, 640, 3), 255, np.uint8)
    raw = backend.detect([blank], [], CircleParams(circle_type="所有圆"))
    assert parse_native_circles(raw)[0] == []
    assert raw["circle_num"] == [[0]]


def test_gray_input_supported(backend):
    gray = cv2.cvtColor(_image_with([(320, 240, 40)]), cv2.COLOR_BGR2GRAY)
    raw = backend.detect([gray], [[2, 0, 0, 640, 480, 0]], CircleParams(circle_type="所有圆"))
    assert len(parse_native_circles(raw)[0]) == 1


# ---------------------------------------------------------------- 后端选择
def test_backend_auto_prefers_native_only_when_present(tmp_path):
    # 空目录没有原生件 -> OpenCV
    b = build_backend("auto", str(tmp_path))
    assert b.host_version == "opencv-linux"
    # 放一个无法导入的假 .so -> 原生加载失败后明确改用 OpenCV（不静默、不崩）
    (tmp_path / "timesai_tools.so").write_bytes(b"not-a-real-extension")
    b2 = build_backend("auto", str(tmp_path))
    assert b2.host_version == "opencv-linux"
    assert build_backend("stub", None).host_version == "stub-offline"


# ---------------------------------------------------------------- 跨进程 IPC
def _host_cmd(*extra: str) -> list[str]:
    return [sys.executable, "-m", "application.hosts.circle38.entry", *extra]


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def test_process_ipc_opencv_backend_end_to_end():
    """真实子进程 + OpenCV 后端：请求→找圆→响应，宿主 EOF 后干净退出 (0)。"""
    t = SubprocessTransport(_host_cmd("--backend", "opencv"), env=_env())
    try:
        proxy = Circle38Proxy(t, default_deadline_ms=20000)
        img = _image_with([(320, 240, 40)])
        raw = proxy.detect([img], [[2, 0, 0, 640, 480, 0]],
                           CircleParams(circle_type="所有圆"))
        circles, _ = parse_native_circles(raw)
        assert len(circles) == 1
        assert abs(circles[0].cx - 320) <= 2.0
    finally:
        t.close()
    assert t.last_returncode == 0


# ---------------------------------------------------------------- 宿主发现
def test_linux_host_name_is_discovered(tmp_path):
    """Linux 宿主无 .exe：主程序同目录的 circle38_host 必须被自动发现。"""
    direct = tmp_path / "circle38_host"
    direct.write_text("#!/bin/sh\n", encoding="utf-8")
    assert find_circle_host(str(tmp_path)) == str(direct)
    cmd, note = resolve_circle_host_cmd("", root=str(tmp_path))
    assert cmd == [str(direct)] and "自动发现" in note
    # onedir 布局：circle38_host/circle38_host
    direct.unlink()
    sub = tmp_path / "circle38_host"
    sub.mkdir()
    inner = sub / "circle38_host"
    inner.write_text("#!/bin/sh\n", encoding="utf-8")
    assert find_circle_host(str(tmp_path)) == str(inner)
