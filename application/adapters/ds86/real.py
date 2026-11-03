"""真实 DS86Adapter：**已接真实采集**（移植旧 `ds86_camera.py` 的采集流，见 scepter.py）。

- SDK 句柄所有者; 帧复制/受控持有后才发布 (避免 SDK 下一帧复用);
- 内参 ppx/ppy -> cx/cy; 深度保留 uint16 + depth_scale_m; 记录对齐空间与标定版本;
- 安全导入: 未注入 SDK 时才惰性导入厂家包, 导入期不采集.
- 采集参数(分辨率/帧率/对齐/曝光)由 `capture_params` 传入（来自 guidance.yaml 的 ds86 段）。
- **序列号获取照旧程序**：旧工程的相机配置里就没有序列号，运行期**扫描 + 选择**：
  界面「扫描设备」（2000 ms 单次扫描）由操作员挑一台写进配置；配置仍为空时，
  打开前按旧 `ds86_camera.py` 的重扫策略（3000 ms × 3）扫一遍，恰好一台才自动采用。

边界：本文件已接通真实采集逻辑，但**未在真机验证**（hardware=NOT_RUN）；
真机首次使用仍须按 D1 范围与现场样本确认（A25）。
"""
from __future__ import annotations

import inspect
import itertools
import re
import time
from typing import Any

import numpy as np

from application.adapters.ds86.scepter import SCAN_ATTEMPTS, SCAN_MS, SCAN_MS_PICK
from application.contracts.enums import FreshnessQuality, ImageSpace
from application.contracts.frames import (
    CaptureRequest,
    CaptureSet,
    FrameBundle,
    FreshnessPolicy,
    ImageRef,
    Intrinsics,
)


class Ds86SdkError(RuntimeError):
    pass


