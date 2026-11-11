"""设备与链路配置页 (E21, 03 §9)。

七节点（4板/机器人/DS86/S100）与 5 相机通道; 物理 Link/逻辑 Board 映射。
三张表 (设备节点 / 相机通道 / 链路板映射) 均支持**新增 / 删除 / 修改**,
每条记录可配**协议** (serial / Modbus-RTU over TCP / JSON-line over TCP / ZMQ)
与其连接参数 (串口 或 IP+端口), 并可**测试连接** (只做连通性, 不下发动作指令)。

未确认的现场输入一律显示"待核对/未启用", 不伪装为已连接; 真实 IP/端口/串口
只能由现场责任人在对话框内输入并勾选"已现场核对"。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from application.contracts.enums import DeviceProtocol
from application.presentation.components import Card, EmptyHint, SectionTitle
from application.presentation.theme import THEME
from application.services.device_config import (
    KIND_LABELS,
    LOCAL_PROTOCOLS,
    PROTOCOL_LABELS,
    SERIAL_PROTOCOLS,
    TCP_PROTOCOLS,
    CameraEntry,
    DeviceEntry,
    LinkEntry,
    endpoint,
    valid_protocol,
)
from application.services.device_probe import PROBE_SAFETY_NOTE, probe
from application.services.device_scan import (
    SCAN_HELP,
    SCAN_MS_PICK,
    ScanOutcome,
    format_network_diagnosis,
    scan_ds86_cameras,
    scan_d405_usb,
)
from application.services.device_state import (
    TONE_DANGER,
    TONE_MUTED,
    TONE_OK,
    TONE_WARN,
)
from application.presentation.pages import state_of, store_of

try:  # pragma: no cover
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
        QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
        QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
        QTableWidgetItem, QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

_TONE_COLORS = {
    TONE_OK: THEME.ok,             # 仅"已连接(会话打开)"
    TONE_WARN: THEME.warn,         # 待核对 / 探测可达(不伪装成功)
    TONE_DANGER: THEME.danger,     # 探测失败
    TONE_MUTED: THEME.text_disabled,   # 未启用 / 未连接
}

# ---------------------------------------------------------------- 字段规格
_PROTO_FIELD = {"key": "protocol", "label": "协议", "kind": "combo",
                "options": PROTOCOL_LABELS, "role": "protocol"}
_ENABLED_FIELD = {"key": "enabled", "label": "启用", "kind": "check"}
_CONFIRMED_FIELD = {"key": "confirmed", "label": "已现场核对", "kind": "check"}
_NOTES_FIELD = {"key": "notes", "label": "备注", "kind": "text"}

_SERIAL_FIELDS = [
    {"key": "serial_port", "label": "串口", "kind": "text", "section": "serial",
     "placeholder": "如 COM3 / /dev/ttyUSB0"},
    {"key": "baudrate", "label": "波特率", "kind": "int", "min": 300,
     "max": 4000000, "section": "serial"},
    {"key": "data_bits", "label": "数据位", "kind": "int", "min": 5, "max": 8,
     "section": "serial"},
    {"key": "parity", "label": "校验", "kind": "combo", "section": "serial",
     "options": {"N": "N 无", "E": "E 偶", "O": "O 奇"}},
    {"key": "stop_bits", "label": "停止位", "kind": "int", "min": 1, "max": 2,
     "section": "serial"},
    {"key": "duplex", "label": "双工", "kind": "combo", "section": "serial",
     "options": {"half": "半双工", "full": "全双工"}},
]
_TCP_FIELDS = [
    {"key": "host", "label": "IP 地址", "kind": "text", "section": "tcp",
     "placeholder": "如 192.168.1.10"},
    {"key": "port", "label": "端口", "kind": "int", "min": 0, "max": 65535,
     "special_zero": True, "section": "tcp"},
]
_DEVICE_SPEC = [
    {"key": "device_id", "label": "设备 ID", "kind": "text"},
    {"key": "name", "label": "名称", "kind": "text"},
    {"key": "kind", "label": "类型", "kind": "combo", "options": KIND_LABELS},
    _PROTO_FIELD, _ENABLED_FIELD, _CONFIRMED_FIELD,
    {"key": "link_id", "label": "链路", "kind": "text", "section": "board"},
    {"key": "address", "label": "板地址", "kind": "int", "min": 0, "max": 255,
     "special_zero": True, "section": "board"},
    {"key": "firmware_profile", "label": "固件 profile", "kind": "text",
     "section": "board"},
    _NOTES_FIELD,
] + _SERIAL_FIELDS + _TCP_FIELDS
_LINK_SPEC = [
    {"key": "link_id", "label": "链路 ID", "kind": "text"},
    {"key": "transport_kind", "label": "物理承载", "kind": "combo",
     "options": {"serial": "serial 串行", "raw_rtu_tunnel": "raw_rtu_tunnel 透传"}},
    _PROTO_FIELD, _ENABLED_FIELD, _CONFIRMED_FIELD, _NOTES_FIELD,
] + _SERIAL_FIELDS + _TCP_FIELDS
# 相机可选协议: 网络类 + 本地 SDK (DS86); 不含串口 (CameraEntry 无串口字段)
_CAMERA_PROTOCOLS = {k: v for k, v in PROTOCOL_LABELS.items()
                     if k in TCP_PROTOCOLS + LOCAL_PROTOCOLS}
# 相机的**网络类**参数: 都归到"网络参数"组, 于是选**本地 SDK** 时整组隐藏
# （本地 SDK 直连没有 IP/端口/流地址/服务通道, 留着这些框只会让人以为要填）。
# 序列号/采集 profile/标定 ID 对本地 SDK 仍然有用, 不进本组。
_CAMERA_NET_FIELDS = [
    {"key": "stream_url", "label": "流地址", "kind": "text", "section": "tcp",
     "placeholder": "如 rtsp://..."},
    {"key": "service_id", "label": "服务 ID", "kind": "text", "section": "tcp"},
    {"key": "channel_id", "label": "服务内通道", "kind": "text", "section": "tcp"},
]
# DEC-081：DS86 也有 IP，而且设备配置里的 IP 会被启动打开使用（可选）。
# 它不放在"网络参数"组里 —— 该组对本地 SDK 会整组收起；单独一个字段，
# 对网络相机仍是必填的 IP，对本地 SDK 则是可选的"按 IP 打开/核对"。
_CAMERA_HOST_FIELD = {
    "key": "host", "label": "IP 地址", "kind": "text",
    "placeholder": "如 192.168.2.10（DS86 可选：填了按此 IP 打开并核对）"}
_CAMERA_PORT_FIELD = next(f for f in _TCP_FIELDS if f["key"] == "port")
_CAMERA_SPEC = [
    {"key": "camera_id", "label": "通道 ID", "kind": "text"},
    {"key": "name", "label": "名称", "kind": "text"},
    {"key": "protocol", "label": "协议", "kind": "combo", "options": _CAMERA_PROTOCOLS,
     "role": "protocol"},
    _ENABLED_FIELD, _CONFIRMED_FIELD,
    # 序列号: 旧工程的相机配置里本来就没有, 运行期「扫描设备」扫出来挑一台写回
    # （`services/device_scan.py`；选本地 SDK 时才显示这个按钮）。
    {"key": "serial", "label": "序列号", "kind": "text"},
    {"key": "capture_profile", "label": "采集 profile", "kind": "text"},
    {"key": "calibration_id", "label": "标定 ID", "kind": "text"},
    _NOTES_FIELD,
    _CAMERA_HOST_FIELD,
] + _CAMERA_NET_FIELDS + [_CAMERA_PORT_FIELD]


def _is_camera_spec(spec: list[dict]) -> bool:
    """这份字段规格是不是"相机通道"（只有它的「序列号」旁要挂「扫描设备」）。"""
    return any(f.get("key") == "camera_id" for f in spec)


def _local_ipv4_networks() -> list[tuple[str, str, str]]:
    """本机启用网卡的 ``(名称, IPv4, 子网掩码)`` —— 扫描 0 台时给现场对网段用。

    用 Qt 的 QNetworkInterface（打包后不需要外部命令）；取不到就如实返回空表。
    """
    try:
        from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
    except Exception:  # noqa: BLE001 - 无 QtNetwork 时诊断降级
        return []
    out: list[tuple[str, str, str]] = []
    try:
        interfaces = QNetworkInterface.allInterfaces()
    except Exception:  # noqa: BLE001
        return []
    for iface in interfaces:
        try:
            entries = iface.addressEntries()
        except Exception:  # noqa: BLE001
            continue
        name = iface.humanReadableName() or iface.name() or "网卡"
        for entry in entries:
            ip = entry.ip()
            try:
                if ip.protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                    continue
            except Exception:  # noqa: BLE001
                continue
            out.append((name, ip.toString(), entry.netmask().toString()))
    return out


if GUI_REQUIRED:

    class _EntryDialog(QDialog):
        """设备/链路/相机的编辑对话框: 字段随协议切换 (串口组 / 网络组)。"""

        def __init__(self, title: str, spec: list[dict], values: dict,
                     parent=None, state=None, *, scanner=None, selector=None,
                     notifier=None) -> None:
            super().__init__(parent)
            self.setWindowTitle(title)
            self.setMinimumWidth(420)
            self._state = state
            self._widgets: dict[str, QWidget] = {}
            self._spec = spec
            self._proto_combo: QComboBox | None = None
            self._kind_combo: QComboBox | None = None
            self._groups: dict[str, QGroupBox] = {}
            # 扫描设备的三个可注入点（默认走真实实现；离线测试注入假件）
            self._scanner = scanner
            self._selector = selector or _select_device
            self._notifier = notifier or _notify
            self.btn_scan: QPushButton | None = None

            outer = QVBoxLayout(self)
            form = QFormLayout()
            groups = {"serial": QGroupBox("串口参数"), "tcp": QGroupBox("网络参数"),
                      "board": QGroupBox("板映射")}
            group_forms: dict[str, QFormLayout] = {}
            for key, box in groups.items():
                gf = QFormLayout(box)
                group_forms[key] = gf

            for f in spec:
                w = self._make_widget(f, values.get(f["key"]))
                self._widgets[f["key"]] = w
                target: QWidget = w
                if f["key"] == "serial" and _is_camera_spec(spec):
                    # 旧工程的序列号就是"扫描 + 选择"来的 → 输入框旁给一个「扫描设备」
                    target = self._with_scan_button(w)
                section = f.get("section")
                if section and section in group_forms:
                    group_forms[section].addRow(f["label"], target)
                else:
                    form.addRow(f["label"], target)
                if f.get("role") == "protocol":
                    self._proto_combo = w
                if f["key"] == "kind":
                    self._kind_combo = w
            outer.addLayout(form)
            for key, box in groups.items():
                if group_forms[key].rowCount():
                    outer.addWidget(box)
                    self._groups[key] = box

            self.result_label = QLabel("未测试")
            self.result_label.setObjectName("SubLabel")
            self.result_label.setWordWrap(True)
            outer.addWidget(self.result_label)

            row = QHBoxLayout()
            self.btn_test = QPushButton("测试连接")
            self.btn_test.setObjectName("Ghost")
            self.btn_test.clicked.connect(self._test)
            row.addWidget(self.btn_test)
            row.addStretch(1)
            self.btn_cancel = QPushButton("取消")
            self.btn_cancel.setObjectName("Ghost")
            self.btn_cancel.clicked.connect(self.reject)
            self.btn_ok = QPushButton("确定")
            self.btn_ok.setObjectName("Primary")
            self.btn_ok.clicked.connect(self.accept)
            row.addWidget(self.btn_cancel)
            row.addWidget(self.btn_ok)
            outer.addLayout(row)

            if self._proto_combo is not None:
                self._proto_combo.currentIndexChanged.connect(self._sync)
            if self._kind_combo is not None:
                self._kind_combo.currentIndexChanged.connect(self._sync)
            if _is_camera_spec(spec):
                def reset_usb_confirmation(*_args):
                    if self._proto_combo.currentData() == DeviceProtocol.REALSENSE_USB.value:
                        self._widgets["confirmed"].setChecked(False)
                self._proto_combo.currentIndexChanged.connect(reset_usb_confirmation)
                self._widgets["serial"].textChanged.connect(reset_usb_confirmation)
            self._sync()

        def _kind(self) -> str:
            return self._kind_combo.currentData() if self._kind_combo else "board"

        # ---------- 控件 ----------
        def _make_widget(self, f: dict, value) -> QWidget:
            kind = f["kind"]
            if kind == "check":
                w = QCheckBox()
                w.setChecked(bool(value))
                return w
            if kind == "combo":
                w = QComboBox()
                for key, label in f["options"].items():
                    w.addItem(label, key)
                idx = w.findData(value)
                w.setCurrentIndex(idx if idx >= 0 else 0)
                return w
            if kind == "int":
                w = QSpinBox()
                w.setRange(int(f.get("min", 0)), int(f.get("max", 1000000)))
                if f.get("special_zero"):
                    w.setSpecialValueText("未设置")
                elif value is None:
                    w.setValue(int(f.get("min", 0)))
                w.setValue(int(value) if value is not None else int(f.get("min", 0)))
                return w
            w = QLineEdit()
            if f.get("placeholder"):
                w.setPlaceholderText(f["placeholder"])
            w.setText("" if value is None else str(value))
            return w

        def _with_scan_button(self, field: QWidget) -> QWidget:
            """把「序列号」输入框和「扫描设备」并成一行（旧程序的扫描 + 选择）。"""
            box = QWidget()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            row.addWidget(field, 1)
            self.btn_scan = QPushButton("扫描设备")
            self.btn_scan.setObjectName("Ghost")
            self.btn_scan.setToolTip(
                "扫描网络上的 DS86 相机（只读，不打开设备），从在线列表里挑一台，"
                "把它的序列号填到这里。旧工程也是这样取序列号的 —— "
                "相机配置里本来就没有序列号。")
            self.btn_scan.clicked.connect(self._scan_devices)
            row.addWidget(self.btn_scan)
            return box

        def _sync(self) -> None:  # pragma: no cover - Qt
            protocol = self._proto_combo.currentData() if self._proto_combo else ""
            if "serial" in self._groups:
                self._groups["serial"].setVisible(protocol in SERIAL_PROTOCOLS)
            if "tcp" in self._groups:
                # 本地 SDK 直连: IP/端口/流地址/服务通道 一律无关, 整组收起
                self._groups["tcp"].setVisible(protocol in TCP_PROTOCOLS)
            if "board" in self._groups:
                self._groups["board"].setVisible(self._kind() == "board")
            local = protocol in LOCAL_PROTOCOLS
            usb = protocol == DeviceProtocol.REALSENSE_USB.value
            self.result_label.setText(
                ("D405 USB：扫描并确认序列号，抓拍时按绑定打开" if usb else
                 "本地 SDK 直连：无需测试；启动时按配置的序列号/IP 打开")
                if local else "未测试")
            self.result_label.setStyleSheet(
                f"color:{THEME.text_disabled};" if local else "")
            btn = getattr(self, "btn_test", None)
            if btn is not None:
                btn.setToolTip("只枚举已绑定的 D405，不启动采集" if usb else
                               "本地 SDK 直连：没有网络参数可测，无需测试"
                               if local else "")
            scan_btn = getattr(self, "btn_scan", None)
            if scan_btn is not None:
                # 只有走厂家 SDK 的相机才谈得上"扫描设备"（网络相机没有可扫的东西）。
                scan_btn.setVisible(local)
                usb = protocol == DeviceProtocol.REALSENSE_USB.value
                scan_btn.setToolTip("扫描本机 USB D405 并选择序列号；扫描不启动采集" if usb
                                    else "扫描 DS86 并选择序列号；扫描不启动采集")
            host = self._widgets.get("host")
            if host is not None:
                host.setEnabled(protocol != DeviceProtocol.REALSENSE_USB.value)

        # ---------- 扫描设备（照旧程序：扫描 + 选择取序列号） ----------
        def _scan_devices(self) -> None:
            """扫描在线 DS86，把选中的序列号写进「序列号」框。"""
            btn = self.btn_scan
            if btn is not None:
                btn.setEnabled(False)
                btn.setText("扫描中…")
            try:
                usb = self._proto_combo.currentData() == DeviceProtocol.REALSENSE_USB.value
                scanner = self._scanner or (scan_d405_usb if usb else scan_ds86_cameras)
                outcome = scanner(scan_ms=SCAN_MS_PICK)
            except Exception as exc:  # noqa: BLE001 - 扫描出意外也要如实说
                outcome = ScanOutcome(
                    error=f"扫描 DS86 失败: {type(exc).__name__}: {exc}")
            finally:
                if btn is not None:
                    btn.setEnabled(True)
                    btn.setText("扫描设备")
            self._apply_scan(outcome)

        def _apply_scan(self, outcome) -> None:
            """讲清楚三种结果：扫不了 / 0 台 / 有设备但挑不了 —— 不拿一种冒充另一种。"""
            usb = self._proto_combo.currentData() == DeviceProtocol.REALSENSE_USB.value
            help_text = "请检查 D405 USB 连接、供电及 pyrealsense2。扫描不启动采集。" if usb else SCAN_HELP
            if outcome.error:
                self._notifier(self, "扫描设备",
                               f"{outcome.error}\n\n{help_text}", True)
                return
            if not outcome.devices:
                if usb:
                    self._notifier(self, "扫描设备", f"未发现 D405 USB 相机。\n\n{help_text}", False)
                    return
                host_widget = self._widgets.get("host")
                configured_ip = (host_widget.text().strip()
                                 if host_widget is not None else "")
                diagnosis = format_network_diagnosis(
                    _local_ipv4_networks(), configured_ip)
                self._notifier(
                    self, "扫描设备",
                    f"扫描 {outcome.scan_ms} ms，未发现在线 DS86 相机。\n\n"
                    f"{diagnosis}\n\n{SCAN_HELP}",
                    False)
                return
            if not outcome.with_serial:
                self._notifier(
                    self, "扫描设备",
                    f"扫描到 {len(outcome.devices)} 台设备，但都没有上报序列号，"
                    "无法按序列号打开：\n"
                    + "\n".join(f"· {d.label()}" for d in outcome.devices), True)
                return
            labels = [d.label() for d in outcome.devices]
            idx = self._selector(self, labels)
            if idx is None:
                return                                    # 放弃选择, 不动输入框
            chosen = outcome.devices[int(idx)]
            if not chosen.has_serial:
                self._notifier(self, "扫描设备",
                               f"{chosen.label()}\n该设备未上报序列号，无法按序列号打开，"
                               "序列号未填入。", True)
                return
            self._widgets["serial"].setText(chosen.serial)
            note = ("该设备标了 [占用]：可能已被其它程序（旧工程/相机工具）打开，"
                    "连接前请先关掉占它的程序。" if chosen.used
                    else "已填入「序列号」；点「确定」保存后生效。")
            self._notifier(self, "扫描设备", f"{chosen.label()}\n{note}", chosen.used)

        # ---------- 取值 ----------
        def values(self) -> dict:
            out: dict = {}
            for f in self._spec:
                key = f["key"]
                w = self._widgets[key]
                if isinstance(w, QCheckBox):
                    out[key] = w.isChecked()
                elif isinstance(w, QComboBox):
                    out[key] = w.currentData()
                elif isinstance(w, QSpinBox):
                    v = w.value()
                    out[key] = None if (f.get("special_zero") and v == 0) else v
                else:
                    text = w.text().strip()
                    out[key] = text
            return out

        def current_entry(self):
            return SimpleNamespace(**self.values())

        def _test(self) -> None:  # pragma: no cover - Qt
            entry = self.current_entry()
            if entry.protocol in SERIAL_PROTOCOLS:
                summary = getattr(entry, "serial_port", "") or "未配置串口"
            elif entry.protocol in LOCAL_PROTOCOLS:
                summary = "本地 SDK"
            else:
                host, port = endpoint(entry)
                summary = f"{host or '未配置IP'}:{port if port is not None else '-'}"
            self.btn_test.setEnabled(False)
            try:
                result = probe(entry)
            finally:
                self.btn_test.setEnabled(True)
            confirmed = bool(getattr(entry, "confirmed", False))
            # 探测结果记进运行期状态: 否则测试连接通过后状态仍停在不动的"待连接"。
            if self._state is not None:
                self._state.record_probe(_entry_key(self.values()), result)
            n_a = bool(getattr(result, "not_applicable", False))
            note = "" if (confirmed or n_a) else "（该记录尚未勾选“已现场核对”，不代表已验收）"
            self.result_label.setText(f"[{summary}] {result.summary}{note}")
            # 可达但未核对 -> 用警示色, 不显示成功绿 (未确认值不伪装为已通过)。
            if n_a:
                tone = THEME.text_disabled   # 本地 SDK: 无需测试, 中性 (不是失败)
            elif result.degraded:
                tone = THEME.warn      # 降级探测: 既不是失败, 也不能报成功
            elif not result.ok:
                tone = THEME.danger
            elif confirmed:
                tone = THEME.ok
            else:
                tone = THEME.warn
            self.result_label.setStyleSheet(f"color:{tone};")

    class DevicesPage(QWidget):
        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.store = store_of(ctx)
            self.state = state_of(ctx)
            self._node_ids: list[str] = []
            self._chan_ids: list[str] = []
            self._link_ids: list[str] = []

            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            top = QHBoxLayout(); top.setSpacing(10)
            self.node_card = Card()
            self.node_title, self.node_btns = _header(self.node_card, "设备节点（7）", [
                ("新增设备", "Primary", self._add_device),
                ("修改", "Ghost", self._edit_device),
                ("删除", "Ghost", self._del_device),
                ("测试连接", "Ghost", self._test_device),
                ("连接/断开", "Ghost", self._toggle_device_conn)])
            self.node_table = _table(["名称", "ID", "类型", "协议", "连接", "状态"])
            self.node_table.doubleClicked.connect(lambda *_a: self._edit_device())
            self.node_table.itemSelectionChanged.connect(self._sync_conn_buttons)
            self.node_card.add(self.node_table)
            top.addWidget(self.node_card, 3)

            self.chan_card = Card()
            self.chan_title, self.chan_btns = _header(self.chan_card, "相机通道（5）", [
                ("新增通道", "Primary", self._add_camera),
                ("修改", "Ghost", self._edit_camera),
                ("删除", "Ghost", self._del_camera),
                ("测试连接", "Ghost", self._test_camera),
                ("连接/断开", "Ghost", self._toggle_camera_conn)])
            self.chan_table = _table(["通道", "名称", "协议", "连接", "状态"])
            self.chan_table.doubleClicked.connect(lambda *_a: self._edit_camera())
            self.chan_table.itemSelectionChanged.connect(self._sync_conn_buttons)
            self.chan_card.add(self.chan_table)
            top.addWidget(self.chan_card, 2)
            root.addLayout(top)

            mid = QHBoxLayout(); mid.setSpacing(10)
            self.link_card = Card()
            self.link_title, _link_btns = _header(self.link_card, "链路 / 板映射（1）", [
                ("新增链路", "Primary", self._add_link),
                ("修改", "Ghost", self._edit_link),
                ("删除", "Ghost", self._del_link),
                ("测试连接", "Ghost", self._test_link)])
            self.link_table = _table(["链路", "承载", "协议", "连接", "绑定板", "状态"])
            self.link_table.doubleClicked.connect(lambda *_a: self._edit_link())
            self.link_card.add(self.link_table)
            mid.addWidget(self.link_card, 3)

            self.bind_card = Card("绑定校验")
            self.bind_layout = QVBoxLayout(); self.bind_card.body().addLayout(self.bind_layout)
            mid.addWidget(self.bind_card, 2)
            root.addLayout(mid, 1)

            if callable(getattr(self.ctx.stack, "camera_reload", None)):
                timing_text = (
                    "生效时机：相机连接参数保存后立即重建会话（任务/运动进行中需先停止）；"
                    "zmq 通道的 IP/端口以相机行为准（未填时回退 s100_01 设备行）；"
                    "串口/透传=空闲重连；固件 profile=断连或下次连接；"
                    "动作/互锁/标定=发布后下次任务；运行中不改底层连接。")
            else:
                timing_text = ("生效时机：串口/透传=空闲重连；固件 profile=断连或下次连接；"
                               "动作/互锁/标定=发布后下次任务；运行中不改底层连接。"
                               "（当前离线栈为假相机，相机连接参数不建立真实会话）")
            self.timing = QLabel(timing_text)
            self.timing.setObjectName("SubLabel"); self.timing.setWordWrap(True)
            root.addWidget(self.timing)
            self.safety = QLabel(PROBE_SAFETY_NOTE)
            self.safety.setObjectName("SubLabel"); self.safety.setWordWrap(True)
            root.addWidget(self.safety)
            self.refresh()

        # ---------- 刷新 ----------
        def refresh(self) -> None:  # pragma: no cover - Qt
            cfg = self.store.config
            self.node_title.setText(f"设备节点（{len(cfg.devices)}）")
            self.chan_title.setText(f"相机通道（{len(cfg.cameras)}）")
            self.link_title.setText(f"链路 / 板映射（{len(cfg.links)}）")
            self._node_ids = [d.device_id for d in cfg.devices]
            _fill(self.node_table, [
                (d.name or d.device_id, d.device_id, KIND_LABELS.get(d.kind, d.kind),
                 PROTOCOL_LABELS.get(d.protocol, d.protocol), d.connection_summary(),
                 self.state.status_text(d)) for d in cfg.devices],
                [self.state.tone_of(d) for d in cfg.devices],
                [self.state.detail_of(d) for d in cfg.devices])
            self._chan_ids = [c.camera_id for c in cfg.cameras]
            _fill(self.chan_table, [
                (c.camera_id, c.name or "-", PROTOCOL_LABELS.get(c.protocol, c.protocol),
                 c.connection_summary(), self.state.status_text(c)) for c in cfg.cameras],
                [self.state.tone_of(c) for c in cfg.cameras],
                [self.state.detail_of(c) for c in cfg.cameras])
            self._link_ids = [l.link_id for l in cfg.links]
            _fill(self.link_table, [
                (l.link_id, l.transport_kind, PROTOCOL_LABELS.get(l.protocol, l.protocol),
                 l.connection_summary(), _bound_boards(cfg, l.link_id),
                 self.state.status_text(l)) for l in cfg.links],
                [self.state.tone_of(l) for l in cfg.links],
                [self.state.detail_of(l) for l in cfg.links])

            while self.bind_layout.count():
                it = self.bind_layout.takeAt(0)
                if it.widget():
                    it.widget().setParent(None)
            lines = _validation_lines(self.store)
            for msg in lines:
                lab = QLabel(msg); lab.setObjectName("SubLabel"); lab.setWordWrap(True)
                self.bind_layout.addWidget(lab)
            self.bind_layout.addStretch(1)      # 内容靠上, 不被纵向摊开
            self.bind_layout.addWidget(EmptyHint(
                "真实验线/地址/序列号来自 E04/E10/E11；UI 绿色不能替代证据。"))
            self._sync_conn_buttons()

        # ---------- 设备节点 ----------
        def _add_device(self) -> None:  # pragma: no cover - Qt
            entry = DeviceEntry(device_id="", name="", kind="board",
                                protocol=DeviceProtocol.SERIAL.value)
            values = _dialog(self, "新增设备", _DEVICE_SPEC, _dict(entry), state=self.state)
            if values is None or not _has_id(self, values, "device_id", "设备 ID"):
                return
            try:
                self.store.add_device(DeviceEntry(**_clean(DeviceEntry, values)))
                self._commit()
            except Exception as exc:  # noqa: BLE001
                _warn(self, f"新增设备失败: {exc}")

        def _edit_device(self) -> None:  # pragma: no cover - Qt
            entry = self._selected(self.node_table, self._node_ids)
            if entry is None:
                return
            # DEC-081：S100 设备行的端点也可能被当作回退来源，改动后同样要重建会话
            if entry == "s100_01" and not self._camera_change_allowed():
                return
            cur = self.store.find_device(entry)
            values = _dialog(self, f"修改设备 {entry}", _DEVICE_SPEC, _dict(cur), state=self.state)
            if values is None:
                return
            try:
                self.store.update_device(entry, DeviceEntry(**_clean(DeviceEntry, values)))
                self._commit()
            except Exception as exc:  # noqa: BLE001
                _warn(self, f"修改设备失败: {exc}")
                return
            if entry == "s100_01":
                self._apply_camera_config("s100_01")

        def _del_device(self) -> None:  # pragma: no cover - Qt
            entry = self._selected(self.node_table, self._node_ids)
            if entry is None or not _confirm(self, f"删除设备 {entry} ?"):
                return
            self.store.remove_device(entry)
            self._commit()

        def _test_device(self) -> None:  # pragma: no cover - Qt
            entry = self._selected(self.node_table, self._node_ids)
            if entry is not None:
                _show_probe(self, self.store.find_device(entry), entry, self.state)

        def _toggle_device_conn(self) -> None:  # pragma: no cover - Qt
            key = self._selected(self.node_table, self._node_ids)
            if key is not None:
                _toggle_session(self, self.state, key)

        # ---------- 相机通道 ----------
        def _camera_change_allowed(self) -> bool:  # pragma: no cover - Qt
            """DEC-081：作业/运动进行中不允许改相机配置（重建会话会把在用会话抽走）。"""
            blocker = getattr(self.ctx.stack, "camera_reload_blocker", None)
            if callable(blocker):
                reason = blocker()
                if reason:
                    _warn(self, f"不能修改相机配置：{reason}")
                    return False
            return True

        def _apply_camera_config(self, camera_id: str | None = None) -> None:
            """保存后按新配置重建相机会话 —— 视觉调试/引导定位立即用新连接，不重启。"""
            reload_fn = getattr(self.ctx.stack, "camera_reload", None)
            if not callable(reload_fn):
                return          # 离线栈是假相机，没有连接配置可重建
            error: Exception | None = None
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                reload_fn(camera_id=camera_id)
            except Exception as exc:  # noqa: BLE001
                error = exc
            finally:
                QApplication.restoreOverrideCursor()
            if error is not None:
                _warn(self, "相机配置已保存，但应用失败："
                            f"{error}\n\n相机会话仍是旧配置；请修正后重试，或重启程序。")
            # 成功不弹窗：保存即生效，状态列/抓拍结果就是反馈（DEC-083）

        def _add_camera(self) -> None:  # pragma: no cover - Qt
            if not self._camera_change_allowed():
                return
            entry = CameraEntry(camera_id="", name="",
                                protocol=DeviceProtocol.JSON_TCP.value)
            values = _dialog(self, "新增相机通道", _CAMERA_SPEC, _dict(entry), state=self.state)
            if values is None or not _has_id(self, values, "camera_id", "通道 ID"):
                return
            try:
                self.store.add_camera(CameraEntry(**_clean(CameraEntry, values)))
                self._commit()
            except Exception as exc:  # noqa: BLE001
                _warn(self, f"新增通道失败: {exc}")
                return
            self._apply_camera_config(str(values.get("camera_id", "")))

        def _edit_camera(self) -> None:  # pragma: no cover - Qt
            if not self._camera_change_allowed():
                return
            entry = self._selected(self.chan_table, self._chan_ids)
            if entry is None:
                return
            cur = self.store.find_camera(entry)
            values = _dialog(self, f"修改通道 {entry}", _CAMERA_SPEC, _dict(cur), state=self.state)
            if values is None:
                return
            try:
                self.store.update_camera(entry, CameraEntry(**_clean(CameraEntry, values)))
                self._commit()
            except Exception as exc:  # noqa: BLE001
                _warn(self, f"修改通道失败: {exc}")
                return
            self._apply_camera_config(entry)

        def _del_camera(self) -> None:  # pragma: no cover - Qt
            if not self._camera_change_allowed():
                return
            entry = self._selected(self.chan_table, self._chan_ids)
            if entry is None or not _confirm(self, f"删除通道 {entry} ?"):
                return
            self.store.remove_camera(entry)
            self._commit()
            self._apply_camera_config(entry)

        def _test_camera(self) -> None:  # pragma: no cover - Qt
            entry = self._selected(self.chan_table, self._chan_ids)
            if entry is not None:
                _show_probe(self, self.store.find_camera(entry), entry, self.state)

        def _toggle_camera_conn(self) -> None:  # pragma: no cover - Qt
            key = self._selected(self.chan_table, self._chan_ids)
            if key is not None:
                _toggle_session(self, self.state, key)

        # ---------- 链路 ----------
        def _add_link(self) -> None:  # pragma: no cover - Qt
            entry = LinkEntry(link_id="", protocol=DeviceProtocol.SERIAL.value)
            values = _dialog(self, "新增链路", _LINK_SPEC, _dict(entry), state=self.state)
            if values is None or not _has_id(self, values, "link_id", "链路 ID"):
                return
            try:
                self.store.add_link(LinkEntry(**_clean(LinkEntry, values)))
                self._commit()
            except Exception as exc:  # noqa: BLE001
                _warn(self, f"新增链路失败: {exc}")

        def _edit_link(self) -> None:  # pragma: no cover - Qt
            entry = self._selected(self.link_table, self._link_ids)
            if entry is None:
                return
            cur = self.store.find_link(entry)
            values = _dialog(self, f"修改链路 {entry}", _LINK_SPEC, _dict(cur), state=self.state)
            if values is None:
                return
            try:
                self.store.update_link(entry, LinkEntry(**_clean(LinkEntry, values)))
                self._commit()
            except Exception as exc:  # noqa: BLE001
                _warn(self, f"修改链路失败: {exc}")

        def _del_link(self) -> None:  # pragma: no cover - Qt
            entry = self._selected(self.link_table, self._link_ids)
            if entry is None or not _confirm(self, f"删除链路 {entry} ?"):
                return
            self.store.remove_link(entry)
            self._commit()

        def _test_link(self) -> None:  # pragma: no cover - Qt
            entry = self._selected(self.link_table, self._link_ids)
            if entry is not None:
                _show_probe(self, self.store.find_link(entry), entry)

        # ---------- 内部 ----------
        def _selected(self, table, ids: list[str]) -> str | None:  # pragma: no cover - Qt
            row = table.currentRow()
            if row < 0 or row >= len(ids):
                QMessageBox.information(self, "提示", "请先选中一行")
                return None
            return ids[row]

        # ---------- 会话按钮启用状态 ----------
        def _sync_conn_buttons(self) -> None:  # pragma: no cover - Qt
            """按当前选中行启用/置灰「连接/断开」。

            **没有会话适配器的行置灰并写明原因**，不让操作员"点了才知道"：
            设备配置里没有对应会话（如没有可用的 zmq 通道）时控制台不报任何错，
            只有这里能说清。注意**不能**用 `_selected()`（它会在没选中时弹对话框），
            而 refresh() 清空选择也会触发本回调。
            """
            self._sync_one(self.node_table, self._node_ids, self.node_btns)
            self._sync_one(self.chan_table, self._chan_ids, self.chan_btns)

        def _sync_one(self, table, ids: list[str], btns: dict) -> None:
            btn = btns.get("连接/断开")
            if btn is None:
                return
            row = table.currentRow()
            key = ids[row] if 0 <= row < len(ids) else None
            can = key is not None and (self.state.has_adapter(key)
                                       or self.state.is_open(key))
            btn.setEnabled(can)
            btn.setToolTip("" if (can or key is None)
                           else self.state.no_adapter_reason(key))

        def _commit(self) -> None:  # pragma: no cover - Qt
            self.store.save()
            self.refresh()


# ---------------------------------------------------------------- 辅助
def _dict(entry) -> dict:
    if entry is None:
        return {}
    return {f: getattr(entry, f) for f in getattr(entry, "__dataclass_fields__", {})}


def _clean(cls, values: dict) -> dict:
    known = set(getattr(cls, "__dataclass_fields__", {}))
    return {k: v for k, v in values.items() if k in known}


def _bound_boards(cfg, link_id: str) -> str:  # pragma: no cover - Qt
    boards = [d for d in cfg.devices if d.kind == "board" and d.link_id == link_id]
    if not boards:
        return "未绑定"
    return "、".join(f"{b.device_id}@{b.address}" for b in boards)


def _validation_lines(store, *, limit: int = 5) -> list[str]:
    """绑定校验卡内容: 先阻塞项, 再未确认/未配置提示 (不把提示藏起来)。"""
    issues = store.validate()
    blocking = [i for i in issues if i.blocking]
    advice = [i for i in issues if not i.blocking]
    lines = [f"✕ {i.message}" for i in blocking] or ["无阻塞项"]
    if advice:
        lines.append(f"待核对/未配置 {len(advice)} 项：")
        lines += [f"• {i.message}" for i in advice[:limit]]
        if len(advice) > limit:
            lines.append(f"… 其余 {len(advice) - limit} 项见各表状态列")
    else:
        lines.append("无未确认项")
    return lines


def _dialog(parent, title, spec, values, state=None) -> dict | None:  # pragma: no cover - Qt
    dlg = _EntryDialog(title, spec, values, parent=parent, state=state)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return dlg.values()
    return None


def _entry_key(values: dict) -> str:
    """从对话框取值里取记录键 (与 DeviceStateRegistry.key_of 同一规则)。"""
    for attr in ("device_id", "camera_id", "link_id"):
        value = values.get(attr)
        if value:
            return str(value)
    return "?"


def _confirm(parent, text: str) -> bool:  # pragma: no cover - Qt
    return QMessageBox.question(
        parent, "确认", text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    ) == QMessageBox.StandardButton.Yes


def _select_device(parent, labels: list[str]) -> int | None:  # pragma: no cover - Qt
    """从在线设备里挑一台（返回选中序号；取消返回 None）。

    旧工程是下拉框；这里是列表对话框 —— 同样把「名称 (序列号) @ IP [占用]」摆给操作员，
    被占用的**照旧列出来**（旧程序也只是标注，不隐藏），由人决定。
    """
    item, ok = QInputDialog.getItem(
        parent, "选择 DS86 设备", "在线设备（[占用] = 可能已被其它程序打开）：",
        labels, 0, False)
    if not ok:
        return None
    return labels.index(item) if item in labels else None


def _notify(parent, title: str, text: str, warn: bool = False) -> None:  # pragma: no cover - Qt
    if warn:
        QMessageBox.warning(parent, title, text)
    else:
        QMessageBox.information(parent, title, text)


def _has_id(parent, values: dict, key: str, label: str) -> bool:  # pragma: no cover - Qt
    if str(values.get(key, "")).strip():
        return True
    QMessageBox.warning(parent, "错误", f"{label}不能为空")
    return False


def _warn(parent, text: str) -> None:  # pragma: no cover - Qt
    QMessageBox.warning(parent, "错误", text)


def _show_probe(parent, entry, entry_id: str, state=None) -> None:  # pragma: no cover - Qt
    if entry is None:
        return
    result = probe(entry)
    if state is not None:
        state.record_probe(entry_id, result)      # 记录后状态列才不是"不动的待连接"
        refresh = getattr(parent, "refresh", None)
        if callable(refresh):
            refresh()
    if getattr(result, "not_applicable", False):
        # 本地 SDK 直连: 无网络参数可测 —— 直接说"无需测试", 不要弹成失败警告。
        QMessageBox.information(
            parent, "测试连接",
            f"{entry_id}\n本地 SDK 直连，无需测试（没有网络参数可测）。\n\n"
            "这种设备是否可用要看「连接/断开」是否真的打开会话，或看真机采集结果。")
        return
    text = f"{entry_id}\n{result.summary}\n\n{PROBE_SAFETY_NOTE}"
    if result.ok:
        QMessageBox.information(parent, "测试连接", text)
    else:
        QMessageBox.warning(parent, "测试连接", text)


def _toggle_session(parent, state, key: str) -> None:  # pragma: no cover - Qt
    """连接/断开 = 真正开关会话; 无适配器时如实说明, 不伪装成功。"""
    try:
        if state.is_open(key):
            state.close(key)
            QMessageBox.information(parent, "断开", f"{key} 会话已关闭")
        else:
            state.open(key)
            QMessageBox.information(parent, "连接", f"{key} 会话已打开")
    except Exception as exc:  # noqa: BLE001
        QMessageBox.warning(parent, "连接/断开", str(exc))
    refresh = getattr(parent, "refresh", None)
    if callable(refresh):
        refresh()


def _toolbar(actions):  # pragma: no cover - Qt
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    for text, objname, handler in actions:
        btn = QPushButton(text)
        btn.setObjectName(objname)
        btn.clicked.connect(handler)
        h.addWidget(btn)
    h.addStretch(1)
    return w


def _header(card, title: str, actions):  # pragma: no cover - Qt
    """卡片头: 分区标题 + 行内工具栏。

    返回 ``(标题, {按钮文字: 按钮})`` —— 页面要按选中行**启用/置灰**按钮
    （没有会话适配器的行不该让点「连接/断开」，见 ``_sync_conn_buttons``）。
    """
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    label = SectionTitle(title)
    h.addWidget(label)
    h.addStretch(1)
    buttons: dict[str, Any] = {}
    for text, objname, handler in actions:
        btn = QPushButton(text)
        btn.setObjectName(objname)
        btn.clicked.connect(handler)
        h.addWidget(btn)
        buttons[text] = btn
    card.add(row)
    return label, buttons


def _table(headers):  # pragma: no cover - Qt
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    return t


def _fill(table, rows, tones=None, tips=None) -> None:  # pragma: no cover - Qt
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            item = QTableWidgetItem(str(val))
            if c == len(row) - 1:            # 状态列着色 (文字本身已表达, 不只靠颜色)
                tone = (tones[r] if tones and r < len(tones) else TONE_MUTED)
                item.setForeground(QColor(_TONE_COLORS.get(tone, THEME.text_primary)))
                if tips and r < len(tips) and tips[r]:
                    # 悬停看原因（如装配失败：机器人连接失败: 192.168.3.66:8080）
                    item.setToolTip(str(tips[r]))
            table.setItem(r, c, item)


__all__ = ["GUI_REQUIRED", "DevicesPage"]
