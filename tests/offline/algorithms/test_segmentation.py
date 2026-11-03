# -*- coding: utf-8 -*-
"""视觉分割算法层 (E19 增强) 离线用例: 深度分割 / 智能分割后处理 / 运行一次 / 方案。

对齐旧工程 `Ganta_Crtl_System/vision_monitor` 的检测流程:

    画多边形 → 深度分割(提示点) → 智能分割(EdgeSAM 点提示 → 多边形) → 运行一次(交集)

这些用例**不加载真实 EdgeSAM 模型**（离线判据不依赖 ~40MB ONNX 是否在机器上）：
智能分割一律注入 :class:`StubSegmenter`（确定性凸包填充）或直接测后处理函数。
真实模型能否加载由 :func:`edge_sam_check_available` 单独判断，界面会如实标注。
"""
from __future__ import annotations

import numpy as np
import pytest

from application.algorithms.guidance.depth_guidance import DepthGuidanceParams
from application.algorithms.segmentation import (
    DEFAULT_MIN_AREA,
    RunOnceScheme,
    StubSegmenter,
    build_encoder_image,
    clip_roi,
    coerce_scheme,
    contour_area_sum,
    default_segmenter,
    has_scheme,
    intersection_result,
    mask_primary_component,
    mask_to_contours,
    mask_to_polygons,
    run_depth_segmentation,
    run_scheme,
    scheme_from_params,
)
from application.contracts.frames import FrameBundle, ImageRef

# 合成帧尺寸与目标块（深度 240×320）。**目标 = 远处**那块：本算法按"有效深度里偏远的
# 连通域"取目标（杆塔/绝缘子这类被检物在远处），故背景 400mm（近）、目标块 3000mm（远）。
H, W = 240, 320
FG_BOX = (90, 70, 230, 170)      # x1, y1, x2, y2（x2/y2 开界）
DEPTH_SCALE = 0.001              # 1 原始值 = 1 mm


def _depth(bg: int = 400, fg: int = 3000) -> np.ndarray:
    d = np.full((H, W), bg, dtype=np.uint16)
    x1, y1, x2, y2 = FG_BOX
    d[y1:y2, x1:x2] = fg
    return d


def _color() -> np.ndarray:
    """彩色帧（本工程约定 RGB）：目标块偏红，便于人工核对。"""
    c = np.zeros((H, W, 3), dtype=np.uint8)
    x1, y1, x2, y2 = FG_BOX
    c[y1:y2, x1:x2] = (200, 40, 40)
    return c


def _frame(*, color=None, depth=None, scale=DEPTH_SCALE) -> FrameBundle:
    return FrameBundle(
        camera_id="cam1", frame_id="f1", capture_id="c1", sequence=1,
        color=None if color is None else ImageRef.from_array(color),
        depth=None if depth is None else ImageRef.from_array(depth),
        depth_scale_m=scale,
    )


# ---------------------------------------------------------------------------
#  深度分割
# ---------------------------------------------------------------------------
def test_clip_roi_clamps_and_rejects_tiny():
    assert clip_roi(None, 100, 200) is None
    assert clip_roi((10, 10, 60, 60), 100, 200) == (10, 10, 60, 60)
    # 越界被夹到图内（x2/y2 开界）
    assert clip_roi((-50, -50, 9999, 9999), 100, 200) == (0, 0, 200, 100)
    # 太小 -> None（调用方据此回落到整图/报错，不静默按 0 面积算）
    assert clip_roi((10, 10, 11, 11), 100, 200) is None


