"""夹爪板调试服务（DEC-097）：连接配置、控制项、单步命令、动作序列。

**边界**：
- 只走高层流程命令（PROC_CMD）和系统控制项；不开放单电机块/原始寄存器写入；
- 监视寄存器只读；
- 连接参数与动作序列存在 `<runtime>/gripper_debug.yaml`，不碰 devices.yaml；
- 心跳由后台线程按配置周期写 0x008A，避免 UI 打开对话框时心跳中断；
- 所有真实动作均为 hardware NOT_RUN，离线只用假会话/假服务器验证协议与流程。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ruamel.yaml import YAML

from application.adapters.gripper_board import registers as R
from application.adapters.gripper_board.modbus_tcp import (
    DEFAULT_PORT,
    ModbusTcpClient,
    ModbusTcpError,
)
from application.services.recipes.service import atomic_write

SEQUENCE_FILE = "gripper_debug.yaml"
STEP_WAIT_DONE = "done"
STEP_WAIT_DELAY = "delay"

# 单步/序列可写的参数字段：key -> (界面名, 单位, 寄存器地址)
PARAM_SPECS: tuple[tuple[str, str, str, int], ...] = (
    ("p1", "参数1", "", R.PROC_PARAM1),
    ("p2", "参数2", "", R.PROC_PARAM2),
    ("p3", "参数3", "", R.PROC_PARAM3),
    ("p4", "参数4", "", R.PROC_PARAM4),
    ("jj_pos", "夹紧位", "mm", R.PROC_JJ_POS),
    ("sk_pos", "松开位", "mm", R.PROC_SK_POS),
    ("vmod_pos", "V 块目标", "mm", R.PROC_VMOD),
)
PARAM_BY_KEY = {key: (label, unit, addr) for key, label, unit, addr in PARAM_SPECS}


class GripperDebugError(RuntimeError):
    """调试服务层的明确错误（未连接/参数非法/板端异常）。"""


# ---------------------------------------------------------------- 配置
@dataclass
class GripperDebugConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    unit_id: int = 1
    timeout_ms: int = 800
    heartbeat_ms: int = 1000
    heartbeat_timeout_ms: int = 5000

    def validate(self) -> None:
        if not self.host.strip():
            raise ValueError("IP/主机不能为空")
        if not (1 <= int(self.port) <= 65535):
            raise ValueError("端口必须在 1~65535")
        if not (1 <= int(self.unit_id) <= 247):
            raise ValueError("从站地址必须在 1~247")
        if not (100 <= int(self.timeout_ms) <= 10000):
            raise ValueError("通信超时必须在 100~10000ms")
        if not (0 <= int(self.heartbeat_ms) <= 60000):
            raise ValueError("心跳周期必须在 0~60000ms（0=关闭）")
        if not (100 <= int(self.heartbeat_timeout_ms) <= 60000):
            raise ValueError("心跳超时必须在 100~60000ms")

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host.strip(), "port": int(self.port),
            "unit_id": int(self.unit_id), "timeout_ms": int(self.timeout_ms),
            "heartbeat_ms": int(self.heartbeat_ms),
            "heartbeat_timeout_ms": int(self.heartbeat_timeout_ms),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "GripperDebugConfig":
        data = data or {}
        cfg = cls(
            host=str(data.get("host") or "127.0.0.1"),
            port=int(data.get("port") or DEFAULT_PORT),
            unit_id=int(data.get("unit_id") or 1),
            timeout_ms=int(data.get("timeout_ms") or 800),
            heartbeat_ms=int(data.get("heartbeat_ms") if data.get("heartbeat_ms")
                             is not None else 1000),
            heartbeat_timeout_ms=int(data.get("heartbeat_timeout_ms") or 5000),
        )
        cfg.validate()
        return cfg


# ---------------------------------------------------------------- 序列模型
@dataclass
class GripperStep:
    name: str
    command: int
    params: dict[str, int] = field(default_factory=dict)
    wait: str = STEP_WAIT_DONE
    delay_ms: int = 500
    timeout_ms: int = 15000

    @property
    def spec(self) -> R.CommandSpec:
        spec = R.COMMAND_BY_CODE.get(int(self.command))
        if spec is None:
            raise GripperDebugError(f"未知流程命令号 0x{int(self.command):02X}")
        return spec

    def validate(self) -> None:
        if self.command not in R.COMMAND_BY_CODE:
            raise ValueError(f"步骤 {self.name!r}: 未知命令 0x{self.command:02X}")
        if self.wait not in (STEP_WAIT_DONE, STEP_WAIT_DELAY):
            raise ValueError(f"步骤 {self.name!r}: 等待方式必须是 done/delay")
        if not (0 <= int(self.delay_ms) <= 600000):
            raise ValueError(f"步骤 {self.name!r}: 延时必须在 0~600000ms")
        if not (100 <= int(self.timeout_ms) <= 600000):
            raise ValueError(f"步骤 {self.name!r}: 超时必须在 100~600000ms")
        for key, value in self.params.items():
            if key not in PARAM_BY_KEY:
                raise ValueError(f"步骤 {self.name!r}: 未知参数 {key!r}")
            if value is None:
                continue
            if not (0 <= int(value) <= 65535):
                raise ValueError(f"步骤 {self.name!r}: 参数 {key} 越界（0~65535）")

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "command": int(self.command),
                "params": {k: int(v) for k, v in self.params.items()},
                "wait": self.wait, "delay_ms": int(self.delay_ms),
                "timeout_ms": int(self.timeout_ms)}

    @classmethod
    def from_dict(cls, data: dict) -> "GripperStep":
        params = {str(k): int(v) for k, v in (data.get("params") or {}).items()}
        step = cls(name=str(data.get("name") or "步骤"),
                   command=int(data.get("command") or 0),
                   params=params,
                   wait=str(data.get("wait") or STEP_WAIT_DONE),
                   delay_ms=int(data.get("delay_ms") or 500),
                   timeout_ms=int(data.get("timeout_ms") or 15000))
        step.validate()
        return step


@dataclass
class GripperSequence:
    name: str
    steps: list[GripperStep] = field(default_factory=list)

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("序列名称不能为空")
        for step in self.steps:
            step.validate()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: dict) -> "GripperSequence":
        seq = cls(name=str(data.get("name") or "未命名序列"),
                  steps=[GripperStep.from_dict(d)
                         for d in (data.get("steps") or [])])
        seq.validate()
        return seq


def default_sequences() -> list[GripperSequence]:
    """首次打开时的示例（只在内存里，用户点「保存」才落盘）。"""
    return [GripperSequence("开合测试", [
        GripperStep("打开", 0x01, wait=STEP_WAIT_DONE, timeout_ms=15000),
        GripperStep("夹紧", 0x02, wait=STEP_WAIT_DONE, timeout_ms=15000),
    ])]


class GripperDebugStore:
    """`<runtime>/gripper_debug.yaml` 的读写。不存在时返回默认配置+示例序列。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> tuple[GripperDebugConfig, list[GripperSequence]]:
        if not os.path.exists(self.path):
            return GripperDebugConfig(), default_sequences()
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = YAML(typ="safe").load(fh) or {}
        except Exception as exc:  # noqa: BLE001 - 坏文件不挡页面, 用默认值并保留原文件
            raise GripperDebugError(
                f"读取 {self.path} 失败：{exc}（原文件保留，可修正后重开）") from exc
        cfg = GripperDebugConfig.from_dict(data.get("connection") or {})
        sequences = [GripperSequence.from_dict(d)
                     for d in (data.get("sequences") or [])]
        return cfg, sequences

    def save(self, config: GripperDebugConfig,
             sequences: list[GripperSequence]) -> None:
        config.validate()
        for seq in sequences:
            seq.validate()
        data = {"connection": config.to_dict(),
                "sequences": [s.to_dict() for s in sequences]}
        yml = YAML(typ="safe")
        yml.default_flow_style = False
        yml.width = 4096
        yml.indent(mapping=2, sequence=4, offset=2)
        from io import StringIO
        buf = StringIO()
        yml.dump(data, buf)
        header = ("# 夹爪板调试配置（DEC-097）。连接参数与动作序列只影响调试页，\n"
                  "# 不写入 devices.yaml；动作执行前请确认现场安全。\n")
        atomic_write(self.path, header + buf.getvalue())


