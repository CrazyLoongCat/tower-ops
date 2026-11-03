"""A03 跨进程 IPC 测试: 主程序 <-> **独立 3.8 宿主子进程** (T33/T19/T21/T25)。

与 InProcessTransport 不同, 本文件跑真实的**进程边界**: 用 SubprocessTransport
spawn 出 `python -m application.hosts.circle38.entry`, 经 stdin/stdout 管道收发。

用 `--stub` 后端 (非原生件): 这里验证的是 **IPC/进程生命周期契约**;
原生加载由 scripts/verify_circle_host.ps1 在 3.8 上验证 (两者互补, 不可互相替代)。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

from application.algorithms.circle.params import CircleParams
from application.contracts.enums import ImageSpace
from application.hosts.circle38.client import (
    Circle38HostCrash,
    Circle38Proxy,
    Circle38Timeout,
    SubprocessTransport,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _host_cmd(*extra: str) -> list[str]:
    return [sys.executable, "-m", "application.hosts.circle38.entry", *extra]


def _image(w=64, h=48):
    return np.zeros((h, w, 3), dtype=np.uint8)


@pytest.fixture()
def transport():
    t = SubprocessTransport(_host_cmd("--stub"))
    try:
        yield t
    finally:
        t.close()


def test_process_roundtrip_and_clean_exit(transport):
    """跨进程往返成功, 且宿主收到 EOF 后**正常退出 (码 0)**。"""
    proxy = Circle38Proxy(transport, default_deadline_ms=15000)
    out = proxy.detect([_image()], [[2, 0, 0, 64, 48, 0]], CircleParams())
    assert out["circle_num"] == [[1]]
    assert transport.alive
    transport.close()
    assert transport.last_returncode == 0          # 干净退出, 非崩溃/超时


def test_sequential_requests_no_partial_message_mix(tmp_path):
    """连续多次请求: 每次都要拿到**完整**响应 (不把半条消息当一条)。"""
    t = SubprocessTransport(_host_cmd("--stub"))
    try:
        proxy = Circle38Proxy(t, default_deadline_ms=15000)
        # 用较大图像让响应更长, 更容易暴露分帧问题
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        for i in range(3):
            out = proxy.detect([img], [[2, 0, 0, 640, 480, 0]], CircleParams())
            assert out["circle_num"] == [[1]], f"第 {i + 1} 次响应不完整/错位"
        assert t.last_request_ok if hasattr(t, "last_request_ok") else True
    finally:
        t.close()
    assert t.last_returncode == 0


def test_host_crash_is_reported_not_faked():
    """宿主进程被杀 -> 报 Crash, 不静默成功。"""
    t = SubprocessTransport(_host_cmd("--stub"))
    try:
        proxy = Circle38Proxy(t, default_deadline_ms=15000)
        proxy.detect([_image()], [[2, 0, 0, 64, 48, 0]], CircleParams())
        t._proc.kill()                              # noqa: SLF001 - 模拟宿主崩溃
        t._proc.wait(timeout=5)
        with pytest.raises(Circle38HostCrash):
            proxy.detect([_image()], [[2, 0, 0, 64, 48, 0]], CircleParams())
    finally:
        t.close()


def test_immediate_exit_host_is_crash():
    """宿主一启动就退出 -> 请求时报 Crash (带退出码/stderr 线索)。"""
    t = SubprocessTransport([sys.executable, "-c", "import sys; sys.exit(7)"],
                            startup_grace_s=1.0)
    try:
        with pytest.raises(Circle38HostCrash) as ei:
            t.request(b"\x00\x00\x00\x02{}", 1000)
        assert "退出码=7" in str(ei.value)
    finally:
        t.close()


def test_silent_host_times_out_not_hangs():
    """宿主不回应 -> 在 deadline 内返回超时, 不挂死。

    DEC-052 起首次请求默认叠加冷启动宽限（startup_grace_s），本用例只验证
    **普通请求期限**，故显式把宽限调小。
    """
    import time
    t = SubprocessTransport([sys.executable, "-c", "import time; time.sleep(30)"],
                            startup_grace_s=0.3)
    try:
        start = time.monotonic()
        with pytest.raises(Circle38Timeout):
            proxy = Circle38Proxy(t, default_deadline_ms=300)
            proxy.detect([_image()], [[2, 0, 0, 64, 48, 0]], CircleParams())
        assert time.monotonic() - start < 3.0      # 没有挂死
    finally:
        t.close()


def test_first_request_gets_startup_grace(tmp_path):
    """DEC-052: 宿主冷启动（3.8 解释器 + 原生件加载）慢于普通期限时，
    首个请求按 startup_grace_s 等待——不把冷启动误判成视觉故障。"""
    script = tmp_path / "slow_start_host.py"
    script.write_text(
        "import sys, time\n"
        "time.sleep(1.2)\n"
        "from application.hosts.circle38.entry import main\n"
        "sys.exit(main(['--stub']))\n",
        encoding="utf-8")
    t = SubprocessTransport([sys.executable, str(script)],
                            startup_grace_s=5.0, env=_env())
    try:
        proxy = Circle38Proxy(t, default_deadline_ms=300)   # 名义期限远小于冷启动
        out = proxy.detect([_image()], [[2, 0, 0, 64, 48, 0]], CircleParams())
        assert out["circle_num"] == [[1]]
    finally:
        t.close()


def test_late_response_from_timed_out_request_is_skipped(tmp_path):
    """DEC-052: 前一请求超时后才到的响应，不能被当成本次响应。

    否则一次超时会让后续每次请求错位一格（拿到旧 request_id），视觉永久失败。
    """
    script = tmp_path / "slow_first_host.py"
    script.write_text(
        "import os, sys, time\n"
        "from application.hosts.circle38.backends import StubCircleBackend\n"
        "from application.hosts.circle38.server import Circle38Server\n"
        "server = Circle38Server(StubCircleBackend())\n"
        "first = True\n"
        "while True:\n"
        "    chunk = os.read(0, 65536)\n"
        "    if not chunk:\n"
        "        break\n"
        "    if first:\n"
        "        first = False\n"
        "        time.sleep(1.5)\n"
        "    out = server.handle_bytes(chunk)\n"
        "    if out:\n"
        "        os.write(1, out)\n",
        encoding="utf-8")
    t = SubprocessTransport([sys.executable, str(script)],
                            startup_grace_s=0.2, env=_env())
    try:
        proxy = Circle38Proxy(t, default_deadline_ms=300)
        with pytest.raises(Circle38Timeout):
            proxy.detect([_image()], [[2, 0, 0, 64, 48, 0]], CircleParams())
        # 第二次请求：第一个请求的慢响应随后才到，必须跳过它、等自己的响应
        proxy._deadline_ms = 5000                    # noqa: SLF001 - 测试内放宽
        out = proxy.detect([_image()], [[2, 0, 0, 64, 48, 0]], CircleParams())
        assert out["circle_num"] == [[1]]
        assert len(t.stale_responses) == 1
        assert t.stale_responses[0]                  # 旧响应有留档
    finally:
        t.close()


def test_close_is_idempotent_and_releases():
    t = SubprocessTransport(_host_cmd("--stub"))
    t.close()
    t.close()                                       # 重复 close 不报错
    assert not t.alive
