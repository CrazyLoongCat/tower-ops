"""视觉步骤「框 + 框内圆」初始布局（DEC-048）。

作业台视觉判定步骤右侧图卡要显示**多个框**、每个框内的**一个圆**，并允许人工
重新调整；调整后的圆用于后续「引导定位」步骤（沿用视觉步骤那一帧）。

初始布局**按检测配置现算**（用户裁定）：

    命名检测 params["circle_debug"]（找圆调试页「另存为检测」保存的 depth/circle 参数）
      → 在**当前帧**上跑 深度引导（run_depth_guidance）得到正外接矩形
      → 每个矩形经 cp38 宿主找圆（find_circles_in_rects）
      → 每个框一个圆

与视觉调试页「找圆调试」是**同一条链**（本模块只做编排，判据全部复用
``guidance_debug`` / ``depth_guidance``，不复制算法）。

没有 ``circle_debug`` 参数（旧检测/未保存找圆调试）时**不伪造**深度框：
退化为「自动检测结果每个圆一个外接方框」，并在 ``message`` 里说明来源。
宿主不可用时保留深度框但圆为空，并如实说明原因。

**只读**：不写检测、不改配方、不动设备；框/圆坐标一律原图像素。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from application.algorithms.circle.params import CircleParams
from application.algorithms.guidance.depth_guidance import DepthGuidanceParams
from application.services.guidance_debug import (
    depth_guidance_stage,
    find_circles_in_rects,
)

SOURCE_CIRCLE_DEBUG = "circle_debug"
SOURCE_RESULT_CIRCLE = "result_circle"
SOURCE_NONE = "none"


@dataclass
class VisionLayout:
    """视觉步骤的框/圆布局。``boxes`` 项: {x1,y1,x2,y2,circle|cx,cy,radius_px}。"""

    boxes: List[dict] = field(default_factory=list)
    selected: Optional[int] = None
    source: str = SOURCE_NONE
    message: str = ""

    def circles(self) -> List[dict]:
        return [dict(b["circle"]) for b in self.boxes if b.get("circle")]


# ---------------------------------------------------------------------------
#  几何助手（框/圆坐标一律原图像素）
# ---------------------------------------------------------------------------
def image_size(frame: Any) -> Tuple[Optional[int], Optional[int]]:
    """取帧的 (宽, 高)；优先彩色，其次深度。取不到返回 (None, None)。"""
    for stream in ("color", "depth"):
        data = getattr(getattr(frame, stream, None), "data", None)
        shape = getattr(data, "shape", None)
        if shape is not None and len(shape) >= 2:
            return int(shape[1]), int(shape[0])
    return None, None


def _circle_dict(cx: float, cy: float, r: float) -> dict:
    return {"cx": float(cx), "cy": float(cy), "radius_px": float(r)}


def _clamp_box(box: dict, width: Optional[int], height: Optional[int]) -> dict:
    x1, x2 = sorted((float(box["x1"]), float(box["x2"])))
    y1, y2 = sorted((float(box["y1"]), float(box["y2"])))
    if width:
        x1, x2 = max(0.0, min(x1, width - 1.0)), max(0.0, min(x2, width - 1.0))
    if height:
        y1, y2 = max(0.0, min(y1, height - 1.0)), max(0.0, min(y2, height - 1.0))
    circle = box.get("circle")
    out = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "circle": None}
    if isinstance(circle, dict):
        cx = float(circle.get("cx", 0.0))
        cy = float(circle.get("cy", 0.0))
        r = float(circle.get("radius_px", 0.0))
        if r > 0:
            out["circle"] = _clamp_circle_in_box(_circle_dict(cx, cy, r), out)
    return out


def _clamp_circle_in_box(circle: dict, box: dict) -> Optional[dict]:
    """把圆整体夹进框内（先夹半径，再按半径夹圆心；框太小返回 None）。"""
    x1, y1, x2, y2 = box["x1"], box["y1"], box["x2"], box["y2"]
    max_r = min(x2 - x1, y2 - y1) / 2.0
    if max_r < 1.0:
        return None
    r = max(1.0, min(float(circle["radius_px"]), max_r))
    cx = max(x1 + r, min(float(circle["cx"]), x2 - r))
    cy = max(y1 + r, min(float(circle["cy"]), y2 - r))
    return _circle_dict(cx, cy, r)


def normalize_boxes(boxes: Sequence[dict],
                    width: Optional[int] = None,
                    height: Optional[int] = None) -> List[dict]:
    """人工提交的框/圆做坐标校验与夹取；非数字/退化框直接拒绝（不静默丢框）。"""
    out: List[dict] = []
    for i, box in enumerate(boxes or ()):
        if not isinstance(box, dict):
            raise ValueError(f"第 {i + 1} 个框格式非法")
        try:
            x1, y1 = float(box["x1"]), float(box["y1"])
            x2, y2 = float(box["x2"]), float(box["y2"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {i + 1} 个框坐标非法: {exc}") from exc
        if abs(x2 - x1) < 2 or abs(y2 - y1) < 2:
            raise ValueError(f"第 {i + 1} 个框太小（宽高需 ≥ 2px）")
        out.append(_clamp_box({"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                               "circle": box.get("circle")}, width, height))
    return out


def box_around_circle(circle: dict, width: Optional[int],
                  height: Optional[int]) -> dict:
    """自动检测圆 -> 外接方框（旧检测没有找圆调试参数时的兜底，不伪造深度框）。"""
    cx, cy, r = float(circle["cx"]), float(circle["cy"]), float(circle["radius_px"])
    margin = max(10.0, r * 0.3)
    side = 2.0 * r + 2.0 * margin
    box = {"x1": cx - side / 2.0, "y1": cy - side / 2.0,
           "x2": cx + side / 2.0, "y2": cy + side / 2.0, "circle": None}
    box = _clamp_box(box, width, height)
    box["circle"] = _clamp_circle_in_box(_circle_dict(cx, cy, r), box)
    return box


def circle_params_from_debug(raw: Any) -> CircleParams:
    """检测 ``params["circle_debug"]["circle"]`` → :class:`CircleParams`（认得的键）。"""
    if not isinstance(raw, dict):
        return CircleParams()
    known = set(CircleParams.__dataclass_fields__)
    return CircleParams(**{k: v for k, v in raw.items() if k in known})


# ---------------------------------------------------------------------------
#  初始布局
# ---------------------------------------------------------------------------
def compute_initial_layout(frame: Any, params: Optional[dict],
                           host_proxy: Any = None,
                           fallback_circles: Sequence[Any] = ()) -> VisionLayout:
    """按**检测保存的找圆调试参数**在**当前帧**上现算框与圆。

    ``fallback_circles``: 本次自动检测的目标圆（``CirclePrimitive`` 或 dict），
    无 ``circle_debug`` 参数/深度框为空时用来生成外接方框。
    """
    width, height = image_size(frame)
    params = params if isinstance(params, dict) else {}
    debug = params.get("circle_debug")
    debug = debug if isinstance(debug, dict) else {}
    depth_raw = debug.get("depth")
    messages: List[str] = []

    if isinstance(depth_raw, dict) and frame is not None:
        try:
            depth_params = DepthGuidanceParams.from_dict(depth_raw)
            stage = depth_guidance_stage(frame, depth_params)
        except Exception as exc:  # noqa: BLE001 - 页面/节点如实展示，不造框
            stage = None
            messages.append(f"按检测配置现算失败：{exc}")
        if stage is not None and stage.rects:
            image = getattr(getattr(frame, "color", None), "data", None)
            if image is None:
                image = stage.stages.get("contour_image")
            hits, errors = [], []
            try:
                hits, errors = find_circles_in_rects(
                    image, stage.rects, circle_params_from_debug(debug.get("circle")),
                    host_proxy)
            except Exception as exc:  # noqa: BLE001 - 宿主缺失/无图：保留框，圆为空
                messages.append(f"找圆不可用：{exc}")
            by_rect: Dict[int, Any] = {}
            for hit in hits:
                by_rect.setdefault(int(hit.rect_index), hit)   # 每框只取第一个圆
            boxes: List[dict] = []
            for i, rect in enumerate(stage.rects):
                idx = int(rect.get("idx", i))
                hit = by_rect.get(idx)
                box = _clamp_box({"x1": rect["x1"], "y1": rect["y1"],
                                  "x2": rect["x2"], "y2": rect["y2"],
                                  "circle": None}, width, height)
                if hit is not None:
                    box["circle"] = _clamp_circle_in_box(
                        _circle_dict(hit.cx, hit.cy, hit.radius), box)
                boxes.append(box)
            n_circle = sum(1 for b in boxes if b.get("circle"))
            messages.insert(0, f"按检测配置现算：{len(boxes)} 个矩形，"
                               f"{n_circle} 个框内找到圆")
            if errors:
                messages.append(f"{len(errors)} 个矩形找圆异常（详见调试页）")
            selected = next((i for i, b in enumerate(boxes)
                             if b.get("circle")), None)
            return VisionLayout(boxes=boxes, selected=selected,
                                source=SOURCE_CIRCLE_DEBUG,
                                message="；".join(messages))
        if stage is not None and not stage.rects:
            messages.append(f"深度引导未找到候选矩形（过滤后轮廓数="
                            f"{stage.n_contours}）")
    elif not isinstance(depth_raw, dict):
        messages.append("该检测未保存找圆调试参数（找圆调试页『另存为检测』会保存）")

    fallback = [c for c in (fallback_circles or ()) if c is not None]
    circles = []
    for c in fallback:
        try:
            if isinstance(c, dict):
                circles.append(_circle_dict(c["cx"], c["cy"],
                                            c.get("radius_px", c.get("radius", 0))))
            else:
                circles.append(_circle_dict(getattr(c, "cx"), getattr(c, "cy"),
                                            getattr(c, "radius_px")))
        except (KeyError, TypeError, AttributeError, ValueError):
            continue
    circles = [c for c in circles if c["radius_px"] > 0]
    if circles:
        boxes = [box_around_circle(c, width, height) for c in circles]
        selected = 0 if boxes[0].get("circle") else None
        messages.append("框按本次自动检测圆的外接方框生成（非深度框）")
        return VisionLayout(boxes=boxes, selected=selected,
                            source=SOURCE_RESULT_CIRCLE,
                            message="；".join(messages))
    messages.append("没有可显示的框与圆（自动检测也无圆目标）")
    return VisionLayout(boxes=[], selected=None, source=SOURCE_NONE,
                        message="；".join(messages))


def find_circle_in_rect(frame: Any, params: Optional[dict], host_proxy: Any,
                        rect: dict) -> Tuple[Optional[dict], str]:
    """人工新画一个框后，在该框内找圆（同一套检测参数/宿主）。

    返回 ``(circle|None, message)``；找不到圆不造圆，说明原因。
    """
    params = params if isinstance(params, dict) else {}
    debug = params.get("circle_debug")
    debug = debug if isinstance(debug, dict) else {}
    image = getattr(getattr(frame, "color", None), "data", None)
    if image is None:
        image = getattr(getattr(frame, "depth", None), "data", None)
    if image is None or frame is None:
        return None, "当前帧没有可用于找圆的图像"
    probe = dict(rect)
    probe.setdefault("idx", 0)
    try:
        hits, errors = find_circles_in_rects(
            image, [probe], circle_params_from_debug(debug.get("circle")), host_proxy)
    except Exception as exc:  # noqa: BLE001 - 宿主缺失/无图：如实说明
        return None, f"找圆不可用：{exc}"
    if not hits:
        extra = f"；{errors[0]}" if errors else ""
        return None, f"框内未找到圆{extra}"
    hit = hits[0]
    return _circle_dict(hit.cx, hit.cy, hit.radius), "框内找到圆"


__all__ = [
    "SOURCE_CIRCLE_DEBUG", "SOURCE_NONE", "SOURCE_RESULT_CIRCLE", "VisionLayout",
    "box_around_circle", "circle_params_from_debug", "compute_initial_layout",
    "find_circle_in_rect", "image_size", "normalize_boxes",
]
