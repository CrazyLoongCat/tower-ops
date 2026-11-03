"""设备/链路/相机配置的模型、持久化与校验 (E21)。

覆盖: 种子生成 / YAML 往返 / 增删改 / 原子写+备份 / 协议必填校验 /
未确认值默认 / 内存模式不落盘 / 绑定校验复用。全部离线, 不接触真实设备。
"""
from __future__ import annotations

import os

import pytest

from application.contracts.enums import DeviceProtocol
from application.services.device_config import (
    CameraEntry,
    DeviceConfigStore,
    DeviceEntry,
    LinkEntry,
    default_config,
    endpoint,
    is_placeholder,
    valid_protocol,
)


def _store(tmp_path) -> DeviceConfigStore:
    return DeviceConfigStore(str(tmp_path / "devices.yaml"))


# ---------------------------------------------------------------- 种子
def test_seed_file_created_when_missing(tmp_path):
    st = _store(tmp_path)
    cfg = st.load()
    assert os.path.exists(st.path)
    assert len(cfg.devices) == 7 and len(cfg.links) == 1 and len(cfg.cameras) == 5
    assert cfg.devices[0].device_id == "stm32_01"


def test_seed_values_are_unconfirmed_no_fabrication(tmp_path):
    """种子不得代填现场值: 连接参数留空且 confirmed 全 False。"""
    cfg = default_config()
    for d in cfg.devices:
        assert d.confirmed is False and d.enabled is False
        if d.protocol in (DeviceProtocol.SERIAL.value,):
            assert is_placeholder(d.serial_port)
        else:
            assert is_placeholder(d.host) and d.port is None


def test_in_memory_store_does_not_write(tmp_path):
    path = tmp_path / "should_not_exist.yaml"
    st = DeviceConfigStore.in_memory()
    st.load()
    st.add_device(DeviceEntry("x1", "X", "board"))
    st.save()
    assert not path.exists()
    assert st.find_device("x1") is not None


# ---------------------------------------------------------------- 往返
def test_yaml_roundtrip_preserves_order_and_values(tmp_path):
    st = _store(tmp_path)
    st.load()
    d = DeviceEntry("cam_net", "网络相机", "camera", DeviceProtocol.JSON_TCP.value,
                    enabled=True, confirmed=True, host="10.0.0.5", port=8889,
                    notes="现场核对 2026-10-14")
    st.add_device(d)
    st.save()

    reloaded = DeviceConfigStore(st.path).load()
    got = reloaded.devices[-1]
    assert got.device_id == "cam_net"
    assert got.host == "10.0.0.5" and got.port == 8889
    assert got.confirmed is True and got.enabled is True
    # 块状输出: 可直接人工阅读/编辑
    text = open(st.path, encoding="utf-8").read()
    assert "device_id: cam_net" in text and "{" not in text.split("\n", 3)[-1][:200]


# ---------------------------------------------------------------- CRUD
def test_crud_all_three_collections(tmp_path):
    st = _store(tmp_path)
    st.load()

    st.add_device(DeviceEntry("p1", "P", "board"))
    st.update_device("p1", DeviceEntry("p1", "P2", "board", enabled=True))
    assert st.find_device("p1").name == "P2"
    st.remove_device("p1")
    assert st.find_device("p1") is None

    st.add_link(LinkEntry("l9"))
    st.update_link("l9", LinkEntry("l9", notes="n"))
    assert st.find_link("l9").notes == "n"
    st.remove_link("l9")
    assert st.find_link("l9") is None

    st.add_camera(CameraEntry("c9", "C"))
    st.update_camera("c9", CameraEntry("c9", "C2"))
    assert st.find_camera("c9").name == "C2"
    st.remove_camera("c9")
    assert st.find_camera("c9") is None


def test_duplicate_ids_rejected(tmp_path):
    st = _store(tmp_path)
    st.load()
    with pytest.raises(ValueError):
        st.add_device(DeviceEntry("stm32_01"))
    with pytest.raises(ValueError):
        st.add_link(LinkEntry("link0"))
    with pytest.raises(ValueError):
        st.add_camera(CameraEntry("ds86_01"))


