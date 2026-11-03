"""E09 S100 客户端契约测试（离线，协议对齐 E05）。

覆盖卡的验收点：解码严格校验 / 拒绝 pickle / request_id 去重 / 超时重建换 epoch /
epoch 隔离旧任务 / 有界队列 / 逐通道错误聚合 / FrameBundle 元数据一致。
全部进程内，不碰网络。客户端与服务端都走 E05 冻结的 **submit → result** 命令集。
"""
from __future__ import annotations

import json

import pytest

from application.adapters.s100.client import S100CameraService
from application.adapters.s100.fake_service import FakeFaults, FakeS100Service
from application.adapters.s100.framing import (
    MultipartDecodeError,
    ProtocolMismatch,
    V2_MAGIC,
)
from application.adapters.s100.multipart_decoder import decode_multipart
from application.adapters.s100.session import (
    InProcessTransport,
    QueueFull,
    SingleReqSession,
)
from application.contracts.camera_v2 import V2_VERSION
from application.contracts.frames import CaptureRequest
from application.services.camera import CameraRegistry

CAM1 = "cam1"


def _service(**fault_kw) -> FakeS100Service:
    return FakeS100Service(faults=FakeFaults(**fault_kw))


def _session_for(service: FakeS100Service, **kw) -> SingleReqSession:
    return SingleReqSession(lambda: InProcessTransport(service.handle), **kw)


def _client(service: FakeS100Service, channels=None, **kw) -> S100CameraService:
    chans = channels or {"d405_01": "cam1", "d405_02": "cam2"}
    return S100CameraService(_session_for(service, **kw), chans)


def _req(cmd: str, rid: str, payload: dict | None = None) -> bytes:
    body = {"v": V2_VERSION, "cmd": cmd, "request_id": rid, "client_id": "tester",
            "deadline_ms": 2000}
    if payload:
        body["payload"] = payload
    return json.dumps(body, separators=(",", ":")).encode()


def _frame_parts(svc: FakeS100Service, cam: str = CAM1, rid: str = "r1") -> list[bytes]:
    """按协议走 submit -> result 取回帧多段。"""
    accepted = svc.handle(_req("submit", f"{rid}:submit", {"cams": [cam]}))
    capture_id = json.loads(accepted[1].decode())["capture_id"]
    return svc.handle(_req("result", f"{rid}:result",
                           {"capture_id": capture_id}))


# ---------------------------------------------------------------- 解码
def test_decode_happy_path_and_intrinsics_mapping():
    parts = _frame_parts(_service())
    d = decode_multipart(parts, expected_request_id="r1:result")
    assert d.channel_id == CAM1 and d.sequence == 1
    assert d.color.shape == (480, 848, 3) and str(d.color.dtype) == "uint8"
    assert d.depth.shape == (480, 848) and str(d.depth.dtype) == "uint16"
    assert d.depth_scale_m == 0.001
    assert d.intrinsics.cx == 848 / 2.0 and d.intrinsics.cy == 480 / 2.0   # ppx/ppy -> cx/cy


def test_decode_rejects_pickle_v1():
    with pytest.raises(ProtocolMismatch) as ei:
        decode_multipart(_frame_parts(_service(send_pickle=True)))
    assert "pickle" in str(ei.value)


def test_decode_rejects_unknown_magic():
    with pytest.raises(ProtocolMismatch):
        decode_multipart([b"WHOAMI", b"{}"])


def test_decode_rejects_length_mismatch():
    with pytest.raises(MultipartDecodeError) as ei:
        decode_multipart(_frame_parts(_service(bad_nbytes=True)))
    assert "不符" in str(ei.value)


def test_decode_rejects_shape_mismatch():
    with pytest.raises(MultipartDecodeError):
        decode_multipart(_frame_parts(_service(bad_shape=True)))


def test_decode_rejects_request_id_mismatch():
    with pytest.raises(MultipartDecodeError) as ei:
        decode_multipart(_frame_parts(_service()), expected_request_id="other")
    assert "request_id" in str(ei.value)


def test_decode_requires_depth_scale_when_depth_present():
    parts = _frame_parts(_service())
    header = json.loads(parts[1].decode())
    header.pop("depth_scale_m")
    parts[1] = json.dumps(header).encode()
    with pytest.raises(MultipartDecodeError) as ei:
        decode_multipart(parts)
    assert "depth_scale" in str(ei.value)


def test_decode_requires_required_stream():
    """只要 depth 时缺 depth 必须报错, 不能默认成功。"""
    parts = _frame_parts(_service())
    header = json.loads(parts[1].decode())
    header.pop("depth")
    trimmed = [V2_MAGIC, json.dumps(header).encode(), parts[2]]     # 去掉深度段
    with pytest.raises(MultipartDecodeError) as ei:
        decode_multipart(trimmed, required_streams=("depth",))
    assert "depth" in str(ei.value)


# ---------------------------------------------------------------- 会话
def test_session_dedup_same_request_id_not_resent():
    svc = _service()
    t = InProcessTransport(svc.handle)
    session = SingleReqSession(lambda: t)
    a = session.call(_req("ping", "dup"), request_id="dup", deadline_ms=500)
    b = session.call(_req("ping", "dup"), request_id="dup", deadline_ms=500)
    assert a.ok and b.ok
    assert b.from_cache is True
    assert t.calls == 1            # 迟到/重复不当新任务