def _call_enumerate(fn, *, scan_ms: int, attempts: int) -> Any:
    """调用 SDK 的枚举接口, **只传它真正接受的参数**。

    真实包装 `ScepterDs86Sdk.enumerate_devices(scan_ms, attempts)` 收这两个参数；
    测试用的极简 mock（`MockDs86Sdk` / `_FakeSdk`）只接受零参。这里按签名过滤而不是
    ``try/except TypeError`` —— 后者会把 SDK 内部的真 TypeError 一并吞掉。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):          # 内建/无签名可查: 退回零参调用
        return fn()
    kwargs: dict = {}
    if "scan_ms" in params:
        kwargs["scan_ms"] = scan_ms
    if "attempts" in params:
        kwargs["attempts"] = attempts
    return fn(**kwargs)


def _serials_of(rows: Any) -> list[str]:
    """从 SDK 枚举结果里取**真实**序列号（缺失/空一律跳过，**不编造**）。

    曾经这里用 ``camera_id`` 顶替空 SN —— 那会把"一台设备都没枚举到"伪装成
    "枚举到一台叫 ds86_01 的设备"，真机打开时必报
    ``scOpenDeviceBySN -15 (SC_INVALID_PARAMS)``（2026-10-15 现场症状）。
    """
    out: list[str] = []
    for row in (rows or []):
        raw = (row.get("serial") if isinstance(row, dict)
               else getattr(row, "serial", None))
        sn = "" if raw is None else str(raw).strip()
        if sn and sn not in out:
            out.append(sn)
    return out


def _row_field(row: Any, key: str) -> str:
    raw = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    return "" if raw is None else str(raw).strip()


def _describe_rows(rows: Any) -> str:
    """在线设备行 -> ``SN（IP=x.x.x.x）`` 文本（无 SN 的行跳过）。"""
    out: list[str] = []
    for row in (rows or []):
        sn = _row_field(row, "serial")
        if not sn:
            continue
        ip = _row_field(row, "ip")
        item = f"{sn}（IP={ip}）" if ip else sn
        if item not in out:
            out.append(item)
    return "、".join(out)


# Scepter SDK `ScReturnStatus`（vendor/camera/scepter_sdk/ScepterDS_enums.py）。
# 打开失败时不只要给数字，还要说清楚"到底是网段、占用还是序列号"（DEC-080：
# 现场 -106 被统一按 -15 解释，把网络问题误报成序列号问题）。
_SC_STATUS_HINTS: dict[int, tuple[str, str]] = {
    -1: ("SC_NO_DEVICE_CONNECTED", "设备未连接；确认相机上电、网线已接。"),
    -15: ("SC_INVALID_PARAMS",
          "按序列号/IP 打开时设备列表里没有该设备（序列号/IP 不匹配，或枚举后"
          "设备列表已被清空而没重扫）。"),
    -100: ("SC_NO_ADAPTER_CONNECTED",
           "SDK 未找到可用网卡；确认连接相机的网卡已启用、网线正常。"),
    -103: ("SC_CAMERA_OPENED", "设备已经被打开；用完后先关闭再重开。"),
    -104: ("SC_CMD_ERROR", "与设备通信失败；检查网线/网段与设备是否忙。"),
    -105: ("SC_CMD_SYNC_TIME_OUT", "与设备通信超时；检查网线/网段，设备是否响应。"),
    -106: ("SC_IP_NOT_MATCH",
           "相机与主机网卡的 IP/子网掩码不匹配（不在同一网段）。枚举能看到设备，"
           "但 SDK 拒绝打开：把相机 IP 与主机网卡改到同一网段（例如相机 "
           "192.168.2.x、主机 192.168.2.y/24），或给主机加一块与相机同网段的网卡地址。"),
    -107: ("SC_NOT_STOP_STREAM", "相机流还没停止；先停流再关设备。"),
    -110: ("SC_CAMERA_OPENING", "设备正在被打开；稍后重试。"),
    -111: ("SC_CAMERA_OPENED_BY_ANOTHER_APP",
           "设备已被其它程序打开（如 ScepterGUITool、旧工程程序）；关闭占用程序后重试。"),
}


def _status_code_of(exc: Exception) -> int | None:
    """从 ``ScStatus(-106)`` / ``ScStatus(-15)`` 里取状态码；没有就返回 None。"""
    match = re.search(r"ScStatus\(\s*(-?\d+)\s*\)", str(exc))
    return int(match.group(1)) if match else None


def _status_hint(code: int) -> str:
    name, hint = _SC_STATUS_HINTS.get(
        code, ("", "SDK 未登记该状态码的说明；请把状态码与相机 IP 一并反馈。"))
    head = f"ScStatus({code})" + (f" = {name}" if name else "")
    return f"{head}：{hint}"


class Ds86Adapter:
    """DS86 随臂相机适配器. sdk 注入 Mock 或真实 Scepter 封装."""

    def __init__(self, camera_id: str = "ds86_01", *, sdk: Any = None,
                 serial: str | None = None, device_ip: str | None = None,
                 frame_id: str = "ds86",
                 alignment_space: ImageSpace = ImageSpace.DEPTH,
                 calibration_version: str | None = None,
                 pose_provider=None, capture_params: dict | None = None) -> None:
        self.camera_id = camera_id
        self._sdk = sdk
        self._serial = serial
        # DEC-081：设备配置里的相机 IP（可选）。DS86 有 IP，但 SDK 默认按 SN 打开；
        # 填了 IP 后启动打开会**先核对在线 SN 的 IP**，再走 scOpenDeviceByIP。
        self._device_ip = str(device_ip or "").strip()
        self._frame_id = frame_id
        self._alignment_space = alignment_space
        self._calibration_version = calibration_version
        self._pose_provider = pose_provider
        # 采集参数（分辨率/帧率/工作模式/对齐/曝光等）: 来自 guidance.yaml 的 ds86 段
        self._capture_params = dict(capture_params or {})
        self._open = False
        self._epoch = 0
        # 本次真正用来打开设备的序列号（配置留空时由枚举定出），只作运行期记录。
        self._resolved_serial: str | None = None
        self._seq = itertools.count(1)
        self._held: list[FrameBundle] = []
        self._pose_at_capture: list[float] | None = None

    # ---------- 生命周期 ----------
    def _ensure_sdk(self) -> None:
        """确保有可用 SDK：优先用注入的（测试 mock），否则建**真实采集**封装。

        真实路径 = `scepter.ScepterDs86Sdk`（移植自旧 `ds86_camera.py` 的采集流）；
        厂家包由 `bootstrap.vendor_paths` 挂载，**不复制**到本工程。
        """
        if self._sdk is not None:
            return
        from application.adapters.ds86.scepter import ScepterDs86Sdk, ScepterError
        ok, why = ScepterDs86Sdk.available()
        if not ok:
            raise Ds86SdkError(f"DS86 SDK 不可用: {why}")
        try:
            self._sdk = ScepterDs86Sdk(params=self._capture_params)
        except ScepterError as exc:  # pragma: no cover - 导入期已检查
            raise Ds86SdkError(f"创建 DS86 采集封装失败: {exc}") from exc

    def scan_devices(self, *, scan_ms: int = SCAN_MS_PICK,
                     attempts: int = 1) -> list[dict]:
        """扫描在线设备**行**（名称/序列号/IP/状态）—— 只读，不打开设备。

        与旧工程 `vision.ds86_camera.query_devices` 同一件事；界面「扫描设备」
        拿这些行让操作员**挑一台**，把序列号写进配置（旧工程就是这么取 SN 的）。
        """
        self._ensure_sdk()
        return _call_enumerate(self._sdk.enumerate_devices,
                               scan_ms=scan_ms, attempts=attempts)

    def enumerate(self) -> list[str]:
        """枚举**在线设备序列号**（只读，不打开）。只回真实 SN，缺 SN 的不顶替。"""
        return _serials_of(self.scan_devices())

    def scan_like_open(self) -> list[dict]:
        """按**打开前那轮重扫**的策略扫一遍（3000 ms × 3，旧 `ds86_camera.py`）。

        单次 2000 ms 扫描在忙网上偶发 0 台 —— 旧程序是靠在打开前连扫三次扛过去的，
        所以这里定序列号与给诊断时都用这轮扫描，不拿单次结果去下"现场没设备"的结论。
        """
        return self.scan_devices(scan_ms=SCAN_MS, attempts=SCAN_ATTEMPTS)

    def _resolve_serial(self) -> str:
        """定出**要打开的那台设备序列号**；任何情况下都不拿 ``camera_id`` 顶替。

        配置里填了序列号就用它（打错了会在打开失败时把在线 SN 一并列出来）；
        没填就用**打开前那轮重扫**的结果：**恰好一台**才自动采用（旧工程配置本就没有序列号，
        现场就是单台直连，行为与旧程序一致，并在日志里说明）；
        0 台或多台一律报明确错误，让操作员到设备配置页去定，而不是编一个 SN 硬试。

        DEC-081：配置了相机 IP 且没填序列号时，按 IP 在在线列表里找那台（SN 唯一才采用）——
        这样「填 IP 不填 SN」也能启动，而不是拿多台里的第一台硬试。
        """
        configured = str(self._serial or "").strip()
        if configured:
            return configured
        rows = self.scan_like_open()
        found = _serials_of(rows)
        if self._device_ip:
            matches = [row for row in rows
                       if _row_field(row, "ip") == self._device_ip]
            matched_sns = _serials_of(matches)
            if len(matched_sns) == 1:
                return matched_sns[0]
            if not matched_sns:
                raise Ds86SdkError(
                    f"DS86 未配置序列号，且在线设备里没有 IP={self._device_ip!r} 的相机"
                    f"（扫描到 {len(found)} 台：{_describe_rows(rows) or '无'}）："
                    "请核对设备配置页的相机 IP，或点「扫描设备」重新扫描。")
            raise Ds86SdkError(
                f"DS86 配置的 IP={self._device_ip!r} 对应多台设备"
                f"（SN: {'、'.join(matched_sns)}）：请同时填写序列号。")
        if len(found) == 1:
            return found[0]
        if not found:
            raise Ds86SdkError(
                "DS86 未配置序列号，且未扫描到任何在线设备"
                f"（已照旧程序在打开前重扫 {SCAN_MS} ms × {SCAN_ATTEMPTS}）："
                "请确认相机已上电、网线已接、没有被其它程序占用；"
                "或在设备配置页的相机「序列号」旁点「扫描设备」，从在线列表里挑一台填进去。")
        raise Ds86SdkError(
            f"DS86 未配置序列号，但扫描到 {len(found)} 台设备（SN: {'、'.join(found)}）："
            "请在设备配置页的相机「序列号」里指定要用的那一台（可点「扫描设备」选择）。")

    def _verify_configured_ip(self, serial: str) -> None:
        """DEC-081：启动打开前用**配置的 IP** 核对在线设备，IP 填错时先给明确原因。

        只扫一轮（3000 ms）。扫描不到任何设备时不在这里下结论 —— 让真正的打开失败
        去报 ScStatus（可能是 -106 网段不匹配，也可能是设备没上电）。
        """
        if not self._device_ip:
            return
        try:
            rows = self.scan_devices(scan_ms=SCAN_MS, attempts=1)
        except Exception:  # noqa: BLE001 - 核对失败不掩盖后面的打开结果
            return
        matches = [row for row in rows if _row_field(row, "serial") == serial]
        if not matches:
            return
        row_ip = _row_field(matches[0], "ip")
        if row_ip and row_ip != self._device_ip:
            raise Ds86SdkError(
                f"DS86 配置的相机 IP {self._device_ip!r} 与在线设备 {serial!r} 的 "
                f"IP {row_ip!r} 不一致：请到设备配置页核对相机 IP（或清空 IP，"
                "改为按序列号打开）。")

    def _open_failure_hint(self, serial: str, exc: Exception) -> str:
        """打开失败时补上"此刻到底有哪些设备在线" + **按真实状态码给原因**。

        2026-10-15 现场：``-15 (SC_INVALID_PARAMS)`` = 按 SN 打开时设备列表里没有该 SN
        （打错、设备没扫到、或枚举后列表被清空未重扫）。
        2026-10-23 现场：``-106 (SC_IP_NOT_MATCH)`` = 相机与主机网卡不在同一网段；
        旧实现无论实际状态码是什么都打印 -15 的说明，把网络问题误报成序列号问题
        （DEC-080）。这里只扫一轮（3000 ms）：打开时已经连扫过三次，再叠三次只会让
        界面多冻 9 秒。
        """
        try:
            rows = _call_enumerate(self._sdk.enumerate_devices,
                                   scan_ms=SCAN_MS, attempts=1)
        except Exception:  # noqa: BLE001 - 补诊断不得掩盖原始失败
            rows = []
        found = _serials_of(rows)
        lines = [f"DS86 打开失败: {exc}"]
        if self._device_ip:
            lines.append(f"配置的相机 IP: {self._device_ip}")
        if found:
            lines.append(f"当前在线设备 SN: {_describe_rows(rows)}")
            if serial not in found:
                lines.append(
                    f"配置的序列号 {serial!r} 不在其中 —— 请核对设备配置页的相机「序列号」"
                    "（可点「扫描设备」重新扫一遍挑；或清空，改为按唯一在线设备自动打开）。")
            else:
                lines.append(
                    f"配置的序列号 {serial!r} 在在线列表中，但打开被拒绝 —— "
                    "见下方状态码说明。")
        else:
            lines.append("且未枚举到任何在线设备（未上电 / 未接网线 / 被其它程序占用）。")
        code = _status_code_of(exc)
        if code is not None:
            lines.append(_status_hint(code))
        return "\n".join(lines)

    def open(self, camera_id: str) -> None:
        self._ensure_sdk()
        if camera_id != self.camera_id:
            raise ValueError(f"未知相机 {camera_id!r}")
        serial = self._resolve_serial()
        self._verify_configured_ip(serial)
        from application.adapters.ds86.scepter import ScepterError
        try:
            self._open_sdk(serial)
        except ScepterError as exc:
            raise Ds86SdkError(self._open_failure_hint(serial, exc)) from exc
        self._resolved_serial = serial
        self._open = True
        self._epoch += 1

    def _open_sdk(self, serial: str) -> None:
        """按配置打开：有 IP 用 ``open(serial, ip=...)``，否则按 SN（DEC-081）。

        用签名过滤而不是硬调 ``ip=`` —— 离线/测试的极简 SDK 只接受 ``open(serial)``，
        没配 IP 时必须保持旧路径不变。
        """
        fn = self._sdk.open
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        if not self._device_ip:
            fn(serial)
            return
        if "ip" not in params:
            raise Ds86SdkError(
                "当前 DS86 SDK 封装不支持按 IP 打开（open 没有 ip 参数）；"
                "请清空设备配置里的相机 IP，改为按序列号打开。")
        fn(serial, ip=self._device_ip)

    def close(self, camera_id: str | None = None) -> None:
        if self._sdk is not None:
            try:
                self._sdk.close()
            except Exception:  # noqa: BLE001
                pass
        self._open = False
        self._resolved_serial = None

    def status(self) -> dict:
        return {"camera_id": self.camera_id, "open": self._open,
                "epoch": self._epoch, "sdk": type(self._sdk).__name__,
                "serial": self._resolved_serial or str(self._serial or ""),
                "ip": self._device_ip}

    # ---------- 抓拍 ----------
    def capture(self, request: CaptureRequest) -> CaptureSet:
        cs = CaptureSet(request=request)
        for cid in request.camera_ids:
            if cid != self.camera_id:
                cs.errors[cid] = f"未配置相机 {cid!r}"
                continue
            if not self._open:
                cs.errors[cid] = "相机未打开"
                continue
            try:
                cs.frames[cid] = self._make_frame(request)
            except Exception as exc:  # noqa: BLE001
                cs.errors[cid] = f"采集失败: {exc}"
        cs.delta_quality = (FreshnessQuality.DEVICE_TIME_VERIFIED
                            if cs.frames else FreshnessQuality.UNKNOWN)
        return cs

    def _make_frame(self, request: CaptureRequest) -> FrameBundle:
        sdk_frame = self._sdk.read_frame()
        # 受控复制: 立即复制, 使已发布帧不受 SDK 下一帧复用影响 (T21)
        depth = np.array(sdk_frame.depth, dtype=np.uint16, copy=True)
        color = np.array(sdk_frame.color, copy=True)
        if depth.dtype != np.uint16:
            raise Ds86SdkError("深度必须是 uint16 原始值")
        intr = sdk_frame.intrinsics
        fx, fy = float(intr["fx"]), float(intr["fy"])
        cx = float(intr.get("cx", intr.get("ppx")))
        cy = float(intr.get("cy", intr.get("ppy")))
        h, w = depth.shape
        now = time.monotonic()
        if self._pose_provider is not None:
            self._pose_at_capture = self._pose_provider()
        seq = sdk_frame.seq
        fresh = (FreshnessQuality.ARRIVAL_ONLY
                 if request.freshness_policy is FreshnessPolicy.ALLOW_ARRIVAL_ONLY
                 else FreshnessQuality.DEVICE_TIME_VERIFIED)
        frame = FrameBundle(
            camera_id=self.camera_id,
            frame_id=f"{self.camera_id}-f{seq}",
            capture_id=f"{self.camera_id}-c{seq}",
            sequence=seq,
            session_epoch=self._epoch,
            service_id="local",
            channel_id=self.camera_id,
            serial=self._resolved_serial or str(self._serial or "unknown"),
            run_id=request.run_id,
            station_instance_id=request.station_instance_id,
            step_id=request.step_id,
            capture_request_id=request.request_id,
            device_timestamp_ns=sdk_frame.timestamp_ns,
            device_time_domain="scepter_device",
            producer_receive_monotonic_s=now,
            capture_accepted_monotonic_s=now,
            freshness_quality=fresh,
            color=ImageRef.from_array(color),
            depth=ImageRef.from_array(depth),
            depth_scale_m=float(sdk_frame.depth_scale_m),
            intrinsics=Intrinsics(fx=fx, fy=fy, cx=cx, cy=cy, width=w, height=h,
                                  intrinsics_space=self._alignment_space,
                                  calibration_version=self._calibration_version),
            alignment_space=self._alignment_space,
            calibration_version=self._calibration_version,
            depth_valid_ratio=float(np.count_nonzero(depth)) / depth.size,
            exposure_quality="UNKNOWN",
            capture_errors=(),
        )
        self._held.append(frame)
        # 已复制为自有数组；缓存有界，避免连续监控时无限积累彩色/深度帧。
        del self._held[:-8]
        return frame

    # ---------- 姿态关联 ----------
    def pose_at_last_capture(self) -> list[float] | None:
        return self._pose_at_capture

    def invalidates_on_move(self, current_pose: list[float] | None) -> bool:
        if self._pose_at_capture is None or current_pose is None:
            return False
        return any(abs(a - b) > 1e-6
                   for a, b in zip(self._pose_at_capture, current_pose))

    def held_frames(self) -> list[FrameBundle]:
        return self._held


__all__ = ["Ds86Adapter", "Ds86SdkError"]
