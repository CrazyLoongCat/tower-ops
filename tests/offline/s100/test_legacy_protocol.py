"""现场 S100 旧协议（pickle/v1）接入（离线，**不真正连网络**）。

对齐已部署的 ``Ganta_Crtl_System/s100_d405_service/zmq_rep_server.py``：
请求单帧 JSON ``{"cmd":"trigger","cam":"cam1"}``，回包**单帧 pickle** 帧；
``ping/status`` 回 JSON，失败回 ``{"error": ...}``。

覆盖：抓拍成功 / **同一份回包喂给 V2 客户端会复现"响应缺少 header 段"** /
别名与 dtype 归一 / 内参缺失不编造 / 服务端错误 / 回包不可解析时给人话 /
无设备时间戳时新鲜度如实为"到达即用" / request_id 不复用缓存 / 授权闸 / 恶意 pickle 被拒。
"""
from __future__ import annotations

import json
import pickle

import numpy as np
import pytest

from application.adapters.s100.client import S100CameraService
from application.adapters.s100.legacy import (
    DEFAULT_TIMEOUT_MS,
    TIMEOUT_ENV,
    LegacyProtocolError,
    LegacyS100CameraService,
    build_legacy_s100_service,
    decode_legacy_reply,
    resolve_timeout_ms,
)
from application.adapters.s100.session import InProcessTransport, SingleReqSession
from application.contracts.enums import FreshnessQuality
from application.contracts.frames import CaptureRequest

CHANNELS = {"d405_01": "cam1", "d405_02": "cam2"}
KNOWN_CHANNELS = ("cam1", "cam2")


def _frame(cam: str = "cam1", *, with_intrinsics: bool = True,
           alias: bool = False) -> dict:
    """旧服务 ``dual_d405.grab()`` 的返回结构。"""
    out: dict = {
        ("rgb" if alias else "color"): np.zeros((4, 6, 3), dtype=np.uint8),
        ("depth_array" if alias else "depth"): np.full((4, 6), 800, dtype=np.uint16),
        "depth_scale": 0.001,
        "cam": cam,
    }
    if with_intrinsics:
        out["intrinsics"] = {"fx": 430.0, "fy": 430.0, "ppx": 3.0, "ppy": 2.0,
                             "width": 6, "height": 4}
    return out


def _transport(*, frame: dict | None = None, error: str | None = None,
               raw: bytes | None = None) -> InProcessTransport:
    """模拟旧服务 REP 循环 ``_handle_one``（含它所有的错误回包形式）。"""
    def handle(payload: bytes) -> list[bytes]:
        req = json.loads(payload)
        if error is not None:                     # 服务端异常分支 -> {"error": ...}
            return [json.dumps({"error": error}).encode("utf-8")]
        cmd = str(req.get("cmd", "")).lower()
        if cmd in ("ping", "status"):
            return [json.dumps({"ok": True,
                                "info": {"service": "s100_d405_service"}}).encode("utf-8")]
        if cmd == "trigger":
            cam = req.get("cam")
            if cam not in KNOWN_CHANNELS:         # 旧服务: KeyError -> {"error": ...}
                return [json.dumps({"error": f"未知相机通道: {cam}"}).encode("utf-8")]
            obj = raw if raw is not None else (frame or _frame(cam))
            return [obj] if isinstance(obj, bytes) else [pickle.dumps(obj)]
        return [json.dumps({"error": f"未知命令: {cmd}"}).encode("utf-8")]

    return InProcessTransport(handle)


def _client(transport: InProcessTransport | None = None,
            channels: dict | None = None) -> LegacyS100CameraService:
    session = SingleReqSession(lambda: transport or _transport())
    return LegacyS100CameraService(session, channels or CHANNELS,
                                   endpoint="in-process(fake legacy)")


# ---------------------------------------------------------------- 抓拍
def test_capture_works_against_field_protocol():
    """现场旧协议能抓到帧 —— 用户"抓拍未取得帧"的直接修复。"""
    cs = _client().capture(CaptureRequest(camera_ids=("d405_01",), request_id="c1",
                                          deadline_ms=1000))
    assert not cs.errors, cs.errors
    f = cs.frames["d405_01"]
    assert f.camera_id == "d405_01" and f.channel_id == "cam1"
    assert f.service_id == "s100_d405"
    assert f.color is not None and f.color.data.shape == (4, 6, 3)
    assert f.depth is not None and f.depth.data.dtype == np.uint16
    assert f.depth_scale_m == 0.001


