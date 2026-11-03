"""运动方案执行器（移植旧工程 `robot_worker` 的 exec 模式）。

三种执行：**方案回放** / **引导定位** / **套筒偏移**。要点照搬旧实现：

- 单点用 ``connect=0, block=0`` **非阻塞下发**，随后轮询——这样运动中可随时暂停/继续/停止；
- 到位判据 ``pos_tol=0.002 m`` / ``jt_tol=1.0°``；
- **单点到位超时即停止，弹窗交人处理**（重试本点 / 跳过(风险自担) / 中止方案）——
  **绝不自动下发下一点**（防撞机）；
- 暂停 = ``rm_set_arm_pause`` + 等标志；继续 = ``continue_`` 并**重置超时计时**；
- 停止/中止一律 ``rm_set_arm_stop``；
- 引导定位：粗/精两段（可配子集），每段**人工确认后**才按 ``[x/1000, 0, z/1000]``(米) 偏移；
  **程序内入口**（配方「引导定位」步骤, DEC-033）可带偏移上限与自己的确认端口，
  与手动按钮共用同一套闸/同一引导服务；
- 执行前统一坐标系守卫 ``ensure_production_frames``（Bolt 工具系 + Ganta 工作系）。

**方案内可混排套筒偏移**：路点方式为 ``socket_offset`` 时，走**同一个**旧工程动作——Bolt
工具系 Y 轴平移偏移，**方向由 Y 分量的正负号决定**（+ = 进入/前进 Y+，− = 脱离/后退 Y−），
与「套筒偏移」按钮同源（旧工程 ``_run_socket_move`` / ``_run_pickup``）。
该动作与旧实现一样是**阻塞式**（偏移小、耗时短）：**偏移进行中暂停不生效**，停止可用；
未到位同样**绝不自动继续**，交人处理。

**DEC-085**：real 模式不再有环境变量运动闸；每次动作只复核机械臂连接。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Sequence

from application.adapters.realman.motion import JOINT_TOL_DEG, POS_TOL_M
# 引导定位的段定义放在 contracts（中性层），避免 recipes.service ↔ motion_runner 循环导入
from application.contracts.enums import GUIDE_STAGES
from application.services.motion_config import (
    MotionConfigStore,
    MotionPlan,
    SOCKET_MODE_LABELS,
    is_socket_mode,
    is_tool_offset_mode,
    socket_signed_mm,
    tool_offset_mm,
)
from application.infrastructure import logging_setup

_log = logging_setup.get_logger("services.motion")

STATE_IDLE = "idle"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_GUIDE = "guide"
STATE_ERROR = "error"

TIMEOUT_CHOICES = ("重试本点", "跳过本点(风险自担)", "中止方案")
CONFIRM_YES = "是"
CONFIRM_NO = "否"


def _default_confirm(title: str, message: str, options: list[str]) -> str | None:
    """无 UI 时的默认确认：**一律拒绝**（宁可不动，也不在无人确认时移动）。"""
    return None


class MotionRunner:
    """运动方案执行器：工作线程执行 + 状态可轮询。"""

    def __init__(self, arm: Any, store: MotionConfigStore, *,
                 log: Callable[[str], None] = print,
                 confirm: Callable[[str, str, list[str]], str | None] | None = None,
                 guidance: Any = None) -> None:
        self.arm = arm
        self.store = store
        self.log = log
        self.confirm = confirm or _default_confirm
        # 引导定位：**本程序内闭环**（DS86 抓图 → 找圆 → 偏差），不再连外部视觉程序
        self.guidance = guidance

        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._quit = threading.Event()
        self._pause = threading.Event()
        self._cancel_guide = threading.Event()
        self._state: dict = {"mode": STATE_IDLE, "plan_id": None, "index": 0,
                             "total": 0, "message": "", "guide_stage": "",
                             "last_error": "", "guide_results": [],
                             # DEC-058: 引导定位细分阶段，供步骤节点做"检测卡死"看门狗
                             "guide_phase": "", "guide_phase_since": 0.0,
                             # DEC-084：引导检测结果/画面（作业台右卡画圆用）
                             "last_guide": None, "last_guide_frame": None}

    # ---------- 状态 ----------
    @property
    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._state["mode"] in (STATE_PLAYING, STATE_PAUSED, STATE_GUIDE)

    @property
    def cancel_requested(self) -> bool:
        """作业是否已请求中止（stop()/方案被停止）。供**程序内调用方**的确认等待轮询。"""
        return self._quit.is_set()

    @property
    def guide_detect_timeout_s(self) -> float:
        """引导"准备+检测"阶段超时秒数（DEC-058；<=0 表示不启用看门狗）。"""
        try:
            return float(self.store.config.profile.guide_detect_timeout_s or 0)
        except Exception:  # noqa: BLE001 - 配置读不到就退回默认 60s
            return 60.0

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._state.update(kw)

    def wait(self, timeout: float = 5.0) -> bool:
        """等待当前作业结束（测试/命令行用）。"""
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    # ---------- 前置检查 ----------
    def _ready(self, action: str) -> bool:
        """机械臂连接检查（DEC-085：real 模式默认允许运动，无环境变量闸）。"""
        if getattr(self.arm, "connected", False) is False:
            self.log(f"[Motion] {action} 被拒：机械臂未连接")
            self._set(mode=STATE_ERROR, last_error="机械臂未连接")
            return False
        return True

    def _ensure_frames(self) -> None:
        fn = getattr(self.arm, "ensure_production_frames", None)
        if not callable(fn):
            return
        t0 = time.monotonic()
        # DEC-058 排查：这里会读机器人当前坐标系（SDK 调用），卡住时日志停在这一行之前
        self.log("[Motion] 准备生产坐标系（读机器人工具系/工作系）…")
        ok, actions = fn()
        self.log(f"[Motion] 坐标系准备完成 用时 {time.monotonic() - t0:.2f}s "
                 f"一致={ok} 切换={actions}")

    def _spawn(self, target: Callable[..., None], *args: Any) -> bool:
        """起运动线程；**线程内任何未捕获异常都必须落到 ERROR 状态**（DEC-058）。

        否则 ``_run_guide`` 等在设了 ``mode=guide/playing`` 之后抛异常，线程死掉而
        状态永远停在"执行中"：界面一直转圈、没有确认条、机械臂不动、也没有报错。
        """
        if self.busy:
            self.log("[Motion] 已有运动任务在执行，请先停止")
            return False
        self._stop.clear()
        self._quit.clear()
        self._pause.clear()
        name = getattr(target, "__name__", "motion")

        def _thread_main() -> None:
            try:
                target(*args)
            except Exception as exc:  # noqa: BLE001 - 线程兜底: 转 ERROR, 不静默退出
                msg = f"运动线程异常中止（{name}）: {exc}"
                self.log(f"[Motion] {msg}")
                _log.exception("[Motion] %s", msg)
                self._set(mode=STATE_ERROR, last_error=msg)

        self._thread = threading.Thread(target=_thread_main, daemon=True)
        self._thread.start()
        return True

    # ---------- 1) 方案回放 ----------
    def start_plan(self, plan_id: str) -> bool:
        plan = self.store.config.find_plan(plan_id)
        if plan is None:
            self._set(mode=STATE_ERROR, last_error=f"方案 {plan_id!r} 不存在")
            self.log(f"[Motion] 方案 {plan_id!r} 不存在")
            return False
        if not plan.waypoints:
            self._set(mode=STATE_ERROR, last_error=f"方案 {plan_id!r} 无路点")
            self.log(f"[Motion] 方案 {plan_id!r} 无路点")
            return False
        if not self._ready(f"运行方案 {plan_id!r}"):
            return False
        return self._spawn(self._run_plan, plan.clone())

    def _run_plan(self, plan: MotionPlan) -> None:
        self._set(mode=STATE_PLAYING, plan_id=plan.plan_id, index=0,
                  total=len(plan.waypoints), message=f"开始执行 {plan.plan_id}",
                  last_error="")
        self.log(f"[Motion] 开始执行方案 {plan.plan_id}（默认方式={plan.default_mode} "
                 f"速度={plan.speed} 点数={len(plan.waypoints)}）")
        self._ensure_frames()
        per_point = float(plan.timeout or self.store.config.profile.point_timeout_s or 30)
        try:
            for i, wp in enumerate(plan.waypoints):
                if self._quit.is_set():
                    break
                self._set(index=i + 1, message=f"第 {i + 1}/{len(plan.waypoints)} 点")
                if not self._move_point(plan, wp, per_point):
                    break
            if self._quit.is_set():
                self.log(f"[Motion] 方案 {plan.plan_id} 已中止")
            elif self._state.get("mode") == STATE_ERROR:
                # 点位/相对偏移已置 ERROR（如读位姿失败/下发失败）：保留错误，不冒充完成。
                self.log(f"[Motion] 方案 {plan.plan_id} 失败结束："
                         f"{self._state.get('last_error') or ''}")
            else:
                self.log(f"[Motion] 方案 {plan.plan_id} 执行完成")
                self._set(mode=STATE_IDLE, message=f"{plan.plan_id} 完成")
                return
        finally:
            if self._state.get("mode") not in (STATE_ERROR,):
                self._set(mode=STATE_IDLE)

    def _move_point(self, plan: MotionPlan, wp: Any, per_point_timeout: float) -> bool:
        """下发单点并等到位。返回 False 表示应结束方案。"""
        mode = wp.mode
        if is_socket_mode(mode):
            # 套筒偏移路点：走 Bolt 工具系 Y 轴平移偏移（非点位运动）。
            return self._socket_point(plan, wp, per_point_timeout)
        if is_tool_offset_mode(mode):
            # 工具系相对偏移（DEC-060）：算目标位姿 + 一条 movel 真直线。
            return self._tool_offset_point(plan, wp, per_point_timeout)
        target = [float(x) for x in wp.pos]
        return self._send_point(plan, target, mode, per_point_timeout)

    def _send_point(self, plan: MotionPlan, target: list[float], mode: str,
                    per_point_timeout: float) -> bool:
        """下发绝对点位（movel/movej）并等到位；超时停下交人处理。"""
        send = self.arm.movej if mode == "movej" else self.arm.movel
        try:
            send(target, plan.speed, plan.blend, 0, 0)      # 非阻塞下发
        except Exception as exc:  # noqa: BLE001
            self.log(f"[Motion] 下发失败: {exc}")
            self._set(mode=STATE_ERROR, last_error=f"下发失败: {exc}")
            return False

        t0 = time.time()
        while True:
            if self._quit.is_set():
                self._safe_stop()
                return False
            if self._pause.is_set():
                self.arm.pause()
                self._set(mode=STATE_PAUSED, message="已暂停")
                while self._pause.is_set() and not self._quit.is_set():
                    time.sleep(0.05)
                if self._quit.is_set():
                    self._safe_stop()
                    return False
                self.arm.continue_()
                self._set(mode=STATE_PLAYING)
                t0 = time.time()                            # 恢复后重置超时计时
                continue
            if self._arrived(mode, target):
                return True
            if time.time() - t0 > per_point_timeout:
                # 单点超时：立即停止，交人处理；**绝不自动下发下一点**
                self._safe_stop()
                self._set(mode=STATE_PAUSED,
                          message=f"第 {self.state['index']} 点到位超时({per_point_timeout:.0f}s)")
                self.log(f"[Motion] 方案 {plan.plan_id} 第 {self.state['index']} 点"
                         f"到位超时({per_point_timeout:.0f}s)，已停止等待用户处理")
                choice = self.confirm(
                    "运动方案到位超时",
                    f"方案「{plan.plan_id}」第 {self.state['index']} 点未在 "
                    f"{per_point_timeout:.0f}s 内到位，机器人已停止。请确认现场安全后选择：",
                    list(TIMEOUT_CHOICES))
                if choice == "跳过本点(风险自担)":
                    self.log("[Motion] 用户选择跳过本点（风险自担）")
                    return True
                if choice == "重试本点":
                    self.log("[Motion] 用户选择重试本点")
                    try:
                        self.arm.continue_()
                        send(target, plan.speed, plan.blend, 0, 0)
                    except Exception as exc:  # noqa: BLE001
                        self._set(mode=STATE_ERROR, last_error=f"重试下发失败: {exc}")
                        return False
                    self._set(mode=STATE_PLAYING)
                    t0 = time.time()
                    continue
                self.log("[Motion] 用户中止方案")
                return False
            time.sleep(0.05)

    def _tool_offset_point(self, plan: MotionPlan, wp: Any,
                           per_point_timeout: float) -> bool:
        """工具系相对偏移路点（DEC-060）：Bolt 工具系 dx/dy/dz(mm) → 目标位姿 → **一条 movel**。

        - 偏移按**当前位姿**与 Bolt 工具系轴方向计算，姿态保持不变；
        - 因此「先 Z+5mm，再 (0,+100,-5)mm」就是先抬 5mm、再斜直线回到原目标位；
        - 读位姿/工具系失败 → 失败（不猜方向、不降级成逐轴走）；未到位交人处理。
        """
        dx, dy, dz = tool_offset_mm(wp)
        label = "工具系偏移"
        if all(abs(v) <= 1e-9 for v in (dx, dy, dz)):
            self.log(f"[Motion] {label}: 偏移为 0，跳过")
            return True
        # 本点**发出前**的暂停闸（与套筒偏移一致）。
        while self._pause.is_set() and not self._quit.is_set():
            self.arm.pause()
            self._set(mode=STATE_PAUSED, message="已暂停")
            time.sleep(0.05)
        if self._quit.is_set():
            self._safe_stop()
            return False
        self._set(mode=STATE_PLAYING)
        fn = getattr(self.arm, "tool_offset_target", None)
        target = None
        try:
            if callable(fn):
                target = fn([dx / 1000.0, dy / 1000.0, dz / 1000.0])
        except Exception as exc:  # noqa: BLE001 - 读位姿/工具系失败=不可执行
            self.log(f"[Motion] {label} 计算目标位姿失败: {exc}")
            self._set(mode=STATE_ERROR, last_error=f"{label}计算目标位姿失败: {exc}")
            return False
        if not target or len(target) < 6:
            self.log(f"[Motion] {label} 无法取得当前位姿/工具系方向，未移动")
            self._set(mode=STATE_ERROR,
                      last_error=f"{label}无法取得当前位姿或工具系方向")
            return False
        self.log(f"[Motion] 方案 {plan.plan_id} 第 {self.state.get('index')} 点 {label} "
                 f"(dx,dy,dz)=({dx:+.1f}, {dy:+.1f}, {dz:+.1f})mm "
                 f"→ movel 目标=({target[0]:.4f}, {target[1]:.4f}, {target[2]:.4f})m")
        return self._send_point(plan, [float(x) for x in target], "movel",
                                per_point_timeout)

    def _socket_point(self, plan: MotionPlan, wp: Any, per_point_timeout: float) -> bool:
        """方案里的一个**套筒偏移**路点（Bolt 工具系 Y 轴平移偏移）。False=结束方案。

        与「套筒偏移」按钮同源：带符号偏移量记在 Y 分量(mm)——**+ = 进入/前进(Y+)，
        − = 脱离/后退(Y−)**，方向直接取正负号。偏移是**阻塞式**（旧实现如此）——发出后
        **暂停不生效**，停止可用；未到位**绝不自动继续**，交人处理（与点位超时同一套三选一）。
        """
        label = SOCKET_MODE_LABELS.get(wp.mode, wp.mode)
        mm = socket_signed_mm(wp)
        if abs(mm) <= 1e-9:
            self.log(f"[Motion] {label}: 偏移为 0，跳过")
            return True
        # 本点**发出前**的暂停要等；发出后靠 stop_check 停止。
        while self._pause.is_set() and not self._quit.is_set():
            self.arm.pause()
            self._set(mode=STATE_PAUSED, message="已暂停")
            time.sleep(0.05)
        if self._quit.is_set():
            self._safe_stop()
            return False
        self._set(mode=STATE_PLAYING)
        speed = int(self.store.config.profile.guide_move_speed or 10)
        off = [0.0] * 6
        off[1] = mm / 1000.0
        try:
            res = self.arm.move_offset(off, v=speed,
                                       stop_check=lambda: self._quit.is_set(),
                                       timeout=float(per_point_timeout))
        except Exception as exc:  # noqa: BLE001
            self.log(f"[Motion] {label} 下发失败: {exc}")
            self._set(mode=STATE_ERROR, last_error=f"{label}下发失败: {exc}")
            return False
        if res == 0:
            self.log(f"[Motion] 方案 {plan.plan_id} {label} {mm:+.1f}mm"
                     f"（Bolt 工具系 Y 轴，速度{speed}%）到位")
            return True
        if res == -2:                          # 被 stop_check 中止（用户停止）
            self.log(f"[Motion] {label} 已停止")
            return False
        # -1 未连接 / -3 到位超时 / 其它下发错误：停止并交人处理，**绝不自动继续**。
        self._safe_stop()
        self._set(mode=STATE_PAUSED,
                  message=f"第 {self.state['index']} 点({label})未到位")
        self.log(f"[Motion] 方案 {plan.plan_id} 第 {self.state['index']} 点({label})未到位，"
                 f"move_offset 返回 {res}，已停止等待用户处理")
        choice = self.confirm(
            "套筒偏移未到位",
            f"方案「{plan.plan_id}」第 {self.state['index']} 点（{label} {mm:+.1f}mm）"
            f"未正常到位（返回码 {res}），机器人已停止。请确认现场安全后选择：",
            list(TIMEOUT_CHOICES))
        if choice == "跳过本点(风险自担)":
            self.log("[Motion] 用户选择跳过本点（风险自担）")
            return True
        if choice == "重试本点":
            self.log("[Motion] 用户选择重试本点")
            try:
                res2 = self.arm.move_offset(off, v=speed,
                                            stop_check=lambda: self._quit.is_set(),
                                            timeout=float(per_point_timeout))
            except Exception as exc:  # noqa: BLE001
                self._set(mode=STATE_ERROR, last_error=f"{label}重试失败: {exc}")
                return False
            if res2 == 0:
                self._set(mode=STATE_PLAYING)
                return True
            self._set(mode=STATE_ERROR, last_error=f"{label}重试未到位: {res2}")
            return False
        self.log("[Motion] 用户中止方案")
        return False

    def _arrived(self, mode: str, target: list[float]) -> bool:
        cur = (self.arm.get_current_joint() if mode == "movej"
               else self.arm.get_current_pose())
        if not cur or len(cur) < 6:
            return False
        if mode == "movej":
            return max(abs(cur[k] - target[k]) for k in range(6)) < JOINT_TOL_DEG
        return (sum((cur[k] - target[k]) ** 2 for k in range(3)) ** 0.5) < POS_TOL_M

    def _safe_stop(self) -> None:
        try:
            self.arm.stop()
        except Exception:  # noqa: BLE001
            pass

    # ---------- 2) 引导定位 ----------
    def start_guide(self, *, speed: int | None = None,
                    stages: Sequence[str] = GUIDE_STAGES,
                    max_offset_mm: float = 0.0,
                    confirm: Callable[[str, str, list[str]], str | None] | None = None,
                    override: dict | None = None,
                    fine_profile_ref: str | None = None) -> bool:
        """引导定位入口：粗/精段先**本地检测**出偏移，人工确认后在 Bolt 工具系偏移。

        手动入口（「运动方案」页按钮）用默认参数：两段、无偏移上限、用界面确认框。
        **程序内入口**（配方「引导定位」步骤, DEC-033）可传 ``stages`` 子集、
        ``max_offset_mm`` 上限和自己的 ``confirm`` 端口（受限选项 + 留证）——
        与手动入口共用同一套运动闸/连接闸与同一个引导服务，不另开通道。

        ``override``（DEC-048）：视觉判定步骤发布的 ``{frame, circle{cx,cy,radius_px}}``。
        有它时**第一段**不重新抓图/找圆，直接用该帧与人工确认圆心算偏移；其后各段
        （若配方同时选粗+精）仍走正常闭环重新检测。
        """
        if not self._ready("引导定位"):
            return False
        prof = self.store.config.profile
        want = tuple(s for s in GUIDE_STAGES if s in set(stages))   # 顺序固定 粗→精
        if not want:
            self._set(mode=STATE_ERROR, last_error=f"引导段选择非法: {stages!r}")
            self.log(f"[Motion] 引导段选择非法: {stages!r}（只能是 {GUIDE_STAGES} 的子集）")
            return False
        # DEC-058 排查：把"用谁的帧/圆"写清楚，现场日志一眼能看出走的是哪条检测链
        if override:
            c = override.get("circle") or {}
            frame = override.get("frame")
            src = (f"沿用视觉步骤 {override.get('step_id')!r} 的帧"
                   f"(帧id={getattr(frame, 'frame_id', None)}, "
                   f"圆心=({c.get('cx')},{c.get('cy')}) r={c.get('radius_px')}px)")
        else:
            src = "无视觉步骤帧（将重新抓图找圆）"
        self.log(f"[Motion] 引导定位启动: 段={list(want)} "
                 f"速度={int(speed or prof.guide_move_speed or 10)}% "
                 f"上限={float(max_offset_mm or 0.0):.1f}mm "
                 f"精定位参数={fine_profile_ref or '默认'} {src}")
        return self._spawn(self._run_guide,
                           int(speed or prof.guide_move_speed or 10),
                           float(prof.guide_recv_timeout_s or 600),
                           want, float(max_offset_mm or 0.0), confirm, override,
                           fine_profile_ref)

    def _log_guide_params(self, fine_profile_ref: str | None) -> None:
        """DEC-070：记录本次引导实际生效的轴映射/符号与 tcp（读不到就跳过，不打断引导）。"""
        try:
            cfg = getattr(getattr(self.guidance, "config_store", None), "config", None)
            params = cfg.offsets_for(fine_profile_ref) if cfg is not None else None
            if params is None:
                return
            def _m(m) -> str:
                return (f"相机X→{m.x_axis}({'+' if int(m.x_sign) > 0 else '−'}) "
                        f"相机Y→{m.y_axis}({'+' if int(m.y_sign) > 0 else '−'})")
            self.log(f"[Motion] 引导参数: 粗[{_m(params.coarse)}] 精[{_m(params.fine)}] "
                     f"tcp=({float(params.tcp_x):g}, {float(params.tcp_y):g}, "
                     f"{float(params.tcp_z):g})mm")
        except Exception:  # noqa: BLE001 - 纯诊断日志，读不到不打断引导
            return

    def _run_guide(self, speed: int, recv_timeout: float,
                   stages: tuple[str, ...] = GUIDE_STAGES,
                   max_offset_mm: float = 0.0,
                   confirm: Callable[[str, str, list[str]], str | None] | None = None,
                   override: dict | None = None,
                   fine_profile_ref: str | None = None) -> None:
        """粗 → 精（可只跑其一）：每段先**本地检测**出偏移，人工确认后再偏移。

        ``max_offset_mm > 0`` 时 |X|/|Z| 任一超过上限 → **拒绝移动**并置 error
        （上限检查在人工确认**之前**，不让人被异常检测值带偏）。
        每段结果追加进 ``state["guide_results"]``，供程序内调用方判定本步成败。
        """
        confirm = confirm or self.confirm
        self._set(mode=STATE_GUIDE, guide_stage="", message="引导定位中",
                  last_error="", guide_results=[],
                  guide_phase="prepare", guide_phase_since=time.monotonic())
        self.log(f"[Motion] 引导线程开始: 段={list(stages)} "
                 f"线程={threading.current_thread().name}")
        self._ensure_frames()
        if self.guidance is None:
            self.log("[Motion] 引导定位：未接入本地引导服务")
            self._set(mode=STATE_ERROR, last_error="未接入本地引导服务", guide_phase="")
            return
        # DEC-070：把**实际生效**的轴映射与 tcp 打进日志——方向不对时现场一眼可核对
        # （粗/精都朝远离目标的方向走 = 轴符号反了，到「参数配置」页改符号）。
        self._log_guide_params(fine_profile_ref)

        def _record(**row: Any) -> None:
            with self._lock:
                rows = list(self._state.get("guide_results") or [])
                rows.append(row)
                self._state["guide_results"] = rows

        self._cancel_guide.clear()
        used_override = False
        for stage in stages:
            # DEC-058: 超时被外部 stop 之后，卡住的旧线程即使随后返回也不得继续动作。
            if (self._quit.is_set() or self._cancel_guide.is_set()
                    or self._thread is not threading.current_thread()):
                self._set(mode=STATE_IDLE, message="引导结束（已取消）", guide_phase="")
                return
            label = "粗定位" if stage == "coarse" else "精定位"
            use_override = bool(override) and not used_override
            t_stage = time.monotonic()
            self._set(guide_stage=stage, message=f"{label}检测中",
                      guide_phase="detect", guide_phase_since=time.monotonic())
            self.log(f"[Motion] {label}：开始检测"
                     f"（{'按视觉判定步骤的圆心与同帧深度' if use_override else '自动抓图找圆'}）")
            source = "auto"
            result = None
            if use_override:
                # DEC-048：第一段沿用视觉步骤的帧与人工确认圆，不重新抓图找圆。
                # DEC-063：**操作员显式选择的圆是最终依据**——不支持/异常时不再静默回退
                # 自动找圆（否则会按"第一个自动圆"移动，与操作员的选择无关）。
                fn = getattr(self.guidance, "detect_with_circle", None)
                if not callable(fn):
                    msg = "引导服务不支持按视觉步骤圆心检测，不能使用操作员选择的圆"
                    self.log(f"[Motion] {label} {msg}")
                    _record(stage=stage, ok=False, message=msg,
                            confirmed=False, applied=False)
                    self._set(mode=STATE_ERROR, last_error=f"{label}检测失败: {msg}",
                              guide_phase="")
                    return
                c = override.get("circle") or {}
                kwargs = dict(frame=override.get("frame"),
                              cx=float(c.get("cx")),
                              cy=float(c.get("cy")),
                              radius_px=float(c.get("radius_px") or 0.0))
                if fine_profile_ref:
                    kwargs["fine_profile_ref"] = fine_profile_ref
                try:
                    try:
                        result = fn(**kwargs)
                    except TypeError:
                        # 兼容不含 fine_profile_ref 参数的旧假引导服务
                        if "fine_profile_ref" in kwargs:
                            kwargs.pop("fine_profile_ref")
                            result = fn(**kwargs)
                        else:
                            raise
                    used_override = True
                    source = "vision_step"
                    self.log(f"[Motion] {label}：按视觉判定步骤圆心"
                             f"({float(c.get('cx')):.0f},{float(c.get('cy')):.0f}) "
                             f"与同帧深度定位（不重新抓图找圆）")
                except Exception as exc:  # noqa: BLE001 - 操作员选择优先, 失败即失败
                    msg = f"按操作员选择的圆心检测失败: {exc}"
                    self.log(f"[Motion] {label} {msg}")
                    _record(stage=stage, ok=False, message=msg,
                            confirmed=False, applied=False)
                    self._set(mode=STATE_ERROR, last_error=f"{label}检测失败: {msg}",
                              guide_phase="")
                    return
            if result is None:
                try:
                    # 精定位：粗定位已把目标移到中心附近，重新检测时优先取**离光心最近**
                    # 的有效圆，避免多圆场景按 hits[0] 继续移动（DEC-063）；
                    # 并选用该步骤配置的精定位参数套（DEC-065）。
                    fn = getattr(self.guidance, "detect_fine", None) if stage == "fine" else None
                    if callable(fn):
                        try:
                            result = (fn(fine_profile_ref=fine_profile_ref)
                                      if fine_profile_ref else fn())
                        except TypeError:      # 兼容旧假引导服务
                            result = fn()
                    else:
                        try:
                            result = (self.guidance.detect(
                                fine_profile_ref=fine_profile_ref)
                                if fine_profile_ref else self.guidance.detect())
                        except TypeError:
                            result = self.guidance.detect()
                except Exception as exc:  # noqa: BLE001 - 检测异常=步骤失败, 不让线程静默死掉
                    self.log(f"[Motion] 引导定位·{label} 检测异常: {exc}")
                    _record(stage=stage, ok=False, message=f"检测异常: {exc}",
                            confirmed=False, applied=False)
                    self._set(mode=STATE_ERROR,
                              last_error=f"{label}检测异常: {exc}", guide_phase="")
                    return
            if not getattr(result, "ok", False):
                self.log(f"[Motion] 引导定位·{label} 检测失败: {result.message}")
                _record(stage=stage, ok=False, message=f"检测失败: {result.message}",
                        confirmed=False, applied=False)
                self._set(mode=STATE_ERROR,
                          last_error=f"{label}检测失败: {result.message}", guide_phase="")
                return
            off = result.coarse if stage == "coarse" else result.fine
            x_mm, z_mm = float(off[0]), float(off[1])
            xyz = result.cam_xyz
            self._set(last_guide=result,
                      last_guide_frame=getattr(self.guidance, "last_frame", None),
                      guide_phase="confirm",
                      guide_phase_since=time.monotonic())
            self.log(f"[Motion] {label}：相机系 XYZ=({xyz[0]:.2f}, {xyz[1]:.2f}, "
                     f"{xyz[2]:.0f})mm（深度 {result.used_depth_mm:.0f}mm）→ "
                     f"偏移 X={x_mm:.2f}mm Z={z_mm:.2f}mm "
                     f"（基准={'相机中心' if stage == 'coarse' else '工具中心(tcp)'}, "
                     f"来源={source}, 检测耗时 {time.monotonic() - t_stage:.2f}s）")
            row: dict[str, Any] = {
                "stage": stage, "ok": True, "source": source,
                "cam_xyz": tuple(xyz) if xyz else None,
                "x_mm": x_mm, "z_mm": z_mm,
                "used_depth_mm": float(result.used_depth_mm or 0.0),
                "confirmed": False, "applied": False, "message": "",
            }
            if max_offset_mm > 0 and (abs(x_mm) > max_offset_mm
                                      or abs(z_mm) > max_offset_mm):
                msg = (f"{label}偏移超上限（|X|={abs(x_mm):.1f}mm "
                       f"|Z|={abs(z_mm):.1f}mm > 上限 {max_offset_mm:.1f}mm），未移动")
                self.log(f"[Motion] {msg}")
                _record(**{**row, "message": msg})
                self._set(mode=STATE_ERROR, last_error=msg, guide_phase="")
                return
            choice = confirm(
                f"引导定位 · {label}",
                f"检测到偏差 X={x_mm:.2f}mm Z={z_mm:.2f}mm\n是否按该偏移移动？"
                + ("\n（精定位=工具中心对准圆心：确认后相机画面里圆心会偏离画面中心"
                   "（工具中心标定位置），属正常）" if stage == "fine" else ""),
                [CONFIRM_YES, CONFIRM_NO])
            if choice != CONFIRM_YES:
                self.log(f"[Motion] {label} 未确认，终止引导")
                _record(**{**row, "message": "未确认（操作者拒绝移动）"})
                self._set(mode=STATE_IDLE, message=f"{label}未确认，引导结束",
                          guide_phase="")
                return
            row["confirmed"] = True
            self._set(guide_phase="move", guide_phase_since=time.monotonic())
            self.log(f"[Motion] {label}：已确认，下发偏移 {x_mm:.2f}/{z_mm:.2f}mm")
            try:
                res = self.arm.move_offset(
                    [x_mm / 1000.0, 0.0, z_mm / 1000.0, 0.0, 0.0, 0.0],
                    v=speed, stop_check=lambda: self._quit.is_set())
            except Exception as exc:  # noqa: BLE001 - 下发异常=步骤失败, 不留"执行中"
                self.log(f"[Motion] 引导定位·{label} 偏移下发异常: {exc}")
                _record(**{**row, "message": f"偏移下发异常: {exc}"})
                self._set(mode=STATE_ERROR,
                          last_error=f"{label}偏移下发异常: {exc}", guide_phase="")
                return
            self.log(f"[Motion] {label} 偏移下发返回: {res}")
            if res != 0:
                _record(**{**row, "message": f"偏移下发失败（返回码 {res}）"})
                self._set(mode=STATE_ERROR, last_error=f"{label}偏移失败: {res}",
                          guide_phase="")
                return
            row["applied"] = True
            row["message"] = "已按偏移移动"
            _record(**row)
        self._set(mode=STATE_IDLE, guide_stage="", message="引导定位完成",
                  guide_phase="")
        self.log("[Motion] 引导定位完成")

    def cancel_guide(self) -> None:
        """取消引导等待（在下一段检测/移动前生效）。"""
        self._cancel_guide.set()

    # ---------- 3) 套筒偏移 ----------
    def request_socket_move(self, mm: float, *, axis: str = "Y",
                            speed: int | None = None) -> bool:
        """套筒沿 Bolt 工具系 Y 轴偏移（正=前进/负=后退）。"""
        if not self._ready("套筒偏移"):
            return False
        return self._spawn(self._run_socket, float(mm), axis,
                           int(speed or self.store.config.profile.guide_move_speed or 10))

    def _run_socket(self, mm: float, axis: str, speed: int) -> None:
        self._ensure_frames()
        idx = {"X": 0, "Y": 1, "Z": 2}.get(axis.upper(), 1)
        off = [0.0] * 6
        off[idx] = mm / 1000.0
        tool = self.store.config.profile.active_tool or "Bolt"
        try:
            res = self.arm.move_offset(off, v=speed,
                                       stop_check=lambda: self._quit.is_set())
        finally:
            self._set(mode=STATE_IDLE)
        self.log(f"[Motion] 套筒{'前进' if mm > 0 else '后退'} {abs(mm):.1f}mm"
                 f"（{tool} 工具系 {axis.upper()} 轴，速度{speed}%）下发返回: {res}")

    # ---------- 控制 ----------
    def pause(self) -> None:
        if self._state["mode"] == STATE_PLAYING:
            self._pause.set()

    def resume(self) -> None:
        if self._state["mode"] == STATE_PAUSED:
            self._pause.clear()

    def stop(self) -> None:
        """停止当前作业（含引导等待），机械臂保持连接可重跑。"""
        self._quit.set()
        self._pause.clear()
        self.cancel_guide()
        self._safe_stop()
        self._set(mode=STATE_IDLE, message="已停止", guide_phase="")


__all__ = [
    "CONFIRM_NO", "CONFIRM_YES", "GUIDE_STAGES", "MotionRunner", "STATE_ERROR",
    "STATE_GUIDE", "STATE_IDLE", "STATE_PAUSED", "STATE_PLAYING", "TIMEOUT_CHOICES",
]
