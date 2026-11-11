"""「参数配置」页（DEC-065）。

左侧分组；右侧可编辑表单。所有修改先落在**内存配置对象**，点「保存」后原子落盘：
- 全局参数（运动参数 / 工具系·工作系 / 杆塔宽度算法 / 螺栓直径，两列展示）：
  运动参数在 `configs/motion/motion.yaml` 的 profile；两个计算算法在
  `configs/operations/manual_ops.yaml`（DEC-148）；
  工具系/工作系列表来自**已连接机器人**；离线/读取失败时只读显示当前值，不允许手填。
- 快捷键：全局快捷键绑定（DEC-115，存 `configs/runtime/settings.yaml`；
  原「视觉引导参数」块已从本页移除，guidance.yaml 仍由引导定位使用）。
- 精定位偏移参数：默认套（offsets.fine/tcp）+ 可命名的额外套 `fine_profiles`；
  执行方案的「引导定位」步骤可选择用哪一套。
- 运行/日志参数：`configs/runtime/settings.yaml`（日志文件级别/保留天数/控制台开关；
  改后需重启生效）。
"""
from __future__ import annotations

import math
import os

from application.algorithms.guidance.offsets import AxisMapping, OffsetParams
from application.bootstrap import app_paths
from application.presentation.components import Card, EmptyHint
from application.services.guidance_config import FineOffsetProfile
from application.services.runtime_config import (
    DEFAULT_SHORTCUTS, LOG_LEVELS, SHORTCUT_ACTIONS, RuntimeSettings,
    RuntimeSettingsStore, duplicate_shortcuts,
)