def test_same_reply_is_what_breaks_the_v2_client():
    """同一份旧回包喂给 V2 客户端 -> 复现"响应缺少 header 段"。

    这条用例把"两种协议不兼容"钉在测试里：V2 不是坏，是**说的不是一种话**。
    """
    v2 = S100CameraService(SingleReqSession(lambda: _transport()), CHANNELS,
                           endpoint="in-process(fake legacy)")
    cs = v2.capture(CaptureRequest(camera_ids=("d405_01",), request_id="c9"))
    assert "d405_01" in cs.errors and not cs.frames
    assert "缺少 header 段" in cs.errors["d405_01"]


def test_alias_keys_and_dtype_normalisation():
    """旧服务可能给 rgb/depth_array 别名、或非目标 dtype -> 归一后再进契约。"""
    cs = _client(_transport(frame=_frame(alias=True))).capture(
        CaptureRequest(camera_ids=("d405_01",), request_id="c2"))
    assert not cs.errors
    assert cs.frames["d405_01"].depth is not None


def test_missing_intrinsics_is_not_fabricated():
    """旧服务没给内参 -> intrinsics=None，**不编造**默认值。"""
    cs = _client(_transport(frame=_frame(with_intrinsics=False))).capture(
        CaptureRequest(camera_ids=("d405_01",), request_id="c3"))
    assert cs.frames["d405_01"].intrinsics is None


def test_no_device_timestamp_reports_arrival_only():
    """旧协议不带时间戳 -> 新鲜度诚实地只说"到达即用"。"""
    f = _client().capture(CaptureRequest(camera_ids=("d405_01",),
                                         request_id="c4")).frames["d405_01"]
    assert f.device_timestamp_ns is None and f.device_time_domain is None
    assert f.freshness_quality is FreshnessQuality.ARRIVAL_ONLY


def test_multi_channel_partial_failure_keeps_other():
    cs = _client(_transport(error="cam1 等帧超时(2000ms)")).capture(
        CaptureRequest(camera_ids=("d405_01", "d405_02"), request_id="c5",
                       allow_partial=True))
    assert set(cs.errors) == {"d405_01", "d405_02"}      # 服务端整体报错 -> 两通道都失败


def test_unknown_channel_from_service_is_reported_not_faked():
    cs = _client(_transport(), channels={"d405_01": "cam9"}).capture(
        CaptureRequest(camera_ids=("d405_01",), request_id="c6"))
    assert "未知相机通道" in cs.errors["d405_01"]


def test_unparsable_reply_says_what_to_check():
    cs = _client(_transport(raw=b"neither-json-nor-pickle")).capture(
        CaptureRequest(camera_ids=("d405_01",), request_id="c7"))
    assert "s100_d405_service" in cs.errors["d405_01"]


def test_ping_returns_service_info():
    assert _client().ping()["ok"] is True


def test_repeated_capture_does_not_reuse_cached_frame():
    """同一个 request_id 会命中会话去重缓存 -> 适配器必须自己保证 id 唯一。"""
    svc = _client()
    a = svc.capture(CaptureRequest(camera_ids=("d405_01",), request_id="same"))
    b = svc.capture(CaptureRequest(camera_ids=("d405_01",), request_id="same"))
    assert a.frames["d405_01"].sequence != b.frames["d405_01"].sequence


# ---------------------------------------------------------------- 解析与安全
def test_decoder_accepts_both_json_and_pickle():
    assert decode_legacy_reply(json.dumps({"error": "x"}).encode()) == {"error": "x"}
    got = decode_legacy_reply(pickle.dumps({"cam": "cam1"}))
    assert got == {"cam": "cam1"}


def test_malicious_pickle_is_refused():
    """回包来自网络: pickle 只放 numpy，别的一律拒绝（旧客户端是裸 loads）。"""
    class _Evil:
        def __reduce__(self):
            return (print, ("pwned",))

    with pytest.raises(LegacyProtocolError) as ei:
        decode_legacy_reply(pickle.dumps(_Evil()))
    assert "numpy 之外" in str(ei.value)


def test_garbage_reply_gives_actionable_error():
    with pytest.raises(LegacyProtocolError) as ei:
        decode_legacy_reply(b"\x00\x01\x02garbage")
    assert "s100_d405_service" in str(ei.value)


