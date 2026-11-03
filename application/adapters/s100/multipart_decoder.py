"""S100 V2 多段响应的解码与严格校验 (E09 步骤③)。

顺序：魔数/版本 -> 头 JSON -> 段数与索引 -> 声明长度 vs 实际字节数 ->
shape 与 dtype 一致性 -> 构建**独立内存**的只读数组（服务器缓冲可能复用）。

任一步不符即抛错，**不做"尽力而为"的部分解析** —— 宁可报错，不可把错帧当数据。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from application.contracts.camera_v2 import (
    BYTE_ORDER,
    PART_COLOR,
    PART_DEPTH,
    check_multipart_limits,
)
from application.contracts.enums import ImageSpace
from application.contracts.frames import Intrinsics
from application.adapters.s100.framing import (
    MultipartDecodeError,
    ProtocolMismatch,
    SUPPORTED_DTYPES,
    V2_MAGIC,
    V2_VERSION,
    looks_like_pickle,
)

_DTYPE_ITEMSIZE = {"uint8": 1, "uint16": 2, "float32": 4}


@dataclass(frozen=True)
class DecodedFrame:
    """解码后的一帧 (数组已脱离传输缓冲)。"""

    camera_id: str
    channel_id: str
    request_id: str | None
    epoch: int
    sequence: int
    device_timestamp_ns: int | None
    width: int
    height: int
    depth_scale_m: float | None
    color: Any  # np.ndarray | None
    depth: Any  # np.ndarray | None
    intrinsics: Intrinsics | None


def decode_multipart(parts: Sequence[bytes], *,
                     expected_request_id: str | None = None,
                     required_streams: Sequence[str] = ("color", "depth")) -> DecodedFrame:
    """把 V2 多段响应解成 :class:`DecodedFrame`。任何不自洽都抛错。"""
    if not parts:
        raise ProtocolMismatch("空响应")
    check_multipart_limits(parts)          # 段数/单段/总长上限: 拒绝而非截断
    head = bytes(parts[0])
    if looks_like_pickle(head):
        raise ProtocolMismatch("收到 pickle 响应：设计规定 V2 不回退 V1/pickle，已拒绝")
    if head != V2_MAGIC:
        raise ProtocolMismatch(f"未知魔数 {head[:16]!r}（期望 {V2_MAGIC!r}）")

    if len(parts) < 2:
        raise MultipartDecodeError("多段响应缺 header 段")
    try:
        header = json.loads(bytes(parts[1]).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise MultipartDecodeError(f"header 不是合法 JSON: {exc}") from exc
    if not isinstance(header, dict):
        raise MultipartDecodeError("header 必须是 JSON 对象")
    if header.get("v") != V2_VERSION:
        raise MultipartDecodeError(f"协议版本不符: {header.get('v')!r} != {V2_VERSION}")
    byte_order = header.get("byte_order", BYTE_ORDER)
    if byte_order != BYTE_ORDER:
        raise MultipartDecodeError(
            f"byte_order 不支持: {byte_order!r}（本版本只接受 {BYTE_ORDER!r}）")
    if "error" in header:
        raise MultipartDecodeError(f"服务端返回错误: {header['error']}")
    rid = header.get("request_id")
    if expected_request_id is not None and rid != expected_request_id:
        raise MultipartDecodeError(
            f"request_id 不匹配: 收到 {rid!r}, 期望 {expected_request_id!r}（迟到响应不当新任务）")

    streams: dict[str, Any] = {}
    indices: dict[str, int] = {}
    for name in ("color", "depth"):
        meta = header.get(name)
        if meta is None:
            if name in required_streams:
                raise MultipartDecodeError(f"缺少必需流 {name!r}")
            streams[name] = None
            continue
        # 段索引: 头可显式声明 `part`; 缺省回退到固定约定 (color=2, depth=3)
        default_index = PART_COLOR if name == "color" else PART_DEPTH
        try:
            index = int(meta.get("part", default_index))
        except (TypeError, ValueError) as exc:
            raise MultipartDecodeError(f"{name} 的 part 索引非法: {exc}") from exc
        indices[name] = index
        streams[name] = _decode_stream(name, meta, parts, index)

    # 段数与索引: MAGIC + header + 每个存在的流各一段
    present = [n for n in ("color", "depth") if streams.get(n) is not None]
    expected_parts = 2 + len(present)
    if len(parts) != expected_parts:
        raise MultipartDecodeError(
            f"段数不符: 收到 {len(parts)} 段, 头声明应为 {expected_parts} 段")
    used = sorted(indices[n] for n in present)
    if len(set(used)) != len(used):
        raise MultipartDecodeError(f"段索引重复: {indices}")
    for n in present:
        if not (2 <= indices[n] < len(parts)):
            raise MultipartDecodeError(f"{n} 的段索引越界: {indices[n]}")

    width = int(header.get("width") or 0)
    height = int(header.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MultipartDecodeError(f"帧宽高非法: {width}x{height}")
    _check_consistency("color", streams.get("color"), width, height, want_channels=3)
    _check_consistency("depth", streams.get("depth"), width, height, want_channels=None)

    intrinsics = _decode_intrinsics(header.get("intrinsics"), width, height)
    scale = header.get("depth_scale_m")
    if streams.get("depth") is not None and scale is None:
        raise MultipartDecodeError("有深度流但缺 depth_scale_m（不能默认成功）")
    try:
        sequence = int(header.get("seq", 0))
        epoch = int(header.get("epoch", 0))
    except (TypeError, ValueError) as exc:
        raise MultipartDecodeError(f"seq/epoch 非法: {exc}") from exc

    return DecodedFrame(
        camera_id=str(header.get("cam") or ""),
        channel_id=str(header.get("cam") or ""),
        request_id=None if rid is None else str(rid),
        epoch=epoch,
        sequence=sequence,
        device_timestamp_ns=header.get("device_timestamp_ns"),
        width=width,
        height=height,
        depth_scale_m=None if scale is None else float(scale),
        color=streams.get("color"),
        depth=streams.get("depth"),
        intrinsics=intrinsics,
    )


# ---------------------------------------------------------------- 内部
def _decode_stream(name: str, meta: Any, parts: Sequence[bytes], index: int):
    """按 header 声明校验并构建**独立内存**的数组。"""
    import numpy as np

    if not isinstance(meta, dict):
        raise MultipartDecodeError(f"{name} 的元数据必须是对象")
    dtype = str(meta.get("dtype", ""))
    if dtype not in SUPPORTED_DTYPES:
        raise MultipartDecodeError(f"{name} dtype 不支持: {dtype!r}")
    shape = meta.get("shape")
    if not isinstance(shape, (list, tuple)) or not shape:
        raise MultipartDecodeError(f"{name} shape 非法: {shape!r}")
    shape = tuple(int(x) for x in shape)
    if any(x <= 0 for x in shape):
        raise MultipartDecodeError(f"{name} shape 含非正值: {shape}")

    if index >= len(parts):
        raise MultipartDecodeError(f"{name} 缺字节段 (索引 {index}, 共 {len(parts)} 段)")
    buf = bytes(parts[index])

    declared = meta.get("nbytes")
    expect = _DTYPE_ITEMSIZE[dtype]
    for x in shape:
        expect *= x
    if declared is not None and int(declared) != len(buf):
        raise MultipartDecodeError(
            f"{name} 声明长度 {declared} 与实际字节数 {len(buf)} 不符")
    if expect != len(buf):
        raise MultipartDecodeError(
            f"{name} shape/dtype 推出 {expect} 字节, 实际 {len(buf)} 字节")

    arr = np.frombuffer(buf, dtype=dtype).reshape(shape)
    return np.ascontiguousarray(arr).copy()      # 脱离传输缓冲, 服务器复用不篡改


def _check_consistency(name: str, arr, width: int, height: int,
                       want_channels: int | None) -> None:
    if arr is None:
        return
    if arr.shape[0] != height or arr.shape[1] != width:
        raise MultipartDecodeError(
            f"{name} 尺寸 {arr.shape[:2]} 与 header {width}x{height} 不符")
    if want_channels is not None and (arr.ndim != 3 or arr.shape[2] != want_channels):
        raise MultipartDecodeError(
            f"{name} 通道数不符: {arr.shape}（期望 HxWx{want_channels}）")


def _decode_intrinsics(meta: Any, width: int, height: int) -> Intrinsics | None:
    """把服务端 ppx/ppy 命名转成契约的 cx/cy。"""
    if meta is None:
        return None
    if not isinstance(meta, dict):
        raise MultipartDecodeError("intrinsics 必须是对象")
    def pick(*names):
        for n in names:
            if meta.get(n) is not None:
                return meta[n]
        return None
    fx, fy = pick("fx"), pick("fy")
    cx, cy = pick("cx", "ppx"), pick("cy", "ppy")
    if None in (fx, fy, cx, cy):
        raise MultipartDecodeError(f"intrinsics 缺项: {sorted(meta)}")
    try:
        return Intrinsics(
            fx=float(fx), fy=float(fy), cx=float(cx), cy=float(cy),
            width=int(meta.get("width", width)), height=int(meta.get("height", height)),
            intrinsics_space=ImageSpace.DEPTH,
            calibration_version=meta.get("calibration_version"),
        )
    except Exception as exc:  # noqa: BLE001
        raise MultipartDecodeError(f"intrinsics 非法: {exc}") from exc


__all__ = ["DecodedFrame", "decode_multipart"]
