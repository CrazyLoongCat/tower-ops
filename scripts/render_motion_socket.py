"""把「运动方案」页两个子 tab 离屏渲染成 PNG（**套筒动作路点**的界面证据）。

用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_motion_socket.py OUT_DIR

用**演示用**运动配置（写在临时目录，**非现场值**）：一条含 ``movel``/``movej``、**套筒偏移**
（``socket_offset``，带符号 mm：+ 进入/前进，− 脱离/后退）与**工具系偏移**
（``tool_offset``，dx/dy/dz mm，DEC-060）的**混排**方案，给「路径规划」路点表留下两类行。
工具链与 ``render_ui.py`` 相同：只走离线栈，不接触真实设备。
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
from application.services.motion_config import MotionConfigStore, MotionPlan, Waypoint

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")


def _demo_motion(tmp: str) -> str:
    """演示方案：movel/movej 与**套筒偏移**混排（符号定方向；写在临时目录，非现场值）。"""
    path = os.path.join(tmp, "motion.yaml")
    st = MotionConfigStore(path)
    st.load()
    st.config.plans.append(MotionPlan(
        plan_id="演示：取套筒+对位", desc="render 演示（非现场值）", default_mode="movel",
        speed=20, blend=10, timeout=30,
        waypoints=[
            Waypoint("movej", (10.0, 20.0, 30.0, 0.0, 90.0, 0.0)),
            Waypoint("movel", (0.312, -0.045, 0.520, 3.14, 0.0, 0.0)),
            Waypoint("socket_offset", (0.0, 12.0, 0.0, 0.0, 0.0, 0.0)),   # +12mm 进入套筒
            Waypoint("socket_offset", (0.0, 6.0, 0.0, 0.0, 0.0, 0.0)),    # +6mm 套筒前进
            Waypoint("tool_offset", (0.0, 0.0, 5.0, 0.0, 0.0, 0.0)),      # DEC-060 Z+5mm 抬升
            Waypoint("tool_offset", (0.0, 100.0, -5.0, 0.0, 0.0, 0.0)),   # 斜直线回原目标
            Waypoint("socket_offset", (0.0, -6.0, 0.0, 0.0, 0.0, 0.0)),   # −6mm 套筒后退
            Waypoint("socket_offset", (0.0, -12.0, 0.0, 0.0, 0.0, 0.0)),  # −12mm 脱离套筒
        ], source="render 演示（非现场值）"))
    st.save()
    return path


def main(argv: list[str]) -> int:
    out = argv[0] if argv else os.path.join(_ROOT, "runtime", "ui_shots")
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp()
    stack = build_offline_stack(
        db_path=os.path.join(tmp, "r.db"), runtime_dir=tmp, recipe_path=RECIPE,
        motion_config_path=_demo_motion(tmp))
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

    win._select("motion")            # noqa: SLF001
    app.processEvents()
    page = win.pages["motion"]
    page.tabs.setCurrentIndex(0)     # 路径规划
    app.processEvents()
    win.grab().save(os.path.join(out, "motion_planning_socket.png"))
    print("saved motion_planning_socket.png")
    page.tabs.setCurrentIndex(1)     # 路径实施
    app.processEvents()
    win.grab().save(os.path.join(out, "motion_execution.png"))
    print("saved motion_execution.png")

    # 关闭顺序（Qt 关闭顺序那条坑）：停定时器 -> 关窗 -> deleteLater -> 跑事件 -> 关栈。
    win.shutdown()
    win.close()
    win.deleteLater()
    app.processEvents()
    stack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
