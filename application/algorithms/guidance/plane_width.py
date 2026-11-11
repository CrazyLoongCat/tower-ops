"""深度图上量「两条平行棱线（轮廓边）之间的距离」（DEC-143/144）。

从现场脚本（scripts/measure_plane_width.py）提取到应用层，供视觉调试页
「棱线测量」页签与脚本共用同一实现（DEC-144 集成，参数在界面可调）：

  1. 有效深度 → 逆深度空间 `t = a·u + b·v + c` RANSAC 最大平面（无需内参）
     + 全分辨率回代 + 8 连通域 + 小开运算去毛刺；
  2. 逐行取掩膜两端点（长轴近水平自动按列扫；也可强制 row/col）；
  3. 两侧边界分别 RANSAC 3D 直线拟合，两侧同为内点的行求同高度两端点三维距离；
  4. 输出宽度中位/均值/IQR、平面法线倾角、面平均深度，以及用于叠加的左右端点对。

纯 numpy+cv2，无 UI 依赖。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------- 平面检测

def _score_chunk(coeffs, su, sv, sd, tol):
    tp = (coeffs[:, 0, None] * su[None, :] + coeffs[:, 1, None] * sv[None, :]
          + coeffs[:, 2, None])
    with np.errstate(divide="ignore", invalid="ignore"):
        pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
    return ((tp > 1e-12) & (np.abs(pd - sd[None, :]) <= tol)).sum(axis=1)


def _ransac_plane(su, sv, st, sd, *, iterations, tol, rng):
    n = su.size
    if n < 3:
        return None
    best, best_cnt = None, -1
    batch, done = 64, 0
    while done < iterations:
        k = min(batch, iterations - done)
        idx = rng.integers(0, n, size=(k, 3))
        valid = ~((idx[:, 0] == idx[:, 1]) | (idx[:, 0] == idx[:, 2])
                  | (idx[:, 1] == idx[:, 2]))
        cu, cv = su[idx], sv[idx]
        A = np.stack([cu, cv, np.ones_like(cu)], axis=2)
        det = np.linalg.det(A)
        ok = valid & (np.abs(det) > 1e-12)
        coeffs = np.zeros((k, 3))
        if ok.any():
            coeffs[ok] = np.linalg.solve(A[ok], st[idx][ok][..., None])[..., 0]
        for s in range(0, k, 16):
            cc, okk = coeffs[s:s + 16], ok[s:s + 16]
            if not okk.any():
                continue
            cnt = _score_chunk(cc, su, sv, sd, tol).astype(np.int64)
            cnt[~okk] = -1
            j = int(np.argmax(cnt))
            if cnt[j] > best_cnt:
                best_cnt, best = int(cnt[j]), cc[j].copy()
        done += k
    return best


def _refine(coeff, su, sv, st, sd, tol, iters=3):
    for _ in range(iters):
        tp = coeff[0] * su + coeff[1] * sv + coeff[2]
        with np.errstate(divide="ignore", invalid="ignore"):
            pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
        m = (tp > 1e-12) & (np.abs(pd - sd) <= tol)
        if int(m.sum()) < 3:
            break
        A = np.column_stack([su[m], sv[m], np.ones(int(m.sum()))])
        new, *_ = np.linalg.lstsq(A, st[m], rcond=None)
        coeff = np.asarray(new, dtype=np.float64)
    tp = coeff[0] * su + coeff[1] * sv + coeff[2]
    with np.errstate(divide="ignore", invalid="ignore"):
        pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
    return coeff, sd - pd


def fit_largest_plane(depth_mm: np.ndarray, valid: np.ndarray, *,
                      tol_mm: float = 8.0, iterations: int = 300,
                      sample_max: int = 20000, open_kernel: int = 5,
                      seed: int = 42,
                      ) -> Optional[Dict[str, Any]]:
    """逆深度 RANSAC 找最大平面（整块连通面）。返回 mask/法线等，失败 None。"""
    h, w = depth_mm.shape[:2]
    ys, xs = np.nonzero(valid)
    total = xs.size
    if total < 100:
        return None
    d = depth_mm[ys, xs].astype(np.float64)
    un = xs.astype(np.float64) / w
    vn = ys.astype(np.float64) / h
    t = 1.0 / d
    rng = np.random.default_rng(seed)
    if total > sample_max:
        samp = rng.choice(total, size=sample_max, replace=False)
    else:
        samp = np.arange(total)
    coeff = _ransac_plane(un[samp], vn[samp], t[samp], d[samp],
                          iterations=iterations, tol=tol_mm, rng=rng)
    if coeff is None:
        return None
    coeff, _ = _refine(coeff, un[samp], vn[samp], t[samp], d[samp], tol_mm)
    tp = coeff[0] * un + coeff[1] * vn + coeff[2]
    with np.errstate(divide="ignore", invalid="ignore"):
        pd = 1.0 / np.where(tp > 1e-12, tp, -1.0)
    inl = (tp > 1e-12) & (np.abs(d - pd) <= tol_mm)

    full = np.zeros((h, w), np.uint8)
    full[ys[inl], xs[inl]] = 1
    ncomp, labels, stats, _ = cv2.connectedComponentsWithStats(full, connectivity=8)
    if ncomp <= 1:
        return None
    j = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    mask = (labels == j).astype(np.uint8)
    if int(mask.sum()) < 500:
        return None
    # 小开运算去掉细毛刺（核大小可调，最小 3、奇数，尽量不影响棱边位置）
    k = max(3, int(open_kernel))
    if k % 2 == 0:
        k += 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    if int(mask.sum()) < 500:
        return None

    return {
        "mask": mask, "pixels": int(mask.sum()), "valid_total": int(total),
        "ratio_of_valid": float(mask.sum()) / total,
    }


# ---------------------------------------------------------------- 棱线间距

def _row_or_col_scan(mask: np.ndarray) -> str:
    """按掩膜像素协方差判断长轴方向：长轴近竖直→按行扫左右；
    长轴近水平（画面旋转过）→按列扫上下。返回 "row" / "col"。"""
    ys, xs = np.nonzero(mask)
    xs_c = xs - xs.mean()
    ys_c = ys - ys.mean()
    sxx = float((xs_c * xs_c).mean())
    syy = float((ys_c * ys_c).mean())
    return "row" if syy >= sxx else "col"


def _ransac_line3d(P: np.ndarray, *, thr_mm: float = 3.0, iterations: int = 1200,
                   seed: int = 1) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RANSAC 三维直线：返回 (线上点 c, 单位方向 d, 每点是否为内点的bool数组)。"""
    rng = np.random.default_rng(seed)
    n = P.shape[0]
    if n < 3:
        return P.mean(axis=0), np.array([0.0, 1.0, 0.0]), np.zeros(n, bool)
    best_c, best_d, best_cnt = None, None, -1
    for _ in range(int(iterations)):
        i, j = rng.integers(0, n, 2)
        if i == j:
            continue
        v = P[j] - P[i]
        nd = float(np.linalg.norm(v))
        if nd < 30.0:
            continue
        d = v / nd
        w = P - P[i]
        dist = np.linalg.norm(w - np.outer(w @ d, d), axis=1)
        cnt = int((dist < thr_mm).sum())
        if cnt > best_cnt:
            best_cnt, best_c, best_d = cnt, P[i].copy(), d.copy()
    if best_c is None:
        return P.mean(axis=0), np.array([0.0, 1.0, 0.0]), np.zeros(n, bool)
    c, d = best_c, best_d
    for _ in range(4):    # 内点 PCA 精修
        w = P - c
        dist = np.linalg.norm(w - np.outer(w @ d, d), axis=1)
        m = dist < thr_mm
        if int(m.sum()) < 3:
            break
        Q = P[m]
        c = Q.mean(axis=0)
        _, _, vt = np.linalg.svd(Q - c, full_matrices=False)
        d = vt[0] / max(np.linalg.norm(vt[0]), 1e-12)
    w = P - c
    dist = np.linalg.norm(w - np.outer(w @ d, d), axis=1)
    return c, d, dist < thr_mm


