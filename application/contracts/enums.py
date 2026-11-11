"""固定状态枚举 (12 §4.2, 02 §3).

这些枚举是跨层契约: UI/流程/服务/记录使用同一份定义, 不各自维护第二套.
"""
from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """可读、可序列化、可比较字符串的枚举基类 (避免依赖 3.11 的 enum.StrEnum)."""

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.value


class TaskState(StrEnum):
    """任务状态 (02 §3.2). PAUSED 不能由点击即时设置; STOPPED 需停止证据确认."""

    IDLE = "IDLE"
    VALIDATING = "VALIDATING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    WAITING_OPERATOR = "WAITING_OPERATOR"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULT = "FAULT"
    COMPLETED = "COMPLETED"


class CommandState(StrEnum):
    """命令生命周期 (12 §4.2). 传输写成功只标 SENT; 有效 ACK 才 ACCEPTED."""

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    SENT = "SENT"
    ACCEPTED = "ACCEPTED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class DetectionStatus(StrEnum):
    """检测结果六态 (12 §4.2). NO_TARGET 不是默认通过."""

    PASS = "PASS"
    FAIL = "FAIL"
    NO_TARGET = "NO_TARGET"
    INDETERMINATE = "INDETERMINATE"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class StopState(StrEnum):
    """逐执行器停止结果 (12 §4.2). 未知归任务 FAULT 摘要, 不另造 STOP_UNKNOWN 任务态."""

    STOPPING = "STOPPING"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class StopScope(StrEnum):
    """停止范围 (12 §4.2). 任务终止与停止全部范围分别说明."""

    TASK_TERMINATE = "TASK_TERMINATE"
    STOP_ALL = "STOP_ALL"


class TelemetryFreshness(StrEnum):
    """遥测时效 (02 §3.3). 失效后数值保留显示, 但不能继续作为正常反馈."""

    VALID = "VALID"
    STALE = "STALE"
    UNVERIFIED = "UNVERIFIED"


class ConnectionState(StrEnum):
    """链路连接状态 (02 §3.3)."""

    OFFLINE = "OFFLINE"
    CONNECTING = "CONNECTING"
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"


class DeviceOperationalState(StrEnum):
    """设备业务操作状态 (02 §3.3). 与链路状态独立."""

    READY = "READY"
    BUSY = "BUSY"
    FAULT = "FAULT"
    UNKNOWN = "UNKNOWN"


class FreshnessQuality(StrEnum):
    """帧新鲜度质量 (02 §6.2). 请求后收到图不等于请求后曝光."""

    DEVICE_TIME_VERIFIED = "DEVICE_TIME_VERIFIED"
    ARRIVAL_ONLY = "ARRIVAL_ONLY"
    UNKNOWN = "UNKNOWN"


class ImageSpace(StrEnum):
    """图像像素所属空间 (02 §1.1). 像素指标标 px, 不冒充 mm."""

    COLOR = "COLOR"
    DEPTH = "DEPTH"
    ROI = "ROI"


class OrientationConvention(StrEnum):
    """首版领域位姿旋转约定 (02 §1.1): R = Rz * Ry * Rx."""

    RPY_ZYX_DEG = "rpy_zyx_deg"


# 引导定位的段（顺序固定 粗→精）；运动方案执行器与配方校验共用同一份定义。
GUIDE_STAGES = ("coarse", "fine")

# 引导定位的默认相机（DEC-034 的 DS86；DEC-137 起不再"只认"这一台——
# 步骤/页面可选任意已启用相机通道，如 D405 USB；本常量只作缺省值与现场默认排序）。
GUIDANCE_CAMERA_ID = "ds86_01"


class NodeType(StrEnum):
    """固定 node type 映射 (12 §4.4). 未知类型在发布前拒绝."""

    ROBOT_MOVE_JOINT = "robot.move_joint"
    # 按全局参数「螺栓直径」选运动方案路点（DEC-155）：读 manual_ops.yaml 的
    # bolt_size（16/20/24），查步骤里的 16/20/24→路点序号映射，只执行该路点。
    ROBOT_MOVE_BY_BOLT_SIZE = "robot.move_by_bolt_size"
    MECHANISM_ACTION = "mechanism.action"
    # 自定义操作: 选一个**已注册的自定义方法**（安全边界由方法库声明），
    # 可对多个设备同时下发、等全部完成（见 application/services/methods.py）。
    CUSTOM_CALL = "custom.call"
    CAMERA_CAPTURE = "camera.capture"
    VISION_RUN = "vision.run"
    # 引导定位: 相机抓图（默认 DS86，DEC-137 可指定已启用相机）→ 深度引导找圆 →
    # 粗/精偏差 → **每段人工确认**后按 Bolt 工具系 [x,0,z] 偏移（本程序内闭环，DEC-033）。
    GUIDED_POSITION = "vision.guided_position"
    CONDITION = "condition"
    WAIT_STATE = "wait.state"
    OPERATOR_CONFIRM = "operator.confirm"
    RECORD_EVIDENCE = "record.evidence"
    SUBFLOW = "subflow"
    REPEAT = "repeat"
    # 并行执行步骤组（DEC-096）：两个分支同时开始，整组都成功才算完成。
    # 首版**只允许**一个 robot.move_joint 分支 + 一个 mechanism.action 分支；
    # 任一分失败 -> 整组失败并停止另一分支（不嵌套）。
    PARALLEL_GROUP = "parallel.group"


