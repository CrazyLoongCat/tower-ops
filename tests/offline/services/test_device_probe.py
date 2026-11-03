"""连通性探测 (E21): 只做握手/只读 ping, 不下发动作指令。

覆盖: TCP 成功/失败 / 占位地址拒绝 (不发连接) / 依赖缺失优雅降级 /
协议分发与 sent_bytes 自证。全部在本机回环上进行, 不接触真实设备。
"""
from __future__ import annotations

import builtins
import socket
import threading
from types import SimpleNamespace

import pytest

from application.contracts.enums import DeviceProtocol
from application.services.device_config import CameraEntry, DeviceEntry, LinkEntry
from application.services.device_probe import (
    ProbeResult,
    probe,
    probe_serial,
    probe_tcp,
    probe_zmq,
)


class _Listener:
    """本机回环监听器: 供 TCP 探测的成功路径使用。"""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._accept_loop, daemon=True)
        self._t.start()

    def _accept_loop(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._t.join(timeout=1.0)


@pytest.fixture()
def listener():
    srv = _Listener()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture()
def zmq_rep_server():
    """真实 ZMQ REP 服务(回环): 按旧 S100 服务格式回 ping（含 info）。无 pyzmq 时整用例跳过。"""
    zmq = pytest.importorskip("zmq")
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP)
    sock.bind("tcp://127.0.0.1:*")
    port = int(sock.getsockopt_string(zmq.LAST_ENDPOINT).rsplit(":", 1)[1])
    sock.setsockopt(zmq.RCVTIMEO, 200)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                msg = sock.recv_json()
            except zmq.Again:
                continue
            except Exception:  # noqa: BLE001
                break
            sock.send_json({"ok": True, "info": {
                "service": "s100_d405_service", "started": False,
                "cams": {"cam1": {"serial": "<未启动>"},
                         "cam2": {"serial": "<未启动>"}}}})

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield port
    finally:
        stop.set()
        t.join(timeout=2.0)
        try:
            sock.close(0)
            ctx.term()
        except Exception:  # noqa: BLE001
            pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- TCP
def test_probe_tcp_success(listener):
    r = probe_tcp("127.0.0.1", listener.port, timeout=2.0)
    assert r.ok and r.is_real and r.latency_ms is not None
    assert r.sent_bytes == 0                    # 只握手, 不发帧


def test_probe_tcp_failure_closed_port():
    r = probe_tcp("127.0.0.1", _free_port(), timeout=1.0)
    assert not r.ok and r.is_real and "失败" in r.detail


# ---------------------------------------------------------------- 占位拒绝
def test_probe_refuses_placeholder_host():
    entry = DeviceEntry("d1", protocol=DeviceProtocol.JSON_TCP.value,
                        host="UNCONFIRMED", port=8888)
    r = probe(entry)
    assert not r.ok and r.is_real is False      # 未打开任何 socket
    assert "未配置" in r.detail or "未确认" in r.detail


def test_probe_refuses_unset_port():
    entry = DeviceEntry("d1", protocol=DeviceProtocol.MODBUS_TCP.value,
                        host="10.0.0.1", port=None)
    r = probe(entry)
    assert not r.ok and r.is_real is False and "端口" in r.detail


def test_probe_serial_refuses_placeholder():
    r = probe_serial("待核对")
    assert not r.ok and r.is_real is False


def test_probe_camera_record_without_serial_port_is_safe():
    """相机记录没有 serial_port 字段: serial 协议下不得抛异常, 应报未配置。"""
    cam = CameraEntry("c1", protocol=DeviceProtocol.SERIAL.value)
    r = probe(cam)
    assert not r.ok and r.is_real is False


def test_probe_unknown_protocol():
    r = probe(SimpleNamespace(protocol="canbus", host="x", port=1))
    assert not r.ok and "未知协议" in r.detail


# ---------------------------------------------------------------- 协议分发
def test_probe_dispatches_tcp_protocols(listener):
    for proto in (DeviceProtocol.MODBUS_TCP.value, DeviceProtocol.JSON_TCP.value,
                  DeviceProtocol.VENDOR_TCP.value):
        r = probe(DeviceEntry("d", protocol=proto, host="127.0.0.1",
                              port=listener.port))
        assert r.ok, (proto, r.detail)
        assert r.protocol == proto


