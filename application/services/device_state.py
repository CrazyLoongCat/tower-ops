"""设备运行期状态：最近探测记录 + 会话开关 (E19/E21)。

**为什么单独一层**：配置 (`devices.yaml`) 只存**基线值**，不代表任何"连接"事实。
"测试连接"是一次性的端口握手，"已连接"是**会话已真正打开**——两者不同，不能混为一谈：

    装配失败 / 未启用 / 待核对 / 未连接 / 探测可达 / 探测降级 / 探测失败 / 无需测试 / 已连接

"无需测试"针对**本地 SDK 直连**（DS86 相机、随臂机器人这类没有网络参数的设备）：
它既不是成功也不是失败，所以中性显示，不冒充"已连接"。把它当"探测失败"标红会误导
操作员去查网线（2026-10-15 真机反馈）。

"装配失败"来自生产栈启动时**单台设备装配失败但整栈照常起来**（见
:mod:`application.bootstrap.production`）：这类失败**不致命**，但不能静默——设备页必须
把该设备标出来并说明原因，否则操作员进了界面也不知道哪台没连上。

- **会话状态直接查适配器** ``status()["open"]``，本层不另存副本 —— 避免状态漂移；
- 探测记录只存**运行期内存**，不写回配置文件（配置基线不被运行状态污染）；
- 无适配器的设备调用 :meth:`open` 会抛出**明确原因**，不静默失败、不伪装成功。

安全边界：本层不自己发起任何真实连接。真实设备连接/动作仍受 D1 授权约束。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from application.infrastructure import logging_setup

_log = logging_setup.get_logger("services.device_state")

# 展示状态
STATUS_DISABLED = "未启用"
STATUS_ENABLED = "已启用"

# 语义色调 (由展示层映射到具体颜色, 本层不认识 THEME)
TONE_OK = "ok"
TONE_WARN = "warn"
TONE_DANGER = "danger"
TONE_MUTED = "muted"

STATUS_TONES: dict[str, str] = {
    STATUS_ENABLED: TONE_OK,
    STATUS_DISABLED: TONE_MUTED,
}

ALL_STATUSES = (STATUS_DISABLED, STATUS_ENABLED)



def startup_failure_map(startup_issues: Any) -> dict[str, str]:
    """把装配期的 ``StartupIssue`` 列表摊平成 ``key -> 原因``。

    **鸭子类型，不 import bootstrap** —— services 不反向依赖装配层。
    ``StartupIssue`` 只要提供 ``keys``（受影响的配置 ID）与 ``device``/``reason`` 文本即可。
    """
    out: dict[str, str] = {}
    for issue in (startup_issues or []):
        reason = str(getattr(issue, "reason", "") or "装配失败")
        label = str(getattr(issue, "device", "") or "")
        detail = f"{label}: {reason}" if label else reason
        for key in (getattr(issue, "keys", ()) or ()):
            out[str(key)] = detail
    return out


@dataclass(frozen=True)
class ProbeRecord:
    """最近一次探测结果 (运行期, 不落盘)。"""

    ok: bool
    degraded: bool
    detail: str
    at_utc: str
    latency_ms: float | None = None
    not_applicable: bool = False   # 本地 SDK 直连: "无需测试" (非失败)

    @property
    def clock(self) -> str:
        """便于放表格的 HH:MM:SS。"""
        try:
            return datetime.fromisoformat(self.at_utc).astimezone().strftime("%H:%M:%S")
        except Exception:  # noqa: BLE001
            return self.at_utc


class DeviceStateRegistry:
    """探测记录 + 会话开关；会话真伪以适配器为准。"""

    def __init__(self, cameras: dict[str, Any] | None = None,
                 startup_issues: Any = None, *,
                 profile: str | None = None, session_note: str = "") -> None:
        self._cameras: dict[str, Any] = dict(cameras or {})
        self._probes: dict[str, ProbeRecord] = {}
        # 装配期就失败的设备（不致命）: key -> 原因。优先级高于会话/探测状态。
        self._startup_failures: dict[str, str] = startup_failure_map(startup_issues)
        # 本栈是怎么装会话的: profile 与"本次没装哪个服务"的说明。
        # 用途单一但关键 —— 报"无适配器"时要说**这一栈的实情**，
        # 不能拿离线的假设去解释真机（那会把人往授权/网络方向带）。
        self._profile = profile
        self._session_note = str(session_note or "")

    # ---------- 装配期失败 ----------
    @property
    def startup_failures(self) -> dict[str, str]:
        """本栈装配时不成功的设备: ``key -> 原因``（只读副本）。"""
        return dict(self._startup_failures)

    def startup_failure(self, entry_or_key: Any) -> str | None:
        key = (entry_or_key if isinstance(entry_or_key, str)
               else self.key_of(entry_or_key))
        return self._startup_failures.get(key)

    # ---------- 适配器/会话 ----------
    def has_adapter(self, key: str) -> bool:
        return key in self._cameras

    def adapter(self, key: str) -> Any | None:
        return self._cameras.get(key)

    def rebind_cameras(self, cameras: dict[str, Any] | None,
                       startup_issues: Any = None) -> None:
        """DEC-081：设备配置保存后相机适配器已重建 —— 本层同步换表。

        会话状态本来就“直接查适配器”，换表后即反映新会话；``startup_issues`` 给出时
        连装配失败原因一起重算（保存配置后旧的红标不能留着）。
        """
        old_keys = set(self._cameras)
        self._cameras = dict(cameras or {})
        # 旧探测记录对新会话没有意义（换了 IP/端口后"探测可达"会误导）——清掉
        for key in old_keys | set(self._cameras):
            self._probes.pop(key, None)
        if startup_issues is not None:
            self._startup_failures = startup_failure_map(startup_issues)

    def is_open(self, key: str) -> bool:
        """直接问适配器，不缓存 —— 状态不会与真实会话漂移。"""
        adapter = self._cameras.get(key)
        status = getattr(adapter, "status", None)
        if not callable(status):
            return False
        try:
            return bool((status() or {}).get("open"))
        except Exception:  # noqa: BLE001 - 适配器异常不应让 UI 崩
            return False

    def open(self, key: str) -> None:
        """打开会话。无适配器时抛出**明确原因**（不伪装成功）。"""
        adapter = self._cameras.get(key)
        if adapter is None:
            _log.warning("连接失败(无适配器) %s: %s", key, self.no_adapter_reason(key),
                         extra={"device_id": key})
            raise RuntimeError(self.no_adapter_reason(key))
        try:
            adapter.open(key)
        except Exception as exc:  # noqa: BLE001 - 记录后原样抛出，交给界面提示
            _log.error("连接失败 %s: %s", key, exc, extra={"device_id": key})
            raise
        _log.info("已连接 %s", key, extra={"device_id": key})

    def no_adapter_reason(self, key: str) -> str:
        """这个 key **为什么**没有会话适配器 —— 按本栈实情说。

        曾经的文案写死"离线仅 DS86 有适配器…真实连接需现场授权 / 待 E09"，
        在 real 模式下会把人往授权/网络方向带；真实原因往往是**本次压根没选那台设备**
        （如设备配置里没有可用的 zmq 通道）——"没配"不是失败，控制台也不会报，
        所以这句话必须自己说清。
        """
        if key in self._startup_failures:
            why = f"该设备本次装配失败：{self._startup_failures[key]}"
        elif self._session_note:
            why = self._session_note
        elif self._profile == "production":
            why = ("本次启动没有为该通道装配会话：确认设备配置里已启用该通道、"
                   "zmq 通道填了 channel_id，且对应 S100 服务可用")
        else:
            why = ("离线会话只有 DS86，以及设备配置里填了 channel_id 的 zmq 通道；"
                   "本通道不在其中（不是连接失败）")
        return f"{key!r} 无适配器，无法建立会话：{why}"

    def close(self, key: str) -> None:
        adapter = self._cameras.get(key)
        if adapter is None:
            raise RuntimeError(f"{key!r} 无适配器，无可关闭的会话")
        adapter.close(key)
        _log.info("已断开 %s", key, extra={"device_id": key})

    # ---------- 探测记录 ----------
    def record_probe(self, key: str, result: Any) -> ProbeRecord:
        rec = ProbeRecord(
            ok=bool(getattr(result, "ok", False)),
            degraded=bool(getattr(result, "degraded", False)),
            detail=str(getattr(result, "detail", "")),
            at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            latency_ms=getattr(result, "latency_ms", None),
            not_applicable=bool(getattr(result, "not_applicable", False)),
        )
        self._probes[key] = rec
        extra = {"device_id": key}
        if rec.not_applicable:
            _log.info("探测 %s: 无需测试 (%s)", key, rec.detail, extra=extra)
        elif rec.ok:
            _log.info("探测 %s: 可达%s", key, f" ({rec.detail})" if rec.detail else "",
                      extra=extra)
        else:
            level = _log.warning if rec.degraded else _log.error
            level("探测 %s: %s%s", key, "降级" if rec.degraded else "失败",
                  f" ({rec.detail})" if rec.detail else "", extra=extra)
        return rec

    def last_probe(self, key: str) -> ProbeRecord | None:
        return self._probes.get(key)

    # ---------- 展示状态 ----------
    def status_text(self, entry: Any) -> str:
        """只有两个状态：未启用 / 已启用（DEC-086）。

        已启用 = 配置启用且（会话已打开 / 探针到达 / 本地 SDK 无需探针）；
        未启用 = 配置未启用、装配失败、尚未探测或探测失败。
        详细原因仍在 ``detail_of()`` 的悬停提示里，不丢诊断信息。
        """
        key = self.key_of(entry)
        if not getattr(entry, "enabled", False):
            return STATUS_DISABLED
        if key in self._startup_failures:
            return STATUS_DISABLED
        if self.is_open(key):
            return STATUS_ENABLED
        rec = self._probes.get(key)
        if rec is not None and (rec.not_applicable or rec.ok or rec.degraded):
            return STATUS_ENABLED
        return STATUS_DISABLED

    def tone_of(self, entry: Any) -> str:
        """DEC-087：展示色只有两档——已启用=绿，未启用=红。"""
        return (TONE_OK if self.status_text(entry) == STATUS_ENABLED
                else TONE_DANGER)

    def detail_of(self, entry: Any) -> str:
        """状态补充说明 (给悬停/提示用): 装配原因、会话实况、最近探测时间或适配器情况。"""
        key = self.key_of(entry)
        if key in self._startup_failures:
            return f"装配失败：{self._startup_failures[key]}"
        if self.is_open(key):
            return "会话已打开"
        rec = self._probes.get(key)
        if rec is not None:
            if rec.not_applicable:
                return f"最近检查 {rec.clock}：本地 SDK 直连，无网络参数，无需测试"
            return f"最近探测 {rec.clock}：{rec.detail}"
        if self.has_adapter(key):
            return "有适配器：可建立会话（尚未连接）"
        return "无适配器：只能探测，不能建立会话"

    @staticmethod
    def key_of(entry: Any) -> str:
        """记录键：设备用 device_id，相机用 camera_id，链路用 link_id。

        同一台相机在设备表与相机表里 ID 相同 => 共享同一份会话状态 (同一台物理设备)。
        """
        for attr in ("device_id", "camera_id", "link_id"):
            value = getattr(entry, attr, None)
            if value:
                return str(value)
        return "?"


__all__ = [
    "ALL_STATUSES", "DeviceStateRegistry", "ProbeRecord", "STATUS_DISABLED",
    "STATUS_ENABLED", "STATUS_TONES", "TONE_DANGER", "TONE_MUTED", "TONE_OK",
    "TONE_WARN", "startup_failure_map",
]
