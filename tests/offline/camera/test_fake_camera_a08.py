"""A08 FakeDS86 相机测试 (T07/T12/T21/T31 离线)."""
from __future__ import annotations

import numpy as np

from application.adapters.ds86.fake import ExposureQuality, FakeCamera, FakeCameraConfig
from application.contracts.enums import FreshnessQuality, ImageSpace
from application.contracts.frames import CaptureRequest, FreshnessPolicy


def _cam(pose=None):
    c = FakeCamera(FakeCameraConfig(), pose_provider=(lambda: pose) if pose else None)
    c.open("ds86_01")
    return c


def _req(**kw):
    kw.setdefault("camera_ids", ("ds86_01",))
    return CaptureRequest(**kw)


def test_new_sequence_per_capture():
    c = _cam()
    s1 = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    s2 = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    assert s2.sequence == s1.sequence + 1
    assert s2.frame_id != s1.frame_id


def test_stale_cache_does_not_advance_sequence():
    c = _cam()
    f1 = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    c.faults.stale_cache = True
    f2 = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    # 冒充旧缓存: 序号未前进 (上层据此判定不新鲜)
    assert f2.sequence == f1.sequence


def test_depth_is_uint16_with_scale():
    c = _cam()
    f = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    assert f.depth.data.dtype == np.uint16
    assert f.depth_scale_m == 0.001
    f.require_depth_metrics()  # 不抛异常


def test_intrinsics_match_alignment_space_and_size():
    c = _cam()
    f = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    assert f.intrinsics.width == 640 and f.intrinsics.height == 480
    assert f.intrinsics.intrinsics_space is ImageSpace.DEPTH
    assert f.alignment_space is ImageSpace.DEPTH


def test_published_frame_not_mutated_by_later_capture():
    c = _cam()
    f1 = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    before = f1.depth.data.copy()
    c.capture(_req(allow_partial=True))
    assert np.array_equal(f1.depth.data, before)  # SDK 式复用不改变已发布帧


def test_continuous_preview_keeps_bounded_history_and_owned_frames():
    c = _cam()
    first = c.capture(_req()).frames["ds86_01"]
    before = first.depth.data.copy()
    for _ in range(20):
        c.capture(_req())
    assert len(c.held_frames()) == 8
    assert np.array_equal(first.depth.data, before)


def test_readonly_view():
    c = _cam()
    f = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    view = f.depth.readonly_view()
    assert view.flags.writeable is False


def test_low_valid_depth_reported():
    c = _cam()
    c.faults.low_valid_depth = True
    f = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    assert f.depth_valid_ratio == 0.0


def test_timeout_injection_produces_errors():
    c = _cam()
    c.faults.timeout = True
    cs = c.capture(_req(allow_partial=False))
    assert not cs.ok
    assert "ds86_01" in cs.errors


def test_freshness_policy_arrival_only():
    c = _cam()
    f = c.capture(_req(freshness_policy=FreshnessPolicy.ALLOW_ARRIVAL_ONLY,
                       allow_partial=True)).frames["ds86_01"]
    assert f.freshness_quality is FreshnessQuality.ARRIVAL_ONLY
    f2 = c.capture(_req(allow_partial=True)).frames["ds86_01"]
    assert f2.freshness_quality is FreshnessQuality.DEVICE_TIME_VERIFIED


def test_pose_association_invalidates_on_move():
    robot_pose = [0.0] * 6
    c = FakeCamera(FakeCameraConfig(), pose_provider=lambda: list(robot_pose))
    c.open("ds86_01")
    c.capture(_req(allow_partial=True))
    assert not c.invalidates_on_move(list(robot_pose))
    robot_pose[0] = 10.0  # 移动过
    assert c.invalidates_on_move(list(robot_pose))


def test_unsupported_camera_error():
    c = _cam()
    cs = c.capture(CaptureRequest(camera_ids=("d405_01",)))
    assert "d405_01" in cs.errors
