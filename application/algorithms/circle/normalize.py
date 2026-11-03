"""圆检测原生返回规范化 (12 §4.1, 02 §8.1/§8.2, T14/T16/T21/T38).

把 cp38 detect_circle 的嵌套返回规整为项目图元; 畸形返回/空目标/缩放/ROI 只转换一次.
原生坐标已是全图坐标 (见 detect_circle.py 注释), 默认不再二次映射.

``circles_for_show`` 的包裹层数随调用/原生版本而变（DEC-054）：空结果可能是
``[]``、``[[]]`` 或 ``[[[]]]``，按图/按矩形分组时还会多包一层。空占位一律跳过，
**不是畸形**；只有真正像圆却存不下的条目（长度/数值非法）才抛
``NativeResultError``。``circles_for_show`` 里没有有效圆时回退
``circle_centers``/``radius``（与旧工程 ``_extract_circles`` 同口径）。

DEC-055：**整帧返回可能整体多包一层**（宿主曾把"不限区域"当成一个空矩形送进
原生，现场 ``circles_for_show=[[[]]]`` 同时 ``circle_centers``/``radius`` 也深一层）。
因此 ``circle_centers``/``radius``/``circle_num`` 一律按任意层数递归展开；
畸形时报错附带原生真实形状摘要，现场不用再猜包裹层数。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from application.contracts.enums import ImageSpace
from application.contracts.results import CirclePrimitive


class NativeResultError(Exception):
    """原生返回结构非法 (畸形返回)."""


def _first(x):
    """取出嵌套列表首元素; 不存在返回 None."""
    if isinstance(x, (list, tuple)) and x:
        return x[0]
    return None


def _metric_matrix(raw: dict, key: str):
    """取 raw[key][0] (原生把每图结果包在列表里)."""
    return _first(raw.get(key))


def _flatten_show(node, out: list, stats: dict) -> None:
    """展开 ``circles_for_show`` 的任意层包裹，只把**叶子条目**交给上层校验。

    - ``None`` / 空列表：合法的"没有圆"占位，计数后跳过（DEC-054，不是畸形）；
    - 首元素是列表（按图/按矩形分组）：递归下钻；
    - 其它（首元素是标量）：叶子，原样收下由调用方校验长度与数值。
    """
    if node is None or (isinstance(node, (list, tuple)) and not node):
        stats["empty"] += 1
        return
    if not isinstance(node, (list, tuple)):
        out.append(node)
        return
    if isinstance(node[0], (list, tuple)) or node[0] is None:
        for child in node:
            _flatten_show(child, out, stats)
        return
    out.append(node)


def _flatten_numbers(node, out: list) -> None:
    """展开覆盖度等**数值**列表的任意层包裹（与 ``_flatten_show`` 同口径）。"""
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        if not node:
            return
        if isinstance(node[0], (list, tuple)) or node[0] is None:
            for child in node:
                _flatten_numbers(child, out)
            return
        out.extend(node)
        return
    out.append(node)


def _show_circles(raw: dict, coverages, stats: dict) -> list[CirclePrimitive]:
    """``circles_for_show`` -> 圆图元；空占位跳过，flag=0 的候选不计入。"""
    leaves: list = []
    _flatten_show(raw.get("circles_for_show"), leaves, stats)
    cov_values: list = []
    _flatten_numbers(coverages, cov_values)
    circles: list[CirclePrimitive] = []
    for item in leaves:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            raise NativeResultError(f"circles_for_show 元素结构非法: {item!r}")
        flag, cx, cy, r = item[0], item[1], item[2], item[3]
        try:
            flag_i = int(flag)
            cx_f, cy_f, r_f = float(cx), float(cy), float(r)
        except (TypeError, ValueError) as exc:
            raise NativeResultError(
                f"circles_for_show 元素数值非法: {item!r}") from exc
        if not flag_i:
            # 原生用 flag 标记"验证未通过"的候选；旧工程 `_extract_circles` 同样忽略
            stats["rejected"] += 1
            continue
        cov = None
        if len(circles) < len(cov_values):
            try:
                cov = float(cov_values[len(circles)])
            except (TypeError, ValueError):
                cov = None
        circles.append(CirclePrimitive(cx=cx_f, cy=cy_f, radius_px=r_f,
                                       coverage=cov, flag=flag_i))
    return circles


def _flatten_centers(node, out: list, stats: dict) -> None:
    """展开 ``circle_centers`` 的任意层包裹（与 ``_flatten_show`` 同口径）。

    - ``None`` / 空列表：合法空占位，计数后跳过（不是畸形）；
    - 首元素是列表/None：按图 / 按图内矩形分组多包了一层，继续下钻；
    - 首元素是标量：叶子条目 ``[id, cx, cy]``（旧工程读 ``centers[0][0]``）。

    现场同一帧的返回可能比正常形态多包一层（DEC-055：``circles_for_show``
    出现 ``[[[]]]`` 时 ``circle_centers`` 同样会被多包），所以这里**不能**
    假设固定两层。
    """
    if node is None or (isinstance(node, (list, tuple)) and not node):
        stats["center_empty"] = stats.get("center_empty", 0) + 1
        return
    if not isinstance(node, (list, tuple)):
        out.append(node)
        return
    if isinstance(node[0], (list, tuple)) or node[0] is None:
        for child in node:
            _flatten_centers(child, out, stats)
        return
    out.append(node)


def _centers_circles(raw: dict, stats: dict) -> list[CirclePrimitive]:
    """回退: 原生圆心列表 -> 圆图元（递归展开任意层包裹）。

    ``circle_centers`` 正常形态 ``[[[id,cx,cy], ...]]``（按图）；``radius``
    同层数。两者都为空 = 没有圆；有圆心却结构非法才抛 ``NativeResultError``。
    """
    centers: list = []
    _flatten_centers(raw.get("circle_centers"), centers, stats)
    radii: list = []
    _flatten_numbers(raw.get("radius"), radii)
    circles: list[CirclePrimitive] = []
    for i, c in enumerate(centers):
        if not isinstance(c, (list, tuple)) or len(c) < 3:
            raise NativeResultError(f"circle_centers[{i}] 结构非法: {c!r}")
        try:
            cx, cy = float(c[1]), float(c[2])
        except (TypeError, ValueError) as exc:
            raise NativeResultError(f"circle_centers[{i}] 数值非法: {c!r}") from exc
        r = 0.0
        if i < len(radii):
            try:
                r = float(radii[i])
            except (TypeError, ValueError) as exc:
                raise NativeResultError(
                    f"radius[{i}] 数值非法: {radii[i]!r}") from exc
        circles.append(CirclePrimitive(cx=cx, cy=cy, radius_px=r))
    return circles


def _first_scalar(node):
    """把任意层包裹的首元素剥到标量; 空/不存在返回 None。"""
    while isinstance(node, (list, tuple)):
        if not node:
            return None
        node = node[0]
    return node


def _shape(node, depth: int = 0) -> str:
    """原生结构的紧凑形状摘要（报错用，如 ``circles_for_show=[[[]]]``）。"""
    if depth >= 4:
        return "…"
    if node is None:
        return "None"
    if isinstance(node, (list, tuple)):
        if not node:
            return "[]"
        if len(node) > 4:
            head = ",".join(_shape(x, depth + 1) for x in node[:2])
            return f"[{head},…×{len(node)}]"
        return "[" + ",".join(_shape(x, depth + 1) for x in node) + "]"
    if isinstance(node, bool):
        return "bool"
    if isinstance(node, (int, float)):
        return "num"
    return type(node).__name__


def _shape_summary(raw: dict) -> str:
    """畸形返回的形状摘要（DEC-055）：现场直接看出包裹层数，不再靠猜。"""
    text = ("circles_for_show=" + _shape(raw.get("circles_for_show"))
            + " circle_centers=" + _shape(raw.get("circle_centers"))
            + " radius=" + _shape(raw.get("radius")))
    return text if len(text) <= 200 else text[:199] + "…"


def parse_native_circles(raw: object) -> tuple[list[CirclePrimitive], dict]:
    """解析原生返回 -> (圆图元列表, 附加指标)。结构非法抛 NativeResultError。"""
    if raw is None or not isinstance(raw, dict):
        raise NativeResultError(f"原生返回必须是 dict, 得到 {type(raw).__name__}")
    for key in ("circle_num", "circle_centers", "radius", "circles_for_show"):
        if key not in raw:
            raise NativeResultError(f"原生返回缺少字段 {key!r}")

    coverages = _metric_matrix(raw, "circle_coverage")
    stats = {"empty": 0, "rejected": 0, "center_empty": 0}
    try:
        circles = _show_circles(raw, coverages, stats)
        if not circles:
            # 首选列表里没有有效圆（空/全 flag=0）时回退原生中心列表，不据此判畸形
            circles = _centers_circles(raw, stats)
    except NativeResultError as exc:
        # DEC-055：畸形时把原生**真实形状**带出来，现场一眼看出包了几层
        raise NativeResultError(f"{exc}（原生形状: {_shape_summary(raw)}）") from exc

    # circle_num 同样可能多包一层；取第一个标量，取不到按 0（排障指标，不据此判定）
    circle_nums: list = []
    _flatten_numbers(raw.get("circle_num"), circle_nums)
    raw_num = 0
    if circle_nums:
        try:
            raw_num = int(circle_nums[0])
        except (TypeError, ValueError):
            raw_num = 0

    metrics = {
        "raw_circle_num": raw_num,
        "reported_count": len(circles),
        "statues": _first_scalar(raw.get("statues")),
        "error_msg": _first_scalar(raw.get("error_msg")),
        # DEC-054：空占位与 flag=0 候选各有多少（排障用；0 个圆是 NO_TARGET，不是畸形）
        "empty_show_entries": stats["empty"],
        "show_rejected": stats["rejected"],
        # DEC-055：回退中心列表时跳过了多少空占位（同口径排障）
        "empty_center_entries": stats["center_empty"],
    }
    return circles, metrics


def apply_roi_and_scale(
    circles: list[CirclePrimitive],
    *,
    roi: tuple[int, int, int, int] | None,
    scale: float,
    coordinates_are_full_image: bool,
) -> list[CirclePrimitive]:
    """把 ROI 局部/缩放坐标映射回原图, 只转换一次.

    coordinates_are_full_image=True (原生默认) 时不重复转换.
    """
    if coordinates_are_full_image or (roi is None and scale == 1.0):
        return circles
    ox, oy = (roi[0], roi[1]) if roi else (0, 0)
    out: list[CirclePrimitive] = []
    for c in circles:
        out.append(CirclePrimitive(
            cx=c.cx / scale + ox,
            cy=c.cy / scale + oy,
            radius_px=c.radius_px / scale,
            image_space=c.image_space,
            coverage=c.coverage,
            flag=c.flag,
        ))
    return out


def to_image_space(circles: list[CirclePrimitive], space: ImageSpace) -> list[CirclePrimitive]:
    return [
        CirclePrimitive(cx=c.cx, cy=c.cy, radius_px=c.radius_px, image_space=space,
                        coverage=c.coverage, flag=c.flag)
        for c in circles
    ]


__all__ = [
    "NativeResultError", "apply_roi_and_scale", "parse_native_circles",
    "to_image_space",
]
