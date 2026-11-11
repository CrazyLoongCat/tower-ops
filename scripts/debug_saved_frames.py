#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线回放本地抓帧：DS86 找圆/定位调参 + 视觉分割/运行一次回放（DEC-125）。

支持两种输入形态（自动配对）：
  1. DEC-123 保存的一套：`<名>.png`（彩色）+ `<名>_depth.npy` + `<名>_depth16.png`
     + `<名>_depth.json`（元数据：depth_scale / 内参）；
  2. 运行记录帧：`<相机>-fN_color.npy` + `<相机>-fN_depth.npy`；
  3. 只有彩色图：只做展示/分割（无深度时跳过深度引导）。

对每帧执行（与界面/生产同一条链）：
  深度引导（深度切分→二值化→形态学→轮廓→正外接矩形）
  → 每个矩形找圆（OpenCV 后端或 `--circle-host` 指定的 cp38 宿主）
  → 圆心 XYZ → 粗/精偏移（用 `configs/motion/guidance.yaml` 的映射/tcp）
  →（可选）按 `--detection`/`--scheme` 回放「运行一次」视觉分割
输出：标注 PNG（原图坐标；DS86 默认再按显示旋转 270° 出图）、每帧 JSON、
汇总 CSV；参数可用 `--set depth.xxx=...` / `--set circle.xxx=...` 覆盖做对比。

用法（tower-ops 目录）：
    python scripts/debug_saved_frames.py --input runtime/frames --out runtime/debug_replay
    python scripts/debug_saved_frames.py --input D:/saved --out D:/debug \
        --set depth.depth_min_mm=120 --set depth.depth_max_mm=400 \
        --set circle.min_radius=8 --detection 圆孔检测-1
    python scripts/debug_saved_frames.py --input x.png --fx 600 --fy 600 --cx 800 --cy 600
    python scripts/debug_saved_frames.py --input D:/saved --circle-host dist_host/circle38_host/circle38_host.exe

边界：纯离线读图，不连相机、不动机械臂；`--real-segmenter` 才会尝试真实 EdgeSAM，
缺 onnxruntime/模型时如实标注并回落占位分割器（结果不是真实模型）。
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import cv2  # noqa: E402

from application.algorithms.circle.params import CircleParams  # noqa: E402
from application.algorithms.guidance.depth_guidance import (  # noqa: E402
    DepthGuidanceParams, depth_to_mm, point_depth_xyz,
)
from application.algorithms.guidance.offsets import compute_robot_offsets  # noqa: E402
from application.algorithms.segmentation.edge_sam import default_segmenter  # noqa: E402
from application.algorithms.segmentation.scheme import (  # noqa: E402
    coerce_scheme, run_scheme,
)
from application.contracts.enums import ImageSpace  # noqa: E402
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics  # noqa: E402
from application.hosts.circle38.opencv_backend import OpenCVCircleBackend  # noqa: E402
from application.services.guidance_config import GuidanceConfigStore  # noqa: E402
from application.services.guidance_debug import (  # noqa: E402
    depth_guidance_stage, find_circles_in_rects,
)
from application.services.guidance_service import intrinsics_to_dict  # noqa: E402
from application.services.vision_detections import VisionDetectionStore  # noqa: E402

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")
# ---------------------------------------------------------------- 输入配对（DEC-126 共用服务）
from application.services.frame_import import (  # noqa: E402
    ImportedFrame as SavedFrame,
    discover_frames as _discover_frames,
    imwrite_unicode,
    load_frame as _load_frame,
    to_bundle,
)


def discover_frames(inputs: list[str], args) -> list[SavedFrame]:
    return _discover_frames(inputs)


def load_frame(entry: SavedFrame, args) -> SavedFrame:
    return _load_frame(entry, fx=args.fx, fy=args.fy, cx=args.cx, cy=args.cy,
                       width=args.width, height=args.height,
                       depth_scale=args.depth_scale)


# ---------------------------------------------------------------- 参数
def _guidance_config(path: str | None):
    store = GuidanceConfigStore(path or os.path.join(
        _ROOT, "configs", "motion", "guidance.yaml"), seed_if_missing=False)
    store.load()
    return store.config


def _apply_sets(base: dict, overrides: dict[str, Any], prefix: str) -> dict:
    out = dict(base)
    for key, value in overrides.items():
        if key.startswith(prefix + "."):
            out[key[len(prefix) + 1:]] = value
    return out