class ConditionOp(StrEnum):
    """结构化比较算子 (12 §4.4). 无 eval / 动态导入 / 可执行 resolver."""

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"
    IN = "in"
    ALL = "all"
    ANY = "any"


class SourceKind(StrEnum):
    """动作来源 (02 §2). 互锁不因来源是 manual 而跳过硬条件."""

    MANUAL = "manual"
    WORKFLOW = "workflow"
    RECOVERY = "recovery"


class FailureClass(StrEnum):
    """失败分类 (02 §9.4). 每类有明确策略, 不用一个 except 统一吞掉."""

    COMMUNICATION = "COMMUNICATION"
    DEVICE = "DEVICE"
    STALENESS = "STALENESS"
    QUALITY = "QUALITY"
    ALGORITHM = "ALGORITHM"
    PROCESS = "PROCESS"
    RECORD = "RECORD"
    CANCELLED = "CANCELLED"


class PausePolicy(StrEnum):
    """暂停策略 (12 §4.4). boundary 等实际完成; immediate_hold 仅设备已验证."""

    BOUNDARY = "boundary"
    IMMEDIATE_HOLD = "immediate_hold"


class RetryClass(StrEnum):
    """重试类别 (02 §5 表). 非幂等运动/夹紧/增量默认不自动重发."""

    IDEMPOTENT_QUERY = "idempotent_query"
    NON_IDEMPOTENT = "non_idempotent"
    NO_RETRY = "no_retry"


class LinkTransportKind(StrEnum):
    """物理承载 (02 §4.1). 透传仅在确认有串口服务器后启用."""

    SERIAL = "serial"
    RAW_RTU_TUNNEL = "raw_rtu_tunnel"


class DeviceProtocol(StrEnum):
    """设备连接协议 (03 §9, E21). UI/探测/持久化共用同一份定义.

    - SERIAL: 板卡串口链路 (pyserial), 对应旧 link 字段;
    - MODBUS_TCP: Modbus-RTU over TCP (功能码 + CRC16, 参照 jute_ui/maintenance/protocol.py);
    - JSON_TCP: 换行/raw_decode 的 JSON 行协议 (msg_id/cmd, 参照 vision_monitor/communication);
    - ZMQ: REQ/REP 请求-应答 (参照 s100_d405_service/zmq_rep_server.py);
    - RTSP: 网络摄像机 RTSP 流 (参照旧工程摄像头配置);
    - HTTP: 网络摄像机 HTTP 流/快照;
    - VENDOR_TCP: 厂商 SDK over TCP (如睿尔曼机械臂 8080), 探测同 TCP 握手;
    - SDK: 本地 SDK 直连 (如 DS86 相机), **无网络参数, 不做连通性探测**.
    """

    SERIAL = "serial"
    MODBUS_TCP = "modbus_tcp"
    JSON_TCP = "json_tcp"
    ZMQ = "zmq"
    RTSP = "rtsp"
    HTTP = "http"
    VENDOR_TCP = "vendor_tcp"
    SDK = "sdk"
    REALSENSE_USB = "realsense_usb"


class FailurePolicy(StrEnum):
    """检测失败去向策略 (12 §4.4). 未批准时停在故障/复核入口."""

    CONTINUE = "continue"
    PAUSE_FOR_REVIEW = "pause_for_review"
    RECAPTURE = "recapture"
    ABORT = "abort"
    FAULT = "fault"


__all__ = [
    "CommandState", "ConditionOp", "ConnectionState", "DetectionStatus",
    "DeviceOperationalState", "DeviceProtocol", "FailureClass", "FailurePolicy",
    "FreshnessQuality", "GUIDANCE_CAMERA_ID", "GUIDE_STAGES", "ImageSpace",
    "LinkTransportKind",
    "NodeType", "OrientationConvention", "PausePolicy", "RetryClass",
    "SourceKind", "StopScope", "StopState", "StrEnum", "TaskState",
    "TelemetryFreshness",
]