# ---------------------------------------------------------------- 会话
@dataclass
class MonitorValue:
    name: str
    label: str
    group: str
    value: float | int
    raw: int
    unit: str
    text: str
    bits: str = ""


@dataclass
class ProcProgress:
    status: int
    current_step: int = 0
    total_steps: int = 0
    progress: int = 0
    error_code: int = 0
    step_status: int = 0
    elapsed_ms: int = 0
    cmd_count: int = 0
    last_cmd: int = 0

    @property
    def text(self) -> str:
        return R.PROC_STATUS_TEXT.get(self.status, f"未知({self.status})")

    @property
    def running(self) -> bool:
        return self.status == 1


class _HeartbeatThread(threading.Thread):
    def __init__(self, session: "GripperSession", interval_ms: int) -> None:
        super().__init__(name="gripper-heartbeat", daemon=True)
        self._session = session
        self._interval = max(0.1, interval_ms / 1000.0)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._session.feed_heartbeat()
            except Exception as exc:  # noqa: BLE001 - 心跳失败记录, 由页面提示
                if self._session.connected:
                    self._session.heartbeat_error = str(exc)


class GripperSession:
    """一条到夹爪板的调试会话（单连接、串行请求、后台心跳）。"""

    def __init__(self, client_factory: Callable[..., Any] = ModbusTcpClient) -> None:
        self._client_factory = client_factory
        self.client: Any | None = None
        self.config: GripperDebugConfig | None = None
        self.identity: dict[str, Any] = {}
        self.identity_warning: str | None = None
        self.heartbeat_count = 0
        self.heartbeat_error: str | None = None
        self.last_heartbeat_monotonic: float | None = None
        self._heartbeat: _HeartbeatThread | None = None
        self._active_step: GripperStep | None = None
        self._active_started = 0.0

    # ---------- 连接 ----------
    @property
    def connected(self) -> bool:
        return bool(self.client is not None and self.client.connected)

    def connect(self, config: GripperDebugConfig) -> dict[str, Any]:
        config.validate()
        self.disconnect()
        client = self._client_factory(config.host, config.port, config.unit_id,
                                      config.timeout_ms / 1000.0)
        try:
            client.connect()
            regs = client.read_holding(R.DEV_TYPE, 5)
        except Exception:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        dev_type = regs[0]
        if dev_type != 0x0002:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
            raise GripperDebugError(
                f"设备类型 0x{dev_type:04X} 不是 0x0002（夹爪控制板），"
                f"拒绝连接；请核对 IP/端口/从站地址")
        self.client = client
        self.config = config
        self.identity = {
            "dev_type": dev_type,
            "fw_ver": regs[1],
            "hw_ver": regs[2],
            "dev_addr": regs[3],
            "sys_status": regs[4],
        }
        self.identity_warning = (
            None if dev_type == 0x0002
            else f"设备类型 0x{dev_type:04X} 不是 0x0002（夹爪控制板），请确认 IP/从站地址")
        self.heartbeat_count = 0
        self.heartbeat_error = None
        if config.heartbeat_ms > 0:
            self._heartbeat = _HeartbeatThread(self, config.heartbeat_ms)
            self._heartbeat.start()
        return dict(self.identity)

    def disconnect(self) -> None:
        hb, self._heartbeat = self._heartbeat, None
        if hb is not None:
            hb.stop()
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self.heartbeat_error = None
        self._active_step = None

    def _ensure(self) -> Any:
        if not self.connected:
            raise GripperDebugError("未连接夹爪板")
        return self.client

    # ---------- 心跳 ----------
    def feed_heartbeat(self) -> int:
        client = self._ensure()
        self.heartbeat_count = (self.heartbeat_count + 1) & 0xFFFF
        client.write_register(R.HEARTBEAT, self.heartbeat_count)
        self.last_heartbeat_monotonic = time.monotonic()
        self.heartbeat_error = None
        return self.heartbeat_count

    def set_heartbeat_timeout(self, timeout_ms: int) -> None:
        if not (100 <= int(timeout_ms) <= 60000):
            raise ValueError("心跳超时必须在 100~60000ms")
        self._ensure().write_register(R.HEARTBEAT_TO, int(timeout_ms))

    def set_watchdog_timeout(self, timeout_ms: int) -> None:
        if not (100 <= int(timeout_ms) <= 60000):
            raise ValueError("看门狗超时必须在 100~60000ms")
        self._ensure().write_register(R.IWDG_TIMEOUT, int(timeout_ms))

    # ---------- 控制项 ----------
    def trigger_sys_ctrl(self, mask: int) -> None:
        self._ensure().write_register(R.SYS_CTRL, int(mask) & 0xFFFF)

    def set_sys_mode(self, mode: int) -> None:
        if int(mode) not in R.SYS_MODES:
            raise ValueError(f"系统模式越界: {mode}")
        self._ensure().write_register(R.SYS_MODE, int(mode))

    def set_proc_mode(self, mode: int) -> None:
        if int(mode) not in R.PROC_MODES:
            raise ValueError(f"流程模式越界: {mode}")
        self._ensure().write_register(R.PROC_MODE, int(mode))

    def proc_ctrl(self, mask: int) -> None:
        self._ensure().write_register(R.PROC_CTRL, int(mask) & 0xFFFF)

    def save_params(self) -> None:
        self._ensure().write_register(R.PARAM_SAVE, R.PARAM_SAVE_MAGIC)

    def load_params(self) -> None:
        self._ensure().write_register(R.PARAM_LOAD, R.PARAM_LOAD_MAGIC)

    def reset_params(self) -> None:
        self._ensure().write_register(R.PARAM_RESET, R.PARAM_RESET_MAGIC)

    # ---------- 监督 ----------
    def read_monitor(self) -> dict[str, MonitorValue]:
        client = self._ensure()
        blocks: dict[int, list[int]] = {}
        for start, count in R.MONITOR_BLOCKS:
            blocks[start] = client.read_holding(start, count)
        decoded = R.decode_monitor(blocks)
        out: dict[str, MonitorValue] = {}
        for name in R.MONITOR_ORDER:
            reg = R.MONITOR_REGISTERS[name]
            value, raw = decoded[name]
            out[name] = MonitorValue(
                name=name, label=reg.label, group=reg.group, value=value,
                raw=raw, unit=reg.unit, text=reg.format_value(value, raw),
                bits=reg.bits)
        return out

    def read_progress(self) -> ProcProgress:
        regs = self._ensure().read_holding(R.PROC_STATUS, 10)
        elapsed = (regs[7] << 16) | regs[6]          # 0x0816 低字, 0x0817 高字
        return ProcProgress(
            status=regs[0], current_step=regs[1], total_steps=regs[2],
            progress=regs[3], error_code=regs[4], step_status=regs[5],
            elapsed_ms=elapsed, cmd_count=regs[8], last_cmd=regs[9])

    def read_param_status(self) -> int:
        return self._ensure().read_holding(R.PARAM_STATUS, 1)[0]

    # ---------- 单步命令 ----------
    def start_command(self, command: int, params: dict[str, int] | None = None) -> R.CommandSpec:
        spec = R.COMMAND_BY_CODE.get(int(command))
        if spec is None:
            raise GripperDebugError(f"未知流程命令号 0x{int(command):02X}")
        client = self._ensure()
        progress = self.read_progress()
        if progress.running:
            raise GripperDebugError(
                f"上一条流程仍在执行（状态={progress.text}，"
                f"步骤 {progress.current_step}/{progress.total_steps}），请先中止或等待完成")
        for key, value in (params or {}).items():
            if key not in PARAM_BY_KEY:
                raise GripperDebugError(f"未知参数 {key!r}")
            if value is None:
                continue
            if not (0 <= int(value) <= 65535):
                raise GripperDebugError(f"参数 {key} 越界（0~65535）")
            label, _unit, addr = PARAM_BY_KEY[key]
            client.write_register(addr, int(value))
        client.write_register(R.PROC_CMD, spec.code)
        self._active_step = GripperStep(spec.name, spec.code, dict(params or {}))
        self._active_started = time.monotonic()
        return spec

    def abort_command(self) -> None:
        self.proc_ctrl(R.PROC_CTRL_ABORT)

    def pause_command(self) -> None:
        self.proc_ctrl(R.PROC_CTRL_PAUSE)

    def resume_command(self) -> None:
        self.proc_ctrl(R.PROC_CTRL_RESUME)

    def skip_step(self) -> None:
        self.proc_ctrl(R.PROC_CTRL_SKIP)


