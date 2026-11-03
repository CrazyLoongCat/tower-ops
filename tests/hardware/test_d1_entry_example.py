"""D1 单设备入口骨架 (A24). 默认被 conftest 跳过; 不自动连接任何设备.

真实使用时: 由现场人员按已批准范围补全命令与保护条件, 并回填原始记录。
"""
from __future__ import annotations

import os


def _require(env_key: str) -> str:
    value = os.environ.get(env_key)
    if not value:
        raise RuntimeError(f"缺少现场参数 {env_key}; 未获授权不得连接设备")
    return value


def test_d1_link_identity_readonly_placeholder():
    """占位: 只读链路身份核对. 未实现真实连接; 需 INPUT-LINK/INPUT-FW confirmed。"""
    # 现场步骤(待批准): 打开串口 -> 读身份 -> 记录 -> 关闭; 不下发任何动作
    _require("JT_D1_PORT")  # 未提供端口即失败, 防止误连
    raise AssertionError("D1 骨架未实现; 需在获准范围内补全后再运行")


def test_d1_movej_single_waypoint_placeholder():
    """占位: 单示教点 movej. 需 INPUT-PATH confirmed + D1-SCOPE:approved。"""
    _require("JT_D1_ROBOT_IP")
    raise AssertionError("D1 骨架未实现; 未获授权不得下发运动")
