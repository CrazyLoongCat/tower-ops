"""DS86 真实采集接线（离线，用 **mock SDK**；不接真机）。

mock 的帧数据用**真实 ctypes 缓冲**，与真机走同一条 `memmove → numpy` 代码路径，
因此能验"接线是否正确"；但**不证明**真机能出图（hardware 仍 NOT_RUN）。

覆盖：枚举 / 按序列号打开（含重扫重试）/ 启动参数被调用 / 对齐模式选内参传感器 /
深度必须 uint16 / 抓帧转 numpy 正确 / 超时返回 None / 未打开报错 / 关闭停流关设备 /
Ds86Adapter 端到端产出合法 FrameBundle。
"""
from __future__ import annotations

import ctypes
from types import SimpleNamespace

import numpy as np
import pytest

from application.adapters.ds86.real import Ds86Adapter, Ds86SdkError
from application.adapters.ds86.scepter import (
    GrabResult,
    ScepterDs86Device,
    ScepterDs86Sdk,
    ScepterError,
)
from application.contracts.frames import CaptureRequest

W, H = 8, 6
FX, FY, CX, CY = 200.0, 200.0, 4.0, 3.0


# ---------------------------------------------------------------- mock SDK
def _cstr(value: str):
    """C 风格字符数组（含尾部 null），供 _decode 解析。"""
    raw = value.encode("utf-8") + b"\x00"
    return (ctypes.c_char * len(raw))(*[bytes([b]) for b in raw])


class _Info:
    def __init__(self, serial: str) -> None:
        self.productName = _cstr("DS86")
        self.serialNumber = _cstr(serial)
        self.ip = _cstr("192.168.1.50")
        self.status = 1


class _Intr:
    fx, fy, cx, cy = FX, FY, CX, CY
    k1 = k2 = p1 = p2 = k3 = k4 = k5 = k6 = 0.0


class _Frame:
    def __init__(self, width: int, height: int, payload: bytes) -> None:
        self.width = width
        self.height = height
        self._buf = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        self.pFrameData = ctypes.cast(self._buf, ctypes.c_void_p)


class _MockCam:
    """最小 ScepterTofCam 行为记录器。"""

    def __init__(self, owner: "_MockApi") -> None:
        self.owner = owner
        self.calls: list = owner.calls

    def scGetDeviceCount(self, _ms=None):
        self.calls.append("scGetDeviceCount")
        return self.owner.count

    def scGetDeviceInfoList(self, _n=None):
        return (0, [_Info(self.owner.serial)])

    def scOpenDeviceBySN(self, sn=None):
        # 记**真实字符串**（厂家接口收 c_char_p；直接 bytes(sn) 拿到的是指针内容，没有意义）
        value = getattr(sn, "value", sn)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", "ignore")
        self.calls.append(("scOpenDeviceBySN", value))
        return self.owner.open_rc

    def scOpenDeviceByIP(self, ip=None):
        value = getattr(ip, "value", ip)
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", "ignore")
        self.calls.append(("scOpenDeviceByIP", value))
        return self.owner.open_rc

    def scStartStream(self):
        self.calls.append("scStartStream")
        return 0

    def scStopStream(self):
        self.calls.append("scStopStream")
        return 0

    def scCloseDevice(self):
        self.calls.append("scCloseDevice")
        return 0

    # 启动参数（只记录）
    def __getattr__(self, name: str):
        if name.startswith("scSet"):
            def _rec(*args, **_k):
                self.calls.append((name, *[getattr(a, "value", a) for a in args]))
                return 0
            return _rec
        raise AttributeError(name)

    def scGetSensorIntrinsicParameters(self, sensor=None):
        self.calls.append(("scGetSensorIntrinsicParameters", sensor))
        return (0, _Intr())

    def scGetFrameReady(self, _t=None):
        self.calls.append("scGetFrameReady")
        if self.owner.timeout:
            return (1, None)                       # 非 0 = 超时
        return (0, SimpleNamespace(depth=True, color=True,
                                   transformedDepth=False, transformedColor=False))

    def scGetFrame(self, frame_type=None):
        if frame_type == self.owner.enums.ScFrameType.SC_DEPTH_FRAME:
            return (0, _Frame(W, H, self.owner.depth_bytes))
        if frame_type == self.owner.enums.ScFrameType.SC_COLOR_FRAME:
            return (0, _Frame(W, H, self.owner.color_bytes))
        return (-1, None)