def test_update_missing_raises(tmp_path):
    st = _store(tmp_path)
    st.load()
    with pytest.raises(KeyError):
        st.update_device("nope", DeviceEntry("nope"))


# ---------------------------------------------------------------- 原子写/备份
def test_atomic_write_creates_backup(tmp_path):
    st = _store(tmp_path)
    st.load()
    st.add_device(DeviceEntry("new1"))
    st.save()                                  # 第二次写 -> 生成 .bak
    assert os.path.exists(st.path + ".bak")


# ---------------------------------------------------------------- 校验
def test_validate_ids_and_connection_rules(tmp_path):
    st = _store(tmp_path)
    st.load()
    cfg = st.config
    cfg.devices.append(DeviceEntry("stm32_01"))            # 重复 ID
    cfg.devices.append(DeviceEntry("tcp1", protocol=DeviceProtocol.JSON_TCP.value,
                                   host="1.2.3.4", port=99999))  # 端口越界
    issues = st.validate()
    blocking = [i for i in issues if i.blocking]
    codes = {i.code for i in blocking}
    assert "设备.id" in codes
    assert "conn.port" in codes


def test_validate_unconfirmed_is_advisory(tmp_path):
    """未配置 / 未核对只作非阻塞提示, 不阻断保存。"""
    st = _store(tmp_path)
    st.load()
    issues = st.validate()
    assert issues and all(not i.blocking for i in issues)
    assert any(i.code == "unconfirmed" for i in issues)


def test_binding_report_uses_board_binding(tmp_path):
    st = _store(tmp_path)
    st.load()
    # stm32_01 绑定 link0 地址1; 再加一块同链路同地址 -> 冲突
    st.add_device(DeviceEntry("stm32_dup", "dup", "board",
                              protocol=DeviceProtocol.SERIAL.value,
                              link_id="link0", address=1))
    report = st.binding_report()
    assert any("地址" in m for m in report.messages())
    assert not report.ok


def test_endpoint_parses_stream_url_and_prefers_explicit():
    from application.services.device_config import endpoint
    cam = CameraEntry("c", protocol="rtsp", stream_url="rtsp://10.1.2.3:8554/s")
    assert endpoint(cam) == ("10.1.2.3", 8554)
    cam2 = CameraEntry("c2", protocol="rtsp", host="10.9.9.9", port=554,
                       stream_url="rtsp://10.1.2.3:8554/s")
    assert endpoint(cam2) == ("10.9.9.9", 554)          # 显式优先
    assert endpoint(DeviceEntry("d", protocol="sdk"))[0] == ""


def test_shipped_devices_config_loads_and_validates():
    """仓库里 ship 的 configs/devices/devices.yaml 必须能加载且无阻塞项。

    **注意**：该文件是给用户/现场在其上继续编辑的（设备配置页会写它），
    所以这里只断言**结构不变量**，不断言具体某条记录的取值 ——
    否则用户一改配置测试就红。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    path = root / "configs" / "devices" / "devices.yaml"
    st = DeviceConfigStore(str(path))
    cfg = st.load()
    assert cfg.schema_version == 1
    assert cfg.devices and cfg.links and cfg.cameras
    assert not [i for i in st.validate() if i.blocking]
    # 每条记录都要有来源/核对备注，便于回溯（这是配置纪律，不是具体取值）
    for d in cfg.devices:
        assert d.notes, f"{d.device_id} 缺备注"
    for c in cfg.cameras:
        assert c.notes, f"{c.camera_id} 缺备注"


def test_shipped_config_network_camera_endpoints_resolve():
    """凡声明为 rtsp/http 的相机，端点必须能从 stream_url 或 host/port 解析出来。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    st = DeviceConfigStore(str(root / "configs" / "devices" / "devices.yaml"))
    netcams = [c for c in st.load().cameras if c.protocol in ("rtsp", "http")]
    for cam in netcams:
        host, port = endpoint(cam)
        assert host and port, f"{cam.camera_id} 的端点解析不出 host:port"


