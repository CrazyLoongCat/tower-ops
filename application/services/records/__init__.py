"""records 子包: 记录、报告、留存与回放 (12 §3, 02 §11, E25)。"""
from application.services.records.replay import (
    RefusingBoard,
    RefusingBoardRegistry,
    RefusingRobot,
    ReplayStack,
    build_replay_stack,
)
from application.services.records.reports import (
    RunReport,
    build_run_report,
    export_report,
    render_markdown,
)
from application.services.records.service import (
    RecordService,
    StorageBackpressure,
)

__all__ = [
    "RecordService", "RefusingBoard", "RefusingBoardRegistry", "RefusingRobot",
    "ReplayStack", "RunReport", "StorageBackpressure", "build_replay_stack",
    "build_run_report", "export_report", "render_markdown",
]
