# -*- coding: utf-8 -*-
"""深度图引导检测算法流程（**1:1 移植**自 `Ganta_Crtl_System/vision_monitor/vision/depth_guidance_pipeline.py`）。

移植原则：**算法不改** —— 切分/归一化/二值化/形态学/轮廓过滤的判据与默认值原样保留，
它们来自现场调参结果。仅调整模块位置并补来源标注。

（原文件说明，保留）
本文件单独实现「ROBOT 引导检测」中基于深度图的算法流程，便于查看与后续
调整算法。整体流程分为三步：

1. 深度切分 -> 归一化 0-255 -> 二值化 -> 形态学滤波 -> 轮廓查找与过滤
2. 由过滤后的轮廓生成「以中心点为圆心、边长为 X 的正外接矩形」，供彩色图
   自动画框（与手动画框等价，画完可继续调整大小与位置）
3. 找圆后以圆心为中心画直径为 R 的圆，取该圆内有效深度的平均值用于计算
   X/Y/Z 距离，避免单点深度波动；该功能可开关，R 可任意配置（单位 pix）

所有可调参数集中在 :class:`DepthGuidanceParams`，调用入口为
:func:`run_depth_guidance`（步骤 1+2）与 :func:`average_depth_in_circle`
（步骤 3）。本模块仅依赖 numpy / opencv，不依赖任何 UI。

深度单位约定：
    - ``depth_array`` 为相机原始深度数组（uint16），其物理单位由
      ``depth_scale``（米/原始单位）决定。
    - 本模块统一换算到毫米(mm)处理：``depth_mm = depth_array * depth_scale * 1000``。
    - 切分上下限 ``depth_min_mm / depth_max_mm`` 单位均为 mm。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
from typing import List, Dict, Any, Optional, Tuple

import cv2
import numpy as np


# =====================================================================
#  参数定义
# =====================================================================
@dataclass
class DepthGuidanceParams:
    """深度图引导检测全部可调参数

    所有长度/面积/范围参数均按图像像素或毫米(mm)解释，与字段名标注一致。
    任意 ``min``/``max`` 对中，若 ``min > max`` 则该条件被忽略（不限制）。
    """

    # ---- 步骤 1：深度切分 ----
    # 深度切分上下限（mm），剔除该范围之外的深度
    depth_min_mm: float = 200.0
    depth_max_mm: float = 3000.0
    # 归一化时是否按切分范围线性映射到 0-255；False 则按全图有效深度最小最大值映射
    normalize_by_range: bool = True
    # 将无效深度(原始值 65535 或超出切分范围)赋值为 255，
    # 避免背景与前景在二值化时无法分割(否则置 0)
    invalid_as_255: bool = True
    # 视为无效深度的原始值阈值(默认 65535)，大于等于该值视为无效
    invalid_raw_threshold: float = 65535.0

    # ---- 步骤 1：二值化 ----
    # 二值化方法: binary / binary_inv / otsu / triangle / adaptive_mean / adaptive_gaussian
    thresh_method: str = "otsu"
    # 二值化阈值（仅 binary / binary_inv 生效；otsu/triangle 自动计算时会作为下限补充）
    thresh_value: float = 127.0
    # 自适应二值化块大小（奇数，仅 adaptive_* 生效）
    adaptive_block_size: int = 15
    # 自适应二值化常量 C（仅 adaptive_* 生效）
    adaptive_c: float = 5.0

    # ---- 步骤 1：形态学滤波 ----
    # 形态学操作: none / open / close / erode / dilate
    morph_op: str = "close"
    # 形态学内核大小（正方形，奇数）
    morph_kernel_size: int = 3

    # ---- 步骤 1：轮廓过滤 ----
    min_contour_len: float = 0.0          # 轮廓周长下限（pix）
    max_contour_len: float = 1e9          # 轮廓周长上限（pix）
    min_contour_area: float = 0.0         # 轮廓面积下限（pix^2）
    max_contour_area: float = 1e9         # 轮廓面积上限（pix^2）
    min_bbox_w: float = 0.0               # 外接矩形宽下限（pix）
    max_bbox_w: float = 1e9               # 外接矩形宽上限（pix）
    min_bbox_h: float = 0.0               # 外接矩形高下限（pix）
    max_bbox_h: float = 1e9               # 外接矩形高上限（pix）
    min_aspect_ratio: float = 0.0         # 长宽比下限（宽/高）
    max_aspect_ratio: float = 1e9         # 长宽比上限（宽/高）
    center_x_min: float = 0.0             # 中心点 x 下限（pix）
    center_x_max: float = 1e9             # 中心点 x 上限（pix）
    center_y_min: float = 0.0             # 中心点 y 下限（pix）
    center_y_max: float = 1e9             # 中心点 y 上限（pix）
    # 中心点采用方式: centroid（轮廓质心）/ bbox（外接矩形中心）
    center_mode: str = "bbox"

    # ---- 步骤 2：正外接矩形 ----
    # 以中心点设置边长为 X 的正矩形（pix）
    rect_side_length: float = 100.0

    # ---- 步骤 3：圆心区域平均深度 ----
    # 是否启用「以圆心为中心、直径 R 取平均深度」
    avg_depth_enable: bool = False
    # 直径 R（pix），取圆内有效深度平均值
    avg_depth_diameter: float = 20.0
    # 深度有效性下限（mm），低于该值视为无效深度（如 0 表示无效）
    depth_valid_min_mm: float = 1.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "DepthGuidanceParams":
        if not d:
            return cls()
        kwargs = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**kwargs)

    def copy(self) -> "DepthGuidanceParams":
        return replace(self)


# 二值化方法选项（供 UI 下拉框引用）
THRESH_METHODS = ("binary", "binary_inv", "otsu", "triangle",
                  "adaptive_mean", "adaptive_gaussian")
# 形态学操作选项
MORPH_OPS = ("none", "open", "close", "erode", "dilate")
# 中心点方式
CENTER_MODES = ("bbox", "centroid")


# =====================================================================
#  步骤 1：深度切分 -> 归一化 -> 二值化 -> 形态学 -> 轮廓过滤
# =====================================================================
def depth_to_mm(depth_array: np.ndarray, depth_scale: float) -> np.ndarray:
    """原始深度数组 -> 毫米(float32)"""
    return depth_array.astype(np.float32) * float(depth_scale) * 1000.0


def segment_depth(depth_mm: np.ndarray, params: DepthGuidanceParams) -> np.ndarray:
    """深度切分：保留 [depth_min_mm, depth_max_mm] 范围，其余置 0

    Returns:
        与输入同形状的 float32 数组，范围外像素为 0
    """
    lo = float(params.depth_min_mm)
    hi = float(params.depth_max_mm)
    out = np.zeros_like(depth_mm, dtype=np.float32)
    mask = (depth_mm >= lo) & (depth_mm <= hi)
    out[mask] = depth_mm[mask]
    return out


def normalize_to_gray(masked_mm: np.ndarray,
                      params: DepthGuidanceParams,
                      raw_depth: Optional[np.ndarray] = None) -> np.ndarray:
    """归一化到 0-255 灰度图(uint8)

    前后景分割逻辑：
    - 切分范围 [depth_min_mm, depth_max_mm] 内的像素（前景）按深度
      线性映射到 0-255；
    - 范围外的像素（背景，包括深度大于上限、小于下限、
      无效值 65535 等）默认置 0；开启 ``invalid_as_255`` 时统一赋值 255，
      使背景与前景在二值化时完全分割。

    - ``normalize_by_range=True``：按切分范围线性映射前景到 0-255
    - ``normalize_by_range=False``：按前景（范围内）最小最大值映射
    """
    out = np.zeros_like(masked_mm, dtype=np.float32)
    # 范围内 = 切分后仍 > 0 的像素（前景）；范围外 = 背景
    in_range = masked_mm > 0

    if not np.any(in_range):
        # 全为背景：开启 invalid_as_255 则全 255，否则全 0
        if params.invalid_as_255:
            return np.full(masked_mm.shape, 255, dtype=np.uint8)
        return np.zeros(masked_mm.shape, dtype=np.uint8)

    if params.normalize_by_range:
        lo = float(params.depth_min_mm)
        hi = float(params.depth_max_mm)
    else:
        lo = float(masked_mm[in_range].min())
        hi = float(masked_mm[in_range].max())
    if hi <= lo:
        hi = lo + 1.0

    # 前景按范围线性映射
    out[in_range] = (masked_mm[in_range] - lo) * 255.0 / (hi - lo)
    np.clip(out, 0, 255, out=out)
    # 背景（范围外）赋值 255，与前景完全分割
    if params.invalid_as_255:
        out[~in_range] = 255.0
    return out.astype(np.uint8)


def binarize(gray: np.ndarray, params: DepthGuidanceParams) -> np.ndarray:
    """二值化

    支持方法：binary / binary_inv / otsu / triangle / adaptive_mean /
    adaptive_gaussian。返回 uint8 二值图（0/255）。
    """
    method = (params.thresh_method or "binary").lower()
    t = float(params.thresh_value)
    if method == "binary":
        _, dst = cv2.threshold(gray, t, 255, cv2.THRESH_BINARY)
    elif method == "binary_inv":
        _, dst = cv2.threshold(gray, t, 255, cv2.THRESH_BINARY_INV)
    elif method == "otsu":
        _, dst = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    elif method == "triangle":
        _, dst = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_TRIANGLE)
    elif method in ("adaptive_mean", "adaptive_gaussian"):
        block = max(3, int(params.adaptive_block_size))
        if block % 2 == 0:
            block += 1  # 必须为奇数
        am = (cv2.ADAPTIVE_THRESH_MEAN_C if method == "adaptive_mean"
              else cv2.ADAPTIVE_THRESH_GAUSSIAN_C)
        dst = cv2.adaptiveThreshold(
            gray, 255, am, cv2.THRESH_BINARY, block, float(params.adaptive_c))
    else:
        _, dst = cv2.threshold(gray, t, 255, cv2.THRESH_BINARY)
    return dst


def morphology(binary: np.ndarray, params: DepthGuidanceParams) -> np.ndarray:
    """形态学滤波：open/close/erode/dilate，kernel 正方形"""
    op = (params.morph_op or "none").lower()
    if op == "none":
        return binary
    ks = max(1, int(params.morph_kernel_size))
    if ks % 2 == 0:
        ks += 1  # 形态学内核需为奇数
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (ks, ks))
    op_map = {
        "open": cv2.MORPH_OPEN,
        "close": cv2.MORPH_CLOSE,
        "erode": cv2.MORPH_ERODE if hasattr(cv2, "MORPH_ERODE") else None,
        "dilate": cv2.MORPH_DILATE if hasattr(cv2, "MORPH_DILATE") else None,
    }
    if op in ("erode", "dilate") and op_map[op] is None:
        # 部分 OpenCV 版本无 MORPH_ERODE/MORPH_DILATE 枚举，使用对应函数
        if op == "erode":
            return cv2.erode(binary, kernel)
        return cv2.dilate(binary, kernel)
    return cv2.morphologyEx(binary, op_map[op], kernel)


def _contour_center(contour: np.ndarray, params: DepthGuidanceParams) -> Tuple[float, float]:
    """计算轮廓中心点坐标（bbox 中心或质心）"""
    if params.center_mode == "centroid":
        m = cv2.moments(contour)
        if m["m00"] > 0:
            return (m["m10"] / m["m00"], m["m01"] / m["m00"])
    x, y, w, h = cv2.boundingRect(contour)
    return (x + w / 2.0, y + h / 2.0)


def find_filtered_contours(binary: np.ndarray,
                           params: DepthGuidanceParams) -> List[np.ndarray]:
    """轮廓查找并按参数过滤，返回通过过滤的轮廓列表"""
    contours, _ = cv2.findContours(
        binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    def in_range(v, lo, hi):
        if lo > hi:
            return True
        return lo <= v <= hi

    kept: List[np.ndarray] = []
    for c in contours:
        area = float(cv2.contourArea(c))
        length = float(cv2.arcLength(c, True))
        x, y, w, h = cv2.boundingRect(c)
        aspect = (w / h) if h > 0 else 0.0
        cx, cy = _contour_center(c, params)
        if not in_range(length, params.min_contour_len, params.max_contour_len):
            continue
        if not in_range(area, params.min_contour_area, params.max_contour_area):
            continue
        if not in_range(w, params.min_bbox_w, params.max_bbox_w):
            continue
        if not in_range(h, params.min_bbox_h, params.max_bbox_h):
            continue
        if not in_range(aspect, params.min_aspect_ratio, params.max_aspect_ratio):
            continue
        if not in_range(cx, params.center_x_min, params.center_x_max):
            continue
        if not in_range(cy, params.center_y_min, params.center_y_max):
            continue
        kept.append(c)
    return kept


# =====================================================================
#  步骤 1.5：最终轮廓筛选（参数过滤后的第二层筛选，按度量取最大/最小）
# =====================================================================
# 筛选模式下拉选项（顺序即 UI 显示顺序）
SEG_FILTER_MODES = (
    "所有轮廓",
    "最大面积轮廓", "最小面积轮廓",
    "最大轮廓长度", "最小轮廓长度",
    "最大外接正矩形", "最小外接正矩形",
    "最大外接旋转矩形", "最小外接旋转矩形",
)


def contour_metric(contour: np.ndarray, mode: str) -> float:
    """按筛选模式计算轮廓度量值

    - *面积* 轮廓: cv2.contourArea（轮廓围成的面积）
    - 轮廓长度: cv2.arcLength(closed=True)（轮廓周长）
    - 外接正矩形: cv2.boundingRect 的 w*h
    - 外接旋转矩形: cv2.minAreaRect 的 w*h（最小面积外接旋转矩形）
    """
    if "面积" in mode:
        return float(cv2.contourArea(contour))
    if "长度" in mode:
        return float(cv2.arcLength(contour, True))
    if "旋转矩形" in mode:
        _, (rw, rh), _angle = cv2.minAreaRect(contour)
        return float(abs(rw) * abs(rh))
    # 外接正矩形（兜底）
    _x, _y, bw, bh = cv2.boundingRect(contour)
    return float(bw * bh)


def filter_contours_final(contours: List[np.ndarray],
                          mode: str,
                          top_n: int = 1) -> List[np.ndarray]:
    """参数过滤后的第二层筛选：按用户选择的模式输出最终轮廓

    Args:
        contours: 步骤 1 参数过滤后的轮廓列表
        mode: :data:`SEG_FILTER_MODES` 之一；"所有轮廓" 原样返回（不排序），
            其余按对应度量排序后取前 ``top_n`` 个——"最大X" 取该度量前 N 大
            （降序），"最小X" 取前 N 小（升序），度量并列时按先出现者优先
        top_n: 保留的轮廓个数（>=1）；N=1 即取单个最大/最小轮廓

    Returns:
        筛选后的轮廓列表（新列表，不改输入；按度量排序）
    """
    if not contours:
        return []
    if mode not in SEG_FILTER_MODES or mode == "所有轮廓":
        return list(contours)
    pick_max = mode.startswith("最大")
    ordered = sorted(contours,
                     key=lambda c: contour_metric(c, mode),
                     reverse=pick_max)
    n = max(1, int(top_n))
    return ordered[:n]


# =====================================================================
#  提示点：轮廓内距离变换内点（供 EdgeSAM 点提示使用）
# =====================================================================
def contour_interior_points(contour: np.ndarray,
                            k: int = 3,
                            margin: float = 5.0) -> List[Tuple[float, float]]:
    """在轮廓内部选取 K 个离边缘尽可能远的提示点（距离变换最大内点）

    质心（moments 一阶矩）只保证面积等效中心，凹形轮廓（C/L 形、弯管
    截面）的形心可能落在轮廓外或贴边——不适合直接当分割提示点。本函数
    对轮廓填充 mask 做 distanceTransform（每像素值 = 到边缘最近距离），
    贪心选取局部最大值点：

    - 第 1 点 = 最大内切圆圆心：必在轮廓内、离边缘最远（始终输出，
      即使 max < margin——过细的碎条也保底有 1 个提示点）；
    - 第 2..K 点 = 抑制已选点内切圆后的下一个最大值点，且需
      max >= margin 才采纳（离边缘太近的坏点不输出）。

    Args:
        contour: Nx1x2 int32 轮廓（调用方坐标系像素）
        k: 最多提示点数（>=1；实际数量取决于形状与 margin）
        margin: 除首点外，提示点到边缘的最小距离（像素）

    Returns:
        [(x, y), ...] 提示点列表（调用方坐标系像素，浮点）
    """
    if contour is None or len(contour) < 3:
        return []
    k = max(1, int(k))
    x, y, w, h = cv2.boundingRect(contour)
    pad = 2   # bbox 外 1px 背景圈 + 1px 余量，保证 distanceTransform 有边界
    mask = np.zeros((h + 2 * pad, w + 2 * pad), dtype=np.uint8)
    shifted = contour.reshape(-1, 2).astype(np.int32) - [x - pad, y - pad]
    cv2.drawContours(mask, [shifted.reshape(-1, 1, 2)], -1, 255, -1)
    dist = cv2.distanceTransform(mask, cv2.DIST_L2, 3)

    pts: List[Tuple[float, float]] = []
    d = dist.copy()
    for i in range(k):
        _, maxv, _, maxloc = cv2.minMaxLoc(d)
        if maxv <= 0:
            break
        if pts and maxv < margin:
            break
        px, py = maxloc
        pts.append((float(px + x - pad), float(py + y - pad)))
        # 抑制该点的内切圆范围，使下一个点沿形状分布
        cv2.circle(d, maxloc, int(np.ceil(maxv)), 0, -1)
    return pts


# =====================================================================
#  步骤 2：轮廓 -> 正外接矩形（以中心点为圆心、边长 X）
# =====================================================================
def contours_to_rects(contours: List[np.ndarray],
                      params: DepthGuidanceParams,
                      img_w: int, img_h: int) -> List[Dict[str, Any]]:
    """由轮廓生成正外接矩形

    以轮廓中心点为圆心，构造边长为 ``rect_side_length`` 的正矩形，并裁剪到
    图像边界内。返回列表元素: ``{cx, cy, x1, y1, x2, y2, w, h}``。
    """
    side = float(params.rect_side_length)
    half = side / 2.0
    rects: List[Dict[str, Any]] = []
    for c in contours:
        cx, cy = _contour_center(c, params)
        x1 = int(round(max(0, cx - half)))
        y1 = int(round(max(0, cy - half)))
        x2 = int(round(min(img_w - 1, cx + half)))
        y2 = int(round(min(img_h - 1, cy + half)))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        rects.append({
            "cx": float(cx), "cy": float(cy),
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "w": x2 - x1, "h": y2 - y1,
        })
    return rects


# =====================================================================
#  步骤 3：圆心区域平均深度 -> XYZ
# =====================================================================
def average_depth_in_circle(depth_mm: np.ndarray, cx: float, cy: float,
                            diameter: float,
                            valid_min_mm: float = 1.0) -> Tuple[float, np.ndarray]:
    """以 (cx, cy) 为中心、直径 diameter 画圆，取圆内有效深度平均值

    Args:
        depth_mm: 毫米深度图(float32)
        cx, cy: 圆心像素坐标
        diameter: 直径 R(pix)
        valid_min_mm: 有效深度下限(mm)，低于该值视为无效

    Returns:
        (avg_depth_mm, mask): 平均深度(mm)，无效时为 0.0；圆内有效像素掩膜
    """
    if depth_mm is None or depth_mm.size == 0 or diameter <= 0:
        return 0.0, np.zeros((0, 0), dtype=np.uint8)
    h, w = depth_mm.shape[:2]
    r = diameter / 2.0
    x1 = max(0, int(np.floor(cx - r)))
    x2 = min(w - 1, int(np.ceil(cx + r)))
    y1 = max(0, int(np.floor(cy - r)))
    y2 = min(h - 1, int(np.ceil(cy + r)))
    if x2 < x1 or y2 < y1:
        return 0.0, np.zeros((0, 0), dtype=np.uint8)

    # 在局部 ROI 内生成圆形掩膜，避免整图运算
    roi = depth_mm[y1:y2 + 1, x1:x2 + 1]
    yy, xx = np.ogrid[:roi.shape[0], :roi.shape[1]]
    local_cx = cx - x1
    local_cy = cy - y1
    circle_mask = (xx - local_cx) ** 2 + (yy - local_cy) ** 2 <= r * r
    valid = circle_mask & (roi > valid_min_mm)
    if not np.any(valid):
        return 0.0, circle_mask.astype(np.uint8)
    avg = float(roi[valid].mean())
    return avg, valid.astype(np.uint8)


def compute_xyz_from_depth_mm(depth_mm: float, x: float, y: float,
                              intrinsics: Dict[str, Any]) -> Tuple[float, float, float]:
    """由深度(mm) + 像素坐标 + 内参计算相机坐标系 XYZ(mm)

    与 guidance_locate_tab.pixel_to_xyz 保持一致（米->毫米）。
    """
    if depth_mm <= 0:
        return (0.0, 0.0, 0.0)
    depth_m = depth_mm / 1000.0
    fx = float(intrinsics["fx"])
    fy = float(intrinsics["fy"])
    ppx = float(intrinsics.get("ppx", intrinsics.get("cx", 0.0)))
    ppy = float(intrinsics.get("ppy", intrinsics.get("cy", 0.0)))
    X = (x - ppx) * depth_m / fx
    Y = (y - ppy) * depth_m / fy
    Z = depth_m
    return (X * 1000.0, Y * 1000.0, Z * 1000.0)


def point_depth_xyz(depth_mm: np.ndarray, x: float, y: float,
                    intrinsics: Dict[str, Any],
                    params: DepthGuidanceParams
                    ) -> Tuple[Optional[Tuple[float, float, float]], float]:
    """统一计算点 (x,y) 的 XYZ：启用时用圆心区域平均深度，否则单点深度

    Returns:
        (xyz_mm, depth_mm): xyz_mm 为 (X,Y,Z) mm，深度无效时为 None；
        depth_mm 为实际采用的深度(mm)
    """
    if depth_mm is None or intrinsics is None:
        return None, 0.0
    h, w = depth_mm.shape[:2]
    xi, yi = int(round(x)), int(round(y))
    if not (0 <= xi < w and 0 <= yi < h):
        return None, 0.0

    if params.avg_depth_enable and params.avg_depth_diameter > 0:
        depth, _ = average_depth_in_circle(
            depth_mm, float(x), float(y),
            float(params.avg_depth_diameter),
            valid_min_mm=float(params.depth_valid_min_mm))
    else:
        v = float(depth_mm[yi, xi])
        depth = v if v >= params.depth_valid_min_mm else 0.0

    if depth <= 0:
        return None, 0.0
    xyz = compute_xyz_from_depth_mm(depth, float(x), float(y), intrinsics)
    return xyz, depth


# =====================================================================
#  通道 ROI 深度检测：矩形裁剪 + 深度范围二值化 + 多边形 mask 与运算
# =====================================================================
def detect_roi_polygon_overlap(depth_array: np.ndarray,
                               depth_scale: float,
                               roi: Optional[Tuple[int, int, int, int]],
                               polygon: List[Tuple[float, float]],
                               depth_min_mm: float,
                               depth_max_mm: float,
                               morph_op: str = "close",
                               morph_kernel: int = 3,
                               min_contour_area: float = 0.0
                               ) -> Dict[str, Any]:
    """通道深度检测：按用户 ROI 矩形裁剪深度图，深度范围二值化后找轮廓，
    与用户绘制的多边形 mask 做与运算，判断是否存在重叠。

    深度二值化直接按 [depth_min_mm, depth_max_mm] 范围筛选（不同于
    run_depth_guidance 的归一化->二值化路径），形态学去噪复用 morphology()。

    Args:
        depth_array: 原始深度数组（uint16）
        depth_scale: 米/原始单位
        roi: (x1, y1, x2, y2) 原始图像坐标系（x2/y2 为开界），None 时不检测
        polygon: [(x, y), ...] 原始图像坐标系顶点，少于 3 点视为无 mask
                 （无 mask 时只找轮廓，不做重叠判断）
        depth_min_mm / depth_max_mm: 有效深度范围（mm）
        morph_op / morph_kernel: 形态学去噪参数（见 morphology()）
        min_contour_area: 最小轮廓面积过滤（pix²），0 表示不过滤

    Returns:
        dict:
            contours: 通过过滤的轮廓列表（ROI 内局部坐标，np.ndarray）
            offset: ROI 左上角 (x1, y1)，局部坐标 + offset = 原图坐标
            overlap_flags: 各轮廓是否与多边形重叠（无 polygon 时为空列表）
            overlap: 是否存在任一重叠（无 polygon 时为 None）
            n_contours / n_overlap
    """
    result: Dict[str, Any] = {
        "contours": [], "offset": (0, 0), "overlap_flags": [],
        "overlap": None, "n_contours": 0, "n_overlap": 0,
    }
    if depth_array is None or depth_array.size == 0 or roi is None:
        return result
    h, w = depth_array.shape[:2]
    x1, y1, x2, y2 = (int(v) for v in roi[:4])
    x1 = max(0, min(x1, w - 1))
    y1 = max(0, min(y1, h - 1))
    x2 = max(x1 + 1, min(int(x2), w))
    y2 = max(y1 + 1, min(int(y2), h))
    crop = depth_array[y1:y2, x1:x2]
    if crop.size == 0:
        return result
    result["offset"] = (x1, y1)

    depth_mm = depth_to_mm(crop, depth_scale)
    # 深度范围二值化：范围内像素为前景 255，其余（含无效值）为 0
    mask = ((depth_mm >= float(depth_min_mm)) &
            (depth_mm <= float(depth_max_mm))).astype(np.uint8) * 255

    mp = DepthGuidanceParams(morph_op=morph_op, morph_kernel_size=int(morph_kernel))
    binary = morphology(mask, mp)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if min_contour_area > 0:
        contours = [c for c in contours
                    if cv2.contourArea(c) >= float(min_contour_area)]
    result["contours"] = list(contours)
    result["n_contours"] = len(contours)

    if not polygon or len(polygon) < 3:
        return result

    # 多边形 mask（ROI 局部坐标）
    poly = np.asarray(polygon, dtype=np.int32).reshape(-1, 2) - (x1, y1)
    poly_mask = np.zeros(binary.shape, dtype=np.uint8)
    cv2.fillPoly(poly_mask, [poly], 255)

    flags: List[bool] = []
    for c in contours:
        c_mask = np.zeros(binary.shape, dtype=np.uint8)
        cv2.drawContours(c_mask, [c], -1, 255, thickness=-1)
        inter = cv2.bitwise_and(c_mask, poly_mask)
        flags.append(bool(np.any(inter)))
    result["overlap_flags"] = flags
    result["n_overlap"] = sum(1 for f in flags if f)
    result["overlap"] = result["n_overlap"] > 0
    return result


# =====================================================================
#  顶层入口：步骤 1 + 步骤 2
# =====================================================================
def run_depth_guidance(depth_array: np.ndarray,
                       depth_scale: float,
                       params: DepthGuidanceParams,
                       color_image: Optional[np.ndarray] = None,
                       include_rects: bool = True
                       ) -> Dict[str, Any]:
    """执行深度图引导检测流程（步骤 1；include_rects=True 时含步骤 2）

    Args:
        depth_array: 原始深度数组(uint16)
        depth_scale: 米/原始单位
        params: 参数
        color_image: 可选彩色图，用于在 debug 图上叠加轮廓（与自动框）
        include_rects: 是否生成正外接矩形（步骤2）。False 时只执行步骤1
            的轮廓筛选（深度图分割用法），结果不含 rects，debug 图只叠加
            轮廓、不画矩形/中心点。

    Returns:
        dict:
            - rects: 正外接矩形列表 [{cx,cy,x1,y1,x2,y2,w,h}, ...]
              （include_rects=False 时为空列表）
            - contours: 通过过滤的轮廓列表
            - depth_mm: 毫米深度图
            - gray: 归一化灰度图
            - binary: 二值图
            - filtered: 形态学滤波后的二值图
            - debug_image: 在彩色图/灰度图上叠加轮廓与自动框的结果图
            - n_contours: 通过过滤的轮廓数
    """
    depth_mm = depth_to_mm(depth_array, depth_scale)
    masked = segment_depth(depth_mm, params)
    gray = normalize_to_gray(masked_mm=masked, params=params,
                            raw_depth=depth_array)
    binary = binarize(gray, params)
    filtered = morphology(binary, params)
    contours = find_filtered_contours(filtered, params)
    h, w = depth_mm.shape[:2]
    rects = contours_to_rects(contours, params, w, h) if include_rects else []

    # 轮廓筛选结果图：在形态学滤波后的二值图上叠加保留轮廓(绿)，便于观察过滤效果
    contour_image = cv2.cvtColor(filtered, cv2.COLOR_GRAY2BGR)
    cv2.drawContours(contour_image, contours, -1, (0, 255, 0), 2)
    if include_rects:
        for r in rects:
            cx, cy = int(r["cx"]), int(r["cy"])
            cv2.circle(contour_image, (cx, cy), 3, (0, 0, 255), -1)

    # debug 图：优先在彩色图上叠加，否则用灰度图
    debug_image: Optional[np.ndarray] = None
    if color_image is not None:
        debug_image = color_image.copy()
        cv2.drawContours(debug_image, contours, -1, (0, 255, 0), 2)
        if include_rects:
            for r in rects:
                cv2.rectangle(debug_image, (r["x1"], r["y1"]),
                              (r["x2"], r["y2"]), (0, 165, 255), 2)
                cv2.circle(debug_image, (int(r["cx"]), int(r["cy"])), 3,
                           (0, 0, 255), -1)
    else:
        debug_image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        cv2.drawContours(debug_image, contours, -1, (0, 255, 0), 2)
        if include_rects:
            for r in rects:
                cv2.rectangle(debug_image, (r["x1"], r["y1"]),
                              (r["x2"], r["y2"]), (0, 165, 255), 2)

    return {
        "rects": rects,
        "contours": contours,
        "depth_mm": depth_mm,
        "gray": gray,
        "binary": binary,
        "filtered": filtered,
        "contour_image": contour_image,
        "debug_image": debug_image,
        "n_contours": len(contours),
    }


# =====================================================================
#  辅助：在结果图上绘制圆心区域 R 圆（步骤 3 可视化）
# =====================================================================
def draw_avg_depth_circle(image: np.ndarray, cx: float, cy: float,
                          diameter: float, depth_mm: Optional[float] = None
                          ) -> np.ndarray:
    """在结果图上绘制直径 R 的圆心区域圆，便于用户调整

    Args:
        image: BGR 图像
        cx, cy: 圆心像素坐标
        diameter: 直径 R(pix)
        depth_mm: 可选，附带平均深度文字
    """
    out = image.copy()
    r = int(round(diameter / 2.0))
    if r > 0:
        cv2.circle(out, (int(round(cx)), int(round(cy))), r, (255, 0, 255), 1)
    if depth_mm is not None:
        txt = f"R={diameter:.0f} d={depth_mm:.0f}mm"
        cv2.putText(out, txt, (int(round(cx)) + r + 3, int(round(cy)) + 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 1)
    return out


# =====================================================================
#  独立测试入口
# =====================================================================
if __name__ == "__main__":
    import os
    import sys

    if len(sys.argv) < 2:
        print("用法: python depth_guidance_pipeline.py <深度图.tiff> [彩色图.jpg]")
        sys.exit(0)

    depth_path = sys.argv[1]
    color_path = sys.argv[2] if len(sys.argv) > 2 else None
    depth = cv2.imread(depth_path, cv2.IMREAD_UNCHANGED)
    if depth is None:
        print(f"无法读取深度图: {depth_path}")
        sys.exit(1)
    color = cv2.imread(color_path) if color_path else None

    p = DepthGuidanceParams()
    res = run_depth_guidance(depth, 0.001, p, color)
    print(f"通过过滤轮廓数: {res['n_contours']}, 生成矩形数: {len(res['rects'])}")
    for i, r in enumerate(res["rects"]):
        print(f"  矩形#{i + 1}: 中心=({r['cx']:.1f},{r['cy']:.1f}) "
              f"框=({r['x1']},{r['y1']})-({r['x2']},{r['y2']})")

    dbg = res["debug_image"]
    if dbg is not None:
        out_path = os.path.splitext(depth_path)[0] + "_depth_guidance.jpg"
        cv2.imwrite(out_path, dbg)
        print(f"debug 图已保存: {out_path}")
