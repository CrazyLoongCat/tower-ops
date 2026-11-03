"""cp38 圆检测宿主自检 (A03) —— 用 ENV-CIRCLE 的 3.8 解释器运行。

目的: 验证 **原生件加载 / 合法输出 / 正常退出** 与 **IPC 端到端** 两条链路。

运行 (ENV-CIRCLE 解释器):
    E:\\AnacondaEnvs\\towerops-circle\\python.exe scripts\\check_native_circle.py
    # 如原生件不在默认位置, 用 --module-dir 指向含
    # timesai_tools.cp38-win_amd64.pyd + opencv_*.dll 的目录

判据 (A03):
- PASS_STRUCTURE: 原生可加载且返回结构合法 (dict 含 statues/circle_num/circles_for_show 等);
- 是否检出圆单独报告 —— 合成图检不到不代表加载失败, 但**代表样本须真实图** (T38 另记);
- IPC 往返成功 (头字段/长度/request_id 校验通过);
- 进程正常退出 (退出码 0), 不留未释放资源。

本脚本不连接任何设备; 只用合成图。
"""
from __future__ import annotations

import argparse
import importlib.metadata as _md
import importlib.util
import json
import os
import platform
import struct
import sys
import time

# 控制台下中文输出: 强制 UTF-8, 避免日志里出现乱码 (Windows GBK 代码页)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONDA_HINT = (r"conda install -p E:\AnacondaEnvs\towerops-circle numpy opencv -y"
              "   # 推荐: 3.8 环境常因缺 OpenSSL DLL 导致 pip 无法用 HTTPS")

# 仓库内原生件默认位置
_DEFAULT_NATIVE_DIR = os.path.join(
    os.path.dirname(_ROOT), "Ganta_Crtl_System", "vision_monitor", "vision",
    "detect_circle")

REQUIRED_NATIVE_DLLS = ("opencv_core450.dll", "opencv_imgproc450.dll",
                        "msvcp140.dll", "vcruntime140.dll")


def _synthetic_image(with_circle: bool = True):
    """合成测试图 (仅用于加载/结构验证; 不是工艺代表样本)。

    失败时返回 None, 并把原因写进 _SYNTH_ERROR 供报告显示 (不要静默 None)。
    """
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        _SYNTH_ERROR.append(f"{type(exc).__name__}: {exc}")
        return None
    h, w = 480, 640
    img = np.full((h, w, 3), 90, dtype=np.uint8)
    if with_circle:
        yy, xx = np.mgrid[0:h, 0:w]
        r = 60
        ring = (np.abs(np.sqrt((xx - 320.0) ** 2 + (yy - 240.0) ** 2) - r) < 3)
        img[ring] = (235, 235, 235)
    return img


_SYNTH_ERROR: list[str] = []


