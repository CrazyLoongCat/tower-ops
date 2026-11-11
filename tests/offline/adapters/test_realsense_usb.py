from types import SimpleNamespace as NS

import numpy as np
import pytest

from application.adapters.realsense_usb import D405UsbCamera, scan_d405
from application.contracts.frames import CaptureRequest, FreshnessPolicy
from application.contracts.enums import FreshnessQuality, ImageSpace
from application.services.device_config import CameraEntry, DeviceConfig, DeviceConfigStore
from application.services.device_scan import scan_d405_usb


class FakeRS:
    camera_info = NS(name="name", serial_number="serial")
    stream = NS(depth="depth", color="color")
    format = NS(z16="z16", bgr8="bgr8")

    def __init__(self):
        self.serials = ["A", "B"]
        self.selected = []
        self.stopped = 0
        self.fail = False

    def context(self):
        return NS(query_devices=lambda: [NS(get_info=lambda key, s=s: "D405" if key == "name" else s)
                                         for s in self.serials])

    def config(self):
        return NS(enable_device=lambda s: self.selected.append(s), enable_stream=lambda *args: None)

    def pipeline(self):
        rs = self
        class Pipeline:
            def start(self, cfg):
                intr = NS(fx=10., fy=11., ppx=1., ppy=1., width=3, height=2)
                return NS(get_device=lambda: NS(first_depth_sensor=lambda: NS(get_depth_scale=lambda: .0001)),
                          get_stream=lambda s: NS(as_video_stream_profile=lambda: NS(get_intrinsics=lambda: intr)))
            def poll_for_frames(self):
                return None
            def wait_for_frames(self, timeout):
                if rs.fail:
                    raise RuntimeError("unplugged")
                return NS(get_depth_frame=lambda: NS(get_data=lambda: np.ones((2, 3), dtype=np.uint16)),
                          get_color_frame=lambda: NS(get_data=lambda: np.ones((2, 3, 3), dtype=np.uint8)))
            def stop(self):
                rs.stopped += 1
        return Pipeline()

    def align(self, stream):
        return NS(process=lambda frames: frames)


def test_scan_and_capture_bind_to_serial_and_reconnect():
    rs = FakeRS()
    assert [x["serial"] for x in scan_d405(rs)] == ["A", "B"]
    cam = D405UsbCamera("d405_01", "B", rs=rs)
    strict = CaptureRequest(camera_ids=("d405_01",))
    assert "无法满足" in cam.capture(strict).errors["d405_01"]
    assert rs.selected == []
    req = CaptureRequest(camera_ids=("d405_01",), freshness_policy=FreshnessPolicy.ALLOW_ARRIVAL_ONLY)
    frame = cam.capture(req).frames["d405_01"]
    assert rs.selected == ["B"]
    assert frame.serial == "B" and frame.depth_scale_m == .0001
    assert frame.depth.data.dtype == np.uint16
    assert frame.alignment_space == ImageSpace.COLOR
    assert frame.freshness_quality == FreshnessQuality.ARRIVAL_ONLY
    rs.fail = True
    assert "unplugged" in cam.capture(req).errors["d405_01"]
    assert not cam.status()["open"] and rs.stopped == 1
    rs.fail = False
    rs.serials = ["A"]
    assert "未发现绑定" in cam.capture(req).errors["d405_01"]
    assert rs.selected == ["B"]
    rs.serials = ["B", "A"]
    assert cam.capture(req).ok and rs.selected == ["B", "B"]


def test_scan_failure_is_distinct_from_zero_devices():
    def fail():
        raise RuntimeError("sdk unavailable")
    assert not scan_d405_usb(scanner=fail).ok
    assert scan_d405_usb(scanner=lambda: []).ok


def test_usb_binding_requires_confirmation_and_unique_serial():
    store = DeviceConfigStore.in_memory(DeviceConfig(cameras=[CameraEntry(camera_id="a", serial="A")]))
    with pytest.raises(ValueError, match="已绑定"):
        store.add_camera(CameraEntry(camera_id="b", protocol="realsense_usb", enabled=True,
                                     confirmed=True, serial="A"))
    with pytest.raises(ValueError, match="现场核对"):
        store.add_camera(CameraEntry(camera_id="b", protocol="realsense_usb", enabled=True,
                                     confirmed=False, serial="B"))
    store.add_camera(CameraEntry(camera_id="b", protocol="realsense_usb", enabled=True,
                                 confirmed=True, serial="B"))


def test_usb_probe_distinguishes_discovery_from_capture(monkeypatch):
    from application.services.device_probe import probe
    from application.services.device_scan import ScanOutcome, ScannedCamera
    monkeypatch.setattr("application.services.device_scan.scan_d405_usb", lambda: ScanOutcome(
        devices=(ScannedCamera(name="D405", serial="A", status=1),)))
    cam = CameraEntry(camera_id="a", protocol="realsense_usb", enabled=True, confirmed=True, serial="A")
    result = probe(cam)
    assert result.ok and "尚未验证抓拍" in result.detail
    cam.serial = "B"
    assert not probe(cam).ok