class _MockApi:
    """提供 ScepterTofCam 与各枚举的模块外形。"""

    def __init__(self, *, serial: str = "SN-TEST", count: int = 1, open_rc: int = 0,
                 timeout: bool = False, depth_raw: int = 500) -> None:
        self.calls: list = []
        self.serial = serial
        self.count = count
        self.open_rc = open_rc
        self.timeout = timeout
        depth = np.full((H, W), depth_raw, dtype="uint16")
        color = np.zeros((H, W, 3), dtype="uint8")
        color[:, :, 1] = 200                       # BGR 绿
        self.depth_bytes = depth.tobytes()
        self.color_bytes = color.tobytes()
        self.last_cam: _MockCam | None = None

    # ---- 供 ScepterDs86Device 使用 ----
    class ScepterDS_api:  # noqa: N801 - 模仿厂家命名
        pass

    class ScepterDS_types:  # noqa: N801 - 模仿厂家命名
        """DEC-046: 启动参数下发的结构体（IR 校正 / 三种 TOF 滤波）。"""

        class ScIRGMMCorrectionParams:
            def __init__(self):
                self.enable = None
                self.threshold = None

        class ScTimeFilterParams:
            def __init__(self):
                self.enable = None
                self.threshold = None

        class ScConfidenceFilterParams:
            def __init__(self):
                self.enable = None
                self.threshold = None

        class ScFlyingPixelFilterParams:
            def __init__(self):
                self.enable = None
                self.threshold = None

    @property
    def ScepterDS_enums(self):  # noqa: N802
        return self.enums

    @property
    def enums(self):
        S = self.ScepterDS_api
        if not hasattr(S, "_enums"):
            from types import SimpleNamespace as NS

            class ScFrameType:
                SC_DEPTH_FRAME = 1
                SC_COLOR_FRAME = 2
                SC_TRANSFORM_DEPTH_IMG_TO_COLOR_SENSOR_FRAME = 3
                SC_TRANSFORM_COLOR_IMG_TO_DEPTH_SENSOR_FRAME = 4

            class ScSensorType:
                SC_TOF_SENSOR = 10
                SC_COLOR_SENSOR = 11

            class ScWorkMode:
                SC_ACTIVE_MODE = 20
                SC_HARDWARE_TRIGGER_MODE = 21
                SC_SOFTWARE_TRIGGER_MODE = 22

            class ScExposureControlMode:
                SC_EXPOSURE_CONTROL_MODE_AUTO = 30
                SC_EXPOSURE_CONTROL_MODE_MANUAL = 31

            S._enums = NS(ScFrameType=ScFrameType, ScSensorType=ScSensorType,
                          ScWorkMode=ScWorkMode,
                          ScExposureControlMode=ScExposureControlMode)
        return S._enums


def _api(**kw) -> _MockApi:
    api = _MockApi(**kw)

    def _factory():
        api.last_cam = _MockCam(api)
        return api.last_cam

    api.ScepterDS_api.ScepterTofCam = _factory
    return api


def _device(**kw) -> ScepterDs86Device:
    api = kw.pop("api", None) or _api(**kw)
    return ScepterDs86Device(sdk=api, log=lambda _m: None)


# ---------------------------------------------------------------- 设备
def test_enumerate_returns_devices_without_opening():
    api = _api(serial="SN-A")
    dev = ScepterDs86Device(sdk=api)
    found = dev.enumerate()
    assert found and found[0]["serial"] == "SN-A"
    assert found[0]["ip"] == "192.168.1.50"
    assert "scOpenDeviceBySN" not in [c[0] if isinstance(c, tuple) else c for c in api.calls]


def test_open_requires_serial_and_starts_stream():
    dev = _device()
    with pytest.raises(ScepterError) as ei:
        dev.open("")
    assert "序列号" in str(ei.value)

    dev = _device()
    dev.open("SN-TEST", {"tof_resolution": "640x480", "fps": 5,
                         "align_mode": "none"})
    names = [c[0] if isinstance(c, tuple) else c for c in dev._sdk.calls]  # noqa: SLF001
    assert "scGetDeviceCount" in names          # 打开前重扫（旧坑）
    assert "scStartStream" in names
    assert "scSetToFResolution" in names and "scSetFrameRate" in names
    assert dev.is_open and dev.intrinsics["fx"] == FX


def test_open_retries_scan_then_fails_clearly():
    dev = _device(count=0)
    with pytest.raises(ScepterError) as ei:
        dev.open("SN-TEST")
    assert "未扫描到设备" in str(ei.value)
    assert dev.is_open is False


def test_open_failure_reports_status():
    dev = _device(open_rc=-15)
    with pytest.raises(ScepterError) as ei:
        dev.open("SN-TEST")
    assert "-15" in str(ei.value)


def test_intrinsics_sensor_follows_align_mode():
    for align, expect in (("depth_to_color", 11), ("none", 10)):
        dev = _device()
        dev.open("SN-TEST", {"align_mode": align})
        calls = [c for c in dev._sdk.calls if isinstance(c, tuple)  # noqa: SLF001
                 and c[0] == "scGetSensorIntrinsicParameters"]
        assert calls and calls[-1][1] == expect, (align, calls)
        dev.close()


