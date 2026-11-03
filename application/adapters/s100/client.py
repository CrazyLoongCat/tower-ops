"""S100 相机服务客户端 (E09，协议对齐 E05)。

把 S100 的 V2 采集接到统一相机契约上：

- **唯一服务连接所有者**（复用 :class:`SingleReqSession`），页面不得自开私有 socket；
- 采集走 E05 冻结的 **`submit` → `result`** 两步：submit 立即受理拿 `capture_id`，
  再按期限有界轮询 result；**不使用已废弃的 `trigger` 命令**；
- 逐通道聚合 :class:`FrameBundle` 与错误，一个通道失败不拖垮其它通道。

**真实连接默认不启用**：真实设备连接受 D1 授权约束（见 `doc/实施记录/基线与授权.md`），
本模块只提供实现；是否注册真实传输由装配方决定（离线装配只注册进程内 Fake）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Mapping, Sequence

from application.adapters.s100.framing import ICD_STATUS, PROTOCOL_NAME, SERVICE_NAME
from application.adapters.s100.multipart_decoder import DecodedFrame, decode_multipart
from application.adapters.s100.session import Reply, SingleReqSession
from application.contracts.camera_v2 import Envelope, encode_request
from application.contracts.enums import FreshnessQuality
from application.contracts.frames import (
    CaptureRequest,
    CaptureSet,
    FrameBundle,
    FreshnessPolicy,
    ImageRef,
)

POLL_INTERVAL_S = 0.02


class S100Error(RuntimeError):
    """S100 交互失败（含服务端错误码）。"""


def _header_of(parts: Sequence[bytes]) -> dict:
    if len(parts) < 2:
        raise S100Error("响应缺少 header 段")
    try:
        head = json.loads(bytes(parts[1]).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise S100Error(f"响应 header 不是合法 JSON: {exc}") from exc
    if not isinstance(head, dict):
        raise S100Error("响应 header 必须是 JSON 对象")
    return head


class S100CameraService:
    """S100 通道的相机适配器（一个服务连接服务多个通道）。"""

    def __init__(self, session: SingleReqSession, channels: Mapping[str, str], *,
                 service_id: str = SERVICE_NAME, endpoint: str = "",
                 client_id: str = "windows-main", icd_status: str = ICD_STATUS) -> None:
        self._session = session
        self._channels = dict(channels)          # camera_id -> channel_id(cam1/cam2)
        self.service_id = service_id
        self.endpoint = endpoint
        self.client_id = client_id
        self.icd_status = icd_status
        # 供界面如实标注来源（legacy 适配器用 "s100-v1-pickle" 区分，见 DEC-015）
        self.protocol = PROTOCOL_NAME

    # ---------- 相机契约 ----------
    def enumerate(self) -> list[str]:
        return sorted(self._channels)

    def open(self, camera_id: str) -> None:
        if camera_id not in self._channels:
            raise ValueError(f"未知 S100 相机 {camera_id!r}")
        self._session.open()

    def close(self, camera_id: str | None = None) -> None:
        self._session.close()

    def status(self) -> dict:
        # open 以**会话实况**为准（采集会隐式建立会话），不另存标记
        return {
            "service_id": self.service_id,
            "open": self._session.is_open,
            "channels": dict(self._channels),
            "epoch": self._session.epoch,
            "icd": self.icd_status,
            "endpoint": self.endpoint,
        }

    def ping(self) -> Reply:
        env = Envelope(cmd="ping", request_id="ping", client_id=self.client_id,
                       deadline_ms=2000)
        return self._session.call(encode_request(env), request_id="ping",
                                  deadline_ms=2000, priority=10)

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
        cs.delta_quality = (FreshnessQuality.DEVICE_TIME_VERIFIED if not cs.errors
                            else FreshnessQuality.UNKNOWN)
        return cs

    # ---------- 内部：submit -> result ----------
    def _capture_one(self, cid: str, channel: str,
                     request: CaptureRequest) -> FrameBundle:
        deadline = max(int(request.deadline_ms), 1)
        base = f"{request.request_id or 'adhoc'}:{cid}"
        capture_id = self._submit(base, channel, request, deadline)
        return self._fetch(cid, channel, request, capture_id, base, deadline)

    def _submit(self, base: str, channel: str, request: CaptureRequest,
                deadline: int) -> str:
        rid = f"{base}:submit"
        env = Envelope(
            cmd="submit", request_id=rid, client_id=self.client_id,
            deadline_ms=deadline,
            payload={"cams": [channel],
                     "required_streams": list(request.required_streams),
                     "allow_partial": True})
        reply = self._session.call(encode_request(env), request_id=rid,
                                   deadline_ms=deadline)
        if not reply.ok:
            raise S100Error(reply.error or "submit 失败")
        head = _header_of(reply.parts)
        if head.get("error"):
            raise S100Error(f"{head['error']}: {head.get('message', '')}")
        capture_id = head.get("capture_id")
        if not capture_id:
            raise S100Error("submit 未返回 capture_id")
        return str(capture_id)

    def _fetch(self, cid: str, channel: str, request: CaptureRequest,
               capture_id: str, base: str, deadline: int) -> FrameBundle:
        """有界轮询 result；每次用**不同 request_id**（否则会命中会话去重缓存）。"""
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            rid = f"{base}:result:{attempt}"
            env = Envelope(cmd="result", request_id=rid, client_id=self.client_id,
                           deadline_ms=deadline,
                           payload={"capture_id": capture_id, "cam": channel})
            reply = self._session.call(encode_request(env), request_id=rid,
                                       deadline_ms=deadline)
            if not reply.ok:
                raise S100Error(reply.error or "result 失败")
            head = _header_of(reply.parts)
            code = head.get("error")
            elapsed_ms = (time.monotonic() - started) * 1000.0
            if code in ("NOT_FOUND", "EXPIRED", "BUSY") and elapsed_ms < deadline:
                time.sleep(POLL_INTERVAL_S)      # 尚未就绪: 有界重试
                continue
            if code:
                raise S100Error(f"{code}: {head.get('message', '')}")
            status = head.get("status")
            if status not in (None, "OK"):
                # 只有"仍在进行"才等待; CANCELLED/FAILED/EXPIRED 是终态, 立即报错不空转
                if status in ("ACCEPTED", "RUNNING") and elapsed_ms < deadline:
                    time.sleep(POLL_INTERVAL_S)
                    continue
                raise S100Error(f"采集未成功: status={status} "
                                f"{head.get('frame_error', '')}")
            decoded = decode_multipart(reply.parts, expected_request_id=rid,
                                       required_streams=request.required_streams)
            return self._to_bundle(cid, channel, request, decoded)

    def _to_bundle(self, cid: str, channel: str, request: CaptureRequest,
                   d: DecodedFrame) -> FrameBundle:
        fresh = (FreshnessQuality.ARRIVAL_ONLY
                 if request.freshness_policy is FreshnessPolicy.ALLOW_ARRIVAL_ONLY
                 else FreshnessQuality.DEVICE_TIME_VERIFIED)
        valid_ratio = None
        if d.depth is not None and d.depth.size:
            valid_ratio = float((d.depth != 0).sum()) / float(d.depth.size)
        return FrameBundle(
            camera_id=cid,
            frame_id=f"{cid}-f{d.sequence}",
            capture_id=f"{cid}-c{d.sequence}",
            sequence=d.sequence,
            session_epoch=d.epoch or self._session.epoch,
            service_id=self.service_id,
            channel_id=channel,
            serial=None,
            run_id=request.run_id,
            station_instance_id=request.station_instance_id,
            step_id=request.step_id,
            capture_request_id=request.request_id,
            device_timestamp_ns=d.device_timestamp_ns,
            device_time_domain="s100_device",
            producer_receive_monotonic_s=time.monotonic(),
            capture_accepted_monotonic_s=time.monotonic(),
            utc_recorded=None,
            freshness_quality=fresh,
            color=None if d.color is None else ImageRef.from_array(d.color),
            depth=None if d.depth is None else ImageRef.from_array(d.depth),
            depth_scale_m=d.depth_scale_m,
            intrinsics=d.intrinsics,
            alignment_space=d.intrinsics.intrinsics_space if d.intrinsics else None,
            calibration_version=(d.intrinsics.calibration_version
                                 if d.intrinsics else None),
            depth_valid_ratio=valid_ratio,
            exposure_quality=None,
            capture_errors=(),
        )


__all__ = ["S100CameraService", "S100Error"]
