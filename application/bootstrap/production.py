"""生产栈装配：用**真实适配器**装出可跑真机的应用栈。

与 :func:`application.bootstrap.composition.build_offline_stack` 并列，二者**不互相回退**：
- offline：Fake 硬件，供离线开发与测试（487 用例靠它）；
- production：真实 SDK/网络，**未选中的设备不留 Fake 替身**（宁缺勿假）。

三条硬规则：
1. **DEC-085 起没有环境变量闸**：`python main.py real` 即连接全部设备并允许运动；
2. **机器人仍被 :class:`GatedRobot` 包住**：未确认示教点的 `move_joint` 直接拒绝；
3. **没有已确认示教点就没有可用的关节目标** —— 配方校验会因此拒绝运动类流程，
   不拿离线占位值凑真实运动（总纲 §1041）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ruamel.yaml import YAML

from application.adapters.ds86.real import Ds86Adapter
from application.adapters.camera_mirror import MIRRORED_CAMERA_IDS, wrap_capture_mirror
from application.adapters.realman.motion import (
    NullMotionArm,
    RealmanMotionArm,
)
from application.adapters.realman.real import RmRobotAdapter
from application.adapters.s100.legacy import build_legacy_s100_service
from application.adapters.s100.real import build_s100_service
from application.services.guidance_config import GuidanceConfigStore
from application.services.guidance_service import GuidanceService
from application.services.motion_config import MotionConfigStore
from application.services.motion_runner import MotionRunner
from application.services.vision_detections import VisionDetectionStore
from application.adapters.stm32.registry import Stm32Registry
from application.algorithms.circle.detector import CircleDecisionConfig
from application.algorithms.circle.params import CircleParams
from application.algorithms.circle.pipeline import CirclePipeline, CirclePipelineSpec
from application.bootstrap.authorization import GatedRobot
from application.bootstrap.composition import AppStack, default_actions
from application.bootstrap.startup_recipe import load_startup_recipe
from application.infrastructure import logging_setup
from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import DeviceProtocol, StopState, TaskState
from application.contracts.targets import JointTarget
from application.domain.resources import ResourceManager
from application.domain.telemetry import StateStore
from application.hosts.circle38.client import Circle38Proxy, SubprocessTransport
from application.infrastructure.storage import SqliteStore
from application.services.actions import ActionExecutor
from application.services.camera import CameraRegistry
from application.services.device_config import DeviceConfigStore, endpoint, is_placeholder
from application.services.device_state import DeviceStateRegistry
from application.services.methods import default_methods
from application.services.records.service import RecordService
from application.services.recipes.service import (
    ReferenceCatalog, RecipeService, load_recipes_dir,
)
from application.services.stop import StopCoordinator, StopTarget
from application.services.task import TaskService
from application.services.teach_points import TeachPointStore
from application.hosts.circle38.opencv_backend import OpenCVCircleBackend
from application.workflow.state import TaskStateMachine

# S100 真实协议: 现场部署的是旧协议(pickle/v1)，V2 多段编码仍是 draft（见 DEC-007/015）
SUPPORTED_S100_PROTOCOLS = ("legacy", "v2")
PRODUCTION_PIPELINE_ID = "ds86_circle_pipeline"
PRODUCTION_PIPELINE_VERSION = "production-unconfirmed"


@dataclass(frozen=True)
class StartupIssue:
    """某台设备装配时不可用。**不致命**：界面照常起，该设备标为不可用。

    依据：用户在真机调试时要能**进去看状态**（哪台没连上、为什么），
    因一台设备连不上就整个程序退出，等于把人挡在门外。授权缺失仍然致命（见同文件）。

    ``keys`` 是这条失败**影响的配置 ID**（``device_id`` / ``camera_id``），
    供界面把对应的那一行标红并显示 ``reason``；留空则只进汇总，不落到具体行。
    """

    device: str
    reason: str
    keys: tuple[str, ...] = ()


class ProductionError(RuntimeError):
    """生产栈装配失败（**仅限整栈无法建立**的情形，如授权缺失/存储不可用）。"""


class NoRobot:
    """未选机器人（或选了但装配失败）时的占位：**所有调用一律拒绝**（不留 Fake 替身）。

    ``reason`` 用于把"未选"与"选了但没连上"区分开 —— 别让连不上的人看到
    "本次未启用机器人"而以为自己没选。
    """

    def __init__(self, reason: str = "机器人未装配") -> None:
        self._reason = reason

    def connect(self) -> None:
        raise ProductionError(self._reason)

    disconnect = close = connect
    request_stop = connect

    def read_state(self) -> dict:
        return {"connected": False, "move_count": 0, "reason": self._reason}

    def read_joint(self):
        return None

    def read_pose(self):
        return None

    def move_joint(self, *a: Any, **k: Any):
        raise ProductionError(f"{self._reason}，不能执行运动")

    def set_io(self, *a: Any, **k: Any):
        raise ProductionError(f"{self._reason}，不能操作 IO")

    def poll(self, *a: Any, **k: Any):
        raise ProductionError(self._reason)


class UnavailableCircleBackend:
    """生产下圆宿主不可用（未配置或启动失败）：**拒绝**，不返回假检测结果。

    ``reason`` 为启动失败的具体原因（DEC-053）；未给时用"未配置"的默认说明。
    """

    host_version = "unavailable"

    def __init__(self, reason: str = "") -> None:
        self._reason = reason or (
            "真实圆宿主未配置（--circle-host-cmd）；视觉判定不可用。"
            "未配置时不给假结果。")

    def detect(self, images, rects, params):  # noqa: ANN001
        raise ProductionError(self._reason)


class _ProxyBackend:
    """把 Circle38Proxy 适配成 pipeline 需要的 backend 形态。"""

    def __init__(self, proxy: Circle38Proxy, host_version: str = "cp38-native") -> None:
        self._proxy = proxy
        self._host_version = host_version

    @property
    def host_version(self) -> str:
        return self._host_version

    def detect(self, images, rects, params):  # noqa: ANN001
        return self._proxy.detect(images, rects, params)


class _ArmHandle:
    """给 RmRobotAdapter 看的句柄外形（只要 id 字段）。"""

    def __init__(self, ident: int) -> None:
        self.id = ident


class MotionArmSdkFacade:
    """把 :class:`RealmanMotionArm` 适配成 RmRobotAdapter 期望的 SDK 外形。

    目的：**流程执行与运动方案共用同一个 SDK 句柄** —— 不对同一台控制器开两条连接。
    """

    def __init__(self, arm: RealmanMotionArm) -> None:
        self._arm = arm

    def create_robot_arm(self, ip: str, port: int, level: int) -> _ArmHandle:
        return _ArmHandle(1 if self._arm.connected else -1)

    def delete_robot_arm(self) -> int:
        self._arm.disconnect()
        return 0

    def get_current_arm_state(self):
        if not self._arm.connected:
            return (-1, {})
        return (0, {"joint": self._arm.get_current_joint(),
                    "pose": self._arm.get_current_pose()})

    def get_robot_info(self):
        return (0, self._arm.get_arm_info())

    def movej(self, joint, v, r, connect, block):
        return self._arm.movej(joint, v, r, connect, block)

    def set_arm_stop(self):
        return self._arm.stop()

    def set_arm_pause(self):
        return self._arm.pause()

    def set_arm_continue(self):
        return self._arm.continue_()

    def set_arm_delete_trajectory(self):
        return self._arm.delete_trajectory()

    def set_do_state(self, io_num, state):
        return self._arm.set_do(io_num, state)

    def get_io_input(self):
        return (0, self._arm.get_di())


@dataclass
class ProductionRequest:
    """生产栈装配参数。"""

    recipe_path: str = ""
    db_path: str = ""
    runtime_dir: str = ""
    device_config_path: str | None = None
    motion_config_path: str | None = None
    guidance_config_path: str | None = None
    vision_detections_path: str | None = None
    client_id: str = "windows-main"
    circle_host_cmd: list[str] | None = None
    confirmed_targets: dict[str, JointTarget] = field(default_factory=dict)
    # 已确认示教点文件（DEC-067）：界面「运动方案 → 示教点」登记写回这里，
    # real 启动也从这里载入；空 = 不接示教点登记（界面按钮禁用）。
    targets_path: str = ""
    # S100 真实协议: "legacy"=现场旧协议(pickle/v1，默认) | "v2"=E05 多段编码(仍 draft)
    s100_protocol: str = "legacy"
    # 圆检测后端（DEC-073）：auto=有宿主命令用宿主、否则进程内 OpenCV（Linux 单程序）；
    # host=必须用宿主（没有/起不来则不可用，不回退）；opencv=强制进程内 OpenCV 后端
    circle_backend: str = "auto"
    sdk_overrides: dict[str, Any] = field(default_factory=dict)   # 测试注入 mock SDK

    def __post_init__(self) -> None:
        if self.s100_protocol not in SUPPORTED_S100_PROTOCOLS:
            raise ProductionError(
                f"不支持的 S100 协议: {self.s100_protocol!r}；"
                f"可选 {SUPPORTED_S100_PROTOCOLS}")
        if (self.circle_backend or "").strip().lower() not in (
                "auto", "host", "opencv"):
            raise ProductionError(
                f"不支持的圆检测后端: {self.circle_backend!r}；可选 auto/host/opencv")


def load_confirmed_targets(path: str) -> dict[str, JointTarget]:
    """读取**已确认的示教点**文件；文件不存在或条目标记未确认 -> 不返回该项。

    文件格式（键=示教点引用名，值=6 个关节角 deg + 示教来源）::

        targets:
          - target_id: teach_01          # 也可写 station_id（旧键名，兼容保留）
            joint_deg: [ ... 现场示教值 ... ]
            waypoint_ref: taught_2026xxxx
            confirmed: true

    无工位后，这里的键就是**配方运动步骤 ``joint_target_ref`` 引用的示教点键**。
    """
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = YAML(typ="safe").load(fh) or {}
    out: dict[str, JointTarget] = {}
    for item in (data.get("targets") or []):
        if not item.get("confirmed", False):
            continue
        sid = str(item.get("target_id") or item.get("station_id") or "").strip()
        ref = str(item.get("waypoint_ref") or "").strip()
        if not sid or not ref:
            continue
        out[sid] = JointTarget(joint_deg=tuple(item["joint_deg"]),
                               waypoint_ref=ref)
    return out


def ds86_capture_params(raw: dict | None, *, align_mode: str | None = None) -> dict:
    """把 guidance.yaml 里 `ds86_*` 前缀的参数转成采集封装要的键名。

    旧工程 camera_config.json 用 `ds86_tof_resolution` 这类前缀；采集封装用无前缀名。
    `align_mode` 若给了设备配置里的 capture_profile 则优先（否则用配置里的）。
    """
    out: dict = {}
    for key, value in (raw or {}).items():
        out[key[5:] if key.startswith("ds86_") else key] = value
    if align_mode:
        out["align_mode"] = align_mode
    return out


def build_production_stack(req: ProductionRequest) -> AppStack:
    """装配生产栈。**未授权直接拒绝**；未选设备不留 Fake 替身。"""
    os.makedirs(req.runtime_dir, exist_ok=True)

    device_config = DeviceConfigStore(
        req.device_config_path or os.path.join(req.runtime_dir, "devices.yaml"))
    device_config.load()

    resources = ResourceManager()
    state_store = StateStore()
    actions = default_actions()
    interlocks = InterlockServiceCompat(resources, state_store, actions)

    store = SqliteStore(req.db_path)
    store.open()
    records = RecordService(store, runtime_dir=req.runtime_dir)
    # 全局日志（DEC-029）：文本日志文件 + 日志→事件表桥接（真实设备模式同样落盘）。
    logging_facility = logging_setup.wire_records(records, req.runtime_dir)
    log = logging_setup.get_logger("bootstrap.production")
    log.info("装配生产栈 profile=production devices=全部 runtime=%s", req.runtime_dir)

    # ---------- 运动方案 / 引导定位配置（相机与机器人都要用，故先建） ----------
    motion_config = MotionConfigStore(
        req.motion_config_path or os.path.join(req.runtime_dir, "motion.yaml"))
    motion_config.load()
    profile = motion_config.config.profile
    # 命名视觉检测（「视觉调试」页另存，供配方 vision.run 引用）
    vision_detections = VisionDetectionStore(
        req.vision_detections_path
        or os.path.join(req.runtime_dir, "detections.yaml"))
    vision_detections.load()
    # 引导定位配置：DS86 采集参数与视觉算法参数同源（旧工程 camera_config.json 的 DS86 引导页）
    guidance_store = GuidanceConfigStore(
        req.guidance_config_path or os.path.join(req.runtime_dir, "guidance.yaml"))
    guidance_store.load()

    # ---------- 设备装配（**单台失败不致命**：记录原因、标为不可用、继续装其余） ----------
    startup_issues: list[StartupIssue] = []

    def _attempt(name: str, build: Any, keys: tuple[str, ...] = ()) -> Any:
        try:
            return build()
        except Exception as exc:  # noqa: BLE001 - 单台失败不拖垮整栈
            reason = f"{type(exc).__name__}: {exc}"
            startup_issues.append(StartupIssue(name, reason, keys=keys))
            print(f"[real] {name} 不可用: {reason}"
                  f"（不致命：界面照常启动，设备页会标红）")
            return None

    # ---------- 相机（按当前设备配置装配；保存配置后可热重载，DEC-081/083） ----------
    camera_registry = CameraRegistry()

    def _build_ds86_adapter() -> Any:
        cam_cfg = device_config.find_camera("ds86_01")
        adapter = Ds86Adapter(
            "ds86_01", sdk=req.sdk_overrides.get("ds86"),
            serial=(cam_cfg.serial if cam_cfg else None) or None,
            # DEC-081：设备配置里的相机 IP（可选）；填了启动就按它打开并核对
            device_ip=(cam_cfg.host if cam_cfg else None) or None,
            calibration_version=(cam_cfg.calibration_id if cam_cfg else None) or None,
            capture_params=ds86_capture_params(
                guidance_store.config.ds86,
                align_mode=(cam_cfg.capture_profile if cam_cfg else "") or None))
        adapter.open("ds86_01")
        st = adapter.status()
        log.info("DS86 已打开: SN=%s IP=%s",
                 st.get("serial") or "(未配置)",
                 st.get("ip") or "(未配置，按 SN 打开)")
        return adapter

    def _build_s100_adapters() -> tuple[dict[str, Any], list[StartupIssue]]:
        """按**端点分组**建 S100 客户端：不同相机行可以指向不同的 S100 服务（DEC-083）。

        原先要求所有 zmq 通道配同一个端点，改一行动手就会整组报错；现在每个端点
        一个客户端，各通道互不拖累。某个端点建不起来只影响它自己的通道。
        """
        cams: dict[str, Any] = {}
        issues: list[StartupIssue] = []
        try:
            groups, invalid = _s100_groups(device_config)
        except Exception as exc:  # noqa: BLE001 - 配置异常不拖垮整栈
            issues.append(StartupIssue(
                "S100 相机服务", f"{type(exc).__name__}: {exc}",
                keys=tuple(c.camera_id for c in device_config.config.cameras
                           if c.protocol == DeviceProtocol.ZMQ.value)))
            return cams, issues
        if invalid:
            issues.append(StartupIssue(
                "S100 相机通道", "S100 端点未配置/未确认: " + "、".join(invalid),
                keys=tuple(invalid)))
        if not groups:
            issues.append(StartupIssue(
                "S100 相机服务", "设备配置里没有可用的 zmq 通道（缺 channel_id 或端点未配置）",
                keys=tuple(c.camera_id for c in device_config.config.cameras
                           if c.protocol == DeviceProtocol.ZMQ.value)))
            return cams, issues
        for endpoint, chans in groups.items():
            try:
                if req.s100_protocol == "v2":
                    # E05 V2 多段编码：**draft**，现场服务尚未部署 -> 只能显式选择
                    service = build_s100_service(endpoint, chans,
                                                 client_id=req.client_id)
                else:
                    # 默认：现场**已部署**的旧协议（pickle/v1）—— 照旧客户端接法（DEC-015）
                    service = build_legacy_s100_service(endpoint, chans,
                                                        client_id=req.client_id)
            except Exception as exc:  # noqa: BLE001 - 单端点失败不拖累其它端点
                reason = f"{type(exc).__name__}: {exc}"
                issues.append(StartupIssue(f"S100 相机服务（{endpoint}）", reason,
                                           keys=tuple(chans)))
                print(f"[real] S100 相机服务（{endpoint}）不可用: {reason}"
                      f"（不致命：界面照常启动，设备页会标红）")
                continue
            for cam_id in chans:
                # DEC-043: D405-2 / D405-4 倒装，采集帧自动水平+垂直双镜像；
                # 只有这两台的注册项包装饰器（legacy 与 V2 共用），其它通道保持原适配器
                cams[cam_id] = (wrap_capture_mirror(service)
                                if cam_id in MIRRORED_CAMERA_IDS else service)
        return cams, issues

    def _build_camera_adapters() -> tuple[dict[str, Any], list[StartupIssue]]:
        """按**此刻的**设备配置重建全部相机会话（初次装配与热重载共用）。"""
        cams: dict[str, Any] = {}
        issues: list[StartupIssue] = []

        def _camera_failed(name: str, exc: Exception, keys: tuple[str, ...]) -> None:
            reason = f"{type(exc).__name__}: {exc}"
            issues.append(StartupIssue(name, reason, keys=keys))
            print(f"[real] {name} 不可用: {reason}"
                  f"（不致命：界面照常启动，设备页会标红）")

        try:
            cams["ds86_01"] = _build_ds86_adapter()
        except Exception as exc:  # noqa: BLE001 - 单台失败不拖垮整栈
            _camera_failed("DS86 相机", exc, ("ds86_01",))

        s100_cams, s100_issues = _build_s100_adapters()
        cams.update(s100_cams)
        issues.extend(s100_issues)
        return cams, issues

    cameras, camera_issues = _build_camera_adapters()
    startup_issues.extend(camera_issues)
    for _cid, _adapter in cameras.items():
        camera_registry.register(_cid, _adapter)
    s100_session_note = ""

    # ---------- 机器人（**单一 SDK 句柄**：流程与运动方案共用，不开两条连接） ----------
    robot: Any
    motion_arm: Any

    def _build_robot() -> Any:
        dev = device_config.find_device("eco65_01")
        host = (dev.host if dev else "") or ""
        port = int((dev.port if dev and dev.port else 8080))
        if is_placeholder(host):
            raise ProductionError(f"机器人地址未配置/未确认: {host!r}（见设备配置页）")
        arm = RealmanMotionArm(profile, sdk=req.sdk_overrides.get("arm"),
                               ip=host, port=port)
        if not arm.connect():
            raise ProductionError(f"机器人连接失败: {host}:{port}")
        # 流程执行器仍走 RmRobotAdapter（句柄/到位判据齐全），底层共用同一个 arm
        inner = RmRobotAdapter(sdk=MotionArmSdkFacade(arm), ip=host, port=port)
        inner.connect()
        return GatedRobot(inner), GatedRobot(arm)

    built_robot = _attempt("ECO65 机器人", _build_robot, keys=("eco65_01",))
    if built_robot is not None:
        robot, motion_arm = built_robot
    else:
        # 机器人不可用 -> 用 NoRobot/NullMotionArm：任何运动都被明确拒绝（不留假替身）
        issue = next((i for i in startup_issues if "eco65_01" in i.keys), None)
        robot = NoRobot(f"ECO65 机器人装配失败：{issue.reason}" if issue
                        else "本次未启用机器人")
        motion_arm = NullMotionArm(profile)

    # ---------- 圆检测后端（DEC-053/072/073） ----------
    # 三种来源：
    # 1) 外部宿主子进程（Windows cp38 原生 3.8；stdin/stdout 管道，装配即拉起）；
    # 2) **进程内 OpenCV 自研后端**（opencv-linux）——纯 Python + cv2，可直接跑在
    #    主程序 3.12 进程里；Linux 单程序发布不需要再带 circle38_host；
    # 3) 都不可用 -> UnavailableCircleBackend（如实拒绝，不返回假结果）。
    # 选择规则（req.circle_backend）：
    #   auto   = 有宿主命令用宿主（拉起失败→不可用，**不静默换算法**）；
    #            没有宿主命令→进程内 OpenCV；
    #   host   = 只认宿主（没有/失败=不可用）；
    #   opencv = 强制进程内 OpenCV（即使配了宿主命令也不起宿主）。
    startup_notices: list[str] = []          # 启动提示（不致命，界面/控制台可见）
    circle_kind = (req.circle_backend or "auto").strip().lower()
    host_proxy: Any = None
    backend: Any = None
    host_label = "unavailable"
    use_host = circle_kind in ("auto", "host") and bool(req.circle_host_cmd)
    if circle_kind == "host" and not req.circle_host_cmd:
        host_label = "unavailable"
        backend = UnavailableCircleBackend()
    if use_host:
        transport = SubprocessTransport(list(req.circle_host_cmd))
        try:
            transport.start()
        except Exception as exc:  # noqa: BLE001 - 宿主拉起失败不拖垮整栈
            reason = (f"圆宿主启动失败: {' '.join(req.circle_host_cmd)} -> {exc}；"
                      "视觉判定与找圆不可用（不给假结果）")
            log.error(reason)
            startup_notices.append(reason)
            transport.close()
            host_proxy = None
            backend = UnavailableCircleBackend(reason)
        else:
            log.info("圆宿主已启动: %s", " ".join(req.circle_host_cmd))
            host_proxy = Circle38Proxy(transport)
            backend = _ProxyBackend(host_proxy)
            host_label = "cp38-native"
    if backend is None and (circle_kind == "opencv"
                            or (circle_kind == "auto" and not use_host)):
        try:
            inproc = OpenCVCircleBackend()
            inproc.load()
        except Exception as exc:  # noqa: BLE001 - 缺 cv2 等: 如实不可用
            reason = (f"OpenCV 圆检测后端不可用: {exc}；"
                      "视觉判定与找圆不可用（不给假结果）")
            log.error(reason)
            startup_notices.append(reason)
            backend = UnavailableCircleBackend(reason)
        else:
            # 注意：沿用 host_proxy 这个参数名（GuidanceService / vision_layout 都按
            # “有 detect(images, rects, params) 的对象”使用）；进程内后端接口相同。
            host_proxy = inproc
            backend = inproc
            host_label = "opencv-linux"
            log.info("圆检测后端: 进程内 OpenCV（opencv-linux；非 cp38 原生，结果不保证逐位一致）")
    if backend is None:                       # host 模式但没有命令（上面已置 Unavailable）
        backend = UnavailableCircleBackend()

    spec = CirclePipelineSpec(
        pipeline_id=PRODUCTION_PIPELINE_ID, version=PRODUCTION_PIPELINE_VERSION,
        params=CircleParams(), decision=CircleDecisionConfig(),
        host=host_label)
    vision = CirclePipeline(spec, backend)

    # ---------- 板卡：本次不启用的保持"无链路"，动作会明确失败 ----------
    boards = Stm32Registry()

    # ---------- 执行链 ----------
    executor = ActionExecutor(interlocks=interlocks, records=records, robot=robot,
                              cameras=cameras, boards=boards, vision=vision,
                              vision_detections=vision_detections, offline=False,
                              host_proxy=host_proxy)
    executor.state_store = state_store
    if hasattr(robot, "tick"):
        executor.register_tickable(robot.tick)

    stop = StopCoordinator()
    stop.register(StopTarget(
        executor_id="eco65_01",
        request_stop=robot.request_stop,
        # 真实停止核实需 A25 现场证据；在此之前一律 UNKNOWN，不把"发了停止"当"已停稳"
        verify_stopped=lambda: StopState.UNKNOWN,
    ))

    joint_targets = dict(req.confirmed_targets)
    # 示教点登记（DEC-067）：「运动方案」页读取当前关节后写回同一文件；这里建 store
    # 后它与 catalog / TaskService **共用同一个 dict**，登记/删除即时生效（无需重启）。
    teach_points = None
    if req.targets_path:
        teach_points = TeachPointStore(req.targets_path,
                                       confirmed_targets=joint_targets)
    methods_reg = default_methods()
    # 配方/编辑器里的相机引用以**设备配置里已启用的相机**为准（DEC-034）：会话可能因
    # 连不上而缺席（如 DS86 没扫到在线设备），但配方仍应可编辑/保存；运行时该步骤
    # 会如实报「未连接」，不拿别的相机帧凑数。
    configured_cams = {c.camera_id for c in device_config.config.cameras if c.enabled}
    catalog = ReferenceCatalog(
        joint_targets=joint_targets,
        pipelines={PRODUCTION_PIPELINE_ID},
        actions=actions,
        cameras=configured_cams | set(cameras),
        devices={"eco65_01"} | set(cameras),
        subflows=None,
        motion_plans={p.plan_id for p in motion_config.config.plans},
        fine_offset_profiles={p.profile_id for p in guidance_store.config.fine_profiles},
        detections={d.detection_id: d.pipeline_ref
                    for d in vision_detections.detections},
        methods=methods_reg,
    )
    recipes = RecipeService(catalog)
    # 配方目录自动发现（DEC-035）：「另存为」到配方目录的配方**下次启动直接出现在下拉里**。
    # 先扫目录、再载入 `--recipe` 指定文件 —— 同一 recipe_id 以 `--recipe` 为准；
    # 目录里的坏文件只跳过并记原因，不拖垮启动。
    load_recipes_dir(recipes, _recipes_scan_dir(req.recipe_path),
                     skip_paths=(req.recipe_path or "",))
    # DEC-044：`--recipe` 指名文件坏了**也不致命**——跳过并记通知，界面照常打开；
    # 若是 configs 目录里的旧版且包内有同相对路径的新版副本，自动备份旧文件并换新。
    notice = load_startup_recipe(recipes, req.recipe_path,
                                 log=logging_setup.get_logger(
                                     "bootstrap.production").warning)
    if notice is not None:
        startup_notices.append(notice.message)

    # 引导定位：**本程序内闭环**（DS86 抓图 → 深度引导管线 → cp38 宿主找圆 → 偏差）
    guidance = GuidanceService(guidance_store, camera=cameras.get("ds86_01"),
                               host_proxy=host_proxy,
                               log=logging_setup.get_logger("guidance").info)
    # 运动方案执行器：**先于 TaskService 建好** —— 绑定「运动方案」的配方步骤编译时要接上它。
    motion = MotionRunner(motion_arm, motion_config,
                          log=logging_setup.get_logger("motion").info, guidance=guidance)
    # 「停止全部」也要能停**运动方案/引导定位**：运动线程不接进停止协调器时，
    # 全局停止只停任务与机构。停稳核实：没有进行中的作业 = 无需核实（CONFIRMED）；
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
        state_machine=TaskStateMachine(), recipe_service=recipes, executor=executor,
        records=records, stop_coordinator=stop, joint_targets=joint_targets,
        subflows=None, motion=motion, methods=methods_reg)

    if startup_issues:
        log.warning("有 %d 台设备装配不可用（不致命，界面标红）: %s", len(startup_issues),
                    "; ".join(f"{i.device}:{i.reason}" for i in startup_issues))
    log.info("生产栈装配完成: 相机通道=%s 机器人=%s",
             ", ".join(sorted(camera_registry.camera_ids())) or "(无)",
             type(robot).__name__)

    stack = AppStack(
        profile="production", resources=resources, state_store=state_store,
        actions=actions, interlocks=interlocks, records=records, store=store,
        robot=robot, cameras=cameras, boards=boards, vision=vision, executor=executor,
        recipes=recipes, task=task, stop=stop, host_proxy=host_proxy,
        device_config=device_config,
        device_state=DeviceStateRegistry(cameras=cameras,
                                         startup_issues=startup_issues,
                                         profile="production",
                                         session_note=s100_session_note),
        camera_registry=camera_registry,
        motion_config=motion_config, motion=motion,
        vision_detections=vision_detections,
        teach_points=teach_points,
        startup_issues=startup_issues,
        startup_notices=startup_notices,
        logging=logging_facility,
        runtime_dir=req.runtime_dir,
    )

    # ---------- 相机配置热重载（DEC-081） ----------
    # 设备配置页保存相机连接参数后，不必重启程序：按新配置重建相机会话，并把
    # 执行器/设备状态/引导定位看到的那份（同一个）cameras 字典就地换掉。
    # 任务或运动正在执行时拒绝重建（不能把正在用的会话从脚下抽走）。
    _ACTIVE_TASK_STATES = {
        TaskState.VALIDATING, TaskState.READY, TaskState.RUNNING,
        TaskState.PAUSE_REQUESTED, TaskState.PAUSED, TaskState.WAITING_OPERATOR,
        TaskState.STOPPING, TaskState.FAULT,
    }

    def _camera_reload_blocker() -> str | None:
        state = stack.task.state_machine.state
        if state in _ACTIVE_TASK_STATES:
            return (f"任务状态为 {state.value}；请先停止/完成当前作业"
                    "（FAULT 请先完成故障恢复）再修改相机配置")
        if getattr(motion, "busy", False):
            return "运动方案正在执行；请先停止再修改相机配置"
        return None

    def _ds86_config_signature() -> Any:
        cam = device_config.find_camera("ds86_01")
        if cam is None:
            return None
        return (bool(cam.enabled), (cam.serial or "").strip(),
                (cam.host or "").strip(), (cam.capture_profile or "").strip(),
                (cam.calibration_id or "").strip())

    def _s100_config_signature() -> Any:
        rows = tuple(sorted(
            (c.camera_id, bool(c.enabled), (c.host or "").strip(), c.port,
             (c.channel_id or "").strip())
            for c in device_config.config.cameras
            if c.protocol == DeviceProtocol.ZMQ.value))
        dev = device_config.find_device("s100_01")
        fallback = ((dev.host or "").strip(), dev.port) if dev is not None else None
        return rows, fallback

    _built_signatures = {
        "ds86": _ds86_config_signature(),
        "s100": _s100_config_signature(),
    }

    def _close_camera_adapters(adapters: list[Any]) -> None:
        seen: set[int] = set()
        for adapter in adapters:
            if id(adapter) in seen:          # 多通道共用同一个 S100 服务
                continue
            seen.add(id(adapter))
            try:
                adapter.close(None)
            except Exception:  # noqa: BLE001 - 关闭旧会话失败不阻止换新
                pass

    def _reload_cameras(camera_id: str | None = None) -> str:
        """只重建**配置确实变了**（或刚保存的那一行）的相机；失败保留旧会话。

        DEC-083：保存 D405 的 IP 不再触发 DS86 的 3×3000ms 扫描与全局重建；
        任一组的重建失败只影响该组，旧适配器继续可用。``camera_id`` 是刚保存的
        配置 ID —— 即使值没变（例如想重试连接）也只重建对应组。
        """
        blocker = _camera_reload_blocker()
        if blocker:
            raise RuntimeError(blocker)

        ds86_sig = _ds86_config_signature()
        s100_sig = _s100_config_signature()
        ds86_changed = (ds86_sig != _built_signatures["ds86"]
                        or camera_id == "ds86_01")
        s100_changed = (s100_sig != _built_signatures["s100"]
                        or camera_id in set(_zmq_camera_ids())
                        or camera_id == "s100_01")

        applied: list[str] = []
        problems: list[str] = []
        ds86_problem: str | None = None

        # ---- DS86：单独重建，失败保留旧适配器 ----
        if ds86_changed:
            try:
                new_ds86 = _build_ds86_adapter()
            except Exception as exc:  # noqa: BLE001
                ds86_problem = f"{type(exc).__name__}: {exc}"
                problems.append(f"DS86 相机: {ds86_problem}")
            else:
                old = cameras.get("ds86_01")
                cameras["ds86_01"] = new_ds86
                camera_registry.register("ds86_01", new_ds86)
                if old is not None:
                    _close_camera_adapters([old])
                guidance.camera = new_ds86
                applied.append("DS86")
                _built_signatures["ds86"] = ds86_sig

        # ---- S100：按端点分组重建；任一分组失败则整组保留旧会话 ----
        s100_new_issues: list[StartupIssue] | None = None
        if s100_changed:
            s100_cams, s100_new_issues = _build_s100_adapters()
            if s100_new_issues:
                _close_camera_adapters(list(s100_cams.values()))
                problems.extend(f"{i.device}: {i.reason}" for i in s100_new_issues)
                s100_new_issues = None          # 未应用：保留旧 Issues
            else:
                old_keys = [cid for cid in list(cameras) if cid != "ds86_01"]
                old_adapters = [cameras[cid] for cid in old_keys]
                for cid in old_keys:
                    cameras.pop(cid, None)
                    camera_registry.unregister(cid)
                _close_camera_adapters(old_adapters)
                cameras.update(s100_cams)
                for cid, adapter in s100_cams.items():
                    camera_registry.register(cid, adapter)
                applied.append("S100")
                _built_signatures["s100"] = s100_sig

        if not applied and not problems:
            return "相机配置无变化，未重建会话"

        # ---- 装配失败原因：只替换已重建组的相关条目 ----
        configured = {c.camera_id for c in device_config.config.cameras if c.enabled}
        camera_keys = set(configured) | {"ds86_01"} | set(_zmq_camera_ids())
        kept = [i for i in startup_issues
                if not (set(getattr(i, "keys", ()) or ()) & camera_keys)]
        new_issues: list[StartupIssue] = list(kept)
        if ds86_changed and "DS86" in applied:
            pass                                       # 成功：清掉旧红标
        elif ds86_changed and ds86_problem:
            new_issues.append(StartupIssue(
                "DS86 相机", ds86_problem, keys=("ds86_01",)))
        else:
            new_issues.extend(i for i in startup_issues
                              if "ds86_01" in set(getattr(i, "keys", ()) or ()))
        if s100_changed and s100_new_issues is not None:
            new_issues.extend(s100_new_issues)
        else:
            new_issues.extend(i for i in startup_issues
                              if set(getattr(i, "keys", ()) or ()) & set(_zmq_camera_ids()))
        startup_issues[:] = new_issues
        stack.device_state.rebind_cameras(cameras, startup_issues)
        catalog.cameras.clear()
        catalog.cameras.update(configured | set(cameras))

        detail = "、".join(applied) or "无"
        if problems:
            msg = "；".join(problems)
            log.warning("相机配置部分未应用（旧会话保留）: %s", msg)
            return (f"已重建：{detail}；以下配置未应用，仍使用旧会话：\n{msg}")
        log.info("相机配置已应用: %s", detail)
        return f"相机配置已应用：{detail}"

    def _zmq_camera_ids(*, buildable_only: bool = False) -> list[str]:
        out: list[str] = []
        for cam in device_config.config.cameras:
            if cam.protocol != DeviceProtocol.ZMQ.value:
                continue
            if buildable_only and (not cam.enabled
                                   or not (cam.channel_id or "").strip()):
                continue
            out.append(cam.camera_id)
        return out

    stack.camera_reload = _reload_cameras
    stack.camera_reload_blocker = _camera_reload_blocker
    return stack


# ---------------------------------------------------------------- 启动探测（DEC-087）
def initial_probe(device_config: DeviceConfigStore, device_state: DeviceStateRegistry,
                  *, timeout: float = 0.8, probe_fn: Any = None
                  ) -> list[tuple[str, str, str]]:
    """启动时对**已启用**的设备/相机/链路探测一遍，初始化 未启用/已启用 状态。

    - 跳过已有会话的条目（会话打开本身就是“已启用”）；
    - 本地 SDK（DS86/随臂机器人）探测为 not_applicable，按已启用处理；
    - 单条探测异常不影响其它条目；结果只写运行期状态，不改设备配置。
    返回 ``[(key, 状态文本, 悬停详情)]``。
    """
    if probe_fn is None:
        from application.services.device_probe import probe as probe_fn  # noqa: N806
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    entries = (list(device_config.config.devices)
               + list(device_config.config.cameras)
               + list(device_config.config.links))
    for entry in entries:
        key = device_state.key_of(entry)
        if not key or key == "?" or key in seen:
            continue
        seen.add(key)
        if not getattr(entry, "enabled", False):
            continue
        if device_state.is_open(key):
            out.append((key, device_state.status_text(entry),
                        device_state.detail_of(entry)))
            continue
        try:
            try:
                result = probe_fn(entry, timeout=timeout)
            except TypeError:          # 测试注入的简化探针（无 timeout 参数）
                result = probe_fn(entry)
        except Exception:  # noqa: BLE001 - 单条探测失败不挡启动
            continue
        device_state.record_probe(key, result)
        out.append((key, device_state.status_text(entry),
                    device_state.detail_of(entry)))
    return out


# ---------------------------------------------------------------- 内部
def InterlockServiceCompat(resources, state_store, actions):  # noqa: N802
    """与离线栈一致的互锁服务（单独抽出来便于阅读）。"""
    from application.domain.interlocks import InterlockService
    return InterlockService(resources, state_store, actions)


def _s100_groups(device_config: DeviceConfigStore
                 ) -> tuple[dict[str, dict[str, str]], list[str]]:
    """zmq 相机通道按 **S100 端点分组** -> ``{tcp://host:port: {camera_id: channel}}``。

    DEC-083：相机行自己填了 IP/端口就用它（同一端点共用一个 S100 客户端），
    没填才回退 ``s100_01`` 设备行。不同通道可以指向不同的 S100 服务，互不拖累；
    端点没配置/未确认的通道单独返回，装配时按通道标红而不是拖垮其它通道。
    """
    dev = device_config.find_device("s100_01")
    fb_host, fb_port = endpoint(dev) if dev is not None else ("", None)
    groups: dict[str, dict[str, str]] = {}
    invalid: list[str] = []
    for cam in device_config.config.cameras:
        if cam.protocol != DeviceProtocol.ZMQ.value or not cam.enabled:
            continue
        channel = (cam.channel_id or "").strip()
        if not channel:
            continue                       # 缺 channel_id：由"没有通道"提示统一说明
        host, port = endpoint(cam)
        if is_placeholder(host) or not port:
            host, port = fb_host, fb_port
        try:
            port_i = int(port) if port is not None else 0
        except (TypeError, ValueError):
            port_i = 0
        if is_placeholder(host) or not (1 <= port_i <= 65535):
            invalid.append(cam.camera_id)
            continue
        groups.setdefault(f"tcp://{host}:{port_i}", {})[cam.camera_id] = channel
    return groups, invalid


def _recipes_scan_dir(recipe_path: str | None) -> str:
    """要自动发现的配方目录：`--recipe` 文件所在目录；没给时 `configs/recipes`。"""
    if recipe_path:
        return os.path.dirname(os.path.abspath(recipe_path))
    from application.bootstrap import app_paths
    return app_paths.recipes_dir()


def make_run_context(run_id: str = "production") -> RunContext:
    return RunContext(run_id=run_id,
                      versions=VersionSnapshot("production", "v0", "-", "-"))


__all__ = [
    "MotionArmSdkFacade", "NoRobot", "PRODUCTION_PIPELINE_ID",
    "PRODUCTION_PIPELINE_VERSION", "ProductionError", "ProductionRequest",
    "StartupIssue", "UnavailableCircleBackend", "initial_probe",
    "build_production_stack", "ds86_capture_params", "load_confirmed_targets",
    "make_run_context",
]
