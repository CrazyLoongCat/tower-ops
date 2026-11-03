"""圆宿主自动发现与「启动即拉起」（DEC-053，离线）。

现场症状：手工双击 ``circle38_host.exe`` 后「运行一次找圆」仍报"圆宿主不可用"。
根因是主程序与宿主走**子进程 stdin/stdout 管道**，不是端口服务——手工启动的
进程主程序根本不会去找。本文件守住：

1. ``resolve_circle_host_cmd``：显式 ``--circle-host-cmd`` 优先；否则在主程序
   同目录找 ``circle38_host.exe`` / ``circle38_host\\circle38_host.exe`` /
   源码布局 ``dist_host\\...``；带空格的显式文件路径原样保留；
2. ``SubprocessTransport.start()``：不等第一次请求就拉起，``started`` 可查；
3. 生产栈装配即启动宿主，拉起失败只记通知、不挡界面（找圆时如实报原因）。
"""
from __future__ import annotations

import os
import sys

import pytest

from application.bootstrap import circle_host
from application.bootstrap.circle_host import (
    find_circle_host,
    host_candidates,
    resolve_circle_host_cmd,
)
from application.bootstrap.production import (
    ProductionError,
    ProductionRequest,
    UnavailableCircleBackend,
    build_production_stack,
)
from application.hosts.circle38.client import SubprocessTransport

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _touch(path) -> str:
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"")
    return path


# ---------------------------------------------------------------- 自动发现
def test_auto_discovers_host_beside_main_exe(tmp_path):
    exe = _touch(tmp_path / "circle38_host.exe")
    cmd, note = resolve_circle_host_cmd("", root=str(tmp_path))
    assert cmd == [exe]
    assert "自动发现" in note and exe in note


def test_auto_discovers_onedir_subfolder(tmp_path):
    """PyInstaller onedir 产物整目录拷到主程序旁：circle38_host\\circle38_host.exe。"""
    exe = _touch(tmp_path / "circle38_host" / "circle38_host.exe")
    cmd, note = resolve_circle_host_cmd("", root=str(tmp_path))
    assert cmd == [exe]
    assert "自动发现" in note


def test_auto_discovers_source_dist_host_layout(tmp_path):
    """源码运行：tower-ops\\dist_host\\circle38_host\\circle38_host.exe。"""
    exe = _touch(tmp_path / "dist_host" / "circle38_host" / "circle38_host.exe")
    cmd, _ = resolve_circle_host_cmd("", root=str(tmp_path))
    assert cmd == [exe]


def test_candidate_order_prefers_flat_exe(tmp_path):
    """同目录同时有扁平可执行与 onedir 文件夹时，用直接摆在外面的那个。

    DEC-072：Linux 宿主文件叫 ``circle38_host``（无 .exe），与 onedir 目录同名，
    两者不能共存；该「扁平+目录同时存在」场景只在 Windows（.exe）上验证。
    """
    flat = _touch(tmp_path / circle_host._host_names()[0])
    assert host_candidates(str(tmp_path))[0] == flat
    assert find_circle_host(str(tmp_path)) == flat
    if os.name == "nt":
        _touch(tmp_path / "circle38_host" / circle_host._host_names()[0])
        assert find_circle_host(str(tmp_path)) == flat


def test_default_root_is_app_root(tmp_path, monkeypatch):
    exe = _touch(tmp_path / "circle38_host.exe")
    monkeypatch.setattr(circle_host.app_paths, "app_root", lambda: str(tmp_path))
    cmd, _ = resolve_circle_host_cmd("")
    assert cmd == [exe]


def test_missing_host_returns_none_with_hint(tmp_path):
    cmd, note = resolve_circle_host_cmd("", root=str(tmp_path))
    assert cmd is None
    assert "circle38_host" in note and "报错" in note


# ---------------------------------------------------------------- 显式优先
def test_explicit_file_path_wins_and_spaces_kept_whole(tmp_path):
    _touch(tmp_path / "circle38_host.exe")                 # 自动发现也有货
    explicit = _touch(tmp_path / "Program Files" / "host" / "circle38_host.exe")
    assert " " in explicit
    cmd, note = resolve_circle_host_cmd(explicit, root=str(tmp_path))
    # 整条路径就是一个参数：带空格也不会被 split 切碎
    assert cmd == [explicit]
    assert "显式" in note


def test_explicit_multi_part_command_still_split(tmp_path):
    """兼容「解释器 + 参数」写法（不是现成文件时按空格切分）。"""
    text = "C:\\Somewhere\\python.exe -m application.hosts.circle38.entry"
    cmd, note = resolve_circle_host_cmd(text, root=str(tmp_path))
    assert cmd == ["C:\\Somewhere\\python.exe", "-m",
                   "application.hosts.circle38.entry"]
    assert "显式" in note


# ---------------------------------------------------------------- 启动即拉起
def test_start_raises_when_command_missing(tmp_path):
    t = SubprocessTransport([str(tmp_path / "no-such-host.exe")])
    with pytest.raises(OSError):
        t.start()
    assert t.started is False
    assert t.alive is False
    t.close()                                               # 失败后 close 也安全


