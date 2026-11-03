"""把全部导航页面与操作台监控弹窗离屏渲染成 PNG（界面验证证据用）。

用法（tower-ops 目录）：
    python scripts/render_ui.py out/                 # 各页截图 + 拼图 ui_all.png
    QT_QPA_PLATFORM=offscreen python scripts/render_ui.py out/   # 无显示环境

输出：导航页面 PNG（1440x1024）、monitor_1/2/4.png 与 ui_all.png（拼图）。
只用离线栈 (Fake 硬件 + IPC stub), 不接触真实设备。
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
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QFontDatabase, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"需要 PySide6: {exc}\n安装: python -m pip install PySide6")

from application.bootstrap.composition import build_offline_stack
from application.presentation.app import NAV_PAGES, MainWindow, UiContext
from application.presentation.theme import THEME
from application.presentation.viewmodel import WorkbenchViewModel
from application.services.vision_detections import VisionDetection, VisionDetectionStore

RECIPE = os.path.join(_ROOT, "configs", "recipes", "ds86_two_station_example.yaml")
# 渲染演示用的命名检测（**只写进本次渲染的临时目录**；仓库种子 configs/vision/detections.yaml 保持为空）
DEMO_DETECTION = "演示：DS86 圆孔检测"
DEMO_PIPELINE = "approved_ds86_circle_pipeline"


def main(argv: list[str]) -> int:
    out_dir = argv[0] if argv else os.path.join(_ROOT, "runtime", "ui_shots")
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp()
    # 一条命名视觉检测落在**渲染临时目录**（默认的 runtime/ 里是空的）；名称与来源都写明是演示。
    det_path = os.path.join(tmp, "detections.yaml")
    dstore = VisionDetectionStore(det_path)
    dstore.load()
    dstore.upsert(VisionDetection(detection_id=DEMO_DETECTION, camera_ref="ds86_01",
                                  pipeline_ref=DEMO_PIPELINE,
                                  source="render_ui 离屏演示（非现场值）"))
    dstore.save()
    # 设备配置页渲染用仓库里 ship 的 configs/devices/devices.yaml (与 main.py 一致),
    # 否则会渲染成中性种子, 与程序实际显示不符。
    stack = build_offline_stack(
        db_path=os.path.join(tmp, "r.db"), runtime_dir=tmp, recipe_path=RECIPE,
        device_config_path=os.path.join(_ROOT, "configs", "devices", "devices.yaml"),
        motion_config_path=os.path.join(_ROOT, "configs", "motion", "motion.yaml"),
        # DEC-069：参数配置页按现场引导配置渲染（tcp/深度/找圆都是现场值）
        guidance_config_path=os.path.join(_ROOT, "configs", "motion", "guidance.yaml"),
        vision_detections_path=det_path)
    app = QApplication([])
    # Windows offscreen 插件有时不枚举系统字体，显式载入以避免中文全部变成方框。
    if not QFontDatabase.families():
        font_path = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", "msyh.ttc")
        if os.path.isfile(font_path):
            QFontDatabase.addApplicationFont(font_path)
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

    shots: list[tuple[str, QPixmap]] = []

    def grab(key: str, name: str, prepare=None) -> None:
        win._select(key)          # noqa: SLF001 - 同一代码库内驱动
        if prepare is not None:
            prepare()
        app.processEvents()
        pm = win.grab()
        pm.save(os.path.join(out_dir, f"{name}.png"))
        shots.append((name, pm))
        print("saved", name)

    for label, key in NAV_PAGES:
        grab(key, key)

    montage(shots, os.path.join(out_dir, "ui_all.png"))
    print("saved ui_all.png")

    # 操作台监控证据：只复用本脚本离线栈中已打开的 Fake 会话。
    console = win.pages["console"]
    channels = list(console.camera_buttons)
    if channels:
        console.open_monitor(channels[0])
        monitor = console.monitor
        monitor._timer.stop()
        if monitor._future is not None:
            monitor._future.result(timeout=5)
        monitor.refresh()
        for count in (1, 2, 4):
            monitor.layout_pick.setCurrentIndex(monitor.layout_pick.findData(count))
            for tile, cid in zip(monitor.visible_tiles(), channels):
                tile.pick.setCurrentIndex(tile.pick.findData(cid))
            monitor.refresh()
            app.processEvents()
            monitor.grab().save(os.path.join(out_dir, f"monitor_{count}.png"))
            print("saved", f"monitor_{count}")
        monitor.close()

    # 配方编辑态证据 (DEC-024): **真实示例配方**复制成一份内存草稿(不落盘, 仍是离线示例,
    # 发布被闸住), 给运动步骤按步骤绑定一条真实运动方案, 把视觉步骤改成引用命名视觉检测。
    # 演示草稿只在进程内存里, 渲染结束即消失 —— 图上是"编辑完还没发"的样子。
    page = win.pages["recipes"]
    ed = page.editor

    def _demo_recipe() -> None:
        from application.contracts.enums import NodeType
        rid = "ds86_bound_demo"
        if not ed.exists(rid):
            ed.duplicate("ds86_two_station_example", rid)
            # 自定义操作 (custom.call): 选一个方法 + 勾选多台设备（同时下发、等全部完成）
            if not any(s.type is NodeType.CUSTOM_CALL for s in ed.model.sequence):
                sid = ed.add_step(NodeType.CUSTOM_CALL)
                devs = ed.devices()[:2]
                if devs:
                    ed.set_step_devices(sid, devs)
        plans = ed.motion_plans()
        if plans:
            ed.set_motion_plan("move", plans[0])          # 按步骤绑方案：这一步按方案回放
        if ed.detections():
            ed.set_detection("detect", ed.detections()[0][0])  # 视觉判定引用"调试完另存"的检测
        page._sel_step = "move"                           # noqa: SLF001
        page._reload()                                    # noqa: SLF001

    grab("recipes", "recipes_bound_plan", _demo_recipe)

    def _select_detect() -> None:
        page._sel_step = "detect"                         # noqa: SLF001
        page._reload()                                    # noqa: SLF001

    grab("recipes", "recipes_bound_detection", _select_detect)

    def _select_custom() -> None:
        from application.contracts.enums import NodeType
        custom = next((s.id for s in ed.model.sequence
                       if s.type is NodeType.CUSTOM_CALL), None)
        if custom:
            page._sel_step = custom                         # noqa: SLF001
            page._reload()                                  # noqa: SLF001

    grab("recipes", "recipes_bound_custom", _select_custom)
    print(f"demo: plan={ed.motion_plans()[:1]} detection={ed.detections()[:1]} "
          f"publish_ok={ed.can_publish()[0]}")

    # 运行中态 (作业台)：先把配方选择切回离线示例，别让演示草稿影响这一张。
    ed.select("ds86_two_station_example")
    page._reload()                                        # noqa: SLF001
    vm.start_current()
    vm.tick()                                             # 单次推进: 仍处"执行中"
    win.refresh_all()
    grab("workbench", "workbench_running")
    grab("console", "console_running")

    # 退出顺序照"Qt 关闭顺序"那条坑: 停定时器 -> 关窗 -> deleteLater -> 跑事件 -> 关栈。
    # 少这步图还是出得来, 但进程会在解释器退出时崩 (rc=139) —— 出证据的脚本更该看退出码。
    win.shutdown()
    win.close()
    win.deleteLater()
    app.processEvents()
    stack.close()
    return 0


def montage(shots, path: str, cols: int = 3, cw: int = 720, ch: int = 512) -> None:
    rows = (len(shots) + cols - 1) // cols
    canvas = QPixmap(cw * cols, ch * rows)
    canvas.fill(Qt.GlobalColor.white)
    p = QPainter(canvas)
    for i, (_name, pm) in enumerate(shots):
        x = (i % cols) * cw
        y = (i // cols) * ch
        p.drawPixmap(x, y, pm.scaled(cw, ch, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))
    p.end()
    canvas.save(path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
