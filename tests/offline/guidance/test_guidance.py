"""引导定位本地闭环（离线；不接真机、不碰网络）。

覆盖：像素→XYZ 与螺旋搜索 / 偏差轴映射与符号 / 深度引导算法（合成深度图）/
引导服务（抓图→找圆→XYZ→偏差，含结果图；宿主不可用与缺深度时报错不给假值）/
引导配置（加载、校验、旧值导入逐项核对）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from application.algorithms.circle.params import CircleParams
from application.algorithms.guidance.depth_guidance import (
    DepthGuidanceParams,
    depth_to_mm,
    point_depth_xyz,
    run_depth_guidance,
)
from application.algorithms.guidance.offsets import (
    AxisMapping,
    OffsetParams,
    compute_robot_offsets,
)
from application.algorithms.guidance.pixel_ops import compute_pixel_xyz, pixel_to_xyz
from application.contracts.enums import ImageSpace
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
from application.services.guidance_config import (
    FineOffsetProfile,
    GuidanceConfigStore,
    import_legacy_guidance,
    validate,
)
from application.services.guidance_service import (
    GuidanceService,
    extract_circles,
    intrinsics_to_dict,
)

ROOT = Path(__file__).resolve().parents[3]
W, H = 320, 240
RAW_DISC = 500              # 原始深度值
SCALE = 0.001               # 米/原始单位 → 500mm
FX = FY = 200.0
PPX, PPY = W / 2.0, H / 2.0


def _intr_dict() -> dict:
    return {"fx": FX, "fy": FY, "ppx": PPX, "ppy": PPY, "width": W, "height": H}


def _depth_with_disc(cx: int = 160, cy: int = 120, radius: int = 30,
                     raw: int = RAW_DISC) -> np.ndarray:
    depth = np.zeros((H, W), dtype="uint16")
    yy, xx = np.mgrid[0:H, 0:W]
    depth[np.hypot(xx - cx, yy - cy) < radius] = raw
    return depth


def _frame(depth: np.ndarray | None = None) -> FrameBundle:
    depth = _depth_with_disc() if depth is None else depth
    color = np.zeros((H, W, 3), dtype="uint8")
    return FrameBundle(
        camera_id="ds86_01", frame_id="f1", capture_id="c1", sequence=1,
        service_id="local", channel_id="ds86_01",
        color=ImageRef.from_array(color), depth=ImageRef.from_array(depth),
        depth_scale_m=SCALE,
        intrinsics=Intrinsics(fx=FX, fy=FY, cx=PPX, cy=PPY, width=W, height=H,
                              intrinsics_space=ImageSpace.DEPTH,
                              calibration_version="t"))


class FakeHost:
    """假 cp38 宿主：按配置返回一个圆（不动原生件）。"""

    def __init__(self, circles=None, *, fail: bool = False) -> None:
        self.circles = circles if circles is not None else [(160.0, 120.0, 28.0)]
        self.fail = fail
        self.calls = 0

    def detect(self, images, rects, params: CircleParams) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("注入: 宿主失败")
        show = [[1, cx, cy, r] for (cx, cy, r) in self.circles]
        return {"statues": [1], "error_msg": [""], "circle_num": [[len(show)]],
                "circles_for_show": [show], "circle_centers": [], "radius": []}


def _store(tmp_path, **over) -> GuidanceConfigStore:
    st = GuidanceConfigStore(str(tmp_path / "g.yaml"), seed_if_missing=False)
    st.load()
    # 合成图无需滤波；其余保持与出厂配置一致的既定阈值（binary_inv/127）
    st._config.depth.morph_op = "none"          # noqa: SLF001
    st._config.depth.thresh_method = "binary_inv"   # noqa: SLF001
    st._config.depth.thresh_value = 127             # noqa: SLF001
    st._config.circle = {"circle_type": "最大圆"}     # noqa: SLF001
    for k, v in over.items():
        setattr(st._config.depth, k, v)
    return st


# ---------------------------------------------------------------- 像素/偏差
def test_pixel_to_xyz_center_and_offset():
    assert pixel_to_xyz(1.0, PPX, PPY, _intr_dict()) == (0.0, 0.0, 1.0)
    x, y, z = pixel_to_xyz(0.5, PPX + 20, PPY, _intr_dict())
    assert x == pytest.approx(0.05) and y == pytest.approx(0.0) and z == 0.5
    assert pixel_to_xyz(0.0, 10, 10, _intr_dict()) == (0.0, 0.0, 0.0)


def test_compute_pixel_xyz_valid_and_spiral_search():
    depth = np.zeros((H, W), dtype="uint16")
    depth[120, 160] = 500                       # 有效点
    got = compute_pixel_xyz(160, 120, depth, _intr_dict(), SCALE)
    assert got is not None and got[2] == pytest.approx(500.0)

    # (161,120) 无效 -> 螺旋搜索应找到邻近的 (160,120)，并给出 search_rect
    got2 = compute_pixel_xyz(161, 120, depth, _intr_dict(), SCALE)
    assert got2 is not None and got2[2] == pytest.approx(500.0)
    assert got2[3] is not None

    assert compute_pixel_xyz(160, 120, np.zeros((H, W), dtype="uint16"),
                             _intr_dict(), SCALE) is None


def test_axis_mapping_and_signs():
    # 相机 X→Z(-)、Y→X(-)（旧工程默认）
    mapping = AxisMapping("Z", -1, "X", -1)
    params = OffsetParams(coarse=mapping, fine=mapping, tcp_x=0.0, tcp_y=0.0)
    coarse, fine = compute_robot_offsets((10.0, 4.0, 500.0), params)
    assert coarse == (-4.0, -10.0) and fine == (-4.0, -10.0)   # (robot_x=-y, robot_z=-x)


def test_fine_offset_is_relative_to_tcp():
    mapping = AxisMapping("Z", -1, "X", -1)
    params = OffsetParams(coarse=mapping, fine=mapping, tcp_x=2.0, tcp_y=5.0)
    coarse, fine = compute_robot_offsets((10.0, 4.0, 500.0), params)
    assert coarse == (-4.0, -10.0)                    # 粗：相对相机中心
    assert fine == (1.0, -8.0)                        # 精：相对工具中心 (cam - tcp)


def _legacy_offsets_only(cam_x, cam_y, coarse, fine, tcp_x, tcp_y):
    """旧工程 `guidance_locate_tab._compute_robot_offsets` 原样分支（对照基线）。

    粗: 识别点相对相机中心；精: 识别点相对工具中心 (cam - tcp)；
    **后写覆盖先写**（相机 X、Y 都映射到同一机械手轴时，以 Y 的映射为准）。
    """
    def one(mapping, x, y):
        axis_x, sign_x, axis_y, sign_y = mapping
        robot_x, robot_z = 0.0, 0.0
        if axis_x == "Z":
            robot_z = sign_x * x
        else:
            robot_x = sign_x * x
        if axis_y == "X":
            robot_x = sign_y * y
        else:
            robot_z = sign_y * y
        return robot_x, robot_z

    return (one(coarse, cam_x, cam_y),
            one(fine, cam_x - tcp_x, cam_y - tcp_y))


def test_offsets_match_legacy_branch_structure_exactly():
    """逐组合对照旧工程：粗/精映射、符号、tcp、同轴覆盖行为都必须**逐位相同**。"""
    combos = [(ax, sx, ay, sy)
              for ax in ("X", "Z") for sx in (1, -1)
              for ay in ("X", "Z") for sy in (1, -1)]
    samples = [((0.0, 0.0), 0.0, 0.0),
               ((10.0, 4.0), 0.0, 0.0),
               ((10.0, 4.0), 104.0, 0.0),           # 现场 guidance.yaml 的 tcp_x
               ((-12.34, 56.78), 104.0, -7.5)]
    for coarse in combos:
        for fine in combos:
            for (cam_x, cam_y), tcp_x, tcp_y in samples:
                params = OffsetParams(
                    coarse=AxisMapping(*coarse), fine=AxisMapping(*fine),
                    tcp_x=tcp_x, tcp_y=tcp_y)
                got = compute_robot_offsets((cam_x, cam_y, 500.0), params)
                want = _legacy_offsets_only(cam_x, cam_y, coarse, fine,
                                            tcp_x, tcp_y)
                assert got == want, (coarse, fine, cam_x, cam_y, tcp_x, tcp_y)


def test_axis_mapping_rejects_bad_values():
    with pytest.raises(ValueError):
        AxisMapping("Y", -1, "X", -1)
    with pytest.raises(ValueError):
        AxisMapping("Z", 0, "X", -1)
    with pytest.raises(ValueError):
        compute_robot_offsets((1.0, 2.0))


# ---------------------------------------------------------------- 算法
def test_depth_pipeline_finds_candidate_on_synthetic_disc():
    depth = _depth_with_disc()
    params = DepthGuidanceParams(morph_op="none", thresh_method="binary_inv",
                                 thresh_value=127, depth_min_mm=200.0,
                                 depth_max_mm=3000.0, rect_side_length=100.0)
    res = run_depth_guidance(depth, SCALE, params, color_image=None,
                             include_rects=True)
    assert res["n_contours"] >= 1
    assert res["rects"], "应在合成圆盘上找到候选矩形"
    r = res["rects"][0]
    assert abs(int(r["cx"]) - 160) <= 8 and abs(int(r["cy"]) - 120) <= 8
    assert res["depth_mm"].max() == pytest.approx(500.0)
    assert res["debug_image"] is not None


def test_point_depth_xyz_uses_average_depth_when_enabled():
    depth = _depth_with_disc()
    depth_mm = depth_to_mm(depth, SCALE)
    params = DepthGuidanceParams(avg_depth_enable=True, avg_depth_diameter=11,
                                 depth_valid_min_mm=1.0)
    xyz, used = point_depth_xyz(depth_mm, 160.0, 120.0, _intr_dict(), params)
    assert xyz is not None and used == pytest.approx(500.0, abs=1.0)
    assert xyz[0] == pytest.approx(0.0, abs=0.5)      # 圆心在光心 -> X≈0


# ---------------------------------------------------------------- 引导服务
def test_guidance_service_end_to_end(tmp_path):
    host = FakeHost()
    svc = GuidanceService(_store(tmp_path), camera=None, host_proxy=host)
    result = svc.detect(frame=_frame())
    assert result.ok, result.message
    assert host.calls >= 1
    x, y, z = result.cam_xyz
    assert z == pytest.approx(500.0, abs=2.0)
    assert result.coarse is not None and result.fine is not None
    assert result.circle is not None
    assert result.debug_image is not None and result.depth_mm is not None


def test_guidance_service_skips_circle_with_invalid_depth_like_legacy(tmp_path):
    """第一个圆压在没有深度的区域时，按旧工程顺序取**下一个深度有效**的圆。"""
    host = FakeHost([(10.0, 10.0, 8.0), (160.0, 120.0, 28.0)])
    svc = GuidanceService(_store(tmp_path), camera=None, host_proxy=host)
    result = svc.detect(frame=_frame())
    assert result.ok, result.message
    assert result.circle is not None
    assert result.circle.cx == pytest.approx(160.0)   # 跳过了无深度的第一个圆
    assert result.cam_xyz[2] == pytest.approx(500.0, abs=2.0)


def test_detect_fine_prefers_circle_nearest_optical_center(tmp_path):
    """精定位多圆：优先取离光心最近的圆，而不是第一个自动圆（DEC-063）。"""
    depth = _depth_with_disc()
    yy, xx = np.mgrid[0:H, 0:W]
    depth[np.hypot(xx - 40, yy - 40) < 25] = 500       # 第二个圆（不是要定位的目标）
    host = FakeHost([(40.0, 40.0, 20.0), (160.0, 120.0, 28.0)])
    svc = GuidanceService(_store(tmp_path), camera=None, host_proxy=host)
    result = svc.detect_fine(frame=_frame(depth))
    assert result.ok, result.message
    assert result.circle.cx == pytest.approx(160.0)   # 离光心最近的圆
    assert result.circle.cy == pytest.approx(120.0)


def test_guidance_service_without_host_reports_error(tmp_path):
    svc = GuidanceService(_store(tmp_path), camera=None, host_proxy=None)
    result = svc.detect(frame=_frame())
    assert not result.ok and "找圆不可用" in result.message
    assert result.coarse is None and result.fine is None    # 不给假偏移


def test_guidance_service_missing_depth_reports_error(tmp_path):
    svc = GuidanceService(_store(tmp_path), camera=None, host_proxy=FakeHost())
    frame = _frame()
    frame.depth = None
    result = svc.detect(frame=frame)
    assert not result.ok and "深度" in result.message and result.cam_xyz is None


def test_guidance_service_no_candidate_reports_reason(tmp_path):
    svc = GuidanceService(_store(tmp_path), camera=None, host_proxy=FakeHost())
    blank = np.zeros((H, W), dtype="uint16")          # 全无效 -> 无候选
    result = svc.detect(frame=_frame(blank))
    assert not result.ok and "未找到候选" in result.message
    assert result.coarse is None and result.fine is None    # 不给假偏移


def test_guidance_service_requires_camera_when_no_frame(tmp_path):
    svc = GuidanceService(_store(tmp_path), camera=None, host_proxy=FakeHost())
    result = svc.detect()                              # 未给帧也未接相机
    assert not result.ok and "抓图失败" in result.message


def test_extract_circles_prefers_show_then_centers():
    assert extract_circles({"circles_for_show": [[[1, 10, 20, 5]]]}) == \
        [{"cx": 10, "cy": 20, "radius": 5.0}]
    assert extract_circles({"circles_for_show": [[[0, 1, 2, 3]]],
                            "circle_centers": [[[0, 7, 8]]],
                            "radius": [[4.0]]}) == [{"cx": 7, "cy": 8, "radius": 4.0}]


def test_extract_circles_tolerates_empty_nested_placeholders():
    """DEC-054：空占位（任意包裹层）不是畸形；没有有效圆时回退中心列表。"""
    assert extract_circles({"circles_for_show": [[[]]], "circle_centers": [],
                            "radius": []}) == []
    assert extract_circles({"circles_for_show": [[[]]],
                            "circle_centers": [[[0, 30, 40]]],
                            "radius": [[9.0]]}) == \
        [{"cx": 30, "cy": 40, "radius": 9.0}]
    assert extract_circles({"circles_for_show": [[[], [1, 11, 22, 3]]]}) == \
        [{"cx": 11, "cy": 22, "radius": 3.0}]


def test_extract_circles_extra_layer_centers_and_radius():
    """DEC-055：整帧返回多包一层时，中心/半径回退同样要能取到。"""
    assert extract_circles({"circles_for_show": [[[]]],
                            "circle_centers": [[[[0, 30, 40]]]],
                            "radius": [[[9.0]]]}) == \
        [{"cx": 30, "cy": 40, "radius": 9.0}]
    assert extract_circles({"circles_for_show": [[[]]],
                            "circle_centers": [[[[]]]],
                            "radius": [[[]]]}) == []


def test_intrinsics_to_dict_maps_cx_cy_to_ppx_ppy():
    frame = _frame()
    d = intrinsics_to_dict(frame.intrinsics)
    assert d["ppx"] == PPX and d["ppy"] == PPY and d["fx"] == FX


# ---------------------------------------------------------------- 配置
def test_shipped_guidance_config_loads_and_validates():
    cfg = GuidanceConfigStore(str(ROOT / "configs" / "motion" / "guidance.yaml")).load()
    assert validate(cfg) == []
    assert cfg.offsets.coarse.x_axis in ("X", "Z")
    assert cfg.circle.get("circle_type")
    assert cfg.depth.depth_min_mm < cfg.depth.depth_max_mm


def test_legacy_guidance_import_matches_source():
    """导入值应与旧 camera_config.json 的引导页逐项一致。"""
    import json
    legacy_path = (ROOT.parent / "Ganta_Crtl_System" / "vision_monitor" /
                   "config" / "camera_config.json")
    if not legacy_path.exists():
        pytest.skip("旧工程配置不在")
    raw = json.loads(legacy_path.read_text(encoding="utf-8"))
    page = next(e for e in raw["visual_pages"] if e.get("type") == "depth_guide")["config"]
    cfg = import_legacy_guidance(raw)
    assert cfg.depth.thresh_method == page["depth_thresh_method"]
    assert cfg.depth.depth_min_mm == float(page["depth_seg_min_mm"])
    assert cfg.depth.depth_max_mm == float(page["depth_seg_max_mm"])   # 旧键 depth_seg_max_mm
    assert cfg.depth.avg_depth_enable == bool(page["avg_depth_enable"])
    assert cfg.depth.avg_depth_diameter == float(page["avg_depth_diameter"])
    assert cfg.offsets.coarse.x_axis == page["robot_coarse_x_axis"]
    assert cfg.offsets.tcp_x == float(page["robot_tcp_x"])
    assert cfg.circle["circle_type"] == page["circle_type"]


def test_legacy_import_rejects_missing_page():
    with pytest.raises(ValueError):
        import_legacy_guidance({"visual_pages": [{"type": "monitor"}]})


def test_legacy_import_maps_seg_and_avg_depth_keys():
    """旧 UI 的键名例外：depth_seg_* / avg_depth_*（DEC-059 修的是前缀拼接漏映射）。"""
    raw = {"visual_pages": [{"type": "depth_guide", "config": {
        "depth_seg_min_mm": 123.0,
        "depth_seg_max_mm": 456.0,
        "avg_depth_enable": True,
        "avg_depth_diameter": 7.5,
        "depth_thresh_method": "binary_inv",
        "circle_type": "最小圆",
    }}]}
    cfg = import_legacy_guidance(raw)
    assert cfg.depth.depth_min_mm == 123.0
    assert cfg.depth.depth_max_mm == 456.0
    assert cfg.depth.avg_depth_enable is True
    assert cfg.depth.avg_depth_diameter == 7.5


def test_validate_reports_bad_values():
    st = GuidanceConfigStore(None, seed_if_missing=False)
    st.load()
    st._config.depth.thresh_method = "nope"           # noqa: SLF001
    st._config.depth.depth_min_mm = 5000.0            # noqa: SLF001
    st._config.depth.depth_max_mm = 100.0             # noqa: SLF001
    st._config.circle = {}                            # noqa: SLF001
    issues = " | ".join(validate(st.config))
    assert "二值化方法" in issues and "深度切分区间" in issues and "circle_type" in issues


# ---------------------------------------------------------------- DEC-065 多套精定位参数
def test_fine_profiles_roundtrip_and_offsets_for(tmp_path):
    path = str(tmp_path / "g.yaml")
    st = GuidanceConfigStore(path, seed_if_missing=False)
    cfg = st.load()
    cfg.fine_profiles.append(FineOffsetProfile(
        profile_id="第二套", fine=AxisMapping("X", 1, "Z", 1),
        tcp_x=50.0, tcp_y=0.0, desc="测试"))
    st.save()
    again = GuidanceConfigStore(path, seed_if_missing=False).load()
    assert again.fine_profile_ids() == ["第二套"]
    params = again.offsets_for("第二套")
    assert params is not None
    assert params.fine == AxisMapping("X", 1, "Z", 1)
    assert params.tcp_x == 50.0
    assert again.offsets_for("不存在") is None
    assert again.offsets_for(None) == again.offsets


def test_detect_uses_selected_fine_profile(tmp_path):
    st = _store(tmp_path)
    cfg = st.config
    cfg.fine_profiles.append(FineOffsetProfile(
        profile_id="第二套", fine=AxisMapping("X", 1, "Z", 1),
        tcp_x=50.0, tcp_y=0.0))
    svc = GuidanceService(st, camera=None, host_proxy=FakeHost())
    base = svc.detect(frame=_frame())
    alt = svc.detect(frame=_frame(), fine_profile_ref="第二套")
    assert base.ok and alt.ok
    assert alt.fine != base.fine
    bad = svc.detect(frame=_frame(), fine_profile_ref="不存在")
    assert not bad.ok and "不存在" in bad.message


# ---------------------------------------------------------------- DEC-069 横向/纵向 tcp 分离
def test_fine_offset_ignores_tcp_z_and_zero_lateral_equals_coarse():
    """DEC-069：精定位只减横向 tcp_x/tcp_y；tcp_z（沿光轴距离）不影响横向偏移。

    相机与工作位同轴（相机在套筒正上方）→ tcp_x=tcp_y=0，精定位=粗定位。
    """
    mapping = AxisMapping("Z", -1, "X", -1)
    cam = (46.25, 5.09, 198.0)
    coaxial = OffsetParams(coarse=mapping, fine=mapping,
                           tcp_x=0.0, tcp_y=0.0, tcp_z=104.0)
    coarse, fine = compute_robot_offsets(cam, coaxial)
    assert fine == coarse                       # 纵向 104 不再被当成横向补偿
    # tcp_z 变化不影响横向结果（只登记/展示）
    other = OffsetParams(coarse=mapping, fine=mapping,
                         tcp_x=0.0, tcp_y=0.0, tcp_z=250.0)
    assert compute_robot_offsets(cam, other) == (coarse, fine)
    # 横向错位仍按原公式生效（旧机型保持原行为）
    lateral = OffsetParams(coarse=mapping, fine=mapping, tcp_x=104.0, tcp_z=0.0)
    _, fine_lateral = compute_robot_offsets(cam, lateral)
    assert fine_lateral != coarse


def test_tcp_z_roundtrip_default_and_profile(tmp_path):
    path = str(tmp_path / "g.yaml")
    st = GuidanceConfigStore(path, seed_if_missing=False)
    cfg = st.load()
    cfg.offsets = OffsetParams(coarse=cfg.offsets.coarse, fine=cfg.offsets.fine,
                               tcp_x=0.0, tcp_y=0.0, tcp_z=104.0)
    cfg.fine_profiles.append(FineOffsetProfile(
        profile_id="纵向套", tcp_x=0.0, tcp_y=0.0, tcp_z=88.5, desc="测试"))
    st.save()
    again = GuidanceConfigStore(path, seed_if_missing=False).load()
    assert again.offsets.tcp_z == 104.0
    params = again.offsets_for("纵向套")
    assert params is not None and params.tcp_z == 88.5


# ---------------------------------------------------------------- DEC-109 公开抓图
class _FakeCam:
    def __init__(self, frame) -> None:
        self.frame = frame

    def capture(self, _req):
        from types import SimpleNamespace
        return SimpleNamespace(errors={}, frames={"ds86_01": self.frame})


def test_capture_frame_grabs_once_without_detection_and_records_last_frame(tmp_path):
    """「运动方案」页单次视觉检测用：只抓帧，不跑深度引导/找圆。"""
    from types import SimpleNamespace
    frame = SimpleNamespace(frame_id="f-1", color=None, depth=None)
    svc = GuidanceService(_store(tmp_path), camera=_FakeCam(frame),
                          host_proxy=None)
    got = svc.capture_frame()
    assert got is frame
    assert svc.last_frame is frame