def parse_sets(items: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or ():
        if "=" not in item:
            raise SystemExit(f"--set 需要 key=value：{item!r}")
        key, raw = item.split("=", 1)
        try:
            val: Any = json.loads(raw)
        except Exception:  # noqa: BLE001 - 非 JSON 当字符串
            val = raw
        out[key.strip()] = val
    return out


# ---------------------------------------------------------------- 找圆后端
def make_circle_backend(args):
    if args.circle_host:
        from application.hosts.circle38.client import Circle38Proxy, SubprocessTransport
        transport = SubprocessTransport([args.circle_host])
        return Circle38Proxy(transport), f"cp38 宿主：{args.circle_host}"
    backend = OpenCVCircleBackend()
    backend.load()
    return backend, ("OpenCV 进程内后端（非 cp38 原生；参数语义相同、结果可能不完全一致；"
                     "Windows 可用 --circle-host 指定 circle38_host.exe）")


# ---------------------------------------------------------------- 找圆/定位回放
def run_guidance_replay(frame: FrameBundle, cfg, host, sets: dict, fine_ref: str | None):
    depth_base = dataclasses.asdict(cfg.depth)
    depth_over = _apply_sets(depth_base, sets, "depth")
    try:
        depth_params = DepthGuidanceParams.from_dict(depth_over)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"深度参数非法: {exc}"}
    out: dict[str, Any] = {"rects": [], "circles": [], "errors": []}
    if frame.depth is None or not frame.depth_scale_m:
        out["error"] = "该帧缺深度或 depth_scale，跳过深度引导"
        return out
    try:
        stage = depth_guidance_stage(frame, depth_params)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"深度引导失败: {exc}"
        return out
    out["rects"] = [dict(r) for r in stage.rects]
    image = frame.color.data if frame.color is not None else stage.stages.get("contour_image")
    if image is None:
        out["error"] = "没有可用于找圆的彩色图/轮廓图"
        return out
    circle_over = _apply_sets(dict(cfg.circle or {}), sets, "circle")
    known = set(CircleParams.__dataclass_fields__)
    params = CircleParams(**{k: v for k, v in circle_over.items() if k in known})
    try:
        hits, errors = find_circles_in_rects(image, stage.rects, params, host)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"找圆失败: {exc}"
        return out
    out["errors"] = list(errors)
    depth_mm = depth_to_mm(frame.depth.data, float(frame.depth_scale_m))
    offsets = cfg.offsets_for(fine_ref)
    intr = intrinsics_to_dict(frame.intrinsics) if frame.intrinsics is not None else None
    circles = []
    for hit in hits:
        item = {"rect_index": int(hit.rect_index), "cx": float(hit.cx),
                "cy": float(hit.cy), "radius_px": float(hit.radius)}
        if intr is not None:
            xyz, used = point_depth_xyz(depth_mm, hit.cx, hit.cy, intr, depth_params)
            item["depth_mm"] = float(used)
            if xyz is not None and offsets is not None:
                coarse, fine = compute_robot_offsets(xyz, offsets)
                item.update(xyz_mm=[float(v) for v in xyz],
                            coarse_mm=[float(v) for v in coarse],
                            fine_mm=[float(v) for v in fine])
        circles.append(item)
    out["circles"] = circles
    out["depth_params"] = depth_over
    out["circle_params"] = circle_over
    out["stage_image"] = stage.stages.get("contour_image")
    return out


# ---------------------------------------------------------------- 分割回放
def load_scheme(args):
    if args.scheme:
        with open(args.scheme, encoding="utf-8") as fh:
            if args.scheme.lower().endswith(".json"):
                data = json.load(fh)
            else:
                from ruamel.yaml import YAML
                data = YAML(typ="safe").load(fh)
        scheme = coerce_scheme(data)
        if scheme is None:
            raise SystemExit(f"不是有效的运行一次方案：{args.scheme}")
        return scheme, f"文件 {args.scheme}"
    if args.detection:
        store = VisionDetectionStore(os.path.join(
            _ROOT, "configs", "vision", "detections.yaml"), seed_if_missing=False)
        det = store.find(args.detection)
        if det is None:
            raise SystemExit(f"找不到视觉检测 {args.detection!r}")
        scheme = coerce_scheme((det.params or {}).get("scheme"))
        if scheme is None:
            raise SystemExit(f"检测 {args.detection!r} 没有保存「运行一次」方案")
        return scheme, f"检测 {args.detection}"
    return None, ""


