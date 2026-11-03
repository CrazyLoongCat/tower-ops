"""图像显示组件 (E19, 03 §7)。

把 numpy 数组渲染成 QImage 显示:
- 彩色 (H,W,3) uint8 -> RGB888;
- 深度/灰度 (H,W) -> 伪彩 (实现委托 `application.algorithms.image_ops.depth_to_rgb`,
  忽略无效值(0), 便于看深度分布);
- 单通道按灰度复制成三通道; 空/None 显示占位文字。

约束: 只做**显示**, 不改数据、不做算法判定。像素距离不能冒充 mm —— 度量与单位由
页面依据帧元数据 (depth_scale_m / intrinsics) 标注, 组件不猜。
(深度伪彩的映射区间(mm)与帧深度比例由调用方传入: ``show_array(..., depth_lo_mm=,
depth_hi_mm=, depth_scale_m=)``; 不给时走 ``image_ops`` 默认口径 200–3000 mm、
1 原始值 = 1 mm。DEC-046: 现场页面按通道解析区间后显式传入。)
"""
from __future__ import annotations

try:  # pragma: no cover
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import QLabel, QSizePolicy
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QLabel = object  # type: ignore[assignment]

__all__ = ["GUI_REQUIRED", "ImageView", "depth_to_rgb", "to_qimage"]


def depth_to_rgb(array, *, lo: float | None = None, hi: float | None = None,
                 depth_scale_m: float | None = None):
    """深度/灰度 -> RGB 伪彩。0 视为无效值, 单独置黑, 不参与归一化。

    **实现只有一套**: 委托到
    :func:`application.algorithms.image_ops.depth_to_rgb` (E19, DEC-021)。
    本组件只负责显示, 色标/有效范围的分位数等算法约定不在这里维护 ——
    改伪彩只改 image_ops 一处。这里保持惰性导入, 免得无 cv2 的环境连界面模块都导入不了。

    ``lo``/``hi`` 是 mm 显式映射区间 (DEC-046: 调用方应按通道/相机给对区间,
    DS86 现场就是 100–300 mm); 不给则走 image_ops 默认 200–3000 mm。
    """
    from application.algorithms.image_ops import depth_to_rgb as _impl
    return _impl(array, lo=lo, hi=hi, depth_scale_m=depth_scale_m)


def to_qimage(array, *, kind: str = "color", depth_lo_mm: float | None = None,
              depth_hi_mm: float | None = None,
              depth_scale_m: float | None = None):
    """numpy 数组 -> QImage (已 copy, 不依赖原缓冲生命周期)。"""
    import numpy as np
    from PySide6.QtGui import QImage

    a = np.asarray(array)
    if a.size == 0:
        return None
    if kind == "depth" or a.ndim == 2:
        rgb = depth_to_rgb(a, lo=depth_lo_mm, hi=depth_hi_mm,
                           depth_scale_m=depth_scale_m)
    elif a.ndim == 3 and a.shape[2] >= 3:
        rgb = a[:, :, :3]
    else:
        return None
    rgb = np.ascontiguousarray(rgb.astype("uint8"))
    h, w = rgb.shape[:2]
    img = QImage(rgb.data, int(w), int(h), int(rgb.strides[0]),
                 QImage.Format.Format_RGB888)
    return img.copy()          # 不持有 numpy 缓冲


if GUI_REQUIRED:

    class ImageView(QLabel):
        """等比缩放显示一帧图像; 未设置时显示占位文字。"""

        def __init__(self, placeholder: str = "尚未抓拍", parent=None) -> None:
            super().__init__(parent)
            self.setObjectName("ImageView")
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setMinimumSize(240, 180)
            self.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Expanding)
            self.setStyleSheet(
                "background:#0E1A22; color:#93A6B2; border:1px solid #24323C;"
                " border-radius:6px;")
            self._placeholder = placeholder
            self._pixmap: QPixmap | None = None
            self.clear_image()

        # ---------- 显示 ----------
        def show_array(self, array, *, kind: str = "color",
                       depth_lo_mm: float | None = None,
                       depth_hi_mm: float | None = None,
                       depth_scale_m: float | None = None) -> bool:
            if array is None:
                self.clear_image()
                return False
            img = to_qimage(array, kind=kind, depth_lo_mm=depth_lo_mm,
                            depth_hi_mm=depth_hi_mm,
                            depth_scale_m=depth_scale_m)
            if img is None:
                self.clear_image()
                return False
            self._pixmap = QPixmap.fromImage(img)
            self.setText("")
            self._rescale()
            return True

        def clear_image(self, text: str | None = None) -> None:
            self._pixmap = None
            self.setText(text if text is not None else self._placeholder)

        def set_placeholder(self, text: str) -> None:
            self._placeholder = text
            if self._pixmap is None:
                self.setText(text)

        @property
        def available(self) -> bool:
            return self._pixmap is not None

        # ---------- 缩放 ----------
        def _rescale(self) -> None:  # pragma: no cover - Qt
            if self._pixmap is None:
                return
            self.setPixmap(self._pixmap.scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

        def resizeEvent(self, event) -> None:  # pragma: no cover - Qt
            super().resizeEvent(event)
            self._rescale()

else:  # pragma: no cover - 无 Qt 环境
    class ImageView:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")
