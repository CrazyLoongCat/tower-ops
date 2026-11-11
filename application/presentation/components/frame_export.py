"""抓帧图片保存（DEC-121 / DEC-122 / DEC-123）。

凡是有「抓帧/单次抓拍」的地方，都提供「保存图片」：
- 有彩色流：保存**彩色原图**为所选路径（PNG/JPEG）；
- 有深度流：保存一套**带深度信息、可线下调试**的原始深度文件：
  `<基名>_depth.npy`（uint16 原始值）、`<基名>_depth16.png`（**16 位无损灰度 PNG**，
  像素值=相机原始深度值，可用 OpenCV/ImageJ 直接读）、`<基名>_depth.json`
  （depth_scale_m/内参/尺寸/相机与帧号等元数据，换算公式写明）；
  另存 `<基名>_depth.png` 作**伪彩预览图**（仅用于看，不用于调试计算）；
- 没有彩色流时伪彩图直接存到所选路径；原始深度三件套照常保存；
- 保存的是**采集到的原始帧数据**（不含显示旋转/图像算法预览），文件名带相机与时间；
- 默认目录 `<runtime>/captures/`，路径可用对话框改；QImage 写盘支持中文路径。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

try:  # pragma: no cover - Qt
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QFileDialog

    from .image_view import to_qimage

    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False

__all__ = ["save_frame_files", "save_frame_image", "write_depth_sidecar"]


def _frame_arrays(frame):
    color = getattr(getattr(frame, "color", None), "data", None)
    depth = getattr(getattr(frame, "depth", None), "data", None)
    return color, depth


def save_frame_files(frame, parent=None, *, camera_id: str | None = None,
                     title: str = "保存图片") -> list[str]:
    """弹保存对话框并写下当前帧（彩色 + 深度）；返回实际写出的文件列表。

    - 彩色：保存到所选路径（PNG/JPEG）；
    - 深度：原始值 `.npy` + 伪彩 `.png`（同目录、基名后缀 `_depth`）；
    - 没有彩色流时，深度伪彩图直接保存到所选路径。
    取消返回空列表。
    """
    if frame is None:
        return []
    color, depth = _frame_arrays(frame)
    if color is None and depth is None:
        return []

    cam = str(camera_id or getattr(frame, "camera_id", "") or "frame")
    try:
        from application.bootstrap import app_paths
        base = os.path.join(app_paths.runtime_dir(), "captures")
    except Exception:  # noqa: BLE001
        base = os.path.join(".", "captures")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:  # noqa: BLE001 - 目录建不了就让对话框自己选
        pass
    default = os.path.join(base, f"{cam}_{datetime.now():%Y%m%d_%H%M%S}.png")
    path, _filter = QFileDialog.getSaveFileName(
        parent, title, default, "PNG 图片 (*.png);;JPEG 图片 (*.jpg);;所有文件 (*)")
    if not path:
        return []

    root, _ext = os.path.splitext(path)
    saved: list[str] = []
    if color is not None:
        image = to_qimage(color, kind="color")
        if image is not None and image.save(path):
            saved.append(path)
    if depth is not None:
        saved += write_depth_sidecar(
            depth, root, frame=frame, camera_id=cam,
            colormap_path=(path if color is None else None))
    return saved


def write_depth_sidecar(depth, root: str, *, frame=None,
                        camera_id: str | None = None,
                        colormap_path: str | None = None) -> list[str]:
    """把原始深度写成可线下调试的一套文件（DEC-123），返回写出的路径列表。

    - ``<root>_depth.npy``     : numpy 原始值（uint16）
    - ``<root>_depth16.png``   : 16 位无损灰度 PNG，像素值 = 相机原始深度值
    - ``<root>_depth.json``    : 元数据（depth_scale_m、内参、尺寸、相机/帧号、换算公式）
    - ``<root>_depth.png``     : 伪彩预览（仅用来看；有彩色时与彩色同目录）
    """
    saved: list[str] = []
    try:
        import numpy as _np
        arr = _np.ascontiguousarray(_np.asarray(depth).astype(_np.uint16))
    except Exception:  # noqa: BLE001
        return saved
    if arr.ndim < 2:
        return saved
    h, w = int(arr.shape[0]), int(arr.shape[1])

    npy_path = root + "_depth.npy"
    try:
        _np.save(npy_path, arr)
        saved.append(npy_path)
    except Exception:  # noqa: BLE001
        pass

    png16_path = root + "_depth16.png"
    try:
        image = QImage(arr.data, w, h, int(arr.strides[0]),
                       QImage.Format.Format_Grayscale16).copy()
        if image.save(png16_path):
            saved.append(png16_path)
    except Exception:  # noqa: BLE001
        pass

    meta = {
        "camera_id": str(camera_id or getattr(frame, "camera_id", "") or ""),
        "frame_id": getattr(frame, "frame_id", None),
        "capture_id": getattr(frame, "capture_id", None),
        "sequence": getattr(frame, "sequence", None),
        "width": w,
        "height": h,
        "dtype": "uint16",
        "depth_scale_m": getattr(frame, "depth_scale_m", None),
        "unit_note": "深度(米) = 像素值 × depth_scale_m；0 通常表示无效/无回波",
        "files": {"npy": os.path.basename(npy_path),
                  "depth16_png": os.path.basename(png16_path)},
        "saved_utc": datetime.now(timezone.utc).isoformat(),
        "note": "原始深度数据（未套显示旋转与图像算法预览）；_depth.png 仅为伪彩预览图",
    }
    intr = getattr(frame, "intrinsics", None)
    if intr is not None:
        meta["intrinsics"] = {
            "fx": getattr(intr, "fx", None), "fy": getattr(intr, "fy", None),
            "cx": getattr(intr, "cx", None), "cy": getattr(intr, "cy", None),
            "width": getattr(intr, "width", None), "height": getattr(intr, "height", None),
            "space": getattr(getattr(intr, "intrinsics_space", None), "value", None),
        }
    json_path = root + "_depth.json"
    try:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        saved.append(json_path)
    except Exception:  # noqa: BLE001
        pass

    depth_image = to_qimage(arr, kind="depth")
    if depth_image is not None:
        cmap = colormap_path or (root + "_depth.png")
        try:
            if depth_image.save(cmap):
                saved.append(cmap)
        except Exception:  # noqa: BLE001
            pass
    return saved


def save_frame_image(frame, parent=None, *, camera_id: str | None = None,
                     title: str = "保存图片") -> str | None:
    """兼容入口：保存一套文件并返回第一个（彩色/主图）路径。"""
    saved = save_frame_files(frame, parent, camera_id=camera_id, title=title)
    return saved[0] if saved else None
