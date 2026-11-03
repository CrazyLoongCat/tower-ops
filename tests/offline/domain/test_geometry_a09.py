"""A09 几何/单位/标定适用性测试 (T07/T31/T32 几何子项)."""
from __future__ import annotations

import numpy as np
import pytest

from application.contracts.targets import CartesianPose
from application.domain.geometry.calibration import (
    Calibration,
    CalibrationKind,
    CalibrationStore,
    TransformService,
    check_applicability,
)
from application.domain.geometry.transforms import (
    GeometryError,
    ds86_target_in_base,
    fixed_camera_target_in_base,
    invert,
    mat_mul,
    matrix_to_pose,
    matrix_to_rpy,
    pose_to_matrix,
    require_homogeneous,
    rpy_to_matrix,
    transform_point,
)


class TestRotationConvention:
    def test_rpy_zyx_matches_reference(self):
        # 与 Jgt handeye._rpy_to_mat (R=Rz*Ry*Rx) 同构: Rz(90) 应为标准绕Z旋转
        R = rpy_to_matrix(0.0, 0.0, 90.0)
        assert np.allclose(R, [[0, -1, 0], [1, 0, 0], [0, 0, 1]], atol=1e-9)

    def test_rx_90(self):
        R = rpy_to_matrix(90.0, 0.0, 0.0)
        assert np.allclose(R, [[1, 0, 0], [0, 0, -1], [0, 1, 0]], atol=1e-9)

    def test_roundtrip(self):
        for rpy in [(10, 20, 30), (-45, 5, 170), (0, 0, 0), (90, 0, 45)]:
            R = rpy_to_matrix(*rpy)
            rx, ry, rz = matrix_to_rpy(R)
            assert np.allclose(rpy_to_matrix(rx, ry, rz), R, atol=1e-6)

    def test_does_not_swap_convention(self):
        # R=Rz*Ry*Rx: 组合 (rx=90, rz=90) 应与 Rz*Rx 一致而不等于 Rx*Rz
        comb = rpy_to_matrix(90, 0, 90)
        rz_rx = rpy_to_matrix(0, 0, 90) @ rpy_to_matrix(90, 0, 0)
        rx_rz = rpy_to_matrix(90, 0, 0) @ rpy_to_matrix(0, 0, 90)
        assert np.allclose(comb, rz_rx, atol=1e-9)
        assert not np.allclose(comb, rx_rz, atol=1e-9)


class TestHomogeneous:
    def test_pose_matrix_roundtrip(self):
        pose = [100.0, 200.0, 300.0, 10.0, 20.0, 30.0]
        T = pose_to_matrix(pose)
        assert np.allclose(matrix_to_pose(T), pose, atol=1e-6)

    def test_require_homogeneous_rejects_bad_last_row(self):
        T = np.eye(4)
        T[3] = [1, 2, 3, 4]
        with pytest.raises(GeometryError):
            require_homogeneous(T)

    def test_require_homogeneous_rejects_shape(self):
        with pytest.raises(GeometryError):
            require_homogeneous(np.eye(3))

    def test_invert(self):
        T = pose_to_matrix([10, 20, 30, 5, 6, 7])
        assert np.allclose(mat_mul(T, invert(T)), np.eye(4), atol=1e-9)

    def test_pose_requires_six(self):
        with pytest.raises(GeometryError):
            pose_to_matrix([1, 2, 3])


class TestDirection:
    def test_TA_B_maps_B_to_A(self):
        # T_A_B 平移 (10,0,0): B 原点 -> A 的 (10,0,0)
        T_A_B = pose_to_matrix([10, 0, 0, 0, 0, 0])
        assert transform_point(T_A_B, (0, 0, 0)) == (10.0, 0.0, 0.0)

    def test_chain_order_matters(self):
        T1 = pose_to_matrix([10, 0, 0, 0, 0, 0])
        T2 = pose_to_matrix([0, 20, 0, 0, 0, 0])
        p = transform_point(mat_mul(T1, T2), (0, 0, 0))
        assert p == (10.0, 20.0, 0.0)
        p2 = transform_point(mat_mul(T2, T1), (0, 0, 0))
        assert p2 == (10.0, 20.0, 0.0)  # 平移可交换但保证方向: 均为 B->A
        # 反向链会得到不同结果, 确保方向敏感
        assert transform_point(mat_mul(invert(T1), T2), (0, 0, 0)) != p


