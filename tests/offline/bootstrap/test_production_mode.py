"""生产模式（离线；**不接触任何真实设备**）。

覆盖：装配出的栈不含 Fake 组件 / 缺厂家 SDK·占位地址·连接失败时**单台失败不致命**
（记进 startup_issues、界面照常起、该设备标红）且**不回退 Fake** /
占位示教点拒绝运动 / 圆宿主未配置时拒绝而非给假结果。DEC-085：real 固定连接全部设备，
无环境变量闸、总是允许运动。
"""
from __future__ import annotations

import pytest

from application.bootstrap.authorization import (
    GatedRobot,
    MotionBlocked,
    is_confirmed_waypoint,
    require_confirmed_target,
)
from application.bootstrap.production import (
    NoRobot,
    ProductionError,
    ProductionRequest,
    UnavailableCircleBackend,
    _s100_groups,
    build_production_stack,
    load_confirmed_targets,
)
from application.contracts.targets import JointTarget
from application.services.device_config import (
    CameraEntry,
    DeviceConfig,
    DeviceConfigStore,
    DeviceEntry,
)
from application.services.device_state import STATUS_DISABLED, TONE_DANGER

S100_HOST = "10.20.30.40"
S100_PORT = 5555


def _cfg_path(tmp_path, *, s100_host=S100_HOST, robot_host="10.20.30.41"):
    """写一份**测试自造**的设备配置（不碰仓库里用户那份）。"""
    path = tmp_path / "devices.yaml"
    store = DeviceConfigStore(str(path), seed_if_missing=False)
    store._config = DeviceConfig(                      # noqa: SLF001 - 测试构造
        devices=[
            DeviceEntry("s100_01", "S100", "edge", "zmq", enabled=True,
                        confirmed=True, host=s100_host, port=S100_PORT),
            DeviceEntry("eco65_01", "ECO65", "robot", "vendor_tcp", enabled=True,
                        confirmed=True, host=robot_host, port=8080),
        ],
        links=[],
        cameras=[
            CameraEntry("d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
                        host=s100_host, port=S100_PORT, channel_id="cam1"),
            CameraEntry("d405_02", "D405-2", "zmq", enabled=True, confirmed=True,
                        host=s100_host, port=S100_PORT, channel_id="cam2"),
        ])
    store.save()
    return str(path)


def _req(tmp_path, **kw):
    kw.setdefault("recipe_path", "")
    return ProductionRequest(
        db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
        device_config_path=_cfg_path(tmp_path), **kw)


def _issue_for(stack, key):
    """取出影响某个配置 ID 的装配失败记录（没有则返回 None）。"""
    return next((i for i in (stack.startup_issues or []) if key in i.keys), None)


def _camera_entry(camera_id, protocol="local_sdk"):
    """造一个"启用且已核对"的相机条目，用来问界面会怎么标它。"""
    return CameraEntry(camera_id, camera_id, protocol, enabled=True, confirmed=True)


# ---------------------------------------------------------------- 装配
def test_s100_only_stack_builds_without_touching_hardware(tmp_path, monkeypatch):
    """S100 客户端是惰性连接的：装配不打开 socket。"""
    stack = build_production_stack(_req(tmp_path))
    try:
        assert stack.profile == "production"
        assert sorted(stack.camera_registry.camera_ids()) == ["d405_01", "d405_02"]
        # 未选机器人 -> NoRobot，不是 FakeRobot
        assert isinstance(stack.robot, NoRobot)
        assert type(stack.robot).__module__ == NoRobot.__module__
        # 未选 DS86 -> 不在相机表里（宁缺勿假）
        assert "ds86_01" not in stack.cameras
        # 未开运动闸 -> 机器人对任何运动都拒绝
        with pytest.raises(ProductionError):
            stack.robot.move_joint(None)
    finally:
        stack.close()