def test_protocol_helpers():
    assert valid_protocol("zmq") and not valid_protocol("tcp")
    for token in ("", "UNCONFIRMED", "待核对", "  ", None):
        assert is_placeholder(token)
    assert not is_placeholder("192.168.1.10")


# ---------------------------------------------------------------- 回归守卫
def test_dialog_specs_match_entry_models():
    """编辑 spec 的字段键必须都存在于对应 dataclass, 否则用户输入会被静默丢弃。"""
    from application.presentation.pages import devices as ui
    for spec, cls in ((ui._DEVICE_SPEC, DeviceEntry),
                      (ui._LINK_SPEC, LinkEntry),
                      (ui._CAMERA_SPEC, CameraEntry)):
        known = set(cls.__dataclass_fields__)
        keys = {f["key"] for f in spec}
        assert keys <= known, f"{cls.__name__} 缺字段: {sorted(keys - known)}"


def test_camera_with_serial_protocol_does_not_crash_validate():
    """相机遇上 serial 协议(相机记录无串口字段)时 validate 不应抛异常。"""
    st = DeviceConfigStore.in_memory()
    st.add_camera(CameraEntry("c1", protocol=DeviceProtocol.SERIAL.value))
    issues = st.validate()
    assert isinstance(issues, list)
    assert any(i.code == "conn.serial" for i in issues)


def test_rename_to_existing_id_rejected(tmp_path):
    """改名成已存在的 ID 必须被拒绝 (add 的重复检查对 update 同样生效)。"""
    st = _store(tmp_path)
    st.load()
    with pytest.raises(ValueError):
        st.update_device("stm32_02", DeviceEntry("stm32_01"))
    with pytest.raises(ValueError):
        st.update_camera("d405_02", CameraEntry("ds86_01"))
    st.add_link(LinkEntry("l2"))
    with pytest.raises(ValueError):
        st.update_link("l2", LinkEntry("link0"))
    # 不改名则允许正常更新
    st.update_device("stm32_02", DeviceEntry("stm32_02", notes="ok"))
    assert st.find_device("stm32_02").notes == "ok"


# ---------------------------------------------------------------- 图像算法参数 (DEC-021)
def test_camera_image_ops_defaults_empty_and_not_shared():
    """新增字段必须向后兼容: 缺省为空映射, 且不同记录不能共享同一个 dict。"""
    a, b = CameraEntry("a"), CameraEntry("b")
    assert a.image_ops == {}
    a.image_ops["hflip"] = True
    assert b.image_ops == {}, "默认值必须各自新建, 否则一条记录会污染另一条"


def test_image_ops_roundtrips_through_yaml(tmp_path):
    """按通道保存的图像算法参数要能原样落盘读回。"""
    st = _store(tmp_path)
    st.load()
    entry = st.find_camera("ds86_01")
    ops = {"hflip": True, "rotate": 90, "threshold_mode": "otsu",
           "detect": "contour", "detect_min_area": 40.5}
    st.update_camera("ds86_01", CameraEntry(**{**entry.__dict__, "image_ops": ops}))
    st.save()

    again = _store(tmp_path)
    again.load()
    back = again.find_camera("ds86_01")
    assert back.image_ops == ops
    assert back.name == entry.name, "写回 image_ops 不该动其它字段"


def test_legacy_camera_record_without_image_ops_still_loads():
    """旧 devices.yaml 没有 image_ops 键也要能加载 (未知键忽略、缺键取默认)。"""
    from application.services.device_config import DeviceConfig, _entry

    raw = {"camera_id": "ds86_01", "name": "DS86", "notes": "旧记录",
           "some_future_key": 1}
    cam = _entry(CameraEntry, raw)
    assert cam.image_ops == {} and cam.name == "DS86"
    assert not hasattr(cam, "some_future_key")

    cfg = DeviceConfig.from_dict({"schema_version": 1, "devices": [], "links": [],
                                  "cameras": [raw]})
    assert cfg.cameras[0].image_ops == {}