def run_scheme_replay(frame: FrameBundle, scheme, *, real: bool):
    segmenter, note = default_segmenter(offline=not real)
    if segmenter is None:
        return {"error": f"智能分割不可用: {note}"}
    try:
        res = run_scheme(scheme, frame, segmenter)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"运行一次失败: {exc}"}
    return {
        "segmenter": getattr(segmenter, "name", type(segmenter).__name__),
        "note": note,
        "error": res.error,
        "n_contours": res.n_contours,
        "depth_contours": res.depth_contours,
        "intersection_contours": res.intersection_contours,
        "prompts": res.prompts,
    }


# ---------------------------------------------------------------- 出图
def _rotate_display(img: np.ndarray, deg: int) -> np.ndarray:
    deg = int(deg) % 360
    if deg == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if deg == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def annotate(entry: SavedFrame, guidance: dict, seg: dict | None,
             display_rotate: int) -> np.ndarray | None:
    if entry.color is None:
        return None
    img = entry.color.copy()
    for i, rect in enumerate(guidance.get("rects") or []):
        x1, y1 = int(rect["x1"]), int(rect["y1"])
        x2, y2 = int(rect["x2"]), int(rect["y2"])
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(img, f"rect{i}", (x1 + 3, y1 + 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1, cv2.LINE_AA)
    for c in guidance.get("circles") or []:
        cx, cy = int(round(c["cx"])), int(round(c["cy"]))
        r = max(1, int(round(c.get("radius_px", 0))))
        cv2.circle(img, (cx, cy), r, (0, 0, 255), 2)
        cv2.drawMarker(img, (cx, cy), (255, 255, 0), cv2.MARKER_CROSS, 12, 1)
        label = f"({cx},{cy}) r={r}"
        if "coarse_mm" in c:
            label += " dx=%.1f dz=%.1f" % tuple(c["coarse_mm"])
        cv2.putText(img, label, (cx + 6, cy - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1, cv2.LINE_AA)
    if seg and not seg.get("error"):
        for contour in seg.get("depth_contours") or []:
            pts = np.asarray(contour, dtype=np.int32).reshape(-1, 1, 2)
            if len(pts) >= 3:
                cv2.polylines(img, [pts], True, (0, 255, 0), 2)
        for contour in seg.get("intersection_contours") or []:
            pts = np.asarray(contour, dtype=np.int32).reshape(-1, 1, 2)
            if len(pts) >= 3:
                cv2.polylines(img, [pts], True, (255, 0, 0), 2)
        for group in seg.get("prompts") or []:
            for p in group:
                p = np.asarray(p, dtype=float).reshape(-1)
                if p.size >= 2:
                    cv2.drawMarker(img, (int(p[0]), int(p[1])), (0, 255, 255),
                                   cv2.MARKER_CROSS, 10, 1)
    text = f"{os.path.basename(entry.name)} | {entry.camera_id} | " \
           f"{img.shape[1]}x{img.shape[0]} | rects={len(guidance.get('rects') or [])} " \
           f"circles={len(guidance.get('circles') or [])}"
    cv2.putText(img, text, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                (255, 255, 255), 2, cv2.LINE_AA)
    return _rotate_display(img, display_rotate)


def depth_preview(entry: SavedFrame, depth_params: DepthGuidanceParams,
                  display_rotate: int) -> np.ndarray | None:
    if entry.depth is None:
        return None
    from application.algorithms.image_ops import depth_to_rgb
    rgb = depth_to_rgb(entry.depth, None, lo=depth_params.depth_min_mm,
                       hi=depth_params.depth_max_mm,
                       depth_scale_m=entry.depth_scale_m)
    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return _rotate_display(img, display_rotate)


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="离线回放本地抓帧（找圆/定位 + 视觉分割）")
    ap.add_argument("--input", nargs="+", required=True,
                    help="目录或文件（可多个）")
    ap.add_argument("--out", default=os.path.join(_ROOT, "runtime", "debug_replay"),
                    help="输出目录（默认 runtime/debug_replay）")
    ap.add_argument("--guidance", default=None,
                    help="引导配置文件（默认 configs/motion/guidance.yaml）")
    ap.add_argument("--circle-host", default=None,
                    help="cp38 圆宿主 exe 路径；不填用 OpenCV 进程内后端")
    ap.add_argument("--detection", default=None,
                    help="按已保存视觉检测名回放「运行一次」（读其 scheme）")
    ap.add_argument("--scheme", default=None,
                    help="直接给运行一次方案 YAML/JSON 文件")
    ap.add_argument("--real-segmenter", action="store_true",
                    help="尝试真实 EdgeSAM（需 onnxruntime + 模型；失败如实标注）")
    ap.add_argument("--display-rotate", type=int, default=270,
                    help="输出图显示旋转角度（默认 270；DS86 与界面一致）")
    ap.add_argument("--no-depth-preview", action="store_true",
                    help="不输出深度伪彩图")
    ap.add_argument("--fx", type=float, default=None)
    ap.add_argument("--fy", type=float, default=None)
    ap.add_argument("--cx", type=float, default=None)
    ap.add_argument("--cy", type=float, default=None)
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--depth-scale", type=float, default=None,
                    help="无 JSON 时的 depth_scale（米/原始值），缺省 0.001")
    ap.add_argument("--set", action="append", default=[],
                    help="参数覆盖，如 --set depth.depth_min_mm=120 --set circle.min_radius=8")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sets = parse_sets(args.set)
    os.makedirs(args.out, exist_ok=True)
    cfg = _guidance_config(args.guidance)
    host, host_note = make_circle_backend(args)
    scheme, scheme_src = load_scheme(args)
    entries = discover_frames(args.input, args)
    if not entries:
        print("没有找到可用的帧（支持 *_depth.npy / *_depth16.png / 彩色图 / *_color.npy）")
        return 2
    print(f"发现 {len(entries)} 帧；找圆后端：{host_note}")
    rows: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        entry = load_frame(entry, args)
        bundle = to_bundle(entry, i)
        guidance = dict(run_guidance_replay(bundle, cfg, host, sets, None))
        seg = run_scheme_replay(bundle, scheme, real=args.real_segmenter) if scheme else None
        display_rotate = args.display_rotate if str(entry.camera_id).lower().startswith(
            "ds86") else 0
        img = annotate(entry, guidance, seg, display_rotate)
        base_out = os.path.join(args.out, os.path.basename(entry.name))
        if img is not None:
            imwrite_unicode(base_out + "_annotated.png",
                            cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        if entry.depth is not None and not args.no_depth_preview:
            depth_params = DepthGuidanceParams.from_dict(
                _apply_sets(dataclasses.asdict(cfg.depth), sets, "depth"))
            preview = depth_preview(entry, depth_params, display_rotate)
            if preview is not None:
                imwrite_unicode(base_out + "_depth_preview.png", preview)
        result = {
            "frame": os.path.basename(entry.name),
            "camera_id": entry.camera_id,
            "color_size": (list(entry.color.shape[:2][::-1])
                           if entry.color is not None else None),
            "depth_size": (list(entry.depth.shape[:2][::-1])
                           if entry.depth is not None else None),
            "depth_scale_m": entry.depth_scale_m,
            "has_intrinsics": entry.intrinsics is not None,
            "guidance": guidance,
            "segmentation": seg,
            "notes": entry.notes,
        }
        with open(base_out + "_result.json", "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
        best = (guidance.get("circles") or [{}])[0]
        rows.append({
            "frame": result["frame"], "camera": entry.camera_id,
            "width": (result["color_size"] or [None])[0],
            "height": (result["color_size"] or [None, None])[1],
            "depth_scale_m": entry.depth_scale_m,
            "rects": len(guidance.get("rects") or []),
            "circles": len(guidance.get("circles") or []),
            "cx": best.get("cx"), "cy": best.get("cy"),
            "radius_px": best.get("radius_px"),
            "depth_mm": best.get("depth_mm"),
            "x_mm": (best.get("xyz_mm") or [None, None, None])[0],
            "y_mm": (best.get("xyz_mm") or [None, None, None])[1],
            "z_mm": (best.get("xyz_mm") or [None, None, None])[2],
            "coarse_dx_mm": (best.get("coarse_mm") or [None, None])[0],
            "coarse_dz_mm": (best.get("coarse_mm") or [None, None])[1],
            "fine_dx_mm": (best.get("fine_mm") or [None, None])[0],
            "fine_dz_mm": (best.get("fine_mm") or [None, None])[1],
            "guidance_error": guidance.get("error"),
            "seg_error": (seg or {}).get("error"),
            "notes": "; ".join(entry.notes),
        })
        print(f"  {result['frame']}: rects={rows[-1]['rects']} circles={rows[-1]['circles']} "
              f"coarse={rows[-1]['coarse_dx_mm']},{rows[-1]['coarse_dz_mm']}"
              + (f"  [guidance: {guidance['error']}]" if guidance.get("error") else ""))
    csv_path = os.path.join(args.out, "summary.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"输出：{args.out}（每帧 *_annotated.png / *_result.json，汇总 summary.csv）")
    if scheme is not None:
        print(f"分割回放：{scheme_src}（segmenter 见每帧 JSON）")
    try:
        if hasattr(host, "close"):
            host.close()
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
