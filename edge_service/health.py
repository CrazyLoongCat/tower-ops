"""边缘服务能力与健康上报 (E08)。"""

from __future__ import annotations

from typing import Any

from application.contracts.camera_v2 import (
    BYTE_ORDER,
    COMMANDS,
    MAX_MULTIPART_PARTS,
    MAX_PENDING_JOBS,
    MAX_QUEUE_DEPTH,
    MAX_TOTAL_BYTES,
    RESULT_TTL_MS,
    SUPPORTED_DTYPES,
    V1_POLICY,
    V2_VERSION,
)


def capabilities() -> dict:
    """服务自称能力：客户端据此判断能否满足所需流/协议，避免"假装成功"。"""
    return {
        "v": V2_VERSION,
        "commands": list(COMMANDS),
        "streams": ["color", "depth"],
        "dtypes": list(SUPPORTED_DTYPES),
        "byte_order": BYTE_ORDER,
        "max_multipart_parts": MAX_MULTIPART_PARTS,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "max_pending_jobs": MAX_PENDING_JOBS,
        "max_queue_depth": MAX_QUEUE_DEPTH,
        "result_ttl_ms": RESULT_TTL_MS,
        "v1_policy": V1_POLICY,
    }


def health_report(registry: Any, jobs: Any, results: Any, *,
                  service_epoch: int, extra: dict | None = None) -> dict:
    """健康汇总：**逐通道**状态（不把单通道失败抹成整体 OK）。"""
    report = {
        "service_epoch": int(service_epoch),
        "channels": registry.health(),
        "jobs": jobs.stats(),
        "results": results.stats(),
        "frame_store": registry.store_stats(),
    }
    if extra:
        report.update(extra)
    return report


__all__ = ["capabilities", "health_report"]