def test_startup_params_apply_ir_hdr_and_filters():
    """DEC-046: 旧工程 _apply_startup_params/_apply_filter_params 的 IR/HDR/滤波逐项下发。"""
    dev = _device()
    dev.open("SN-TEST", {
        "align_mode": "none",
        "ir_gain": 20,
        "ir_correction_enable": True, "ir_correction_threshold": 3,
        "hdr_enable": True, "hdr_low_exposure_time": 80, "hdr_high_exposure_time": 200,
        "wdr_enable": False,
        "time_filter_enable": True, "time_filter_threshold": 1,
        "confidence_filter_enable": True, "confidence_filter_threshold": 15,
        "flying_pixel_filter_enable": True, "flying_pixel_filter_threshold": 5,
        "fill_hole_filter_enable": False,
        "spatial_filter_enable": True,
    })
    calls = [c for c in dev._sdk.calls if isinstance(c, tuple)]  # noqa: SLF001
    by_name: dict = {}
    for c in calls:
        by_name.setdefault(c[0], []).append(c[1:])

    def _val(x):
        return getattr(x, "value", x)

    assert _val(by_name["scSetIRGMMGain"][0][0]) == 20
    ir = by_name["scSetIRGMMCorrection"][0][0]
    assert _val(ir.enable) is True and _val(ir.threshold) == 3
    assert _val(by_name["scSetHDRModeEnabled"][0][0]) is True
    hdr = {int(_val(a)): int(_val(b)) for a, b in by_name["scSetExposureTimeOfHDR"]}
    assert hdr == {0: 80, 1: 200}
    assert _val(by_name["scSetWDRModeEnabled"][0][0]) is False
    time_f = by_name["scSetTimeFilterParams"][0][0]
    assert _val(time_f.enable) is True and _val(time_f.threshold) == 1
    conf = by_name["scSetConfidenceFilterParams"][0][0]
    assert _val(conf.enable) is True and _val(conf.threshold) == 15
    fly = by_name["scSetFlyingPixelFilterParams"][0][0]
    assert _val(fly.enable) is True and _val(fly.threshold) == 5
    assert _val(by_name["scSetFillHoleFilterEnabled"][0][0]) is False
    assert _val(by_name["scSetSpatialFilterEnabled"][0][0]) is True
    dev.close()


def test_startup_params_hdr_off_skips_hdr_exposure():
    dev = _device()
    dev.open("SN-TEST", {"align_mode": "none", "hdr_enable": False})
    names = [c[0] if isinstance(c, tuple) else c for c in dev._sdk.calls]  # noqa: SLF001
    assert "scSetHDRModeEnabled" in names
    assert "scSetExposureTimeOfHDR" not in names
    dev.close()


def test_filter_params_skipped_without_sdk_types_but_open_succeeds():
    """厂家包没导出结构体时只记录跳过，不阻断打开。"""
    logs: list[str] = []
    dev = ScepterDs86Device(sdk=_api(), log=logs.append)
    dev.sdk.ScepterDS_types = None  # type: ignore[attr-defined]  # 显式抹掉结构体导出
    dev.open("SN-TEST", {"align_mode": "none", "confidence_filter_enable": True})
    assert dev.is_open
    assert any("结构体" in m for m in logs)
    dev.close()



    dev = _device()
    dev.open("SN-TEST", {"align_mode": "none"})
    got = dev.grab()
    assert isinstance(got, GrabResult)
    assert got.depth.shape == (H, W) and got.depth.dtype == np.uint16
    assert int(got.depth[0, 0]) == 500
    assert got.color.shape == (H, W, 3) and int(got.color[0, 0, 1]) == 200
    assert got.depth_scale_m == 0.001
    # 是副本：改返回值不影响 SDK 缓冲
    got.depth[:] = 1
    got2 = dev.grab()
    assert int(got2.depth[0, 0]) == 500


def test_grab_timeout_returns_none():
    dev = _device(timeout=True)
    dev.open("SN-TEST")
    assert dev.grab() is None                    # 超时不算错误


def test_grab_before_open_raises():
    dev = _device()
    with pytest.raises(ScepterError):
        dev.grab()


def test_close_stops_stream_and_device():
    dev = _device()
    dev.open("SN-TEST")
    dev.close()
    names = [c[0] if isinstance(c, tuple) else c for c in dev._sdk.calls]  # noqa: SLF001
    assert "scStopStream" in names and "scCloseDevice" in names
    assert dev.is_open is False


# ---------------------------------------------------------------- SDK 适配壳
def test_sdk_facade_matches_adapter_contract():
    api = _api()
    sdk = ScepterDs86Sdk(params={"align_mode": "none"},
                         device=ScepterDs86Device(sdk=api))
    assert [d["serial"] for d in sdk.enumerate_devices()] == ["SN-TEST"]
    sdk.open("SN-TEST")
    assert sdk.get_intrinsics()["fy"] == FY
    frame = sdk.read_frame()
    assert frame.seq == 1 and frame.depth.dtype == np.uint16
    assert frame.color.shape == (H, W, 3)
    sdk.close()
    assert sdk.read_frame  # 仍可调用（未打开时由设备报错）


def test_sdk_facade_fills_color_when_missing():
    api = _api()
    dev = ScepterDs86Device(sdk=api)
    dev.open("SN-TEST")
    # 让抓帧只返回深度（彩色缺失）也不报错，给占位
    dev._cam.scGetFrameReady = lambda _t=None: (0, SimpleNamespace(  # noqa: SLF001
        depth=True, color=False, transformedDepth=False, transformedColor=False))
    sdk = ScepterDs86Sdk(device=dev)
    frame = sdk.read_frame()
    assert frame.color.shape == (H, W, 3) and int(frame.color.max()) == 0


