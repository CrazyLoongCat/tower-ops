"""A03 圆检测宿主/IPC 测试 (T33/T19/T21/T25, T38 兼容子项).

离线用 stub 后端验证 IPC 契约; 真实 cp38 原生加载/合法输出/正常退出证据
须在 3.8 宿主实测 (本文件的 stub 通过不算 A03 完成).
"""
from __future__ import annotations

import sys

import numpy as np
import pytest

from application.algorithms.circle.detector import CircleDetector
from application.algorithms.circle.params import CircleParams
from application.contracts.enums import DetectionStatus
from application.contracts.frames import FrameBundle, ImageRef, Intrinsics
from application.contracts.enums import ImageSpace
from application.hosts.circle38.backends import StubCircleBackend
from application.hosts.circle38.client import (
    Circle38ErrorResponse,
    Circle38HostCrash,
    Circle38LateResult,
    Circle38Proxy,
    Circle38Timeout,
    InProcessTransport,
)
from application.hosts.circle38.framing import (
    FramingError,
    MessageStreamDecoder,
    encode_message,
)
from application.hosts.circle38.protocol import (
    ProtocolError,
    Request,
    build_request,
    parse_response,
    parse_request,
)
from application.hosts.circle38.server import Circle38Server


class _FlakyBackend:
    host_version = "flaky"

    def __init__(self, mode="ok"):
        self.mode = mode

    def detect(self, images, rects, params):
        if self.mode == "raise":
            raise RuntimeError("native boom")
        return StubCircleBackend([(10.0, 20.0, 5.0)]).detect(images, rects, params)


def _image():
    return np.zeros((480, 640, 3), dtype=np.uint8)


class TestFraming:
    def test_roundtrip(self):
        raw = encode_message({"a": 1, "_payload_bytes": 3}, b"abc")
        msgs = MessageStreamDecoder().feed(raw)
        assert msgs == [({"a": 1, "_payload_bytes": 3}, b"abc")]

    def test_half_packet(self):
        raw = encode_message({"a": 1, "_payload_bytes": 3}, b"abc")
        dec = MessageStreamDecoder()
        assert dec.feed(raw[:5]) == []
        assert len(dec.feed(raw[5:])) == 1

    def test_sticky_packets(self):
        raw = encode_message({"a": 1, "_payload_bytes": 0}) + \
            encode_message({"b": 2, "_payload_bytes": 0})
        assert len(MessageStreamDecoder().feed(raw)) == 2

    def test_oversize_header_rejected(self):
        with pytest.raises(FramingError):
            encode_message({"x": "y" * (70 * 1024)})

    def test_declared_oversize_rejected(self):
        import struct
        # 手工构造声明超长 payload 的头
        bad = struct.pack(">I", 4) + b"{\"a\":1}" + b""
        # header 声明 payload_bytes 巨大 -> 解码时拒绝
        import json
        hdr = json.dumps({"_payload_bytes": 100 * 1024 * 1024}).encode()
        raw = struct.pack(">I", len(hdr)) + hdr
        with pytest.raises(FramingError):
            MessageStreamDecoder().feed(raw)


class TestProtocol:
    def test_request_roundtrip(self):
        req = Request(request_id="r1", image=_image(), rects=[[2, 1, 2, 3, 4, 0]],
                      params=CircleParams())
        header, payload = _split(build_request(req))
        parsed = parse_request(header, payload)
        assert parsed.request_id == "r1"
        assert parsed.image.shape == (480, 640, 3)
        assert parsed.image.dtype == np.uint8

    def test_length_mismatch_rejected(self):
        header = {
            "protocol": "jt.circle", "version": 1, "request_id": "r",
            "op": "detect", "shape": [2, 2, 3], "dtype": "uint8",
            "rects": [], "params": {},
        }
        with pytest.raises(ProtocolError):
            parse_request(header, b"\x00" * 5)  # 期望 12 字节

    def test_version_mismatch_rejected(self):
        header = {"protocol": "jt.circle", "version": 99, "request_id": "r",
                  "op": "detect", "shape": [1], "dtype": "uint8", "rects": [],
                  "params": {}}
        with pytest.raises(ProtocolError):
            parse_request(header, b"\x00")

    def test_bad_dtype_rejected(self):
        header = {"protocol": "jt.circle", "version": 1, "request_id": "r",
                  "op": "detect", "shape": [2], "dtype": "complex64", "rects": [],
                  "params": {}}
        with pytest.raises(ProtocolError):
            parse_request(header, b"\x00" * 8)


