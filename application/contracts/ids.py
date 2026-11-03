"""稳定标识 (12 §4.1, 02 §1).

原则: 标签、IP、COM 端口或列表顺序都不是唯一业务身份.
所有 ID 为非空、无首尾空白的字符串; 地址/端口变化不改变 ID.
"""
from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

# 通用稳定 ID: 非空、禁止纯空白、禁止内嵌空白 (允许字母数字 . _ -).
_StableId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128,
                      pattern=r"^[^\s]+$"),
]

DeviceId = _StableId
LinkId = _StableId
BoardId = _StableId
CameraId = _StableId
ChannelId = _StableId
SerialNumber = _StableId
ServiceId = _StableId
StationId = _StableId
StepId = _StableId
ActionId = _StableId
RecipeId = _StableId
PipelineId = _StableId
CalibrationId = _StableId
ToolId = _StableId

RunId = _StableId
StationInstanceId = _StableId
StepExecutionId = _StableId
CaptureId = _StableId
CaptureRequestId = _StableId
FrameId = _StableId
ResultId = _StableId
RequestId = _StableId
ClientId = _StableId
CommandId = _StableId

# 会话/世代号: 单调递增的软件关联, 不代表固件已实现会话字段 (02 §4.2).
SessionEpoch = Annotated[int, Field(ge=0)]
FrameSequence = Annotated[int, Field(ge=0)]
EventSequence = Annotated[int, Field(ge=0)]
SnapshotRevision = Annotated[int, Field(ge=0)]

__all__ = [
    "ActionId", "BoardId", "CalibrationId", "CameraId", "CaptureId",
    "CaptureRequestId", "ChannelId", "ClientId", "CommandId", "DeviceId",
    "EventSequence", "FrameId", "FrameSequence", "LinkId", "PipelineId",
    "RecipeId", "RequestId", "ResultId", "RunId", "SerialNumber", "ServiceId",
    "SessionEpoch", "SnapshotRevision", "StationId", "StationInstanceId",
    "StepExecutionId", "StepId", "ToolId",
]
