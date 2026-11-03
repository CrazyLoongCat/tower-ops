"""采集层自动镜像（DEC-043）：指定相机在成像时自动「水平 + 垂直」双镜像。

现场 D405-2 / D405-4 倒装（上下颠倒），用户要求这两台相机**成像时自动增加水平
和垂直镜像**。双镜像等价 180° 旋转：彩色与深度**同一变换**，内参 cx/cy 同步换算
（cx' = W-1-cx，cy' = H-1-cy），因此预览/判定/证据/引导像素→3D 拿到的是一致且
自洽的帧，而不是「显示翻了、判定还用原图」的显示层修饰（那是 image_ops 的职责，
DEC-021，仅预览）。

实现为**适配器装饰器**：装配时只给 :data:`MIRRORED_CAMERA_IDS` 里的相机注册装饰后的
适配器（legacy 与 V2 共用同一装饰器），按 frame 的 camera_id 过滤——只有集合内的相机被
翻转，其它帧原样透传（不复制、不改元数据）。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

import numpy as np

from application.contracts.frames import FrameBundle, ImageRef, Intrinsics

# 需要自动双镜像的相机（用户 2026-10-19 指定；改这里即可增减，不依赖设备配置改动）
MIRRORED_CAMERA_IDS: frozenset[str] = frozenset({"d405_02", "d405_04"})


def _mirror_array(array: Any, *, horizontal: bool, vertical: bool) -> np.ndarray:
    """对 HxW(…C) 数组做镜像；结果新的 C 连续数组（不与源缓冲共享）。"""
    out = array
    if vertical:
        out = out[::-1, :]
    if horizontal:
        out = out[:, ::-1]
    return np.ascontiguousarray(out)


def _mirror_ref(ref: ImageRef | None, *, horizontal: bool, vertical: bool) -> ImageRef | None:
    if ref is None:
        return None
    data = _mirror_array(ref.data, horizontal=horizontal, vertical=vertical)
    return ImageRef.from_array(data, encoding=ref.encoding, generation=ref.generation)


def _mirror_intrinsics(inr: Intrinsics | None, *,
                       horizontal: bool, vertical: bool) -> Intrinsics | None:
    """镜像后像素坐标系变了：cx/cy 按 (W-1)-x / (H-1)-y 换算（fx/fy 不变）。"""
    if inr is None:
        return None
    cx, cy = inr.cx, inr.cy
    if horizontal:
        cx = (inr.width - 1) - cx
    if vertical:
        cy = (inr.height - 1) - cy
    return replace(inr, cx=cx, cy=cy)


def mirror_frame_bundle(frame: FrameBundle, *,
                        horizontal: bool = True,
                        vertical: bool = True) -> FrameBundle:
    """返回镜像后的新 FrameBundle：彩色/深度同变换、内参同步、其余元数据不变。"""
    if not horizontal and not vertical:
        return frame
    return replace(
        frame,
        color=_mirror_ref(frame.color, horizontal=horizontal, vertical=vertical),
        depth=_mirror_ref(frame.depth, horizontal=horizontal, vertical=vertical),
        intrinsics=_mirror_intrinsics(frame.intrinsics,
                                      horizontal=horizontal, vertical=vertical),
    )


class MirroredCameraAdapter:
    """相机适配器装饰器：指定相机 id 的抓拍帧自动双镜像，其它相机原样透传。"""

    def __init__(self, inner: Any, camera_ids: Iterable[str] = MIRRORED_CAMERA_IDS,
                 *, horizontal: bool = True, vertical: bool = True) -> None:
        self.inner = inner
        self.camera_ids = frozenset(camera_ids)
        self.horizontal = bool(horizontal)
        self.vertical = bool(vertical)

    def capture(self, request: Any) -> Any:
        result = self.inner.capture(request)
        frames = result.frames
        mirrored = {cid: mirror_frame_bundle(frame,
                                             horizontal=self.horizontal,
                                             vertical=self.vertical)
                    for cid, frame in frames.items() if cid in self.camera_ids}
        if mirrored:
            frames = dict(frames)
            frames.update(mirrored)
            result.frames = frames
        return result

    # 其余接口（open/close/status/stream 等）一律透传给内层适配器
    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def wrap_capture_mirror(service: Any,
                        camera_ids: Iterable[str] = MIRRORED_CAMERA_IDS) -> MirroredCameraAdapter:
    """装配用入口：把一个相机服务包成「指定相机自动双镜像」的适配器。"""
    return MirroredCameraAdapter(service, camera_ids)


__all__ = [
    "MIRRORED_CAMERA_IDS",
    "MirroredCameraAdapter",
    "mirror_frame_bundle",
    "wrap_capture_mirror",
]
