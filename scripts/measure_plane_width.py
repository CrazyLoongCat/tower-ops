#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用深度四件套测「两条平行棱线（轮廓边）之间的距离」，即一块可见面的宽度。

场景：D405/DS86 保存的 `<名>_depth.npy` + `<名>_depth.json`（DEC-123 四件套），
量取画面里柱面等平面体的左右两条轮廓棱边之间的三维距离（现场常在手画线处量）。

做法：
  1. 有效深度（剔除 0/65535），逆深度空间 `t = a·u + b·v + c` 做确定性 RANSAC
     找**最大平面**（平面投影后逆深度是像素坐标线性函数，无需内参即可找平面），
     全分辨率回代精修 + 8 连通域取整块面，再做小开运算去掉毛刺；
  2. 用掩膜像素的协方差判断棱边方向（近竖直→按行扫描左右边界；近水平→按列扫描）；
  3. 每条扫描线的两端点只有**外侧确实是背景/无效**（深度跳变到更远或无效）才计为
     轮廓棱边 —— 缝线、共面钢架、近处遮挡边界一律不算；
  4. 两侧都有效的扫描线上，同高度左右两点转三维（相机系 mm）求欧氏距离，
     统计中位数/均值/IQR；拟合两条 3D 直线报告夹角，平面法线报告倾角；
  5. 可选在彩色图上把检测到的两条棱边画出来，人工核对。

用法（tower-ops 目录）：
    python scripts/measure_plane_width.py "F:/配置及数据/runtime/runtime/captures/*_depth.npy" \
        --out runtime/plane_width --viz
    python scripts/measure_plane_width.py x_depth.npy --tol-mm 8

边界：纯离线读文件；只测「最大平面」一块面，画面最大平面不是目标面时先换帧
或加 --min-depth/--max-depth 限定。倾角大、面被画面截断或大面积遮挡的帧会被
质量门限过滤（详见输出 CSV 的 span/rows 列）。
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------- 读取四件套

def load_capture(npy_path: str) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    """读 <名>_depth.npy + <名>_depth.json → (原始深度, 毫米深度, 元数据)。"""
    meta_path = npy_path[: -len("_depth.npy")] + "_depth.json"
    with open(meta_path, encoding="utf-8") as fh:
        meta = json.load(fh)
    raw = np.load(npy_path).astype(np.uint16)
    scale = float(meta.get("depth_scale_m") or 1e-3)
    depth_mm = raw.astype(np.float32) * scale * 1000.0
    return raw, depth_mm, meta


def _depth_scale(meta: Dict[str, Any]) -> float:
    return float(meta.get("depth_scale_m") or 1e-3)


def _intrinsics(meta: Dict[str, Any]) -> Optional[Dict[str, float]]:
    intr = meta.get("intrinsics") or {}
    try:
        return {"fx": float(intr["fx"]), "fy": float(intr["fy"]),
                "cx": float(intr["cx"]), "cy": float(intr["cy"])}
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 算法（应用层共用，DEC-144）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from application.algorithms.guidance.plane_width import (  # noqa: E402
    fit_largest_plane,
    measure_edge_distance,
    measure_plane_width,
)