def test_probe_rtsp_and_http_derive_endpoint_from_stream_url(listener):
    """网络摄像机只填 stream_url 也能探测: host/port 由 URL 解析。"""
    for proto, url in ((DeviceProtocol.RTSP.value,
                        f"rtsp://127.0.0.1:{listener.port}/stream1"),
                       (DeviceProtocol.HTTP.value,
                        f"http://127.0.0.1:{listener.port}/stream")):
        cam = CameraEntry("c", protocol=proto, stream_url=url)
        assert cam.host == "" and cam.port is None      # 只配了流地址
        r = probe(cam)
        assert r.ok, (proto, r.detail)
        assert r.sent_bytes == 0                        # 只握手


def test_probe_sdk_local_is_not_applicable_not_unreachable():
    """DS86/ECO65 这类本地 SDK 设备: 无网络参数 → "无需测试"，**不是**"不可达"。

    2026-10-15 真机反馈: 原来返回 ``ok=False``，UI 照着渲染成"不可达"，
    把操作员往网线/授权方向带；这类设备本来就没有连通性可测。
    """
    r = probe(CameraEntry("ds86_01", protocol=DeviceProtocol.SDK.value))
    assert not r.ok and r.is_real is False       # 没碰过任何 socket
    assert r.not_applicable is True              # 单独一档: 非成功也非失败
    assert "本地 SDK" in r.detail
    assert r.summary.startswith("无需测试")
    assert "不可达" not in r.summary


def test_probe_sdk_local_also_covers_device_records():
    """设备表里的本地 SDK 记录（随臂机器人 ECO65）同样走"无需测试"。"""
    r = probe(DeviceEntry("eco65_01", protocol=DeviceProtocol.SDK.value))
    assert r.not_applicable and r.summary.startswith("无需测试")


def test_probe_accepts_link_and_camera_records(listener):
    link = LinkEntry("l0", protocol=DeviceProtocol.JSON_TCP.value,
                     host="127.0.0.1", port=listener.port)
    cam = CameraEntry("c0", protocol=DeviceProtocol.MODBUS_TCP.value,
                      host="127.0.0.1", port=listener.port)
    assert probe(link).ok and probe(cam).ok


# ---------------------------------------------------------------- 依赖缺失
def test_probe_zmq_real_reply_ok(zmq_rep_server):
    """装了 pyzmq 且服务真的应答时: 探测成功, 并如实记录发出字节数。

    DEC-083：ping 通 ≠ 能抓帧 —— 把老服务 info 里的 `started`/通道摆出来；
    服务未启动时明确说“抓拍一定失败”。
    """
    r = probe_zmq("127.0.0.1", zmq_rep_server, timeout=2.0)
    assert r.ok and r.is_real and not r.degraded
    assert r.sent_bytes > 0                    # 只发出只读 ping
    assert r.latency_ms is not None
    assert "服务启动=False" in r.detail
    assert "通道=cam1=<未启动>、cam2=<未启动>" in r.detail
    assert "抓拍一定失败" in r.detail


def test_probe_zmq_missing_dependency_degrades_to_tcp(listener, monkeypatch):
    """缺 pyzmq 是环境问题不是设备问题: 降级为 TCP 可达性检查, 并给出安装命令。"""
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "zmq":
            raise ImportError("no zmq")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = probe_zmq("127.0.0.1", listener.port, timeout=1.0)
    assert not r.ok and r.degraded is True     # 不冒充可用
    assert "TCP 端口可达" in r.detail           # 仍给出有用的信息
    assert "pyzmq" in r.detail and "pip install pyzmq" in r.detail
    assert r.sent_bytes == 0                   # 没装库就没发任何字节
    assert "降级" in r.summary


def test_probe_zmq_missing_dependency_and_port_closed(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "zmq":
            raise ImportError("no zmq")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    r = probe_zmq("127.0.0.1", _free_port(), timeout=1.0)
    assert not r.ok and r.degraded is True
    assert "TCP 端口也不可达" in r.detail and "pip install pyzmq" in r.detail


def test_probe_zmq_against_non_service(listener):
    """对只有 TCP 监听、无 ZMQ 应答的端口: 握手可能成功但要如实报不可用。"""
    pytest.importorskip("zmq")
    r = probe_zmq("127.0.0.1", listener.port, timeout=0.5)
    assert isinstance(r, ProbeResult) and not r.ok and r.degraded is False


def test_probe_records_audit_callback(listener):
    seen = []
    probe(DeviceEntry("d", protocol=DeviceProtocol.JSON_TCP.value,
                      host="127.0.0.1", port=listener.port),
          audit=seen.append)
    assert seen and seen[0].ok
