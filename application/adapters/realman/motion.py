"""真实机械臂运动控制（**1:1 移植**旧工程 `src/core/robot_arm.py`，真机实测通过）。

移植原则：**逻辑不改**。数值、判据、时序、以及下面这些踩坑结论都来自现场验证，
擅自"优化"等于把已验证的行为换成未验证的行为：

- 速度 ``v`` 是**示教器百分比**(1~100)，不是 mm/s；由 ``profile.speed_cap`` 钳制；
- 三代 ECO_65 的 ``rm_movel_offset`` 会返回 ``-7``（偏移未实现）→ **不用它**，
  改用「``rm_set_pos_step`` 步进 + ``teach_frame=1`` + Bolt 工具系 + 到位轮询」；
- 下发前要 ``rm_set_arm_continue()`` 解除上一次"停止"留下的抑制，
  否则表现为"停止后再点一次才动"；
- 进入偏移前记录并**还原** ``teach_frame`` 与工具系，避免污染后续绝对运动；
- ``get_current_pose``/``get_current_joint`` 失败一律返回 ``None``，
  **绝不返回占位/假数据**。

支持注入 ``sdk``（离线测试用 mock），未注入时才惰性导入厂家包。
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Sequence

from application.services.motion_config import RobotMotionProfile

DEFAULT_IP = "192.168.33.80"
DEFAULT_PORT = 8080
DEFAULT_MODEL = "ECO_65"
DEFAULT_THREAD_MODE = 2

# 到位判据（来自旧工程 _execute_plan / move_offset，勿改）
POS_TOL_M = 0.002          # 位姿模式位置容差(m)
JOINT_TOL_DEG = 1.0        # 关节模式角度容差(°)


class MotionArmError(RuntimeError):
    """运动控制不可用（未连接 / 缺 SDK / 切换失败）。"""


class RealmanMotionArm:
    """ECO65 真实运动控制（真机实测逻辑的移植）。"""

    def __init__(self, profile: RobotMotionProfile | None = None, *,
                 sdk: Any = None, ip: str = DEFAULT_IP, port: int = DEFAULT_PORT,
                 model: str = DEFAULT_MODEL,
                 thread_mode: int = DEFAULT_THREAD_MODE) -> None:
        self.profile = profile or RobotMotionProfile()
        self.ip = ip
        self.port = int(port)
        self.model = model
        self.thread_mode = int(thread_mode)
        self._sdk = sdk                     # 注入的 RoboticArm 实例（测试用）
        self._robot: Any = sdk              # 兼容旧命名
        self._handle: Any = None
        self.connected = False
        self.has_force_sensor = False
        self._frame_mode = "base"
        self._frame_name: str | None = None
        self._base_tool: str | None = None
        self._base_work: str | None = None

    # ---------- 连接 ----------
    def _ensure_sdk(self) -> None:
        if self._robot is not None:
            return
        try:
            from Robotic_Arm.rm_robot_interface import (   # type: ignore  # noqa: F401
                RoboticArm,
                rm_thread_mode_e,
            )
        except Exception as exc:  # noqa: BLE001
            from application.bootstrap.vendor_paths import describe_import_failure
            raise MotionArmError(describe_import_failure(
                "Robotic_Arm", exc,
                note="需 Robotic_Arm 包已挂载（包内自带 linux_arm/linux_x86 .so 与 win_64 DLL，"
                     "按平台自动选择）；用 --robot-sdk-dir 指定**包含 Robotic_Arm 的父目录**")) from exc
        self._robot = RoboticArm(rm_thread_mode_e(self.thread_mode))

    def connect(self) -> bool:
        self._ensure_sdk()
        self._handle = self._robot.rm_create_robot_arm(self.ip, self.port, 3)
        if getattr(self._handle, "id", -1) == -1:
            return False
        self.connected = True
        info = self.get_arm_info()
        ctrl_model = info.get("arm_model")
        # 型号以配置为准，不依赖控制器上报（官方 demo 曾因上报不符出错）
        if ctrl_model and str(ctrl_model) != str(self.model):
            self._warn(f"控制器上报型号={ctrl_model} 与配置型号={self.model} 不一致；"
                       f"一律以配置型号为准")
        self._capture_base_frame()
        return True

    def _capture_base_frame(self) -> None:
        """连接时捕获当前工具系/工作系，作为"复位到基坐标系"的基准。

        本程序绝对运动按基座坐标；若连接时已激活工作系，只告警、不擅自清除。
        """
        fl = self.get_frame_list()
        if not fl:
            return
        self._base_tool = fl.get("current_tool")
        self._base_work = fl.get("current_work")
        if self._base_work:
            self._warn(f"控制器当前激活工作系={self._base_work}，"
                       f"本程序绝对运动按基坐标系，可能与预期不符")

    def disconnect(self) -> None:
        if self._robot is not None:
            try:
                self._robot.rm_delete_robot_arm()
            except Exception:  # noqa: BLE001
                pass
        self.connected = False
        self._handle = None

    def get_arm_info(self) -> dict:
        if self._robot is None:
            return {}
        try:
            res, model = self._robot.rm_get_robot_info()
            if res == 0:
                self.has_force_sensor = bool(model.get("six_force_sensor", 0))
                return model
        except Exception as exc:  # noqa: BLE001
            self._warn(f"get_arm_info 失败: {exc}")
        return {}

    # ---------- 运动 ----------
    def _clamp_speed(self, v: Any) -> int:
        cap = int(self.profile.speed_cap)
        return int(min(max(int(v), 1), cap))

    def movej(self, joint: Sequence[float], v: int = 20, r: int = 0,
              connect: int = 0, block: int = 1) -> Any:
        return self._robot.rm_movej(list(joint), self._clamp_speed(v), r, connect, block)

    def movej_p(self, pose: Sequence[float], v: int = 20, r: int = 0,
                connect: int = 0, block: int = 1) -> Any:
        return self._robot.rm_movej_p(list(pose), self._clamp_speed(v), r, connect, block)

    def movel(self, pose: Sequence[float], v: int = 20, r: int = 0,
              connect: int = 0, block: int = 1) -> Any:
        return self._robot.rm_movel(list(pose), self._clamp_speed(v), r, connect, block)

    def movec(self, via: Sequence[float], to: Sequence[float], v: int = 20, r: int = 0,
              loop: int = 0, connect: int = 0, block: int = 1) -> Any:
        return self._robot.rm_movec(list(via), list(to), self._clamp_speed(v), r,
                                    loop, connect, block)

    def move_waypoints(self, path: Sequence[Sequence[float]], speed: int = 20,
                       blend: int = 12, block: bool = False) -> int:
        """批量下发：中间点 connect=1 融合、末点 connect=0；block=False 非阻塞。"""
        if not path:
            return 0
        speed = self._clamp_speed(speed)
        for i, pose in enumerate(path):
            last = (i == len(path) - 1)
            c = 0 if last else 1
            b = 0 if last else (0 if not block else 1)
            self.movel(pose, speed, blend, c, b)
        return 0

    # ---------- 坐标系 ----------
    @property
    def frame_mode(self) -> str:
        return self._frame_mode

    def set_frame_choice(self, mode: str, name: str | None = None) -> bool:
        """仅记录偏移运动所用坐标系，**不**改控制器全局激活系。

        真正的切换在 move_offset 内部瞬时完成并立即还原，避免 UI 选择污染后续绝对运动。
        """
        if mode not in ("base", "tool", "work"):
            return False
        self._frame_mode = mode
        self._frame_name = name if mode != "base" else None
        return True

    def get_frame_list(self) -> dict | None:
        """返回 {'tool':[...], 'work':[...], 'current_tool':.., 'current_work':..}；未连接 None。"""
        if self._robot is None:
            return None
        out = {"tool": [], "work": [], "current_tool": None, "current_work": None}
        try:
            r = self._robot.rm_get_total_tool_frame()
            if isinstance(r, dict) and r.get("return_code") == 0:
                out["tool"] = list(r.get("tool_names") or [])
            _, cur = self._robot.rm_get_current_tool_frame()
            if isinstance(cur, dict):
                out["current_tool"] = cur.get("name")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"查询工具系失败: {exc}")
        try:
            r = self._robot.rm_get_total_work_frame()
            if isinstance(r, dict) and r.get("return_code") == 0:
                out["work"] = list(r.get("work_names") or [])
            _, cur = self._robot.rm_get_current_work_frame()
            if isinstance(cur, dict):
                out["current_work"] = cur.get("name")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"查询工作系失败: {exc}")
        return out

    def select_frame(self, kind: str, name: str) -> bool:
        if self._robot is None:
            return False
        try:
            if kind == "tool":
                return self._robot.rm_change_tool_frame(name) == 0
            if kind == "work":
                return self._robot.rm_change_work_frame(name) == 0
        except Exception as exc:  # noqa: BLE001
            self._warn(f"切换坐标系失败: {exc}")
        return False

    def ensure_production_frames(self) -> tuple[bool, list[str]]:
        """断言/设置激活工具系为 Bolt、工作系为 Ganta（取自 profile）。

        运动方案回放 / 引导 / 套筒 开始前的全局坐标系统一守卫。
        返回 (是否已一致, 实际切换动作列表)。
        """
        if self._robot is None:
            return (False, [])
        want_tool = self.profile.active_tool or "Bolt"
        want_work = self.profile.active_work or "Ganta"
        fl = self.get_frame_list() or {}
        actions: list[str] = []
        if want_work and (fl.get("current_work") or "") != want_work:
            if self.select_frame("work", want_work):
                actions.append(f"work->{want_work}")
        if want_tool and (fl.get("current_tool") or "") != want_tool:
            if self.select_frame("tool", want_tool):
                actions.append(f"tool->{want_tool}")
        return (not actions, actions)

    def restore_base_frame(self) -> bool:
        """恢复到连接时捕获的基准系（无显式"清除工作系"接口，故只能恢复到当时状态）。"""
        if self._robot is None:
            return False
        try:
            if self._base_tool:
                self._robot.rm_change_tool_frame(self._base_tool)
            if self._base_work:
                self._robot.rm_change_work_frame(self._base_work)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"复位坐标系失败: {exc}")
            return False
        self._frame_mode = "base"
        self._frame_name = None
        return True

    # ---------- 偏移运动（真机验证方式：步进接口） ----------
    def _get_teach_frame(self) -> int:
        try:
            rc, ft = self._robot.rm_get_teach_frame()
            if rc == 0 and isinstance(ft, int):
                return ft
        except Exception:  # noqa: BLE001
            pass
        return 0

    @staticmethod
    def _rot_euler_zyx(v: Sequence[float], rx: float, ry: float, rz: float) -> list[float]:
        """向量按 RM 欧拉约定(R=Rz·Ry·Rx，弧度)旋转。"""
        x, y, z = v
        x, y, z = x, math.cos(rx) * y - math.sin(rx) * z, math.sin(rx) * y + math.cos(rx) * z
        x, y, z = math.cos(ry) * x + math.sin(ry) * z, y, -math.sin(ry) * x + math.cos(ry) * z
        x, y, z = math.cos(rz) * x - math.sin(rz) * y, math.sin(rz) * x + math.cos(rz) * y, z
        return [x, y, z]

    def _tool_axis_base_dir(self, axis: str) -> list[float] | None:
        """工具系某轴在基座系下的单位方向（用于投影到位判据）。失败 None。"""
        if self._robot is None:
            return None
        try:
            rc, fr = self._robot.rm_get_current_tool_frame()
            if rc != 0 or not isinstance(fr, dict):
                return None
            pose = fr.get("pose")
            euler = pose.get("euler") if isinstance(pose, dict) else None
            if euler is None:
                euler = fr.get("euler")
            if euler is None and isinstance(pose, (list, tuple)) and len(pose) >= 6:
                rx, ry, rz = (float(x) for x in pose[3:6])
            elif isinstance(euler, (list, tuple)) and len(euler) >= 3:
                rx, ry, rz = (float(x) for x in euler[:3])
            elif isinstance(euler, dict):
                rx = float(euler.get("rx", 0.0))
                ry = float(euler.get("ry", 0.0))
                rz = float(euler.get("rz", 0.0))
            else:
                return None
            local = {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}[axis]
            vec = self._rot_euler_zyx(local, rx, ry, rz)
            norm = math.sqrt(sum(x * x for x in vec))
            return None if norm < 1e-9 else [x / norm for x in vec]
        except Exception:  # noqa: BLE001
            return None

    def tool_offset_target(self, offset_m: Sequence[float]) -> list[float] | None:
        """按 **Bolt 工具系**平移偏移(米)算目标位姿（基座系 ``[x,y,z,rx,ry,rz]``）。

        供 ``tool_offset`` 路点使用：读取当前位姿与 Bolt 工具系三轴在基座系的方向，
        叠加 ``[dx,dy,dz]`` 得到目标位置，**姿态保持不变**；随后由调用方下发一条
        ``movel``（真直线）。任一读取/切换失败返回 ``None``（不造假、不猜方向）。
        """
        if self._robot is None or not self.connected:
            return None
        p0 = self.get_current_pose()
        if p0 is None or len(p0) < 6:
            return None
        tool = self.profile.active_tool or "Bolt"
        prev = self.get_frame_list() or {}
        prev_tool = prev.get("current_tool")
        switched = False
        try:
            if prev_tool != tool:
                if self._robot.rm_change_tool_frame(tool) != 0:
                    self._warn(f"tool_offset_target: 切换工具系 {tool} 失败")
                    return None
                switched = True
            dirs = {ax: self._tool_axis_base_dir(ax) for ax in ("x", "y", "z")}
            if any(v is None for v in dirs.values()):
                return None
            dx, dy, dz = (float(offset_m[0]), float(offset_m[1]), float(offset_m[2]))
            target = [float(p0[i])
                      + dx * dirs["x"][i] + dy * dirs["y"][i] + dz * dirs["z"][i]
                      for i in range(3)]
            target.extend(float(v) for v in p0[3:6])
            return target
        except Exception as exc:  # noqa: BLE001 - 读失败=不可执行, 如实返回
            self._warn(f"tool_offset_target 失败: {exc}")
            return None
        finally:
            if switched and prev_tool:
                try:
                    self._robot.rm_change_tool_frame(prev_tool)
                except Exception as exc:  # noqa: BLE001
                    self._warn(f"tool_offset_target: 还原工具系失败: {exc}")

    def move_offset(self, offset: Sequence[float], v: int = 20, r: int = 0,
                    connect: int = 0, block: int = 1,
                    stop_check: Callable[[], bool] | None = None,
                    tol: float = 0.002, timeout: float = 30.0) -> int:
        """工具系(Bolt)下沿轴平移偏移（**真机验证实现**，勿改）。

        offset: [dx,dy,dz,drx,dry,drz]（位置 m）；只做平移，姿态分量仅告警。
        返回 0=到位 / -1=未连接或切换失败 / -2=被 stop_check 中止 / -3=到位超时 / 其它=下发错误。
        """
        if self._robot is None or not self.connected:
            return -1
        v = self._clamp_speed(v)
        if any(abs(offset[i]) > 1e-9 for i in (3, 4, 5)):
            self._warn("move_offset: 仅支持平移偏移(xyz)，姿态分量被忽略")
        prev = self.get_frame_list() or {}
        prev_tool = prev.get("current_tool")
        prev_teach = self._get_teach_frame()
        tool = self.profile.active_tool or "Bolt"
        applied_tool = False
        try:
            if self._robot.rm_change_tool_frame(tool) == 0:
                applied_tool = True
            else:
                self._warn(f"move_offset: 切换工具系 {tool} 失败")
            self._robot.rm_set_teach_frame(1)          # 步进参考系 = 工具系
            try:
                # 解除上次"停止"留下的抑制，否则出现"停止后再点一次才动"
                self._robot.rm_set_arm_continue()
            except Exception:  # noqa: BLE001
                pass
            axis_dirs = {ax: self._tool_axis_base_dir(ax) for ax in ("x", "y", "z")}
            teach_enum = {"x": 0, "y": 1, "z": 2}
            last_res = 0
            for idx, ax in enumerate(("x", "y", "z")):
                val = float(offset[idx])
                if abs(val) <= 1e-9:
                    continue
                p0 = self.get_current_pose()
                res = self._robot.rm_set_pos_step(teach_enum[ax], val, int(v), 0)
                if res != 0:
                    self._warn(f"move_offset: rm_set_pos_step 返回 {res}（{ax} {val:.6f}m）")
                    return res
                t0 = time.time()
                settled = 0
                last = None
                while True:
                    if stop_check is not None and stop_check():
                        try:
                            self._robot.rm_set_arm_stop()
                        except Exception:  # noqa: BLE001
                            pass
                        return -2
                    cur = self.get_current_pose()
                    if cur and len(cur) >= 3 and p0 and len(p0) >= 3:
                        dvec = [cur[i] - p0[i] for i in range(3)]
                        if axis_dirs[ax]:
                            proj = sum(dvec[i] * axis_dirs[ax][i] for i in range(3))
                            if abs(proj - val) < tol:
                                last_res = 0
                                break
                        if last:                              # 收敛兜底
                            mv = math.sqrt(sum((cur[i] - last[i]) ** 2 for i in range(3)))
                            settled = settled + 1 if mv < tol * 0.5 else 0
                            if settled >= 4:
                                last_res = 0
                                break
                    last = cur
                    if time.time() - t0 > timeout:
                        try:
                            self._robot.rm_set_arm_stop()
                        except Exception:  # noqa: BLE001
                            pass
                        self._warn(f"move_offset: 到位超时({timeout:.0f}s)，已停止")
                        return -3
                    time.sleep(0.05)
            return last_res
        finally:
            try:
                self._robot.rm_set_teach_frame(prev_teach if prev_teach in (0, 1) else 0)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"move_offset: 还原 teach_frame 失败: {exc}")
            try:
                if applied_tool and prev_tool:
                    self._robot.rm_change_tool_frame(prev_tool)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"move_offset: 还原工具系失败: {exc}")

    # ---------- 夹爪（取套筒） ----------
    def grip_socket(self, speed: int = 200, force: int = 50, block: bool = True,
                    timeout: int = 10) -> Any:
        return self._robot.rm_set_gripper_pick_on(speed, force, block, timeout)

    def release_socket(self, speed: int = 200, block: bool = True, timeout: int = 10) -> Any:
        return self._robot.rm_set_gripper_release(speed, block, timeout)

    # ---------- IO ----------
    def set_do(self, io_num: int, state: bool) -> Any:
        return self._robot.rm_set_do_state(io_num, state)

    def get_di(self) -> Sequence[bool]:
        res, arr = self._robot.rm_get_io_input()
        return arr if res == 0 else []

    # ---------- 暂停 / 继续 / 停止 ----------
    def pause(self) -> Any:
        return self._robot.rm_set_arm_pause()

    def continue_(self) -> Any:
        return self._robot.rm_set_arm_continue()

    def stop(self) -> Any:
        return self._robot.rm_set_arm_stop()

    def delete_trajectory(self) -> Any:
        return self._robot.rm_set_arm_delete_trajectory()

    # ---------- 查询 ----------
    def get_current_pose(self) -> list[float] | None:
        """当前末端位姿（基座系 [x,y,z,rx,ry,rz]）。失败返回 None，**绝不造假**。"""
        if self._robot is None:
            return None
        try:
            res, state = self._robot.rm_get_current_arm_state()
            if res == 0 and isinstance(state, dict) and state.get("pose"):
                return list(state["pose"])
        except Exception as exc:  # noqa: BLE001
            self._warn(f"get_current_pose 失败: {exc}")
        return None

    def get_current_joint(self) -> list[float] | None:
        """当前关节角（6 维，度）。失败返回 None，**绝不造假**。"""
        if self._robot is None:
            return None
        try:
            res, state = self._robot.rm_get_current_arm_state()
            if res == 0 and isinstance(state, dict) and state.get("joint"):
                return [float(j) for j in state["joint"]]
        except Exception as exc:  # noqa: BLE001
            self._warn(f"get_current_joint 失败: {exc}")
        return None

    def status(self) -> dict:
        """给设备状态注册表看的接口（会话真伪以此为据）。"""
        return {"arm": self.model, "open": bool(self.connected),
                "connected": bool(self.connected), "ip": self.ip, "port": self.port,
                "tool": self.profile.active_tool, "work": self.profile.active_work}

    # ---------- 内部 ----------
    def _warn(self, message: str) -> None:
        print(f"[MotionArm] {message}")


class NullMotionArm:
    """未接入机械臂时的占位：``connected=False``，任何调用**明确失败**（不留假替身）。

    页面据此显示"机械臂未连接"并禁用按钮；绝不用假对象让界面看起来能用。
    """

    connected = False

    def __init__(self, profile: RobotMotionProfile | None = None) -> None:
        self.profile = profile or RobotMotionProfile()

    def status(self) -> dict:
        return {"arm": "none", "open": False, "connected": False,
                "reason": "未接入机械臂（profile=offline 或未选 eco65）"}

    def __getattr__(self, name: str) -> Any:
        def _denied(*_a: Any, **_k: Any) -> Any:
            raise MotionArmError(f"机械臂未接入，无法调用 {name!r}")
        return _denied


__all__ = [
    "DEFAULT_IP", "DEFAULT_MODEL", "DEFAULT_PORT", "DEFAULT_THREAD_MODE",
    "JOINT_TOL_DEG", "MotionArmError", "NullMotionArm", "POS_TOL_M",
    "RealmanMotionArm",
]