# ---------------------------------------------------------------- 适配器端到端
def test_adapter_open_and_capture_produces_frame_bundle():
    api = _api()
    adapter = Ds86Adapter("ds86_01", serial="SN-TEST",
                          sdk=ScepterDs86Sdk(params={"align_mode": "none"},
                                             device=ScepterDs86Device(sdk=api)))
    adapter.open("ds86_01")
    cs = adapter.capture(CaptureRequest(camera_ids=("ds86_01",), request_id="t1"))
    assert not cs.errors, cs.errors
    frame = cs.frames["ds86_01"]
    # ImageRef.dtype 是字符串, 真正的数组在 .data
    assert frame.depth.data.shape == (H, W) and frame.depth.dtype == "uint16"
    assert frame.color.data.shape == (H, W, 3)
    assert int(frame.depth.data[0, 0]) == 500
    assert frame.intrinsics.fx == FX and frame.intrinsics.cx == CX
    assert frame.depth_scale_m == 0.001
    assert frame.depth_valid_ratio == pytest.approx(1.0)
    assert frame.camera_id == "ds86_01"
    adapter.close()


def test_continuous_monitor_history_is_bounded_with_mock_sdk():
    adapter = Ds86Adapter("ds86_01", serial="SN-TEST",
                          sdk=ScepterDs86Sdk(params={"align_mode": "none"},
                                             device=ScepterDs86Device(sdk=_api())))
    adapter.open("ds86_01")
    try:
        request = CaptureRequest(camera_ids=("ds86_01",))
        first = adapter.capture(request).frames["ds86_01"]
        before = first.depth.data.copy()
        for _ in range(20):
            assert not adapter.capture(request).errors
        assert len(adapter.held_frames()) == 8
        assert np.array_equal(first.depth.data, before)
    finally:
        adapter.close()


def test_adapter_reports_not_open_and_wrong_camera():
    adapter = Ds86Adapter("ds86_01", sdk=_api())
    cs = adapter.capture(CaptureRequest(camera_ids=("ds86_01",), request_id="t2"))
    assert "未打开" in cs.errors["ds86_01"]
    adapter2 = Ds86Adapter("ds86_01", sdk=_api())
    cs2 = adapter2.capture(CaptureRequest(camera_ids=("d405_01",), request_id="t3"))
    assert "未配置相机" in cs2.errors["d405_01"]


def test_adapter_without_injected_sdk_reports_unavailable_on_linux(monkeypatch):
    """未注入 SDK 时走真实包装：沙箱（Linux）应给出**明确**的不可用原因。"""
    from application.adapters.ds86 import scepter
    monkeypatch.setattr(scepter, "import_scepter_sdk",
                        lambda: (_ for _ in ()).throw(scepter.ScepterError("注入: 无厂家包")))
    adapter = Ds86Adapter("ds86_01")
    with pytest.raises(Ds86SdkError) as ei:
        adapter._ensure_sdk()                     # noqa: SLF001
    assert "SDK 不可用" in str(ei.value) and "无厂家包" in str(ei.value)


# ---------------------------------------------------------------- 序列号解析
# 真机症状（2026-10-15）：配置里相机序列号留空时，适配器曾拿 **camera_id**（"ds86_01"）
# 当序列号递给 `scOpenDeviceBySN` → 列表里没有这个 SN → ScStatus(-15)。
# 下面这组用例把"绝不编造序列号"钉死，并覆盖单台自动采用 / 0 台 / 多台的三种走法。
class _FakeSdk:
    """只实现适配器用到的那几个方法，记录**实际用来打开的序列号**。"""

    def __init__(self, devices=("SN-TEST",), *, open_fails=False,
                 open_status: int | None = -15, ip: str = "") -> None:
        self._devices = [{"serial": s, "ip": ip} for s in devices]
        self.open_fails = open_fails
        self.open_status = open_status
        self.opened_with: list[str] = []
        self.opened_ip: list[str | None] = []

    def enumerate_devices(self):
        return list(self._devices)

    def open(self, serial: str, *, ip: str | None = None) -> None:
        self.opened_with.append(serial)
        self.opened_ip.append(ip)
        if self.open_fails:
            from application.adapters.ds86.scepter import ScepterError
            if self.open_status is None:
                raise ScepterError("scOpenDeviceBySN 失败（无状态码）")
            raise ScepterError(
                f"scOpenDeviceBySN 失败, ScStatus({self.open_status}), SN={serial!r}")

    def close(self) -> None:
        pass

    def get_intrinsics(self):
        return None

    def read_frame(self):
        raise AssertionError("本用例不该抓帧")


def test_open_without_serial_uses_the_only_online_device():
    """配置没填序列号 + 现场只有一台 → 按枚举到的那台打开（与旧工程行为一致）。"""
    sdk = _FakeSdk(("SN-ONE",))
    adapter = Ds86Adapter("ds86_01", sdk=sdk)
    adapter.open("ds86_01")
    assert sdk.opened_with == ["SN-ONE"]
    assert adapter.status()["serial"] == "SN-ONE"      # 记住真正用的 SN


