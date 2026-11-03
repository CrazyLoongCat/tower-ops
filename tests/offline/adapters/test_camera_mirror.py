"""DEC-043 采集层自动双镜像（D405-2 / D405-4 倒装）。

守的边界：
- 彩色与深度**同一变换**（水平+垂直 = 180°），内参 cx/cy 同步换算成 (W-1)-x / (H-1)-y；
- 只翻转指定相机 id，其它相机原样透传（不复制、不改元数据）；
- 帧元数据（序号/时间戳/抓拍 id/深度比例…）原样保留，翻转后的数组 C 连续且不与源共享；
- 装饰器对其余接口（open/close/status）透传给内层适配器。
"""
from __future__ import annotations

import numpy as np
import pytest

from application.adapters.camera_mirror import (
    MIRRORED_CAMERA_IDS,
    MirroredCameraAdapter,
    mirror_frame_bundle,
)
from application.contracts.enums import FreshnessQuality, ImageSpace
from application.contracts.frames import (
    CaptureRequest,
    CaptureSet,
    FrameBundle,
    ImageRef,
    Intrinsics,
)


def _frame(camera_id: str = "d405_02", *, w: int = 5, h: int = 3,
           with_depth: bool = True, with_intrinsics: bool = True) -> FrameBundle:
    color = np.arange(h * w * 3, dtype=np.uint8).reshape(h, w, 3)
    depth = (np.arange(h * w, dtype=np.uint16) + 1).reshape(h, w)
    inr = (Intrinsics(fx=600.0, fy=600.0, cx=1.0, cy=1.0, width=w, height=h,
                      intrinsics_space=ImageSpace.COLOR, calibration_version="cal-v1")
           if with_intrinsics else None)
    return FrameBundle(
        camera_id=camera_id, frame_id=f"{camera_id}-f7", capture_id=f"{camera_id}-c7",
        sequence=7, run_id="run-1", step_id="s1", capture_request_id="req-1",
        device_timestamp_ns=123456789, freshness_quality=FreshnessQuality.UNKNOWN,
        color=ImageRef.from_array(color),
        depth=ImageRef.from_array(depth) if with_depth else None,
        depth_scale_m=0.001, intrinsics=inr,
        alignment_space=ImageSpace.COLOR, calibration_version="cal-v1",
        depth_valid_ratio=0.5,
    )


# ---------------------------------------------------------------- 纯函数
def test_h_plus_v_mirror_equals_180_rotation():
    frame = _frame()
    out = mirror_frame_bundle(frame)
    assert np.array_equal(out.color.data, np.rot90(frame.color.data, 2))
    assert np.array_equal(out.depth.data, np.rot90(frame.depth.data, 2))


@pytest.mark.parametrize("horizontal,vertical", [(True, False), (False, True)])
def test_single_axis_mirror(horizontal, vertical):
    frame = _frame()
    out = mirror_frame_bundle(frame, horizontal=horizontal, vertical=vertical)
    expect = frame.color.data[:, ::-1] if horizontal else frame.color.data[::-1, :]
    assert np.array_equal(out.color.data, expect)
    expect_d = frame.depth.data[:, ::-1] if horizontal else frame.depth.data[::-1, :]
    assert np.array_equal(out.depth.data, expect_d)


def test_intrinsics_cx_cy_are_mirrored():
    frame = _frame(w=5, h=3)                       # cx=1.0 cy=1.0
    out = mirror_frame_bundle(frame)
    assert out.intrinsics is not None
    assert out.intrinsics.cx == pytest.approx((5 - 1) - 1.0)
    assert out.intrinsics.cy == pytest.approx((3 - 1) - 1.0)
    # 焦距/尺寸/标定版本不变
    assert out.intrinsics.fx == 600.0 and out.intrinsics.fy == 600.0
    assert out.intrinsics.width == 5 and out.intrinsics.height == 3
    assert out.intrinsics.calibration_version == "cal-v1"


def test_metadata_preserved_and_arrays_contiguous_unshared():
    frame = _frame()
    out = mirror_frame_bundle(frame)
    assert out.camera_id == frame.camera_id
    assert out.frame_id == frame.frame_id
    assert out.capture_id == frame.capture_id
    assert out.sequence == frame.sequence
    assert out.device_timestamp_ns == frame.device_timestamp_ns
    assert out.run_id == frame.run_id and out.step_id == frame.step_id
    assert out.capture_request_id == frame.capture_request_id
    assert out.depth_scale_m == frame.depth_scale_m
    assert out.depth_valid_ratio == frame.depth_valid_ratio
    assert out.color is not None and out.depth is not None
    assert out.color.shape == (3, 5, 3) and out.depth.shape == (3, 5)
    for ref in (out.color, out.depth):
        assert ref.data.flags["C_CONTIGUOUS"]
    assert not np.shares_memory(out.color.data, frame.color.data)
    assert not np.shares_memory(out.depth.data, frame.depth.data)
    # dtype 不变
    assert out.color.dtype == frame.color.dtype and out.depth.dtype == frame.depth.dtype


