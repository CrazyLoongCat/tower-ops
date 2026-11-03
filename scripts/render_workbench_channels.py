"""DEC-051 作业台右卡「相机通道页签」离屏出图（界面证据，2 张）。

流程（全离线，不接设备）：
1. 临时 devices.yaml 启用 DS86 + D405-1（S100 通道 cam1），D405-2~4 不启用；
2. 临时执行方案：抓拍 DS86 → 抓拍 D405-1 → 视觉判定（DS86 帧）；
3. 跑到视觉判定待确认：页签自动跟随本步视觉相机（DS86），框/圆可调
   → `01_作业台_相机页签_DS86.png`；
4. 切到 D405-1 页签：只读预览，提示不能在此画框/圆
   → `02_作业台_相机页签_D405只读.png`。

只用于出图；不写仓库种子/不写用户配置。用法（tower-ops 目录）：
    QT_QPA_PLATFORM=offscreen python scripts/render_workbench_channels.py <out_dir>
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


def _write_device_config(path: str) -> None:
    """临时设备配置：只启用 DS86 + D405-1（与现场相机通道配置同口径）。"""
    from application.services.device_config import (
        CameraEntry, DeviceConfig, DeviceConfigStore)

    store = DeviceConfigStore(path, seed_if_missing=False)
    store._config = DeviceConfig(cameras=[          # noqa: SLF001 - 出图脚本构造
        CameraEntry("ds86_01", "DS86 (随臂)", enabled=True, confirmed=True),
        CameraEntry("d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
                    host="192.168.2.117", port=5555, channel_id="cam1"),
        CameraEntry("d405_02", "D405-2", "zmq", enabled=False, confirmed=False,
                    host="192.168.2.117", port=5555, channel_id="cam2"),
        CameraEntry("d405_03", "D405-3", "zmq", enabled=False, confirmed=False),
        CameraEntry("d405_04", "D405-4", "zmq", enabled=False, confirmed=False),
    ])
    store.save()


def main(argv: list[str]) -> int:
    out_dir = argv[0] if argv else os.path.join(_ROOT, "runtime", "ui_shots")
    os.makedirs(out_dir, exist_ok=True)

    from PySide6.QtWidgets import QApplication

    from application.bootstrap.composition import build_offline_stack
    from application.presentation.app import MainWindow, UiContext
    from application.presentation.theme import THEME
    from application.presentation.viewmodel import WorkbenchViewModel

    tmp = tempfile.mkdtemp(prefix="wb_channels_")
    cfg_path = os.path.join(tmp, "devices.yaml")
    _write_device_config(cfg_path)
    stack = build_offline_stack(db_path=os.path.join(tmp, "r.db"),
                                runtime_dir=tmp, recipe_path=RECIPE,
                                device_config_path=cfg_path)
    stack.recipes.draft({
        "recipe_id": "演示_多相机页签", "version": "v1", "state": "draft",
        "sequence": [
            {"id": "cap_ds86", "type": "camera.capture", "camera_ref": "ds86_01"},
            {"id": "cap_d405", "type": "camera.capture", "camera_ref": "d405_01"},
            {"id": "det", "type": "vision.run",
             "input_from": {"step": "cap_ds86", "output": "frame"},
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
        win._select("workbench")             # noqa: SLF001 - 同一代码库内驱动
        win.refresh_all()
        page = win.pages["workbench"]
        vm.select_recipe("演示_多相机页签")
        vm.start_current()
        vm.tick()                            # 抓拍 DS86 完成
        page.refresh()
        page._on_step_choice("确认")         # noqa: SLF001
        vm.tick()                            # 抓拍 D405-1 完成
        page.refresh()
        page._on_step_choice("确认")         # noqa: SLF001
        vm.tick()                            # 视觉判定完成，等视觉步骤确认
        page.refresh()
        app.processEvents()

        tabs = page.channel_tabs
        names = [tabs.tabText(i) for i in range(tabs.count())]
        print("页签:", names)
        print("当前页签:", page._selected_camera_id())  # noqa: SLF001
        print("图卡标题:", page.image_caption.text())
        print("框/圆说明:", page.target_status.text())
        dlg = page._step_dialog              # noqa: SLF001 - 出图时藏弹框露出右卡
        if dlg is not None:
            dlg.hide()
        page._sync_image_card()              # noqa: SLF001
        app.processEvents()
        path1 = os.path.join(out_dir, "01_作业台_相机页签_DS86.png")
        win.grab().save(path1)
        print(path1)

        # 切到 D405-1 页签：只读预览（不出现框/圆调整）
        for i in range(tabs.count()):
            if tabs.tabData(i) == "d405_01":
                tabs.setCurrentIndex(i)      # 发 currentChanged，模拟操作员点击
                break
        app.processEvents()
        print("切换后页签:", page._selected_camera_id())  # noqa: SLF001
        print("图卡标题:", page.image_caption.text())
        print("框/圆说明:", page.target_status.text())
        path2 = os.path.join(out_dir, "02_作业台_相机页签_D405只读.png")
        win.grab().save(path2)
        print(path2)
        return 0
    finally:
        win.shutdown()
        win.close()
        win.deleteLater()
        app.processEvents()
        stack.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
