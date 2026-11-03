"""E08 V2 服务循环（离线，纯字节进出，不碰网络）。

覆盖：默认拒绝 / 白名单 / 超限先拒 / pickle 与版本拒绝 / 未知命令 /
submit→result / 未知 capture_id / 逐通道健康 / 重启换 service_epoch，
以及**客户端↔服务端在同一协议上的往复**（复用 E09 客户端）。
"""
from __future__ import annotations

import json

import pytest

from application.adapters.s100.client import S100CameraService
from application.adapters.s100.session import InProcessTransport, SingleReqSession
from application.contracts.camera_v2 import (
    MAGIC,
    MAX_HEADER_BYTES,
    V2_VERSION,
    decode_request,
)
from application.contracts.frames import CaptureRequest
from edge_service.camera_registry import CameraRegistry, ChannelConfig
from edge_service.capture_jobs import CaptureJobManager
from edge_service.security import AccessPolicy
from edge_service.zmq_server import V2Server

CONFIGS = [ChannelConfig("cam1", "SN-A", width=64, height=48),
           ChannelConfig("cam2", "SN-B", width=64, height=48)]
CLIENT = "windows-main"


def _server(*, policy=None, **job_kw):
    reg = CameraRegistry(CONFIGS)
    reg.start_all()
    jobs = CaptureJobManager(reg, service_epoch=5, **job_kw)
    return V2Server(reg, jobs, policy=policy), jobs, reg


def _req(cmd: str, *, rid: str = "r1", client: str = CLIENT,
         payload: dict | None = None, deadline: int = 2000,
         v: int = V2_VERSION) -> list[bytes]:
    body = {"v": v, "cmd": cmd, "request_id": rid, "client_id": client,
            "deadline_ms": deadline}
    if payload:
        body["payload"] = payload
    return [json.dumps(body, separators=(",", ":")).encode("utf-8")]


def _head(reply: list[bytes]) -> dict:
    return json.loads(reply[1].decode("utf-8"))


# ---------------------------------------------------------------- 访问控制
def test_default_policy_denies_all_clients():
    """默认拒绝：现场未确认前不得对外服务。"""
    server, _jobs, _reg = _server()
    head = _head(server.handle(_req("ping")))
    assert head.get("error") == "UNAUTHORIZED"


def test_whitelist_allows_known_client_and_rejects_other():
    server, _jobs, _reg = _server(policy=AccessPolicy(allowed_clients=(CLIENT,)))
    assert "error" not in _head(server.handle(_req("ping", client=CLIENT)))
    assert _head(server.handle(_req("ping", client="intruder"))).get("error") == \
        "UNAUTHORIZED"


def test_missing_client_id_rejected_when_required():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    head = _head(server.handle(_req("ping", client="")))
    assert head.get("error") == "UNAUTHORIZED"


def test_oversize_rejected_before_parsing():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    head = _head(server.handle([b"x" * (MAX_HEADER_BYTES + 1)]))
    assert head.get("error") == "TOO_LARGE"


def test_pickle_and_bad_version_rejected():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    head = _head(server.handle([b"\x80\x04\x95\x00\x00\x00\x00."]))
    assert head.get("error") == "BAD_ENVELOPE"
    head2 = _head(server.handle(_req("ping", v=1)))
    assert head2.get("error") == "BAD_VERSION"


def test_unknown_command_unsupported():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    head = _head(server.handle(_req("launch")))
    assert head.get("error") == "BAD_ENVELOPE"      # 命令不在白名单 -> 解析即拒


# ---------------------------------------------------------------- 命令
def test_ping_capabilities_health():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    assert _head(server.handle(_req("ping")))["service_epoch"] == 5
    caps = _head(server.handle(_req("capabilities")))
    assert caps["v"] == V2_VERSION and "submit" in caps["commands"]
    health = _head(server.handle(_req("health")))
    assert set(health["channels"]) == {"cam1", "cam2"}
    assert health["channels"]["cam1"]["state"] == "OK"


