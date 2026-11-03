"""边缘服务访问控制 (E08 / E05 访问边界)。

**默认拒绝**：白名单未显式配置时一律 `UNAUTHORIZED` —— 现场未确认前不得把服务对任意客户端开放。

**不要过度承诺**：明文白名单只提供**有限的访问检查**，**不提供保密，也不防重放**；
需要保密/认证请按 [S100访问边界与密钥管理.md](../../doc/部署/S100访问边界与密钥管理.md)
确认方案（如 CURVE）并**验证两端构建**后再声明启用。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from application.contracts.camera_v2 import (
    MAX_HEADER_BYTES,
    Envelope,
    ErrorCode,
    TooLarge,
)

SECURITY_NOTE = ("明文白名单只做访问检查; 不提供保密与防重放; "
                 "加密/认证方案见 doc/部署/S100访问边界与密钥管理.md")


class AccessDenied(RuntimeError):
    """未授权客户端或未满足访问条件。"""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.UNAUTHORIZED) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class AccessPolicy:
    """访问策略：白名单 + 报文上限 + 是否强制 client_id。"""

    allowed_clients: tuple[str, ...] = ()
    allow_all: bool = False            # 仅用于离线/受控环境；现场应保持 False
    require_client_id: bool = True
    max_request_bytes: int = MAX_HEADER_BYTES
    note: str = field(default=SECURITY_NOTE, init=False)

    # ---------- 解析前：先按长度拒绝（不解 JSON） ----------
    def check_size(self, data: bytes) -> None:
        if len(data) > self.max_request_bytes:
            raise TooLarge(
                f"请求 {len(data)} 字节超过上限 {self.max_request_bytes}")

    # ---------- 接任务前：鉴权 ----------
    def authorize(self, envelope: Envelope) -> None:
        client = (envelope.client_id or "").strip()
        if self.require_client_id and not client:
            raise AccessDenied("缺少 client_id（策略要求标识客户端）")
        if self.allow_all:
            return
        if client not in self.allowed_clients:
            raise AccessDenied(f"客户端 {client or '(空)'!r} 不在允许列表内")

    def as_dict(self) -> dict:
        return {"allow_all": self.allow_all,
                "allowed_clients": list(self.allowed_clients),
                "require_client_id": self.require_client_id,
                "max_request_bytes": self.max_request_bytes,
                "note": self.note}


__all__ = ["AccessDenied", "AccessPolicy", "SECURITY_NOTE"]
