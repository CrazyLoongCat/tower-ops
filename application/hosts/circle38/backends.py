"""宿主后端: 真实原生（cp38 3.8 / 未来 Linux .so）、OpenCV 自研（DEC-072）与离线 stub。

NativeCircleBackend 只在宿主进程加载 timesai_tools; 主程序不得导入它.
加载失败明确报错, 不静默回退到 stub; Linux 无原生件时由入口选择 OpenCV 自研后端
（`opencv_backend.OpenCVCircleBackend`，host_version=opencv-linux）。
"""
from __future__ import annotations

import os
from typing import Any

from application.algorithms.circle.params import CircleParams
# app_paths 只用标准库 (3.8 也能导入), 故 3.8 宿主侧引用它不会引入 pydantic 等负担
from application.bootstrap import app_paths


class NativeLoadError(RuntimeError):
    """原生件加载失败 (ABI/DLL/路径)."""


def _default_module_dir() -> str:
    """cp38 原生件目录 (由本文件位置或冻结布局推导, 不依赖 cwd)。

    - **源码运行**：`tower-ops/vendor/circle38/detect_circle`（DEC-078 起统一收进本工程）；
    - **打包运行**：包内 `detect_circle/` —— 宿主 exe 自带原生件与 opencv DLL
      （见 `packaging/circle38_host.spec`）。主程序包里**没有**这一份，所以主程序若误调
      这里会明确报"目录不存在"，与"主程序不得导入 cp38 原生件"的约定一致。
    """
    if app_paths.is_frozen():
        return os.path.join(app_paths.bundle_root(), "detect_circle")
    here = os.path.dirname(os.path.abspath(__file__))
    project = os.path.abspath(os.path.join(here, "..", "..", ".."))   # tower-ops
    return os.path.join(project, "vendor", "circle38", "detect_circle")


# 导入时求值即可: 冻结后 bootloader 先于用户代码设好 sys.frozen/_MEIPASS
DEFAULT_MODULE_DIR = _default_module_dir()


def _safe_circle_num(ret: Any) -> int:
    """安全读取原生返回的 ``circle_num``（层数不定；任何异常都按 0）。

    原生 ``ret[2]`` 的包裹层数会随调用形态变化（``[[1]]`` / ``[[[1]]]`` / ``[]``），
    这里递归取第一个标量——只用于判断"要不要按旧工程做 resize 重试"，
    取不到就当作 0（重试无害），与旧工程 ``_safe_circle_num`` 的稳健口径一致。
    """
    try:
        node = ret[2]
    except (TypeError, IndexError, KeyError):
        return 0
    while isinstance(node, (list, tuple)):
        if not node:
            return 0
        node = node[0]
    try:
        return int(node)
    except (TypeError, ValueError):
        return 0


class NativeCircleBackend:
    """真实 cp38 圆检测. 仅可在 Python 3.8 宿主内构造 (惰性导入)."""

    def __init__(self, module_dir: str | None = None) -> None:
        # 注意: **不要**回退到本模块所在目录 —— 那会把"未配置路径"伪装成
        # "路径里有文件却导不进来"。缺省用仓库内 detect_circle, 并在 load() 校验。
        self._dir = module_dir or DEFAULT_MODULE_DIR
        self._fn = None

    @property
    def host_version(self) -> str:
        return "cp38-native"

    @property
    def module_dir(self) -> str:
        return self._dir

    def load(self) -> None:
        """安全导入原生件; 显式添加 DLL 目录 (3.8+ 不再从 sys.path 搜 DLL)."""
        import sys
        _dir = self._dir
        if not os.path.isdir(_dir):
            raise NativeLoadError(
                f"原生件目录不存在: {_dir!r} (用 --module-dir 指定 detect_circle 目录)")
        if not any(f.endswith((".pyd", ".so")) for f in os.listdir(_dir)):
            raise NativeLoadError(
                f"目录 {_dir!r} 内没有原生件 "
                f"(需要 timesai_tools.cp38-win_amd64.pyd 或 Linux 版 timesai_tools*.so)")
        if _dir not in sys.path:
            sys.path.insert(0, _dir)
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(_dir)
            except (OSError, FileNotFoundError) as exc:
                raise NativeLoadError(f"无法加载原生 DLL 目录 {_dir!r}: {exc}") from exc
        try:
            import timesai_tools  # type: ignore
        except Exception as exc:  # noqa: BLE001 - 明确报告, 不回退
            raise NativeLoadError(
                f"无法从 {_dir!r} 导入 timesai_tools (需 Python 3.8.10 + 同目录 DLL): {exc}"
            ) from exc
        self._fn = timesai_tools.detect_circle

    def detect(self, images: list[Any], rects: list[list[int]],
               params: CircleParams) -> dict:
        """``rects`` = **本帧扁平矩形列表**（DEC-055 后端契约），这里按图包一层。

        旧工程 ``detect_circle_imp`` 现场调用形如 ``[img], [[rect]]``；若把空列表
        ``[[]]`` 当成"不限区域"送进去，原生会按"一个空矩形"处理，返回结构多包
        一层（现场 ``circles_for_show=[[[]]]``）。主程序侧已改为无 ROI 送全图矩形，
        宿主这里只负责把它变成原生的按图形态。
        """
        if self._fn is None:
            raise NativeLoadError("原生后端未 load()")
        inputs = params.to_inputs()
        inputs["0"] = images
        per_image = [list(rects or [])]      # 单帧单图：按图包裹一次
        inputs["1"] = per_image
        inputs["2"] = per_image
        ret = self._fn(inputs)
        # DEC-055：对齐旧工程 detect_circle_imp 的多级 resize 重试——
        # 检不到圆时 1.0 → 0.5 → 0.2 再试（困难场景提高检出率；最后一次结果为准）。
        if _safe_circle_num(ret) == 0:
            for factor in (0.5, 0.2):
                inputs["19"] = factor
                ret = self._fn(inputs)
                if _safe_circle_num(ret) != 0:
                    break
        return {
            "statues": ret[0], "error_msg": ret[1], "circle_num": ret[2],
            "circles": ret[3], "circle_centers": ret[4], "radius": ret[5],
            "diameters": ret[6], "circle_points": ret[7],
            "circle_coverage": ret[8], "output_images": ret[9],
            "circles_for_show": ret[10],
        }


class StubCircleBackend:
    """离线 stub: 与原生同构的确定性返回; 不加载任何原生件, 不替代真实证据."""

    def __init__(self, circles: list[tuple[float, float, float]] | None = None,
                 *, statues: int = 1) -> None:
        self._circles = circles if circles is not None else [(320.0, 240.0, 40.0)]
        self._statues = statues

    @property
    def host_version(self) -> str:
        return "stub-offline"

    def detect(self, images: list[Any], rects: list[list[int]],
               params: CircleParams) -> dict:
        show = [[1, cx, cy, r] for (cx, cy, r) in self._circles]
        centers = [[[0, cx, cy] for (cx, cy, r) in self._circles]]
        radii = [[r for (cx, cy, r) in self._circles]]
        return {
            "statues": [self._statues], "error_msg": [""],
            "circle_num": [[len(self._circles)]],
            "circles": [None], "circle_centers": centers, "radius": radii,
            "diameters": [None], "circle_points": [None],
            "circle_coverage": [], "output_images": [None],
            "circles_for_show": [show],
        }


__all__ = ["NativeCircleBackend", "NativeLoadError", "StubCircleBackend"]