class TestServer:
    def test_ok_path(self):
        server = Circle38Server(StubCircleBackend([(100.0, 200.0, 30.0)]))
        req = Request("r1", _image(), [[2, 0, 0, 10, 10, 0]], CircleParams())
        header, payload = _split(server.handle_bytes(build_request(req)))
        resp = parse_response(header, payload)
        assert resp.ok and resp.request_id == "r1"
        assert resp.result["circle_num"] == [[1]]

    def test_backend_exception_becomes_error(self):
        server = Circle38Server(_FlakyBackend("raise"))
        req = Request("r1", _image(), [], CircleParams())
        header, _ = _split(server.handle_bytes(build_request(req)))
        resp = parse_response(header, b"")
        assert not resp.ok and "后端异常" in resp.error

    def test_bad_request_becomes_error(self):
        server = Circle38Server(StubCircleBackend())
        header, _ = _split(server.handle_bytes(encode_message(
            {"protocol": "jt.circle", "version": 1, "request_id": "r",
             "op": "detect", "shape": [2], "dtype": "uint8", "rects": [], "params": {},
             "_payload_bytes": 1}, b"\x00")))
        resp = parse_response(header, b"")
        assert not resp.ok and "请求非法" in resp.error


class TestProxy:
    def _proxy(self, backend=None):
        server = Circle38Server(backend or StubCircleBackend([(300.0, 200.0, 40.0)]))
        return Circle38Proxy(InProcessTransport(server))

    def test_ok(self):
        p = self._proxy()
        result = p.detect([_image()], [[2, 0, 0, 10, 10, 0]], CircleParams())
        assert result["circle_num"] == [[1]]

    def test_error_response_raises(self):
        p = self._proxy(backend=_FlakyBackend("raise"))
        with pytest.raises(Circle38ErrorResponse):
            p.detect([_image()], [], CircleParams())

    def test_crash_raises(self):
        server = Circle38Server(StubCircleBackend())
        transport = InProcessTransport(server)
        p = Circle38Proxy(transport)
        transport.close()
        with pytest.raises(Circle38HostCrash):
            p.detect([_image()], [], CircleParams())

    def test_timeout_raises(self):
        class TimeoutTransport:
            alive = True
            def request(self, payload, deadline_ms):
                return None
            def close(self):
                pass
        p = Circle38Proxy(TimeoutTransport())
        with pytest.raises(Circle38Timeout):
            p.detect([_image()], [], CircleParams())

    def test_late_result_archived(self):
        # 传输返回"上一次请求"的响应 -> 应作为迟到处理
        server = Circle38Server(StubCircleBackend())
        stale = server.handle_bytes(build_request(
            Request("circle-0", _image(), [], CircleParams())))

        class LateTransport:
            alive = True
            def request(self, payload, deadline_ms):
                return stale
            def close(self):
                pass
        p = Circle38Proxy(LateTransport())
        with pytest.raises(Circle38LateResult):
            p.detect([_image()], [], CircleParams())
        assert p.archived_late == ["circle-0"]

    def test_proxy_as_detector_backend_pass(self):
        p = self._proxy()
        frame = FrameBundle(
            camera_id="ds86_01", frame_id="f", capture_id="c", sequence=1,
            color=ImageRef.from_array(_image()),
            depth=ImageRef.from_array(np.zeros((480, 640), dtype=np.uint16)),
            depth_scale_m=0.001,
            intrinsics=Intrinsics(600, 600, 320, 240, 640, 480, ImageSpace.DEPTH),
        )
        det = CircleDetector(p, pipeline_id="pipe", pipeline_version="v1")
        r = det.run(frame, CircleParams(), result_id="res", host="circle38")
        assert r.status is DetectionStatus.PASS
        assert r.host == "circle38"


def test_main_program_does_not_import_native():
    """主程序侧不得导入 timesai_tools (ABI 边界)."""
    assert "timesai_tools" not in sys.modules


