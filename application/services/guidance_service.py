"""引导定位服务：深度引导在**本程序内闭环**（不再连旧的视觉程序）。

默认用 DS86 抓图；DEC-137 起只要设备配置里启用了相机通道（如 D405 USB），
粗/精定位都可以指定该相机（抓图→深度引导→找圆→偏差 的算法链与相机无关）。

流程（与旧工程一致，只是不再跨进程/跨机器）：

    抓相机帧 → run_depth_guidance(深度切分…轮廓…正外接矩形)
      → 每个矩形经 **cp38 宿主**找圆 → 取圆心
      → point_depth_xyz(圆心均值深度) → 相机系 XYZ(mm)
      → 按轴映射算粗/精偏差 (x,z) mm

**找圆必须走 cp38 宿主**：`detect_circle_imp` 是 3.8 原生件，主程序不得导入
（见 `application/hosts/circle38`）。宿主不可用时**明确报错，不给假偏移**。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Sequence, Tuple

from application.algorithms.circle.params import CircleParams
from application.algorithms.guidance.depth_guidance import (
    DepthGuidanceParams,
    depth_to_mm,
    point_depth_xyz,
    run_depth_guidance,
)
from application.algorithms.guidance.offsets import compute_robot_offsets
from application.algorithms.guidance.pixel_ops import compute_pixel_xyz
from application.contracts.enums import GUIDANCE_CAMERA_ID
from application.contracts.frames import CaptureRequest, FreshnessPolicy
from application.infrastructure import logging_setup
from application.services.guidance_config import GuidanceConfigStore

# 细粒度排查日志（DEC-058）：落**文件**（DEBUG），不进事件库，避免刷屏；
# 里程碑仍走注入的 ``log`` 回调（INFO，进文件+事件库）。
_log_dbg = logging_setup.get_logger("guidance")

# 默认相机（DEC-034 的向后兼容默认值）：配方校验/编辑器默认值共用同一常量。
# DEC-137：引导定位不再**只认** DS86 —— 只要设备配置里启用了相机通道，
# 粗/精定位都可以用（例如 D405 USB）；`camera_id` 为空时仍回退这台默认相机。
DEFAULT_CAMERA_ID = GUIDANCE_CAMERA_ID


class GuidanceError(RuntimeError):
    """引导定位不可用（相机/宿主/参数缺失）。"""


@dataclass(frozen=True)
class CircleHit:
    """一个检出圆（全图坐标）。``diameter_mm`` 由找圆链按圆心深度换算（DEC-147，
    0 表示深度/内参不可用、如实不给假数）。"""

    rect_index: int
    cx: float
    cy: float
    radius: float
    diameter_mm: float = 0.0


@dataclass
class GuidanceResult:
    """一次引导检测的结果。``ok=False`` 时 message 说明原因，**不给偏移数值**。"""

    ok: bool
    message: str = ""
    cam_xyz: Optional[Tuple[float, float, float]] = None
    coarse: Optional[Tuple[float, float]] = None      # 粗定位 (x,z) mm
    fine: Optional[Tuple[float, float]] = None        # 精定位 (x,z) mm
    circle: Optional[CircleHit] = None
    rects: list = field(default_factory=list)
    debug_image: Any = None
    depth_mm: Any = None
    used_depth_mm: float = 0.0


def extract_circles(ret: dict) -> list[dict]:
    """从 cp38 返回结果提取圆（移植旧 `_extract_circles`）。

    兼容 ``circles_for_show`` 的多种包裹层数（DEC-054）：**扁平**
    ``[[flag,cx,cy,r], ...]``、**按图/按矩形分组** ``[[[...]]]`` 乃至更深的空占位
    （``[[]]`` / ``[[[]]]``）。空列表与 ``None`` 只是"没有圆"，**不是畸形**，一律跳过；
    ``flag`` 为假的候选忽略。列表里没有有效圆时回退 ``circle_centers``/``radius``。
    """
    circles: list[dict] = []

    def _take(node: object) -> bool:
        if not isinstance(node, (list, tuple)):
            return False
        if not node:
            return False
        if isinstance(node[0], (list, tuple)) or node[0] is None:
            found = False
            for sub in node:
                found = _take(sub) or found
            return found
        if len(node) < 4:
            return False
        flag, cx, cy, r = node[0], node[1], node[2], node[3]
        if not flag:
            return False
        try:
            circles.append({"cx": int(cx), "cy": int(cy), "radius": float(r)})
        except (TypeError, ValueError):
            return False
        return True

    def _numbers(node: object, out: list) -> None:
        """展开半径等数值列表的任意层包裹（与判定层同口径）。"""
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            if not node:
                return
            if isinstance(node[0], (list, tuple)) or node[0] is None:
                for sub in node:
                    _numbers(sub, out)
                return
            out.extend(node)
            return
        out.append(node)

    def _centers(node: object, out: list) -> None:
        """展开 ``circle_centers`` 的任意层包裹；空占位跳过（DEC-055：层数不固定）。"""
        if node is None or (isinstance(node, (list, tuple)) and not node):
            return
        if not isinstance(node, (list, tuple)):
            return
        if isinstance(node[0], (list, tuple)) or node[0] is None:
            for sub in node:
                _centers(sub, out)
            return
        if len(node) >= 3:
            out.append(node)

    if not _take(ret.get("circles_for_show")):
        centers: list = []
        _centers(ret.get("circle_centers") or [], centers)
        radii: list = []
        _numbers(ret.get("radius") or [], radii)
        for ci_idx, cc in enumerate(centers):
            r = 0.0
            if ci_idx < len(radii):
                try:
                    r = float(radii[ci_idx])
                except (TypeError, ValueError):
                    r = 0.0
            try:
                circles.append({"cx": int(cc[1]), "cy": int(cc[2]), "radius": r})
            except (TypeError, ValueError):
                continue
    return circles


def intrinsics_to_dict(intrinsics: Any) -> dict:
    """契约 Intrinsics → 算法需要的 {fx,fy,ppx,ppy}（cx/cy 映射到 ppx/ppy）。"""
    return {"fx": float(intrinsics.fx), "fy": float(intrinsics.fy),
            "ppx": float(intrinsics.cx), "ppy": float(intrinsics.cy),
            "width": int(intrinsics.width), "height": int(intrinsics.height)}


def circle_diameter_mm(radius_px: float, depth_mm: float, intr: dict) -> float:
    """圆半径(px)+圆心处所用深度(mm)+内参 → 物理直径(mm)，DEC-147。

    ``直径 = 2·r_px·Z / √(fx·fy)``（Z 即找圆+均值深度流程里圆心处实际用的深度）。
    参数缺失/非正时返回 0.0——如实不给假数，由界面决定是否显示。
    """
    try:
        fx = float(intr.get("fx") or 0.0)
        fy = float(intr.get("fy") or 0.0)
        r = float(radius_px)
        z = float(depth_mm)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if r <= 0 or z <= 0 or fx <= 0 or fy <= 0:
        return 0.0
    return 2.0 * r * z / math.sqrt(fx * fy)


class GuidanceService:
    """本地深度引导：抓图 → 找圆 → 相机 XYZ → 粗/精偏差。"""

    def __init__(self, config_store: GuidanceConfigStore, *,
                 camera: Any = None, host_proxy: Any = None,
                 camera_id: str = DEFAULT_CAMERA_ID,
                 cameras: dict[str, Any] | None = None,
                 log=None) -> None:
        self.config_store = config_store
        self.camera = camera                 # 默认相机适配器（Fake 或 real）
        self.host_proxy = host_proxy         # cp38 圆宿主代理（Circle38Proxy）
        self.camera_id = camera_id
        # DEC-137：多相机支持 —— 生产栈传入**同一个**（热重载就地更新的）cameras 字典；
        # 检测时按 camera_id 取当时的适配器，不把"启动时那一台"焊死。
        self.cameras = cameras if cameras is not None else {}
        self.last_camera_id = camera_id
        # DEC-084：最近一次引导实际使用的帧（作业台右卡在粗/精定位确认时显示圆）
        self.last_frame: Any = None
        self._log = log or (lambda _m: None)

    # ---------- 相机选择 ----------
    def camera_ids(self) -> list[str]:
        """当前**有采集会话**的相机 ID（设备配置已启用且本次运行连接成功）。"""
        ids = set(self.cameras)
        if self.camera is not None and self.camera_id:
            ids.add(self.camera_id)
        return sorted(ids)

    def _adapter(self, camera_id: str | None) -> tuple[str, Any]:
        """返回 ``(camera_id, adapter)``；adapter 缺失时由调用方如实报错。"""
        cid = str(camera_id or self.camera_id or DEFAULT_CAMERA_ID)
        adapter = self.cameras.get(cid) if self.cameras else None
        if adapter is None and cid == self.camera_id:
            adapter = self.camera      # 兼容只注入单台适配器（离线/测试）的旧用法
        return cid, adapter

    # ---------- 主入口 ----------
    def detect(self, frame: Any = None, *, prefer_center: bool = False,
               fine_profile_ref: str | None = None,
               camera_id: str | None = None) -> GuidanceResult:
        """检测 → 相机 XYZ → 粗/精偏差。

        ``prefer_center=True``：在多个有效圆中选**离相机光心最近**的那个（用于精定位——
        粗定位已把选定的目标移到视野中心，重新检测时不能被"第一个自动圆"带偏）。
        ``fine_profile_ref``：选用哪一套**精定位偏移参数**（DEC-065；空=默认套，
        未知=如实失败，不悄悄回退）。
        ``camera_id``（DEC-137）：用哪台相机抓图（空=引导服务默认相机）。
        深度引导/找圆链路与相机无关，任何提供 color+depth+内参 的相机通道都可用。
        默认 False 保持旧程序行为：按顺序取第一个深度有效的圆。
        """
        cfg = self.config_store.config
        params = cfg.offsets_for(fine_profile_ref)
        if params is None:
            return GuidanceResult(
                False, f"精定位参数套 {fine_profile_ref!r} 不存在（请到「参数配置」页检查）")
        cid = str(camera_id or self.camera_id or DEFAULT_CAMERA_ID)
        t_all = time.monotonic()
        t0 = time.monotonic()
        if frame is None:
            # DEC-058 排查：抓图是最容易卡住的一步，先落一行再进 SDK
            _log_dbg.debug("[Guide] 开始抓图 相机=%s", cid)
        try:
            frame = frame if frame is not None else self._capture(cid)
        except Exception as exc:  # noqa: BLE001
            _log_dbg.debug("[Guide] 抓图失败 用时 %.2fs 原因=%s",
                           time.monotonic() - t0, exc)
            return GuidanceResult(False, f"抓图失败: {exc}")
        _log_dbg.debug("[Guide] 取帧完成 用时 %.2fs 帧id=%s 深度=%s",
                       time.monotonic() - t0, getattr(frame, "frame_id", None),
                       getattr(getattr(getattr(frame, "depth", None), "data", None),
                               "shape", None))
        if frame is None:
            return GuidanceResult(
                False, f"未取到相机 {cid!r} 的帧（未连接或抓拍失败）")
        self.last_frame = frame
        self.last_camera_id = str(getattr(frame, "camera_id", "") or cid)

        depth = getattr(getattr(frame, "depth", None), "data", None)
        scale = getattr(frame, "depth_scale_m", None)
        if depth is None or not scale:
            return GuidanceResult(False, "帧缺原始深度或 depth_scale，不能做度量")
        intrinsics = getattr(frame, "intrinsics", None)
        if intrinsics is None:
            return GuidanceResult(False, "帧缺内参，不能换算 XYZ")
        color = getattr(getattr(frame, "color", None), "data", None)

        t_depth = time.monotonic()
        res = run_depth_guidance(depth, scale, cfg.depth, color_image=color,
                                 include_rects=True)
        _log_dbg.debug("[Guide] 深度引导完成 用时 %.2fs 轮廓=%s 矩形=%d",
                       time.monotonic() - t_depth, res.get("n_contours"),
                       len(res.get("rects") or []))
        rects = res.get("rects") or []
        if not rects:
            return GuidanceResult(
                False, f"未找到候选目标（过滤后轮廓数={res.get('n_contours', 0)}）",
                rects=[], debug_image=res.get("debug_image"), depth_mm=res.get("depth_mm"))

        image_for_circle = color if color is not None else res.get("contour_image")
        t_circle = time.monotonic()
        try:
            hits = self._find_circles(image_for_circle, rects, cfg.circle)
        except Exception as exc:  # noqa: BLE001 - 宿主不可用等: 如实报告, 不给假偏移
            return GuidanceResult(False, f"找圆不可用: {exc}", rects=rects,
                                  debug_image=res.get("debug_image"),
                                  depth_mm=res.get("depth_mm"))
        _log_dbg.debug("[Guide] 找圆阶段完成 用时 %.2fs 命中=%d",
                       time.monotonic() - t_circle, len(hits))
        if not hits:
            return GuidanceResult(False, f"{len(rects)} 个矩形内均未找到圆",
                                  rects=rects, debug_image=res.get("debug_image"),
                                  depth_mm=res.get("depth_mm"))

        # 与旧工程一致：按矩形/圆的顺序取第一个**深度有效**的圆心。
        # ``prefer_center``（精定位）则在有效圆中取离光心最近的：粗定位已把选定的圆
        # 移到中心；若仍取 hits[0]，多圆场景会继续按"原始自动选择的圆"移动（用户实测）。
        intr_dict = intrinsics_to_dict(intrinsics)
        ppx = float(intr_dict.get("ppx", intr_dict.get("cx", 0.0)))
        ppy = float(intr_dict.get("ppy", intr_dict.get("cy", 0.0)))
        hit = None
        xyz = None
        used = 0.0
        best_key: float | None = None
        for candidate in hits:
            c_xyz, c_used = point_depth_xyz(res["depth_mm"], candidate.cx,
                                            candidate.cy, intr_dict, cfg.depth)
            if c_xyz is None:
                # 均值深度无效时回退到螺旋搜索单点深度（与旧工程一致）
                fallback = compute_pixel_xyz(candidate.cx, candidate.cy, depth,
                                             intr_dict, scale)
                if fallback is None:
                    continue
                c_xyz = (fallback[0], fallback[1], fallback[2])
                c_used = float(fallback[2])
            if not prefer_center:
                hit, xyz, used = candidate, c_xyz, c_used
                break
            key = (candidate.cx - ppx) ** 2 + (candidate.cy - ppy) ** 2
            if best_key is None or key < best_key:
                hit, xyz, used, best_key = candidate, c_xyz, c_used, key
        if hit is None or xyz is None:
            return GuidanceResult(False, "所有圆心处深度均无效（均值与单点都取不到）",
                                  rects=rects, circle=hits[0],
                                  debug_image=res.get("debug_image"),
                                  depth_mm=res.get("depth_mm"))
        if prefer_center:
            _log_dbg.debug(
                "[Guide] 精定位选圆：选中 (%s,%s) r=%.1fpx，离光心 %.1fpx（候选 %d，"
                "总命中 %d）", hit.cx, hit.cy, hit.radius,
                float(best_key) ** 0.5 if best_key is not None else -1.0,
                len(hits), len(hits))
        # DEC-147 修复：CircleHit 是 frozen dataclass，用 replace 生成带直径的新实例
        hit = replace(hit, diameter_mm=circle_diameter_mm(
            hit.radius, used, intr_dict))
        coarse, fine = compute_robot_offsets(xyz, params)
        _log_dbg.debug("[Guide] 检测完成 总用时 %.2fs", time.monotonic() - t_all)
        self._log(f"[Guide] 相机系 XYZ=({xyz[0]:.2f}, {xyz[1]:.2f}, {xyz[2]:.0f})mm "
                  f"深度={used:.0f}mm 直径≈{hit.diameter_mm:.1f}mm → 粗={coarse} 精={fine}")
        return GuidanceResult(True, "检测成功", cam_xyz=tuple(xyz), coarse=coarse,
                              fine=fine, circle=hit, rects=rects,
                              debug_image=res.get("debug_image"),
                              depth_mm=res.get("depth_mm"), used_depth_mm=used)

    def detect_fine(self, frame: Any = None,
                    fine_profile_ref: str | None = None,
                    camera_id: str | None = None) -> GuidanceResult:
        """精定位检测：重新抓图找圆，但在多个圆中**优先离光心最近**的有效圆。

        粗定位已把操作员选定的圆移到相机中心附近；若仍取第一个自动圆，多圆场景会
        按"原始自动选择的圆"继续移动（用户 2026-10-22 实测反馈）。选不到中心附近的
        仍然按距离排序取最近的有效圆；没有有效圆则如实失败。
        ``fine_profile_ref``：该步骤选用的精定位参数套（DEC-065）。
        ``camera_id``（DEC-137）：用哪台相机抓图（空=默认相机）。
        """
        return self.detect(frame=frame, prefer_center=True,
                           fine_profile_ref=fine_profile_ref, camera_id=camera_id)

    def detect_with_circle(self, *, frame: Any, cx: float, cy: float,
                           radius_px: float = 0.0,
                           fine_profile_ref: str | None = None) -> GuidanceResult:
        """按**视觉判定步骤的帧与人工确认圆心**直接算 XYZ/粗精偏移（DEC-048）。

        不重新抓图、不重新找圆：帧由视觉步骤提供，圆心是作业台人工调整后的结果。
        深度无效时与 ``detect`` 同样回退螺旋搜索单点深度；仍无效则明确失败，
        **不给假偏移**。``fine_profile_ref`` 选择精定位参数套（DEC-065）。
        """
        cfg = self.config_store.config
        params = cfg.offsets_for(fine_profile_ref)
        if params is None:
            return GuidanceResult(
                False, f"精定位参数套 {fine_profile_ref!r} 不存在（请到「参数配置」页检查）")
        t0 = time.monotonic()
        _log_dbg.debug("[Guide] 按视觉步骤圆心检测 开始 圆心=(%s,%s) r=%s 帧id=%s",
                       cx, cy, radius_px, getattr(frame, "frame_id", None))
        if frame is None:
            return GuidanceResult(False, "缺少视觉步骤帧，不能按手动圆心定位")
        self.last_frame = frame
        self.last_camera_id = str(getattr(frame, "camera_id", "") or self.camera_id)
        try:
            cx, cy = float(cx), float(cy)
        except (TypeError, ValueError):
            return GuidanceResult(False, "圆心坐标非法")
        if not (cx == cx and cy == cy):  # NaN 检查（不引入额外依赖）
            return GuidanceResult(False, "圆心坐标非法")
        depth = getattr(getattr(frame, "depth", None), "data", None)
        scale = getattr(frame, "depth_scale_m", None)
        if depth is None or not scale:
            return GuidanceResult(False, "帧缺原始深度或 depth_scale，不能做度量")
        intrinsics = getattr(frame, "intrinsics", None)
        if intrinsics is None:
            return GuidanceResult(False, "帧缺内参，不能换算 XYZ")
        depth_mm = depth_to_mm(depth, float(scale))
        intr_dict = intrinsics_to_dict(intrinsics)
        hit = CircleHit(0, cx, cy, float(radius_px or 0.0))
        xyz, used = point_depth_xyz(depth_mm, cx, cy, intr_dict, cfg.depth)
        if xyz is None:
            fallback = compute_pixel_xyz(cx, cy, depth, intr_dict, float(scale))
            if fallback is None:
                return GuidanceResult(
                    False, "圆心处深度无效（均值与单点均取不到），不能按手动圆心定位",
                    circle=hit, depth_mm=depth_mm)
            xyz = (fallback[0], fallback[1], fallback[2])
            used = float(fallback[2])
        hit = replace(hit, diameter_mm=circle_diameter_mm(
            hit.radius, used, intr_dict))
        coarse, fine = compute_robot_offsets(xyz, params)
        _log_dbg.debug("[Guide] 按视觉步骤圆心检测 完成 用时 %.2fs",
                       time.monotonic() - t0)
        self._log(f"[Guide] 视觉步骤圆心 ({cx:.0f},{cy:.0f}) 相机系 XYZ="
                  f"({xyz[0]:.2f}, {xyz[1]:.2f}, {xyz[2]:.0f})mm 深度={used:.0f}mm "
                  f"直径≈{hit.diameter_mm:.1f}mm → 粗={coarse} 精={fine}")
        return GuidanceResult(True, "按视觉步骤圆心检测成功", cam_xyz=tuple(xyz),
                              coarse=coarse, fine=fine, circle=hit, rects=[],
                              depth_mm=depth_mm, used_depth_mm=used)

    # ---------- 内部 ----------
    def capture_frame(self, camera_id: str | None = None) -> Any:
        """公开抓图（「运动方案」页单次视觉检测用，DEC-109）。

        只抓一帧，不做深度引导/找圆；结果同时记到 ``last_frame``。
        ``camera_id``（DEC-137）：页面选择的引导相机（空=默认相机）。
        抓图异常按 ``GuidanceError`` 抛出，由调用方如实展示。
        """
        frame = self._capture(camera_id)
        if frame is not None:
            self.last_frame = frame
        return frame

    def _capture(self, camera_id: str | None = None) -> Any:
        cid, camera = self._adapter(camera_id)
        if camera is None:
            raise GuidanceError(
                f"相机 {cid!r} 没有可用的采集会话（设备配置未启用或本次运行未连接）")
        # DEC-137：D405 USB 只能给"到达即可"的帧（未验证曝光时间），
        # 用适配器自报能力决定新鲜度策略；DS86/S100 仍要求设备时间。
        policy = (FreshnessPolicy.ALLOW_ARRIVAL_ONLY
                  if not getattr(camera, "supports_device_time", True)
                  else FreshnessPolicy.REQUIRE_DEVICE_TIME)
        cs = camera.capture(CaptureRequest(
            camera_ids=(cid,), request_id="guidance",
            step_id="guidance", allow_partial=True, freshness_policy=policy))
        err = cs.errors.get(cid)
        if err:
            raise GuidanceError(err)
        frame = cs.frames.get(cid)
        if frame is not None:
            self.last_camera_id = cid
            self.last_frame = frame       # DEC-084：供作业台右卡显示引导画面/圆
        return frame

    def _find_circles(self, image: Any, rects: Sequence[dict],
                      circle_raw: dict) -> list[CircleHit]:
        if self.host_proxy is None:
            raise GuidanceError(
                "cp38 圆宿主不可用：找圆需要 3.8 原生件（见 E06/A03），"
                "当前不给假偏移")
        if image is None:
            return []
        known = set(CircleParams.__dataclass_fields__)
        params = CircleParams(**{k: v for k, v in (circle_raw or {}).items()
                                 if k in known})
        hits: list[CircleHit] = []
        for rect in rects:
            x1, y1 = int(rect["x1"]), int(rect["y1"])
            w, h = int(rect["x2"]) - x1, int(rect["y2"]) - y1
            if w < 5 or h < 5:
                continue
            algo_rect = [2, x1, y1, w, h, 0]
            t0 = time.monotonic()
            # DEC-058 排查：找圆走 cp38 子进程，卡住时这里是最后一行
            _log_dbg.debug("[Guide] 找圆请求 rect#%s (%d,%d %dx%d) → cp38 宿主",
                           rect.get("idx", 0), x1, y1, w, h)
            try:
                # 与 CircleDetector 同一约定: rects 为**扁平列表**（本帧只送这一个矩形）
                ret = self.host_proxy.detect([image], [algo_rect], params)
            except Exception as exc:  # noqa: BLE001 - 单矩形失败不影响其它
                _log_dbg.debug("[Guide] 找圆异常 rect#%s 用时 %.2fs 原因=%s",
                               rect.get("idx", 0), time.monotonic() - t0, exc)
                self._log(f"[Guide] 矩形#{x1},{y1} 找圆异常: {exc}")
                continue
            _log_dbg.debug("[Guide] 找圆返回 rect#%s 用时 %.2fs",
                           rect.get("idx", 0), time.monotonic() - t0)
            for c in extract_circles(ret):
                hits.append(CircleHit(rect_index=int(rect.get("idx", 0)),
                                      cx=float(c["cx"]), cy=float(c["cy"]),
                                      radius=float(c["radius"])))
        return hits


__all__ = ["CircleHit", "GuidanceError", "GuidanceResult", "GuidanceService",
           "circle_diameter_mm", "extract_circles", "intrinsics_to_dict"]
