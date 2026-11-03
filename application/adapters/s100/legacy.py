"""现场 S100 **旧协议**接入（V1 / pickle，与已部署的 s100_d405_service 对齐）。

**为什么单独一条路**：现场那台 S100 上跑的是旧服务
（``Ganta_Crtl_System/s100_d405_service/zmq_rep_server.py``）：

    请求（单帧 JSON）  {"cmd": "ping"} / {"cmd": "trigger", "cam": "cam1"}
    回包（单帧）       trigger -> pickle.dumps(帧 dict)；ping/status -> JSON；
                      失败 -> {"error": "..."}

而 :mod:`application.adapters.s100.client` 说的是 **V2 多段协议**
（``[b"S100V2", header_json, color, depth]``，命令 ``submit``/``result``，见 DEC-007 / E05 ICD）。
两者不兼容：旧回包只有**一段**，V2 客户端的 ``_header_of()`` 要求 ``>= 2`` 段，
于是报 **"响应缺少 header 段"** → 抓拍"未取得帧"。

本模块照旧客户端 ``Ganta_Crtl_System/vision_monitor/vision/s100_d405_client.py``
把这条**现场真实可用**的路径接回来。它是**显式选择**的协议（装配方按
``s100_protocol`` 指定），不是"V2 悄悄降级到 pickle" —— 后者仍由
:data:`application.contracts.camera_v2.V1_POLICY` 禁止。

DEC-085：real 模式直接连接，无环境变量闸；
回包 pickle **来自网络**，因此用受限 Unpickler 只放 numpy 反序列化（旧客户端是裸
``pickle.loads``；这里补上这层，不改变正常帧的解析结果）。
"""
from __future__ import annotations

import io
import json
import os
import pickle
from typing import Any, Mapping

from application.adapters.s100.session import SingleReqSession
from application.contracts.enums import FreshnessQuality, ImageSpace
from application.contracts.frames import (
    CaptureRequest,
    CaptureSet,
    FrameBundle,
    ImageRef,
    Intrinsics,
)

SERVICE_NAME = "s100_d405"
PROTOCOL_NAME = "s100-v1-pickle"
ICD_STATUS = "现场旧协议（pickle/v1，非 E05 V2 多段）"

# DEC-079：客户端超时 **必须大于** 服务端 ``grab_timeout_ms``（旧服务默认 2000ms）。
# 两个默认值相等时，服务端正常抓一帧耗时接近 2s 就会先被客户端判超时（"超时（已重建
# 连接）"）。旧客户端 ``s100_d405_client.py`` 用的是 3000ms，这里对齐现场值。
DEFAULT_TIMEOUT_MS = 3000
TIMEOUT_ENV = "TOWEROPS_S100_TIMEOUT_MS"
_MIN_TIMEOUT_MS = 500
_MAX_TIMEOUT_MS = 30000


def resolve_timeout_ms(value: int | None = None) -> int:
    """确定旧协议客户端超时（毫秒）：显式参数 > 环境变量 > :data:`DEFAULT_TIMEOUT_MS`。

    环境变量便于现场不重打包地调整（例如某台 S100 抓帧偏慢）；非法/越界值回落默认，
    并收敛到 500–30000ms，避免把主线程/工作线程卡死。
    """
    raw: Any = value
    if raw is None:
        raw = os.environ.get(TIMEOUT_ENV)
    try:
        ms = int(raw) if raw is not None else DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        ms = DEFAULT_TIMEOUT_MS
    return max(_MIN_TIMEOUT_MS, min(_MAX_TIMEOUT_MS, ms))


class LegacyProtocolError(RuntimeError):
    """旧协议交互失败（含"回包根本不是本协议"这种情形）。"""


class _NumpyOnlyUnpickler(pickle.Unpickler):
    """只允许 numpy 反序列化符号通过 —— 回包来自网络，不放任意可调用对象。

    帧 dict 里只有 dict/str/float 与 ``numpy.ndarray``；``numpy`` 之外的
    ``find_class`` 一律拒绝（否则等于允许对端在本机执行任意代码）。
    """

    def find_class(self, module: str, name: str):  # noqa: ANN201
        if module == "numpy" or module.startswith("numpy."):
            return super().find_class(module, name)
        raise LegacyProtocolError(
            f"回包 pickle 里出现 numpy 之外的对象 {module}.{name}；已拒绝反序列化")