def test_submit_then_result_returns_frame_parts():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    accepted = _head(server.handle(_req("submit", payload={"cams": ["cam1"]})))
    assert accepted["accepted"] is True and accepted["capture_id"]
    reply = server.handle(_req("result", payload={
        "capture_id": accepted["capture_id"], "cam": "cam1"}))
    assert reply[0] == MAGIC and len(reply) == 4
    head = _head(reply)
    assert head["status"] == "OK" and head["cam"] == "cam1"
    assert head["color"]["part"] == 2 and head["depth"]["part"] == 3


def test_result_unknown_capture_id_is_not_found():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    head = _head(server.handle(_req("result", payload={"capture_id": "nope"})))
    assert head.get("error") == "NOT_FOUND"


def test_submit_busy_when_pending_exceeds_limit():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True), max_pending=1)
    assert _head(server.handle(_req("submit", rid="a",
                                    payload={"cams": ["cam1"]})))["accepted"] is True
    head = _head(server.handle(_req("submit", rid="b", payload={"cams": ["cam2"]})))
    assert head.get("error") == "BUSY"


def test_cancel_and_release_paths():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    cid = _head(server.handle(_req("submit", payload={"cams": ["cam1"]})))["capture_id"]
    assert _head(server.handle(_req("cancel", payload={"capture_id": cid})))["cancelled"]
    # 取消后取结果: 由 pump 落成 CANCELLED
    head = _head(server.handle(_req("result", payload={"capture_id": cid,
                                                       "cam": "cam1"})))
    assert head["status"] == "CANCELLED" and head["frame_error"] == "已取消"
    assert _head(server.handle(_req("release", payload={"capture_id": cid})))["released"]
    assert _head(server.handle(_req("cancel")))["error"] == "BAD_ENVELOPE"


def test_restart_bumps_service_epoch():
    server, jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    assert server.restart() == 6
    assert _head(server.handle(_req("ping")))["service_epoch"] == 6
    assert jobs.stats()["pending"] == 0


# ---------------------------------------------------------------- 端到端
def test_client_and_server_speak_the_same_v2_protocol():
    """E09 客户端 ↔ E08 服务端：同一 E05 协议上跑通采集。"""
    server, _jobs, _reg = _server(policy=AccessPolicy(allowed_clients=(CLIENT,)))
    # 请求是单段, 服务端 handle 收的是"多段列表", 故包一层
    client = S100CameraService(
        SingleReqSession(lambda: InProcessTransport(lambda raw: server.handle([raw]))),
        {"d405_01": "cam1"}, client_id=CLIENT, endpoint="in-process(edge)")
    cs = client.capture(CaptureRequest(camera_ids=("d405_01",), request_id="e2e",
                                       deadline_ms=1500))
    assert not cs.errors, cs.errors
    frame = cs.frames["d405_01"]
    assert frame.depth.shape == (48, 64) and frame.color.shape == (48, 64, 3)
    assert frame.depth_scale_m == 0.001
    assert frame.intrinsics.cx == 32.0 and frame.intrinsics.cy == 24.0
    assert frame.service_id == "s100_d405"
    assert client.status()["open"] is True          # 采集隐式建会


def test_client_reports_server_denied_as_channel_error():
    server, _jobs, _reg = _server()                 # 默认拒绝
    client = S100CameraService(
        SingleReqSession(lambda: InProcessTransport(lambda raw: server.handle([raw]))),
        {"d405_01": "cam1"}, client_id=CLIENT)
    cs = client.capture(CaptureRequest(camera_ids=("d405_01",), request_id="denied",
                                       deadline_ms=500))
    assert "d405_01" in cs.errors and not cs.frames


def test_envelope_round_trip_through_server():
    server, _jobs, _reg = _server(policy=AccessPolicy(allow_all=True))
    env = decode_request(_req("capabilities")[0])
    assert env.cmd == "capabilities" and env.v == V2_VERSION
    assert "error" not in _head(server.handle([_req("capabilities")[0]]))
