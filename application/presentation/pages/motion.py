"""运动方案页（移植旧工程 `ui/panels/motion_plan_panel.py`）。

页内两个子 tab，**编辑与调用隔离**（与旧工程一致）：

- **路径规划**：**示教点**（读取机械臂当前关节 → 登记为命名关节目标，写回
  `configs/site/joint_targets.yaml`，执行方案步骤引用它；DEC-067）、方案管理
  （新建/重命名/删除）、新点默认方式、速度%/交融半径%/单点超时、
  路点表（每行独立选方式，可混用 ``movel``/``movej``、**套筒偏移**与**工具系偏移**）、
  读取当前位姿/关节、增删行/上下移/应用。``movel`` 行界面填 **mm + rad**，内部自动换算成 **m** 下发；
  ``movej`` 行填**度**；**套筒偏移行**只在「y / j2」列填**带符号偏移(mm)**（+ = 进入/前进，− = 脱离/后退），
  其余列只读——整理自旧工程「套筒偏移」与「取出套筒」两组按钮（底层同一动作：Bolt 工具系 Y 轴偏移）；
  **工具系偏移行**前 3 列填 **dx/dy/dz(mm)**（Bolt 工具系，可正负），后 3 列只读——执行时按当前位姿
  算目标位姿并走**一条 movel 直线**（例如先 Z+5 抬升、再 (0,+100,−5) 斜回目标，DEC-060）。
- **路径实施**：运行方案（单选）、运行/暂停/继续/停止、**引导定位**（粗定位/精定位
  两个按钮，各跑对应段，每次人工确认后移动）、**套筒前进/后退**（Bolt 工具系 Y 轴；
  与方案里的套筒偏移同一动作）；不再显示执行日志（DEC-107）。

**运动闸**：未开运动闸时所有运动按钮禁用并提示原因；读取类（读位姿/读关节）不受限。
数据来自 `configs/motion/motion.yaml`（含旧工程**实测导入**的方案），每条带来源标注。
"""
from __future__ import annotations


