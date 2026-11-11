#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""深度图批量统计：最小/平均深度、分位数、无效占比、最大平面（DEC-127）。

对每张深度图（DEC-123 保存的 `<名>_depth.npy` / `<cam>-fN_depth.npy` / `_depth16.png`）统计：

- 基础：尺寸、有效/无效像素（原始值 0 或 ≥65535）、深度最小/最大值（含像素位置）、
  均值/中位数/标准差、分位数 P1/P5/P25/P50/P75/P95/P99、中心区域深度、深度直方图；
- 最大平面：在「逆深度 t = a·u + b·v + c」空间做确定性 RANSAC——任何三维平面投影到
  图像后，逆深度都是像素坐标的线性函数，与相机内参无关；再对全分辨率有效像素做最小二乘
  精修 + 最大连通域过滤（保证是「一块面」而不是散点）。有内参（`_depth.json` 或 --fx 等）
  时提示平面法线、与光轴夹角、沿光轴距离；可一次统计前 N 块平面；
- 输出：控制台中文报告；`--out 目录` 时另写 `summary.csv`（每图一行）、每图
  `<名>_stats.json`、`<名>_planes.png`（伪彩 + 平面叠加，便于人工核对）。

用法（tower-ops 目录）：
    python scripts/depth_stats.py runtime/frames/ds86_01_20261105_160709_depth.npy
    python scripts/depth_stats.py runtime/frames --out runtime/depth_stats
    python scripts/depth_stats.py D:/saved --out D:/depth_stats --planes 3
    python scripts/depth_stats.py x_depth.npy --fx 1200 --fy 1200 --cx 800 --cy 600

约定（与 frame_import / depth_guidance 一致）：
- 深度单位：`深度(mm) = 原始值 × depth_scale × 1000`；depth_scale 默认 1e-3，即
  1 原始值 = 1mm；同目录 `_depth.json` 的 depth_scale_m 优先，`--depth-scale` 可强制覆盖；
- 无效像素：原始值 0（无回波）或 ≥ --invalid-threshold（默认 65535），统计前剔除；
- 纯离线只读文件，不连相机、不动机械臂；RANSAC 固定随机种子，同输入结果可复现。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Any

import numpy as np

try:  # 画预览/读 16 位 PNG 需要；没有也能做统计（只是不出预览）
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]

_NPY_SUFFIX = ".npy"
_PNG16_SUFFIX = "_depth16.png"
_PCTLS = (1, 5, 25, 50, 75, 95, 99)
_PLANE_COLORS_BGR = (
    (0, 255, 0),      # P1 绿
    (0, 170, 255),    # P2 橙
    (255, 0, 255),    # P3 品红
    (255, 255, 0),    # P4 青
    (128, 0, 255),    # P5 红紫
)


# ------------------------------------------------------------------ 输入发现与读取

def discover_inputs(paths: list[str]) -> list[str]:
    """把文件/目录/通配展开成深度图文件列表（目录递归找 *.npy 与 *_depth16.png）。"""
    out: list[str] = []
    for p in paths:
        for pat in (p,):
            import glob as _glob
            hits = _glob.glob(pat)
            if not hits:
                hits = [p]
            for h in hits:
                if os.path.isdir(h):
                    for name in sorted(os.listdir(h)):
                        full = os.path.join(h, name)
                        if os.path.isdir(full):
                            inner = discover_inputs([full])
                            out.extend(inner)
                        elif name.lower().endswith(_NPY_SUFFIX) or \
                                name.lower().endswith(_PNG16_SUFFIX):
                            out.append(full)
                elif os.path.isfile(h):
                    out.append(h)
    # 去重保序
    seen, uniq = set(), []
    for f in out:
        key = os.path.abspath(f).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


class NonDepthError(RuntimeError):
    """输入是图像但不是 2D 深度图（例如彩色 npy）。"""


def load_depth_array(path: str) -> np.ndarray:
    """读深度数组：.npy 直接 np.load；16 位 PNG 用 cv2 原值读。非 2D 报错。"""
    if path.lower().endswith(_NPY_SUFFIX):
        arr = np.load(path, allow_pickle=False)
    else:
        if cv2 is None:
            raise RuntimeError("读 16 位 PNG 需要 OpenCV（本环境没有），请改用 .npy")
        arr = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise RuntimeError("读不出图像内容")
    if arr.ndim == 3:  # 彩色 npy / 彩色图：不是深度图
        raise NonDepthError(f"不是深度图（shape={arr.shape}，应为 2D）")
    if arr.ndim != 2:
        raise NonDepthError(f"不是 2D 深度数组（shape={arr.shape}）")
    return arr