def draw_overlay(color_bgr: np.ndarray, info: Dict[str, Any],
                 intr: Dict[str, float], title: str) -> np.ndarray:
    """在彩色图上画检测到的左右轮廓点（红/绿）与宽度文字。"""
    img = color_bgr.copy()
    for pl, pr in info["pairs"][:: max(1, len(info["pairs"]) // 60)]:
        for p, col in ((pl, (0, 0, 255)), (pr, (0, 255, 0))):
            z = max(float(p[2]), 1.0)
            u = int(round(intr["fx"] * p[0] / z + intr["cx"]))
            v = int(round(intr["fy"] * p[1] / z + intr["cy"]))
            cv2.circle(img, (u, v), 3, col, -1)
    lines = [title,
             f"width {info['width_mm']:.1f} mm  (mean {info['width_mean_mm']:.1f}, "
             f"IQR {info['width_iqr_mm']:.1f})",
             f"tilt {info['tilt_deg']:.1f} deg  depth {info['face_depth_mm']:.0f} mm  "
             f"lines {info['scan_lines']} ({info['scan_mode']})"]
    for i, t in enumerate(lines):
        y = 26 + 24 * i
        cv2.putText(img, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (0, 0, 0), 4)
        cv2.putText(img, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 0), 2)
    return img


# ---------------------------------------------------------------- 批处理

def analyze_one(npy_path: str, args) -> Optional[Dict[str, Any]]:
    raw, depth_mm, meta = load_capture(npy_path)
    intr = _intrinsics(meta)
    if intr is None:
        print(f"[跳过] {npy_path}: 缺内参", file=sys.stderr)
        return None
    info = measure_plane_width(
        raw, _depth_scale(meta), intr, tol_mm=args.tol_mm, iterations=args.iterations,
        open_kernel=args.open_kernel, line_thr_mm=args.line_thr_mm,
        scan=args.scan, depth_min_mm=args.min_depth,
        depth_max_mm=args.max_depth,
        min_width_mm=args.min_width, max_width_mm=args.max_width,
        seed=args.seed)
    if not info.get("ok"):
        print(f"[跳过] {npy_path}: {info.get('reason', '测量失败')}",
              file=sys.stderr)
        return None
    info.update({"image": os.path.basename(npy_path).replace("_depth.npy", ""),
                 "path": os.path.abspath(npy_path)})
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="两条轮廓棱线的三维间距（面宽）测量。")
    ap.add_argument("inputs", nargs="+", help="*_depth.npy 文件 / 通配 / 目录")
    ap.add_argument("--out", default=None, help="输出目录（CSV/标注图）")
    ap.add_argument("--viz", action="store_true", help="写标注图")
    ap.add_argument("--tol-mm", type=float, default=8.0, help="平面内点深度容差(mm)")
    ap.add_argument("--open-kernel", type=int, default=5, help="掩膜开运算核(奇数，3=轻)")
    ap.add_argument("--line-thr-mm", type=float, default=3.0, help="棱边直线内点容差(mm)")
    ap.add_argument("--scan", default="auto", choices=("auto", "row", "col"),
                    help="扫描方向：auto=按长轴；row=按行；col=按列")
    ap.add_argument("--iterations", type=int, default=300)
    ap.add_argument("--min-depth", type=float, default=0.0)
    ap.add_argument("--max-depth", type=float, default=0.0)
    ap.add_argument("--min-width", type=float, default=5.0, help="量程下限(mm)")
    ap.add_argument("--max-width", type=float, default=3000.0, help="量程上限(mm)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    files = []
    for p in args.inputs:
        hits = glob.glob(p)
        if not hits:
            hits = [p]
        for h in hits:
            if os.path.isdir(h):
                files += sorted(glob.glob(os.path.join(h, "*_depth.npy")))
            elif h.endswith("_depth.npy"):
                files.append(h)
    files = list(dict.fromkeys(files))
    if not files:
        print("没有找到 *_depth.npy", file=sys.stderr)
        return 2
    if args.out:
        os.makedirs(args.out, exist_ok=True)

    rows = []
    for f in files:
        try:
            info = analyze_one(f, args)
        except Exception as exc:  # noqa: BLE001 - 单帧失败不影响其它
            print(f"[跳过] {f}: {exc}", file=sys.stderr)
            continue
        if info is None:
            continue
        rows.append(info)
        print(f"{info['image']}: width={info['width_mm']:.1f}mm "
              f"(mean {info['width_mean_mm']:.1f}, IQR {info['width_iqr_mm']:.1f}) "
              f"tilt={info['tilt_deg']:.1f}° depth={info['face_depth_mm']:.0f}mm "
              f"lines={info['scan_lines']}")
        if args.out and args.viz:
            color_path = f[: -len("_depth.npy")] + ".png"
            color = cv2.imread(color_path)
            if color is None:
                arr = np.fromfile(color_path, np.uint8)
                color = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            img = draw_overlay(color, info, _intrinsics(load_capture(f)[2]),
                               info["image"])
            cv2.imwrite(os.path.join(args.out, info["image"] + "_width.png"), img)

    if rows:
        widths = np.array([r["width_mm"] for r in rows])
        print(f"\n共 {len(rows)} 帧：宽度中位数 {np.median(widths):.1f}mm，"
              f"P25–P75 {np.percentile(widths, 25):.1f}–"
              f"{np.percentile(widths, 75):.1f}mm")
        if args.out:
            csv_path = os.path.join(args.out, "plane_width.csv")
            cols = ("image", "width_mm", "width_mean_mm", "width_iqr_mm",
                    "width_min_mm", "width_max_mm", "scan_lines", "scan_mode", "line_angle_deg",
                    "tilt_deg", "face_depth_mm", "pixels", "ratio_of_valid", "path")
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
                wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                wr.writeheader()
                for r in rows:
                    wr.writerow({k: r.get(k) for k in cols})
            print(f"写出 {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
