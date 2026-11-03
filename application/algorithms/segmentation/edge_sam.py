# -*- coding: utf-8 -*-
"""智能分割：EdgeSAM 点提示分割（点 prompt → 目标 mask → 多边形）。

移植自旧工程 `Ganta_Crtl_System/vision_monitor/vision/edge_sam_infer.py`（两段式 ONNX，
X-AnyLabeling 合并版）。**判据不改**：encoder 输入固定 1024×1024（decoder 位置编码按
SAM 标准 1024 烘焙，改 512 会坐标整体错位一半——旧工程已实测），一张图只编码一次并缓存，
每次 predict 只跑轻量 decoder；mask 后处理（连通域锚定 + 多边形后过滤）逐字保留。

**与旧工程的唯一差异**：本工程帧的彩色图约定为 **RGB**（见 `image_ops.as_rgb` / 显示层），
故本封装输入即 RGB，**不再**做 BGR→RGB 转换（旧工程输入是 BGR）。

依赖 **onnxruntime**，惰性导入：未安装时构造抛 :class:`RuntimeError`，由界面如实提示。
模型目录由 :func:`resolve_models_dir` 解析（env / 包内 / 应用根 / 旧工程目录）。
离线栈用 :class:`StubSegmenter` 顶替真实模型——它**不加载任何模型**，只在提示点上做确定性
的凸包填充，用来跑通界面与流程；**绝不可**被读成"真实分割效果"。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional, Protocol, Sequence, runtime_checkable

import numpy as np

MODEL_DIR_ENV = "TOWER_OPS_EDGE_SAM_DIR"
ENCODER_NAME = "edge_sam_encoder.onnx"
DECODER_NAME = "edge_sam_decoder.onnx"
DEFAULT_INPUT_SIZE = 1024        # 勿改：decoder 位置编码按 1024 烘焙
MODEL_SUBDIR = os.path.join("models", "edge_sam-r20231213")


def _candidate_model_dirs() -> list[str]:
    """按优先级列出可能的模型目录（不一定都存在）。

    DEC-078：模型统一收进 **`tower-ops/vendor/models/edge_sam-r20231213`**（源码运行），
    打包后仍是包内 `models/edge_sam-r20231213`。
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))          # .../tower-ops
    cands: list[str] = []
    env = os.environ.get(MODEL_DIR_ENV)
    if env:
        cands.append(env)
    try:
        from application.bootstrap.app_paths import app_root, bundle_root
        cands.append(os.path.join(bundle_root(), MODEL_SUBDIR))
        cands.append(os.path.join(app_root(), MODEL_SUBDIR))
    except Exception:  # noqa: BLE001 - app_paths 不可用时退回相对路径
        cands.append(os.path.join(root, MODEL_SUBDIR))
    cands.append(os.path.join(root, MODEL_SUBDIR))
    cands.append(os.path.join(root, "vendor", MODEL_SUBDIR))
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        c = os.path.abspath(c)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _has_models(d: str) -> bool:
    return (os.path.isfile(os.path.join(d, ENCODER_NAME))
            and os.path.isfile(os.path.join(d, DECODER_NAME)))


def resolve_models_dir() -> Optional[str]:
    """返回第一个同时含两个 ONNX 的目录；都没有则 None（调用方据此报错/禁用）。"""
    for d in _candidate_model_dirs():
        if _has_models(d):
            return d
    return None


def models_available() -> bool:
    """是否能找到 EdgeSAM 模型（不含 onnxruntime 是否安装的检查）。"""
    return resolve_models_dir() is not None


def _probe_onnxruntime() -> Optional[str]:
    """尝试导入 onnxruntime；成功返回 None，失败返回原因字符串。"""
    try:
        import onnxruntime  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return f"onnxruntime 未安装/不可用：{exc}"
    return None


def check_available() -> Optional[str]:
    """智能分割是否可用：可用返回 None，否则返回**原因**（供界面如实提示）。

    只做非破坏性检查（不加载模型、不建会话）——建会话很慢，界面不该在"能不能用"上卡。
    """
    reason = _probe_onnxruntime()
    if reason:
        return reason
    if resolve_models_dir() is None:
        return (f"未找到 EdgeSAM 模型（{ENCODER_NAME} / {DECODER_NAME}）；"
                f"可设环境变量 {MODEL_DIR_ENV} 指定目录")
    return None


@runtime_checkable
class Segmenter(Protocol):
    """智能分割后端协议（真实 EdgeSAM 与离线 Stub 共用）。"""

    def set_image(self, image) -> float:
        """编码一张图（RGB），返回编码耗时毫秒。"""

    def predict(self, points: Sequence) -> tuple[np.ndarray, float, float]:
        """点提示 → (mask uint8 255=目标, score, 解码耗时毫秒)。"""


