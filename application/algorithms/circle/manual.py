"""手动微调检测圆的判定 (DEC-042)。

作业台在弹出的步骤确认框里允许操作者拖动检测圆；**调整完毕后结果以手动圆为准**。
本模块把"一个手动圆 + 流水线判定阈值"折算成 PASS/FAIL，规则与
:class:`~application.algorithms.circle.detector.CircleDetector` 一致:

- 半径必须在 ``[radius_min_px, radius_max_px]`` 内；
- 手动圆无覆盖率数据（``coverage=None``），不伪造覆盖率判据；
- 手动圆数恒为 1，必须落在期望圆数范围内。

不生成视觉运动目标，只改判定结果；调用方负责留证（记录自动圆与手动圆）。
"""
from __future__ import annotations

from application.algorithms.circle.detector import CircleDecisionConfig
from application.contracts.enums import DetectionStatus
from application.contracts.results import CirclePrimitive


def decide_manual_circle(circle: CirclePrimitive,
                         decision: CircleDecisionConfig) -> tuple[DetectionStatus, str]:
    """按流水线阈值判定单个手动圆，返回 (状态, 可读原因)。"""
    r = float(circle.radius_px)
    if not (decision.radius_min_px <= r <= decision.radius_max_px):
        return (DetectionStatus.FAIL,
                f"手动圆半径 {r:.1f}px 不在允许范围 "
                f"[{decision.radius_min_px:.1f}, {decision.radius_max_px:.1f}]px")
    if not (decision.expected_count_min <= 1 <= decision.expected_count_max):
        return (DetectionStatus.FAIL,
                f"手动圆数 1 超出期望范围 "
                f"[{decision.expected_count_min}, {decision.expected_count_max}]")
    return (DetectionStatus.PASS,
            f"手动检测圆：圆心 ({circle.cx:.0f}, {circle.cy:.0f}) "
            f"半径 {r:.1f}px 满足判据")


__all__ = ["decide_manual_circle"]
