"""图像算法纯函数 (image_ops) 离线测试 (E19 增强, DEC-021)。

覆盖: 参数表 ↔ 取值对象不变式、coerce 收敛规则、几何/十字线(照旧程序视觉结果)、
显示增强/降噪、二值化与形态学**复用 depth_guidance 判据**(逐位相等)、深度伪彩、
检测三类、apply_chain 的"原始帧只读"约定、参数快照文字。

**不涉硬件**: 全部是合成数组, 不打开相机、不连设备。长度/面积指标用像素;
**深度切分上下限是 mm**(本工程 1 原始值 = 1 mm, 应用时按帧 depth_scale 换算, DEC-025)。
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from application.algorithms.guidance.depth_guidance import (
    DepthGuidanceParams, binarize as dg_binarize, morphology as dg_morphology,
)
from application.algorithms.image_ops import (
    PANEL_GROUPS, PANEL_KEYS, PARAM_GROUPS, PARAM_KEYS, apply_brightness_contrast,
    apply_chain, apply_geometry, binarize_image, canny, coerce_params,
    dataclass_field_names, default_params, denoise, depth_display_range_mm,
    depth_to_rgb, describe, detect, draw_crosshair, format_detections,
    keys_in_scope, morphology_image, param_spec, to_gray,
)


def _asym(h=3, w=4):
    """小的非对称灰度图 (镜像/旋转能看出差别)。"""
    return np.arange(h * w, dtype=np.uint8).reshape(h, w)


# ---------------------------------------------------------------- 参数表
def test_param_table_and_dataclass_agree():
    """参数表 ⊆ dataclass 字段, 且每个 spec 自带渲染所需的键。"""
    fields = set(dataclass_field_names())
    assert set(PARAM_KEYS) <= fields
    assert len(PARAM_KEYS) == len(set(PARAM_KEYS))          # 无重键
    for spec in PARAM_KEYS:
        s = param_spec(spec)
        assert {"key", "label", "kind", "default", "scope", "group",
                "group_label"} <= set(s)
        assert s["scope"] in ("color", "depth", "both")
        assert s["group_label"] in [g["label"] for g in PARAM_GROUPS]
        if s["kind"] == "choice":
            assert s["default"] in [c[0] for c in s["choices"]]
        if s["kind"] in ("int", "float"):
            assert s["min"] <= s["default"] <= s["max"]


def test_scope_split_covers_both_channels():
    """几何/十字线对两路同时生效; 深度伪彩只作用深度路; 检测只作用彩色路。"""
    both = set(keys_in_scope("color")) & set(keys_in_scope("depth"))
    assert {"hflip", "vflip", "rotate", "crosshair"} <= both
    assert param_spec("depth_colormap")["scope"] == "depth"
    assert param_spec("detect")["scope"] == "color"


def test_panel_keys_drop_only_hidden_groups_keeping_data_model():
    """面板可见集 = 参数集 − 隐藏组 (十字线/显示增强); 隐藏组的参数仍在数据模型里。

    用户 2026-10-16「只从面板移除」: 面板不渲染十字线/显示增强, 但 draw_crosshair/
    apply_brightness_contrast 与它们的参数照旧保留, 持久化也不受影响。
    """
    panel = set(PANEL_KEYS)
    assert panel < set(PARAM_KEYS)                        # 真子集 (确实少了东西)
    hidden = {"crosshair", "cross_x_pct", "cross_y_pct",
              "grayscale", "brightness", "contrast"}
    assert hidden & panel == set()                        # 隐藏组不在面板里
    assert hidden <= set(PARAM_KEYS)                      # 但仍在参数表/字段里
    # 新增的深度切分上下限必须在面板上, 且标签写明单位 mm
    assert {"depth_seg_min_mm", "depth_seg_max_mm"} <= panel
    assert param_spec("depth_seg_min_mm")["label"].endswith("(mm)")
    assert param_spec("depth_seg_max_mm")["label"].endswith("(mm)")
    # PANEL_GROUPS 只保留没有 panel=False 的组
    assert [g["group"] for g in PANEL_GROUPS] == \
        [g["group"] for g in PARAM_GROUPS if g.get("panel", True)]


def test_coerce_clamps_unknown_and_bad_enum():
    p = coerce_params({
        "brightness": 9999,          # 越界 -> 夹紧到上限
        "contrast": 0.0,             # 越界 -> 夹紧到下限
        "threshold_mode": "没有这个模式",  # 非法枚举 -> 回默认
        "no_such_key": 123,          # 未知键 -> 忽略
    })
    assert p.brightness == 100
    assert p.contrast == 0.1
    assert p.threshold_mode == param_spec("threshold_mode")["default"]
    assert not hasattr(p, "no_such_key")


def test_coerce_non_mapping_and_garbage_values():
    assert coerce_params(None).is_default()
    assert coerce_params([]).is_default()
    assert coerce_params({}).is_default()
    assert coerce_params({"brightness": "abc"}).brightness == \
        param_spec("brightness")["default"]


def test_changed_keys_and_roundtrip():
    p = default_params().copy(brightness=30, rotate=90)
    assert set(p.changed_keys()) == {"brightness", "rotate"}
    assert not p.is_default()
    assert coerce_params(p.to_dict()).to_dict() == p.to_dict()


# ---------------------------------------------------------------- 几何 / 十字线
def test_geometry_mirror_matches_numpy_flip():
    img = _asym()
    got = apply_geometry(img, coerce_params({"vflip": True, "hflip": True}))
    assert np.array_equal(got, img[::-1, ::-1])


def test_geometry_rotate_matches_numpy_rot90():
    img = _asym(3, 4)
    assert np.array_equal(apply_geometry(img, coerce_params({"rotate": 90})),
                          np.rot90(img, 3))
    assert np.array_equal(apply_geometry(img, coerce_params({"rotate": 180})),
                          np.rot90(img, 2))
    assert np.array_equal(apply_geometry(img, coerce_params({"rotate": 270})),
                          np.rot90(img, 1))
    assert apply_geometry(img, coerce_params({"rotate": 90})).shape == (4, 3)


def test_geometry_noop_keeps_image():
    img = _asym()
    assert np.array_equal(apply_geometry(img, default_params()), img)


def test_crosshair_is_red_horizontal_green_vertical_and_input_untouched():
    """照旧程序 draw_crosshair 的视觉结果: 横线红、竖线绿, 按画面百分比定位。"""
    img = np.zeros((100, 100, 3), np.uint8)
    out = draw_crosshair(img, coerce_params({"crosshair": True,
                                            "cross_x_pct": 25, "cross_y_pct": 75}))
    assert tuple(out[75, 10]) == (255, 0, 0)     # 横线 (y=75) 红
    assert tuple(out[10, 25]) == (0, 255, 0)     # 竖线 (x=25) 绿
    assert tuple(out[75, 25]) == (0, 255, 0)     # 交点绿压红 (竖线后画)
    assert not img.any(), "旧程序原地改输入; 本模块必须先复制"


def test_crosshair_off_returns_input_and_handles_gray():
    img = np.zeros((20, 20, 3), np.uint8)
    assert draw_crosshair(img, default_params()) is img
    gray = np.zeros((20, 20), np.uint8)
    out = draw_crosshair(gray, coerce_params({"crosshair": True}))
    assert out.shape == (20, 20, 3)              # 单通道会自动升成三通道


# ---------------------------------------------------------------- 显示 / 降噪
def test_brightness_and_contrast_use_convert_scale_abs():
    flat = np.full((4, 4), 50, np.uint8)
    assert int(apply_brightness_contrast(flat, coerce_params({"brightness": 30}))[0, 0]) == 80
    assert int(apply_brightness_contrast(flat, coerce_params({"contrast": 2.0}))[0, 0]) == 100
    assert apply_brightness_contrast(flat, default_params()) is flat   # 默认不白算


def test_denoise_median_removes_hot_pixel_and_even_kernel_is_fixed():
    img = np.full((9, 9), 10, np.uint8)
    img[4, 4] = 250
    assert int(denoise(img, coerce_params({"denoise": "median",
                                           "denoise_k": 3}))[4, 4]) == 10
    # k=4 是偶数: depth_guidance 约定自动 +1 -> 5, 不应抛异常
    even = denoise(img, coerce_params({"denoise": "gaussian", "denoise_k": 4}))
    assert even.shape == img.shape and even.dtype == np.uint8
    assert denoise(img, coerce_params({"denoise": "none"})) is img


def test_to_gray_variants():
    assert to_gray(np.zeros((3, 3), np.uint8)).shape == (3, 3)
    assert to_gray(np.zeros((3, 3, 3), np.uint8)).shape == (3, 3)
    assert to_gray(np.zeros((3, 3, 4), np.uint8)).shape == (3, 3)
    with pytest.raises(ValueError):
        to_gray(np.zeros((2, 2, 2), np.uint8))


# ---------------------------------------------------------------- 复用 depth_guidance
def test_binarize_output_is_bitwise_identical_to_depth_guidance():
    """判据必须与旧工程一致 —— 复用 depth_guidance.binarize, 不另写一套阈值语义。"""
    rng = np.random.default_rng(0)
    gray = rng.integers(0, 256, size=(24, 24), dtype=np.uint8)
    for mode in ("binary", "binary_inv", "otsu"):
        got = binarize_image(gray, coerce_params({"threshold_mode": mode,
                                                 "threshold_value": 100}))
        ref = dg_binarize(gray, DepthGuidanceParams(thresh_method=mode,
                                                    thresh_value=100.0))
        assert np.array_equal(got, ref), mode
    assert binarize_image(gray, default_params()) is gray      # "none" 不动图


def test_morphology_output_is_bitwise_identical_to_depth_guidance():
    rng = np.random.default_rng(1)
    binary = (rng.random((21, 21)) > 0.5).astype(np.uint8) * 255
    for op in ("open", "close", "erode", "dilate"):
        got = morphology_image(binary, coerce_params({"morph_op": op, "morph_k": 5}))
        ref = dg_morphology(binary, DepthGuidanceParams(morph_op=op,
                                                        morph_kernel_size=5))
        assert np.array_equal(got, ref), op
    assert morphology_image(binary, default_params()) is binary


def test_canny_returns_binary_edges():
    img = np.zeros((60, 60), np.uint8)
    img[20:40, 20:40] = 255
    edges = canny(img, default_params().copy(edge=True))
    assert edges.dtype == np.uint8
    assert set(np.unique(edges)) <= {0, 255}
    assert int((edges > 0).sum()) > 0


# ---------------------------------------------------------------- 深度伪彩
def test_depth_to_rgb_marks_invalid_black_and_colors_valid():
    d = np.full((8, 8), 1000, np.uint16)
    d[0, 0] = 0                                  # 无效应纯黑
    rgb = depth_to_rgb(d)
    assert rgb.shape == (8, 8, 3) and rgb.dtype == np.uint8
    assert not rgb[0, 0].any()
    assert rgb[4, 4].any()


def test_depth_to_rgb_invalid_stays_black_under_foreign_colormaps():
    """applyColorMap 的 0 不是黑 (JET 下是深蓝), 必须显式压黑。"""
    d = np.full((8, 8), 1000, np.uint16)
    d[0, 0] = 0
    for name in ("jet", "turbo", "bone", "hot", "gray"):
        rgb = depth_to_rgb(d, default_params().copy(depth_colormap=name))
        assert not rgb[0, 0].any(), name


def test_depth_to_rgb_falls_back_when_colormap_missing():
    """cv2 版本没有的色标: 降级到 JET 而不是抛错 (界面下拉不该让页面崩)。"""
    d = np.full((8, 8), 1000, np.uint16)
    bad = default_params().copy(depth_colormap="definitely_not_a_colormap")
    assert depth_to_rgb(d, bad).shape == (8, 8, 3)


def test_depth_to_rgb_uses_explicit_segment_range():
    """显式切分上下限 [下限, 上限] (**mm**): 范围内线性铺开, 范围外**夹紧**到色标两端(不排除)。"""
    p = default_params().copy(depth_seg_min_mm=1000, depth_seg_max_mm=2000)
    low = depth_to_rgb(np.array([[1000]], np.uint16), p)[0, 0]
    mid = depth_to_rgb(np.array([[1500]], np.uint16), p)[0, 0]
    high = depth_to_rgb(np.array([[2000]], np.uint16), p)[0, 0]
    assert not np.array_equal(low, high)
    assert not np.array_equal(low, mid) and not np.array_equal(mid, high)
    # 低于下限 / 高于上限 都夹紧到两端; 60000 仍 <65535 算有效但超上限
    assert np.array_equal(depth_to_rgb(np.array([[500]], np.uint16), p)[0, 0], low)
    assert np.array_equal(depth_to_rgb(np.array([[60000]], np.uint16), p)[0, 0], high)
    # 显式 lo/hi (同样按 mm) 覆盖参数 (供调用方临时改范围, 不改参数对象)
    assert np.array_equal(depth_to_rgb(np.array([[1500]], np.uint16), p,
                                       lo=1000, hi=2000)[0, 0], mid)


def test_depth_to_rgb_converts_mm_bounds_by_frame_depth_scale():
    """mm 阈值落回原始值: 0.002 m/单位 (=2 mm/单位) 时, 1000~2000 mm 对应原始 500~1000。"""
    p = default_params().copy(depth_seg_min_mm=1000, depth_seg_max_mm=2000)
    scale = 0.002
    low = depth_to_rgb(np.array([[500]], np.uint16), p, depth_scale_m=scale)[0, 0]
    high = depth_to_rgb(np.array([[1000]], np.uint16), p, depth_scale_m=scale)[0, 0]
    assert not np.array_equal(low, high)
    # 同样的 mm 范围在 1 mm/单位 的帧上, 端点原始值就是 1000/2000
    assert np.array_equal(low, depth_to_rgb(np.array([[1000]], np.uint16), p)[0, 0])
    assert np.array_equal(high, depth_to_rgb(np.array([[2000]], np.uint16), p)[0, 0])
    # 非法比例: 报错而不是悄悄用默认口径
    with pytest.raises(ValueError):
        depth_to_rgb(np.array([[1000]], np.uint16), p, depth_scale_m=0.0)


def test_depth_segment_keys_accept_legacy_names_and_write_mm_names():
    """旧键名 depth_seg_min/max (DEC-023 没标单位) 仍认; 新键名在场时以新键名为准。"""
    p = coerce_params({"depth_seg_min": 500, "depth_seg_max": 900})
    assert (p.depth_seg_min_mm, p.depth_seg_max_mm) == (500, 900)
    q = coerce_params({"depth_seg_min": 500, "depth_seg_max_mm": 1234})
    assert (q.depth_seg_min_mm, q.depth_seg_max_mm) == (500, 1234)
    # 落盘/快照一律用带单位的键名
    assert "depth_seg_min_mm" in default_params().to_dict()


# ---------------------------------------------------------------- 显示区间解析 (DEC-046)
def test_depth_display_range_ds86_uses_field_guidance_range():
    """DS86 未显式存过区间时, 用引导配置里旧工程现场的显示区间 (100–300 mm)。"""
    cfg = {"ds86_depth_map_min_mm": 100, "ds86_depth_map_max_mm": 300}
    assert depth_display_range_mm("ds86_01", None, cfg) == (100.0, 300.0)
    assert depth_display_range_mm("DS86_01", {}, cfg) == (100.0, 300.0)
    # 无前缀键名也认 (旧工程 _depth_to_colormap 的 cfg 键)
    assert depth_display_range_mm(
        "ds86_01", None,
        {"depth_map_min_mm": 120, "depth_map_max_mm": 320}) == (120.0, 320.0)


def test_depth_display_range_explicit_channel_params_win():
    """通道显式存过区间(含旧键名) → 以通道为准, 不再用引导配置。"""
    cfg = {"ds86_depth_map_min_mm": 100, "ds86_depth_map_max_mm": 300}
    assert depth_display_range_mm(
        "ds86_01", {"depth_seg_min_mm": 500, "depth_seg_max_mm": 900},
        cfg) == (500.0, 900.0)
    assert depth_display_range_mm(
        "ds86_01", {"depth_seg_min": 400, "depth_seg_max": 800},
        cfg) == (400.0, 800.0)


def test_depth_display_range_non_ds86_and_bad_cfg_fall_back_to_defaults():
    cfg = {"ds86_depth_map_min_mm": 100, "ds86_depth_map_max_mm": 300}
    assert depth_display_range_mm("d405_01", None, cfg) == (200.0, 3000.0)
    assert depth_display_range_mm("ds86_01", None, None) == (200.0, 3000.0)
    assert depth_display_range_mm(
        "ds86_01", None,
        {"ds86_depth_map_min_mm": 300, "ds86_depth_map_max_mm": 100}
    ) == (200.0, 3000.0)                       # 上下限反了 → 回默认
    assert depth_display_range_mm(
        "ds86_01", None,
        {"ds86_depth_map_min_mm": "x", "ds86_depth_map_max_mm": 300}
    ) == (200.0, 3000.0)                       # 非数值 → 回默认


def test_ds86_field_distance_under_default_range_looks_black():
    """DEC-046 复现: 现场 ~150 mm 的深度按默认 200–3000 mm 映射, 全被夹到色标最低端。

    这就是"深度伪彩全黑"的机制 —— 数据在, 映射区间不对。按解析出的现场区间
    (100–300 mm) 上色后同一个值明显有颜色。
    """
    frame = np.full((4, 4), 150, np.uint16)       # 1 原始值 = 1 mm (DS86)
    lo, hi = depth_display_range_mm(
        "ds86_01", None,
        {"ds86_depth_map_min_mm": 100, "ds86_depth_map_max_mm": 300})
    fixed = depth_to_rgb(frame, lo=lo, hi=hi)[0, 0]
    broken = depth_to_rgb(frame)[0, 0]            # 默认 200–3000: 150 < 200
    assert int(broken.max()) < 80                 # 最低端深蓝 (0,0,40), 肉眼即"全黑"
    assert int(fixed.max()) > int(broken.max()) + 40
    # 亮度随距离变化: 修好后不同距离能区分, 修之前完全分不开
    near = depth_to_rgb(np.array([[120]], np.uint16), lo=lo, hi=hi)[0, 0]
    far = depth_to_rgb(np.array([[290]], np.uint16), lo=lo, hi=hi)[0, 0]
    assert not np.array_equal(near, far)
    same_a = depth_to_rgb(np.array([[120]], np.uint16))[0, 0]
    same_b = depth_to_rgb(np.array([[290]], np.uint16))[0, 0]
    # 默认映射下 120/290 mm 都挤在色标最暗的 ~3% 里: 极暗、几乎分不出 (看起来"全黑")
    assert int(same_a.max()) < 80 and int(same_b.max()) < 80
    assert abs(int(same_a[-1]) - int(same_b[-1])) < 40


def test_depth_to_rgb_treats_65535_and_nan_as_invalid_black():
    """饱和值 65535 与非有限值同 0 一样算无效: 一律纯黑 (照旧程序 mask_valid)。"""
    d = np.full((4, 4), 1000, np.uint16)
    d[0, 0] = 65535
    rgb = depth_to_rgb(d)
    assert not rgb[0, 0].any()
    assert rgb[1, 1].any()
    # 浮点深度里的 NaN: 非有限值也应压黑 (旧程序 np.isfinite 那一路)
    f = np.full((4, 4), 1200.0, np.float32)
    f[2, 2] = np.nan
    f[3, 3] = np.inf
    rgb_f = depth_to_rgb(f)
    assert not rgb_f[2, 2].any() and not rgb_f[3, 3].any()
    assert rgb_f[0, 0].any()


# ---------------------------------------------------------------- 检测
def test_detect_none_is_empty():
    s = detect(np.zeros((10, 10, 3), np.uint8), default_params())
    assert s.kind == "none" and s.count == 0 and s.is_empty()


def test_detect_contour_finds_blob_and_area_filter_excludes_it():
    img = np.zeros((240, 320, 3), np.uint8)
    cv2.rectangle(img, (100, 80), (180, 160), (255, 255, 255), -1)
    s = detect(img, coerce_params({"detect": "contour"}))
    assert s.count == 1
    it = s.items[0]
    assert abs(it.cx - 140) <= 3 and abs(it.cy - 120) <= 3
    assert abs(it.w - 81) <= 3 and abs(it.h - 81) <= 3
    assert it.area > 6000
    # 面积下限抬到 20000 -> 该块被筛掉 (参数真的生效, 不是摆设)
    assert detect(img, coerce_params({"detect": "contour",
                                      "detect_min_area": 20000.0})).count == 0


def test_detect_circle_finds_synthetic_circle():
    img = np.zeros((240, 320, 3), np.uint8)
    cv2.circle(img, (160, 120), 40, (255, 255, 255), -1)
    img = cv2.GaussianBlur(img, (5, 5), 0)       # 实数帧是平滑的, 硬边合成图 Hough 反而难
    s = detect(img, coerce_params({"detect": "circle"}))
    assert s.count >= 1
    it = s.items[0]
    assert abs(it.cx - 160) <= 5 and abs(it.cy - 120) <= 5
    assert abs(it.radius - 40) <= 6
    # 半径下限抬到 60 -> 该圆被排除
    assert detect(img, coerce_params({"detect": "circle",
                                      "circle_min_radius": 60})).count == 0


def test_detect_line_finds_long_horizontal_segment():
    img = np.zeros((240, 320, 3), np.uint8)
    cv2.line(img, (20, 40), (300, 40), (255, 255, 255), 3)
    s = detect(img, coerce_params({"detect": "line"}))
    assert s.count >= 1
    it = s.items[0]
    assert it.length >= 100
    assert abs(it.h) <= 2                        # 近似水平
    # 票数阈值抬到上限 -> 检不到 (阈值可调且生效)
    assert detect(img, coerce_params({"detect": "line",
                                      "line_votes": 500})).count == 0


def test_format_detections_reports_pixel_units():
    img = np.zeros((240, 320, 3), np.uint8)
    cv2.rectangle(img, (100, 80), (180, 160), (255, 255, 255), -1)
    text = "\n".join(format_detections(detect(img, coerce_params(
        {"detect": "contour"}))))
    assert "轮廓" in text and "像素单位" in text and "px²" in text


# ---------------------------------------------------------------- 主链
def _rich_params():
    return coerce_params({
        "rotate": 90, "crosshair": True, "grayscale": True, "brightness": 20,
        "denoise": "gaussian", "threshold_mode": "otsu", "morph_op": "open",
        "edge": True, "depth_colormap": "jet", "detect": "contour",
        "circle_accum": 40, "line_votes": 90,
    })


def test_apply_chain_never_touches_input_frames():
    """**原始帧只读** —— 这是「显示预览不等于改数据」的硬约束。"""
    rng = np.random.default_rng(2)
    color = rng.integers(0, 256, size=(40, 60, 3), dtype=np.uint8)
    depth = rng.integers(500, 2000, size=(40, 60), dtype=np.uint16)
    color_before, depth_before = color.copy(), depth.copy()
    res = apply_chain(color, depth, _rich_params())
    assert np.array_equal(color, color_before)
    assert np.array_equal(depth, depth_before)
    assert res.color.shape == (60, 40, 3)        # 旋转 90° 后 H/W 互换
    assert res.depth.shape == (60, 40, 3)
    assert res.color.dtype == np.uint8 and res.depth.dtype == np.uint8


def test_apply_chain_composes_geometry_on_depth_too():
    """几何变换对两路同步生效 —— 深度伪彩在几何之后做, 形状必须一致。"""
    depth = np.full((30, 50), 1200, np.uint16)
    res = apply_chain(None, depth, coerce_params({"vflip": True, "rotate": 90}))
    assert res.color is None and res.depth is not None
    assert res.depth.shape == (50, 30, 3)


def test_apply_chain_notes_missing_streams():
    color = np.zeros((10, 10, 3), np.uint8)
    depth = np.full((10, 10), 1000, np.uint16)
    only_color = apply_chain(color, None, default_params())
    assert only_color.depth is None
    assert any("无深度帧" in n for n in only_color.notes)
    only_depth = apply_chain(None, depth, default_params())
    assert only_depth.color is None
    assert any("无彩色帧" in n for n in only_depth.notes)
    none = apply_chain(None, None, default_params())
    assert none.color is None and none.depth is None
    assert len(none.notes) == 2
    assert none.detections.kind == "none"


def test_apply_chain_states_depth_segment_unit_mm():
    """深度路 notes 必须把单位说清: mm + 按本帧比例换算 (没给比例也要明说)。"""
    depth = np.full((8, 8), 1000, np.uint16)
    res = apply_chain(None, depth, default_params(), depth_scale_m=0.001)
    assert any("深度切分 200~3000 mm" in n and "0.001" in n for n in res.notes)
    # 没给比例: 按本工程口径 1 原始值 = 1 mm, 并在提示里写明"未提供"
    res2 = apply_chain(None, depth, default_params().copy(depth_seg_min_mm=300,
                                                          depth_seg_max_mm=600))
    assert any("深度切分 300~600 mm" in n and "未提供深度比例" in n for n in res2.notes)


def test_apply_chain_defaults_reproduce_the_raw_frame():
    """全默认参数下预览＝原图 (用像素相等证明"默认什么都不做")。"""
    color = np.zeros((12, 16, 3), np.uint8)
    color[4:8, 4:10] = 200
    res = apply_chain(color, None, default_params())
    assert np.array_equal(res.color, color)
    assert res.detections.is_empty()


def test_apply_chain_reports_empty_detection_when_switched_on():
    color = np.zeros((12, 16, 3), np.uint8)      # 全黑 -> 检不出东西
    res = apply_chain(color, None, coerce_params({"detect": "contour"}))
    assert res.detections.kind == "contour" and res.detections.count == 0
    assert any("未检出" in n for n in res.notes)


# ---------------------------------------------------------------- 参数快照
def test_describe_empty_on_defaults_else_grouped_with_names():
    assert describe(default_params()) == ()
    one = describe(default_params().copy(brightness=10))
    assert one == ("显示增强: 亮度=10",)
    many = describe(default_params().copy(hflip=True, rotate=90))
    assert len(many) == 1 and "水平镜像=开" in many[0] and "旋转=90°" in many[0]
    everything = describe(default_params(), only_changed=False)
    assert len(everything) == len(PARAM_GROUPS)
