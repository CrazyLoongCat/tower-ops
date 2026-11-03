"""相机适配注册 (E09 产出：适配注册)。

页面与流程通过注册表拿相机，**不各自开私有 socket**：注册表持有适配器，
抓拍时按请求的 camera_ids 扇出，未注册的通道如实记为该通道错误（不伪装成功）。
"""
from __future__ import annotations

from typing import Any, Mapping

from application.contracts.frames import CaptureRequest, CaptureSet


class CameraRegistry:
    """camera_id -> 适配器 的注册表；一个适配器可服务多个通道。"""

    def __init__(self, adapters: Mapping[str, Any] | None = None) -> None:
        self._adapters: dict[str, Any] = dict(adapters or {})

    # ---------- 注册 ----------
    def register(self, camera_id: str, adapter: Any) -> None:
        self._adapters[camera_id] = adapter

    def unregister(self, camera_id: str) -> None:
        self._adapters.pop(camera_id, None)

    @property
    def adapters(self) -> dict[str, Any]:
        """给既有消费者（执行器/状态注册表）用的普通字典视图。"""
        return dict(self._adapters)

    def camera_ids(self) -> list[str]:
        return sorted(self._adapters)

    def has(self, camera_id: str) -> bool:
        return camera_id in self._adapters

    # ---------- 会话 ----------
    def open(self, camera_id: str) -> None:
        adapter = self._adapters.get(camera_id)
        if adapter is None:
            raise ValueError(f"未注册相机 {camera_id!r}")
        adapter.open(camera_id)

    def close(self, camera_id: str) -> None:
        adapter = self._adapters.get(camera_id)
        if adapter is None:
            raise ValueError(f"未注册相机 {camera_id!r}")
        adapter.close(camera_id)

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for cid, adapter in self._adapters.items():
            fn = getattr(adapter, "status", None)
            try:
                out[cid] = fn() if callable(fn) else {"open": None}
            except Exception as exc:  # noqa: BLE001
                out[cid] = {"open": None, "error": f"{type(exc).__name__}: {exc}"}
        return out

    # ---------- 抓拍 ----------
    def capture(self, request: CaptureRequest) -> CaptureSet:
        cs = CaptureSet(request=request)
        for cid in request.camera_ids:
            adapter = self._adapters.get(cid)
            if adapter is None:
                cs.errors[cid] = f"未注册相机 {cid!r}"
                continue
            try:
                sub = adapter.capture(CaptureRequest(
                    camera_ids=(cid,),
                    required_streams=request.required_streams,
                    deadline_ms=request.deadline_ms,
                    allow_partial=True,
                    freshness_policy=request.freshness_policy,
                    request_id=request.request_id,
                    run_id=request.run_id,
                    station_instance_id=request.station_instance_id,
                    step_id=request.step_id,
                ))
            except Exception as exc:  # noqa: BLE001
                cs.errors[cid] = f"{type(exc).__name__}: {exc}"
                continue
            cs.frames.update(sub.frames)
            cs.errors.update(sub.errors)
        return cs


__all__ = ["CameraRegistry"]