def main() -> int:
    ap = argparse.ArgumentParser(description="cp38 circle host self-check (A03)")
    ap.add_argument("--module-dir", default=_DEFAULT_NATIVE_DIR)
    ap.add_argument("--out", default=None, help="把 JSON 结果写入该文件")
    args = ap.parse_args()

    report: dict = {
        "interpreter": sys.executable,
        "version": platform.python_version(),
        "version_info": list(sys.version_info[:3]),
        "bits": struct.calcsize("P") * 8,
        "platform": platform.platform(),
        "module_dir": args.module_dir,
        "expected_python": "3.8.10 (pyd 编译版本; 注释称不兼容 3.8.20+)",
    }
    problems: list[str] = []

    # 1) 解释器
    if sys.version_info[:2] != (3, 8):
        problems.append(f"解释器不是 3.8 (得到 {platform.python_version()})")
    if report["bits"] != 64:
        problems.append(f"解释器不是 64 位 (得到 {report['bits']})")
    report["patch_note"] = (
        "与 3.8.10 不同的小版本可能导致 .pyd 加载失败, 见加载结果"
        if sys.version_info[:3] != (3, 8, 10) else "与 3.8.10 一致")

    # 2) 依赖
    #    注意: **不要 import cv2** —— 本路径不需要它, 而且把 conda/pip 的 OpenCV
    #    载入本进程, 会与 detect_circle 自带的 opencv_*450.dll 冲突,
    #    导致 timesai_tools 报 "DLL 初始化例程失败"。只探测存在性, 不导入。
    #
    #    numpy 则必须**真正 import** 一次: find_spec 只能证明文件在,
    #    证明不了它能加载 (缺 MKL/OpenSSL 等配套 DLL 时会 ImportError),
    #    而"文件在但导不进来"正是最常见的坑。
    deps: dict[str, str] = {}
    try:
        import numpy as np  # noqa: F401
        deps["numpy"] = getattr(np, "__version__", "ok")
        report["numpy_import"] = "ok"
    except Exception as exc:  # noqa: BLE001
        deps["numpy"] = "存在但无法导入"
        report["numpy_import"] = f"{type(exc).__name__}: {exc}"
        problems.append(f"numpy 无法导入: {exc}")
    if importlib.util.find_spec("cv2") is None:
        deps["cv2"] = "absent (本路径不需要; 亦避免与本机 OpenCV 冲突)"
    else:
        deps["cv2"] = (_dist_version("opencv-python") or "present") + \
            " (未导入; 本路径不需要)"
    if "ssl" not in sys.modules:
        try:
            import ssl  # noqa: F401
            report["ssl"] = "ok"
        except Exception as exc:  # noqa: BLE001
            report["ssl"] = f"不可用: {exc}"
            problems.append(
                "ssl 模块不可用 -> pip 无法使用 HTTPS; 建议改用 conda 安装依赖")
    report["dependencies"] = deps
    report["cv2_imported_in_process"] = "cv2" in sys.modules
    if any("MISSING" in v for v in deps.values()):
        report["install_hint"] = CONDA_HINT

    # 3) 原生件是否在位
    present: dict[str, bool] = {}
    pyd = None
    for fname in os.listdir(args.module_dir) if os.path.isdir(args.module_dir) else []:
        if fname.endswith(".pyd"):
            pyd = os.path.join(args.module_dir, fname)
    present["timesai_tools.pyd"] = bool(pyd)
    for dll in REQUIRED_NATIVE_DLLS:
        present[dll] = os.path.exists(os.path.join(args.module_dir, dll))
    report["native_files"] = present
    if not pyd:
        problems.append(f"在 {args.module_dir!r} 未找到 .pyd")
    for dll, ok in present.items():
        if dll != "timesai_tools.pyd" and not ok:
            problems.append(f"缺原生 DLL: {dll}")

    # 4) 原生加载 + 合法输出
    #    只要原生件在位就**一律尝试加载**: 加载结果本身就是最有价值的诊断,
    #    不能被 ssl/依赖之类的软前置遮住 (否则会得到"没加载也没报错"的空结论)。
    native_result: dict = {"loaded": False, "structure_ok": False}
    image = _synthetic_image()
    report["gate"] = {"native_present": bool(pyd), "image_ok": image is not None,
                      "soft_problems": [p for p in problems]}
    if pyd and image is not None:
        try:
            from application.hosts.circle38.backends import NativeCircleBackend
            backend = NativeCircleBackend(module_dir=args.module_dir)
            backend.load()
            native_result["loaded"] = True

            h, w = image.shape[:2]
            # DEC-055：后端契约 = 本帧扁平矩形列表（后端内部按图包一层）
            raw = backend.detect([image], [[2, 0, 0, w, h, 0]], _default_params())
            required_keys = ("statues", "circle_num", "circle_centers", "radius",
                             "circles_for_show")
            missing = [k for k in required_keys if k not in raw]
            native_result["missing_keys"] = missing
            native_result["structure_ok"] = not missing
            native_result["raw_summary"] = {
                k: (str(raw.get(k))[:120] if k in raw else None) for k in required_keys
            }
            try:
                from application.algorithms.circle.normalize import parse_native_circles
                circles, metrics = parse_native_circles(raw)
                native_result["normalized_circles"] = [
                    {"cx": c.cx, "cy": c.cy, "r": c.radius_px} for c in circles]
                native_result["metrics"] = metrics
            except Exception as exc:  # noqa: BLE001
                native_result["normalize_error"] = str(exc)
                problems.append(f"规范化失败: {exc}")
            if missing:
                problems.append(f"原生返回缺字段: {missing}")
        except Exception as exc:  # noqa: BLE001
            native_result["load_error"] = f"{type(exc).__name__}: {exc}"
            problems.append(f"原生加载/调用失败: {exc}")
    else:
        native_result["skipped"] = ("缺原生件" if not pyd
                                    else "无法构造合成图: "
                                         + ("; ".join(_SYNTH_ERROR) or "numpy 不可用"))
    report["native"] = native_result

    # 5) IPC 端到端 (真实原生后端, 经长度前缀 JSON 头 + 图像字节)
    ipc_result: dict = {"ok": False}
    if native_result.get("structure_ok"):
        try:
            from application.hosts.circle38.client import Circle38Proxy, InProcessTransport
            from application.hosts.circle38.server import Circle38Server
            server = Circle38Server(_loaded_backend(args.module_dir))
            proxy = Circle38Proxy(InProcessTransport(server))
            t0 = time.monotonic()
            out = proxy.detect([image], [[2, 0, 0, image.shape[1], image.shape[0], 0]],
                               _default_params())
            ipc_result = {"ok": True, "elapsed_ms": (time.monotonic() - t0) * 1000.0,
                          "keys": sorted(out)[:8]}
            proxy.close()
        except Exception as exc:  # noqa: BLE001
            ipc_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            problems.append(f"IPC 端到端失败: {exc}")
    report["ipc"] = ipc_result

    # 6) 结论
    report["problems"] = problems
    report["verdict"] = "PASS" if not problems else "FAIL"
    report["note"] = ("合成图仅验证加载/结构/IPC; 真实工艺代表样本与误检漏检归 T38, 本脚本不覆盖")

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print("----- BEGIN circle-host report -----")
    print(payload)
    print("----- END circle-host report -----")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"[check] 已写入 {args.out}")
    print(f"[check] 结论: {report['verdict']}")
    for p in problems:
        print(f"[check] - {p}")
    if "install_hint" in report:
        print(f"[check] 依赖缺失, 建议: {report['install_hint']}")
    return 0 if report["verdict"] == "PASS" else 3


def _dist_version(dist: str) -> str | None:
    try:
        return _md.version(dist)
    except Exception:  # noqa: BLE001
        return None


def _default_params():
    from application.algorithms.circle.params import CircleParams
    return CircleParams()


def _loaded_backend(module_dir: str):
    from application.hosts.circle38.backends import NativeCircleBackend
    b = NativeCircleBackend(module_dir=module_dir)
    b.load()
    return b


if __name__ == "__main__":
    raise SystemExit(main())