def test_open_never_sends_camera_id_as_serial():
    """回归钉：空序列号时**绝不能**把 camera_id 当 SN 发出去（-15 的根因）。"""
    sdk = _FakeSdk(("SN-ONE",))
    Ds86Adapter("ds86_01", sdk=sdk).open("ds86_01")
    assert "ds86_01" not in sdk.opened_with


def test_configured_serial_wins_over_enumeration():
    sdk = _FakeSdk(("SN-ONE",))
    Ds86Adapter("ds86_01", sdk=sdk, serial="SN-CFG").open("ds86_01")
    assert sdk.opened_with == ["SN-CFG"]


def test_open_without_serial_and_no_device_tells_operator_what_to_do():
    sdk = _FakeSdk(())
    adapter = Ds86Adapter("ds86_01", sdk=sdk)
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")
    assert "未扫描到任何在线设备" in str(ei.value) and "序列号" in str(ei.value)
    assert sdk.opened_with == []                        # 没乱试


def test_open_without_serial_and_multiple_devices_lists_them():
    sdk = _FakeSdk(("SN-A", "SN-B"))
    adapter = Ds86Adapter("ds86_01", sdk=sdk)
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")
    msg = str(ei.value)
    assert "SN-A" in msg and "SN-B" in msg and "2 台" in msg
    assert sdk.opened_with == []


# ---------------------------------------------------------------- 配置的 IP（DEC-081）
def test_configured_camera_ip_is_used_when_opening():
    """设备配置里的相机 IP 填了：打开走 ip=，状态里能查到；SN 仍用于记录。"""
    sdk = _FakeSdk(("SN-REAL",), ip="192.168.2.50")
    adapter = Ds86Adapter("ds86_01", sdk=sdk, serial="SN-REAL",
                          device_ip="192.168.2.50")
    adapter.open("ds86_01")
    assert sdk.opened_with == ["SN-REAL"]
    assert sdk.opened_ip == ["192.168.2.50"]
    assert adapter.status()["ip"] == "192.168.2.50"
    adapter.close()


def test_configured_camera_ip_mismatch_is_rejected_before_open():
    """配置 IP 与在线 SN 的 IP 不一致 → 启动就报清楚，别等 SDK 给 -106。"""
    sdk = _FakeSdk(("SN-REAL",), ip="192.168.3.10")
    adapter = Ds86Adapter("ds86_01", sdk=sdk, serial="SN-REAL",
                          device_ip="192.168.2.50")
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")
    msg = str(ei.value)
    assert "不一致" in msg and "192.168.2.50" in msg and "192.168.3.10" in msg
    assert sdk.opened_with == []          # 没拿错误 IP 去开


def test_configured_ip_without_serial_selects_the_matching_online_device():
    """只填 IP 不填 SN：按 IP 在在线列表里找唯一那台，不拿第一台硬试。"""
    sdk = _FakeSdk(("SN-A", "SN-B"), ip="192.168.2.50")
    adapter = Ds86Adapter("ds86_01", sdk=sdk, device_ip="192.168.2.50")
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")           # 两台同 IP -> 不猜
    assert "对应多台设备" in str(ei.value)
    assert sdk.opened_with == []

    sdk = _FakeSdk(("SN-ONE",), ip="192.168.2.50")
    adapter = Ds86Adapter("ds86_01", sdk=sdk, device_ip="192.168.2.50")
    adapter.open("ds86_01")
    assert sdk.opened_with == ["SN-ONE"]
    assert sdk.opened_ip == ["192.168.2.50"]
    adapter.close()


def test_scepter_device_opens_by_ip():
    """真实封装：传 ip 时调用厂家 scOpenDeviceByIP（不再扫列表按 SN 打开）。"""
    api = _api(serial="SN-REAL")
    sdk = ScepterDs86Sdk(params={"align_mode": "none"},
                         device=ScepterDs86Device(sdk=api))
    sdk.open("SN-REAL", ip="192.168.2.50")
    calls = [c for c in api.calls if isinstance(c, tuple)]
    assert ("scOpenDeviceByIP", "192.168.2.50") in calls
    assert not any(c[0] == "scOpenDeviceBySN" for c in calls)
    sdk.close()


def test_open_failure_hint_lists_online_sns():
    """序列号填错时：错误里要能看见"现在在线的是哪几台"。"""
    sdk = _FakeSdk(("SN-REAL",), open_fails=True)
    adapter = Ds86Adapter("ds86_01", sdk=sdk, serial="WRONG")
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")
    msg = str(ei.value)
    assert "SN-REAL" in msg and "WRONG" in msg
    assert "-15" in msg and "SC_INVALID_PARAMS" in msg


def test_adapter_enumerate_does_not_fabricate_serial():
    """枚举里的空 SN 不得被 camera_id 顶替（否则"没设备"看起来像"有一台"）。"""
    adapter = Ds86Adapter("ds86_01", sdk=_FakeSdk(("", "")))
    assert adapter.enumerate() == []