def test_ds86_without_vendor_sdk_is_reported_not_fatal(tmp_path, monkeypatch):
    """缺厂家 SDK：**不拖垮整栈**，但必须记进 startup_issues、且**不得**回退到 FakeCamera。"""
    stack = build_production_stack(_req(tmp_path))
    try:
        assert stack.profile == "production"
        assert "ds86_01" not in stack.cameras                  # 宁缺勿假：没有 Fake 替身
        assert "ds86_01" not in list(stack.camera_registry.camera_ids())
        issue = _issue_for(stack, "ds86_01")
        assert issue is not None and "SDK" in issue.reason
        # 界面据此把该相机标红并说明原因
        entry = _camera_entry("ds86_01")
        assert stack.device_state.status_text(entry) == STATUS_DISABLED
        assert stack.device_state.tone_of(entry) == TONE_DANGER
        assert "SDK" in stack.device_state.detail_of(entry)
    finally:
        stack.close()


def test_robot_connect_failure_enters_instead_of_exiting(tmp_path, monkeypatch):
    """真机场景：机器人连不上（如 192.168.3.66:8080）**不得**让程序退出。

    这是本次修复的直接依据：装配失败也要能进界面看状态，而不是把人挡在门外。
    """

    class _DeadArm:
        def __init__(self, *a, **k):
            pass

        def connect(self):        # 构造成功但连不上
            return False

    monkeypatch.setattr("application.bootstrap.production.RealmanMotionArm", _DeadArm)
    req = ProductionRequest(recipe_path="",
                            db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
                            device_config_path=_cfg_path(tmp_path,
                                                         robot_host="192.168.3.66"))
    stack = build_production_stack(req)          # 关键：**不抛异常** -> 界面照常起
    try:
        assert isinstance(stack.robot, NoRobot)
        issue = _issue_for(stack, "eco65_01")
        assert issue is not None
        assert "机器人连接失败" in issue.reason and "192.168.3.66:8080" in issue.reason
        # 运动一律拒绝，且报错说清是"没连上"而不是"没选"
        with pytest.raises(ProductionError) as ei:
            stack.robot.move_joint(None)
        assert "连接失败" in str(ei.value)
        entry = DeviceEntry("eco65_01", "ECO65", "robot", "vendor_tcp",
                            enabled=True, confirmed=True,
                            host="192.168.3.66", port=8080)
        assert stack.device_state.tone_of(entry) == TONE_DANGER
        assert "机器人连接失败" in stack.device_state.detail_of(entry)
    finally:
        stack.close()


def test_placeholder_robot_host_reported_not_fatal(tmp_path, monkeypatch):
    """占位机器人地址：不致命（照常起），但记进 startup_issues 且运动一律拒绝。"""
    req = ProductionRequest(recipe_path="",
                            db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
                            device_config_path=_cfg_path(tmp_path, robot_host="待核对"))
    stack = build_production_stack(req)
    try:
        assert isinstance(stack.robot, NoRobot)
        issue = _issue_for(stack, "eco65_01")
        assert issue is not None and "机器人地址" in issue.reason
        with pytest.raises(ProductionError):
            stack.robot.move_joint(None)
        entry = DeviceEntry("eco65_01", "ECO65", "robot", "vendor_tcp",
                            enabled=True, confirmed=True, host="待核对", port=8080)
        assert stack.device_state.tone_of(entry) == TONE_DANGER
        assert "机器人地址" in stack.device_state.detail_of(entry)
    finally:
        stack.close()


def test_placeholder_s100_endpoint_reported_not_fatal(tmp_path, monkeypatch):
    """占位 S100 端点：不致命，但该端点的相机通道（d405_01/02）必须被标出来。"""
    req = ProductionRequest(recipe_path="",
                            db_path=str(tmp_path / "r.db"), runtime_dir=str(tmp_path),
                            device_config_path=_cfg_path(tmp_path, s100_host="UNCONFIRMED"))
    stack = build_production_stack(req)
    try:
        assert list(stack.camera_registry.camera_ids()) == []        # 两个通道都没起来
        issue = _issue_for(stack, "d405_01")
        assert issue is not None and "S100 端点" in issue.reason
        assert "d405_02" in issue.keys                    # 同一次失败影响两个通道
        assert stack.device_state.tone_of(_camera_entry("d405_01", "zmq")) == TONE_DANGER
    finally:
        stack.close()


