"""DS86 真实采集的 SDK 桥（**移植**旧工程 `vision_monitor/vision/ds86_camera.py` 的采集流）。

移植原则：**采集流程不改** —— 设备扫描 → 按序列号打开 → 应用启动参数 → 启动流 →
读内参（按对齐模式选传感器）→ 逐帧取深度/彩色 → 停流关设备。旧代码里的坑也一并保留：

- `query_devices` 退出会 `scShutdown` 清空 SDK 内部设备列表，**打开前必须重新扫描**，
  否则 `scOpenDeviceBySN` 返回 `-15 (SC_INVALID_PARAMS)`；故打开时带重试扫描
  （3000 ms × 3，间隔 0.5 s，与旧 `ds86_camera.py` 一致）；界面**扫描设备**用旧
  `guidance_locate_tab` 的 2000 ms 单次（见 `SCAN_MS_PICK`）；
- 序列号要去尾部 `\\x00` 并用 `c_char_p` 显式包装（null 终止的 C 字符串）；
- 启动参数失败**只记录不中断**（设备可能不支持某些项）；
- 帧数据 `memmove` 出来后 **`copy()`**，避免被 SDK 下一帧覆盖。

SDK 可**注入**（离线测试用 mock），未注入时才惰性导入厂家包
（`scepter_sdk`，由 `bootstrap.vendor_paths` 把旧工程目录挂到 sys.path）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional

import numpy as np

# 支持的档位（与旧工程一致）
TOF_RESOLUTIONS = {"320x240": (320, 240), "640x480": (640, 480)}
COLOR_RESOLUTIONS = {"640x480": (640, 480), "800x600": (800, 600),
                     "1600x1200": (1600, 1200)}
DEPTH_SCALE_M = 0.001          # DS86 深度原始单位 = 毫米
SCAN_MS = 3000                 # 打开的**重扫**时长（旧 ds86_camera.py 打开前那轮扫描）
SCAN_ATTEMPTS = 3              # 打开的重扫次数（旧程序 3 次，每次间隔 SCAN_RETRY_SLEEP_S）
SCAN_MS_PICK = 2000            # 界面"扫描设备"的时长（旧 guidance_locate_tab.py 取 2000）
SCAN_RETRY_SLEEP_S = 0.5
STREAM_WARMUP_S = 1.0
FRAME_TIMEOUT_MS = 1200


class ScepterError(RuntimeError):
    """DS86 SDK 不可用或采集失败。"""


@dataclass(frozen=True)
class GrabResult:
    """一帧采集结果（数组已 copy，与 SDK 缓冲解耦）。"""

    depth: np.ndarray                       # uint16 (H, W)，单位毫米
    color: Optional[np.ndarray]             # uint8 (H, W, 3)，BGR
    intrinsics: dict
    depth_scale_m: float = DEPTH_SCALE_M


def import_scepter_sdk() -> Any:
    """惰性导入厂家包（顶层 `scepter_sdk`，由 vendor_paths 挂载）。失败抛明确错误。"""
    try:
        from scepter_sdk import ScepterDS_api, ScepterDS_enums, ScepterDS_types
    except Exception as exc:  # noqa: BLE001
        from application.bootstrap.vendor_paths import describe_import_failure
        raise ScepterError(describe_import_failure(
            "scepter_sdk", exc,
            note="需对应平台的厂家 SDK 已挂载（仓库内现只有 Windows x64 包；"
                 "Linux/ARM64 须厂家提供 Linux 版，并用 --ds86-sdk-dir 指定）")) from exc
    return SimpleNamespace(ScepterDS_api=ScepterDS_api, ScepterDS_enums=ScepterDS_enums,
                           ScepterDS_types=ScepterDS_types)


class ScepterDs86Device:
    """DS86 采集设备（一个 SDK 句柄所有者）。"""

    def __init__(self, sdk: Any = None, *, log: Callable[[str], None] | None = None) -> None:
        self._sdk = sdk
        self._cam: Any = None
        self._intrinsics: dict | None = None
        self._is_open = False
        self._align_mode = "none"
        self._log = log or (lambda _m: None)

    # ---------- SDK/枚举访问 ----------
    @property
    def sdk(self) -> Any:
        if self._sdk is None:
            self._sdk = import_scepter_sdk()
        return self._sdk

    def _enum(self, name: str) -> Any:
        return getattr(self.sdk.ScepterDS_enums, name)

    def _ctypes(self) -> Any:
        import ctypes
        return ctypes

    @staticmethod
    def available() -> tuple[bool, str]:
        """厂家包能否导入（不打开设备）。"""
        try:
            import_scepter_sdk()
            return True, ""
        except ScepterError as exc:
            return False, str(exc)

    @property
    def is_open(self) -> bool:
        return self._is_open and self._cam is not None

    @property
    def intrinsics(self) -> dict | None:
        return self._intrinsics

    # ---------- 枚举 ----------
    def enumerate(self, scan_ms: int = SCAN_MS_PICK, attempts: int = 1) -> list[dict]:
        """扫描可连设备（**只读**，不打开）。

        ``scan_ms``/``attempts`` 与旧程序对齐：界面"扫描设备"用 2000 ms 单次
        （旧 ``guidance_locate_tab._refresh_ds86_devices``）；打开前的重扫用
        3000 ms × 3（旧 ``ds86_camera.py``）—— 单次扫描在忙网上偶发 0 台，
        旧程序就是靠这轮重试扛过去的。
        """
        attempts = max(1, int(attempts))
        for attempt in range(1, attempts + 1):
            rows = self._scan_once(scan_ms)
            if rows or attempt == attempts:
                return rows
            time.sleep(SCAN_RETRY_SLEEP_S)
        return []

    def _scan_once(self, scan_ms: int) -> list[dict]:
        """扫一轮: 返回设备行（台数 0 或 SDK 异常都回空表, 由调用方决定要不要重扫）。"""
        ct = self._ctypes()
        cam = None
        out: list[dict] = []
        try:
            cam = self.sdk.ScepterDS_api.ScepterTofCam()
            count = cam.scGetDeviceCount(ct.c_uint32(scan_ms))
            if count <= 0:
                return []
            ret, info_list = cam.scGetDeviceInfoList(count)
            if ret != 0:
                return []
            for info in info_list:
                out.append({"name": _decode(info.productName),
                            "serial": _decode(info.serialNumber),
                            "ip": _decode(info.ip),
                            "status": int(info.status)})
        except Exception as exc:  # noqa: BLE001 - 枚举失败返回空, 由调用方报错
            self._log(f"[DS86] 枚举失败: {exc}")
            return []
        finally:
            try:
                del cam
            except Exception:  # noqa: BLE001
                pass
        return out

    # ---------- 打开/关闭 ----------
    def open(self, serial: str, params: dict | None = None, *,
             ip: str | None = None) -> None:
        """按序列号打开并启动流。``params`` 为采集参数（分辨率/帧率/对齐/曝光等）。

        DEC-081：``ip`` 非空时走厂家 ``scOpenDeviceByIP``（DS86 有 IP；设备配置里
        填了 IP 就用配置的），不再依赖“按 SN 打开”。序列号仍用于记录与诊断。
        """
        if self._is_open:
            return
        ct = self._ctypes()
        params = dict(params or {})
        serial = (serial or "").rstrip("\x00").strip()
        ip = (ip or "").strip()
        if not serial and not ip:
            raise ScepterError("DS86 未指定设备序列号或 IP（见设备配置页的相机序列号/IP）")
        align_mode = str(params.get("align_mode", "none"))
        self._align_mode = align_mode

        cam = self.sdk.ScepterDS_api.ScepterTofCam()
        self._cam = cam
        if ip:
            # DEC-081: 按**配置的 IP** 打开。厂家样例同样先扫一轮再 scOpenDeviceByIP
            # （SDK 内部要先有设备列表），这里照做；失败状态码由调用方解释。
            count = 0
            for _ in range(SCAN_ATTEMPTS):
                count = cam.scGetDeviceCount(ct.c_uint32(SCAN_MS))
                if count > 0:
                    break
                time.sleep(SCAN_RETRY_SLEEP_S)
            if count <= 0:
                self._cam = None
                raise ScepterError(
                    f"DS86 未扫描到设备，无法按 IP={ip!r} 打开；"
                    "请检查网络连接与设备供电（网段须与相机一致）")
            ret = cam.scOpenDeviceByIP(ct.c_char_p(ip.encode("utf-8")))
            if ret != 0:
                self._cam = None
                raise ScepterError(
                    f"scOpenDeviceByIP 失败, ScStatus({ret}), IP={ip!r}"
                    + (f", SN={serial!r}" if serial else ""))
        else:
            # 1) 重新扫描（旧坑: 枚举后 SDK 会清空设备列表, 不重扫则打开报 -15）
            count = 0
            for _ in range(SCAN_ATTEMPTS):
                count = cam.scGetDeviceCount(ct.c_uint32(SCAN_MS))
                if count > 0:
                    break
                time.sleep(SCAN_RETRY_SLEEP_S)
            if count <= 0:
                self._cam = None
                raise ScepterError("DS86 未扫描到设备，请检查网络连接与设备供电")
            # 2) 按序列号打开（显式 null 终止 C 字符串）
            ret = cam.scOpenDeviceBySN(ct.c_char_p(serial.encode("utf-8")))
            if ret != 0:
                self._cam = None
                raise ScepterError(f"scOpenDeviceBySN 失败, ScStatus({ret}), SN={serial!r}")
        # 3) 启动参数 + 启动流
        self._apply_startup_params(cam, params, align_mode)
        ret = cam.scStartStream()
        if ret != 0:
            self._cam = None
            raise ScepterError(f"scStartStream 失败, ScStatus({ret})")
        time.sleep(STREAM_WARMUP_S)
        # 4) 内参
        intr = self._read_intrinsics(cam, align_mode)
        if intr is None:
            self.close()
            raise ScepterError("读取相机内参失败（对齐空间可能不匹配）")
        self._intrinsics = intr
        self._is_open = True

    def close(self) -> None:
        cam, self._cam = self._cam, None
        self._is_open = False
        if cam is None:
            return
        for step in ("scStopStream", "scCloseDevice"):
            try:
                getattr(cam, step)()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 抓帧 ----------
    def grab(self, timeout_ms: int = FRAME_TIMEOUT_MS) -> GrabResult | None:
        """取一帧（深度必需；彩色可有）。超时返回 None，不抛异常。"""
        if not self.is_open:
            raise ScepterError("DS86 未打开，不能抓帧")
        ct = self._ctypes()
        cam = self._cam
        frame_type = self._enum("ScFrameType")
        ret, ready = cam.scGetFrameReady(ct.c_uint16(timeout_ms))
        if ret != 0:
            return None                                   # 超时不算错误

        depth = None
        color = None
        # 与旧工程一致：**按配置的对齐模式**选帧。
        # 只在 depth_to_color 时取「深度→彩色」帧；color_to_depth 时才取
        # 「彩色→深度」帧。不能只凭 ready.transformed* 判断，否则在 depth_to_color
        # 下可能拿到低分辨率深度系彩色帧，却用彩色内参算 XYZ（像素尺度错位）。
        if (self._align_mode == "depth_to_color"
                and getattr(ready, "transformedDepth", False)):
            ret_d, dframe = cam.scGetFrame(
                frame_type.SC_TRANSFORM_DEPTH_IMG_TO_COLOR_SENSOR_FRAME)
        elif getattr(ready, "depth", False):
            ret_d, dframe = cam.scGetFrame(frame_type.SC_DEPTH_FRAME)
        else:
            ret_d, dframe = -1, None
        if ret_d == 0 and dframe is not None and dframe.width > 0:
            depth = _frame_to_depth(dframe)

        if (self._align_mode == "color_to_depth"
                and getattr(ready, "transformedColor", False)):
            ret_c, cframe = cam.scGetFrame(
                frame_type.SC_TRANSFORM_COLOR_IMG_TO_DEPTH_SENSOR_FRAME)
        elif getattr(ready, "color", False):
            ret_c, cframe = cam.scGetFrame(frame_type.SC_COLOR_FRAME)
        else:
            ret_c, cframe = -1, None
        if ret_c == 0 and cframe is not None and cframe.width > 0:
            color = _frame_to_color(cframe)

        if depth is None:
            return None
        return GrabResult(depth=depth, color=color,
                          intrinsics=dict(self._intrinsics or {}),
                          depth_scale_m=DEPTH_SCALE_M)

    # ---------- 内部 ----------
    def _apply_startup_params(self, cam: Any, cfg: dict, align_mode: str) -> None:
        """分辨率/帧率/工作模式/对齐/曝光/增益/IR/HDR/滤波。**失败只记录不中断**（与旧工程一致）。

        DEC-046: 按旧工程 ``ds86_camera._apply_startup_params`` + ``_apply_filter_params``
        补齐 IR GMM 增益/校正、HDR/WDR 与时间/置信度/飞点/填洞/空间滤波 —— 这些键在
        guidance.yaml 的 ds86 段（前缀剥离后）里现场已调好，之前移植时漏了下发。
        键名/默认值逐项对齐旧程序；校正/滤波的结构体参数取自 ``ScepterDS_types``，
        测试假 SDK 没有该模块时只记录跳过，不中断打开。
        """
        ct = self._ctypes()

        def _try(label: str, fn: Callable[[], Any]) -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                self._log(f"[DS86] 设置 {label} 失败(忽略): {exc}")

        def _struct(name: str) -> Any:
            """取厂家结构体类；SDK 未导出该类型时抛错（由 _try 记录并跳过）。"""
            types = getattr(self.sdk, "ScepterDS_types", None)
            if types is None or not hasattr(types, name):
                raise ScepterError(f"SDK 未提供结构体 {name}")
            return getattr(types, name)

        w, h = TOF_RESOLUTIONS.get(str(cfg.get("tof_resolution", "640x480")), (640, 480))
        _try("TOF 分辨率", lambda: cam.scSetToFResolution(ct.c_int32(w), ct.c_int32(h)))
        cw, ch = COLOR_RESOLUTIONS.get(str(cfg.get("color_resolution", "640x480")),
                                       (640, 480))
        _try("彩色分辨率", lambda: cam.scSetColorResolution(ct.c_int32(cw), ct.c_int32(ch)))
        _try("帧率", lambda: cam.scSetFrameRate(ct.c_uint8(int(cfg.get("fps", 5)))))

        mode_map = {"active": self._enum("ScWorkMode").SC_ACTIVE_MODE,
                    "hardware_trigger": self._enum("ScWorkMode").SC_HARDWARE_TRIGGER_MODE,
                    "software_trigger": self._enum("ScWorkMode").SC_SOFTWARE_TRIGGER_MODE}
        sc_mode = mode_map.get(str(cfg.get("work_mode", "active")),
                               mode_map["active"])
        _try("工作模式", lambda: cam.scSetWorkMode(sc_mode))

        if align_mode == "color_to_depth":
            _try("对齐(color_to_depth)",
                 lambda: cam.scSetTransformColorImgToDepthSensorEnabled(ct.c_bool(True)))
        elif align_mode == "depth_to_color":
            _try("对齐(depth_to_color)",
                 lambda: cam.scSetTransformDepthImgToColorSensorEnabled(ct.c_bool(True)))

        exp_mode = self._enum("ScExposureControlMode")
        tof_mode = str(cfg.get("tof_exposure_mode", "auto"))
        sc_tof = (exp_mode.SC_EXPOSURE_CONTROL_MODE_AUTO if tof_mode == "auto"
                  else exp_mode.SC_EXPOSURE_CONTROL_MODE_MANUAL)
        sensor = self._enum("ScSensorType")
        _try("TOF 曝光模式",
             lambda: cam.scSetExposureControlMode(sensor.SC_TOF_SENSOR, sc_tof))
        if tof_mode == "manual":
            _try("TOF 曝光时间", lambda: cam.scSetExposureTime(
                sensor.SC_TOF_SENSOR, ct.c_int32(int(cfg.get("tof_exposure_time", 1000)))))

        color_mode = str(cfg.get("color_exposure_mode", "auto"))
        sc_color = (exp_mode.SC_EXPOSURE_CONTROL_MODE_AUTO if color_mode == "auto"
                    else exp_mode.SC_EXPOSURE_CONTROL_MODE_MANUAL)
        _try("彩色曝光模式",
             lambda: cam.scSetExposureControlMode(sensor.SC_COLOR_SENSOR, sc_color))
        if color_mode == "manual":
            _try("彩色曝光时间", lambda: cam.scSetExposureTime(
                sensor.SC_COLOR_SENSOR,
                ct.c_int32(int(cfg.get("color_exposure_time", 15000)))))
        _try("彩色增益", lambda: cam.scSetColorGain(ct.c_float(float(cfg.get("color_gain", 1.0)))))

        # IR GMM 增益 / 校正（旧工程 ds86_camera.py，现场 ir_gain=20）
        _try("IR GMM 增益",
             lambda: cam.scSetIRGMMGain(ct.c_uint8(int(cfg.get("ir_gain", 20)))))

        def _ir_correction() -> None:
            params = _struct("ScIRGMMCorrectionParams")()
            params.threshold = ct.c_int32(int(cfg.get("ir_correction_threshold", 0)))
            params.enable = ct.c_bool(bool(cfg.get("ir_correction_enable", False)))
            cam.scSetIRGMMCorrection(params)
        _try("IR GMM 校正", _ir_correction)

        # HDR / WDR（HDR 要求 TOF 曝光为手动，上面已设；低/高曝光按 frameIndex 0/1 下发）
        hdr_on = bool(cfg.get("hdr_enable", False))
        _try("HDR 开关", lambda: cam.scSetHDRModeEnabled(ct.c_bool(hdr_on)))
        if hdr_on:
            _try("HDR 低曝光(frameIndex=0)", lambda: cam.scSetExposureTimeOfHDR(
                ct.c_uint8(0), ct.c_int32(int(cfg.get("hdr_low_exposure_time", 500)))))
            _try("HDR 高曝光(frameIndex=1)", lambda: cam.scSetExposureTimeOfHDR(
                ct.c_uint8(1), ct.c_int32(int(cfg.get("hdr_high_exposure_time", 5000)))))
        _try("WDR 开关",
             lambda: cam.scSetWDRModeEnabled(ct.c_bool(bool(cfg.get("wdr_enable", False)))))

        # TOF 滤波：时间 / 置信度 / 飞点 三结构体 + 填洞 / 空间 两个开关（旧 _apply_filter_params）
        def _struct_filter(label: str, struct_name: str, setter: str,
                           enable_key: str, default_enable: bool,
                           thresh_key: str) -> None:
            def apply() -> None:
                params = _struct(struct_name)()
                params.enable = ct.c_bool(bool(cfg.get(enable_key, default_enable)))
                params.threshold = ct.c_int32(int(cfg.get(thresh_key, 0)))
                getattr(cam, setter)(params)
            _try(label, apply)

        _struct_filter("时间滤波", "ScTimeFilterParams", "scSetTimeFilterParams",
                       "time_filter_enable", True, "time_filter_threshold")
        _struct_filter("置信度滤波", "ScConfidenceFilterParams",
                       "scSetConfidenceFilterParams",
                       "confidence_filter_enable", True,
                       "confidence_filter_threshold")
        _struct_filter("飞点滤波", "ScFlyingPixelFilterParams",
                       "scSetFlyingPixelFilterParams",
                       "flying_pixel_filter_enable", True,
                       "flying_pixel_filter_threshold")
        _try("填洞滤波", lambda: cam.scSetFillHoleFilterEnabled(
            ct.c_bool(bool(cfg.get("fill_hole_filter_enable", False)))))
        _try("空间滤波", lambda: cam.scSetSpatialFilterEnabled(
            ct.c_bool(bool(cfg.get("spatial_filter_enable", False)))))

    def _read_intrinsics(self, cam: Any, align_mode: str) -> dict | None:
        """读对齐目标空间的内参：depth_to_color 用 COLOR 传感器，否则用 TOF。"""
        sensor = self._enum("ScSensorType")
        target = (sensor.SC_COLOR_SENSOR if align_mode == "depth_to_color"
                  else sensor.SC_TOF_SENSOR)
        try:
            ret, p = cam.scGetSensorIntrinsicParameters(target)
        except Exception as exc:  # noqa: BLE001
            self._log(f"[DS86] 读内参异常: {exc}")
            return None
        if ret != 0:
            return None
        return {"fx": float(p.fx), "fy": float(p.fy), "cx": float(p.cx), "cy": float(p.cy),
                "k1": float(p.k1), "k2": float(p.k2), "p1": float(p.p1), "p2": float(p.p2),
                "k3": float(p.k3), "k4": float(p.k4), "k5": float(p.k5), "k6": float(p.k6)}


class ScepterDs86Sdk:
    """把 :class:`ScepterDs86Device` 适配成 `Ds86Adapter` 期望的 SDK 外形。

    与 `adapters/ds86/mock_sdk.py::MockDs86Sdk` **同构**（enumerate_devices/open/close/
    get_intrinsics/read_frame），因此真实与离线走同一条适配器代码路径。
    """

    def __init__(self, params: dict | None = None, *,
                 device: "ScepterDs86Device | None" = None,
                 log: Callable[[str], None] | None = None) -> None:
        self._device = device if device is not None else ScepterDs86Device(log=log)
        self._params = dict(params or {})
        self._seq = 0

    @staticmethod
    def available() -> tuple[bool, str]:
        return ScepterDs86Device.available()

    def enumerate_devices(self, scan_ms: int = SCAN_MS_PICK,
                          attempts: int = 1) -> list[dict]:
        return self._device.enumerate(scan_ms=scan_ms, attempts=attempts)

    def open(self, serial: str, *, ip: str | None = None) -> None:
        self._device.open(serial, self._params, ip=ip)

    def close(self) -> None:
        self._device.close()

    def get_intrinsics(self) -> dict | None:
        return self._device.intrinsics

    def read_frame(self):
        """抓一帧，返回与 `MockScFrame` 同构的对象（适配器只依赖这些字段）。"""
        result = self._device.grab()
        if result is None:
            raise ScepterError("抓帧超时：未在超时内取到深度帧")
        self._seq += 1
        color = result.color
        if color is None:                      # 彩色缺失时给同尺寸占位, 不阻断深度
            color = np.zeros((*result.depth.shape, 3), dtype="uint8")
        return SimpleNamespace(
            depth=result.depth, color=color, intrinsics=result.intrinsics,
            seq=self._seq, timestamp_ns=int(time.monotonic() * 1e9),
            depth_scale_m=result.depth_scale_m)


# ---------------------------------------------------------------- 帧转换
def _frame_to_depth(frame: Any) -> np.ndarray:
    """ScFrame(深度 uint16) → np.ndarray (H, W) uint16（copy，避免被下一帧覆盖）。"""
    import ctypes
    size = int(frame.width) * int(frame.height)
    buf = (ctypes.c_uint16 * size)()
    ctypes.memmove(buf, frame.pFrameData, size * 2)
    return np.ctypeslib.as_array(buf).reshape(int(frame.height), int(frame.width)).copy()


def _frame_to_color(frame: Any) -> np.ndarray:
    """ScFrame(彩色 BGR888) → np.ndarray (H, W, 3) uint8（copy）。"""
    import ctypes
    size = int(frame.width) * int(frame.height) * 3
    buf = (ctypes.c_uint8 * size)()
    ctypes.memmove(buf, frame.pFrameData, size)
    return np.ctypeslib.as_array(buf).reshape(
        int(frame.height), int(frame.width), 3).copy()


def _decode(c_char_array: Any) -> str:
    """c_char 数组 → str（去掉尾部 null 与空白）。"""
    try:
        raw = bytes(c_char_array)
    except Exception:  # noqa: BLE001
        return ""
    return raw.split(b"\x00", 1)[0].decode("utf-8", "ignore").strip()


__all__ = [
    "COLOR_RESOLUTIONS", "DEPTH_SCALE_M", "GrabResult", "SCAN_ATTEMPTS", "SCAN_MS",
    "SCAN_MS_PICK", "SCAN_RETRY_SLEEP_S", "ScepterDs86Device", "ScepterDs86Sdk",
    "ScepterError", "TOF_RESOLUTIONS", "import_scepter_sdk",
]
