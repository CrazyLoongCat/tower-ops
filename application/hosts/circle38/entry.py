"""圆检测宿主进程入口 (12 §4.3, 02 §8.3)。

运行方式（任一解释器；Windows 3.8 原生 / Linux OpenCV 自研后端，DEC-072）::

    <python> -m application.hosts.circle38.entry [--backend auto|native|opencv|stub]
             [--module-dir <detect_circle 目录>]

从 stdin 读长度前缀报文, 向 stdout 写响应; EOF/SIGTERM 时正常退出 (退出码 0).
本入口不自动连接任何设备, 只做圆检测算法调用。

``--backend auto``（默认）：``--module-dir``（或仓库内 detect_circle）里若有原生件
（``.pyd`` / ``.so``）就优先加载原生；否则用 **OpenCV 自研后端**（host_version=
``opencv-linux``，与 cp38 原生不保证逐位一致，见模块 docstring）。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from application.hosts.circle38.backends import (
    DEFAULT_MODULE_DIR,
    NativeCircleBackend,
    StubCircleBackend,
)
from application.hosts.circle38.opencv_backend import (
    OpenCVCircleBackend,
    OpenCVLoadError,
)
from application.hosts.circle38.server import Circle38Server


def _has_native_module(module_dir: str | None) -> bool:
    directory = module_dir or DEFAULT_MODULE_DIR
    if not os.path.isdir(directory):
        return False
    return any(f.endswith((".pyd", ".so")) for f in os.listdir(directory))


def build_backend(kind: str, module_dir: str | None) -> Any:
    """按 ``--backend`` 构造并 load() 后端；失败抛异常（由入口转成明确退出码）。

    ``auto``：有原生件 → 先试原生（失败**明确报原因**后试 OpenCV，不静默降到 stub）；
    没有原生件 → OpenCV 自研后端。
    """
    kind = (kind or "auto").strip().lower()
    if kind == "stub":
        return StubCircleBackend()
    if kind == "native":
        backend = NativeCircleBackend(module_dir=module_dir)
        backend.load()
        return backend
    if kind == "opencv":
        backend = OpenCVCircleBackend()
        backend.load()
        return backend
    if kind != "auto":
        raise ValueError(f"未知 --backend: {kind!r}")
    errors: list[str] = []
    if _has_native_module(module_dir):
        try:
            backend = NativeCircleBackend(module_dir=module_dir)
            backend.load()
            return backend
        except Exception as exc:  # noqa: BLE001 - 明确记录后改用自研后端
            errors.append(f"原生后端不可用: {exc}")
            sys.stderr.write(f"[circle38] {errors[-1]}；改用 OpenCV 自研后端\n")
    try:
        backend = OpenCVCircleBackend()
        backend.load()
        return backend
    except OpenCVLoadError as exc:
        errors.append(f"OpenCV 后端不可用: {exc}")
    raise RuntimeError("；".join(errors) or "没有可用后端")


def _read_chunk(fd: int, size: int) -> bytes:
    try:
        return os.read(fd, size)
    except (OSError, KeyboardInterrupt):
        return b""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="circle detection host")
    parser.add_argument("--module-dir", default=None,
                        help="detect_circle 目录 (含原生件; 缺省为仓库内 detect_circle)")
    parser.add_argument("--backend", default="auto",
                        choices=("auto", "native", "opencv", "stub"),
                        help="圆检测后端：auto=有原生件用原生，否则 OpenCV 自研后端；"
                             "stub 仅离线联调（明确标注，非真实证据）")
    parser.add_argument("--stub", action="store_true",
                        help="兼容旧参数：等价于 --backend stub（离线/联调，非真实证据）")
    args = parser.parse_args(argv)

    kind = "stub" if args.stub else args.backend
    try:
        backend = build_backend(kind, args.module_dir)
    except Exception as exc:  # noqa: BLE001 - 明确失败, 不以非零退出伪装可用
        sys.stderr.write(f"[circle38] 后端不可用: {exc}\n")
        return 3
    if kind == "stub":
        sys.stderr.write("[circle38] 警告: 使用 stub 后端 (离线/联调), 非真实算法\n")
    version = getattr(backend, "host_version", "unknown")
    note = ("（OpenCV 自研实现，非 cp38 原生；阈值语义相同、结果不保证逐位一致）"
            if version == "opencv-linux" else "")
    sys.stderr.write(f"[circle38] 后端: {version}{note}\n")

    server = Circle38Server(backend)
    stdin_fd, stdout_fd = 0, 1
    running = True
    while running:
        chunk = _read_chunk(stdin_fd, 65536)
        if not chunk:
            break  # EOF: 主程序关闭管道
        response = server.handle_bytes(chunk)
        if response:
            try:
                os.write(stdout_fd, response)
            except OSError:
                break
    return 0


if __name__ == "__main__":  # pragma: no cover - 解释器间入口
    raise SystemExit(main())