def test_start_launches_before_first_request_and_close_is_clean():
    """start() 拉起进程（不等请求）；close() 让宿主干净退出（退出码 0）。"""
    t = SubprocessTransport(
        [sys.executable, "-m", "application.hosts.circle38.entry", "--stub"],
        startup_grace_s=5.0)
    try:
        assert t.started is False
        t.start()
        assert t.started is True
        assert t.alive is True
    finally:
        t.close()
    assert t.last_returncode == 0


# ---------------------------------------------------------------- 生产栈接线
def _cfg_path(tmp_path):
    """测试自造的设备配置（只含 S100，装配不打开 socket）。"""
    from application.services.device_config import (
        CameraEntry, DeviceConfig, DeviceConfigStore, DeviceEntry,
    )
    path = tmp_path / "devices.yaml"
    store = DeviceConfigStore(str(path), seed_if_missing=False)
    store._config = DeviceConfig(                      # noqa: SLF001 - 测试构造
        devices=[DeviceEntry("s100_01", "S100", "edge", "zmq", enabled=True,
                             confirmed=True, host="10.20.30.40", port=5555)],
        links=[],
        cameras=[CameraEntry("d405_01", "D405-1", "zmq", enabled=True,
                             confirmed=True, host="10.20.30.40", port=5555,
                             channel_id="cam1")])
    store.save()
    return str(path)


def _stack(tmp_path, monkeypatch, host_cmd, *, circle_backend: str = "auto"):
    monkeypatch.setenv("PYTHONPATH", ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
    req = ProductionRequest(
        recipe_path="", db_path=str(tmp_path / "r.db"),
        runtime_dir=str(tmp_path), device_config_path=_cfg_path(tmp_path),
        circle_host_cmd=host_cmd, circle_backend=circle_backend)
    return build_production_stack(req)


def test_production_stack_starts_host_at_build_time(tmp_path, monkeypatch):
    stack = _stack(tmp_path, monkeypatch, [
        sys.executable, "-m", "application.hosts.circle38.entry", "--stub"])
    try:
        assert stack.host_proxy is not None
        transport = stack.host_proxy._transport          # noqa: SLF001 - 求证已拉起
        assert transport.started is True
        assert transport.alive is True
        assert stack.vision.spec.host == "cp38-native"
    finally:
        proxy = stack.host_proxy
        stack.close()
    assert proxy._transport.last_returncode == 0          # noqa: SLF001


def test_production_stack_survives_host_spawn_failure(tmp_path, monkeypatch):
    """宿主 exe 不存在：界面照常起，只留通知；找圆如实报具体原因（不给假结果）。"""
    stack = _stack(tmp_path, monkeypatch, [str(tmp_path / "no-such-host.exe")])
    try:
        assert stack.host_proxy is None
        assert stack.vision.spec.host == "unavailable"
        notices = "\n".join(stack.startup_notices or [])
        assert "圆宿主启动失败" in notices
        backend = stack.vision._detector._backend          # noqa: SLF001 - 求证不留假结果
        with pytest.raises(ProductionError) as ei:
            backend.detect([], [], None)
        assert "圆宿主启动失败" in str(ei.value)
    finally:
        stack.close()


def test_unavailable_backend_keeps_default_message():
    with pytest.raises(ProductionError) as ei:
        UnavailableCircleBackend().detect([], [], None)
    assert "--circle-host-cmd" in str(ei.value)


# ---------------------------------------------------------------- DEC-073 进程内后端
def test_auto_without_host_uses_in_process_opencv(tmp_path, monkeypatch):
    """没有宿主命令时 auto 走**进程内 OpenCV 后端**（opencv-linux，Linux 单程序）。"""
    stack = _stack(tmp_path, monkeypatch, None)
    try:
        assert stack.vision.spec.host == "opencv-linux"
        assert getattr(stack.host_proxy, "host_version", "") == "opencv-linux"
        assert stack.host_proxy.detect.__call__ is not None   # 同一后端供引导定位用
        assert not any("圆宿主" in n for n in (stack.startup_notices or []))
    finally:
        stack.close()


def test_host_mode_without_host_is_unavailable(tmp_path, monkeypatch):
    """显式 host 模式且没有宿主命令 -> 不可用（不回退到其它算法）。"""
    stack = _stack(tmp_path, monkeypatch, None, circle_backend="host")
    try:
        assert stack.vision.spec.host == "unavailable"
        assert stack.host_proxy is None
    finally:
        stack.close()


def test_explicit_opencv_ignores_host_cmd(tmp_path, monkeypatch):
    """显式 opencv：即使给了宿主命令也**不起宿主**，用进程内后端。"""
    stack = _stack(tmp_path, monkeypatch,
                   [str(tmp_path / "no-such-host")], circle_backend="opencv")
    try:
        assert stack.vision.spec.host == "opencv-linux"
        assert getattr(stack.host_proxy, "host_version", "") == "opencv-linux"
    finally:
        stack.close()


def test_invalid_circle_backend_rejected():
    with pytest.raises(ProductionError):
        ProductionRequest(circle_backend="stub")
