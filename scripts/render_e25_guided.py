"""把「引导定位」步骤的界面离屏渲染成 PNG（DEC-033 的界面证据）。

用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_e25_guided.py OUT_DIR

两张图：
- ``recipes_guided_step.png``   「配方流程」页：引导定位步骤的属性（相机/段/速度/偏移上限）；
- ``workbench_guided_confirm.png`` 「作业台」页：每段人工确认**弹窗**（**演示提示**，
  不是真实运行；离线栈不接真机，也不产生任何运动；DEC-059 起由内联提示条改为弹窗）。
工具链与 ``render_ui.py`` 相同：只走离线栈。
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
from application.contracts.enums import NodeType

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")


def main(argv: list[str]) -> int:
    out = argv[0] if argv else os.path.join(_ROOT, "runtime", "ui_shots")
    os.makedirs(out, exist_ok=True)
    tmp = tempfile.mkdtemp()
    stack = build_offline_stack(db_path=os.path.join(tmp, "r.db"), runtime_dir=tmp,
                                recipe_path=RECIPE)
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

    # ---- 1) 配方流程页：引导定位步骤属性 ----
    page = win.pages["recipes"]
    page.editor.new_recipe("引导定位演示")
    page.editor.set_offline_draft(False)
    page.editor.add_step(NodeType.CAMERA_CAPTURE)
    sid = page.editor.add_step(NodeType.GUIDED_POSITION)
    page.editor.set_step_field(sid, "guide_speed_pct", 10)
    page.editor.set_step_field(sid, "guide_max_offset_mm", 20.0)
    page._sel_step = sid
    page.refresh()
    app.processEvents()
    win._select("recipes")           # noqa: SLF001
    app.processEvents()
    win.grab().save(os.path.join(out, "recipes_guided_step.png"))
    print("saved recipes_guided_step.png")

    # ---- 2) 作业台页：每段人工确认**弹窗**（演示提示，非真实运行；DEC-059）----
    wb = win.pages["workbench"]
    wb.vm.pending_operator_prompt = lambda: {          # 演示注入（不改产品行为）
        "title": "引导定位 · 粗定位",
        "message": "检测到偏差 X=10.00mm Z=-5.00mm\n是否按该偏移移动？",
        "options": ["是", "否"],
        "step_id": sid,
    }
    wb._sync_confirm()
    win._select("workbench")         # noqa: SLF001
    app.processEvents()
    dlg = wb._guide_dialog
    if dlg is not None:
        dlg.grab().save(os.path.join(out, "workbench_guided_confirm.png"))
        print("saved workbench_guided_confirm.png (确认弹窗)")
    else:                            # 理论上不会发生；保底仍出一张主窗
        win.grab().save(os.path.join(out, "workbench_guided_confirm.png"))
        print("saved workbench_guided_confirm.png (无弹窗，退回主窗)")

    # 关闭顺序（Qt 关闭顺序那条坑）：停定时器 -> 关窗 -> deleteLater -> 跑事件 -> 关栈。
    win.shutdown()
    win.close()
    win.deleteLater()
    app.processEvents()
    stack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
