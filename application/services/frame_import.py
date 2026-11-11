"""本地抓帧导入（DEC-126）。

把「保存图片」保存下来的文件重新读成 :class:`FrameBundle`，供视觉调试页离线查看/调参。
支持三种来源（与 DEC-123 的保存格式一一对应）：
  1. 四件套：`<名>.png/.jpg` + `<名>_depth.npy` 或 `<名>_depth16.png` + `<名>_depth.json`；
  2. 运行记录帧：`<相机>-fN_color.npy` + `<名>_depth.npy`（无 JSON 时 depth_scale 默认 0.001）；
  3. 单独彩色图：只有画面，无深度。
内参 / depth_scale 从 JSON 读；缺内参可通过参数指定；都缺时如实记 note，不猜默认内参。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

try:  # pragma: no cover - 图片文件读取
    import cv2
except Exception:  # noqa: BLE001
    cv2 = None  # type: ignore[assignment]

from application.contracts.enums import ImageSpace
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff")

__all__ = ["ImportedFrame", "discover_frames", "imwrite_unicode",
           "load_frame", "to_bundle"]


@dataclass
class ImportedFrame:
    """一份本地帧（路径 + 读出来的数组/元数据）。"""

    name: str
    color_path: str | None = None
    depth_path: str | None = None
    meta_path: str | None = None
    color: np.ndarray | None = None      # RGB uint8
    depth: np.ndarray | None = None      # uint16 原始值
    depth_scale_m: float | None = None
    intrinsics: Intrinsics | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def camera_id(self) -> str:
        base = os.path.basename(self.name)
        if "-f" in base:
            return base.split("-f")[0]
        return "ds86_01" if base.lower().startswith("ds86") else "file"


def _strip_suffix(path: str, suffixes: tuple[str, ...]) -> str | None:
    for suf in suffixes:
        if path.lower().endswith(suf):
            return path[: -len(suf)]
    return None


def _first_existing(candidates: Sequence[str]) -> str | None:
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _imread_unicode(path: str, flags: int) -> np.ndarray | None:
    """cv2 读图（支持中文/Unicode 路径，DEC-128）。

    Windows 上 OpenCV 的 ``cv2.imread`` 用 ANSI 文件接口，路径含中文（如
    ``F:/配置及数据/...``）会直接返回 None/读失败；这里改成
    ``np.fromfile``（Python 层按 Unicode 打开）+ ``cv2.imdecode`` 解码。
    """
    if cv2 is None:  # pragma: no cover
        return None
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, flags)
    except Exception:  # noqa: BLE001 - 读不了返回 None，由调用方记 note
        return None


def imwrite_unicode(path: str, image_bgr: np.ndarray) -> bool:
    """cv2 写图（支持中文/Unicode 路径，DEC-128）。image 为 BGR。"""
    if cv2 is None:  # pragma: no cover
        return False
    try:
        ext = os.path.splitext(path)[1].lower() or ".png"
        ok, buf = cv2.imencode(ext, image_bgr)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _read_rgb(path: str) -> np.ndarray | None:
    if path.lower().endswith(".npy"):
        arr = np.load(path)
    elif cv2 is not None:
        arr = _imread_unicode(path, cv2.IMREAD_COLOR)
        if arr is not None:
            arr = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
    else:  # pragma: no cover
        return None
    if arr is None:
        return None
    if arr.ndim == 2:
        arr = np.dstack([arr, arr, arr])
    if arr.ndim == 3 and arr.shape[2] == 4:
        arr = arr[:, :, :3]
    return np.ascontiguousarray(arr.astype(np.uint8))


def _read_depth(path: str) -> np.ndarray | None:
    if path.lower().endswith(".npy"):
        arr = np.load(path)
    elif cv2 is not None:
        arr = _imread_unicode(path, cv2.IMREAD_UNCHANGED)
    else:  # pragma: no cover
        return None
    if arr is None or getattr(arr, "ndim", 0) != 2:
        return None
    return np.ascontiguousarray(arr.astype(np.uint16))


def _read_fail_note(kind: str, path: str | None) -> str:
    if not path:
        return f"{kind}读取失败：没有找到配对文件"
    if not os.path.isfile(path):
        return f"{kind}读取失败：文件不存在 {path}"
    try:
        size = os.path.getsize(path)
    except Exception:  # noqa: BLE001
        size = -1
    return (f"{kind}读取失败：文件存在（{size} 字节）但解码失败 {path}"
            "（检查文件是否完整、扩展名与实际格式是否一致）")


def _load_meta(path: str | None) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _intrinsics_from_meta(meta: dict, *, width: int | None, height: int | None,
                          fx=None, fy=None, cx=None, cy=None,
                          meta_width=None, meta_height=None) -> Intrinsics | None:
    intr = meta.get("intrinsics") if isinstance(meta.get("intrinsics"), dict) else {}
    fx = fx or intr.get("fx")
    fy = fy or intr.get("fy")
    cx = cx if cx is not None else intr.get("cx")
    cy = cy if cy is not None else intr.get("cy")
    w = meta_width or intr.get("width") or width
    h = meta_height or intr.get("height") or height
    if not (fx and fy and cx is not None and cy is not None and w and h):
        return None
    try:
        space = ImageSpace(str(intr.get("space") or "depth"))
    except Exception:  # noqa: BLE001
        space = ImageSpace.DEPTH
    try:
        return Intrinsics(fx=float(fx), fy=float(fy), cx=float(cx), cy=float(cy),
                          width=int(w), height=int(h), intrinsics_space=space,
                          calibration_version="imported")
    except Exception:  # noqa: BLE001
        return None


def discover_frames(inputs: Sequence[str]) -> list[ImportedFrame]:
    """把目录/文件列表配对成 ImportedFrame（只定位文件，不读数组）。"""
    files: list[str] = []
    for item in inputs:
        if os.path.isdir(item):
            for dirpath, _dirs, names in os.walk(item):
                for n in names:
                    files.append(os.path.join(dirpath, n))
        elif os.path.isfile(item):
            files.append(item)
    roots: dict[str, ImportedFrame] = {}

    def entry(root: str) -> ImportedFrame:
        if root not in roots:
            roots[root] = ImportedFrame(name=root)
        return roots[root]

    for path in sorted(set(files)):
        low = path.lower()
        if low.endswith("_depth.json"):
            entry(_strip_suffix(path, ("_depth.json",)) or path).meta_path = path
            continue
        if low.endswith("_depth.npy"):
            entry(_strip_suffix(path, ("_depth.npy",)) or path).depth_path = path
            continue
        if low.endswith("_depth16.png"):
            e = entry(_strip_suffix(path, ("_depth16.png",)) or path)
            e.depth_path = e.depth_path or path
            continue
        if low.endswith("_color.npy"):
            entry(_strip_suffix(path, ("_color.npy",)) or path).color_path = path
            continue
        if low.endswith("_depth.png"):
            e = entry(_strip_suffix(path, ("_depth.png",)) or path)
            e.notes.append("该文件是深度伪彩预览（不用于计算），已忽略")
            continue
        if low.endswith(_IMAGE_EXTS):
            entry(os.path.splitext(path)[0]).color_path = path
    for e in roots.values():
        base = e.name
        if e.depth_path is None:
            e.depth_path = _first_existing([base + "_depth.npy", base + "_depth16.png"])
        if e.color_path is None:
            e.color_path = _first_existing(
                [base + ".png"] + [base + ext for ext in _IMAGE_EXTS]
                + [base + "_color.npy"])
        if e.meta_path is None:
            e.meta_path = _first_existing([base + "_depth.json"])
    keep = [e for e in roots.values() if e.color_path or e.depth_path]
    keep.sort(key=lambda f: f.name)
    return keep


def load_frame(entry: ImportedFrame, *, fx=None, fy=None, cx=None, cy=None,
               width=None, height=None, depth_scale=None) -> ImportedFrame:
    """读取数组 + 元数据；缺内参/深度比例时如实记 note（不猜默认内参）。"""
    meta = _load_meta(entry.meta_path)
    if entry.color_path:
        entry.color = _read_rgb(entry.color_path)
        if entry.color is None:
            entry.notes.append(_read_fail_note("彩色", entry.color_path))
    if entry.depth_path:
        entry.depth = _read_depth(entry.depth_path)
        if entry.depth is None:
            entry.notes.append(_read_fail_note("深度", entry.depth_path))
    scale = meta.get("depth_scale_m") or depth_scale
    entry.depth_scale_m = float(scale) if scale else None
    if entry.depth is not None and entry.depth_scale_m is None:
        entry.depth_scale_m = 0.001
        entry.notes.append("缺 depth_scale（无 JSON/未指定），按 1 原始值=1mm 处理")
    h, w = (entry.depth.shape[:2] if entry.depth is not None
            else (entry.color.shape[:2] if entry.color is not None else (None, None)))
    entry.intrinsics = _intrinsics_from_meta(
        meta, width=w, height=h, fx=fx, fy=fy, cx=cx, cy=cy,
        meta_width=width, meta_height=height)
    if entry.depth is not None and entry.intrinsics is None:
        entry.notes.append("缺内参：不能算 XYZ/偏移（可用 fx/fy/cx/cy 指定）")
    return entry


def to_bundle(entry: ImportedFrame, index: int = 0) -> FrameBundle:
    return FrameBundle(
        camera_id=entry.camera_id, frame_id=os.path.basename(entry.name),
        capture_id=f"import-{index}", sequence=index, service_id="file-import",
        channel_id=entry.camera_id,
        color=(ImageRef.from_array(entry.color) if entry.color is not None else None),
        depth=(ImageRef.from_array(entry.depth) if entry.depth is not None else None),
        depth_scale_m=entry.depth_scale_m, intrinsics=entry.intrinsics,
        alignment_space=(entry.intrinsics.intrinsics_space
                         if entry.intrinsics is not None else None),
        calibration_version="imported",
    )
