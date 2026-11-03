"""找圆调试服务：在**已抓拍的一帧**上分步跑 DS86「深度引导找圆」链，供视觉调试页显示。

与 :class:`application.services.guidance_service.GuidanceService` 的分工：

- ``GuidanceService.detect`` 是**闭环**（抓图 → 深度引导 → 找圆 → XYZ → 粗/精偏移），
  给配方「引导定位」步骤用；
- 本模块面向**调试**：帧由页面传入、不抓图、不动配方；返回每个步骤的阶段图、
  **全部**矩形与全部圆、逐矩形的失败原因，以及选中圆的 XYZ/偏移。同一帧可反复
  调参重跑。

算法判据全部复用既有模块（1:1 旧工程移植），这里**不复制判据**：

    深度切分/归一化/二值化/形态学/轮廓过滤/正外接矩形 → ``run_depth_guidance``
    找圆（cp38 宿主，detect_circle_imp）            → 与 ``GuidanceService`` 同一约定
    圆心 XYZ（均值深度 / 螺旋搜索兜底）             → ``point_depth_xyz`` + ``compute_pixel_xyz``
    粗/精偏移                                      → ``compute_robot_offsets``

**找圆必须走 cp38 宿主**（3.8 原生件，主程序不得导入）；宿主缺失或抛异常时如实
返回 ``stage="circle"`` 与原因，**绝不造圆、不给假偏移**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from application.algorithms.circle.params import CircleParams
from application.algorithms.guidance.depth_guidance import (
    DepthGuidanceParams,
    point_depth_xyz,
    run_depth_guidance,
)
from application.algorithms.guidance.offsets import OffsetParams, compute_robot_offsets
from application.algorithms.guidance.pixel_ops import compute_pixel_xyz
from application.services.guidance_service import (
    CircleHit,
    extract_circles,
    intrinsics_to_dict,
)

# 调试阶段名（与界面下拉、结果文案共用一份）
STAGE_ORIG = "orig"
STAGE_GRAY = "gray"
STAGE_BINARY = "binary"
STAGE_FILTERED = "filtered"
STAGE_CONTOUR = "contour"
STAGE_CHOICES = (
    (STAGE_ORIG, "原彩色 + 矩形叠加"),
    (STAGE_GRAY, "归一化灰度"),
    (STAGE_BINARY, "二值图"),
    (STAGE_FILTERED, "形态学滤波"),
    (STAGE_CONTOUR, "轮廓筛选图"),
)


@dataclass
class DepthStageResult:
    """步骤 1+2（深度切分 → 轮廓 → 正外接矩形）的结果与阶段图。"""

    stages: Dict[str, Any] = field(default_factory=dict)   # gray/binary/filtered/...
    rects: List[dict] = field(default_factory=list)
    contours: List[Any] = field(default_factory=list)
    n_contours: int = 0


@dataclass
class CircleDebugResult:
    """一次找圆调试的结果。``ok=False`` 时 ``stage`` 指出卡在哪一步。"""

    ok: bool
    stage: str = "depth"            # depth / circle / xyz / done
    message: str = ""
    stages: Dict[str, Any] = field(default_factory=dict)
    rects: List[dict] = field(default_factory=list)
    contours: List[Any] = field(default_factory=list)
    n_contours: int = 0
    hits: List[CircleHit] = field(default_factory=list)
    rect_errors: List[str] = field(default_factory=list)
    selected: Optional[CircleHit] = None
    cam_xyz: Optional[Tuple[float, float, float]] = None
    used_depth_mm: float = 0.0
    coarse: Optional[Tuple[float, float]] = None
    fine: Optional[Tuple[float, float]] = None


def _frame_depth(frame: Any) -> Tuple[Any, Any]:
    """取 (原始深度数组, depth_scale)；缺任一返回 (None, None)。"""
    depth = getattr(getattr(frame, "depth", None), "data", None)
    scale = getattr(frame, "depth_scale_m", None)
    if depth is None or not scale:
        return None, None
    return depth, scale


def depth_guidance_stage(frame: Any,
                         params: DepthGuidanceParams) -> DepthStageResult:
    """步骤 1+2：深度切分 → 归一化 → 二值化 → 形态学 → 轮廓 → 正外接矩形。

    缺深度或 depth_scale 时抛 ``ValueError``（附带清楚原因，页面直接展示）。
    """
    depth, scale = _frame_depth(frame)
    if depth is None:
        raise ValueError("该帧缺原始深度或 depth_scale，不能做深度引导")
    color = getattr(getattr(frame, "color", None), "data", None)
    res = run_depth_guidance(depth, float(scale), params,
                             color_image=color, include_rects=True)
    stages = {key: res.get(key) for key in
              ("depth_mm", "gray", "binary", "filtered",
               "contour_image", "debug_image")}
    return DepthStageResult(stages=stages, rects=list(res.get("rects") or []),
                            contours=list(res.get("contours") or []),
                            n_contours=int(res.get("n_contours", 0)))


def find_circles_in_rects(image: Any, rects: Sequence[dict],
                          params: CircleParams, host_proxy: Any
                          ) -> Tuple[List[CircleHit], List[str]]:
    """在每个正外接矩形内找圆（与 ``GuidanceService._find_circles`` 同一约定）。

    - 矩形坐标转原生 ``[2, x, y, w, h, 0]``，圆坐标**已是全图坐标**；
    - 边长 < 5px 的矩形跳过并记一条说明（调试要能看到"为什么没找"，不静默）；
    - 单矩形异常只记原因、继续其它矩形；
    - 宿主缺失直接抛 ``RuntimeError``（页面如实展示，不造圆）。
    """
    if host_proxy is None:
        raise RuntimeError(
            "cp38 圆宿主不可用：找圆需要 3.8 原生件（detect_circle_imp），"
            "当前不给假圆")
    if image is None:
        raise ValueError("没有可用于找圆的图像（该帧无彩色且无轮廓图）")

    hits: List[CircleHit] = []
    errors: List[str] = []
    for i, rect in enumerate(rects):
        x1, y1 = int(rect["x1"]), int(rect["y1"])
        w, h = int(rect["x2"]) - x1, int(rect["y2"]) - y1
        if w < 5 or h < 5:
            errors.append(f"矩形#{i} ({x1},{y1}) 边长 {w}x{h} < 5px，跳过找圆")
            continue
        algo_rect = [2, x1, y1, w, h, 0]
        try:
            ret = host_proxy.detect([image], [algo_rect], params)
        except Exception as exc:  # noqa: BLE001 - 单矩形失败不影响其它
            errors.append(f"矩形#{i} ({x1},{y1}) 找圆异常：{exc}")
            continue
        found = extract_circles(ret or {})
        for c in found:
            hits.append(CircleHit(rect_index=int(rect.get("idx", i)),
                                  cx=float(c["cx"]), cy=float(c["cy"]),
                                  radius=float(c["radius"])))
    return hits, errors


def circle_xyz(frame: Any, hit: CircleHit, depth_mm: Any,
               depth_params: DepthGuidanceParams
               ) -> Tuple[Optional[Tuple[float, float, float]], float]:
    """圆心 → 相机系 XYZ(mm)：均值深度优先，无效时螺旋搜索单点深度兜底。

    返回 ``(xyz, used_depth_mm)``；两者都取不到时 ``xyz=None``。
    """
    intrinsics = getattr(frame, "intrinsics", None)
    if intrinsics is None:
        return None, 0.0
    intr = intrinsics_to_dict(intrinsics)
    xyz, used = point_depth_xyz(depth_mm, hit.cx, hit.cy, intr, depth_params)
    if xyz is not None:
        return tuple(xyz), float(used)
    depth, scale = _frame_depth(frame)
    if depth is None:
        return None, 0.0
    fallback = compute_pixel_xyz(hit.cx, hit.cy, depth, intr, float(scale))
    if fallback is None:
        return None, 0.0
    return (fallback[0], fallback[1], fallback[2]), float(fallback[2])


def run_circle_debug(frame: Any, *,
                     depth_params: DepthGuidanceParams,
                     circle_params: CircleParams,
                     offsets: Optional[OffsetParams] = None,
                     host_proxy: Any = None,
                     only_depth: bool = False) -> CircleDebugResult:
    """整条调试链：深度引导 → 逐矩形找圆 → 选中圆 XYZ → 粗/精偏移。

    ``only_depth=True`` 时只跑步骤 1+2（界面的「深度检测」按钮），不做找圆。
    """
    try:
        depth_stage = depth_guidance_stage(frame, depth_params)
    except Exception as exc:  # noqa: BLE001 - 页面如实展示
        return CircleDebugResult(ok=False, stage="depth", message=str(exc))

    result = CircleDebugResult(
        ok=False, stage="depth",
        stages=depth_stage.stages, rects=depth_stage.rects,
        contours=depth_stage.contours, n_contours=depth_stage.n_contours)
    if not depth_stage.rects:
        result.message = (f"未找到候选目标（过滤后轮廓数={depth_stage.n_contours}）；"
                          f"检查深度的切分上下限 / 二值化 / 形态学 / 轮廓过滤等参数")
        return result
    if only_depth:
        result.ok = True
        result.stage = "depth"
        result.message = (f"深度引导完成：保留轮廓 {depth_stage.n_contours} 个，"
                          f"正外接矩形 {len(depth_stage.rects)} 个")
        return result

    color = getattr(getattr(frame, "color", None), "data", None)
    image = color if color is not None else depth_stage.stages.get("contour_image")
    try:
        hits, errors = find_circles_in_rects(image, depth_stage.rects,
                                             circle_params, host_proxy)
    except Exception as exc:  # noqa: BLE001 - 宿主缺失/无图: 如实报告
        result.stage = "circle"
        result.message = f"找圆不可用：{exc}"
        return result
    result.hits = hits
    result.rect_errors = errors
    if not hits:
        result.stage = "circle"
        result.message = (f"{len(depth_stage.rects)} 个矩形内均未找到圆"
                          + (f"；{len(errors)} 条异常见明细" if errors else ""))
        return result

    hit = hits[0]
    result.selected = hit
    xyz, used = circle_xyz(frame, hit, depth_stage.stages.get("depth_mm"),
                           depth_params)
    if xyz is None:
        result.stage = "xyz"
        result.message = "圆心处深度无效（均值与螺旋搜索单点均取不到），不能换算 XYZ"
        return result
    result.cam_xyz = tuple(xyz)
    result.used_depth_mm = used
    try:
        coarse, fine = compute_robot_offsets(xyz, offsets or OffsetParams())
    except Exception as exc:  # noqa: BLE001 - 偏移映射配置非法
        result.stage = "xyz"
        result.message = f"粗/精偏移计算失败：{exc}"
        return result
    result.coarse, result.fine = coarse, fine
    result.ok = True
    result.stage = "done"
    result.message = "找圆调试完成"
    return result


__all__ = [
    "CircleDebugResult", "DepthStageResult", "STAGE_BINARY", "STAGE_CHOICES",
    "STAGE_CONTOUR", "STAGE_FILTERED", "STAGE_GRAY", "STAGE_ORIG",
    "circle_xyz", "depth_guidance_stage", "find_circles_in_rects",
    "run_circle_debug",
]
