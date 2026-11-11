"""作业台页 (E23/A18, 按 03 §4.1 + V0.2 主稿重做)。

区域: 配方/状态/版本 -> 执行导航条 -> 步骤时间线 | 证据图像区 -> 任务依赖条
      -> 最近事件 -> 阻止原因 + 固定操作栏。
无工位后步骤清单按**当前配方实际步骤**动态渲染（未启动时显示配方声明的步骤）。
"停止全部"在顶部常驻; 本页操作栏同样按状态矩阵启用。
"""
from __future__ import annotations

import os

from application.presentation.components import (
    ActionRail, CaptureGalleryView, Card, DeviceStrip, EmptyHint, KV, StatusPill,
    Stepper,
)
from application.presentation.components.target_layout_view import TargetLayoutImageView
from application.presentation.theme import THEME, state_colors
from application.presentation.timefmt import to_beijing_text
from application.services.recipes.editor import step_title

try:  # pragma: no cover
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QComboBox, QDialog, QFrame, QHBoxLayout, QLabel,
        QMessageBox, QPushButton, QRadioButton, QScrollArea, QTabBar,
        QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

_STATUS_CN = {
    "PENDING": "待执行", "SUCCESS": "完成", "RUNNING": "执行中",
    "FAILURE": "失败", "INVALID": "未执行", "STOPPED": "已停止",
}


def _operator_name() -> str:
    """确认留证用操作员名（与配方发布同一约定，环境变量优先）。"""
    return (os.environ.get("JT_OPERATOR")
            or os.environ.get("USERNAME") or os.environ.get("USER") or "operator")

