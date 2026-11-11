"""离线组合: 用 Fake 硬件 + 离线圆宿主装配完整闭环栈 (12 §3, A17/A19).

不是生产回退: profile 明确标 offline, 拒绝用 Fake 冒充真实设备 (见 lifecycle 校验).
两个工位的关节目标仅为离线占位, 未确认现场值 (P01/INPUT-PATH pending).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from application.adapters.ds86.fake import FakeCamera, FakeCameraConfig
from application.adapters.camera_mirror import MIRRORED_CAMERA_IDS, wrap_capture_mirror
from application.adapters.realman.fake import FakeRobot, FakeRobotConfig
from application.adapters.stm32.board import BoardSession, FirmwareSemantics
from application.adapters.stm32.codec import FakeFrameCodec, FakeFrameType, FakeProfile
from application.adapters.stm32.registry import Stm32Registry
from application.adapters.stm32.session import LinkSession
from application.adapters.stm32.transport import ScriptedTransport
from application.adapters.s100.client import S100CameraService
from application.adapters.s100.fake_service import FakeS100Service
from application.adapters.s100.session import (
    InProcessTransport as S100InProcessTransport,
    SingleReqSession,
)
from application.adapters.realman.motion import NullMotionArm
from application.contracts.enums import SourceKind, StopState, DeviceProtocol
from application.services.camera import CameraRegistry
from application.services.guidance_config import GuidanceConfigStore
from application.services.guidance_service import GuidanceService
from application.services.motion_config import MotionConfigStore
from application.services.motion_runner import MotionRunner
from application.services.operation_config import OperationConfigStore
from application.services.vision_detections import VisionDetectionStore
from application.contracts.targets import JointTarget
from application.domain.actions import ActionCatalog, ActionDefinition, ParamSpec
from application.domain.interlocks import InterlockService
from application.domain.resources import (
    CAPTURE_STILLNESS, ResourceManager, device_resource,
)
from application.domain.telemetry import StateStore, TelemetryRecord
from application.contracts.enums import TelemetryFreshness, NodeType
from application.algorithms.circle.detector import CircleDecisionConfig
from application.algorithms.circle.params import CircleParams
from application.algorithms.circle.pipeline import CirclePipeline, CirclePipelineSpec
from application.hosts.circle38.backends import StubCircleBackend
from application.hosts.circle38.client import Circle38Proxy, InProcessTransport
from application.hosts.circle38.server import Circle38Server
from application.infrastructure import logging_setup
from application.infrastructure.storage import SqliteStore
from application.services.actions import ActionExecutor
from application.services.device_config import DeviceConfigStore
from application.services.device_state import DeviceStateRegistry
from application.services.methods import default_methods
from application.services.recipes.service import (
    ReferenceCatalog, RecipeService, load_recipes_dir,
)
from application.bootstrap.startup_recipe import load_startup_recipe
from application.services.records.service import RecordService
from application.services.stop import StopCoordinator, StopTarget
from application.services.task import TaskService
from application.workflow.state import TaskStateMachine
from application.workflow.subflows import SubflowRegistry, SubflowTemplate
from application.services.recipes.models import StepModel

# 离线占位: 两个示教点 (未确认; 真实值由 INPUT-PATH 提供)。
# 无工位后，示教点只是**配方步骤里引用的关节目标键**，不再是"工位"。
_OFFLINE_JOINT_TARGETS = {
    "teach_01": JointTarget(joint_deg=[10, 20, 30, 0, 90, 0],
                            waypoint_ref="offline_placeholder"),
    "teach_02": JointTarget(joint_deg=[-10, 20, 30, 0, 90, 0],
                            waypoint_ref="offline_placeholder"),
}
OFFLINE_ACTIONS = [
    # DEC-096：机构动作占用**板卡自己的资源 + 静止保护**，不再占 motion_global —
    # 这样「并行执行步骤组」里机器人运动与机构动作可以同时进行；抓拍仍因
    # capture_stillness 被挡住。互斥仍是硬条件，只按物理执行器拆分。
    ActionDefinition(action_id="approved_single_board_action",
                     device_id="stm32_01",
                     resource_ids=(device_resource("stm32_01"), CAPTURE_STILLNESS),
                     parameters=(ParamSpec(name="force", minimum=0, maximum=100,
                                           required=False),),
                     timeout_ms=3000),
]
OFFLINE_PIPELINES = {"approved_ds86_circle_pipeline"}


def default_actions() -> ActionCatalog:
    return ActionCatalog(OFFLINE_ACTIONS)


def default_subflows() -> SubflowRegistry:
    """离线示例子流程模板 (E13): 抓拍 + 判定, 供 repeat 多次采样复用。"""
    tpl = SubflowTemplate(
        template_id="capture_and_detect",
        version="v1",
        steps=(
            StepModel(id="sub_capture", type=NodeType.CAMERA_CAPTURE,
                      camera_ref="ds86_01"),
            StepModel(id="sub_detect", type=NodeType.VISION_RUN,
                      input_from={"step": "sub_capture", "output": "frame"},
                      pipeline_ref="approved_ds86_circle_pipeline"),
        ),
        allow_overrides=("deadline_ms",),
        description="抓拍并判定 (可复用于多次采样/重抓拍)",
    )
    return SubflowRegistry([tpl])


BOARD_SEMANTICS = FirmwareSemantics(
    command_type=FakeFrameType.COMMAND, ack_type=FakeFrameType.ACK,
    done_type=FakeFrameType.DONE, error_type=FakeFrameType.ERROR,
    stop_type=0x14, is_fake=True,
)


def _make_offline_board() -> tuple[Stm32Registry, LinkSession]:
    """离线板回路: 脚本传输在收到命令帧时回 ACK+DONE (仅离线, 非金样本)."""
    profile = FakeProfile()

    def device_reply(data: bytes) -> list[bytes]:
        try:
            frame = profile.decode(data)
        except Exception:  # noqa: BLE001
            return []
        if frame.frame_type != BOARD_SEMANTICS.command_type or frame.command_id is None:
            return []
        return [
            profile.encode(address=frame.address, frame_type=BOARD_SEMANTICS.ack_type,
                           command_id=frame.command_id, payload=bytes([0x00])),
            profile.encode(address=frame.address, frame_type=BOARD_SEMANTICS.done_type,
                           command_id=frame.command_id, payload=bytes([0x00])),
        ]

    # 离线板回路: 命令帧 -> ACK+DONE 应答 (host 与 device 同进程回环)
    transport = ScriptedTransport(on_write=device_reply)
    codec = FakeFrameCodec(profile)
    link = LinkSession("link0", transport, codec, max_frames_per_flush=4)
    link.open()
    registry = Stm32Registry()
    registry.add_link(link)
    registry.add_board(BoardSession("stm32_01", 0x01, link, BOARD_SEMANTICS), "link0")
    return registry, link



@dataclass
class AppStack:
    """装配好的应用栈。``profile`` 明确区分 ``offline`` / ``production``，不是可回退的 fallback。

    offline 由 :func:`build_offline_stack` 造（Fake 硬件）；
    production 由 :mod:`application.bootstrap.production` 造（真实适配器）。
    两者的属性名一致，页面无需区分。
    """

    profile: str
    resources: ResourceManager
    state_store: StateStore
    actions: ActionCatalog
    interlocks: InterlockService
    records: RecordService
    store: SqliteStore
    robot: Any
    cameras: dict[str, Any]
    boards: Stm32Registry
    vision: CirclePipeline
    executor: ActionExecutor
    recipes: RecipeService
    task: TaskService
    stop: StopCoordinator
    host_proxy: Any
    device_config: DeviceConfigStore
    device_state: DeviceStateRegistry
    camera_registry: CameraRegistry
    motion_config: MotionConfigStore
    motion: MotionRunner
    # 命名视觉检测（「视觉调试」页另存，供配方 vision.run 引用）
    vision_detections: Any = None
    # 操作台「杆塔宽度 / 螺栓直径」计算配置（DEC-148）
    operation_config: Any = None
    # 示教点登记（DEC-067；仅 production 装配，离线为 None）
    teach_points: Any = None
    # 装配期不可用的设备（**不致命**: 界面照常起, 该设备标为不可用）
    startup_issues: Any = None
    # 装配期执行方案的问题说明（**不致命**: DEC-044，坏配方跳过/旧版自动换新）
    startup_notices: Any = None
    outputs: Any = None
    # 全局日志设施（文本日志文件 + 事件桥，DEC-029）
    logging: Any = None
    # 运行目录（DEC-097：夹爪调试页把连接/序列存在 <runtime>/gripper_debug.yaml）
    runtime_dir: str = ""
    # DEC-081：设备配置保存后按当前配置重建相机会话（仅生产栈提供）。
    # ``camera_reload()`` 返回给人看的摘要；``camera_reload_blocker()`` 返回
    # 不能改的原因（任务/运动进行中），None=可改。
    camera_reload: Any = None
    camera_reload_blocker: Any = None
    _closed: bool = field(default=False)

    def close(self) -> None:
        """关闭顺序: 停止 -> 设备/宿主 -> 记录 -> 存储 (从简, 离线无真实句柄)."""
        if self._closed:
            return
        for cam in self.cameras.values():
            try:
                cam.close(None)      # FakeCamera 与 S100CameraService 都接受 None
            except Exception:  # noqa: BLE001
                pass
        try:
            self.robot.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.host_proxy.close()
        except Exception:  # noqa: BLE001
            pass
        self.store.close()
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


def _recipes_scan_dir(recipe_path: str) -> str:
    """要自动发现的配方目录：`--recipe` 文件所在目录；没给时 `configs/recipes`。"""
    if recipe_path:
        return os.path.dirname(os.path.abspath(recipe_path))
    from application.bootstrap import app_paths
    return app_paths.recipes_dir()


def build_offline_stack(
    *,
    db_path: str,
    runtime_dir: str,
    recipe_path: str,
    circles: list[tuple[float, float, float]] | None = None,
    decision: CircleDecisionConfig | None = None,
    stop_verify_unknown_for: set[str] | None = None,
    camera_low_depth: bool = False,
    capture_timeout: bool = False,
    host_fail: bool = False,
    camera_closed: bool = False,
    device_config_path: str | None = None,
    motion_config_path: str | None = None,
    guidance_config_path: str | None = None,
    vision_detections_path: str | None = None,
    operation_config_path: str | None = None,
) -> AppStack:
    """装配离线栈. 不打开真实串口/SDK/相机; 不自动执行任何运动."""
    os.makedirs(runtime_dir, exist_ok=True)

    # 资源/互锁/时效
    resources = ResourceManager()
    state_store = StateStore()
    actions = default_actions()
    interlocks = InterlockService(resources, state_store, actions)

    # 记录/存储
    store = SqliteStore(db_path)
    store.open()
    records = RecordService(store, runtime_dir=runtime_dir)
    # 全局日志（DEC-029）：文本日志文件 + 日志→事件表桥接；领域事件另镜像一行到文件。
    logging_facility = logging_setup.wire_records(records, runtime_dir)
    log = logging_setup.get_logger("bootstrap.composition")
    log.info("装配离线栈 profile=offline runtime=%s db=%s recipe=%s",
             runtime_dir, os.path.basename(db_path), os.path.basename(recipe_path))

    # 设备配置 (E21): 默认落在运行目录, 不污染仓库 configs/; 显式传入时以传入为准。
    device_config = DeviceConfigStore(
        device_config_path or os.path.join(runtime_dir, "devices.yaml"))
    device_config.load()

    # 设备 (Fake)
    robot = FakeRobot(FakeRobotConfig(deg_per_tick=30.0, stable_window_ticks=1,
                                      stop_confirm_ticks=1))
    robot.connect()
    state_store.set_max_age("eco65_01", 10000.0)
    state_store.update(TelemetryRecord(
        device_id="eco65_01", values={"state": "READY", "joints": robot.read_joint()},
        validity=TelemetryFreshness.VALID,
    ))
    ds86 = FakeCamera(FakeCameraConfig(), pose_provider=lambda: robot.read_pose())
    ds86.open("ds86_01")
    ds86.faults.low_valid_depth = camera_low_depth
    ds86.faults.timeout = capture_timeout
    if camera_closed:
        ds86.close("ds86_01")  # 模拟相机掉线
    cameras = {"ds86_01": ds86}

    # S100 D405 (E09): 离线走**进程内 Fake V2 服务**（不碰网络）。
    # 真实 ZMQ 传输 (ZmqReqTransport) 默认**不注册** —— 真实设备连接受 D1 授权约束。
    camera_registry = CameraRegistry(cameras)
    s100_channels = {c.camera_id: c.channel_id
                     for c in device_config.config.cameras
                     if c.protocol == DeviceProtocol.ZMQ.value
                     and (c.channel_id or "").strip()}
    if s100_channels:
        fake_s100 = FakeS100Service(channels=sorted(set(s100_channels.values())))
        session = SingleReqSession(lambda: S100InProcessTransport(fake_s100.handle))
        s100_service = S100CameraService(session, s100_channels,
                                         endpoint="in-process(fake S100 V2)")
        # DEC-043: D405-2 / D405-4 倒装，采集帧自动水平+垂直双镜像；
        # 只有这两台的注册项包装饰器，其它通道保持原适配器对象（行为零变化）
        for cam_id in s100_channels:
            adapter = (wrap_capture_mirror(s100_service)
                       if cam_id in MIRRORED_CAMERA_IDS else s100_service)
            cameras[cam_id] = adapter
            camera_registry.register(cam_id, adapter)
        session_note = ""
    else:
        session_note = ("本次未装配 S100 服务：设备配置里没有 channel_id 非空的 zmq 通道，"
                        "所以 zmq 通道都没有会话")

    # 视觉: 通过 IPC 代理使用 stub 宿主 (离线不加载 cp38)
    if host_fail:
        class _FailingBackend:
            host_version = "stub-failing"

            def detect(self, images, rects, params):
                raise RuntimeError("注入: 圆宿主失败")
        backend = _FailingBackend()
    else:
        backend = StubCircleBackend(circles if circles is not None
                                    else [(320.0, 240.0, 40.0)])
    host_server = Circle38Server(backend)
    host_proxy = Circle38Proxy(InProcessTransport(host_server))
    spec = CirclePipelineSpec(
        pipeline_id="approved_ds86_circle_pipeline", version="offline-v1",
        params=CircleParams(), decision=decision or CircleDecisionConfig(
            radius_min_px=20, radius_max_px=60),
    )
    vision = CirclePipeline(spec, host_proxy)

    # 板卡 (离线回环)
    boards_registry, link = _make_offline_board()

    # 执行链
    executor = ActionExecutor(interlocks=interlocks, records=records, robot=robot,
                              cameras=cameras, boards=boards_registry, vision=vision,
                              host_proxy=host_proxy)   # DEC-048: 视觉步骤现算框/找圆
    executor.state_store = state_store  # 供 wait.state 节点使用
    executor.register_tickable(lambda: robot.tick())
    executor.register_tickable(lambda: boards_registry.dispatch())

    # 停止协调
    stop = StopCoordinator()
    unknown = stop_verify_unknown_for or set()

    def _robot_verify() -> StopState:
        robot.tick()  # 推进一次以确认停止
        if "eco65_01" in unknown:
            return StopState.UNKNOWN
        return StopState.CONFIRMED if robot.state.name in ("READY", "DISCONNECTED") \
            else StopState.UNKNOWN

    stop.register(StopTarget(
        executor_id="eco65_01",
        request_stop=robot.request_stop,
        verify_stopped=_robot_verify,
    ))
    stop.register(StopTarget(
        executor_id="ds86_01",
        request_stop=lambda: None,
        verify_stopped=lambda: (StopState.UNKNOWN if "ds86_01" in unknown
                                else StopState.CONFIRMED),
    ))

    # 配方服务
    subflows = default_subflows()
    # 运动方案 (「运动方案」页): 离线**不接假机械臂**（NullMotionArm，任何运动都被拒），
    # 但方案数据可读可编辑；plan_id 供配方步骤按步骤绑定（`motion_plan_ref`）。
    motion_config = MotionConfigStore(
        motion_config_path or os.path.join(runtime_dir, "motion.yaml"))
    motion_config.load()
    # 命名视觉检测 (「视觉调试」页「另存为检测」)：detection_id -> pipeline_ref，
    # 供配方 vision.run 步骤引用（`detection_ref`）。
    vision_detections = VisionDetectionStore(
        vision_detections_path or os.path.join(runtime_dir, "detections.yaml"))
    operation_config = OperationConfigStore(
        operation_config_path or os.path.join(runtime_dir, "manual_ops.yaml"))
    operation_config.load()
    executor.operation_config = operation_config   # DEC-155：按螺栓直径选路点
    vision_detections.load()
    # 供 vision.run 步骤解析检测里保存的「运行一次方案」；离线档位下智能分割不可用时
    # 用占位后端跑通流程（结果里如实标注「非真实模型」）。
    executor.vision_detections = vision_detections
    executor.offline = True
    methods_reg = default_methods()
    # 相机引用以**设备配置里已启用的相机**为准（DEC-034）：离线就算会话没建起来
    # （如 DS86 未连接），配方也应可编辑/校验引用；运行时再如实报未连接。
    configured_cams = {c.camera_id for c in device_config.config.cameras if c.enabled}
    catalog = ReferenceCatalog(
        joint_targets=dict(_OFFLINE_JOINT_TARGETS),
        pipelines=set(OFFLINE_PIPELINES),
        actions=actions,
        cameras=configured_cams | set(cameras),
        devices={"stm32_01", "eco65_01", "ds86_01"},
        subflows=subflows,
        motion_plans={p.plan_id for p in motion_config.config.plans},
        detections={d.detection_id: d.pipeline_ref
                    for d in vision_detections.detections},
        methods=methods_reg,
    )
    recipes = RecipeService(catalog)
    # 配方目录自动发现（DEC-035）：「另存为」到配方目录的配方**下次启动直接出现在下拉里**。
    # 先扫目录、再载入 `--recipe` 指定文件 —— 同一 recipe_id 以 `--recipe` 为准；
    # 目录里的坏文件只跳过并记原因，不拖垮启动。
    load_recipes_dir(recipes, _recipes_scan_dir(recipe_path),
                     skip_paths=(recipe_path,))
    # DEC-044：`--recipe` 指名文件坏了**也不致命**——跳过并记通知，界面照常打开；
    # 若是 configs 目录里的旧版且包内有同相对路径的新版副本，自动备份旧文件并换新。
    startup_notices: list[str] = []
    notice = load_startup_recipe(recipes, recipe_path,
                                 log=logging_setup.get_logger(
                                     "bootstrap.composition").warning)
    if notice is not None:
        startup_notices.append(notice.message)

    # 引导定位：离线也能跑通（FakeCamera 抓图 + 进程内 stub 圆宿主），只是不产生真实动作
    guidance_config = GuidanceConfigStore(
        guidance_config_path or os.path.join(runtime_dir, "guidance.yaml"))
    guidance_config.load()
    catalog.fine_offset_profiles = {
        p.profile_id for p in guidance_config.config.fine_profiles}
    guidance = GuidanceService(guidance_config, camera=cameras.get("ds86_01"),
                               host_proxy=host_proxy,
                               log=logging_setup.get_logger("guidance").info)
    # motion_config 已在上方(配方服务前)载入；离线用 NullMotionArm，任何运动都被拒。
    # **先于 TaskService 建好**：绑定「运动方案」的配方步骤在编译时要接上它
    # （离线照样拒运动 —— 与「运动方案」页手动运行同一闸）。
    motion = MotionRunner(NullMotionArm(motion_config.config.profile), motion_config,
                          log=logging_setup.get_logger("motion").info, guidance=guidance)
    # 「停止全部」也要能停**运动方案/引导定位**：运动线程不接进停止协调器时，
    # 全局停止只停任务与采集。停稳核实：没有进行中的作业 = 无需核实（CONFIRMED）；
    # 有作业时已下发 arm.stop() 并让线程退出，但机械臂**停稳**在现场证据之前
    # 一律 UNKNOWN（不把「发了停止」当「已停稳」）。
    motion_active = {"v": False}

    def _motion_stop() -> None:
        motion_active["v"] = bool(motion.busy)
        motion.stop()

    def _motion_verify() -> StopState:
        return StopState.UNKNOWN if motion_active["v"] else StopState.CONFIRMED

    stop.register(StopTarget(
        executor_id="motion",
        request_stop=_motion_stop,
        verify_stopped=_motion_verify,
    ))

    task = TaskService(
        state_machine=TaskStateMachine(),
        recipe_service=recipes,
        executor=executor,
        records=records,
        stop_coordinator=stop,
        joint_targets=dict(_OFFLINE_JOINT_TARGETS),
        subflows=subflows,
        motion=motion,
        methods=methods_reg,
    )

    return AppStack(
        profile="offline", resources=resources, state_store=state_store,
        actions=actions, interlocks=interlocks, records=records, store=store,
        robot=robot, cameras=cameras, boards=boards_registry, vision=vision,
        executor=executor, recipes=recipes, task=task, stop=stop,
        host_proxy=host_proxy, device_config=device_config,
        device_state=DeviceStateRegistry(cameras=cameras, profile="offline",
                                         session_note=session_note),
        camera_registry=camera_registry,
        motion_config=motion_config, motion=motion,
        vision_detections=vision_detections,
        operation_config=operation_config,
        startup_notices=startup_notices,
        logging=logging_facility,
        runtime_dir=runtime_dir,
    )


__all__ = [
    "AppStack", "OFFLINE_ACTIONS", "OFFLINE_PIPELINES", "OfflineStack",
    "build_offline_stack",
    "default_actions",
]


# 历史名保留（既有导入路径不破）
OfflineStack = AppStack
