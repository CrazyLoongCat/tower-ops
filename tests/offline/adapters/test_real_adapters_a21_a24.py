"""A21-A24 真实适配器离线接口测试 (T05/T07/T25/T31/T32 离线子项; implementation)."""
from __future__ import annotations

import numpy as np
import pytest

from application.adapters.ds86.mock_sdk import MockDs86Sdk
from application.adapters.ds86.real import Ds86Adapter
from application.adapters.realman.mock_sdk import MockRmSdk
from application.adapters.realman.real import RmRobotAdapter, RobotSdkError
from application.adapters.stm32.real import FirmwareProfile, ProfileCodec, load_firmware_profile
from application.contracts.enums import CommandState, FreshnessQuality, ImageSpace
from application.contracts.frames import CaptureRequest
from application.contracts.targets import CartesianPose, JointTarget
from application.services.devices import (
    build_binding_report,
    validate_camera_binding,
    validate_link_board_binding,
    validate_startup_mode,
)

CFG = "configs/firmware_profiles/offline_draft.yaml"


# ---------------- A21 ----------------
def _confirmed_profile() -> FirmwareProfile:
    return FirmwareProfile(
        profile_version="test-confirmed", sof=b"\xAA\x55", address_offset=2,
        address_len=1, type_offset=3, type_len=1, command_id_offset=4,
        command_id_len=2, length_offset=6, length_len=1, payload_offset=7,
        crc_len=2, confirmed=True,
    )


class TestA21Codec:
    def test_roundtrip(self):
        codec = ProfileCodec(_confirmed_profile())
        raw = codec.encode({"address": 2, "frame_type": 0x12, "command_id": 7,
                            "payload": b"\x01\x02"})
        frames = codec.feed(raw)
        assert len(frames) == 1
        assert frames[0].address == 2 and frames[0].command_id == 7

    def test_half_and_sticky(self):
        codec = ProfileCodec(_confirmed_profile())
        raw = codec.encode({"address": 1, "frame_type": 0x11, "command_id": 1, "payload": b"ab"})
        assert codec.feed(raw[:3]) == []
        out = codec.feed(raw[3:] + raw)
        assert len(out) == 2

    def test_crc_error_resync(self):
        codec = ProfileCodec(_confirmed_profile())
        bad = bytearray(codec.encode({"address": 1, "frame_type": 1, "command_id": 1, "payload": b"x"}))
        bad[-1] ^= 0xFF
        good = codec.encode({"address": 1, "frame_type": 1, "command_id": 2, "payload": b"y"})
        out = codec.feed(bytes(bad) + good)
        assert len(out) == 1 and out[0].command_id == 2

    def test_unconfirmed_profile_flagged(self):
        prof = FirmwareProfile(profile_version="draft")
        assert prof.confirmed is False and not prof.is_fake

    def test_load_draft_yaml(self):
        from ruamel.yaml import YAML
        with open(CFG, encoding="utf-8") as fh:
            prof = load_firmware_profile(YAML(typ="safe").load(fh))
        assert prof.confirmed is False
        assert prof.sof == b"\xAA\x55"


# ---------------- A22 ----------------
class TestA22RobotAdapter:
    def _adapter(self):
        sdk = MockRmSdk()
        ad = RmRobotAdapter(sdk=sdk)
        ad.connect()
        return ad, sdk

    def test_connect_creates_handle(self):
        ad, sdk = self._adapter()
        assert ad.connected and "create" in sdk.calls

    def test_pose_unit_conversion(self):
        ad, sdk = self._adapter()
        sdk.joint_deg = [1000.0, 0, 0, 0, 0, 0]
        sdk.pose_m_rad = [1.0, 2.0, 3.0, 0.0, 0.0, 0.0]
        pose = ad.read_pose()
        assert isinstance(pose, CartesianPose)
        assert pose.translation_mm == (1000.0, 2000.0, 3000.0)  # m -> mm

    def test_movej_requires_joint_target(self):
        ad, _ = self._adapter()
        with pytest.raises(TypeError):
            ad.move_joint(CartesianPose(frame_id="base", translation_mm=[0, 0, 0],
                                        orientation_deg=[0, 0, 0]))

    def test_movej_rejects_out_of_limits(self):
        ad, _ = self._adapter()
        h = ad.move_joint(JointTarget(joint_deg=[0, 0, 0, 0, 0, 999]))
        assert h.state is CommandState.REJECTED

    def test_movej_nonblocking_then_complete(self):
        ad, sdk = self._adapter()
        h = ad.move_joint(JointTarget(joint_deg=[10, 0, 0, 0, 0, 0]))
        assert h.state is CommandState.ACCEPTED  # 接受 != 到位
        assert "movej:0" in sdk.calls            # 非阻塞 block=0
        # 到位 + 稳定窗 (第二次 poll 才满足稳定窗)
        ad._stable_window_ms = 0
        ad.poll(h)
        assert ad.poll(h) is CommandState.COMPLETED

    def test_stop_immediate(self):
        ad, sdk = self._adapter()
        ad.move_joint(JointTarget(joint_deg=[10, 0, 0, 0, 0, 0]))
        ad.request_stop()
        assert sdk.stop_called == 1
        assert ad.poll(ad._active) is CommandState.CANCELLED

    def test_disconnect_deletes_handle(self):
        ad, sdk = self._adapter()
        ad.disconnect()
        assert "delete" in sdk.calls and not ad.connected

    def test_no_sdk_injected_raises_clear_error(self):
        ad = RmRobotAdapter()
        with pytest.raises(RobotSdkError):
            ad.connect()