def decode_legacy_reply(raw: bytes) -> Any:
    """还原旧服务回包：**先 JSON、后 pickle**（与旧客户端 ``_recv_dict`` 一致）。

    ``ping``/``status`` 回 JSON，``trigger`` 回 pickle 帧，失败回 ``{"error": ...}``；
    两种都要认。既不是 JSON 也不是 pickle 时，给出**可操作**的报错。
    """
    data = bytes(raw or b"")
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 不是 JSON 就按 pickle 试
        pass
    try:
        return _NumpyOnlyUnpickler(io.BytesIO(data)).load()
    except LegacyProtocolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LegacyProtocolError(
            f"回包既不是 JSON 也不是 pickle: {exc}；请确认 S100 上跑的是 "
            f"s100_d405_service（旧协议），而不是 V2 服务") from exc


class LegacyZmqTransport:
    """旧协议的 ZMQ REQ 传输：**单帧 JSON 请求 → 单帧回包**。

    超时/异常一律抛出去，由 :class:`SingleReqSession` 关闭并重建连接
    （REQ 超时后套接字进错误态，不能继续用 —— 旧客户端也是这么处理的）。
    """

    def __init__(self, endpoint: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 context: Any = None) -> None:
        import zmq                            # 惰性导入: 未装 pyzmq 不影响其它协议
        self._zmq = zmq
        self._endpoint = endpoint
        self._timeout_ms = int(timeout_ms)
        self._ctx = context or zmq.Context.instance()
        self._sock = None
        self._connect()

    def _connect(self) -> None:
        sock = self._ctx.socket(self._zmq.REQ)
        sock.setsockopt(self._zmq.LINGER, 0)
        sock.setsockopt(self._zmq.SNDTIMEO, self._timeout_ms)
        sock.setsockopt(self._zmq.RCVTIMEO, self._timeout_ms)
        sock.connect(self._endpoint)
        self._sock = sock

    def request(self, payload: bytes) -> list[bytes]:
        """``payload`` 为旧协议的 JSON 请求体（utf-8）；返回**单段**回包。"""
        if self._sock is None:
            raise RuntimeError("socket 已关闭")
        try:
            self._sock.send(bytes(payload))       # 等价旧客户端的 send_json: 一帧文本
            return [bytes(self._sock.recv())]
        except self._zmq.Again as exc:            # 发送/接收超时
            raise TimeoutError(f"ZMQ 超时({self._timeout_ms}ms): {exc}") from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close(0)
            except Exception:  # noqa: BLE001
                pass
            self._sock = None


