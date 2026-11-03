"""E07 边缘 CameraRegistry / 通道 Actor / 帧缓存（离线）。

覆盖：按序列号绑定与拒绝重复 / 设备缺失只影响本通道 / 逐通道独立启停与 epoch /
插拔不换逻辑 ID / 帧缓存有界 + 复制持有 + 只读 / 导入期不开设备。
"""
from __future__ import annotations

import numpy as np
import pytest

from edge_service.camera_actor import STATE_FAILED, STATE_OK, STATE_STOPPED
from edge_service.camera_registry import (
    BindError,
    CameraRegistry,
    ChannelConfig,
    load_edge_config,
)
from edge_service.frame_store import FrameStore

CONFIGS = [
    ChannelConfig("cam1", "SN-A", width=64, height=48),
    ChannelConfig("cam2", "SN-B", width=64, height=48),
]


def _registry(**kw) -> CameraRegistry:
    return CameraRegistry(CONFIGS, **kw)


# ---------------------------------------------------------------- 绑定
def test_duplicate_serial_rejected():
    dupes = [ChannelConfig("cam1", "SN-A"), ChannelConfig("cam2", "SN-A")]
    with pytest.raises(BindError) as ei:
        CameraRegistry(dupes)
    assert "重复" in str(ei.value)


def test_blank_channel_id_rejected():
    with pytest.raises(BindError):
        ChannelConfig("   ")


def test_construction_does_not_open_devices():
    """导入/构造不得打开设备：未 start 时状态应为 STOPPED。"""
    reg = _registry()
    assert {h["state"] for h in reg.health().values()} == {STATE_STOPPED}


# ---------------------------------------------------------------- 启停
def test_start_all_marks_ok_and_grab_increments_sequence():
    reg = _registry()
    health = reg.start_all()
    assert {h["state"] for h in health.values()} == {STATE_OK}
    actor = reg.actor("cam1")
    f1 = actor.grab()
    f2 = actor.grab()
    assert (f1.sequence, f2.sequence) == (1, 2)
    assert f1.channel == "cam1" and f1.depth.shape == (48, 64)
    assert f1.depth_scale_m == 0.001 and f1.intrinsics is not None


def test_restart_bumps_epoch_and_clears_channel_frames():
    reg = _registry()
    reg.start_all()
    reg.actor("cam1").grab()
    before = reg.actor("cam1").health().epoch
    health = reg.restart("cam1")
    assert health["epoch"] == before + 1
    assert reg.actor("cam1").store.latest("cam1") is None      # 旧 epoch 帧已清
    assert reg.actor("cam1").health().sequence == 0


def test_single_channel_failure_does_not_mark_all_failed():
    """cam2 序列号不在枚举结果中 -> 只有 cam2 失败。"""
    reg = _registry(enumerated=["SN-A"])
    health = reg.start_all()
    assert health["cam1"]["state"] == STATE_OK
    assert health["cam2"]["state"] == STATE_FAILED
    assert "设备缺失" in health["cam2"]["last_error"]


def test_blank_serial_channel_fails_only_itself():
    reg = CameraRegistry([ChannelConfig("cam1", "SN-A", width=32, height=32),
                          ChannelConfig("cam2", "", width=32, height=32)])
    health = reg.start_all()
    assert health["cam1"]["state"] == STATE_OK
    assert health["cam2"]["state"] == STATE_FAILED


def test_grab_failure_stays_local():
    reg = _registry()
    reg.start_all()
    reg.actor("cam2")._source.opened = False             # noqa: SLF001 - 模拟通道掉线
    with pytest.raises(RuntimeError):
        reg.actor("cam2").grab()
    assert reg.actor("cam2").health().state == STATE_FAILED
    assert reg.actor("cam1").health().state == STATE_OK   # 不受影响


# ---------------------------------------------------------------- 身份
def test_identity_follows_serial_not_enumeration_order():
    """枚举顺序变化不得互换 cam1/cam2 —— 身份由序列号决定。"""
    reg = _registry(enumerated=["SN-B", "SN-A"])          # 顺序刻意颠倒
    reg.start_all()
    assert reg.serial_of("cam1") == "SN-A"
    assert reg.serial_of("cam2") == "SN-B"
    assert reg.actor("cam1").health_dict()["serial"] == "SN-A"


def test_rebind_marks_disappeared_and_restarts_reappeared():
    reg = _registry(enumerated=["SN-A", "SN-B"])
    reg.start_all()
    epoch_before = reg.actor("cam2").health().epoch
    reg.rebind(["SN-A"])                                  # B 被拔掉
    assert reg.actor("cam2").health().state == STATE_FAILED
    reg.rebind(["SN-A", "SN-B"])                          # B 插回
    assert reg.actor("cam2").health().state == STATE_OK
    assert reg.actor("cam2").health().epoch == epoch_before + 1   # 换 epoch 重建


# ---------------------------------------------------------------- 帧缓存
def test_frame_store_copies_and_freezes_arrays():
    store = FrameStore(capacity_per_channel=2)
    src = np.full((4, 4), 7, dtype="uint16")
    stored = store.put(channel="c", sequence=1, epoch=0, depth=src,
                       depth_scale_m=0.002)
    src[:] = 999                                          # 改源数组
    assert int(stored.depth[0, 0]) == 7                   # 已复制, 不受影响
    assert stored.depth.flags.writeable is False          # 只读, 防下游改写
    with pytest.raises(ValueError):
        stored.depth[0, 0] = 1
    assert stored.depth_scale_m == 0.002                  # 保留原始深度单位


def test_frame_store_is_bounded():
    store = FrameStore(capacity_per_channel=2)
    for i in range(5):
        store.put(channel="c", sequence=i, epoch=0, depth=np.zeros((2, 2), "uint16"))
    assert len(store.history("c")) == 2
    assert store.latest("c").sequence == 4
    assert store.stats()["channels"] == {"c": 2}


# ---------------------------------------------------------------- 配置
def test_shipped_edge_config_loads_with_unbound_serials():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    service, channels = load_edge_config(str(root / "configs" / "edge" / "cameras.yaml"))
    assert [c.channel_id for c in channels] == ["cam1", "cam2"]
    assert service.get("port") == 5555
    # 现场值未确认: 序列号必须留空, 不得代填
    assert all(c.serial == "" for c in channels)