# ---------------------------------------------------------------- 序列执行
@dataclass
class SequenceState:
    phase: str = "idle"          # idle/running/paused/done/error/stopped
    index: int = 0
    total: int = 0
    message: str = ""
    progress: ProcProgress | None = None

    @property
    def text(self) -> str:
        mapping = {"idle": "空闲", "running": "执行中", "paused": "已暂停",
                   "done": "完成", "error": "错误", "stopped": "已中止"}
        return mapping.get(self.phase, self.phase)


class GripperSequenceRunner:
    """页内动作序列：逐步下发 PROC_CMD，等完成或固定延时后进入下一步。"""

    def __init__(self, session: GripperSession,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.session = session
        self._clock = clock
        self.sequence: GripperSequence | None = None
        self.state = SequenceState()
        self._step_started: float | None = None
        self._paused_at: float | None = None

    def start(self, sequence: GripperSequence) -> None:
        sequence.validate()
        if not sequence.steps:
            raise GripperDebugError("序列没有步骤")
        if not self.session.connected:
            raise GripperDebugError("未连接夹爪板")
        if self.state.phase in ("running", "paused"):
            raise GripperDebugError("已有序列正在执行，请先中止")
        self.sequence = sequence
        self.state = SequenceState(phase="running", index=0,
                                   total=len(sequence.steps), message="开始执行")
        self._step_started = None
        self._paused_at = None
        self.tick()

    def tick(self) -> SequenceState:
        if self.state.phase != "running" or self.sequence is None:
            return self.state
        step = self.sequence.steps[self.state.index]
        now = self._clock()
        if self._step_started is None:
            try:
                # 每个步骤都先下发命令；wait=delay 只是不检查板端完成，固定延时后下一步
                self.session.start_command(step.command, step.params)
                self._step_started = now
                self.state.message = f"步骤 {self.state.index + 1}/{self.state.total}：{step.name}"
            except Exception as exc:  # noqa: BLE001 - 明确失败, 停在当前步骤
                self._fail(f"步骤 {step.name!r} 下发失败：{exc}")
            return self.state

        elapsed_ms = (now - self._step_started) * 1000.0
        if step.wait == STEP_WAIT_DELAY:
            if elapsed_ms >= step.delay_ms:
                self._advance()
            return self.state

        try:
            progress = self.session.read_progress()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"读取流程状态失败：{exc}")
            return self.state
        self.state.progress = progress
        if progress.status == 2:
            self._advance()
        elif progress.status in (3, 4):
            self._fail(
                f"步骤 {step.name!r} 失败：{progress.text}"
                f"（错误码={progress.error_code}，步骤状态={progress.step_status}）")
        elif elapsed_ms > step.timeout_ms:
            try:
                self.session.abort_command()
            except Exception:  # noqa: BLE001 - 中止失败不掩盖超时
                pass
            self._fail(f"步骤 {step.name!r} 超时（{step.timeout_ms}ms），已请求中止")
        return self.state

    def pause(self) -> SequenceState:
        if self.state.phase == "running":
            try:
                self.session.pause_command()
            except Exception as exc:  # noqa: BLE001
                self.state.message = f"暂停请求失败：{exc}"
                return self.state
            self.state.phase = "paused"
            self._paused_at = self._clock()
            self.state.message = "已暂停（板端流程暂停）"
        return self.state

    def resume(self) -> SequenceState:
        if self.state.phase == "paused":
            try:
                self.session.resume_command()
            except Exception as exc:  # noqa: BLE001
                self.state.message = f"继续请求失败：{exc}"
                return self.state
            self.state.phase = "running"
            if self._paused_at is not None and self._step_started is not None:
                # 暂停期间不计入步骤超时
                self._step_started += self._clock() - self._paused_at
            self._paused_at = None
            self.state.message = "已继续"
        return self.state

    def stop(self) -> SequenceState:
        if self.state.phase in ("running", "paused"):
            try:
                self.session.abort_command()
            except Exception:  # noqa: BLE001
                pass
            self.state.phase = "stopped"
            self.state.message = "已中止"
        return self.state

    def _advance(self) -> None:
        self.state.index += 1
        self._step_started = None
        if self.state.index >= self.state.total:
            self.state.phase = "done"
            self.state.message = "全部步骤完成"
        else:
            self.state.message = "准备下一步"

    def _fail(self, message: str) -> None:
        self.state.phase = "error"
        self.state.message = message


__all__ = [
    "GripperDebugConfig", "GripperDebugError", "GripperDebugStore",
    "GripperSequence", "GripperSequenceRunner", "GripperSession", "GripperStep",
    "MonitorValue", "PARAM_BY_KEY", "PARAM_SPECS", "ProcProgress",
    "SEQUENCE_FILE", "STEP_WAIT_DELAY", "STEP_WAIT_DONE",
    "default_sequences",
]