class LegacyS100CameraService:
    """旧协议下的 S100 相机适配器（与 :class:`S100CameraService` **同一个相机契约**）。

    每通道一次 ``trigger`` 往返（REQ/REP 锁步），把旧帧 dict 转成 :class:`FrameBundle`：
    ``color``(BGR uint8) / ``depth``(uint16) / ``depth_scale`` / ``intrinsics`` / ``cam``，
    兼容别名 ``rgb`` / ``depth_array``（与旧客户端一致）。
    """

    def __init__(self, session: SingleReqSession, channels: Mapping[str, str], *,
                 endpoint: str = "", service_id: str = SERVICE_NAME,
                 client_id: str = "windows-main") -> None:
        self._session = session
        self._channels = dict(channels)           # camera_id -> channel_id(cam1/cam2)
        self.service_id = service_id
        self.endpoint = endpoint
        self.client_id = client_id
        self.protocol = PROTOCOL_NAME
        self.icd_status = ICD_STATUS
        self._seq = 0

    # ---------- 相机契约 ----------
    def enumerate(self) -> list[str]:
        return sorted(self._channels)

    def open(self, camera_id: str) -> None:
        if camera_id not in self._channels:
            raise ValueError(f"未知 S100 相机 {camera_id!r}")
        self._session.open()

    def close(self, camera_id: str | None = None) -> None:
        self._session.close()

    def _timeout_ms(self) -> int:
        return int(self._session.timing.get("timeout_ms") or DEFAULT_TIMEOUT_MS)

    def status(self) -> dict:
        return {
            "service_id": self.service_id,
            "open": self._session.is_open,
            "channels": dict(self._channels),
            "epoch": self._session.epoch,
            "protocol": self.protocol,
            "icd": self.icd_status,
            "endpoint": self.endpoint,
            "timeout_ms": self._timeout_ms(),
            "rebuild_count": self._session.rebuild_count,
        }

    def ping(self) -> Any:
        """``{"cmd": "ping"}`` -> 服务的 info（运维排查用）。"""
        request_id = "ping"
        reply = self._session.call(_json_bytes({"cmd": "ping"}),
                                   request_id=request_id,
                                   deadline_ms=self._timeout_ms())
        if not reply.ok:
            raise LegacyProtocolError(reply.error or "ping 失败")
        return self._reply_object(reply.parts, "ping")

    def capture(self, request: CaptureRequest) -> CaptureSet:
        cs = CaptureSet(request=request)
        for cid in request.camera_ids:
            channel = self._channels.get(cid)
            if channel is None:
                cs.errors[cid] = f"未配置的 S100 通道 {cid!r}"
                continue
            try:
                cs.frames[cid] = self._capture_one(cid, channel, request)
            except Exception as exc:  # noqa: BLE001 - 逐通道失败互不影响
                cs.errors[cid] = f"{type(exc).__name__}: {exc}"
        # 旧协议**不带设备时间戳**，只能说"到达即用"，不冒充设备时间校验过
        cs.delta_quality = (FreshnessQuality.ARRIVAL_ONLY if not cs.errors
                            else FreshnessQuality.UNKNOWN)
        return cs

    # ---------- 内部 ----------
    _CAPTURE_ATTEMPTS = 2

    def _capture_one(self, cid: str, channel: str,
                     request: CaptureRequest) -> FrameBundle:
        self._seq += 1
        # 每次 capture 用**不同 request_id**：会话对已作答的 id 直接返回缓存,
        # 复用同一个 id 会拿到上一帧。
        base_rid = f"{request.request_id or 'adhoc'}:{cid}:{self._seq}"
        reply = None
        attempts = 0
        for attempts in range(1, self._CAPTURE_ATTEMPTS + 1):
            # 首次超时后会话已重建连接；再试一次通常能拿到帧（DEC-079）。
            # 重试用新 request_id 的后缀，避免命中会话里对失败结果的缓存。
            rid = base_rid if attempts == 1 else f"{base_rid}:r{attempts}"
            reply = self._session.call(
                _json_bytes({"cmd": "trigger", "cam": channel}), request_id=rid,
                deadline_ms=max(int(request.deadline_ms), self._timeout_ms()))
            if reply.ok:
                break
            if attempts < self._CAPTURE_ATTEMPTS:
                continue
        assert reply is not None
        if not reply.ok:
            raise LegacyProtocolError(self._failure_detail(reply, attempts))
        data = self._reply_object(reply.parts, "trigger")
        if data.get("error"):
            raise LegacyProtocolError(f"服务端报错: {data['error']}")
        if "color" not in data and "rgb" not in data and "depth" not in data:
            raise LegacyProtocolError("服务端没有返回帧数据（回了 ping/status 之类的应答？）")
        return self._to_bundle(cid, channel, request, data)

    def _failure_detail(self, reply: Any, attempts: int) -> str:
        """超时/断链报错附上端点与超时值，现场不用翻日志猜是"没回"还是"回得慢"。"""
        detail = reply.error or "trigger 失败"
        extra = (f"端点={self.endpoint or '未配置'}，客户端超时={self._timeout_ms()}ms"
                 f"（服务端 grab 超时约 2000ms，客户端应大于它）")
        if attempts > 1:
            extra += f"，已自动重试 {attempts - 1} 次"
        return f"{detail}（{extra}）"

    @staticmethod
    def _reply_object(parts: list[bytes], what: str) -> dict:
        if not parts:
            raise LegacyProtocolError(f"{what} 没有回包")
        data = decode_legacy_reply(parts[0])
        if not isinstance(data, dict):
            raise LegacyProtocolError(f"{what} 回包不是 JSON/pickle 对象: "
                                      f"{type(data).__name__}")
        return data

    def _to_bundle(self, cid: str, channel: str, request: CaptureRequest,
                   data: dict) -> FrameBundle:
        import numpy as np

        color = data.get("color")
        if color is None:
            color = data.get("rgb")
        depth = data.get("depth")
        if depth is None:
            depth = data.get("depth_array")

        if color is None and depth is None:
            raise LegacyProtocolError("服务端未返回有效图像数据（相机未就绪？）")
        if color is None:
            # 深度引导以彩色图为主显示面（与旧客户端同判据）
            raise LegacyProtocolError("服务端未返回彩色流，本帧不能用于引导")

        color = np.ascontiguousarray(np.asarray(color))
        if color.dtype != np.uint8:
            color = np.clip(color, 0, 255).astype(np.uint8)
        height, width = int(color.shape[0]), int(color.shape[1])

        if depth is not None:
            depth = np.ascontiguousarray(np.asarray(depth))
            if depth.dtype != np.uint16:
                depth = depth.astype(np.uint16)
            if tuple(depth.shape[:2]) != (height, width):
                depth = None      # 尺寸不一致无法逐像素引导 -> 置空, 不硬凑

        scale = data.get("depth_scale")
        depth_scale_m = float(scale) if scale else None

        return FrameBundle(
            camera_id=cid,
            frame_id=f"{cid}-f{self._seq}",
            capture_id=f"{cid}-c{self._seq}",
            sequence=self._seq,
            session_epoch=self._session.epoch,
            service_id=self.service_id,
            channel_id=channel,
            serial=None,
            run_id=request.run_id,
            station_instance_id=request.station_instance_id,
            step_id=request.step_id,
            capture_request_id=request.request_id,
            # 旧协议没有设备时间戳/序号 -> 如实留空, 新鲜度只说"到达即用"
            device_timestamp_ns=None,
            device_time_domain=None,
            producer_receive_monotonic_s=None,
            capture_accepted_monotonic_s=None,
            utc_recorded=None,
            freshness_quality=FreshnessQuality.ARRIVAL_ONLY,
            color=ImageRef.from_array(color),
            depth=None if depth is None else ImageRef.from_array(depth),
            depth_scale_m=depth_scale_m,
            intrinsics=_intrinsics_of(data.get("intrinsics"), width, height),
            alignment_space=ImageSpace.DEPTH,
            calibration_version=None,
            depth_valid_ratio=(float((depth != 0).sum()) / float(depth.size)
                               if depth is not None and depth.size else None),
            exposure_quality=None,
            capture_errors=(),
        )


