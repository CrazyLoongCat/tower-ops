"""配方视觉步骤的「深度分割 → 每框找圆」生产执行路径（DEC-056）。

命名视觉检测若保存了找圆调试参数（``params["circle_debug"]["depth"]``，由视觉
调试页「另存为检测」写入，DEC-047），配方视觉步骤按**与找圆调试页「运行一次」
同一条链**在真实帧上执行：

    深度分割（``run_depth_guidance``）→ 正外接矩形
      → 每个矩形内找圆（cp38 宿主 ``detect_circle_imp``）→ 每框取第一个圆
      → 按检测的判定配置做 PASS / FAIL / NO_TARGET

作业台右卡的框/圆（``vision_layout.compute_initial_layout``，DEC-048）用**同一份
参数、同一套算法**，所以判定用的圆就是操作员在右卡看到的圆；操作员指定定位圆
（DEC-054）与人工框圆（DEC-042）不变。

没有保存找圆调试参数的旧检测**回落整图流水线**（行为不变，不伪造深度框）；
帧缺深度/深度比例、宿主不可用、深度参数非法一律 ERROR——检测明确要求按深度
引导执行时，不拿整图结果冒充。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from application.algorithms.circle.detector import (
    CircleDecisionConfig,
    meets_decision,
)
from application.algorithms.guidance.depth_guidance import DepthGuidanceParams
from application.contracts.enums import DetectionStatus, ImageSpace
from application.contracts.frames import FrameBundle
from application.contracts.results import (
    CirclePrimitive,
    DetectionResult,
    MetricMeasurement,
)
from application.services.guidance_debug import (
    depth_guidance_stage,
    find_circles_in_rects,
)
from application.services.vision_layout import circle_params_from_debug

VISION_MODE = "depth_rects"


def depth_circle_debug(params: Any) -> Optional[Dict[str, Any]]:
    """检测 ``params`` 里可执行的找圆调试参数；没有 ``depth`` 段返回 ``None``。

    只有 ``circle`` 段（或完全没有 ``circle_debug``）的旧检测不算可执行——
    没有深度切分参数就做不出矩形，回落到整图流水线。
    """
    if not isinstance(params, dict):
        return None
    debug = params.get("circle_debug")
    if isinstance(debug, dict) and isinstance(debug.get("depth"), dict):
        return debug
    return None


def run_depth_rects_vision(
    frame: FrameBundle,
    *,
    circle_debug: Dict[str, Any],
    result_id: str,
    decision: Optional[CircleDecisionConfig] = None,
    host_proxy: Any = None,
    pipeline_id: Optional[str] = None,
    pipeline_version: Optional[str] = None,
    host: str = "cp38-native",
    image_space: ImageSpace = ImageSpace.COLOR,
    cancelled: bool = False,
) -> DetectionResult:
    """在真实帧上跑「深度分割 → 每矩形找圆 → 判定」，产出 ``DetectionResult``。

    判定口径（DEC-056）：
    - 无矩形 → NO_TARGET（深度分割没找到候选目标）；
    - 有矩形但一个圆都没找到 → NO_TARGET；
    - 某个矩形内没找到圆 → FAIL（每个矩形对应一个目标，不能当通过）；
    - 找到的圆不满足半径/完整度判据 → FAIL；
    - 全部合格且合格圆数在 ``expected_count_min..max`` → PASS；超出范围 → FAIL。

    不伪造任何圆：宿主缺失、深度缺失、分割异常都如实返回 ERROR/NO_TARGET。
    """
    decision = decision or CircleDecisionConfig()
    circle_params = circle_params_from_debug(circle_debug.get("circle"))
    quality = {"vision_mode": VISION_MODE, "geometry": "circle_debug"}
    base = dict(
        result_id=result_id,
        capture_id=frame.capture_id,
        frame_id=frame.frame_id,
        run_id=frame.run_id,
        station_instance_id=frame.station_instance_id,
        step_id=frame.step_id,
        pipeline_id=pipeline_id,
        pipeline_version=pipeline_version,
        algorithm_version="depth-rects.v1",
        host=host,
        params_hash=circle_params.params_hash(),
        image_space=image_space,
    )

    if cancelled:
        return DetectionResult(status=DetectionStatus.CANCELLED, reason="取消",
                               quality=quality, **base)

    # 与整图流水线同一前置：需要彩色原图（找圆在彩色图上做，坐标为全图像素）。
    if frame.color is None:
        return DetectionResult(status=DetectionStatus.ERROR, reason="缺彩色原图",
                               errors=("no_color",), quality=quality, **base)

    # 质量门：深度有效比例过低 -> INDETERMINATE（不冒充通过，也不当失败）。
    if (decision.min_depth_valid_ratio > 0 and frame.depth_valid_ratio is not None
            and frame.depth_valid_ratio < decision.min_depth_valid_ratio):
        return DetectionResult(
            status=DetectionStatus.INDETERMINATE,
            reason=f"深度有效比例 {frame.depth_valid_ratio:.3f} 低于阈值",
            metrics={"depth_valid_ratio": float(frame.depth_valid_ratio)},
            quality=quality, **base)

    try:
        depth_params = DepthGuidanceParams.from_dict(circle_debug.get("depth") or {})
    except Exception as exc:  # noqa: BLE001 - 检测参数非法: 如实回报, 不猜默认
        return DetectionResult(
            status=DetectionStatus.ERROR,
            reason=f"检测保存的找圆调试深度参数非法: {exc}",
            errors=(str(exc),), quality=quality, **base)

    try:
        stage = depth_guidance_stage(frame, depth_params)
    except Exception as exc:  # noqa: BLE001 - 缺深度/深度比例: 如实回报
        return DetectionResult(
            status=DetectionStatus.ERROR,
            reason=f"深度分割失败: {exc}",
            errors=(str(exc),), quality=quality, **base)

    rects = list(stage.rects or ())
    metrics = {"boxes": float(len(rects)),
               "n_contours": float(stage.n_contours)}
    if not rects:
        return DetectionResult(
            status=DetectionStatus.NO_TARGET,
            reason=f"深度分割未找到候选矩形（过滤后轮廓数={stage.n_contours}）",
            metrics=metrics, quality=quality, **base)

    image = frame.color.data
    try:
        hits, rect_errors = find_circles_in_rects(
            image, rects, circle_params, host_proxy)
    except Exception as exc:  # noqa: BLE001 - 宿主缺失/无图: 如实回报
        return DetectionResult(
            status=DetectionStatus.ERROR,
            reason=f"找圆不可用: {exc}",
            errors=(str(exc),), metrics=metrics, quality=quality, **base)

    # 每个矩形只取第一个圆（与 DEC-048 右卡布局同口径），并按矩形顺序输出。
    by_rect: Dict[int, Any] = {}
    for hit in hits:
        by_rect.setdefault(int(hit.rect_index), hit)
    circles: List[CirclePrimitive] = []
    missing: List[int] = []
    for i, rect in enumerate(rects):
        idx = int(rect.get("idx", i))
        hit = by_rect.get(idx)
        if hit is None:
            missing.append(i)
            continue
        circles.append(CirclePrimitive(
            cx=float(hit.cx), cy=float(hit.cy), radius_px=float(hit.radius),
            image_space=image_space))

    errors = tuple(str(e) for e in rect_errors)
    metrics.update({"boxes_with_circle": float(len(circles)),
                    "boxes_without_circle": float(len(missing)),
                    "candidate_count": float(len(circles))})
    measurements = [MetricMeasurement(name="radius", value=c.radius_px, unit="px")
                    for c in circles]

    if not circles:
        extra = f"；{len(errors)} 条异常见明细" if errors else ""
        return DetectionResult(
            status=DetectionStatus.NO_TARGET,
            reason=f"{len(rects)} 个矩形内均未找到圆{extra}",
            errors=errors, metrics=metrics, quality=quality, **base)

    passing = [c for c in circles if meets_decision(c, decision)]
    metrics["passing_count"] = float(len(passing))

    if missing:
        return DetectionResult(
            status=DetectionStatus.FAIL,
            reason=(f"深度分割 {len(rects)} 个矩形，其中 {len(missing)} 个框内"
                    f"未找到圆"),
            targets=circles, measurements=measurements, metrics=metrics,
            errors=errors, quality=quality, **base)

    if len(passing) < len(circles):
        return DetectionResult(
            status=DetectionStatus.FAIL,
            reason=f"{len(circles) - len(passing)} 个圆不满足半径/完整度判据",
            targets=circles, measurements=measurements, metrics=metrics,
            errors=errors, quality=quality, **base)

    if decision.expected_count_min <= len(passing) <= decision.expected_count_max:
        return DetectionResult(
            status=DetectionStatus.PASS,
            reason=f"深度分割 {len(rects)} 个矩形，框内圆均合格（{len(passing)} 个）",
            targets=passing, measurements=measurements, metrics=metrics,
            errors=errors, quality=quality, **base)

    return DetectionResult(
        status=DetectionStatus.FAIL,
        reason=(f"合格圆数 {len(passing)} 超出期望范围 "
                f"[{decision.expected_count_min},{decision.expected_count_max}]"),
        targets=passing, measurements=measurements, metrics=metrics,
        errors=errors, quality=quality, **base)


__all__ = ["VISION_MODE", "depth_circle_debug", "run_depth_rects_vision"]
