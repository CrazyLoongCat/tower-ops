"""夹爪板（TCR_H1.5_S1.0_UPHandCtr）Modbus TCP 适配层。"""
from application.adapters.gripper_board.modbus_tcp import (
    DEFAULT_PORT,
    ModbusTcpClient,
    ModbusTcpError,
    ModbusTcpTimeout,
)
from . import registers

__all__ = [
    "DEFAULT_PORT", "ModbusTcpClient", "ModbusTcpError", "ModbusTcpTimeout",
    "registers",
]
