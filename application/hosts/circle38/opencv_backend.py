"""Linux / OpenCV 圆检测后端（DEC-072）。

**背景**：cp38 的 ``timesai_tools.cp38-win_amd64.pyd`` 只有 Windows 3.8 原生件，
Ubuntu 上没有对应 .so。为让 Ubuntu 包自带「找圆/引导定位」能力，这里按旧工程
``detect_circle_imp`` 暴露的参数语义，用 OpenCV 复刻同一条处理思路：

    灰度/高斯 → 梯度边缘（Canny）→ 连通弧段 → 最小二乘圆拟合 → 弧段合并
    → 半径/完整度/拟合误差/点数筛选 → 按 circle_type 选圆

**这不是原生算法**：``host_version = "opencv-linux"``，行为与 cp38 原生件不保证逐位一致，
现场阈值需按样本复核；对外证据一律标注后端名，``hardware = NOT_RUN``。
若现场以后拿到 Linux 版原生 ``timesai_tools``（.so），宿主 ``--backend native`` 仍优先原生。

参数映射（与 :class:`CircleParams` 同名同义，未用到的项在下方注明）：
- ``grad_thresh``：Canny 低阈值（高阈值 = 2×低阈值），默认 50
- ``guass_sigma``：高斯模糊 sigma，<=0 关闭
- ``min_path_len``：弧段最少像素点数
- ``arc_fit_error``：单条弧段拟合的**平均**残差上限（px）
- ``circle_fit_error``：弧段合并容差 + 最终圆的内点残差上限（px）
- ``min_radius`` / ``max_radius``：半径范围（px）
- ``min_cover_ratio`` / ``max_cover_ratio``：弧段角覆盖比例范围（0..1）
- ``min_arc_points`` / ``max_arc_points``：内点点数范围
- ``circle_validation_pro``：内点比例下限
- ``circle_type``：``最大圆 / 最小圆 / 所有圆 / 最大圆和最小圆``
- ``output_type`` / ``output_point`` / ``anchor_thresh`` / ``resize_factor`` / ``thread_num``：
  现有宿主契约不返回调试图/构成点，这些字段只做校验与日志占位（不影响几何结果）。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from application.algorithms.circle.params import CircleParams


class OpenCVLoadError(RuntimeError):
    """OpenCV 不可用（未安装/导入失败）。"""


_CIRCLE_TYPES = ("最大圆", "最小圆", "所有圆", "最大圆和最小圆")


class OpenCVCircleBackend:
    """与 ``NativeCircleBackend`` 同接口的纯 OpenCV 实现（Linux/沙箱通用）。"""

    host_version = "opencv-linux"

    def __init__(self) -> None:
        self._cv2 = None

    def load(self) -> None:
        try:
            import cv2  # noqa: F401 - 只在这里导入；宿主进程按需加载
        except Exception as exc:  # noqa: BLE001
            raise OpenCVLoadError(f"无法导入 OpenCV(cv2): {exc}") from exc
        self._cv2 = cv2

    # ------------------------------------------------------------------ 主入口
    def detect(self, images: list[Any], rects: list[list[int]],
               params: CircleParams) -> dict:
        """``rects`` = 本帧扁平矩形列表 ``[type,x,y,w,h,angle]``（宿主契约）。"""
        if self._cv2 is None:
            raise OpenCVLoadError("OpenCV 后端未 load()")
        if not images:
            raise ValueError("缺少输入图像")
        image = images[0]
        if not isinstance(image, np.ndarray) or image.size == 0:
            raise ValueError(f"图像非法: {type(image).__name__}")
        if params.circle_type not in _CIRCLE_TYPES:
            raise ValueError(
                f"未知找圆类型 {params.circle_type!r}（可选 {_CIRCLE_TYPES}）")

        gray = self._to_gray(image)
        circles = self._detect_circles(gray, rects, params)
        kept = self._select(circles, params.circle_type)
        return self._native_result(kept)

    # ------------------------------------------------------------------ 处理链
    def _to_gray(self, image: np.ndarray) -> np.ndarray:
        cv2 = self._cv2
        if image.ndim == 2:
            return image
        if image.ndim == 3 and image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    def _edge_map(self, gray: np.ndarray, params: CircleParams) -> np.ndarray:
        cv2 = self._cv2
        sigma = float(params.guass_sigma or 0.0)
        blurred = (cv2.GaussianBlur(gray, (0, 0), sigma) if sigma > 0 else gray)
        low = max(1, int(round(float(params.grad_thresh or 50.0))))
        high = max(low + 1, low * 2)
        return cv2.Canny(blurred, low, high)

    def _regions(self, shape: tuple[int, int],
                 rects: list[list[int]]) -> list[tuple[int, int, int, int, int]]:
        """把矩形归一化成 ``(x, y, w, h, angle)``；空列表 = 整图。"""
        h_img, w_img = int(shape[0]), int(shape[1])
        if not rects:
            return [(0, 0, w_img, h_img, 0)]
        out = []
        for rect in rects:
            try:
                _type, x, y, w, h, angle = [int(v) for v in list(rect)[:6]]
            except (TypeError, ValueError):
                continue
            x = max(0, min(x, w_img - 1))
            y = max(0, min(y, h_img - 1))
            w = max(1, min(w, w_img - x))
            h = max(1, min(h, h_img - y))
            out.append((x, y, w, h, angle))
        return out or [(0, 0, w_img, h_img, 0)]

    def _region_mask(self, shape: tuple[int, int],
                     region: tuple[int, int, int, int, int]) -> np.ndarray:
        cv2 = self._cv2
        x, y, w, h, angle = region
        mask = np.zeros(shape, dtype=np.uint8)
        if angle:
            corners = cv2.boxPoints(((x + w / 2.0, y + h / 2.0), (w, h), angle))
            cv2.fillPoly(mask, [np.round(corners).astype(np.int32)], 255)
        else:
            mask[y:y + h, x:x + w] = 255
        return mask

    def _detect_circles(self, gray: np.ndarray, rects: list[list[int]],
                        params: CircleParams) -> list[dict]:
        cv2 = self._cv2
        edges = self._edge_map(gray, params)
        tol = max(0.5, float(params.circle_fit_error or 2.0))
        arcs: list[dict] = []
        for region in self._regions(gray.shape, rects):
            mask = self._region_mask(gray.shape, region)
            region_edges = cv2.bitwise_and(edges, mask)
            contours = cv2.findContours(region_edges, cv2.RETR_LIST,
                                        cv2.CHAIN_APPROX_NONE)[-2]
            for contour in contours:
                pts = contour.reshape(-1, 2).astype(np.float64)
                if len(pts) < max(5, int(float(params.min_path_len or 0.0))):
                    continue
                fit = _fit_circle(pts)
                if fit is None:
                    continue
                cx, cy, r, resid = fit
                if not (math.isfinite(cx) and math.isfinite(cy) and math.isfinite(r)):
                    continue
                if r < 2.0 or r > 0.75 * max(gray.shape):
                    continue                     # 直线/噪声拟合出的巨大半径
                if resid > max(1.0, float(params.arc_fit_error or 1.0)):
                    continue                     # 弧段本身不像圆弧
                arcs.append({"cx": cx, "cy": cy, "r": r, "pts": pts})
        merged = self._merge_arcs(arcs, tol)
        return [self._evaluate(c, params) for c in merged]

    def _merge_arcs(self, arcs: list[dict], tol: float) -> list[dict]:
        """把同一圆的弧段合并（中心距与半径差都在容差内）；并查集，确定性顺序。"""
        parent = list(range(len(arcs)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        scale = max(1.0, tol)
        r_tol = max(3.0, scale * 2.0)      # 同一条描边会产生内外两条边缘，半径差约等于线宽
        c_tol = max(2.5, scale * 1.5)
        for i in range(len(arcs)):
            for j in range(i + 1, len(arcs)):
                a, b = arcs[i], arcs[j]
                if (abs(a["r"] - b["r"]) <= r_tol
                        and math.hypot(a["cx"] - b["cx"], a["cy"] - b["cy"])
                        <= c_tol):
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[rj] = ri
        groups: dict[int, list[dict]] = {}
        for i in range(len(arcs)):
            groups.setdefault(find(i), []).append(arcs[i])
        out = []
        for key in sorted(groups):
            parts = groups[key]
            pts = np.vstack([p["pts"] for p in parts])
            fit = _fit_circle(pts)
            if fit is not None:
                cx, cy, r, resid = fit
                out.append({"cx": cx, "cy": cy, "r": r, "pts": pts})
        return out

    def _evaluate(self, item: dict, params: CircleParams) -> dict:
        """计算覆盖比例/内点比例，按参数给出是否通过（flag）。"""
        pts = item["pts"]
        cx, cy, r = item["cx"], item["cy"], item["r"]
        dist = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        tol = max(1.0, float(params.circle_fit_error or 2.0))
        inside = np.abs(dist - r) <= tol
        n_in = int(np.count_nonzero(inside))
        n_all = max(1, int(pts.shape[0]))
        inlier_ratio = n_in / n_all
        coverage = _angular_coverage(pts[inside], cx, cy) if n_in else 0.0
        ok = (
            float(params.min_radius or 0.0) <= r <= float(params.max_radius or 1e9)
            and float(params.min_cover_ratio or 0.0) - 1e-9
            <= coverage <= float(params.max_cover_ratio or 1.0) + 1e-9
            and int(params.min_arc_points or 0) <= n_in <= int(params.max_arc_points or 10 ** 9)
            and inlier_ratio + 1e-9 >= float(params.circle_validation_pro or 0.0)
        )
        return {"cx": float(cx), "cy": float(cy), "r": float(r),
                "coverage": float(coverage), "points": n_in, "ok": bool(ok)}

    @staticmethod
    def _select(circles: list[dict], circle_type: str) -> list[dict]:
        good = [c for c in circles if c["ok"]]
        if not good:
            return []
        by_r = sorted(good, key=lambda c: c["r"])
        if circle_type == "最大圆":
            return [by_r[-1]]
        if circle_type == "最小圆":
            return [by_r[0]]
        if circle_type == "最大圆和最小圆":
            return by_r if len(by_r) == 1 else [by_r[0], by_r[-1]]
        return by_r                       # 所有圆

    @staticmethod
    def _native_result(circles: list[dict]) -> dict:
        """与原生同构的返回结构（宿主契约；下游 ``parse_native_circles`` 同口径）。"""
        show = [[1, c["cx"], c["cy"], c["r"]] for c in circles]
        centers = [[[0, c["cx"], c["cy"]] for c in circles]]
        radii = [[c["r"] for c in circles]]
        diameters = [[2.0 * c["r"] for c in circles]]
        coverage = [[c["coverage"] for c in circles]]
        return {
            "statues": [1], "error_msg": [""],
            "circle_num": [[len(circles)]],
            "circles": [None], "circle_centers": centers, "radius": radii,
            "diameters": diameters, "circle_points": [None],
            "circle_coverage": coverage, "output_images": [None],
            "circles_for_show": [show],
        }


def _fit_circle(pts: np.ndarray) -> tuple[float, float, float, float] | None:
    """Kasa 代数最小二乘圆拟合；返回 ``(cx, cy, r, 平均径向残差)``。"""
    if pts.shape[0] < 3:
        return None
    x = pts[:, 0]
    y = pts[:, 1]
    a = np.column_stack([x, y, np.ones_like(x)])
    b = x * x + y * y
    try:
        sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx = sol[0] / 2.0
    cy = sol[1] / 2.0
    r2 = sol[2] + cx * cx + cy * cy
    if not math.isfinite(r2) or r2 <= 0:
        return None
    r = math.sqrt(r2)
    resid = float(np.mean(np.abs(np.hypot(x - cx, y - cy) - r)))
    return float(cx), float(cy), float(r), resid


def _angular_coverage(pts: np.ndarray, cx: float, cy: float) -> float:
    """弧段完整度：1 − 最大角度空隙/2π（整圆≈1，短弧≈弧长占比）。"""
    if pts.shape[0] < 2:
        return 0.0
    angles = np.sort(np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx))
    gaps = np.diff(np.concatenate([angles, angles[:1] + 2.0 * math.pi]))
    max_gap = float(np.max(gaps)) if gaps.size else 2.0 * math.pi
    return max(0.0, min(1.0, 1.0 - max_gap / (2.0 * math.pi)))


__all__ = ["OpenCVCircleBackend", "OpenCVLoadError"]
