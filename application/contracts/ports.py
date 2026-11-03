"""最小端口 (Port) 契约 (12 §4.1, 02 §2).

用 typing.Protocol 描述职责, 不含实现. 异步操作返回可查询 Handle, UI 不阻塞等待.
实现处 (adapters/services) 必须满足这些结构契约.
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from application.contracts.context import RunContext
from application.contracts.enums import SourceKind, StopScope
from application.contracts.frames import CaptureRequest, CaptureSet, FrameBundle
from application.contracts.handles import CommandHandle, StopSummary
from application.contracts.results import DetectionResult
from application.contracts.targets import CartesianPose, JointTarget


@runtime_checkable
class ByteTransportPort(Protocol):
    """字节传输: 把 data 写到物理链路. 不知道帧语义 (02 §4.1)."""

    def write(self, data: bytes) -> bool: ...

    def close(self) -> None: ...

    @property
    def is_open(self) -> bool: ...


@runtime_checkable
class FirmwareCodecPort(Protocol):
    """固件帧编解码. 真实布局以 ICD/抓包为输入; 假协议只服务离线 (02 §4.2)."""

    @property
    def profile_version(self) -> str: ...

    def encode(self, command: Any) -> bytes: ...

    def feed(self, data: bytes) -> list[Any]:
        """增量喂入字节, 返回本次可解析出的完整帧; 半包缓存有界."""
        ...


@runtime_checkable
class LinkPort(Protocol):
    """物理链路: 单读写所有者; 维护 host_link_epoch 与有界接收缓存."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def send(self, data: bytes) -> bool: ...

    def poll(self) -> list[bytes]: ...

    @property
    def epoch(self) -> int: ...

    @property
    def is_open(self) -> bool: ...


@runtime_checkable
class BoardPort(Protocol):
    """逻辑板: 按地址/身份路由; 追踪每板命令; 提供停止优先级 (02 §4.3)."""

    @property
    def board_id(self) -> str: ...

    def execute_action(self, action_id: str, params: dict[str, Any]) -> CommandHandle: ...

    def query(self, command_id: str) -> CommandHandle: ...

    def request_stop(self, command_id: str | None = None) -> None: ...


@runtime_checkable
class RobotPort(Protocol):
    """机器人: Jgt 基线 movej 关节目标; 其他运动能力需独立验证 (02 §5)."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def read_joint(self) -> Sequence[float] | None: ...

    def read_pose(self) -> CartesianPose | None: ...

    def read_state(self) -> dict[str, Any]: ...

    def move_joint(self, target: JointTarget, speed_pct: int = 20, blend: int = 0) -> CommandHandle: ...

    def request_stop(self) -> None: ...

    def set_io(self, io_num: int, state: bool) -> bool: ...

    def read_io(self) -> Sequence[bool]: ...


@runtime_checkable
class CameraPort(Protocol):
    """相机: 采集单帧或集合; 取消保留执行语义 (02 §2/§6)."""

    def enumerate(self) -> Sequence[str]: ...

    def open(self, camera_id: str) -> None: ...

    def close(self, camera_id: str) -> None: ...

    def capture(self, request: CaptureRequest) -> CaptureSet: ...

    def cancel_capture(self, request_id: str) -> None: ...

    def status(self) -> dict[str, Any]: ...


@runtime_checkable
class VisionPort(Protocol):
    """视觉: 无硬件运动副作用; 结果绑定来源与版本 (02 §8)."""

    def list_capabilities(self) -> Sequence[str]: ...

    def validate_pipeline(self, pipeline_id: str) -> list[str]: ...

    def run(self, pipeline_id: str, frame: FrameBundle, params: dict[str, Any] | None = None) -> DetectionResult: ...

    def cancel(self, request_id: str) -> None: ...


@runtime_checkable
class TransformPort(Protocol):
    """坐标变换: 明确坐标系或返回拒绝原因; 未知标定不返回默认成功坐标 (02 §5.4)."""

    def transform_target(self, source: CartesianPose, target_frame: str, calibration_id: str) -> CartesianPose: ...


@runtime_checkable
class StopPort(Protocol):
    """停止协调: 冻结调度、清待发、独立发停止、逐设备核验新鲜状态 (02 §5.3)."""

    def request_stop(self, scope: StopScope, reason: str) -> StopSummary: ...

    def get_stop_summary(self, request_id: str) -> StopSummary | None: ...


@runtime_checkable
class TaskPort(Protocol):
    """任务服务: 校验/启动/暂停/继续/单步/终止 (02 §2/§3.2)."""

    def validate(self, recipe_id: str) -> list[str]: ...

    def start(self, recipe_id: str, source: SourceKind = SourceKind.WORKFLOW) -> str: ...

    def request_pause(self) -> None: ...

    def resume(self) -> None: ...

    def single_step(self) -> None: ...

    def terminate(self, reason: str = "") -> StopSummary: ...


@runtime_checkable
class RecordPort(Protocol):
    """记录: 关键意图/检查点确认后才新下发危险动作; 停止不等 DB (02 §11)."""

    def append_event(self, event: dict[str, Any]) -> None: ...

    def save_capture(self, frame: FrameBundle, path: str) -> str: ...

    def list_runs(self, limit: int = 50) -> Iterable[dict[str, Any]]: ...

    def export_report(self, run_id: str, out_path: str) -> str: ...

    def replay(self, run_id: str) -> Iterable[dict[str, Any]]: ...


@runtime_checkable
class RecipePort(Protocol):
    """配方/配置: 五层校验、草稿/发布/冻结快照、override 保护 (02 §10)."""

    def draft(self, recipe: dict[str, Any]) -> str: ...

    def validate(self, recipe_id: str) -> list[str]: ...

    def publish(self, recipe_id: str, operator: str) -> str: ...

    def compare(self, recipe_id: str, revision_a: str, revision_b: str) -> dict[str, Any]: ...

    def activate(self, recipe_id: str, revision: str) -> RunContext: ...


@runtime_checkable
class ActionCatalogPort(Protocol):
    """动作目录: action_id -> 板/通道/固件指令及完成条件 (01 §3)."""

    def get(self, action_id: str) -> Any: ...

    def known(self, action_id: str) -> bool: ...


__all__ = [
    "ActionCatalogPort", "BoardPort", "ByteTransportPort", "CameraPort",
    "FirmwareCodecPort", "LinkPort", "RecipePort", "RecordPort", "RobotPort",
    "StopPort", "TaskPort", "TransformPort", "VisionPort",
]
