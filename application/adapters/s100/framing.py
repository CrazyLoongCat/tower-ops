"""S100 V2 多段帧：常量与剖面的**转发层** (E09)。

真正的单一定义在 :mod:`application.contracts.camera_v2`（E05，服务端与客户端共用）。
本模块保留既有导入路径（`adapters` 内部与测试），并补上本适配特有的服务标识。

编码状态见 :data:`ICD_STATUS`：**candidate（待两端评审冻结）**；
与真实实机的字节级对齐仍需 E06/E10/E11 证据。
"""
from __future__ import annotations

from application.contracts.camera_v2 import (  # noqa: F401 - 转发供既有导入路径使用
    BYTE_ORDER,
    ICD_STATUS,
    MAX_MULTIPART_PARTS,
    MAGIC,
    MultipartDecodeError,
    PROTOCOL_NAME,
    ProtocolMismatch,
    SUPPORTED_DTYPES,
    TooLarge,
    V1_PICKLE_PREFIX,
    V2_VERSION,
    check_multipart_limits,
    looks_like_v1_pickle,
)

V2_MAGIC = MAGIC                                  # 兼容旧名
PICKLE_PREFIX = V1_PICKLE_PREFIX                  # 兼容旧名
looks_like_pickle = looks_like_v1_pickle          # 兼容旧名
SERVICE_NAME = "s100_d405"

__all__ = [
    "BYTE_ORDER", "ICD_STATUS", "MAX_MULTIPART_PARTS", "PICKLE_PREFIX",
    "PROTOCOL_NAME", "SERVICE_NAME", "SUPPORTED_DTYPES", "TooLarge", "V2_MAGIC",
    "V2_VERSION", "MultipartDecodeError", "ProtocolMismatch",
    "check_multipart_limits", "looks_like_pickle",
]
