# -*- coding: utf-8 -*-
"""视觉调试页(E19)「图像算法」面板离屏出图（界面验证证据, 7 张）。

用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_e19_vision.py out_dir/

内容：默认预览 / 几何镜像同行 / 深度切分上下限(mm) / 深度无效压黑 / 轮廓检测参数快照 /
原图对照 / 保存到本通道。全部走**离线栈**（FakeCamera + 进程内 stub 视觉），不碰真实设备：
第 4 张用离线栈的故障注入 `camera_low_depth=True` 拿到全 0 深度，演示“无效深度一律压黑”。

两条纪律：
- 设备配置**先复制到临时目录**再用 —— 第 7 张会点「保存到本通道」，绝不写仓库里那份
  ship 的 `configs/devices/devices.yaml`（DEC-023 踩过）。
- 退出顺序照“Qt 关闭顺序”那条坑：停定时器 → 关窗 → deleteLater → 跑事件 → 关栈，
  否则图出得来但进程 rc=139；出证据的脚本更该看退出码。
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


def _make_window(app, tag: str, *, camera_low_depth: bool = False):
    """离线栈 + 主窗口；设备配置用**临时副本**（出图会落盘“保存到本通道”）。"""
    tmp = tempfile.mkdtemp(prefix=f"e19_{tag}_")
    dev_copy = os.path.join(tmp, "devices.yaml")
    shutil.copyfile(DEVICES, dev_copy)
    stack = build_offline_stack(
        db_path=os.path.join(tmp, "r.db"), runtime_dir=tmp, recipe_path=RECIPE,
        camera_low_depth=camera_low_depth, device_config_path=dev_copy,
        motion_config_path=MOTION)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    win = MainWindow(UiContext(stack=stack, vm=vm))
    win.resize(*THEME.target_screen)
    win.show()
    app.processEvents()
    return win, stack


def _capture(app, win, stack, cid: str = CID):
    """选中通道 -> 单次抓拍 -> 返回视觉页（拿不到帧直接报错, 不静默出空图）。"""
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


def _scroll_to_group(app, page, label_text: str) -> None:
    """把某一组滚到面板**顶部**（整组：标题 + 说明 + 参数行 都进画面）。

    只调 ``ensureWidgetVisible``（"进入视野"）不够 —— 深度伪彩在面板下部，那样滚完标题贴在
    视口底边、下面的「色标 / 切分下限 (mm) / 切分上限 (mm)」还在视口外，出图看着像"没有这两行"。
    这里直接把纵向滚动条设到该组标题所在位置。
    """
    from PySide6.QtWidgets import QLabel, QScrollArea
    area = page.algo.findChild(QScrollArea)
    if area is None:  # pragma: no cover - 版面改了才会没有
        return
    app.processEvents()
    labels = [lab for lab in area.findChildren(QLabel) if lab.text() == label_text]
    if not labels:
        raise SystemExit(f"面板上没找到分组标题: {label_text!r}")
    lab = labels[0]
    inner_y = lab.mapTo(area.widget(), lab.rect().topLeft()).y()
    bar = area.verticalScrollBar()
    bar.setValue(max(0, min(bar.maximum(), inner_y - 4)))
    app.processEvents()


def _set(page, key: str, value) -> None:
    w, kind = page._widgets[key]
    if kind == "bool":
        w.setChecked(bool(value))
    elif kind == "choice":
        w.setCurrentIndex(w.findData(value))
    else:
        w.setValue(value)


def main(argv: list[str]) -> int:
    out_dir = argv[0] if argv else os.path.join(_ROOT, "runtime", "e19_shots")
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

    # ---------- 正常离线栈: 01/02/03/05/06/07 ----------
    win, stack = _make_window(app, "normal")
    page = _capture(app, win, stack)
    grab(win, "01_抓拍后_默认参数_预览等于原图")
    print("  帧元数据:", page.kv_scale.text())

    _set(page, "hflip", True)
    _set(page, "vflip", True)
    app.processEvents()
    grab(win, "02_几何_镜像同行_预览即时生效")

    page.btn_algo_reset.click()
    app.processEvents()
    # 本帧深度恒为 1000 mm(=1.0 m): 下限抬到 1200 mm 后全部低于下限 -> 夹紧到色标低端
    _set(page, "depth_seg_min_mm", 1200)
    _set(page, "depth_seg_max_mm", 3000)
    _scroll_to_group(app, page, "深度伪彩")   # 把带( mm )标签的切分组滚进画面
    grab(win, "03_深度切分上下限_mm_范围外夹紧")
    print("  预览提示:", page.preview_hint.text())

    page.btn_algo_reset.click()
    _scroll_to_group(app, page, "几何变换")
    _set(page, "detect", "contour")
    _set(page, "detect_min_area", 10.0)
    app.processEvents()
    page._run_pipeline()           # noqa: SLF001 - 判定结果里带图像算法参数快照
    app.processEvents()
    grab(win, "05_轮廓检测_结果记参数快照")

    page.chk_raw.setChecked(True)
    app.processEvents()
    grab(win, "06_原图对照_未套算法")
    page.chk_raw.setChecked(False)
    app.processEvents()

    _set(page, "threshold_value", 20)
    app.processEvents()
    page.btn_algo_save.click()     # 写到**临时副本**设备配置, 仓库 ship 配置不动
    app.processEvents()
    grab(win, "07_保存到本通道")
    print("  保存后:", page.result.text().splitlines()[0])
    _close(app, win, stack)

    # ---------- 故障注入栈: 04 全 0 深度 -> 无效一律压黑 ----------
    win2, stack2 = _make_window(app, "lowdepth", camera_low_depth=True)
    page2 = _capture(app, win2, stack2)
    grab(win2, "04_深度无效_0一律压黑")
    print("  有效深度比:", page2.kv_valid.text())
    _close(app, win2, stack2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
