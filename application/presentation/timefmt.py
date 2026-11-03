"""界面时间显示（DEC-038）：事件库存的是 UTC，界面统一显示**北京时间**（UTC+8）。

存储不动：事件/审计表里的 ``utc`` 字段仍按 UTC 写入（排序与机器时区无关，是真值）；
只在**显示**时换算。本模块不依赖 Qt，页面把它用在表格/标签文案里。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 北京时间 = UTC+8（中国无夏令时，用固定偏移，不依赖机器时区设置）
BEIJING_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")

_FMT = "%Y-%m-%d %H:%M:%S"


def to_beijing_text(value: object) -> str:
    """把事件里的 UTC 时间串换成北京时间文本 ``YYYY-MM-DD HH:MM:SS``。

    - 带时区（``+00:00`` / ``Z``）按所带时区换算；
    - 不带时区的旧数据按 UTC 处理（本工程事件从写入起就带 UTC 时区）；
    - 空值返回空串；解析失败原样返回（宁可显示原文，也不吞掉/编造时间）。
    """
    if not value:
        return ""
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BEIJING_TZ).strftime(_FMT)


__all__ = ["BEIJING_TZ", "to_beijing_text"]
