"""运动方案页（移植旧工程 `ui/panels/motion_plan_panel.py`）。

页内三个子 tab，**编辑与调用隔离**（与旧工程一致）：

- **路径规划**：**示教点**（读取机械臂当前关节 → 登记为命名关节目标，写回
  `configs/site/joint_targets.yaml`，执行方案步骤引用它；DEC-067）、方案管理
  （新建/重命名/删除）、新点默认方式、速度%/交融半径%/单点超时、
  路点表（每行独立选方式，可混用 ``movel``/``movej``、**套筒偏移**与**工具系偏移**）、
  读取当前位姿/关节、增删行/上下移/应用。``movel`` 行界面填 **mm + rad**，内部自动换算成 **m** 下发；
  ``movej`` 行填**度**；**套筒偏移行**前 3 列填 Bolt 工具系 **dx/dy/dz(mm)**（可正负；
  历史用法 Y：+ = 进入/前进，− = 脱离/后退；DEC-139 起 X/Z 也放开），后 3 列只读；
  **工具系偏移行**前 3 列填 **dx/dy/dz(mm)**（Bolt 工具系，可正负），后 3 列只读——执行时按当前位姿
  算目标位姿并走**一条 movel 直线**（例如先 Z+5 抬升、再 (0,+100,−5) 斜回目标，DEC-060）。
- **路径实施**：运行方案（单选）、运行/暂停/继续/停止、**引导定位**（粗定位/精定位
  两个按钮，各跑对应段，每次人工确认后移动）、**套筒前进/后退**（Bolt 工具系 Y 轴；
  与方案里的套筒偏移同一动作）；不再显示执行日志（DEC-107）。
- **手动调整**（DEC-139/145）：Bolt 工具系 **X/Y/Z 正向/负向单步点动**（步长 mm + 速度%），
  与套筒偏移同一底层原语（``move_offset``）；右侧实时显示**工具位姿 3D 示意**
  （只读，约 1s 刷新，数值含 mm / rad）。

**运动闸**：未开运动闸时所有运动按钮禁用并提示原因；读取类（读位姿/读关节）不受限。
数据来自 `configs/motion/motion.yaml`（含旧工程**实测导入**的方案），每条带来源标注。
"""
from __future__ import annotations

import threading


from application.presentation.components import Card, EmptyHint, KV
from application.presentation.components.robot_pose_view import RobotPoseView
from application.presentation.components.target_layout_view import TargetLayoutImageView
from application.presentation.timefmt import to_beijing_text
from application.services.vision_layout import (
    compute_initial_layout, find_circle_in_rect,
)
from application.services.motion_config import (
    POINT_MODES,
    SOCKET_MODE_LABELS,
    TOOL_OFFSET_MODE_LABELS,
    VALID_MODES,
    MotionConfigStore,
    MotionPlan,
    Waypoint,
    is_socket_mode,
    is_tool_offset_mode,
)

try:  # pragma: no cover
    from PySide6.QtCore import Qt, QTimer, Signal
    from PySide6.QtWidgets import (
        QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
        QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
        QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton,
        QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

_MODE_LABELS = {
    "movel": "直线 movel",
    "movej": "关节 movej",
    **SOCKET_MODE_LABELS,   # 套筒偏移（+ = 进入/前进，− = 脱离/后退）
    **TOOL_OFFSET_MODE_LABELS,   # 工具系偏移（dx/dy/dz mm，近似一条直线 movel）
}


def _motion_store(ctx) -> MotionConfigStore:
    """取栈里的运动配置；没有时用内存 store（不写盘）。"""
    store = getattr(getattr(ctx, "stack", None), "motion_config", None)
    if isinstance(store, MotionConfigStore):
        return store
    return MotionConfigStore(None, seed_if_missing=False)


# 示教点登记（DEC-067）用的对话框入口：统一成模块级函数，便于测试 monkeypatch、不阻塞自动化。
def _ask_text(parent, title: str, label: str, text: str = "") -> tuple[str, bool]:
    from PySide6.QtWidgets import QInputDialog
    return QInputDialog.getText(parent, title, label, text=text)


def _confirm(parent, title: str, text: str) -> bool:
    from PySide6.QtWidgets import QMessageBox
    return QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    ) == QMessageBox.StandardButton.Yes


def _warn_dialog(parent, title: str, text: str) -> None:
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.warning(parent, title, text)


def _info_dialog(parent, title: str, text: str) -> None:
    from PySide6.QtWidgets import QMessageBox
    QMessageBox.information(parent, title, text)


