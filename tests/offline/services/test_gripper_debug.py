"""夹爪板调试服务测试（DEC-097）：命令下发顺序、心跳、序列状态机、配置落盘。"""
from __future__ import annotations

import pytest

from application.adapters.gripper_board import registers as R
from application.services.gripper_debug import (
    GripperDebugConfig, GripperDebugError, GripperDebugStore, GripperSequence,
    GripperSequenceRunner, GripperSession, GripperStep, ProcProgress,
)


class FakeClient:
    """实现 GripperSession 用到的客户端接口；不碰网络。"""

    def __init__(self, *_args, **_kwargs) -> None:
        self.connected = True
        self.writes: list[tuple[int, int]] = []
        self.progress = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def read_holding(self, address: int, count: int) -> list[int]:
        if address == R.DEV_TYPE:
            return [0x0002, 0x0100, 0x0105, 1, 0x0001]
        if address == R.PROC_STATUS:
            return list(self.progress[:count]) + [0] * max(0, count - len(self.progress))
        return [0] * count

    def write_register(self, address: int, value: int) -> None:
        self.writes.append((address, value))
        if address == R.PROC_CMD:
            self.progress[0] = 1

    def write_registers(self, address: int, values: list[int]) -> None:
        for i, value in enumerate(values):
            self.write_register(address + i, value)


def _session(**cfg) -> tuple[GripperSession, FakeClient]:
    client = FakeClient()
    session = GripperSession(client_factory=lambda *a, **k: client)
    config = GripperDebugConfig(host="127.0.0.1", heartbeat_ms=0, **cfg)
    session.connect(config)
    return session, client


def test_session_identity_and_heartbeat():
    session, client = _session()
    assert session.identity["dev_type"] == 0x0002
    assert session.identity_warning is None
    assert session.feed_heartbeat() == 1
    assert client.writes[-1] == (R.HEARTBEAT, 1)
    assert session.feed_heartbeat() == 2
    session.disconnect()
    assert not session.connected


def test_wrong_device_type_is_refused():
    class WrongDevice(FakeClient):
        def read_holding(self, address, count):
            if address == R.DEV_TYPE:
                return [0x0099, 1, 1, 1, 0]
            return super().read_holding(address, count)

    client = WrongDevice()
    session = GripperSession(client_factory=lambda *a, **k: client)
    with pytest.raises(GripperDebugError, match="不是 0x0002"):
        session.connect(GripperDebugConfig(host="127.0.0.1", heartbeat_ms=0))
    assert not client.connected


def test_start_command_writes_params_before_command():
    session, client = _session()
    spec = session.start_command(0x02, {"jj_pos": 250, "sk_pos": 280, "p1": 5})
    assert spec.code == 0x02
    assert client.writes == [
        (R.PROC_JJ_POS, 250), (R.PROC_SK_POS, 280), (R.PROC_PARAM1, 5),
        (R.PROC_CMD, 0x02),
    ]
    session.disconnect()


def test_start_command_rejected_while_board_busy():
    session, client = _session()
    client.progress[0] = 1
    with pytest.raises(GripperDebugError, match="仍在执行"):
        session.start_command(0x01, {})
    session.disconnect()


def test_read_progress_decodes_32bit_elapsed():
    session, client = _session()
    client.progress = [1, 2, 3, 50, 7, 9, 0x1234, 0x0001, 4, 0x71]
    progress = session.read_progress()
    assert (progress.status, progress.current_step, progress.total_steps) == (1, 2, 3)
    assert progress.progress == 50
    assert progress.error_code == 7
    assert progress.elapsed_ms == 0x11234
    assert progress.cmd_count == 4
    assert progress.last_cmd == 0x71
    session.disconnect()


