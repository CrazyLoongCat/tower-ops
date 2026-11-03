"""A15 StopCoordinator 测试 (T17/T18/T19/T28 离线)."""
from __future__ import annotations

from application.contracts.enums import StopScope, StopState
from application.services.stop import StopCoordinator, StopTarget

calls: dict[str, int] = {}


def _target(name, verify):
    calls[name] = 0

    def req():
        calls[name] += 1
    return StopTarget(executor_id=name, request_stop=req, verify_stopped=verify)


def test_all_confirmed():
    sc = StopCoordinator()
    sc.register(_target("robot", lambda: StopState.CONFIRMED))
    sc.register(_target("board", lambda: StopState.CONFIRMED))
    s = sc.request_stop(StopScope.STOP_ALL, "terminate")
    assert s.all_confirmed and not s.has_unknown
    assert calls["robot"] == 1 and calls["board"] == 1


def test_unknown_not_all_green():
    sc = StopCoordinator()
    sc.register(_target("robot", lambda: StopState.CONFIRMED))
    sc.register(_target("board", lambda: StopState.UNKNOWN))
    s = sc.request_stop(StopScope.STOP_ALL, "terminate")
    assert not s.all_confirmed and s.has_unknown


def test_verify_exception_is_unknown_not_crash():
    sc = StopCoordinator()

    def boom():
        raise RuntimeError("读状态超时")
    sc.register(_target("robot", boom))
    s = sc.request_stop(StopScope.STOP_ALL, "terminate")
    assert s.executors["robot"].state is StopState.UNKNOWN


def test_request_exception_is_failed():
    sc = StopCoordinator()

    def bad_req():
        raise RuntimeError("链路断了")
    sc.register(StopTarget(executor_id="board", request_stop=bad_req,
                           verify_stopped=lambda: StopState.CONFIRMED))
    s = sc.request_stop(StopScope.STOP_ALL, "terminate")
    assert s.executors["board"].state is StopState.FAILED


def test_scope_task_terminate_subset():
    sc = StopCoordinator()
    sc.register(StopTarget("robot", lambda: None, lambda: StopState.CONFIRMED, task_scoped=True))
    sc.register(StopTarget("s100", lambda: None, lambda: StopState.CONFIRMED, task_scoped=False))
    sc.request_stop(StopScope.TASK_TERMINATE, "terminate")
    assert calls.get("robot") == 1
    assert "s100" not in calls  # 非任务范围不停止


def test_dedupe_same_request_id():
    sc = StopCoordinator()
    sc.register(_target("robot", lambda: StopState.CONFIRMED))
    s1 = sc.request_stop(StopScope.STOP_ALL, "x", request_id="rq1")
    s2 = sc.request_stop(StopScope.STOP_ALL, "x", request_id="rq1")
    assert s1 is s2 and calls["robot"] == 1  # 去重, 不重复下发


def test_freeze_and_clear_failures_do_not_block_stop():
    sc = StopCoordinator(
        freeze_scheduler=lambda: (_ for _ in ()).throw(RuntimeError("bt busy")),
        clear_pending=lambda: (_ for _ in ()).throw(RuntimeError("queue busy")),
    )
    sc.register(_target("robot", lambda: StopState.CONFIRMED))
    s = sc.request_stop(StopScope.STOP_ALL, "x")
    assert s.all_confirmed  # 冻结/清队列异常不阻止停止