from application.presentation.components import Card, EmptyHint, KV
from application.presentation.components.target_layout_view import TargetLayoutImageView
from application.presentation.timefmt import to_beijing_text
from application.services.vision_layout import compute_initial_layout
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
    from PySide6.QtCore import Qt, QTimer
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
        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.store = _motion_store(ctx)
            self.runner = getattr(ctx.stack, "motion", None)
            # 示教点登记（DEC-067）：只有 production 装配才有；离线为 None（按钮禁用）
            self.teach_store = getattr(ctx.stack, "teach_points", None)
            # DEC-109：引导定位的「视觉方案」与人工框/圆（画面过期后不重复使用）
            self._guide_frame = None          # 最近一次「单次视觉检测」的帧
            self._guide_frame_valid = False   # 机械臂移动过 -> 旧帧不再作为定位依据
            self._guide_layout_note = ""

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

            self.wp_table = QTableWidget(0, 7)
            self.wp_table.setHorizontalHeaderLabels(
                ["方式", "x / j1", "y / j2", "z / j3", "rx / j4", "ry / j5", "rz / j6"])
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
                          "关节行按 j1..j6(°) 填；套筒偏移行只在「y / j2」列填带符号偏移(mm)"
                          "（+ = 进入/前进，− = 脱离/后退），执行时用引导速度；"
                          "工具系偏移行前 3 列填 Bolt 工具系 dx/dy/dz(mm)，执行时走一条 movel 直线。"
                          "读位姿/读关节需机械臂已连接。")
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

            guide = QGroupBox("引导定位（DS86 深度引导：本地抓图→找圆→偏差，每次人工确认）")
            gh = QHBoxLayout(guide)
            # 左：操作按钮 + 检测结果；右：可画框/画圆的视频画面（DEC-107 / DEC-109）。
            ops = QVBoxLayout()
            row_speed = QHBoxLayout()
            row_speed.addWidget(QLabel("速度%:"))
            self.guide_speed = QSpinBox(); self.guide_speed.setRange(1, 100)
            row_speed.addWidget(self.guide_speed)
            row_speed.addStretch(1)
            ops.addLayout(row_speed)
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
            right = QVBoxLayout()
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
            for b in (self.btn_draw_box, self.btn_draw_circle, self.btn_del_target):
                tbar.addWidget(b)
            tbar.addStretch(1)
            right.addLayout(tbar)
            self.view_result = TargetLayoutImageView("尚未检测")
            self.view_result.mode_changed.connect(self._sync_guide_mode_buttons)
            self.view_result.message.connect(
                lambda text: self.guide_status.setText(text or ""))
            self.view_result.layout_changed.connect(self._on_guide_layout_changed)
            self.view_result.selection_changed.connect(
                lambda *_a: self._on_guide_layout_changed())
            right.addWidget(self.view_result, 1)
            self.guide_status = QLabel(
                "选择视觉方案 →「单次视觉检测」→ 可在图上画框/画圆并拖动调整；"
                "粗定位/精定位用选中框的圆（没有则用第一个有圆的框）。")
            self.guide_status.setObjectName("SubLabel")
            self.guide_status.setWordWrap(True)
            right.addWidget(self.guide_status)
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

            - 套筒偏移行**只有「y / j2」列(带符号偏移 mm)可填**，其余列置 0 且只读；
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
                if socket and c != 2:               # 套筒偏移：只有 y/j2 列(偏移)保留
                    item.setText("0")
                    item.setFlags(readonly)
                    item.setToolTip(
                        f"{_MODE_LABELS[mode]}只用「y / j2」列填偏移(mm)；正=进入/前进，负=脱离/后退")
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
            try:
                return Waypoint(mode, tuple(vals))
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

        def _pause(self) -> None:  # pragma: no cover - Qt
            if self.runner:
                self.runner.pause()

        def _resume(self) -> None:  # pragma: no cover - Qt
            if self.runner:
                self.runner.resume()

        def _stop(self) -> None:  # pragma: no cover - Qt
            if self.runner:
                self.runner.stop()

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

        def _on_guide_detection_changed(self) -> None:  # pragma: no cover - Qt
            """切换视觉方案：旧画面的框/圆不再代表新方案，清掉避免混用。"""
            self._reset_guide_frame("已切换视觉方案：请重新「单次视觉检测」")

        def _reset_guide_frame(self, reason: str = "") -> None:  # pragma: no cover - Qt
            self._guide_frame = None
            self._guide_frame_valid = False
            self._guide_layout_note = ""
            try:
                self.view_result.set_target_mode(None)
                self.view_result.clear_target_layout()
            except Exception:  # noqa: BLE001
                pass
            self._set_guide_draw_buttons(False)
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
            self._guide_layout_note = str(getattr(layout, "message", "") or "")
            color = getattr(getattr(frame, "color", None), "data", None)
            if color is not None:
                self.view_result.show_array(color, kind="color")
            else:
                self.view_result.clear_image("该帧无彩色流")
            self.view_result.set_target_edit_mode(True)
            self.view_result.set_layout(getattr(layout, "boxes", []),
                                        getattr(layout, "selected", None))
            self._set_guide_draw_buttons(True)
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
            frame = None
            if det is not None:
                cap = getattr(svc, "capture_frame", None)
                if callable(cap):
                    try:
                        frame = cap()
                    except Exception as exc:  # noqa: BLE001
                        self.kv_detect.set_key("检测", f"失败：抓图失败: {exc}")
                        self._reset_guide_frame("抓图失败，没有可调整的画面")
                        return
                    if frame is None:
                        self.kv_detect.set_key("检测", "失败：未取到 DS86 帧")
                        self._reset_guide_frame("未取到帧，请检查相机")
                        return
                    layout = compute_initial_layout(
                        frame, getattr(det, "params", None),
                        getattr(svc, "host_proxy", None))
                    note = f"视觉方案 {det.detection_id}"
                    if getattr(det, "camera_ref", "") not in ("", "ds86_01"):
                        note += f"（该检测记录相机 {det.camera_ref}，本页仍用 DS86 引导相机）"
                    self._apply_guide_frame(frame, layout)
                    self.kv_detect.set_key(
                        "检测", f"{note}：{layout.message or '已按检测配置现算'}")
                    circle, _idx = self._selected_guide_circle()
                    if circle is not None:
                        self._guide_offsets_for_circle(svc, frame, circle)
                    return
            # 未选视觉方案（或旧引导服务没有抓帧接口）：旧的自动检测路径
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
            self.runner.start_guide(speed=self.guide_speed.value(), stages=(stage,),
                                    override=override)
            self._mark_guide_frame_stale()

        def _cancel_guide(self) -> None:  # pragma: no cover - Qt
            if self.runner:
                self.runner.cancel_guide()

        def _socket(self, sign: int) -> None:  # pragma: no cover - Qt
            if not self._guard_motion("套筒偏移"):
                return
            mm = sign * abs(self.sock_mm.value())
            self.runner.request_socket_move(mm, axis="Y", speed=self.sock_speed.value())
            self._mark_guide_frame_stale()

        # ---------------- 门控与确认 ----------------
        def _guard_motion(self, action: str) -> bool:  # pragma: no cover - Qt
            arm = getattr(self.runner, "arm", None)
            if not getattr(arm, "connected", False):
                QMessageBox.warning(self, action, "机械臂未连接")
                return False
            return True

        def _confirm(self, title: str, message: str, options: list[str]) -> str | None:  # pragma: no cover
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
            can_move = connected
            for btn in (self.btn_run, self.btn_pause, self.btn_resume,
                        self.btn_guide_coarse, self.btn_guide_fine,
                        self.btn_fwd, self.btn_back):
                btn.setEnabled(can_move)
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

else:  # pragma: no cover
    class MotionPage:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")


__all__ = ["GUI_REQUIRED", "MotionPage"]
