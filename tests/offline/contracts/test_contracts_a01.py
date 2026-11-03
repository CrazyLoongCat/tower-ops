"""A01 契约测试 (T07/T14/T21/T32/T33 结构子项).

覆盖: 稳定ID、JointTarget/CartesianPose 分型、单位、Handle/StopSummary、FrameBundle.
契约层无 Qt/SDK 副作用 —— 本文件不导入任何设备库.
"""
from __future__ import annotations

import importlib
import sys

import pytest
from pydantic import ValidationError

from application.contracts import units
from application.contracts.enums import (
    CommandState,
    DetectionStatus,
    FreshnessQuality,
    ImageSpace,
    StopScope,
    StopState,
)
from application.contracts.frames import (
    CaptureRequest,
    CaptureSet,
    FrameBundle,
    ImageRef,
    Intrinsics,
)
from application.contracts.handles import CommandHandle, StopSummary
from application.contracts.ids import CameraId
from application.contracts.results import CirclePrimitive, DetectionResult
from application.contracts.targets import CartesianPose, JointTarget, require_joint_target


class TestJointTarget:
    def test_accepts_exactly_six_finite(self):
        jt = JointTarget(joint_deg=[10, -20, 30, 0, 90, -180])
        assert len(jt.joint_deg) == 6
        assert jt.joint_deg[2] == 30.0

    @pytest.mark.parametrize("bad", [
        [1, 2, 3, 4, 5],          # 缺项
        [1, 2, 3, 4, 5, 6, 7],    # 多项
        [],                        # 空
    ])
    def test_rejects_wrong_length_no_padding(self, bad):
        with pytest.raises(ValidationError):
            JointTarget(joint_deg=bad)

    @pytest.mark.parametrize("bad", [
        [1, 2, 3, 4, 5, float("nan")],
        [1, 2, 3, 4, 5, float("inf")],
        [1, 2, 3, 4, 5, "-inf"],
    ])
    def test_rejects_non_finite(self, bad):
        with pytest.raises(ValidationError):
            JointTarget(joint_deg=bad)

    def test_rejects_unknown_field(self):
        with pytest.raises(ValidationError):
            JointTarget(joint_deg=[1, 2, 3, 4, 5, 6], illegal_mode="movel")


class TestPoseTyping:
    def test_cartesian_requires_frame(self):
        with pytest.raises(ValidationError):
            CartesianPose(translation_mm=[1, 2, 3], orientation_deg=[0, 0, 0])  # 缺 frame_id

    def test_cartesian_default_convention(self):
        p = CartesianPose(frame_id="base", translation_mm=[1, 2, 3], orientation_deg=[0, 0, 0])
        assert p.orientation_convention.value == "rpy_zyx_deg"

    def test_pose_is_not_joint_target(self):
        p = CartesianPose(frame_id="base", translation_mm=[1, 2, 3], orientation_deg=[0, 0, 0])
        with pytest.raises(TypeError):
            require_joint_target(p)

    def test_boxed_list_is_not_joint_target(self):
        with pytest.raises(TypeError):
            require_joint_target([1, 2, 3, 4, 5, 6])


class TestUnits:
    def test_length_roundtrip(self):
        assert units.mm_to_m(1000.0) == 1.0
        assert units.m_to_mm(1.0) == 1000.0

    def test_depth_scale(self):
        assert units.depth_raw_to_m(1500, 0.001) == pytest.approx(1.5)
        assert units.depth_raw_to_mm(1500, 0.001) == pytest.approx(1500.0)

    def test_depth_mm_back_to_raw(self):
        """切分上下限按 mm 给 -> 落回原始数组量纲 (DEC-025); 1 原始值 = 1 mm 时是恒等。"""
        assert units.depth_mm_to_raw(1500.0, 0.001) == pytest.approx(1500.0)
        assert units.depth_mm_to_raw(1000.0, 0.002) == pytest.approx(500.0)   # 0.002 m/单位 = 2 mm/单位
        assert units.depth_raw_to_mm(units.depth_mm_to_raw(250.0, 0.0001),
                                     0.0001) == pytest.approx(250.0)          # 往返一致

    def test_depth_scale_must_be_positive(self):
        with pytest.raises(ValueError):
            units.depth_raw_to_m(1, 0.0)
        with pytest.raises(ValueError):
            units.depth_raw_to_m(1, -0.001)
        with pytest.raises(ValueError):
            units.depth_mm_to_raw(1, 0.0)          # 非法比例一律拒绝, 不猜默认值

    def test_angle_units(self):
        assert units.deg_to_rad(180.0) == pytest.approx(3.14159265, abs=1e-6)


