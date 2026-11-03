"""引导定位配置：深度算法参数 + 轴映射 + 找圆参数 + DS86 采集（含旧值导入）。

数值来自旧工程 `Ganta_Crtl_System/vision_monitor/config/camera_config.json`
（DS86「ROBOT引导」页，**现场调参结果**）→ 每条带 `source: 旧工程实测导入`。

导入只做**结构与范围规范化**，不改数值：动它就等于把现场调好的参数换成没调过的。
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from io import StringIO
from typing import Any, Mapping

from ruamel.yaml import YAML

from application.algorithms.guidance.depth_guidance import (
    CENTER_MODES,
    MORPH_OPS,
    THRESH_METHODS,
    DepthGuidanceParams,
)
from application.algorithms.guidance.offsets import AxisMapping, OffsetParams
from application.services.recipes.service import atomic_write, rollback

CONFIG_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)
LEGACY_SOURCE = ("旧工程实测导入（Ganta_Crtl_System/vision_monitor/config/"
                 "camera_config.json 的 DS86 引导页）")

# 旧配置 → 深度参数 的字段映射。多数旧键 = `depth_` + 字段名，但有几个例外：
#   - 切分上下限：旧 UI 存的是 depth_seg_min_mm / depth_seg_max_mm（不是 depth_depth_min_mm）
#   - 圆心平均深度：旧 UI 存的是 avg_depth_enable / avg_depth_diameter（无 depth_ 前缀）
# 此前一律按 `depth_` 前缀拼接，导致这四个字段被静默忽略、退回默认值
# （depth_max 3000、平均深度关/20px），与旧程序实际用的现场值不一致（DEC-059）。
_DEPTH_LEGACY_KEYS = {
    "depth_min_mm": "depth_seg_min_mm",
    "depth_max_mm": "depth_seg_max_mm",
    "avg_depth_enable": "avg_depth_enable",
    "avg_depth_diameter": "avg_depth_diameter",
}
_DEPTH_KEYS = tuple(f for f in DepthGuidanceParams.__dataclass_fields__)

# 找圆参数（传给 cp38 宿主 detect_circle_imp）
_CIRCLE_KEYS = ("circle_type", "min_radius", "max_radius", "min_cover_ratio",
                "grad_thresh", "circle_fit_error", "arc_fit_error")
# DS86 采集/曝光（原样保留，供相机适配器使用）
_DS86_KEYS = ("ds86_fps", "ds86_work_mode", "ds86_align_mode",
              "ds86_tof_resolution", "ds86_color_resolution",
              "ds86_tof_exposure_mode", "ds86_tof_exposure_time",
              "ds86_color_exposure_mode", "ds86_color_exposure_time",
              "ds86_color_gain", "ds86_ir_gain", "ds86_hdr_enable",
              "ds86_hdr_low_exposure_time", "ds86_hdr_high_exposure_time",
              "ds86_confidence_filter_enable", "ds86_confidence_filter_threshold",
              "ds86_flying_pixel_filter_enable", "ds86_flying_pixel_filter_threshold",
              "ds86_spatial_filter_enable", "ds86_depth_map_min_mm",
              "ds86_depth_map_max_mm")


@dataclass
class FineOffsetProfile:
    """命名的**精定位偏移参数**（DEC-065）。

    默认精定位参数仍在 ``offsets.fine/tcp_x/tcp_y``；这里放**额外**的参数套，
    由执行方案的引导定位步骤按 ``fine_profile_ref`` 选用。轴映射/符号与 tcp 标定
    与默认套同义（只影响精定位，不影响粗定位）。
    """

    profile_id: str
    fine: AxisMapping = field(default_factory=AxisMapping)
    tcp_x: float = 0.0
    tcp_y: float = 0.0
    # DEC-069：沿相机光轴的纵向距离（只登记/展示，不参与横向补偿）
    tcp_z: float = 0.0
    desc: str = ""
    source: str = "本工程新建"

    def __post_init__(self) -> None:
        self.profile_id = str(self.profile_id or "").strip()
        if not self.profile_id:
            raise ValueError("精定位参数套 profile_id 不能为空")
        self.tcp_x = float(self.tcp_x)
        self.tcp_y = float(self.tcp_y)
        self.tcp_z = float(self.tcp_z)

    def to_dict(self) -> dict:
        return {"profile_id": self.profile_id,
                "fine": asdict(self.fine),
                "tcp_x": self.tcp_x, "tcp_y": self.tcp_y, "tcp_z": self.tcp_z,
                "desc": self.desc, "source": self.source}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FineOffsetProfile":
        fine = dict(data.get("fine") or {})
        return cls(profile_id=str(data.get("profile_id") or ""),
                   fine=AxisMapping(**fine) if fine else AxisMapping(),
                   tcp_x=float(data.get("tcp_x", 0.0)),
                   tcp_y=float(data.get("tcp_y", 0.0)),
                   tcp_z=float(data.get("tcp_z", 0.0)),
                   desc=str(data.get("desc", "")),
                   source=str(data.get("source", "")))


@dataclass
class GuidanceConfig:
    """整份引导定位配置。"""

    schema_version: int = CONFIG_SCHEMA_VERSION
    depth: DepthGuidanceParams = field(default_factory=DepthGuidanceParams)
    offsets: OffsetParams = field(default_factory=OffsetParams)
    circle: dict = field(default_factory=dict)
    ds86: dict = field(default_factory=dict)
    fine_profiles: list[FineOffsetProfile] = field(default_factory=list)
    source: str = LEGACY_SOURCE

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "depth": self.depth.to_dict(),
            "offsets": {
                "coarse": asdict(self.offsets.coarse),
                "fine": asdict(self.offsets.fine),
                "tcp_x": self.offsets.tcp_x,
                "tcp_y": self.offsets.tcp_y,
                "tcp_z": self.offsets.tcp_z,
            },
            "fine_profiles": [p.to_dict() for p in self.fine_profiles],
            "circle": dict(self.circle),
            "ds86": dict(self.ds86),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GuidanceConfig":
        if not isinstance(data, Mapping):
            raise ValueError("引导配置顶层必须是映射")
        version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"不支持的 schema_version={version!r}")
        off = dict(data.get("offsets") or {})
        coarse = dict(off.get("coarse") or {})
        fine = dict(off.get("fine") or {})
        profiles = []
        for raw in (data.get("fine_profiles") or []):
            profiles.append(FineOffsetProfile.from_dict(dict(raw or {})))
        return cls(
            schema_version=int(version),
            depth=DepthGuidanceParams.from_dict(dict(data.get("depth") or {})),
            offsets=OffsetParams(coarse=AxisMapping(**coarse) if coarse else AxisMapping(),
                                 fine=AxisMapping(**fine) if fine else AxisMapping(),
                                 tcp_x=float(off.get("tcp_x", 0.0)),
                                 tcp_y=float(off.get("tcp_y", 0.0)),
                                 tcp_z=float(off.get("tcp_z", 0.0))),
            circle=dict(data.get("circle") or {}),
            ds86=dict(data.get("ds86") or {}),
            fine_profiles=profiles,
            source=str(data.get("source", "")))

    # ---- 精定位参数解析（DEC-065） ----
    def fine_profile_ids(self) -> list[str]:
        return [p.profile_id for p in self.fine_profiles]

    def find_fine_profile(self, profile_id: str | None) -> FineOffsetProfile | None:
        ref = (profile_id or "").strip()
        if not ref:
            return None
        return next((p for p in self.fine_profiles if p.profile_id == ref), None)

    def offsets_for(self, fine_profile_ref: str | None = None) -> OffsetParams | None:
        """按精定位参数套返回完整 OffsetParams；空=默认套；未知=返回 None。"""
        ref = (fine_profile_ref or "").strip()
        if not ref:
            return self.offsets
        profile = self.find_fine_profile(ref)
        if profile is None:
            return None
        return OffsetParams(coarse=self.offsets.coarse, fine=profile.fine,
                            tcp_x=profile.tcp_x, tcp_y=profile.tcp_y,
                            tcp_z=profile.tcp_z)


def validate(cfg: GuidanceConfig) -> list[str]:
    """校验并报告问题（不修值）。"""
    issues: list[str] = []
    d = cfg.depth
    if d.thresh_method not in THRESH_METHODS:
        issues.append(f"二值化方法非法: {d.thresh_method!r}；可选 {THRESH_METHODS}")
    if d.morph_op not in MORPH_OPS:
        issues.append(f"形态学操作非法: {d.morph_op!r}；可选 {MORPH_OPS}")
    if d.center_mode not in CENTER_MODES:
        issues.append(f"中心方式非法: {d.center_mode!r}；可选 {CENTER_MODES}")
    if d.depth_min_mm >= d.depth_max_mm:
        issues.append(f"深度切分区间非法: {d.depth_min_mm} >= {d.depth_max_mm}")
    if d.rect_side_length <= 0:
        issues.append("正外接矩形边长必须为正")
    if d.avg_depth_diameter <= 0:
        issues.append("均值深度直径必须为正")
    if int(d.morph_kernel_size) < 1:
        issues.append("形态学内核必须为正")
    elif d.morph_op != "none" and int(d.morph_kernel_size) % 2 == 0:
        issues.append(f"形态学内核必须为奇数: {d.morph_kernel_size}")
    if float(d.depth_valid_min_mm) < 0:
        issues.append("深度有效下限不能为负")
    if not cfg.circle.get("circle_type"):
        issues.append("缺找圆类型 circle_type")
    seen: set[str] = set()
    for p in cfg.fine_profiles:
        if not p.profile_id:
            issues.append("精定位参数套 profile_id 不能为空")
        elif p.profile_id in seen:
            issues.append(f"精定位参数套 ID 重复: {p.profile_id!r}")
        seen.add(p.profile_id)
        if not (float(p.tcp_x) == float(p.tcp_x) and float(p.tcp_y) == float(p.tcp_y)
                and float(p.tcp_z) == float(p.tcp_z)):
            issues.append(f"精定位参数套 {p.profile_id!r} 的 tcp 含非有限值")
    if not (float(cfg.offsets.tcp_x) == float(cfg.offsets.tcp_x)
            and float(cfg.offsets.tcp_y) == float(cfg.offsets.tcp_y)
            and float(cfg.offsets.tcp_z) == float(cfg.offsets.tcp_z)):
        issues.append("默认精定位偏移的 tcp 含非有限值")
    return issues


# ---------------------------------------------------------------- 旧值导入
def _mapping(prefix: str, raw: Mapping[str, Any]) -> AxisMapping:
    axis = str(raw.get(f"robot_{prefix}_x_axis", "Z") or "Z").upper()
    sign = 1 if str(raw.get(f"robot_{prefix}_x_sign", "-")) == "+" else -1
    y_axis = str(raw.get(f"robot_{prefix}_y_axis", "X") or "X").upper()
    y_sign = 1 if str(raw.get(f"robot_{prefix}_y_sign", "-")) == "+" else -1
    return AxisMapping(x_axis=axis, x_sign=sign, y_axis=y_axis, y_sign=y_sign)


def import_legacy_guidance(camera_config: Mapping[str, Any], *,
                           page_name: str = "ROBOT引导") -> GuidanceConfig:
    """从旧 `camera_config.json` 的 DS86 引导页导入。找不到该页则报错（不猜）。"""
    page = None
    for entry in (camera_config.get("visual_pages") or []):
        if str(entry.get("name")) == page_name or entry.get("type") == "depth_guide":
            page = entry.get("config") or {}
            break
    if page is None:
        raise ValueError(f"旧配置里找不到引导页 {page_name!r}（拒绝用其它页凑数）")

    depth_raw = {}
    for key in _DEPTH_KEYS:
        legacy_key = _DEPTH_LEGACY_KEYS.get(key, f"depth_{key}")
        if legacy_key in page:
            depth_raw[key] = page[legacy_key]
    depth = DepthGuidanceParams.from_dict(depth_raw)

    offsets = OffsetParams(
        coarse=_mapping("coarse", page),
        fine=_mapping("fine", page),
        tcp_x=float(page.get("robot_tcp_x", 0.0) or 0.0),
        tcp_y=float(page.get("robot_tcp_y", 0.0) or 0.0))

    circle = {k: page[k] for k in _CIRCLE_KEYS if k in page}
    ds86 = {k: page[k] for k in _DS86_KEYS if k in page}
    return GuidanceConfig(depth=depth, offsets=offsets, circle=circle, ds86=ds86)


# ---------------------------------------------------------------- 读写
def _dump_yaml(data: dict) -> str:
    yml = YAML(typ="safe")
    yml.default_flow_style = False
    yml.width = 4096
    yml.indent(mapping=2, sequence=4, offset=2)
    yml.sort_base_mapping_type_on_output = False
    buf = StringIO()
    yml.dump(data, buf)
    return buf.getvalue()


class GuidanceConfigStore:
    """引导配置的读写（单文件原子落盘）。"""

    def __init__(self, path: str | None = None, *, seed_if_missing: bool = True) -> None:
        self.path = os.path.abspath(path) if path else None
        self._seed_if_missing = seed_if_missing
        self._config: GuidanceConfig | None = None

    def load(self) -> GuidanceConfig:
        if self.path and os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self._config = GuidanceConfig.from_dict(YAML(typ="safe").load(fh) or {})
        elif self._seed_if_missing:
            self._config = GuidanceConfig()
            self.save()
        else:
            self._config = GuidanceConfig()
        return self._config

    def save(self) -> None:
        if self._config is None:
            raise RuntimeError("尚未 load()")
        if self.path is None:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        header = ("# 引导定位配置（DS86 深度引导）。由程序维护。\n"
                  "# source 标注来源：旧工程实测导入 的值是现场调参结果，\n"
                  "# 不代表本工程已重新调过参。\n")
        atomic_write(self.path, header + _dump_yaml(self._config.to_dict()), backup=True)

    def rollback(self) -> None:
        if self.path:
            rollback(self.path)
            self._config = None

    @property
    def config(self) -> GuidanceConfig:
        if self._config is None:
            self.load()
        assert self._config is not None
        return self._config


__all__ = [
    "CONFIG_SCHEMA_VERSION", "FineOffsetProfile", "GuidanceConfig",
    "GuidanceConfigStore", "LEGACY_SOURCE", "import_legacy_guidance", "validate",
]
