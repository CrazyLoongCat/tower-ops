"""presentation 层: 作业台/手动/视觉/配方/配置/诊断页面与 ViewModel (12 §3, 03).

UI 只持视图状态并发意图, 不持设备句柄、不拼帧、不等待硬件、不运行算法.
"""
from application.presentation.theme import THEME, Theme

__all__ = ["THEME", "Theme"]