# ---------------------------------------------------------------- S100 协议选择
def test_real_s100_defaults_to_field_legacy_protocol(tmp_path, monkeypatch):
    """现场部署的是旧 pickle 服务 -> real 默认走 legacy（开箱就能抓帧）。"""
    from application.adapters.s100.legacy import LegacyS100CameraService

    stack = build_production_stack(_req(tmp_path))
    try:
        svc = stack.cameras["d405_01"]
        assert isinstance(svc, LegacyS100CameraService)
        assert svc.protocol == "s100-v1-pickle"
    finally:
        stack.close()


def test_real_s100_v2_is_explicit_opt_in(tmp_path, monkeypatch):
    """V2 多段编码仍是 draft -> 只能**显式**选择，不做静默切换。"""
    from application.adapters.s100.client import S100CameraService

    stack = build_production_stack(_req(tmp_path, s100_protocol="v2"))
    try:
        assert isinstance(stack.cameras["d405_01"], S100CameraService)
        assert stack.cameras["d405_01"].protocol == "s100-camera-v2"
    finally:
        stack.close()


def test_unknown_s100_protocol_rejected(tmp_path):
    with pytest.raises(ProductionError) as ei:
        ProductionRequest(recipe_path="",
                          db_path="x", runtime_dir="y", s100_protocol="v3")
    assert "S100 协议" in str(ei.value)


# ---------------------------------------------------------------- 运动闸
class _RecorderRobot:
    def __init__(self) -> None:
        self.moves: list[Any] = []

    def move_joint(self, target, *a, **k):
        self.moves.append(target)
        return "accepted"

    def set_io(self, n, state):
        return True

    def read_state(self) -> dict:
        return {"connected": True, "move_count": len(self.moves)}


def test_gated_robot_only_blocks_unconfirmed_targets():
    """DEC-085：无环境变量运动闸；只拒绝未确认示教点的 move_joint。"""
    inner = _RecorderRobot()
    gate = GatedRobot(inner)
    with pytest.raises(MotionBlocked):
        gate.move_joint(JointTarget(joint_deg=(1, 2, 3, 4, 5, 6),
                                    waypoint_ref="offline_placeholder"))
    assert inner.moves == [] and gate.blocked_calls
    assert gate.move_joint(JointTarget(joint_deg=(1, 2, 3, 4, 5, 6),
                                       waypoint_ref="taught_20261015")) == "accepted"


# ---------------------------------------------------------------- 启动执行方案（DEC-044）
# 现场报障（2026-10-19）：exe 旁边的 configs/recipes 留着 schema v1 旧配方，
# `--recipe` 指名载入没有兜底 -> ValidationError 直接整栈退出。现在：跳过/换新，不挡界面。
_V1_RECIPE = """\
schema_version: 1
recipe_id: ds86_two_station_example
version: 0.1
state: draft
stations:
  - {station_id: station_01}
required_cameras: [ds86_01]
sequence:
  - {id: capture, type: camera.capture, target_from: {station_field: joint_target_ref}}
"""

_V2_RECIPE = """\
schema_version: 2
recipe_id: ds86_two_station_example
version: 0.2-draft
state: draft
is_offline_draft: true
sequence:
  - {id: capture, type: camera.capture, camera_ref: ds86_01}
"""


def _recipe_layout(tmp_path, *, with_seed: bool):
    configs = tmp_path / "configs"
    seed = tmp_path / "seed"
    user = configs / "recipes" / "ds86_two_station_example.yaml"
    user.parent.mkdir(parents=True)
    user.write_text(_V1_RECIPE, encoding="utf-8")
    if with_seed:
        seed_file = seed / "recipes" / "ds86_two_station_example.yaml"
        seed_file.parent.mkdir(parents=True)
        seed_file.write_text(_V2_RECIPE, encoding="utf-8")
    return configs, seed, user