def test_adapter_on_real_wrapper_sends_the_enumerated_sn():
    """把修复钉在**真实包装**上：实际传给 `scOpenDeviceBySN` 的是枚举到的 SN。"""
    api = _api(serial="SN-REAL")
    sdk = ScepterDs86Sdk(params={"align_mode": "none"},
                         device=ScepterDs86Device(sdk=api))
    adapter = Ds86Adapter("ds86_01", sdk=sdk)          # 配置未填序列号
    adapter.open("ds86_01")
    sent = [c[1] for c in api.calls
            if isinstance(c, tuple) and c[0] == "scOpenDeviceBySN"]
    assert sent == ["SN-REAL"]
    assert "ds86_01" not in sent               # camera_id 绝不出现在这里
    adapter.close()


def test_adapter_maps_minus15_to_actionable_diagnosis():
    """真机原症状：-15 要变成"在线有哪几台 + 怎么办"，而不是干巴巴一个 ScStatus。"""
    api = _api(serial="SN-REAL", open_rc=-15)
    sdk = ScepterDs86Sdk(params={"align_mode": "none"},
                         device=ScepterDs86Device(sdk=api))
    adapter = Ds86Adapter("ds86_01", sdk=sdk, serial="TYPED-WRONG")
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")
    msg = str(ei.value)
    assert "SN-REAL" in msg and "TYPED-WRONG" in msg
    assert "SC_INVALID_PARAMS" in msg
    assert adapter.status()["open"] is False


def test_open_failure_minus106_reports_ip_not_match_not_serial_problem():
    """2026-10-23 现场：-106 是 SC_IP_NOT_MATCH（网段不匹配），不得再按 -15 解释。

    现场现象：枚举能看到 SN、SN 也一致，但打开失败——说明不是序列号问题，是
    相机与主机网卡不在同一网段；错误里要给出相机 IP 与"改同网段"的指引。
    """
    sdk = _FakeSdk(("SN-REAL",), open_fails=True, open_status=-106,
                   ip="192.168.3.10")
    adapter = Ds86Adapter("ds86_01", sdk=sdk, serial="SN-REAL")
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")
    msg = str(ei.value)
    assert "SC_IP_NOT_MATCH" in msg and "同一网段" in msg
    assert "IP=192.168.3.10" in msg
    assert "在在线列表中，但打开被拒绝" in msg
    assert "SC_INVALID_PARAMS" not in msg          # 不能把网络问题误报成序列号问题


def test_open_failure_without_status_code_does_not_invent_minus15():
    sdk = _FakeSdk(("SN-REAL",), open_fails=True, open_status=None)
    adapter = Ds86Adapter("ds86_01", sdk=sdk, serial="SN-REAL")
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")
    msg = str(ei.value)
    assert "ScStatus(" not in msg
    assert "SC_INVALID_PARAMS" not in msg


def test_ensure_sdk_succeeds_when_sdk_available(monkeypatch):
    """回归：厂家包**可用**时 `_ensure_sdk` 必须真的建出 SDK。

    2026-10-15 真机复现：此处曾有一条陈旧的
    `raise Ds86SdkError("真实 Scepter 封装需在 A23/A24 登记后接线")`，
    它在 SDK 已构造成功之后**无条件**抛出，使真实相机一律不可用；
    离线用例因"沙箱里厂家包本来就导不进来"而先在更早的分支退出，**照不出来**。
    """
    from application.adapters.ds86 import scepter

    class _StubSdk:
        def __init__(self, params=None, **kw):   # noqa: D107
            self.params = params

        @staticmethod
        def available() -> tuple[bool, str]:
            return True, ""

    monkeypatch.setattr(scepter, "ScepterDs86Sdk", _StubSdk)
    adapter = Ds86Adapter("ds86_01", capture_params={"fps": 15})
    adapter._ensure_sdk()                        # noqa: SLF001
    assert isinstance(adapter._sdk, _StubSdk)
    assert adapter._sdk.params == {"fps": 15}


# ---------------------------------------------------------------- 扫描策略 (照旧程序)
# 旧工程的序列号靠**运行期扫描**取得（相机配置里本来就没有 SN）：
#   guidance_locate_tab 扫描 2000 ms 一次 -> 操作员挑一台；
#   打开前 ds86_camera.py 再扫 3000 ms × 3（单次扫描在忙网上偶发 0 台）。
# 下面把这两档钉住，免得又退回"1000 ms 扫一次、扫到 0 就断言现场没设备"。
class _CountingCam:
    """按给定台数序列应答的相机壳（记录每次扫描的时长）。"""

    def __init__(self, counts) -> None:
        self._counts = list(counts)
        self.scans: list[int] = []

    def scGetDeviceCount(self, ms=None):
        self.scans.append(getattr(ms, "value", ms))
        return self._counts.pop(0) if self._counts else 0

    def scGetDeviceInfoList(self, _n=None):
        return (0, [_Info("SN-LATE")])


