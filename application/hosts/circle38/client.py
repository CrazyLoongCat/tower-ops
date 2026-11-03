"""主程序侧 3.8 宿主代理 (12 §4.3, 02 §8.3, T19/T21/T25/T33).

实现 CircleBackend 协议, 可直接供 CircleDetector 使用.
- 每次请求新 request_id; 超时/崩溃/迟到明确报错 (不装作成功);
- 迟到结果只存档, 不冒充本次结果;
- 传输可替换: 进程内 (测试) 或子进程 (真实 3.8).
"""
from __future__ import annotations

import itertools
import os
import queue
import subprocess
import tempfile
import threading
import time
from typing import Any, Protocol

from application.algorithms.circle.params import CircleParams
from application.hosts.circle38.framing import MessageStreamDecoder, encode_message
from application.hosts.circle38.protocol import (
    ProtocolError,
    Request,
    build_request,
    parse_response,
)


class Circle38Error(RuntimeError):
    """宿主通信错误基类."""


class Circle38Timeout(Circle38Error):
    pass


class Circle38HostCrash(Circle38Error):
    pass


class Circle38ErrorResponse(Circle38Error):
    pass


class Circle38LateResult(Circle38Error):
    """收到的结果不属于当前请求 (迟到/错序)."""


class Circle38Transport(Protocol):
    def request(self, payload: bytes, deadline_ms: int) -> bytes | None: ...

    def close(self) -> None: ...

    @property
    def alive(self) -> bool: ...


class InProcessTransport:
    """进程内传输 (离线测试): 直接调用宿主侧处理逻辑."""

    def __init__(self, server) -> None:
        self._server = server
        self._alive = True

    def request(self, payload: bytes, deadline_ms: int) -> bytes | None:
        if not self._alive:
            return None
        return self._server.handle_bytes(payload)

    def close(self) -> None:
        self._alive = False

    @property
    def alive(self) -> bool:
        return self._alive


