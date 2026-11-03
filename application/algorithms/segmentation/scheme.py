# -*- coding: utf-8 -*-
"""运行一次「方案」：把检测三步固化成可存可跑的一份数据 + 真实执行。

旧工程的做法是"现场调好参数点一下运行一次"，参数散落在控件里、算完就没了。本工程把它
固化成 :class:`RunOnceScheme`——**保存的就是运行一次的实现逻辑**（图源 / 运算区域 / 用户
多边形 / 深度分割参数 / 最终轮廓筛选 / 提示点参数），存进「视觉检测」的 ``params["scheme"]``，
配方流程按 ``detection_ref`` 取出来在真实帧上重跑，判定与调试页完全同一条算法路径。

:func:`run_scheme` 是**真实**执行（调用 :mod:.depth_seg / :mod:.edge_sam / :mod:.intersect），
不是查表回放；离线栈可传入 :class:`StubSegmenter` 顶替模型（见 :mod:.edge_sam 的告诫）。

坐标约定：**方案里一律存原图坐标**（整帧像素），跑的时候按 ROI 偏移转到运算区域局部坐标
参与运算，产出再平移回原图坐标；这样方案与界面显示尺寸解耦，配方里也直接可用。
长度/面积一律**像素(px)**，不换算 mm。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import numpy as np

from application.algorithms.guidance.depth_guidance import (
    DepthGuidanceParams,
    SEG_FILTER_MODES,
)

from .depth_seg import (
    OP_REGION_CODES,
    SOURCE_CODES,
    build_encoder_image,
    clip_roi,
    offset_points,
    run_depth_segmentation,
)
from .edge_sam import Segmenter, mask_primary_component, mask_to_polygons
from .intersect import intersection_result

SCHEME_SCHEMA_VERSION = 1

# 默认最小交集面积（px²），旧工程 run_detect_once 用 contourArea < 1 丢弃碎斑
DEFAULT_MIN_AREA = 1.0


def _poly_to_list(polygon) -> list[list[float]]:
    """多边形顶点 -> [[x, y], ...] 纯 float（可安全写 YAML）。"""
    if polygon is None:
        return []
    out: list[list[float]] = []
    for p in np.asarray(polygon, dtype=np.float64).reshape(-1, 2):
        out.append([float(p[0]), float(p[1])])
    return out


@dataclass
class RunOnceScheme:
    """一次「运行一次」的完整参数（= 要保存的实现逻辑）。

    Attributes:
        source: 智能分割编码图源：``color``/``depth``/``fused``。
        op_region: 运算区域：``whole`` 整张图 / ``roi`` ROI 裁剪图。
        roi: ROI 矩形 (x1, y1, x2, y2)，原图坐标；``op_region=whole`` 时忽略。
        polygon: 用户绘制多边形顶点 [[x, y], ...]，**原图坐标**。
        depth_params: 深度分割参数（:class:`DepthGuidanceParams` 字段字典）。
        seg_filter_mode: 最终轮廓筛选模式（:data:`SEG_FILTER_MODES` 之一）。
        seg_filter_n: 筛选保留轮廓个数（>=1）。
        prompt_k: 每个轮廓最多取的提示点数（>=1）。
        prompt_margin: 除首点外提示点到边缘最小距离（像素）。
        min_area: 交集轮廓最小面积（px²）。
    """

    schema_version: int = SCHEME_SCHEMA_VERSION
    source: str = "color"
    op_region: str = "whole"
    roi: Optional[tuple[float, float, float, float]] = None
    polygon: list[list[float]] = field(default_factory=list)
    depth_params: dict[str, Any] = field(default_factory=dict)
    seg_filter_mode: str = "所有轮廓"
    seg_filter_n: int = 1
    prompt_k: int = 3
    prompt_margin: float = 5.0
    min_area: float = DEFAULT_MIN_AREA
    label: str = ""
    note: str = ""

    # ---------- 序列化 ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "source": str(self.source),
            "op_region": str(self.op_region),
            "roi": None if self.roi is None else [float(v) for v in self.roi],
            "polygon": _poly_to_list(self.polygon),
            "depth_params": dict(self.depth_params or {}),
            "seg_filter_mode": str(self.seg_filter_mode),
            "seg_filter_n": int(self.seg_filter_n),
            "prompt_k": int(self.prompt_k),
            "prompt_margin": float(self.prompt_margin),
            "min_area": float(self.min_area),
            "label": str(self.label),
            "note": str(self.note),
        }

    def guidance_params(self) -> DepthGuidanceParams:
        return DepthGuidanceParams.from_dict(self.depth_params)


def coerce_scheme(data) -> Optional[RunOnceScheme]:
    """从 dict/None 宽容地构造方案；不是方案（缺关键字段/类型不对）返回 None。

    宽容不等于猜：只接受认识的键、缺项用默认值，**但**必须是"看得出是方案"的字典
    （含 ``schema_version`` 或任一核心几何/参数键）。完全无关的字典返回 None，
    免得配方把别的 params 误当方案。
    """
    if data is None:
        return None
    if isinstance(data, RunOnceScheme):
        return data
    if not isinstance(data, dict):
        return None
    core_keys = {"schema_version", "source", "op_region", "roi", "polygon",
                 "depth_params", "seg_filter_mode", "seg_filter_n",
                 "prompt_k", "prompt_margin", "min_area"}
    if not (set(data) & core_keys):
        return None
    source = str(data.get("source", "color"))
    if source not in SOURCE_CODES:
        source = "color"
    op_region = str(data.get("op_region", "whole"))
    if op_region not in OP_REGION_CODES:
        op_region = "whole"
    seg_filter_mode = str(data.get("seg_filter_mode", "所有轮廓"))
    if seg_filter_mode not in SEG_FILTER_MODES:
        seg_filter_mode = "所有轮廓"
    roi_raw = data.get("roi")
    roi = None
    if roi_raw is not None:
        try:
            vals = [float(v) for v in list(roi_raw)[:4]]
            if len(vals) == 4:
                roi = (vals[0], vals[1], vals[2], vals[3])
        except (TypeError, ValueError):
            roi = None
    poly_raw = data.get("polygon") or []
    try:
        polygon = _poly_to_list(poly_raw)
    except (TypeError, ValueError):
        polygon = []

    def _int(key: str, default: int, lo: int = 1) -> int:
        try:
            return max(lo, int(data.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _float(key: str, default: float, lo: float = 0.0) -> float:
        try:
            return max(lo, float(data.get(key, default)))
        except (TypeError, ValueError):
            return default

    dp = data.get("depth_params")
    if not isinstance(dp, dict):
        dp = {}
    try:
        schema_version = int(data.get("schema_version", SCHEME_SCHEMA_VERSION))
    except (TypeError, ValueError):
        schema_version = SCHEME_SCHEMA_VERSION
    return RunOnceScheme(
        schema_version=schema_version,
        source=source,
        op_region=op_region,
        roi=roi,
        polygon=polygon,
        depth_params=dict(dp),
        seg_filter_mode=seg_filter_mode,
        seg_filter_n=_int("seg_filter_n", 1),
        prompt_k=_int("prompt_k", 3),
        prompt_margin=_float("prompt_margin", 5.0),
        min_area=_float("min_area", DEFAULT_MIN_AREA),
        label=str(data.get("label", "") or ""),
        note=str(data.get("note", "") or ""),
    )


def scheme_from_params(params) -> Optional[RunOnceScheme]:
    """从「视觉检测」的 params 字典取出方案（``params["scheme"]``）。"""
    if not isinstance(params, dict):
        return None
    return coerce_scheme(params.get("scheme"))


def has_scheme(params) -> bool:
    """该 params 是否含方案（配方/界面据此判断走"运行一次"路径而非旧占位路径）。"""
    return scheme_from_params(params) is not None


# ---------------------------------------------------------------------------
#  真实执行
# ---------------------------------------------------------------------------
@dataclass
class RunOnceResult:
    """运行一次的产出（覆盖层数据均**原图坐标**，可直接画在整帧上）。"""

    ok: bool = False
    error: Optional[str] = None
    source: str = "color"
    source_note: str = ""
    op_region: str = "whole"
    roi: Optional[tuple[int, int, int, int]] = None

    # 深度分割
    depth_contours: list[np.ndarray] = field(default_factory=list)   # 原图坐标
    prompts: list[list[tuple[float, float]]] = field(default_factory=list)  # 原图坐标
    n_contours: int = 0

    # 智能分割
    smart_polygons: list[np.ndarray] = field(default_factory=list)   # 原图坐标（外轮廓）
    smart_mask: Optional[np.ndarray] = None    # 运算区域局部坐标，255=目标（可 None）
    roi_offset: tuple[int, int] = (0, 0)

    # 运行一次（交集）
    intersection_contours: list[np.ndarray] = field(default_factory=list)  # 原图坐标
    area_px: int = 0
    hit: bool = False

    enc_image: Optional[np.ndarray] = None     # 运算区域内编码图（供预览）
    segmenter_name: str = ""
    notes: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)


def _shift_contours(contours: Iterable, offset) -> list[np.ndarray]:
    ox, oy = int(offset[0]), int(offset[1])
    out = []
    for c in contours:
        arr = np.asarray(c, dtype=np.int32).reshape(-1, 1, 2).copy()
        arr[:, 0, 0] += ox
        arr[:, 0, 1] += oy
        out.append(arr)
    return out


def _local_polygon(polygon, offset) -> list[list[float]]:
    ox, oy = int(offset[0]), int(offset[1])
    return [[float(x) - ox, float(y) - oy] for x, y in _poly_to_list(polygon)]


def run_scheme(scheme: RunOnceScheme, frame, segmenter: Optional[Segmenter] = None,
               *, require_segmenter: bool = True) -> RunOnceResult:
    """在**一帧**上真实执行方案（深度分割 → 智能分割 → 运行一次）。

    Args:
        scheme: 方案（:class:`RunOnceScheme`）。
        frame: :class:`application.contracts.frames.FrameBundle`。
        segmenter: 智能分割后端；None 且 ``require_segmenter`` 时如实失败（不静默跳过）。
        require_segmenter: 无 segmenter 时是否视为错误。False 时只跑深度分割与交集为空的降级。

    Returns:
        :class:`RunOnceResult`；任何前置不足（缺深度/缺图源/无后端）都如实记 ``error``/
        ``notes``，不假装成功。
    """
    import time
    res = RunOnceResult(source=scheme.source, op_region=scheme.op_region)
    t_all = time.perf_counter()

    color = None
    if getattr(frame, "color", None) is not None:
        color = np.asarray(frame.color.data)
    depth = None
    if getattr(frame, "depth", None) is not None:
        depth = np.asarray(frame.depth.data)
    depth_scale = getattr(frame, "depth_scale_m", None)

    if depth is None:
        res.error = "该帧无深度图，无法执行深度分割（运行一次需要深度）"
        res.timings_ms["total"] = (time.perf_counter() - t_all) * 1000.0
        return res
    if depth_scale is None or not (float(depth_scale) > 0):
        res.error = "该帧缺有效 depth_scale_m，无法把深度切分上下限(mm)换算到原始值"
        res.timings_ms["total"] = (time.perf_counter() - t_all) * 1000.0
        return res

    h, w = depth.shape[:2]
    box = None
    if scheme.op_region == "roi":
        box = clip_roi(scheme.roi, h, w)
        if box is None:
            res.notes.append("ROI 无效（越界/过小），本次按整张图执行")
    offset = (box[0], box[1]) if box else (0, 0)
    res.roi = box
    res.roi_offset = offset

    depth_run = depth[offset[1]:offset[1] + (box[3] - box[1] if box else h),
                      offset[0]:offset[0] + (box[2] - box[0] if box else w)]
    color_run = None
    if color is not None:
        color_run = color[offset[1]:offset[1] + depth_run.shape[0],
                          offset[0]:offset[0] + depth_run.shape[1]]

    # --- 深度分割 ---
    t0 = time.perf_counter()
    seg = run_depth_segmentation(
        depth_run, float(depth_scale), scheme.guidance_params(),
        filter_mode=scheme.seg_filter_mode, top_n=int(scheme.seg_filter_n),
        k=int(scheme.prompt_k), margin=float(scheme.prompt_margin),
        roi=None)   # ROI 已在此处裁好，交给深度分割时按整张
    res.timings_ms["depth_seg"] = (time.perf_counter() - t0) * 1000.0
    res.n_contours = int(seg.get("n_contours", 0))
    depth_contours_local = list(seg.get("contours", []))
    prompts_local = list(seg.get("prompts", []))
    filtered = seg.get("filtered")

    res.depth_contours = _shift_contours(depth_contours_local, offset)
    res.prompts = [offset_points(grp, offset) for grp in prompts_local]

    if not prompts_local:
        res.notes.append("深度分割未产出提示点（无满足条件的轮廓）——智能分割/交集为空")
        res.ok = True
        res.timings_ms["total"] = (time.perf_counter() - t_all) * 1000.0
        return res

    # --- 智能分割 ---
    if segmenter is None:
        if require_segmenter:
            res.error = ("无智能分割后端（onnxruntime/EdgeSAM 模型不可用），"
                         "无法执行运行一次的智能分割步骤")
            res.timings_ms["total"] = (time.perf_counter() - t_all) * 1000.0
            return res
        res.notes.append("无智能分割后端：仅完成深度分割，交集为空")
        res.ok = True
        res.timings_ms["total"] = (time.perf_counter() - t_all) * 1000.0
        return res

    enc_image, note = build_encoder_image(scheme.source, color_run, depth_run,
                                          float(depth_scale), filtered)
    res.source_note = note
    if enc_image is None:
        res.error = f"处理图源构造失败：{note}"
        res.timings_ms["total"] = (time.perf_counter() - t_all) * 1000.0
        return res
    res.enc_image = enc_image
    res.segmenter_name = getattr(segmenter, "name", type(segmenter).__name__)

    t0 = time.perf_counter()
    enc_ms = float(segmenter.set_image(enc_image))
    res.timings_ms["encode"] = enc_ms
    hh, ww = enc_image.shape[:2]
    smart_mask = np.zeros((hh, ww), dtype=np.uint8)
    dec_total = 0.0
    for grp in prompts_local:
        prompt_pts = [(float(x), float(y)) for x, y in grp]
        mask, _score, dec_ms = segmenter.predict(prompt_pts)
        dec_total += float(dec_ms)
        mask = np.asarray(mask)
        if mask.shape[:2] != (hh, ww):
            import cv2
            mask = cv2.resize(mask, (ww, hh), interpolation=cv2.INTER_NEAREST)
        mask, _dropped = mask_primary_component(mask, (prompt_pts[0][0], prompt_pts[0][1]))
        smart_mask = np.maximum(smart_mask, np.asarray(mask, dtype=np.uint8))
    res.timings_ms["decode"] = dec_total

    polys_local = mask_to_polygons(smart_mask)
    res.smart_mask = smart_mask
    res.smart_polygons = _shift_contours(polys_local, offset)

    # --- 运行一次：交集 ---
    t0 = time.perf_counter()
    inter = intersection_result(smart_mask, _local_polygon(scheme.polygon, offset),
                                (hh, ww), min_area=float(scheme.min_area))
    res.timings_ms["intersect"] = (time.perf_counter() - t0) * 1000.0
    res.intersection_contours = _shift_contours(inter["contours"], offset)
    res.area_px = int(inter["area_px"])
    res.hit = bool(inter["hit"])
    if not res.hit:
        res.notes.append("智能分割结果与用户多边形无有效交集")
    res.ok = True
    res.timings_ms["total"] = (time.perf_counter() - t_all) * 1000.0
    return res


__all__ = [
    "SCHEME_SCHEMA_VERSION", "DEFAULT_MIN_AREA",
    "RunOnceScheme", "RunOnceResult",
    "coerce_scheme", "scheme_from_params", "has_scheme", "run_scheme",
]
