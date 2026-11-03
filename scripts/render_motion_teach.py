"""把「运动方案 → 路径规划」页顶部「示教点」分组离屏渲染成 PNG（DEC-067 界面证据）。

用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_motion_teach.py OUT_DIR

**演示用示教点**（写在临时目录，数值是界面演示，**非现场示教值**；现场登记必须在
真实机械臂上「读取当前关节」）。只走离线栈，不接触真实设备。
"""
from __future__ import annotations

import os
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
    raise SystemExit(f"需要 PySide6: {exc}")

from application.bootstrap.composition import build_offline_stack
from application.presentation.app import MainWindow, UiContext
from application.presentation.theme import THEME
from application.presentation.viewmodel import WorkbenchViewModel
from application.services.teach_points import TeachPointStore

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")


def _demo_teach_store(tmp: str) -> TeachPointStore:
    """演示示教点（临时目录；数值仅用于界面演示，非现场值）。"""
    path = os.path.join(tmp, "joint_targets.yaml")
    from datetime import datetime, timezone
    fixed = datetime(2026, 10, 22, 1, 30, 0, tzinfo=timezone.utc)
    store = TeachPointStore(path, clock=lambda: fixed)
    store.add("teach_01", [45.005, 90.0, -150.0, 121.7, 90.0, 0.0])
    store.add("teach_02", [45.004, -81.499, -127.519, 119.02, 90.002, 0.0])
    return store


def main(argv: list[str]) -> int:
    out_dir = argv[0] if argv else os.path.join(_ROOT, "runtime", "ui_shots")
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp()
    stack = build_offline_stack(db_path=os.path.join(tmp, "r.db"), runtime_dir=tmp,
                                recipe_path=RECIPE)
    # 离线装配没有示教点登记；这里只给渲染演示注入一个临时 store（不写仓库配置）
    stack.teach_points = _demo_teach_store(tmp)
    app = QApplication([])
    font = QFont()
    font.setFamilies(list(THEME.font_stack))
    font.setPointSize(10)
    app.setFont(font)
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    win = MainWindow(UiContext(stack=stack, vm=vm))
    win.resize(*THEME.target_screen)
    win.show()
    app.processEvents()
    win._select("motion")
    win.pages["motion"].tabs.setCurrentIndex(0)
    app.processEvents()
    shot = os.path.join(out_dir, "motion_teach.png")
    win.grab().save(shot)
    print(f"saved {shot}")
    # 关闭顺序：停定时器 -> 关窗 -> deleteLater -> 跑事件（Qt 关闭顺序坑）
    win.shutdown(); win.close(); win.deleteLater(); app.processEvents()
    stack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