def test_run_depth_segmentation_finds_blob_and_prompts_inside():
    seg = run_depth_segmentation(_depth(), DEPTH_SCALE, DepthGuidanceParams(),
                                 filter_mode="所有轮廓", top_n=1, k=3, margin=5.0)
    assert seg["n_contours"] == 1
    assert len(seg["contours"]) == 1
    assert seg["roi_offset"] == (0, 0)
    # 提示点分组：每组对应一个最终轮廓；点数不超过 k
    assert len(seg["prompts"]) == 1
    pts = seg["prompts"][0]
    assert 1 <= len(pts) <= 3
    # 提示点必须落在目标块内（轮廓内取点，不许点到背景上）
    x1, y1, x2, y2 = FG_BOX
    for px, py in pts:
        assert x1 <= px <= x2 and y1 <= py <= y2, (px, py)
    # 轮廓面积应接近目标块面积（±15%）
    area = contour_area_sum(seg["contours"])
    box_area = (x2 - x1) * (y2 - y1)
    assert abs(area - box_area) / box_area < 0.15, (area, box_area)


def test_depth_segmentation_polarity_depends_on_binarization():
    """前景/背景极性由二值化方法决定 —— DEC-028「分割结果和旧程序正好相反」的根因。

    场景：**目标比背景近**（归一化后目标更暗、背景更亮）。此时：
    - otsu（THRESH_BINARY）把**更亮的背景**当白色前景 → 找到的轮廓是整幅背景
      （= 目标的互补），正是用户看到的"相反"；
    - 旧现场配置 binary_inv（反转阈值）把**更暗的目标**当白色前景 → 轮廓落在目标块上。
    所以 E19「视觉分割」页默认必须是 binary_inv（对齐旧现场配置），不能用算法 dataclass 的 otsu。
    """
    # 近目标：目标 400mm（近、暗），背景 3000mm（远、亮）
    d = np.full((H, W), 3000, dtype=np.uint16)
    x1, y1, x2, y2 = FG_BOX
    d[y1:y2, x1:x2] = 400

    otsu = run_depth_segmentation(d, DEPTH_SCALE, DepthGuidanceParams(),
                                  filter_mode="最大面积轮廓", top_n=1)
    assert otsu["contours"], otsu
    assert contour_area_sum(otsu["contours"]) > 0.5 * H * W   # 互补：整幅背景

    inv = run_depth_segmentation(
        d, DEPTH_SCALE,
        DepthGuidanceParams(thresh_method="binary_inv", morph_op="none",
                            morph_kernel_size=3),
        filter_mode="最大面积轮廓", top_n=1)
    assert inv["contours"], inv
    tgt_area = contour_area_sum(inv["contours"])
    box_area = (x2 - x1) * (y2 - y1)
    assert abs(tgt_area - box_area) / box_area < 0.15, (tgt_area, box_area)


def test_run_depth_segmentation_with_roi_offsets_are_relative_to_crop():
    roi = (60, 40, 280, 220)
    seg = run_depth_segmentation(_depth(), DEPTH_SCALE, DepthGuidanceParams(),
                                 filter_mode="所有轮廓", top_n=1, k=2, roi=roi)
    assert seg["roi_offset"] == (60, 40)
    # 轮廓是**运算区域坐标系**（未平移）；调用方用 offset 转到原图
    c = np.asarray(seg["contours"][0]).reshape(-1, 2)
    assert c[:, 0].min() >= 0 and c[:, 0].max() <= roi[2] - roi[0]
    assert c[:, 1].min() >= 0 and c[:, 1].max() <= roi[3] - roi[1]
    assert seg["depth_run"].shape == (roi[3] - roi[1], roi[2] - roi[0])


def test_run_depth_segmentation_empty_input_is_not_an_exception():
    seg = run_depth_segmentation(None, DEPTH_SCALE, DepthGuidanceParams())
    assert seg["contours"] == [] and seg["prompts"] == [] and seg["n_contours"] == 0


