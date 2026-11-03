# -*- coding: utf-8 -*-
"""「日志诊断」页(E24)离屏出图（界面验证证据）。

用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_e24_diagnostics.py out_dir/

内容：全量日志（不限于当前 run）/ 按等级·设备过滤 / 打开日志文件入口 —— 全走**离线栈**
（Fake 硬件），不碰真实设备。产出的图用于证明「诊断页不再是空的」并展示过滤交互。

两条纪律：
- 设备配置**只用临时副本**（保险起见），绝不写仓库 ship 的 `configs/devices/devices.yaml`。
- 退出顺序照“Qt 关闭顺序”那条坑：停定时器 → 关窗 → deleteLater → 跑事件 → 关栈；
  出证据的脚本更该看退出码（否则图出得来但 rc=139）。
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
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"需要 PySide6: {exc}\n安装: python -m pip install PySide6")

from application.bootstrap.composition import build_offline_stack
from application.infrastructure import logging_setup
from application.infrastructure.events import Event, Severity
from application.presentation.app import MainWindow, UiContext
from application.presentation.theme import THEME
from application.presentation.viewmodel import WorkbenchViewModel

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")
DEVICES = os.path.join(_ROOT, "configs", "devices", "devices.yaml")
MOTION = os.path.join(_ROOT, "configs", "motion", "motion.yaml")


def _make_window(app):
    tmp = tempfile.mkdtemp(prefix="e24_diag_")
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
    return win, stack, tmp


def _close(app, win, stack) -> None:
    win.shutdown()
    win.close()
    win.deleteLater()
    app.processEvents()
    stack.close()
    logging_setup.reset()


def _seed(stack) -> None:
    """造几类来源的日志：设备连接/探测、任务、视觉、异常 —— 演示「全部来源」。"""
    stack.device_state.open("ds86_01")
    from application.services.device_probe import ProbeResult
    stack.device_state.record_probe("eco65_01", ProbeResult(False, "TCP 连接失败", is_real=True))
    stack.records.append_event(Event(event_type="task.start", severity=Severity.INFO,
                                     message="启动 ds86_two_station_example", run_id="run-1"))
    try:
        raise ValueError("注入：相机未打开")
    except ValueError:
        logging_setup.get_logger("services.actions").error(
            "抓拍集合失败", exc_info=True,
            extra={"run_id": "run-1", "device_id": "ds86_01"})
    stack.records.append_event(Event(event_type="task.fault", severity=Severity.ERROR,
                                     message="抓拍集合失败: 相机未打开", run_id="run-1",
                                     device_id="ds86_01", reason="相机未打开"))
    logging_setup.get_logger("services.vision_detections").info(
        "视觉检测已新增 圆检测-离线 pipeline=approved_ds86_circle_pipeline",
        extra={"device_id": "ds86_01"})


def main(argv: list[str]) -> int:
    out_dir = argv[0] if argv else os.path.join(_ROOT, "runtime", "e24_shots")
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

    win, stack, tmp = _make_window(app)
    _seed(stack)
    win._select("diagnostics")          # noqa: SLF001 - 同一代码库内驱动
    app.processEvents()
    page = win.pages["diagnostics"]
    page.refresh()
    app.processEvents()
    grab(win, "01_全量日志_全部来源")
    print("  行数:", page.event_table.rowCount(), " 日志文件:", page.path_label.text())

    page.severity_combo.setCurrentIndex(page.severity_combo.findData("ERROR"))
    page.refresh()
    app.processEvents()
    grab(win, "02_按等级过滤_ERROR")
    page.severity_combo.setCurrentIndex(0)

    page.device_combo.setCurrentIndex(page.device_combo.findData("ds86_01"))
    page.refresh()
    app.processEvents()
    grab(win, "03_按设备过滤_ds86_01")
    page.device_combo.setCurrentIndex(0)

    page.keyword_edit.setText("抓拍")
    page.refresh()
    app.processEvents()
    grab(win, "04_关键字过滤_抓拍")
    page.keyword_edit.clear()

    _close(app, win, stack)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
