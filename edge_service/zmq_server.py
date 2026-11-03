"""S100 V2 服务循环 (E08)。

- **REP 单所有者**：同一时刻严格一问一答（REQ/REP 语义），发送用 multipart；
- **鉴权与超限在解析/接任务前拒绝**（超限按字节数先拒，鉴权在接任务前）；
- 每次请求后调用 :meth:`CaptureJobManager.pump` —— **SDK 等待不占 REP**；
- 重启换 `service_epoch`，旧任务不隐式继续。

:meth:`V2Server.serve_forever` 会打开真实 socket，**默认不启用**：真实部署需按
[S100访问边界与密钥管理.md](../../doc/部署/S100访问边界与密钥管理.md) 确认后再启用。
离线测试只调 :meth:`V2Server.handle`（纯字节进、字节出，不碰网络）。
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from application.contracts.camera_v2 import (
    BYTE_ORDER,
    MAGIC,
    V2_VERSION,
    Envelope,
    ErrorCode,
    ProtocolMismatch,
    TooLarge,
    decode_request,
)
from edge_service.capture_jobs import CaptureJobManager, JobRejected
from edge_service.health import capabilities, health_report
from edge_service.result_store import JobResult
from edge_service.security import AccessDenied, AccessPolicy

DEFAULT_ENDPOINT_HINT = "tcp://<S100_IP>:<port>（端口由部署确认，代码不硬编码）"


class V2Server:
    """V2 服务端：纯函数式 ``handle`` + 可选真实循环。"""

    def __init__(self, registry: Any, jobs: CaptureJobManager, *,
                 policy: AccessPolicy | None = None,
                 server_info: dict | None = None) -> None:
        self.registry = registry
        self.jobs = jobs
        self.results = jobs.results
        # 默认**拒绝**：现场未确认前不对外服务
        self.policy = policy if policy is not None else AccessPolicy()
        self.server_info = dict(server_info or {})

    # ---------- 请求入口（不碰网络） ----------
    def handle(self, parts: Sequence[bytes]) -> list[bytes]:
        try:
            if not parts:
                return self._error(None, ErrorCode.BAD_ENVELOPE, "空请求")
            raw = bytes(parts[0])
            self.policy.check_size(raw)            # 1) 超限: 解析前就拒
            envelope = decode_request(raw)         # 2) 解析
            self.policy.authorize(envelope)        # 3) 鉴权: 接任务前就拒
        except TooLarge as exc:
            return self._error(None, ErrorCode.TOO_LARGE, str(exc))
        except AccessDenied as exc:
            return self._error(None, exc.code, str(exc))
        except ProtocolMismatch as exc:
            code = (ErrorCode.BAD_VERSION if "版本" in str(exc)
                    else ErrorCode.BAD_ENVELOPE)
            return self._error(None, code, str(exc))
        try:
            # 推进已有任务（SDK 等待不占 REP）。**只在 result/health 前 pump**：
            # submit 后立即 pump 会让任务当场完成、取消根本没窗口。
            if envelope.cmd in ("result", "health"):
                self.jobs.pump()
            return self._dispatch(envelope)
        except JobRejected as exc:
            return self._error(envelope, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - 内部错误不回显敏感细节
            return self._error(envelope, ErrorCode.INTERNAL, f"{type(exc).__name__}")

    def _dispatch(self, env: Envelope) -> list[bytes]:
        cmd = env.cmd
        if cmd == "ping":
            return self._ok({"ok": True, "service_epoch": self.jobs.service_epoch}, env)
        if cmd == "capabilities":
            return self._ok(capabilities(), env)
        if cmd == "health":
            return self._ok(health_report(self.registry, self.jobs, self.results,
                                          service_epoch=self.jobs.service_epoch,
                                          extra=self.server_info), env)
        if cmd == "submit":
            return self._submit(env)
        if cmd == "result":
            return self._result(env)
        if cmd == "cancel":
            cid = (env.payload or {}).get("capture_id")
            if not cid:
                return self._error(env, ErrorCode.BAD_ENVELOPE, "cancel 需要 capture_id")
            return self._ok({"cancelled": self.jobs.cancel(str(cid))}, env)
        if cmd == "release":
            cid = (env.payload or {}).get("capture_id")
            if not cid:
                return self._error(env, ErrorCode.BAD_ENVELOPE, "release 需要 capture_id")
            return self._ok({"released": self.results.release(str(cid))}, env)
        return self._error(env, ErrorCode.UNSUPPORTED, f"命令 {cmd!r} 不支持")

    def _submit(self, env: Envelope) -> list[bytes]:
        payload = env.payload or {}
        ticket = self.jobs.submit(
            request_id=env.request_id, client_id=env.client_id,
            channels=payload.get("cams") or [],
            deadline_ms=env.deadline_ms,
            required_streams=tuple(payload.get("required_streams") or ("color", "depth")),
            allow_partial=bool(payload.get("allow_partial", True)))
        return self._ok({"accepted": True, **ticket.as_dict()}, env)

    def _result(self, env: Envelope) -> list[bytes]:
        payload = env.payload or {}
        cid = payload.get("capture_id")
        if not cid:
            return self._error(env, ErrorCode.BAD_ENVELOPE, "result 需要 capture_id")
        cid = str(cid)
        cam = payload.get("cam")
        if self.results.expired(cid):
            return self._error(env, ErrorCode.EXPIRED, f"结果 {cid!r} 已过期")
        item = (self.results.get(cid, str(cam)) if cam else self.results.get(cid))
        if item is None:
            return self._error(env, ErrorCode.NOT_FOUND, f"未知 capture_id {cid!r}")
        return self._frame_reply(env, item)

    # ---------- 应答构造 ----------
    def _base(self, env: Envelope | None) -> dict:
        body: dict = {"v": V2_VERSION}
        if env is not None:
            body["request_id"] = env.request_id
        body["service_epoch"] = self.jobs.service_epoch
        return body

    def _ok(self, body: dict, env: Envelope | None) -> list[bytes]:
        head = self._base(env)
        head.update(body)
        return [MAGIC, json.dumps(head, ensure_ascii=False).encode("utf-8")]

    def _error(self, env: Envelope | None, code: ErrorCode, message: str) -> list[bytes]:
        head = self._base(env)
        head.update({"error": code.value, "message": message})
        return [MAGIC, json.dumps(head, ensure_ascii=False).encode("utf-8")]

    def _frame_reply(self, env: Envelope, item: JobResult) -> list[bytes]:
        body = {"capture_id": item.capture_id, "status": item.status,
                "cam": item.channel, "channel_epoch": item.channel_epoch,
                "seq": getattr(item.frame, "sequence", 0),
                "epoch": getattr(item.frame, "epoch", 0)}
        # 同一 capture 的各通道状态一并给出, 便于客户端判断集合结果
        body["channel_status"] = {r.channel: r.status
                                  for r in self.results.all_for(item.capture_id)}
        if item.error:
            body["frame_error"] = item.error
        frame = item.frame
        if frame is None:
            return self._ok(body, env)

        color, depth = frame.color, frame.depth
        if color is None and depth is None:
            return self._ok(body, env)
        ref = depth if depth is not None else color
        body.update({"width": int(ref.shape[1]), "height": int(ref.shape[0]),
                     "depth_scale_m": frame.depth_scale_m,
                     "device_timestamp_ns": frame.device_timestamp_ns,
                     "byte_order": BYTE_ORDER})
        intr = frame.intrinsics
        if intr is not None:
            body["intrinsics"] = {"fx": intr.fx, "fy": intr.fy,
                                  "ppx": intr.cx, "ppy": intr.cy,
                                  "width": intr.width, "height": intr.height}
        # 段索引按实际发出顺序分配 (color 在前), 头里显式声明, 不留歧义
        payloads: list[bytes] = []
        index = 2
        if color is not None:
            body["color"] = {"dtype": str(color.dtype), "shape": list(color.shape),
                             "nbytes": int(color.nbytes), "part": index}
            payloads.append(color.tobytes())
            index += 1
        if depth is not None:
            body["depth"] = {"dtype": str(depth.dtype), "shape": list(depth.shape),
                             "nbytes": int(depth.nbytes), "part": index}
            payloads.append(depth.tobytes())

        head = self._base(env)
        head.update(body)
        return [MAGIC, json.dumps(head, ensure_ascii=False).encode("utf-8"), *payloads]

    # ---------- 真实循环（默认不启用） ----------
    def serve_forever(self, endpoint: str, *, timeout_ms: int = 2000,
                      max_requests: int | None = None) -> None:
        """真实 REP 循环。**需先按部署文档确认访问边界后再启用。**

        单所有者严格交替 recv/send；异常只让本次请求失败，不退出循环。
        """
        import zmq
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.REP)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, int(timeout_ms))
        sock.bind(endpoint)
        served = 0
        try:
            while max_requests is None or served < max_requests:
                try:
                    parts = sock.recv_multipart()
                except zmq.Again:
                    continue
                served += 1
                try:
                    reply = self.handle(parts)
                except Exception:  # noqa: BLE001
                    reply = self._error(None, ErrorCode.INTERNAL, "服务内部错误")
                sock.send_multipart(reply)
        finally:
            sock.close(0)

    def restart(self) -> int:
        """服务重启：换 service_epoch，旧任务不隐式继续。"""
        return self.jobs.bump_epoch()


__all__ = ["DEFAULT_ENDPOINT_HINT", "V2Server"]
