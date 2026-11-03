"""夹爪板调试页（DEC-097）。

一页完成：Modbus TCP 连接、系统控制项、单步流程命令、只读监视、动作序列。
只走高层 PROC_CMD 与系统控制字；不开放单电机块/原始寄存器写入。
连接参数与序列存在 `<runtime>/gripper_debug.yaml`，不写 devices.yaml。
"""
from __future__ import annotations

import os
import time

from application.presentation.components import Card, EmptyHint, KV, StatusPill
from application.presentation.theme import THEME

try:  # pragma: no cover - Qt
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
        QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
        QProgressBar, QPushButton, QScrollArea, QSpinBox, QTableWidget,
        QTableWidgetItem, QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]


def _confirm(parent, title: str, text: str) -> bool:
    """危险操作确认（默认「否」）。"""
    answer = QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)
    return answer == QMessageBox.StandardButton.Yes


def _warn(parent, title: str, text: str) -> None:
    QMessageBox.warning(parent, title, text)


def _parse_int_text(text: str) -> int:
    """参数输入：默认十进制；``0x`` 前缀按十六进制。"""
    text = text.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    return int(text, 10)


if GUI_REQUIRED:
    from application.services.gripper_debug import (
        SEQUENCE_FILE, STEP_WAIT_DELAY, STEP_WAIT_DONE, GripperDebugConfig,
        GripperDebugError, GripperDebugStore, GripperSequence,
        GripperSequenceRunner, GripperSession, GripperStep, PARAM_SPECS,
        default_sequences,
    )
    from application.adapters.gripper_board import registers as R

    class StepDialog(QDialog):
        """新增/编辑动作序列的一步。"""

        def __init__(self, parent, step: GripperStep | None = None) -> None:
            super().__init__(parent)
            self.setWindowTitle("动作步骤")
            self.setMinimumWidth(520)
            self.result_step: GripperStep | None = None
            form = QFormLayout(self)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

            self.name_edit = QLineEdit((step.name if step else "") or "")
            self.name_edit.setPlaceholderText("留空时用命令名称")
            form.addRow("步骤名称", self.name_edit)

            self.cmd_combo = QComboBox()
            for spec in R.COMMANDS:
                self.cmd_combo.addItem(
                    f"{spec.group} · {spec.label}  (0x{spec.code:02X})", spec.code)
            self.cmd_combo.currentIndexChanged.connect(
                lambda _i: self._sync_name_hint())
            form.addRow("流程命令", self.cmd_combo)

            self.params: dict[str, QLineEdit] = {}
            param_grid = QGridLayout()
            for i, (key, label, unit, _addr) in enumerate(PARAM_SPECS):
                edit = QLineEdit()
                edit.setPlaceholderText("留空=不写")
                edit.setToolTip("写入 0x%04X" % dict(
                    (k, a) for k, _l, _u, a in PARAM_SPECS)[key])
                if step and key in step.params:
                    edit.setText(str(step.params[key]))
                param_grid.addWidget(QLabel(f"{label}{('(' + unit + ')') if unit else ''}"),
                                     i // 2, (i % 2) * 2)
                param_grid.addWidget(edit, i // 2, (i % 2) * 2 + 1)
                self.params[key] = edit
            form.addRow("流程参数", param_grid)

            self.wait_combo = QComboBox()
            self.wait_combo.addItem("检查板端完成（PROC_STATUS=完成）", STEP_WAIT_DONE)
            self.wait_combo.addItem("固定延时（不检查完成）", STEP_WAIT_DELAY)
            form.addRow("等待方式", self.wait_combo)

            self.delay_spin = QSpinBox()
            self.delay_spin.setRange(0, 600000)
            self.delay_spin.setSuffix(" ms")
            self.delay_spin.setValue((step.delay_ms if step else 500))
            form.addRow("固定延时", self.delay_spin)

            self.timeout_spin = QSpinBox()
            self.timeout_spin.setRange(100, 600000)
            self.timeout_spin.setSuffix(" ms")
            self.timeout_spin.setValue((step.timeout_ms if step else 15000))
            form.addRow("步骤超时", self.timeout_spin)

            note = QLabel("参数留空表示本次不下发该参数（沿用板端当前值）。"
                          "标定类命令执行前还会再确认一次。")
            note.setObjectName("SubLabel")
            note.setWordWrap(True)
            form.addRow("", note)

            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok
                | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            form.addRow("", buttons)

            if step:
                idx = self.cmd_combo.findData(step.command)
                self.cmd_combo.setCurrentIndex(idx if idx >= 0 else 0)
                idx = self.wait_combo.findData(step.wait)
                self.wait_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self._sync_name_hint()

        def _sync_name_hint(self) -> None:
            if not self.name_edit.text().strip():
                spec = R.COMMAND_BY_CODE.get(self.cmd_combo.currentData())
                self.name_edit.setPlaceholderText(spec.label if spec else "步骤名称")

        def accept(self) -> None:  # noqa: D401
            try:
                state = self.result_step = self._build()
                state.validate()
            except Exception as exc:  # noqa: BLE001
                _warn(self, "步骤无效", str(exc))
                return
            super().accept()

        def _build(self) -> GripperStep:
            code = int(self.cmd_combo.currentData())
            spec = R.COMMAND_BY_CODE[code]
            params: dict[str, int] = {}
            for key, edit in self.params.items():
                text = edit.text().strip()
                if not text:
                    continue
                try:
                    params[key] = _parse_int_text(text)
                except ValueError as exc:
                    raise ValueError(f"参数 {key!r} 不是整数：{text!r}") from exc
            return GripperStep(
                name=self.name_edit.text().strip() or spec.label,
                command=code, params=params,
                wait=str(self.wait_combo.currentData()),
                delay_ms=int(self.delay_spin.value()),
                timeout_ms=int(self.timeout_spin.value()),
            )

    class GripperPage(QWidget):
        """夹爪板调试页。"""

        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.session = GripperSession()
            self.runner = GripperSequenceRunner(self.session)
            runtime = getattr(getattr(ctx, "stack", None), "runtime_dir", "") or "runtime"
            self.store = GripperDebugStore(os.path.join(runtime, SEQUENCE_FILE))
            try:
                self.config, self.sequences = self.store.load()
            except Exception as exc:  # noqa: BLE001 - 坏文件不挡页面
                self.config = GripperDebugConfig()
                self.sequences = default_sequences()
                self._load_error = str(exc)
            else:
                self._load_error = None
            if not self.sequences:
                self.sequences = default_sequences()
            self.seq_index = 0
            self._monitor_values: dict[str, object] = {}
            self._last_monitor_at = 0.0
            self._manual_active = False
            self._manual_started = 0.0

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            inner = QWidget()
            root = QVBoxLayout(inner)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)
            root.addWidget(self._connection_card())
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(self._control_card(), 1)
            row.addWidget(self._step_card(), 1)
            root.addLayout(row)
            root.addWidget(self._monitor_card())
            root.addWidget(self._sequence_card())
            scroll.setWidget(inner)
            outer.addWidget(scroll)

            self._timer = QTimer(self)
            self._timer.setInterval(200)
            self._timer.timeout.connect(self._tick)
            self._timer.start()
            self._refresh_sequence_combo()
            self._refresh_step_table()
            self.refresh()
            if self._load_error:
                self.conn_note.setText(self._load_error)

        # ------------------------------------------------------------ 连接
        def _connection_card(self) -> Card:
            card = Card("连接 · Modbus TCP")
            self.host_edit = QLineEdit(self.config.host)
            self.host_edit.setPlaceholderText("板子 IP，例如 192.168.1.50")
            self.port_spin = QSpinBox(); self.port_spin.setRange(1, 65535)
            self.port_spin.setValue(self.config.port); self.port_spin.setFixedWidth(90)
            self.unit_spin = QSpinBox(); self.unit_spin.setRange(1, 247)
            self.unit_spin.setValue(self.config.unit_id); self.unit_spin.setFixedWidth(80)
            self.timeout_spin = QSpinBox(); self.timeout_spin.setRange(100, 10000)
            self.timeout_spin.setSuffix(" ms"); self.timeout_spin.setValue(self.config.timeout_ms)
            self.timeout_spin.setFixedWidth(110)
            top_row = QHBoxLayout()
            top_row.setSpacing(8)
            top_row.addWidget(QLabel("IP/主机"))
            top_row.addWidget(self.host_edit, 1)
            top_row.addWidget(QLabel("端口"))
            top_row.addWidget(self.port_spin)
            top_row.addWidget(QLabel("从站地址"))
            top_row.addWidget(self.unit_spin)
            top_row.addWidget(QLabel("通信超时"))
            top_row.addWidget(self.timeout_spin)

            self.connect_btn = QPushButton("连接")
            self.connect_btn.setObjectName("Primary")
            self.connect_btn.setFixedWidth(90)
            self.connect_btn.clicked.connect(self._connect)
            self.disconnect_btn = QPushButton("断开")
            self.disconnect_btn.setObjectName("Ghost")
            self.disconnect_btn.setFixedWidth(90)
            self.disconnect_btn.clicked.connect(self._disconnect)
            self.save_cfg_btn = QPushButton("保存配置")
            self.save_cfg_btn.setObjectName("Ghost")
            self.save_cfg_btn.setFixedWidth(110)
            self.save_cfg_btn.clicked.connect(self._save_all)
            self.conn_pill = StatusPill("未连接", THEME.text_secondary)
            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)
            btn_row.addWidget(self.connect_btn)
            btn_row.addWidget(self.disconnect_btn)
            btn_row.addWidget(self.save_cfg_btn)
            btn_row.addWidget(self.conn_pill)
            btn_row.addStretch(1)
            card.body().addLayout(top_row)
            card.body().addLayout(btn_row)
            self.identity_label = QLabel("设备：未连接")
            self.identity_label.setObjectName("SubLabel")
            self.identity_label.setWordWrap(True)
            card.add(self.identity_label)
            self.conn_note = QLabel(
                "标准 Modbus TCP（MBAP，无 CRC），端口默认 502；从站地址对应板端 DEV_ADDR。"
                "连接后后台按心跳周期写 0x008A，避免打开对话框时板端心跳超时。")
            self.conn_note.setObjectName("SubLabel")
            self.conn_note.setWordWrap(True)
            card.add(self.conn_note)
            return card

        def _config_from_ui(self) -> GripperDebugConfig:
            cfg = GripperDebugConfig(
                host=self.host_edit.text().strip(),
                port=int(self.port_spin.value()),
                unit_id=int(self.unit_spin.value()),
                timeout_ms=int(self.timeout_spin.value()),
                heartbeat_ms=(int(self.heartbeat_spin.value())
                              if int(self.hb_check.currentData()) == 1 else 0),
                heartbeat_timeout_ms=int(self.hb_timeout_spin.value()),
            )
            cfg.validate()
            return cfg

        def _connect(self) -> None:
            try:
                cfg = self._config_from_ui()
                identity = self.session.connect(cfg)
            except Exception as exc:  # noqa: BLE001
                self.conn_pill.set_text("连接失败", THEME.danger)
                self.conn_note.setText(str(exc))
                _warn(self, "连接失败", str(exc))
                self._sync_enabled()
                return
            self.config = cfg
            try:
                self.store.save(cfg, self.sequences)
            except Exception:  # noqa: BLE001 - 配置落盘失败不挡连接
                pass
            dev = f"0x{identity['dev_type']:04X}"
            fw = f"0x{identity['fw_ver']:04X}"
            hw = f"0x{identity['hw_ver']:04X}"
            self.identity_label.setText(
                f"设备类型 {dev} · 固件 {fw} · 硬件 {hw} · 从站地址 {identity['dev_addr']}")
            self.conn_note.setText(
                self.session.identity_warning
                or "已连接；心跳后台运行。写参数/动作前请确认现场安全。")
            self.conn_pill.set_text("已连接", THEME.ok)
            self._sync_enabled()
            self._tick(force_monitor=True)

        def _disconnect(self) -> None:
            self.runner.stop()
            self._manual_active = False
            self.session.disconnect()
            self.conn_pill.set_text("未连接", THEME.text_secondary)
            self.identity_label.setText("设备：未连接")
            self.hb_status.setText("心跳：未连接")
            self.manual_status.set_key("流程状态", "未连接")
            self._sync_enabled()

        def _save_all(self) -> None:
            try:
                cfg = self._config_from_ui()
                self.store.save(cfg, self.sequences)
                self.config = cfg
            except Exception as exc:  # noqa: BLE001
                _warn(self, "保存失败", str(exc))
                return
            self.conn_note.setText(f"已保存到 {self.store.path}")

        # ------------------------------------------------------------ 控制项
        def _control_card(self) -> Card:
            card = Card("控制项")
            form = QFormLayout()
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            sys_row = QHBoxLayout()
            self.enable_btn = self._button("使能", self._enable)
            self.estop_btn = self._button("急停", self._estop, "Danger")
            self.clear_err_btn = self._button("清错", self._clear_error)
            self.reset_btn = self._button("复位", self._reset)
            for b in (self.enable_btn, self.estop_btn, self.clear_err_btn, self.reset_btn):
                sys_row.addWidget(b)
            sys_row.addStretch(1)
            form.addRow("系统", sys_row)

            self.sys_mode_combo = QComboBox()
            for value, text in R.SYS_MODES.items():
                self.sys_mode_combo.addItem(text, value)
            self.sys_mode_btn = self._button("应用", self._apply_sys_mode)
            mode_row = QHBoxLayout(); mode_row.addWidget(self.sys_mode_combo, 1)
            mode_row.addWidget(self.sys_mode_btn)
            form.addRow("工作模式", mode_row)

            self.hb_check = QComboBox()
            self.hb_check.addItem("后台喂心跳", 1)
            self.hb_check.addItem("不喂心跳（连接后不写 0x008A）", 0)
            self.heartbeat_spin = QSpinBox(); self.heartbeat_spin.setRange(100, 60000)
            self.heartbeat_spin.setSuffix(" ms"); self.heartbeat_spin.setValue(
                self.config.heartbeat_ms or 1000)
            self.hb_timeout_spin = QSpinBox(); self.hb_timeout_spin.setRange(100, 60000)
            self.hb_timeout_spin.setSuffix(" ms")
            self.hb_timeout_spin.setValue(self.config.heartbeat_timeout_ms)
            hb_btn = self._button("写入超时", self._apply_hb_timeout)
            hb_row = QHBoxLayout()
            hb_row.addWidget(self.hb_check, 1)
            hb_row.addWidget(self.heartbeat_spin)
            hb_row.addWidget(QLabel("超时"))
            hb_row.addWidget(self.hb_timeout_spin)
            hb_row.addWidget(hb_btn)
            form.addRow("心跳", hb_row)
            self.hb_check.setCurrentIndex(0 if self.config.heartbeat_ms > 0 else 1)
            self.hb_status = QLabel("心跳：未连接")
            self.hb_status.setObjectName("SubLabel")
            form.addRow("", self.hb_status)

            self.iwdg_spin = QSpinBox(); self.iwdg_spin.setRange(100, 60000)
            self.iwdg_spin.setSuffix(" ms"); self.iwdg_spin.setValue(3000)
            self.iwdg_btn = self._button("写入", self._apply_watchdog)
            wd_row = QHBoxLayout(); wd_row.addWidget(self.iwdg_spin); wd_row.addWidget(self.iwdg_btn)
            wd_row.addStretch(1)
            form.addRow("看门狗超时", wd_row)

            self.proc_mode_combo = QComboBox()
            for value, text in R.PROC_MODES.items():
                self.proc_mode_combo.addItem(text, value)
            self.proc_mode_btn = self._button("应用", self._apply_proc_mode)
            pm_row = QHBoxLayout(); pm_row.addWidget(self.proc_mode_combo, 1)
            pm_row.addWidget(self.proc_mode_btn)
            form.addRow("流程模式", pm_row)

            ctrl_row = QHBoxLayout()
            for text, fn in (("中止", lambda: self._proc_ctrl(R.PROC_CTRL_ABORT, "中止")),
                             ("暂停", lambda: self._proc_ctrl(R.PROC_CTRL_PAUSE, "暂停")),
                             ("继续", lambda: self._proc_ctrl(R.PROC_CTRL_RESUME, "继续")),
                             ("跳过", lambda: self._proc_ctrl(R.PROC_CTRL_SKIP, "跳过"))):
                ctrl_row.addWidget(self._button(text, fn))
            ctrl_row.addStretch(1)
            form.addRow("流程控制", ctrl_row)

            store_row = QHBoxLayout()
            store_row.addWidget(self._button("保存参数", self._save_params))
            store_row.addWidget(self._button("加载参数", self._load_params))
            store_row.addWidget(self._button("恢复默认", self._reset_params, "Danger"))
            store_row.addStretch(1)
            form.addRow("参数存储", store_row)
            self.store_status = QLabel("存储状态：-")
            self.store_status.setObjectName("SubLabel")
            form.addRow("", self.store_status)

            card.body().addLayout(form)
            return card

        def _button(self, text: str, slot, style: str = "Ghost") -> QPushButton:
            btn = QPushButton(text)
            btn.setObjectName(style)
            btn.clicked.connect(slot)
            return btn

        def _write(self, fn, success_text: str) -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                _warn(self, "操作失败", str(exc))
                self._sync_enabled()
                return
            self.conn_note.setText(success_text)

        def _enable(self) -> None:
            self._write(lambda: self.session.trigger_sys_ctrl(R.SYS_CTRL_ENABLE),
                        "已下发使能（SYS_CTRL bit0）")

        def _estop(self) -> None:
            # 急停不做二次确认：安全操作要即时
            self._write(lambda: self.session.trigger_sys_ctrl(R.SYS_CTRL_ESTOP),
                        "已下发急停（SYS_CTRL bit1），请现场核对")

        def _clear_error(self) -> None:
            self._write(lambda: self.session.trigger_sys_ctrl(R.SYS_CTRL_CLEAR_ERROR),
                        "已下发清错（SYS_CTRL bit2）")

        def _reset(self) -> None:
            if not _confirm(self, "复位", "将向板端下发复位（SYS_CTRL bit3），确认继续？"):
                return
            self._write(lambda: self.session.trigger_sys_ctrl(R.SYS_CTRL_RESET),
                        "已下发复位（SYS_CTRL bit3）")

        def _apply_sys_mode(self) -> None:
            mode = int(self.sys_mode_combo.currentData())
            self._write(lambda: self.session.set_sys_mode(mode), f"已设置工作模式：{R.SYS_MODES[mode]}")

        def _apply_proc_mode(self) -> None:
            mode = int(self.proc_mode_combo.currentData())
            self._write(lambda: self.session.set_proc_mode(mode), f"已设置流程模式：{R.PROC_MODES[mode]}")

        def _apply_hb_timeout(self) -> None:
            ms = int(self.hb_timeout_spin.value())
            self._write(lambda: self.session.set_heartbeat_timeout(ms),
                        f"已写入心跳超时 {ms} ms（保存后生效需再写 EEPROM）")

        def _apply_watchdog(self) -> None:
            ms = int(self.iwdg_spin.value())
            self._write(lambda: self.session.set_watchdog_timeout(ms),
                        f"已写入看门狗超时 {ms} ms")

        def _proc_ctrl(self, mask: int, text: str) -> None:
            self._write(lambda: self.session.proc_ctrl(mask), f"已下发流程控制：{text}")

        def _save_params(self) -> None:
            if not _confirm(self, "保存参数", "将把当前参数写入板端 EEPROM，确认继续？"):
                return
            self._write(self.session.save_params, "已下发参数保存（0xA5A5）")

        def _load_params(self) -> None:
            if not _confirm(self, "加载参数", "将从 EEPROM 读回参数，可能覆盖当前运行参数，确认继续？"):
                return
            self._write(self.session.load_params, "已下发参数加载（0x5A5A）")

        def _reset_params(self) -> None:
            if not _confirm(self, "恢复默认",
                            "将恢复出厂默认参数；按板端规则，现场标定值可能保留。\n确认继续？"):
                return
            self._write(self.session.reset_params, "已下发恢复默认（0xDEAD）")

        # ------------------------------------------------------------ 单步
        def _step_card(self) -> Card:
            card = Card("单步调试 · 高层流程命令")
            form = QFormLayout()
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            self.cmd_combo = QComboBox()
            for spec in R.COMMANDS:
                self.cmd_combo.addItem(
                    f"{spec.group} · {spec.label}  (0x{spec.code:02X})", spec.code)
            self.cmd_combo.currentIndexChanged.connect(
                lambda _i: self._sync_command_note())
            form.addRow("流程命令", self.cmd_combo)
            self.cmd_note = QLabel("")
            self.cmd_note.setObjectName("SubLabel")
            self.cmd_note.setWordWrap(True)
            form.addRow("", self.cmd_note)

            self.param_edits: dict[str, QLineEdit] = {}
            grid = QGridLayout()
            for i, (key, label, unit, addr) in enumerate(PARAM_SPECS):
                edit = QLineEdit()
                edit.setPlaceholderText("留空=不写")
                edit.setToolTip(f"0x{addr:04X}")
                grid.addWidget(QLabel(f"{label}{('(' + unit + ')') if unit else ''}"),
                               i // 2, (i % 2) * 2)
                grid.addWidget(edit, i // 2, (i % 2) * 2 + 1)
                self.param_edits[key] = edit
            form.addRow("流程参数", grid)

            btn_row = QHBoxLayout()
            self.send_btn = self._button("下发命令", self._send_manual, "Primary")
            self.abort_btn = self._button("中止", self._abort_manual)
            self.pause_btn = self._button("暂停", self._pause_manual)
            self.resume_btn = self._button("继续", self._resume_manual)
            for b in (self.send_btn, self.abort_btn, self.pause_btn, self.resume_btn):
                btn_row.addWidget(b)
            btn_row.addStretch(1)
            form.addRow("", btn_row)

            self.manual_status = KV("流程状态", "未连接")
            form.addRow("", self.manual_status)
            self.manual_progress = QProgressBar()
            self.manual_progress.setRange(0, 100)
            form.addRow("进度", self.manual_progress)
            self.manual_detail = QLabel("-")
            self.manual_detail.setObjectName("SubLabel")
            self.manual_detail.setWordWrap(True)
            form.addRow("", self.manual_detail)
            card.body().addLayout(form)
            self._sync_command_note()
            return card

        def _sync_command_note(self) -> None:
            code = int(self.cmd_combo.currentData())
            spec = R.COMMAND_BY_CODE[code]
            text = f"{spec.name}：{spec.description or '无补充说明'}"
            if spec.requires_confirm:
                text += "（标定类命令，执行前需确认）"
            self.cmd_note.setText(text)

        def _parse_manual_params(self) -> dict[str, int]:
            out: dict[str, int] = {}
            for key, edit in self.param_edits.items():
                text = edit.text().strip()
                if not text:
                    continue
                try:
                    out[key] = _parse_int_text(text)
                except ValueError as exc:
                    raise GripperDebugError(f"参数 {key!r} 不是整数：{text!r}") from exc
            return out

        def _send_manual(self) -> None:
            code = int(self.cmd_combo.currentData())
            spec = R.COMMAND_BY_CODE[code]
            if spec.requires_confirm:
                if not _confirm(self, "确认标定命令",
                                f"将执行「{spec.label}」（0x{code:02X}），可能改写板端零点/行程。\n"
                                "确认现场安全并继续？"):
                    return
            try:
                params = self._parse_manual_params()
                self.session.start_command(code, params)
                self._manual_active = True
                self._manual_started = time.monotonic()
                self._sync_command_note()
                self.conn_note.setText(f"已下发：{spec.label}（0x{code:02X}）")
            except Exception as exc:  # noqa: BLE001
                _warn(self, "下发失败", str(exc))
                return
            self._sync_enabled()

        def _abort_manual(self) -> None:
            self._write(self.session.abort_command, "已下发流程中止")
            self._manual_active = False
            self._sync_enabled()

        def _pause_manual(self) -> None:
            self._write(self.session.pause_command, "已下发流程暂停")

        def _resume_manual(self) -> None:
            self._write(self.session.resume_command, "已下发流程继续")

        # ------------------------------------------------------------ 监视
        def _monitor_card(self) -> Card:
            card = Card("只读监视")
            self.monitor_table = QTableWidget(len(R.MONITOR_ORDER), 4)
            self.monitor_table.setHorizontalHeaderLabels(["分组", "名称", "数值", "原始值"])
            self.monitor_table.verticalHeader().setVisible(False)
            self.monitor_table.verticalHeader().setDefaultSectionSize(24)
            self.monitor_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.monitor_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            hh = self.monitor_table.horizontalHeader()
            hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
            self.monitor_table.setColumnWidth(0, 80)
            self.monitor_table.setColumnWidth(1, 190)
            self.monitor_table.setColumnWidth(3, 100)
            for row, name in enumerate(R.MONITOR_ORDER):
                reg = R.MONITOR_REGISTERS[name]
                self.monitor_table.setItem(row, 0, QTableWidgetItem(reg.group))
                item = QTableWidgetItem(reg.label)
                item.setToolTip(reg.bits or f"0x{reg.address:04X}")
                self.monitor_table.setItem(row, 1, item)
                self.monitor_table.setItem(row, 2, QTableWidgetItem("-"))
                self.monitor_table.setItem(row, 3, QTableWidgetItem("-"))
            self.monitor_table.setMinimumHeight(260)
            card.add(self.monitor_table)
            note = QLabel("只读；数值按寄存器 Scale 换算，原始值为板端 16 位寄存器原值。"
                          "流程状态 0=空闲 1=执行中 2=完成 3=错误 4=中止。")
            note.setObjectName("SubLabel")
            note.setWordWrap(True)
            card.add(note)
            return card

        # ------------------------------------------------------------ 序列
        def _sequence_card(self) -> Card:
            card = Card("动作序列 · 组合动作")
            top = QHBoxLayout()
            self.seq_combo = QComboBox()
            self.seq_combo.currentIndexChanged.connect(lambda _i: self._on_seq_changed())
            self.new_seq_btn = self._button("新建序列", self._new_sequence)
            self.del_seq_btn = self._button("删除序列", self._delete_sequence)
            self.seq_name_edit = QLineEdit()
            self.seq_name_edit.setPlaceholderText("序列名称")
            self.seq_name_edit.editingFinished.connect(self._apply_seq_name)
            self.save_seq_btn = self._button("保存序列", self._save_all)
            for w in (QLabel("序列"), self.seq_combo, self.new_seq_btn,
                      self.del_seq_btn, QLabel("名称"), self.seq_name_edit,
                      self.save_seq_btn):
                top.addWidget(w)
            top.setStretch(1, 1)
            top.setStretch(5, 1)
            card.body().addLayout(top)

            self.step_table = QTableWidget(0, 6)
            self.step_table.setHorizontalHeaderLabels(
                ["#", "步骤名称", "流程命令", "参数", "等待", "超时"])
            self.step_table.verticalHeader().setVisible(False)
            self.step_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.step_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.step_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.step_table.doubleClicked.connect(lambda _i: self._edit_step())
            hh = self.step_table.horizontalHeader()
            for col, width in ((0, 44), (1, 180), (2, 210), (4, 120), (5, 90)):
                hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
                self.step_table.setColumnWidth(col, width)
            hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            self.step_table.setMinimumHeight(190)
            card.add(self.step_table)

            edit_row = QHBoxLayout()
            self.add_step_btn = self._button("添加步骤", self._add_step)
            self.edit_step_btn = self._button("编辑选中", self._edit_step)
            self.del_step_btn = self._button("删除选中", self._remove_step)
            self.up_step_btn = self._button("上移", lambda: self._move_step(-1))
            self.down_step_btn = self._button("下移", lambda: self._move_step(1))
            for btn in (self.add_step_btn, self.edit_step_btn, self.del_step_btn,
                        self.up_step_btn, self.down_step_btn):
                edit_row.addWidget(btn)
            edit_row.addStretch(1)
            card.body().addLayout(edit_row)

            run_row = QHBoxLayout()
            self.run_seq_btn = self._button("运行序列", self._run_sequence, "Primary")
            self.pause_seq_btn = self._button("暂停", self._pause_sequence)
            self.resume_seq_btn = self._button("继续", self._resume_sequence)
            self.stop_seq_btn = self._button("中止", self._stop_sequence, "Danger")
            for b in (self.run_seq_btn, self.pause_seq_btn,
                      self.resume_seq_btn, self.stop_seq_btn):
                run_row.addWidget(b)
            run_row.addStretch(1)
            card.body().addLayout(run_row)

            self.seq_status = QLabel("空闲")
            self.seq_status.setObjectName("SubLabel")
            self.seq_status.setWordWrap(True)
            card.add(self.seq_status)
            self.seq_progress = QProgressBar()
            self.seq_progress.setRange(0, 100)
            card.add(self.seq_progress)
            note = QLabel("每一步先写参数再写 PROC_CMD；「检查板端完成」会轮询 PROC_STATUS，"
                          "「固定延时」只等指定时间。序列与连接参数保存在 "
                          f"{self.store.path}，不写 devices.yaml；离开本页会中止正在运行的序列。")
            note.setObjectName("SubLabel")
            note.setWordWrap(True)
            card.add(note)
            return card

        # ------------------------------------------------------------ 序列编辑
        def _current_sequence(self) -> GripperSequence | None:
            if not (0 <= self.seq_index < len(self.sequences)):
                return None
            return self.sequences[self.seq_index]

        def _refresh_sequence_combo(self, select: int | None = None) -> None:
            self.seq_combo.blockSignals(True)
            self.seq_combo.clear()
            for seq in self.sequences:
                self.seq_combo.addItem(seq.name)
            self.seq_index = min(max(0, select if select is not None else self.seq_index),
                                 max(0, len(self.sequences) - 1))
            self.seq_combo.setCurrentIndex(self.seq_index if self.sequences else -1)
            self.seq_combo.blockSignals(False)
            self._on_seq_changed()

        def _on_seq_changed(self) -> None:
            self.seq_index = self.seq_combo.currentIndex()
            seq = self._current_sequence()
            self.seq_name_edit.setText(seq.name if seq else "")
            self._refresh_step_table()

        def _apply_seq_name(self) -> None:
            seq = self._current_sequence()
            if seq is None:
                return
            name = self.seq_name_edit.text().strip()
            if not name or name == seq.name:
                return
            seq.name = name
            self._refresh_sequence_combo(self.seq_index)
            self.seq_status.setText("序列名称已改，点「保存序列」落盘")

        def _new_sequence(self) -> None:
            self.sequences.append(GripperSequence("新序列", []))
            self._refresh_sequence_combo(len(self.sequences) - 1)
            self.seq_status.setText("已新建序列（未保存）")

        def _delete_sequence(self) -> None:
            seq = self._current_sequence()
            if seq is None:
                return
            if not _confirm(self, "删除序列", f"确认删除序列「{seq.name}」？"):
                return
            del self.sequences[self.seq_index]
            self._refresh_sequence_combo(max(0, self.seq_index - 1))
            self.seq_status.setText("已删除序列（点「保存序列」落盘）")

        def _refresh_step_table(self) -> None:
            seq = self._current_sequence()
            steps = seq.steps if seq else []
            self.step_table.setRowCount(len(steps))
            for row, step in enumerate(steps):
                spec = R.COMMAND_BY_CODE.get(step.command)
                params = "，".join(
                    f"{dict((k, l) for k, l, _u, _a in PARAM_SPECS).get(k, k)}={v}"
                    for k, v in step.params.items()) or "—"
                wait = "检查完成" if step.wait == STEP_WAIT_DONE else f"延时 {step.delay_ms}ms"
                for col, text in enumerate((
                        str(row + 1), step.name,
                        f"{spec.label if spec else '?'} (0x{step.command:02X})",
                        params, wait, f"{step.timeout_ms}ms")):
                    item = QTableWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, row)
                    self.step_table.setItem(row, col, item)
            self.seq_progress.setValue(0)

        def _add_step(self) -> None:
            seq = self._current_sequence()
            if seq is None:
                _warn(self, "没有序列", "请先新建一个序列")
                return
            dlg = StepDialog(self)
            if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_step is None:
                return
            seq.steps.append(dlg.result_step)
            self._refresh_step_table()
            self.step_table.selectRow(len(seq.steps) - 1)
            self.seq_status.setText("已添加步骤（未保存）")

        def _edit_step(self) -> None:
            seq = self._current_sequence()
            row = self.step_table.currentRow()
            if seq is None or not (0 <= row < len(seq.steps)):
                return
            dlg = StepDialog(self, seq.steps[row])
            if dlg.exec() != QDialog.DialogCode.Accepted or dlg.result_step is None:
                return
            seq.steps[row] = dlg.result_step
            self._refresh_step_table()
            self.step_table.selectRow(row)
            self.seq_status.setText("已修改步骤（未保存）")

        def _remove_step(self) -> None:
            seq = self._current_sequence()
            row = self.step_table.currentRow()
            if seq is None or not (0 <= row < len(seq.steps)):
                return
            del seq.steps[row]
            self._refresh_step_table()
            self.seq_status.setText("已删除步骤（未保存）")

        def _move_step(self, delta: int) -> None:
            seq = self._current_sequence()
            row = self.step_table.currentRow()
            if seq is None or not (0 <= row < len(seq.steps)):
                return
            target = row + delta
            if not (0 <= target < len(seq.steps)):
                return
            seq.steps[row], seq.steps[target] = seq.steps[target], seq.steps[row]
            self._refresh_step_table()
            self.step_table.selectRow(target)
            self.seq_status.setText("已调整顺序（未保存）")

        def _run_sequence(self) -> None:
            seq = self._current_sequence()
            if seq is None or not seq.steps:
                _warn(self, "无法运行", "当前序列没有步骤")
                return
            if not self.session.connected:
                _warn(self, "未连接", "请先连接夹爪板")
                return
            dangerous = [s.name for s in seq.steps
                         if R.COMMAND_BY_CODE.get(s.command)
                         and R.COMMAND_BY_CODE[s.command].requires_confirm]
            text = (f"将执行「{seq.name}」共 {len(seq.steps)} 步动作。\n"
                    "确认现场安全、机构运动范围内无人后继续？")
            if dangerous:
                text = (f"序列包含标定类命令：{'、'.join(dangerous)}，"
                        "可能改写板端零点/行程。\n" + text)
            if not _confirm(self, "运行序列", text):
                return
            try:
                self.runner.start(seq)
            except Exception as exc:  # noqa: BLE001
                _warn(self, "启动失败", str(exc))
                return
            self._sync_enabled()
            self._update_sequence_state()

        def _pause_sequence(self) -> None:
            self.runner.pause()
            self._update_sequence_state()
            self._sync_enabled()

        def _resume_sequence(self) -> None:
            self.runner.resume()
            self._update_sequence_state()
            self._sync_enabled()

        def _stop_sequence(self) -> None:
            self.runner.stop()
            self._update_sequence_state()
            self._sync_enabled()

        # ------------------------------------------------------------ 周期
        def _tick(self, force_monitor: bool = False) -> None:
            if not self.isVisible() and not force_monitor:
                return
            if not self.session.connected:
                if self.session.heartbeat_error:
                    self.conn_note.setText(f"心跳失败：{self.session.heartbeat_error}")
                self._sync_enabled()
                return
            now = time.monotonic()
            try:
                if self.runner.state.phase in ("running", "paused"):
                    self.runner.tick()
                    self._update_sequence_state()
                if self._manual_active:
                    self._update_manual_progress()
                interval = 1.0 if self.runner.state.phase in ("running", "paused") else 0.6
                if force_monitor or now - self._last_monitor_at >= interval:
                    self._last_monitor_at = now
                    self._update_monitor()
                self._update_heartbeat_label()
                param_status = self._monitor_values.get("PARAM_STATUS")
                if param_status is not None:
                    self.store_status.setText(
                        f"存储状态：{param_status.text}（0=空闲 1=执行 2=完成 3=错误）")
            except Exception as exc:  # noqa: BLE001 - 轮询失败不弹窗, 标红并断开
                self.conn_pill.set_text("连接异常", THEME.danger)
                self.conn_note.setText(str(exc))
                self._manual_active = False
                try:
                    self.session.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._sync_enabled()

        def _update_heartbeat_label(self) -> None:
            if not self.session.connected:
                self.hb_status.setText("心跳：未连接")
                return
            if self.session.config is not None and self.session.config.heartbeat_ms <= 0:
                self.hb_status.setText("心跳：已关闭（不写 0x008A）")
                return
            if self.session.heartbeat_error:
                self.hb_status.setText(f"心跳失败：{self.session.heartbeat_error}")
                return
            last = self.session.last_heartbeat_monotonic
            if last is None:
                self.hb_status.setText("心跳：等待首次发送")
            else:
                age = time.monotonic() - last
                self.hb_status.setText(
                    f"心跳：计数 {self.session.heartbeat_count}，{age:.1f}s 前写入")

        def _update_monitor(self) -> None:
            values = self.session.read_monitor()
            self._monitor_values = values
            for row, name in enumerate(R.MONITOR_ORDER):
                val = values.get(name)
                if val is None:
                    continue
                item = self.monitor_table.item(row, 2)
                if item is not None:
                    item.setText(val.text)
                    item.setToolTip(val.bits or f"0x{val.raw:04X}")
                raw = self.monitor_table.item(row, 3)
                if raw is not None:
                    raw.setText(f"0x{val.raw & 0xFFFF:04X}" if val.raw >= 0
                                else str(val.raw))

        def _update_manual_progress(self) -> None:
            progress = self.session.read_progress()
            self._apply_progress(progress)
            if progress.status in (2, 3, 4):
                self._manual_active = False
                self._sync_enabled()

        def _apply_progress(self, progress) -> None:
            self.manual_status.set_key("流程状态", progress.text,
                                       THEME.ok if progress.status == 2
                                       else THEME.danger if progress.status in (3, 4)
                                       else THEME.deep_teal)
            self.manual_progress.setValue(int(progress.progress))
            self.manual_detail.setText(
                f"步骤 {progress.current_step}/{progress.total_steps} · "
                f"错误码 {progress.error_code} · 步骤状态 {progress.step_status} · "
                f"已耗时 {progress.elapsed_ms}ms · 累计 {progress.cmd_count} 次")

        def _update_sequence_state(self) -> None:
            st = self.runner.state
            text = f"{st.text}：{st.message}"
            self.seq_status.setText(text)
            if st.progress is not None:
                self.seq_progress.setValue(int(st.progress.progress))
            elif st.phase == "done":
                self.seq_progress.setValue(100)
            if st.phase in ("done", "error", "stopped", "idle"):
                self.run_seq_btn.setEnabled(self.session.connected and not self._manual_active)
            self._sync_enabled()

        # ------------------------------------------------------------ 状态
        def _sync_enabled(self) -> None:
            connected = self.session.connected
            self.connect_btn.setEnabled(not connected)
            self.disconnect_btn.setEnabled(connected)
            self.save_cfg_btn.setEnabled(True)
            for btn in (self.enable_btn, self.estop_btn, self.clear_err_btn,
                        self.reset_btn, self.sys_mode_btn, self.iwdg_btn,
                        self.proc_mode_btn, self.send_btn, self.abort_btn,
                        self.pause_btn, self.resume_btn, self.run_seq_btn,
                        self.pause_seq_btn, self.resume_seq_btn,
                        self.stop_seq_btn):
                btn.setEnabled(connected)
            for w in (self.hb_timeout_spin, self.iwdg_spin,
                      self.sys_mode_combo, self.proc_mode_combo):
                w.setEnabled(connected)
            active = self.runner.state.phase in ("running", "paused")
            for w in (self.seq_combo, self.seq_name_edit, self.new_seq_btn,
                      self.del_seq_btn, self.save_seq_btn, self.add_step_btn,
                      self.edit_step_btn, self.del_step_btn, self.up_step_btn,
                      self.down_step_btn):
                w.setEnabled(not active)
            self.send_btn.setEnabled(connected and not active and not self._manual_active)
            self.run_seq_btn.setEnabled(connected and not self._manual_active and not active)
            self.pause_seq_btn.setEnabled(connected and self.runner.state.phase == "running")
            self.resume_seq_btn.setEnabled(connected and self.runner.state.phase == "paused")
            self.stop_seq_btn.setEnabled(connected and active)
            self.hb_check.setEnabled(not connected)
            self.heartbeat_spin.setEnabled(not connected)
            self.conn_pill.set_text(
                "已连接" if connected else "未连接",
                THEME.ok if connected else THEME.text_secondary)

        def refresh(self) -> None:  # noqa: pragma: no cover - Qt
            self._sync_enabled()
            if self.session.connected:
                self._update_heartbeat_label()
                self._update_sequence_state()
            else:
                self.manual_status.set_key("流程状态", "未连接")
                self.hb_status.setText("心跳：未连接")

        def hideEvent(self, event) -> None:  # noqa: D401
            # 离开页面即停止序列（不把正在跑的调试序列留在后台）；连接与心跳保留。
            if self.runner.state.phase in ("running", "paused"):
                self.runner.stop()
                self._update_sequence_state()
            super().hideEvent(event)

        def shutdown(self) -> None:
            self._timer.stop()
            try:
                self.runner.stop()
            except Exception:  # noqa: BLE001
                pass
            self.session.disconnect()


__all__ = ["GUI_REQUIRED", "GripperPage", "StepDialog"]