# ---------------------------------------------------------------------------
#  mask 后处理（两后端共用，判据来自旧工程）
# ---------------------------------------------------------------------------
def mask_primary_component(mask, point):
    """保留包含 ``point`` 的连通域（单目标锚定）。

    一组提示点来自同一个最终轮廓（同一目标），但 SAM 在真实图上可能把 mask 输出成
    多个面积相近的连通域（相对过滤拦不住）。以该组首个提示点所在连通域为准，其余丢弃；
    锚点不在任何前景连通域内时回退保留面积最大的连通域。

    Returns:
        (mask, n_dropped)：过滤后的 mask（连通域数必为 0 或 1）与被丢弃的连通域数。
    """
    import cv2
    if mask is None or np.asarray(mask).size == 0:
        return mask, 0
    mask = np.asarray(mask)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    if n <= 2:
        return mask, 0
    px, py = int(point[0]), int(point[1])
    if 0 <= py < mask.shape[0] and 0 <= px < mask.shape[1] and labels[py, px] > 0:
        kept = int(labels[py, px])
    else:
        kept = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    out = np.where(labels == kept, 255, 0).astype(np.uint8)
    return out, n - 2


def mask_to_polygons(mask, min_area: float = 30.0, eps_frac: float = 0.002):
    """二值 mask 转多边形顶点列表（后过滤规则对齐 XAnyLabeling）。

    Args:
        mask: uint8 二值图（255=目标）。
        min_area: 最小轮廓面积（丢弃噪点碎斑，绝对阈值）。
        eps_frac: approxPolyDP 的周长比例系数（越大顶点越少）。

    Returns:
        [np.ndarray Nx1x2 int32, ...] 多边形列表（外轮廓）。
    """
    import cv2
    if mask is None or np.asarray(mask).size == 0:
        return []
    contours, _ = cv2.findContours(np.asarray(mask), cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    polys = []
    for c in contours:
        if cv2.contourArea(c) < float(min_area):
            continue
        eps = float(eps_frac) * cv2.arcLength(c, True)
        poly = cv2.approxPolyDP(c, eps, True)
        if poly.shape[0] >= 3:
            polys.append(poly)
    if len(polys) > 1:
        img_area = float(mask.shape[0]) * float(mask.shape[1])
        polys = [p for p in polys if cv2.contourArea(p) < img_area * 0.9]
    if len(polys) > 1:
        areas = [cv2.contourArea(p) for p in polys]
        avg_area = float(np.mean(areas))
        polys = [p for p in polys if cv2.contourArea(p) > avg_area * 0.2]
    return polys


# ---------------------------------------------------------------------------
#  真实后端：EdgeSAM 两段式 ONNX
# ---------------------------------------------------------------------------
class EdgeSamSegmenter:
    """EdgeSAM 两段式推理（encoder 缓存 + 点 prompt 解码）。

    Args:
        model_dir: ONNX 所在目录；缺省用 :func:`resolve_models_dir`。
        providers: onnxruntime 执行提供器，缺省 CPU。
        input_size: encoder 输入边长（SAM 标准 1024，勿改）。

    Raises:
        RuntimeError: onnxruntime 未安装或模型文件缺失。
    """

    def __init__(self, model_dir: Optional[str] = None,
                 providers: Optional[list[str]] = None,
                 input_size: int = DEFAULT_INPUT_SIZE) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime 未安装，无法使用智能分割"
                "（ENV-MAIN: pip install onnxruntime）") from exc
        self.model_dir = model_dir or resolve_models_dir()
        if not self.model_dir:
            raise RuntimeError(
                f"未找到 EdgeSAM 模型（{ENCODER_NAME}/{DECODER_NAME}）；"
                f"可设环境变量 {MODEL_DIR_ENV} 指定目录")
        enc = os.path.join(self.model_dir, ENCODER_NAME)
        dec = os.path.join(self.model_dir, DECODER_NAME)
        missing = [p for p in (enc, dec) if not os.path.isfile(p)]
        if missing:
            raise RuntimeError("EdgeSAM 模型文件缺失: " + ", ".join(missing))
        self.input_size = int(input_size)
        self.providers = list(providers) if providers else ["CPUExecutionProvider"]
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self._enc = ort.InferenceSession(enc, so, providers=self.providers)
        self._dec = ort.InferenceSession(dec, so, providers=self.providers)
        self._embedding = None      # [1,256,64,64] float32
        self._crop_hw = None        # 编码时图尺寸 (H, W)

    @property
    def name(self) -> str:
        return "EdgeSAM(ONNX)"

    def set_image(self, image) -> float:
        """对输入图执行图像编码并缓存（RGB）。返回编码耗时毫秒。"""
        import cv2
        if image is None or np.asarray(image).size == 0:
            raise ValueError("空图像")
        arr = np.asarray(image)[:, :, :3]
        h, w = arr.shape[:2]
        img = cv2.resize(arr, (self.input_size, self.input_size),
                         interpolation=cv2.INTER_LINEAR)
        x = np.ascontiguousarray(img.transpose(2, 0, 1))[None]     # [1,3,S,S]
        t0 = time.perf_counter()
        self._embedding = self._enc.run(["image_embeddings"], {"input_image": x})[0]
        self._crop_hw = (h, w)
        return (time.perf_counter() - t0) * 1000.0

    @property
    def has_embedding(self) -> bool:
        return self._embedding is not None

    def predict(self, points, labels=None):
        """按提示点预测目标 mask（坐标为该编码图的像素坐标）。"""
        import cv2
        if self._embedding is None:
            raise RuntimeError("请先调用 set_image() 编码图")
        if not points:
            raise ValueError("至少需要一个提示点")
        n = len(points)
        if labels is None:
            labels = [1] * n
        if len(labels) != n:
            raise ValueError("labels 与 points 数量不一致")
        h, w = self._crop_hw
        sx = self.input_size / float(w)
        sy = self.input_size / float(h)
        coords = np.asarray([[float(px) * sx, float(py) * sy] for px, py in points],
                            dtype=np.float32)[None]
        labs = np.asarray(labels, dtype=np.float32)[None]
        t0 = time.perf_counter()
        scores, masks = self._dec.run(
            ["scores", "masks"],
            {"image_embeddings": self._embedding,
             "point_coords": coords, "point_labels": labs})
        scores = np.asarray(scores).reshape(-1)
        best = int(np.argmax(scores))
        m = np.asarray(masks)[0, best]
        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
        mask = ((m > 0).astype(np.uint8)) * 255
        return mask, float(scores[best]), (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------------------
#  离线后端：确定性 Stub（**不含任何模型**，仅用于跑通界面/流程）
# ---------------------------------------------------------------------------
class StubSegmenter:
    """离线占位分割器：把提示点凸包填充成 mask。

    **不是**真实分割——没有模型、没有语义，只保证"点在哪、掩码就在哪附近"，
    让界面、交集、存方案的流程能在无 onnxruntime 的沙箱里跑通并出图。界面须明确标注
    "离线占位（非真实模型）"。
    """

    name = "离线占位分割器（非真实模型）"

    def __init__(self, pad: int = 6) -> None:
        self._shape: Optional[tuple[int, int]] = None
        self._pad = int(pad)

    def set_image(self, image) -> float:
        arr = np.asarray(image)
        self._shape = arr.shape[:2]
        return 0.0

    def predict(self, points):
        import cv2
        if self._shape is None:
            raise RuntimeError("请先调用 set_image()")
        h, w = self._shape
        mask = np.zeros((h, w), dtype=np.uint8)
        pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
        if len(pts) == 0:
            return mask, 0.0, 0.0
        if len(pts) == 1:
            cx, cy = int(pts[0, 0]), int(pts[0, 1])
            r = max(2, self._pad)
            cv2.circle(mask, (cx, cy), r, 255, -1)
        else:
            hull = cv2.convexHull(pts.astype(np.int32))
            cv2.fillPoly(mask, [hull], 255)
            k = 2 * self._pad + 1
            mask = cv2.dilate(mask, np.ones((k, k), np.uint8))
            mask = np.clip(mask, 0, 255).astype(np.uint8)
        return mask, 1.0, 0.0


def make_segmenter(*, prefer_stub: bool = False) -> "Segmenter":
    """按可用性返回后端：默认真实 EdgeSAM，不可用则抛错；``prefer_stub`` 直接给占位。"""
    if prefer_stub:
        return StubSegmenter()
    return EdgeSamSegmenter()


def default_segmenter(*, offline: bool):
    """按运行档位给出智能分割后端与**如实标注**。

    Returns:
        (segmenter, note)：
        - 真实模型可用 -> (EdgeSamSegmenter, "EdgeSAM(ONNX，真实模型)")；
        - 不可用且 ``offline=True`` -> (StubSegmenter, "离线占位（非真实模型）：<原因>")，
          供离线栈/沙箱跑通流程（**不是**真实分割）；
        - 不可用且真机 -> (None, "<原因>")，调用方据此拒绝执行，不拿占位冒充。
    """
    reason = check_available()
    if reason is None:
        return EdgeSamSegmenter(), "EdgeSAM(ONNX，真实模型)"
    if offline:
        return StubSegmenter(), f"离线占位（非真实模型）：{reason}"
    return None, reason


__all__ = [
    "MODEL_DIR_ENV", "ENCODER_NAME", "DECODER_NAME", "MODEL_SUBDIR",
    "Segmenter", "EdgeSamSegmenter", "StubSegmenter",
    "resolve_models_dir", "models_available", "check_available", "make_segmenter",
    "default_segmenter",
    "mask_primary_component", "mask_to_polygons",
]
