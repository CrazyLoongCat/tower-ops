"""运动方案执行器与**本地引导**（离线；**不接真机**）。

覆盖：未开运动闸一律拒绝 / 逐点非阻塞下发(connect=0,block=0)并等到位 / 暂停·继续·停止 /
单点超时**停下交人处理且不下发下一点** / 引导定位**本地检测**两段人工确认后按 mm→m 偏移 /
套筒 Y 轴偏移 / 取消引导。
"""
from __future__ import annotations

import time

import pytest

from application.services.guidance_service import CircleHit, GuidanceResult
from application.services.motion_config import (
    MotionConfigStore,
    MotionPlan,
    Waypoint,
)
from application.services.motion_runner import (
    STATE_IDLE,
    STATE_PAUSED,
    STATE_PLAYING,
    MotionRunner,
)


class FakeGuidance:
    """假引导服务：按序给出粗/精检测结果（**不抓图、不找圆**，只验执行器配合）。"""

    def __init__(self, *, coarse=(10.0, -5.0), fine=(1.0, 0.5),
                 ok=True, message="") -> None:
        self.coarse = coarse
        self.fine = fine
        self.ok = ok
        self.message = message
        self.calls = 0

    def detect(self) -> GuidanceResult:
        self.calls += 1
        if not self.ok:
            return GuidanceResult(False, self.message)
        return GuidanceResult(
            True, "检测成功", cam_xyz=(12.0, 3.0, 500.0), coarse=self.coarse,
            fine=self.fine, circle=CircleHit(0, 12.0, 3.0, 25.0),
            used_depth_mm=500.0)


# ---------------------------------------------------------------- 假机械臂
class FakeArm:
    """到位即"瞬移"的假臂（默认）；slow=True 时永不到位，用于超时/暂停测试。"""

    def __init__(self, *, slow: bool = False, connected: bool = True) -> None:
        self.connected = connected
        self.slow = slow
        self.calls: list[tuple] = []
        self.offsets: list[tuple] = []
        self.tool_targets: list[tuple] = []
        self.stopped = 0
        self.paused = 0
        self.continued = 0
        self.frames_called = 0
        self._pose = [0.0] * 6
        self._joint = [0.0] * 6

    # 运动
    def movel(self, pose, v=20, r=0, connect=0, block=1):
        self.calls.append(("movel", tuple(pose), v, r, connect, block))
        if not self.slow:
            self._pose = list(pose)
        return 0

    def movej(self, joint, v=20, r=0, connect=0, block=1):
        self.calls.append(("movej", tuple(joint), v, r, connect, block))
        if not self.slow:
            self._joint = list(joint)
        return 0

    def move_offset(self, off, v=20, r=0, connect=0, block=1, stop_check=None,
                    tol=0.002, timeout=30.0):
        self.offsets.append((tuple(off), v))
        return 0

    def tool_offset_target(self, off_m):
        """离线假臂：工具系轴 = 基座轴（方向恒等），目标 = 当前位姿 + 偏移。"""
        self.tool_targets.append(tuple(off_m))
        pose = list(self._pose)
        for i in range(3):
            pose[i] += float(off_m[i])
        return pose

    # 查询
    def get_current_pose(self):
        return list(self._pose)

    def get_current_joint(self):
        return list(self._joint)

    # 生命周期/控制
    def ensure_production_frames(self):
        self.frames_called += 1
        return (True, [])

    def pause(self):
        self.paused += 1
        return 0

    def continue_(self):
        self.continued += 1
        return 0

    def stop(self):
        self.stopped += 1
        return 0


def _store(tmp_path) -> MotionConfigStore:
    st = MotionConfigStore(str(tmp_path / "motion.yaml"), seed_if_missing=False)
    st.load()
    st.add_plan(MotionPlan("p1", "t", "movel", 30, 10, 30,
                           [Waypoint("movel", (0.1, 0.2, 0.3, 0, 0, 0)),
                            Waypoint("movej", (1, 2, 3, 4, 5, 6))],
                           source="本工程新建"))
    st.save()
    return st


def _gate_on(monkeypatch) -> None:
    """DEC-085：real 模式无环境变量闸，保留空函数避免改动大量调用点。"""
    return None


