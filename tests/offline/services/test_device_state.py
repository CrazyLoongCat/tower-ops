"""设备运行期状态: 最近探测记录 + 会话开关 (E19/E21)。

覆盖: 状态优先级(装配事实 > 配置事实 > 会话事实 > 探测记录) / 会话真伪以适配器为准 /
装配期失败被标红且说明原因 / 无适配器给出明确原因 / 探测记录只存运行期。全部离线。
"""
from __future__ import annotations

from application.services.device_config import CameraEntry, DeviceEntry, LinkEntry
from application.services.device_probe import ProbeResult
from application.services.device_state import (
    STATUS_DISABLED,
    STATUS_ENABLED,
    TONE_DANGER,
    TONE_MUTED,
    TONE_OK,
    TONE_WARN,
    DeviceStateRegistry,
)


class _FakeAdapter:
    """最小适配器: 只有 status()['open'] / open() / close()。"""

    def __init__(self) -> None:
        self._open = False

    def open(self, cid: str) -> None:
        self._open = True

    def close(self, cid: str) -> None:
        self._open = False

    def status(self) -> dict:
        return {"camera_id": "cam", "open": self._open}


def _reachable() -> ProbeResult:
    return ProbeResult(True, "TCP 端口可达", is_real=True, latency_ms=3.0)


def test_only_two_statuses_disabled_or_enabled_by_probe():
    """DEC-086：只有 未启用/已启用；探针到达（含降级）即已启用。"""
    reg = DeviceStateRegistry()
    d = DeviceEntry("d1", enabled=False, confirmed=True)
    assert reg.status_text(d) == STATUS_DISABLED

    d2 = DeviceEntry("d2", enabled=True, confirmed=True)
    assert reg.status_text(d2) == STATUS_DISABLED      # 尚未探测 -> 未启用
    reg.record_probe("d2", _reachable())
    assert reg.status_text(d2) == STATUS_ENABLED
    assert reg.tone_of(d2) == TONE_OK
    reg.record_probe("d2", ProbeResult(False, "连接失败", is_real=True))
    assert reg.status_text(d2) == STATUS_DISABLED
    assert reg.tone_of(d2) == TONE_DANGER


def test_degraded_probe_counts_as_enabled():
    reg = DeviceStateRegistry()
    d = DeviceEntry("d1", enabled=True, confirmed=True)
    reg.record_probe("d1", ProbeResult(False, "未装 pyzmq", degraded=True, is_real=True))
    assert reg.status_text(d) == STATUS_ENABLED
    assert reg.tone_of(d) == TONE_OK


def test_local_sdk_probe_shows_not_applicable_not_failed():
    """本地 SDK 直连（DS86 相机 / 随臂机器人）: "无需测试"，中性色，不标红。

    2026-10-15 真机反馈：原来落到"探测失败"（danger 红），像设备坏了。
    """
    reg = DeviceStateRegistry()
    d = CameraEntry("ds86_01", protocol="sdk", enabled=True, confirmed=True)
    reg.record_probe("ds86_01", ProbeResult(
        False, "本地 SDK 直连，无网络参数，无需测试（不做连通性探测）",
        protocol="sdk", target="本地 SDK", not_applicable=True))
    assert reg.status_text(d) == STATUS_ENABLED
    assert reg.tone_of(d) == TONE_OK
    assert "无需测试" in reg.detail_of(d)


def test_local_sdk_session_still_outranks_not_applicable():
    """会话真开起来时仍然是"已连接"（无需测试不掩盖真实连接事实）。"""
    reg = DeviceStateRegistry(cameras={"ds86_01": _FakeAdapter()})
    d = CameraEntry("ds86_01", protocol="sdk", enabled=True, confirmed=True)
    reg.record_probe("ds86_01", ProbeResult(False, "无需测试", not_applicable=True))
    assert reg.status_text(d) == STATUS_ENABLED
    reg.open("ds86_01")
    assert reg.status_text(d) == STATUS_ENABLED


def test_session_overrides_probe():
    """会话打开 > 探测记录: 已连接才是绿。"""
    adapter = _FakeAdapter()
    reg = DeviceStateRegistry(cameras={"cam1": adapter})
    cam = CameraEntry("cam1", enabled=True, confirmed=True)
    reg.record_probe("cam1", _reachable())
    assert reg.status_text(cam) == STATUS_ENABLED
    reg.open("cam1")
    assert reg.is_open("cam1") is True
    assert reg.status_text(cam) == STATUS_ENABLED
    assert reg.tone_of(cam) == TONE_OK
    reg.close("cam1")
    assert reg.status_text(cam) == STATUS_ENABLED        # 回到探测记录


def test_open_without_adapter_raises_clear_reason():
    reg = DeviceStateRegistry(cameras={})
    d = DeviceEntry("board1", enabled=True, confirmed=True)
    assert reg.has_adapter("board1") is False
    assert reg.tone_of(d) == TONE_DANGER         # DEC-087：未启用=红
    try:
        reg.open("board1")
        raise AssertionError("无适配器不应静默成功")
    except RuntimeError as exc:
        assert "无适配器" in str(exc)


def test_session_state_comes_from_adapter_not_cache():
    """适配器自己关了会话, registry 必须立刻反映 (不缓存副本)。"""
    adapter = _FakeAdapter()
    reg = DeviceStateRegistry(cameras={"cam1": adapter})
    cam = CameraEntry("cam1", enabled=True, confirmed=True)
    reg.open("cam1")
    assert reg.status_text(cam) == STATUS_ENABLED
    adapter.close("cam1")                     # 适配器侧直接关闭
    assert reg.is_open("cam1") is False
    assert reg.status_text(cam) == STATUS_DISABLED