def _intrinsics_of(raw: Any, width: int, height: int) -> Intrinsics | None:
    """旧服务的 intrinsics（fx/fy/ppx/ppy/width/height）-> 契约 Intrinsics。

    缺内参时返回 ``None``，**不编造默认值**（``Intrinsics`` 本身也拒绝非正 fx/fy）。
    """
    intr = raw if isinstance(raw, Mapping) else {}
    try:
        fx = float(intr.get("fx") or 0)
        fy = float(intr.get("fy") or 0)
    except (TypeError, ValueError):
        return None
    if fx <= 0 or fy <= 0:
        return None
    ppx = intr.get("ppx", intr.get("cx"))
    ppy = intr.get("ppy", intr.get("cy"))
    try:
        cx = float(ppx) if ppx else width / 2.0
        cy = float(ppy) if ppy else height / 2.0
        w = int(intr.get("width") or width)
        h = int(intr.get("height") or height)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return Intrinsics(fx=fx, fy=fy, cx=cx, cy=cy, width=w, height=h,
                      intrinsics_space=ImageSpace.DEPTH)


def _json_bytes(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(obj), separators=(",", ":")).encode("utf-8")


def build_legacy_s100_service(endpoint: str, channels: Mapping[str, str], *,
                              client_id: str = "windows-main",
                              timeout_ms: int | None = None
                              ) -> LegacyS100CameraService:
    """现场用：授权后创建旧协议客户端。**构建不连接**（首次使用才 open）。

    超时（DEC-079）：显式 ``timeout_ms`` > 环境变量 ``TOWEROPS_S100_TIMEOUT_MS`` >
    默认 3000ms；必须大于服务端 ``grab_timeout_ms``（旧服务默认 2000ms）。
    """
    effective_ms = resolve_timeout_ms(timeout_ms)
    session = SingleReqSession(
        lambda: LegacyZmqTransport(endpoint, timeout_ms=effective_ms),
        timeout_ms=effective_ms)
    return LegacyS100CameraService(session, channels, endpoint=endpoint,
                                   client_id=client_id)


__all__ = [
    "DEFAULT_TIMEOUT_MS", "ICD_STATUS", "LegacyProtocolError",
    "LegacyS100CameraService", "LegacyZmqTransport", "PROTOCOL_NAME",
    "SERVICE_NAME", "TIMEOUT_ENV", "build_legacy_s100_service",
    "decode_legacy_reply", "resolve_timeout_ms",
]
