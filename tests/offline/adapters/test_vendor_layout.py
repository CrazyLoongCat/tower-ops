"""DEC-078：厂家 SDK / 模型统一收进 `tower-ops/vendor/`，迁移只需一个目录。

覆盖：vendor 目录结构与关键库；`vendor_paths` 默认路径；EdgeSAM 模型解析；
Linux 下 `scepter_sdk` / `Robotic_Arm` 能按平台加载对应的 .so。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
VENDOR = os.path.join(ROOT, "vendor")


def _has(path: str) -> bool:
    return os.path.isfile(path) or os.path.isdir(path)


def test_vendor_tree_is_self_contained():
    assert _has(os.path.join(VENDOR, "robot", "Robotic_Arm", "rm_robot_interface.py"))
    assert _has(os.path.join(VENDOR, "robot", "Robotic_Arm", "libs", "linux_arm",
                             "libapi_c.so"))
    assert _has(os.path.join(VENDOR, "robot", "Robotic_Arm", "libs", "linux_x86",
                             "libapi_c.so"))
    assert _has(os.path.join(VENDOR, "camera", "scepter_sdk", "ScepterDS_api.py"))
    assert _has(os.path.join(VENDOR, "camera", "scepter_sdk", "linux", "x64",
                             "libScepter_api.so"))
    assert _has(os.path.join(VENDOR, "camera", "scepter_sdk", "linux", "aarch64",
                             "libScepter_api.so"))
    assert _has(os.path.join(VENDOR, "camera", "scepter_sdk", "windows", "x64",
                             "Scepter_api.dll"))
    assert _has(os.path.join(VENDOR, "models", "edge_sam-r20231213",
                             "edge_sam_encoder.onnx"))
    assert _has(os.path.join(VENDOR, "models", "edge_sam-r20231213",
                             "edge_sam_decoder.onnx"))
    assert _has(os.path.join(VENDOR, "circle38", "detect_circle"))


def test_vendor_paths_default_to_tower_ops_vendor():
    from application.bootstrap import vendor_paths
    robot_dir, camera_dir = vendor_paths.default_sdk_dirs()
    assert os.path.abspath(robot_dir) == os.path.join(VENDOR, "robot")
    assert os.path.abspath(camera_dir) == os.path.join(VENDOR, "camera")
    assert os.path.isdir(os.path.join(robot_dir, "Robotic_Arm"))
    assert os.path.isdir(os.path.join(camera_dir, "scepter_sdk"))


def test_edge_sam_resolves_vendor_models():
    from application.algorithms.segmentation import edge_sam
    resolved = edge_sam.resolve_models_dir()
    assert resolved is not None
    assert os.path.abspath(resolved) == os.path.join(
        VENDOR, "models", "edge_sam-r20231213")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux 库加载")
def test_scepter_linux_loads_platform_library():
    """在**子进程**里导入，避免 SDK 模块/路径污染其它用例的 sys.modules。"""
    code = (
        "import scepter_sdk.ScepterDS_api as m\n"
        "print(str(m.sc_cam_lib._name))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": os.path.join(VENDOR, "camera")},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    lib = proc.stdout.strip().splitlines()[-1]
    machine = os.uname().machine.lower()
    if machine in ("x86_64", "amd64"):
        assert lib.endswith(os.path.join("linux", "x64", "libScepter_api.so"))
    elif machine in ("aarch64", "arm64"):
        assert lib.endswith(os.path.join("linux", "aarch64", "libScepter_api.so"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux 库加载")
def test_robotic_arm_linux_loads_platform_library():
    """子进程导入 Robotic_Arm：封装按 platform.machine() 选 linux_arm/linux_x86 并加载 .so。"""
    code = (
        "import platform\n"
        "import Robotic_Arm.rm_ctypes_wrap as m\n"
        "assert hasattr(m, 'rm_api_version')\n"
        "print(platform.machine())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": os.path.join(VENDOR, "robot")},
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