class _CountingApi:
    """提供 ScepterTofCam 的最小模块外形（每次构造取下一个相机壳）。"""

    def __init__(self, counts) -> None:
        self.cams = [_CountingCam(c) for c in counts]
        self.built: list[_CountingCam] = []
        api = self

        class _ScepterApi:
            @staticmethod
            def ScepterTofCam():                      # noqa: N802 - 仿厂家命名
                return api.next_cam()

        self.ScepterDS_api = _ScepterApi

    def next_cam(self) -> _CountingCam:
        cam = self.cams.pop(0) if self.cams else _CountingCam([0])
        self.built.append(cam)
        return cam


def test_enumerate_defaults_to_the_old_pick_scan_time():
    """界面用的扫描时长 = 旧 guidance_locate_tab 的 2000 ms（不是 1000）。"""
    from application.adapters.ds86.scepter import SCAN_MS_PICK

    api = _CountingApi([[1]])
    dev = ScepterDs86Device(sdk=api)
    rows = dev.enumerate()
    assert [r["serial"] for r in rows] == ["SN-LATE"]
    assert api.built[0].scans == [SCAN_MS_PICK] == [2000]


def test_enumerate_retries_until_devices_show_up(monkeypatch):
    """多轮扫描：前几轮空手就重扫（旧程序就是靠这轮重试扛过偶发 0 台）。"""
    from application.adapters.ds86 import scepter as scepter_mod
    from application.adapters.ds86.scepter import SCAN_ATTEMPTS, SCAN_MS, SCAN_RETRY_SLEEP_S

    sleeps: list[float] = []
    monkeypatch.setattr(scepter_mod.time, "sleep", lambda s: sleeps.append(s))
    api = _CountingApi([[0], [0], [1]])
    dev = ScepterDs86Device(sdk=api)
    rows = dev.enumerate(scan_ms=SCAN_MS, attempts=SCAN_ATTEMPTS)
    assert [r["serial"] for r in rows] == ["SN-LATE"]
    assert [c.scans for c in api.built] == [[SCAN_MS], [SCAN_MS], [SCAN_MS]]
    assert sleeps == [SCAN_RETRY_SLEEP_S] * 2          # 空手两次 -> 睡两次


def test_enumerate_stops_retrying_once_it_has_devices(monkeypatch):
    from application.adapters.ds86 import scepter as scepter_mod
    from application.adapters.ds86.scepter import SCAN_ATTEMPTS

    sleeps: list[float] = []
    monkeypatch.setattr(scepter_mod.time, "sleep", lambda s: sleeps.append(s))
    api = _CountingApi([[1], [1], [1]])
    dev = ScepterDs86Device(sdk=api)
    dev.enumerate(attempts=SCAN_ATTEMPTS)
    assert len(api.built) == 1 and sleeps == []        # 第一次就扫到, 不再重扫


def test_open_rescan_matches_the_old_program(monkeypatch):
    """打开前的重扫：3000 ms × 3（旧 ds86_camera.py），不是单次。"""
    from application.adapters.ds86 import scepter as scepter_mod
    from application.adapters.ds86.scepter import (
        SCAN_ATTEMPTS, SCAN_MS, SCAN_RETRY_SLEEP_S,
    )

    sleeps: list[float] = []
    monkeypatch.setattr(scepter_mod.time, "sleep", lambda s: sleeps.append(s))
    api = _CountingApi([[0], [0], [0]])
    dev = ScepterDs86Device(sdk=api)
    with pytest.raises(ScepterError) as ei:
        dev.open("SN-TEST")
    assert "未扫描到设备" in str(ei.value)
    assert api.built[0].scans == [SCAN_MS] * SCAN_ATTEMPTS == [3000, 3000, 3000]
    assert sleeps == [SCAN_RETRY_SLEEP_S] * SCAN_ATTEMPTS


def test_adapter_scan_devices_forwards_scan_policy_to_the_sdk(monkeypatch):
    """适配器的 `scan_devices` 要把时长/次数**真的传给 SDK**（界面据此扫 2000 ms）。"""
    from application.adapters.ds86 import scepter as scepter_mod
    from application.adapters.ds86.scepter import SCAN_MS

    monkeypatch.setattr(scepter_mod.time, "sleep", lambda _s: None)
    api = _api(serial="SN-X")
    adapter = Ds86Adapter("ds86_01",
                          sdk=ScepterDs86Sdk(device=ScepterDs86Device(sdk=api)))
    rows = adapter.scan_devices()                       # 默认 = 旧界面那档
    assert [r["serial"] for r in rows] == ["SN-X"]

    api2 = _CountingApi([[0], [0], [0]])
    adapter2 = Ds86Adapter("ds86_01",
                           sdk=ScepterDs86Sdk(device=ScepterDs86Device(sdk=api2)))
    assert adapter2.scan_like_open() == []               # 打开前那轮: 3000 × 3
    # 每轮扫描都新建一个相机壳（与旧 query_devices 一致），故 3 个壳各扫一次
    assert [c.scans for c in api2.built] == [[SCAN_MS]] * 3


