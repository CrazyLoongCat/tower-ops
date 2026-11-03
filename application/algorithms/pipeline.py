"""可组合算法流水线 (E14, 02 §8.1/§8.3)。

- 步骤引用**已注册算法**; 未注册即 ERROR (发布前由 E16 五层验证拒绝);
- 每步运行前按 Descriptor 校验输入 (原始深度/dtype/内参/标定);
- 输出统一为 DetectionResult, 各算法质量指标放在 quality 里, **不伪造同种置信度**;
- 需要隔离宿主的算法 (如 circle.detect) 必须注入宿主运行器, 否则明确 ERROR,
  不在主进程假装成功。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from application.algorithms.depth.depth_ops import (
    depth_range_mask, mask_bbox, pixel_to_point_mm,
)
from application.algorithms.line.line_ops import measure_length
from application.algorithms.registry import AlgorithmRegistry, check_frame_for
from application.algorithms.registry import UnknownAlgorithm, default_registry
from application.contracts.enums import DetectionStatus
from application.contracts.frames import FrameBundle
from application.contracts.results import DetectionResult, MetricMeasurement


@dataclass
class PipelineStep:
    algorithm_id: str
    params: dict[str, Any] = field(default_factory=dict)
    # 空目标是否算失败: None=按算法默认; False 表示允许空 (仅记录)
    require_target: bool = True


class AlgorithmPipeline:
    """按序执行算法步骤; 任一步 ERROR 即整体 ERROR (取消语义保留)。"""

    def __init__(self, pipeline_id: str, version: str, steps: list[PipelineStep], *,
                 registry: AlgorithmRegistry | None = None,
                 host_runners: dict[str, Callable[..., Any]] | None = None) -> None:
        self.pipeline_id = pipeline_id
        self.version = version
        self.steps = list(steps)
        self.registry = registry or default_registry()
        self.host_runners = host_runners or {}
        self._inproc = {
            "depth.range_segment": self._depth_range,
            "depth.pixel_to_xyz": self._pixel_xyz,
            "line.measure": self._line_measure,
        }

    # ---------- 运行 ----------
    def run(self, frame: FrameBundle, *, result_id: str,
            cancelled: bool = False) -> DetectionResult:
        base = dict(result_id=result_id, capture_id=frame.capture_id,
                    frame_id=frame.frame_id, run_id=frame.run_id,
                    station_instance_id=frame.station_instance_id,
                    step_id=frame.step_id, pipeline_id=self.pipeline_id,
                    pipeline_version=self.version)
        if cancelled:
            return DetectionResult(status=DetectionStatus.CANCELLED, reason="取消", **base)

        measurements: list[MetricMeasurement] = []
        quality: dict[str, Any] = {}
        no_target = False
        for step in self.steps:
            try:
                desc = self.registry.get(step.algorithm_id)
            except UnknownAlgorithm as exc:
                return DetectionResult(status=DetectionStatus.ERROR,
                                       reason=f"未知算法: {exc}", **base)
            # 输入校验 (原始深度/dtype/内参/标定)
            try:
                check_frame_for(desc, frame)
            except Exception as exc:  # noqa: BLE001
                return DetectionResult(status=DetectionStatus.ERROR,
                                       reason=f"{step.algorithm_id} 输入不满足: {exc}",
                                       **base)
            runner = self._inproc.get(step.algorithm_id)
            if runner is None:
                runner = self.host_runners.get(step.algorithm_id)
            if runner is None:
                return DetectionResult(
                    status=DetectionStatus.ERROR,
                    reason=f"{step.algorithm_id} 需要宿主 {desc.host!r}, 未注入运行器; "
                           f"不在主进程假装成功",
                    **base)
            try:
                out = runner(frame, step)
            except Exception as exc:  # noqa: BLE001
                return DetectionResult(status=DetectionStatus.ERROR,
                                       reason=f"{step.algorithm_id} 执行异常: {exc}",
                                       **base)
            measurements += out.get("measurements", [])
            quality[step.algorithm_id] = out.get("quality", {})
            if out.get("no_target"):
                no_target = True
                if step.require_target:
                    return DetectionResult(status=DetectionStatus.NO_TARGET,
                                           reason=f"{step.algorithm_id} 未检出目标",
                                           measurements=measurements, quality=quality,
                                           **base)

        status = DetectionStatus.NO_TARGET if no_target else DetectionStatus.PASS
        return DetectionResult(status=status, measurements=measurements, quality=quality,
                               **base)

    # ---------- 内置步骤 ----------
    def _depth_range(self, frame: FrameBundle, step: PipelineStep) -> dict:
        p = step.params
        mask = depth_range_mask(frame.depth.data, frame.depth_scale_m,
                                min_mm=float(p["min_mm"]), max_mm=float(p["max_mm"]))
        bbox = mask_bbox(mask)
        ratio = float(mask.sum()) / float(mask.size)
        return {
            "no_target": bbox is None,
            "quality": {"mask_ratio": ratio, "shape": tuple(mask.shape)},
            "measurements": [MetricMeasurement("mask_ratio", ratio, "ratio")],
            "_mask": mask, "_bbox": bbox,
        }

    def _pixel_xyz(self, frame: FrameBundle, step: PipelineStep) -> dict:
        p = step.params
        u, v = int(p["u"]), int(p["v"])
        raw = float(frame.depth.data[v, u])
        intr = frame.intrinsics
        x, y, z = pixel_to_point_mm(u, v, raw, frame.depth_scale_m,
                                    fx=intr.fx, fy=intr.fy, cx=intr.cx, cy=intr.cy)
        return {
            "no_target": False,
            "quality": {"depth_raw": raw},
            "measurements": [MetricMeasurement("x", x, "mm"),
                             MetricMeasurement("y", y, "mm"),
                             MetricMeasurement("z", z, "mm")],
        }

    def _line_measure(self, frame: FrameBundle, step: PipelineStep) -> dict:
        p = step.params
        pts = p["points"]
        p1, p2 = pts[0], pts[1]
        mid_u = (float(p1[0]) + float(p2[0])) / 2.0
        mid_v = (float(p1[1]) + float(p2[1])) / 2.0
        depth_raw = None
        if frame.depth is not None:
            depth_raw = float(frame.depth.data[int(mid_v), int(mid_u)])
        fx = frame.intrinsics.fx if frame.intrinsics is not None else None
        depth_ok = (frame.depth is not None and frame.depth_scale_m and fx)
        m = measure_length(p1, p2,
                           depth_raw=depth_raw if depth_ok else None,
                           depth_scale_m=frame.depth_scale_m if depth_ok else None,
                           fx=fx if depth_ok else None)
        measurements = [MetricMeasurement("length", m["length_px"], "px")]
        if m["length_mm"] is not None:
            measurements.append(MetricMeasurement("length", m["length_mm"], "mm"))
        return {"no_target": False,
                "quality": {"unit_note": m["unit_note"],
                            "depth_available": bool(depth_ok)},
                "measurements": measurements}


__all__ = ["AlgorithmPipeline", "PipelineStep"]