if GUI_REQUIRED:

    class StepConfirmDialog(QDialog):
        """步骤完成确认弹框（DEC-042）: **非模态**——弹着也能在右卡拖检测圆。

        只提供受限选项（确认继续 / 不正确）；窗口关闭按钮/Esc 在等待期间无效，
        必须显式选择（避免"点了叉就当确认"）。选择「不正确」由服务层暂停作业。
        """

        def __init__(self, parent=None, on_choice=None) -> None:
            super().__init__(parent)
            self._active = False
            self._on_choice = on_choice
            self.setWindowTitle("步骤完成确认")
            self.setModal(False)
            self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
            self.setMinimumWidth(460)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(18, 16, 18, 14)
            lay.setSpacing(10)
            self.title = QLabel("步骤完成确认")
            self.title.setStyleSheet("font-size:16px; font-weight:700;")
            self.message = QLabel("")
            self.message.setWordWrap(True)
            self.circle_hint = QLabel("")
            self.circle_hint.setWordWrap(True)
            self.circle_hint.setStyleSheet(f"color:{THEME.text_secondary};")
            self.status = QLabel("")
            self.status.setWordWrap(True)
            self.status.setStyleSheet(f"color:{THEME.deep_teal}; font-weight:600;")
            btns = QHBoxLayout()
            btns.addStretch(1)
            self.ok_btn = QPushButton("确认继续")
            self.ok_btn.setObjectName("Primary")
            self.ok_btn.clicked.connect(lambda: self._choose("确认"))
            self.bad_btn = QPushButton("不正确")
            self.bad_btn.setObjectName("Ghost")
            self.bad_btn.clicked.connect(lambda: self._choose("不正确"))
            btns.addWidget(self.ok_btn)
            btns.addWidget(self.bad_btn)
            lay.addWidget(self.title)
            lay.addWidget(self.message)
            lay.addWidget(self.circle_hint)
            lay.addWidget(self.status)
            lay.addLayout(btns)

        def _choose(self, choice: str) -> None:
            if callable(self._on_choice):
                self._on_choice(choice)

        def set_prompt(self, prompt: dict, *, circle_available: bool) -> None:
            self.title.setText(prompt.get("title") or "步骤完成确认")
            self.message.setText(prompt.get("message") or "")
            if circle_available:
                self.circle_hint.setText(
                    "右侧图卡显示本次抓拍；可点「画框」「画圆」自定义，拖动框/圆调整，"
                    "拖圆右侧白点改半径，选中后按 Delete 删除；松开即按手动框/圆重新判定。"
                    "「定位圆」下拉或点图上的圆可指定用哪个圆定位（首项=不指定，"
                    "引导定位将重新找圆）；后续粗/精定位沿用这一帧与该圆。")
            else:
                self.circle_hint.setText("右侧图卡显示本次抓拍原图。")
            self.status.setText("")

        def set_status_text(self, text: str) -> None:
            self.status.setText(text or "")

        def mark_active(self, active: bool) -> None:
            self._active = bool(active)

        def is_active_prompt(self) -> bool:
            return self._active

        def closeEvent(self, event) -> None:  # pragma: no cover - Qt
            if self._active:
                event.ignore()     # 等待期间不允许直接关掉（必须选择）
                return
            super().closeEvent(event)

        def reject(self) -> None:  # pragma: no cover - Qt
            if self._active:
                return
            super().reject()

    class GuideConfirmDialog(QDialog):
        """引导定位每段人工确认弹框（DEC-033/059）: **非模态**——弹着也能看右卡图像。

        与「步骤完成确认」一样只提供受限选项（是 / 否）；等待期间关闭按钮/Esc 无效，
        必须显式选择（避免"点了叉就当确认"）。选择「否」由运动线程按"未确认"结束本步，
        **不移动机械臂**。
        """

        def __init__(self, parent=None, on_choice=None) -> None:
            super().__init__(parent)
            self._active = False
            self._on_choice = on_choice
            self.setWindowTitle("引导定位确认")
            self.setModal(False)
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
            self.setMinimumWidth(460)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(18, 16, 18, 14)
            lay.setSpacing(10)
            self.title = QLabel("引导定位确认")
            self.title.setStyleSheet("font-size:16px; font-weight:700;")
            self.message = QLabel("")
            self.message.setWordWrap(True)
            self.hint = QLabel("确认后才按该偏差移动机械臂；不确认不会移动。")
            self.hint.setStyleSheet(f"color:{THEME.text_secondary};")
            self.hint.setWordWrap(True)
            self._btn_row = QHBoxLayout()
            lay.addWidget(self.title)
            lay.addWidget(self.message)
            lay.addWidget(self.hint)
            lay.addLayout(self._btn_row)

        def buttons(self) -> list:
            """当前选项按钮（测试/键盘用）。"""
            out = []
            for i in range(self._btn_row.count()):
                w = self._btn_row.itemAt(i).widget()
                if w is not None:
                    out.append(w)
            return out

        def _choose(self, choice: str) -> None:
            if self._active and callable(self._on_choice):
                self._on_choice(choice)

        def set_prompt(self, prompt: dict) -> None:
            self.title.setText(prompt.get("title") or "引导定位确认")
            self.message.setText(prompt.get("message") or "")
            while self._btn_row.count():
                item = self._btn_row.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            self._btn_row.addStretch(1)
            for i, opt in enumerate(prompt.get("options") or ()):
                b = QPushButton(str(opt))
                b.setObjectName("Primary" if i == 0 else "Ghost")
                b.clicked.connect(
                    lambda _c=False, choice=str(opt): self._choose(choice))
                self._btn_row.addWidget(b)

        def mark_active(self, active: bool) -> None:
            self._active = bool(active)

        def closeEvent(self, event) -> None:  # pragma: no cover - Qt
            if self._active:
                event.ignore()     # 等待期间不允许直接关掉（必须选择）
                return
            super().closeEvent(event)

        def reject(self) -> None:  # pragma: no cover - Qt
            if self._active:
                return
            super().reject()

    class FaultRecoveryDialog(QDialog):
        """故障恢复弹窗（DEC-061 / DEC-104）：只选择从失败步骤恢复或重新执行整个任务。

        - **从失败步骤恢复**：同一 run 只重试失败的那一步，已完成步骤不重放；
        - **重新执行整个任务**：新建 run 从头开始；
        - 不再要求填写人工处理说明或勾选确认（DEC-104）。
        """

        def __init__(self, parent=None, info: dict | None = None) -> None:
            super().__init__(parent)
            self._info = dict(info or {})
            self.setWindowTitle("故障恢复")
            self.setModal(True)
            self.setMinimumWidth(540)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(18, 16, 18, 14)
            lay.setSpacing(10)
            title = QLabel("故障恢复")
            title.setStyleSheet("font-size:16px; font-weight:700;")
            lay.addWidget(title)
            step = self._info.get("step_title") or self._info.get("step_id") or "-"
            reason = self._info.get("reason") or "步骤失败"
            msg = QLabel(
                f"失败步骤：{step}\n失败原因：{reason}\n")
            msg.setWordWrap(True)
            lay.addWidget(msg)
            self.resume_radio = QRadioButton(
                "从失败步骤恢复")
            self.restart_radio = QRadioButton(
                "重新执行整个任务")
            retryable = bool(self._info.get("retryable"))
            self.resume_radio.setEnabled(retryable)
            if retryable:
                self.resume_radio.setChecked(True)
            else:
                self.restart_radio.setChecked(True)
                note = QLabel("该失败无法原地重试（复合步骤/停止未核实等），只能重新执行整个任务。")
                note.setWordWrap(True)
                note.setStyleSheet(f"color:{THEME.text_secondary};")
                lay.addWidget(note)
            lay.addWidget(self.resume_radio)
            lay.addWidget(self.restart_radio)
            btns = QHBoxLayout()
            btns.addStretch(1)
            cancel = QPushButton("取消")
            cancel.setObjectName("Ghost")
            cancel.clicked.connect(self.reject)
            ok = QPushButton("执行恢复")
            ok.setObjectName("Primary")
            ok.clicked.connect(self.accept)
            btns.addWidget(cancel)
            btns.addWidget(ok)
            lay.addLayout(btns)

        def choice(self) -> str:
            return "resume" if self.resume_radio.isChecked() else "restart"

    class WorkbenchPage(QWidget):
        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.vm = ctx.vm
            self._autotick = True
            self._shown_frame_id = None
            self._shown_frame = None
            # DEC-051: 相机页签状态。页签 = 设备配置里已启用的相机；有新抓拍/新
            # 待确认视觉步骤时自动切到对应相机，操作员手动点过页签后不再自动抢。
            self._channel_sig = None
            self._auto_follow = True
            self._seen_run_key = None
            self._seen_prompt_key = None
            self._step_dialog: StepConfirmDialog | None = None
            self._guide_dialog: GuideConfirmDialog | None = None
            self._guide_prompt_key = None
            # DEC-119：相机卡片可弹出独立窗口（与操作台抓拍弹窗同一组件）
            self._image_popup = None
            self._popup_open = False
            self._popup_owner = None
            self._build()
            from PySide6.QtCore import QTimer
            self._timer = QTimer(self)
            self._timer.setInterval(200)
            self._timer.timeout.connect(self._on_tick)
            self._timer.start()
            self.refresh()

        # ---------- 构建 ----------
        def _build(self) -> None:
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            head = QHBoxLayout()
            head.setSpacing(28)
            # 执行方案选择（DEC-041）：作业台直接选方案，不必回配方页；
            # 来源 = 启动时自动发现的配方目录（DEC-035），运行中禁用。
            self.recipe_pick = QWidget()
            hp = QVBoxLayout(self.recipe_pick)
            hp.setContentsMargins(0, 0, 0, 0); hp.setSpacing(4)
            pick_lab = QLabel("执行方案"); pick_lab.setObjectName("SubLabel")
            self.recipe_combo = QComboBox()
            self.recipe_combo.setMinimumWidth(260)
            self.recipe_combo.addItem("（未选择）", None)
            self.recipe_combo.activated.connect(self._on_recipe_selected)
            hp.addWidget(pick_lab); hp.addWidget(self.recipe_combo)
            self._combo_ids: list[str] = []
            self._combo_busy = False
            self.kv_state = QWidget()
            hs = QVBoxLayout(self.kv_state)
            hs.setContentsMargins(0, 0, 0, 0); hs.setSpacing(4)
            lab = QLabel("当前状态"); lab.setObjectName("SubLabel")
            self.state_pill = StatusPill("待检查")
            hs.addWidget(lab); hs.addWidget(self.state_pill)
            self.kv_version = KV("执行方案版本", "-")
            head.addWidget(self.recipe_pick, 2)
            head.addWidget(self.kv_state, 1)
            head.addWidget(self.kv_version, 1)
            head.addStretch(2)
            root.addLayout(head)

            self.stepper = Stepper(["执行"])
            root.addWidget(self.stepper)

            # DEC-059: 引导定位每段人工确认改为**弹窗**（GuideConfirmDialog），
            # 不再在执行步骤上方占一条提示条；弹窗非模态，右卡图像仍可查看。

            mid = QHBoxLayout()
            mid.setSpacing(10)
            self._image_slot = mid
            mid.addWidget(self._steps_card(), 3)
            self.image_card = self._image_card()
            mid.addWidget(self.image_card, 4)
            root.addLayout(mid, 1)

            self.dep_strip = DeviceStrip()
            root.addWidget(self.dep_strip)

            ev = QFrame(); ev.setObjectName("DeviceStrip")
            evl = QHBoxLayout(ev); evl.setContentsMargins(12, 6, 12, 6); evl.setSpacing(10)
            t = QLabel("最近事件"); t.setObjectName("SectionTitle")
            self.event_label = QLabel("未下发动作"); self.event_label.setObjectName("EventRow")
            self.event_time = QLabel("-"); self.event_time.setObjectName("EventRow")
            evl.addWidget(t); evl.addWidget(self.event_time); evl.addWidget(self.event_label)
            evl.addStretch(1)
            root.addWidget(ev)

            bottom = QHBoxLayout(); bottom.setSpacing(10)
            self.block_box = QFrame(); self.block_box.setObjectName("BlockBox")
            bl = QVBoxLayout(self.block_box); bl.setContentsMargins(14, 8, 14, 8); bl.setSpacing(2)
            self.block_title = QLabel("初始检查尚未完成"); self.block_title.setObjectName("BlockTitle")
            self.block_sub = QLabel("连接、动作反馈、标定与急停需确认"); self.block_sub.setObjectName("BlockSub")
            self.block_sub.setWordWrap(True)
            bl.addWidget(self.block_title); bl.addWidget(self.block_sub)
            bottom.addWidget(self.block_box, 1)

            self.rail = ActionRail()
            self.rail.connect(self._on_action)
            bottom.addWidget(self.rail, 2)
            root.addLayout(bottom)

            self.hint = QLabel(""); self.hint.setObjectName("RailCaption")
            root.addWidget(self.hint)

        def _steps_card(self) -> Card:
            card = Card("执行步骤")
            # DEC-099：步骤多时卡片高度不能把整页往下撑，步骤行放进内部滚动区。
            self.steps_scroll = QScrollArea()
            self.steps_scroll.setWidgetResizable(True)
            self.steps_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            self.steps_scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.steps_container = QWidget()
            self.steps_layout = QVBoxLayout(self.steps_container)
            self.steps_layout.setContentsMargins(0, 0, 0, 0)
            self.steps_layout.setSpacing(6)
            self.steps_scroll.setWidget(self.steps_container)
            card.body().addWidget(self.steps_scroll, 1)
            self._step_rows: list[tuple[QLabel, StatusPill]] = []
            self._step_ids: list[str] = []
            self._step_labels: list[str] = []
            self.steps_layout.addStretch(1)
            return card

        def _rebuild_step_rows(self, ids: list[str],
                               labels: list[str] | None = None) -> None:  # pragma: no cover - Qt
            """按当前执行方案的步骤重建行（步骤集合/显示名变了才重建，避免每 tick 抖动）。

            ``labels`` = 中文显示名（DEC-037）：设了步骤名称用名称，否则用中文类型名；
            英文步骤 ID 只留在悬停提示里（条件分支等引用仍在配方页可见）。
            """
            names = list(labels) if labels is not None else list(ids)
            while self.steps_layout.count():
                item = self.steps_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None); w.deleteLater()
            self._step_rows = []
            self._step_ids = list(ids)
            self._step_labels = names
            for i, (sid, label) in enumerate(zip(ids, names)):
                row = QWidget(); h = QHBoxLayout(row)
                h.setContentsMargins(0, 4, 0, 4); h.setSpacing(10)
                num = QLabel(str(i + 1)); num.setObjectName("StepNum"); num.setFixedWidth(18)
                txt = QWidget(); tv = QVBoxLayout(txt)
                tv.setContentsMargins(0, 0, 0, 0); tv.setSpacing(2)
                tl = QLabel(label); tl.setObjectName("StepTitle")
                tl.setToolTip(f"步骤 ID：{sid}")
                tv.addWidget(tl)
                pill = StatusPill("待执行")
                h.addWidget(num); h.addWidget(txt, 1); h.addWidget(pill)
                self.steps_layout.addWidget(row)
                self._step_rows.append((tl, pill))
            if not ids:
                self.steps_layout.addWidget(EmptyHint("当前执行方案没有步骤。"))
            self.steps_layout.addStretch(1)

        def _image_card(self) -> Card:
            card = Card("相机通道 · 抓拍与检测圆")
            # DEC-094：页签+图像+脚注抽成 CaptureGalleryView，作业台与操作台弹窗共用。
            self.gallery = CaptureGalleryView("尚未抓拍 · 离线未采集真实图像")
            self.image_view = self.gallery.view
            self.image_box = self.image_view      # 兼容旧引用
            self.image_view.circle_edited.connect(self._on_circle_edited)
            self.image_view.box_requested.connect(self._on_box_requested)
            self.image_view.circle_drawn.connect(self._on_circle_drawn)
            self.image_view.layout_changed.connect(self._on_layout_changed)
            self.image_view.selection_changed.connect(self._on_target_selected)
            self.image_view.message.connect(self._on_target_message)
            self.image_view.mode_changed.connect(self._sync_target_mode_buttons)

            self.channel_tabs = self.gallery.tabs
            self.channel_tabs.currentChanged.connect(self._on_channel_selected)

            bar = QHBoxLayout(); bar.setSpacing(6)
            self.draw_box_btn = QPushButton("画框")
            self.draw_box_btn.setCheckable(True)
            self.draw_box_btn.setEnabled(False)
            self.draw_box_btn.setToolTip("在彩色图上按住左键拖出一个框；框内按检测配置找圆")
            self.draw_box_btn.toggled.connect(self._on_draw_box_toggled)
            self.draw_circle_btn = QPushButton("画圆")
            self.draw_circle_btn.setCheckable(True)
            self.draw_circle_btn.setEnabled(False)
            self.draw_circle_btn.setToolTip("在已有框内按住左键拖动画圆（以起点为圆心）")
            self.draw_circle_btn.toggled.connect(self._on_draw_circle_toggled)
            self.delete_target_btn = QPushButton("删除选中")
            self.delete_target_btn.setEnabled(False)
            self.delete_target_btn.setToolTip("删除选中的框；若选中的是框内的圆，只删圆")
            self.delete_target_btn.clicked.connect(self._on_delete_target)
            self.btn_save_img = QPushButton("保存图片")
            self.btn_save_img.setEnabled(False)
            self.btn_save_img.setToolTip(
                "保存当前抓拍帧：彩色 PNG + 原始深度（npy / 16位PNG / JSON 元数据）+ 伪彩预览；"
                "均不含显示旋转与预览算法")
            self.btn_save_img.clicked.connect(self._save_shown_image)
            self.btn_popup = QPushButton("弹窗")
            self.btn_popup.setCheckable(True)
            self.btn_popup.setToolTip("把相机通道画面弹出为独立窗口（成像与画框/画圆不变）；再点一次关闭")
            self.btn_popup.toggled.connect(
                lambda checked: self.toggle_image_popup(bool(checked),
                                                        owner="workbench"))
            bar.addWidget(self.draw_box_btn)
            bar.addWidget(self.draw_circle_btn)
            bar.addWidget(self.delete_target_btn)
            bar.addWidget(self.btn_save_img)
            bar.addWidget(self.btn_popup)
            bar.addStretch(1)
            self.gallery.add_toolbar(bar)

            # DEC-054: 操作员**显式指定**用哪个圆做定位（后续「引导定位」沿用）。
            pick = QHBoxLayout(); pick.setSpacing(6)
            pick.addWidget(QLabel("定位圆"))
            self.target_pick = QComboBox()
            self.target_pick.setObjectName("TargetPick")
            self.target_pick.setMinimumWidth(220)
            self.target_pick.setToolTip(
                "选择哪个框里的圆作为后续「引导定位」的定位圆；"
                "选「不指定」则引导定位自己重新找圆")
            self.target_pick.setEnabled(False)
            self.target_pick.currentIndexChanged.connect(self._on_target_pick)
            pick.addWidget(self.target_pick, 1)
            self.gallery.add_toolbar(pick)
            self._pick_busy = False

            self.target_status = QLabel("本步没有可调整的框/圆")
            self.target_status.setObjectName("SubLabel")
            self.target_status.setWordWrap(True)
            self.gallery.add_widget_toolbar(self.target_status)

            self.image_caption = self.gallery.caption
            self.image_time = self.gallery.time
            card.body().addWidget(self.gallery, 1)
            return card

        def _save_shown_image(self) -> None:  # pragma: no cover - Qt
            """DEC-121：保存当前抓拍帧（原始数据，不套显示旋转/预览算法）。"""
            from application.presentation.components.frame_export import (
                save_frame_files,
            )
            frame = getattr(self, "_shown_frame", None)
            if frame is None:
                return
            saved = save_frame_files(frame, self, title="保存抓拍图片")
            if saved:
                self.image_caption.setText("已保存：" + "；".join(saved))

        # ---------- 相机卡片弹窗（DEC-119，与操作台共用同一组件） ----------
        def _ensure_image_popup(self):
            popup = getattr(self, "_image_popup", None)
            if popup is None:
                from application.presentation.components.capture_popup import (
                    CapturePopup,
                )
                popup = CapturePopup(self.image_card, self._image_slot,
                                     parent=self.window(),
                                     title="相机通道 · 抓拍与检测圆")
                popup.finished.connect(self._on_image_popup_finished)
                self._image_popup = popup
            return popup

        def _on_image_popup_finished(self, *_a) -> None:  # pragma: no cover - Qt
            self._popup_open = False
            self._popup_owner = None
            btn = getattr(self, "btn_popup", None)
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)

        def toggle_image_popup(self, show: bool | None = None, *,
                               owner: str | None = None) -> bool:
            """打开/关闭相机卡片弹窗；返回打开后的状态。owner 记录谁打开的。"""
            popup = self._ensure_image_popup()
            want = (not self._popup_open) if show is None else bool(show)
            if want:
                popup.attach()
                popup.show()
                popup.raise_()
                self._popup_open = True
                self._popup_owner = owner
            else:
                popup.close()
                self._popup_open = False
                self._popup_owner = None
            btn = getattr(self, "btn_popup", None)
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(bool(want))
                btn.blockSignals(False)
            return want

        def image_popup_open(self) -> bool:
            return bool(getattr(self, "_popup_open", False))

        # ---------- 交互 ----------
        def _on_action(self, key: str) -> None:  # pragma: no cover - Qt
            if not self.vm.buttons().get(key) and key != "stop_all":
                self.refresh(); return
            console = getattr(self.window(), "pages", {}).get("console")
            monitor = getattr(console, "monitor", None)
            if key in ("start", "single_step", "resume") and monitor is not None:
                if not monitor.yield_camera():
                    self.vm._set_hint("正在结束监控采集，请稍后再次执行此操作", "warn")
                    self.window().refresh_all()
                    return
            try:
                if key == "start":
                    self.vm.start_current()
                elif key == "single_step":
                    self.vm.single_step()
                elif key == "fault_recover":
                    self._open_fault_recovery()
                elif key == "request_pause":
                    self.vm.request_pause()
                elif key == "resume":
                    self.vm.resume(True)
                elif key == "terminate":
                    self.vm.terminate()
                elif key == "stop_all":
                    self.vm.stop_all()
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"操作被拒绝: {exc}", "bad")  # noqa: SLF001
            # 同步刷新顶部与整窗
            win = self.window()
            if hasattr(win, "refresh_all"):
                win.refresh_all()
            else:
                self.refresh()

        def _open_fault_recovery(self) -> None:  # pragma: no cover - Qt
            """故障恢复弹窗：从失败步骤恢复 / 重新执行整个任务（DEC-061）。"""
            try:
                info = self.vm.fault_info()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "故障恢复", f"读取故障信息失败: {exc}")
                return
            dlg = FaultRecoveryDialog(self.window(), info)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                if dlg.choice() == "resume":
                    self.vm.resume_from_fault()
                else:
                    self.vm.restart_task()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "故障恢复失败", str(exc))
            win = self.window()
            if hasattr(win, "refresh_all"):
                win.refresh_all()
            else:
                self.refresh()

        def _on_confirm(self, choice: str) -> None:  # pragma: no cover - Qt
            """运行页提交确认（仅声明过的选项；节点也会再校验一次）。"""
            try:
                self.vm.provide_operator(choice, _operator_name())
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"确认失败: {exc}", "bad")  # noqa: SLF001
            win = self.window()
            if hasattr(win, "refresh_all"):
                win.refresh_all()
            else:
                self.refresh()

        def _sync_confirm(self) -> None:  # pragma: no cover - Qt
            """有等待中的引导定位确认就弹**非模态弹窗**，否则收起（DEC-059）。

            与步骤确认弹窗同一模式：提示出现才显示，提示消失即隐藏；选项变化时才重建按钮。
            非模态是为了操作员能一边看右卡图像一边决定 —— 确认前机械臂不会移动。
            """
            try:
                prompt = self.vm.pending_operator_prompt()
            except Exception:  # noqa: BLE001
                prompt = None
            dlg = self._guide_dialog
            if not prompt:
                if dlg is not None and dlg.isVisible():
                    dlg.mark_active(False)
                    dlg.hide()
                self._guide_prompt_key = None
                return
            dlg = self._ensure_guide_dialog()
            dlg.mark_active(True)
            key = (prompt.get("title"), prompt.get("message"),
                   tuple(prompt.get("options") or ()))
            changed = key != self._guide_prompt_key
            if changed:
                self._guide_prompt_key = key
                dlg.set_prompt(prompt)
                dlg.adjustSize()
            if not dlg.isVisible():
                dlg.show()
                dlg.raise_()
                dlg.activateWindow()
            elif changed:
                dlg.raise_()

        def _ensure_guide_dialog(self) -> "GuideConfirmDialog":  # pragma: no cover - Qt
            if self._guide_dialog is None:
                self._guide_dialog = GuideConfirmDialog(
                    self.window(), on_choice=self._on_confirm)
            return self._guide_dialog

        def _on_step_choice(self, choice: str) -> None:  # pragma: no cover - Qt
            """步骤确认框的选择：确认继续 / 不正确（不正确由服务层暂停）。"""
            try:
                self.vm.confirm_step(choice, _operator_name())
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"步骤确认失败: {exc}", "bad")  # noqa: SLF001
            win = self.window()
            if hasattr(win, "refresh_all"):
                win.refresh_all()
            else:
                self.refresh()

        def _ensure_step_dialog(self) -> "StepConfirmDialog":  # pragma: no cover - Qt
            if self._step_dialog is None:
                self._step_dialog = StepConfirmDialog(
                    self.window(), on_choice=self._on_step_choice)
            return self._step_dialog

        def _sync_step_dialog(self) -> None:  # pragma: no cover - Qt
            """有步骤待确认就显示（非模态）；提示消失即隐藏。"""
            try:
                prompt = self.vm.pending_step_prompt()
            except Exception:  # noqa: BLE001
                prompt = None
            dlg = self._step_dialog
            if not prompt:
                if dlg is not None and dlg.isVisible():
                    dlg.mark_active(False)
                    dlg.hide()
                self._step_prompt_key = None
                return
            try:
                circle = self.vm.step_circle_state()
            except Exception:  # noqa: BLE001
                circle = {"available": False}
            dlg = self._ensure_step_dialog()
            dlg.mark_active(True)
            key = (prompt.get("step_id"), prompt.get("index"), prompt.get("title"))
            if key != getattr(self, "_step_prompt_key", None):
                self._step_prompt_key = key
                dlg.set_prompt(prompt, circle_available=bool(circle.get("available")))
                dlg.adjustSize()
            if circle.get("available") and circle.get("manual"):
                _dia = float(circle.get("diameter_mm") or 0.0)
                dlg.set_status_text(
                    f"当前判定（手动圆）：{circle.get('status')} · "
                    f"{circle.get('reason') or ''}"
                    + (f" · 直径≈{_dia:.1f}mm" if _dia > 0 else ""))
            if not dlg.isVisible():
                dlg.show()

        def _on_circle_edited(self, cx: int, cy: int, r: int) -> None:  # pragma: no cover - Qt
            """右卡拖动检测圆松开：**以手动圆为准**重算视觉判定（DEC-042）。"""
            try:
                info = self.vm.apply_step_circle(cx, cy, r, _operator_name())
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"手动检测圆未应用: {exc}", "bad")  # noqa: SLF001
                return
            if self._step_dialog is not None:
                _dia = float(info.get("diameter_mm") or 0.0)
                self._step_dialog.set_status_text(
                    f"已按手动圆判定：{info.get('status')} · "
                    f"圆心 ({info.get('cx'):.0f}, {info.get('cy'):.0f}) "
                    f"半径 {info.get('radius_px'):.0f}px"
                    + (f" · 直径≈{_dia:.1f}mm" if _dia > 0 else ""))
            win = self.window()
            if hasattr(win, "refresh_all"):
                win.refresh_all()
            else:
                self.refresh()

        # ---------- 多相机页签（DEC-051） ----------
        def _configured_cameras(self) -> list:  # pragma: no cover - Qt
            """设备配置里**已启用**的相机（顺序同配置文件）: [(camera_id, 显示名)]。"""
            try:
                store = getattr(getattr(self.ctx, "stack", None), "device_config", None)
                cfg = getattr(store, "config", None)
                cams = list(getattr(cfg, "cameras", None) or [])
            except Exception:  # noqa: BLE001 - 读不到配置就退化为不显示页签
                return []
            return [(c.camera_id, c.name or c.camera_id) for c in cams
                    if getattr(c, "enabled", False) and getattr(c, "camera_id", "")]

        def _selected_camera_id(self):  # pragma: no cover - Qt
            if not hasattr(self, "channel_tabs"):
                return None
            idx = self.channel_tabs.currentIndex()
            if idx < 0:
                return None
            return self.channel_tabs.tabData(idx) or None

        def _select_camera_tab(self, camera_id) -> None:  # pragma: no cover - Qt
            for i in range(self.channel_tabs.count()):
                if self.channel_tabs.tabData(i) == camera_id:
                    self.channel_tabs.blockSignals(True)
                    self.channel_tabs.setCurrentIndex(i)
                    self.channel_tabs.blockSignals(False)
                    self._shown_frame_id = None
                    return

        def _preferred_camera(self, channels, frames, latest, state):  # pragma: no cover - Qt
            """自动跟随：待确认视觉步骤那一帧的相机 > 最新抓拍相机 > 第一个页签。"""
            ids = [cid for cid, _name in channels]
            step_frame_id = state.get("frame_id") if state.get("available") else None
            if step_frame_id:
                for cid, frame in frames.items():
                    if getattr(frame, "frame_id", None) == step_frame_id and cid in ids:
                        return cid
            cam = getattr(latest, "camera_id", None) if latest is not None else None
            if cam in ids:
                return cam
            return ids[0] if ids else None

        def _sync_channel_tabs(self, frames, latest, state) -> None:  # pragma: no cover - Qt
            """页签 = 设备配置已启用相机；配置变化时重建，标题用相机名称。"""
            channels = self._configured_cameras()
            sig = tuple(channels)
            if sig != self._channel_sig:
                prev = self._selected_camera_id()
                self._channel_sig = sig
                self.channel_tabs.blockSignals(True)
                while self.channel_tabs.count():
                    self.channel_tabs.removeTab(0)
                for cid, name in channels:
                    idx = self.channel_tabs.addTab(name)
                    self.channel_tabs.setTabData(idx, cid)
                    self.channel_tabs.setTabToolTip(idx, f"相机通道：{cid}")
                self.channel_tabs.blockSignals(False)
                self.channel_tabs.setVisible(bool(channels))
                self._shown_frame_id = None
                ids = [cid for cid, _n in channels]
                want = prev if prev in ids else None
                if want is None:
                    want = self._preferred_camera(channels, frames, latest, state)
                if want:
                    self._select_camera_tab(want)
            elif channels and self._auto_follow:
                want = self._preferred_camera(channels, frames, latest, state)
                if want and want != self._selected_camera_id():
                    self._select_camera_tab(want)

        def _on_channel_selected(self, _index: int) -> None:  # pragma: no cover - Qt
            # 操作员手动选页签后不再自动切；下一次新抓拍/新待确认步骤恢复跟随
            self._auto_follow = False
            self._shown_frame_id = None
            self._sync_image_card()

        def _camera_label(self, camera_id) -> str:  # pragma: no cover - Qt
            for cid, name in (self._channel_sig or ()):
                if cid == camera_id:
                    return f"{name}（{cid}）" if name != cid else cid
            return str(camera_id or "-")

        def _sync_image_card(self) -> None:  # pragma: no cover - Qt
            """右卡：按相机页签显示本 run 各相机最近抓拍；仅 DS86 视觉帧可调框/圆。"""
            try:
                frames = dict(self.vm.current_frames())
            except Exception:  # noqa: BLE001
                frames = {}
            try:
                latest = self.vm.current_frame()
            except Exception:  # noqa: BLE001
                latest = None
            try:
                state = self.vm.step_circle_state()
                state = dict(state) if isinstance(state, dict) else {"available": False}
            except Exception:  # noqa: BLE001
                state = {"available": False}
            try:
                guide = self.vm.guide_circle_state()
                guide = dict(guide) if isinstance(guide, dict) else {"available": False}
            except Exception:  # noqa: BLE001
                guide = {"available": False}

            # 有新抓拍 / 出现新的待确认视觉步骤 -> 恢复“自动跟随页签”
            run_key = ((getattr(latest, "run_id", None), getattr(latest, "frame_id", None))
                       if latest is not None else None)
            prompt_key = ((state.get("step_id"), state.get("frame_id"))
                          if state.get("available") else None)
            if run_key != self._seen_run_key or prompt_key != self._seen_prompt_key:
                self._seen_run_key, self._seen_prompt_key = run_key, prompt_key
                self._auto_follow = True

            self._sync_channel_tabs(frames, latest, state)
            camera_id = self._selected_camera_id()
            frame = frames.get(camera_id) if camera_id else None
            # 没有按相机缓存时（旧执行器/测试替身）退回“最近一次抓拍”；
            # 有缓存就以缓存为准——某通道没抓拍过要如实占位，不拿别的相机顶替。
            # 例外：设备配置里**没有已启用相机**时页签不显示，此时直接显示
            # 最近一次抓拍帧（离线/未配置相机的场景，图是真抓的，照常展示）。
            if frame is None and latest is not None \
                    and (camera_id is None
                         or (not frames
                             and getattr(latest, "camera_id", None) == camera_id)):
                frame = latest

            # DEC-084：引导定位（粗/精）检测完成时，右卡显示**引导实际使用的帧**，
            # 并画出检测圆 —— 原先帧不在执行器缓存里、也没圆，操作员看不到对准依据。
            guide_active = bool(guide.get("available") and guide.get("active"))
            guide_frame = guide.get("frame") if guide_active else None
            if guide_frame is not None:
                frame = guide_frame

            # DEC-119：DS86 相机画面按显示旋转 270°（只转显示，框/圆坐标仍是原图）
            self._shown_frame = frame
            self.btn_save_img.setEnabled(
                frame is not None and (getattr(frame, "color", None) is not None
                                       or getattr(frame, "depth", None) is not None))
            shown_cam = (getattr(frame, "camera_id", None) if frame is not None
                         else camera_id)
            self.image_view.set_display_rotation(
                270 if str(shown_cam or "").lower().startswith("ds86") else 0)
            color = getattr(frame, "color", None) if frame is not None else None
            if frame is not None:
                key = (getattr(frame, "camera_id", None),
                       getattr(frame, "run_id", None), getattr(frame, "frame_id", None))
                if key != self._shown_frame_id:
                    if color is not None:
                        try:
                            self.image_view.show_array(color.readonly_view(), kind="color")
                        except Exception:  # noqa: BLE001
                            self.image_view.show_array(color.data, kind="color")
                    else:
                        self.image_view.clear_image("该帧无彩色流")
                    self._shown_frame_id = key
            else:
                if self._shown_frame_id is not None:
                    self.image_view.clear_image()
                    self._shown_frame_id = None
                if camera_id:
                    self.image_view.clear_image(
                        f"{self._camera_label(camera_id)}：本次运行尚未抓拍")

            # 框/圆调整只对**本步视觉帧**、且只在 DS86 相机图上（用户 2026-10-21 裁定）
            step_frame_id = state.get("frame_id") if state.get("available") else None
            on_step_frame = bool(frame is not None and step_frame_id
                                 and getattr(frame, "frame_id", None) == step_frame_id)
            cam_id = getattr(frame, "camera_id", None) if frame is not None else None
            is_ds86 = str(cam_id or "").lower().startswith("ds86")
            drawable = bool(state.get("available") and on_step_frame and is_ds86)
            if drawable:
                self._sync_target_layout(state)
            else:
                reason = "本步没有可调整的框/圆"
                if state.get("available"):
                    step_cam = None
                    for cid, f in frames.items():
                        if getattr(f, "frame_id", None) == step_frame_id:
                            step_cam = cid
                            break
                    if frame is None:
                        reason = "该步视觉相机本次运行尚未抓拍，抓拍后才可调整框/圆"
                    elif not on_step_frame:
                        reason = (f"框/圆调整在本步视觉相机"
                                  f"（{self._camera_label(step_cam or step_frame_id)}）上；"
                                  f"此处只读预览")
                    elif not is_ds86:
                        reason = "当前只支持在 DS86 相机图上调整框/圆；此处只读预览"
                self._sync_target_layout({"available": False, "reason": reason})

            # DEC-084：引导定位的检测圆（只读，画在当前显示的引导帧上）
            if guide_active and guide_frame is not None:
                c = guide.get("circle") or {}
                try:
                    cx, cy = float(c["cx"]), float(c["cy"])
                    r = max(float(c.get("radius_px") or 0.0), 1.0)
                except (KeyError, TypeError, ValueError):
                    self.image_view.clear_circle_overlay()
                    self.image_view.clear_points_overlay()
                else:
                    self.image_view.set_circle_overlay(cx, cy, r)
                    self.image_view.set_points_overlay([(cx, cy)])
            else:
                self.image_view.clear_circle_overlay()
                self.image_view.clear_points_overlay()

            if frame is not None and color is not None:
                cap = (f"相机 {self._camera_label(getattr(frame, 'camera_id', None))} · "
                       f"抓拍步骤：{getattr(frame, 'step_id', None) or '-'}")
                if drawable:
                    boxes = state.get("boxes") or []
                    n_box = len(boxes)
                    n_circle = len([b for b in boxes if b.get("circle")])
                    cap += (f" · 视觉判定 {state.get('status')}"
                            f"{'（手动框/圆）' if state.get('manual') else ''}"
                            f" · 框 {n_box}/圆 {n_circle}")
                else:
                    cap += " · 原始彩色图"
                self.image_caption.setText(cap)
                # DEC-038: 界面时间一律北京时间显示（存储仍是 UTC）
                self.image_time.setText(
                    to_beijing_text(getattr(frame, "utc_recorded", None)) or "-")
            elif frame is not None:
                self.image_caption.setText("该帧无彩色流")
                self.image_time.setText("-")
            else:
                self.image_caption.setText(
                    "该通道本次运行尚未抓拍" if camera_id else "此图未用于当前任务")
                self.image_time.setText("-")

            if guide_active and guide_frame is not None:
                c = guide.get("circle") or {}
                stage = str(guide.get("stage") or "")
                label = "粗定位" if stage == "coarse" else (
                    "精定位" if stage == "fine" else "引导定位")
                cap = (f"引导定位·{label} · 相机 "
                       f"{self._camera_label(getattr(guide_frame, 'camera_id', None))}")
                try:
                    cap += (f" · 检测圆 ({float(c['cx']):.0f}, {float(c['cy']):.0f}) "
                            f"r={float(c.get('radius_px') or 0.0):.0f}px")
                except (KeyError, TypeError, ValueError):
                    pass
                self.image_caption.setText(cap)
                # DEC-038: 界面时间一律北京时间显示（存储仍是 UTC）
                self.image_time.setText(
                    to_beijing_text(getattr(guide_frame, "utc_recorded", None)) or "-")

        def _set_target_buttons(self, enabled: bool) -> None:  # pragma: no cover - Qt
            for btn in (self.draw_box_btn, self.draw_circle_btn,
                        self.delete_target_btn):
                btn.setEnabled(bool(enabled))

        def _sync_target_mode_buttons(self, mode: str) -> None:  # pragma: no cover - Qt
            for btn, name in ((self.draw_box_btn, "box"),
                              (self.draw_circle_btn, "circle")):
                btn.blockSignals(True)
                btn.setChecked(mode == name)
                btn.blockSignals(False)

        def _sync_target_layout(self, state: dict) -> None:  # pragma: no cover - Qt
            """右卡同步节点里的多框/多圆布局；拖动期间不回灌，避免打断操作。"""
            available = bool(state.get("available")) and self.image_view.available
            self._set_target_buttons(available)
            if not available:
                self._sync_target_pick([], None, False)
                if not self.image_view.is_dragging():
                    self.image_view.set_target_mode(None)
                    self.image_view.clear_target_layout()
                self.target_status.setText(
                    str(state.get("reason") or "本步没有可调整的框/圆"))
                return
            boxes = state.get("boxes") or []
            selected = state.get("selected")
            self._sync_target_pick(boxes, selected, True)
            if self.image_view.is_dragging():
                return
            self.image_view.set_layout(boxes, selected)
            n_circle = len([b for b in boxes if b.get("circle")])
            parts = [f"框 {len(boxes)} 个 · 圆 {n_circle} 个"]
            if selected is not None and 0 <= selected < len(boxes) \
                    and boxes[selected].get("circle"):
                parts.append(f"定位圆=框 {selected + 1}")
            else:
                parts.append("未选中定位圆（引导定位将重新找圆）")
            if state.get("manual"):
                parts.append("已按手动框/圆判定")
            if state.get("layout_message"):
                parts.append(str(state["layout_message"]))
            self.target_status.setText("；".join(parts))

        def _sync_target_pick(self, boxes, selected,  # pragma: no cover - Qt
                              available: bool) -> None:
            """「定位圆」下拉：列所有框内圆 + 首项「不指定」，与节点选择同步。"""
            self._pick_busy = True
            try:
                self.target_pick.clear()
                if not available:
                    self.target_pick.setEnabled(False)
                    return
                self.target_pick.addItem("（不指定）引导定位将重新找圆", -1)
                for i, b in enumerate(boxes or []):
                    c = b.get("circle")
                    if not c:
                        continue
                    self.target_pick.addItem(
                        f"框 {i + 1} · ({float(c['cx']):.0f},"
                        f"{float(c['cy']):.0f}) r={float(c['radius_px']):.1f}px",
                        i)
                want = (int(selected) if selected is not None
                        and 0 <= int(selected) < len(boxes or [])
                        and (boxes or [])[int(selected)].get("circle") else -1)
                idx = self.target_pick.findData(want)
                self.target_pick.setCurrentIndex(idx if idx >= 0 else 0)
                has_circle = self.target_pick.count() > 1
                self.target_pick.setEnabled(has_circle)
            finally:
                self._pick_busy = False

        # ---- 多框/多圆交互（DEC-048） ----
        def _on_draw_box_toggled(self, checked: bool) -> None:  # pragma: no cover - Qt
            if checked:
                self.image_view.set_target_mode("box")
            elif self.image_view.target_mode() == "box":
                self.image_view.set_target_mode(None)

        def _on_draw_circle_toggled(self, checked: bool) -> None:  # pragma: no cover - Qt
            if checked:
                self.image_view.set_target_mode("circle")
            elif self.image_view.target_mode() == "circle":
                self.image_view.set_target_mode(None)

        def _on_delete_target(self) -> None:  # pragma: no cover - Qt
            self.image_view.delete_selected()

        def _on_target_message(self, text: str) -> None:  # pragma: no cover - Qt
            self.target_status.setText(text or "")
            if text:
                self.vm._set_hint(text, "warn")  # noqa: SLF001

        def _on_target_selected(self, kind: str, index: int) -> None:  # pragma: no cover - Qt
            if index is None or index < 0:
                return
            try:
                self.vm.select_step_circle(int(index), _operator_name())
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"选择框/圆失败: {exc}", "warn")  # noqa: SLF001
            self._sync_target_pick_from_state()

        def _on_target_pick(self, index: int) -> None:  # pragma: no cover - Qt
            """「定位圆」下拉：操作员显式指定（首项=不指定，引导定位重新找圆）。"""
            if self._pick_busy:
                return
            data = self.target_pick.itemData(index)
            box = None if data is None or int(data) < 0 else int(data)
            try:
                self.vm.select_step_circle(box, _operator_name())
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"指定定位圆失败: {exc}", "warn")  # noqa: SLF001
            # 让图卡高亮/状态文字跟上（选择本身不重算判定，见 DEC-048/054）
            state = self.vm.step_circle_state()
            if state.get("available"):
                self._sync_target_layout(state)

        def _sync_target_pick_from_state(self) -> None:  # pragma: no cover - Qt
            """点图选择后把下拉跟上（节点选择已变，重算布局除外）。"""
            try:
                state = self.vm.step_circle_state()
            except Exception:  # noqa: BLE001
                return
            if not state.get("available"):
                return
            self._sync_target_pick(state.get("boxes") or [],
                                   state.get("selected"), True)

        def _apply_layout_result(self, info: dict) -> None:  # pragma: no cover - Qt
            dlg = self._step_dialog
            if dlg is not None and dlg.isVisible():
                circle = info.get("circle")
                if circle:
                    dlg.set_status_text(
                        f"已按手动框/圆判定：{info.get('status')} · "
                        f"框 {int(info.get('selected_box') or 0) + 1} 圆心 "
                        f"({circle['cx']:.0f}, {circle['cy']:.0f}) "
                        f"半径 {circle['radius_px']:.0f}px")
                else:
                    dlg.set_status_text(
                        f"已按手动框/圆判定：{info.get('status')} · 当前没有检测圆")
            win = self.window()
            if hasattr(win, "refresh_all"):
                win.refresh_all()
            else:
                self.refresh()

        def _on_box_requested(self, x1: int, y1: int, x2: int, y2: int) -> None:  # pragma: no cover - Qt
            try:
                info = self.vm.add_step_box(x1, y1, x2, y2, _operator_name())
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"新增框未应用: {exc}", "bad")  # noqa: SLF001
                return
            if info.get("message"):
                self.target_status.setText(str(info["message"]))
            self._apply_layout_result(info)

        def _on_circle_drawn(self, box_index: int, cx: int, cy: int, r: int) -> None:  # pragma: no cover - Qt
            try:
                info = self.vm.set_step_circle(box_index, cx, cy, r, _operator_name())
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"画圆未应用: {exc}", "bad")  # noqa: SLF001
                return
            self._apply_layout_result(info)

        def _on_layout_changed(self) -> None:  # pragma: no cover - Qt
            layout = self.image_view.layout()
            try:
                info = self.vm.apply_step_layout(
                    layout.get("boxes") or [], layout.get("selected"),
                    _operator_name())
            except Exception as exc:  # noqa: BLE001
                self.vm._set_hint(f"框/圆调整未应用: {exc}", "bad")  # noqa: SLF001
                return
            self._apply_layout_result(info)

        def _on_recipe_selected(self, index: int) -> None:  # pragma: no cover - Qt
            """用户在下拉里选了另一条执行方案（DEC-041）。运行中不允许切换。"""
            if self._combo_busy:
                return
            rid = self.recipe_combo.itemData(index)
            if not rid or rid == self.vm.selected_recipe:
                return
            if self.vm.is_task_active():
                self.refresh()   # 还原下拉显示, 不动正在跑的任务
                return
            self.vm.select_recipe(rid)
            self.vm._set_hint(f"已选择执行方案 {rid}", "ok")  # noqa: SLF001
            win = self.window()
            if hasattr(win, "refresh_all"):
                win.refresh_all()
            else:
                self.refresh()

        def _sync_recipe_combo(self, st) -> None:  # pragma: no cover - Qt
            """把目录里的执行方案同步进下拉；仅在列表变化时重建（避免每 tick 抖动）。"""
            ids = list(st.recipes)
            if ids != self._combo_ids:
                self._combo_ids = ids
                self._combo_busy = True
                try:
                    self.recipe_combo.clear()
                    if not ids:
                        self.recipe_combo.addItem("（无可用执行方案）", None)
                    for rid in ids:
                        self.recipe_combo.addItem(rid, rid)
                finally:
                    self._combo_busy = False
            idx = self.recipe_combo.findData(st.selected_recipe)
            if idx >= 0 and self.recipe_combo.currentIndex() != idx:
                self._combo_busy = True
                try:
                    self.recipe_combo.setCurrentIndex(idx)
                finally:
                    self._combo_busy = False
            active = self.vm.is_task_active()
            self.recipe_combo.setEnabled(bool(ids) and not active)
            self.recipe_combo.setToolTip(
                "运行中，不能切换执行方案；停止后可切换"
                if active else
                "选择要执行的执行方案（启动时自动发现的配方目录，DEC-035）")

        def _on_tick(self) -> None:  # pragma: no cover - Qt
            if self._autotick and self.vm.state().task_state == "RUNNING":
                self.vm.tick()
                win = self.window()
                if hasattr(win, "refresh_all"):
                    win.refresh_all()
                else:
                    self.refresh()

        def shutdown(self) -> None:
            """停止页面自带定时器 (关闭/退出前调用, 避免回调打到已销毁对象)。"""
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
            dlg = self._step_dialog
            if dlg is not None:
                try:
                    dlg.mark_active(False)
                    dlg.close()
                except Exception:  # noqa: BLE001
                    pass
            gdlg = self._guide_dialog
            if gdlg is not None:
                try:
                    gdlg.mark_active(False)
                    gdlg.close()
                except Exception:  # noqa: BLE001
                    pass

        # ---------- 刷新 ----------
        def refresh(self) -> None:  # pragma: no cover - Qt
            st = self.vm.state()
            self._sync_confirm()
            self._sync_recipe_combo(st)
            self._sync_image_card()
            self._sync_step_dialog()
            fg, _bg = state_colors(st.task_state)
            self.state_pill.set_text(_STATE_LABEL.get(st.task_state, st.task_state),
                                     tone=fg)

            # 已完成/已停止后，用户若在下拉里换了执行方案，旧 run 的步骤/版本/事件
            # 不能继续顶在界面上（否则看起来“选了不刷新”）。只有任务进行中，或
            # 当前选中方案就是这份 run 的方案时，才沿用 run 的结果。
            active = self.vm.is_task_active()
            same_recipe = (not st.recipe_id) or (st.recipe_id == st.selected_recipe)
            use_run = bool(st.steps) and (active or same_recipe)
            version = (st.recipe_version if use_run
                       else self._declared_recipe_version(st.selected_recipe))
            self.kv_version.set_key("执行方案版本", version or "-")

            # 步骤清单: 有编译结果用编译出的步骤, 否则用执行方案声明的步骤；
            # 显示名（DEC-037）= 步骤名称，未设则中文类型名（英文 ID 只进悬停提示）。
            run_ids = [s.step_id for s in st.steps] if use_run else []
            ids = run_ids or self._declared_step_ids(st.selected_recipe)
            titles = self._step_titles(
                (st.recipe_id or st.selected_recipe) if use_run else st.selected_recipe)
            labels = [titles.get(sid, sid) for sid in ids]
            if ids != self._step_ids or labels != getattr(self, "_step_labels", []):
                self._rebuild_step_rows(ids, labels)
            status_by_id = ({s.step_id: s.status for s in st.steps}
                            if use_run else {})
            for sid, (_lbl, pill) in zip(self._step_ids, self._step_rows):
                status = status_by_id.get(sid, "PENDING")
                pill.set_text(_STATUS_CN.get(status, status))
            done = sum(1 for s in st.steps if s.status == "SUCCESS") if use_run else 0
            total = max(len(self._step_ids), 1)
            self.stepper.set_stages(["执行"])
            self.stepper.set_current(0, completed=1 if use_run and st.steps
                                    and done >= total else 0)

            self.dep_strip.set_items([
                ("控制板1", "待核对"), ("ECO65", "待连接"), ("DS86", "待连接"),
            ])
            if use_run and st.image_source != "NONE":
                self.event_label.setText("已抓拍并判定")
            ev_text, ev_time = "未下发动作", "-"
            if use_run and st.run_id:
                events = []
                try:
                    events = list(self.ctx.stack.records.replay(st.run_id))
                except Exception:  # noqa: BLE001
                    events = []
                if events:
                    last = events[-1]
                    ev_text = f"{last.get('event_type')}: {last.get('message') or ''}"
                    ev_time = to_beijing_text(last.get("utc"))   # 北京时间（DEC-038）
                else:
                    ev_text, ev_time = "任务已启动（未下发动作）", st.run_id
            self.event_label.setText(ev_text)
            self.event_time.setText(ev_time)

            if st.blocking_reasons:
                self._style_block(blocked=True)
                self.block_title.setText("初始检查尚未完成")
                self.block_sub.setText("；".join(st.blocking_reasons))
            else:
                self._style_block(blocked=False)
                self.block_title.setText("可以启动作业")
                self.block_sub.setText("连接、动作反馈、标定与急停需确认")

            self.rail.set_enabled(st.buttons)
            self.hint.setText(st.hint)

        def _style_block(self, *, blocked: bool) -> None:  # pragma: no cover - Qt
            box, title, sub = ("BlockBox", "BlockTitle", "BlockSub") if blocked else \
                ("BlockBoxOk", "BlockTitleOk", "BlockSubOk")
            for w, name in ((self.block_box, box), (self.block_title, title),
                            (self.block_sub, sub)):
                w.setObjectName(name)
                w.style().unpolish(w)   # objectName 变更后需重新应用样式表
                w.style().polish(w)

        def _declared_step_ids(self, recipe_id) -> list[str]:  # pragma: no cover - Qt
            """未启动时, 取执行方案声明的步骤顺序, 让步骤清单先显示出来。"""
            if not recipe_id:
                return []
            svc = getattr(self.ctx.stack, "recipes", None)
            if svc is None:
                return []
            try:
                model = svc.get_draft(recipe_id)
                return [s.id for s in model.sequence]
            except Exception:  # noqa: BLE001
                return []

        def _declared_recipe_version(self, recipe_id) -> str | None:
            """选中方案自身的版本号（未运行/切换到别的方案后显示）。"""
            if not recipe_id:
                return None
            svc = getattr(self.ctx.stack, "recipes", None)
            if svc is None:
                return None
            try:
                return svc.get_draft(recipe_id).version
            except Exception:  # noqa: BLE001
                return None

        def _step_titles(self, recipe_id) -> dict[str, str]:  # pragma: no cover - Qt
            """步骤显示名（DEC-037）: id -> 中文名；未设名称回退中文类型名。"""
            if not recipe_id:
                return {}
            svc = getattr(self.ctx.stack, "recipes", None)
            if svc is None:
                return {}
            try:
                model = svc.get_draft(recipe_id)
                return {s.id: step_title(s) for s in model.sequence}
            except Exception:  # noqa: BLE001
                return {}


_STATE_LABEL = {
    "IDLE": "待检查", "VALIDATING": "检查中", "READY": "就绪",
    "RUNNING": "执行中", "PAUSE_REQUESTED": "正在暂停", "PAUSED": "已暂停",
    "WAITING_OPERATOR": "待人工处理", "STOPPING": "停止中",
    "STOPPED": "已停止", "FAULT": "故障", "COMPLETED": "已完成",
}


__all__ = ["GUI_REQUIRED", "WorkbenchPage"]
