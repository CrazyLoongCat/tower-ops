"""跨解释器 IPC 验证 (A03 / T33) —— 用 **ENV-MAIN(3.12)** 运行。

验证真实架构的那条边界: 主进程(3.12) spawn 出**独立 3.8 宿主进程**, 经 stdin/stdout
管道收发; 宿主机内使用**真实原生件** (timesai_tools) 或 `--stub`。

运行 (ENV-MAIN 解释器):
    <py312> scripts\\verify_circle_ipc_cross.py --host-python <ENV-CIRCLE python.exe> --out <report.json>

要点:
- 不用 `conda run` 包装: 直接把 <host_env>\\Library\\bin 与 \\DLLs 注入子进程 PATH,
  等价于激活环境, 且不干扰 stdin/stdout 管道;
- **每个阶段独立 try**: 任一阶段失败也要产出报告 (不能一崩就什么都不留);
- 覆盖 正常往返 / 连续请求分帧 / 宿主崩溃 / 超时;
- 只连本机子进程, 不接触任何设备。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


def _host_env(host_python: str) -> dict:
    """构造 3.8 宿主子进程环境: 注入 <env>\\Library\\bin 与 \\DLLs (等价 conda 激活)。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = _ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env_dir = os.path.dirname(os.path.abspath(host_python))
    dll_dirs = [os.path.join(env_dir, "Library", "bin"), os.path.join(env_dir, "DLLs")]
    env["PATH"] = os.pathsep.join(
        [d for d in dll_dirs if os.path.isdir(d)] + [env.get("PATH", "")])
    return env


def _image(w=640, h=480, size=60):
    import numpy as np
    img = np.full((h, w, 3), 90, dtype=np.uint8)
    yy, xx = np.mgrid[0:h, 0:w]
    ring = (abs(((xx - w / 2) ** 2 + (yy - h / 2) ** 2) ** 0.5 - size) < 3)
    img[ring] = (235, 235, 235)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description="cross-interpreter circle38 IPC check (A03)")
    ap.add_argument("--host-python", required=True, help="ENV-CIRCLE 的 3.8 python.exe")
    ap.add_argument("--module-dir", default=None, help="detect_circle 目录")
    ap.add_argument("--deadline-ms", type=int, default=20000)
    ap.add_argument("--stub", action="store_true",
                    help="用 stub 后端联调 (不加载原生件; 仅验进程边界, 不得当真实证据)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from application.algorithms.circle.params import CircleParams
    from application.hosts.circle38.backends import DEFAULT_MODULE_DIR
    from application.hosts.circle38.client import (
        Circle38Error, Circle38HostCrash, Circle38Proxy, Circle38Timeout,
        SubprocessTransport,
    )

    module_dir = args.module_dir or DEFAULT_MODULE_DIR
    report: dict = {
        "main_interpreter": sys.executable,
        "main_version": sys.version.split()[0],
        "host_python": args.host_python,
        "host_version": None,
        "module_dir": module_dir,
        "host_uses_native": not args.stub,
    }
    problems: list[str] = []
    env = _host_env(args.host_python)

    # 0) 宿主解释器版本 (独立探测, 失败不影响后续)
    try:
        r = subprocess.run([args.host_python, "-c",
                            "import sys;print(sys.version.split()[0])"],
                           capture_output=True, text=True, timeout=20, env=env)
        report["host_version"] = (r.stdout or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        report["host_version_probe_error"] = f"{type(exc).__name__}: {exc}"

    host_cmd = ([args.host_python, "-m", "application.hosts.circle38.entry", "--stub"]
                if args.stub else
                [args.host_python, "-m", "application.hosts.circle38.entry",
                 "--module-dir", module_dir])

    transport = SubprocessTransport(host_cmd, env=env)
    proxy = Circle38Proxy(transport, default_deadline_ms=args.deadline_ms)
    try:
        # 1) 正常往返 (真实原生件 + 大图 -> 走多块分帧)
        try:
            t0 = time.monotonic()
            out = proxy.detect([_image()], [[2, 0, 0, 640, 480, 0]], CircleParams())
            report["roundtrip_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
            report["circle_num"] = out.get("circle_num")
            report["roundtrip_ok"] = bool(out)
        except Exception as exc:  # noqa: BLE001
            report["roundtrip_ok"] = False
            report["roundtrip_error"] = f"{type(exc).__name__}: {exc}"
            problems.append(f"正常往返失败: {type(exc).__name__}: {exc}")

        # 2) 连续请求分帧
        if report.get("roundtrip_ok"):
            try:
                oks = 0
                for _ in range(3):
                    o2 = proxy.detect([_image()], [[2, 0, 0, 640, 480, 0]], CircleParams())
                    if o2.get("circle_num") == report["circle_num"]:
                        oks += 1
                report["sequential_ok"] = oks
                if oks != 3:
                    problems.append(f"连续请求分帧异常: {oks}/3")
            except Exception as exc:  # noqa: BLE001
                report["sequential_ok"] = 0
                problems.append(f"连续请求异常: {type(exc).__name__}: {exc}")

        # 3) 宿主崩溃 -> 必须报崩溃, 不静默成功
        try:
            if transport._proc is not None:            # noqa: SLF001 - 故意注入崩溃
                transport._proc.kill()
                transport._proc.wait(timeout=10)
            proxy.detect([_image()], [[2, 0, 0, 640, 480, 0]], CircleParams())
            report["crash_detected"] = False
            problems.append("宿主被杀后仍返回成功 (应报崩溃)")
        except Circle38HostCrash:
            report["crash_detected"] = True
        except Circle38Error as exc:
            report["crash_detected"] = f"其他Circle38异常: {type(exc).__name__}"
            problems.append(f"崩溃处理异常: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            report["crash_detected"] = f"未预期异常: {type(exc).__name__}"
            problems.append(f"崩溃处理异常: {type(exc).__name__}: {exc}")
    finally:
        transport.close()

    # 4) 超时: 一个不响应的宿主 (独立进程)
    try:
        silent = SubprocessTransport(
            [args.host_python, "-c", "import time;time.sleep(60)"], env=env)
        try:
            p2 = Circle38Proxy(silent, default_deadline_ms=400)
            try:
                p2.detect([_image(32, 32, 5)], [[2, 0, 0, 32, 32, 0]], CircleParams())
                report["timeout_detected"] = False
                problems.append("不响应宿主未触发超时")
            except Circle38Timeout:
                report["timeout_detected"] = True
        finally:
            silent.close()
    except Exception as exc:  # noqa: BLE001
        report["timeout_probe_error"] = f"{type(exc).__name__}: {exc}"

    report["problems"] = problems
    report["verdict"] = "PASS" if not problems else "FAIL"

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print("----- BEGIN cross-ipc report -----")
    print(payload)
    print("----- END cross-ipc report -----")
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"[check] 已写入 {args.out}")
    print(f"[check] 结论: {report['verdict']}")
    for p in problems:
        print(f"[check] - {p}")
    return 0 if report["verdict"] == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