class TestCameraChains:
    def test_ds86_and_fixed_are_distinct(self):
        T_base_flange = pose_to_matrix([100, 0, 0, 0, 0, 0])
        T_flange_cam = pose_to_matrix([0, 50, 0, 0, 0, 0])
        T_cam_target = pose_to_matrix([10, 0, 0, 0, 0, 0])
        ds86 = ds86_target_in_base(T_base_flange, T_flange_cam, T_cam_target)
        # 固定相机: 独立外参 (不是随臂矩阵)
        fixed = fixed_camera_target_in_base(pose_to_matrix([0, 0, 300, 0, 0, 0]),
                                            T_cam_target)
        assert not np.allclose(ds86, fixed)
        assert np.allclose(ds86[:3, 3], (110.0, 50.0, 0.0), atol=1e-9)


class TestCalibration:
    def _store(self):
        return CalibrationStore([
            Calibration(calibration_id="he_ds86", kind=CalibrationKind.HAND_EYE,
                        matrix=np.eye(4).tolist(), version="v1", camera_id="ds86_01"),
            Calibration(calibration_id="fx_d405_1", kind=CalibrationKind.FIXED_CAMERA,
                        matrix=pose_to_matrix([0, 0, 500, 0, 0, 0]).tolist(),
                        version="v1", camera_id="d405_01", resolution=(640, 480)),
        ])

    def test_unknown_calibration_rejected(self):
        svc = TransformService(self._store())
        with pytest.raises(GeometryError):
            svc.ds86_to_base(
                CartesianPose(frame_id="ds86", translation_mm=[0, 0, 0],
                              orientation_deg=[0, 0, 0]),
                np.eye(4).tolist(), "missing")

    def test_kind_mismatch_rejected(self):
        svc = TransformService(self._store())
        with pytest.raises(GeometryError):
            svc.fixed_camera_to_base(
                CartesianPose(frame_id="d405", translation_mm=[0, 0, 0],
                              orientation_deg=[0, 0, 0]), "he_ds86")

    def test_resolution_change_invalidates(self):
        cal = self._store().get("fx_d405_1")
        with pytest.raises(GeometryError):
            check_applicability(cal, resolution=(1280, 720))

    def test_tool_change_invalidates(self):
        cal = Calibration(calibration_id="t", kind=CalibrationKind.TOOL,
                          matrix=np.eye(4).tolist(), version="v1", tool_id="gripA")
        with pytest.raises(GeometryError):
            check_applicability(cal, tool_id="gripB")

    def test_install_revision_change_invalidates(self):
        cal = Calibration(calibration_id="t", kind=CalibrationKind.FIXED_CAMERA,
                          matrix=np.eye(4).tolist(), version="v1",
                          install_revision="rev1")
        with pytest.raises(GeometryError):
            check_applicability(cal, install_revision="rev2")

    def test_units_must_be_mm(self):
        cal = Calibration(calibration_id="m", kind=CalibrationKind.FIXED_CAMERA,
                          matrix=np.eye(4).tolist(), version="v1", units="m")
        with pytest.raises(GeometryError):
            cal.as_matrix()

    def test_hand_eye_requires_capture_pose(self):
        svc = TransformService(self._store())
        with pytest.raises(GeometryError):
            svc.transform_target(
                CartesianPose(frame_id="ds86", translation_mm=[1, 2, 3],
                              orientation_deg=[0, 0, 0]),
                "base", "he_ds86")  # 缺 T_base_flange

    def test_ds86_to_base_end_to_end(self):
        svc = TransformService(self._store())
        out = svc.ds86_to_base(
            CartesianPose(frame_id="ds86", translation_mm=[10, 20, 30],
                          orientation_deg=[0, 0, 0]),
            pose_to_matrix([100, 0, 0, 0, 0, 0]).tolist(), "he_ds86")
        assert out.frame_id == "base"
        assert out.translation_mm == pytest.approx((110.0, 20.0, 30.0))
        assert out.calibration_version == "v1"
