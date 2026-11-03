# -*- coding: utf-8 -*-
"""视觉调试页「图像算法」纯函数实现 (E19 增强, DEC-021)。

**纯函数**: 入参 ndarray -> 出参 ndarray / dataclass。本模块**不碰 Qt、不读配置、
不写文件、不改入参数组**。界面层 (presentation) 只负责把 :class:`ImageOpsParams`
递进来、把结果图递出去。

来源与取舍 (照旧程序 `Ganta_Crtl_System/vision_monitor/`):

- ``apply_transform``      -> :func:`apply_geometry` (镜像 + 旋转, 顺序照旧: 先垂直后水平)
- ``draw_crosshair``       -> :func:`draw_crosshair` (按画面百分比定位; 旧程序在 BGRA 上
  画 **红横线 / 绿竖线**, 本模块照其**视觉效果**重写)
- 切分/二值化/形态学/轮廓过滤 -> 复用 :mod:`application.algorithms.guidance.depth_guidance`
  里的通用件 (:func:`binarize` / :func:`morphology` / :func:`find_filtered_contours`),
  **不重新实现**判据, 避免两套阈值语义漂移。
- 旧工程 ``AlgorithmRegistry`` 里的 crosshair/template/yolo 多为空壳, **不移植**;
  只保留真能算出数的三类检测 (轮廓/圆/直线)。

颜色通道约定: 本模块内部一律按 **RGB** 处理, 与
:class:`application.presentation.components.image_view.ImageView` 的 ``kind="color"``
对齐; cv2 原生 BGR 只在 :func:`depth_to_rgb` 里做一次显式转换, 不让调用方猜。

深度伪彩是**唯一事实源**: ``image_view`` 的 ``depth_to_rgb`` 已改为委托本模块,
色标/映射范围只有一套实现。**无效像素 (0 / 饱和值 65535 / 非有限值) 一律纯黑**
(照旧程序 ``_depth_to_colormap`` 的 ``mask_valid = (d > 0) & (d < 65535)``)。

单位: 长度/半径/面积一律**像素** (检测类指标不换算 mm);
**深度切分上下限例外 —— 单位是 mm**: 深度原始值本身就是毫米量化
(本工程各通道 ``depth_scale_m = 0.001``, 1 原始值 = 1 mm; DS86 侧 ``DEPTH_SCALE_M``
注释写明"深度原始单位 = 毫米", 旧程序 ``depth_seg_min_mm/depth_seg_max_mm`` 同此口径),
调用方再用该帧的 ``depth_scale_m`` 换算成原始数组量纲 —— 见 :func:`depth_to_rgb`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Sequence

import cv2
import numpy as np

from ...contracts.units import depth_mm_to_raw
from ..guidance.depth_guidance import (
    DepthGuidanceParams,
    binarize as _dg_binarize,
    find_filtered_contours as _dg_find_contours,
    morphology as _dg_morphology,
)
from .params import (
    PARAM_GROUPS,
    ImageOpsParams,
    coerce_params,
    param_spec,
)

__all__ = [
    "DEFAULT_DEPTH_SCALE_M", "DetectionItem", "DetectionSummary", "ImageOpsResult",
    "apply_brightness_contrast", "apply_chain", "apply_geometry", "as_rgb",
    "binarize_image", "canny", "denoise", "describe", "detect", "draw_crosshair",
    "draw_detections", "format_detections", "morphology_image", "param_label",
    "to_gray", "depth_to_rgb",
]

# 本工程深度口径: 原始值 = 毫米量化 (DS86 ``DEPTH_SCALE_M`` 注释 / 各适配器
# ``depth_scale_m = 0.001``)。调用方没给比例时按此换算, 并在界面上如实标注。
DEFAULT_DEPTH_SCALE_M = 0.001


# =====================================================================
#  小工具
# =====================================================================
def _p(params: Any) -> ImageOpsParams:
    """把 ImageOpsParams / dict / None 统一成合法取值对象。"""
    if params is None:
        return coerce_params({})
    if isinstance(params, ImageOpsParams):
        return params
    return coerce_params(params)


def _odd(k: Any) -> int:
    """核大小取奇数 (照 depth_guidance: 偶数 +1)。"""
    n = max(1, int(k))
    return n + 1 if n % 2 == 0 else n


def to_gray(img: np.ndarray) -> np.ndarray:
    """任意图像 -> 单通道灰度 (uint8)。已是单通道则原样返回。"""
    a = np.asarray(img)
    if a.ndim == 2:
        return a
    if a.ndim == 3 and a.shape[2] == 4:
        return cv2.cvtColor(a, cv2.COLOR_RGBA2GRAY)
    if a.ndim == 3 and a.shape[2] == 3:
        return cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    raise ValueError(f"不支持的图像形状: {a.shape}")


def as_rgb(img: np.ndarray) -> np.ndarray:
    """:func:`apply_chain` 的显示图统一出口 —— 恒为 3 通道 RGB uint8。

    单通道 (灰度/二值/边缘) 复制成三通道, 这样界面侧 ``kind="color"`` 一条路径够用。
    """
    a = np.asarray(img)
    if a.ndim == 2:
        return cv2.cvtColor(a, cv2.COLOR_GRAY2RGB)
    if a.ndim == 3 and a.shape[2] == 1:
        return cv2.cvtColor(a[:, :, 0], cv2.COLOR_GRAY2RGB)
    if a.ndim == 3 and a.shape[2] == 4:
        return cv2.cvtColor(a, cv2.COLOR_RGBA2RGB)
    if a.ndim == 3 and a.shape[2] > 3:
        return np.ascontiguousarray(a[:, :, :3])
    return a


def _to_uint8(img: np.ndarray) -> np.ndarray:
    """cv2 多数算子要求 uint8; 其它位深线性截断到 0-255。"""
    a = np.asarray(img)
    if a.dtype == np.uint8:
        return a
    if a.dtype.kind == "f":
        return np.clip(a, 0, 255).astype(np.uint8)
    if a.dtype == np.bool_:
        return (a.astype(np.uint8)) * 255
    lo, hi = float(a.min()), float(a.max())
    if hi <= lo:
        return np.zeros(a.shape, dtype=np.uint8)
    return (((a.astype(np.float32) - lo) / (hi - lo)) * 255.0).astype(np.uint8)


# =====================================================================
#  1) 几何变换  (照旧 apply_transform)
# =====================================================================
def apply_geometry(img: np.ndarray, params: Any) -> np.ndarray:
    """镜像 + 旋转 (90° 整数倍)。

    顺序照旧程序 ``image_utils.apply_transform``: **先垂直镜像, 后水平镜像**, 最后旋转。
    不做变换时原样返回 (不复制); cv2 的 flip/rotate 都返回新数组, 不会改到入参。
    """
    p = _p(params)
    out = np.asarray(img)
    if p.vflip:
        out = cv2.flip(out, 0)
    if p.hflip:
        out = cv2.flip(out, 1)
    rot = int(p.rotate)
    if rot == 90:
        out = cv2.rotate(out, cv2.ROTATE_90_CLOCKWISE)
    elif rot == 180:
        out = cv2.rotate(out, cv2.ROTATE_180)
    elif rot == 270:
        out = cv2.rotate(out, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return out


# =====================================================================
#  2) 十字线  (照旧 draw_crosshair 的视觉结果)
# =====================================================================
def draw_crosshair(img: np.ndarray, params: Any) -> np.ndarray:
    """按画面百分比画十字线: **横线红、竖线绿**, 线宽 4 (照旧程序)。

    ``cross_x_pct`` 定**竖**线的 x, ``cross_y_pct`` 定**横**线的 y —— 与旧程序
    ``crosshair_x`` / ``crosshair_y`` 的语义一致。与旧程序唯一差别: 旧程序**原地**
    改传入图, 本函数先复制 (纯函数约定), 另处也自动处理单通道输入。
    """
    p = _p(params)
    if not p.crosshair:
        return img
    out = as_rgb(img).copy()
    h, w = out.shape[:2]
    x = int(round(w * float(p.cross_x_pct) / 100.0))
    y = int(round(h * float(p.cross_y_pct) / 100.0))
    x = max(0, min(x, w - 1))
    y = max(0, min(y, h - 1))
    if out.shape[2] == 4:
        # 4 通道 (BGRA) 画线要带 alpha —— 照旧程序
        cv2.line(out, (0, y), (w - 1, y), (255, 0, 0, 255), 4)
        cv2.line(out, (x, 0), (x, h - 1), (0, 255, 0, 255), 4)
    else:
        cv2.line(out, (0, y), (w - 1, y), (255, 0, 0), 4)
        cv2.line(out, (x, 0), (x, h - 1), (0, 255, 0), 4)
    return out


# =====================================================================
#  3) 显示增强 / 降噪
# =====================================================================
def apply_brightness_contrast(img: np.ndarray, params: Any) -> np.ndarray:
    """亮度/对比度: ``out = |src * contrast + brightness|`` (cv2.convertScaleAbs)。

    默认 (亮度 0 / 对比度 1.0) 时原样返回, 不进 cv2 免得白算一遍。
    """
    p = _p(params)
    if int(p.brightness) == 0 and float(p.contrast) == 1.0:
        return img
    return cv2.convertScaleAbs(np.asarray(img),
                              alpha=float(p.contrast), beta=float(p.brightness))


def denoise(img: np.ndarray, params: Any) -> np.ndarray:
    """降噪: 高斯 / 中值 / 双边(保边)。核大小自动取奇数 (照 depth_guidance 约定)。

    中值滤波 ksize 必须为 >=3 的奇数 —— ``denoise_k`` 为 1 时视为不降噪 (cv2 会报错)。
    """
    p = _p(params)
    kind = str(p.denoise or "none").lower()
    if kind == "none":
        return img
    src = _to_uint8(np.asarray(img))
    k = _odd(p.denoise_k)
    if kind == "gaussian":
        if k < 3:
            return img
        return cv2.GaussianBlur(src, (k, k), 0)
    if kind == "median":
        if k < 3:
            return img
        return cv2.medianBlur(src, k)
    if kind == "bilateral":
        return cv2.bilateralFilter(src, max(1, k), 75.0, 75.0)
    return img


# =====================================================================
#  4) 二值化 / 边缘 / 形态学  (复用 depth_guidance 的判据)
# =====================================================================
def binarize_image(img: np.ndarray, params: Any) -> np.ndarray:
    """二值化。判据**复用** :func:`depth_guidance.binarize` (同旧程序)。

    ``threshold_mode="none"`` 时不处理、原样返回。
    """
    p = _p(params)
    mode = str(p.threshold_mode or "none").lower()
    if mode == "none":
        return img
    gray = _to_uint8(to_gray(img))
    dg = DepthGuidanceParams(thresh_method=mode,
                             thresh_value=float(p.threshold_value))
    return _dg_binarize(gray, dg)


def morphology_image(img: np.ndarray, params: Any) -> np.ndarray:
    """形态学: 开/闭/腐蚀/膨胀。**复用** :func:`depth_guidance.morphology`。

    内核为正方形, 大小自动取奇数。``morph_op="none"`` 时不处理、原样返回。
    """
    p = _p(params)
    if str(p.morph_op or "none").lower() == "none":
        return img
    src = _to_uint8(np.asarray(img))
    dg = DepthGuidanceParams(morph_op=str(p.morph_op),
                             morph_kernel_size=int(p.morph_k))
    return _dg_morphology(src, dg)


def canny(img: np.ndarray, params: Any) -> np.ndarray:
    """Canny 双阈值边缘图 (uint8, 0/255)。``high <= low`` 时抬到 ``low+1`` 保证可算。"""
    p = _p(params)
    gray = _to_uint8(to_gray(img))
    lo = float(p.canny_low)
    hi = float(p.canny_high)
    if hi <= lo:
        hi = lo + 1.0
    return cv2.Canny(gray, lo, hi)


# =====================================================================
#  5) 深度伪彩  (唯一事实源)
# =====================================================================
# 内置色标 (深蓝 -> 蓝 -> 青 -> 绿 -> 黄 -> 红); "auto" 用它
_LUT_STOPS = (
    (0, 0, 40), (0, 80, 255), (0, 200, 200), (120, 255, 80),
    (255, 220, 0), (255, 60, 0),
)
_LUT_CACHE: np.ndarray | None = None


def _lut() -> np.ndarray:
    """内置色标查表 (0-255 -> RGB), 首次调用时构建并缓存。"""
    global _LUT_CACHE
    if _LUT_CACHE is None:
        stops = np.asarray(_LUT_STOPS, dtype="float32")
        xs = np.linspace(0.0, 1.0, len(stops))
        t = np.linspace(0.0, 1.0, 256)
        _LUT_CACHE = np.stack(
            [np.interp(t, xs, stops[:, i]) for i in range(3)], axis=1
        ).astype("uint8")
    return _LUT_CACHE


def _colormap(name: str) -> int:
    """cv2 色标常量; 该版本没有就降级到 JET (不抛错, 只少一个可选值)。"""
    cmap = getattr(cv2, "COLORMAP_" + str(name).upper(), None)
    return cv2.COLORMAP_JET if cmap is None else cmap


def depth_to_rgb(depth: np.ndarray, params: Any = None, *,
                 lo: float | None = None,
                 hi: float | None = None,
                 depth_scale_m: float | None = None) -> np.ndarray:
    """深度/灰度 -> 伪彩 **RGB** (uint8, 3 通道)。

    照旧程序 ``ds86_camera._depth_to_colormap``:

    - **无效深度** (原始值 0 或饱和值 65535, 以及非有限值) 一律输出纯黑,
      且不参与映射 —— 与本工程既有的"0 无效"口径合并为 ``(d > 0) & (d < 65535)``。
    - 映射范围取**显式切分上下限** [``depth_seg_min_mm``, ``depth_seg_max_mm``],
      **单位 mm** (旧程序同名字段即 mm): 范围外像素夹紧到色标两端
      (``np.clip((d_raw - min_raw) / (max_raw - min_raw), 0, 1)``), 与旧程序一致;
      ``lo``/``hi`` 也是 mm, 供调用方临时覆盖 (不改参数对象)。
    - 按 ``depth_scale_m`` (米/原始值) 把 mm 换回原始数组量纲;
      未给比例时用 :data:`DEFAULT_DEPTH_SCALE_M` (本工程口径 1 原始值 = 1 mm),
      给了非法比例 (≤0/非有限) 则**报错而不用默认值**。
    - 色标 ``params.depth_colormap``: ``auto`` 用内置色标, 其余走 ``cv2.applyColorMap``
      (cv2 给的是 BGR, 这里转回 RGB); applyColorMap 的 0 不是黑(JET 下是深蓝), 无效像素再显式压黑。

    注意: 本函数是**深度伪彩的唯一实现**, ``presentation/components/image_view.py``
    的同名函数已委托到这里 —— 改色标/切分范围只改这一处。
    """
    p = _p(params)
    mm_lo = float(lo if lo is not None else p.depth_seg_min_mm)
    mm_hi = float(hi if hi is not None else p.depth_seg_max_mm)
    scale = DEFAULT_DEPTH_SCALE_M if depth_scale_m is None else float(depth_scale_m)
    min_v = depth_mm_to_raw(mm_lo, scale)     # 非法比例在这里拒绝, 不静默回落默认
    max_v = depth_mm_to_raw(mm_hi, scale)
    if max_v <= min_v:                       # 上下限反了就保底 (照旧程序)
        max_v = min_v + 1.0

    a = np.asarray(depth)
    if a.ndim == 3:
        a = to_gray(a)
    af = a.astype("float32")
    # 无效: 0 / 饱和值 65535 / 非有限值 (照旧程序 mask_valid = (d > 0) & (d < 65535))
    valid = np.isfinite(af) & (af > 0) & (af < 65535.0)
    # 先替掉非有限值再归一化, 免得 (nan-min)/... 的 nan 进 uint8 转换刷 RuntimeWarning
    af_safe = np.where(np.isfinite(af), af, min_v)
    norm = np.clip((af_safe - min_v) / (max_v - min_v), 0.0, 1.0)
    idx = (norm * 255.0).astype("uint8")

    name = str(p.depth_colormap or "auto").lower()
    if name == "auto":
        rgb = _lut()[idx]
    else:
        rgb = cv2.cvtColor(cv2.applyColorMap(idx, _colormap(name)),
                           cv2.COLOR_BGR2RGB)
    rgb = np.ascontiguousarray(rgb)
    rgb[~valid] = 0        # applyColorMap 的 0 不是黑 (JET 下是深蓝), 显式压黑
    return rgb


# =====================================================================
#  6) 检测类  (轮廓 / 圆 / 直线)
# =====================================================================
@dataclass(frozen=True)
class DetectionItem:
    """一个检测结果 (像素坐标)。"""

    kind: str
    index: int
    cx: float
    cy: float
    x: float
    y: float
    w: float
    h: float
    area: float = 0.0
    radius: float = 0.0
    length: float = 0.0

    def label(self) -> str:
        if self.kind == "circle":
            return f"#{self.index} 圆心({self.cx:.0f},{self.cy:.0f}) R={self.radius:.0f}px"
        if self.kind == "line":
            return f"#{self.index} ({self.x:.0f},{self.y:.0f})->({self.x + self.w:.0f}," \
                   f"{self.y + self.h:.0f}) 长{self.length:.0f}px"
        return f"#{self.index} 中心({self.cx:.0f},{self.cy:.0f}) " \
               f"{self.w:.0f}x{self.h:.0f} 面积{self.area:.0f}px²"


@dataclass(frozen=True)
class DetectionSummary:
    """一次检测的全部结果 (只作调参参考, **不参与「运行判定」**)。"""

    kind: str
    count: int
    items: tuple[DetectionItem, ...]
    note: str = ""

    def is_empty(self) -> bool:
        return not self.items


@dataclass(frozen=True)
class ImageOpsResult:
    """:func:`apply_chain` 的产物: 两路显示图 + 检测结果 + 生效参数。"""

    color: np.ndarray | None
    depth: np.ndarray | None
    detections: DetectionSummary
    params: ImageOpsParams
    notes: tuple[str, ...] = ()


def _as_binary(gray: np.ndarray) -> np.ndarray:
    """检测要二值输入: **画面上已是二值就用它** (所见即所测), 否则按 Otsu 自动二值。"""
    u = np.unique(gray)
    if u.size <= 2 and bool(np.all(np.isin(u, (0, 255)))):
        return gray
    _, b = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return b


def _detect_contours(gray: np.ndarray, p: ImageOpsParams) -> tuple[DetectionItem, ...]:
    binary = _as_binary(gray)
    dg = DepthGuidanceParams(min_contour_area=float(p.detect_min_area),
                             max_contour_area=float(p.detect_max_area))
    found = _dg_find_contours(binary, dg)
    found = sorted(found, key=lambda c: float(cv2.contourArea(c)), reverse=True)
    items: List[DetectionItem] = []
    for i, c in enumerate(found[: max(1, int(p.detect_top_n))], 1):
        x, y, w, h = cv2.boundingRect(c)
        m = cv2.moments(c)
        cx = (m["m10"] / m["m00"]) if m["m00"] else (x + w / 2.0)
        cy = (m["m01"] / m["m00"]) if m["m00"] else (y + h / 2.0)
        items.append(DetectionItem(
            kind="contour", index=i, cx=float(cx), cy=float(cy),
            x=float(x), y=float(y), w=float(w), h=float(h),
            area=float(cv2.contourArea(c))))
    return tuple(items)


def _detect_circles(gray: np.ndarray, p: ImageOpsParams) -> tuple[DetectionItem, ...]:
    max_r = int(p.circle_max_radius) if int(p.circle_max_radius) > 0 else 0
    param1 = max(1.0, float(p.canny_high))     # HoughCircles 内部 Canny 的高阈值
    param2 = max(1.0, float(p.circle_accum))
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2,
        minDist=float(max(10, int(p.circle_min_radius))),
        param1=param1, param2=param2,
        minRadius=int(p.circle_min_radius), maxRadius=max_r)
    if circles is None:
        return ()
    items: List[DetectionItem] = []
    for i, (x, y, r) in enumerate(np.round(circles[0]).astype(float), 1):
        items.append(DetectionItem(
            kind="circle", index=i, cx=float(x), cy=float(y),
            x=float(x - r), y=float(y - r), w=float(2 * r), h=float(2 * r),
            area=float(np.pi * r * r), radius=float(r)))
    items.sort(key=lambda it: it.radius, reverse=True)
    return tuple(items[: max(1, int(p.detect_top_n))])


def _detect_lines(gray: np.ndarray, p: ImageOpsParams) -> tuple[DetectionItem, ...]:
    lo = float(p.canny_low)
    hi = max(lo + 1.0, float(p.canny_high))
    edges = cv2.Canny(gray, lo, hi)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180.0,
                            threshold=max(1, int(p.line_votes)),
                            minLineLength=max(1, int(p.line_min_len)),
                            maxLineGap=5)
    if lines is None:
        return ()
    items: List[DetectionItem] = []
    for i, (x1, y1, x2, y2) in enumerate(
            np.round(lines.reshape(-1, 4)).astype(int), 1):
        dx, dy = float(x2 - x1), float(y2 - y1)
        items.append(DetectionItem(
            kind="line", index=i, cx=float((x1 + x2) / 2.0),
            cy=float((y1 + y2) / 2.0), x=float(x1), y=float(y1),
            w=dx, h=dy, length=float(np.hypot(dx, dy))))
    items.sort(key=lambda it: it.length, reverse=True)
    return tuple(items[: max(1, int(p.detect_top_n))])


def detect(img: np.ndarray, params: Any) -> DetectionSummary:
    """在**显示图**上做像素级检测, 不改入参、不画图 (画图见 :func:`draw_detections`)。

    轮廓检测取画面的灰度并按需自动二值 (画面已是二值则直接用, 所见即所测);
    圆/直线走 Hough, 阈值即 ``detect_thresh``。**结果是调参参考, 不参与「运行判定」。**
    """
    p = _p(params)
    kind = str(p.detect or "none").lower()
    if kind == "none":
        return DetectionSummary(kind="none", count=0, items=())
    gray = _to_uint8(to_gray(img))
    note = ""
    if kind == "contour":
        items = _detect_contours(gray, p)
    elif kind == "circle":
        items = _detect_circles(gray, p)
    elif kind == "line":
        items = _detect_lines(gray, p)
    else:
        return DetectionSummary(kind="none", count=0, items=(),
                                note=f"未知检测方式: {kind}")
    if not items:
        note = "未检出目标 (阈值/面积范围可再调)"
    return DetectionSummary(kind=kind, count=len(items), items=items, note=note)


def draw_detections(img: np.ndarray, summary: DetectionSummary) -> np.ndarray:
    """把检测结果画到图上 (返回副本, 不改入参)。轮廓绿框 / 圆品红圈 / 直线琥珀色。"""
    if summary.is_empty():
        return img
    out = as_rgb(img).copy()
    for it in summary.items:
        if it.kind == "circle":
            color = (255, 0, 255)
            cv2.circle(out, (int(round(it.cx)), int(round(it.cy))),
                       max(1, int(round(it.radius))), color, 2)
            cv2.drawMarker(out, (int(round(it.cx)), int(round(it.cy))), color,
                           cv2.MARKER_CROSS, 10, 2)
        elif it.kind == "line":
            color = (255, 160, 0)
            cv2.line(out, (int(round(it.x)), int(round(it.y))),
                     (int(round(it.x + it.w)), int(round(it.y + it.h))), color, 2)
        else:
            color = (0, 255, 0)
            cv2.rectangle(out, (int(round(it.x)), int(round(it.y))),
                          (int(round(it.x + it.w)), int(round(it.y + it.h))),
                          color, 2)
        cv2.putText(out, str(it.index),
                    (int(round(it.x)) + 2, max(12, int(round(it.y)) - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1,
                    cv2.LINE_AA)
    return out


def format_detections(summary: DetectionSummary) -> tuple[str, ...]:
    """检测结果的人读文字 (供「结构化结果」卡片)。"""
    labels = {"contour": "轮廓", "circle": "圆", "line": "直线"}
    name = labels.get(summary.kind, summary.kind)
    lines = [f"[{name}检测] 命中 {summary.count} 个 (像素单位, 不换算 mm)"]
    lines.extend("  " + it.label() for it in summary.items)
    if summary.note:
        lines.append("  " + summary.note)
    return tuple(lines)


# =====================================================================
#  7) 参数快照 (结果卡片标注本次生效的参数)
# =====================================================================
def _format_value(spec: dict, value: Any) -> str:
    kind = spec["kind"]
    if kind == "bool":
        return "开" if value else "关"
    if kind == "choice":
        for code, label in spec["choices"]:
            if code == value:
                return str(label)
        return str(value)
    if kind == "float":
        dec = int(spec.get("decimals", 2))
        return f"{float(value):.{dec}f}{spec.get('suffix', '')}"
    if kind == "int":
        return f"{int(value)}{spec.get('suffix', '')}"
    return str(value)


def describe(params: Any, *, only_changed: bool = True) -> tuple[str, ...]:
    """参数快照, 分组一行 (供「结构化结果」卡片标注本次生效参数)。

    ``only_changed=True`` (默认) 只列与默认值不同的项 —— 全默认时返回空元组,
    界面据此显示「全部为默认值 (未做图像处理)」, 而不是糊一堆默认值上去。
    """
    p = _p(params)
    keys = set(p.changed_keys()) if only_changed else set(spec["key"] for spec
                                                          in _all_specs())
    lines: List[str] = []
    for group in PARAM_GROUPS:
        specs = [spec for spec in group["params"] if spec["key"] in keys]
        if not specs:
            continue
        # 一律带参数名: "显示增强: 亮度=10" 才读得懂, "显示增强: 10" 会让人猜是哪个参数
        parts = [f'{spec["label"]}={_format_value(spec, getattr(p, spec["key"]))}'
                 for spec in specs]
        lines.append(f'{group["label"]}: ' + ", ".join(parts))
    return tuple(lines)


def _all_specs() -> Iterable[dict]:
    for group in PARAM_GROUPS:
        for spec in group["params"]:
            yield spec


# =====================================================================
#  8) 主链: 一次跑完两路
# =====================================================================
def _color_display(color: np.ndarray, p: ImageOpsParams) -> np.ndarray:
    """彩色路显示图 (恒 3 通道 RGB)。

    顺序: 几何 -> 灰度化 -> 亮度/对比度 -> 降噪 -> 二值化 -> 形态学 -> 边缘。
    降噪放在二值化**之前** (先压噪再分割); 形态学放在二值化**之后** (清二值碎点);
    边缘检测取**当前图** —— 因此与二值化同开时得到的是二值轮廓线, 属预期行为。
    """
    work: np.ndarray = apply_geometry(color, p)
    if p.grayscale:
        work = to_gray(work)
    work = apply_brightness_contrast(work, p)
    work = denoise(work, p)
    if str(p.threshold_mode or "none").lower() != "none":
        work = binarize_image(work, p)
    if str(p.morph_op or "none").lower() != "none":
        work = morphology_image(work, p)
    if p.edge:
        work = canny(work, p)
    return as_rgb(work)


def _depth_display(depth: np.ndarray, p: ImageOpsParams,
                   depth_scale_m: float | None = None) -> np.ndarray:
    """深度路显示图: 几何 -> 伪彩 (恒 3 通道 RGB)。

    切分上下限按 **mm** 给, 这里按该帧 ``depth_scale_m`` 落回原始数组量纲。
    """
    return depth_to_rgb(apply_geometry(depth, p), p, depth_scale_m=depth_scale_m)


def apply_chain(color: np.ndarray | None,
                depth: np.ndarray | None,
                params: Any, *,
                depth_scale_m: float | None = None) -> ImageOpsResult:
    """跑完整条链路: 两路各自出**显示图**, 检测在彩色路上做, 十字线最后画 (永远在最上层)。

    **绝不修改原始帧**: 所有 cv2 算子都返回新数组, 需要就地画的 (:func:`draw_crosshair`
    / :func:`draw_detections`) 都先 ``copy()``。原始帧仍可直接喂给判定链路。

    ``depth_scale_m`` 只影响深度伪彩: 切分上下限按 mm 给, 用它换算成原始值
    (不给则用本工程口径 :data:`DEFAULT_DEPTH_SCALE_M`, 并在 notes 里写明"未提供比例")。
    """
    p = _p(params)
    notes: List[str] = []
    color_out: np.ndarray | None = None
    depth_out: np.ndarray | None = None
    scale = DEFAULT_DEPTH_SCALE_M if depth_scale_m is None else float(depth_scale_m)

    if color is None:
        notes.append("无彩色帧: 彩色路未处理")
    else:
        base = _color_display(color, p)
        summary = detect(base, p)
        disp = draw_detections(base, summary)
        color_out = draw_crosshair(disp, p)
    if depth is None:
        notes.append("无深度帧: 深度路未处理")
        depth_out = None
    else:
        depth_out = draw_crosshair(_depth_display(depth, p, scale), p)
        # 单位如实写出来: 面板上是 mm, 落到原始数组前按帧比例换算过 —— 免得又被读成"原始值"
        notes.append(f"深度切分 {p.depth_seg_min_mm}~{p.depth_seg_max_mm} mm"
                     + (f"（按本帧深度比例 {scale:g} m/单位 换算）" if depth_scale_m
                        else "（本帧未提供深度比例，按本工程口径 1 原始值 = 1 mm 换算）"))
    if color is None:
        summary = DetectionSummary(kind="none", count=0, items=(),
                                   note="无彩色帧, 未做检测")
    if p.detect != "none" and summary.is_empty() and color is not None:
        notes.append("检测已开启但本次未检出目标")
    return ImageOpsResult(color=color_out, depth=depth_out,
                          detections=summary, params=p, notes=tuple(notes))


def param_label(key: str) -> str:
    """参数中文标签 (界面/结果卡片共用, 取自唯一事实源)。"""
    return str(param_spec(key)["label"])
