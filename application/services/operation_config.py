"""操作台「杆塔宽度 / 螺栓直径」计算的配置存储（DEC-148）。

文件：`<configs>/operations/manual_ops.yaml`。两个操作各一份配置：

    tower_width:
      camera: ds86_01            # 上次选择的相机（再次打开直接加载）
      algorithm: 默认             # 上次选择的算法（指向 algorithms 里的键）
      algorithms:                # 多套算法按名字区分；可在 yaml 里复制改名
        默认: {tol_mm: 8.0, ...}
    bolt_size:
      camera: ds86_01
      algorithm: 默认
      algorithms:
        默认: {circle_type: 最大圆, ...}

- 首次启动写默认（「默认」一套，参数见 ``DEFAULTS``）；已有文件**不覆盖**。
- 只存参数和选择，不存计算结果。
"""
from __future__ import annotations

import copy
import io
import os
from typing import Any, Dict, Optional

from application.infrastructure.config.atomic import atomic_write, rollback

try:  # 现场用 ruamel（保留字段顺序/注释）；沙箱无 ruamel 时退回 PyYAML
    from ruamel.yaml import YAML as _RuamelYAML
except Exception:  # noqa: BLE001
    _RuamelYAML = None  # type: ignore[assignment]

try:
    import yaml as _pyyaml
except Exception:  # noqa: BLE001
    _pyyaml = None  # type: ignore[assignment]


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        if _RuamelYAML is not None:
            data = _RuamelYAML(typ="safe").load(fh) or {}
        elif _pyyaml is not None:
            data = _pyyaml.safe_load(fh) or {}
        else:  # pragma: no cover - 两个 YAML 库都缺时才到这里
            raise RuntimeError("缺少 YAML 库（ruamel.yaml 或 PyYAML）")
    return data if isinstance(data, dict) else {}


def _dump_yaml(data: dict) -> str:
    buf = io.StringIO()
    if _RuamelYAML is not None:
        yaml = _RuamelYAML()
        yaml.default_flow_style = False
        yaml.allow_unicode = True
        yaml.dump(data, buf)
    elif _pyyaml is not None:
        _pyyaml.safe_dump(data, buf, allow_unicode=True, sort_keys=False)
    else:  # pragma: no cover
        raise RuntimeError("缺少 YAML 库（ruamel.yaml 或 PyYAML）")
    return buf.getvalue()


class OperationConfigStore:
    """manual_ops.yaml 的读写；配置对象是普通 dict（缺项用 DEFAULTS 合并补齐）。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = os.path.abspath(path) if path else None
        self._config: Optional[Dict[str, Any]] = None

    # ---------- 读/写 ----------
    def load(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        if self.path and os.path.exists(self.path):
            data = _load_yaml(self.path)
        cfg = _default_config()
        for op in OPS:
            src = data.get(op)
            if not isinstance(src, dict):
                continue
            algos = src.get("algorithms")
            if isinstance(algos, dict) and algos:
                cfg[op]["algorithms"] = {
                    str(name): {**copy.deepcopy(DEFAULTS[op]), **(p or {})}
                    for name, p in algos.items() if isinstance(p, dict)
                } or cfg[op]["algorithms"]
            if src.get("algorithm") is not None:
                # DEC-149：algorithm 可以是视觉检测 id，不限在 algorithms 里
                cfg[op]["algorithm"] = str(src["algorithm"])
            if src.get("camera"):
                cfg[op]["camera"] = str(src["camera"])
            try:
                cfg[op]["value_mm"] = float(src.get("value_mm") or 0.0)
            except (TypeError, ValueError):
                cfg[op]["value_mm"] = 0.0
        self._config = cfg
        return cfg

    def save(self) -> None:
        if self._config is None:
            raise RuntimeError("尚未 load()")
        if self.path is None:
            return
        atomic_write(self.path, _HEADER + _dump_yaml(self._config),
                     backup=True)

    def rollback(self) -> None:
        if self.path:
            rollback(self.path)
            self._config = None

    # ---------- 访问 ----------
    @property
    def config(self) -> Dict[str, Any]:
        if self._config is None:
            self.load()
        assert self._config is not None
        return self._config

    def algorithm_names(self, op: str) -> list[str]:
        return list(self.config[op]["algorithms"].keys())

    def selection(self, op: str) -> Dict[str, str]:
        cfg = self.config[op]
        return {"camera": str(cfg.get("camera") or ""),
                "algorithm": str(cfg.get("algorithm") or "默认")}

    def value(self, op: str) -> float:
        """全局参数里的计算值（杆塔宽度 mm / 螺栓直径 mm）。"""
        try:
            return float(self.config[op].get("value_mm") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def set_value(self, op: str, value_mm: float) -> None:
        self.config[op]["value_mm"] = float(value_mm)

    def set_selection(self, op: str, *, camera: Optional[str] = None,
                      algorithm: Optional[str] = None) -> None:
        cfg = self.config[op]
        if camera is not None:
            cfg["camera"] = str(camera)
        if algorithm is not None:
            # DEC-149：算法名即「视觉检测」的 detection_id（可为 ""=默认全局参数），
            # 不再限定必须在 algorithms 里。
            cfg["algorithm"] = str(algorithm)

    def algorithm_params(self, op: str, name: Optional[str] = None) -> Dict[str, Any]:
        """取某套算法的参数（缺省=当前选中那套）；返回副本。"""
        cfg = self.config[op]
        key = name or cfg.get("algorithm") or "默认"
        algo = cfg["algorithms"]
        params = algo.get(key) or algo.get("默认")
        if not params and algo:
            params = next(iter(algo.values()))
        return dict(params or {})


__all__ = ["BOLT", "BOLT_SIZES", "DEFAULTS", "OPS", "OperationConfigStore",
           "TWR", "nearest_bolt_size"]