def test_missing_streams_and_intrinsics_are_tolerated():
    frame = _frame(with_depth=False, with_intrinsics=False)
    out = mirror_frame_bundle(frame)
    assert out.depth is None and out.intrinsics is None
    assert np.array_equal(out.color.data, np.rot90(frame.color.data, 2))


def test_identity_request_returns_same_bundle():
    frame = _frame()
    assert mirror_frame_bundle(frame, horizontal=False, vertical=False) is frame


# ---------------------------------------------------------------- 装饰器
class _InnerAdapter:
    """最小内层适配器：记录调用，返回预置 CaptureSet。"""

    def __init__(self, frames: dict[str, FrameBundle]) -> None:
        self.frames = frames
        self.calls: list[CaptureRequest] = []
        self.opened: list[str] = []
        self.closed: list[str] = []

    def capture(self, request: CaptureRequest) -> CaptureSet:
        self.calls.append(request)
        sub = {cid: self.frames[cid] for cid in request.camera_ids if cid in self.frames}
        return CaptureSet(request=request, frames=sub)

    def open(self, camera_id: str) -> None:
        self.opened.append(camera_id)

    def close(self, camera_id: str) -> None:
        self.closed.append(camera_id)

    def status(self) -> dict:
        return {"open": True}


def test_wrapper_mirrors_only_target_cameras():
    other = _frame("d405_01")
    target = _frame("d405_02")
    inner = _InnerAdapter({"d405_01": other, "d405_02": target})
    adapter = MirroredCameraAdapter(inner)
    req = CaptureRequest(camera_ids=("d405_01", "d405_02"))
    out = adapter.capture(req)
    # 非目标相机: 完全原样（同一 frame 对象）
    assert out.frames["d405_01"] is other
    # 目标相机: 已翻转 + 内参换算
    assert np.array_equal(out.frames["d405_02"].color.data,
                          np.rot90(target.color.data, 2))
    assert out.frames["d405_02"].intrinsics.cx == pytest.approx(3.0)   # (5-1)-1
    assert other.intrinsics is not None and other.intrinsics.cx == 1.0  # 源未被改动


def test_wrapper_passes_through_other_methods():
    inner = _InnerAdapter({})
    adapter = MirroredCameraAdapter(inner, camera_ids=("d405_04",))
    adapter.open("d405_04")
    adapter.close("d405_04")
    assert adapter.status() == {"open": True}
    assert inner.opened == ["d405_04"] and inner.closed == ["d405_04"]


def test_wrapper_returns_untouched_result_when_no_target_frame():
    other = _frame("netcam_03")
    inner = _InnerAdapter({"netcam_03": other})
    adapter = MirroredCameraAdapter(inner)
    out = adapter.capture(CaptureRequest(camera_ids=("netcam_03",)))
    assert out.frames["netcam_03"] is other


def test_default_mirrored_ids_are_the_two_field_cameras():
    """用户点名的两台相机（D405-2 / D405-4）；改动此集合须同步 DEC-043。"""
    assert MIRRORED_CAMERA_IDS == frozenset({"d405_02", "d405_04"})


# ---------------------------------------------------------------- 端到端(Fake V2)
def test_fake_s100_capture_end_to_end_mirrors_d405_02_intrinsics():
    from application.adapters.s100.client import S100CameraService
    from application.adapters.s100.fake_service import FakeS100Service
    from application.adapters.s100.session import InProcessTransport, SingleReqSession

    fake = FakeS100Service(channels=("cam1", "cam2"))
    session = SingleReqSession(lambda: InProcessTransport(fake.handle))
    service = S100CameraService(session, {"d405_01": "cam1", "d405_02": "cam2"},
                                endpoint="in-process(fake)")
    adapter = MirroredCameraAdapter(service)
    cs = adapter.capture(CaptureRequest(camera_ids=("d405_01", "d405_02"),
                                        deadline_ms=2000))
    assert not cs.errors and set(cs.frames) == {"d405_01", "d405_02"}
    plain = cs.frames["d405_01"]
    mirrored = cs.frames["d405_02"]
    # 两台帧都拿到，尺寸一致（848x480），只有被点名的那台内参被镜像换算
    assert mirrored.color.shape == plain.color.shape == (480, 848, 3)
    assert plain.intrinsics is not None and mirrored.intrinsics is not None
    assert plain.intrinsics.cx == pytest.approx(848 / 2.0)
    assert mirrored.intrinsics.cx == pytest.approx((848 - 1) - 848 / 2.0)
    assert mirrored.intrinsics.cy == pytest.approx((480 - 1) - 480 / 2.0)
    # 深度图也是 180°（Fake 合成圆盘居中 -> 翻转后仍相等，这里验证形状与连续性）
    assert mirrored.depth.shape == (480, 848)
    assert mirrored.depth.data.flags["C_CONTIGUOUS"]
    assert mirrored.depth_scale_m == pytest.approx(0.001)
