"""TaskService: 任务校验/启动/暂停/继续/单步/终止 (12 §4.2, 02 §2/§3, A13/A14).

TaskService 是唯一任务状态权威; 生产 tick 只在 RUNNING 下推进;
根完成结束该 run 的生产 tick, 新任务生成新 run_id; BT 失败不当物理停稳.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from py_trees.common import Status

from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import (
    FailureClass,
    FailurePolicy,
    SourceKind,
    StopScope,
    TaskState,
)
from application.services.recipes.models import RecipeModel
from application.workflow.compiler import CompiledRun, compile_recipe
from application.workflow.ledger import ExecutionLedger
from application.workflow.state import TaskStateMachine
from application.infrastructure import logging_setup
from application.infrastructure.events import Event, Severity

_log = logging_setup.get_logger("services.task")


class TaskError(RuntimeError):
    pass


# 可以从这些状态起一个新 run（与 state._ALLOWED 中 ->VALIDATING 的来源一致；
# 界面层 viewmodel 有同名常量，服务层不反向依赖界面，故各自定义）
STARTABLE_STATES = (
    TaskState.IDLE, TaskState.COMPLETED, TaskState.STOPPED, TaskState.FAULT,
)


@dataclass
class TaskService:
    """生产任务的唯一调度与状态所有者."""

    state_machine: TaskStateMachine
    recipe_service: Any
    executor: Any
    ledger: ExecutionLedger = field(default_factory=ExecutionLedger)
    records: Any = None
    stop_coordinator: Any = None
    joint_targets: dict = field(default_factory=dict)
    subflows: Any = None            # SubflowRegistry (E13)
    motion: Any = None              # MotionRunner (运动方案绑定步由它回放)
    methods: Any = None             # MethodRegistry (自定义操作 custom.call)
    _run_counter: itertools.count = field(default_factory=lambda: itertools.count(1))
    compiled: CompiledRun | None = None
    run_ctx: RunContext | None = None
    revision: str | None = None

    # ---------- 校验/启动 ----------
    def available_recipes(self) -> list[str]:
        """可用配方 id 列表 (草稿 + 已发布), 供 UI 选择."""
        if self.recipe_service is None:
            return []
        ids = set()
        fn = getattr(self.recipe_service, "drafts", None)
        if callable(fn):
            ids.update(fn())
        published = getattr(self.recipe_service, "_published", {})  # noqa: SLF001
        ids.update(published.keys())
        return sorted(ids)

    def validate(self, recipe_id: str, *, allow_draft: bool = False) -> list[str]:
        problems: list[str] = []
        if self.recipe_service is None:
            return ["无执行方案服务"]
        try:
            model = self.recipe_service.get_draft(recipe_id)
        except Exception as exc:  # noqa: BLE001
            return [f"无草稿 {recipe_id!r}: {exc}"]
        report = self.recipe_service.validate(recipe_id)
        problems += report.errors
        if model.state != "published" and not allow_draft:
            problems.append("执行方案未发布 (草稿不能生产执行, 除非显式 allow_draft)")
        return problems

    def start(self, recipe_id: str, *, revision: str | None = None,
              source: SourceKind = SourceKind.WORKFLOW,
              allow_draft: bool = False,
              step_confirm: bool = False,
              recovery: bool = False) -> str:
        problems = self.validate(recipe_id, allow_draft=allow_draft)
        if problems:
            raise TaskError("启动校验失败: " + "; ".join(problems))
        if recovery:
            # DEC-104：故障后「重新执行整个任务」——不再要求人工处理说明，
            # 选择即自动清 UNKNOWN/隔离并结算旧 run 的在途租约，再起新 run。
            if self.state_machine.state is not TaskState.FAULT:
                raise TaskError("只有 FAULT 状态才能使用故障恢复重新执行")
            self._clear_recovery_blocks(settle_leases=True)
        elif self.state_machine.state is TaskState.FAULT:
            # 不允许绕过故障恢复：有 UNKNOWN/隔离资源时，必须先走作业台
            # 「故障恢复」；没有这些残留时允许直接重新启动。
            interlocks = getattr(self.executor, "interlocks", None)
            fn = getattr(interlocks, "quarantined", None)
            quarantined = dict(fn()) if callable(fn) else {}
            unknown = self.ledger.unknown_entries() if self.ledger else []
            if unknown or quarantined:
                raise TaskError(
                    "故障状态还有未核实执行(UNKNOWN)或被隔离资源；"
                    "请使用「故障恢复」后再继续")
        model = self._resolve_recipe(recipe_id, revision)
        run_id = f"run-{next(self._run_counter)}"
        self.state_machine.run_id = run_id
        self.state_machine.begin_validation("启动校验")
        self.state_machine.mark_ready()
        versions = VersionSnapshot(
            recipe_id=recipe_id, recipe_version=self.revision or model.version,
            config_hash=self.recipe_service.running_hash(recipe_id, self.revision)
            if self.revision else model.version,
            calibration_hash="unset",
        )
        self.run_ctx = RunContext(run_id=run_id, versions=versions)
        self.ledger = ExecutionLedger()
        self.executor.ledger = self.ledger
        self.compiled = compile_recipe(model, self.run_ctx, self.executor,
                                       self.joint_targets, subflows=self.subflows,
                                       motion=self.motion,
                                       detections=self._detection_map(),
                                       methods=self.methods,
                                       step_confirm=step_confirm)
        self.compiled.tree.setup()
        self.state_machine.start()
        if self.records is not None:
            self.records.start_run(run_id, recipe_id=recipe_id,
                                   recipe_version=versions.recipe_version,
                                   config_hash=versions.config_hash,
                                   calibration_hash=versions.calibration_hash)
        self._emit("task.start", Severity.INFO, f"启动 {recipe_id}")
        if recovery:
            self._emit("task.recovery", Severity.WARNING,
                       "故障后重新执行整个任务（新 run）")
        return run_id

    def _emit(self, event_type: str, severity: Severity, message: str) -> None:
        if self.records is None:
            return
        try:
            self.records.append_event_nonblocking(Event(
                event_type=event_type, severity=severity, message=message,
                run_id=self.state_machine.run_id,
            ))
        except Exception:  # noqa: BLE001 - 事件失败不影响调度
            pass

    def _detection_map(self) -> dict[str, str]:
        """命名视觉检测映射 (由配方服务的引用目录提供); 无则空表。"""
        fn = getattr(self.recipe_service, "detection_map", None)
        return fn() if callable(fn) else {}

    def _resolve_recipe(self, recipe_id: str, revision: str | None) -> RecipeModel:
        if revision is not None:
            self.revision = revision
            for rev, model, _h in self.recipe_service._published.get(recipe_id, []):  # noqa: SLF001
                if rev == revision:
                    return model
            raise TaskError(f"未找到发布版本 {recipe_id}@{revision}")
        try:
            return self.recipe_service.get_draft(recipe_id)
        except Exception as exc:  # noqa: BLE001
            raise TaskError(str(exc)) from exc

    # ---------- tick ----------
    def tick(self) -> TaskState:
        """推进一次生产 tick (仅 RUNNING). 根完成结束该 run, 不自动重启."""
        if self.state_machine.state is not TaskState.RUNNING or self.compiled is None:
            return self.state_machine.state
        self.executor.advance()
        try:
            self.compiled.tree.tick()
        except Exception as exc:  # noqa: BLE001
            # 记录/存储失败等阻止新动作 -> FAULT, 保留在途未知与停止处置
            # DEC-052：异常带 traceback 入日志，便于定位，不只留一句消息。
            _log.exception("执行期异常，任务转 FAULT: %s", exc,
                           extra={"run_id": self.state_machine.run_id})
            self.state_machine.fail(FailureClass.RECORD, f"执行期异常: {exc}")
            self._emit("task.fault", Severity.ERROR, f"执行期异常: {exc}")
            self._finish("FAULT")
            return self.state_machine.state
        status = self.compiled.tree.root.status
        # DEC-042: 步骤确认闸收到「不正确」——不按故障处理，在安全边界暂停，
        # 等操作者决定继续（重新确认该步）或终止。
        rejected = self._consume_step_rejection()
        if rejected is not None and self.state_machine.state is TaskState.RUNNING:
            title = rejected.get("title") or rejected.get("step_id") or "步骤"
            self._emit("task.paused", Severity.WARNING,
                       f"步骤「{title}」判定不正确，作业已暂停")
            self.request_pause(f"步骤确认：{title} 判定不正确")
            return self.state_machine.state
        # DEC-064：步骤后判断属性不通过 -> 安全边界 PAUSED（不是设备故障）。
        check_stop = self._consume_check_pause()
        if check_stop and self.state_machine.state is TaskState.RUNNING:
            self._emit("task.paused", Severity.WARNING,
                       f"步骤判断不通过，作业已暂停：{check_stop}")
            self.request_pause(f"步骤判断不通过：{check_stop}")
            return self.state_machine.state
        if status is Status.SUCCESS:
            self.state_machine.complete()
            self._emit("task.completed", Severity.INFO, "全部步骤完成")
            self._finish("COMPLETED")
        elif status is Status.FAILURE:
            self._on_failure()
        return self.state_machine.state

    def _consume_step_rejection(self) -> dict | None:
        """取第一个「不正确」未处理的确认闸（DEC-042）。"""
        gates = getattr(self.compiled, "gates", None) or ()
        for gate in gates:
            fn = getattr(gate, "consume_rejection", None)
            if not callable(fn):
                continue
            info = fn()
            if info:
                return info
        return None

    def _consume_check_pause(self) -> str | None:
        """取第一个未处理的「步骤后判断不通过」原因（DEC-064）。"""
        checks = getattr(self.compiled, "checks", None) or ()
        for node in checks:
            fn = getattr(node, "consume_pause_request", None)
            if not callable(fn):
                continue
            reason = fn()
            if reason:
                return reason
        return None

    def _on_failure(self) -> None:
        node = self.compiled.find_failure() if self.compiled else None
        policy = getattr(node, "failure_policy", None)
        reason = getattr(node, "failure_reason", "") or "步骤失败"
        # DEC-052：日志里带上失败节点/步骤/策略，避免只有一句原因无从定位。
        _log.error("步骤失败: node=%s step_id=%s policy=%s reason=%s",
                   getattr(node, "name", None) or "-",
                   getattr(node, "step_id", None) or "-",
                   getattr(policy, "value", policy) or "-", reason,
                   extra={"run_id": self.state_machine.run_id,
                          "step_id": getattr(node, "step_id", None)})
        if policy in (FailurePolicy.PAUSE_FOR_REVIEW, FailurePolicy.RECAPTURE):
            self.state_machine.request_operator(reason or "需人工复核")
            self._emit("task.waiting_operator", Severity.WARNING, reason)
            return
        self.state_machine.fail(FailureClass.PROCESS, reason)
        self._emit("task.fault", Severity.ERROR, reason)
        self._finish("FAULT")

    def _finish(self, state: str) -> None:
        if self.records is not None and self.run_ctx is not None:
            try:
                self.records.finish_run(self.run_ctx.run_id, state)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 暂停/继续/单步 ----------
    def request_pause(self, reason: str = "",
                      *, confirm_at_boundary: bool = True) -> TaskState:
        """请求暂停；默认**在安全边界落定**为 PAUSED（DEC-041）。

        页面在按钮事件里调用——此刻没有 tick 在执行，动作边界即 tick 边界，
        等价于 ``single_step`` 里的 request_pause+confirm_pause。停定后再点
        「继续作业」才能真的恢复（此前停在 PAUSE_REQUESTED，界面永远灰）。
        在途动作不被打断（真实设备的中途停下仍靠「终止/停止全部」，须现场核实）。
        ``confirm_at_boundary=False`` 保留两段式（先请求、后显式确认）语义。
        """
        self.state_machine.request_pause(reason)
        if confirm_at_boundary:
            self.state_machine.confirm_pause(safe_boundary=True,
                                             reason=reason or "到达安全边界")
        return self.state_machine.state

    def confirm_pause(self, safe_boundary: bool) -> TaskState:
        return self.state_machine.confirm_pause(safe_boundary)

    def resume(self, reconciled: bool = True) -> TaskState:
        return self.state_machine.resume(reconciled)

    def single_step(self, recipe_id: str | None = None, *,
                    allow_draft: bool = False,
                    revision: str | None = None,
                    step_confirm: bool = False) -> TaskState:
        """执行一个明确业务步骤并在安全边界结束 (READY/RUNNING/PAUSED)。

        DEC-041：**停止态也能单步**——IDLE/COMPLETED/STOPPED/FAULT 下先按
        ``start`` 的同一套校验+编译起一个新 run，再推进一次 tick 并暂停；
        校验不过就如实报 TaskError，不绕过发布闸。
        ``step_confirm`` 与 ``start`` 同义（DEC-042，作业台开启步骤确认闸）。

        DEC-101：**同一方案的停止/故障恢复不再从头开始**——若当前 run 就是
        当前选中的方案，单步按「问题步骤」继续（同一 run、换新 attempt），
        已完成步骤/确认闸由账本跳过；只有换方案或没有可用执行树时才新起 run。
        """
        state = self.state_machine.state
        same_run = bool(
            self.compiled is not None and self.run_ctx is not None
            and (not recipe_id
                 or self.run_ctx.versions.recipe_id == recipe_id))
        if state in STARTABLE_STATES:
            if state is TaskState.STOPPED and same_run:
                self.resume_from_stopped()
            elif state is TaskState.FAULT and same_run:
                # DEC-104：不再要求人工说明；选择恢复即自动清 UNKNOWN/隔离。
                self.resume_from_fault()
            else:
                if not recipe_id:
                    raise TaskError("单步需要先选择执行方案")
                self.start(recipe_id, revision=revision, allow_draft=allow_draft,
                           step_confirm=step_confirm)
        elif state is TaskState.READY:
            self.state_machine.start()
        elif state not in (TaskState.RUNNING, TaskState.PAUSED):
            raise TaskError(f"当前 {state.value} 不支持单步")
        elif state is TaskState.PAUSED:
            self.state_machine.resume(reconciled=True)
        self.tick()
        if self.state_machine.state is TaskState.RUNNING:
            self.state_machine.request_pause()
            self.state_machine.confirm_pause(safe_boundary=True)
        return self.state_machine.state

    def resume_from_stopped(self) -> TaskState:
        """STOPPED 后从**当前未完成的步骤**继续（同一 run，DEC-101）。

        与 ``resume_from_fault`` 的区别：停止不一定有 FAILURE 节点，可能是
        当前步骤还在 RUNNING 或被人工停止；找第一个非 SUCCESS 的顶层节点，
        给它换新 attempt（已提交的旧命令不再轮询），整树复位后继续推进。
        已完成的步骤/确认闸靠旧账本 terminal 结果跳过，不重放。
        """
        st = self.state_machine.state
        if st is not TaskState.STOPPED:
            raise TaskError(f"当前 {st.value} 不是 STOPPED，不能从停止步骤继续")
        if self.compiled is None or self.run_ctx is None:
            raise TaskError("没有可继续的执行树，请重新启动任务")
        node = self._first_unfinished_node()
        root = getattr(self.compiled.tree, "root", None)
        if node is not None and root is not None \
                and getattr(node, "parent", None) is not root:
            raise TaskError(
                "当前步骤在复合步骤/子流程/循环内部，无法安全地只继续该步；"
                "请重新执行整个任务")
        if node is not None and node.status is Status.RUNNING \
                and not callable(getattr(node, "arm_retry", None)):
            raise TaskError(
                "当前步骤是复合步骤/循环且不支持原地重试；请重新执行整个任务")
        note = self._clear_recovery_blocks(settle_leases=False)
        attempt = None
        if node is not None:
            arm = getattr(node, "arm_retry", None)
            if callable(arm) and node.status is not Status.INVALID:
                attempt = arm(note)
        try:
            if root is not None:
                root.stop(Status.INVALID)
        except Exception as exc:  # noqa: BLE001
            raise TaskError(f"复位执行树失败: {exc}") from exc
        # 同一 run 复核后继续（不重新编译、不换 run_id）。
        self.state_machine.begin_validation("停止后继续：复核现场")
        self.state_machine.mark_ready("现场已处理，继续执行")
        self.state_machine.start("从停止步骤继续")
        if self.records is not None:
            try:
                self.records.start_run(
                    self.run_ctx.run_id,
                    recipe_id=self.run_ctx.versions.recipe_id,
                    recipe_version=self.run_ctx.versions.recipe_version,
                    config_hash=self.run_ctx.versions.config_hash,
                    calibration_hash=self.run_ctx.versions.calibration_hash,
                )
            except Exception:  # noqa: BLE001 - 记录失败不挡恢复
                pass
        suffix = (f"（第 {attempt} 次尝试）" if attempt is not None else "")
        self._emit("task.recovery", Severity.WARNING,
                   f"从停止步骤继续：{getattr(node, 'step_id', '') or '已完成步骤'}"
                   f"{suffix}；{note}")
        return self.state_machine.state

    def _first_unfinished_node(self):
        """按执行树登记顺序取第一个非 SUCCESS 的顶层节点。"""
        if self.compiled is None:
            return None
        for node in self.compiled.nodes.values():
            if node.status is not Status.SUCCESS:
                return node
        return None

    # ---------- 终止 ----------
    def terminate(self, reason: str = "", scope: StopScope = StopScope.TASK_TERMINATE):
        self.state_machine.request_stop(scope, reason)
        summary = None
        if self.stop_coordinator is not None:
            summary = self.stop_coordinator.request_stop(scope, reason)
        all_confirmed = bool(summary and summary.all_confirmed)
        unknown = [k for k, v in (summary.executors.items() if summary else [])
                   if v.state.name != "CONFIRMED"]
        self.state_machine.confirm_stopped(all_confirmed, unknown, reason)
        # DEC-041：该 run 不再 tick，poll 不会再结算在途动作的租约；这里统一结算，
        # 否则 motion_global 等资源会一直占着，下一次启动/单步被互锁拒绝。
        settle = getattr(self.executor, "settle_run_leases", None)
        if callable(settle):
            settle(self.state_machine.run_id, stop_confirmed=all_confirmed)
        self._finish(self.state_machine.state.value)
        return summary

    # ---------- 故障恢复（DEC-061） ----------
    def fault_info(self) -> dict[str, Any]:
        """当前 FAULT 的可恢复信息（给作业台「故障恢复」弹窗）。"""
        node = self.compiled.find_failure() if self.compiled else None
        root = getattr(getattr(self.compiled, "tree", None), "root", None)
        retryable = bool(
            node is not None
            and callable(getattr(node, "arm_retry", None))
            and root is not None
            and getattr(node, "parent", None) is root
        )
        step_id = getattr(node, "step_id", "") or ""
        interlocks = getattr(self.executor, "interlocks", None)
        quarantine = {}
        fn = getattr(interlocks, "quarantined", None)
        if callable(fn):
            try:
                quarantine = dict(fn())
            except Exception:  # noqa: BLE001 - 查询失败不挡恢复入口
                quarantine = {}
        unknown = self.ledger.unknown_entries() if self.ledger else []
        return {
            "run_id": self.state_machine.run_id,
            "step_id": step_id,
            # DEC-105：弹窗展示中文步骤名（DEC-037），没有中文名时回退中文类型名，
            # 再回退英文 step_id；英文 id 不直接给操作员看。
            "step_title": self._step_display_title(step_id) or step_id,
            "name": getattr(node, "name", "") or "",
            "reason": getattr(node, "failure_reason", "") or "",
            "failure_class": (self.state_machine.failure_class.value
                              if self.state_machine.failure_class else ""),
            "unknown_count": len(unknown),
            "quarantine": quarantine,
            "retryable": retryable,
        }

    def _step_display_title(self, step_id: str) -> str | None:
        """把节点 step_id 解析成配方里的中文显示名（DEC-037 / DEC-105）。"""
        sid = (step_id or "").split("#", 1)[0]
        if not sid or self.recipe_service is None:
            return None
        rid = self.run_ctx.versions.recipe_id if self.run_ctx else None
        if not rid:
            return None
        try:
            model = self.recipe_service.get_draft(rid)
        except Exception:  # noqa: BLE001 - 找不到草稿就回退 id
            return None
        try:
            from application.services.recipes.editor import step_title
        except Exception:  # noqa: BLE001
            return None
        for step in model.sequence:
            if step.id == sid:
                return step_title(step)
            for branch in step.branches:
                if branch.id == sid:
                    return step_title(branch)
        return None

    def resume_from_fault(self) -> TaskState:
        """FAULT 后**从失败步骤原地恢复**（同一 run，DEC-061）。

        - 只重试失败的那个步骤（换新 attempt 账本键）；已完成的步骤靠旧账本的
          terminal 结果跳过，不重放动作、也不再弹旧的步骤确认框；
        - DEC-104：不再要求人工处理说明/勾选；选择后自动清 UNKNOWN/隔离资源；
        - 之后按 ``FAULT -> VALIDATING -> READY -> RUNNING`` 复核迁移，不重新编译。
        """
        st = self.state_machine.state
        if st is not TaskState.FAULT:
            raise TaskError(f"当前 {st.value} 不是 FAULT，不能从失败步骤恢复")
        if self.compiled is None or self.run_ctx is None:
            raise TaskError("没有可恢复的执行树")
        node = self.compiled.find_failure()
        root = getattr(self.compiled.tree, "root", None)
        if node is None or not callable(getattr(node, "arm_retry", None)):
            raise TaskError("找不到可重试的失败步骤，请改用「重新执行整个任务」")
        if root is None or getattr(node, "parent", None) is not root:
            raise TaskError(
                "失败发生在复合步骤/子流程/循环内部，无法安全地只重试该步；"
                "请改用「重新执行整个任务」")
        reason = self._clear_recovery_blocks(settle_leases=False)
        attempt = node.arm_retry(reason)
        # 整树复位到 INVALID：下次 tick 各节点重新 initialise；已完成步骤/闸由账本跳过。
        try:
            self.compiled.tree.root.stop(Status.INVALID)
        except Exception as exc:  # noqa: BLE001 - 复位失败就不能假装恢复
            raise TaskError(f"复位执行树失败: {exc}") from exc
        # 同一 run 复核后继续（不重新编译、不换 run_id）。
        self.state_machine.begin_validation("故障恢复：复核现场")
        self.state_machine.mark_ready("恢复执行")
        self.state_machine.start("从失败步骤恢复")
        if self.records is not None:
            try:
                self.records.start_run(
                    self.run_ctx.run_id,
                    recipe_id=self.run_ctx.versions.recipe_id,
                    recipe_version=self.run_ctx.versions.recipe_version,
                    config_hash=self.run_ctx.versions.config_hash,
                    calibration_hash=self.run_ctx.versions.calibration_hash,
                )
            except Exception:  # noqa: BLE001 - 记录失败不挡恢复
                pass
        self._emit("task.recovery", Severity.WARNING,
                   f"从失败步骤恢复：{getattr(node, 'step_id', '') or '-'} "
                   f"（第 {attempt} 次尝试）")
        return self.state_machine.state

    def _clear_recovery_blocks(self, *, settle_leases: bool) -> str:
        """故障/停止恢复前自动清 UNKNOWN、解除隔离，并按需结算旧 run 租约。

        DEC-104：不再要求操作员填写说明；清理原因固定记为「故障恢复」，
        仍写入账本/隔离历史，不是静默丢弃。
        ``settle_leases`` 仅用于「重新执行整个任务」（旧 run 被放弃）。
        """
        reason = "故障恢复"
        unknown = list(self.ledger.unknown_entries()) if self.ledger else []
        interlocks = getattr(self.executor, "interlocks", None)
        quarantine: dict = {}
        fn = getattr(interlocks, "quarantined", None)
        if callable(fn):
            try:
                quarantine = dict(fn())
            except Exception:  # noqa: BLE001
                quarantine = {}
        for e in unknown:
            self.ledger.clear_unknown(e.key, reason)
        clear = getattr(interlocks, "clear_quarantine", None)
        if callable(clear):
            for rid in quarantine:
                clear(rid, reason)
        if settle_leases:
            settle = getattr(self.executor, "settle_run_leases", None)
            if callable(settle):
                settle(self.state_machine.run_id, stop_confirmed=True)
        return reason

    # ---------- 快照 ----------
    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.state_machine.run_id,
            "state": self.state_machine.state.value,
            "recipe_version": self.run_ctx.versions.recipe_version if self.run_ctx else None,
            "unknown_executors": list(self.state_machine.unknown_executors),
            "ledger_unknown": len(self.ledger.unknown_entries()),
        }


__all__ = ["TaskError", "TaskService"]
