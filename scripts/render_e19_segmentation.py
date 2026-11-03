# -*- coding: utf-8 -*-
"""视觉调试页(E19)「视觉分割 / 运行一次」离屏出图（界面验证证据, 7 张）。

用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_e19_segmentation.py out_dir/

内容：深度分割(轮廓+提示点) / 运行一次(交集命中) / 另存为检测(保存运行一次方案) /
载回检测(方案复原) / 几何变换生效时绘制分割停用 / 画ROI(左键拖拽) / 清除(保留ROI)。
全部走**离线栈**（FakeCamera）。

两条纪律（照 render_e19_vision.py）：
- 设备配置与运行目录**先复制到临时目录**再用 —— 「另存为检测」会落盘，绝不写仓库里那份
  ship 的 `configs/devices/devices.yaml`（DEC-023 踩过）。
- 退出顺序照"Qt 关闭顺序"：停定时器 → 关窗 → deleteLater → 跑事件 → 关栈，否则 rc=139。

**关于智能分割后端**：本脚本**不注入占位**——用的是栈里真实的 `default_segmenter`。沙箱里
装了 onnxruntime 且仓库有 EdgeSAM 模型，故跑的是**真实模型**，结果里如实标注后端名；
若真机缺模型，界面会明确报错而不会用占位冒充（宁缺勿假）。
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

try:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"需要 PySide6: {exc}\n安装: python -m pip install PySide6")

from application.bootstrap.composition import build_offline_stack
from application.presentation.app import MainWindow, UiContext
from application.presentation.pages import vision as vis_mod
from application.presentation.theme import THEME
from application.presentation.viewmodel import WorkbenchViewModel

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")
DEVICES = os.path.join(_ROOT, "configs", "devices", "devices.yaml")
MOTION = os.path.join(_ROOT, "configs", "motion", "motion.yaml")
CID = "ds86_01"
POLY = [[120, 120], [520, 120], [520, 360], [120, 360]]   # FakeCamera 帧 640×480


def _make_window(app, tag: str):
    tmp = tempfile.mkdtemp(prefix=f"e19seg_{tag}_")
    dev_copy = os.path.join(tmp, "devices.yaml")
    shutil.copyfile(DEVICES, dev_copy)
    stack = build_offline_stack(
        db_path=os.path.join(tmp, "r.db"), runtime_dir=tmp, recipe_path=RECIPE,
        device_config_path=dev_copy, motion_config_path=MOTION)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    win = MainWindow(UiContext(stack=stack, vm=vm))
    win.resize(*THEME.target_screen)
    win.show()
    app.processEvents()
    return win, stack


def _capture(app, win, stack, cid: str = CID):
    win._select("vision")          # noqa: SLF001
    app.processEvents()
    page = win.pages["vision"]
    cam = stack.device_config.find_camera(cid)
    cam.enabled = True
    cam.confirmed = True
    row = next(i for i in range(page.chan_list.count())
               if page.chan_list.item(i).data(Qt.ItemDataRole.UserRole) == cid)
    page.chan_list.setCurrentRow(row)
    app.processEvents()
    page._capture()                # noqa: SLF001
    app.processEvents()
    if page._frame is None:
        raise SystemExit(f"抓拍失败: {page.result.text()}")
    page.right_tabs.setCurrentWidget(page.seg)     # 出的是「视觉分割」页
    app.processEvents()
    return page


def _close(app, win, stack) -> None:
    win.shutdown()
    win.close()
    win.deleteLater()
    app.processEvents()
    stack.close()


def _drag_roi(page, app, x1, y1, x2, y2) -> None:
    """在彩色主图上用**左键**拖拽画 ROI（新增入口；右键同样可用）。

    坐标给的是图像像素，经 `mapFromScene` 落到视口坐标——与真实点击走同一条事件路径。
    """
    view = page.view_color.view
    p1 = view.mapFromScene(QPointF(float(x1), float(y1)))
    p2 = view.mapFromScene(QPointF(float(x2), float(y2)))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, p1)
    QTest.mouseMove(view.viewport(), QPoint((p1.x() + p2.x()) // 2, (p1.y() + p2.y()) // 2))
    QTest.mouseMove(view.viewport(), p2)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, p2)
    app.processEvents()


def main(argv: list[str]) -> int:
    out_dir = argv[0] if argv else os.path.join(_ROOT, "runtime", "e19seg_shots")
    os.makedirs(out_dir, exist_ok=True)

    app = QApplication([])
    font = QFont()
    font.setFamilies(list(THEME.font_stack))
    font.setPointSize(10)
    app.setFont(font)

    def grab(win, name: str) -> None:
        app.processEvents()
        win.grab().save(os.path.join(out_dir, f"{name}.png"))
        print("saved", name)

    win, stack = _make_window(app, "seg")
    page = _capture(app, win, stack)
    page._set_polygon([list(p) for p in POLY])      # noqa: SLF001 - 画用户多边形(品红)
    app.processEvents()

    # 01 深度分割：标轮廓(绿) + 轮廓内提示点(黄十字)
    page._run_depth_seg()                            # noqa: SLF001
    app.processEvents()
    grab(win, "01_深度分割_轮廓与提示点")
    print("  深度分割:", page.result.text().splitlines()[0])

    # 02 运行一次：真实算法路径(深度分割 → 智能分割 → 交集)
    page._run_once()                                 # noqa: SLF001
    app.processEvents()
    grab(win, "02_运行一次_真实算法_交集")
    res = getattr(page, "_seg_result", None)
    if res is not None:
        print(f"  运行一次: hit={res.hit} 面积={res.area_px}px² 后端={res.segmenter_name}")

    # 03 另存为检测：把「运行一次方案」存进检测（脚本内替换命名对话框，不弹窗）
    vis_mod._ask_text = lambda *a, **k: ("演示-绝缘子", True)   # noqa: SLF001
    page.btn_save_det.click()
    app.processEvents()
    grab(win, "03_另存为检测_保存运行一次方案")
    print("  另存:", page.result.text().splitlines()[0])

    # 04 载回检测：改掉手上的设置，再双击检测把方案复原（面板 + 多边形叠加）
    page._set_polygon([[10, 10], [20, 10], [20, 20]])          # noqa: SLF001
    page._seg_widgets["source"].setCurrentIndex(
        page._seg_widgets["source"].findData("depth"))
    page.det_list.setCurrentRow(0)
    page._load_detection_scheme()                              # noqa: SLF001
    app.processEvents()
    grab(win, "04_载回检测_运行一次方案复原")
    print("  载回:", page.result.text().splitlines()[0])

    # 05 几何变换生效 -> 绘制/分割停用（原始帧坐标与预览不再对齐）
    page._widgets["hflip"][0].setChecked(True)
    app.processEvents()
    page._run_once()                                           # noqa: SLF001
    app.processEvents()
    grab(win, "05_几何变换_绘制分割停用")
    print("  停用说明:", page.result.text().splitlines()[0][:30], "…")

    # 06 画 ROI（左键拖拽，新增入口）+ 07 清除（清多边形/提示点/分割框，ROI 作为运算区域保留）
    page._widgets["hflip"][0].setChecked(False)                # noqa: SLF001 - 复位几何变换
    app.processEvents()
    page._set_polygon([list(p) for p in POLY])                 # noqa: SLF001
    page._run_once()                                           # noqa: SLF001 - 先攒出叠加
    app.processEvents()
    page.btn_draw_roi.setChecked(True)                         # noqa: SLF001
    page._seg_mode("roi")                                      # noqa: SLF001
    _drag_roi(page, app, 60, 60, 300, 220)                     # 左键拖拽画出运算区域
    grab(win, "06_画ROI_左键拖拽")
    print("  画ROI:", page._roi)
    page.btn_seg_clear.click()                                 # 清除（真实按钮接线）
    app.processEvents()
    grab(win, "07_清除_保留ROI")
    print("  清除:", page.result.text().splitlines()[0][:32], "…")

    _close(app, win, stack)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