class TestHandles:
    def test_command_lifecycle_needs_ack_and_done(self):
        h = CommandHandle(command_id="cmd_1", action_ref="clamp")
        assert h.state is CommandState.CREATED and not h.is_terminal
        h.state = CommandState.SENT
        assert not h.is_success  # 写成功 != 完成
        h.state = CommandState.ACCEPTED
        assert not h.is_success
        h.state = CommandState.COMPLETED
        h.done_ok = True
        assert h.is_success and h.is_terminal

    def test_unknown_is_terminal(self):
        h = CommandHandle(command_id="c", action_ref="a")
        h.state = CommandState.UNKNOWN
        assert h.is_terminal and not h.is_success

    def test_stop_summary_unknown_not_all_green(self):
        s = StopSummary(scope=StopScope.STOP_ALL, reason="fault", request_id="rq")
        s.report("robot").state = StopState.CONFIRMED
        s.report("board_01").state = StopState.UNKNOWN
        assert not s.all_confirmed
        assert s.has_unknown


class TestFrameBundle:
    def _frame(self):
        return FrameBundle(
            camera_id="ds86_01", frame_id="f1", capture_id="c1", sequence=5,
        )

    def test_depth_metrics_required(self):
        f = self._frame()
        with pytest.raises(ValueError):
            f.require_depth_metrics()

    def test_unknown_calibration_and_no_default(self):
        f = self._frame()
        f.depth = ImageRef(data=object(), dtype="uint16", shape=(2, 2), stride=(4, 2), nbytes=8)
        with pytest.raises(ValueError):
            f.require_depth_metrics()  # 缺 depth_scale_m

    def test_freshness_default_unknown(self):
        assert self._frame().freshness_quality is FreshnessQuality.UNKNOWN

    def test_capture_request_validates(self):
        with pytest.raises(ValueError):
            CaptureRequest(camera_ids=(), deadline_ms=1000)
        with pytest.raises(ValueError):
            CaptureRequest(camera_ids=("ds86_01",), deadline_ms=0)

    def test_capture_set_strict_default(self):
        req = CaptureRequest(camera_ids=("ds86_01", "d405_01"))
        cs = CaptureSet(request=req)
        cs.frames["ds86_01"] = self._frame()
        assert not cs.ok  # 默认 allow_partial=False, 缺一台则不通过

    def test_intrinsics_reject_default(self):
        with pytest.raises(ValueError):
            Intrinsics(fx=0, fy=1, cx=0, cy=0, width=10, height=10,
                       intrinsics_space=ImageSpace.COLOR)


class TestDetectionResult:
    def test_six_state_distinct(self):
        r = DetectionResult(result_id="res", status=DetectionStatus.NO_TARGET)
        assert not r.is_pass and r.is_terminal_negative

    def test_cancelled_is_negative_but_not_review(self):
        r = DetectionResult(result_id="res", status=DetectionStatus.CANCELLED)
        assert not r.is_terminal_negative  # 取消保留执行语义, 不进入复核分支

    def test_late_result_scoped_out(self):
        r = DetectionResult(result_id="res", status=DetectionStatus.PASS,
                            run_id="run1", station_instance_id="st1")
        assert r.belongs_to("run1", "st1")
        assert not r.belongs_to("run1", "st2")  # 换工位后旧结果作废

    def test_circle_radius_is_px(self):
        c = CirclePrimitive(cx=10, cy=20, radius_px=5, image_space=ImageSpace.COLOR)
        assert c.radius_px == 5
        with pytest.raises(ValueError):
            CirclePrimitive(cx=0, cy=0, radius_px=-1)


def test_contracts_have_no_device_side_effects():
    """契约层导入不得拉起 Qt / 串口 / ZMQ / 厂家 SDK."""
    forbidden = ("PySide6", "serial", "zmq", "timesai_tools", "cv2")
    already = set(sys.modules)
    for mod in [
        "application.contracts.ids", "application.contracts.enums",
        "application.contracts.units", "application.contracts.targets",
        "application.contracts.handles", "application.contracts.frames",
        "application.contracts.results", "application.contracts.context",
        "application.contracts.ports",
    ]:
        importlib.import_module(mod)
    newly = set(sys.modules) - already
    leaked = [m for m in newly if m.split(".")[0] in forbidden]
    assert not leaked, f"契约层引入了设备依赖: {leaked}"
