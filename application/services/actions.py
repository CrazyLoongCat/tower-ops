"""动作/设备调用链服务 (12 §4.2, 02 §2, A13).

统一链: 预检 -> 资源 -> 持久化意图 -> 一次提交 -> 反馈核验 -> 结果存证.
source/operator/context 完整; 服务层拒绝绕过权限/保护/工位来源; SDK/算法细节不进入 UI.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from application.contracts.context import ExecutionKey, RunContext
from application.contracts.enums import (
    CommandState,
    DetectionStatus,
    ImageSpace,
    SourceKind,
    StopScope,
    StopState,
)
from application.contracts.frames import CaptureRequest, CaptureSet, FrameBundle
from application.contracts.handles import CommandHandle
from application.contracts.results import DetectionResult, MetricMeasurement
from application.contracts.targets import JointTarget
from application.domain.interlocks import InterlockService
from application.domain.resources import CAPTURE_STILLNESS, MOTION_GLOBAL
from application.domain.errors import InterlockDenied, ValidationFailure
from application.domain.actions import ActionDefinition
from application.services.vision_production import (
    VISION_MODE,
    depth_circle_debug,
    run_depth_rects_vision,
)
from application.workflow.ledger import ExecutionLedger
from application.domain.resources import CAPTURE_STILLNESS, MOTION_GLOBAL
from application.infrastructure import logging_setup

_log = logging_setup.get_logger("services.actions")


class SourceScopeError(RuntimeError):
    """来源/工位作用域不符: 迟到/换工位的请求被拒绝."""


@dataclass
class ActionExecutor:
    """把领域互锁、记录、设备适配器与账本编排为统一的执行链."""

    interlocks: InterlockService
    records: Any = None
    robot: Any = None
    cameras: dict[str, Any] = field(default_factory=dict)
    vision: Any = None
    vision_detections: Any = None      # 命名视觉检测存储（解析「运行一次方案」）
    operation_config: Any = None       # 操作台计算/全局参数存储（manual_ops.yaml，DEC-155）
    offline: bool = False              # 离线档位：智能分割不可用时用占位后端（如实标注）
    boards: Any = None
    ledger: Any = None
    host_proxy: Any = None             # cp38 圆宿主代理（视觉步骤现算框/找圆, DEC-048）
    _tickables: list[Callable[[], Any]] = field(default_factory=list)
    _leases: dict[str, Any] = field(default_factory=dict)
    # 最近一次抓拍/视觉结果 (DEC-042)：作业台右侧图卡展示与手动检测圆用。
    # 只保留本进程最近一次，读侧按 run_id 匹配，避免跨 run 显示旧图。
    last_frame: FrameBundle | None = None
    last_frames: dict[str, FrameBundle] = field(default_factory=dict)
    # DEC-051: 本 run 内**每台相机**最近一次抓拍帧（作业台多相机页签用）。
    # 与 last_frames 的区别：last_frames 只保留最近一次抓拍调用的帧（可能是别的相机），
    # 这里按 camera_id 累积；读侧仍按帧上的 run_id 过滤，不跨 run 显示旧图。
    camera_frames: dict[str, FrameBundle] = field(default_factory=dict)
    last_capture_run_id: str | None = None
    last_detection: DetectionResult | None = None
    last_detection_run_id: str | None = None
    # 视觉步骤确认后的「定位圆 + 所用帧」(DEC-048)：引导定位步骤按 run/步骤/相机
    # 匹配后沿用，不再重新抓图找圆。只保留最近一次；不写配方/检测。
    vision_guide_target: dict | None = None
    # DEC-064：本次运行中**运动到示教点完成时**记录的末端位姿（mm/deg）。
    # 键 = (run_id, 示教点引用)：步骤后判断「机械臂末端位姿」用同一 run 的实测位姿作期望值。
    pose_by_target: dict[tuple[str, str], tuple[float, float, float, float, float, float]] = \
        field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.ledger is None:
            self.ledger = ExecutionLedger()

    # ---------- 末端位姿（DEC-064） ----------
    def current_pose_mm_deg(self) -> tuple[float, ...] | None:
        """读当前末端位姿，统一成 ``(x,y,z mm, rx,ry,rz deg)``；读不到返回 None。"""
        robot = self.robot
        if robot is None:
            return None
        # 真实适配器：read_pose() -> CartesianPose(mm/deg)
        fn = getattr(robot, "read_pose", None)
        if callable(fn):
            try:
                pose = fn()
            except Exception:  # noqa: BLE001 - 读失败视为不可判断, 不造假
                pose = None
            if pose is not None:
                tr = getattr(pose, "translation_mm", None)
                ori = getattr(pose, "orientation_deg", None)
                if tr is not None and ori is not None:
                    vals = list(tr)[:3] + list(ori)[:3]
                    return tuple(float(v) for v in vals) if len(vals) == 6 else None
                if isinstance(pose, (list, tuple)) and len(pose) >= 6:
                    # 离线 FakeRobot.pose_deg（测试用，按 mm/deg 解释）
                    return tuple(float(v) for v in pose[:6])
        # 真实 MotionRunner 的臂：get_current_pose() -> [m, rad]
        fn = getattr(robot, "get_current_pose", None)
        if callable(fn):
            try:
                pose = fn()
            except Exception:  # noqa: BLE001
                pose = None
            if isinstance(pose, (list, tuple)) and len(pose) >= 6:
                vals = [float(pose[0]) * 1000.0, float(pose[1]) * 1000.0,
                        float(pose[2]) * 1000.0,
                        math.degrees(float(pose[3])), math.degrees(float(pose[4])),
                        math.degrees(float(pose[5]))]
                return tuple(vals)
        return None

    def remember_target_pose(self, target_ref: str | None,
                             run_id: str = "") -> None:
        """运动到示教点完成后记录实测位姿，供后续步骤后判断使用。"""
        ref = (target_ref or "").strip()
        if not ref:
            return
        pose = self.current_pose_mm_deg()
        if pose is not None:
            self.pose_by_target[(str(run_id or ""), ref)] = tuple(pose)  # type: ignore[assignment]

    def target_pose_mm_deg(self, target_ref: str | None,
                           run_id: str = "") -> tuple[float, ...] | None:
        return self.pose_by_target.get((str(run_id or ""), (target_ref or "").strip()))

    # ---------- 辅助 ----------
    def register_tickable(self, fn: Callable[[], Any]) -> None:
        """离线 Fake 设备的时间推进 (真实设备由链路轮询更新)."""
        self._tickables.append(fn)

    def advance(self) -> None:
        for fn in self._tickables:
            fn()

    def _holder(self, key: ExecutionKey) -> str:
        return f"{key.run_id}/{key.station_instance_id}/{key.step_id}#{key.attempt}"

    def _already_submitted(self, key: ExecutionKey):
        """同一 key 已提交则返回原 Handle, 不重复下发 (T27)."""
        if self.ledger is None:
            return None
        entry = self.ledger.entry(key)
        if entry is not None and entry.submitted:
            return entry.handle
        return None

    # ---------- 机器人 movej ----------
    def submit_move_joint(self, key: ExecutionKey, ctx: RunContext, target: JointTarget,
                          source: SourceKind, operator: str,
                          speed_pct: int = 20) -> CommandHandle | None:
        existing = self._already_submitted(key)
        if existing is not None:
            return existing
        if self.robot is None:
            raise ValidationFailure("未配置机器人适配器")
        holder = self._holder(key)
        lease = self.interlocks.acquire_resources(
            (MOTION_GLOBAL,), holder=holder, source=source,
            reason=f"move_joint@{key.step_id}",
        )
        self._leases[holder] = lease
        # 危险动作前先持久化意图; 失败则不下发运动
        provisional = CommandHandle(command_id=f"{holder}#intent",
                                    action_ref="robot.move_joint")
        provisional.state = CommandState.VALIDATED
        try:
            self._persist_intent(provisional, key, ctx)
        except Exception:
            self._release(key)
            raise
        handle = self.robot.move_joint(target, speed_pct=speed_pct)
        self._persist_intent(handle, key, ctx)
        _log.info("运动下发 move_joint step=%s 速度=%d%% 关节=%s", key.step_id,
                  speed_pct, list(getattr(target, "joint_deg", ()) or ()),
                  extra={"run_id": ctx.run_id, "step_id": key.step_id})
        if self.ledger is not None:
            self.ledger.submit_once(key, handle)
        return handle

    # ---------- 机构动作 ----------
    def submit_mechanism(self, key: ExecutionKey, ctx: RunContext, action_id: str,
                         params: dict[str, Any], source: SourceKind,
                         operator: str) -> CommandHandle | None:
        existing = self._already_submitted(key)
        if existing is not None:
            return existing
        holder = self._holder(key)
        # 互锁: 参数/前置/相反动作/资源 一并校验 (来源不绕过)
        lease = self.interlocks.authorize(action_id, params, holder=holder,
                                          source=source)
        self._leases[holder] = lease
        defn = self.interlocks.action_definition(action_id)
        board = self._board_for(defn)
        handle = board.issue_action(action_id, params)
        _log.info("机构动作下发 %s step=%s 参数=%s", action_id, key.step_id, params,
                  extra={"run_id": ctx.run_id, "step_id": key.step_id})
        try:
            # 意图持久化确认后才提交 (危险动作不能先发后记)
            self._persist_intent(handle, key, ctx)
        except Exception:
            board.retire(handle)
            self._release(key)
            raise
        if not board.submit(handle):
            self._release(key)
        if self.ledger is not None:
            self.ledger.submit_once(key, handle)
        return handle

    def poll(self, key: ExecutionKey, handle: CommandHandle) -> CommandState:
        """查询 Handle 状态; UNKNOWN 时隔离资源并记入账本 (T04/T27)."""
        state = handle.state
        if state is CommandState.UNKNOWN:
            holder = self._holder(key)
            lease = self._leases.get(holder)
            if lease is not None:
                self.interlocks.quarantine(lease, "命令 UNKNOWN, 资源隔离")
            if self.ledger is not None:
                self.ledger.mark_unknown(key)
        elif state in (CommandState.COMPLETED, CommandState.REJECTED,
                       CommandState.FAILED, CommandState.CANCELLED):
            self._release(key)
            if self.ledger is not None and state is CommandState.COMPLETED:
                self.ledger.mark_terminal(key)
            if self.records is not None:
                # 完成/失败后更新账本, 使持久化状态反映真实终态 (T20/T22)
                try:
                    self.records.update_command(
                        handle, run_id=key.run_id,
                        station_instance_id=key.station_instance_id,
                        step_id=key.step_id,
                    )
                except Exception:  # noqa: BLE001 - 记录失败不掩盖设备结果
                    pass
        return state

    def _release(self, key: ExecutionKey) -> None:
        holder = self._holder(key)
        lease = self._leases.pop(holder, None)
        if lease is not None:
            self.interlocks.release(lease)

    def settle_run_leases(self, run_id: str, *, stop_confirmed: bool) -> list[str]:
        """任务终止后结算该 run 中**尚未 poll 到终态**的动作租约（DEC-041）。

        被终止的 run 不再 tick，``poll`` 不会再跑到，若不结算，这些租约
        （如 ``motion_global``）会一直占着，下一次启动/单步会被互锁拒绝。

        - 停止已核实（CONFIRMED）：在途动作随设备停止一并结束 -> 释放租约；
        - 停稳未核实：租约转 **quarantine**，人工核对前不得再分配（不假装安全）。
        """
        prefix = f"{run_id}/"
        holders = [h for h in self._leases if h.startswith(prefix)]
        for holder in holders:
            lease = self._leases.pop(holder)
            if stop_confirmed:
                self.interlocks.release(lease)
            else:
                self.interlocks.quarantine(lease, "任务终止后停稳未核实")
        return holders

    def _board_for(self, defn: ActionDefinition):
        if self.boards is None:
            raise ValidationFailure("未配置板卡适配器")
        return self.boards.get(defn.device_id) if hasattr(self.boards, "get") else self.boards

    def _persist_intent(self, handle: CommandHandle | None, key: ExecutionKey,
                        ctx: RunContext) -> None:
        """危险动作下发前持久化确认; 失败即抛出让调用方不下发."""
        if handle is None or self.records is None:
            return
        self.records.confirm_command_intent(
            handle, run_id=key.run_id,
            station_instance_id=key.station_instance_id, step_id=key.step_id,
        )

    # ---------- 抓拍 ----------
    def capture(self, ctx: RunContext, request: CaptureRequest) -> CaptureSet:
        if not self.cameras:
            raise ValidationFailure("未配置相机适配器")
        holder = f"capture/{request.request_id or 'adhoc'}"
        # 抓拍占静止保护 + motion_global (冻结运动, T06)
        lease = self.interlocks.acquire_resources(
            (MOTION_GLOBAL, CAPTURE_STILLNESS), holder=holder,
            source=SourceKind.WORKFLOW, reason="camera.capture",
        )
        try:
            cs = CaptureSet(request=request)
            for cam_id in request.camera_ids:
                cam = self.cameras.get(cam_id)
                if cam is None:
                    cs.errors[cam_id] = f"未配置相机 {cam_id!r}"
                    continue
                sub = cam.capture(CaptureRequest(
                    camera_ids=(cam_id,),
                    required_streams=request.required_streams,
                    deadline_ms=request.deadline_ms,
                    allow_partial=True,
                    freshness_policy=request.freshness_policy,
                    request_id=request.request_id,
                    run_id=ctx.run_id,
                    station_instance_id=ctx.station_instance_id,
                    step_id=request.step_id,
                ))
                cs.frames.update(sub.frames)
                cs.errors.update(sub.errors)
        finally:
            self.interlocks.release(lease)
        if self.records is not None:
            self.records.save_capture_set(cs)          # 先登记集合 (帧->run 关联)
            for frame in cs.frames.values():
                self.records.save_capture(frame)
        if cs.frames:
            # DEC-042: 作业台右侧图卡显示最近一次抓拍的原图（按 run 匹配）
            self.last_frames = dict(cs.frames)
            self.last_frame = next(iter(cs.frames.values()))
            # DEC-051: 按相机累积本 run 的最近帧（多相机页签各自显示自己的图）
            self.camera_frames.update(cs.frames)
            self.last_capture_run_id = ctx.run_id
        if cs.errors:
            _log.warning("抓拍完成，有错误: 通道=%s 帧数=%d 错误=%s",
                         list(request.camera_ids), len(cs.frames), cs.errors,
                         extra={"run_id": ctx.run_id})
        else:
            _log.info("抓拍完成: 通道=%s 帧数=%d", list(request.camera_ids),
                      len(cs.frames), extra={"run_id": ctx.run_id})
        return cs

    # ---------- 视觉 ----------
    def circle_debug_params(self, detection_id: str | None) -> dict | None:
        """该命名检测保存的找圆调试参数（有 ``depth`` 段才算可执行，DEC-056）。

        存储读取失败/检测不存在/旧检测没保存参数一律返回 ``None``（回落整图
        流水线）；**不在这里抛错**，视觉步骤本身照常可运行。
        """
        store = self.vision_detections
        if store is None or not detection_id:
            return None
        try:
            det = store.find(detection_id)
        except Exception:  # noqa: BLE001 - 读存储失败当没参数, 走整图流水线
            return None
        params = getattr(det, "params", None) if det is not None else None
        return depth_circle_debug(params)

    def run_vision(self, ctx: RunContext, frame: FrameBundle, *, result_id: str,
                   cancelled: bool = False,
                   detection_id: str | None = None) -> DetectionResult:
        if self.vision is None:
            raise ValidationFailure("未配置视觉服务")
        # DEC-056：检测保存了找圆调试参数时，按「深度分割 → 每框找圆」执行，
        # 与视觉调试页「找圆调试-运行一次」同一条链；旧检测回落整图流水线。
        debug = self.circle_debug_params(detection_id)
        if debug is not None:
            spec = getattr(self.vision, "spec", None)
            result = run_depth_rects_vision(
                frame,
                circle_debug=debug,
                result_id=result_id,
                decision=getattr(spec, "decision", None),
                host_proxy=self.host_proxy,
                pipeline_id=(getattr(spec, "pipeline_id", None)
                             or getattr(self.vision, "pipeline_id", None)),
                pipeline_version=(getattr(spec, "version", None)
                                  or getattr(self.vision, "version", None)),
                host=(getattr(spec, "host", "") or
                      ("cp38-native" if self.host_proxy is not None
                       else "unavailable")),
                image_space=getattr(spec, "image_space", ImageSpace.COLOR),
                cancelled=cancelled)
            mode = VISION_MODE
        else:
            result = self.vision.run(frame, result_id=result_id, cancelled=cancelled)
            mode = "pipeline"
        if self.records is not None:
            self.records.save_result(result)
        self.last_detection = result
        self.last_detection_run_id = ctx.run_id
        status = getattr(result, "status", None)
        if status is DetectionStatus.ERROR:
            # DEC-052：失败要有可定位的明细——原因/错误码/来源帧一起记，
            # 不能只留一个"视觉 ERROR"。
            _log.error(
                "视觉判定失败 pipeline=%s version=%s mode=%s reason=%s errors=%s "
                "result=%s frame=%s camera=%s",
                getattr(result, "pipeline_id", "?") or "?",
                getattr(result, "pipeline_version", "?") or "?",
                mode,
                getattr(result, "reason", None), getattr(result, "errors", ()),
                getattr(result, "result_id", None), getattr(frame, "frame_id", None),
                getattr(frame, "camera_id", None),
                extra={"run_id": ctx.run_id,
                       "step_id": getattr(frame, "step_id", None),
                       "station_instance_id": getattr(ctx, "station_instance_id", None)})
        else:
            _log.info("视觉判定 pipeline=%s mode=%s 结果=%s",
                      getattr(result, "pipeline_id", "?"), mode,
                      getattr(status, "value", status),
                      extra={"run_id": ctx.run_id,
                             "step_id": getattr(frame, "step_id", None)})
        return result

    # ---------- 视觉「运行一次」方案（命名视觉检测里的实现逻辑） ----------
    def detection_scheme(self, detection_id: str):
        """取命名检测里保存的「运行一次」方案；没有则 None（回落到流水线）。

        方案存于 ``VisionDetection.params["scheme"]``（见 ``vision_detections`` 与
        ``algorithms.segmentation.scheme``）。**没有方案不报错**——那是旧的
        "只绑流水线"检测，照旧走流水线。
        """
        store = self.vision_detections
        if store is None or not detection_id:
            return None
        det = store.find(detection_id)
        if det is None:
            return None
        from application.algorithms.segmentation import scheme_from_params
        return scheme_from_params(getattr(det, "params", None))

    def run_detection_scheme(self, ctx: RunContext, frame: FrameBundle, *,
                             detection_id: str, scheme, result_id: str) -> DetectionResult:
        """按方案在帧上**真实执行**运行一次逻辑，产出 :class:`DetectionResult`。

        真实路径：深度分割 → 智能分割（EdgeSAM，离线占位需显式档位）→ 交集。
        智能分割不可用（真机缺 onnxruntime/模型）时返回 ERROR——不拿占位冒充真实分割。
        """
        from application.algorithms.segmentation import default_segmenter, run_scheme
        si = getattr(ctx, "station_instance_id", None)
        segmenter, note = default_segmenter(offline=bool(self.offline))
        base = dict(
            result_id=result_id, capture_id=getattr(frame, "capture_id", None),
            frame_id=getattr(frame, "frame_id", None), run_id=getattr(ctx, "run_id", None),
            station_instance_id=si, step_id=f"vision.run:{detection_id}",
            pipeline_id=f"scheme:{detection_id}",
            pipeline_version=str(getattr(scheme, "schema_version", "")),
            host="run_once", algorithm_version="segmentation.v1",
        )
        if segmenter is None:
            result = DetectionResult(
                status=DetectionStatus.ERROR, **base,
                reason="智能分割不可用，无法执行运行一次方案",
                errors=(note,))
            if self.records is not None:
                self.records.save_result(result)
            _log.error("运行一次方案 %s 失败: 智能分割不可用 (%s)", detection_id, note,
                       extra={"run_id": getattr(ctx, "run_id", None),
                              "step_id": f"vision.run:{detection_id}"})
            return result
        run = run_scheme(scheme, frame, segmenter)
        quality = {
            "segmenter": run.segmenter_name or type(segmenter).__name__,
            "segmenter_note": note,
            "source": run.source, "source_note": run.source_note,
            "timings_ms": dict(run.timings_ms),
            "n_depth_contours": len(run.depth_contours),
            "n_smart_polygons": len(run.smart_polygons),
        }
        measurements = [
            MetricMeasurement(name="intersection_area", value=float(run.area_px), unit="px"),
            MetricMeasurement(name="n_prompts",
                              value=float(sum(len(g) for g in run.prompts)), unit="px"),
        ]
        overlays = list(run.intersection_contours)
        if run.error:
            status = DetectionStatus.ERROR
            reason = run.error
            errors = (run.error,)
        elif run.hit:
            status = DetectionStatus.PASS
            reason = f"运行一次命中：交集面积 {run.area_px} px²"
            errors = ()
        else:
            status = DetectionStatus.NO_TARGET
            reason = "运行一次未命中：智能分割与用户多边形无有效交集"
            errors = ()
        result = DetectionResult(
            status=status, **base,
            measurements=measurements,
            metrics={"intersection_area_px": float(run.area_px)},
            quality=quality, overlays=overlays, reason=reason, errors=errors)
        if self.records is not None:
            self.records.save_result(result)
        self.last_detection = result
        self.last_detection_run_id = getattr(ctx, "run_id", None)
        _log.info("运行一次方案 %s: 状态=%s 交集面积=%s px²", detection_id,
                  status.value, run.area_px,
                  extra={"run_id": getattr(ctx, "run_id", None),
                         "step_id": f"vision.run:{detection_id}"})
        return result


    # ---------- 自定义操作（custom.call） ----------
    def run_custom_method(self, ctx: RunContext, method, device_refs: tuple[str, ...],
                          params: dict[str, Any] | None = None):
        """执行一个**已注册的自定义方法**（多设备: 同时下发, 等全部完成）。

        安全边界在**执行期**再次强制（不只靠发布前校验），任一不满足即**如实拒绝**，
        不伪造成功：

        - 方法声明 ``offline_ok=False`` 而当前是离线档位 -> 拒绝；
        - 方法声明 ``needs_motion=True`` -> 本版本自定义方法未接运动闸，拒绝；
        - 方法声明 ``needs_board=True`` 而无板卡适配器 -> 拒绝。

        多设备语义: 一次性对 ``device_refs`` 全部下发（issue），再统一收集全部结果
        （wait-all）；内置只读探测方法本身是快速同步调用，故"并发"体现为"一次下发、
        统一等齐"，逐台结果一并记入 detail 与 ``per_device``。
        """
        from application.services.methods import MethodOutcome
        if method is None:
            return MethodOutcome(ok=False, detail="自定义方法未解析")
        if not method.offline_ok and self.offline:
            return MethodOutcome(
                ok=False,
                detail=f"方法 {method.method_id!r} 不允许离线执行（需要真实设备/机构）")
        if method.needs_motion:
            return MethodOutcome(
                ok=False,
                detail=f"方法 {method.method_id!r} 需要运动闸；本版本自定义方法未接运动执行，拒绝")
        if method.needs_board and self.boards is None:
            return MethodOutcome(
                ok=False, detail=f"方法 {method.method_id!r} 需要板卡适配器，当前未配置")
        if method.runner is None:
            return MethodOutcome(ok=False, detail=f"方法 {method.method_id!r} 无可执行实现")
        env = {"executor": self, "ctx": ctx, "boards": self.boards,
               "device_state": getattr(self, "state_store", None)}
        try:
            outcome = method.runner(env, tuple(device_refs), dict(params or {}))
        except Exception as exc:  # noqa: BLE001 - 方法内部异常转失败, 不冒泡崩树
            _log.error("自定义方法 %s 执行异常: %s", method.method_id, exc,
                       extra={"run_id": getattr(ctx, "run_id", None)})
            return MethodOutcome(ok=False, detail=f"执行异常: {exc}")
        _log.info("自定义方法 %s 设备=%s 结果=%s", method.method_id,
                  list(device_refs), "OK" if outcome.ok else "FAIL",
                  extra={"run_id": getattr(ctx, "run_id", None)})
        return outcome


__all__ = ["ActionExecutor", "SourceScopeError"]
