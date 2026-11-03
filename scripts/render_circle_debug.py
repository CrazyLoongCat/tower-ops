# -*- coding: utf-8 -*-
"""视觉调试页「找圆调试」+ 作业台「彩色/深度」离屏出图（DEC-045 界面证据, 5 张）。

用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_circle_debug.py out_dir/

内容：01 深度检测（轮廓+正外接矩形）/ 02 找圆完成 / 03 运行一次找圆调试（XYZ+粗精偏移）/
04 阶段图切到二值图 / 05 作业台右卡彩色 + 深度伪彩。

全部走**离线栈**（FakeCamera + 进程内 stub 圆宿主），不碰真实设备、不驱动机械臂；
离线圆来自 stub（非真实检测），出图只证明**界面与流程**，不替代现场证据。
第 05 张把视觉页刚抓到的**同一帧**交给作业台右卡刷新（布局证据，非完整作业流程）。
设备配置先复制到临时目录再用，不写仓库 ship 的那份。

退出顺序照“Qt 关闭顺序”那条坑：停定时器 → 关窗 → deleteLater → 跑事件 → 关栈；
出证据的脚本必须看退出码（rc=0 才算过）。
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
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"需要 PySide6: {exc}\n安装: python -m pip install PySide6")

from application.bootstrap.composition import build_offline_stack
from application.presentation.app import MainWindow, UiContext
from application.presentation.theme import THEME
from application.presentation.viewmodel import WorkbenchViewModel

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")
DEVICES = os.path.join(_ROOT, "configs", "devices", "devices.yaml")
MOTION = os.path.join(_ROOT, "configs", "motion", "motion.yaml")
CID = "ds86_01"


def _make_window(app):
    tmp = tempfile.mkdtemp(prefix="circle_debug_")
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
    return win, stack, vm


def _capture(app, win, stack, cid: str = CID):
    win._select("vision")          # noqa: SLF001 - 同一代码库内驱动
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
    return page


def _close(app, win, stack) -> None:
    win.shutdown()
    win.close()
    win.deleteLater()
    app.processEvents()
    stack.close()


def main(argv: list[str]) -> int:
    out_dir = argv[0] if argv else os.path.join(_ROOT, "runtime", "circle_debug_shots")
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

    win, stack, vm = _make_window(app)
    page = _capture(app, win, stack)
    frame = page._frame

    page.right_tabs.setCurrentWidget(page.circle)
    app.processEvents()

    page.btn_cd_depth.click()
    app.processEvents()
    if not page._circle_rects:
        raise SystemExit(f"深度检测没找到矩形: {page.result.text()}")
    print("  深度:", page.result.text().splitlines()[1])
    grab(win, "01_深度检测_轮廓与正外接矩形")

    page.btn_cd_circles.click()
    app.processEvents()
    if not page._circle_hits:
        raise SystemExit(f"找圆没命中: {page.result.text()}")
    print("  找圆:", page.result.text().splitlines()[1])
    grab(win, "02_找圆完成_圆与圆心叠加")

    page.btn_cd_run.click()
    app.processEvents()
    res = page._circle_result
    if res is None or not res.ok:
        raise SystemExit(f"运行一次失败: {page.result.text()}")
    print("  阶段:", res.stage, "XYZ:", res.cam_xyz)
    grab(win, "03_运行一次_相机系XYZ与粗精偏移")

    combo = page._circle_widgets["stage"]
    combo.setCurrentIndex(combo.findData("binary"))
    app.processEvents()
    grab(win, "04_阶段图_二值图")

    # 05 作业台右卡: 同一帧交给右卡刷新（布局证据）
    win._select("workbench")       # noqa: SLF001
    win.refresh_all()
    app.processEvents()
    wb = win.pages["workbench"]
    vm.current_frame = lambda: frame         # noqa: SLF001 - 出图脚本注入
    wb._sync_image_card()                    # noqa: SLF001
    app.processEvents()
    if not (wb.image_view.available and wb.depth_view.available):
        raise SystemExit("作业台右卡没有同时显示彩色与深度")
    print("  作业台标题:", wb.image_caption.text())
    grab(win, "05_作业台右卡_彩色与深度伪彩")

    _close(app, win, stack)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
