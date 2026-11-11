# -*- coding: utf-8 -*-
"""工具位姿 3D 示意视图（DEC-140/145，「运动方案 → 手动调整」页）。

**轻量自绘**：不引入 OpenGL / Qt3D / WebEngine，也不用机械臂 CAD 模型——
用 QPainter 做等距投影，显示基座坐标系、地面网格、当前工具位置与姿态三轴。

诚实边界：这是**工具位姿示意**，不是按运动学还原的机械臂外形；画面不参与任何
运动计算，只读 ``get_current_pose()`` 的结果。数值同时以文字标注（mm / rad）。
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

try:  # pragma: no cover - 与其它组件一致的 Qt 依赖边界
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QPainter, QPen
    from PySide6.QtWidgets import QWidget
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

__all__ = ["GUI_REQUIRED", "RobotPoseView"]

_AZ = math.radians(35.0)      # 方位角
_EL = math.radians(25.0)      # 俯仰角
_GRID_HALF_MM = 500.0         # 地面网格半边长
_GRID_STEP_MM = 100.0
_AXIS_LEN_MM = 140.0          # 基座坐标轴长度
_TOOL_AXIS_MM = 90.0          # 工具姿态三轴长度

_C_BG = QColor("#f7f9fc")
_C_GRID = QColor("#dbe2ec")
_C_TEXT = QColor("#334155")
_C_MUTED = QColor("#64748b")
_C_DASH = QColor("#9aa8bd")
_C_X = QColor("#d64545")
_C_Y = QColor("#2f9e62")
_C_Z = QColor("#3f6fd8")
_C_TOOL = QColor("#0f766e")


def _rpy_matrix(rx: float, ry: float, rz: float) -> tuple[tuple[float, ...], ...]:
    """RYX/ZYX 常用约定：R = Rz(rz)·Ry(ry)·Rx(rx)（仅用于画姿态示意）。"""
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )


class RobotPoseView(QWidget):
    """QPainter 等距投影的位姿示意（只读显示，无副作用）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pose: Optional[tuple[float, ...]] = None
        self._connected = False
        self.setMinimumSize(360, 300)
        self.setToolTip("当前工具位姿示意（基座系，等距投影）；只读显示，不参与运动计算。")

    # ---------- 数据 ----------
    def set_connected(self, connected: bool) -> None:
        connected = bool(connected)
        if connected != self._connected:
            self._connected = connected
            if not connected:
                self._pose = None
            self.update()

    def set_pose(self, pose: Optional[Sequence[float]]) -> None:
        if pose is None or len(pose) < 6:
            self._pose = None
        else:
            self._pose = tuple(float(v) for v in pose[:6])
        self.update()

    def pose(self) -> Optional[tuple[float, ...]]:
        """最近一次收到的位姿（测试/调试用）；无则 None。"""
        return self._pose

    # ---------- 投影 ----------
    def _project(self, x_mm: float, y_mm: float, z_mm: float,
                 w: int, h: int) -> QPointF:
        xr = x_mm * math.cos(_AZ) - y_mm * math.sin(_AZ)
        yr = x_mm * math.sin(_AZ) + y_mm * math.cos(_AZ)
        sx = xr
        sy = z_mm * math.cos(_EL) - yr * math.sin(_EL)   # 正 = 屏幕上方
        scale = min(w / 1600.0, h / 900.0)
        cx, cy = w * 0.5, h * 0.62
        return QPointF(cx + sx * scale, cy - sy * scale)

    # ---------- 绘制 ----------
    def paintEvent(self, _event) -> None:  # pragma: no cover - Qt 绘制
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.fillRect(self.rect(), _C_BG)
            w, h = self.width(), self.height()
            self._draw_grid(p, w, h)
            self._draw_base_axes(p, w, h)
            if self._pose is not None and self._connected:
                self._draw_tool(p, w, h)
            self._draw_overlay(p, w, h)
        finally:
            p.end()

    def _draw_grid(self, p: QPainter, w: int, h: int) -> None:
        p.setPen(QPen(_C_GRID, 1.0))
        g = _GRID_HALF_MM
        v = -g
        while v <= g + 1e-9:
            p.drawLine(self._project(v, -g, 0, w, h),
                       self._project(v, g, 0, w, h))
            p.drawLine(self._project(-g, v, 0, w, h),
                       self._project(g, v, 0, w, h))
            v += _GRID_STEP_MM

    def _draw_base_axes(self, p: QPainter, w: int, h: int) -> None:
        o = self._project(0, 0, 0, w, h)
        axes = (
            ("X", _AXIS_LEN_MM, 0, 0, _C_X),
            ("Y", 0, _AXIS_LEN_MM, 0, _C_Y),
            ("Z", 0, 0, _AXIS_LEN_MM, _C_Z),
        )
        for label, dx, dy, dz, color in axes:
            tip = self._project(dx, dy, dz, w, h)
            p.setPen(QPen(color, 2.4))
            p.drawLine(o, tip)
            p.setPen(QPen(color))
            p.drawText(tip + QPointF(4, 4), label)

    def _draw_tool(self, p: QPainter, w: int, h: int) -> None:
        assert self._pose is not None
        x, y, z, rx, ry, rz = self._pose
        tip = self._project(x * 1000.0, y * 1000.0, z * 1000.0, w, h)

        # 基座 -> 工具 的虚线（位置向量大致方向）
        p.setPen(QPen(_C_DASH, 1.2, Qt.PenStyle.DashLine))
        p.drawLine(self._project(0, 0, 0, w, h), tip)

        # 工具点
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_C_TOOL)
        p.drawEllipse(tip, 5.0, 5.0)

        # 工具姿态三轴（RPY 旋转矩阵的列向量）
        rot = _rpy_matrix(rx, ry, rz)
        cols = (
            ("Xt", (rot[0][0], rot[1][0], rot[2][0]), _C_X),
            ("Yt", (rot[0][1], rot[1][1], rot[2][1]), _C_Y),
            ("Zt", (rot[0][2], rot[1][2], rot[2][2]), _C_Z),
        )
        for label, vec, color in cols:
            end = self._project(x * 1000.0 + vec[0] * _TOOL_AXIS_MM,
                                y * 1000.0 + vec[1] * _TOOL_AXIS_MM,
                                z * 1000.0 + vec[2] * _TOOL_AXIS_MM, w, h)
            p.setPen(QPen(color, 2.0))
            p.drawLine(tip, end)
            p.setPen(QPen(color))
            p.drawText(end + QPointF(3, 3), label)

    def _draw_overlay(self, p: QPainter, w: int, h: int) -> None:
        p.setPen(QPen(_C_TEXT))
        font = QFont()
        font.setPointSizeF(max(8.0, font.pointSizeF()))
        p.setFont(font)
        p.drawText(10, 18, "工具位姿（基座系）")
        small = QFont(font)
        small.setPointSizeF(max(7.5, font.pointSizeF() - 1.0))
        p.setFont(small)
        p.setPen(QPen(_C_MUTED))
        if not self._connected:
            p.drawText(QRectF(0.0, h * 0.74, float(w), 48.0),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       "机械臂未连接\n（实时位姿不可用）")
        elif self._pose is None:
            p.drawText(QRectF(0.0, h * 0.74, float(w), 24.0),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       "读取位姿中…")
        else:
            x, y, z, rx, ry, rz = self._pose
            line1 = (f"X {x * 1000.0:.1f} mm   Y {y * 1000.0:.1f} mm   "
                     f"Z {z * 1000.0:.1f} mm")
            line2 = f"RX {rx:.3f}   RY {ry:.3f}   RZ {rz:.3f} rad"
            p.setPen(QPen(_C_TEXT))
            p.drawText(10, 34, line1)
            p.drawText(10, 50, line2)
        p.setPen(QPen(_C_MUTED))
        p.drawText(10, h - 8, "示意：非运动学还原；数值来自机器人当前位姿")
