"""界面时间显示（DEC-038）：事件库存 UTC，界面统一显示北京时间。纯函数，不依赖 Qt。"""
from __future__ import annotations

from application.presentation.timefmt import to_beijing_text


def test_utc_is_converted_to_beijing_text():
    # 事件库真实格式（isoformat 带微秒与时区）
    assert to_beijing_text("2026-10-17T13:20:00.123456+00:00") == "2026-10-17 21:20:00"


def test_z_suffix_and_cross_day():
    assert to_beijing_text("2026-10-17T18:30:05Z") == "2026-10-18 02:30:05"


def test_naive_text_is_treated_as_utc():
    # 老数据没带时区：按 UTC 处理，不假装是本机时间
    assert to_beijing_text("2026-10-17T13:20:00") == "2026-10-17 21:20:00"


def test_empty_and_unparsable_pass_through():
    assert to_beijing_text(None) == ""
    assert to_beijing_text("") == ""
    assert to_beijing_text("not-a-time") == "not-a-time"   # 原文返回，不吞不编