def test_build_encoder_image_sources_and_honest_notes():
    depth, color = _depth(), _color()
    seg = run_depth_segmentation(depth, DEPTH_SCALE, DepthGuidanceParams())
    img, note = build_encoder_image("color", color, depth, DEPTH_SCALE, seg["filtered"])
    assert img is not None and img.shape == color.shape and note == "彩色图"
    img2, note2 = build_encoder_image("depth", color, depth, DEPTH_SCALE, seg["filtered"])
    assert img2 is not None and note2 == "深度伪彩图"
    img3, note3 = build_encoder_image("fused", color, depth, DEPTH_SCALE, seg["filtered"])
    assert img3 is not None and "融合图" in note3
    # 掩码外置黑：目标块外应为纯黑
    assert int(img3[0, 0].sum()) == 0
    # 缺彩色时不冒充：明确返回 None + 原因
    img4, note4 = build_encoder_image("color", None, depth, DEPTH_SCALE, seg["filtered"])
    assert img4 is None and "无彩色图" in note4
    img5, note5 = build_encoder_image("fused", color, depth, DEPTH_SCALE, None)
    assert img5 is None and "融合图需要" in note5


def test_build_encoder_image_does_not_reorder_channels():
    """本工程帧契约是 RGB，封装**不得**再做 BGR↔RGB 转换（旧工程输入是 BGR）。"""
    color = np.zeros((8, 8, 3), dtype=np.uint8)
    color[..., 0] = 200     # R
    color[..., 2] = 10      # B
    img, _note = build_encoder_image("color", color, None, DEPTH_SCALE, None)
    assert tuple(int(v) for v in img[0, 0]) == (200, 0, 10)


# ---------------------------------------------------------------------------
#  智能分割后处理（判据来自旧工程，逐条对齐）
# ---------------------------------------------------------------------------
def test_mask_primary_component_keeps_component_containing_point():
    mask = np.zeros((60, 100), dtype=np.uint8)
    mask[5:15, 5:25] = 255          # 小连通域（重心 x≈15）
    mask[30:55, 40:95] = 255        # 大连通域（面积更大）
    out, dropped = mask_primary_component(mask, (10, 10))
    assert dropped >= 0
    assert out[10, 10] == 255 and out[40, 40] == 0        # 只留锚点那一个
    # 锚点不在任何前景内 -> 回退保留面积最大的
    out2, _ = mask_primary_component(mask, (99, 59))
    assert out2[40, 40] == 255 and out2[10, 10] == 0


def test_mask_to_polygons_drops_specks_below_min_area():
    mask = np.zeros((80, 80), dtype=np.uint8)
    mask[10:60, 10:60] = 255       # 大块
    mask[70:72, 70:72] = 255       # 2×2 碎斑
    polys = mask_to_polygons(mask, min_area=30.0)
    assert len(polys) == 1
    c = np.asarray(polys[0]).reshape(-1, 2)
    assert c[:, 0].min() >= 5 and c[:, 1].min() >= 5     # 只剩大块


def test_stub_segmenter_is_deterministic_and_never_claims_to_be_a_model():
    s = StubSegmenter()
    img = np.ascontiguousarray(_color())
    ms = s.set_image(img)
    assert isinstance(ms, float)
    m1, _sc1, _ms1 = s.predict([(150.0, 120.0)])
    m2, _sc2, _ms2 = s.predict([(150.0, 120.0)])
    assert np.array_equal(m1, m2)                          # 确定性
    assert m1.shape[:2] == img.shape[:2]
    assert m1.max() == 255 and m1.any()
    assert "非真实模型" in s.name                           # 如实标注，绝不冒充模型


def test_default_segmenter_reports_reason_when_unavailable(monkeypatch):
    """真机缺 onnxruntime/模型时**拒绝**（不拿占位冒充）；离线档位才给占位并标注。"""
    from application.algorithms.segmentation import edge_sam

    monkeypatch.setattr(edge_sam, "check_available", lambda: "onnxruntime 未安装")
    seg, note = default_segmenter(offline=False)
    assert seg is None and "onnxruntime" in note
    seg2, note2 = default_segmenter(offline=True)
    assert isinstance(seg2, StubSegmenter) and "离线占位" in note2


