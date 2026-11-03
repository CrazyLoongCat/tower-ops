"""真实连通性探测 (E21, 03 §9)。

**只测连通性, 不下发任何动作指令**:
- serial  : 尝试打开串口并立即关闭 (不发字节);
- modbus_tcp / json_tcp : 仅完成 TCP 三次握手后关闭 (不发任何帧);
- zmq     : 发一条**只读** ``{"cmd": "ping"}`` 并等应答 (参照旧 s100_d405_service);
- sdk     : **本地 SDK 直连不探测** —— 结果标 ``not_applicable``（"无需测试"），
            既不报成功也不报失败（原来是 ``ok=False`` → UI 显示"不可达"，属误报）。

安全边界 (与项目离线边界一致):
- 探测**只由用户显式点击触发**, 不自动、不定时、不随装配触发;
- 占位/未确认的地址 (空, 含 UNCONFIRMED/"待核对") 直接拒绝, 不打开 socket ——
  避免把"未配置"伪装成"连接失败";
- 短超时 (默认 2s), 依赖缺失 (pyserial/pyzmq) 时优雅降级并如实说明;
- :class:`ProbeResult` 记录 :attr:`sent_bytes` 与 :attr:`is_real`, 便于审计与自证。
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any

from application.contracts.enums import DeviceProtocol
from application.services.device_config import (
    LOCAL_PROTOCOLS,
    SERIAL_PROTOCOLS,
    TCP_PROTOCOLS,
    endpoint,
    is_placeholder,
)
from application.infrastructure import logging_setup

_log = logging_setup.get_logger("services.device_probe")

DEFAULT_TIMEOUT = 2.0

PROBE_SAFETY_NOTE = (
    "连通性探测只做 TCP 握手/串口打开/ZMQ ping, 不发送任何动作指令帧; "
    "真实设备动作与运动通道仍需 D1-TEST 授权。")

# 缺 pyzmq 时的可执行修复命令 (in-程序 与 文档 共用同一句)。
ZMQ_INSTALL_HINT = "python -m pip install pyzmq"

_PARITY_MAP = {"N": "PARITY_NONE", "E": "PARITY_EVEN", "O": "PARITY_ODD",
               "M": "PARITY_MARK", "S": "PARITY_SPACE"}


@dataclass
class ProbeResult:
    """一次探测的结果。``ok`` 为 True 仅表示"端口/服务可达", 不代表设备功能正常。

    ``not_applicable`` 用于**本地 SDK 直连**（如 DS86 相机、随臂机器人）：这类设备
    没有网络参数, 本来就没有"连通性"可测 —— 它既不是成功也不是失败, 所以单独标一档,
    免得 UI 把"无需测试"渲染成"不可达"（那会把操作员往网线/授权方向带）。
    """

    ok: bool
    detail: str
    protocol: str = ""
    target: str = ""
    latency_ms: float | None = None
    sent_bytes: int = 0
    is_real: bool = False          # 是否真的打开过网络/串口 (未确认地址=false)
    degraded: bool = False         # 降级探测: 只证明到得了端口, 未验证该协议 (如缺 pyzmq)
    not_applicable: bool = False   # 本地 SDK 直连: 无网络参数, 无需测试 (非失败)

    @property
    def summary(self) -> str:
        if self.not_applicable:
            return f"无需测试：{self.detail}"
        state = "可达" if self.ok else ("部分可达(降级)" if self.degraded else "不可达")
        latency = f"（{self.latency_ms:.0f} ms）" if self.latency_ms is not None else ""
        return f"{state}{latency}：{self.detail}"


def _target_text(entry: Any) -> str:
    if getattr(entry, "protocol", "") in SERIAL_PROTOCOLS:
        return str(getattr(entry, "serial_port", "") or "未配置串口")
    if getattr(entry, "protocol", "") in LOCAL_PROTOCOLS:
        return "本地 SDK"
    host, port = endpoint(entry)
    return f"{host or '未配置IP'}:{port if port is not None else '-'}"


def probe(entry: Any, *, timeout: float = DEFAULT_TIMEOUT,
          audit: Any = None) -> ProbeResult:
    """按 ``entry.protocol`` 做真实探测。``entry`` 为带协议/连接字段的记录。"""
    protocol = getattr(entry, "protocol", "")
    target = _target_text(entry)
    result = _dispatch(entry, protocol, target, timeout)
    if getattr(result, "not_applicable", False):
        _log.info("探测 %s(%s): 无需测试", target, protocol)
    elif getattr(result, "ok", False):
        _log.info("探测 %s(%s): 可达%s", target, protocol,
                  f" ({result.detail})" if getattr(result, "detail", "") else "")
    else:
        _log.warning("探测 %s(%s): 不可达 (%s)", target, protocol,
                     getattr(result, "detail", ""))
    if audit is not None:
        try:
            audit(result)
        except Exception:  # noqa: BLE001 - 审计失败不影响探测本身
            pass
    return result


def _dispatch(entry: Any, protocol: str, target: str, timeout: float) -> ProbeResult:
    if protocol in SERIAL_PROTOCOLS:
        return probe_serial(getattr(entry, "serial_port", ""),
                            baudrate=int(getattr(entry, "baudrate", 115200) or 115200),
                            data_bits=int(getattr(entry, "data_bits", 8) or 8),
                            parity=str(getattr(entry, "parity", "N") or "N"),
                            stop_bits=int(getattr(entry, "stop_bits", 1) or 1),
                            timeout=timeout)
    if protocol in LOCAL_PROTOCOLS:
        # 本地 SDK 直连: 没有网络参数可测 —— 标 not_applicable 而不是 ok=False,
        # 否则 UI 会把它读成"不可达"（真机上 ECO65/DS86 就是这么被误报的）。
        return ProbeResult(False, "本地 SDK 直连，无网络参数，无需测试（不做连通性探测）",
                           protocol=protocol, target=target, not_applicable=True)
    if protocol == DeviceProtocol.ZMQ.value:
        host, port, guard = _tcp_params(entry, protocol, target)
        if guard is not None:
            return guard
        return probe_zmq(host, port, timeout=timeout)
    if protocol in TCP_PROTOCOLS:
        # modbus_tcp / json_tcp / rtsp / http / vendor_tcp: 只做 TCP 握手。
        host, port, guard = _tcp_params(entry, protocol, target)
        if guard is not None:
            return guard
        return probe_tcp(host, port, timeout=timeout, protocol=protocol)
    return ProbeResult(False, f"未知协议 {protocol!r}，无法探测",
                       protocol=protocol, target=target)


def _tcp_params(entry: Any, protocol: str, target: str):
    """取 host/port (rtsp/http 可由 stream_url 解析) 并做占位/越界检查。"""
    host, port = endpoint(entry)
    if is_placeholder(host):
        return None, None, ProbeResult(
            False, "地址未配置或未确认（不发起连接）",
            protocol=protocol, target=target)
    if port is None or is_placeholder(port):
        return None, None, ProbeResult(
            False, "端口未配置（不发起连接）",
            protocol=protocol, target=target)
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None, None, ProbeResult(
            False, f"端口非法: {port!r}", protocol=protocol, target=target)
    if not (1 <= port <= 65535):
        return None, None, ProbeResult(
            False, f"端口越界: {port}", protocol=protocol, target=target)
    return host, port, None


def probe_tcp(host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT,
              protocol: str = DeviceProtocol.JSON_TCP.value) -> ProbeResult:
    """仅 TCP 握手 (connect 成功后立即关闭), 不发送任何字节。"""
    target = f"{host}:{port}"
    started = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
    except OSError as exc:
        return ProbeResult(False, f"TCP 连接失败: {exc}", protocol=protocol,
                           target=target, is_real=True)
    latency = (time.perf_counter() - started) * 1000.0
    return ProbeResult(True, "TCP 端口可达（未发送任何帧）", protocol=protocol,
                       target=target, latency_ms=latency, is_real=True)


def probe_serial(port: str, *, baudrate: int = 115200, data_bits: int = 8,
                 parity: str = "N", stop_bits: int = 1,
                 timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """尝试打开串口并立即关闭 (不发字节)。"""
    if is_placeholder(port):
        return ProbeResult(False, "串口未配置或未确认（不打开串口）",
                           protocol=DeviceProtocol.SERIAL.value, target=str(port))
    try:  # pyserial 列入主依赖, 仍惰性导入以支持无外设环境
        import serial  # type: ignore
    except ImportError:
        return ProbeResult(False, "未安装 pyserial，无法探测串口",
                           protocol=DeviceProtocol.SERIAL.value, target=port)
    try:
        ser = serial.Serial(
            port=port,
            baudrate=int(baudrate),
            bytesize=getattr(serial, f"{_bytesize_name(data_bits)}", 8),
            parity=getattr(serial, _PARITY_MAP.get(str(parity).upper(), "PARITY_NONE"),
                           getattr(serial, "PARITY_NONE", "N")),
            stopbits=getattr(serial, f"{_stopbits_name(stop_bits)}", 1),
            timeout=min(timeout, 1.0),
        )
    except Exception as exc:  # noqa: BLE001 - pyserial 抛出的异常类型繁多
        return ProbeResult(False, f"打开串口失败: {exc}",
                           protocol=DeviceProtocol.SERIAL.value, target=port,
                           is_real=True)
    try:
        ser.close()
    except Exception:  # noqa: BLE001
        pass
    return ProbeResult(True, "串口可打开（未发送任何字节）",
                       protocol=DeviceProtocol.SERIAL.value, target=port,
                       is_real=True)


def probe_zmq(host: str, port: int, *, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    """发一条只读 ping 并等应答 (REQ/REP)。"""
    target = f"{host}:{port}"
    if is_placeholder(host) or port is None:
        return ProbeResult(False, "地址/端口未配置或未确认（不发起连接）",
                           protocol=DeviceProtocol.ZMQ.value, target=target)
    try:
        import zmq  # type: ignore
    except ImportError:
        return _zmq_without_dependency(host, port, timeout)
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    ms = int(timeout * 1000)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, ms)
    sock.setsockopt(zmq.SNDTIMEO, ms)
    # 只读 ping 载荷; 显式构造字节以便如实记录发送长度 (审计自证)。
    payload = json.dumps({"cmd": "ping"}, separators=(",", ":")).encode("utf-8")
    started = time.perf_counter()
    reply: Any = None
    try:
        sock.connect(f"tcp://{host}:{port}")
        sock.send(payload)
        reply = sock.recv_json()
    except Exception as exc:  # noqa: BLE001 - zmq.Again 超时等
        return ProbeResult(False, f"ZMQ ping 失败: {exc}",
                           protocol=DeviceProtocol.ZMQ.value, target=target,
                           sent_bytes=len(payload), is_real=True)
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
    latency = (time.perf_counter() - started) * 1000.0
    detail = "ZMQ ping 有应答"
    # DEC-083：ping 只证明“服务进程在”，不代表它能抓帧；老服务回包带 info.started/cams，
    # 直接摆出来，免得把“端口通”误当成“相机可用”。
    if isinstance(reply, dict):
        info = reply.get("info") if isinstance(reply.get("info"), dict) else {}
        notes: list[str] = []
        if "started" in info:
            notes.append(f"服务启动={bool(info.get('started'))}")
        cams = info.get("cams")
        if isinstance(cams, dict) and cams:
            shown: list[str] = []
            for name, meta in cams.items():
                serial = ""
                if isinstance(meta, dict):
                    serial = str(meta.get("serial") or "").strip()
                shown.append(f"{name}={serial or '?'}")
            notes.append("通道=" + "、".join(shown))
        if notes:
            detail += "（" + "；".join(notes) + "）"
        if info.get("started") is False:
            detail += "；服务未启动，抓拍一定失败"
    return ProbeResult(True, detail, protocol=DeviceProtocol.ZMQ.value,
                       target=target, latency_ms=latency, sent_bytes=len(payload),
                       is_real=True)


def _zmq_without_dependency(host: str, port: int, timeout: float) -> ProbeResult:
    """缺 pyzmq 时的降级: 只证明"端口到得了", 绝不冒充"ZMQ 服务正常"。

    缺依赖是**环境问题**, 不是设备问题 —— 所以照常做一次真实 TCP 握手拿到有用信息,
    并给出可执行的修复命令; 结果标 ``degraded`` 以便 UI 用警示色而不是失败红。
    """
    tcp = probe_tcp(host, port, timeout=timeout,
                    protocol=DeviceProtocol.ZMQ.value)
    hint = f"安装: {ZMQ_INSTALL_HINT}"
    if tcp.ok:
        return ProbeResult(
            False,
            f"TCP 端口可达，但未安装 pyzmq，未能验证 ZMQ 应答（{hint}）",
            protocol=DeviceProtocol.ZMQ.value, target=tcp.target,
            latency_ms=tcp.latency_ms, is_real=True, degraded=True)
    return ProbeResult(
        False,
        f"未安装 pyzmq，且 TCP 端口也不可达: {tcp.detail}（{hint}）",
        protocol=DeviceProtocol.ZMQ.value, target=tcp.target,
        is_real=True, degraded=True)


def _bytesize_name(data_bits: int) -> str:
    return {5: "FIVEBITS", 6: "SIXBITS", 7: "SEVENBITS", 8: "EIGHTBITS"}.get(
        int(data_bits), "EIGHTBITS")


def _stopbits_name(stop_bits: int) -> str:
    return {1: "STOPBITS_ONE", 2: "STOPBITS_TWO"}.get(int(stop_bits), "STOPBITS_ONE")


__all__ = [
    "DEFAULT_TIMEOUT", "PROBE_SAFETY_NOTE", "ProbeResult", "ZMQ_INSTALL_HINT",
    "probe", "probe_serial", "probe_tcp", "probe_zmq",
]