def find_meta_path(path: str) -> str | None:
    """找配对元数据 `_depth.json`（DEC-123 四件套命名）。"""
    base_dir = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    cands = []
    if name.lower().endswith("_depth.npy"):
        cands.append(os.path.join(base_dir, name[: -len("_depth.npy")] + "_depth.json"))
    if name.lower().endswith(_PNG16_SUFFIX):
        cands.append(os.path.join(base_dir, name[: -len("_depth16.png")] + "_depth.json"))
    cands.append(os.path.join(base_dir, os.path.splitext(name)[0] + ".json"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def load_meta(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------ 统计工具

def _percentiles(values: np.ndarray) -> dict[str, float]:
    pct = np.percentile(values, _PCTLS)
    return {f"p{q:02d}": float(v) for q, v in zip(_PCTLS, pct)}


def _histogram(values: np.ndarray, lo: float, hi: float, bins: int = 10) -> dict[str, Any]:
    if hi <= lo:
        hi = lo + 1.0
    counts, edges = np.histogram(values, bins=bins, range=(lo, hi))
    return {
        "lo_mm": float(lo), "hi_mm": float(hi), "bins": int(bins),
        "edges_mm": [float(e) for e in edges],
        "counts": [int(c) for c in counts],
    }


# ------------------------------------------------------------------ 平面检测（逆深度空间 RANSAC）

def _score_chunk(coeffs: np.ndarray, su: np.ndarray, sv: np.ndarray, sd: np.ndarray,
                 tol_mm: float) -> np.ndarray:
    """一批平面系数对样本点的深度内点数。coeffs: (k,3)，返回 (k,)。"""
    tp = (coeffs[:, 0, None] * su[None, :]
          + coeffs[:, 1, None] * sv[None, :]
          + coeffs[:, 2, None])
    with np.errstate(divide="ignore", invalid="ignore"):
        pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
    good = (tp > 1e-12) & (np.abs(pd - sd[None, :]) <= tol_mm)
    return good.sum(axis=1)


def _ransac_inverse_depth(su: np.ndarray, sv: np.ndarray, st: np.ndarray, sd: np.ndarray,
                          *, iterations: int, tol_mm: float,
                          rng: np.random.Generator) -> np.ndarray | None:
    """最小样本=3 的确定性 RANSAC（向量化、分批）。返回最好平面系数 (a,b,c)。"""
    n = su.size
    if n < 3:
        return None
    best: np.ndarray | None = None
    best_count = -1
    batch = 64
    done = 0
    while done < iterations:
        k = min(batch, iterations - done)
        idx = rng.integers(0, n, size=(k, 3))
        # 去掉三点重合的退化样本
        valid = ~((idx[:, 0] == idx[:, 1]) | (idx[:, 0] == idx[:, 2]) | (idx[:, 1] == idx[:, 2]))
        # 组装成 (k, 3点, 3列)：每行 [u, v, 1]，右端项 t
        cols_u = su[idx]
        cols_v = sv[idx]
        A = np.stack([cols_u, cols_v, np.ones_like(cols_u)], axis=2)
        b = st[idx]
        det = np.linalg.det(A)
        ok = valid & (np.abs(det) > 1e-12)
        coeffs = np.zeros((k, 3), dtype=np.float64)
        if ok.any():
            # b 以 (n,3,1) 形式传入，numpy 1.x / 2.x 行为一致；结果 (n,3,1)→(n,3)
            sol = np.linalg.solve(A[ok], b[ok][..., None])
            coeffs[ok] = sol[..., 0]
        for s in range(0, k, 16):
            cc = coeffs[s:s + 16]
            okk = ok[s:s + 16]
            if not okk.any():
                continue
            counts = _score_chunk(cc, su, sv, sd, tol_mm).astype(np.int64)
            counts[~okk] = -1
            j = int(np.argmax(counts))
            if counts[j] > best_count:
                best_count = int(counts[j])
                best = cc[j].copy()
        done += k
    return best


def _refine_plane(coeff: np.ndarray, un: np.ndarray, vn: np.ndarray,
                  t: np.ndarray, d: np.ndarray, tol_mm: float,
                  iters: int = 3) -> tuple[np.ndarray, np.ndarray]:
    """用全分辨率（剩余）有效像素做最小二乘精修，返回 (系数, 每像深残差mm)。"""
    for _ in range(iters):
        tp = coeff[0] * un + coeff[1] * vn + coeff[2]
        with np.errstate(divide="ignore", invalid="ignore"):
            pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
        m = (tp > 1e-12) & (np.abs(pd - d) <= tol_mm)
        if int(m.sum()) < 3:
            break
        A = np.column_stack([un[m], vn[m], np.ones(int(m.sum()))])
        new, *_ = np.linalg.lstsq(A, t[m], rcond=None)
        coeff = np.asarray(new, dtype=np.float64)
    tp = coeff[0] * un + coeff[1] * vn + coeff[2]
    with np.errstate(divide="ignore", invalid="ignore"):
        pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
    resid = d - pd
    return coeff, resid


def _largest_component_mask(mask_valid: np.ndarray, ys: np.ndarray, xs: np.ndarray,
                            shape: tuple[int, int]) -> np.ndarray | None:
    """在整帧上取 inlier 的最大 8 连通域（保证平面是一整块面）。无 cv2 时原样返回。"""
    if cv2 is None:
        return mask_valid if bool(mask_valid.any()) else None
    full = np.zeros(shape, dtype=np.uint8)
    full[ys[mask_valid], xs[mask_valid]] = 1
    n, labels, stats, _ = cv2.connectedComponentsWithStats(full, connectivity=8)
    if n <= 1:
        return None
    j = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    keep = labels == j
    return keep[ys, xs]


def _plane_stats(comp: np.ndarray, xs: np.ndarray, ys: np.ndarray, d: np.ndarray,
                 resid: np.ndarray, coeff: np.ndarray, shape: tuple[int, int],
                 valid_total: int, intrinsics: dict[str, float] | None,
                 order: int) -> dict[str, Any]:
    h, w = shape
    px = int(comp.sum())
    sel_x = xs[comp]
    sel_y = ys[comp]
    dd = d[comp]
    r2 = resid[comp]
    a, b, c = (float(coeff[0]), float(coeff[1]), float(coeff[2]))
    # 归一化像素坐标 un=x/w, vn=y/h 还原成「每像素」系数：t = α·x + β·y + γ
    alpha, beta, gamma = a / w, b / h, c
    mean_d = float(dd.mean())
    stats: dict[str, Any] = {
        "index": int(order),
        "pixels": px,
        "ratio_of_valid": round(px / max(valid_total, 1), 6),
        "mean_depth_mm": round(mean_d, 2),
        "min_depth_mm": round(float(dd.min()), 2),
        "max_depth_mm": round(float(dd.max()), 2),
        "rms_mm": round(float(np.sqrt(np.mean(r2 * r2))), 3),
        "bbox_xyxy": [int(sel_x.min()), int(sel_y.min()),
                      int(sel_x.max()), int(sel_y.max())],
        "centroid_xy": [round(float(sel_x.mean()), 1), round(float(sel_y.mean()), 1)],
        # 无内参也能给的「倾斜强度」：横向/纵向每 100 像素的深度变化（mm，按平均深度折算）
        "slope_x_mm_per_100px": round(-alpha * mean_d * mean_d * 100.0, 3),
        "slope_y_mm_per_100px": round(-beta * mean_d * mean_d * 100.0, 3),
        "inverse_depth_coeff": [round(a, 9), round(b, 9), round(c, 9)],
    }
    if intrinsics:
        fx, fy = float(intrinsics["fx"]), float(intrinsics["fy"])
        cx, cy = float(intrinsics["cx"]), float(intrinsics["cy"])
        # t = α·x + β·y + γ；相机系 X/Z=(x-cx)/fx → 平面 A·X+B·Y+C·Z = 1
        A = alpha * fx
        B = beta * fy
        C = alpha * cx + beta * cy + gamma
        norm = float(np.sqrt(A * A + B * B + C * C))
        if norm > 0:
            nx, ny, nz = A / norm, B / norm, C / norm
            tilt = float(np.degrees(np.arccos(min(1.0, abs(nz)))))
            stats["normal_xyz"] = [round(nx, 5), round(ny, 5), round(nz, 5)]
            stats["tilt_deg_vs_axis"] = round(tilt, 3)
        if abs(C) > 1e-12:
            stats["depth_at_principal_mm"] = round(1.0 / C, 2)
    return stats


def estimate_largest_planes(depth_mm: np.ndarray, valid: np.ndarray, *,
                            n_planes: int, tol_mm: float, iterations: int,
                            sample_max: int, min_plane_ratio: float,
                            seed: int,
                            intrinsics: dict[str, float] | None
                            ) -> tuple[list[dict[str, Any]], list[np.ndarray]]:
    """检出面积最大的前 N 块平面。返回 (平面统计列表, 每平面整帧 bool 掩膜)。

    贪心移除会受「起点平面」影响（首块可能同时串到两块相邻面），因此内部固定多搜
    几块候选（至少 3 块），最后按连通域面积降序取前 N——保证 1 号就是画面里最大的面。
    """
    h, w = depth_mm.shape
    ys, xs = np.nonzero(valid)
    total_valid = int(xs.size)
    if total_valid < 50:
        return [], []
    d = depth_mm[ys, xs].astype(np.float64)
    un = xs.astype(np.float64) / float(w)
    vn = ys.astype(np.float64) / float(h)
    t = 1.0 / d
    rng = np.random.default_rng(int(seed))
    remaining = np.ones(total_valid, dtype=bool)
    planes: list[dict[str, Any]] = []
    masks: list[np.ndarray] = []
    min_pixels = max(64, int(round(min_plane_ratio * total_valid)))
    search_n = min(5, max(int(n_planes), 3))

    for _ in range(search_n):
        rem_idx = np.where(remaining)[0]
        if rem_idx.size < 3:
            break
        if rem_idx.size > sample_max:
            samp = rng.choice(rem_idx, size=sample_max, replace=False)
        else:
            samp = rem_idx
        coeff = _ransac_inverse_depth(un[samp], vn[samp], t[samp], d[samp],
                                      iterations=iterations, tol_mm=tol_mm, rng=rng)
        if coeff is None:
            break
        # 只用剩余点做精修与内点判定，避免两块平面互相污染
        coeff, resid = _refine_plane(coeff, un, vn, t, d, tol_mm)
        comp = None
        for _round in range(2):
            inliers = (np.abs(resid) <= tol_mm) & remaining
            comp = _largest_component_mask(inliers, ys, xs, (h, w))
            if comp is None or int(comp.sum()) < min_pixels:
                comp = None
                break
            # 只用这块连通域内部再拟合一次，收敛到这块面本身（去掉跨面串点的影响）
            sub = comp
            A = np.column_stack([un[sub], vn[sub], np.ones(int(sub.sum()))])
            newc, *_ = np.linalg.lstsq(A, t[sub], rcond=None)
            coeff = np.asarray(newc, dtype=np.float64)
            tp = coeff[0] * un + coeff[1] * vn + coeff[2]
            with np.errstate(divide="ignore", invalid="ignore"):
                pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
            resid = d - pd
        if comp is None:
            # 达不到最小面积：停止（不是错，是画面里没有更大的平面了）
            break
        info = _plane_stats(comp, xs, ys, d, resid, coeff, (h, w),
                            total_valid, intrinsics, len(planes) + 1)
        planes.append(info)
        full = np.zeros((h, w), dtype=bool)
        full[ys[comp], xs[comp]] = True
        masks.append(full)
        remaining &= ~comp

    # 按面积降序重排，只保留请求的 N 块；index 与预览配色同步重编号
    if len(planes) > 1:
        order = sorted(range(len(planes)), key=lambda i: planes[i]["pixels"],
                       reverse=True)
        planes = [planes[i] for i in order]
        masks = [masks[i] for i in order]
    for i, p in enumerate(planes):
        p["index"] = i + 1
    return planes[: int(n_planes)], masks[: int(n_planes)]


# ------------------------------------------------------------------ 单图分析

def analyze_image(path: str, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = load_depth_array(path)
    h, w = int(raw.shape[0]), int(raw.shape[1])
    meta_path = find_meta_path(path)
    meta = load_meta(meta_path)

    # depth_scale 优先级：命令行 > _depth.json > 默认 1e-3（1 原始值=1mm）
    scale = args.depth_scale
    scale_src = "命令行"
    if scale is None:
        scale = meta.get("depth_scale_m")
        scale_src = "JSON"
        if not scale:
            scale = 1e-3
            scale_src = "默认(1原始值=1mm)"
    scale = float(scale)

    depth_mm = raw.astype(np.float32) * scale * 1000.0
    zero_mask = raw == 0
    sat_mask = raw >= float(args.invalid_threshold)
    valid = ~(zero_mask | sat_mask)
    total = h * w
    valid_total = int(valid.sum())

    result: dict[str, Any] = {
        "image": os.path.basename(path),
        "path": os.path.abspath(path),
        "width": w, "height": h,
        "dtype": str(raw.dtype),
        "depth_scale_m": scale,
        "depth_scale_source": scale_src,
        "unit": "mm",
        "metadata_path": meta_path,
        "pixels": total,
        "valid_pixels": valid_total,
        "valid_ratio": round(valid_total / max(total, 1), 6),
        "zero_pixels": int(zero_mask.sum()),
        "saturated_pixels": int(sat_mask.sum()),
        "invalid_threshold": float(args.invalid_threshold),
        "min_plane_ratio": float(args.min_plane_ratio),
        "depth": None,
        "histogram": None,
        "intrinsics": None,
        "planes": [],
    }

    # 内参：命令行 > JSON；都不全时如实置空（不猜）
    intr = meta.get("intrinsics") if isinstance(meta.get("intrinsics"), dict) else {}
    fx = args.fx or intr.get("fx")
    fy = args.fy or intr.get("fy")
    cx = args.cx if args.cx is not None else intr.get("cx")
    cy = args.cy if args.cy is not None else intr.get("cy")
    intrinsics = None
    if fx and fy and cx is not None and cy is not None:
        intrinsics = {"fx": float(fx), "fy": float(fy),
                      "cx": float(cx), "cy": float(cy)}
        result["intrinsics"] = intrinsics

    planes: list[dict[str, Any]] = []
    plane_masks: list[np.ndarray] = []
    if valid_total > 0:
        vals = depth_mm[valid]
        ys, xs = np.nonzero(valid)
        j_min = int(np.argmin(vals))
        j_max = int(np.argmax(vals))
        pct = _percentiles(vals)

        # 中心 20%×20% 区域（以画面中心为中心）的中位数——常用来对照引导/对焦是否落在工件上
        cw, chh = max(1, w // 10), max(1, h // 10)
        x0, x1 = w // 2 - cw, w // 2 + cw
        y0, y1 = h // 2 - chh, h // 2 + chh
        center_box = valid[y0:y1, x0:x1]
        center_med = (float(np.median(depth_mm[y0:y1, x0:x1][center_box]))
                      if bool(center_box.any()) else None)

        result["depth"] = {
            "min_mm": round(float(vals[j_min]), 2),
            "min_xy": [int(xs[j_min]), int(ys[j_min])],
            "max_mm": round(float(vals[j_max]), 2),
            "max_xy": [int(xs[j_max]), int(ys[j_max])],
            "mean_mm": round(float(vals.mean()), 2),
            "median_mm": round(float(np.median(vals)), 2),
            "std_mm": round(float(vals.std()), 2),
            "percentiles_mm": {k: round(v, 2) for k, v in pct.items()},
            "center_median_mm": None if center_med is None else round(center_med, 2),
            "center_box_xyxy": [int(x0), int(y0), int(x1), int(y1)],
        }
        if not args.no_histogram:
            result["histogram"] = _histogram(vals, pct["p01"], pct["p99"])

        planes, plane_masks = estimate_largest_planes(
            depth_mm, valid,
            n_planes=args.planes, tol_mm=args.tol_mm,
            iterations=args.iterations, sample_max=args.sample,
            min_plane_ratio=args.min_plane_ratio, seed=args.seed,
            intrinsics=intrinsics,
        )
        # 平面叠加再落回整帧无效计数之外——平面统计只看有效像素
        result["planes"] = planes

    extras = {
        "depth_mm": depth_mm,
        "valid": valid,
        "plane_masks": plane_masks,
    }
    return result, extras


# ------------------------------------------------------------------ 输出

def _fmt(v: Any, nd: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def print_report(res: dict[str, Any]) -> None:
    line = "=" * 78
    print(f"\n{line}\n{res['image']}   （{res['path']}）\n{line}")
    print(f"尺寸 {res['width']}×{res['height']}  dtype {res['dtype']}  "
          f"depth_scale={res['depth_scale_m']:g} m/raw（{res['depth_scale_source']}）  单位 mm")
    print(f"像素 {res['pixels']}：有效 {res['valid_pixels']} "
          f"({res['valid_ratio'] * 100:.1f}%)，零值 {res['zero_pixels']}，"
          f"饱和/无效(≥{res['invalid_threshold']:g}) {res['saturated_pixels']}")
    d = res.get("depth")
    if not d:
        print("有效像素为 0，无法统计深度。")
        return
    print(f"最小深度 {_fmt(d['min_mm'])} @ (x,y)=({d['min_xy'][0]},{d['min_xy'][1]})"
          f"    最大深度 {_fmt(d['max_mm'])} @ ({d['max_xy'][0]},{d['max_xy'][1]})")
    print(f"平均深度 {_fmt(d['mean_mm'])}    中位数 {_fmt(d['median_mm'])}    "
          f"标准差 {_fmt(d['std_mm'])}")
    pct = d["percentiles_mm"]
    print("分位数 P01 {} / P05 {} / P25 {} / P50 {} / P75 {} / P95 {} / P99 {}".format(
        _fmt(pct["p01"]), _fmt(pct["p05"]), _fmt(pct["p25"]), _fmt(pct["p50"]),
        _fmt(pct["p75"]), _fmt(pct["p95"]), _fmt(pct["p99"])))
    if d.get("center_median_mm") is not None:
        bx = d["center_box_xyxy"]
        print(f"中心区域(像素 x{bx[0]}~{bx[2]}, y{bx[1]}~{bx[3]})深度中位数 "
              f"{_fmt(d['center_median_mm'])}")
    hist = res.get("histogram")
    if hist:
        counts = hist["counts"]
        top = max(counts) if counts else 0
        print(f"直方图 [{hist['lo_mm']:.0f}, {hist['hi_mm']:.0f}] mm "
              f"（每格 {(hist['hi_mm'] - hist['lo_mm']) / max(hist['bins'], 1):.0f} mm）：")
        for i, cnt in enumerate(counts):
            lo = hist["edges_mm"][i]
            bar = "#" * int(round(40.0 * cnt / top)) if top else ""
            print(f"  {lo:8.0f} | {cnt:>9d} {bar}")
    if res.get("intrinsics"):
        i = res["intrinsics"]
        print(f"内参 fx={i['fx']:g} fy={i['fy']:g} cx={i['cx']:g} cy={i['cy']:g}")
    planes = res.get("planes") or []
    if not planes:
        ratio = res.get("min_plane_ratio", 0.01) * 100
        print(f"平面：未检出连通域面积占比 ≥ {ratio:.1f}% 的平面。")
    for p in planes:
        bbox = p["bbox_xyxy"]
        print(f"平面 #{p['index']}（最大平面按面积排序）：{p['pixels']} px，"
              f"占有效 {p['ratio_of_valid'] * 100:.1f}%，深度 {_fmt(p['min_depth_mm'])}~"
              f"{_fmt(p['max_depth_mm'])}，平均 {_fmt(p['mean_depth_mm'])}，"
              f"RMS {_fmt(p['rms_mm'], 2)}")
        print(f"    bbox(x0,y0,x1,y1)=({bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]})，"
              f"质心 ({_fmt(p['centroid_xy'][0])},{_fmt(p['centroid_xy'][1])})，"
              f"斜率 X {p['slope_x_mm_per_100px']:+.2f} / Y {p['slope_y_mm_per_100px']:+.2f} mm/100px")
        if p.get("normal_xyz"):
            n = p["normal_xyz"]
            print(f"    法线 ({n[0]:+.4f},{n[1]:+.4f},{n[2]:+.4f})，与光轴夹角 "
                  f"{_fmt(p.get('tilt_deg_vs_axis'), 2)}°，"
                  f"主点处光轴深度 {_fmt(p.get('depth_at_principal_mm'))}")


def write_json(res: dict[str, Any], out_dir: str) -> str:
    stem = os.path.splitext(res["image"])[0]
    path = os.path.join(out_dir, f"{stem}_stats.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)
    return path


_CSV_BASE = (
    "image", "width", "height", "dtype", "valid_pixels", "valid_ratio",
    "zero_pixels", "saturated_pixels",
    "depth_min_mm", "depth_min_x", "depth_min_y", "depth_max_mm",
    "depth_mean_mm", "depth_median_mm", "depth_std_mm",
    "p01_mm", "p05_mm", "p25_mm", "p75_mm", "p95_mm", "p99_mm",
    "center_median_mm", "plane_count",
)
_CSV_PLANE = (
    "pixels", "ratio_of_valid", "mean_mm", "min_mm", "max_mm", "rms_mm",
    "bbox_x0", "bbox_y0", "bbox_x1", "bbox_y1", "tilt_deg", "depth_at_axis_mm",
)


def write_summary_csv(results: list[dict[str, Any]], path: str, n_planes: int) -> str:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        header = list(_CSV_BASE)
        for i in range(1, max(1, n_planes) + 1):
            header += [f"plane{i}_{c}" for c in _CSV_PLANE]
        writer.writerow(header)
        for res in results:
            d = res.get("depth") or {}
            pct = d.get("percentiles_mm") or {}
            row = [
                res["image"], res["width"], res["height"], res["dtype"],
                res["valid_pixels"], res["valid_ratio"],
                res["zero_pixels"], res["saturated_pixels"],
                d.get("min_mm", ""), *(d.get("min_xy") or ["", ""]),
                d.get("max_mm", ""), d.get("mean_mm", ""),
                d.get("median_mm", ""), d.get("std_mm", ""),
                pct.get("p01", ""), pct.get("p05", ""), pct.get("p25", ""),
                pct.get("p75", ""), pct.get("p95", ""), pct.get("p99", ""),
                d.get("center_median_mm", ""),
                len(res.get("planes") or []),
            ]
            planes = res.get("planes") or []
            for i in range(1, max(1, n_planes) + 1):
                if i <= len(planes):
                    p = planes[i - 1]
                    b = p["bbox_xyxy"]
                    row += [
                        p["pixels"], p["ratio_of_valid"], p["mean_depth_mm"],
                        p["min_depth_mm"], p["max_depth_mm"], p["rms_mm"],
                        b[0], b[1], b[2], b[3],
                        p.get("tilt_deg_vs_axis", ""),
                        p.get("depth_at_principal_mm", ""),
                    ]
                else:
                    row += [""] * len(_CSV_PLANE)
            writer.writerow(row)
    return path


def save_preview(path: str, res: dict[str, Any], extras: dict[str, Any],
                 args: argparse.Namespace) -> str | None:
    """伪彩深度图 + 最大平面叠加（绿=最大平面，其余按序换色），便于人工核对。"""
    if cv2 is None:
        return None
    depth_mm = extras["depth_mm"]
    valid = extras["valid"]
    masks = extras["plane_masks"]
    d = res.get("depth") or {}
    pct = (d.get("percentiles_mm") or {})
    lo = args.viz_min if args.viz_min is not None else float(pct.get("p01", 0.0))
    hi = args.viz_max if args.viz_max is not None else float(pct.get("p99", 1.0))
    if hi <= lo:
        hi = lo + 1.0
    norm = np.zeros(depth_mm.shape, dtype=np.float32)
    norm[valid] = np.clip((depth_mm[valid] - lo) / (hi - lo), 0.0, 1.0)
    img = cv2.applyColorMap((norm * 255.0).astype(np.uint8), cv2.COLORMAP_TURBO)
    img[~valid] = (0, 0, 0)

    overlay = img.copy()
    for i, mask in enumerate(masks):
        color = _PLANE_COLORS_BGR[i % len(_PLANE_COLORS_BGR)]
        overlay[mask] = color
        ys, xs = np.nonzero(mask)
        if xs.size:
            cv2.rectangle(overlay, (int(xs.min()), int(ys.min())),
                          (int(xs.max()), int(ys.max())), color, 3)
    img = cv2.addWeighted(overlay, 0.45, img, 0.55, 0)

    lines = [res["image"]]
    if d:
        lines.append(f"valid {res['valid_ratio'] * 100:.1f}%  "
                     f"min {d['min_mm']:.1f}  med {d['median_mm']:.1f}  "
                     f"mean {d['mean_mm']:.1f} mm")
    for p in res.get("planes") or []:
        lines.append(f"P{p['index']} area {p['ratio_of_valid'] * 100:.1f}%  "
                     f"depth {p['mean_depth_mm']:.1f} mm  rms {p['rms_mm']:.2f}")
    lines.append(f"viz {lo:.0f}-{hi:.0f} mm (TURBO), black=invalid")
    for n, text in enumerate(lines):
        y = 28 + 24 * n
        cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                    (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62,
                    (255, 255, 255), 1, cv2.LINE_AA)
    from application.services.frame_import import imwrite_unicode
    imwrite_unicode(path, img)
    return path


# ------------------------------------------------------------------ 入口

def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="深度图统计：最小/平均深度、分位数、无效占比、最大平面（逆深度 RANSAC）。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("inputs", nargs="+",
                    help="深度图文件 / 目录（递归） / 通配；支持 *_depth.npy 与 *_depth16.png")
    ap.add_argument("--out", default=None,
                    help="输出目录；给出时写 summary.csv、每图 *_stats.json 与 *_planes.png")
    ap.add_argument("--depth-scale", type=float, default=None,
                    help="原始值→米的换算（默认：读 _depth.json 的 depth_scale_m，缺省 1e-3=1原始值1mm）")
    ap.add_argument("--invalid-threshold", type=float, default=65535.0,
                    help="原始值 ≥ 该值视为无效（与 depth_guidance 默认一致）")
    ap.add_argument("--fx", type=float, default=None, help="内参 fx（覆盖 JSON）")
    ap.add_argument("--fy", type=float, default=None, help="内参 fy（覆盖 JSON）")
    ap.add_argument("--cx", type=float, default=None, help="内参 cx（覆盖 JSON）")
    ap.add_argument("--cy", type=float, default=None, help="内参 cy（覆盖 JSON）")
    ap.add_argument("--planes", type=int, default=1, choices=range(1, 6),
                    help="统计前 N 块平面（按面积降序；1~5）")
    ap.add_argument("--tol-mm", type=float, default=8.0,
                    help="平面内点的深度容差（mm）")
    ap.add_argument("--min-plane-ratio", type=float, default=0.01,
                    help="平面连通域至少占有效像素的比例，低于即停")
    ap.add_argument("--iterations", type=int, default=400, help="RANSAC 迭代次数")
    ap.add_argument("--sample", type=int, default=120000, help="RANSAC 每轮取样的最大点数")
    ap.add_argument("--seed", type=int, default=42, help="RANSAC 随机种子（保证可复现）")
    ap.add_argument("--no-histogram", action="store_true", help="控制台不打印直方图")
    ap.add_argument("--no-preview", action="store_true", help="不写 *_planes.png 预览")
    ap.add_argument("--viz-min", type=float, default=None,
                    help="预览伪彩下限（mm；默认取有效深度 P01）")
    ap.add_argument("--viz-max", type=float, default=None,
                    help="预览伪彩上限（mm；默认取有效深度 P99）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    files = discover_inputs(args.inputs)
    if not files:
        print("没有找到可处理的深度图（.npy / *_depth16.png）。", file=sys.stderr)
        return 2
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    t0 = time.time()
    results: list[dict[str, Any]] = []
    failed: list[tuple[str, str]] = []
    skipped: list[str] = []
    preview_warned = False
    for f in files:
        try:
            res, extras = analyze_image(f, args)
        except NonDepthError as exc:  # 目录里顺手扫到的彩色 npy 等：不算失败
            print(f"[忽略] {f}: {exc}", file=sys.stderr)
            skipped.append(f)
            continue
        except Exception as exc:  # noqa: BLE001 - 单张失败不挡其它
            print(f"[跳过] {f}: {exc}", file=sys.stderr)
            failed.append((f, str(exc)))
            continue
        print_report(res)
        results.append(res)
        if args.out:
            jp = write_json(res, args.out)
            print(f"[写出] {jp}")
            if not args.no_preview:
                stem = os.path.splitext(res["image"])[0]
                png = save_preview(os.path.join(args.out, f"{stem}_planes.png"),
                                   res, extras, args)
                if png:
                    print(f"[写出] {png}")
                elif cv2 is None and not preview_warned:
                    print("[提示] 本环境没有 OpenCV，跳过预览图（统计与 CSV/JSON 不受影响）。")
                    preview_warned = True
        extras.clear()  # 大数组不跨图累积

    if args.out and results:
        csv_path = write_summary_csv(results, os.path.join(args.out, "summary.csv"),
                                     args.planes)
        print(f"[写出] {csv_path}")

    dt = time.time() - t0
    msg = f"\n共处理 {len(results)} 张深度图，失败 {len(failed)} 张"
    if skipped:
        msg += f"，忽略非深度图 {len(skipped)} 张"
    print(f"{msg}，用时 {dt:.1f}s。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