def test_monitor_uses_scale_and_raw_values():
    session, client = _session()
    original = client.read_holding

    def fake_read(address: int, count: int) -> list[int]:
        if address == 0x0000:
            return [2, 0x0100, 0x0105, 1, 0, 0x0100, 0, 5, 240, 0x00FA,
                    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        if address == 0x0900:
            return [25000, 28000, 0, 0, 0, 0, 0, 0, 0, 120, 80, 0, 0]
        return [0] * count

    client.read_holding = fake_read  # type: ignore[assignment]
    values = session.read_monitor()
    assert values["DEV_TYPE"].text == "0x0002"
    assert values["BUS_VOLTAGE"].value == pytest.approx(24.0)
    assert values["PCB_TEMP"].value == pytest.approx(25.0)
    assert values["JZ_JJ_POS"].value == pytest.approx(250.0)
    assert values["FORCE1_KG"].value == pytest.approx(1.2)
    client.read_holding = original  # type: ignore[assignment]
    session.disconnect()


def test_store_saves_and_loads_connection_and_sequences(tmp_path):
    path = tmp_path / "gripper_debug.yaml"
    store = GripperDebugStore(str(path))
    cfg = GripperDebugConfig(host="192.168.1.50", port=502, unit_id=2,
                             timeout_ms=600, heartbeat_ms=1500,
                             heartbeat_timeout_ms=5000)
    seq = GripperSequence("测试", [
        GripperStep("打开", 0x01, wait="done", timeout_ms=20000),
        GripperStep("夹紧", 0x02, params={"jj_pos": 250}, wait="delay",
                    delay_ms=800, timeout_ms=15000),
    ])
    store.save(cfg, [seq])
    cfg2, seqs = store.load()
    assert cfg2.host == "192.168.1.50" and cfg2.unit_id == 2
    assert [s.name for s in seqs] == ["测试"]
    assert seqs[0].steps[1].params["jj_pos"] == 250
    assert seqs[0].steps[1].wait == "delay"


class RunnerSession:
    connected = True

    def __init__(self) -> None:
        self.commands: list[tuple[int, dict]] = []
        self.progress = ProcProgress(status=1)
        self.aborted = 0
        self.paused = 0
        self.resumed = 0

    def start_command(self, code, params=None):
        self.commands.append((code, dict(params or {})))
        self.progress = ProcProgress(status=1)

    def read_progress(self):
        return self.progress

    def abort_command(self):
        self.aborted += 1

    def pause_command(self):
        self.paused += 1

    def resume_command(self):
        self.resumed += 1


def _sequence(*steps: GripperStep) -> GripperSequence:
    return GripperSequence("序列", list(steps))


def test_runner_advances_to_next_step_on_done():
    session = RunnerSession()
    now = [0.0]
    runner = GripperSequenceRunner(session, clock=lambda: now[0])
    seq = _sequence(GripperStep("a", 0x01), GripperStep("b", 0x02))
    runner.start(seq)
    assert [c[0] for c in session.commands] == [0x01]
    session.progress = ProcProgress(status=2)
    now[0] = 0.2
    runner.tick()                     # 完成本步 -> 进入下一步
    runner.tick()                     # 下一 tick 下发下一步
    assert [c[0] for c in session.commands] == [0x01, 0x02]
    session.progress = ProcProgress(status=2)
    runner.tick()
    assert runner.state.phase == "done"


def test_runner_stops_on_board_error():
    session = RunnerSession()
    runner = GripperSequenceRunner(session, clock=lambda: 0.0)
    runner.start(_sequence(GripperStep("a", 0x01)))
    session.progress = ProcProgress(status=3, error_code=9)
    runner.tick()
    assert runner.state.phase == "error"
    assert "错误码=9" in runner.state.message


def test_runner_timeout_aborts_current_step():
    session = RunnerSession()
    now = [0.0]
    runner = GripperSequenceRunner(session, clock=lambda: now[0])
    runner.start(_sequence(GripperStep("a", 0x01, timeout_ms=100)))
    session.progress = ProcProgress(status=1)
    now[0] = 0.2
    runner.tick()
    assert session.aborted == 1
    assert runner.state.phase == "error"
    assert "超时" in runner.state.message


def test_runner_delay_step_advances_without_board_done():
    session = RunnerSession()
    now = [0.0]
    runner = GripperSequenceRunner(session, clock=lambda: now[0])
    runner.start(_sequence(
        GripperStep("a", 0x70, wait="delay", delay_ms=100),
        GripperStep("b", 0x71, wait="delay", delay_ms=100)))
    now[0] = 0.15
    runner.tick()                     # 延时到 -> 下一步
    runner.tick()                     # 下发下一步
    assert [c[0] for c in session.commands] == [0x70, 0x71]


def test_runner_pause_resume_stop_calls_board():
    session = RunnerSession()
    runner = GripperSequenceRunner(session, clock=lambda: 0.0)
    runner.start(_sequence(GripperStep("a", 0x01)))
    runner.pause()
    assert session.paused == 1 and runner.state.phase == "paused"
    runner.resume()
    assert session.resumed == 1 and runner.state.phase == "running"
    runner.stop()
    assert session.aborted == 1 and runner.state.phase == "stopped"
