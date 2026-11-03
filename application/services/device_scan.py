"""DS86 相机扫描（界面用）：照旧程序的"扫描 + 选择"取序列号（E21）。

**为什么有这一层**：旧工程 `vision_monitor` 的相机配置里**根本没有序列号**
（`config/camera_config.json` 里只有采集参数），序列号是运行期**扫出来让操作员挑**的：

    guidance_locate_tab._refresh_ds86_devices()
      devices = ds86_query_devices(scan_time_ms=2000)   # 2000 ms 扫描
      label   = f"{name} ({serial}) @ {ip}" + " [占用]"（status != 1）
      选中项 -> serial 写进 worker 配置

新工程照此：设备配置页的相机对话框里点「扫描设备」，列出在线相机，挑一台把序列号写进
「序列号」字段保存。**只读**：扫描不打开设备、不启动流（与"测试连接"同级的只读操作）。

本层把"扫描"从界面里摘出来：页面只调本函数，**不直接碰适配器**，也不 import 厂家包。
区分三种结果，不混为一谈：

- ``error`` 非空 —— **扫不了**（厂家包没挂上 / SDK 报错），与"现场没有设备"是两件事；
- ``devices`` 为空 —— 扫到了 0 台在线；
- 有设备但 ``serial`` 为空 —— 设备在线却**没上报序列号**，按 SN 打开这条路走不通。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from application.infrastructure import logging_setup

_log = logging_setup.get_logger("services.device_scan")

SCAN_MS_PICK = 2000          # 与旧 guidance_locate_tab 的扫描时长一致（见 scepter.SCAN_MS_PICK）
STATUS_CONNECTABLE = 1       # Scepter ScDeviceInfo.status == 1 表示可连接

SCAN_HELP = (
    "请确认：相机已上电、网线已接、**与本机网卡在同一网段/同一子网掩码**；"
    "被其它程序（旧工程、相机厂商工具）打开着的设备会显示为 [占用]。\n"
    "DS86 的设备发现是局域网广播：换过网段（如 192.168.3.x → 192.168.2.x）后，"
    "若相机仍是旧网段 IP，扫描就会得到 0 台 —— 给网卡加一个同网段地址，"
    "或用厂家 ScepterGUITool 把相机 IP 改到当前网段。\n"
    "扫描只读，不打开设备、不启动取流。"
)


def format_network_diagnosis(local_networks: list[tuple[str, str, str]],
                             configured_ip: str = "") -> str:
    """把本机网卡地址与相机配置 IP 拼成"为什么扫不到"的现场诊断文字。

    ``local_networks``: ``(网卡名, IPv4, 子网掩码)`` 列表。纯函数，便于离线测试。
    """
    import ipaddress

    lines: list[str] = []
    parsed: list[tuple[str, Any]] = []
    for name, ip, mask in local_networks:
        try:
            iface = ipaddress.ip_interface(f"{str(ip).strip()}/{str(mask).strip()}")
        except ValueError:
            continue
        if iface.ip.is_loopback or iface.ip.is_link_local:
            continue
        parsed.append((name, iface))
        lines.append(f"  · {name}: {iface.ip}/{iface.netmask}"
                     f"（网段 {iface.network}）")
    if lines:
        lines.insert(0, "本机网卡 IPv4：")
    else:
        lines.append("未能读取本机网卡 IPv4 地址（可在命令行执行 ipconfig 核对）。")

    configured = str(configured_ip or "").strip()
    if configured:
        try:
            cam_ip = ipaddress.ip_address(configured)
        except ValueError:
            cam_ip = None
            lines.append(f"相机配置 IP {configured!r} 不是合法地址。")
        if cam_ip is not None:
            same = [name for name, iface in parsed
                    if cam_ip in iface.network]
            if same:
                lines.append(f"相机配置 IP {cam_ip} 与网卡 {'、'.join(same)} 同网段。")
            else:
                lines.append(f"相机配置 IP {cam_ip} 与本机所有网卡都**不在同一网段**"
                             " —— 这是扫描 0 台最常见的原因。")
    else:
        lines.append("相机配置 IP 为空（可填上用于核对；不填也可先扫描）。")

    lines.append("DS86 的设备发现是局域网广播：相机与连接它的网卡必须同网段/同掩码。"
                 "换网段后若相机仍是旧网段 IP，扫描会得到 0 台：给该网卡加一个同网段"
                 "地址（Windows 例：netsh interface ipv4 add address \"以太网\" "
                 "192.168.3.100 255.255.255.0），或用 ScepterGUITool 把相机 IP "
                 "改到当前网段（也可启用 DHCP）。")
    return "\n".join(lines)


@dataclass(frozen=True)
class ScannedCamera:
    """一台在线 DS86（字段来自 SDK 的 ``ScDeviceInfo``）。"""

    name: str = ""
    serial: str = ""
    ip: str = ""
    status: int = 0

    @property
    def connectable(self) -> bool:
        return self.status == STATUS_CONNECTABLE

    @property
    def used(self) -> bool:
        """被占用/不可连（UI 标 ``[占用]``；**不隐藏**，旧程序也只是标注）。"""
        return not self.connectable

    @property
    def has_serial(self) -> bool:
        return bool(str(self.serial).strip())

    def label(self) -> str:
        """旧程序的列表文字：``名称 (序列号) @ IP [占用]``。"""
        text = self.name.strip() or "DS86"
        text += f" ({self.serial})" if self.has_serial else " (未上报序列号)"
        if self.ip.strip():
            text += f" @ {self.ip}"
        if self.used:
            text += " [占用]"
        return text


@dataclass(frozen=True)
class ScanOutcome:
    """一次扫描的结果（携带**诚实**的失败说明）。"""

    devices: tuple[ScannedCamera, ...] = ()
    error: str = ""                      # 非空 = 扫不了（不是"没有设备"）
    scan_ms: int = SCAN_MS_PICK
    detail: str = ""                     # SDK 层附加说明（如导入失败诊断）

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def with_serial(self) -> tuple[ScannedCamera, ...]:
        """有序列号、可按 SN 打开的那些（挑设备时用这批）。"""
        return tuple(d for d in self.devices if d.has_serial)


def _call_enumerate(device: Any, *, scan_ms: int, attempts: int) -> Any:
    """只传设备真正接受的参数（测试 fake 可能只接受 scan_ms）。"""
    import inspect

    fn = device.enumerate
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(scan_ms=scan_ms)
    kwargs: dict[str, Any] = {}
    if "scan_ms" in params:
        kwargs["scan_ms"] = scan_ms
    if "attempts" in params:
        kwargs["attempts"] = attempts
    return fn(**kwargs)


def scan_ds86_cameras(*, scan_ms: int = SCAN_MS_PICK, attempts: int = 1,
                      device_factory: Callable[[], Any] | None = None) -> ScanOutcome:
    """扫描在线 DS86 相机（**只读**）。

    ``device_factory`` 供离线测试注入假设备；为空时才去要真实厂家包 ——
    包没挂上时返回 ``error``（说清怎么解决），**不**返回"没有设备"。
    ``attempts`` > 1 时由设备层做多次扫描（界面"扫不到自动再试一次"用）。
    """
    if device_factory is None:
        try:
            from application.adapters.ds86.scepter import ScepterDs86Device
        except Exception as exc:  # noqa: BLE001
            return ScanOutcome(error=f"加载 DS86 适配器失败: {exc}", scan_ms=scan_ms)
        ok, why = ScepterDs86Device.available()
        if not ok:
            return ScanOutcome(
                error="DS86 厂家包不可用，无法扫描设备（这**不是**「现场没有相机」）。\n"
                      "扫描需要真实模式：`tower-ops.exe real`（或 main.py real）启动时会把"
                      "厂家包挂到 sys.path。",
                scan_ms=scan_ms, detail=why)
        device_factory = ScepterDs86Device
    try:
        device = device_factory()
        rows = _call_enumerate(device, scan_ms=scan_ms,
                               attempts=max(1, int(attempts)))
    except Exception as exc:  # noqa: BLE001 - 扫描失败要如实回, 不能伪装成"没设备"
        return ScanOutcome(error=f"扫描 DS86 失败: {type(exc).__name__}: {exc}",
                           scan_ms=scan_ms)
    cameras = tuple(
        ScannedCamera(name=str(row.get("name", "") or ""),
                      serial=str(row.get("serial", "") or ""),
                      ip=str(row.get("ip", "") or ""),
                      status=int(row.get("status", 0) or 0))
        for row in (rows or []) if isinstance(row, dict))
    _log.info("扫描 DS86 相机: 在线 %d 台 (扫描 %dms × %d)", len(cameras), scan_ms,
              max(1, int(attempts)))
    return ScanOutcome(devices=cameras, scan_ms=scan_ms)


__all__ = ["SCAN_HELP", "SCAN_MS_PICK", "STATUS_CONNECTABLE", "ScanOutcome",
           "ScannedCamera", "scan_ds86_cameras"]
