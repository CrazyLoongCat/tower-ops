"""D405 USB adapter. SDK is imported lazily; discovery never starts streaming."""
from __future__ import annotations

import threading
import time

import numpy as np

from application.contracts.enums import FreshnessQuality, ImageSpace
from application.contracts.frames import CaptureSet, FrameBundle, FreshnessPolicy, ImageRef, Intrinsics


def scan_d405(rs=None):
    if rs is None:
        import pyrealsense2 as rs
    devices = []
    for dev in rs.context().query_devices():
        name = dev.get_info(rs.camera_info.name)
        if "D405" in name.upper():
            serial = dev.get_info(rs.camera_info.serial_number)
            devices.append({"name": name, "serial": serial})
    return devices


class D405UsbCamera:
    """One logical channel bound to exactly one physical serial number."""
    # DEC-137：帧只有"到达即可"的新鲜度（曝光时间未验证），引导定位等调用方据此
    # 选 ALLOW_ARRIVAL_ONLY，不要用 REQUIRE_DEVICE_TIME 直接把它打回。
    supports_device_time = False

    def __init__(self, camera_id, serial, *, calibration_version=None, rs=None):
        self.camera_id = camera_id
        self.serial = str(serial or "").strip()
        if not self.serial:
            raise ValueError("D405 USB 尚未绑定序列号")
        self.calibration_version = calibration_version or None
        self._rs = rs
        self._pipeline = None
        self._profile = None
        self._align = None
        self._lock = threading.RLock()
        self._epoch = 0
        self._seq = 0

    def open(self, camera_id):
        with self._lock:
            if camera_id != self.camera_id:
                raise ValueError(f"未知相机 {camera_id!r}")
            if self._pipeline is not None:
                return
            if self._rs is None:
                try:
                    import pyrealsense2 as rs
                except ImportError as exc:
                    raise RuntimeError("未安装 pyrealsense2，无法使用 D405 USB") from exc
                self._rs = rs
            rs = self._rs
            if not any(d['serial'] == self.serial for d in scan_d405(rs)):
                raise RuntimeError(f"未发现绑定的 D405 序列号 {self.serial}")
            pipeline = rs.pipeline()
            config = rs.config()
            config.enable_device(self.serial)
            config.enable_stream(rs.stream.depth, 1280, 720, rs.format.z16, 30)
            config.enable_stream(rs.stream.color, 1280, 720, rs.format.bgr8, 30)
            profile = pipeline.start(config)
            try:
                scale = float(profile.get_device().first_depth_sensor().get_depth_scale())
                if not np.isfinite(scale) or scale <= 0:
                    raise ValueError("相机返回的深度比例无效")
                intr = profile.get_stream(rs.stream.color).as_video_stream_profile().get_intrinsics()
                align = rs.align(rs.stream.color)
            except Exception:
                pipeline.stop()
                raise
            self._pipeline, self._profile, self._align = pipeline, profile, align
            self._scale, self._intr = scale, intr
            self._epoch += 1

    def close(self, camera_id=None):
        with self._lock:
            pipeline, self._pipeline = self._pipeline, None
            self._profile = self._align = None
            if pipeline is not None:
                pipeline.stop()

    def status(self):
        return {"camera_id": self.camera_id, "serial": self.serial,
                "open": self._pipeline is not None, "epoch": self._epoch}

    def capture(self, request):
        result = CaptureSet(request=request)
        for cid in request.camera_ids:
            if cid != self.camera_id:
                result.errors[cid] = f"未知相机 {cid!r}"
                continue
            if request.freshness_policy is FreshnessPolicy.REQUIRE_DEVICE_TIME:
                result.errors[cid] = "D405 USB 尚未验证曝光时间，无法满足设备时间新鲜度要求"
                continue
            with self._lock:
                try:
                    self.open(cid)
                    # Discard the queued frames before accepting a newly arrived frame.
                    self._pipeline.poll_for_frames()
                    frames = self._align.process(self._pipeline.wait_for_frames(request.deadline_ms))
                    depth_frame, color_frame = frames.get_depth_frame(), frames.get_color_frame()
                    if not depth_frame or not color_frame:
                        raise RuntimeError("未收到完整彩色和深度帧")
                    depth = np.array(depth_frame.get_data(), copy=True, order="C")
                    color = np.array(color_frame.get_data(), copy=True, order="C")
                    if depth.dtype != np.uint16 or depth.ndim != 2 or color.shape[:2] != depth.shape:
                        raise ValueError("相机返回的图像格式或对齐尺寸无效")
                    h, w = depth.shape
                    intr = self._intr
                    if (w, h) != (intr.width, intr.height):
                        raise ValueError("图像尺寸与彩色内参不一致")
                    self._seq += 1
                    now = time.monotonic()
                    result.frames[cid] = FrameBundle(
                        camera_id=cid, frame_id=f"{cid}-f{self._seq}",
                        capture_id=f"{cid}-c{self._seq}", sequence=self._seq,
                        session_epoch=self._epoch, service_id="local_usb", channel_id=cid,
                        serial=self.serial, run_id=request.run_id,
                        station_instance_id=request.station_instance_id, step_id=request.step_id,
                        capture_request_id=request.request_id, capture_accepted_monotonic_s=now,
                        freshness_quality=FreshnessQuality.ARRIVAL_ONLY,
                        color=ImageRef.from_array(color), depth=ImageRef.from_array(depth),
                        depth_scale_m=self._scale,
                        intrinsics=Intrinsics(fx=intr.fx, fy=intr.fy, cx=intr.ppx, cy=intr.ppy,
                            width=w, height=h, intrinsics_space=ImageSpace.COLOR,
                            calibration_version=self.calibration_version),
                        alignment_space=ImageSpace.COLOR, calibration_version=self.calibration_version,
                        depth_valid_ratio=float(np.count_nonzero(depth)) / depth.size)
                except Exception as exc:
                    try:
                        self.close(cid)
                    except Exception:
                        pass
                    result.errors[cid] = f"D405 USB 采集失败: {type(exc).__name__}: {exc}"
        result.delta_quality = FreshnessQuality.ARRIVAL_ONLY if result.frames else FreshnessQuality.UNKNOWN
        return result