def _patch_app_paths(monkeypatch, configs, seed):
    monkeypatch.setattr("application.bootstrap.app_paths.configs_dir",
                        lambda: str(configs))
    monkeypatch.setattr("application.bootstrap.app_paths.seed_configs_dir",
                        lambda: str(seed))


def test_bad_startup_recipe_is_replaced_and_startup_continues(tmp_path, monkeypatch):
    """旧版启动配方 + 包内有新版 -> 自动备份换新，装配照常完成（不抛 ValidationError）。"""
    configs, seed, user = _recipe_layout(tmp_path, with_seed=True)
    _patch_app_paths(monkeypatch, configs, seed)

    stack = build_production_stack(_req(tmp_path, recipe_path=str(user)))
    try:
        assert stack.startup_notices, "换新必须留下通知（控制台/诊断可见）"
        assert any("已自动换新" in m for m in stack.startup_notices)
        assert "ds86_two_station_example" in stack.recipes.drafts()
        backups = list(user.parent.glob("*.incompatible-*.bak"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == _V1_RECIPE
        assert user.read_text(encoding="utf-8") == _V2_RECIPE
    finally:
        stack.close()


def test_bad_startup_recipe_without_seed_does_not_block_startup(tmp_path, monkeypatch):
    """旧版启动配方 + 包内没有新版 -> 只跳过并记通知；界面照常起，作业台无默认方案。"""
    configs, seed, user = _recipe_layout(tmp_path, with_seed=False)
    _patch_app_paths(monkeypatch, configs, seed)

    stack = build_production_stack(_req(tmp_path, recipe_path=str(user)))
    try:
        assert stack.startup_notices
        assert any("已跳过" in m for m in stack.startup_notices)
        assert stack.recipes.drafts() == []      # 坏配方没有被当成可用方案
        assert user.read_text(encoding="utf-8") == _V1_RECIPE   # 文件原样保留
    finally:
        stack.close()


def test_armed_gate_still_refuses_placeholder_target():
    """开闸也不能动到占位示教点 —— 没有现场示教就不许动。"""
    inner = _RecorderRobot()
    gate = GatedRobot(inner)
    for ref in (None, "", "offline_placeholder", "待核对"):
        with pytest.raises(MotionBlocked):
            gate.move_joint(JointTarget(joint_deg=(0, 0, 0, 0, 0, 0),
                                        waypoint_ref=ref))
    assert inner.moves == []


def test_waypoint_confirmation_helper():
    assert is_confirmed_waypoint("taught_20261015") is True
    for ref in (None, "", "offline_placeholder", "PLACEHOLDER_1", "待核对", "todo"):
        assert is_confirmed_waypoint(ref) is False
    with pytest.raises(MotionBlocked):
        require_confirmed_target(JointTarget(joint_deg=(0, 0, 0, 0, 0, 0)))


def test_gate_transparent_for_read_methods():
    gate = GatedRobot(_RecorderRobot())
    assert gate.read_state()["connected"] is True      # 读类方法透明代理
    assert gate.motion_allowed is True                 # DEC-085：real 恒允许运动


# ---------------------------------------------------------------- 示教点文件
def test_confirmed_targets_file_filters_unconfirmed(tmp_path):
    path = tmp_path / "targets.yaml"
    path.write_text(
        "targets:\n"
        "  - station_id: station_01\n"
        "    joint_deg: [1, 2, 3, 4, 5, 6]\n"
        "    waypoint_ref: taught_20261015\n"
        "    confirmed: true\n"
        "  - station_id: station_02\n"
        "    joint_deg: [0, 0, 0, 0, 0, 0]\n"
        "    waypoint_ref: taught_tbd\n"
        "    confirmed: false\n",
        encoding="utf-8")
    targets = load_confirmed_targets(str(path))
    assert set(targets) == {"station_01"}              # 未确认的被忽略
    assert targets["station_01"].joint_deg == (1, 2, 3, 4, 5, 6)


def test_missing_targets_file_is_empty_not_error(tmp_path):
    assert load_confirmed_targets(str(tmp_path / "nope.yaml")) == {}
    assert load_confirmed_targets("") == {}


# ---------------------------------------------------------------- 圆宿主
def test_circle_backend_without_host_refuses_instead_of_faking():
    backend = UnavailableCircleBackend()
    assert backend.host_version == "unavailable"
    with pytest.raises(ProductionError) as ei:
        backend.detect([], [], None)
    assert "未配置" in str(ei.value)


# ---------------------------------------------------------------- 界面标注
def test_badge_reflects_profile():
    """顶部徽标必须按 profile 明说，不把真实设备写成"设计示例"。"""
    from application.presentation.app import OFFLINE_BADGE, PRODUCTION_BADGE, profile_badge
    assert profile_badge("production") == PRODUCTION_BADGE
    assert profile_badge("offline") == OFFLINE_BADGE
    assert profile_badge(None) == OFFLINE_BADGE          # 未标记时保守显示离线示例
    assert "真实设备" in PRODUCTION_BADGE


# ---------------------------------------------------------------- S100 端点来源（DEC-081/083）
def _s100_store(*, device_host: str = S100_HOST,
                cameras: list[CameraEntry] | None = None) -> DeviceConfigStore:
    return DeviceConfigStore.in_memory(DeviceConfig(
        devices=[DeviceEntry("s100_01", "S100", "board", "zmq", enabled=True,
                             confirmed=True, host=device_host, port=S100_PORT)],
        cameras=cameras or []))


def test_s100_groups_prefer_camera_row_connection_config():
    """用户改的是「相机通道」的 IP/端口 → 必须用它，不能还读 s100_01 设备行。"""
    store = _s100_store(cameras=[CameraEntry(
        "d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
        host="192.168.2.117", port=5555, channel_id="cam1")])
    groups, invalid = _s100_groups(store)
    assert groups == {"tcp://192.168.2.117:5555": {"d405_01": "cam1"}}
    assert invalid == []


def test_s100_groups_allow_different_endpoints_per_channel():
    """DEC-083：改其中一条通道的 IP 不再让整组报错 —— 不同端点各建一个客户端。"""
    store = _s100_store(cameras=[
        CameraEntry("d405_01", protocol="zmq", enabled=True,
                    host="192.168.2.115", port=5555, channel_id="cam1"),
        CameraEntry("d405_02", protocol="zmq", enabled=True,
                    host="192.168.2.117", port=5555, channel_id="cam2"),
    ])
    groups, invalid = _s100_groups(store)
    assert groups == {
        "tcp://192.168.2.115:5555": {"d405_01": "cam1"},
        "tcp://192.168.2.117:5555": {"d405_02": "cam2"},
    }
    assert invalid == []


def test_s100_groups_fall_back_to_device_row_for_old_configs():
    """旧配置只在 s100_01 设备行填了地址：仍按它启动（兼容）。"""
    store = _s100_store(cameras=[CameraEntry(
        "d405_01", protocol="zmq", enabled=True, host="", port=None,
        channel_id="cam1")])
    groups, invalid = _s100_groups(store)
    assert groups == {f"tcp://{S100_HOST}:{S100_PORT}": {"d405_01": "cam1"}}
    assert invalid == []


def test_s100_groups_report_unconfigured_channel_without_blocking_others():
    """一条通道端点没配置只标它自己，不影响其它可用的通道。"""
    store = _s100_store(device_host="", cameras=[
        CameraEntry("d405_01", protocol="zmq", enabled=True, host="",
                    port=None, channel_id="cam1"),
        CameraEntry("d405_02", protocol="zmq", enabled=True,
                    host="192.168.2.117", port=5555, channel_id="cam2"),
    ])
    groups, invalid = _s100_groups(store)
    assert groups == {"tcp://192.168.2.117:5555": {"d405_02": "cam2"}}
    assert invalid == ["d405_01"]


# ---------------------------------------------------------------- 相机配置热重载（DEC-081/083）
def test_camera_reload_applies_new_d405_endpoint_without_restart(tmp_path, monkeypatch):
    """保存相机 IP 后 reload：按新端点重建该通道，不需要重启。"""
    stack = build_production_stack(_req(tmp_path))
    try:
        assert stack.cameras["d405_01"].endpoint == f"tcp://{S100_HOST}:{S100_PORT}"
        stack.device_config.update_camera("d405_01", CameraEntry(
            "d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
            host="192.168.2.115", port=5555, channel_id="cam1"))
        msg = stack.camera_reload()
        assert "S100" in msg
        assert stack.cameras["d405_01"].endpoint == "tcp://192.168.2.115:5555"
        # 另一条通道仍用旧端点（DEC-083：端点分组，不互相拖累）
        assert stack.cameras["d405_02"].endpoint == f"tcp://{S100_HOST}:{S100_PORT}"
    finally:
        stack.close()


def test_camera_reload_of_d405_does_not_reopen_ds86(tmp_path, monkeypatch):
    """改 D405 的 IP 不触发 DS86 重扫/重开 —— 去掉保存时“一大堆检查”（DEC-083）。"""
    from application.adapters.ds86.mock_sdk import MockDs86Sdk

    req = _req(tmp_path, sdk_overrides={"ds86": MockDs86Sdk()})
    stack = build_production_stack(req)
    try:
        ds86_before = stack.cameras["ds86_01"]
        assert getattr(ds86_before, "_open", False) is True
        stack.device_config.update_camera("d405_01", CameraEntry(
            "d405_01", "D405-1", "zmq", enabled=True, confirmed=True,
            host="192.168.2.115", port=5555, channel_id="cam1"))
        msg = stack.camera_reload()
        assert "S100" in msg and "DS86" not in msg
        assert stack.cameras["ds86_01"] is ds86_before   # 没重开 DS86
        assert ds86_before._open is True                  # 旧会话仍可用
    finally:
        stack.close()


# ---------------------------------------------------------------- 启动探测初始化（DEC-087）
def test_initial_probe_marks_enabled_by_reachability():
    from application.bootstrap.production import initial_probe
    from application.services.device_probe import ProbeResult
    from application.services.device_state import (
        STATUS_DISABLED, STATUS_ENABLED, TONE_DANGER, TONE_OK,
        DeviceStateRegistry,
    )

    class _OpenCam:
        def status(self):
            return {"open": True}

    store = DeviceConfigStore.in_memory(DeviceConfig(
        devices=[DeviceEntry("ok", enabled=True, confirmed=True,
                             protocol="modbus_tcp", host="10.0.0.1", port=502),
                 DeviceEntry("down", enabled=True, confirmed=True,
                             protocol="modbus_tcp", host="10.0.0.2", port=502),
                 DeviceEntry("off", enabled=False, confirmed=True,
                             protocol="modbus_tcp", host="10.0.0.3", port=502)],
        links=[], cameras=[]))
    reg = DeviceStateRegistry(cameras={})
    calls = []

    def fake_probe(entry, timeout=None):
        calls.append(entry.device_id)
        if entry.device_id == "ok":
            return ProbeResult(True, "TCP 端口可达", is_real=True)
        return ProbeResult(False, "TCP 连接失败", is_real=True)

    out = initial_probe(store, reg, probe_fn=fake_probe)
    assert set(calls) == {"ok", "down"}                  # 未启用的不探测
    status = {k: s for k, s, _d in out}
    assert status["ok"] == STATUS_ENABLED and status["down"] == STATUS_DISABLED
    assert reg.tone_of(store.find_device("ok")) == TONE_OK
    assert reg.tone_of(store.find_device("down")) == TONE_DANGER
