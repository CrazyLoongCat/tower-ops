"""真实 S100 服务接入（DEC-085：real 模式直接连接，无环境变量闸）。

- **构建不自动连接**（会话惰性打开），地址不得是占位值；
- `python main.py real` 即按设备配置建立真实会话。
"""
from __future__ import annotations

from typing import Mapping

from application.adapters.s100.client import S100CameraService
from application.adapters.s100.session import SingleReqSession, ZmqReqTransport
from application.services.device_config import is_placeholder

DEFAULT_TIMEOUT_MS = 2000


def real_transport_factory(endpoint: str, *, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    """返回真实 ZMQ 传输工厂（本函数本身不打开 socket）。"""
    if is_placeholder(endpoint):
        raise ValueError(f"真实端点未配置或未确认: {endpoint!r}")
    return lambda: ZmqReqTransport(endpoint, timeout_ms=timeout_ms)


def build_s100_service(endpoint: str, channels: Mapping[str, str], *,
                       client_id: str = "windows-main",
                       timeout_ms: int = DEFAULT_TIMEOUT_MS) -> S100CameraService:
    """创建真实 S100 客户端。**构建不连接**（首次使用才 open）。"""
    factory = real_transport_factory(endpoint, timeout_ms=timeout_ms)
    session = SingleReqSession(factory, timeout_ms=timeout_ms)
    return S100CameraService(session, channels, endpoint=endpoint,
                             client_id=client_id)


__all__ = ["DEFAULT_TIMEOUT_MS", "build_s100_service", "real_transport_factory"]
