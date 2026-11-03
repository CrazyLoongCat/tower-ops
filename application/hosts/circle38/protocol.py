"""主机<->3.8宿主 报文契约 (12 §4.3, 02 §8.3, T33).

请求头 + 原始图像字节; 响应头 + 可选结果字节.
version/request_id/shape/dtype/长度全部检查; 不合法即拒绝, 不装作成功.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from application.algorithms.circle.params import CircleParams
from application.hosts.circle38.framing import (
    MAX_PAYLOAD_BYTES,
    FramingError,
    encode_message,
)

PROTOCOL = "jt.circle"
VERSION = 1

_DTYPE_ITEMSIZE = {
    "uint8": 1, "int8": 1, "uint16": 2, "int16": 2, "float32": 4, "float64": 8,
}


class ProtocolError(Exception):
    """报文不合法 (版本/长度/dtype/shape/上限)."""


@dataclass
class Request:
    request_id: str
    image: np.ndarray
    rects: list[list[int]]
    params: CircleParams
    deadline_ms: int = 2000

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(int(x) for x in self.image.shape)


@dataclass
class Response:
    request_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    elapsed_ms: float = 0.0
    host_version: str = ""
    payload: bytes = b""


def build_request(req: Request) -> bytes:
    img = req.image
    header = {
        "protocol": PROTOCOL,
        "version": VERSION,
        "request_id": req.request_id,
        "op": "detect",
        "shape": [int(x) for x in img.shape],
        "dtype": str(img.dtype),
        "encoding": "bgr8" if img.ndim == 3 else "gray8",
        "byte_order": "little",
        "deadline_ms": int(req.deadline_ms),
        "params": req.params.to_inputs(),
        "rects": [list(map(int, r)) for r in req.rects],
        "_payload_bytes": int(img.nbytes),
    }
    return encode_message(header, img.tobytes(order="C"))


def parse_request(header: dict, payload: bytes) -> Request:
    if header.get("protocol") != PROTOCOL:
        raise ProtocolError(f"协议不符: {header.get('protocol')!r}")
    if int(header.get("version", -1)) != VERSION:
        raise ProtocolError(f"版本不符: {header.get('version')!r}")
    if header.get("op") != "detect":
        raise ProtocolError(f"未知操作: {header.get('op')!r}")
    dtype = str(header.get("dtype"))
    itemsize = _DTYPE_ITEMSIZE.get(dtype)
    if itemsize is None:
        raise ProtocolError(f"不支持的 dtype: {dtype!r}")
    shape = header.get("shape")
    if not isinstance(shape, list) or not shape:
        raise ProtocolError("shape 非法")
    count = 1
    for d in shape:
        if not isinstance(d, int) or d <= 0:
            raise ProtocolError(f"shape 非法: {shape}")
        count *= d
    expected = count * itemsize
    if expected != len(payload):
        raise ProtocolError(
            f"图像字节数不符: shape{shape}/{dtype} 期望 {expected}, 实际 {len(payload)}"
        )
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ProtocolError("图像超过载荷上限")
    image = np.frombuffer(payload, dtype=dtype).reshape(shape).copy()
    params = _params_from_inputs(header.get("params", {}))
    rects = [list(map(int, r)) for r in header.get("rects", [])]
    return Request(
        request_id=str(header.get("request_id", "")),
        image=image, rects=rects, params=params,
        deadline_ms=int(header.get("deadline_ms", 2000)),
    )


def build_response_ok(request_id: str, result: dict, elapsed_ms: float,
                      host_version: str) -> bytes:
    header = {
        "protocol": PROTOCOL, "version": VERSION, "request_id": request_id,
        "status": "ok", "elapsed_ms": float(elapsed_ms),
        "host_version": host_version, "result": _jsonable(result),
        "_payload_bytes": 0,
    }
    return encode_message(header, b"")


def build_response_error(request_id: str, message: str, host_version: str) -> bytes:
    header = {
        "protocol": PROTOCOL, "version": VERSION, "request_id": request_id,
        "status": "error", "error": message, "host_version": host_version,
        "_payload_bytes": 0,
    }
    return encode_message(header, b"")


def parse_response(header: dict, payload: bytes) -> Response:
    if header.get("protocol") != PROTOCOL:
        raise ProtocolError(f"响应协议不符: {header.get('protocol')!r}")
    status = header.get("status")
    return Response(
        request_id=str(header.get("request_id", "")),
        ok=(status == "ok"),
        result=header.get("result"),
        error=header.get("error"),
        elapsed_ms=float(header.get("elapsed_ms", 0.0)),
        host_version=str(header.get("host_version", "")),
        payload=payload,
    )


def _jsonable(result: dict) -> dict:
    """仅保留可 JSON 序列化的几何字段 (不把大图塞进头)."""
    keep = ("statues", "error_msg", "circle_num", "circle_centers", "radius",
            "diameters", "circle_coverage", "circles_for_show")
    out: dict[str, Any] = {}
    for k in keep:
        if k in result:
            out[k] = result[k]
    return out


def _params_from_inputs(inputs: dict) -> CircleParams:
    if not inputs:
        return CircleParams()
    mapping = {
        "output_type": "3", "circle_type": "4", "output_point": "5",
        "min_radius": "6", "max_radius": "7", "min_cover_ratio": "8",
        "max_cover_ratio": "9", "grad_thresh": "10", "anchor_thresh": "11",
        "min_path_len": "12", "guass_sigma": "13", "arc_fit_error": "14",
        "circle_fit_error": "15", "min_arc_points": "16", "max_arc_points": "17",
        "circle_validation_pro": "18", "resize_factor": "19", "thread_num": "20",
    }
    kwargs = {}
    for name, key in mapping.items():
        if key in inputs:
            kwargs[name] = inputs[key]
    return CircleParams(**kwargs)


__all__ = [
    "PROTOCOL", "VERSION", "ProtocolError", "Request", "Response",
    "build_request", "build_response_error", "build_response_ok", "parse_request",
    "parse_response",
]
