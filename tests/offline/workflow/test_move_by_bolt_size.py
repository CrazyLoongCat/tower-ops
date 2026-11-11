"""robot.move_by_bolt_size（DEC-155）：按全局参数螺栓直径选运动方案路点。

覆盖：21.7mm 就近取 20 → 只执行映射路点（1-based 转 0-based）；可选"先到路点"
先执行；未配置映射 / 未接入全局参数存储 → 明确失败；输出 bolt_size 供判断留证。
"""
from __future__ import annotations

import pytest
from py_trees.common import Status

from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import NodeType
from application.services.recipes.models import StepModel
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.steps import MoveByBoltSizeNode, build_node


class FakeMotion:
    busy = False
    state = {"mode": "idle"}

    def __init__(self):
        self.calls: list = []

    def start_selected(self, plan_id, indices):
        self.calls.append((plan_id, list(indices)))
        self.state = {"mode": "playing"}
        return True


class FakeStore:
    def __init__(self, value):
        self.value_mm = value

    def value(self, op):
        return self.value_mm


def _ctx():
    return RunContext(run_id="r1", versions=VersionSnapshot("t", "v", "-", "-"))


class _Exec:
    ledger = None
    records = None

    def __init__(self, store):
        self.operation_config = store


def _step(**over):
    base = dict(id="s2", type=NodeType.ROBOT_MOVE_BY_BOLT_SIZE,
                motion_plan_ref="planA",
                bolt_size_waypoints={"16": 1, "20": 2, "24": 3})
    base.update(over)
    return StepModel(**base)


def test_selects_waypoint_by_nearest_bolt_size():
    fm = FakeMotion()
    node = build_node(_step(), ctx=_ctx(), executor=_Exec(FakeStore(21.7)),
                      outputs=OutputStore(), motion=fm)
    assert isinstance(node, MoveByBoltSizeNode)
    node.initialise()
    assert node.update() is Status.RUNNING
    assert fm.calls == [("planA", [1])]          # 20 → 路点 2（0-based 1）
    fm.state = {"mode": "idle"}
    assert node.update() is Status.SUCCESS


def test_via_waypoint_runs_first_then_selected():
    fm = FakeMotion()
    node = build_node(_step(bolt_size_via_waypoint=2), ctx=_ctx(),
                      executor=_Exec(FakeStore(23.4)),
                      outputs=OutputStore(), motion=fm)
    node.initialise()
    assert node.update() is Status.RUNNING
    assert fm.calls == [("planA", [1, 2])]       # 先到#2，再到#3（24mm）


def test_missing_mapping_fails_clearly():
    node = build_node(_step(bolt_size_waypoints={"16": 1}), ctx=_ctx(),
                      executor=_Exec(FakeStore(20.0)),
                      outputs=OutputStore(), motion=FakeMotion())
    node.initialise()
    assert node.update() is Status.FAILURE
    assert "未在本步配置对应路点" in (node.failure_reason or "")


def test_missing_store_fails_clearly():
    node = build_node(_step(), ctx=_ctx(), executor=_Exec(None),
                      outputs=OutputStore(), motion=FakeMotion())
    node.initialise()
    assert node.update() is Status.FAILURE
    assert "全局参数存储" in (node.failure_reason or "")


def test_records_bolt_size_output():
    outs = OutputStore()
    node = build_node(_step(), ctx=_ctx(), executor=_Exec(FakeStore(21.7)),
                      outputs=outs, motion=FakeMotion())
    node.initialise()
    node.update()
    assert outs.get("", "s2", "bolt_size") == 20