# ---------------- A23 ----------------
class TestA23Ds86Adapter:
    def test_capture_and_intrinsics_alias(self):
        sdk = MockDs86Sdk()
        ad = Ds86Adapter(sdk=sdk)
        ad.open("ds86_01")
        cs = ad.capture(CaptureRequest(camera_ids=("ds86_01",), allow_partial=True))
        f = cs.frames["ds86_01"]
        assert f.depth.data.dtype == np.uint16 and f.depth_scale_m == 0.001
        assert f.intrinsics.cx == 320.0 and f.intrinsics.cy == 240.0  # ppx/ppy -> cx/cy
        assert f.alignment_space is ImageSpace.DEPTH

    def test_buffer_copy_isolates_published_frame(self):
        sdk = MockDs86Sdk(reuse_buffer=True)
        ad = Ds86Adapter(sdk=sdk)
        ad.open("ds86_01")
        f1 = ad.capture(CaptureRequest(camera_ids=("ds86_01",), allow_partial=True)).frames["ds86_01"]
        before = f1.depth.data.copy()
        sdk._depth[0, 0] = 12345  # SDK 复用缓冲改写
        ad.capture(CaptureRequest(camera_ids=("ds86_01",), allow_partial=True))
        assert np.array_equal(f1.depth.data, before)  # 已发布帧未被改写

    def test_sequence_increments(self):
        sdk = MockDs86Sdk()
        ad = Ds86Adapter(sdk=sdk)
        ad.open("ds86_01")
        a = ad.capture(CaptureRequest(camera_ids=("ds86_01",), allow_partial=True)).frames["ds86_01"]
        b = ad.capture(CaptureRequest(camera_ids=("ds86_01",), allow_partial=True)).frames["ds86_01"]
        assert b.sequence == a.sequence + 1

    def test_close(self):
        sdk = MockDs86Sdk()
        ad = Ds86Adapter(sdk=sdk)
        ad.open("ds86_01")
        ad.close()
        assert sdk.closed and not ad.status()["open"]


# ---------------- A24 ----------------
class TestA24Binding:
    def test_duplicate_address_same_link_rejected(self):
        links = [{"link_id": "L", "transport_kind": "serial"}]
        boards = [{"board_id": "b1", "link_id": "L", "address": 1},
                  {"board_id": "b2", "link_id": "L", "address": 1}]
        issues = validate_link_board_binding(links, boards)
        assert any(i.code == "board.addr" for i in issues)

    def test_same_address_independent_links_ok(self):
        links = [{"link_id": "L1", "transport_kind": "serial"},
                 {"link_id": "L2", "transport_kind": "serial"}]
        boards = [{"board_id": "b1", "link_id": "L1", "address": 1},
                  {"board_id": "b2", "link_id": "L2", "address": 1}]
        assert validate_link_board_binding(links, boards) == []

    def test_bad_transport_kind(self):
        issues = validate_link_board_binding([{"link_id": "L", "transport_kind": "tcp"}], [])
        assert any(i.code == "link.kind" for i in issues)

    def test_duplicate_camera_serial(self):
        cams = [{"camera_id": "d405_01", "serial": "S"},
                {"camera_id": "d405_02", "serial": "S"}]
        issues = validate_camera_binding(cams)
        assert any(i.code == "camera.serial" for i in issues)

    def test_unconfirmed_firmware_blocks(self):
        report = build_binding_report(firmware_profile={"profile_version": "x",
                                                        "confirmed": False})
        assert not report.ok

    def test_startup_mode_requires_published_recipe(self):
        issues = validate_startup_mode("auto", has_published_recipe=False,
                                       calibrations_ok=True)
        assert any(i.code == "startup.recipe" for i in issues)
        issues2 = validate_startup_mode("auto", has_published_recipe=True,
                                        calibrations_ok=False)
        assert any(i.code == "startup.calib" for i in issues2)