# ---------------------------------------------------------------- 门控
def test_unconnected_arm_refused(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    runner = MotionRunner(FakeArm(connected=False), _store(tmp_path))
    assert runner.start_plan("p1") is False
    assert runner.state["last_error"] == "机械臂未连接"


def test_motion_thread_exception_lands_in_error_state(tmp_path, monkeypatch):
    """DEC-058: 运动线程里的未捕获异常必须置 ERROR，不能静默退出留下"执行中"。"""
    _gate_on(monkeypatch)
    st = _store(tmp_path)

    def _boom():
        raise RuntimeError("模拟线程异常")

    runner = MotionRunner(FakeArm(), st)
    assert runner._spawn(_boom) is True          # noqa: SLF001 - 直接验兜底
    assert runner.wait(3.0) is True
    assert runner.state["mode"] == "error"
    assert "模拟线程异常" in runner.state["last_error"]
    assert runner.busy is False


def test_unknown_or_empty_plan_refused(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    st = _store(tmp_path)
    st.add_plan(MotionPlan("empty", source="本工程新建"))
    runner = MotionRunner(FakeArm(), st)
    assert runner.start_plan("nope") is False
    assert runner.start_plan("empty") is False


# ---------------------------------------------------------------- 方案回放
def test_plan_playback_sends_points_nonblocking_and_completes(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = FakeArm()
    runner = MotionRunner(arm, _store(tmp_path))
    assert runner.start_plan("p1") is True
    assert runner.wait(5.0)
    assert [c[0] for c in arm.calls] == ["movel", "movej"]
    for _name, _pos, _v, blend, connect, block in arm.calls:
        assert connect == 0 and block == 0          # 非阻塞下发，便于暂停/停止
        assert blend == 10
    assert arm.frames_called == 1                   # 执行前统一坐标系
    assert runner.state["mode"] == STATE_IDLE


def test_plan_pause_resume_stop(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = FakeArm(slow=True)                        # 永不到位，便于在中途暂停/停止
    st = _store(tmp_path)
    plan = st.config.find_plan("p1")
    plan.timeout = 60                               # 避免测试期间触发单点超时
    runner = MotionRunner(arm, st)

    assert runner.start_plan("p1") is True
    time.sleep(0.3)
    assert runner.state["mode"] == STATE_PLAYING

    runner.pause()
    time.sleep(0.3)
    assert runner.state["mode"] == STATE_PAUSED and arm.paused >= 1

    runner.resume()
    time.sleep(0.3)
    assert runner.state["mode"] == STATE_PLAYING and arm.continued >= 1

    runner.stop()
    assert runner.wait(3.0)
    assert arm.stopped >= 1
    assert runner.state["mode"] == STATE_IDLE


def test_point_timeout_stops_and_never_advances_without_user(tmp_path, monkeypatch):
    """单点到位超时：停下、**不下发下一点**；默认确认(无 UI)=拒绝 -> 中止方案。"""
    _gate_on(monkeypatch)
    arm = FakeArm(slow=True)
    st = _store(tmp_path)
    st.config.find_plan("p1").timeout = 1           # 1 秒超时
    runner = MotionRunner(arm, st)                        # 默认 confirm 一律拒绝
    assert runner.start_plan("p1") is True
    assert runner.wait(8.0)
    assert len(arm.calls) == 1                      # 只下发过第一点
    assert arm.stopped >= 1                         # 超时后停止
    assert runner.state["mode"] == STATE_IDLE


def test_point_timeout_user_chooses_retry(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = FakeArm(slow=True)
    st = _store(tmp_path)
    st.config.find_plan("p1").timeout = 1
    choices = ["重试本点", "中止方案"]
    runner = MotionRunner(arm, st, confirm=lambda *a: choices.pop(0) if choices else "中止方案")
    assert runner.start_plan("p1") is True
    assert runner.wait(10.0)
    assert len(arm.calls) == 2                      # 重试 -> 再次下发同一点
    assert runner.state["mode"] == STATE_IDLE


# ---------------------------------------------------------------- 引导定位（本地闭环）
def test_guide_detects_twice_and_moves_by_confirmed_offsets(tmp_path, monkeypatch):
    """粗/精各**检测一次**（中间移动），人工确认后按 [x/1000, 0, z/1000] 偏移。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    guide = FakeGuidance()
    runner = MotionRunner(arm, _store(tmp_path), confirm=lambda *a: "是",
                          guidance=guide)
    assert runner.start_guide(speed=8) is True
    assert runner.wait(5.0)
    assert guide.calls == 2                              # 两段各检测一次
    assert len(arm.offsets) == 2
    assert arm.offsets[0][0] == (0.010, 0.0, -0.005, 0.0, 0.0, 0.0)   # mm -> m，Y 不收
    assert arm.offsets[1][0] == (0.001, 0.0, 0.0005, 0.0, 0.0, 0.0)
    assert arm.offsets[0][1] == 8                                     # 速度用界面值
    assert runner.state["mode"] == STATE_IDLE


def test_guide_user_declines_no_motion(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = FakeArm()
    runner = MotionRunner(arm, _store(tmp_path), confirm=lambda *a: "否",
                          guidance=FakeGuidance())
    assert runner.start_guide() is True
    assert runner.wait(5.0)
    assert arm.offsets == []                              # 未确认 -> 不动


def test_guide_detection_failure_reports_error(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = FakeArm()
    runner = MotionRunner(arm, _store(tmp_path), confirm=lambda *a: "是",
                          guidance=FakeGuidance(ok=False, message="未找到候选目标"))
    assert runner.start_guide() is True
    assert runner.wait(5.0)
    assert runner.state["mode"] == "error"
    assert "未找到候选目标" in runner.state["last_error"]
    assert arm.offsets == []


def test_guide_without_service_reports_error(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    runner = MotionRunner(FakeArm(), _store(tmp_path))    # 未接入引导服务
    assert runner.start_guide() is True
    assert runner.wait(5.0)
    assert runner.state["mode"] == "error"
    assert "引导服务" in runner.state["last_error"]


def test_cancel_guide_stops_before_next_stage(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = FakeArm()
    guide = FakeGuidance()

    seen: list[str] = []

    def confirm(title, message, options):
        seen.append(title)
        if len(seen) == 1:
            return "是"          # 第一段(粗)确认 -> 移动
        runner.cancel_guide()    # 第二段(精)前取消
        return "否"

    runner = MotionRunner(arm, _store(tmp_path), confirm=confirm, guidance=guide)
    assert runner.start_guide() is True
    assert runner.wait(5.0)
    assert runner.state["mode"] == STATE_IDLE
    assert len(arm.offsets) == 1                          # 只完成了粗定位
    assert "取消" in runner.state["message"] or guide.calls >= 1


# ---------------------------------------------------------------- 套筒
def test_socket_move_y_axis(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = FakeArm()
    runner = MotionRunner(arm, _store(tmp_path))
    assert runner.request_socket_move(+5.0, speed=10) is True
    assert runner.wait(5.0)
    assert arm.offsets[0][0] == (0.0, 0.005, 0.0, 0.0, 0.0, 0.0)   # 前进=Y+
    assert runner.request_socket_move(-2.0) is True
    assert runner.wait(5.0)
    assert arm.offsets[1][0] == (0.0, -0.002, 0.0, 0.0, 0.0, 0.0)  # 后退=Y-


class _OffsetArm(FakeArm):
    """可指定 move_offset 返回码的假臂（测套筒偏移未到位分支）。"""

    def __init__(self, res: int = 0, **kw) -> None:
        super().__init__(**kw)
        self.off_res = res

    def move_offset(self, off, v=20, r=0, connect=0, block=1, stop_check=None,
                    tol=0.002, timeout=30.0):
        self.offsets.append((tuple(off), v))
        return self.off_res


def _socket_plan(st, plan_id="sock", mm_values=(8.0, -8.0, 8.0)):
    st.add_plan(MotionPlan(plan_id, "套筒偏移", "movel", 30, 10, 30,
                           [Waypoint("movel", (0.1, 0.2, 0.3, 0, 0, 0)),
                            *[Waypoint("socket_offset", (0, mm, 0, 0, 0, 0))
                              for mm in mm_values],
                            Waypoint("movej", (1, 2, 3, 4, 5, 6))],
                           source="本工程新建"))


def test_plan_replays_socket_offsets_mixed_with_points(tmp_path, monkeypatch):
    """方案里**混排**套筒偏移：方向取自 Y 分量正负号（+ = Y+，− = Y−），用引导速度，顺序不乱。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    st = _store(tmp_path)
    _socket_plan(st)
    runner = MotionRunner(arm, st)
    assert runner.start_plan("sock") is True
    assert runner.wait(5.0)
    # 点位运动走 movel/movej，套筒偏移走 move_offset —— 两条通道都在。
    assert [c[0] for c in arm.calls] == ["movel", "movej"]
    assert [o[0] for o in arm.offsets] == [
        (0.0, 0.008, 0.0, 0.0, 0.0, 0.0),      # +8mm -> Y+
        (0.0, -0.008, 0.0, 0.0, 0.0, 0.0),     # −8mm -> Y−
        (0.0, 0.008, 0.0, 0.0, 0.0, 0.0),      # +8mm -> Y+
    ]
    assert all(v == 10 for _off, v in arm.offsets)     # 固定用 profile.guide_move_speed
    assert runner.state["mode"] == STATE_IDLE


def test_socket_only_plan_runs_without_point_moves(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = FakeArm()
    st = _store(tmp_path)
    st.add_plan(MotionPlan("sock_only", "只取套筒", "movel", 30, 10, 30,
                           [Waypoint("socket_offset", (0, 20, 0, 0, 0, 0))],
                           source="本工程新建"))
    runner = MotionRunner(arm, st)
    assert runner.start_plan("sock_only") is True
    assert runner.wait(5.0)
    assert arm.calls == []                                # 没有点位运动
    assert arm.offsets[0][0] == (0.0, 0.02, 0.0, 0.0, 0.0, 0.0)


def test_tool_offset_plan_lifts_then_diagonal_back_to_target(tmp_path, monkeypatch):
    """DEC-060：先工具系 Z+5mm 抬升，再 (0,+100,−5)mm 一条 movel 斜回原目标位。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    arm._pose = [0.1, 0.2, 0.3, 0.0, 0.0, 0.0]          # noqa: SLF001 - 测试起点
    st = _store(tmp_path)
    st.add_plan(MotionPlan("lift100", "抬升5再斜回", "movel", 10, 0, 100,
                           [Waypoint("tool_offset", (0, 0, 5, 0, 0, 0)),
                            Waypoint("tool_offset", (0, 100, -5, 0, 0, 0))],
                           source="本工程新建"))
    runner = MotionRunner(arm, st)
    assert runner.start_plan("lift100") is True
    assert runner.wait(5.0)

    # 两次相对偏移都以“当前位姿”为基准：5mm 抬升 → (0,100,−5)mm 斜线。
    assert len(arm.tool_targets) == 2
    assert arm.tool_targets[0] == pytest.approx((0.0, 0.0, 0.005))
    assert arm.tool_targets[1] == pytest.approx((0.0, 0.1, -0.005))
    # 两个路点各下发一条 movel，非阻塞、速度用方案速度、交融 0（在抬升点停稳再斜走）。
    assert [c[0] for c in arm.calls] == ["movel", "movel"]
    assert arm.calls[0][1][:3] == pytest.approx((0.1, 0.2, 0.305))   # 抬升 5mm
    assert arm.calls[1][1][:3] == pytest.approx((0.1, 0.3, 0.3))     # 斜向回原目标
    for _name, _pos, v, blend, connect, block in arm.calls:
        assert v == 10 and blend == 0 and connect == 0 and block == 0
    # 终点 = 原目标位（相对起点 Y +100mm，Z 不变）。
    assert arm.get_current_pose()[:3] == pytest.approx([0.1, 0.3, 0.3])
    assert runner.state["mode"] == STATE_IDLE


def test_tool_offset_without_arm_support_fails_without_motion(tmp_path, monkeypatch):
    """机械臂适配器不支持算目标位姿时：步骤失败、不下发任何点位（不猜方向）。"""
    _gate_on(monkeypatch)

    class NoToolArm(FakeArm):
        tool_offset_target = None            # 属性存在但不可调用

    arm = NoToolArm()
    st = _store(tmp_path)
    st.add_plan(MotionPlan("lift", "抬升", "movel", 10, 0, 100,
                           [Waypoint("tool_offset", (0, 0, 5, 0, 0, 0))],
                           source="本工程新建"))
    runner = MotionRunner(arm, st)
    assert runner.start_plan("lift") is True
    assert runner.wait(5.0)
    assert arm.calls == []                   # 没有下发任何动作
    assert runner.state["mode"] == "error"
    assert "工具系" in runner.state["last_error"]


def test_socket_offset_negative_goes_y_minus(tmp_path, monkeypatch):
    """只含**负向**套筒偏移的方案：下发 Y−（脱离/后退），不按正数处理。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    st = _store(tmp_path)
    st.add_plan(MotionPlan("exit", "脱离套筒", "movel", 30, 10, 30,
                           [Waypoint("socket_offset", (0, -12, 0, 0, 0, 0))],
                           source="本工程新建"))
    runner = MotionRunner(arm, st)
    assert runner.start_plan("exit") is True
    assert runner.wait(5.0)
    assert arm.offsets == [((0.0, -0.012, 0.0, 0.0, 0.0, 0.0), 10)]


def test_socket_offset_supports_x_y_z_components(tmp_path, monkeypatch):
    """DEC-139：套筒偏移可填 Bolt 工具系 X/Y/Z 三轴，一次 move_offset 带全部分量。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    st = _store(tmp_path)
    st.add_plan(MotionPlan("sock_xyz", "三轴套筒偏移", "movel", 30, 10, 30,
                           [Waypoint("socket_offset", (5, 10, -2, 0, 0, 0))],
                           source="本工程新建"))
    runner = MotionRunner(arm, st)
    assert runner.start_plan("sock_xyz") is True
    assert runner.wait(5.0)
    assert arm.offsets == [((0.005, 0.010, -0.002, 0.0, 0.0, 0.0), 10)]
    assert runner.state["mode"] == STATE_IDLE


def test_tool_jog_moves_single_axis_with_speed(tmp_path, monkeypatch):
    """DEC-139：手动调整页的单轴点动（X/Y/Z ±mm）走同一 move_offset 原语。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    runner = MotionRunner(arm, _store(tmp_path))
    assert runner.request_tool_jog("X", 5.0, speed=23) is True
    assert runner.wait(5.0)
    assert arm.offsets[-1] == ((0.005, 0.0, 0.0, 0.0, 0.0, 0.0), 23)
    assert runner.request_tool_jog("z", -2.5, speed=15) is True
    assert runner.wait(5.0)
    assert arm.offsets[-1] == ((0.0, 0.0, -0.0025, 0.0, 0.0, 0.0), 15)
    # 非法轴：拒绝并置错误，不下发。
    before = len(arm.offsets)
    assert runner.request_tool_jog("A", 1.0) is False
    assert len(arm.offsets) == before
    assert "轴非法" in runner.state["last_error"]


def test_socket_action_not_arrived_never_auto_continues(tmp_path, monkeypatch):
    """套筒偏移未到位：停止并交人处理；默认确认=拒绝 -> 中止，**不下发下一点**。"""
    _gate_on(monkeypatch)
    arm = _OffsetArm(res=-3)
    st = _store(tmp_path)
    st.add_plan(MotionPlan("s", "d", "movel", 30, 10, 30,
                           [Waypoint("socket_offset", (0, 8, 0, 0, 0, 0)),
                            Waypoint("movej", (1, 2, 3, 4, 5, 6))],
                           source="本工程新建"))
    runner = MotionRunner(arm, st)                        # 默认 confirm 一律拒绝
    assert runner.start_plan("s") is True
    assert runner.wait(5.0)
    assert len(arm.offsets) == 1 and arm.calls == []      # 第 1 点未到位，未下发第 2 点
    assert arm.stopped >= 1
    assert runner.state["mode"] == STATE_IDLE


def test_socket_action_skip_continues_plan(tmp_path, monkeypatch):
    _gate_on(monkeypatch)
    arm = _OffsetArm(res=-3)
    st = _store(tmp_path)
    st.add_plan(MotionPlan("s", "d", "movel", 30, 10, 30,
                           [Waypoint("socket_offset", (0, 8, 0, 0, 0, 0)),
                            Waypoint("movej", (1, 2, 3, 4, 5, 6))],
                           source="本工程新建"))
    runner = MotionRunner(arm, st, confirm=lambda *a: "跳过本点(风险自担)")
    assert runner.start_plan("s") is True
    assert runner.wait(5.0)
    assert [c[0] for c in arm.calls] == ["movej"]         # 跳过未到位的套筒点，继续下一点
    assert runner.state["mode"] == STATE_IDLE




# ---------------------------------------------------------------- DEC-070 引导参数日志
def test_guide_params_logged_with_mapping_signs_and_tcp(tmp_path):
    """DEC-070：引导启动日志打印实际生效的轴映射/符号与 tcp，方向不对可现场核对。"""
    from types import SimpleNamespace

    from application.algorithms.guidance.offsets import AxisMapping, OffsetParams
    from application.services.guidance_config import GuidanceConfigStore

    gst = GuidanceConfigStore(None, seed_if_missing=False)
    gst.load()
    gst._config.offsets = OffsetParams(          # noqa: SLF001 - 直接摆一组映射
        coarse=AxisMapping("Z", 1, "X", 1),
        fine=AxisMapping("Z", 1, "X", 1),
        tcp_x=0.0, tcp_y=0.0, tcp_z=104.0)
    lines: list[str] = []
    runner = MotionRunner(FakeArm(), _store(tmp_path),
                          log=lines.append,
                          guidance=SimpleNamespace(config_store=gst))
    runner._log_guide_params(None)               # noqa: SLF001
    text = "\n".join(lines)
    assert "粗[相机X→Z(+) 相机Y→X(+)]" in text
    assert "精[相机X→Z(+) 相机Y→X(+)]" in text
    assert "tcp=(0, 0, 104)mm" in text


def test_fine_confirm_explains_tool_center_not_image_center(tmp_path, monkeypatch):
    """DEC-071：精定位确认框说明「工具中心对准圆心，相机画面圆心会偏离中心」，粗定位不提示。"""
    _gate_on(monkeypatch)
    prompts: list[tuple[str, str]] = []

    def confirm(title: str, message: str, options: list[str]) -> str | None:
        prompts.append((title, message))
        return "是"

    runner = MotionRunner(FakeArm(), _store(tmp_path), confirm=confirm,
                          guidance=FakeGuidance())
    assert runner.start_guide() is True
    assert runner.wait(5.0)
    fine = [m for t, m in prompts if "精定位" in t]
    coarse = [m for t, m in prompts if "粗定位" in t]
    assert fine and "工具中心" in fine[0] and "偏离画面中心" in fine[0]
    assert coarse and "工具中心" not in coarse[0]


# ---------------------------------------------------------------- DEC-118 手动圆 override
class _OverrideGuidance(FakeGuidance):
    """支持按操作员绘制圆心检测的假引导服务。"""

    def __init__(self) -> None:
        super().__init__()
        self.circle_calls = []

    def detect_with_circle(self, *, frame, cx, cy, radius_px=0.0,
                           fine_profile_ref=None):
        self.circle_calls.append((cx, cy, radius_px, frame))
        return GuidanceResult(
            True, "按操作员圆心", cam_xyz=(20.0, 0.0, 500.0),
            coarse=(2.5, -1.0), fine=(0.5, 0.2),
            circle=CircleHit(0, cx, cy, radius_px), used_depth_mm=500.0)


def test_guide_override_uses_manual_circle_and_moves(tmp_path, monkeypatch):
    """界面画/调过的圆作为粗定位依据：不重新找圆，按该圆偏移移动。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    guide = _OverrideGuidance()
    runner = MotionRunner(arm, _store(tmp_path), confirm=lambda *a: "是",
                          guidance=guide)
    frame = object()
    assert runner.start_guide(
        speed=10, stages=("coarse",),
        override={"frame": frame,
                  "circle": {"cx": 123.0, "cy": 45.0, "radius_px": 20.0},
                  "step_id": "motion_page:auto:box1"}) is True
    assert runner.wait(5.0)
    assert guide.circle_calls == [(123.0, 45.0, 20.0, frame)]
    assert guide.calls == 0                                    # 不重新自动找圆
    assert arm.offsets[0][0] == (0.0025, 0.0, -0.001, 0.0, 0.0, 0.0)
    assert arm.offsets[0][1] == 10                             # 速度用界面值
    assert runner.state["mode"] == STATE_IDLE


# ---------------------------------------------------------------- DEC-124 偏移上限
class _HugeGuidance(FakeGuidance):
    def __init__(self, coarse):
        super().__init__(coarse=coarse, fine=coarse)


def test_guide_rejects_offset_over_configured_limit(tmp_path, monkeypatch):
    """偏差超过调用方上限（手动引导=参数配置值）-> 拒绝移动，不下发。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    runner = MotionRunner(arm, _store(tmp_path), confirm=lambda *a: "是",
                          guidance=_HugeGuidance((80.0, -60.0)))
    assert runner.start_guide(stages=("coarse",), max_offset_mm=50.0) is True
    assert runner.wait(5.0)
    assert arm.offsets == []
    assert runner.state["mode"] == "error"
    assert "上限" in str(runner.state.get("last_error"))


def test_guide_rejects_offset_over_hard_cap_even_without_limit(tmp_path, monkeypatch):
    """即使上限配 0（关闭软上限），超 200mm 硬上限也必须拒绝。"""
    _gate_on(monkeypatch)
    arm = FakeArm()
    runner = MotionRunner(arm, _store(tmp_path), confirm=lambda *a: "是",
                          guidance=_HugeGuidance((250.0, -10.0)))
    assert runner.start_guide(stages=("coarse",), max_offset_mm=0.0) is True
    assert runner.wait(5.0)
    assert arm.offsets == []
    assert runner.state["mode"] == "error"
    assert "硬上限" in str(runner.state.get("last_error"))
