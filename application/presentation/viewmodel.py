"""「作业台」页 的 ViewModel (A18, 12 §5, 03 §4/§5；曾名"工位任务台")。

纯 Python, 无 Qt 依赖 -> 可在离线测试直接验证按钮矩阵/阻止原因/停止摘要/来源标识.
UI 只调用这些方法发"意图", 不直接触碰设备或运行算法.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from application.contracts.enums import StopScope, TaskState

ACTIVE_STATES = (
    TaskState.RUNNING, TaskState.PAUSE_REQUESTED, TaskState.PAUSED,
    TaskState.WAITING_OPERATOR, TaskState.STOPPING,
)
STARTABLE_STATES = (
    TaskState.IDLE, TaskState.COMPLETED, TaskState.STOPPED, TaskState.FAULT,
)


@dataclass
class StepView:
    station_id: str
    step_id: str
    status: str = "PENDING"


@dataclass
class RowView:
    """通用两列/三列行 (设备/停止摘要用)."""

    left: str
    right: str
    tone: str = "normal"  # normal / ok / warn / bad


@dataclass
class WorkbenchState:
    run_id: str | None
    task_state: str
    recipe_id: str | None
    recipe_version: str | None
    recipes: list[str] = field(default_factory=list)
    selected_recipe: str | None = None
    allow_offline_draft: bool = False
    stations: list[str] = field(default_factory=list)
    steps: list[StepView] = field(default_factory=list)
    image_source: str = "NONE"           # CURRENT / HISTORY / REPLAY / NONE
    blocking_reasons: list[str] = field(default_factory=list)
    buttons: dict[str, bool] = field(default_factory=dict)
    stop_summary: list[RowView] = field(default_factory=list)
    devices: list[RowView] = field(default_factory=list)
    disabled_devices: list[str] = field(default_factory=list)
    unknown_action_count: int = 0
    hint: str = ""
    hint_tone: str = "ok"


class WorkbenchViewModel:
    """作业台视图模型. 只发意图; 服务层仍做权限/互锁校验."""

    def __init__(self, *, task, records=None, disabled_devices=(),
                 recipes: list[str] | None = None,
                 allow_offline_draft: bool = False,
                 default_recipe: str | None = None) -> None:
        self._task = task
        self._records = records
        self._disabled = list(disabled_devices)
        self.selected_recipe: str | None = None
        self.allow_offline_draft = allow_offline_draft
        self._default_recipe = default_recipe
        self._hint = ""
        self._hint_tone = "ok"
        self._recipes_override = recipes
        self._auto_select_first()

    # ---------- 配方 ----------
    def _auto_select_first(self) -> None:
        """默认选中**启动时载入的执行方案**（DEC-041）；不在目录里才退回第一条。"""
        options = self.recipes()
        if self.selected_recipe in options and self.selected_recipe is not None:
            return
        if self._default_recipe in options:
            self.selected_recipe = self._default_recipe
            return
        if options:
            self.selected_recipe = options[0]

    def recipes(self) -> list[str]:
        if self._recipes_override is not None:
            return list(self._recipes_override)
        fn = getattr(self._task, "available_recipes", None)
        if callable(fn):
            try:
                return list(fn())
            except Exception:  # noqa: BLE001
                return []
        return []

    def select_recipe(self, recipe_id: str | None) -> None:
        self.selected_recipe = recipe_id

    def is_task_active(self) -> bool:
        """任务进行中（RUNNING/暂停/停止中…）：此时不允许换执行方案（DEC-041）。"""
        try:
            return bool(self._task.state_machine.is_active)
        except Exception:  # noqa: BLE001
            return False

    def recipe_in_use(self, recipe_id: str) -> bool:
        """该执行方案是否正被**进行中的任务**使用（删除前保护，DEC-108）。"""
        if not self.is_task_active():
            return False
        run_ctx = getattr(self._task, "run_ctx", None)
        return bool(run_ctx and run_ctx.versions.recipe_id == recipe_id)

    def forget_recipe(self, recipe_id: str) -> None:
        """执行方案被删除后：若它正被选中，回退到第一条可用方案（DEC-108）。"""
        if self.selected_recipe == recipe_id:
            self.selected_recipe = None
            self._auto_select_first()

    # ---------- 校验/能否启动 ----------
    def blocking_reasons(self) -> list[str]:
        """明确的阻止原因; 未选配方时给可操作提示, 不产生空 id 的噪声报错."""
        if not self.selected_recipe:
            return ["未选择执行方案"]
        try:
            return list(self._task.validate(
                self.selected_recipe, allow_draft=self.allow_offline_draft))
        except Exception as exc:  # noqa: BLE001
            return [f"校验失败: {exc}"]

    def can_start(self) -> bool:
        return (bool(self.selected_recipe)
                and self._task.state_machine.state in STARTABLE_STATES
                and not self.blocking_reasons())

    # ---------- 视图状态 ----------
    def state(self, *, image_source: str = "NONE") -> WorkbenchState:
        sm = self._task.state_machine
        steps: list[StepView] = []
        compiled = getattr(self._task, "compiled", None)
        # 任务已停止/故障时, 仍处 RUNNING 的节点在界面上显示为"已停止", 不误报为运行中
        stopping = sm.state in (TaskState.STOPPED, TaskState.FAULT)
        if compiled is not None:
            for name, node in compiled.nodes.items():
                # 无工位后节点名 = "<run作用域>/<step_id>"; 只取步骤名展示
                _, _, step_id = name.partition("/")
                status = node.status.name
                if stopping and status == "RUNNING":
                    status = "STOPPED"
                steps.append(StepView(station_id="", step_id=step_id, status=status))
        return WorkbenchState(
            run_id=sm.run_id or None,
            task_state=sm.state.value,
            recipe_id=(self._task.run_ctx.versions.recipe_id if self._task.run_ctx
                       else None),
            recipe_version=(self._task.run_ctx.versions.recipe_version
                            if self._task.run_ctx else None),
            recipes=self.recipes(),
            selected_recipe=self.selected_recipe,
            allow_offline_draft=self.allow_offline_draft,
            stations=[], steps=steps, image_source=image_source,
            blocking_reasons=self.blocking_reasons(),
            buttons=self.buttons(),
            stop_summary=self._stop_summary(),
            devices=self._devices(),
            disabled_devices=list(self._disabled),
            unknown_action_count=len(self._task.ledger.unknown_entries()),
            hint=self._hint, hint_tone=self._hint_tone,
        )

    def buttons(self) -> dict[str, bool]:
        """按钮矩阵 (DEC-041): 由状态机 + 配方可启动性共同决定; 停止入口始终保留。

        - 运行中: 暂停 / 终止流程 可点（PAUSE_REQUESTED 只是瞬时态, PAUSED 后可继续）;
        - 停止态 (IDLE/COMPLETED/STOPPED/FAULT): 启动 与 **单步** 都可点
          （单步 = 起一个新 run 只推进一步后停在安全边界）;
        - 暂停后: 继续作业 / 单步 可点。
        """
        st = self._task.state_machine.state
        can_start = st in STARTABLE_STATES and self.can_start()
        return {
            "start": can_start,
            "single_step": (st in (TaskState.READY, TaskState.RUNNING, TaskState.PAUSED)
                            or can_start),
            "fault_recover": st is TaskState.FAULT,
            "request_pause": st is TaskState.RUNNING,
            "resume": st in (TaskState.PAUSED, TaskState.WAITING_OPERATOR),
            "terminate": st in ACTIVE_STATES,
            "stop_all": st in ACTIVE_STATES,
        }

    def _stop_summary(self) -> list[RowView]:
        coord = getattr(self._task, "stop_coordinator", None)
        if coord is None:
            return []
        summary = None
        for _sid, s in reversed(list(getattr(coord, "_active", {}).items())):  # noqa: SLF001
            summary = s
            break
        if summary is None:
            return []
        tone = {"CONFIRMED": "ok", "STOPPING": "warn",
                "UNKNOWN": "bad", "FAILED": "bad"}
        return [
            RowView(left=eid, right=rep.state.name,
                    tone=tone.get(rep.state.name, "normal"))
            for eid, rep in summary.executors.items()
        ]

    def _devices(self) -> list[RowView]:
        out: list[RowView] = []
        robot = getattr(getattr(self._task, "executor", None), "robot", None)
        if robot is not None:
            st = robot.read_state()
            out.append(RowView(left="eco65_01", right=f"机器人 {st.get('operational', '-')}"))
        cams = getattr(getattr(self._task, "executor", None), "cameras", {}) or {}
        for cid, cam in cams.items():
            s = cam.status()
            state = "已打开" if s.get("open") else "未打开"
            out.append(RowView(left=cid, right=f"相机 {state}"))
        return out

    # ---------- 意图 (UI -> 服务) ----------
    def start_current(self) -> str:
        if not self.selected_recipe:
            raise RuntimeError("未选择执行方案")
        # DEC-042: 作业台启动的作业在每个步骤后都需人工确认（其它入口默认不弹）
        run_id = self._task.start(self.selected_recipe,
                                  allow_draft=self.allow_offline_draft,
                                  step_confirm=True)
        self._set_hint(f"已启动 {self.selected_recipe} (run={run_id})", "ok")
        return run_id

    # ---------- 步骤完成确认 (DEC-042) ----------
    def _step_gates(self) -> list:
        compiled = getattr(self._task, "compiled", None)
        return list(getattr(compiled, "gates", None) or ())

    def pending_step_prompt(self) -> dict | None:
        """当前是否有步骤执行完在等人工确认（弹框内容）；无则 None。"""
        for gate in self._step_gates():
            try:
                pending = getattr(gate, "pending_prompt", None)
            except Exception:  # noqa: BLE001 - 无此属性/求值异常都当"没在等"
                continue
            if isinstance(pending, dict) and pending.get("options"):
                return pending
        return None

    def confirm_step(self, choice: str, operator: str = "operator") -> bool:
        """提交步骤确认选择；仅接受闸**已声明**的选项（确认/不正确）。

        「不正确」由服务层在下一个 tick 把作业停在安全边界（PAUSED），
        这里不直接改状态，避免绕过状态机。
        """
        prompt = self.pending_step_prompt()
        if not prompt or choice not in prompt.get("options", ()):
            self._set_hint("确认未送达（提示已结束或选项不在允许列表）", "bad")
            return False
        for gate in self._step_gates():
            try:
                pending = getattr(gate, "pending_prompt", None)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(pending, dict) or not pending.get("options"):
                continue
            provider = getattr(gate, "provide", None)
            if not callable(provider):
                continue
            ok = bool(provider(choice, operator))
            if ok:
                if choice == "不正确":
                    self._set_hint("已判定该步骤不正确，作业将在安全边界暂停", "warn")
                else:
                    self._set_hint(f"已确认步骤「{prompt.get('step_title') or ''}」正确，继续",
                                   "ok")
            return ok
        return False

    # ---------- 右侧图卡 / 手动检测圆 (DEC-042) ----------
    def current_frame(self, camera_id: str | None = None):
        """本 run 最近一次抓拍帧（跨 run 的旧帧不显示）。

        ``camera_id`` 给定时返回**该相机**本 run 的最近帧（DEC-051 多相机页签）；
        没抓拍过返回 None。
        """
        if camera_id:
            return self.current_frames().get(camera_id)
        ex = getattr(self._task, "executor", None)
        if ex is None:
            return None
        run_id = getattr(self._task.state_machine, "run_id", "") or None
        if getattr(ex, "last_capture_run_id", None) != run_id:
            return None
        return getattr(ex, "last_frame", None)

    def current_frames(self) -> dict[str, Any]:
        """本 run 内每台相机最近一次抓拍帧（camera_id -> FrameBundle）。

        DEC-051：执行器按相机累积 ``camera_frames``；这里按本次 run 过滤，
        上一轮 run 的帧不返回（与 ``current_frame`` 同口径）。
        """
        ex = getattr(self._task, "executor", None)
        if ex is None:
            return {}
        run_id = getattr(self._task.state_machine, "run_id", "") or None
        if getattr(ex, "last_capture_run_id", None) != run_id:
            return {}
        frames = getattr(ex, "camera_frames", None) or {}
        return {cid: frame for cid, frame in frames.items()
                if getattr(frame, "run_id", None) == run_id}

    def current_detection(self):
        """本 run 最近一次视觉结果（跨 run 的旧结果不显示）。"""
        ex = getattr(self._task, "executor", None)
        if ex is None:
            return None
        run_id = getattr(self._task.state_machine, "run_id", "") or None
        if getattr(ex, "last_detection_run_id", None) != run_id:
            return None
        return getattr(ex, "last_detection", None)

    def _pending_vision_node(self):
        """待确认的那一步若是视觉步骤，返回其节点；否则 None。"""
        prompt = self.pending_step_prompt()
        if not prompt:
            return None
        compiled = getattr(self._task, "compiled", None)
        finder = getattr(compiled, "find_step", None)
        if not callable(finder):
            return None
        try:
            node = finder(prompt.get("step_id") or "")
        except Exception:  # noqa: BLE001
            return None
        return node if callable(getattr(node, "circle_state", None)) else None

    def step_circle_state(self) -> dict:
        """待确认视觉步骤的检测圆状态（可否拖动、现有圆、当前判定）。"""
        node = self._pending_vision_node()
        if node is None:
            return {"available": False, "reason": "当前待确认步骤不是可微调的视觉步骤"}
        try:
            return dict(node.circle_state())
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": f"读取检测圆失败: {exc}"}

    def apply_step_circle(self, cx, cy, radius_px,
                          operator: str = "operator") -> dict:
        """以手动拖动的圆为准重算当前视觉步骤的判定（DEC-042）。"""
        node = self._pending_vision_node()
        if node is None:
            raise RuntimeError("当前没有待确认的视觉步骤，不能调整检测圆")
        info = node.apply_manual_circle(cx, cy, radius_px, operator)
        tone = "ok" if info.get("status") == "PASS" else "warn"
        self._set_hint(
            f"已按手动检测圆判定：{info.get('status')}（半径 {info.get('radius_px'):.0f}px）",
            tone)
        return info

    # ---------- 多框/多圆（DEC-048） ----------
    def _layout_node(self):
        node = self._pending_vision_node()
        if node is None or not callable(getattr(node, "apply_layout", None)):
            raise RuntimeError("当前没有待确认的视觉步骤，不能调整框/圆")
        return node

    def apply_step_layout(self, boxes, selected, operator: str = "operator") -> dict:
        """以人工调整的多框/多圆布局为准重算判定（仅本次运行有效）。"""
        node = self._layout_node()
        info = node.apply_layout(boxes, selected, operator)
        tone = "ok" if info.get("status") == "PASS" else "warn"
        self._set_hint(
            f"已按手动框/圆判定：{info.get('status')}"
            f"（{info.get('boxes')} 个框 / {info.get('circles')} 个圆）", tone)
        return info

    def add_step_box(self, x1, y1, x2, y2, operator: str = "operator") -> dict:
        """人工新画一个框（框内按检测配置找圆）。"""
        node = self._layout_node()
        info = node.add_box(x1, y1, x2, y2, operator)
        tone = "ok" if info.get("status") == "PASS" else "warn"
        why = info.get("message") or ""
        self._set_hint(f"已新增框：{why}" if why else "已新增框", tone)
        return info

    def set_step_circle(self, box_index, cx, cy, radius_px,
                        operator: str = "operator") -> dict:
        """人工在某个框内画/调整圆，并以该圆为定位圆。"""
        node = self._layout_node()
        info = node.set_box_circle(box_index, cx, cy, radius_px, operator)
        tone = "ok" if info.get("status") == "PASS" else "warn"
        self._set_hint(
            f"已按框 {int(box_index) + 1} 的圆判定：{info.get('status')}", tone)
        return info

    def select_step_circle(self, box_index,
                           operator: str = "operator") -> bool:
        """操作员指定哪个框的圆作为后续引导定位的定位圆（不重算判定）。

        ``box_index=None`` = 不指定（引导定位将重新找圆）。带 ``operator`` 的
        **显式选择**会记一条 ``vision.select_target``（DEC-054）供追溯。
        """
        node = self._pending_vision_node()
        if node is None or not callable(getattr(node, "select_box", None)):
            return False
        changed = bool(node.select_box(box_index, operator))
        if changed and operator:
            state = self.step_circle_state()
            boxes = state.get("boxes") or []
            circle = None
            if box_index is not None and 0 <= int(box_index) < len(boxes):
                circle = boxes[int(box_index)].get("circle")
            if circle:
                self._set_hint(
                    f"已指定定位圆：框{int(box_index) + 1} "
                    f"({circle.get('cx', 0):.0f}, {circle.get('cy', 0):.0f})", "ok")
            else:
                self._set_hint("已取消指定：引导定位将重新找圆", "warn")
        return changed

    # ---------- 引导定位：每段人工确认（DEC-033） ----------
    def pending_operator_prompt(self) -> dict | None:
        """运行中是否有步骤在等人工确认（当前=引导定位每段确认）。

        节点接口: ``pending_prompt`` 属性（dict|None）+ ``provide(choice, operator)``。
        与 ``operator.confirm`` 节点语义一致: **受限选项**，不提供自由文本。
        """
        compiled = getattr(self._task, "compiled", None)
        nodes = getattr(compiled, "nodes", None) or {}
        for node in nodes.values():
            try:
                pending = getattr(node, "pending_prompt", None)
            except Exception:  # noqa: BLE001 - 其他节点无此属性/求值异常都当"没在等"
                continue
            if isinstance(pending, dict) and pending.get("options"):
                return pending
        return None

    def provide_operator(self, choice: str, operator: str = "operator") -> bool:
        """把运行页的选择提交给等待中的节点；仅接受该提示**已声明**的选项。"""
        compiled = getattr(self._task, "compiled", None)
        nodes = getattr(compiled, "nodes", None) or {}
        for node in nodes.values():
            try:
                pending = getattr(node, "pending_prompt", None)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(pending, dict) or not pending.get("options"):
                continue
            provider = getattr(node, "provide", None)
            if not callable(provider) or choice not in pending["options"]:
                return False
            ok = bool(provider(choice, operator))
            self._set_hint(f"已提交确认「{choice}」" if ok else "确认未送达（提示已结束）",
                           "ok" if ok else "bad")
            return ok
        return False

    def guide_circle_state(self) -> dict:
        """当前/刚完成的引导定位检测圆与画面（DEC-084，供作业台右卡画圆）。

        仅在引导阶段（检测完成待确认 / 移动中）返回 ``active=True``；引导结束后
        不把旧圆留在右卡上。
        """
        motion = getattr(self._task, "motion", None)
        state = getattr(motion, "state", None)
        if not isinstance(state, dict):
            return {"available": False}
        result = state.get("last_guide")
        frame = state.get("last_guide_frame")
        circle = getattr(result, "circle", None) if result is not None else None
        if frame is None or circle is None:
            return {"available": False}
        run_id = getattr(getattr(self._task, "state_machine", None), "run_id", "") or None
        frame_run = getattr(frame, "run_id", None)
        if run_id and frame_run and frame_run != run_id:
            return {"available": False}
        active = state.get("mode") == "guide" or bool(state.get("guide_phase"))
        try:
            return {
                "available": True,
                "active": bool(active),
                "frame": frame,
                "stage": str(state.get("guide_stage") or ""),
                "circle": {"cx": float(circle.cx), "cy": float(circle.cy),
                           "radius_px": float(circle.radius)},
            }
        except (TypeError, ValueError, AttributeError):
            return {"available": False}

    def tick(self) -> str:
        return self._task.tick().value

    def request_pause(self) -> str:
        """暂停 = 请求 + 在安全边界落定（DEC-041：否则永远停在"正在暂停"）。"""
        s = self._task.request_pause().value
        self._set_hint("已暂停（安全边界）", "warn")
        return s

    def confirm_pause(self, safe_boundary: bool) -> str:
        return self._task.confirm_pause(safe_boundary).value

    def resume(self, reconciled: bool = True) -> str:
        s = self._task.resume(reconciled).value
        self._set_hint("已继续", "ok")
        return s

    def single_step(self) -> str:
        # DEC-042: 与启动作业同一套步骤确认（单步也只推进一步）
        s = self._task.single_step(
            self.selected_recipe, allow_draft=self.allow_offline_draft,
            step_confirm=True).value
        self._set_hint(f"单步后状态: {s}", "ok")
        return s

    # ---------- 故障恢复 (DEC-061) ----------
    def fault_info(self) -> dict:
        """FAULT 时给弹窗的信息（失败步骤/原因/UNKNOWN/隔离/能否原地恢复）。"""
        fn = getattr(self._task, "fault_info", None)
        return dict(fn()) if callable(fn) else {}

    def resume_from_fault(self) -> str:
        """从失败步骤原地恢复（同一 run）；不再要求人工说明/勾选（DEC-104）。"""
        s = self._task.resume_from_fault().value
        self._set_hint("已从失败步骤恢复，继续执行", "ok")
        return s

    def restart_task(self) -> str:
        """故障后重新执行整个任务（新 run，从头开始）。"""
        if not self.selected_recipe:
            raise RuntimeError("未选择执行方案")
        run_id = self._task.start(
            self.selected_recipe, allow_draft=self.allow_offline_draft,
            step_confirm=True, recovery=True)
        self._set_hint(f"已重新执行整个任务 (run={run_id})", "ok")
        return run_id

    def terminate(self) -> list[dict]:
        return self._stop(StopScope.TASK_TERMINATE, "终止流程")

    def stop_all(self) -> list[dict]:
        return self._stop(StopScope.STOP_ALL, "停止全部")

    def _stop(self, scope: StopScope, reason: str) -> list[dict]:
        summary = self._task.terminate(reason, scope)
        rows = [{"executor": eid, "state": rep.state.name}
                for eid, rep in summary.executors.items()]
        bad = [r["executor"] for r in rows if r["state"] != "CONFIRMED"]
        if bad:
            self._set_hint(f"{reason}: 未确认 {', '.join(bad)}", "bad")
        else:
            self._set_hint(f"{reason}: 全部已确认", "ok")
        return rows

    def _set_hint(self, text: str, tone: str) -> None:
        self._hint = text
        self._hint_tone = tone

    @property
    def hint(self) -> str:
        return self._hint

    @property
    def hint_tone(self) -> str:
        return self._hint_tone

    @property
    def stop_requires_no_confirmation(self) -> bool:
        """停止请求不弹确认阻挡 (12 §5)."""
        return True

    def terminate_scope_note(self) -> str:
        return "终止流程: 停止本任务相关动作, 不自动回零/松夹; 停止全部: 停止所有执行器."


__all__ = ["RowView", "StepView", "WorkbenchState", "WorkbenchViewModel"]