class SubprocessTransport:
    """子进程传输 (真实 3.8 宿主). 目标解释器与脚本由配置提供 (A03/A24)。

    实现要点 (A03 硬化):
    - **读取用后台线程 + 有界队列**: Windows 上 `select()` 不支持管道句柄,
      而阻塞式 read 又会卡住 deadline, 所以用线程按消息分帧投递;
    - **按消息边界返回**: 不会把半条消息当成一条响应 (用 MessageStreamDecoder 分帧);
    - stderr 重定向到临时文件: 既不阻塞子进程, 又能在崩溃时给出尾部信息;
    - 明确区分 超时(None) / 崩溃(Circle38HostCrash) / 干净退出。
    """

    def __init__(self, cmd: list[str], *, startup_grace_s: float = 5.0,
                 stderr_tail_bytes: int = 4096,
                 env: dict[str, str] | None = None) -> None:
        self._cmd = cmd
        self._proc: subprocess.Popen | None = None
        self._startup_grace_s = startup_grace_s
        self._stderr_tail_bytes = stderr_tail_bytes
        self._env = env                      # 子进程环境 (如补 Library\bin 到 PATH)
        self._q: "queue.Queue[bytes]" = queue.Queue(maxsize=64)   # 有界
        self._eof = threading.Event()
        self._reader: threading.Thread | None = None
        self._err = None
        self.last_returncode: int | None = None   # close() 后记录宿主退出码
        self._fresh_start = False                 # 进程刚拉起：首帧按冷启动宽限
        # 迟到响应留档（DEC-052）：不能当成本次响应，留证便于诊断。
        self.stale_responses: list[bytes] = []

    # ---------- 生命周期 ----------
    def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        err = tempfile.TemporaryFile()
        try:
            proc = subprocess.Popen(
                self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=err, env=self._env,
            )
        except Exception:
            # 拉起失败（找不到 exe / 无权限）：别留下临时文件句柄
            try:
                err.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        self._err = err
        self._proc = proc
        self._fresh_start = True
        self._reader = threading.Thread(target=self._read_loop, name="circle38-read",
                                        daemon=True)
        self._reader.start()

    def start(self) -> None:
        """**立即拉起**宿主，不等第一次请求（DEC-053：启动主程序即启动宿主）。

        失败抛 ``OSError`` 等（宿主 exe 不存在/无权限），由装配层决定是否降级。
        """
        self._ensure_started()

    @property
    def started(self) -> bool:
        """宿主进程是否已拉起（不代表仍在运行；存活看 :attr:`alive`）。"""
        return self._proc is not None

    def _read_loop(self) -> None:  # pragma: no cover - 线程
        decoder = MessageStreamDecoder()
        fd = self._proc.stdout.fileno()          # type: ignore[union-attr]
        while True:
            try:
                chunk = os.read(fd, 65536)       # 有数据就返回, 不阻塞等待满缓冲
            except OSError:
                break
            if not chunk:
                break                            # EOF: 宿主退出或关闭 stdout
            try:
                for header, body in decoder.feed(chunk):
                    self._q.put(encode_message(header, body))   # 只投递**完整**消息
            except Exception:  # noqa: BLE001 - 分帧错误视为宿主故障
                break
        self._eof.set()

    def _stderr_tail(self) -> str:
        try:
            self._err.seek(0)
            data = self._err.read() or b""
            tail = data[-self._stderr_tail_bytes:]
            return tail.decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _crash_msg(self) -> str:
        code = None if self._proc is None else self._proc.poll()
        tail = self._stderr_tail()
        msg = f"宿主进程不可用 (退出码={code})"
        return f"{msg}; stderr尾部: {tail}" if tail else msg

    # ---------- 请求 ----------
    def request(self, payload: bytes, deadline_ms: int) -> bytes | None:
        """发送一帧并等待**一条完整**响应。返回 None 表示超时 (宿主仍存活)。

        DEC-052 两处修正（现场"DS86 抓拍后视觉判定必失败"的宿主侧根因）：
        - **冷启动宽限**：进程是本次请求才拉起的（3.8 解释器 + OpenCV +
          原生 pyd 首次加载）时，首帧等待放宽到 ``startup_grace_s``；
          此前该参数闲置，2s 的普通期限可能不够首次加载，第一请求必然超时；
        - **迟到响应不当本次结果**：上一请求超时后才到的响应若被读走，
          后续每次请求都会错位一格、永久失败；现按 request_id 跳过并留档。
        """
        self._ensure_started()
        assert self._proc is not None and self._proc.stdin is not None
        if self._proc.poll() is not None:
            raise Circle38HostCrash(self._crash_msg())
        effective_ms = deadline_ms
        if self._fresh_start:
            self._fresh_start = False
            effective_ms = max(deadline_ms, int(self._startup_grace_s * 1000))
        expect = _message_request_id(payload)
        try:
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise Circle38HostCrash(f"写入宿主 stdin 失败: {exc}; {self._stderr_tail()}") from exc

        deadline = time.monotonic() + effective_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = self._q.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if self._eof.is_set() and self._q.empty():
                    raise Circle38HostCrash(self._crash_msg())
                continue
            msg_id = _message_request_id(msg)
            if expect is not None and msg_id not in (None, expect):
                # 迟到/错序的旧响应：留档、跳过，等本次的响应
                if len(self.stale_responses) < 32:
                    self.stale_responses.append(msg)
                continue
            return msg

    def close(self) -> None:
        """关闭 stdin(发 EOF) -> 等待干净退出 -> 必要时 terminate/kill。"""
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()               # 宿主读到 EOF 后应返回 0
            except Exception:  # noqa: BLE001
                pass
            try:
                self.last_returncode = proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                    self.last_returncode = proc.wait(timeout=2)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                        self.last_returncode = proc.wait(timeout=2)
                    except Exception:  # noqa: BLE001
                        self.last_returncode = None
            if self._err is not None:
                try:
                    self._err.close()
                except Exception:  # noqa: BLE001
                    pass
                self._err = None

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class Circle38Proxy:
    """主程序侧代理. 不导入任何 cp38 原生件."""

    def __init__(self, transport: Circle38Transport, *,
                 default_deadline_ms: int = 2000) -> None:
        self._transport = transport
        self._deadline_ms = default_deadline_ms
        self._ids = itertools.count(1)
        self.last_request_id: str | None = None
        self.archived_late: list[str] = []

    def detect(self, images: list[Any], rects: list[list[int]],
               params: CircleParams) -> dict:
        if not images:
            raise Circle38Error("没有图像输入")
        request_id = f"circle-{next(self._ids)}"
        self.last_request_id = request_id
        req = Request(request_id=request_id, image=images[0], rects=rects,
                      params=params, deadline_ms=self._deadline_ms)
        payload = build_request(req)
        raw = self._transport.request(payload, self._deadline_ms)
        if raw is None:
            if not self._transport.alive:
                raise Circle38HostCrash("宿主不可用/已崩溃")
            raise Circle38Timeout(
                f"请求 {request_id} 超时（宿主无响应；首次调用已按冷启动宽限等待，"
                f"仍超时请检查宿主是否卡住或算法耗时过长）")
        header, body = _split_one(raw)
        resp = parse_response(header, body)
        if resp.request_id != request_id:
            # 迟到/错序结果只存档, 不冒充本次 (T16/T33)
            self.archived_late.append(resp.request_id)
            raise Circle38LateResult(
                f"收到 {resp.request_id!r} 结果, 期望 {request_id!r}"
            )
        if not resp.ok:
            raise Circle38ErrorResponse(resp.error or "宿主返回错误")
        return resp.result or {}

    def close(self) -> None:
        self._transport.close()

    @property
    def alive(self) -> bool:
        return self._transport.alive


def _split_one(raw: bytes) -> tuple[dict, bytes]:
    from application.hosts.circle38.framing import MessageStreamDecoder
    msgs = MessageStreamDecoder().feed(raw)
    if not msgs:
        raise ProtocolError("响应不完整")
    return msgs[0]


def _message_request_id(raw: bytes) -> str | None:
    """从一条完整报文中取 request_id（取不到返回 None，交由上层按协议错误处理）。"""
    try:
        msgs = MessageStreamDecoder().feed(raw)
        if not msgs:
            return None
        rid = msgs[0][0].get("request_id")
        return str(rid) if rid else None
    except Exception:  # noqa: BLE001 - 解析失败不在这里判罪
        return None


__all__ = [
    "Circle38Error", "Circle38ErrorResponse", "Circle38HostCrash",
    "Circle38LateResult", "Circle38Proxy", "Circle38Timeout",
    "Circle38Transport", "InProcessTransport", "SubprocessTransport",
]