def test_adapter_scan_tolerates_minimal_mock_sdk():
    """注入的极简 mock 只接受零参 → 按签名过滤, **不许**因此 TypeError。"""
    sdk = _FakeSdk(("SN-ONE",))
    adapter = Ds86Adapter("ds86_01", sdk=sdk)
    rows = adapter.scan_devices(scan_ms=3000, attempts=3)
    assert [r["serial"] for r in rows] == ["SN-ONE"]
    assert adapter.enumerate() == ["SN-ONE"]


def test_resolving_serial_uses_the_rescan_policy():
    """配置为空时定序列号，走的也是"打开前那轮重扫"（3000 ms × 3）。"""
    from application.adapters.ds86.scepter import SCAN_ATTEMPTS, SCAN_MS

    class _WaySdk(_FakeSdk):
        def __init__(self) -> None:
            super().__init__(("SN-ONE",))
            self.ways: list[tuple] = []

        def enumerate_devices(self, scan_ms=None, attempts=None):
            self.ways.append((scan_ms, attempts))
            return super().enumerate_devices()

    sdk = _WaySdk()
    Ds86Adapter("ds86_01", sdk=sdk).open("ds86_01")
    assert sdk.ways == [(SCAN_MS, SCAN_ATTEMPTS)] == [(3000, 3)]


def test_failure_hint_scans_only_once_so_the_ui_does_not_freeze():
    """打开失败后的补扫只扫一轮 —— 打开时已连扫三次，再叠三次界面要多冻 9 秒。"""
    from application.adapters.ds86.scepter import SCAN_MS

    class _WaySdk(_FakeSdk):
        def __init__(self) -> None:
            super().__init__(("SN-REAL",))
            self.ways: list[tuple] = []

        def enumerate_devices(self, scan_ms=None, attempts=None):
            self.ways.append((scan_ms, attempts))
            return super().enumerate_devices()

    sdk = _WaySdk()
    sdk.open_fails = True
    adapter = Ds86Adapter("ds86_01", sdk=sdk, serial="WRONG")
    with pytest.raises(Ds86SdkError) as ei:
        adapter.open("ds86_01")
    assert "SN-REAL" in str(ei.value)
    assert sdk.ways == [(SCAN_MS, 1)]                    # 只补扫一轮


# ---------------------------------------------------------------- 对齐模式选帧（DEC-062）
class _ModeCam(_MockCam):
    """transformedDepth/transformedColor 同时 ready 的相机（最容易取错帧的组合）。"""

    def scGetFrameReady(self, _t=None):
        return (0, SimpleNamespace(depth=True, color=True,
                                   transformedDepth=True, transformedColor=True))

    def scGetFrame(self, frame_type=None):
        ft = frame_type
        e = self.owner.enums.ScFrameType
        if ft == e.SC_TRANSFORM_DEPTH_IMG_TO_COLOR_SENSOR_FRAME:
            self.calls.append(("SC_TRANSFORM_DEPTH",))
            return (0, _Frame(10, 8, np.full((8, 10), 600, dtype="uint16").tobytes()))
        if ft == e.SC_TRANSFORM_COLOR_IMG_TO_DEPTH_SENSOR_FRAME:
            self.calls.append(("SC_TRANSFORM_COLOR",))
            arr = np.zeros((4, 5, 3), dtype="uint8"); arr[:, :, 0] = 9
            return (0, _Frame(5, 4, arr.tobytes()))
        return super().scGetFrame(frame_type)


def _mode_api():
    api = _api()
    api.ScepterDS_api.ScepterTofCam = lambda: _ModeCam(api)
    return api


def test_grab_uses_raw_color_in_depth_to_color_mode():
    """depth_to_color：深度取变换帧、彩色取**原始帧**（旧工程行为）。

    不能因为 transformedColor 就取彩色→深度帧：那是深度系低分辨率图，
    却会被彩色内参解释，XYZ/精定位会整体错位。
    """
    dev = ScepterDs86Device(sdk=_mode_api(), log=lambda _m: None)
    dev.open("SN-TEST", {"align_mode": "depth_to_color"})
    g = dev.grab()
    assert g.depth.shape == (8, 10)            # 变换后的深度
    assert g.color.shape[:2] == (H, W)         # 原始彩色 8x6，不是 5x4
    names = [c for c in dev._sdk.calls if isinstance(c, tuple) and c[0].startswith("SC_")]  # noqa: SLF001
    assert ("SC_TRANSFORM_DEPTH",) in names
    assert ("SC_TRANSFORM_COLOR",) not in names


def test_grab_uses_transformed_color_in_color_to_depth_mode():
    """color_to_depth：彩色取变换帧、深度取原始帧。"""
    dev = ScepterDs86Device(sdk=_mode_api(), log=lambda _m: None)
    dev.open("SN-TEST", {"align_mode": "color_to_depth"})
    g = dev.grab()
    assert g.depth.shape == (H, W)             # 原始深度 8x6
    assert g.color.shape[:2] == (4, 5)         # 变换后的彩色
    names = [c for c in dev._sdk.calls if isinstance(c, tuple) and c[0].startswith("SC_")]  # noqa: SLF001
    assert ("SC_TRANSFORM_COLOR",) in names
    assert ("SC_TRANSFORM_DEPTH",) not in names