def test_default_segmenter_prefers_real_model_when_available(monkeypatch):
    """模型与 onnxruntime 都在时用真实 EdgeSAM（离线档位也是真模型，如实标注）。"""
    from application.algorithms.segmentation import edge_sam

    monkeypatch.setattr(edge_sam, "check_available", lambda: None)
    seg, note = default_segmenter(offline=True)
    assert seg is not None and not isinstance(seg, StubSegmenter)
    assert "EdgeSAM" in note


# ---------------------------------------------------------------------------
#  运行一次：交集
# ---------------------------------------------------------------------------
def test_intersection_result_hit_and_miss():
    mask = np.zeros((60, 60), dtype=np.uint8)
    mask[20:40, 20:40] = 255
    inside = [[25.0, 25.0], [35.0, 25.0], [35.0, 35.0], [25.0, 35.0]]
    r = intersection_result(mask, inside, mask.shape, min_area=1.0)
    assert r["hit"] is True and r["area_px"] > 0
    outside = [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0]]
    r2 = intersection_result(mask, outside, mask.shape, min_area=1.0)
    assert r2["hit"] is False and r2["area_px"] == 0 and r2["contours"] == []


def test_mask_to_contours_min_area_filters():
    mask = np.zeros((50, 50), dtype=np.uint8)
    mask[10:40, 10:40] = 255
    mask[44:47, 44:47] = 255        # 3×3 碎斑（面积 ~4px²）
    assert len(mask_to_contours(mask, min_area=1.0)) == 2
    assert len(mask_to_contours(mask, min_area=100.0)) == 1


# ---------------------------------------------------------------------------
#  方案（= 要保存的「运行一次」实现逻辑）
# ---------------------------------------------------------------------------
def _scheme(**kw) -> RunOnceScheme:
    base = dict(source="color", op_region="whole", polygon=[
        [100.0, 80.0], [220.0, 80.0], [220.0, 160.0], [100.0, 160.0]],
        depth_params=DepthGuidanceParams(depth_min_mm=200.0,
                                         depth_max_mm=3000.0).to_dict(),
        seg_filter_mode="所有轮廓", seg_filter_n=1, prompt_k=3, prompt_margin=5.0)
    base.update(kw)
    return RunOnceScheme(**base)


def test_scheme_roundtrip_through_dict():
    s = _scheme()
    d = s.to_dict()
    back = coerce_scheme(d)
    assert back is not None
    assert back.source == "color" and back.polygon == s.polygon
    assert back.seg_filter_n == 1 and back.prompt_k == 3
    assert back.guidance_params().depth_max_mm == 3000.0
    # JSON/YAML 友好：多边形是纯 float 列表
    assert all(isinstance(v, float) for pt in d["polygon"] for v in pt)


def test_coerce_scheme_rejects_unrelated_dict_and_clamps_bad_values():
    assert coerce_scheme(None) is None
    assert coerce_scheme({"pipeline": {"pipeline_id": "x"}}) is None   # 不是方案
    s = coerce_scheme({"source": "nope", "op_region": "nope",
                       "seg_filter_mode": "nope", "polygon": "bad",
                       "seg_filter_n": 0, "prompt_k": -5,
                       "prompt_margin": -1.0, "min_area": -2.0,
                       "roi": [1, 2]})
    assert s is not None
    assert s.source == "color" and s.op_region == "whole"
    assert s.seg_filter_mode == "所有轮廓"
    assert s.polygon == [] and s.roi is None
    assert s.seg_filter_n >= 1 and s.prompt_k >= 1
    assert s.prompt_margin >= 0.0 and s.min_area >= 0.0


def test_scheme_from_params_and_has_scheme():
    params = {"scheme": _scheme().to_dict(), "display_image_ops": {"hflip": True}}
    assert has_scheme(params) is True
    assert scheme_from_params(params).source == "color"
    assert has_scheme({}) is False
    assert scheme_from_params({}) is None
    assert scheme_from_params(None) is None


