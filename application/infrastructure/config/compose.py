"""配置合成与五层全量验证 (E16, 02 §10)。

合成顺序 (总纲 §4.4): **已批准站点默认 → 已发布设备/动作/标定 → 配方允许参数 →
工位允许 override**。每层合成前检查 override 白名单与受保护路径; 合成后做五层全量验证。
运行快照冻结为不可变副本, 运行中编辑草稿不影响在途快照。
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

from application.infrastructure.config.base import (
    CONFIG_SCHEMA_VERSION,
    ProtectedPathError,
    is_protected,
    require_schema_version,
)

SECTIONS = ("site", "devices", "actions", "interlocks", "calibrations",
            "vision_pipelines", "recipes", "models")


@dataclass
class ComposeInputs:
    """四层输入, 按顺序合成。"""

    site_defaults: dict = field(default_factory=dict)
    published: dict = field(default_factory=dict)
    recipe_allowed: dict = field(default_factory=dict)
    station_overrides: dict = field(default_factory=dict)


@dataclass
class ConfigReport:
    recipe_id: str = ""
    layers: dict[str, list[str]] = field(default_factory=dict)

    def add(self, layer: str, msg: str) -> None:
        self.layers.setdefault(layer, []).append(msg)

    @property
    def errors(self) -> list[str]:
        return [f"[{l}] {m}" for l, ms in self.layers.items() for m in ms]

    @property
    def ok(self) -> bool:
        return not self.layers


@dataclass
class ConfigCatalog:
    """验证引用所需的外部事实 (已注册的设备/动作/流水线/标定/模型/子流程 id)。"""

    device_ids: set = field(default_factory=set)
    action_ids: set = field(default_factory=set)
    pipeline_ids: set = field(default_factory=set)
    calibration_ids: set = field(default_factory=set)
    model_ids: set = field(default_factory=set)
    subflow_ids: set = field(default_factory=set)


# ---------------- 合成 ----------------
def _flatten_dotted(data: dict, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in data.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten_dotted(v, key + "."))
        else:
            out[key] = v
    return out


def check_override_paths(layer: dict, *, layer_name: str) -> None:
    """合成前: 受保护路径一律拒绝 (设备寻址/互锁/停止/硬限值/固件)。"""
    for path in _flatten_dotted(layer):
        if is_protected(path):
            raise ProtectedPathError(
                f"{layer_name} 试图覆盖受保护路径 {path!r} (寻址/互锁/停止/硬限值不可改)")


def _merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def compose_config(inputs: ComposeInputs, *,
                   schema_version: int = CONFIG_SCHEMA_VERSION) -> dict:
    """按批准顺序合成。

    受保护路径检查只作用于**配方允许参数**与**工位 override** 两层:
    设备寻址/互锁/停止/硬限值只能由已发布的设备/动作/标定基线提供, 不能被配方或工位改写。
    """
    require_schema_version({"schema_version": schema_version})
    merged: dict = {"schema_version": schema_version}
    # 第 1 层: 已批准站点默认 (基线, 可含受保护节)
    for sec in SECTIONS:
        if sec in inputs.site_defaults:
            merged[sec] = copy.deepcopy(inputs.site_defaults[sec])
    # 第 2 层: 已发布设备/动作/标定 (发布门控, 不受 override 白名单限制; 发布即生效见 DEC-039)
    published = {k: v for k, v in inputs.published.items() if k != "schema_version"}
    _merge(merged, published)
    # 第 3~4 层: 配方允许参数 / 工位 override —— 必须过受保护路径检查
    for layer, name in ((inputs.recipe_allowed, "recipe_allowed"),
                        (inputs.station_overrides, "station_overrides")):
        if not layer:
            continue
        payload = {k: v for k, v in layer.items() if k != "schema_version"}
        check_override_paths(payload, layer_name=name)
        _merge(merged, payload)
    if "schema_version" not in merged:
        raise ValueError("合成结果缺 schema_version")
    return merged


# ---------------- 冻结快照 ----------------
@dataclass(frozen=True)
class FrozenConfig:
    data: dict
    hash: str

    def as_dict(self) -> dict:
        return copy.deepcopy(self.data)


def freeze_config(merged: dict) -> FrozenConfig:
    """冻结为不可变运行快照: 深拷贝 + hash; 之后编辑草稿不影响本快照。"""
    frozen = copy.deepcopy(merged)
    payload = json.dumps(frozen, sort_keys=True, ensure_ascii=False, default=str)
    return FrozenConfig(data=frozen, hash=hashlib.sha256(payload.encode()).hexdigest())


# ---------------- 五层验证 ----------------
def validate_composed(merged: dict, catalog: ConfigCatalog | None = None) -> ConfigReport:
    """层1 结构 -> 层2 值/单位 -> 层3 引用 -> 层4 能力 -> 层5 资源/业务。"""
    rep = ConfigReport()
    catalog = catalog or ConfigCatalog()

    # 层1 结构
    try:
        require_schema_version(merged)
    except Exception as exc:  # noqa: BLE001
        rep.add("structure", str(exc))
    for key in merged:
        if key != "schema_version" and key not in SECTIONS:
            rep.add("structure", f"未知配置节 {key!r} (拒绝未定义对象)")
    for sec in SECTIONS:
        if sec in merged and not isinstance(merged[sec], dict):
            rep.add("structure", f"节 {sec!r} 必须是映射")

    devices = merged.get("devices", {}) or {}
    actions = merged.get("actions", {}) or {}
    calibrations = merged.get("calibrations", {}) or {}
    pipelines = merged.get("vision_pipelines", {}) or {}
    interlocks = merged.get("interlocks", {}) or {}
    models = merged.get("models", {}) or {}

    # 层2 值/单位
    for aid, a in actions.items():
        if not isinstance(a, dict):
            rep.add("value", f"动作 {aid!r} 定义非法")
            continue
        for p in (a.get("parameters") or []):
            lo, hi = p.get("min"), p.get("max")
            if lo is not None and hi is not None and float(lo) > float(hi):
                rep.add("value", f"动作 {aid!r} 参数 {p.get('name')!r} min>max")
        if not a.get("resource_ids"):
            rep.add("value", f"动作 {aid!r} 未声明 resource_ids")
    for cid, c in calibrations.items():
        units = (c or {}).get("units", "mm")
        if units != "mm":
            rep.add("value", f"标定 {cid!r} 单位 {units!r} 非 mm (需显式换算)")

    # 层3 引用存在
    known_devices = set(devices) | set(catalog.device_ids)
    for aid, a in actions.items():
        dev = (a or {}).get("device_id")
        if dev and dev not in known_devices:
            rep.add("reference", f"动作 {aid!r} 引用未知设备 {dev!r}")
        for res in ((a or {}).get("resource_ids") or []):
            if str(res).startswith("device:") and res.split(":", 1)[1] not in known_devices:
                rep.add("reference", f"动作 {aid!r} 资源 {res!r} 指向未知设备")
    for pid, p in pipelines.items():
        for cam in ((p or {}).get("camera_refs") or []):
            if cam not in known_devices and cam not in set(calibrations):
                rep.add("reference", f"流水线 {pid!r} 引用未知相机 {cam!r}")
        for mid in ((p or {}).get("model_refs") or []):
            if mid not in set(models) | set(catalog.model_ids):
                rep.add("reference", f"流水线 {pid!r} 引用未知模型 {mid!r}")
        for aid in ((p or {}).get("algorithm_refs") or []):
            if aid not in set(catalog.action_ids) and aid not in set(actions):
                rep.add("reference", f"流水线 {pid!r} 引用未知算法 {aid!r}")
    for iid, it in interlocks.items():
        for res in ((it or {}).get("resource_ids") or []):
            pass  # 资源为自由字符串, 不做存在性检查

    # 层4 能力/设备绑定
    for aid, a in actions.items():
        dev = (a or {}).get("device_id")
        ch = (a or {}).get("channel")
        if dev and isinstance(devices.get(dev), dict):
            chans = set((devices[dev] or {}).get("channels") or [])
            if ch and chans and ch not in chans:
                rep.add("capability", f"动作 {aid!r} 的通道 {ch!r} 不在设备 {dev!r} 能力内")

    # 层5 资源/业务
    res_owner: dict[str, str] = {}
    for aid, a in actions.items():
        for res in ((a or {}).get("resource_ids") or []):
            if res in res_owner and res_owner[res] != aid:
                # 同资源多动作: 需有互锁声明, 否则冲突
                if res not in interlocks:
                    rep.add("business",
                            f"动作 {aid!r} 与 {res_owner[res]!r} 共用资源 {res!r} 但无互锁声明")
            else:
                res_owner[res] = aid
    motion = [aid for aid, a in actions.items()
              if any(str(r).startswith(("motion", "region")) for r in
                     ((a or {}).get("resource_ids") or []))]
    if motion and not (merged.get("interlocks") or {}).get("stop") \
            and "stop" not in (merged.get("interlocks") or {}):
        rep.add("business", "存在运动动作但 interlock 未声明停止规则")
    return rep


__all__ = [
    "ConfigCatalog", "ConfigReport", "ComposeInputs", "FrozenConfig", "SECTIONS",
    "check_override_paths", "compose_config", "freeze_config", "validate_composed",
]
