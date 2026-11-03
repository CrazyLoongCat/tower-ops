"""视觉调试页「图像算法」参数表与取值对象 (E19 增强)。

**单一事实源**: 界面控件、持久化字段与离线用例都从 :data:`PARAM_GROUPS` 生成/校验 ——
新增一个参数只改本文件, 不用改界面代码, 也不会出现「界面上有、配置里没有」。

单位与作用域约定:
- 长度/半径/面积参数一律**像素**(检测类指标不换算 mm, 见 03 §7.2)。
- **例外: 深度切分上下限是毫米(mm)** —— 深度原始值即毫米量化
  (本工程各通道 ``depth_scale_m = 0.001``, 1 原始值 = 1 mm; 旧程序
  ``_depth_to_colormap`` 与配置项 ``depth_seg_min_mm/depth_seg_max_mm`` 同此口径,
  DS86 侧 ``DEPTH_SCALE_M`` 注释亦写明"深度原始单位 = 毫米")。
  面板给值一律 mm, 应用前按**该帧** ``depth_scale_m`` 换算成原始值
  (见 :func:`..ops.depth_to_rgb` 的 ``depth_scale_m`` 形参); 设计基线 03 §7.2 的
  "只有像素距离不能标 mm" 说的是**测量结果**, 与深度量程标注不是一回事。
- ``scope`` 标明参数作用在哪一路: ``color`` / ``depth`` / ``both``。
  几何变换与十字线对两路同时生效(与旧工程一致), 其余只作用于所注明的一路。
- 组上可带 ``panel=False``: 该组不出现在「图像算法」面板 (但参数保留在数据模型里),
  面板据此只渲染 :data:`PANEL_GROUPS`; 参数上可带 ``same_row=True`` 表示与上一个参数排同一行。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace
from typing import Any

__all__ = [
    "PARAM_GROUPS", "PARAM_SPECS", "PARAM_KEYS", "PANEL_GROUPS", "PANEL_KEYS",
    "SCOPES", "ImageOpsParams", "coerce_params", "dataclass_field_names",
    "default_params", "group_of", "keys_in_scope", "param_spec",
]


# 参数作用域
SCOPES = ("color", "depth", "both")

# ---------------------------------------------------------------- 参数表
# kind: bool / choice / int / float
# scope: color / depth / both
PARAM_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "group": "geometry",
        "label": "几何变换",
        # 不再放「照旧程序…」的说明文字 (用户 2026-10-16 要求去掉); 镜像两路同步生效。
        "params": (
            {"key": "hflip", "label": "水平镜像", "kind": "bool", "default": False,
             "scope": "both"},
            # same_row: 与上一个参数排在同一行 (面板据此把 水平/垂直镜像 并排)
            {"key": "vflip", "label": "垂直镜像", "kind": "bool", "default": False,
             "same_row": True, "scope": "both"},
            {"key": "rotate", "label": "旋转", "kind": "choice", "default": 0,
             "choices": ((0, "0°"), (90, "90°"), (180, "180°"), (270, "270°")),
             "scope": "both"},
        ),
    },
    {
        "group": "crosshair",
        "label": "十字线",
        # panel=False: 只在「图像算法」面板上不显示; 参数与 draw_crosshair 仍保留
        # (用户 2026-10-16 要求从面板移除, 但底层不删)。
        "panel": False,
        "note": "照旧程序 draw_crosshair: 按画面百分比定位",
        "params": (
            {"key": "crosshair", "label": "显示十字线", "kind": "bool", "default": False,
             "scope": "both"},
            {"key": "cross_x_pct", "label": "竖线位置 X%", "kind": "int", "default": 50,
             "min": 0, "max": 100, "step": 1, "suffix": " %", "scope": "both"},
            {"key": "cross_y_pct", "label": "横线位置 Y%", "kind": "int", "default": 50,
             "min": 0, "max": 100, "step": 1, "suffix": " %", "scope": "both"},
        ),
    },
    {
        "group": "display",
        "label": "显示增强",
        # panel=False: 同「十字线」—— 面板不显示, apply_brightness_contrast 与参数保留。
        "panel": False,
        "note": "灰度化 + 亮度/对比度; 只作用于彩色路, 结果图仍在彩色位显示",
        "params": (
            {"key": "grayscale", "label": "转灰度", "kind": "bool", "default": False,
             "scope": "color"},
            {"key": "brightness", "label": "亮度", "kind": "int", "default": 0,
             "min": -100, "max": 100, "step": 1, "scope": "color"},
            {"key": "contrast", "label": "对比度", "kind": "float", "default": 1.0,
             "min": 0.1, "max": 3.0, "step": 0.05, "decimals": 2, "scope": "color"},
        ),
    },
    {
        "group": "denoise",
        "label": "降噪",
        "note": "高斯/中值/双边; 核大小会自动取奇数",
        "params": (
            {"key": "denoise", "label": "方式", "kind": "choice", "default": "none",
             "choices": (("none", "不降噪"), ("gaussian", "高斯"), ("median", "中值"),
                         ("bilateral", "双边(保边)")),
             "scope": "color"},
            {"key": "denoise_k", "label": "核大小", "kind": "int", "default": 3,
             "min": 1, "max": 31, "step": 2, "scope": "color"},
        ),
    },
    {
        "group": "threshold",
        "label": "二值化",
        "note": "手动阈值 / Otsu 自动; 复用 depth_guidance.binarize, 判据与旧程序一致",
        "params": (
            {"key": "threshold_mode", "label": "方式", "kind": "choice", "default": "none",
             "choices": (("none", "不二值化"), ("binary", "大于阈值=白"),
                         ("binary_inv", "小于阈值=白"), ("otsu", "Otsu 自动")),
             "scope": "color"},
            {"key": "threshold_value", "label": "阈值", "kind": "int", "default": 127,
             "min": 0, "max": 255, "step": 1, "scope": "color"},
        ),
    },
    {
        "group": "edges",
        "label": "边缘检测",
        "note": "Canny 双阈值; 同时被「直线检测」复用",
        "params": (
            {"key": "edge", "label": "显示边缘图", "kind": "bool", "default": False,
             "scope": "color"},
            {"key": "canny_low", "label": "低阈值", "kind": "int", "default": 50,
             "min": 0, "max": 255, "step": 1, "scope": "color"},
            {"key": "canny_high", "label": "高阈值", "kind": "int", "default": 150,
             "min": 0, "max": 255, "step": 1, "scope": "color"},
        ),
    },
    {
        "group": "morphology",
        "label": "形态学",
        "note": "开/闭/膨胀/腐蚀; 复用 depth_guidance.morphology, 核自动取奇数",
        "params": (
            {"key": "morph_op", "label": "操作", "kind": "choice", "default": "none",
             "choices": (("none", "不做"), ("open", "开运算"), ("close", "闭运算"),
                         ("erode", "腐蚀"), ("dilate", "膨胀")),
             "scope": "color"},
            {"key": "morph_k", "label": "内核", "kind": "int", "default": 3,
             "min": 1, "max": 31, "step": 2, "scope": "color"},
        ),
    },
    {
        "group": "depth",
        "label": "深度伪彩",
        "note": ("色标 + 深度伪彩映射区间(照旧程序 _depth_to_colormap 的映射区间); "
                 "只作用于深度路; 无效深度(0/65535)一律压黑。"
                 "单位 mm：深度原始值即毫米(1 原始值 = 1 mm), 应用时按本帧深度比例换算；"
                 "D405 理想工作距离约 70~500 mm; DS86 现场显示区间约 100~300 mm"
                 "(未单独保存过的 DS86 通道会按引导配置的现场区间自动填, 见 DEC-046)"),
        "params": (
            {"key": "depth_colormap", "label": "色标", "kind": "choice", "default": "auto",
             "choices": (("auto", "内置色标"), ("jet", "JET"), ("turbo", "TURBO"),
                         ("bone", "BONE"), ("hot", "HOT"), ("gray", "GRAY")),
             "scope": "depth"},
            # 深度切分上下限: **毫米(mm)** —— 深度原始值 = 毫米量化(本工程 depth_scale_m=0.001);
            # 旧程序同名字段 depth_seg_min_mm/depth_seg_max_mm。落回原始数组时按帧 depth_scale 换算。
            # 照旧程序: 超范围像素夹紧到色标两端, 只有 0/65535 才算无效压黑。
            {"key": "depth_seg_min_mm", "label": "切分下限 (mm)", "kind": "int",
             "default": 200, "min": 0, "max": 65535, "step": 1, "scope": "depth"},
            {"key": "depth_seg_max_mm", "label": "切分上限 (mm)", "kind": "int",
             "default": 3000, "min": 0, "max": 65535, "step": 1, "scope": "depth"},
        ),
    },
    {
        "group": "detect",
        "label": "检测类",
        "note": ("在显示图上做像素级检测并画框; 结果只作调参参考, 不参与「运行判定」。"
                 "长度/面积一律像素, 不换算 mm。圆/直线检测复用「边缘检测」的双阈值"),
        "params": (
            {"key": "detect", "label": "检测方式", "kind": "choice", "default": "none",
             "choices": (("none", "不做检测"), ("contour", "轮廓/面积筛选"),
                         ("circle", "圆检测"), ("line", "直线检测")),
             "scope": "color"},
            {"key": "detect_min_area", "label": "面积下限", "kind": "float", "default": 50.0,
             "min": 0.0, "max": 1e7, "step": 10.0, "decimals": 1, "suffix": " px²",
             "scope": "color"},
            {"key": "detect_max_area", "label": "面积上限", "kind": "float",
             "default": 1e9, "min": 0.0, "max": 1e9, "step": 100.0, "decimals": 1,
             "suffix": " px²", "scope": "color"},
            {"key": "detect_top_n", "label": "最多输出", "kind": "int", "default": 1,
             "min": 1, "max": 50, "step": 1, "scope": "color"},
            {"key": "circle_accum", "label": "圆·累加器阈值", "kind": "int", "default": 30,
             "min": 1, "max": 500, "step": 1,
             "note": "越小越容易出圆 (误检也多)", "scope": "color"},
            {"key": "circle_min_radius", "label": "圆最小半径", "kind": "int", "default": 5,
             "min": 1, "max": 2000, "step": 1, "suffix": " px", "scope": "color"},
            {"key": "circle_max_radius", "label": "圆最大半径 (0=不限)", "kind": "int",
             "default": 0, "min": 0, "max": 4000, "step": 1, "suffix": " px",
             "scope": "color"},
            {"key": "line_votes", "label": "直线·票数阈值", "kind": "int", "default": 80,
             "min": 1, "max": 500, "step": 1,
             "note": "越小越容易出直线 (误检也多)", "scope": "color"},
            {"key": "line_min_len", "label": "直线最短长度", "kind": "int", "default": 25,
             "min": 1, "max": 2000, "step": 1, "suffix": " px", "scope": "color"},
        ),
    },
)

# 扁平化的参数描述 (界面/持久化/用例都遍历它)
PARAM_SPECS: tuple[dict[str, Any], ...] = tuple(
    {**p, "group": g["group"], "group_label": g["label"]}
    for g in PARAM_GROUPS for p in g["params"]
)
PARAM_KEYS: tuple[str, ...] = tuple(p["key"] for p in PARAM_SPECS)

# 面板可见参数: 页面只渲染这些组 (group.get("panel", True) is True)。
# 隐藏组 (如 十字线/显示增强) 的参数**仍在** PARAM_SPECS/_KEYS/dataclass 里,
# 持久化与纯函数照常可用, 只是界面不再给控件 —— 用户 2026-10-16「只从面板移除」。
PANEL_GROUPS: tuple[dict[str, Any], ...] = tuple(
    g for g in PARAM_GROUPS if g.get("panel", True))
PANEL_KEYS: tuple[str, ...] = tuple(p["key"] for g in PANEL_GROUPS for p in g["params"])

_BY_KEY: dict[str, dict[str, Any]] = {p["key"]: p for p in PARAM_SPECS}

# 旧键名兼容: DEC-023 期间用的 depth_seg_min/depth_seg_max 没标单位, DEC-025 起改名为
# *_mm。已经被存进设备配置/检测快照的旧键名照样认(新键名在场时以新键名为准), 免得
# 旧值被"未知键忽略"规则静默丢掉、回落成默认值。
_LEGACY_KEY_ALIASES: dict[str, str] = {
    "depth_seg_min": "depth_seg_min_mm",
    "depth_seg_max": "depth_seg_max_mm",
}


def param_spec(key: str) -> dict[str, Any]:
    """取参数描述; 未知键直接 KeyError (不静默放过拼错的键名)。"""
    return _BY_KEY[key]


# ---------------------------------------------------------------- 取值对象
@dataclass
class ImageOpsParams:
    """图像算法取值。

    字段与 :data:`PARAM_SPECS` 一一对应(由 :func:`coerce_params` 保证):
    未知键忽略, 缺省键取默认值, 数值按 spec 的 min/max 夹紧。
    """

    # 几何
    hflip: bool = False
    vflip: bool = False
    rotate: int = 0
    # 十字线
    crosshair: bool = False
    cross_x_pct: int = 50
    cross_y_pct: int = 50
    # 显示
    grayscale: bool = False
    brightness: int = 0
    contrast: float = 1.0
    # 降噪
    denoise: str = "none"
    denoise_k: int = 3
    # 二值化
    threshold_mode: str = "none"
    threshold_value: int = 127
    # 边缘
    edge: bool = False
    canny_low: int = 50
    canny_high: int = 150
    # 形态学
    morph_op: str = "none"
    morph_k: int = 3
    # 深度伪彩 (切分上下限单位 **mm**; 旧键名 depth_seg_min/max 仍认, 见 coerce_params)
    depth_colormap: str = "auto"
    depth_seg_min_mm: int = 200
    depth_seg_max_mm: int = 3000
    # 检测
    detect: str = "none"
    detect_min_area: float = 50.0
    detect_max_area: float = 1e9
    detect_top_n: int = 1
    circle_accum: int = 30
    circle_min_radius: int = 5
    circle_max_radius: int = 0
    line_votes: int = 80
    line_min_len: int = 25

    # ---------- 持久化 ----------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> "ImageOpsParams":
        return coerce_params(raw)

    def copy(self, **changes: Any) -> "ImageOpsParams":
        return replace(self, **changes)

    def is_default(self) -> bool:
        return self.to_dict() == default_params().to_dict()

    def changed_keys(self) -> tuple[str, ...]:
        """与默认值不同的参数键 (顺序同 PARAM_SPECS)。"""
        base = default_params()
        return tuple(k for k in PARAM_KEYS if getattr(self, k) != getattr(base, k))


def default_params() -> ImageOpsParams:
    return ImageOpsParams()


def _clamp(value: Any, spec: dict[str, Any]) -> Any:
    kind = spec["kind"]
    if kind == "bool":
        return bool(value)
    if kind == "choice":
        allowed = [c[0] for c in spec["choices"]]
        value = value if value in allowed else spec["default"]
        # 数值型 choice 统一成 int(如 rotate)
        return int(value) if all(isinstance(a, int) and not isinstance(a, bool)
                                 for a in allowed) else value
    if kind == "int":
        try:
            n = int(round(float(value)))
        except (TypeError, ValueError):
            n = int(spec["default"])
        return max(int(spec["min"]), min(int(spec["max"]), n))
    # float
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = float(spec["default"])
    return max(float(spec["min"]), min(float(spec["max"]), f))


def coerce_params(raw: Any) -> ImageOpsParams:
    """把任意来源(配置文件/界面)的取值收敛成合法参数。

    规则: 非映射→全默认; 未知键忽略; 缺键取默认; 越界夹紧; 非法枚举回默认;
    旧键名(``depth_seg_min/max``)按 :data:`_LEGACY_KEY_ALIASES` 改名后参与收敛。
    """
    out: dict[str, Any] = {}
    src = dict(raw) if isinstance(raw, dict) else {}
    for old, new in _LEGACY_KEY_ALIASES.items():
        if old in src and new not in src:
            src[new] = src[old]
    for spec in PARAM_SPECS:
        key = spec["key"]
        out[key] = _clamp(src.get(key, spec["default"]), spec)
    return ImageOpsParams(**out)


def group_of(key: str) -> str:
    return param_spec(key)["group"]


def depth_display_range_mm(camera_id: Any = None, image_ops: Any = None,
                           ds86_cfg: Any = None) -> tuple[float, float]:
    """深度伪彩**显示**区间 (mm)：通道显式参数 > DS86 旧工程显示区间 > 默认。

    - 通道 ``image_ops`` 里显式存了 ``depth_seg_min_mm/max_mm``（含旧键名）→ 以它为准；
    - 否则 DS86 通道用旧工程 ``ds86_depth_map_min_mm/max_mm``（guidance_locate 现场
      调好的 100–300 mm，随 guidance.yaml 导入）。**注意与判定口径分开**：旧工程
      ``depth_seg_min_mm/max_mm``（200–3000 mm）是深度引导的切分范围，不拿来当显示映射；
    - 其它相机 / DS86 配置缺失或非法 → image_ops 默认（200–3000 mm）。

    背景（DEC-046）：伪彩映射区间若远大于现场距离，所有像素落到色标最低端，
    看起来就是"深度图全黑"——数据其实有，只是被映射压扁了。
    """
    src = dict(image_ops) if isinstance(image_ops, dict) else {}
    explicit = any(k in src for k in ("depth_seg_min_mm", "depth_seg_max_mm",
                                      "depth_seg_min", "depth_seg_max"))
    params = coerce_params(src)
    default = (float(params.depth_seg_min_mm), float(params.depth_seg_max_mm))
    if explicit or not str(camera_id or "").lower().startswith("ds86"):
        return default
    cfg = dict(ds86_cfg) if isinstance(ds86_cfg, dict) else {}
    raw_lo = cfg.get("ds86_depth_map_min_mm", cfg.get("depth_map_min_mm"))
    raw_hi = cfg.get("ds86_depth_map_max_mm", cfg.get("depth_map_max_mm"))
    try:
        lo, hi = float(raw_lo), float(raw_hi)
    except (TypeError, ValueError):
        return default
    if not (hi > lo > 0):
        return default
    return lo, hi


def keys_in_scope(scope: str) -> tuple[str, ...]:
    """作用域为 scope 或 both 的参数键 (供渲染分工与用例检查)。"""
    assert scope in ("color", "depth"), scope
    return tuple(p["key"] for p in PARAM_SPECS if p["scope"] in (scope, "both"))


def dataclass_field_names() -> tuple[str, ...]:
    """dataclass 字段名 (用例用它守住「参数表 ⊆ 字段」不变式)。"""
    return tuple(f.name for f in fields(ImageOpsParams))