def measure_edge_distance(plane: Dict[str, Any], depth_mm: np.ndarray,
                          valid: np.ndarray, intr: Dict[str, float], *,
                          min_span: int = 60, line_thr_mm: float = 3.0,
                          scan: str = "auto") -> Optional[Dict[str, Any]]:
    """逐扫描线取掩膜两端点 → 两侧各拟合 3D 直线 → 两侧同为内点的行求 3D 距离。

    ``scan``：auto（按掩膜长轴自动）/ row（按行扫左右）/ col（按列扫上下）。
    """
    mask = plane["mask"]
    depth_f = depth_mm.astype(np.float64)

    def p3(u, v):
        z = float(depth_f[v, u])
        return np.array([(u - intr["cx"]) / intr["fx"] * z,
                         (v - intr["cy"]) / intr["fy"] * z, z])

    # 画面可能整体旋转：棱边接近水平时转置，统一按「行」扫
    mode = scan if scan in ("row", "col") else _row_or_col_scan(mask)
    if mode == "col":
        mask_t = mask.T
    else:
        mask_t = mask
    hh = mask_t.shape[0]

    rows_l, rows_r, pL, pR = [], [], [], []
    for v in range(hh):
        us = np.where(mask_t[v] > 0)[0]
        if us.size < min_span:
            continue
        l, r = int(us.min()), int(us.max())
        if r - l < min_span:
            continue
        if mode == "col":            # 转置坐标：行v=原列，l/r=原行
            pl, pr = p3(v, l), p3(v, r)
        else:
            pl, pr = p3(l, v), p3(r, v)
        pL.append(pl)
        pR.append(pr)
        rows_l.append(v)
    if len(pL) < 30:
        return None
    pL = np.asarray(pL)
    pR = np.asarray(pR)

    thr = max(0.5, float(line_thr_mm))
    c_l, d_l, in_l = _ransac_line3d(pL, thr_mm=thr)
    c_r, d_r, in_r = _ransac_line3d(pR, thr_mm=thr, seed=2)
    both = in_l & in_r
    if int(both.sum()) < 15:
        # 两侧边界不是直线（面被裁断/大面积遮挡），退回全部行＋更宽的内点
        c_l, d_l, in_l = _ransac_line3d(pL, thr_mm=thr * 2.0)
        c_r, d_r, in_r = _ransac_line3d(pR, thr_mm=thr * 2.0, seed=2)
        both = in_l & in_r
        if int(both.sum()) < 10:
            return None

    wa = np.linalg.norm(pR[both] - pL[both], axis=1)
    lo, hi = np.percentile(wa, [10, 90])
    core = wa[(wa >= lo) & (wa <= hi)]
    angle = float(np.degrees(np.arccos(min(1.0, abs(float(d_l @ d_r))))))

    # 平面法线/倾角：掩膜内三维点 PCA，最小奇异向量为法线
    ys_m, xs_m = np.nonzero(mask)
    Pm = np.stack([(xs_m - intr["cx"]) / intr["fx"] * depth_f[ys_m, xs_m],
                   (ys_m - intr["cy"]) / intr["fy"] * depth_f[ys_m, xs_m],
                   depth_f[ys_m, xs_m]], axis=1)
    _, _, vt = np.linalg.svd(Pm - Pm.mean(axis=0), full_matrices=False)
    n = vt[2] / max(np.linalg.norm(vt[2]), 1e-12)
    tilt = float(np.degrees(np.arccos(min(1.0, abs(n[2])))))

    d_z = depth_f[mask > 0]
    sel = np.where(both)[0]
    pairs = [(pL[i], pR[i]) for i in sel[:: max(1, len(sel) // 80)]]
    return {
        "width_mm": float(np.median(wa)),
        "width_mean_mm": float(core.mean()),
        "width_iqr_mm": float(np.percentile(wa, 75) - np.percentile(wa, 25)),
        "width_min_mm": float(wa.min()),
        "width_max_mm": float(wa.max()),
        "scan_lines": int(both.sum()),
        "scan_mode": mode,
        "line_angle_deg": angle,
        "tilt_deg": tilt,
        "face_depth_mm": float(np.median(d_z)),
        "pixels": plane["pixels"],
        "ratio_of_valid": plane["ratio_of_valid"],
        "pairs": pairs,
    }




def measure_plane_width(depth_raw: np.ndarray, depth_scale: float,
                        intrinsics: Dict[str, float], *,
                        tol_mm: float = 8.0, iterations: int = 300,
                        open_kernel: int = 5, line_thr_mm: float = 3.0,
                        min_span: int = 60, scan: str = "auto",
                        depth_min_mm: float = 0.0, depth_max_mm: float = 0.0,
                        min_width_mm: float = 5.0,
                        max_width_mm: float = 3000.0,
                        seed: int = 42) -> Dict[str, Any]:
    """一体化入口：原始深度 → 最大平面 → 两棱线间距（面宽）。

    Returns:
        dict：ok/reason + width_mm/width_mean_mm/width_iqr_mm/width_min_mm/
        width_max_mm/scan_lines/scan_mode/line_angle_deg/tilt_deg/face_depth_mm/
        pixels/ratio_of_valid + ``pairs``（左/右端点三维坐标对，供叠加显示）；
        ``mask``（整幅 bool 掩膜）仅供调试画图，调用方可忽略。
    """
    raw = np.asarray(depth_raw)
    if raw.ndim != 2:
        return {"ok": False, "reason": f"不是 2D 深度数组（shape={raw.shape}）"}
    depth_mm = raw.astype(np.float32) * float(depth_scale) * 1000.0
    valid = (raw > 0) & (raw < 65535)
    if depth_min_mm > 0:
        valid &= depth_mm >= depth_min_mm
    if depth_max_mm > 0:
        valid &= depth_mm <= depth_max_mm
    plane = fit_largest_plane(depth_mm, valid, tol_mm=tol_mm,
                              iterations=iterations, open_kernel=open_kernel,
                              seed=seed)
    if plane is None:
        return {"ok": False, "reason": "未检出平面（有效深度不足或平面占比过小）"}
    info = measure_edge_distance(plane, depth_mm, valid, intrinsics,
                                 min_span=min_span, line_thr_mm=line_thr_mm,
                                 scan=scan)
    if info is None:
        return {"ok": False, "reason": "有效扫描线不足（面被画面截断或大面积遮挡）"}
    if not (min_width_mm <= info["width_mm"] <= max_width_mm):
        return {"ok": False,
                "reason": f"宽度 {info['width_mm']:.1f}mm 超出量程 "
                          f"{min_width_mm:g}–{max_width_mm:g}mm",
                **info}
    return {"ok": True, "reason": "", **info, "mask": plane["mask"]}
