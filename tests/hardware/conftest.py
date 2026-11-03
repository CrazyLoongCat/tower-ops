"""获准硬件测试入口 (12 §4.4, A24)。

默认不被离线测试自动收集/执行 (见 pyproject norecursedirs)。
显式传入 --run-hardware 时才执行；真实设备动作仍须按 A20/D1-SCOPE 完成范围与保护审查。
"""
from __future__ import annotations

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-hardware", action="store_true", default=False,
                     help="显式启用硬件测试")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-hardware"):
        return
    skip = pytest.mark.skip(
        reason="硬件测试默认不执行; 需 --run-hardware（并先完成 A20/D1-SCOPE）")
    for item in items:
        item.add_marker(skip)
