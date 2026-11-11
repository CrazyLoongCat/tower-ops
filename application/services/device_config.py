"""设备/链路/相机配置的模型与 YAML 持久化 (E21, 03 §9)。

设备与寻址属**已发布基线**层 (受保护路径 ``devices./links./boards.``, 见
``infrastructure.config.base``): 只能由本基线提供, 不能被配方/工位 override 覆盖。
本模块提供三类记录的增删改与原子落盘 (临时写 -> 原子替换 + ``.bak`` 备份, 复用
配方服务里的 :func:`atomic_write`), 并做绑定级校验 (ID 唯一 / 协议必填连接参数 /
同链路地址唯一)。

**不代填现场值**: 任何新建/修改的条目默认 ``confirmed=False``; 未确认的连接参数
(串口 / IP / 地址 / 固件 profile) 在 UI 上显示"待核对 / 未启用", 不伪装为已连接。
真实 IP / 端口 / 串口只能由现场责任人在 UI 中输入并勾选"已现场核对"。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from io import StringIO
from typing import Any
from urllib.parse import urlsplit

from ruamel.yaml import YAML

from application.contracts.enums import DeviceProtocol, LinkTransportKind
from application.services.devices import BindingIssue, build_binding_report
from application.services.recipes.service import atomic_write, rollback

CONFIG_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)

SERIAL_PROTOCOLS = (DeviceProtocol.SERIAL.value,)
# 需要 host/port 的网络协议
TCP_PROTOCOLS = (DeviceProtocol.MODBUS_TCP.value, DeviceProtocol.JSON_TCP.value,
                 DeviceProtocol.ZMQ.value, DeviceProtocol.RTSP.value,
                 DeviceProtocol.HTTP.value, DeviceProtocol.VENDOR_TCP.value)
# 本地 SDK 直连: 无网络参数, 不做连通性探测
LOCAL_PROTOCOLS = (DeviceProtocol.SDK.value, DeviceProtocol.REALSENSE_USB.value)
URL_PROTOCOLS = (DeviceProtocol.RTSP.value, DeviceProtocol.HTTP.value)

PROTOCOL_LABELS: dict[str, str] = {
    DeviceProtocol.SERIAL.value: "serial 串口",
    DeviceProtocol.MODBUS_TCP.value: "Modbus-RTU over TCP",
    DeviceProtocol.JSON_TCP.value: "JSON-line over TCP",
    DeviceProtocol.ZMQ.value: "ZMQ REQ/REP",
    DeviceProtocol.RTSP.value: "RTSP 流",
    DeviceProtocol.HTTP.value: "HTTP 流",
    DeviceProtocol.VENDOR_TCP.value: "厂商 SDK over TCP",
    DeviceProtocol.SDK.value: "本地 SDK",
    DeviceProtocol.REALSENSE_USB.value: "D405 USB 直连",
}

KIND_LABELS: dict[str, str] = {
    "board": "控制板", "robot": "机器人", "camera": "相机",
    "vision_service": "视觉服务", "edge": "边缘服务", "other": "其它",
}

# 明显未确认/占位的取值: 探测直接拒绝, 不把"未配"当成"连不上"。
PLACEHOLDER_TOKENS = frozenset({
    "unconfirmed", "unknown", "n/a", "na", "none", "null", "-", "?", "",
    "待核对", "待确认", "待定", "未配置", "未确认",
})


def is_placeholder(value: Any) -> bool:
    """取值是否明显是占位/未确认 (空串、含 UNCONFIRMED、"待核对" 等)。"""
    if value is None:
        return True
    text = str(value).strip().lower()
    if text in PLACEHOLDER_TOKENS:
        return True
    return any(tok in text for tok in ("unconfirmed", "待核对", "待确认"))


def valid_protocol(protocol: str) -> bool:
    return protocol in PROTOCOL_LABELS


def endpoint(entry: Any) -> tuple[str, int | None]:
    """取记录的 (host, port); rtsp/http 可在 host/port 缺省时从 ``stream_url`` 解析。

    这样网络摄像机只填一个 ``stream_url`` 也能做连通性探测与展示。
    """
    host = str(getattr(entry, "host", "") or "").strip()
    port = getattr(entry, "port", None)
    if is_placeholder(host) or port is None:
        url = str(getattr(entry, "stream_url", "") or "").strip()
        if url:
            try:
                parsed = urlsplit(url)
            except ValueError:
                parsed = None
            if parsed is not None:
                if is_placeholder(host):
                    host = parsed.hostname or host
                if port is None:
                    try:
                        port = parsed.port
                    except ValueError:
                        port = None
    return host, port


_KIND_ORDER = ("board", "robot", "camera", "vision_service", "edge", "other")


# ---------------------------------------------------------------- 记录模型
@dataclass
class DeviceEntry:
    """一个设备节点 (7 节点表中的一行), 含协议与连接参数。"""

    device_id: str
    name: str = ""
    kind: str = "board"
    protocol: str = DeviceProtocol.SERIAL.value
    enabled: bool = False
    confirmed: bool = False
    # 串口类
    serial_port: str = ""
    baudrate: int = 115200
    data_bits: int = 8
    parity: str = "N"
    stop_bits: int = 1
    duplex: str = "half"
    # TCP 类
    host: str = ""
    port: int | None = None
    # 板映射
    link_id: str = ""
    address: int | None = None
    firmware_profile: str = ""
    notes: str = ""

    def connection_summary(self) -> str:
        if self.protocol in LOCAL_PROTOCOLS:
            return "本地 SDK（无网络参数）"
        if self.protocol in SERIAL_PROTOCOLS:
            port = self.serial_port or "未配置串口"
            return f"{port} @ {self.baudrate}"
        host, port = endpoint(self)
        return f"{host or '未配置IP'}:{port if port is not None else '-'}"

    def status_text(self) -> str:
        """**配置就绪度**（只看配置本身）。会话与最近探测由 DeviceStateRegistry 给出，
        页面展示应使用 registry 的分级状态（已连接/探测可达/...），不要直接用这个。"""
        if not self.enabled:
            return "未启用"
        if not self.confirmed:
            return "待核对"
        return "未连接"

    def clone(self) -> "DeviceEntry":
        return DeviceEntry(**asdict(self))


@dataclass
class LinkEntry:
    """物理链路 (链路/板映射表的一行)。"""

    link_id: str
    transport_kind: str = LinkTransportKind.SERIAL.value
    protocol: str = DeviceProtocol.SERIAL.value
    enabled: bool = False
    confirmed: bool = False
    serial_port: str = ""
    baudrate: int = 115200
    data_bits: int = 8
    parity: str = "N"
    stop_bits: int = 1
    duplex: str = "half"
    host: str = ""
    port: int | None = None
    notes: str = ""

    def connection_summary(self) -> str:
        if self.protocol in SERIAL_PROTOCOLS:
            return f"{self.serial_port or '未配置串口'} @ {self.baudrate}"
        host = self.host or "未配置IP"
        return f"{host}:{self.port if self.port is not None else '-'}"

    def status_text(self) -> str:
        """**配置就绪度**（只看配置本身）。会话与最近探测由 DeviceStateRegistry 给出，
        页面展示应使用 registry 的分级状态（已连接/探测可达/...），不要直接用这个。"""
        if not self.enabled:
            return "未启用"
        if not self.confirmed:
            return "待核对"
        return "未连接"

    def clone(self) -> "LinkEntry":
        return LinkEntry(**asdict(self))


@dataclass
class CameraEntry:
    """一个相机通道 (5 通道表中的一行)。"""

    camera_id: str
    name: str = ""
    protocol: str = DeviceProtocol.JSON_TCP.value
    enabled: bool = False
    confirmed: bool = False
    serial: str = ""
    service_id: str = ""
    channel_id: str = ""
    host: str = ""
    port: int | None = None
    stream_url: str = ""
    capture_profile: str = ""
    calibration_id: str = ""
    notes: str = ""
    # 视觉调试页「图像算法」按通道保存的参数 (DEC-021)。**只影响显示预览**,
    # 不改原始帧、不喂判定。结构由 application.algorithms.image_ops.params 定义;
    # 这里只当不透明映射存, 读回时用 coerce_params() 收敛 (未知键忽略、越界夹紧)。
    image_ops: dict[str, Any] = field(default_factory=dict)

    def connection_summary(self) -> str:
        if self.protocol in LOCAL_PROTOCOLS:
            ip = str(getattr(self, "host", "") or "").strip()
            return f"本地 SDK（IP={ip}）" if ip else "本地 SDK（无网络参数）"
        if self.stream_url.strip() and is_placeholder(self.host):
            return self.stream_url
        if self.protocol in SERIAL_PROTOCOLS:
            port = getattr(self, "serial_port", "") or self.serial
            return port or "未配置串口"
        host, port = endpoint(self)
        return f"{host or '未配置IP'}:{port if port is not None else '-'}"

    def status_text(self) -> str:
        """**配置就绪度**（只看配置本身）。会话与最近探测由 DeviceStateRegistry 给出，
        页面展示应使用 registry 的分级状态（已连接/探测可达/...），不要直接用这个。"""
        if not self.enabled:
            return "未启用"
        if not self.confirmed:
            return "待核对"
        return "未连接"

    def clone(self) -> "CameraEntry":
        return CameraEntry(**asdict(self))


@dataclass
class DeviceConfig:
    """整份设备配置 (schema_version + 三类记录)。"""

    schema_version: int = CONFIG_SCHEMA_VERSION
    devices: list[DeviceEntry] = field(default_factory=list)
    links: list[LinkEntry] = field(default_factory=list)
    cameras: list[CameraEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "devices": [asdict(d) for d in self.devices],
            "links": [asdict(l) for l in self.links],
            "cameras": [asdict(c) for c in self.cameras],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DeviceConfig":
        if not isinstance(data, dict):
            raise ValueError("设备配置顶层必须是映射")
        version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"不支持的 schema_version={version!r}; 支持 {SUPPORTED_SCHEMA_VERSIONS}")
        return cls(
            schema_version=int(version),
            devices=[_entry(DeviceEntry, d) for d in (data.get("devices") or [])],
            links=[_entry(LinkEntry, d) for d in (data.get("links") or [])],
            cameras=[_entry(CameraEntry, d) for d in (data.get("cameras") or [])],
        )


def _entry(entry_cls, raw: dict):
    """按 dataclass 已知字段过滤构造记录 (忽略未知键, 兼容旧文件)。"""
    if not isinstance(raw, dict):
        raise ValueError(f"记录必须是映射: {raw!r}")
    known = {f for f in entry_cls.__dataclass_fields__}
    return entry_cls(**{k: v for k, v in raw.items() if k in known})


# ---------------------------------------------------------------- 默认种子
def default_config() -> DeviceConfig:
    """离线默认种子: 字段结构齐备, 连接值一律留空/未确认, 不代填现场值。"""
    boards = [
        DeviceEntry("stm32_01", "控制板1", "board", DeviceProtocol.SERIAL.value,
                    serial_port="COM_UNCONFIRMED", link_id="link0", address=1,
                    firmware_profile="offline-draft-v0"),
        DeviceEntry("stm32_02", "控制板2", "board", DeviceProtocol.SERIAL.value),
        DeviceEntry("stm32_03", "控制板3", "board", DeviceProtocol.SERIAL.value),
        DeviceEntry("stm32_04", "控制板4", "board", DeviceProtocol.SERIAL.value),
    ]
    others = [
        DeviceEntry("eco65_01", "ECO65", "robot", DeviceProtocol.MODBUS_TCP.value),
        DeviceEntry("ds86_01", "DS86", "camera", DeviceProtocol.JSON_TCP.value),
        DeviceEntry("s100_01", "S100", "edge", DeviceProtocol.ZMQ.value),
    ]
    links = [LinkEntry("link0", transport_kind=LinkTransportKind.SERIAL.value,
                       protocol=DeviceProtocol.SERIAL.value,
                       serial_port="COM_UNCONFIRMED", duplex="half")]
    cameras = [
        CameraEntry("ds86_01", "DS86 (随臂)", DeviceProtocol.JSON_TCP.value,
                    serial="UNCONFIRMED", channel_id="ds86_01"),
        CameraEntry("d405_01", "D405-1", DeviceProtocol.ZMQ.value),
        CameraEntry("d405_02", "D405-2", DeviceProtocol.ZMQ.value),
        CameraEntry("d405_03", "D405-3", DeviceProtocol.ZMQ.value),
        CameraEntry("d405_04", "D405-4", DeviceProtocol.ZMQ.value),
    ]
    return DeviceConfig(devices=boards + others, links=links, cameras=cameras)


# ---------------------------------------------------------------- YAML 读写
def _dump_yaml(data: dict) -> str:
    """块状 YAML, 保留字段顺序、不折行 —— 现场责任人需能直接读/改。"""
    yml = YAML(typ="safe")
    yml.default_flow_style = False
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.sort_base_mapping_type_on_output = False   # 保持 dataclass 字段顺序
    buf = StringIO()
    yml.dump(data, buf)
    return buf.getvalue()


def _load_yaml(text: str) -> dict:
    yml = YAML(typ="safe")
    return yml.load(text) or {}


class DeviceConfigStore:
    """设备配置的读写与增删改; 落盘走原子写 + ``.bak`` 备份。

    ``path=None`` 时为**内存模式**: 不改动磁盘 (供无绑定配置的演示/测试使用)。
    """

    def __init__(self, path: str | None = None, *, seed_if_missing: bool = True) -> None:
        self.path = os.path.abspath(path) if path else None
        self._seed_if_missing = seed_if_missing
        self._config: DeviceConfig | None = None

    # ---------- 读/写 ----------
    def load(self) -> DeviceConfig:
        """读取配置; 文件不存在时按种子创建并落盘 (seed_if_missing)。"""
        if self.path and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self._config = DeviceConfig.from_dict(_load_yaml(fh.read()))
        elif self._seed_if_missing:
            self._config = default_config()
            self.save()
        else:
            self._config = DeviceConfig()
        return self._config

    def save(self) -> None:
        """原子写 + ``.bak`` 备份; 内存模式为无操作。"""
        if self._config is None:
            raise RuntimeError("尚未 load(), 无可写配置")
        if self.path is None:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        header = ("# 设备与链路配置 (E21). 由 设备配置 页生成/维护。\n"
                  "# confirmed: false = 未现场核对(界面显示待核对/未启用); 真实现场值须由现场责任人填写并勾选。\n"
                  "# notes 记录每条取值的来源(旧工程文件/字段, 或现场核对时间)。\n")
        atomic_write(self.path, header + _dump_yaml(self._config.to_dict()), backup=True)

    def rollback(self) -> None:
        """回退到上一次原子写保留的 ``.bak``。"""
        if self.path is None:
            return
        rollback(self.path)
        self._config = None

    @classmethod
    def in_memory(cls, config: DeviceConfig | None = None) -> "DeviceConfigStore":
        """不落盘的内存 store (供无绑定配置的页面/演示使用)。"""
        store = cls(None, seed_if_missing=False)
        store._config = config or default_config()
        return store

    @property
    def config(self) -> DeviceConfig:
        if self._config is None:
            self.load()
        assert self._config is not None
        return self._config

    def reload(self) -> DeviceConfig:
        self._config = None
        return self.load()

    # ---------- 设备节点 CRUD ----------
    def add_device(self, entry: DeviceEntry) -> DeviceEntry:
        if self.find_device(entry.device_id) is not None:
            raise ValueError(f"设备 ID {entry.device_id!r} 已存在")
        self.config.devices.append(entry)
        return entry

    def update_device(self, device_id: str, entry: DeviceEntry) -> DeviceEntry:
        if entry.device_id != device_id and self.find_device(entry.device_id) is not None:
            raise ValueError(f"设备 ID {entry.device_id!r} 已被占用")
        for i, d in enumerate(self.config.devices):
            if d.device_id == device_id:
                self.config.devices[i] = entry
                return entry
        raise KeyError(f"未找到设备 {device_id!r}")

    def remove_device(self, device_id: str) -> None:
        self.config.devices = [d for d in self.config.devices if d.device_id != device_id]

    def find_device(self, device_id: str) -> DeviceEntry | None:
        return next((d for d in self.config.devices if d.device_id == device_id), None)

    # ---------- 链路 CRUD ----------
    def add_link(self, entry: LinkEntry) -> LinkEntry:
        if self.find_link(entry.link_id) is not None:
            raise ValueError(f"链路 ID {entry.link_id!r} 已存在")
        self.config.links.append(entry)
        return entry

    def update_link(self, link_id: str, entry: LinkEntry) -> LinkEntry:
        if entry.link_id != link_id and self.find_link(entry.link_id) is not None:
            raise ValueError(f"链路 ID {entry.link_id!r} 已被占用")
        for i, l in enumerate(self.config.links):
            if l.link_id == link_id:
                self.config.links[i] = entry
                return entry
        raise KeyError(f"未找到链路 {link_id!r}")

    def remove_link(self, link_id: str) -> None:
        self.config.links = [l for l in self.config.links if l.link_id != link_id]

    def find_link(self, link_id: str) -> LinkEntry | None:
        return next((l for l in self.config.links if l.link_id == link_id), None)

    # ---------- 相机通道 CRUD ----------
    def add_camera(self, entry: CameraEntry) -> CameraEntry:
        self._check_usb_binding(entry)
        if self.find_camera(entry.camera_id) is not None:
            raise ValueError(f"相机 ID {entry.camera_id!r} 已存在")
        self.config.cameras.append(entry)
        return entry

    def update_camera(self, camera_id: str, entry: CameraEntry) -> CameraEntry:
        self._check_usb_binding(entry, exclude=camera_id)
        if entry.camera_id != camera_id and self.find_camera(entry.camera_id) is not None:
            raise ValueError(f"相机 ID {entry.camera_id!r} 已被占用")
        for i, c in enumerate(self.config.cameras):
            if c.camera_id == camera_id:
                self.config.cameras[i] = entry
                return entry
        raise KeyError(f"未找到相机 {camera_id!r}")

    def _check_usb_binding(self, entry: CameraEntry, exclude: str = "") -> None:
        if entry.protocol != DeviceProtocol.REALSENSE_USB.value:
            return
        serial = entry.serial.strip()
        if entry.enabled and (not entry.confirmed or is_placeholder(serial)):
            raise ValueError("启用 D405 USB 前，请扫描选择序列号并勾选已现场核对")
        if serial and any(c.camera_id != exclude and c.serial.strip() == serial
                          for c in self.config.cameras):
            raise ValueError(f"序列号 {serial!r} 已绑定到其他相机")

    def remove_camera(self, camera_id: str) -> None:
        self.config.cameras = [c for c in self.config.cameras if c.camera_id != camera_id]

    def find_camera(self, camera_id: str) -> CameraEntry | None:
        return next((c for c in self.config.cameras if c.camera_id == camera_id), None)

    # ---------- 使用方 ----------
    def boards(self) -> list[DeviceEntry]:
        """绑定到链路的板节点 (kind=='board' 且声明了 link_id)。"""
        return [d for d in self.config.devices
                if d.kind == "board" and d.link_id]

    def binding_inputs(self) -> tuple[list[dict], list[dict], list[dict]]:
        """转成绑定校验所需的 links/boards/cameras 原始字典 (供 E16/A24 校验)。"""
        links = [{"link_id": l.link_id, "transport_kind": l.transport_kind}
                 for l in self.config.links]
        boards = [{"board_id": b.device_id, "link_id": b.link_id, "address": b.address}
                  for b in self.boards()]
        cameras = [{"camera_id": c.camera_id, "serial": c.serial}
                   for c in self.config.cameras]
        return links, boards, cameras

    def binding_report(self):
        """当前配置的绑定校验报告 (复用 services.devices.build_binding_report)。"""
        links, boards, cameras = self.binding_inputs()
        return build_binding_report(links=links, boards=boards, cameras=cameras)

    # ---------- 校验 ----------
    def validate(self) -> list[BindingIssue]:
        """绑定级校验: 重复 ID / 协议必填连接参数 / 边址唯一; 未确认项为非阻塞提示。"""
        issues: list[BindingIssue] = []
        issues += _validate_ids("设备", [d.device_id for d in self.config.devices])
        issues += _validate_ids("链路", [l.link_id for l in self.config.links])
        issues += _validate_ids("相机", [c.camera_id for c in self.config.cameras])
        for d in self.config.devices:
            issues += _validate_connection("设备", d.device_id, d)
        for l in self.config.links:
            issues += _validate_connection("链路", l.link_id, l)
        for c in self.config.cameras:
            issues += _validate_connection("相机", c.camera_id, c)
        issues += self._validate_board_binding()
        return issues

    def _validate_board_binding(self) -> list[BindingIssue]:
        issues: list[BindingIssue] = []
        link_ids = {l.link_id for l in self.config.links}
        seen: set[tuple[str, Any]] = set()
        for b in self.boards():
            if b.link_id not in link_ids:
                issues.append(BindingIssue(
                    "board.link", f"板 {b.device_id!r} 引用未知链路 {b.link_id!r}"))
                continue
            key = (b.link_id, b.address)
            if key in seen:
                issues.append(BindingIssue(
                    "board.addr", f"链路 {b.link_id!r} 上地址 {b.address!r} 重复绑定"))
            seen.add(key)
        return issues

    def unconfirmed_summary(self) -> str:
        """给 UI 的一行未确认提示 (不替代证据)。"""
        pending = [d.device_id for d in self.config.devices
                   if d.enabled and not d.confirmed]
        if not pending:
            return "无未确认项"
        return "未确认设备：" + "、".join(pending)


def _validate_ids(label: str, ids: list[str]) -> list[BindingIssue]:
    issues: list[BindingIssue] = []
    seen: set[str] = set()
    for i in ids:
        if not str(i).strip():
            issues.append(BindingIssue(f"{label}.id", f"{label}存在空 ID"))
            continue
        if i in seen:
            issues.append(BindingIssue(f"{label}.id", f"{label} ID {i!r} 重复"))
        seen.add(i)
    return issues


def _validate_connection(label: str, obj_id: str, obj) -> list[BindingIssue]:
    issues: list[BindingIssue] = []
    if not valid_protocol(obj.protocol):
        issues.append(BindingIssue(
            "protocol", f"{label} {obj_id!r} 协议非法: {obj.protocol!r}"))
        return issues
    if obj.protocol in LOCAL_PROTOCOLS:
        pass   # 本地 SDK (如 DS86): 无网络参数, 不做 host/port 要求
    elif obj.protocol in SERIAL_PROTOCOLS:
        # 相机记录不含串口字段, 用 getattr 兜底, 避免 "相机遇上 serial" 时抛异常。
        if is_placeholder(getattr(obj, "serial_port", "")):
            issues.append(BindingIssue(
                "conn.serial", f"{label} {obj_id!r} 未配置串口", blocking=False))
    else:
        host, port = endpoint(obj)
        if is_placeholder(host):
            issues.append(BindingIssue(
                "conn.host", f"{label} {obj_id!r} 未配置 IP", blocking=False))
        if port is None:
            issues.append(BindingIssue(
                "conn.port", f"{label} {obj_id!r} 未配置端口", blocking=False))
        elif not (1 <= int(port) <= 65535):
            issues.append(BindingIssue(
                "conn.port", f"{label} {obj_id!r} 端口越界: {port!r}"))
    if not obj.confirmed:
        issues.append(BindingIssue(
            "unconfirmed", f"{label} {obj_id!r} 未现场核对 (INPUT 待定)", blocking=False))
    return issues


__all__ = [
    "CONFIG_SCHEMA_VERSION", "DeviceConfig", "DeviceConfigStore", "DeviceEntry",
    "KIND_LABELS", "LOCAL_PROTOCOLS", "LinkEntry", "PROTOCOL_LABELS",
    "PLACEHOLDER_TOKENS", "SERIAL_PROTOCOLS", "SUPPORTED_SCHEMA_VERSIONS",
    "TCP_PROTOCOLS", "URL_PROTOCOLS", "default_config", "endpoint",
    "is_placeholder", "valid_protocol",
]
