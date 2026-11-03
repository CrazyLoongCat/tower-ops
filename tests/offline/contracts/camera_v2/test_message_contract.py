"""S100 V2 双方共同契约用例（版本化样本，E05）。

用 `samples/` 里的版本化消息样本驱动：请求信封编解码、响应多段校验、
错误码、上限拒绝、V1/pickle 拒绝。样本是**两端共同评审**的载体，改动必须同步改文档。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from application.adapters.s100.multipart_decoder import decode_multipart
from application.contracts.camera_v2 import (
    MAGIC,
    MAX_HEADER_BYTES,
    MultipartDecodeError,
    ProtocolMismatch,
    TooLarge,
    V2_VERSION,
    check_multipart_limits,
    decode_request,
    encode_request,
)

SAMPLES = Path(__file__).resolve().parent / "samples"


def _raw(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def _load(name: str) -> dict:
    return json.loads(_raw(name).decode("utf-8"))


# ---------------------------------------------------------------- 样本自身
def test_samples_exist_and_are_version_tagged():
    files = sorted(SAMPLES.glob("*.json"))
    assert files, "缺少版本化消息样本"
    for path in files:
        assert "v2" in path.name, f"样本名必须带版本: {path.name}"
        assert json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 请求
def test_request_samples_decode_and_roundtrip():
    for name in ("v2_request_submit.json", "v2_request_ping.json"):
        env = decode_request(_raw(name))
        assert env.v == V2_VERSION
        again = decode_request(encode_request(env))
        assert (again.cmd, again.request_id, again.client_id, again.deadline_ms) == \
               (env.cmd, env.request_id, env.client_id, env.deadline_ms)


def test_submit_sample_carries_epoch_and_payload():
    env = decode_request(_raw("v2_request_submit.json"))
    assert env.cmd == "submit"
    assert env.service_epoch == 7 and env.channel_epoch == {"cam1": 3}
    assert env.payload["cams"] == ["cam1"]
    assert env.payload["allow_partial"] is True


# ---------------------------------------------------------------- 响应
def test_response_sample_header_decodes_with_arrays():
    header = _load("v2_response_header.json")
    color = np.zeros(tuple(header["color"]["shape"]), dtype=header["color"]["dtype"])
    depth = np.zeros(tuple(header["depth"]["shape"]), dtype=header["depth"]["dtype"])
    parts = [MAGIC, json.dumps(header).encode("utf-8"),
             color.tobytes(), depth.tobytes()]
    d = decode_multipart(parts, expected_request_id="demo-0001")
    assert (d.width, d.height) == (848, 480)
    assert d.depth_scale_m == 0.001
    assert d.color.shape == (480, 848, 3) and d.depth.shape == (480, 848)
    # ppx/ppy -> cx/cy 的转换由解码器负责
    assert d.intrinsics.cx == 424.0 and d.intrinsics.cy == 240.0


def test_error_sample_is_reported_as_error():
    header = _load("v2_error_busy.json")
    with pytest.raises(MultipartDecodeError) as ei:
        decode_multipart([MAGIC, json.dumps(header).encode("utf-8")])
    assert "BUSY" in str(ei.value)


def test_response_rejects_unsupported_byte_order():
    header = _load("v2_response_header.json")
    header["byte_order"] = "big"
    color = np.zeros(tuple(header["color"]["shape"]), dtype="uint8")
    depth = np.zeros(tuple(header["depth"]["shape"]), dtype="uint16")
    with pytest.raises(MultipartDecodeError) as ei:
        decode_multipart([MAGIC, json.dumps(header).encode("utf-8"),
                          color.tobytes(), depth.tobytes()])
    assert "byte_order" in str(ei.value)


# ---------------------------------------------------------------- 拒绝路径
def test_v1_pickle_rejected_in_both_directions():
    pickle_like = b"\x80\x04\x95\x0a\x00\x00\x00\x00\x00\x00\x00."
    with pytest.raises(ProtocolMismatch):
        decode_request(pickle_like)
    with pytest.raises(ProtocolMismatch):
        decode_multipart([pickle_like])


def test_bad_version_and_unknown_command_rejected():
    with pytest.raises(ProtocolMismatch):
        decode_request(json.dumps({"v": 1, "cmd": "ping", "request_id": "x"}).encode())
    with pytest.raises(ProtocolMismatch):
        decode_request(json.dumps({"v": 2, "cmd": "launch", "request_id": "x"}).encode())
    with pytest.raises(ProtocolMismatch):
        decode_request(json.dumps({"v": 2, "cmd": "ping"}).encode())    # 缺 request_id


def test_limits_reject_rather_than_truncate():
    with pytest.raises(TooLarge):
        decode_request(b"x" * (MAX_HEADER_BYTES + 1))
    with pytest.raises(TooLarge):                       # 段数超上限
        check_multipart_limits([MAGIC] + [b""] * 8)
    with pytest.raises(TooLarge):                       # 单段超上限
        check_multipart_limits([MAGIC, b"", b"x" * (64 * 1024 * 1024 + 1)])