def test_session_timeout_rebuilds_and_bumps_epoch():
    svc = _service()
    t = InProcessTransport(svc.handle, timeout_times=1)
    session = SingleReqSession(lambda: t, timeout_ms=200)
    before = session.epoch
    bad = session.call(_req("ping", "t1"), request_id="t1", deadline_ms=200)
    assert not bad.ok and "超时" in bad.error
    assert session.epoch == before + 1 and session.rebuild_count == 1
    good = session.call(_req("ping", "t2"), request_id="t2", deadline_ms=200)
    assert good.ok                  # 重建后仍可用


def test_session_queue_is_bounded():
    session = SingleReqSession(lambda: InProcessTransport(_service().handle),
                               max_pending=2)
    for i in range(2):
        session.submit(_req("ping", f"q{i}"), request_id=f"q{i}", deadline_ms=100)
    with pytest.raises(QueueFull):
        session.submit(_req("ping", "overflow"), request_id="overflow",
                       deadline_ms=100)


def test_session_pumps_in_priority_then_deadline_order():
    seen: list[str] = []

    def svc(payload: bytes):
        seen.append(json.loads(payload.decode())["request_id"])
        return _service().handle(payload)

    session = SingleReqSession(lambda: InProcessTransport(svc))
    session.submit(_req("ping", "low"), request_id="low", deadline_ms=100, priority=0)
    session.submit(_req("ping", "high"), request_id="high", deadline_ms=900, priority=5)
    session.submit(_req("ping", "mid"), request_id="mid", deadline_ms=100, priority=1)
    session.pump()
    assert seen == ["high", "mid", "low"]


def test_epoch_change_mid_flight_marks_stale():
    """响应回来时连接代已变 -> 结果作废, 不落到新会话。"""
    holder: dict = {}

    class _Bumping:
        def __init__(self) -> None:
            self.done = False

        def request(self, payload: bytes):
            if not self.done:
                self.done = True
                holder["session"]._rebuild("注入: 连接代变化")   # noqa: SLF001
            return _service().handle(payload)

        def close(self) -> None:
            pass

    session = SingleReqSession(lambda: _Bumping())
    holder["session"] = session
    r = session.call(_req("ping", "s1"), request_id="s1", deadline_ms=500)
    assert r.stale is True and not r.ok
    assert "作废" in (r.error or "")


# ---------------------------------------------------------------- 客户端
def test_client_capture_builds_frame_bundle():
    client = _client(_service())
    cs = client.capture(CaptureRequest(camera_ids=("d405_01",), request_id="c1",
                                       deadline_ms=1000))
    assert not cs.errors
    f = cs.frames["d405_01"]
    assert f.camera_id == "d405_01" and f.channel_id == "cam1"
    assert f.sequence == 1 and f.session_epoch >= 1
    assert f.depth_scale_m == 0.001
    assert f.intrinsics is not None and f.intrinsics.width == 848
    assert f.depth_valid_ratio is not None and 0.0 < f.depth_valid_ratio < 1.0
    assert f.color is not None and f.depth is not None


def test_client_capture_reports_unknown_channel():
    client = _client(_service())
    cs = client.capture(CaptureRequest(camera_ids=("d405_09",), request_id="c2"))
    assert "d405_09" in cs.errors and not cs.frames


def test_client_partial_failure_keeps_other_channel():
    svc = _service(error="BUSY")
    client = _client(svc)
    cs = client.capture(CaptureRequest(camera_ids=("d405_01", "d405_02"),
                                       request_id="c3", allow_partial=True))
    assert not cs.frames and cs.errors            # 全部失败 -> 如实报错
    svc.faults.error = None
    cs2 = client.capture(CaptureRequest(camera_ids=("d405_01", "d405_02"),
                                        request_id="c4", allow_partial=True))
    assert set(cs2.frames) == {"d405_01", "d405_02"} and not cs2.errors


def test_client_capture_propagates_decode_error_as_channel_error():
    client = _client(_service(bad_nbytes=True))
    cs = client.capture(CaptureRequest(camera_ids=("d405_01",), request_id="c5"))
    assert "d405_01" in cs.errors and "MultipartDecodeError" in cs.errors["d405_01"]


def test_client_open_close_reflects_in_status():
    client = _client(_service())
    assert client.status()["open"] is False
    client.open("d405_01")
    assert client.status()["open"] is True
    client.close("d405_01")
    assert client.status()["open"] is False
    with pytest.raises(ValueError):
        client.open("nope")


def test_capture_implicitly_opens_session_and_status_reflects_it():
    """采集会隐式建立会话; status 必须以会话实况为准, 不另存标记。"""
    client = _client(_service())
    assert client.status()["open"] is False
    cs = client.capture(CaptureRequest(camera_ids=("d405_01",), request_id="io"))
    assert "d405_01" in cs.frames
    assert client.status()["open"] is True


# ---------------------------------------------------------------- 注册表
def test_registry_fanout_and_unregistered_error():
    client = _client(_service())
    reg = CameraRegistry({"d405_01": client, "d405_02": client})
    assert reg.camera_ids() == ["d405_01", "d405_02"]
    cs = reg.capture(CaptureRequest(camera_ids=("d405_01", "d405_09"),
                                    request_id="r1", allow_partial=True))
    assert "d405_01" in cs.frames and "d405_09" in cs.errors


def test_registry_open_close_unknown_raises():
    reg = CameraRegistry({})
    with pytest.raises(ValueError):
        reg.open("nothing")