def test_run_scheme_hits_inside_polygon_with_stub_segmenter():
    res = run_scheme(_scheme(), _frame(color=_color(), depth=_depth()),
                     StubSegmenter())
    assert res.ok and res.error is None
    assert res.segmenter_name == StubSegmenter().name
    assert res.n_contours == 1
    assert len(res.depth_contours) == 1 and res.prompts
    assert res.hit is True and res.area_px > 0
    assert res.source == "color" and res.source_note == "彩色图"
    # 交集轮廓是**原图坐标**：应落在目标块附近
    c = np.asarray(res.intersection_contours[0]).reshape(-1, 2)
    x1, y1, x2, y2 = FG_BOX
    assert c[:, 0].min() >= x1 - 20 and c[:, 0].max() <= x2 + 20
    assert res.timings_ms.get("total", 0) > 0
    # 产出覆盖层坐标可直接画在整帧上
    assert res.smart_mask is not None and res.smart_mask.shape[:2] == (H, W)


def test_run_scheme_miss_reports_no_target_without_faking():
    poly = [[0.0, 0.0], [20.0, 0.0], [20.0, 20.0], [0.0, 20.0]]   # 远离目标块
    res = run_scheme(_scheme(polygon=poly), _frame(color=_color(), depth=_depth()),
                     StubSegmenter())
    assert res.ok and res.error is None
    assert res.hit is False and res.area_px == 0
    assert any("无有效交集" in n for n in res.notes)


def test_run_scheme_requires_depth_and_scale():
    res = run_scheme(_scheme(), _frame(color=_color(), depth=None), StubSegmenter())
    assert res.ok is False and "无深度图" in res.error
    res2 = run_scheme(_scheme(), _frame(color=_color(), depth=_depth(), scale=None),
                      StubSegmenter())
    assert res2.ok is False and "depth_scale" in res2.error


def test_run_scheme_without_segmenter_fails_honestly():
    res = run_scheme(_scheme(), _frame(color=_color(), depth=_depth()), None)
    assert res.ok is False and "智能分割后端" in res.error
    # 显式允许降级时：只跑深度分割，交集为空，并在 notes 里说清
    res2 = run_scheme(_scheme(), _frame(color=_color(), depth=_depth()), None,
                      require_segmenter=False)
    assert res2.ok is True and res2.hit is False
    assert any("仅完成深度分割" in n for n in res2.notes)


def test_run_scheme_roi_region_shifts_back_to_full_frame_coordinates():
    roi = [60.0, 40.0, 280.0, 220.0]
    res = run_scheme(_scheme(op_region="roi", roi=roi),
                     _frame(color=_color(), depth=_depth()), StubSegmenter())
    assert res.ok and res.roi == (60, 40, 280, 220) and res.roi_offset == (60, 40)
    # 提示点/轮廓都平移回原图坐标：应落在目标块附近（不是 ROI 局部坐标）
    assert res.prompts and all(px >= 60 and py >= 40
                              for grp in res.prompts for px, py in grp)
    assert res.hit is True


def test_run_scheme_invalid_roi_falls_back_to_whole_frame_with_note():
    res = run_scheme(_scheme(op_region="roi", roi=[5.0, 5.0, 6.0, 6.0]),
                     _frame(color=_color(), depth=_depth()), StubSegmenter())
    assert res.ok
    assert any("ROI 无效" in n for n in res.notes)
    assert res.roi is None


def test_run_scheme_fused_source_reports_missing_color():
    res = run_scheme(_scheme(source="fused"), _frame(color=None, depth=_depth()),
                     StubSegmenter())
    assert res.ok is False and "融合图" in res.error


def test_default_min_area_matches_legacy_run_detect_once():
    assert DEFAULT_MIN_AREA == pytest.approx(1.0)
