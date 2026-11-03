"""离线 Fake V2 服务（E09 输入：Fake V2 服务接口）。

**与 E05 冻结的命令集一致**：`ping` / `submit`（立即 accepted，返回 capture_id）/
`result`（取回帧或错误）。进程内、**不碰任何网络**。

可注入：超时、错误头、长度不自洽、shape 不自洽、pickle(V1) 响应、epoch 变化 ——
覆盖客户端与服务端的拒绝与隔离路径。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from application.contracts.camera_v2 import BYTE_ORDER, ICD_STATUS, MAGIC, V2_VERSION

DEFAULT_CHANNELS = ("cam1", "cam2")


@dataclass
class FakeFaults:
    timeout: bool = False          # 抛 TimeoutError（触发会话重建）
    error: str | None = None       # submit/result 返回错误码
    bad_nbytes: bool = False       # 头声明长度与实际不符（应被解码器拒绝）
    bad_shape: bool = False        # 头 shape 与实际数组不符
    send_pickle: bool = False      # result 返回 V1/pickle（应被拒绝）
    drop_epoch: int = 0            # 每次 result 递增 epoch（模拟服务重启）


@dataclass
class FakeS100Service:
    """进程内 Fake V2 服务。``handle`` 即 REP 的处理函数。"""

    channels: Sequence[str] = DEFAULT_CHANNELS
    width: int = 848
    height: int = 480
    depth_scale_m: float = 0.001
    faults: FakeFaults = field(default_factory=FakeFaults)
    seq: int = 0
    epoch: int = 7

    def __post_init__(self) -> None:
        self._captures: dict[str, str] = {}

    # ---------- 处理 ----------
    def handle(self, payload: bytes) -> list[bytes]:
        try:
            req = json.loads(payload.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return self._error("BAD_ENVELOPE", f"请求不是合法 JSON: {exc}", None)
        cmd = req.get("cmd")
        rid = req.get("request_id")
        if cmd == "ping":
            return self._ok({"ok": True, "service_epoch": self.epoch,
                             "icd": ICD_STATUS}, rid)
        if cmd in ("capabilities", "health"):
            return self._ok({"ok": True, "v": V2_VERSION, "icd": ICD_STATUS,
                             "channels": list(self.channels)}, rid)
        if cmd == "submit":
            return self._submit(req, rid)
        if cmd == "result":
            return self._result(req, rid)
        if cmd in ("cancel", "release"):
            return self._ok({cmd + "ed": True}, rid)
        return self._error("UNSUPPORTED", f"未知命令 {cmd!r}", rid)

    def _submit(self, req: dict, rid: Any) -> list[bytes]:
        if self.faults.timeout:
            raise TimeoutError("注入: Fake 服务超时")
        if self.faults.error:
            return self._error(self.faults.error, "注入错误", rid)
        payload = req.get("payload") or {}
        cams = payload.get("cams") or []
        if not cams:
            return self._error("BAD_ENVELOPE", "submit 至少需要一个通道", rid)
        cam = str(cams[0])
        if cam not in self.channels:
            return self._error("UNSUPPORTED", f"未知通道 {cam!r}", rid)
        capture_id = f"cap-{self.epoch}-{cam}-{len(self._captures) + 1}"
        self._captures[capture_id] = cam
        return self._ok({"accepted": True, "capture_id": capture_id,
                         "state": "ACCEPTED"}, rid)

    def _result(self, req: dict, rid: Any) -> list[bytes]:
        if self.faults.timeout:
            raise TimeoutError("注入: Fake 服务超时")
        payload = req.get("payload") or {}
        capture_id = str(payload.get("capture_id") or "")
        cam = self._captures.get(capture_id)
        if cam is None:
            return self._error("NOT_FOUND", f"未知 capture_id {capture_id!r}", rid)
        if self.faults.error:
            return self._error(self.faults.error, "注入错误", rid)
        if self.faults.send_pickle:
            import pickle
            return [pickle.dumps({"color": None})]          # V1: 必须被拒绝

        self.seq += 1
        if self.faults.drop_epoch:
            self.epoch += self.faults.drop_epoch
        color, depth = self._synth()
        header = self._header(cam, capture_id, rid, color, depth)
        return [MAGIC, json.dumps(header).encode("utf-8"),
                color.tobytes(), depth.tobytes()]

    # ---------- 内部 ----------
    def _synth(self):
        """合成帧：彩色全零，深度中心一个圆盘（便于离线跑圆检测）。"""
        import numpy as np
        h, w = self.height, self.width
        color = np.zeros((h, w, 3), dtype="uint8")
        depth = np.zeros((h, w), dtype="uint16")
        yy, xx = np.mgrid[0:h, 0:w]
        depth[np.hypot(xx - w / 2.0, yy - h / 2.0) < min(w, h) / 6.0] = 1000
        return color, depth

    def _header(self, cam: str, capture_id: str, rid: Any, color, depth) -> dict:
        declared = color.nbytes if not self.faults.bad_nbytes else color.nbytes + 8
        shape = list(color.shape)
        if self.faults.bad_shape:
            shape = [color.shape[0] + 4, color.shape[1], color.shape[2]]
        return {
            "v": V2_VERSION, "cam": cam, "label": "synthetic(draft)",
            "byte_order": BYTE_ORDER, "status": "OK",
            "capture_id": capture_id, "request_id": rid,
            "seq": self.seq, "epoch": self.epoch,
            "device_timestamp_ns": self.seq * 1_000_000,
            "width": self.width, "height": self.height,
            "depth_scale_m": self.depth_scale_m,
            "color": {"dtype": str(color.dtype), "shape": shape,
                      "nbytes": declared, "part": 2},
            "depth": {"dtype": str(depth.dtype), "shape": list(depth.shape),
                      "nbytes": depth.nbytes, "part": 3},
            "intrinsics": {"fx": 600.0, "fy": 600.0,
                           "ppx": self.width / 2.0, "ppy": self.height / 2.0,
                           "width": self.width, "height": self.height},
        }

    def _ok(self, body: dict, rid: Any) -> list[bytes]:
        header = {"v": V2_VERSION, "request_id": rid, "service_epoch": self.epoch,
                  **body}
        return [MAGIC, json.dumps(header).encode("utf-8")]

    def _error(self, code: str, message: str, rid: Any) -> list[bytes]:
        header = {"v": V2_VERSION, "error": code, "message": message,
                  "request_id": rid, "service_epoch": self.epoch}
        return [MAGIC, json.dumps(header).encode("utf-8")]


__all__ = ["DEFAULT_CHANNELS", "FakeFaults", "FakeS100Service"]
