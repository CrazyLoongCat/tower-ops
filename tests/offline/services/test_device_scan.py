"""DS86 扫描服务（离线，**不接真机**）：照旧程序的"扫描 + 选择"取序列号。

覆盖：设备行的字段与标注（名称/序列号/IP/[占用]）/ 有 SN 与没 SN 的区分 /
扫不了（厂家包不可用、SDK 抛错）与"扫到 0 台"**不是一回事** /
扫描时长与旧程序一致（2000 ms）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.services.device_scan import (
    SCAN_MS_PICK,
    ScanOutcome,
    ScannedCamera,
    scan_ds86_cameras,
)


class _FakeDevice:
    """假 DS86 设备：记录拿到的扫描时长，回固定的设备行。"""

    def __init__(self, rows=None, *, raise_exc: Exception | None = None) -> None:
        self.rows = list(rows or [])
        self.raise_exc = raise_exc
        self.scan_ms: list[int] = []

    def enumerate(self, scan_ms: int = 0):
        self.scan_ms.append(scan_ms)
        if self.raise_exc is not None:
            raise self.raise_exc
        return list(self.rows)


def _row(name="DS86", serial="SN-1", ip="192.168.1.50", status=1):
    return {"name": name, "serial": serial, "ip": ip, "status": status}


# ---------------------------------------------------------------- 设备行
def test_scan_returns_devices_with_name_serial_ip():
    dev = _FakeDevice([_row(serial="SN-A"), _row(name="DS86", serial="SN-B",
                                                 ip="192.168.1.51")])
    out = scan_ds86_cameras(device_factory=lambda: dev)
    assert out.ok and out.error == ""
    assert [d.serial for d in out.devices] == ["SN-A", "SN-B"]
    assert out.devices[0].ip == "192.168.1.50"
    assert all(d.connectable for d in out.devices)


def test_scan_uses_the_old_programs_scan_time():
    """旧 guidance_locate_tab 扫描 2000 ms —— 新工程照抄，别又退回 1000 ms。"""
    dev = _FakeDevice([_row()])
    scan_ds86_cameras(device_factory=lambda: dev)
    assert dev.scan_ms == [SCAN_MS_PICK] == [2000]


def test_scan_marks_occupied_but_keeps_it():
    """被占用的设备**照样列出来**并标 [占用]（旧程序也只是标注，不隐藏）。"""
    dev = _FakeDevice([_row(serial="SN-FREE", status=1),
                       _row(name="DS86", serial="SN-BUSY", status=0)])
    out = scan_ds86_cameras(device_factory=lambda: dev)
    free, busy = out.devices
    assert free.connectable and not free.used
    assert busy.used and not busy.connectable
    assert busy.label() == "DS86 (SN-BUSY) @ 192.168.1.50 [占用]"
    assert free.label() == "DS86 (SN-FREE) @ 192.168.1.50"
    # 被占用的也带序列号 → 仍可挑（挑完界面会提示先关掉占它的程序）
    assert [d.serial for d in out.with_serial] == ["SN-FREE", "SN-BUSY"]


def test_device_without_serial_is_not_offered():
    """设备在线但**没上报序列号**：不能算"有 SN 可挑"，标签也要说清。"""
    out = scan_ds86_cameras(device_factory=lambda: _FakeDevice([_row(serial="")]))
    assert out.ok and len(out.devices) == 1
    assert out.with_serial == ()
    assert out.devices[0].label().endswith("(未上报序列号) @ 192.168.1.50")
    assert not out.devices[0].has_serial


# ---------------------------------------------------------------- 失败与空
def test_zero_devices_is_not_an_error():
    """扫到 0 台 ≠ 扫不了：`error` 必须为空，界面才好说"没发现在线相机"。"""
    out = scan_ds86_cameras(device_factory=lambda: _FakeDevice([]))
    assert out.ok and out.devices == () and out.error == ""


def test_sdk_exception_reports_scan_failure_not_empty():
    out = scan_ds86_cameras(
        device_factory=lambda: _FakeDevice(raise_exc=RuntimeError("device busy")))
    assert not out.ok
    assert "扫描 DS86 失败" in out.error and "device busy" in out.error
    assert out.devices == ()


def test_missing_vendor_package_says_so(monkeypatch):
    """厂家包没挂上时**要说"扫不了"**，不能报"现场没有相机"（那是两件事）。"""
    from application.adapters.ds86 import scepter

    monkeypatch.setattr(scepter.ScepterDs86Device, "available",
                        staticmethod(lambda: (False, "无厂家包 scepter_sdk")))
    out = scan_ds86_cameras()
    assert not out.ok
    assert "厂家包不可用" in out.error and "不是" in out.error
    assert "scepter_sdk" in out.detail


def test_scan_never_opens_a_device():
    """扫描只读：只调 enumerate（假设备只有这一个方法，能过就说明没碰打开/取流）。"""
    dev = _FakeDevice([_row()])
    scan_ds86_cameras(device_factory=lambda: dev)
    assert not hasattr(dev, "open")          # 假设备故意不实现 open


def test_scan_tolerates_odd_rows():
    """SDK 回的字段缺失/None 不能让界面炸掉（宁可少显示，不可崩）。"""
    out = scan_ds86_cameras(device_factory=lambda: _FakeDevice(
        [{"serial": None, "status": None}, "不是 dict"]))
    assert out.ok and len(out.devices) == 1
    assert out.devices[0].serial == "" and out.devices[0].status == 0


def test_outcome_helpers_are_stable():
    cam = ScannedCamera(name="", serial=" SN ", ip="", status=1)
    assert cam.has_serial and cam.label() == "DS86 ( SN )"
    assert ScanOutcome(devices=(cam,)).with_serial == (cam,)
    assert ScanOutcome(error="x").ok is False
    assert ScanOutcome().ok is True


def test_scanned_camera_defaults_come_from_sdk_fields():
    """字段名与 SDK 的 ``ScDeviceInfo`` 对齐（productName/serialNumber/ip/status）。"""
    info = SimpleNamespace(productName="DS86", serialNumber="SN-X",
                           ip="10.0.0.2", status=1)
    cam = ScannedCamera(name=str(info.productName), serial=str(info.serialNumber),
                        ip=str(info.ip), status=int(info.status))
    assert cam.label() == "DS86 (SN-X) @ 10.0.0.2"


# ---------------------------------------------------------------- 网段诊断（DEC-082）
def test_scan_forwards_attempts_when_device_supports_it():
    class _Dev:
        def __init__(self):
            self.got = None

        def enumerate(self, scan_ms=0, attempts=1):
            self.got = (scan_ms, attempts)
            return []

    dev = _Dev()
    scan_ds86_cameras(scan_ms=3000, attempts=2, device_factory=lambda: dev)
    assert dev.got == (3000, 2)


def test_network_diagnosis_flags_camera_on_other_subnet():
    from application.services.device_scan import format_network_diagnosis

    text = format_network_diagnosis(
        [("以太网", "192.168.2.10", "255.255.255.0")], "192.168.3.77")
    assert "192.168.2.10/255.255.255.0" in text
    assert "192.168.2.0/24" in text
    assert "不在同一网段" in text
    assert "广播" in text


def test_network_diagnosis_confirms_same_subnet():
    from application.services.device_scan import format_network_diagnosis

    text = format_network_diagnosis(
        [("以太网", "192.168.2.10", "255.255.255.0")], "192.168.2.50")
    assert "同网段" in text and "不在同一网段" not in text


def test_network_diagnosis_filters_loopback_and_link_local():
    from application.services.device_scan import format_network_diagnosis

    text = format_network_diagnosis(
        [("lo", "127.0.0.1", "255.0.0.0"),
         ("x", "169.254.10.1", "255.255.0.0"),
         ("以太网", "10.0.0.2", "255.255.255.0")], "")
    assert "127.0.0.1" not in text and "169.254.10.1" not in text
    assert "10.0.0.2" in text and "相机配置 IP 为空" in text
