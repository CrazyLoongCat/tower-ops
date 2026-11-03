"""宿主请求处理循环 (12 §4.3, T33)。

纯函数 handle_message(用于离线测试) + 解释器间循环 serve_forever(用于 3.8 进程).
迟到结果只存档: 由客户端按 request_id 匹配; 宿主任一异常都回错误报文, 不装作成功.

**分帧要求 (A03 实测教训)**:
- 解码器必须**跨读取持久**。宿主按 `os.read(fd, 64KB)` 分块收数据, 而一张
  640x480x3 的图约 921KB, 会被切成十几块; 若每次新建解码器, 第一条 header
  声明的载荷永远等不到 -> 下一块从载荷中间开始被当成 header -> JSON 解析崩溃。
- 一块里可能含**多条**完整消息 (粘包), 必须全部处理, 不能只取第一条。
"""
from __future__ import annotations

import time
from typing import Callable

from application.hosts.circle38.framing import FramingError, MessageStreamDecoder
from application.hosts.circle38.protocol import (
    VERSION,
    ProtocolError,
    build_response_error,
    build_response_ok,
    parse_request,
)


class Circle38Server:
    """宿主服务逻辑: 解码请求 -> 后端检测 -> 编码响应。"""

    def __init__(self, backend, host_version: str | None = None) -> None:
        self._backend = backend
        self._host_version = host_version or getattr(backend, "host_version", "unknown")
        self._decoder = MessageStreamDecoder()      # 跨调用持久 (见模块 docstring)
        self.requests_handled = 0

    def handle_bytes(self, data: bytes) -> bytes:
        """处理一批字节 (可能含半包/粘包); 返回可写回的响应字节 (可能为空)。"""
        try:
            messages = self._decoder.feed(data)
        except FramingError as exc:
            self._decoder = MessageStreamDecoder()   # 分帧错乱后重置, 避免持续崩
            return build_response_error("", f"分帧错误: {exc}", self._host_version)
        out = bytearray()
        for header, payload in messages:                # 处理**全部**完整消息
            out += self._handle_one(header, payload)
        return bytes(out)

    def _handle_one(self, header: dict, payload: bytes) -> bytes:
        request_id = str(header.get("request_id", ""))
        try:
            req = parse_request(header, payload)
        except (ProtocolError, FramingError) as exc:
            return build_response_error(request_id, f"请求非法: {exc}", self._host_version)
        self.requests_handled += 1
        t0 = time.monotonic()
        try:
            # DEC-055：后端契约是**本帧扁平矩形列表**（与 ``Circle38Proxy.detect``
            # 同一口径）；按图包裹由 ``NativeCircleBackend`` 自己做一次。
            # 这里若再包一层，原生会收到"一个空矩形"而不是"不限区域"。
            result = self._backend.detect([req.image], req.rects, req.params)
        except Exception as exc:  # noqa: BLE001 - 后端异常->错误报文, 不伪成功
            return build_response_error(request_id, f"后端异常: {exc}", self._host_version)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return build_response_ok(request_id, result, elapsed_ms, self._host_version)


def serve_forever(read: Callable[[], bytes], write: Callable[[bytes], None],
                  backend, *, running: Callable[[], bool] | None = None) -> None:
    """解释器间主循环. read() 返回已读字节(空表示暂无可读); write() 写响应."""
    server = Circle38Server(backend)
    while running is None or running():
        data = read()
        if not data:
            continue
        response = server.handle_bytes(data)
        if response:
            write(response)


__all__ = ["Circle38Server", "serve_forever"]