if GUI_REQUIRED:

    class MotionPage(QWidget):
        # DEC-116：引导/方案线程里要求确认时，经信号排队到 GUI 线程弹窗。
        # 在工作线程里直接 QMessageBox(self)（self 属于 GUI 线程）会报
        # "setParent: new parent is in a different thread" 并崩溃（用户实测）。
        _confirm_request = Signal(str, str, object, object, object)

        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self._gui_thread_ident = threading.get_ident()
            self._confirm_request.connect(self._confirm_on_gui)
            self.store = _motion_store(ctx)
            self.runner = getattr(ctx.stack, "motion", None)
            # 示教点登记（DEC-067）：只有 production 装配才有；离线为 None（按钮禁用）
            self.teach_store = getattr(ctx.stack, "teach_points", None)
            # DEC-109：引导定位的「视觉方案」与人工框/圆（画面过期后不重复使用）
            self._guide_frame = None          # 最近一次「单次视觉检测」的帧
            self._guide_frame_valid = False   # 机械臂移动过 -> 旧帧不再作为定位依据
            self._guide_layout_note = ""
            self._guide_frame_cam = ""        # DEC-137：该帧来自哪台相机
            self._image_popup = None
            self._popup_open = False

            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            head = QHBoxLayout(); head.setSpacing(12)
            self.kv_robot = KV("机器人", "-")
            self.kv_gate = KV("运动闸", "-")
            self.kv_state = KV("执行状态", "空闲")
            for kv in (self.kv_robot, self.kv_gate, self.kv_state):
                head.addWidget(kv)
            head.addStretch(1)
            self.btn_refresh = QPushButton("刷新位姿/关节"); self.btn_refresh.setObjectName("Ghost")
            self.btn_refresh.clicked.connect(self.refresh)
            head.addWidget(self.btn_refresh)
            root.addLayout(head)

            self.tabs = QTabWidget()
            self.tabs.addTab(self._build_planning(), "路径规划")
            self.tabs.addTab(self._build_execution(), "路径实施")
            self.tabs.addTab(self._build_manual(), "手动调整")
            self.tabs.currentChanged.connect(lambda *_a: self._refresh_pose_view())
            root.addWidget(self.tabs, 1)

            self._timer = QTimer(self)
            self._timer.setInterval(250)
            self._timer.timeout.connect(self._tick)
            self._timer.start()
            self.refresh()

        # ---------------- 路径规划 ----------------
        def _build_planning(self) -> QWidget:
            page = QWidget()
            lay = QVBoxLayout(page)

            # DEC-067：示教点登记放最上面（用户要求「路径规划页顶部加分组」）
            lay.addWidget(self._build_teach_group())

            row = QHBoxLayout()
            row.addWidget(QLabel("方案:"))
            self.plan_combo = QComboBox(); self.plan_combo.setMinimumWidth(260)
            self.plan_combo.currentIndexChanged.connect(lambda *_a: self._load_plan())
            row.addWidget(self.plan_combo)
            for text, slot in (("新建", self._new_plan), ("重命名", self._rename_plan),
                               ("删除", self._delete_plan)):
                btn = QPushButton(text); btn.setObjectName("Ghost"); btn.clicked.connect(slot)
                row.addWidget(btn)
            row.addStretch(1)
            lay.addLayout(row)

            row2 = QHBoxLayout()
            row2.addWidget(QLabel("新点方式:"))
            self.new_mode = QComboBox()
            for mode in POINT_MODES:                 # 方案默认方式=点位方式(movel/movej)
                self.new_mode.addItem(_MODE_LABELS[mode], mode)
            row2.addWidget(self.new_mode)
            row2.addWidget(QLabel("速度%:"))
            self.speed_spin = QSpinBox(); self.speed_spin.setRange(1, 100)
            row2.addWidget(self.speed_spin)
            row2.addWidget(QLabel("交融半径%:"))
            self.blend_spin = QSpinBox(); self.blend_spin.setRange(0, 100)
            row2.addWidget(self.blend_spin)
            row2.addWidget(QLabel("单点超时(s):"))
            self.timeout_spin = QSpinBox(); self.timeout_spin.setRange(1, 3600)
            row2.addWidget(self.timeout_spin)
            row2.addStretch(1)
            lay.addLayout(row2)

            self.wp_table = QTableWidget(0, 8)
            self.wp_table.setHorizontalHeaderLabels(
                ["方式", "x / j1", "y / j2", "z / j3", "rx / j4", "ry / j5",
                 "rz / j6", "名称"])
            self.wp_table.verticalHeader().setVisible(False)
            self.wp_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            lay.addWidget(self.wp_table, 1)

            row3 = QHBoxLayout()
            for text, slot in (("读取当前位姿(movel)", self._read_pose),
                               ("读取当前关节(movej)", self._read_joint),
                               ("添加行", self._add_row), ("删除行", self._delete_row),
                               ("上移", self._move_up), ("下移", self._move_down),
                               ("应用", self._apply)):
                btn = QPushButton(text)
                btn.setObjectName("Primary" if text == "应用" else "Ghost")
                btn.clicked.connect(slot)
                row3.addWidget(btn)
            row3.addStretch(1)
            lay.addLayout(row3)

            hint = QLabel("直线行按 x,y,z(mm) + rx,ry,rz(rad) 填（内部换算为 m）；"
                          "关节行按 j1..j6(°) 填；套筒偏移行前 3 列填 Bolt 工具系 "
                          "dx/dy/dz(mm，可正负；历史用法 Y：+ = 进入/前进，− = 脱离/后退)，"
                          "执行时用引导速度逐轴步进；工具系偏移行前 3 列填 dx/dy/dz(mm)，"
                          "执行时走一条 movel 直线。读位姿/读关节需机械臂已连接。")
            hint.setObjectName("SubLabel"); hint.setWordWrap(True)
            lay.addWidget(hint)
            return page

        # ---------------- 示教点（DEC-067） ----------------
        def _build_teach_group(self) -> QWidget:
            g = QGroupBox("示教点（读取机械臂当前关节登记；执行方案「关节目标」引用）")
            lay = QVBoxLayout(g)
            row = QHBoxLayout()
            self.btn_teach_add = QPushButton("读取当前关节并登记")
            self.btn_teach_add.clicked.connect(self._register_teach_point)
            row.addWidget(self.btn_teach_add)
            self.btn_teach_del = QPushButton("删除选中")
            self.btn_teach_del.setObjectName("Ghost")
            self.btn_teach_del.clicked.connect(self._delete_teach_point)
            row.addWidget(self.btn_teach_del)
            row.addStretch(1)
            lay.addLayout(row)

            self.teach_table = QTableWidget(0, 8)
            self.teach_table.setHorizontalHeaderLabels(
                ["名称", "j1(°)", "j2(°)", "j3(°)", "j4(°)", "j5(°)", "j6(°)",
                 "示教时间（北京时间）"])
            self.teach_table.verticalHeader().setVisible(False)
            self.teach_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.teach_table.setSelectionBehavior(
                QAbstractItemView.SelectionBehavior.SelectRows)
            self.teach_table.setSelectionMode(
                QAbstractItemView.SelectionMode.SingleSelection)
            head = self.teach_table.horizontalHeader()
            for col in range(7):
                head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
            head.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
            self.teach_table.setMaximumHeight(140)
            lay.addWidget(self.teach_table)

            self.teach_note = QLabel("")
            self.teach_note.setWordWrap(True)
            self.teach_note.setObjectName("SubLabel")
            lay.addWidget(self.teach_note)
            return g

        def _refresh_teach(self) -> None:  # pragma: no cover - Qt
            self.teach_table.setRowCount(0)
            store = self.teach_store
            if store is None:
                self.teach_note.setText(
                    "离线模式不能登记示教点：示教值必须来自真实机械臂的当前关节"
                    "（离线只有假机械臂，读数不能当示教值）。")
                return
            entries = list(store.entries())
            for e in entries:
                r = self.teach_table.rowCount()
                self.teach_table.insertRow(r)
                cells = [e.target_id] + [f"{v:.3f}" for v in e.joint_deg] + \
                        [to_beijing_text(e.taught_at)]
                for c, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    self.teach_table.setItem(r, c, item)
            if entries:
                self.teach_note.setText(
                    f"已登记 {len(entries)} 个示教点；文件 {store.path}。"
                    "登记值 = 已连接机械臂的当前关节（deg，恰好 6 项），confirmed=true，"
                    "保存后立即生效（执行方案「关节目标」下拉无需重启）。"
                    "删除后引用它的执行方案会校验失败。")
            else:
                self.teach_note.setText(
                    "还没有登记示教点：把机械臂移到目标位置，点「读取当前关节并登记」。"
                    f"保存到 {store.path}，实时生效。")

        def _next_teach_id(self) -> str:
            ids = {e.target_id for e in self.teach_store.entries()} \
                if self.teach_store is not None else set()
            n = 1
            while f"teach_{n:02d}" in ids:
                n += 1
            return f"teach_{n:02d}"

        def _register_teach_point(self) -> None:  # pragma: no cover - Qt
            store = self.teach_store
            if store is None:
                _warn_dialog(self, "登记示教点",
                             "离线模式不能登记示教点：示教值必须来自真实机械臂的当前关节。")
                return
            joint = self._call_arm("get_current_joint")
            if not joint or len(joint) != 6:
                _info_dialog(self, "读取关节",
                             "机械臂未连接或读取当前关节失败，没有登记任何示教点。")
                return
            name, ok = _ask_text(self, "登记示教点",
                                 "示教点名称（执行方案「关节目标」引用的键）:",
                                 text=self._next_teach_id())
            if not ok or not name.strip():
                return
            tid = name.strip()
            exists = store.find(tid) is not None
            if exists and not _confirm(
                    self, "覆盖示教点",
                    f"已存在示教点 {tid!r}，用机械臂当前关节值覆盖它？"):
                return
            try:
                entry = store.add(tid, tuple(float(j) for j in joint),
                                  overwrite=exists)
            except Exception as exc:  # noqa: BLE001 - 如实报错，不吞
                _warn_dialog(self, "登记失败", str(exc))
                return
            self._refresh_teach()
            joints = ", ".join(f"{v:.2f}" for v in entry.joint_deg)
            _info_dialog(self, "登记成功",
                         f"示教点 {entry.target_id} 已保存并立即生效。\n"
                         f"关节: [{joints}]°（deg）\n"
                         f"文件: {store.path}")

        def _delete_teach_point(self) -> None:  # pragma: no cover - Qt
            store = self.teach_store
            if store is None:
                return
            r = self.teach_table.currentRow()
            if r < 0:
                _info_dialog(self, "删除示教点", "请先在表里选中一个示教点。")
                return
            item = self.teach_table.item(r, 0)
            tid = item.text() if item is not None else ""
            if not tid:
                return
            if not _confirm(self, "删除示教点",
                            f"确认删除示教点 {tid!r}？\n"
                            "引用它的执行方案之后会校验失败（需改引用或重新登记）。"):
                return
            try:
                store.remove(tid)
            except Exception as exc:  # noqa: BLE001
                _warn_dialog(self, "删除失败", str(exc))
                return
            self._refresh_teach()

        # ---------------- 路径实施 ----------------
        def _build_execution(self) -> QWidget:
            page = QWidget()
            lay = QVBoxLayout(page)

            self.run_group = QGroupBox("运行方案（单选）")
            self.run_grid = QGridLayout(self.run_group)
            lay.addWidget(self.run_group)

            row = QHBoxLayout()
            self.btn_run = QPushButton("运行"); self.btn_run.setObjectName("Primary")
            self.btn_run.clicked.connect(self._run)
            self.btn_pause = QPushButton("暂停"); self.btn_pause.setObjectName("Ghost")
            self.btn_pause.clicked.connect(self._pause)
            self.btn_resume = QPushButton("继续"); self.btn_resume.setObjectName("Ghost")
            self.btn_resume.clicked.connect(self._resume)
            self.btn_stop = QPushButton("停止"); self.btn_stop.setObjectName("Danger")
            self.btn_stop.clicked.connect(self._stop)
            for b in (self.btn_run, self.btn_pause, self.btn_resume, self.btn_stop):
                b.setMinimumHeight(34); row.addWidget(b)
            row.addStretch(1)
            lay.addLayout(row)

            guide = QGroupBox("引导定位（深度引导：本地抓图→找圆→偏差，每次人工确认）")
            gh = QHBoxLayout(guide)
            # 左：操作按钮 + 检测结果；右：可画框/画圆的视频画面（DEC-107 / DEC-109）。
            ops = QVBoxLayout()
            row_speed = QHBoxLayout()
            row_speed.addWidget(QLabel("速度%:"))
            self.guide_speed = QSpinBox(); self.guide_speed.setRange(1, 100)
            # DEC-118：默认跟「参数配置 → 运动全局 → 引导移动速度」一致。
            # 此前 QSpinBox 初值 0 被 setRange(1,100) 夹成 1%，引导移动几乎看不出来
            # （move_offset 以 1% 速度走几毫米，还可能在 30s 内到位超时）。
            _prof = self.store.config.profile
            self.guide_speed.setValue(
                max(1, min(100, int(_prof.guide_move_speed or 10))))
            row_speed.addWidget(self.guide_speed)
            row_speed.addStretch(1)
            ops.addLayout(row_speed)
            # DEC-137：引导相机可选（默认 DS86；D405 USB 等已启用相机同样可做粗/精定位）
            row_cam = QHBoxLayout()
            row_cam.addWidget(QLabel("引导相机:"))
            self.guide_camera = QComboBox()
            self.guide_camera.setMinimumWidth(130)
            self.guide_camera.setToolTip(
                "粗定位/精定位/单次视觉检测用哪台相机：只列本次运行已连接的相机通道"
                "（设备配置里启用并连接成功的）；默认 DS86，D405 等 USB 相机也可用")
            self.guide_camera.currentIndexChanged.connect(
                lambda *_a: self._on_guide_camera_changed())
            row_cam.addWidget(self.guide_camera, 1)
            ops.addLayout(row_cam)
            # DEC-109：视觉方案 = 视觉调试页「另存为检测」保存的命名检测；
            # 选定后「单次视觉检测」按该检测的找圆调试参数现算框/圆。
            row_det = QHBoxLayout()
            row_det.addWidget(QLabel("视觉方案:"))
            self.guide_detection = QComboBox()
            self.guide_detection.setMinimumWidth(150)
            self.guide_detection.setToolTip(
                "选择视觉调试页保存的命名检测：按它的找圆调试参数现算框/圆；"
                "选「自动找圆」则用引导配置里的深度/找圆参数")
            self.guide_detection.currentIndexChanged.connect(
                lambda *_a: self._on_guide_detection_changed())
            row_det.addWidget(self.guide_detection, 1)
            ops.addLayout(row_det)
            self.btn_detect = QPushButton("单次视觉检测"); self.btn_detect.setObjectName("Ghost")
            self.btn_detect.clicked.connect(self._detect)
            ops.addWidget(self.btn_detect)
            self.btn_guide_coarse = QPushButton("粗定位"); self.btn_guide_coarse.setObjectName("Ghost")
            self.btn_guide_coarse.clicked.connect(lambda: self._guide("coarse"))
            ops.addWidget(self.btn_guide_coarse)
            self.btn_guide_fine = QPushButton("精定位"); self.btn_guide_fine.setObjectName("Ghost")
            self.btn_guide_fine.clicked.connect(lambda: self._guide("fine"))
            ops.addWidget(self.btn_guide_fine)
            self.btn_guide_cancel = QPushButton("取消引导"); self.btn_guide_cancel.setObjectName("Ghost")
            self.btn_guide_cancel.clicked.connect(self._cancel_guide)
            ops.addWidget(self.btn_guide_cancel)
            self.kv_xyz = KV("相机系 XYZ", "-")
            self.kv_coarse = KV("粗定位偏差 (x,z) mm", "-")
            self.kv_fine = KV("精定位偏差 (x,z) mm", "-")
            self.kv_detect = KV("检测", "-")
            for w in (self.kv_xyz, self.kv_coarse, self.kv_fine, self.kv_detect):
                ops.addWidget(w)
            ops.addStretch(1)
            gh.addLayout(ops, 1)
            # 右：画面 + 画框/画圆/删除选中工具条 + 状态说明（DEC-109）
            # DEC-119：整块包成 image_card，可像作业台/操作台那样弹出独立窗口。
            right = QVBoxLayout()
            self.image_card = QWidget()
            _card_lay = QVBoxLayout(self.image_card)
            _card_lay.setContentsMargins(0, 0, 0, 0)
            _card_lay.setSpacing(6)
            tbar = QHBoxLayout(); tbar.setSpacing(6)
            self.btn_draw_box = QPushButton("画框"); self.btn_draw_box.setCheckable(True)
            self.btn_draw_box.setEnabled(False)
            self.btn_draw_box.setToolTip("在彩色图上按住左键拖出一个框")
            self.btn_draw_box.toggled.connect(self._on_guide_draw_box)
            self.btn_draw_circle = QPushButton("画圆"); self.btn_draw_circle.setCheckable(True)
            self.btn_draw_circle.setEnabled(False)
            self.btn_draw_circle.setToolTip("在已有框内按住左键拖动画圆（以起点为圆心）")
            self.btn_draw_circle.toggled.connect(self._on_guide_draw_circle)
            self.btn_del_target = QPushButton("删除选中")
            self.btn_del_target.setEnabled(False)
            self.btn_del_target.setToolTip("删除选中的框；若选中的是框内的圆，只删圆")
            self.btn_del_target.clicked.connect(
                lambda: self.view_result.delete_selected())
            self.btn_save_img = QPushButton("保存图片")
            self.btn_save_img.setEnabled(False)
            self.btn_save_img.setToolTip(
                "保存当前引导帧：彩色 PNG + 原始深度（npy / 16位PNG / JSON 元数据）+ 伪彩预览；"
                "均不含显示旋转与预览算法")
            self.btn_save_img.clicked.connect(self._save_guide_image)
            self.btn_popup = QPushButton("弹窗")
            self.btn_popup.setCheckable(True)
            self.btn_popup.setToolTip("把引导画面弹出为独立窗口（成像与画框/画圆不变）；再点一次关闭")
            self.btn_popup.toggled.connect(self._toggle_image_popup)
            for b in (self.btn_draw_box, self.btn_draw_circle, self.btn_del_target):
                tbar.addWidget(b)
            tbar.addWidget(self.btn_save_img)
            tbar.addWidget(self.btn_popup)
            tbar.addStretch(1)
            _card_lay.addLayout(tbar)
            self.view_result = TargetLayoutImageView("尚未检测")
            # DEC-119：默认引导相机 DS86 画面按显示旋转 270°（坐标仍是原图）；
            # DEC-137：换成其它相机（如 D405 USB）时在 _apply_guide_frame 按帧改回 0°。
            self.view_result.set_display_rotation(270)
            self.view_result.mode_changed.connect(self._sync_guide_mode_buttons)
            # DEC-118：组件只发信号，框/圆由页面落进本地布局（与作业台同一约定）
            self.view_result.box_requested.connect(self._on_guide_box_requested)
            self.view_result.circle_drawn.connect(self._on_guide_circle_drawn)
            self.view_result.message.connect(
                lambda text: self.guide_status.setText(text or ""))
            self.view_result.layout_changed.connect(self._on_guide_layout_changed)
            self.view_result.selection_changed.connect(
                lambda *_a: self._on_guide_layout_changed())
            _card_lay.addWidget(self.view_result, 1)
            self.guide_status = QLabel(
                "选择视觉方案 →「单次视觉检测」→ 可在图上画框/画圆并拖动调整；"
                "粗定位/精定位用选中框的圆（没有则用第一个有圆的框）。")
            self.guide_status.setObjectName("SubLabel")
            self.guide_status.setWordWrap(True)
            _card_lay.addWidget(self.guide_status)
            right.addWidget(self.image_card, 1)
            self._image_slot = right
            gh.addLayout(right, 2)
            lay.addWidget(guide, 1)

            sock = QGroupBox("套筒偏移（Bolt 工具系 Y 轴：前进/进入=Y+，后退/脱离=Y−；"
                             "取套筒=「进入/脱离」，偏移微调=「前进/后退」，同一动作）")
            sh = QHBoxLayout(sock)
            sh.addWidget(QLabel("距离 mm:"))
            self.sock_mm = QDoubleSpinBox(); self.sock_mm.setRange(0.1, 200.0)
            self.sock_mm.setValue(5.0)
            sh.addWidget(self.sock_mm)
            sh.addWidget(QLabel("速度%:"))
            self.sock_speed = QSpinBox(); self.sock_speed.setRange(1, 100); self.sock_speed.setValue(10)
            sh.addWidget(self.sock_speed)
            self.btn_fwd = QPushButton("套筒前进"); self.btn_fwd.setObjectName("Ghost")
            self.btn_fwd.clicked.connect(lambda: self._socket(+1))
            sh.addWidget(self.btn_fwd)
            self.btn_back = QPushButton("套筒后退"); self.btn_back.setObjectName("Ghost")
            self.btn_back.clicked.connect(lambda: self._socket(-1))
            sh.addWidget(self.btn_back)
            sh.addStretch(1)
            lay.addWidget(sock)

            return page

        # ---------------- 手动调整：工具系点动 + 位姿示意（DEC-139/145） ----------------
        def _build_manual(self) -> QWidget:
            page = QWidget()
            lay = QVBoxLayout(page)
            split = QHBoxLayout()

            # 左：工具系点动（Bolt 工具系 X/Y/Z 正负方向，每次一个步长）
            g = QGroupBox("工具系点动（Bolt）")
            gl = QGridLayout(g)
            gl.setHorizontalSpacing(8)
            gl.setVerticalSpacing(8)
            head_row = QHBoxLayout()
            head_row.addWidget(QLabel("步长(mm):"))
            self.jog_step = QDoubleSpinBox()
            self.jog_step.setRange(0.1, 200.0)
            self.jog_step.setDecimals(1)
            self.jog_step.setSingleStep(0.5)
            self.jog_step.setValue(5.0)
            head_row.addWidget(self.jog_step)
            head_row.addSpacing(10)
            head_row.addWidget(QLabel("速度%:"))
            self.jog_speed = QSpinBox()
            self.jog_speed.setRange(1, 100)
            self.jog_speed.setValue(max(1, min(100, int(
                self.store.config.profile.guide_move_speed or 10))))
            head_row.addWidget(self.jog_speed)
            head_row.addStretch(1)
            gl.addLayout(head_row, 0, 0, 1, 3)

            self.jog_buttons: dict[tuple[str, int], QPushButton] = {}
            for i, axis in enumerate(("X", "Y", "Z"), start=1):
                gl.addWidget(QLabel(f"{axis} 轴"), i, 0)
                minus = QPushButton(f"{axis} −")
                minus.setObjectName("Ghost")
                minus.setFixedSize(64, 28)
                minus.clicked.connect(lambda _=False, a=axis: self._jog(a, -1))
                plus = QPushButton(f"{axis} +")
                plus.setObjectName("Primary")
                plus.setFixedSize(64, 28)
                plus.clicked.connect(lambda _=False, a=axis: self._jog(a, +1))
                gl.addWidget(minus, i, 1)
                gl.addWidget(plus, i, 2)
                self.jog_buttons[(axis, -1)] = minus
                self.jog_buttons[(axis, +1)] = plus
            gl.setColumnStretch(3, 1)
            gl.setRowStretch(4, 1)
            left = QVBoxLayout()
            left.addWidget(g)
            left.addStretch(1)
            split.addLayout(left, 0)

            # 右：工具位姿 3D 示意（只读，约 1s 刷新）
            pose_box = QGroupBox("工具位姿（实时）")
            pl = QVBoxLayout(pose_box)
            self.pose_view = RobotPoseView()
            pl.addWidget(self.pose_view, 1)
            right = QVBoxLayout()
            right.addWidget(pose_box, 1)
            split.addLayout(right, 1)

            lay.addLayout(split, 1)
            return page

        # ---------------- 相机画面弹窗（DEC-119） ----------------
        def _ensure_image_popup(self):
            popup = getattr(self, "_image_popup", None)
            if popup is None:
                from application.presentation.components.capture_popup import (
                    CapturePopup,
                )
                popup = CapturePopup(self.image_card, self._image_slot,
                                     parent=self.window(),
                                     title="引导定位 · 相机画面与框/圆")
                popup.finished.connect(self._on_popup_finished)
                self._image_popup = popup
            return popup

        def _on_popup_finished(self, *_a) -> None:  # pragma: no cover - Qt
            self._popup_open = False
            btn = getattr(self, "btn_popup", None)
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)

        def _toggle_image_popup(self, checked: bool) -> None:  # pragma: no cover - Qt
            self._ensure_image_popup()
            if checked:
                self._image_popup.attach()
                self._image_popup.show()
                self._image_popup.raise_()
                self._popup_open = True
            else:
                self._image_popup.close()
                self._popup_open = False

        # ---------------- 数据刷新 ----------------
        def refresh(self) -> None:  # pragma: no cover - Qt
            cfg = self.store.config
            keep = self.current_plan_id()
            self.plan_combo.blockSignals(True)
            self.plan_combo.clear()
            for plan in cfg.plans:
                self.plan_combo.addItem(plan.plan_id, plan.plan_id)
            idx = self.plan_combo.findData(keep)
            if idx >= 0:
                self.plan_combo.setCurrentIndex(idx)
            self.plan_combo.blockSignals(False)
            self._load_plan()
            self._rebuild_run_radios()
            self._rebuild_guide_detections()
            self._rebuild_guide_cameras()
            self._refresh_teach()
            self._refresh_status()

        def current_plan_id(self) -> str | None:
            return self.plan_combo.currentData()

        def _load_plan(self) -> None:  # pragma: no cover - Qt
            plan = self._current_plan()
            self.wp_table.setRowCount(0)
            if plan is None:
                return
            self.speed_spin.setValue(plan.speed)
            self.blend_spin.setValue(plan.blend)
            self.timeout_spin.setValue(plan.timeout)
            idx = self.new_mode.findData(plan.default_mode)
            if idx >= 0:
                self.new_mode.setCurrentIndex(idx)
            for wp in plan.waypoints:
                self._append_row(wp)

        def _current_plan(self) -> MotionPlan | None:  # pragma: no cover - Qt
            pid = self.current_plan_id()
            return None if pid is None else self.store.config.find_plan(pid)

        def _append_row(self, wp: Waypoint) -> None:  # pragma: no cover - Qt
            r = self.wp_table.rowCount()
            self.wp_table.insertRow(r)
            self._fill_row(r, wp)
            self._sync_row_cells(r)

        def _make_mode_combo(self, mode: str) -> QComboBox:  # pragma: no cover - Qt
            combo = QComboBox()
            for m in VALID_MODES:
                combo.addItem(_MODE_LABELS[m], m)
            combo.setCurrentIndex(max(0, combo.findData(mode)))
            combo.currentIndexChanged.connect(
                lambda *_a, c=combo: self._on_row_mode_changed(c))
            return combo

        def _fill_row(self, r: int, wp: Waypoint) -> None:  # pragma: no cover - Qt
            self.wp_table.setCellWidget(r, 0, self._make_mode_combo(wp.mode))
            for c, val in enumerate(wp.pos, start=1):
                shown = val * 1000.0 if (wp.mode == "movel" and c <= 3) else val
                self.wp_table.setItem(r, c, QTableWidgetItem(f"{shown:.6g}"))
            # DEC-156：路点名称（如「螺栓一」），配方「按螺栓直径运动」下拉显示它
            item = QTableWidgetItem(wp.note or "")
            item.setToolTip("路点名称/备注（如：螺栓一）。只用于辨识，不参与运动；"
                            "配方「按螺栓直径运动」的路线下拉会显示该名称。")
            self.wp_table.setItem(r, 7, item)

        def _row_of_combo(self, combo) -> int:  # pragma: no cover - Qt
            for r in range(self.wp_table.rowCount()):
                if self.wp_table.cellWidget(r, 0) is combo:
                    return r
            return -1

        def _on_row_mode_changed(self, combo) -> None:  # pragma: no cover - Qt
            r = self._row_of_combo(combo)
            if r >= 0:
                self._sync_row_cells(r)

        def _sync_row_cells(self, r: int) -> None:  # pragma: no cover - Qt
            """按行的方式启用/停用单元格。

            - 套筒偏移行**前 3 列(dx/dy/dz mm，Bolt 工具系，可正负)可填**，
              后 3 列置 0 且只读（DEC-139 起 X/Z 也放开）；
            - 工具系偏移行**前 3 列(dx/dy/dz mm)可填**，后 3 列置 0 且只读；
            - 点位行(movel/movej) 6 列全可填。
            """
            combo = self.wp_table.cellWidget(r, 0)
            mode = combo.currentData() if isinstance(combo, QComboBox) else "movel"
            editable = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable
                        | Qt.ItemFlag.ItemIsSelectable)
            readonly = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            socket = is_socket_mode(mode)
            tool = is_tool_offset_mode(mode)
            for c in range(1, 7):
                item = self.wp_table.item(r, c)
                if item is None:
                    item = QTableWidgetItem("0")
                    self.wp_table.setItem(r, c, item)
                if socket and c > 3:                # 套筒偏移：后 3 列(姿态)只读
                    item.setText("0")
                    item.setFlags(readonly)
                    item.setToolTip(
                        f"{_MODE_LABELS[mode]}只用前 3 列填 Bolt 工具系 dx/dy/dz(mm，可正负)；"
                        f"后 3 列须为 0")
                elif socket:
                    item.setFlags(editable)
                    item.setToolTip(
                        f"{_MODE_LABELS[mode]}：Bolt 工具系 dx/dy/dz(mm，可正负)；"
                        f"历史用法 Y：+ = 进入/前进，− = 脱离/后退；DEC-139 起 X/Z 也可填")
                elif tool and c > 3:                # 工具系偏移：只用前 3 列
                    item.setText("0")
                    item.setFlags(readonly)
                    item.setToolTip(
                        f"{_MODE_LABELS[mode]}只用前 3 列填 dx/dy/dz(mm，可正负)；后 3 列须为 0")
                elif tool:
                    item.setFlags(editable)
                    item.setToolTip(
                        f"{_MODE_LABELS[mode]}：Bolt 工具系相对偏移(mm)；"
                        f"例：上升 5 = 第3列 +5，斜回目标 = (0, +100, -5)")
                else:
                    item.setFlags(editable)
                    item.setToolTip("")

        def _row_waypoint(self, r: int) -> Waypoint | None:  # pragma: no cover - Qt
            combo = self.wp_table.cellWidget(r, 0)
            mode = combo.currentData() if isinstance(combo, QComboBox) else "movel"
            vals: list[float] = []
            for c in range(1, 7):
                item = self.wp_table.item(r, c)
                try:
                    v = float(item.text()) if item and item.text().strip() else 0.0
                except ValueError:
                    v = 0.0
                if mode == "movel" and c <= 3:
                    v = v / 1000.0          # 界面 mm -> 内部 m
                vals.append(v)
            note_item = self.wp_table.item(r, 7)
            note = note_item.text().strip() if note_item is not None else ""
            try:
                return Waypoint(mode, tuple(vals), note)
            except ValueError as exc:
                QMessageBox.warning(self, "路点非法", f"第 {r + 1} 行：{exc}")
                return None

        def _collect_rows(self) -> list[Waypoint] | None:  # pragma: no cover - Qt
            out: list[Waypoint] = []
            for r in range(self.wp_table.rowCount()):
                wp = self._row_waypoint(r)
                if wp is None:
                    return None
                out.append(wp)
            return out

        # ---------------- 方案管理 ----------------
        def _new_plan(self) -> None:  # pragma: no cover - Qt
            name, ok = QInputDialog.getText(self, "新建方案", "方案名:")
            if not ok or not name.strip():
                return
            try:
                self.store.add_plan(MotionPlan(plan_id=name.strip(),
                                               default_mode=self.new_mode.currentData(),
                                               speed=self.speed_spin.value(),
                                               blend=self.blend_spin.value(),
                                               timeout=self.timeout_spin.value(),
                                               source="本工程新建"))
                self.store.save()
            except ValueError as exc:
                QMessageBox.warning(self, "新建失败", str(exc))
            self.refresh()
            self.plan_combo.setCurrentIndex(max(0, self.plan_combo.findData(name.strip())))

        def _rename_plan(self) -> None:  # pragma: no cover - Qt
            pid = self.current_plan_id()
            if pid is None:
                return
            name, ok = QInputDialog.getText(self, "重命名方案", "新名称:", text=pid)
            if not ok or not name.strip():
                return
            try:
                self.store.rename_plan(pid, name.strip())
                self.store.save()
            except (KeyError, ValueError) as exc:
                QMessageBox.warning(self, "重命名失败", str(exc))
            self.refresh()

        def _delete_plan(self) -> None:  # pragma: no cover - Qt
            pid = self.current_plan_id()
            if pid is None:
                return
            if QMessageBox.question(self, "删除方案", f"确认删除方案 {pid!r} ?") \
                    != QMessageBox.StandardButton.Yes:
                return
            self.store.remove_plan(pid)
            self.store.save()
            self.refresh()

        def _read_pose(self) -> None:  # pragma: no cover - Qt
            pose = self._call_arm("get_current_pose")
            if pose is None:
                QMessageBox.information(self, "读取位姿", "机械臂未连接或读取失败")
                return
            self._append_row(Waypoint("movel", tuple(pose)))
            self.tabs.setCurrentIndex(0)

        def _read_joint(self) -> None:  # pragma: no cover - Qt
            joint = self._call_arm("get_current_joint")
            if joint is None:
                QMessageBox.information(self, "读取关节", "机械臂未连接或读取失败")
                return
            self._append_row(Waypoint("movej", tuple(joint)))

        def _call_arm(self, name: str):  # pragma: no cover - Qt
            arm = getattr(self.runner, "arm", None)
            fn = getattr(arm, name, None)
            if not callable(fn):
                return None
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return None

        def _add_row(self) -> None:  # pragma: no cover - Qt
            self._append_row(Waypoint(self.new_mode.currentData(), (0.0,) * 6))

        def _delete_row(self) -> None:  # pragma: no cover - Qt
            r = self.wp_table.currentRow()
            if r >= 0:
                self.wp_table.removeRow(r)

        def _move_up(self) -> None:  # pragma: no cover - Qt
            self._swap(-1)

        def _move_down(self) -> None:  # pragma: no cover - Qt
            self._swap(+1)

        def _swap(self, delta: int) -> None:  # pragma: no cover - Qt
            r = self.wp_table.currentRow()
            d = r + delta
            if r < 0 or not (0 <= d < self.wp_table.rowCount()):
                return
            was = self._row_waypoint(r)
            other = self._row_waypoint(d)
            if was is None or other is None:
                return
            self.wp_table.removeRow(r)
            self.wp_table.insertRow(d)
            self._fill_row(d, was)
            self._sync_row_cells(d)
            self.wp_table.setCurrentCell(d, 0)

        def _apply(self) -> None:  # pragma: no cover - Qt
            pid = self.current_plan_id()
            if pid is None:
                return
            rows = self._collect_rows()
            if rows is None:
                return
            plan = self._current_plan()
            if plan is None:
                return
            plan.waypoints = rows
            plan.speed = self.speed_spin.value()
            plan.blend = self.blend_spin.value()
            plan.timeout = self.timeout_spin.value()
            plan.default_mode = self.new_mode.currentData()
            self.store.save()
            self._rebuild_run_radios()

        # ---------------- 路径实施 ----------------
        def _rebuild_run_radios(self) -> None:  # pragma: no cover - Qt
            while self.run_grid.count():
                it = self.run_grid.takeAt(0)
                if it.widget():
                    it.widget().setParent(None)
            self.run_radios: dict[str, QRadioButton] = {}
            for i, plan in enumerate(self.store.config.plans):
                rb = QRadioButton(plan.plan_id)
                self.run_grid.addWidget(rb, i // 3, i % 3)
                self.run_radios[plan.plan_id] = rb
            if self.run_radios:
                first = next(iter(self.run_radios.values()))
                first.setChecked(True)

        def _selected_run_plan(self) -> str | None:  # pragma: no cover - Qt
            for pid, rb in getattr(self, "run_radios", {}).items():
                if rb.isChecked():
                    return pid
            return None

        def _run(self) -> None:  # pragma: no cover - Qt
            pid = self._selected_run_plan()
            if pid is None or self.runner is None:
                QMessageBox.information(self, "运行", "请先选择一个方案")
                return
            if not self._guard_motion("运行方案"):
                return
            self.runner.start_plan(pid)
            self._mark_guide_frame_stale()
            self._refresh_status()

        def _pause(self) -> None:  # pragma: no cover - Qt
            if self.runner:
                self.runner.pause()
                self._refresh_status()

        def _resume(self) -> None:  # pragma: no cover - Qt
            if self.runner:
                self.runner.resume()
                self._refresh_status()

        def _stop(self) -> None:  # pragma: no cover - Qt
            if self.runner:
                self.runner.stop()
                self._refresh_status()

        # ---------------- 引导定位：视觉方案 / 框圆（DEC-109） ----------------
        def _detections(self) -> list:
            store = getattr(getattr(self.ctx, "stack", None), "vision_detections", None)
            return list(getattr(store, "detections", None) or [])

        def _rebuild_guide_detections(self) -> None:  # pragma: no cover - Qt
            keep = self.guide_detection.currentData()
            self.guide_detection.blockSignals(True)
            try:
                self.guide_detection.clear()
                self.guide_detection.addItem("（自动找圆：用引导配置）", None)
                for det in self._detections():
                    label = det.detection_id
                    if getattr(det, "label", ""):
                        label += f"（{det.label}）"
                    self.guide_detection.addItem(label, det.detection_id)
                idx = self.guide_detection.findData(keep)
                self.guide_detection.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                self.guide_detection.blockSignals(False)

        def _selected_detection(self):
            rid = self.guide_detection.currentData()
            if not rid:
                return None
            for det in self._detections():
                if det.detection_id == rid:
                    return det
            return None

        def _rebuild_guide_cameras(self) -> None:  # pragma: no cover - Qt
            """DEC-137：引导相机下拉 = 本次运行**已连接**的相机通道（默认 DS86）。"""
            svc = getattr(self.runner, "guidance", None)
            ids_fn = getattr(svc, "camera_ids", None)
            ids = list(ids_fn()) if callable(ids_fn) else []
            if not ids:
                live = getattr(getattr(self.ctx, "stack", None), "cameras", None)
                ids = sorted(live) if isinstance(live, dict) else []
            keep = self.guide_camera.currentData()
            self.guide_camera.blockSignals(True)
            try:
                self.guide_camera.clear()
                for cid in ids:
                    self.guide_camera.addItem(str(cid), str(cid))
                if not ids:
                    self.guide_camera.addItem("（无可用相机）", None)
                idx = self.guide_camera.findData(keep) if keep else -1
                if idx < 0:
                    idx = self.guide_camera.findData("ds86_01")   # 现场默认 DS86
                self.guide_camera.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                self.guide_camera.blockSignals(False)

        def _guide_camera_id(self):
            """当前选中的引导相机 ID；没有可用相机时为 None（用引导服务默认值）。"""
            data = self.guide_camera.currentData()
            return str(data) if data else None

        def _on_guide_camera_changed(self) -> None:  # pragma: no cover - Qt
            """切换引导相机：旧画面属于原相机，不能拿它继续定位（DEC-137）。"""
            if self._guide_frame is not None:
                self._reset_guide_frame("已切换引导相机：请重新「单次视觉检测」")
            elif self._guide_frame_cam:
                self._guide_frame_cam = ""

        def _on_guide_detection_changed(self) -> None:  # pragma: no cover - Qt
            """切换视觉方案：旧画面的框/圆不再代表新方案，清掉避免混用。"""
            self._reset_guide_frame("已切换视觉方案：请重新「单次视觉检测」")

        def _reset_guide_frame(self, reason: str = "") -> None:  # pragma: no cover - Qt
            self._guide_frame = None
            self._guide_frame_valid = False
            self._guide_layout_note = ""
            self._guide_frame_cam = ""
            try:
                self.view_result.set_target_mode(None)
                self.view_result.clear_target_layout()
            except Exception:  # noqa: BLE001
                pass
            self._set_guide_draw_buttons(False)
            self.btn_save_img.setEnabled(False)
            if reason:
                self.guide_status.setText(reason)

        def _set_guide_draw_buttons(self, enabled: bool) -> None:  # pragma: no cover - Qt
            for btn in (self.btn_draw_box, self.btn_draw_circle,
                        self.btn_del_target):
                btn.setEnabled(bool(enabled))
            if not enabled:
                for btn in (self.btn_draw_box, self.btn_draw_circle):
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)

        def _sync_guide_mode_buttons(self, mode: str) -> None:  # pragma: no cover - Qt
            for btn, name in ((self.btn_draw_box, "box"),
                              (self.btn_draw_circle, "circle")):
                btn.blockSignals(True)
                btn.setChecked(mode == name)
                btn.blockSignals(False)

        def _on_guide_draw_box(self, checked: bool) -> None:  # pragma: no cover - Qt
            if checked:
                self.view_result.set_target_mode("box")
            elif self.view_result.target_mode() == "box":
                self.view_result.set_target_mode(None)

        def _on_guide_draw_circle(self, checked: bool) -> None:  # pragma: no cover - Qt
            if checked:
                self.view_result.set_target_mode("circle")
            elif self.view_result.target_mode() == "circle":
                self.view_result.set_target_mode(None)

        def _save_guide_image(self) -> None:  # pragma: no cover - Qt
            """DEC-121：保存当前引导帧（原始数据，不套显示旋转）。"""
            from application.presentation.components.frame_export import (
                save_frame_files,
            )
            if self._guide_frame is None:
                return
            saved = save_frame_files(self._guide_frame, self,
                                     title="保存引导图片")
            if saved:
                self.guide_status.setText("已保存：" + "；".join(saved))

        def _guard_guide_frame_editable(self) -> bool:
            """绘制/调整前确认当前帧仍在效（机械臂移动过就要重新检测）。"""
            if self._guide_frame is not None and self._guide_frame_valid:
                return True
            self.guide_status.setText(
                "画面已过期：请先「单次视觉检测」，再画框/画圆")
            return False

        def _on_guide_box_requested(self, x1: int, y1: int,
                                    x2: int, y2: int) -> None:  # pragma: no cover - Qt
            """画框完成：追加到本地布局；有视觉方案时顺手在框内按该方案找圆。"""
            if not self._guard_guide_frame_editable():
                return
            layout = self.view_result.layout()
            boxes = [dict(b) for b in layout["boxes"]]
            box = {"x1": float(x1), "y1": float(y1),
                   "x2": float(x2), "y2": float(y2), "circle": None}
            det = self._selected_detection()
            svc = getattr(self.runner, "guidance", None)
            if det is not None and svc is not None:
                try:
                    circle, note = find_circle_in_rect(
                        self._guide_frame, getattr(det, "params", None),
                        getattr(svc, "host_proxy", None), dict(box, idx=0))
                except Exception as exc:  # noqa: BLE001 - 找圆失败不挡画框
                    circle, note = None, f"框内找圆异常：{exc}"
                if circle is not None:
                    box["circle"] = circle
            boxes.append(box)
            self.view_result.set_layout(boxes, len(boxes) - 1)
            self.view_result.set_target_edit_mode(True)
            self._on_guide_layout_changed()
            if det is not None and self._guide_frame is not None:
                self.guide_status.setText(
                    f"{self.guide_status.text()}；{note}")

        def _on_guide_circle_drawn(self, idx: int, cx: int, cy: int,
                                   r: int) -> None:  # pragma: no cover - Qt
            """画圆完成：写进该框的圆，并刷新偏差显示（只算不下发）。"""
            if not self._guard_guide_frame_editable():
                return
            layout = self.view_result.layout()
            boxes = [dict(b) for b in layout["boxes"]]
            if not (0 <= int(idx) < len(boxes)):
                return
            boxes[int(idx)]["circle"] = {"cx": float(cx), "cy": float(cy),
                                         "radius_px": float(r)}
            self.view_result.set_layout(boxes, int(idx))
            self.view_result.set_target_edit_mode(True)
            self._on_guide_layout_changed()
            svc = getattr(self.runner, "guidance", None)
            if svc is not None and self._guide_frame is not None:
                self._guide_offsets_for_circle(
                    svc, self._guide_frame, boxes[int(idx)]["circle"])

        def _on_guide_layout_changed(self) -> None:  # pragma: no cover - Qt
            if self._guide_frame is None:
                return
            boxes = self.view_result.boxes()
            n_circle = len([b for b in boxes if b.get("circle")])
            parts = [f"框 {len(boxes)} 个 · 圆 {n_circle} 个"]
            sel = self.view_result.selected().get("index")
            if sel is not None and 0 <= int(sel) < len(boxes) \
                    and boxes[int(sel)].get("circle"):
                parts.append(f"定位圆=框 {int(sel) + 1}")
            else:
                parts.append("未选中定位圆（用第一个有圆的框）")
            if self._guide_layout_note:
                parts.append(self._guide_layout_note)
            if not self._guide_frame_valid:
                parts.append("画面已过期：机械臂移动过，请重新「单次视觉检测」")
            self.guide_status.setText("；".join(parts))

        def _selected_guide_circle(self):
            """当前定位圆：选中框的圆；没选中则第一个有圆的框。返回 (circle, 框索引)。"""
            boxes = self.view_result.boxes()
            sel = self.view_result.selected().get("index")
            if sel is not None and 0 <= int(sel) < len(boxes):
                c = boxes[int(sel)].get("circle")
                if c:
                    return c, int(sel)
            for i, b in enumerate(boxes):
                if b.get("circle"):
                    return b["circle"], i
            return None, None

        def _apply_guide_frame(self, frame, layout) -> None:  # pragma: no cover - Qt
            """显示帧 + 框/圆布局，并把它作为下次粗/精定位的定位依据。"""
            self._guide_frame = frame
            self._guide_frame_valid = True
            self._guide_frame_cam = str(getattr(frame, "camera_id", "") or "")
            self._guide_layout_note = str(getattr(layout, "message", "") or "")
            # DEC-119：DS86 画面按显示旋转 270°（只转显示，坐标仍是原图）；
            # DEC-137：其它相机（如 D405 USB）按原始朝向显示。
            try:
                self.view_result.set_display_rotation(
                    270 if self._guide_frame_cam.startswith("ds86") else 0)
            except Exception:  # noqa: BLE001 - 旋转只是显示用途
                pass
            color = getattr(getattr(frame, "color", None), "data", None)
            if color is not None:
                self.view_result.show_array(color, kind="color")
            else:
                self.view_result.clear_image("该帧无彩色流")
            self.view_result.set_target_edit_mode(True)
            self.view_result.set_layout(getattr(layout, "boxes", []),
                                        getattr(layout, "selected", None))
            self._set_guide_draw_buttons(True)
            self.btn_save_img.setEnabled(True)
            self._on_guide_layout_changed()

        def _guide_offsets_for_circle(self, svc, frame, circle) -> None:
            """按当前定位圆刷新相机 XYZ / 粗精偏差（只算不下发）。"""
            fn = getattr(svc, "detect_with_circle", None)
            if not callable(fn):
                return
            try:
                result = fn(frame=frame, cx=float(circle["cx"]),
                            cy=float(circle["cy"]),
                            radius_px=float(circle.get("radius_px") or 0.0))
            except Exception as exc:  # noqa: BLE001 - 显示用途，失败不打断
                self.kv_detect.set_key("检测", f"偏差计算失败：{exc}")
                return
            if not result.ok:
                self.kv_detect.set_key("检测", f"偏差计算失败：{result.message}")
                return
            x, y, z = result.cam_xyz
            self.kv_xyz.set_key("相机系 XYZ", f"X={x:.2f}  Y={y:.2f}  Z={z:.0f} mm")
            self.kv_coarse.set_key("粗定位偏差 (x,z) mm",
                                   f"{result.coarse[0]:.2f}, {result.coarse[1]:.2f}")
            self.kv_fine.set_key("精定位偏差 (x,z) mm",
                                 f"{result.fine[0]:.2f}, {result.fine[1]:.2f}")

        def _detect(self) -> None:  # pragma: no cover - Qt
            """单次视觉检测：抓帧 →（按视觉方案）现算框/圆 → 可人工调整。

            选定视觉方案时按该检测的找圆调试参数现算（与作业台/视觉调试同一条链）；
            选「自动找圆」时沿用引导配置的自动检测，并把结果圆画成外接框。
            """
            svc = getattr(self.runner, "guidance", None)
            if svc is None:
                QMessageBox.information(self, "视觉检测", "未接入本地引导服务")
                return
            det = self._selected_detection()
            cam = self._guide_camera_id()
            frame = None
            if det is not None:
                cap = getattr(svc, "capture_frame", None)
                if callable(cap):
                    try:
                        try:
                            frame = cap(camera_id=cam) if cam else cap()
                        except TypeError:      # 兼容旧假引导服务（无 camera_id 参数）
                            frame = cap()
                    except Exception as exc:  # noqa: BLE001
                        self.kv_detect.set_key("检测", f"失败：抓图失败: {exc}")
                        self._reset_guide_frame("抓图失败，没有可调整的画面")
                        return
                    if frame is None:
                        self.kv_detect.set_key("检测", "失败：未取到相机帧")
                        self._reset_guide_frame("未取到帧，请检查相机")
                        return
                    layout = compute_initial_layout(
                        frame, getattr(det, "params", None),
                        getattr(svc, "host_proxy", None))
                    note = f"视觉方案 {det.detection_id}"
                    det_cam = getattr(det, "camera_ref", "") or ""
                    cur_cam = str(getattr(frame, "camera_id", "") or cam or "")
                    if det_cam and det_cam != cur_cam:
                        note += (f"（该检测记录相机 {det_cam}，本页用 "
                                 f"{cur_cam or '默认引导相机'}）")
                    self._apply_guide_frame(frame, layout)
                    self.kv_detect.set_key(
                        "检测", f"{note}：{layout.message or '已按检测配置现算'}")
                    circle, _idx = self._selected_guide_circle()
                    if circle is not None:
                        self._guide_offsets_for_circle(svc, frame, circle)
                    return
            # 未选视觉方案（或旧引导服务没有抓帧接口）：旧的自动检测路径
            try:
                result = svc.detect(camera_id=cam) if cam else svc.detect()
            except TypeError:              # 兼容旧假引导服务（无 camera_id 参数）
                result = svc.detect()
            if not result.ok:
                self.kv_detect.set_key("检测", f"失败：{result.message}")
                self._reset_guide_frame("未检出：可换视觉方案或调整检测配置")
                return
            frame = getattr(svc, "last_frame", None)
            layout = compute_initial_layout(
                frame, None, getattr(svc, "host_proxy", None),
                fallback_circles=[{"cx": result.circle.cx,
                                   "cy": result.circle.cy,
                                   "radius_px": result.circle.radius}])
            if layout.boxes:
                layout.message = "框按自动检测圆的外接方框生成（非深度框）"
            if frame is not None:
                self._apply_guide_frame(frame, layout)
            x, y, z = result.cam_xyz
            self.kv_xyz.set_key("相机系 XYZ", f"X={x:.2f}  Y={y:.2f}  Z={z:.0f} mm")
            self.kv_coarse.set_key("粗定位偏差 (x,z) mm",
                                   f"{result.coarse[0]:.2f}, {result.coarse[1]:.2f}")
            self.kv_fine.set_key("精定位偏差 (x,z) mm",
                                 f"{result.fine[0]:.2f}, {result.fine[1]:.2f}")
            c = result.circle
            self.kv_detect.set_key(
                "检测", f"自动找圆 ({c.cx:.0f},{c.cy:.0f}) r={c.radius:.1f}px "
                        f"· 深度 {result.used_depth_mm:.0f}mm")

        def _mark_guide_frame_stale(self) -> None:  # pragma: no cover - Qt
            """机械臂移动后旧画面不再代表当前位置，不能再用它做定位依据（DEC-109）。"""
            if self._guide_frame is None:
                return
            self._guide_frame_valid = False
            self._on_guide_layout_changed()

        def _guide(self, stage: str) -> None:  # pragma: no cover - Qt
            """DEC-107/109：粗/精分按钮；有有效画面时按操作员确认的圆定位。"""
            if not self._guard_motion("引导定位"):
                return
            override = None
            if self._guide_frame is not None and self._guide_frame_valid:
                circle, idx = self._selected_guide_circle()
                if circle is None:
                    _warn_dialog(self, "引导定位",
                                 "当前画面框内没有圆：请先在框内画圆/重新检测，"
                                 "或清空画面改用自动找圆。")
                    return
                override = {
                    "frame": self._guide_frame,
                    "circle": {"cx": circle["cx"], "cy": circle["cy"],
                               "radius_px": circle.get("radius_px", 0.0)},
                    "step_id": f"motion_page:{(self.guide_detection.currentData() or 'auto')}"
                               f":box{idx + 1}",
                }
            elif self._guide_frame is not None and not self._guide_frame_valid:
                if self._selected_detection() is not None:
                    _warn_dialog(self, "引导定位",
                                 "画面已过期（机械臂移动过）：请重新「单次视觉检测」并调整圆，"
                                 "或把视觉方案切回「自动找圆」。")
                    return
                # 自动找圆模式：过期画面不用，按引导配置重新抓图找圆（旧行为）。
            self.runner.confirm = self._confirm
            # DEC-124：手动引导也受单段最大偏移约束（参数配置里的值，默认 50mm）
            _prof = self.store.config.profile
            self.runner.start_guide(speed=self.guide_speed.value(), stages=(stage,),
                                    max_offset_mm=float(
                                        getattr(_prof, "guide_max_offset_mm", 50.0) or 0.0),
                                    camera_id=self._guide_camera_id(),
                                    override=override)
            self._mark_guide_frame_stale()
            self._refresh_status()

        def _cancel_guide(self) -> None:  # pragma: no cover - Qt
            if self.runner:
                self.runner.cancel_guide()
                self._refresh_status()

        def _socket(self, sign: int) -> None:  # pragma: no cover - Qt
            if not self._guard_motion("套筒偏移"):
                return
            mm = sign * abs(self.sock_mm.value())
            self.runner.request_socket_move(mm, axis="Y", speed=self.sock_speed.value())
            self._mark_guide_frame_stale()
            self._refresh_status()

        def _jog(self, axis: str, sign: int) -> None:  # pragma: no cover - Qt
            """手动调整页：Bolt 工具系单轴步进（DEC-139）。"""
            if not self._guard_motion("工具系点动"):
                return
            mm = sign * abs(self.jog_step.value())
            self.runner.request_tool_jog(axis, mm, speed=self.jog_speed.value())
            self._mark_guide_frame_stale()
            self._refresh_status()

        # ---------------- 门控与确认 ----------------
        def _guard_motion(self, action: str) -> bool:  # pragma: no cover - Qt
            arm = getattr(self.runner, "arm", None)
            if not getattr(arm, "connected", False):
                QMessageBox.warning(self, action, "机械臂未连接")
                return False
            return True

        def _confirm(self, title: str, message: str, options: list[str]) -> str | None:  # pragma: no cover - Qt
            """确认回调（可能来自运动工作线程）：一律在 GUI 线程弹窗并阻塞等待结果。

            工作线程直接建 QMessageBox 会跨线程设父对象而崩溃（DEC-116），
            所以这里把请求经信号排队到 GUI 线程执行，工作线程只等结果。
            """
            if threading.get_ident() == self._gui_thread_ident:
                return self._confirm_box(title, message, options)
            holder: dict[str, str | None] = {}
            done = threading.Event()
            self._confirm_request.emit(title, message, list(options), holder, done)
            if not done.wait(1800):          # 30 分钟未处理 = 按未确认处理（不动）
                return None
            return holder.get("choice")

        def _confirm_on_gui(self, title: str, message: str, options,
                            holder: dict, done: threading.Event) -> None:
            try:
                holder["choice"] = self._confirm_box(title, message, options)
            finally:
                done.set()

        def _confirm_box(self, title: str, message: str, options: list[str]) -> str | None:  # pragma: no cover - Qt
            box = QMessageBox(self)
            box.setWindowTitle(title)
            box.setText(message)
            buttons = {}
            for opt in options:
                role = (QMessageBox.ButtonRole.AcceptRole if opt in ("是", "重试本点")
                        else QMessageBox.ButtonRole.RejectRole)
                buttons[box.addButton(opt, role)] = opt
            box.exec()
            clicked = box.clickedButton()
            return buttons.get(clicked)

        # ---------------- 状态与日志 ----------------
        def _refresh_status(self) -> None:  # pragma: no cover - Qt
            arm = getattr(self.runner, "arm", None)
            connected = bool(getattr(arm, "connected", False))
            profile = self.store.config.profile
            self.kv_robot.set_key(
                "机器人", f"{'已连接' if connected else '未连接'}"
                          f"（{profile.active_tool} / {profile.active_work}）")
            self.kv_gate.set_key("运动", "real 模式默认允许（无环境变量闸）")
            # DEC-157：执行中禁用"会再次下发动作"的按钮（双击/连点防重复下发）；
            # 暂停/继续按当前状态单独放行。
            from application.services.motion_runner import (
                STATE_GUIDE, STATE_PAUSED, STATE_PLAYING,
            )
            st = self.runner.state if self.runner is not None else {}
            mode = st.get("mode")
            busy = mode in (STATE_PLAYING, STATE_PAUSED, STATE_GUIDE)
            idle_ok = connected and not busy
            for btn in (self.btn_run,
                        self.btn_guide_coarse, self.btn_guide_fine,
                        self.btn_fwd, self.btn_back,
                        *getattr(self, "jog_buttons", {}).values()):
                btn.setEnabled(idle_ok)
            self.btn_pause.setEnabled(connected and mode == STATE_PLAYING)
            self.btn_resume.setEnabled(connected and mode == STATE_PAUSED)
            self.btn_detect.setEnabled(True)     # 检测不动机械臂，不受运动闸限制
            self.btn_stop.setEnabled(True)      # 停止始终可发（与全局停止一致）
            # 示教点：读取关节是只读操作（不需要运动闸），但必须真实机械臂已连接；
            # 离线装配没有 teach_store → 禁用并说明原因（不拿假机械臂读数当示教值）。
            teach_ok = self.teach_store is not None and connected
            self.btn_teach_add.setEnabled(teach_ok)
            if self.teach_store is None:
                self.btn_teach_add.setToolTip(
                    "离线模式不能登记示教点：示教值必须来自真实机械臂的当前关节")
            elif not connected:
                self.btn_teach_add.setToolTip("机械臂未连接，读不到当前关节")
            else:
                self.btn_teach_add.setToolTip(
                    "读取机械臂当前 6 轴关节角，命名后登记为示教点（写回 joint_targets.yaml）")
            self.btn_teach_del.setEnabled(
                self.teach_store is not None and self.teach_table.rowCount() > 0)

        def _tick(self) -> None:  # pragma: no cover - Qt
            self._drain_state()
            # DEC-145：手动调整页可见时约 1s 读一次当前位姿刷新示意图（只读）。
            if getattr(self, "pose_view", None) is not None and self.isVisible() \
                    and self.tabs.currentIndex() == 2:
                self._pose_tick = getattr(self, "_pose_tick", 0) + 1
                if self._pose_tick % 4 == 0:
                    self._refresh_pose_view()

        def _refresh_pose_view(self) -> None:  # pragma: no cover - Qt
            """读一次机器人当前位姿并刷新 3D 示意（只读，不动机械臂）。"""
            view = getattr(self, "pose_view", None)
            if view is None:
                return
            arm = getattr(self.runner, "arm", None)
            connected = bool(getattr(arm, "connected", False))
            view.set_connected(connected)
            if not connected:
                return
            pose = self._call_arm("get_current_pose")
            if pose and len(pose) >= 6:
                view.set_pose(pose)

        def _drain_state(self) -> None:  # pragma: no cover - Qt
            if self.runner is None:
                return
            st = self.runner.state
            text = {"idle": "空闲", "playing": "执行中", "paused": "已暂停",
                    "guide": "引导定位中", "error": "错误"}.get(st["mode"], st["mode"])
            if st.get("total"):
                text += f" {st.get('index', 0)}/{st['total']}"
            if st.get("message"):
                text += f" · {st['message']}"
            self.kv_state.set_key("执行状态", text)
            self._refresh_status()

        def shutdown(self) -> None:
            """**先停定时器**（Qt 关闭顺序坑：晚了会崩，断言过但 rc=139）。"""
            try:
                self._timer.stop()
            except Exception:  # noqa: BLE001
                pass
            popup = getattr(self, "_image_popup", None)
            if popup is not None:
                try:
                    popup.close()
                    popup.deleteLater()
                except Exception:  # noqa: BLE001
                    pass
                self._image_popup = None
                self._popup_open = False

else:  # pragma: no cover
    class MotionPage:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")


__all__ = ["GUI_REQUIRED", "MotionPage"]