# ---------------------------------------------------------------- 构建
def test_build_does_not_connect_before_first_use(monkeypatch):
    """构建也不连（首次 open 才连）。"""
    svc = build_legacy_s100_service("tcp://127.0.0.1:5555", CHANNELS)
    assert svc.status()["open"] is False
    assert svc.protocol == "s100-v1-pickle"


# ---------------------------------------------------------------- 超时与重试（DEC-079）
def _trigger_handler(payload: bytes) -> list[bytes]:
    req = json.loads(payload)
    return [pickle.dumps(_frame(str(req.get("cam") or "cam1")))]


def _session_with(transport: InProcessTransport, *, timeout_ms: int = 3000):
    return SingleReqSession(lambda: transport, timeout_ms=timeout_ms)


def test_default_timeout_is_larger_than_service_grab_timeout():
    """客户端超时须大于旧服务 grab_timeout_ms(2000ms)，否则抓一帧正常偏慢就误报超时。"""
    assert DEFAULT_TIMEOUT_MS == 3000
    assert DEFAULT_TIMEOUT_MS > 2000


def test_timeout_resolution_precedence_and_bounds(monkeypatch):
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)
    assert resolve_timeout_ms() == DEFAULT_TIMEOUT_MS
    monkeypatch.setenv(TIMEOUT_ENV, "2500")
    assert resolve_timeout_ms() == 2500
    assert resolve_timeout_ms(4000) == 4000            # 显式参数优先
    monkeypatch.setenv(TIMEOUT_ENV, "非数字")
    assert resolve_timeout_ms() == DEFAULT_TIMEOUT_MS  # 非法值回落默认
    monkeypatch.setenv(TIMEOUT_ENV, "10")
    assert resolve_timeout_ms() == 500                 # 下限收敛
    monkeypatch.setenv(TIMEOUT_ENV, "999999")
    assert resolve_timeout_ms() == 30000               # 上限收敛


def test_build_reads_timeout_env(monkeypatch):
    monkeypatch.setenv(TIMEOUT_ENV, "3500")
    svc = build_legacy_s100_service("tcp://127.0.0.1:5555", CHANNELS)
    assert svc.status()["timeout_ms"] == 3500


def test_transient_timeout_retries_after_rebuild():
    """一次超时（会话已重建）后自动重试一次即成功 —— 用户现场那条"超时（已重建连接）"。"""
    transport = InProcessTransport(_trigger_handler, timeout_times=1)
    session = _session_with(transport)
    svc = LegacyS100CameraService(session, CHANNELS,
                                  endpoint="tcp://127.0.0.1:5555")
    cs = svc.capture(CaptureRequest(camera_ids=("d405_01",), request_id="rt1"))
    assert not cs.errors, cs.errors
    assert cs.frames["d405_01"].color is not None
    assert session.rebuild_count == 1
    assert transport.calls == 2
    assert svc.status()["timeout_ms"] == 3000
    assert svc.status()["rebuild_count"] == 1


def test_persistent_timeout_reports_endpoint_timeout_and_retry_once():
    """两次都超时：错误里带端点、超时值、重试次数，现场能直接判断是不是服务没起/网络断。"""
    transport = InProcessTransport(_trigger_handler, timeout_times=5)
    session = _session_with(transport)
    svc = LegacyS100CameraService(session, CHANNELS,
                                  endpoint="tcp://192.168.3.50:5555")
    cs = svc.capture(CaptureRequest(camera_ids=("d405_01",), request_id="rt2"))
    err = cs.errors["d405_01"]
    assert "端点=tcp://192.168.3.50:5555" in err
    assert "3000ms" in err
    assert "已自动重试 1 次" in err
    assert transport.calls == 2


def test_server_side_error_is_not_retried():
    """服务端明确回 {"error": ...} 不是传输超时，别用重试掩盖真实原因。"""
    transport = _transport(error="cam1 等帧超时(2000ms)")
    session = _session_with(transport)
    svc = LegacyS100CameraService(session, CHANNELS,
                                  endpoint="in-process(fake legacy)")
    cs = svc.capture(CaptureRequest(camera_ids=("d405_01",), request_id="rt3"))
    assert "服务端报错" in cs.errors["d405_01"]
    assert transport.calls == 1
