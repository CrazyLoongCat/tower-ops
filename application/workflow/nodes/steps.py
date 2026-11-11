"""py_trees 步骤节点 (A16/E13, 12 §4.4, 02 §9.1/§9.5)。

叶子节点: robot.move_joint / mechanism.action / camera.capture / vision.run /
condition / record.evidence。wait.state 与 operator.confirm 见同目录独立模块。
subflow/repeat 是复合结构, 由编译器展开 (不在叶子工厂处理)。
"""
from __future__ import annotations

import math
import operator
import threading
import time
from typing import Any

from py_trees.common import Status

from application.algorithms.circle.detector import CircleDecisionConfig
from application.algorithms.circle.manual import decide_manual_circle
from application.contracts.context import RunContext
from application.contracts.enums import (
    CommandState, ConditionOp, DetectionStatus, FailurePolicy, NodeType, SourceKind,
    TelemetryFreshness,
)
from application.contracts.frames import CaptureRequest
from application.contracts.results import CirclePrimitive, MetricMeasurement
from application.domain.errors import ValidationFailure
from application.infrastructure import logging_setup
from application.infrastructure.events import Event, Severity
from application.services.vision_layout import (
    VisionLayout,
    box_around_circle,
    compute_initial_layout,
    find_circle_in_rect,
    image_size,
    normalize_boxes,
)
from application.workflow.nodes.base import BaseStepNode, OutputStore
from application.workflow.nodes.operator_confirm import OperatorConfirmNode
from application.workflow.nodes.wait_state import WaitStateNode

_OPS = {
    ConditionOp.EQ: operator.eq, ConditionOp.NE: operator.ne,
    ConditionOp.GT: operator.gt, ConditionOp.GE: operator.ge,
    ConditionOp.LT: operator.lt, ConditionOp.LE: operator.le,
    ConditionOp.IN: lambda a, b: a in b,
}

_log = logging_setup.get_logger("workflow.vision")


class MoveJointNode(BaseStepNode):
    """robot.move_joint: 提交 JointTarget; 到位/稳定窗由适配器判定."""

    def __init__(self, name, *, target, speed_pct: int = 20,
                 target_ref: str | None = None, **kw) -> None:
        super().__init__(name, **kw)
        self.target = target
        self.speed_pct = speed_pct
        self.target_ref = (target_ref or "").strip() or None

    def _tick(self, entry) -> Status:
        if entry is None or not entry.submitted:
            self.executor.submit_move_joint(
                self.key, self.ctx, self.target,
                source=SourceKind.WORKFLOW, operator="workflow",
                speed_pct=self.speed_pct,
            )
            self.executor.advance()
            return Status.RUNNING
        state = self.executor.poll(self.key, entry.handle)
        if state is CommandState.COMPLETED:
            si = self.ctx.station_instance_id or ""
            self.outputs.set(si, self.step_id, "joint", self.target.joint_deg_list)
            # DEC-064：记下到达该示教点时的实测位姿，供后续「末端位姿」判断比较。
            remember = getattr(self.executor, "remember_target_pose", None)
            if callable(remember):
                remember(self.target_ref, self.ctx.run_id)
            return self._mark_success({"final_joint": self.target.joint_deg_list})
        if state in (CommandState.FAILED, CommandState.REJECTED):
            return self._mark_failure(f"movej {state.value}")
        if state is CommandState.UNKNOWN:
            return self._mark_failure("movej UNKNOWN (资源隔离待人工核对)")
        self.executor.advance()
        return Status.RUNNING


class MovePlanNode(BaseStepNode):
    """robot.move_joint **绑定命名「运动方案」** 时的节点: 由运动方案执行器回放方案路点。

    与 :class:`MoveJointNode` 的区别: 不提交单一关节目标, 而是让 ``MotionRunner``
    按方案自身的路点顺序/速度/交融执行 —— 与「运动方案」页手动「运行方案」**同一套闸**:

    - 未开运动闸 / 机械臂未连接 -> ``start_plan`` 直接拒绝, 本节点按失败结束(不留假动作);
    - 离线栈用 ``NullMotionArm``, 一律拒绝 —— 与现状一致(「离线按现状拒运动」)。

    本节点不使用执行账本的 Handle (运动由 MotionRunner 自己的线程驱动), 故覆写
    ``update`` 自行轮询其状态; 结束/失败仍写进账本, 供快照与失败定位使用。
    """

    def __init__(self, name, *, motion, plan_id: str, **kw) -> None:
        super().__init__(name, **kw)
        self.motion = motion
        self.plan_id = plan_id
        self._started = False

    def initialise(self) -> None:
        super().initialise()
        self._started = False

    def update(self) -> Status:
        from application.services.motion_runner import (
            STATE_ERROR, STATE_GUIDE, STATE_PAUSED, STATE_PLAYING,
        )
        if self.motion is None:
            return self._mark_failure("执行方案绑定了运动方案，但执行栈未接入运动方案执行器")
        si = self.ctx.station_instance_id or ""
        if not self._started:
            return self._start()
        st = self.motion.state or {}
        mode = st.get("mode")
        if mode == STATE_ERROR:
            return self._mark_failure(
                f"运动方案 {self.plan_id!r} 失败：{st.get('last_error') or ''}")
        if mode in (STATE_PLAYING, STATE_PAUSED, STATE_GUIDE):
            return Status.RUNNING
        # 回到 idle = 方案跑完
        self.outputs.set(si, self.step_id, "motion_plan", self.plan_id)
        return self._mark_success({"motion_plan": self.plan_id})

    def _start(self) -> Status:
        if getattr(self.motion, "busy", False):
            return self._mark_failure("已有运动任务在执行，运动方案步骤无法开始")
        if not self.motion.start_plan(self.plan_id):
            st = self.motion.state or {}
            err = st.get("last_error") or "离线/未连接/未授权"
            return self._mark_failure(
                f"运动方案 {self.plan_id!r} 被拒：{err}")
        self._started = True
        si = self.ctx.station_instance_id or ""
        self.outputs.set(si, self.step_id, "motion_plan", self.plan_id)
        return Status.RUNNING


class MoveByBoltSizeNode(MovePlanNode):
    """robot.move_by_bolt_size（DEC-155）：按全局参数「螺栓直径」选运动方案路点。

    - 全局参数 = `manual_ops.yaml` 的 ``bolt_size``（16/20/24，取最近档）；
    - 步骤配置 ``bolt_size_waypoints``：{"16":1,"20":2,"24":3} → 方案路点序号（1 起）；
    - 只执行**选中的那一个路点**（与「运动方案」页运行方案同一套运动闸）；
    - 值未配置映射 / 存储缺失 / 下标的方案里越界 → 明确失败，不猜目标。
    """

    def __init__(self, name, *, motion, plan_id: str,
                 size_map: dict[int, int], via_waypoint: int | None = None,
                 operation_config: Any = None, **kw) -> None:
        super().__init__(name, motion=motion, plan_id=plan_id, **kw)
        self.size_map = dict(size_map or {})
        self.via_waypoint = int(via_waypoint) if via_waypoint else None
        self.operation_config = operation_config

    def _start(self) -> Status:
        if self.motion is None:
            return self._mark_failure("执行方案绑定了运动方案，但执行栈未接入运动方案执行器")
        if self.operation_config is None:
            return self._mark_failure(
                "未接入全局参数存储（manual_ops.yaml），不能按螺栓直径选路点")
        from application.services.operation_config import nearest_bolt_size
        size = int(round(nearest_bolt_size(
            self.operation_config.value("bolt_size"))))
        idx = self.size_map.get(size)
        if idx is None:
            return self._mark_failure(
                f"全局参数螺栓直径 {size}mm 未在本步配置对应路点"
                f"（需配置 16/20/24 之一）")
        if getattr(self.motion, "busy", False):
            return self._mark_failure("已有运动任务在执行，运动方案步骤无法开始")
        indices = ([self.via_waypoint - 1] if self.via_waypoint else []) + [int(idx) - 1]
        if not self.motion.start_selected(self.plan_id, indices):
            st = self.motion.state or {}
            err = st.get("last_error") or "离线/未连接/未授权"
            via = f"先到#{self.via_waypoint} 再到" if self.via_waypoint else ""
            return self._mark_failure(
                f"运动方案 {self.plan_id!r} {via}路点#{idx} 被拒：{err}")
        self._started = True
        si = self.ctx.station_instance_id or ""
        self.outputs.set(si, self.step_id, "motion_plan", self.plan_id)
        self.outputs.set(si, self.step_id, "bolt_size", size)   # 供判断/留证
        return Status.RUNNING


