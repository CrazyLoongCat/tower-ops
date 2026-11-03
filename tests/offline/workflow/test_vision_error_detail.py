"""DEC-052: 视觉判定失败必须带**真实原因与来源**，不再只报一句「视觉 ERROR」。

现场表现：DS86 抓拍后视觉判定步骤失败，日志只有「视觉ERROR（取消依赖步骤）」，
宿主未配置/宿主崩溃/输入不满足等原因全被节点吞掉，无法定位。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from py_trees.common import Status

from application.bootstrap.composition import build_offline_stack
from application.contracts.context import RunContext, VersionSnapshot
from application.contracts.enums import DetectionStatus, TaskState
from application.contracts.results import DetectionResult
from application.workflow.nodes.base import OutputStore
from application.workflow.nodes.steps import VisionRunNode

RECIPE = "configs/recipes/ds86_two_station_example.yaml"


class _FakeExecutor:
    """只提供 VisionRunNode 需要的执行器表面（不接真实执行链）。"""

    ledger = None
    vision = SimpleNamespace(pipeline_id="approved_ds86_circle_pipeline")

    def __init__(self, result):
        self._result = result

    def run_vision(self, ctx, frame, *, result_id):  # noqa: ANN001
        return self._result


def _frame():
    return SimpleNamespace(frame_id="frame-77", camera_id="ds86_01", capture_id="cap-9")


def _node(result):
    ctx = RunContext(run_id="r-err", versions=VersionSnapshot("x", "v0", "-", "-"))
    outs = OutputStore()
    outs.set(ctx.station_instance_id or "", "cap", "frame", _frame())
    return VisionRunNode("vision:detect", pipeline_id=None, input_step="cap",
                         step_id="detect", ctx=ctx, executor=_FakeExecutor(result),
                         outputs=outs)


def _error_result() -> DetectionResult:
    return DetectionResult(
        result_id="r-err:detect:it0:1", status=DetectionStatus.ERROR,
        reason=("圆检测后端异常: 宿主进程不可用 (退出码=3)；"
                "stderr尾部: [circle38] 原生加载失败: DLL load failed"),
        errors=("WindowsError: DLL load failed",),
        pipeline_id="approved_ds86_circle_pipeline",
        pipeline_version="production-unconfirmed", frame_id="frame-77")


def test_vision_error_reason_is_surfaced(caplog):
    """失败原因要含底层 reason/错误明细/流水线/帧/相机，且同一条打 ERROR 日志。"""
    node = _node(_error_result())
    with caplog.at_level(logging.ERROR, logger="towerops.workflow.vision"):
        assert node.update() is Status.FAILURE
    reason = node.failure_reason or ""
    assert "视觉判定失败" in reason
    assert "圆检测后端异常" in reason and "DLL load failed" in reason
    assert "approved_ds86_circle_pipeline" in reason
    assert "frame-77" in reason and "ds86_01" in reason
    assert "依赖步骤已取消" in reason

    recs = [r for r in caplog.records if r.name == "towerops.workflow.vision"]
    assert recs and recs[0].levelno == logging.ERROR
    assert "DLL load failed" in recs[0].getMessage()
    assert getattr(recs[0], "step_id", None) == "detect"   # 结构化字段可过滤
    assert getattr(recs[0], "run_id", None) == "r-err"


def test_vision_cancelled_is_described():
    """取消也要说明来源，不再只有两个字「视觉取消」。"""
    result = DetectionResult(
        result_id="r-err:detect:it0:1", status=DetectionStatus.CANCELLED,
        reason="取消", pipeline_id="approved_ds86_circle_pipeline",
        pipeline_version="production-unconfirmed", frame_id="frame-77")
    node = _node(result)
    assert node.update() is Status.FAILURE
    reason = node.failure_reason or ""
    assert "视觉判定被取消" in reason and "frame-77" in reason
    assert "依赖步骤已取消" not in reason          # 自己就是被取消，不重复说


class _ErrorVision:
    """离线栈里替换掉圆流水线：固定返回 ERROR（模拟宿主不可用）。"""

    pipeline_id = "approved_ds86_circle_pipeline"
    pipeline_version = "production-unconfirmed"
    spec = None

    def run(self, frame, *, result_id, cancelled=False):  # noqa: ANN001
        return DetectionResult(
            result_id=result_id, status=DetectionStatus.ERROR,
            reason=("圆检测后端异常: 真实圆宿主未配置（--circle-host-cmd）；"
                    "视觉判定不可用。未配置时不给假结果。"),
            errors=("真实圆宿主未配置",),
            pipeline_id=self.pipeline_id, pipeline_version=self.pipeline_version,
            frame_id=frame.frame_id, capture_id=frame.capture_id,
            run_id=frame.run_id, step_id=frame.step_id,
            station_instance_id=frame.station_instance_id)


def test_task_fault_event_carries_vision_detail(tmp_path):
    """作业台日志/诊断页看到的 task.fault 也要带明细，而不是一句「视觉ERROR」。"""
    stack = build_offline_stack(db_path=str(tmp_path / "r.db"),
                                runtime_dir=str(tmp_path), recipe_path=RECIPE)
    try:
        stack.recipes.draft({
            "recipe_id": "vision_err_case", "version": "v1", "state": "draft",
            "sequence": [
                {"id": "cap", "type": "camera.capture", "camera_ref": "ds86_01"},
                {"id": "det", "type": "vision.run",
                 "input_from": {"step": "cap", "output": "frame"},
                 "pipeline_ref": "approved_ds86_circle_pipeline"},
            ],
        })
        stack.executor.vision = _ErrorVision()
        stack.task.start("vision_err_case", allow_draft=True)
        run_id = stack.task.state_machine.run_id
        state = None
        for _ in range(300):
            state = stack.task.tick()
            if state is TaskState.FAULT:
                break
        assert state is TaskState.FAULT
        faults = [e for e in stack.records.replay(run_id)
                  if e["event_type"] == "task.fault"]
        assert faults, "失败必须留 task.fault 事件"
        msg = faults[-1]["message"]
        assert "视觉判定失败" in msg
        assert "真实圆宿主未配置" in msg and "--circle-host-cmd" in msg
        assert "approved_ds86_circle_pipeline" in msg
        assert "ds86_01" in msg and "依赖步骤已取消" in msg
    finally:
        stack.close()
