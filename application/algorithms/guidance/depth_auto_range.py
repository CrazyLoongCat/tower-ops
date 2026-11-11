"""找圆前自动计算深度切分范围（DEC-133）。

现场约定（用户给定公式）：
    深度下限 = 本帧最小有效深度 − ``auto_min_offset_mm``（默认 10mm）
    深度上限 = 最大平面平均深度 − ``auto_plane_offset_mm``（默认 20mm）

「最大平面」在**逆深度空间** ``t = a·u + b·v + c`` 做确定性 RANSAC：任何三维平面
投影到图像后逆深度都是像素坐标的线性函数，无需相机内参；之后全分辨率最小二乘
精修 + 最大 8 连通域过滤（保证是「一整块面」），取该连通域的平均深度。

纯 numpy+cv2，不依赖工程其它模块；供 ``run_depth_guidance`` 的
``depth_range_mode="auto"``（找圆调试页与生产视觉共用同一条链）调用。
本函数只做计算，不回写参数；失败时 ``ok=False`` 并给出原因，调用方沿用手动上下限。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

__all__ = ["resolve_auto_depth_range"]

_RECT_MIN_PIXELS = 64
_SCORE_CHUNK = 16


def _score_chunk(coeffs: np.ndarray, su: np.ndarray, sv: np.ndarray,
                 inv_lo: np.ndarray, inv_hi: np.ndarray) -> np.ndarray:
    """一批平面系数对样本点的深度内点数。coeffs:(k,3) → (k,)。

    DEC-136：不再对 (k, n) 做 1/tp 除法与 abs，改为比较逆深度区间
    ``[1/(d+tol), 1/(d-tol)]``（``inv_lo``/``inv_hi`` 预先算好、float32），
    内点判断数学等价，实测大幅减少大矩阵运算时间。
    """
    c = coeffs.astype(np.float32, copy=False)
    tp = (c[:, 0, None] * su[None, :]
          + c[:, 1, None] * sv[None, :]
          + c[:, 2, None])
    good = (tp > 1e-12) & (tp >= inv_lo[None, :]) & (tp <= inv_hi[None, :])
    return good.sum(axis=1)


def _ransac_plane(su: np.ndarray, sv: np.ndarray, st: np.ndarray,
                  sd: np.ndarray, *, iterations: int, tol_mm: float,
                  rng: np.random.Generator) -> Optional[np.ndarray]:
    """逆深度空间三点最小样本 RANSAC（向量化分批），返回最好平面系数 (a,b,c)。

    DEC-136：内点判断改逆深度区间比较（无除法）；找到占比 >60% 的平面即提前结束。
    """
    n = su.size
    if n < 3:
        return None
    tol = float(tol_mm)
    safe = sd > tol
    inv_lo = np.zeros(n, dtype=np.float32)
    inv_hi = np.full(n, np.inf, dtype=np.float32)
    inv_lo[safe] = (1.0 / (sd[safe] + tol)).astype(np.float32)
    inv_hi[safe] = (1.0 / (sd[safe] - tol)).astype(np.float32)
    best: Optional[np.ndarray] = None
    best_count = -1
    batch = 64
    done = 0
    while done < iterations:
        k = min(batch, iterations - done)
        idx = rng.integers(0, n, size=(k, 3))
        valid = ~((idx[:, 0] == idx[:, 1]) | (idx[:, 0] == idx[:, 2])
                  | (idx[:, 1] == idx[:, 2]))
        cols_u, cols_v = su[idx], sv[idx]
        A = np.stack([cols_u, cols_v, np.ones_like(cols_u)], axis=2)
        b = st[idx]
        det = np.linalg.det(A)
        ok = valid & (np.abs(det) > 1e-12)
        coeffs = np.zeros((k, 3), dtype=np.float64)
        if ok.any():
            # b 以 (n,3,1) 传入，numpy 1.x / 2.x 行为一致
            coeffs[ok] = np.linalg.solve(A[ok], b[ok][..., None])[..., 0]
        for s in range(0, k, _SCORE_CHUNK):
            cc, okk = coeffs[s:s + _SCORE_CHUNK], ok[s:s + _SCORE_CHUNK]
            if not okk.any():
                continue
            counts = _score_chunk(cc, su, sv, inv_lo, inv_hi).astype(np.int64)
            counts[~okk] = -1
            j = int(np.argmax(counts))
            if counts[j] > best_count:
                best_count = int(counts[j])
                best = cc[j].copy()
        done += k
        if best_count >= 0.6 * n:      # 已找到占主导的平面，提前结束
            break
    return best


def _refine(coeff: np.ndarray, su: np.ndarray, sv: np.ndarray,
            st: np.ndarray, sd: np.ndarray, tol_mm: float,
            iters: int = 3) -> Tuple[np.ndarray, np.ndarray]:
    """在样本点上最小二乘精修，返回 (系数, 每点深度残差mm)。"""
    for _ in range(iters):
        tp = coeff[0] * su + coeff[1] * sv + coeff[2]
        with np.errstate(divide="ignore", invalid="ignore"):
            pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
        m = (tp > 1e-12) & (np.abs(pd - sd) <= tol_mm)
        if int(m.sum()) < 3:
            break
        A = np.column_stack([su[m], sv[m], np.ones(int(m.sum()))])
        new, *_ = np.linalg.lstsq(A, st[m], rcond=None)
        coeff = np.asarray(new, dtype=np.float64)
    tp = coeff[0] * su + coeff[1] * sv + coeff[2]
    with np.errstate(divide="ignore", invalid="ignore"):
        pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
    return coeff, sd - pd


def resolve_auto_depth_range(
        depth_raw: np.ndarray,
        depth_scale: float, *,
        min_offset_mm: float = 10.0,
        plane_offset_mm: float = 20.0,
        tol_mm: float = 8.0,
        invalid_raw_threshold: float = 65535.0,
        iterations: int = 200,
        sample_max: int = 15000,
        max_planes: int = 3,
        min_plane_ratio: float = 0.005,
        seed: int = 42) -> Dict[str, Any]:
    """自动计算深度切分上下限（DEC-133）。

    Returns:
        dict:
            - ok: 是否成功
            - reason: 失败原因（ok=True 时为空串）
            - min_mm / max_mm: 计算出的切分上下限（失败时 None）
            - raw_min_mm: 本帧最小有效深度
            - plane_mean_mm: 最大平面平均深度（失败时 None）
            - plane_pixels / plane_ratio: 最大平面像素数/占有效像素比例
            - clamped: max_mm 是否因不超过 min_mm 被抬到下限之上
    """
    arr = np.asarray(depth_raw)
    if arr.ndim != 2:
        return {"ok": False, "reason": f"不是 2D 深度数组（shape={arr.shape}）",
                "min_mm": None, "max_mm": None, "raw_min_mm": None,
                "plane_mean_mm": None, "plane_pixels": 0, "plane_ratio": 0.0,
                "clamped": False}
    valid = (arr > 0) & (arr < float(invalid_raw_threshold))
    ys, xs = np.nonzero(valid)
    total = int(xs.size)
    if total == 0:
        return {"ok": False, "reason": "本帧没有有效深度像素",
                "min_mm": None, "max_mm": None, "raw_min_mm": None,
                "plane_mean_mm": None, "plane_pixels": 0, "plane_ratio": 0.0,
                "clamped": False}

    mm2 = arr.astype(np.float32) * float(depth_scale) * 1000.0
    d_mm = mm2[ys, xs]
    raw_min = float(d_mm.min())
    lower = raw_min - max(0.0, float(min_offset_mm))

    h, w = int(arr.shape[0]), int(arr.shape[1])
    rng = np.random.default_rng(int(seed))
    # 贪心多候选：单次 RANSAC 可能先抓到一块「部分面」（如被分割的窄带），
    # 剔除后再找下一块，最后取面积最大的（与离线脚本 depth_stats 同一教训）。
    remaining = np.ones(total, dtype=bool)
    best: Optional[Dict[str, Any]] = None
    for _ in range(max(1, int(max_planes))):
        rem_idx = np.where(remaining)[0]
        if rem_idx.size < 32:
            break
        if rem_idx.size > sample_max:
            samp = rng.choice(rem_idx, size=int(sample_max), replace=False)
        else:
            samp = rem_idx
        su = (xs[samp].astype(np.float32) / float(w))
        sv = (ys[samp].astype(np.float32) / float(h))
        sd = d_mm[samp].astype(np.float32, copy=False)
        st = 1.0 / sd
        coeff = _ransac_plane(su, sv, st, sd, iterations=int(iterations),
                              tol_mm=float(tol_mm), rng=rng)
        if coeff is None:
            break
        # 抽样点精修后，把平面回代到全部剩余有效像素——
        # 连通域必须在全分辨率上算，否则抽样点铺回全图连不成整块。
        coeff, _resid = _refine(coeff, su, sv, st, sd, float(tol_mm))
        sel = rem_idx
        un = xs[sel].astype(np.float64) / float(w)
        vn = ys[sel].astype(np.float64) / float(h)
        tp = coeff[0] * un + coeff[1] * vn + coeff[2]
        dd = d_mm[sel].astype(np.float64)
        safe = dd > float(tol_mm)
        inl = np.zeros(sel.size, dtype=bool)
        if safe.any():
            lo = 1.0 / (dd[safe] + float(tol_mm))
            hi = 1.0 / (dd[safe] - float(tol_mm))
            inl[safe] = ((tp[safe] > 1e-12) & (tp[safe] >= lo) & (tp[safe] <= hi))
        full = np.zeros((h, w), dtype=np.uint8)
        full[ys[sel][inl], xs[sel][inl]] = 1
        ncomp, labels, stats, _ = cv2.connectedComponentsWithStats(
            full, connectivity=8)
        if ncomp <= 1:
            break
        j = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        px = int(stats[j, cv2.CC_STAT_AREA])
        if px < _RECT_MIN_PIXELS:
            break
        comp2d = labels == j
        plane_mean = float(mm2[comp2d].mean())
        if best is None or px > best["plane_pixels"]:
            best = {"plane_pixels": px, "plane_mean_mm": plane_mean}
        # 只剔除该连通域（不是全部内点），让下一轮去找别的面
        remaining[sel] &= ~comp2d[ys[sel], xs[sel]]

    if best is None:
        return {"ok": False, "reason": "未检出平面（RANSAC 失败或无连通域）",
                "min_mm": None, "max_mm": None, "raw_min_mm": raw_min,
                "plane_mean_mm": None, "plane_pixels": 0, "plane_ratio": 0.0,
                "clamped": False}
    px = int(best["plane_pixels"])
    plane_mean = float(best["plane_mean_mm"])
    ratio = px / float(total)
    if px < max(_RECT_MIN_PIXELS, int(min_plane_ratio * total)):
        return {"ok": False,
                "reason": f"最大平面连通域占比 {ratio:.1%} 低于 {min_plane_ratio:.1%}",
                "min_mm": None, "max_mm": None, "raw_min_mm": raw_min,
                "plane_mean_mm": plane_mean, "plane_pixels": px,
                "plane_ratio": ratio, "clamped": False}

    upper = plane_mean - max(0.0, float(plane_offset_mm))
    clamped = False
    if upper <= lower:            # 平面离最近点太近：抬到下限之上，避免空区间
        upper = lower + 1.0
        clamped = True
    return {"ok": True, "reason": "",
            "min_mm": round(lower, 2), "max_mm": round(upper, 2),
            "raw_min_mm": round(raw_min, 2),
            "plane_mean_mm": round(plane_mean, 2),
            "plane_pixels": px, "plane_ratio": round(ratio, 4),
            "min_offset_mm": float(min_offset_mm),
            "plane_offset_mm": float(plane_offset_mm),
            "clamped": clamped}