def test_probe_record_keeps_time_and_detail():
    reg = DeviceStateRegistry()
    reg.record_probe("x", _reachable())
    rec = reg.last_probe("x")
    assert rec is not None and rec.ok and rec.at_utc.endswith("+00:00")
    assert len(rec.clock) == 8                # HH:MM:SS
    assert "最近探测" in reg.detail_of(LinkEntry("x"))
    assert reg.last_probe("never") is None


# ---------------------------------------------------------------- 装配期失败
def test_startup_failure_outranks_session_and_probe():
    """装配期就失败的设备: 报"装配失败"/红, 且**优先于**会话与探测记录。

    理由: 装配失败是"这台根本没起来"的事实, 不该被一次探测可达或会话开着盖过去。
    """
    from application.bootstrap.production import StartupIssue
    adapter = _FakeAdapter()
    reg = DeviceStateRegistry(
        cameras={"d1": adapter, "cam1": adapter},
        startup_issues=[StartupIssue(
            "ECO65 机器人",
            "ProductionError: 机器人连接失败: 192.168.3.66:8080",
            keys=("d1", "cam1"))])
    d = DeviceEntry("d1", enabled=True, confirmed=True)
    reg.record_probe("d1", _reachable())
    reg.open("d1")
    assert reg.is_open("d1") is True                     # 会话确实开着
    assert reg.status_text(d) == STATUS_DISABLED         # 装配失败 -> 未启用
    assert reg.tone_of(d) == TONE_DANGER
    failure = reg.startup_failure("d1")
    assert failure and "机器人连接失败" in failure and "192.168.3.66:8080" in failure
    assert "装配失败" in reg.detail_of(d)
    assert set(reg.startup_failures) == {"d1", "cam1"}   # 同一次失败影响两个 key


def test_no_startup_issues_is_the_normal_case():
    reg = DeviceStateRegistry()
    assert reg.startup_failures == {}
    assert reg.startup_failure("anything") is None
    assert reg.status_text(DeviceEntry("d1", enabled=True, confirmed=True)) == STATUS_DISABLED


def test_startup_failure_map_is_duck_typed():
    """鸭子类型: 不 import bootstrap 的具体类型 (services 不反向依赖装配层)。"""
    class _Issue:
        device = "S100 相机服务"
        reason = "ProductionError: S100 端点未配置/未确认: 'UNCONFIRMED':5555"
        keys = ("c1", "c2")

    reg = DeviceStateRegistry(startup_issues=[_Issue()])
    assert set(reg.startup_failures) == {"c1", "c2"}
    assert "S100 端点" in reg.startup_failure("c1")
    assert reg.tone_of(CameraEntry("c2", protocol="zmq",
                                   enabled=True, confirmed=True)) == TONE_DANGER


# ---------------------------------------------------------------- 无适配器原因
def test_no_adapter_reason_says_what_is_actually_true():
    """报"无适配器"要说**本栈实情**, 不能拿离线假设去解释真机。"""
    # 离线默认: 说清离线只有 DS86 + 配了 channel_id 的 zmq 通道
    offline = DeviceStateRegistry(profile="offline")
    assert "无适配器" in offline.no_adapter_reason("d405_09")
    assert "channel_id" in offline.no_adapter_reason("d405_09")

    # 真实模式默认: 指向设备配置/channel_id，**不再**提"离线/待 E09"
    production = DeviceStateRegistry(profile="production")
    reason = production.no_adapter_reason("d405_01")
    assert "channel_id" in reason and "S100" in reason
    assert "离线" not in reason and "E09" not in reason

    # 装配方给的说明优先于 profile 默认 (如"没选 s100"——这不是失败)
    noted = DeviceStateRegistry(
        profile="production",
        session_note="本次未装配 S100 服务（设备配置里没有可用的 zmq 通道）")
    assert "没有可用的 zmq 通道" in noted.no_adapter_reason("d405_01")

    # 装配期失败 > 上面的默认/说明: 直接说失败原因
    from application.bootstrap.production import StartupIssue
    failed = DeviceStateRegistry(profile="production", startup_issues=[
        StartupIssue("S100 相机服务", "ProductionError: S100 端点未配置/未确认",
                     keys=("d405_01",))])
    assert "S100 端点未配置" in failed.no_adapter_reason("d405_01")


def test_open_and_reason_never_disagree():
    reg = DeviceStateRegistry(
        profile="production",
        session_note="本次未装配 S100 服务（设备配置里没有可用的 zmq 通道）")
    try:
        reg.open("d405_01")
        raise AssertionError("无适配器不应静默成功")
    except RuntimeError as exc:
        assert str(exc) == reg.no_adapter_reason("d405_01")


# ---------------------------------------------------------------- 相机配置热重载（DEC-081）
def test_rebind_cameras_swaps_adapters_and_clears_old_startup_failure():
    """设备配置保存后相机适配器重建：状态层换成新表，旧的装配失败红标不残留。"""
    from application.bootstrap.production import StartupIssue

    class _Cam:
        def __init__(self, is_open: bool) -> None:
            self.is_open = is_open

        def status(self):
            return {"open": self.is_open}

    old = _Cam(False)
    reg = DeviceStateRegistry(cameras={"ds86_01": old}, startup_issues=[
        StartupIssue("DS86 相机", "旧失败", keys=("ds86_01",))])
    assert reg.is_open("ds86_01") is False

    new = _Cam(True)
    reg.rebind_cameras({"ds86_01": new}, startup_issues=[])
    assert reg.adapter("ds86_01") is new
    assert reg.is_open("ds86_01") is True
    assert reg.startup_failure("ds86_01") is None      # 旧红标已清
