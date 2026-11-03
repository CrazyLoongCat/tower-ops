"""夹爪板 Modbus 寄存器表（调试页用子集）。

数据来源：`夹爪板寄存器定义表.xlsx` V1.0（2026-10-26），
板端正本：TCR_H1.5_S1.0_UPHandCtr / Core/Inc/registers.h、Src/registers.c。

本模块只收录「夹爪调试」页需要读写的寄存器与流程命令；完整 490 条仍是
xlsx 的 SSOT，后续如做全量仪表/配方动作，应由 xlsx 生成机器可读表，不要
在这里逐条手抄。地址均为 **PDU 地址（0 基）**，不是 4xxxx 表示法。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------- 系统/控制地址
DEV_TYPE = 0x0000
FW_VER = 0x0001
HW_VER = 0x0002
DEV_ADDR = 0x0003
SYS_STATUS = 0x0004
SYS_ERROR = 0x0005
SYS_ERROR2 = 0x0006
LAST_ERROR = 0x0007
BUS_VOLTAGE = 0x0008
PCB_TEMP = 0x0009
SYS_CTRL = 0x0010
SYS_MODE = 0x0011
SYS_CMD = 0x0012
SYS_CMD_STATUS = 0x0013
IWDG_TIMEOUT = 0x0014
DI_STATE = 0x0040
MB_RX_CNT = 0x0080
MB_TX_CNT = 0x0081
MB_CRC_ERR = 0x0082
MB_TIMEOUT_CNT = 0x0083
MB_LAST_ADDR = 0x0084
MB_EXCEPTION = 0x0085
HEARTBEAT = 0x008A
HEARTBEAT_TO = 0x008B
PROC_CMD = 0x0800
PROC_PARAM1 = 0x0801
PROC_PARAM2 = 0x0802
PROC_PARAM3 = 0x0803
PROC_PARAM4 = 0x0804
PROC_JJ_POS = 0x0805
PROC_SK_POS = 0x0806
PROC_VMOD = 0x0807
PROC_STATUS = 0x0810
PROC_CUR_STEP = 0x0811
PROC_TOTAL_STEPS = 0x0812
PROC_PROGRESS = 0x0813
PROC_ERROR_CODE = 0x0814
PROC_STEP_STATUS = 0x0815
PROC_ELAPSED = 0x0816
PROC_CMD_CNT = 0x0818
PROC_LAST_CMD = 0x0819
PROC_STEP_DETAIL = 0x081A
PROC_CTRL = 0x0820
PROC_MODE = 0x0821
PARAM_SAVE = 0x0A00
PARAM_LOAD = 0x0A01
PARAM_RESET = 0x0A02
PARAM_STATUS = 0x0A03
PARAM_CRC = 0x0A04
PARAM_DEF_CRC = 0x0A05
PARAM_SAVE_CNT = 0x0A06

PARAM_SAVE_MAGIC = 0xA5A5
PARAM_LOAD_MAGIC = 0x5A5A
PARAM_RESET_MAGIC = 0xDEAD

# 系统控制字位（写 1 触发）
SYS_CTRL_ENABLE = 1 << 0
SYS_CTRL_ESTOP = 1 << 1
SYS_CTRL_CLEAR_ERROR = 1 << 2
SYS_CTRL_RESET = 1 << 3
SYS_CTRL_CLEAR_RUNTIME = 1 << 4
SYS_CTRL_CLEAR_PEAK = 1 << 5

# 流程控制字位（写 1 触发）
PROC_CTRL_ABORT = 1 << 0
PROC_CTRL_PAUSE = 1 << 1
PROC_CTRL_RESUME = 1 << 2
PROC_CTRL_SKIP = 1 << 3

SYS_MODES = {0: "待机", 1: "手动", 2: "自动", 3: "标定"}
PROC_MODES = {0: "单步", 1: "连续"}
PROC_STATUS_TEXT = {0: "空闲", 1: "执行中", 2: "完成", 3: "错误", 4: "中止"}

RegisterKind = Literal["u16", "i16", "u32", "i32", "bit", "hex"]


@dataclass(frozen=True)
class RegisterDef:
    """一个监视/控制寄存器的显示与解码定义。"""

    name: str
    address: int
    kind: RegisterKind = "u16"
    label: str = ""
    group: str = ""
    unit: str = ""
    scale: float = 0.0          # 0 = 不换算
    precision: int = 0
    bits: str = ""              # 位定义（仅展示）
    writable: bool = False

    @property
    def words(self) -> int:
        return 2 if self.kind in ("u32", "i32") else 1

    def decode_raw(self, registers: list[int], offset: int = 0) -> int:
        if self.kind in ("u32", "i32"):
            low = registers[offset] & 0xFFFF
            high = registers[offset + 1] & 0xFFFF
            raw = (high << 16) | low
            if self.kind == "i32" and raw & 0x80000000:
                raw -= 0x100000000
            return raw
        raw = registers[offset] & 0xFFFF
        if self.kind == "i16" and raw & 0x8000:
            raw -= 0x10000
        return raw

    def decode(self, registers: list[int], offset: int = 0) -> tuple[float | int, int]:
        """返回 (显示值, 原始值)。显示值按 Scale 换算。"""
        raw = self.decode_raw(registers, offset)
        value = raw * self.scale if self.scale else raw
        return value, raw

    def format_value(self, value: float | int, raw: int | None = None) -> str:
        if self.kind == "hex":
            return f"0x{int(value) & 0xFFFF:04X}"
        if self.scale:
            text = f"{float(value):.{self.precision}f}"
        else:
            text = str(int(value))
        if self.unit:
            text += f" {self.unit}"
        return text


def _reg(name: str, address: int, kind: RegisterKind = "u16", *,
         label: str = "", group: str = "", unit: str = "", scale: float = 0.0,
         precision: int = 0, bits: str = "", writable: bool = False) -> RegisterDef:
    return RegisterDef(name=name, address=address, kind=kind,
                       label=label or name, group=group, unit=unit,
                       scale=scale, precision=precision, bits=bits,
                       writable=writable)


# ---------------------------------------------------------------- 监视表
MONITOR_REGISTERS: dict[str, RegisterDef] = {r.name: r for r in (
    _reg("DEV_TYPE", DEV_TYPE, "hex", label="设备类型", group="系统"),
    _reg("FW_VER", FW_VER, "hex", label="固件版本", group="系统"),
    _reg("HW_VER", HW_VER, "hex", label="硬件版本", group="系统"),
    _reg("SYS_STATUS", SYS_STATUS, "bit", label="系统状态字", group="系统",
         bits="bit0=使能 bit3=急停（其余待确认）"),
    _reg("SYS_ERROR", SYS_ERROR, "bit", label="错误字1", group="系统"),
    _reg("SYS_ERROR2", SYS_ERROR2, "bit", label="错误字2", group="系统"),
    _reg("LAST_ERROR", LAST_ERROR, label="最近错误码", group="系统"),
    _reg("BUS_VOLTAGE", BUS_VOLTAGE, unit="V", scale=0.1, precision=1,
         label="母线电压", group="系统"),
    _reg("PCB_TEMP", PCB_TEMP, "i16", unit="°C", scale=0.1, precision=1,
         label="PCB 温度", group="系统"),
    _reg("DI_STATE", DI_STATE, "bit", label="开关状态", group="数字 IO",
         bits="bit0~3=SW1~SW4（1=闭合）"),
    _reg("MB_RX_CNT", MB_RX_CNT, label="Modbus 接收计数", group="通信", unit="帧"),
    _reg("MB_TX_CNT", MB_TX_CNT, label="Modbus 发送计数", group="通信", unit="帧"),
    _reg("MB_CRC_ERR", MB_CRC_ERR, label="CRC 错误计数", group="通信", unit="次"),
    _reg("MB_TIMEOUT_CNT", MB_TIMEOUT_CNT, label="超时计数", group="通信", unit="次"),
    _reg("MB_EXCEPTION", MB_EXCEPTION, label="异常响应计数", group="通信", unit="次"),
    _reg("HEARTBEAT", HEARTBEAT, label="心跳计数（上位机写）", group="通信"),
    _reg("HEARTBEAT_TO", HEARTBEAT_TO, unit="ms", label="心跳超时", group="通信",
         writable=True),
    _reg("PROC_STATUS", PROC_STATUS, label="流程状态", group="流程"),
    _reg("PROC_CUR_STEP", PROC_CUR_STEP, label="当前步骤", group="流程", unit="步"),
    _reg("PROC_TOTAL_STEPS", PROC_TOTAL_STEPS, label="总步骤数", group="流程", unit="步"),
    _reg("PROC_PROGRESS", PROC_PROGRESS, label="进度", group="流程", unit="%"),
    _reg("PROC_ERROR_CODE", PROC_ERROR_CODE, label="流程错误码", group="流程"),
    _reg("PROC_STEP_STATUS", PROC_STEP_STATUS, label="步骤状态", group="流程"),
    _reg("PROC_ELAPSED", PROC_ELAPSED, "u32", unit="ms", label="已耗时", group="流程"),
    _reg("PROC_CMD_CNT", PROC_CMD_CNT, label="累计执行次数", group="流程", unit="次"),
    _reg("PROC_LAST_CMD", PROC_LAST_CMD, label="上次命令号", group="流程"),
    _reg("JZ_JJ_POS", 0x0900, unit="mm", scale=0.01, precision=2,
         label="夹紧位", group="机构"),
    _reg("JZ_SK_POS", 0x0901, unit="mm", scale=0.01, precision=2,
         label="松开位", group="机构"),
    _reg("JZ_CUR_POS", 0x0902, unit="mm", scale=0.01, precision=2,
         label="夹杆当前位置", group="机构"),
    _reg("JZ_LEFT_POS", 0x0903, unit="mm", scale=0.01, precision=2,
         label="左夹爪位置", group="机构"),
    _reg("JZ_RIGHT_POS", 0x0904, unit="mm", scale=0.01, precision=2,
         label="右夹爪位置", group="机构"),
    _reg("VMOD_POS", 0x0905, unit="mm", scale=0.01, precision=2,
         label="V 块当前位置", group="机构"),
    _reg("VMOD_TARGET", 0x0906, unit="mm", scale=0.01, precision=2,
         label="V 块目标位置", group="机构"),
    _reg("FY_POS", 0x0907, unit="°", scale=0.01, precision=2,
         label="俯仰角度", group="机构"),
    _reg("FY_TARGET", 0x0908, unit="°", scale=0.01, precision=2,
         label="俯仰目标", group="机构"),
    _reg("FORCE1_KG", 0x0909, unit="kg", scale=0.01, precision=2,
         label="左拉力", group="机构"),
    _reg("FORCE2_KG", 0x090A, unit="kg", scale=0.01, precision=2,
         label="右拉力", group="机构"),
    _reg("HAND_STATUS", 0x090B, "bit", label="夹爪状态字", group="机构",
         bits="bit0=V块伸出 bit1=V块缩回 bit2=夹杆出 bit3=夹杆回 "
              "bit4=夹紧 bit5=松开 bit6=仰 bit7=俯 bit8~13=对应到位 "
              "bit14=开中 bit15=关中"),
    _reg("HAND_STATUS2", 0x090C, "bit", label="扩展状态字", group="机构"),
    _reg("PARAM_STATUS", PARAM_STATUS, label="存储状态", group="存储"),
    _reg("PARAM_CRC", PARAM_CRC, "hex", label="当前参数 CRC", group="存储"),
    _reg("PARAM_DEF_CRC", PARAM_DEF_CRC, "hex", label="默认参数 CRC", group="存储"),
    _reg("PARAM_SAVE_CNT", PARAM_SAVE_CNT, label="保存次数", group="存储", unit="次"),
)}

MONITOR_ORDER: tuple[str, ...] = tuple(MONITOR_REGISTERS)

# 读取分块：一次 FC03 读一段，减少往返；每块 <= 125 寄存器。
MONITOR_BLOCKS: tuple[tuple[int, int], ...] = (
    (0x0000, 0x0015),   # 0x0000~0x0014
    (0x0040, 0x0002),   # 0x0040~0x0041
    (0x0080, 0x000C),   # 0x0080~0x008B
    (0x0810, 0x000B),   # 0x0810~0x081A
    (0x0900, 0x000D),   # 0x0900~0x090C（含扩展状态字）
    (0x0A03, 0x0004),   # 0x0A03~0x0A06
)


def decode_monitor(blocks: dict[int, list[int]]) -> dict[str, tuple[float | int, int]]:
    """按 MONITOR_REGISTERS 解码一组分块读取结果。"""
    out: dict[str, tuple[float | int, int]] = {}
    for name, reg in MONITOR_REGISTERS.items():
        for start, data in blocks.items():
            if start <= reg.address and reg.address + reg.words <= start + len(data):
                out[name] = reg.decode(data, reg.address - start)
                break
    return out


# ---------------------------------------------------------------- 流程命令
@dataclass(frozen=True)
class CommandSpec:
    code: int
    name: str
    label: str
    group: str
    description: str = ""
    requires_confirm: bool = False


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(0x01, "HAND_OPEN", "夹爪打开（全流程）", "夹爪", "打开到松开位"),
    CommandSpec(0x02, "HAND_CLOSE", "夹爪夹紧（全流程）", "夹爪", "夹紧到夹紧位"),
    CommandSpec(0x03, "HAND_MICRO", "夹爪微张", "夹爪"),
    CommandSpec(0x04, "HAND_FULLOPEN", "夹爪全开（避障）", "夹爪"),
    CommandSpec(0x10, "VMOD_EXTEND", "V 型块伸出", "V 块"),
    CommandSpec(0x11, "VMOD_RETRACT", "V 型块缩回", "V 块"),
    CommandSpec(0x20, "LJZ_CLAMP", "左夹爪夹紧", "左夹爪"),
    CommandSpec(0x21, "LJZ_OPEN", "左夹爪打开", "左夹爪"),
    CommandSpec(0x22, "RJZ_CLAMP", "右夹爪夹紧", "右夹爪"),
    CommandSpec(0x23, "RJZ_OPEN", "右夹爪打开", "右夹爪"),
    CommandSpec(0x30, "JG_EXTEND", "共轴夹杆伸出", "共轴夹杆"),
    CommandSpec(0x31, "JG_RETRACT", "共轴夹杆缩回", "共轴夹杆"),
    CommandSpec(0x40, "FY_UP", "俯仰上抬", "俯仰"),
    CommandSpec(0x41, "FY_DOWN", "俯仰下压", "俯仰"),
    CommandSpec(0x42, "FY_STOP", "俯仰停止", "俯仰"),
    CommandSpec(0x50, "CAL_ZERO", "标定零点", "标定",
                 "板端标定命令，可能改写零点", True),
    CommandSpec(0x51, "CAL_TRAVEL", "标定行程", "标定",
                 "板端标定命令，可能改写行程", True),
    CommandSpec(0x60, "LNK_JG_OPEN", "联动松开夹杆", "联动"),
    CommandSpec(0x61, "LNK_JG_CLOSE", "联动夹紧夹杆", "联动"),
    CommandSpec(0x62, "LNK_VMOD_AVOID", "联动 V 块避让", "联动"),
    CommandSpec(0x70, "STEP_VMOD", "V 型块单步", "单步"),
    CommandSpec(0x71, "STEP_LJZ", "夹爪电机单步", "单步"),
    CommandSpec(0x72, "STEP_JG", "夹杆单步", "单步"),
    CommandSpec(0x73, "STEP_FY", "俯仰单步", "单步"),
    CommandSpec(0x74, "STEP_FY_STOP", "俯仰停止（单步）", "单步"),
)
COMMAND_BY_CODE = {c.code: c for c in COMMANDS}
COMMAND_GROUPS: tuple[str, ...] = tuple(dict.fromkeys(c.group for c in COMMANDS))

# 可由「单步调试 / 动作序列」写的参数字段：界面名 -> 地址
PARAM_FIELDS: tuple[tuple[str, int, str], ...] = (
    ("参数1", PROC_PARAM1, ""),
    ("参数2", PROC_PARAM2, ""),
    ("参数3", PROC_PARAM3, ""),
    ("参数4", PROC_PARAM4, ""),
    ("夹紧位", PROC_JJ_POS, "mm"),
    ("松开位", PROC_SK_POS, "mm"),
    ("V 块目标", PROC_VMOD, "mm"),
)
PARAM_BY_ADDRESS = {addr: (label, unit) for label, addr, unit in PARAM_FIELDS}