def test_host_path_does_not_require_pydantic_or_annotated():
    """**3.8 宿主导入路径不得依赖 pydantic / typing.Annotated (3.9+)**。

    子进程里阻断 pydantic, 再逐个 import 宿主链路模块; 任一模块引入 pydantic 即失败。
    (实机教训: contracts.results 曾 import contracts.ids -> pydantic -> 3.8 报
     `cannot import name 'Annotated' from 'typing'`, 让 A03 首次实跑失败。)
    """
    import os
    import subprocess
    import textwrap

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    code = textwrap.dedent("""
        import sys, importlib
        BLOCK = {"pydantic", "typing_extensions"}
        class Blocker:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in BLOCK:
                    raise ImportError("blocked by test: " + name)
                return None
        sys.meta_path.insert(0, Blocker())
        mods = [
            "application.contracts.enums",
            "application.contracts.results",
            "application.algorithms.circle.params",
            "application.algorithms.circle.normalize",
            "application.hosts.circle38.framing",
            "application.hosts.circle38.protocol",
            "application.hosts.circle38.backends",
            "application.hosts.circle38.server",
            "application.hosts.circle38.client",
        ]
        for m in mods:
            importlib.import_module(m)
        leaked = sorted({n.split(".")[0] for n in sys.modules} & BLOCK)
        assert not leaked, "宿主链路引入了主环境依赖: " + str(leaked)
        print("host-path import OK without pydantic")
    """)
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, cwd=root)
    assert r.returncode == 0, f"宿主链路 import 失败:\n{r.stdout}\n{r.stderr}"
    assert "OK without pydantic" in r.stdout


def _split(raw: bytes):
    msgs = MessageStreamDecoder().feed(raw)
    return msgs[0]


# ---------------------------------------------------------------- DEC-055
# 找圆矩形契约：主程序/代理/后端之间是**本帧扁平矩形列表**（[rect, ...]）；
# 只有 NativeCircleBackend 在调原生前按图包一层（inputs['1']/['2'] = [rects]）。
# 曾经 proxy 报文里的两个 rect 层被 server 再包一次，原生收到的是"一个空矩形"，
# 返回结构整体多包一层（现场 circles_for_show=[[[]]]、centers 深一层）。
class TestRectContract:
    def test_server_passes_flat_rects_to_backend(self):
        seen = {}

        class _Rec:
            host_version = "rec"

            def detect(self, images, rects, params):
                seen["rects"] = rects
                return StubCircleBackend().detect(images, rects, params)

        server = Circle38Server(_Rec())
        req = Request("r1", _image(), [[2, 0, 0, 10, 10, 0]], CircleParams())
        header, payload = _split(server.handle_bytes(build_request(req)))
        assert parse_response(header, payload).ok
        assert seen["rects"] == [[2, 0, 0, 10, 10, 0]]


class TestNativeBackendMapping:
    """NativeCircleBackend 的按图包裹与旧工程多级 resize 重试（不需要真 pyd）。"""

    @staticmethod
    def _backend(fake):
        from application.hosts.circle38.backends import NativeCircleBackend
        b = NativeCircleBackend(module_dir=".")
        b._fn = fake
        return b

    @staticmethod
    def _ret(circle_num=1, cx=300.0, cy=200.0, r=40.0):
        return ([1], [""], [[circle_num]], [None],
                [[[4.0, cx, cy]]], [[r]], [None], [None], [], [None],
                [[[1, cx, cy, r]]])

    def test_flat_rects_wrapped_per_image_for_native(self):
        calls = []

        def fake(inputs):
            calls.append(dict(inputs))
            return self._ret()

        backend = self._backend(fake)
        backend.detect([_image()], [[2, 0, 0, 640, 480, 0]], CircleParams())
        assert calls[0]["1"] == [[[2, 0, 0, 640, 480, 0]]]
        assert calls[0]["2"] == [[[2, 0, 0, 640, 480, 0]]]

    def test_resize_retry_matches_legacy_1_05_02(self):
        factors = []

        def fake(inputs):
            factors.append(inputs["19"])
            return self._ret(circle_num=0 if len(factors) < 3 else 1)

        backend = self._backend(fake)
        backend.detect([_image()], [[2, 0, 0, 640, 480, 0]], CircleParams())
        assert factors == [1.0, 0.5, 0.2]

    def test_resize_retry_stops_at_first_hit(self):
        factors = []

        def fake(inputs):
            factors.append(inputs["19"])
            return self._ret(circle_num=(0 if len(factors) == 1 else 1))

        backend = self._backend(fake)
        backend.detect([_image()], [[2, 0, 0, 640, 480, 0]], CircleParams())
        assert factors == [1.0, 0.5]
