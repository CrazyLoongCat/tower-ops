"""ENV-MAIN 环境自检（仅用标准库，装依赖前也能运行）。

用途：一次输出解释器路径/版本/位数与关键依赖版本，便于回填 doc/实施记录/环境清单.json，
并据此把 A02 从"未验证"推进到带证据状态。

运行（ENV-MAIN 解释器）：
    E:\\AnacondaEnvs\\towerops\\python.exe scripts\\check_env.py

输入/输出说明：打印一份 JSON（位于 BEGIN/END 标记之间），另打印一行结论；
不联网、不连接任何设备、不修改环境。退出码 0 表示解释器自检通过（不表示依赖齐全）。
"""
from __future__ import annotations

import importlib.metadata as md
import json
import platform
import struct
import sys

# pyproject.toml 中 requires-python = ">=3.11,<3.13"
REQUIRES_PYTHON = (3, 11, 3, 13)  # (min_major, min_minor, max_major, max_minor) 半开区间
REQUIRED_DEPS = [
    "pydantic", "ruamel.yaml", "py_trees", "numpy", "opencv-python",
    "pyserial", "pyzmq",  # 主程序依赖（pyproject 声明）
    "PySide6",             # 仅 GUI 需要
    "pytest",              # 离线测试需要
]


def _python_ok() -> bool:
    v = sys.version_info
    return (v.major, v.minor) >= REQUIRES_PYTHON[:2] and (v.major, v.minor) < REQUIRES_PYTHON[2:]


def _dep_versions() -> tuple[dict[str, str], list[str]]:
    found: dict[str, str] = {}
    missing: list[str] = []
    for name in REQUIRED_DEPS:
        try:
            found[name] = md.version(name)
        except md.PackageNotFoundError:
            missing.append(name)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{name}({type(exc).__name__})")
    return found, missing


def main() -> int:
    deps, missing = _dep_versions()
    bits = struct.calcsize("P") * 8
    report = {
        "interpreter": sys.executable,
        "version": platform.python_version(),
        "version_info": list(sys.version_info[:3]),
        "bits": bits,
        "platform": platform.platform(),
        "requires_python": ">=3.11,<3.13",
        "requires_python_ok": _python_ok(),
        "dependencies": deps,
        "missing_or_unimportable": missing,
    }
    print("----- BEGIN env-report JSON -----")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("----- END env-report JSON -----")
    print()
    print(f"[check] 解释器: {report['interpreter']}")
    print(f"[check] 版本: {report['version']} ({bits}-bit)")
    print(f"[check] 版本满足 requires-python: {report['requires_python_ok']}")
    if missing:
        print(f"[check] 缺失/不可用依赖: {', '.join(missing)}")
        print("[check] 安装(主程序依赖): python -m pip install -e .")
        print("[check] 可选: python -m pip install PySide6 pytest")
    else:
        print("[check] 依赖齐全")
    return 0 if report["requires_python_ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