try:  # pragma: no cover
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence
    from PySide6.QtWidgets import (
        QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,
        QGroupBox, QHBoxLayout, QInputDialog, QKeySequenceEdit, QLabel, QLineEdit,
        QListWidget,
        QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
        QSpinBox, QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

_CATEGORIES = (
    ("motion", "全局参数"),
    ("guidance", "视觉引导参数"),
    ("fine", "精定位偏移参数"),
    ("runtime", "运行/日志参数"),
)


def _fit_combo(combo: "QComboBox", *, chars: int = 18) -> "QComboBox":
    # 与配方页同口径的限宽（DEC-036）
    combo.setMaximumWidth(combo.fontMetrics().horizontalAdvance("M") * chars + 48)
    return combo


if GUI_REQUIRED:

    class SettingsPage(QWidget):
        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.stack = ctx.stack
            self.motion_store = getattr(self.stack, "motion_config", None)
            guidance = getattr(getattr(self.stack, "motion", None), "guidance", None)
            self.guidance_store = getattr(guidance, "config_store", None)
            self.operation_store = getattr(self.stack, "operation_config", None)
            self.runtime_store = RuntimeSettingsStore(os.path.join(
                app_paths.configs_dir(), "runtime", "settings.yaml"))
            try:
                self.runtime_store.load()
            except Exception:  # noqa: BLE001 - 坏文件用默认, 保存时重建
                self.runtime_store._settings = RuntimeSettings()  # noqa: SLF001
            self._build()

        # ---------- 构建 ----------
        def _build(self) -> None:
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            # 四块**同时展示**（左上/右上/左下/右下）；每块内部独立滚动，
            # 避免某一组参数特别长把其它三块挤没。
            self.body = QWidget()
            self.form_lay = QGridLayout(self.body)
            self.form_lay.setContentsMargins(0, 0, 0, 0)
            self.form_lay.setSpacing(10)
            self.form_lay.setColumnStretch(0, 1)
            self.form_lay.setColumnStretch(1, 1)
            self.form_lay.setRowStretch(0, 1)
            self.form_lay.setRowStretch(1, 1)
            root.addWidget(self.body, 1)

            bar = QHBoxLayout()
            self.status = QLabel("")
            self.status.setObjectName("SubLabel")
            bar.addWidget(self.status, 1)
            save = QPushButton("保存参数")
            save.setObjectName("Primary")
            save.clicked.connect(self._save)
            bar.addWidget(save)
            root.addLayout(bar)
            self._rebuild()

        def _clear(self) -> None:
            while self.form_lay.count():
                item = self.form_lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
                elif item.layout() is not None:
                    self._clear_layout(item.layout())

        @staticmethod
        def _clear_layout(layout) -> None:
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
                elif item.layout() is not None:
                    SettingsPage._clear_layout(item.layout())

        def _section_card(self, title: str, factory) -> Card:
            card = Card(title)
            card.setMinimumHeight(240)
            card.setSizePolicy(QSizePolicy.Policy.Expanding,
                               QSizePolicy.Policy.Expanding)
            inner = QScrollArea()
            inner.setWidgetResizable(True)
            inner.setFrameShape(QFrame.Shape.NoFrame)
            try:
                content = factory()
            except Exception as exc:  # noqa: BLE001 - 读配置失败如实显示, 不挡其它块
                content = EmptyHint(f"读取参数失败: {exc}")
            inner.setWidget(content)
            card.add(inner)
            return card

        def _rebuild(self) -> None:
            self._clear()
            sections = (
                (0, 0, "全局参数", self._motion_form),
                (0, 1, "快捷键", self._shortcuts_form),
                (1, 0, "精定位偏移参数", self._fine_form),
                (1, 1, "运行 / 日志参数", self._runtime_form),
            )
            for row, col, title, factory in sections:
                self.form_lay.addWidget(self._section_card(title, factory), row, col)

        # ---------- 通用控件 ----------
        def _spin(self, value, lo, hi, *, decimals=0, suffix="", step=1.0,
                  on_change=None) -> QWidget:
            if decimals:
                w = QDoubleSpinBox(); w.setDecimals(decimals)
                w.setSingleStep(step)
                w.setRange(float(lo), float(hi))
                w.setValue(float(value))
            else:
                w = QSpinBox(); w.setSingleStep(int(step))
                w.setRange(int(lo), int(hi))
                w.setValue(int(value))
            if suffix:
                w.setSuffix(suffix)
            if on_change is not None:
                w.valueChanged.connect(on_change)
            return w

        def _combo(self, options, current, on_change=None) -> QComboBox:
            c = _fit_combo(QComboBox())
            for opt in options:
                c.addItem(str(opt), opt)
            idx = c.findData(current)
            c.setCurrentIndex(idx if idx >= 0 else 0)
            if on_change is not None:
                c.currentIndexChanged.connect(
                    lambda _i, cb=c: on_change(cb.currentData()))
            return c

        def _check(self, value, label, on_change=None) -> QCheckBox:
            c = QCheckBox(label); c.setChecked(bool(value))
            if on_change is not None:
                c.toggled.connect(lambda v: on_change(bool(v)))
            return c

        def _line(self, value, on_change=None, placeholder="") -> QLineEdit:
            e = QLineEdit("" if value is None else str(value))
            e.setPlaceholderText(placeholder)
            if on_change is not None:
                e.editingFinished.connect(lambda: on_change(e.text().strip()))
            return e

        # ---------- 运动全局 ----------
        def _frame_options(self, kind: str):
            """返回 (options, reason)。机器人读取失败/离线 -> 只读。"""
            arm = getattr(getattr(self.stack, "motion", None), "arm", None)
            fn = getattr(arm, "get_frame_list", None)
            if not callable(fn):
                return [], "未接入机械臂"
            try:
                fl = fn()
            except Exception as exc:  # noqa: BLE001
                return [], f"读取失败: {exc}"
            if not isinstance(fl, dict):
                return [], "读取失败"
            names = [str(x) for x in (fl.get(kind) or [])]
            return names, "" if names else "机器人未返回列表"

        def _motion_form(self) -> QWidget:
            if self.motion_store is None:
                return EmptyHint("未接入运动配置（motion.yaml）")
            prof = self.motion_store.config.profile
            left = QWidget(); lay = QVBoxLayout(left)
            lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(8)
            g = QGroupBox("坐标系（来自已连接机器人）")
            form = QFormLayout(g)

            def _frame_row(kind, current, setter):
                options, reason = self._frame_options(kind)
                combo = QComboBox()
                if options:
                    for name in options:
                        combo.addItem(name, name)
                    if current and current not in options:
                        combo.addItem(current, current)
                    combo.setCurrentIndex(max(0, combo.findData(current)))
                    combo.currentIndexChanged.connect(
                        lambda _i, c=combo: setter(c.currentData()))
                    note = QLabel("")
                else:
                    combo.addItem(current or "（未设置）", current)
                    combo.setEnabled(False)
                    note = QLabel(f"只读：{reason or '无法读取机器人坐标系'}")
                    note.setWordWrap(True); note.setObjectName("SubLabel")
                return combo, note

            tool, tool_note = _frame_row(
                "tool", prof.active_tool, lambda v: setattr(prof, "active_tool", v))
            form.addRow("工具系（Bolt 等）", tool)
            if tool_note:
                form.addRow("", tool_note)
            work, work_note = _frame_row(
                "work", prof.active_work, lambda v: setattr(prof, "active_work", v))
            form.addRow("工作系（Ganta 等）", work)
            if work_note:
                form.addRow("", work_note)
            lay.addWidget(g)

            g2 = QGroupBox("速度与超时")
            form2 = QFormLayout(g2)
            form2.addRow("速度上限(%)", self._spin(
                prof.speed_cap, 1, 100, suffix=" %",
                on_change=lambda v: setattr(prof, "speed_cap", int(v))))
            form2.addRow("运动方案单点超时(s)", self._spin(
                prof.point_timeout_s, 1, 3600, suffix=" s",
                on_change=lambda v: setattr(prof, "point_timeout_s", int(v))))
            form2.addRow("引导等待视觉超时(s)", self._spin(
                prof.guide_recv_timeout_s, 1, 3600, suffix=" s",
                on_change=lambda v: setattr(prof, "guide_recv_timeout_s", int(v))))
            form2.addRow("引导检测看门狗(s，0=关)", self._spin(
                prof.guide_detect_timeout_s, 0, 3600, suffix=" s",
                on_change=lambda v: setattr(prof, "guide_detect_timeout_s", int(v))))
            form2.addRow("引导移动速度(%)", self._spin(
                prof.guide_move_speed, 1, 100, suffix=" %",
                on_change=lambda v: setattr(prof, "guide_move_speed", int(v))))
            form2.addRow("引导单段最大偏移(mm，0=关闭软上限)", self._spin(
                getattr(prof, "guide_max_offset_mm", 50.0), 0, 1000,
                decimals=1, step=1.0, suffix=" mm",
                on_change=lambda v: setattr(prof, "guide_max_offset_mm", float(v))))
            lay.addWidget(g2)
            note = QLabel("工具系/工作系只在机器人已连接且能读取列表时可改；"
                          "离线或读取失败保持只读，保存时不会改这两个值。")
            note.setWordWrap(True); note.setObjectName("SubLabel")
            lay.addWidget(note)
            lay.addStretch(1)

            right = QWidget(); rlay = QVBoxLayout(right)
            rlay.setContentsMargins(0, 0, 0, 0); rlay.setSpacing(8)
            rlay.addWidget(self._global_values_group())
            rlay.addStretch(1)

            box = QWidget(); grid = QGridLayout(box)
            grid.setContentsMargins(0, 0, 0, 0); grid.setHorizontalSpacing(10)
            grid.addWidget(left, 0, 0)
            grid.addWidget(right, 0, 1)
            grid.setColumnStretch(0, 1); grid.setColumnStretch(1, 1)
            return box

        # ---------- 计算值（DEC-151：操作台计算结果可同步到这里） ----------
        def _global_values_group(self) -> QWidget:
            g = QGroupBox("计算值（操作台计算后可人工改值再同步）")
            if self.operation_store is None:
                return g
            self._value_widgets: dict = {}
            f = QFormLayout(g)
            f.addRow("杆塔宽度(mm)", self._global_value_spin("tower_width"))
            f.addRow("螺栓直径(mm)", self._bolt_size_combo())
            return g

        def _global_value_spin(self, op: str) -> QWidget:
            store = self.operation_store
            value = store.value(op)

            def _changed(v):
                store.set_value(op, float(v))

            spin = self._spin(value, 0.0, 65535.0, decimals=1, step=0.1,
                              suffix=" mm", on_change=_changed)
            self._value_widgets[op] = spin
            return spin

        def _bolt_size_combo(self) -> QWidget:
            """DEC-154：螺栓直径固定三档下拉（16/20/24 mm）。"""
            from application.services.operation_config import (
                BOLT_SIZES, nearest_bolt_size,
            )
            store = self.operation_store
            combo = _fit_combo(QComboBox(), chars=6)
            for v in BOLT_SIZES:
                combo.addItem(f"{v:g}", v)
            cur = store.value("bolt_size")
            idx = combo.findData(nearest_bolt_size(cur if cur > 0 else BOLT_SIZES[0]))
            combo.setCurrentIndex(max(0, idx))
            combo.currentIndexChanged.connect(
                lambda _i, c=combo: store.set_value("bolt_size", float(c.currentData())))
            self._value_widgets["bolt_size"] = combo
            return combo

        def reload_computed_values(self) -> None:
            """DEC-153：操作台弹窗同步计算值后，就地刷新两个数值控件
            （只动这两个控件，不影响本页其它未保存的修改）。"""
            if self.operation_store is None:
                return
            from application.services.operation_config import nearest_bolt_size
            for op, widget in getattr(self, "_value_widgets", {}).items():
                val = self.operation_store.value(op)
                if isinstance(widget, QComboBox):
                    idx = widget.findData(nearest_bolt_size(val))
                    if idx >= 0 and idx != widget.currentIndex():
                        widget.blockSignals(True)
                        widget.setCurrentIndex(idx)
                        widget.blockSignals(False)
                    continue
                if abs(float(widget.value()) - val) > 1e-9:
                    widget.blockSignals(True)
                    widget.setValue(val)
                    widget.blockSignals(False)

        # ---------- 全局快捷键（DEC-115） ----------
        def _shortcuts_form(self) -> QWidget:
            s = self.runtime_store.settings
            shortcuts = dict(s.shortcuts)
            box = QWidget(); lay = QVBoxLayout(box); lay.setSpacing(8)
            g = QGroupBox("全局快捷键（主窗口内任意页面生效）")
            f = QFormLayout(g)
            self._shortcut_edits: dict[str, QKeySequenceEdit] = {}
            for key, label in SHORTCUT_ACTIONS:
                edit = QKeySequenceEdit()
                # 一个动作只绑一个组合键（Ctrl+1 这类组合算一个序列）
                try:
                    edit.setMaximumSequenceLength(1)
                except AttributeError:  # pragma: no cover - 老 Qt 无此接口
                    pass
                edit.setKeySequence(QKeySequence(str(shortcuts.get(key, "") or "")))
                edit.setToolTip("点击后按下要绑定的按键/组合键；按 Backspace 或 Delete 清空 = 不绑定")
                f.addRow(label, edit)
                self._shortcut_edits[key] = edit
            lay.addWidget(g)
            row = QHBoxLayout()
            reset = QPushButton("恢复默认")
            reset.setObjectName("Ghost")
            reset.clicked.connect(self._shortcuts_reset_defaults)
            row.addWidget(reset)
            row.addStretch(1)
            lay.addLayout(row)
            note = QLabel("改完点底部「保存参数」立即生效，不需要重启；"
                          "同一按键不能绑定两个动作，留空 = 不绑定。")
            note.setWordWrap(True); note.setObjectName("SubLabel")
            lay.addWidget(note)
            return box

        def _shortcuts_reset_defaults(self) -> None:
            for key, _label in SHORTCUT_ACTIONS:
                edit = self._shortcut_edits.get(key)
                if edit is not None:
                    edit.setKeySequence(QKeySequence(DEFAULT_SHORTCUTS.get(key, "")))
            self.status.setText("快捷键已恢复默认（未保存）")

        def _collect_shortcuts(self) -> dict[str, str]:
            out: dict[str, str] = {}
            for key, _label in SHORTCUT_ACTIONS:
                edit = getattr(self, "_shortcut_edits", {}).get(key)
                out[key] = (edit.keySequence().toString()
                            if edit is not None else "")
            return out

        # ---------- 精定位偏移 ----------
        def _axis_row(self, mapping: AxisMapping, setter) -> QWidget:
            row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
            ax = _fit_combo(QComboBox(), chars=6)
            for a in ("X", "Z"):
                ax.addItem(a, a)
            ax.setCurrentIndex(max(0, ax.findData(mapping.x_axis)))
            sg = _fit_combo(QComboBox(), chars=6)
            for s in (1, -1):
                sg.addItem("+" if s > 0 else "−", s)
            sg.setCurrentIndex(max(0, sg.findData(mapping.x_sign)))

            def _apply(_i=None) -> None:
                setter(AxisMapping(ax.currentData(), int(sg.currentData()),
                                   mapping.y_axis, mapping.y_sign))
            ax.currentIndexChanged.connect(_apply)
            sg.currentIndexChanged.connect(_apply)
            h.addWidget(QLabel("相机X→")); h.addWidget(ax); h.addWidget(sg)
            ax2 = _fit_combo(QComboBox(), chars=6)
            for a in ("X", "Z"):
                ax2.addItem(a, a)
            ax2.setCurrentIndex(max(0, ax2.findData(mapping.y_axis)))
            sg2 = _fit_combo(QComboBox(), chars=6)
            for s in (1, -1):
                sg2.addItem("+" if s > 0 else "−", s)
            sg2.setCurrentIndex(max(0, sg2.findData(mapping.y_sign)))

            def _apply2(_i=None) -> None:
                setter(AxisMapping(ax.currentData(), int(sg.currentData()),
                                   ax2.currentData(), int(sg2.currentData())))
            ax2.currentIndexChanged.connect(_apply2)
            sg2.currentIndexChanged.connect(_apply2)
            h.addWidget(QLabel("相机Y→")); h.addWidget(ax2); h.addWidget(sg2)
            return row

        def _fine_form(self) -> QWidget:
            if self.guidance_store is None:
                return EmptyHint("未接入引导配置（guidance.yaml）")
            cfg = self.guidance_store.config
            box = QWidget(); lay = QVBoxLayout(box); lay.setSpacing(8)

            g = QGroupBox("默认精定位偏移参数（offsets.fine / tcp）")
            f = QFormLayout(g)
            f.addRow("轴映射/符号", self._axis_row(
                cfg.offsets.fine,
                lambda m: setattr(cfg, "offsets", OffsetParams(
                    coarse=cfg.offsets.coarse, fine=m,
                    tcp_x=cfg.offsets.tcp_x, tcp_y=cfg.offsets.tcp_y,
                    tcp_z=cfg.offsets.tcp_z))))
            f.addRow("tcp_x(mm)", self._spin(
                cfg.offsets.tcp_x, -1000, 1000, decimals=2, suffix=" mm",
                on_change=lambda v: setattr(cfg, "offsets", OffsetParams(
                    coarse=cfg.offsets.coarse, fine=cfg.offsets.fine,
                    tcp_x=float(v), tcp_y=cfg.offsets.tcp_y,
                    tcp_z=cfg.offsets.tcp_z))))
            f.addRow("tcp_y(mm)", self._spin(
                cfg.offsets.tcp_y, -1000, 1000, decimals=2, suffix=" mm",
                on_change=lambda v: setattr(cfg, "offsets", OffsetParams(
                    coarse=cfg.offsets.coarse, fine=cfg.offsets.fine,
                    tcp_x=cfg.offsets.tcp_x, tcp_y=float(v),
                    tcp_z=cfg.offsets.tcp_z))))
            f.addRow("tcp_z(mm)", self._spin(
                cfg.offsets.tcp_z, -1000, 1000, decimals=2, suffix=" mm",
                on_change=lambda v: setattr(cfg, "offsets", OffsetParams(
                    coarse=cfg.offsets.coarse, fine=cfg.offsets.fine,
                    tcp_x=cfg.offsets.tcp_x, tcp_y=cfg.offsets.tcp_y,
                    tcp_z=float(v)))))
            tcp_note = QLabel(
                "tcp_x / tcp_y = 工具中心在相机画面里的横向位置（相机系 X/Y，mm，旧工程标定值）。"
                "精定位 = 让目标点落到工具中心（画面偏离中心该距离处），而不是落到画面中心；"
                "填 0 会让精定位退化成粗定位（相机居中、工具不对准圆）。tcp_z 仅登记沿光轴距离，"
                "不参与运动。若粗/精定位都朝远离圆心的方向移动（越挪越远），说明轴符号反了："
                "改上面的 +/− 后点「保存参数」。")
            tcp_note.setWordWrap(True)
            tcp_note.setObjectName("SubLabel")
            f.addRow("", tcp_note)
            lay.addWidget(g)

            g2 = QGroupBox("额外精定位参数套（在引导定位步骤里按名称选择）")
            v = QVBoxLayout(g2)
            if not cfg.fine_profiles:
                v.addWidget(QLabel("暂无额外参数套。"))
            for i, p in enumerate(list(cfg.fine_profiles)):
                row = QFrame(); row.setObjectName("BlockBox")
                rl = QVBoxLayout(row); rl.setContentsMargins(8, 6, 8, 6)
                head = QHBoxLayout()
                head.addWidget(QLabel(f"参数套：{p.profile_id}"))
                head.addStretch(1)
                dele = QPushButton("删除"); dele.setObjectName("Ghost")
                dele.clicked.connect(lambda _c=False, i=i: self._delete_fine(i))
                head.addWidget(dele)
                rl.addLayout(head)
                rf = QFormLayout()
                rf.addRow("说明", self._line(p.desc, lambda val, obj=p: setattr(obj, "desc", val)))
                rf.addRow("轴映射/符号", self._axis_row(
                    p.fine, lambda m, obj=p: setattr(obj, "fine", m)))
                rf.addRow("tcp_x(mm)", self._spin(
                    p.tcp_x, -1000, 1000, decimals=2, suffix=" mm",
                    on_change=lambda val, obj=p: setattr(obj, "tcp_x", float(val))))
                rf.addRow("tcp_y(mm)", self._spin(
                    p.tcp_y, -1000, 1000, decimals=2, suffix=" mm",
                    on_change=lambda val, obj=p: setattr(obj, "tcp_y", float(val))))
                rf.addRow("tcp_z(mm)", self._spin(
                    p.tcp_z, -1000, 1000, decimals=2, suffix=" mm",
                    on_change=lambda val, obj=p: setattr(obj, "tcp_z", float(val))))
                rl.addLayout(rf)
                v.addWidget(row)
            add = QPushButton("增加精定位参数套")
            add.setObjectName("Ghost")
            add.clicked.connect(self._add_fine)
            v.addWidget(add)
            lay.addWidget(g2)
            return box

        def _add_fine(self) -> None:
            cfg = self.guidance_store.config
            name, ok = QInputDialog.getText(self, "增加精定位参数套", "参数套名称（唯一）:")
            name = (name or "").strip()
            if not ok or not name:
                return
            if cfg.find_fine_profile(name):
                QMessageBox.warning(self, "增加精定位参数套", f"名称 {name!r} 已存在")
                return
            cfg.fine_profiles.append(FineOffsetProfile(
                profile_id=name, fine=cfg.offsets.fine,
                tcp_x=cfg.offsets.tcp_x, tcp_y=cfg.offsets.tcp_y,
                tcp_z=cfg.offsets.tcp_z,
                desc="本工程新建"))
            self._rebuild()
            self.status.setText("已增加参数套（未保存）")

        def _delete_fine(self, index: int) -> None:
            cfg = self.guidance_store.config
            if 0 <= index < len(cfg.fine_profiles):
                pid = cfg.fine_profiles[index].profile_id
                if QMessageBox.question(
                        self, "删除精定位参数套",
                        f"确认删除参数套 {pid!r}？"
                        f"引用它的引导定位步骤会在校验时报错。") != QMessageBox.StandardButton.Yes:
                    return
                cfg.fine_profiles.pop(index)
                self._rebuild()
                self.status.setText("已删除参数套（未保存）")

        # ---------- 运行/日志 ----------
        def _runtime_form(self) -> QWidget:
            s = self.runtime_store.settings
            box = QWidget(); lay = QVBoxLayout(box); lay.setSpacing(8)
            g = QGroupBox("日志")
            f = QFormLayout(g)
            f.addRow("文件日志级别", self._combo(
                LOG_LEVELS, s.log_level, lambda v: setattr(s, "log_level", str(v))))
            f.addRow("日志保留天数", self._spin(
                s.log_backup_days, 1, 365, on_change=lambda v: setattr(s, "log_backup_days", int(v))))
            f.addRow("同时输出控制台", self._check(
                s.log_to_console, "开（GUI 默认关）",
                lambda v: setattr(s, "log_to_console", bool(v))))
            lay.addWidget(g)
            info = QGroupBox("运行信息（只读）")
            fi = QFormLayout(info)
            fac = getattr(self.stack, "logging", None)
            fi.addRow("profile", QLabel(str(getattr(self.stack, "profile", "-"))))
            fi.addRow("运行目录", QLabel(app_paths.runtime_dir()))
            fi.addRow("日志文件", QLabel(str(getattr(fac, "log_path", "-") or "-")))
            fi.addRow("配置目录", QLabel(app_paths.configs_dir()))
            lay.addWidget(info)
            note = QLabel("日志级别/保留天数/控制台开关修改后**需重启程序**生效；"
                          "文件日志级别改为 INFO 会减少诊断页可用的细粒度记录。")
            note.setWordWrap(True); note.setObjectName("SubLabel")
            lay.addWidget(note)
            return box

        # ---------- 保存 ----------
        def _save(self) -> None:
            problems: list[str] = []
            if self.motion_store is not None:
                try:
                    from application.services.motion_config import validate as validate_motion
                    problems += list(validate_motion(self.motion_store.config))
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"运动配置校验失败: {exc}")
            # DEC-115：本页已不再编辑 guidance 深度/找圆/DS86 段，保存不再做
            # guidance 整体校验（否则离线/未填 circle_type 的环境连快捷键都存不了）；
            # 精定位参数套仍随 guidance.yaml 保存。
            if problems:
                QMessageBox.warning(self, "参数未保存", "\n".join(problems))
                return
            shortcuts = self._collect_shortcuts()
            duplicates = duplicate_shortcuts(shortcuts)
            if duplicates:
                QMessageBox.warning(
                    self, "参数未保存",
                    "快捷键重复绑定：" + "、".join(duplicates)
                    + "\n同一按键只能绑定一个动作，请先修改。")
                return
            try:
                if self.motion_store is not None:
                    self.motion_store.save()
                if self.guidance_store is not None:
                    self.guidance_store.save()
                if self.operation_store is not None:
                    self.operation_store.save()
                if self.runtime_store._settings is None:  # noqa: SLF001
                    self.runtime_store.load()
                self.runtime_store.settings.shortcuts = shortcuts
                self.runtime_store.save()
                # 让配方校验/编辑器立刻看到新增/删除的精定位参数套（DEC-065）
                rs = getattr(self.stack, "recipes", None)
                cat = getattr(rs, "catalog", None)
                if cat is not None and self.guidance_store is not None:
                    cat.fine_offset_profiles = set(
                        self.guidance_store.config.fine_profile_ids())
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "保存失败", str(exc))
                return
            # DEC-115：快捷键保存后立即在主窗口生效（无需重启）
            win = self.window()
            reload_fn = getattr(win, "reload_shortcuts", None)
            if callable(reload_fn):
                try:
                    reload_fn()
                except Exception as exc:  # noqa: BLE001 - 已保存成功，只提示重载失败
                    self.status.setText(f"已保存，但快捷键重载失败：{exc}")
                    return
            self.status.setText("已保存（快捷键立即生效；日志级别/保留天数需重启生效）")

        def refresh(self) -> None:  # pragma: no cover - 页面切换由外壳调用
            # 不自动重读（避免丢掉未保存修改）；仅在首次显示时由构建完成。
            return


__all__ = ["SettingsPage"]