class GuidedPositionNode(BaseStepNode):
    """vision.guided_position（引导定位）: 相机抓图→深度引导找圆→粗/精偏差→
    每段**人工确认**后按 Bolt 工具系 ``[x, 0, z]`` 偏移（本程序内闭环, DEC-033）。

    默认用 DS86；DEC-137 起可用**任意已启用的相机通道**（如 D405 USB，由步骤的
    ``camera_ref`` 指定）。与「运动方案」页手动「引导定位」按钮**同一套闸、同一引导服务**：
    由 ``MotionRunner`` 的程序内入口驱动（运动闸 + 机械臂连接 + 同一圆宿主），不另开通道。
    每段确认由本节点提供（**受限选项 + 留证**，与 operator.confirm 同语义）：
    运行页把该段偏差显示给操作者，``provide("是")`` 才移动；
    **检测失败 / 未确认 / 偏移超上限 / 下发未到位 → 步骤失败**，绝不自动继续。
    """

    def __init__(self, name, *, motion, camera_ref: str = "",
                 stages: tuple[str, ...] = ("coarse", "fine"),
                 speed_pct: int | None = None, max_offset_mm: float = 0.0,
                 vision_step_ref: str | None = None,
                 fine_profile_ref: str | None = None, **kw) -> None:
        super().__init__(name, **kw)
        self.motion = motion
        self.camera_ref = camera_ref or ""
        self.stages = tuple(stages)
        self.speed_pct = speed_pct
        self.max_offset_mm = float(max_offset_mm or 0.0)
        # DEC-048：前面最近的视觉判定步骤（中间无新抓拍时）——本步沿用那一帧
        # 与人工确认后的圆，不再重新抓图找圆；无此引用则维持原闭环行为。
        self.vision_step_ref = vision_step_ref or ""
        self.fine_profile_ref = (fine_profile_ref or "").strip() or None
        self._started = False
        self._started_at = 0.0
        self._pending: dict | None = None
        self._choice: str | None = None
        self._answered = threading.Event()
        self._operator = "operator"

    # ---- 供运行页调用（与 operator.confirm 相同的受限选项语义）----
    @property
    def pending_prompt(self) -> dict | None:
        """等待人工确认时的提示（title/message/options）；无则 None。"""
        p = self._pending
        return dict(p) if p else None

    def provide(self, choice: str, operator: str = "operator") -> bool:
        """运行页提交选择；仅接受**已声明选项**（是/否）。未在等待则拒绝。"""
        p = self._pending
        if p is None or choice not in p["options"]:
            return False
        self._choice = choice
        self._operator = operator
        self._pending = None
        self._answered.set()
        _log.debug("引导定位步骤 %s 操作员提交确认：%s（操作员=%s）",
                   self.step_id, choice, operator)
        return True

    def _make_confirm(self):
        """确认端口：**阻塞运动线程**直到运行页 provide() 或作业被中止（None=不移动）。"""

        def confirm(title: str, message: str, options: list[str]) -> str | None:
            _log.debug("引导定位步骤 %s 进入等待确认：%s｜%s（选项=%s）",
                       self.step_id, title, message.replace("\n", " "), options)
            self._pending = {"title": title, "message": message,
                             "options": list(options), "step_id": self.step_id}
            self._choice = None
            self._answered.clear()
            while not self._answered.is_set():
                if getattr(self.motion, "cancel_requested", False):
                    self._pending = None
                    return None
                time.sleep(0.05)
            return self._choice

        return confirm

    def initialise(self) -> None:
        super().initialise()
        self._started = False
        self._started_at = 0.0
        self._pending = None
        self._choice = None
        self._answered.clear()
        self._operator = "operator"

    def update(self) -> Status:
        from application.services.motion_runner import STATE_ERROR, STATE_GUIDE
        if self.motion is None:
            return self._mark_failure("执行方案含引导定位步骤，但执行栈未接入运动方案执行器")
        if not self._started:
            guidance = getattr(self.motion, "guidance", None)
            if self.camera_ref:
                # DEC-137：不再只认 DS86 —— 只要该相机通道此刻有采集会话（设备配置已启用
                # 且连接成功）就能做引导；没有则如实报"没有可用会话"，不拿别的相机凑数。
                ids_fn = getattr(guidance, "camera_ids", None)
                ids = ids_fn() if callable(ids_fn) else None
                if ids is not None:
                    if self.camera_ref not in ids:
                        return self._mark_failure(
                            f"引导定位步骤引用的相机 {self.camera_ref!r} 此刻没有可用会话"
                            f"（本次已打开：{'、'.join(ids) or '无'}；"
                            f"请检查「设备配置」页是否启用/连接）")
                else:
                    bound = getattr(guidance, "camera_id", None)
                    if bound and self.camera_ref != bound:
                        return self._mark_failure(
                            f"引导定位步骤引用相机 {self.camera_ref!r}，"
                            f"但引导服务只接了 {bound!r}")
            if getattr(self.motion, "busy", False):
                return self._mark_failure("已有运动任务在执行，引导定位步骤无法开始")
            override = self._guide_override()
            _log.debug("引导定位步骤 %s 启动：段=%s 速度=%s 上限=%smm 相机=%s 沿用视觉帧=%s",
                       self.step_id, list(self.stages), self.speed_pct,
                       self.max_offset_mm, self.camera_ref or "默认",
                       (override or {}).get("step_id") or "否")
            kwargs = dict(speed=self.speed_pct, stages=self.stages,
                          max_offset_mm=self.max_offset_mm,
                          confirm=self._make_confirm())
            if self.fine_profile_ref:
                kwargs["fine_profile_ref"] = self.fine_profile_ref
            if self.camera_ref:
                kwargs["camera_id"] = self.camera_ref      # 兼容无 camera_id 的旧假执行器
            if override is not None:
                kwargs["override"] = override     # 兼容无 override 参数的旧假执行器
            try:
                ok = self.motion.start_guide(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 拒绝/异常都转步骤失败, 不冒泡崩树
                return self._mark_failure(f"引导定位启动失败: {exc}")
            if not ok:
                st = self.motion.state or {}
                return self._mark_failure(
                    f"引导定位被拒：{st.get('last_error') or '离线/未连接/未授权'}")
            self._started = True
            self._started_at = time.monotonic()
            return Status.RUNNING
        st = self.motion.state or {}
        # DEC-058: 检测阶段看门狗 —— 运动线程卡在相机读帧/找圆宿主时，
        # 不能永远停在"执行中"（界面没有确认条、机械臂不动、也不报错）。
        stalled = self._detect_stall(st)
        if stalled:
            return self._mark_failure(stalled)
        if st.get("mode") == STATE_GUIDE or self._pending is not None:
            return Status.RUNNING
        results = list(st.get("guide_results") or [])
        # 启动竞态保护: start_guide 先起运动线程、线程入口才把 mode 切到 guide。
        # 刚启动的一小段时间里 mode 仍可能是上一个值; 若无结果且刚启动 -> 继续等,
        # 不把"还没开始"误判成"中断"。
        if not results and time.monotonic() - self._started_at < 2.0:
            return Status.RUNNING
        if st.get("mode") == STATE_ERROR:
            self._record(results)
            return self._mark_failure(f"引导定位失败：{st.get('last_error') or ''}")
        applied = [r for r in results if r.get("applied")]
        if results and len(applied) == len(self.stages):
            si = self.ctx.station_instance_id or ""
            self.outputs.set(si, self.step_id, "guide", results)
            self._record(results)
            return self._mark_success({
                "guide_stages": list(self.stages),
                "offsets_mm": [[r.get("x_mm"), r.get("z_mm")] for r in applied]})
        last = results[-1] if results else {}
        self._record(results)
        reason = last.get("message") or "引导定位未完成（未确认或未执行）"
        return self._mark_failure(f"引导定位中断：{reason}")

    def _detect_stall(self, st: dict) -> str | None:
        """检测/准备阶段超时（DEC-058）；没超时返回 None。

        运动线程在"准备坐标系 / 检测"阶段本应数秒内返回；一旦卡在相机读帧、
        找圆宿主或机器人读系调用上，旧实现会让步骤**永远 RUNNING**：
        界面一直"执行中"、没有确认条、机械臂不动、日志也没有下一行。
        这里按 ``motion.guide_detect_timeout_s`` 判超时，**停止引导**（避免卡住的
        线程稍后回来自作主张移动）并如实失败。
        """
        from application.services.motion_runner import STATE_GUIDE
        if str(st.get("mode") or "") != STATE_GUIDE:
            return None                     # 引导已结束（idle/error）：残留阶段不算卡死
        phase = str(st.get("guide_phase") or "")
        if phase not in ("prepare", "detect"):
            return None
        try:
            limit = float(getattr(self.motion, "guide_detect_timeout_s", 0.0) or 0.0)
        except Exception:  # noqa: BLE001 - 读不到超时就不做看门狗
            return None
        try:
            since = float(st.get("guide_phase_since") or 0.0)
        except (TypeError, ValueError):
            return None
        if limit <= 0 or since <= 0:
            return None
        waited = time.monotonic() - since
        if waited < limit:
            return None
        stage = str(st.get("guide_stage") or "")
        label = {"coarse": "粗定位", "fine": "精定位"}.get(stage, "检测")
        try:
            self.motion.stop()
        except Exception:  # noqa: BLE001 - 停止失败也要把步骤判失败
            pass
        msg = (f"引导定位·{label}超过 {limit:.0f} 秒没有返回（阶段={phase}），"
               f"多为相机读帧或找圆宿主无响应；已停止引导，未移动机械臂")
        # DEC-058 排查快照：把卡住时的完整运动状态一起写进日志
        _log.error("%s（运动状态: mode=%s stage=%s message=%r 已等待 %.1fs "
                   "超时阈值 %.0fs）", msg, st.get("mode"), st.get("guide_stage"),
                   st.get("message"), waited, limit, extra={"step_id": self.step_id})
        return msg

    def _guide_override(self) -> dict | None:
        """视觉步骤发布的「同帧 + 定位圆」；run/步骤/相机任一对不上就不用。

        DEC-058：每条不采用的原因都写日志（DEBUG→文件），现场能一眼看出
        「为什么这步又去重新抓图找圆了」。
        """
        def _no(reason: str) -> None:
            _log.debug("引导定位步骤 %s 不沿用视觉步骤帧：%s；将重新抓图找圆",
                       self.step_id, reason)
            return None

        if not self.vision_step_ref:
            return _no("未绑定视觉判定步骤（中间有抓拍/运动/机构动作/自定义调用/子流程/循环）")
        target = getattr(self.executor, "vision_guide_target", None)
        if not isinstance(target, dict):
            return _no("执行器上没有视觉步骤发布的定位目标（未选定位圆/视觉步骤未发布）")
        if target.get("run_id") != self.ctx.run_id:
            return _no(f"run 不匹配（目标 run={target.get('run_id')!r}）")
        if str(target.get("step_id") or "") != self.vision_step_ref:
            return _no(f"步骤不匹配（目标步骤={target.get('step_id')!r}，"
                       f"本步绑定={self.vision_step_ref!r}）")
        cam = str(target.get("camera_id") or "")
        if self.camera_ref and cam and cam != self.camera_ref:
            return _no(f"相机不匹配（目标相机={cam!r}，本步={self.camera_ref!r}）")
        if target.get("frame") is None:
            return _no("视觉步骤没有可用的帧对象")
        circle = target.get("circle") or {}
        try:
            if float(circle.get("radius_px", 0.0)) < 0:
                return _no(f"圆心半径非法（{circle.get('radius_px')!r}）")
            float(circle.get("cx")), float(circle.get("cy"))
        except (TypeError, ValueError):
            return _no(f"圆心坐标非法（{circle!r}）")
        _log.debug("引导定位步骤 %s 沿用视觉步骤 %r 的帧 id=%s 圆心=(%s,%s) r=%spx",
                   self.step_id, self.vision_step_ref,
                   getattr(target.get("frame"), "frame_id", None),
                   circle.get("cx"), circle.get("cy"), circle.get("radius_px"))
        return target

    def _record(self, results: list) -> None:
        """把每段已执行的偏差写进事件（记录操作者与选择），失败尝试也留痕。"""
        records = getattr(self.executor, "records", None)
        if records is None:
            return
        for r in results:
            if not r.get("applied"):
                continue
            try:
                records.append_event_nonblocking(Event(
                    event_type="guide.position", severity=Severity.INFO,
                    message=(f"{r.get('stage')} 偏移 X={float(r.get('x_mm') or 0):.2f}mm "
                             f"Z={float(r.get('z_mm') or 0):.2f}mm 已确认并移动"),
                    run_id=self.ctx.run_id,
                    station_instance_id=self.ctx.station_instance_id,
                    step_id=self.step_id,
                    advice=f"operator={self._operator}",
                ))
            except Exception:  # noqa: BLE001 - 留证失败不影响流程推进/失败判定
                pass


class MechanismActionNode(BaseStepNode):
    """mechanism.action: 具名动作 + 完成反馈."""

    def __init__(self, name, *, action_id: str,
                 params: dict[str, Any] | None = None, **kw) -> None:
        super().__init__(name, **kw)
        self.action_id = action_id
        self.params = params or {}

    def _tick(self, entry) -> Status:
        if entry is None or not entry.submitted:
            self.executor.submit_mechanism(
                self.key, self.ctx, self.action_id, self.params,
                source=SourceKind.WORKFLOW, operator="workflow",
            )
            return Status.RUNNING
        state = self.executor.poll(self.key, entry.handle)
        if state is CommandState.COMPLETED:
            si = self.ctx.station_instance_id or ""
            self.outputs.set(si, self.step_id, "action", self.action_id)
            return self._mark_success({"action": self.action_id})
        if state in (CommandState.FAILED, CommandState.REJECTED):
            return self._mark_failure(f"机构动作 {self.action_id} {state.value}")
        if state is CommandState.UNKNOWN:
            return self._mark_failure("机构动作 UNKNOWN (资源隔离)")
        return Status.RUNNING


class CaptureNode(BaseStepNode):
    """camera.capture: 单台或集合抓拍; 结果为 CaptureSet."""

    def __init__(self, name, *, camera_ids, required_streams=("color", "depth"),
                 deadline_ms: int = 2000, allow_partial: bool = False, **kw) -> None:
        super().__init__(name, **kw)
        self.camera_ids = tuple(camera_ids)
        self.required_streams = tuple(required_streams)
        self.deadline_ms = deadline_ms
        self.allow_partial = allow_partial

    def _tick(self, entry) -> Status:
        request = CaptureRequest(
            camera_ids=self.camera_ids,
            required_streams=self.required_streams,
            deadline_ms=self.deadline_ms,
            allow_partial=self.allow_partial,
            request_id=f"{self.ctx.run_id}:{self.ctx.station_instance_id}:{self.step_id}",
            run_id=self.ctx.run_id,
            station_instance_id=self.ctx.station_instance_id,
            step_id=self.step_id,
        )
        cs = self.executor.capture(self.ctx, request)
        if not cs.ok:
            return self._mark_failure(f"抓拍集合失败: {cs.errors}")
        frame = next(iter(cs.frames.values()))
        si = self.ctx.station_instance_id or ""
        self.outputs.set(si, self.step_id, "frame", frame)
        self.outputs.set(si, self.step_id, "capture_id", frame.capture_id)
        return self._mark_success({"capture_id": frame.capture_id})


class VisionRunNode(BaseStepNode):
    """vision.run: 对上游帧运行流水线, 结果供 condition 判定。

    ``pipeline_id`` 可来自直接 ``pipeline_ref``, 也可由一条命名「视觉检测」
    (``detection_ref``) 解析得到(编译期解析, 见 compiler/``_effective_pipeline``)。
    本栈同一时刻只有一条视觉流水线: 若配方要求的与栈里的不一致, **明确失败**,
    不拿另一条流水线的结果冒充(宁缺勿假)。
    """

    def __init__(self, name, *, pipeline_id: str | None, input_step: str,
                 detection_ref: str | None = None, **kw) -> None:
        super().__init__(name, **kw)
        self.pipeline_id = pipeline_id
        self.input_step = input_step
        self.detection_ref = detection_ref or ""
        # 框/圆布局（DEC-048）：首次被作业台询问时按检测配置现算，之后人工调整沿用
        self._layout: dict | None = None      # {"boxes": [...], "selected": idx|None}
        self._layout_source = "none"
        self._layout_message = ""
        self._layout_manual = False

    def _tick(self, entry) -> Status:
        si = self.ctx.station_instance_id or ""
        frame = self.outputs.get(si, self.input_step, "frame")
        if frame is None:
            return self._mark_failure(f"上游步骤 {self.input_step!r} 无帧")
        result_id = f"{si}:{self.step_id}:it{self.ctx.iteration}:{self.attempt}"
        # 执行链优先级（DEC-056 / DEC-057）：
        #   1) 检测保存了可执行的找圆调试参数（``circle_debug.depth`` 段）→
        #      「深度分割 → 每框找圆」生产路径——与视觉调试页「找圆调试-运行一次」
        #      同一条链，作业台右卡才有框/圆可显示、可人工调整；
        #   2) 否则检测里的「运行一次方案」（视觉分割的 深度分割+EdgeSAM+交集）；
        #   3) 再没有就回落整图流水线。
        # 关键：界面「另存为检测」快照**永远**会写一份 ``params["scheme"]``（哪怕
        # 没画多边形），所以必须先判 circle_debug，否则找圆检测会被方案分支截胡，
        # 右卡因 ``pipeline_id="scheme:…"`` 而返回「没有检测圆，不能手动微调」。
        debug = None
        if self.detection_ref:
            fn = getattr(self.executor, "circle_debug_params", None)
            if callable(fn):
                try:
                    debug = fn(self.detection_ref)
                except Exception:  # noqa: BLE001 - 读存储失败当没有参数, 走后续分支
                    debug = None
        if debug is not None:
            extra = {"detection_id": self.detection_ref} if self.detection_ref else {}
            result = self.executor.run_vision(self.ctx, frame, result_id=result_id,
                                              **extra)
        else:
            scheme = None
            if self.detection_ref:
                fn = getattr(self.executor, "detection_scheme", None)
                if callable(fn):
                    scheme = fn(self.detection_ref)
            if scheme is not None:
                # 该检测保存的是「运行一次」方案：在真实帧上重跑同一算法，不套用流水线
                try:
                    result = self.executor.run_detection_scheme(
                        self.ctx, frame, detection_id=self.detection_ref,
                        scheme=scheme, result_id=result_id)
                except Exception as exc:  # noqa: BLE001 - 执行期异常转步骤失败, 不冒泡崩树
                    return self._mark_failure(f"运行一次方案执行失败: {exc}")
            else:
                actual = getattr(getattr(self.executor, "vision", None), "pipeline_id", None)
                if self.pipeline_id and actual is not None and self.pipeline_id != actual:
                    return self._mark_failure(
                        f"执行方案要求流水线 {self.pipeline_id!r}，本栈只有 {actual!r}；"
                        f"不支持在一次运行中切换流水线")
                extra = {"detection_id": self.detection_ref} if self.detection_ref else {}
                result = self.executor.run_vision(self.ctx, frame, result_id=result_id,
                                                  **extra)
        self.outputs.set(si, self.step_id, "status", result.status.value)
        self.outputs.set(si, self.step_id, "result", result)
        if self.detection_ref:
            self.outputs.set(si, self.step_id, "detection", self.detection_ref)
        if result.status is DetectionStatus.ERROR:
            # DEC-052：把 DetectionResult 的真实原因透出来——此前只报
            # "视觉 ERROR (取消依赖步骤)"，宿主未配置/宿主崩溃/输入不满足全被吞掉。
            return self._mark_failure(self._describe_failure(
                result, frame, "视觉判定失败", suffix="；依赖步骤已取消"))
        if result.status is DetectionStatus.CANCELLED:
            return self._mark_failure(self._describe_failure(
                result, frame, "视觉判定被取消", suffix=""))
        return self._mark_success({"status": result.status.value,
                                   **({"detection": self.detection_ref}
                                      if self.detection_ref else {})})

    def _describe_failure(self, result, frame, headline: str, *, suffix: str) -> str:
        """把失败结果写成可追溯的说明，并打一条 ERROR 日志（DEC-052）。

        只写结果里**确实有**的字段；来源（流水线/结果/帧/相机）用于把日志与
        记录库对起来，不伪造任何信息。
        """
        reason = str(getattr(result, "reason", "") or "").strip()
        if reason == "取消":
            reason = ""
        errors = [str(e) for e in (getattr(result, "errors", ()) or ()) if e]
        extra_errors = [e for e in errors if reason and e not in reason]
        pid = str(getattr(result, "pipeline_id", "") or "")
        pver = str(getattr(result, "pipeline_version", "") or "")
        rid = str(getattr(result, "result_id", "") or "")
        fid = str(getattr(result, "frame_id", "")
                  or getattr(frame, "frame_id", "") or "")
        cam = str(getattr(frame, "camera_id", "") or "")
        bits = [headline] if not reason else [f"{headline}: {reason}"]
        if extra_errors:
            bits.append("错误明细: " + "; ".join(extra_errors[:3]))
        if pid:
            bits.append(f"流水线 {pid}@{pver}" if pver else f"流水线 {pid}")
        if rid:
            bits.append(f"结果 {rid}")
        if fid:
            bits.append(f"帧 {fid}" + (f"（相机 {cam}）" if cam else ""))
        text = "；".join(bits)
        _log.error("视觉步骤 %s: %s（run_id=%s）", self.step_id, text,
                   self.ctx.run_id,
                   extra={"run_id": self.ctx.run_id, "step_id": self.step_id,
                          "station_instance_id": self.ctx.station_instance_id or None})
        return text + suffix

    # ---- 圆直径（DEC-147）：像素半径 × 圆心处深度 / √(fx·fy) ----
    def _circle_diameter_mm(self, cx: float, cy: float, radius_px: float) -> float:
        """自动/手动圆的物理直径(mm)；帧缺深度或内参时返回 0（不伪造）。"""
        from application.services.guidance_service import circle_diameter_mm

        frame = self._frame()
        if frame is None:
            return 0.0
        depth = getattr(getattr(frame, "depth", None), "data", None)
        scale = getattr(frame, "depth_scale_m", None)
        intr = getattr(frame, "intrinsics", None)
        if depth is None or not scale or intr is None:
            return 0.0
        try:
            import numpy as _np
            r = max(2.0, float(radius_px))
            h, w = depth.shape[:2]
            x0, x1 = max(0, int(cx - r)), min(w, int(cx + r) + 1)
            y0, y1 = max(0, int(cy - r)), min(h, int(cy + r) + 1)
            patch = depth[y0:y1, x0:x1].astype("float64") * float(scale) * 1000.0
            yy, xx = _np.mgrid[y0:y1, x0:x1]
            disk = ((xx - cx) ** 2 + (yy - cy) ** 2) <= r * r
            vals = patch[disk & (patch > 0) & (patch < 65535.0)]
            if vals.size == 0:
                return 0.0
            z = float(_np.median(vals))
            return circle_diameter_mm(r, z, {"fx": float(intr.fx),
                                             "fy": float(intr.fy)})
        except Exception:  # noqa: BLE001 - 直径只是附加信息，失败不影响判定
            return 0.0

    # ---- 手动微调检测圆 (DEC-042, 作业台右卡拖动) ----
    def _decision(self) -> CircleDecisionConfig:
        spec = getattr(getattr(self.executor, "vision", None), "spec", None)
        return getattr(spec, "decision", None) or CircleDecisionConfig()

    def _result(self):
        si = self.ctx.station_instance_id or ""
        return self.outputs.get(si, self.step_id, "result")

    def circle_state(self) -> dict:
        """供作业台右卡: 框/圆布局（多个框、每框一个圆）、选中圆与判定。

        DEC-048：首次询问时按**检测保存的找圆调试参数**在当前帧现算初始框/圆；
        没有参数时退化为自动检测圆的外接方框，并在 ``layout_message`` 说明。
        人工调整后状态从本节点内存中的布局读取（仅本次运行有效，不写检测/配方）。
        """
        result = self._result()
        if result is None:
            return {"available": False, "step_id": self.step_id,
                    "reason": "当前步骤没有视觉结果"}
        pid = str(getattr(result, "pipeline_id", "") or "")
        if pid.startswith("scheme:"):
            return {"available": False, "step_id": self.step_id,
                    "reason": "「运行一次」方案没有检测圆，不能手动微调"}
        try:
            layout = self._ensure_layout()
            boxes = [dict(b, circle=dict(b["circle"]) if b.get("circle") else None)
                     for b in layout.boxes]
            selected = self._layout.get("selected") if self._layout else None
            circles = [b["circle"] for b in boxes if b.get("circle")]
            circle = None
            if selected is not None and 0 <= selected < len(boxes):
                c = boxes[selected].get("circle")
                if c:
                    circle = dict(c)
            if circle is None and circles:
                circle = dict(circles[0])
            for _b in boxes:                     # DEC-147：圆直径（有深度才补）
                _c = _b.get("circle")
                if _c is not None and "diameter_mm" not in _c:
                    _d = self._circle_diameter_mm(_c.get("cx", 0.0),
                                                  _c.get("cy", 0.0),
                                                  _c.get("radius_px", 0.0))
                    if _d > 0:
                        _c["diameter_mm"] = round(_d, 1)
            if circle is not None and "diameter_mm" not in circle:
                _d = self._circle_diameter_mm(circle.get("cx", 0.0),
                                              circle.get("cy", 0.0),
                                              circle.get("radius_px", 0.0))
                if _d > 0:
                    circle["diameter_mm"] = round(_d, 1)
            count = len(circles)
            message = self._layout_message
        except Exception as exc:  # noqa: BLE001 - 读取失败如实回报, 不伪造布局
            boxes, selected, circles, circle, count = [], None, [], None, 0
            message = f"框/圆布局读取失败: {exc}"
        metrics = getattr(result, "metrics", {}) or {}
        status = getattr(getattr(result, "status", None), "value", None) \
            or str(getattr(result, "status", ""))
        return {
            "available": True,
            "step_id": self.step_id,
            "status": status,
            "reason": getattr(result, "reason", "") or "",
            "circle": circle,
            "count": count,
            "manual": bool(metrics.get("manual")),
            "frame_id": getattr(result, "frame_id", None),
            "capture_id": getattr(result, "capture_id", None),
            # DEC-048 多框/多圆
            "boxes": boxes,
            "selected": selected,
            "layout_source": self._layout_source,
            "layout_manual": self._layout_manual,
            "layout_message": message,
        }

    # ---- 多框/多圆布局 (DEC-048) ----
    def _frame(self):
        si = self.ctx.station_instance_id or ""
        return self.outputs.get(si, self.input_step, "frame")

    def _detection_params(self) -> dict:
        store = getattr(self.executor, "vision_detections", None)
        if store is None or not self.detection_ref:
            return {}
        try:
            det = store.find(self.detection_ref)
        except Exception:  # noqa: BLE001 - 存储读取失败当没有参数, 走兜底
            return {}
        params = getattr(det, "params", None) if det is not None else None
        return dict(params) if isinstance(params, dict) else {}

    def _ensure_layout(self) -> VisionLayout:
        """按检测配置现算一次初始布局；人工调整后原样返回。"""
        if self._layout is not None:
            return VisionLayout(boxes=self._layout["boxes"],
                                selected=self._layout.get("selected"),
                                source=self._layout_source,
                                message=self._layout_message)
        result = self._result()
        if result is None:
            raise ValueError("当前步骤没有视觉结果")
        try:
            layout = compute_initial_layout(
                frame=self._frame(), params=self._detection_params(),
                host_proxy=getattr(self.executor, "host_proxy", None),
                fallback_circles=list(getattr(result, "targets", ()) or ()))
        except Exception as exc:  # noqa: BLE001 - 现算失败如实说明, 仍可人工画框
            layout = VisionLayout(boxes=[], selected=None, source="none",
                                  message=f"按检测配置现算失败: {exc}")
        self._layout = {"boxes": layout.boxes, "selected": layout.selected}
        self._layout_source = layout.source
        self._layout_message = layout.message
        self._publish_guide_target()
        return layout

    def _layout_frame_camera(self) -> str:
        frame = self._frame()
        cam = getattr(frame, "camera_id", "") or ""
        if cam:
            return cam
        store = getattr(self.executor, "vision_detections", None)
        if store is not None and self.detection_ref:
            try:
                det = store.find(self.detection_ref)
                return str(getattr(det, "camera_ref", "") or "") if det else ""
            except Exception:  # noqa: BLE001
                return ""
        return ""

    def _publish_guide_target(self) -> None:
        """把选中圆 + 本步所用帧发布到执行器，供后续引导定位步骤沿用（DEC-048）。"""
        if self._layout is None:
            return
        idx = self._layout.get("selected")
        boxes = self._layout.get("boxes") or []
        circle = None
        if idx is not None and 0 <= idx < len(boxes) and boxes[idx].get("circle"):
            circle = dict(boxes[idx]["circle"])
        current = getattr(self.executor, "vision_guide_target", None)
        if circle is None:
            if isinstance(current, dict) and current.get("run_id") == self.ctx.run_id \
                    and current.get("step_id") == self.step_id:
                self.executor.vision_guide_target = None
            _log.debug("视觉步骤 %s 未选定定位圆（框序号=%s），已清空定位目标；"
                       "引导定位将重新找圆", self.step_id, idx)
            return
        frame = self._frame()
        self.executor.vision_guide_target = {
            "run_id": self.ctx.run_id,
            "step_id": self.step_id,
            "camera_id": self._layout_frame_camera(),
            "frame": frame,
            "circle": circle,
            "manual": bool(self._layout_manual),
            "source": self._layout_source,
        }
        _log.debug("视觉步骤 %s 发布定位目标：框序号=%s 圆心=(%s,%s) r=%spx "
                   "帧id=%s 相机=%s 人工=%s 来源=%s", self.step_id, idx,
                   circle.get("cx"), circle.get("cy"), circle.get("radius_px"),
                   getattr(frame, "frame_id", None), self._layout_frame_camera(),
                   bool(self._layout_manual), self._layout_source)

    def add_box(self, x1: float, y1: float, x2: float, y2: float,
                operator: str = "operator") -> dict:
        """人工新画一个框：按检测参数在框内找圆后加入布局（找不到圆就空框）。"""
        self._ensure_layout()
        result = self._result()
        if result is None:
            raise ValueError("当前步骤没有视觉结果，不能画框")
        frame = self._frame()
        width, height = image_size(frame)
        box = normalize_boxes([{"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                "circle": None}], width, height)[0]
        circle, why = find_circle_in_rect(
            frame, self._detection_params(),
            getattr(self.executor, "host_proxy", None),
            {"x1": box["x1"], "y1": box["y1"],
             "x2": box["x2"], "y2": box["y2"],
             "idx": len(self._layout["boxes"])})
        box["circle"] = circle
        boxes = list(self._layout["boxes"]) + [box]
        selected = (len(boxes) - 1 if circle is not None
                    else self._layout.get("selected"))
        info = self.apply_layout(boxes, selected, operator)
        info["message"] = why
        return info

    def set_box_circle(self, box_index: int, cx: float, cy: float,
                       radius_px: float, operator: str = "operator") -> dict:
        """人工在某个框内画/调整圆，并以该圆为定位圆重新判定。"""
        self._ensure_layout()
        boxes = [dict(b, circle=dict(b["circle"]) if b.get("circle") else None)
                 for b in self._layout["boxes"]]
        if not (0 <= int(box_index) < len(boxes)):
            raise ValueError(f"框序号 {box_index} 超出范围")
        if float(radius_px) <= 0:
            raise ValueError("检测圆半径必须为正")
        boxes[int(box_index)]["circle"] = {"cx": float(cx), "cy": float(cy),
                                           "radius_px": float(radius_px)}
        return self.apply_layout(boxes, int(box_index), operator)

    def select_box(self, box_index: int, operator: str = "") -> bool:
        """选择某个框作为定位圆来源（不重算判定）。

        ``operator`` 非空 = 操作员**显式指定**定位圆：选择确实变化时记一条
        ``vision.select_target``（DEC-054，作业台「定位圆」下拉/点击图卡都走这里）；
        程序内部调用（不带 operator）保持 DEC-048 口径：不重算、不留证。
        """
        self._ensure_layout()
        boxes = self._layout["boxes"]
        before = self._layout.get("selected")
        if box_index is None or not (0 <= int(box_index) < len(boxes)):
            self._layout["selected"] = None
        else:
            self._layout["selected"] = int(box_index)
        self._publish_guide_target()
        if operator and self._layout.get("selected") != before:
            self._record_selection(operator)
        return True

    def _record_selection(self, operator: str) -> None:
        """记录操作员指定的定位圆（后续引导定位沿用该圆）。"""
        records = getattr(self.executor, "records", None)
        if records is None:
            return
        idx = (self._layout or {}).get("selected")
        boxes = (self._layout or {}).get("boxes") or []
        circle = None
        if idx is not None and 0 <= idx < len(boxes):
            circle = boxes[idx].get("circle")
        what = ("不指定（引导定位将重新找圆）")
        if idx is not None and 0 <= idx < len(boxes):
            what = (f"框{idx + 1}的圆 ({circle['cx']:.0f},{circle['cy']:.0f}) "
                    f"r={circle['radius_px']:.1f}px" if circle
                    else f"框{idx + 1}（框内没有圆，引导定位将重新找圆）")
        try:
            records.append_event_nonblocking(Event(
                event_type="vision.select_target", severity=Severity.INFO,
                message=f"操作员指定定位圆：{what}（后续引导定位沿用）",
                run_id=self.ctx.run_id,
                station_instance_id=self.ctx.station_instance_id,
                step_id=self.step_id,
                advice=f"operator={operator}",
            ))
        except Exception:  # noqa: BLE001 - 留证失败不影响选择生效
            pass

    def apply_layout(self, boxes, selected, operator: str = "operator") -> dict:
        """以人工框/圆布局为准重算判定并回写输出（仅本次运行，不写检测/配方）。"""
        result = self._result()
        if result is None:
            raise ValueError("当前步骤没有视觉结果，不能调整框/圆")
        pid = str(getattr(result, "pipeline_id", "") or "")
        if pid.startswith("scheme:"):
            raise ValueError("「运行一次」方案没有检测圆，不能调整框/圆")
        width, height = image_size(self._frame())
        data = normalize_boxes(boxes, width, height)
        if selected is not None and not (0 <= int(selected) < len(data)):
            selected = None
        if selected is not None and not data[int(selected)].get("circle"):
            selected = None
        if selected is None:
            selected = next((i for i, b in enumerate(data) if b.get("circle")), None)
        old_targets = list(getattr(result, "targets", ()) or ())
        circles = []
        circle_index: dict[int, int] = {}
        for i, box in enumerate(data):
            c = box.get("circle")
            if not c:
                continue
            circle_index[i] = len(circles)
            circles.append(CirclePrimitive(
                cx=float(c["cx"]), cy=float(c["cy"]),
                radius_px=float(c["radius_px"]), image_space=result.image_space,
                coverage=None, flag=0))
        self._layout = {"boxes": data, "selected": selected}
        self._layout_manual = True
        self._layout_source = "manual"
        self._layout_message = (f"人工调整：{len(data)} 个框，{len(circles)} 个圆"
                                + (f"，选中框 {selected + 1}" if selected is not None
                                   else "，未选中圆"))
        if circles:
            focus = circles[circle_index[selected]]
            status, why = decide_manual_circle(focus, self._decision())
            result.targets = circles
            result.status = status
            result.reason = f"手动框圆：{why}"
            result.measurements = [MetricMeasurement(
                name="radius", value=focus.radius_px, unit="px")]
        else:
            status, why = DetectionStatus.NO_TARGET, "没有检测圆（框内未找到圆或已删除）"
            result.targets = []
            result.status = status
            result.reason = f"手动框圆：{why}"
            result.measurements = []
        metrics = {**dict(getattr(result, "metrics", {}) or {}),
                   "manual": 1.0, "manual_boxes": float(len(data)),
                   "manual_circles": float(len(circles))}
        if circles:
            metrics["manual_radius_px"] = float(focus.radius_px)
            metrics["manual_selected_box"] = float(selected)
        result.metrics = metrics
        si = self.ctx.station_instance_id or ""
        self.outputs.set(si, self.step_id, "status", status.value)
        self.outputs.set(si, self.step_id, "result", result)
        self._record_layout(old_targets, circles, selected, status, why, operator)
        self._publish_guide_target()
        focus_dict = (dict(data[selected]["circle"]) if circles and selected is not None
                      else None)
        diameter_mm = (self._circle_diameter_mm(focus_dict["cx"], focus_dict["cy"],
                                                focus_dict["radius_px"])
                       if focus_dict else 0.0)
        return {"status": status.value, "reason": result.reason,
                "boxes": len(data), "circles": len(circles),
                "selected_box": selected, "circle": focus_dict,
                "cx": focus_dict["cx"] if focus_dict else None,
                "cy": focus_dict["cy"] if focus_dict else None,
                "radius_px": focus_dict["radius_px"] if focus_dict else None,
                "diameter_mm": round(diameter_mm, 1),
                "auto_count": len(old_targets), "manual": True}

    def _record_layout(self, old_targets: list, circles: list,
                       selected, status: DetectionStatus, why: str,
                       operator: str) -> None:
        records = getattr(self.executor, "records", None)
        if records is None:
            return
        auto = "；".join(
            f"({float(getattr(c, 'cx', 0.0)):.0f},{float(getattr(c, 'cy', 0.0)):.0f})"
            f" r={float(getattr(c, 'radius_px', 0.0)):.1f}px" for c in old_targets
        ) or "无"
        focus = (f"框{selected + 1}的圆" if selected is not None and circles else "无")
        detail = ""
        if selected is not None and self._layout is not None:
            c = (self._layout.get("boxes") or [])[selected].get("circle")
            if c:
                detail = f" ({c['cx']:.0f},{c['cy']:.0f}) r={c['radius_px']:.1f}px"
        try:
            records.append_event_nonblocking(Event(
                event_type="vision.manual_layout", severity=Severity.WARNING,
                message=(f"手动调整框/圆：自动圆[{auto}] -> {len(circles)} 个圆"
                         f"（{focus}{detail}）；判定 {status.value}（{why}）"),
                run_id=self.ctx.run_id,
                station_instance_id=self.ctx.station_instance_id,
                step_id=self.step_id,
                advice=f"operator={operator}",
            ))
        except Exception:  # noqa: BLE001 - 留证失败不影响判定更新
            pass

    def apply_manual_circle(self, cx: float, cy: float, radius_px: float,
                            operator: str = "operator") -> dict:
        """以**手动拖动的圆**替换判定目标并重算状态（DEC-042）。

        - 只改本步骤输出（status/result），后续 condition 读到的是手动结果；
        - 不生成运动目标；
        - 「运行一次」方案（无圆目标）明确拒绝，不伪造检测圆；
        - 记录事件：自动圆数与手动圆坐标/半径、操作者、重算后的判定。
        """
        result = self._result()
        if result is None:
            raise ValueError("当前步骤没有视觉结果，不能手动微调")
        pid = str(getattr(result, "pipeline_id", "") or "")
        if pid.startswith("scheme:"):
            raise ValueError("「运行一次」方案没有检测圆，不能手动微调")
        r = float(radius_px)
        if r <= 0:
            raise ValueError("检测圆半径必须为正")
        circle = CirclePrimitive(cx=float(cx), cy=float(cy), radius_px=r,
                                 image_space=result.image_space, coverage=None,
                                 flag=0)
        status, why = decide_manual_circle(circle, self._decision())
        old_targets = list(getattr(result, "targets", ()) or ())
        result.targets = [circle]
        result.status = status
        result.reason = f"手动微调：{why}"
        result.measurements = [MetricMeasurement(name="radius", value=r, unit="px")]
        diameter_mm = self._circle_diameter_mm(circle.cx, circle.cy, r)
        result.metrics = {**dict(getattr(result, "metrics", {}) or {}),
                          "manual": 1.0, "manual_radius_px": r}
        if diameter_mm > 0:
            result.metrics["manual_diameter_mm"] = round(diameter_mm, 3)
        si = self.ctx.station_instance_id or ""
        self.outputs.set(si, self.step_id, "status", status.value)
        self.outputs.set(si, self.step_id, "result", result)
        self._record_manual(old_targets, circle, status, why, operator)
        # DEC-048：旧单圆入口也同步到多框布局（右卡从单圆升级为框+圆后保持一致）
        if self._layout is not None:
            boxes = self._layout.get("boxes") or []
            idx = self._layout.get("selected")
            if idx is None or not (0 <= idx < len(boxes)):
                idx = next((i for i, b in enumerate(boxes) if b.get("circle")), None)
            if idx is None:
                box = box_around_circle(
                    {"cx": circle.cx, "cy": circle.cy,
                     "radius_px": circle.radius_px},
                    *image_size(self._frame()))
                boxes.append(box)
                idx = len(boxes) - 1
            boxes[idx]["circle"] = {"cx": circle.cx, "cy": circle.cy,
                                    "radius_px": r}
            self._layout = {"boxes": boxes, "selected": idx}
            self._layout_manual = True
            self._publish_guide_target()
        return {"status": status.value, "reason": result.reason,
                "cx": circle.cx, "cy": circle.cy, "radius_px": r,
                "diameter_mm": round(diameter_mm, 1),
                "auto_count": len(old_targets)}

    def _record_manual(self, old_targets: list, circle: CirclePrimitive,
                       status: DetectionStatus, why: str, operator: str) -> None:
        records = getattr(self.executor, "records", None)
        if records is None:
            return
        auto = "；".join(
            f"({float(getattr(c, 'cx', 0.0)):.0f},{float(getattr(c, 'cy', 0.0)):.0f})"
            f" r={float(getattr(c, 'radius_px', 0.0)):.1f}px" for c in old_targets
        ) or "无"
        try:
            records.append_event_nonblocking(Event(
                event_type="vision.manual_circle", severity=Severity.WARNING,
                message=(f"手动调整检测圆：自动圆[{auto}] -> 手动 "
                         f"({circle.cx:.0f},{circle.cy:.0f}) r={circle.radius_px:.1f}px；"
                         f"判定 {status.value}（{why}）"),
                run_id=self.ctx.run_id,
                station_instance_id=self.ctx.station_instance_id,
                step_id=self.step_id,
                advice=f"operator={operator}",
            ))
        except Exception:  # noqa: BLE001 - 留证失败不影响判定更新
            pass


class ConditionNode(BaseStepNode):
    """condition: 结构化比较, 不下发动作. 不合格进入已定义暂停/失败路径."""

    def __init__(self, name, *, predicate,
                 on_false_policy: FailurePolicy | None = None, **kw) -> None:
        super().__init__(name, **kw)
        self.predicate = predicate
        self.on_false_policy = on_false_policy

    def _tick(self, entry) -> Status:
        ok = self._evaluate(self.predicate)
        if ok:
            return self._mark_success({"condition": True})
        return self._mark_failure("条件不满足", policy=self.on_false_policy
                                  or FailurePolicy.PAUSE_FOR_REVIEW)

    def _evaluate(self, node) -> bool:
        si = self.ctx.station_instance_id or ""
        if node.all:
            return all(self._evaluate(c) for c in node.all)
        if node.any:
            return any(self._evaluate(c) for c in node.any)
        actual = self.outputs.get(si, node.source.step, node.source.field)
        fn = _OPS.get(node.op)
        if fn is None:
            return False
        return bool(fn(actual, node.value))


class PostCheckNode(BaseStepNode):
    """步骤完成后的**判断属性**（DEC-064）。

    与步骤节点分开、独立账本键（``<step>#check``）：步骤本身成功即结算；本节点在每次
    tick 重新判断，全部通过才 SUCCESS 放行。任一项不通过时**不返回 FAILURE**，而是
    记录暂停原因并保持 RUNNING；``TaskService`` 据此把任务停在安全边界 PAUSED
    （用户裁定：不通过 = 安全暂停，不是设备故障）。人工处理后在作业台继续，会重新判断。
    """

    def __init__(self, name: str, *, checks, target_step_id: str,
                 max_age_ms: float = 2000.0, **kw) -> None:
        kw["step_id"] = f"{target_step_id}#check"
        super().__init__(name, **kw)
        self.target_step_id = target_step_id
        self.checks = tuple(checks or ())
        self.max_age_ms = float(max_age_ms)
        self._pause_reason: str | None = None
        self._pause_handled = False

    # ---- 给 TaskService 取暂停原因（只取一次；重新判断失败会再次置位） ----
    def consume_pause_request(self) -> str | None:
        if self._pause_reason and not self._pause_handled:
            self._pause_handled = True
            return self._pause_reason
        return None

    def initialise(self) -> None:
        super().initialise()
        self._pause_reason = None
        self._pause_handled = False

    def _tick(self, entry) -> Status:
        try:
            failures = self._evaluate_all()
        except Exception as exc:  # noqa: BLE001 - 判断本身异常也按不通过处理, 不崩树
            failures = [f"判断执行异常: {exc}"]
        if not failures:
            self._pause_reason = None
            self._pause_handled = True
            return self._mark_success({"post_checks": "PASS"})
        self._pause_reason = "；".join(failures)
        self._pause_handled = False
        return Status.RUNNING

    # ---- 逐项判断 ----
    def _evaluate_all(self) -> list[str]:
        failures: list[str] = []
        for chk in self.checks:
            ok, reason = self._evaluate_one(chk)
            if not ok:
                failures.append(reason)
        return failures

    def _evaluate_one(self, chk) -> tuple[bool, str]:
        kind = str(getattr(chk, "kind", ""))
        if kind == "robot_pose":
            return self._check_robot_pose(chk)
        if kind == "device_field":
            return self._check_device_field(chk)
        if kind == "step_output":
            return self._check_step_output(chk)
        return False, f"未知判断类型 {kind!r}"

    def _check_robot_pose(self, chk) -> tuple[bool, str]:
        ref = str(getattr(chk, "joint_target_ref", "") or "")
        expected = None
        fn = getattr(self.executor, "target_pose_mm_deg", None)
        if callable(fn):
            expected = fn(ref, self.ctx.run_id)
        if expected is None:
            return False, (f"示教点 {ref!r} 在本次运行中还没有实测位姿"
                           f"（需先运动到该示教点）")
        cur_fn = getattr(self.executor, "current_pose_mm_deg", None)
        current = cur_fn() if callable(cur_fn) else None
        if current is None:
            return False, "读不到机械臂当前末端位姿"
        tol = float(getattr(chk, "pos_tol_mm", 2.0))
        dist = math.sqrt(sum((float(current[i]) - float(expected[i])) ** 2
                             for i in range(3)))
        if dist > tol:
            return False, (f"末端位置偏离示教点 {ref!r} {dist:.2f}mm "
                           f"> 容差 {tol:.2f}mm")
        if bool(getattr(chk, "check_rotation", False)):
            rtol = float(getattr(chk, "rot_tol_deg", 2.0))
            worst = 0.0
            for i in (3, 4, 5):
                d = (float(current[i]) - float(expected[i]) + 180.0) % 360.0 - 180.0
                worst = max(worst, abs(d))
            if worst > rtol:
                return False, (f"末端姿态偏离示教点 {ref!r} {worst:.2f}° "
                               f"> 容差 {rtol:.2f}°")
        return True, ""

    def _check_device_field(self, chk) -> tuple[bool, str]:
        device = str(getattr(chk, "device_ref", "") or "")
        field = str(getattr(chk, "field_name", "") or "")
        values = None
        store = getattr(self.executor, "state_store", None)
        if store is not None:
            rec = store.get(device)
            if rec is not None:
                try:
                    fresh = (rec.validity is TelemetryFreshness.VALID
                             and rec.is_fresh(self.max_age_ms))
                except Exception:  # noqa: BLE001
                    fresh = False
                if not fresh:
                    return False, (f"设备 {device!r} 遥测不新鲜"
                                   f"（超过 {self.max_age_ms:.0f}ms 或无效）")
                values = dict(rec.values or {})
        if values is None:
            # 生产栈的 state_store 目前没有统一遥测轮询：机器人状态直接读适配器，
            # 避免“设备字段判断永远说没有遥测”。
            values = self._robot_state_values(device)
        if values is None:
            return False, f"设备 {device!r} 无遥测记录"
        actual = values.get(field)
        ok = _compare_value(actual, getattr(chk, "op", None), getattr(chk, "value", None))
        if not ok:
            return False, (f"设备 {device!r}.{field} 实际={actual!r}，"
                           f"要求 {getattr(chk, 'op', None)} {getattr(chk, 'value', None)!r}")
        return True, ""

    def _robot_state_values(self, device: str) -> dict | None:
        """device_ref 指向机器人时，直接读适配器的 read_state()。"""
        robot = getattr(self.executor, "robot", None)
        if robot is None:
            return None
        cfg = getattr(robot, "config", None)
        ids = {getattr(robot, "robot_id", None),
               getattr(cfg, "robot_id", None) if cfg is not None else None,
               "eco65_01"}
        if device not in {str(i) for i in ids if i}:
            return None
        fn = getattr(robot, "read_state", None)
        if not callable(fn):
            return None
        try:
            st = fn()
        except Exception:  # noqa: BLE001
            return None
        return dict(st) if isinstance(st, dict) else None

    def _check_step_output(self, chk) -> tuple[bool, str]:
        source = str(getattr(chk, "source_step", "") or "")
        field = str(getattr(chk, "field_name", "") or "")
        si = self.ctx.station_instance_id or ""
        actual = self.outputs.get(si, source, field)
        if actual is None:
            outs = self.outputs.step_outputs(si, source)
            if field not in outs:
                return False, f"前序步骤 {source!r} 没有输出字段 {field!r}"
        ok = _compare_value(actual, getattr(chk, "op", None), getattr(chk, "value", None))
        if not ok:
            return False, (f"前序步骤 {source!r}.{field} 实际={actual!r}，"
                           f"要求 {getattr(chk, 'op', None)} {getattr(chk, 'value', None)!r}")
        return True, ""


def _compare_value(actual: Any, op: Any, expected: Any) -> bool:
    """与 condition 同语义的结构化比较（不收字符串求值器）。"""
    try:
        op_enum = op if isinstance(op, ConditionOp) else ConditionOp(str(op))
    except (ValueError, TypeError):
        return False
    fn = _OPS.get(op_enum)
    if fn is None:
        return False
    try:
        return bool(fn(actual, expected))
    except Exception:  # noqa: BLE001 - 类型不匹配=不通过, 不抛
        return False


def make_post_check_node(step, *, ctx, executor, outputs):
    """为带 ``post_checks`` 的步骤构造后置判断节点（编译器插在步骤后、确认闸前）。"""
    checks = tuple(getattr(step, "post_checks", ()) or ())
    if not checks:
        return None
    return PostCheckNode(f"check:{step.id}", checks=checks,
                         target_step_id=step.id, ctx=ctx,
                         executor=executor, outputs=outputs)


class EvidenceNode(BaseStepNode):
    """record.evidence: 汇总上游证据引用; 达到保存确认才 SUCCESS."""

    def __init__(self, name, *, inputs: tuple[str, ...], **kw) -> None:
        super().__init__(name, **kw)
        self.inputs = tuple(inputs)

    def _tick(self, entry) -> Status:
        si = self.ctx.station_instance_id or ""
        refs = {}
        for ref in self.inputs:
            outs = self.outputs.step_outputs(si, ref)
            if outs:
                refs[ref] = outs.get("capture_id") or outs.get("status") or True
            else:
                return self._mark_failure(f"证据引用 {ref!r} 无输出")
        records = getattr(self.executor, "records", None)
        if records is not None:
            records.checkpoint(self.ctx.run_id,
                               f"{si}:{self.step_id}:it{self.ctx.iteration}", refs)
        return self._mark_success(refs)


class CustomCallNode(BaseStepNode):
    """custom.call（自定义操作）: 选一个已注册的自定义方法，可对**多台设备同时下发、等全部完成**。

    方法的**安全边界**（需要运动/机构/是否允许离线）由方法库声明，并在
    ``ActionExecutor.run_custom_method`` 里执行期强制；本节点只负责调用与汇报。
    多设备语义: 一次性把所有设备的指令下发（issue），再统一等待全部完成（wait-all）,
    任一设备失败则本步失败（不掩饰部分失败）。
    """

    def __init__(self, name, *, method, device_refs: tuple[str, ...] = (),
                 params: dict[str, Any] | None = None, **kw) -> None:
        super().__init__(name, **kw)
        self.method = method
        self.device_refs = tuple(device_refs)
        self.params = dict(params or {})

    def _tick(self, entry) -> Status:
        fn = getattr(self.executor, "run_custom_method", None)
        if not callable(fn):
            return self._mark_failure("执行栈未接入自定义方法执行器")
        outcome = fn(self.ctx, self.method, self.device_refs, self.params)
        si = self.ctx.station_instance_id or ""
        self.outputs.set(si, self.step_id, "method", self.method.method_id)
        self.outputs.set(si, self.step_id, "detail", outcome.detail)
        if outcome.ok:
            return self._mark_success({"method": self.method.method_id,
                                       "detail": outcome.detail})
        return self._mark_failure(
            f"自定义方法 {self.method.method_id} 失败: {outcome.detail}")


# ---------- 工厂 (叶子节点) ----------
def _effective_pipeline(step, detections: dict[str, str] | None) -> str | None:
    """解析 vision.run 的检测来源: 直接 pipeline_ref 优先, 否则由 detection_ref 查表。"""
    pipe = (getattr(step, "pipeline_ref", None) or "").strip()
    if pipe:
        return pipe
    det = (getattr(step, "detection_ref", None) or "").strip()
    if det and detections:
        return detections.get(det)
    return None


def build_node(step, *, ctx: RunContext, executor: Any, outputs: OutputStore,
               joint_targets: dict | None = None, motion: Any = None,
               detections: dict[str, str] | None = None,
               methods: Any = None,
               vision_step_ref: str | None = None) -> BaseStepNode:
    """按固定 node type 构造**叶子**节点; 未知类型直接拒绝。

    运动步骤自带 ``joint_target_ref``：从 ``joint_targets``（已登记示教点）按步解析；
    未绑定运动方案且示教点缺失时**直接失败**（不在编译期伪造默认位）。

    subflow / repeat 是复合结构, 由 compiler 展开 (见 workflow/subflows.py,
    workflow/bounded_loop.py), 此处显式拒绝, 避免被当成叶子误处理。
    """
    common = dict(step_id=step.id, ctx=ctx, executor=executor, outputs=outputs)
    t = step.type
    if t is NodeType.ROBOT_MOVE_JOINT:
        plan_ref = (getattr(step, "motion_plan_ref", None) or "").strip()
        if plan_ref:
            # 绑定命名「运动方案」: 回放方案路点, 不再用单一示教点
            return MovePlanNode(f"move:{step.id}", motion=motion, plan_id=plan_ref,
                                **common)
        ref = (getattr(step, "joint_target_ref", None) or "").strip()
        target = (joint_targets or {}).get(ref)
        if target is None:
            raise ValidationFailure(
                f"步骤 {step.id} 的示教点 {ref!r} 未注册"
                f"（运动步骤需 joint_target_ref 或 motion_plan_ref）")
        return MoveJointNode(f"move:{step.id}", target=target,
                             target_ref=ref, **common)
    if t is NodeType.ROBOT_MOVE_BY_BOLT_SIZE:
        plan_ref = (getattr(step, "motion_plan_ref", None) or "").strip()
        if not plan_ref:
            raise ValidationFailure(
                f"步骤 {step.id} 未绑定运动方案（robot.move_by_bolt_size 必须选方案）")
        mapping = {int(k): int(v)
                   for k, v in (getattr(step, "bolt_size_waypoints", None) or {}).items()}
        return MoveByBoltSizeNode(
            f"move_bs:{step.id}", motion=motion, plan_id=plan_ref,
            size_map=mapping,
            via_waypoint=getattr(step, "bolt_size_via_waypoint", None),
            operation_config=getattr(executor, "operation_config", None),
            **common)
    if t is NodeType.CUSTOM_CALL:
        method = None
        if methods is not None:
            method = methods.find(step.method_ref or "")
        if method is None:
            raise ValidationFailure(
                f"步骤 {step.id} 的自定义方法 {step.method_ref!r} 未注册")
        return CustomCallNode(f"custom:{step.id}", method=method,
                              device_refs=tuple(step.device_refs),
                              params=dict(step.params or {}), **common)
    if t is NodeType.MECHANISM_ACTION:
        return MechanismActionNode(f"mech:{step.id}", action_id=step.action_ref, **common)
    if t is NodeType.CAMERA_CAPTURE:
        return CaptureNode(f"capture:{step.id}", camera_ids=(step.camera_ref,), **common)
    if t is NodeType.VISION_RUN:
        return VisionRunNode(f"vision:{step.id}",
                             pipeline_id=_effective_pipeline(step, detections),
                             input_step=step.input_from["step"],
                             detection_ref=getattr(step, "detection_ref", None),
                             **common)
    if t is NodeType.GUIDED_POSITION:
        return GuidedPositionNode(
            f"guide:{step.id}", motion=motion,
            camera_ref=getattr(step, "camera_ref", None) or "",
            stages=tuple(getattr(step, "guide_stages", None) or ("coarse", "fine")),
            speed_pct=getattr(step, "guide_speed_pct", None),
            max_offset_mm=float(getattr(step, "guide_max_offset_mm", 0.0) or 0.0),
            vision_step_ref=vision_step_ref,
            fine_profile_ref=getattr(step, "fine_profile_ref", None),
            **common)
    if t is NodeType.CONDITION:
        return ConditionNode(f"cond:{step.id}", predicate=step.predicate,
                             on_false_policy=step.failure_policy(), **common)
    if t is NodeType.WAIT_STATE:
        return WaitStateNode(f"wait:{step.id}", device_id=step.device_ref or "",
                             field_name=getattr(step, "field_name", None),
                             expected=getattr(step, "expected", None),
                             **common)
    if t is NodeType.OPERATOR_CONFIRM:
        return OperatorConfirmNode(f"confirm:{step.id}", prompt=step.id,
                                   evidence_refs=step.inputs, **common)
    if t is NodeType.RECORD_EVIDENCE:
        return EvidenceNode(f"evidence:{step.id}", inputs=step.inputs, **common)
    if t in (NodeType.SUBFLOW, NodeType.REPEAT):
        raise ValidationFailure(f"步骤 {step.id}: {t.value} 由编译器展开, 不能作叶子构造")
    raise ValidationFailure(f"未知/未支持 node type: {t!r}")


__all__ = [
    "BaseStepNode", "CaptureNode", "ConditionNode", "CustomCallNode", "EvidenceNode",
    "GuidedPositionNode", "MechanismActionNode", "MoveJointNode", "MovePlanNode",
    "OperatorConfirmNode", "OutputStore", "PostCheckNode", "VisionRunNode",
    "WaitStateNode", "build_node", "make_post_check_node",
]
