# -*- coding: utf-8 -*-
"""DEC-046「深度伪彩全黑」修复前后对照 + 作业台右卡离屏出图（2 张）。

用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_depth_range.py out_dir/

内容：
  01_深度伪彩_修复前后对比.png — 同一张合成深度场景（DS86 现场量级 120–290 mm）
     分别按默认 200–3000 mm（修复前）与引导配置现场区间 100–300 mm（修复后）上色；
  02_作业台右卡_仅彩色_DEC-050.png — 作业台右卡现状：只显示彩色抓拍；深度卡按
     DEC-050 已从该卡移除（深度视图仍在视觉调试页）。

全部是**合成帧**（FakeCamera/手工帧），只证明显示链路与界面，不是现场图像；
不打开相机、不连设备、不驱动机械臂。设备配置复制到临时目录再用，不写仓库那份。
退出顺序照 Qt 关闭顺序那条坑；出证据脚本必须看退出码（rc=0 才算过）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import cv2
import numpy as np

from application.algorithms.image_ops import depth_to_rgb

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")
DEVICES = os.path.join(_ROOT, "configs", "devices", "devices.yaml")
MOTION = os.path.join(_ROOT, "configs", "motion", "motion.yaml")
CID = "ds86_01"
FIELD_RANGE = (100.0, 300.0)          # guidance.yaml ds86_depth_map_min/max_mm


def _scene(h: int = 360, w: int = 480) -> np.ndarray:
    """合成深度场景: 底板 ~150 mm、圆盘 ~250 mm、右侧 ~120 mm 台阶、角落无效(0)。"""
    d = np.full((h, w), 150, np.uint16)
    cv2.circle(d, (w // 2, h // 2), h // 4, 250, -1)
    d[:, int(w * 0.8):] = 120
    d[:20, :20] = 0                    # 无效值区域 (显示应纯黑)
    return d


def _label(img: np.ndarray, text: str, x: int, y: int) -> None:
    """ASCII 标签 (cv2 不带中文字模; 中文说明放在文件名与台账里)。"""
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                (255, 255, 255), 2, cv2.LINE_AA)


def render_compare(out_dir: str) -> str:
    scene = _scene()
    before = depth_to_rgb(scene)                                     # 默认 200–3000
    after = depth_to_rgb(scene, lo=FIELD_RANGE[0], hi=FIELD_RANGE[1])
    gap = np.full((scene.shape[0], 12, 3), 40, np.uint8)
    canvas = np.hstack([before, gap, after])
    _label(canvas, "BEFORE default 200-3000 mm (looks black)", 10, 30)
    _label(canvas, "AFTER field 100-300 mm (DEC-046)", before.shape[1] + 22, 30)
    path = os.path.join(out_dir, "01_深度伪彩_修复前后对比.png")
    cv2.imwrite(path, cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR))
    return path


def _frame_bundle(depth_mm: np.ndarray, color: np.ndarray):
    from application.contracts.enums import ImageSpace
    from application.contracts.frames import FrameBundle, ImageRef, Intrinsics

    h, w = depth_mm.shape
    return FrameBundle(
        camera_id=CID, frame_id="render-depth-range", capture_id="render",
        sequence=1, service_id="local", channel_id=CID,
        color=ImageRef.from_array(color),
        depth=ImageRef.from_array(depth_mm),
        depth_scale_m=0.001,
        intrinsics=Intrinsics(fx=400.0, fy=400.0, cx=w / 2, cy=h / 2,
                              width=w, height=h,
                              intrinsics_space=ImageSpace.DEPTH,
                              calibration_version="render"))


def render_workbench(out_dir: str) -> str:
    from PySide6.QtWidgets import QApplication

    from application.bootstrap.composition import build_offline_stack
    from application.presentation.app import MainWindow, UiContext
    from application.presentation.theme import THEME
    from application.presentation.viewmodel import WorkbenchViewModel

    app = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp(prefix="depth_range_")
    dev_copy = os.path.join(tmp, "devices.yaml")
    shutil.copyfile(DEVICES, dev_copy)
    stack = build_offline_stack(
        db_path=os.path.join(tmp, "r.db"), runtime_dir=tmp, recipe_path=RECIPE,
        device_config_path=dev_copy, motion_config_path=MOTION)
    stack.motion.guidance.config_store.config.ds86.update(
        {"ds86_depth_map_min_mm": FIELD_RANGE[0],
         "ds86_depth_map_max_mm": FIELD_RANGE[1]})
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    win = MainWindow(UiContext(stack=stack, vm=vm))
    win.resize(*THEME.target_screen)
    win.show()
    app.processEvents()
    try:
        win._select("workbench")       # noqa: SLF001 - 同一代码库内驱动
        win.refresh_all()
        app.processEvents()
        page = win.pages["workbench"]
        depth = _scene(240, 320)
        color = np.dstack([(depth % 200).astype(np.uint8)] * 3)
        bundle = _frame_bundle(depth, color)
        vm.current_frame = lambda: bundle
        page._sync_image_card()        # noqa: SLF001
        app.processEvents()
        path = os.path.join(out_dir, "02_作业台右卡_仅彩色_DEC-050.png")
        win.grab().save(path)
        return path
    finally:
        win.shutdown()
        win.close()
        win.deleteLater()
        app.processEvents()
        stack.close()


def main(argv: list[str]) -> int:
    out_dir = argv[1] if len(argv) > 1 else "depth_range_out"
    os.makedirs(out_dir, exist_ok=True)
    print(render_compare(out_dir))
    print(render_workbench(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
