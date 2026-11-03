"""DEC-048 作业台视觉步骤「多框 + 框内圆」离屏出图（界面证据，1 张）。

流程（全离线，不接设备）：
1. 临时 runtime 里写一条两步骤执行方案（抓拍 → 视觉判定）；
2. 作业台跑到视觉判定待确认（右卡自动出现框/圆初始布局）；
3. 页面按检测配置再画一个框（框内自动找圆），选中该框的圆；
4. 隐藏步骤确认弹框后抓主窗口 → `01_作业台_多框多圆.png`。

只用于出图；不写仓库种子/不写用户配置。用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_workbench_layout.py <out_dir>
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

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")


def main(argv: list[str]) -> int:
    out_dir = argv[0] if argv else os.path.join(_ROOT, "runtime", "ui_shots")
    os.makedirs(out_dir, exist_ok=True)

    from PySide6.QtWidgets import QApplication

    from application.bootstrap.composition import build_offline_stack
    from application.presentation.app import MainWindow, UiContext
    from application.presentation.theme import THEME
    from application.presentation.viewmodel import WorkbenchViewModel

    tmp = tempfile.mkdtemp(prefix="wb_layout_")
    stack = build_offline_stack(db_path=os.path.join(tmp, "r.db"),
                                runtime_dir=tmp, recipe_path=RECIPE)
    stack.recipes.draft({
        "recipe_id": "演示_多框多圆", "version": "v1", "state": "draft",
        "sequence": [
            {"id": "cap", "type": "camera.capture", "camera_ref": "ds86_01"},
            {"id": "det", "type": "vision.run",
             "input_from": {"step": "cap", "output": "frame"},
             "pipeline_ref": "approved_ds86_circle_pipeline"},
        ],
    })
    app = QApplication.instance() or QApplication([])
    vm = WorkbenchViewModel(task=stack.task, records=stack.records,
                            allow_offline_draft=True)
    win = MainWindow(UiContext(stack=stack, vm=vm))
    win.resize(*THEME.target_screen)
    win.show()
    app.processEvents()
    try:
        win._select("workbench")            # noqa: SLF001 - 同一代码库内驱动
        win.refresh_all()
        page = win.pages["workbench"]
        vm.select_recipe("演示_多框多圆")
        vm.start_current()
        vm.tick()                            # 抓拍完成
        page.refresh()
        page._on_step_choice("确认")         # noqa: SLF001
        vm.tick()                            # 判定完成，等视觉步骤确认
        page.refresh()
        app.processEvents()

        state = vm.step_circle_state()
        circle = state.get("circle")
        if circle:
            cx, cy = int(round(circle["cx"])), int(round(circle["cy"]))
            # 再画一个**独立**的框（放在右侧），并在框内画圆 → 多框多圆的直观展示
            page._on_box_requested(cx + 60, cy - 60, cx + 160, cy + 60)  # noqa: SLF001
            app.processEvents()
            page._on_circle_drawn(1, cx + 110, cy, 30)                   # noqa: SLF001
            app.processEvents()
            state = vm.step_circle_state()
            if len(state.get("boxes") or []) > 1:
                page._on_target_selected("circle", 1)                    # noqa: SLF001
                app.processEvents()

        dlg = page._step_dialog              # noqa: SLF001 - 出图时藏弹框露出右卡
        if dlg is not None:
            dlg.hide()
        page._sync_image_card()              # noqa: SLF001
        app.processEvents()

        print("图卡标题:", page.image_caption.text())
        print("框/圆说明:", page.target_status.text())
        path = os.path.join(out_dir, "01_作业台_多框多圆.png")
        win.grab().save(path)
        print(path)
        return 0
    finally:
        win.shutdown()
        win.close()
        win.deleteLater()
        app.processEvents()
        stack.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
