"""S100 相机 V2 共同契约 (E05)。

**服务端 (`edge_service`) 与客户端 (`adapters/s100`) 共用这一份定义** —— 协议只有一处，
避免两端各写一套而漂移（本项目既有教训：受保护路径/枚举都坚持单一定义）。

内容：Envelope 与命令集、service/channel epoch、去重与 TTL、错误码、多段 part 索引与
byte_order、报文/缓存/队列上限、V1(pickle) 隔离规则。

状态：**candidate（待两端评审冻结）**。ICD 人工门槛要求 wire schema 由两端统一评审；
真实实机对齐仍需 E06/E10/E11 证据，本文件不得被用来宣称"已与实机对齐"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------- 协议标识
PROTOCOL_NAME = "s100-camera-v2"
V2_VERSION = 2
MAGIC = b"S100V2"
ICD_STATUS = "candidate（待两端评审冻结；实机对齐需 E06/E10/E11）"

# ---------------------------------------------------------------- 多段布局
PART_MAGIC = 0
PART_HEADER = 1
PART_COLOR = 2
PART_DEPTH = 3
MAX_PARTS = 4
BYTE_ORDER = "little"
SUPPORTED_DTYPES = ("uint8", "uint16", "float32")

# ---------------------------------------------------------------- 上限（拒绝而非截断）
MAX_HEADER_BYTES = 64 * 1024
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MULTIPART_PARTS = 8
MAX_QUEUE_DEPTH = 8            # 单通道在途采集任务
MAX_PENDING_JOBS = 16          # 服务端在途任务
DEDUP_TTL_MS = 30_000          # 相同 (epoch, client, request, hash) 的去重窗口
RESULT_TTL_MS = 60_000         # 结果保留
MAX_RESULTS = 32               # 结果条数上限（超出按 TTL/最旧淘汰）

# ---------------------------------------------------------------- V1 隔离
V1_PICKLE_PREFIX = b"\x80"     # PROTO 0..5 均以 0x80 开头
V1_POLICY = "V1/pickle 仅作隔离诊断；V2 不静默降级到 pickle"


class ErrorCode(str, Enum):
    """跨机错误码（wire 固定字面量，勿随手改）。"""

    BAD_VERSION = "BAD_VERSION"
    BAD_ENVELOPE = "BAD_ENVELOPE"
    UNAUTHORIZED = "UNAUTHORIZED"
    TOO_LARGE = "TOO_LARGE"
    BUSY = "BUSY"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    EXPIRED = "EXPIRED"
    UNSUPPORTED = "UNSUPPORTED"
    INTERNAL = "INTERNAL"


COMMANDS = ("ping", "capabilities", "health", "submit", "result", "cancel",
            "release", "configure")


# ---------------------------------------------------------------- 异常
class ProtocolMismatch(ValueError):
    """不是本协议（含 V1/pickle），或在多段层面就不成立。"""


class MultipartDecodeError(ValueError):
    """多段能识别，但头/段数/长度/shape/dtype 不自洽。"""


class TooLarge(ValueError):
    """超过报文/队列上限（拒绝，不截断）。"""


# ---------------------------------------------------------------- Envelope
@dataclass
class Envelope:
    """请求信封。`service_epoch`/`channel_epoch` 用于**旧任务隔离**。"""

    cmd: str
    request_id: str
    client_id: str = ""
    deadline_ms: int = 2000
    service_epoch: int | None = None
    channel_epoch: Mapping[str, int] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    v: int = V2_VERSION


def looks_like_v1_pickle(data: bytes) -> bool:
    return bool(data) and data[:1] == V1_PICKLE_PREFIX


def encode_request(envelope: Envelope) -> bytes:
    """客户端 -> 服务端：单段 UTF-8 JSON（紧凑分隔符，避免无谓字节）。"""
    body = {
        "v": envelope.v,
        "cmd": envelope.cmd,
        "request_id": envelope.request_id,
        "client_id": envelope.client_id,
        "deadline_ms": int(envelope.deadline_ms),
    }
    if envelope.service_epoch is not None:
        body["service_epoch"] = int(envelope.service_epoch)
    if envelope.channel_epoch:
        body["channel_epoch"] = {k: int(v) for k, v in envelope.channel_epoch.items()}
    if envelope.payload:
        body["payload"] = dict(envelope.payload)
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode_request(data: bytes) -> Envelope:
    """解析并校验请求；不合法一律抛错，**不做兼容降级**。"""
    if looks_like_v1_pickle(data):
        raise ProtocolMismatch("收到 pickle 请求：V2 不静默降级到 pickle（见 V1_POLICY）")
    if len(data) > MAX_HEADER_BYTES:
        raise TooLarge(f"请求超过 {MAX_HEADER_BYTES} 字节上限")
    try:
        body = json.loads(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ProtocolMismatch(f"请求不是合法 JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ProtocolMismatch("请求必须是 JSON 对象")
    if body.get("v") != V2_VERSION:
        raise ProtocolMismatch(f"版本不符: {body.get('v')!r} != {V2_VERSION}")
    cmd = body.get("cmd")
    if cmd not in COMMANDS:
        raise ProtocolMismatch(f"未知命令 {cmd!r}")
    rid = body.get("request_id")
    if not rid:
        raise ProtocolMismatch("缺 request_id（去重与隔离都依赖它）")
    try:
        deadline = int(body.get("deadline_ms", 2000))
    except (TypeError, ValueError) as exc:
        raise ProtocolMismatch(f"deadline_ms 非法: {exc}") from exc
    if deadline <= 0:
        raise ProtocolMismatch("deadline_ms 必须为正")
    return Envelope(
        cmd=cmd, request_id=str(rid), client_id=str(body.get("client_id", "")),
        deadline_ms=deadline, service_epoch=body.get("service_epoch"),
        channel_epoch=dict(body.get("channel_epoch") or {}),
        payload=dict(body.get("payload") or {}),
    )


def check_multipart_limits(parts: Sequence[bytes]) -> None:
    """段数/单段/总长上限检查：**拒绝而非截断**。"""
    if len(parts) > MAX_MULTIPART_PARTS:
        raise TooLarge(f"段数 {len(parts)} 超过上限 {MAX_MULTIPART_PARTS}")
    total = 0
    for i, part in enumerate(parts):
        n = len(part)
        if i == PART_HEADER and n > MAX_HEADER_BYTES:
            raise TooLarge(f"header 段 {n} 字节超过上限 {MAX_HEADER_BYTES}")
        if n > MAX_PART_BYTES:
            raise TooLarge(f"第 {i} 段 {n} 字节超过单段上限 {MAX_PART_BYTES}")
        total += n
    if total > MAX_TOTAL_BYTES:
        raise TooLarge(f"报文总长 {total} 超过上限 {MAX_TOTAL_BYTES}")


__all__ = [
    "BYTE_ORDER", "COMMANDS", "DEDUP_TTL_MS", "Envelope", "ErrorCode", "ICD_STATUS",
    "MAGIC", "MAX_HEADER_BYTES", "MAX_MULTIPART_PARTS", "MAX_PART_BYTES",
    "MAX_PENDING_JOBS", "MAX_QUEUE_DEPTH", "MAX_RESULTS", "MAX_TOTAL_BYTES",
    "MultipartDecodeError", "PART_COLOR", "PART_DEPTH", "PART_HEADER", "PART_MAGIC",
    "PROTOCOL_NAME", "ProtocolMismatch", "RESULT_TTL_MS", "SUPPORTED_DTYPES",
    "TooLarge", "V1_PICKLE_PREFIX", "V1_POLICY", "V2_VERSION",
    "check_multipart_limits", "decode_request", "encode_request", "looks_like_v1_pickle",
]
