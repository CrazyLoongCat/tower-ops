"""操作台「常用手工操作」弹窗（DEC-148）。

- :class:`ManualCalcDialog`：杆塔宽度 / 螺栓直径计算。算法与相机从
  `manual_ops.yaml` 恢复，改动即写回（下次打开直接是上次选的那套）。
- :class:`MotionAdjustDialog`：机械臂位置调整——把「运动方案 · 手动调整」
  页签装进弹窗操作（复用 MotionPage，不另造一套控制逻辑）。
"""
from __future__ import annotations

from typing import Any, Optional

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton,
        QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except ImportError:  # pragma: no cover
    GUI_REQUIRED = False

from application.services.operation_config import (
    BOLT, TWR, OperationConfigStore,
)

_TITLES = {TWR: "杆塔宽度计算", BOLT: "螺栓直径计算"}


def _store_of(ctx) -> OperationConfigStore:
    store = getattr(getattr(ctx, "stack", None), "operation_config", None)
    if isinstance(store, OperationConfigStore):
        return store
    mem = OperationConfigStore(None)      # 装配缺失时用内存默认，不写盘
    mem.load()
    return mem


def _enabled_cameras(ctx) -> list[tuple[str, str]]:
    store = getattr(getattr(ctx, "stack", None), "device_config", None)
    cfg = getattr(store, "config", None)
    out = []
    for cam in list(getattr(cfg, "cameras", None) or []):
        if getattr(cam, "enabled", False) and getattr(cam, "camera_id", ""):
            out.append((cam.camera_id, getattr(cam, "name", "") or cam.camera_id))
    return out


def _capture_frame(ctx, camera_id: str):
    """抓一帧（与视觉调试页同一套策略）；返回 (frame, error)。"""
    from application.contracts.frames import CaptureRequest, FreshnessPolicy
    from application.contracts.context import RunContext, VersionSnapshot

    stack = getattr(ctx, "stack", None)
    executor = getattr(stack, "executor", None)
    if executor is None:
        return None, "当前栈没有采集执行器"
    run_ctx = (getattr(getattr(stack, "task", None), "run_ctx", None)
               or RunContext(run_id="manual-op", versions=VersionSnapshot(
                   "debug", "v0", "-", "-")))
    cfg_store = getattr(stack, "device_config", None)
    cam_cfg = cfg_store.find_camera(camera_id) if cfg_store is not None else None
    policy = (FreshnessPolicy.ALLOW_ARRIVAL_ONLY
              if getattr(cam_cfg, "protocol", "") == "realsense_usb"
              else FreshnessPolicy.REQUIRE_DEVICE_TIME)
    try:
        cs = executor.capture(run_ctx, CaptureRequest(
            camera_ids=(camera_id,), request_id=f"manual-op-{camera_id}",
            step_id="manual_op", allow_partial=True, freshness_policy=policy))
    except Exception as exc:  # noqa: BLE001
        return None, f"抓拍失败：{exc}"
    err = cs.errors.get(camera_id)
    if err:
        return None, f"抓拍失败：{err}"
    frame = cs.frames.get(camera_id)
    if frame is None:
        return None, "抓拍没有返回帧"
    return frame, None


def _intrinsics_dict(frame) -> Optional[dict]:
    intr = getattr(frame, "intrinsics", None)
    if intr is None:
        return None
    try:
        return {"fx": float(intr.fx), "fy": float(intr.fy),
                "cx": float(intr.cx), "cy": float(intr.cy)}
    except Exception:  # noqa: BLE001
        return None


if GUI_REQUIRED:

    class ManualCalcDialog(QDialog):
        """杆塔宽度 / 螺栓直径计算（算法+相机选择持久化到 manual_ops.yaml）。"""

        def __init__(self, ctx, mode: str, parent=None) -> None:
            super().__init__(parent)
            self.ctx = ctx
            self.mode = mode
            self.store = _store_of(ctx)
            self._pending_value = None
            self.setWindowTitle(_TITLES.get(mode, "计算"))
            self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
            self.resize(760, 640)

            lay = QVBoxLayout(self)
            form = QGridLayout()
            form.setHorizontalSpacing(10)
            form.setVerticalSpacing(8)
            self.algo_combo = QComboBox()
            self.camera_combo = QComboBox()
            form.addWidget(QLabel("算法"), 0, 0)
            form.addWidget(self.algo_combo, 0, 1)
            form.addWidget(QLabel("相机"), 1, 0)
            form.addWidget(self.camera_combo, 1, 1)
            lay.addLayout(form)

            row = QHBoxLayout()
            self.run_button = QPushButton("运行")
            self.run_button.setObjectName("Primary")
            self.run_button.clicked.connect(self._run)
            row.addWidget(self.run_button)
            row.addStretch(1)
            lay.addLayout(row)

            from application.presentation.components.image_view import ImageView
            self.view = ImageView("运行后显示抓拍图与标线")
            self.view.setMinimumHeight(300)
            lay.addWidget(self.view, 1)

            self.result = QLabel("")
            self.result.setWordWrap(True)
            self.result.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            self.result.setMinimumHeight(70)
            self.result.setAlignment(Qt.AlignmentFlag.AlignTop
                                     | Qt.AlignmentFlag.AlignLeft)
            lay.addWidget(self.result)

            # DEC-151/154：计算成功后可人工改值再同步；螺栓直径固定 16/20/24 三档下拉
            from PySide6.QtWidgets import QDoubleSpinBox
            from application.services.operation_config import BOLT_SIZES
            self.sync_row = QHBoxLayout()
            self.sync_spin = QDoubleSpinBox()
            self.sync_spin.setRange(0.0, 65535.0)
            self.sync_spin.setDecimals(1)
            self.sync_spin.setSuffix(" mm")
            self.sync_combo = QComboBox()
            for v in BOLT_SIZES:
                self.sync_combo.addItem(f"{v:g}", v)
            self.sync_btn = QPushButton("同步到全局参数")
            self.sync_btn.setObjectName("Primary")
            self.sync_btn.clicked.connect(self._sync_value)
            self.sync_row.addWidget(QLabel("同步值"))
            self.sync_row.addWidget(self.sync_spin)
            self.sync_row.addWidget(self.sync_combo)
            self.sync_row.addWidget(self.sync_btn)
            self.sync_row.addStretch(1)
            self.sync_spin.setVisible(mode == TWR)
            self.sync_combo.setVisible(mode == BOLT)
            self.sync_widget = QWidget()
            self.sync_widget.setLayout(self.sync_row)
            self.sync_widget.setVisible(False)
            lay.addWidget(self.sync_widget)

            self._reload_choices()
            self.algo_combo.currentIndexChanged.connect(self._persist_selection)
            self.camera_combo.currentIndexChanged.connect(self._persist_selection)

        # ---------- 选择 ----------
        def _detections(self) -> list:
            store = getattr(getattr(self.ctx, "stack", None),
                            "vision_detections", None)
            try:
                return list(store.detections) if store is not None else []
            except Exception:  # noqa: BLE001 - 读不到当没有
                return []

        def _detection(self, detection_id: str):
            if not detection_id:
                return None
            store = getattr(getattr(self.ctx, "stack", None),
                            "vision_detections", None)
            try:
                return store.find(detection_id) if store is not None else None
            except Exception:  # noqa: BLE001
                return None

        def _reload_choices(self) -> None:
            sel = self.store.selection(self.mode)
            self.algo_combo.blockSignals(True)
            self.algo_combo.clear()
            # DEC-149：算法 = 视觉调试「另存为检测」保存的视觉检测（detections.yaml）
            self.algo_combo.addItem("默认（全局参数）", "")
            for det in self._detections():
                text = det.label or det.detection_id
                if det.camera_ref:
                    text = f"{text}（{det.camera_ref}）"
                self.algo_combo.addItem(text, det.detection_id)
            idx = self.algo_combo.findData(sel["algorithm"])
            self.algo_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.algo_combo.blockSignals(False)
            self.camera_combo.blockSignals(True)
            self.camera_combo.clear()
            for cid, label in _enabled_cameras(self.ctx):
                self.camera_combo.addItem(label, cid)
            idx = self.camera_combo.findData(sel["camera"])
            self.camera_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.camera_combo.blockSignals(False)
            if self.camera_combo.count() == 0:
                self.run_button.setEnabled(False)
                self.result.setText("设备配置里没有已启用的相机")

        def _persist_selection(self, *_a) -> None:
            self.store.set_selection(
                self.mode,
                camera=self.camera_combo.currentData() or "",
                algorithm=self.algo_combo.currentData() or "")
            try:
                self.store.save()
            except Exception:  # noqa: BLE001 - 保存失败不挡运行，下次打开退回旧选择
                pass

        def _sync_value(self) -> None:
            """把（可人工修改后的）计算值写入全局参数（manual_ops.yaml）。"""
            if self.mode == TWR:
                value = float(self.sync_spin.value())
            else:
                value = float(self.sync_combo.currentData())
            self.store.set_value(self.mode, value)
            try:
                self.store.save()
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"{self.result.text()}\n同步失败：{exc}")
                return
            # DEC-153：参数配置页已打开时（构建于启动时），同步后须就地刷新数值控件，
            # 否则页面仍显示旧值（0），直到重启才更新。
            try:
                win = self.window()
                pages = getattr(win, "pages", None)
                page = pages.get("settings") if isinstance(pages, dict) else None
                reload_fn = getattr(page, "reload_computed_values", None)
                if callable(reload_fn):
                    reload_fn()
            except Exception:  # noqa: BLE001 - 刷新失败不影响同步结果
                pass
            name = "杆塔宽度" if self.mode == TWR else "螺栓直径"
            self.result.setText(f"{self.result.text()}\n已同步到全局参数：{name} = {value:.1f} mm")

        # ---------- 运行 ----------
        def _run(self) -> None:
            self._persist_selection()
            cid = self.camera_combo.currentData()
            if not cid:
                self.result.setText("请先选择相机")
                return
            self.run_button.setEnabled(False)
            self.result.setText("计算中…")
            self._pending_value = None
            self.sync_widget.setVisible(False)
            try:
                frame, err = _capture_frame(self.ctx, cid)
                if err:
                    self.result.setText(err)
                    return
                params = self.store.algorithm_params(self.mode)
                snapshot = self._detection_snapshot(
                    self.algo_combo.currentData())
                if self.mode == TWR:
                    text = self._run_tower_width(frame, params, snapshot)
                else:
                    text = self._run_bolt_size(frame, params, snapshot)
                self.result.setText(text)
                if self._pending_value is not None and self._pending_value > 0:
                    if self.mode == TWR:
                        self.sync_spin.setValue(float(self._pending_value))
                    else:
                        from application.services.operation_config import (
                            nearest_bolt_size,
                        )
                        idx = self.sync_combo.findData(
                            nearest_bolt_size(self._pending_value))
                        self.sync_combo.setCurrentIndex(max(0, idx))
                    self.sync_widget.setVisible(True)
            except Exception as exc:  # noqa: BLE001 - 如实显示，不崩界面
                self.result.setText(f"计算异常：{exc}")
                self.sync_widget.setVisible(False)
            finally:
                self.run_button.setEnabled(True)

        def _detection_snapshot(self, detection_id: str) -> dict:
            """视觉检测里保存的找圆调试参数快照（DEC-047 的 depth/circle 段）。"""
            det = self._detection(detection_id)
            params = getattr(det, "params", None) if det is not None else None
            snap = (params or {}).get("circle_debug")
            return snap if isinstance(snap, dict) else {}

        def _frame_bgr(self, frame):
            color = getattr(getattr(frame, "color", None), "data", None)
            if color is None:
                return None
            import numpy as np, cv2
            rgb = np.ascontiguousarray(np.asarray(color)[:, :, :3])
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

        def _show_bgr(self, bgr) -> None:
            if bgr is None:
                self.view.clear_image("该相机没有彩色图，只有深度结果")
                return
            import cv2
            self.view.show_array(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB),
                                 kind="color")

        def _run_tower_width(self, frame, params: dict, snapshot: dict) -> str:
            from application.algorithms.guidance.plane_width import (
                measure_plane_width,
            )
            raw = getattr(getattr(frame, "depth", None), "data", None)
            scale = getattr(frame, "depth_scale_m", None)
            intr = _intrinsics_dict(frame)
            if raw is None or not scale:
                return "该帧缺原始深度或 depth_scale，不能算宽度"
            if intr is None:
                return "该帧缺内参，不能换算毫米"
            depth_cfg = (snapshot or {}).get("depth") or {}
            if isinstance(depth_cfg, dict):
                for key in ("depth_min_mm", "depth_max_mm"):
                    if key in depth_cfg:
                        params[key] = depth_cfg[key]
            info = measure_plane_width(
                raw, float(scale), intr,
                tol_mm=float(params.get("tol_mm", 8.0)),
                open_kernel=int(params.get("open_kernel", 5)),
                line_thr_mm=float(params.get("line_thr_mm", 3.0)),
                min_span=int(params.get("min_span", 60)),
                scan=str(params.get("scan", "auto")),
                depth_min_mm=float(params.get("depth_min_mm", 0.0)),
                depth_max_mm=float(params.get("depth_max_mm", 0.0)),
                min_width_mm=float(params.get("min_width_mm", 5.0)),
                max_width_mm=float(params.get("max_width_mm", 3000.0)))
            bgr = self._frame_bgr(frame)
            if bgr is not None and info.get("ok"):
                import cv2
                for pl, pr in info.get("pairs") or []:
                    for p, col in ((pl, (0, 0, 255)), (pr, (0, 180, 0))):
                        z = max(float(p[2]), 1.0)
                        u = int(round(intr["fx"] * float(p[0]) / z + intr["cx"]))
                        v = int(round(intr["fy"] * float(p[1]) / z + intr["cy"]))
                        cv2.circle(bgr, (u, v), 3, col, -1)
                txt = f"{info['width_mm']:.1f} mm"
                cv2.putText(bgr, txt, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                            (0, 0, 0), 4)
                cv2.putText(bgr, txt, (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                            (0, 255, 255), 2)
            self._show_bgr(bgr)
            if not info.get("ok"):
                return f"未得到结果：{info.get('reason', '未知原因')}"
            self._pending_value = float(info["width_mm"])
            return (f"杆塔宽度：{info['width_mm']:.1f} mm\n"
                    f"均值 {info['width_mean_mm']:.1f} · IQR {info['width_iqr_mm']:.1f}"
                    f" · 扫描 {info['scan_lines']} 条\n"
                    f"平面倾角 {info['tilt_deg']:.1f}° · "
                    f"面平均深度 {info['face_depth_mm']:.0f} mm")

        def _run_bolt_size(self, frame, params: dict, snapshot: dict) -> str:
            from application.algorithms.circle.params import CircleParams
            from application.algorithms.guidance.depth_guidance import (
                DepthGuidanceParams,
            )
            from application.services.guidance_debug import run_circle_debug

            # DEC-149：选中的视觉检测快照优先（找圆调试里调好的深度/找圆参数）
            dp_src = dict(params)
            depth_cfg = snapshot.get("depth") if isinstance(snapshot, dict) else None
            circle_cfg = snapshot.get("circle") if isinstance(snapshot, dict) else None
            if isinstance(depth_cfg, dict):
                known = set(DepthGuidanceParams.__dataclass_fields__)
                dp = DepthGuidanceParams.from_dict(
                    {k: v for k, v in depth_cfg.items() if k in known})
            else:
                known = set(DepthGuidanceParams.__dataclass_fields__)
                dp = DepthGuidanceParams(**{k: v for k, v in dp_src.items()
                                            if k in known})
            cp_src = dict(params)
            if isinstance(circle_cfg, dict):
                cp_src.update({k: v for k, v in circle_cfg.items()
                               if k in CircleParams.__dataclass_fields__})
            cp = CircleParams(
                circle_type=str(cp_src.get("circle_type", "最大圆")),
                min_radius=float(cp_src.get("min_radius", 15.0)),
                max_radius=float(cp_src.get("max_radius", 350.0)),
                grad_thresh=float(cp_src.get("grad_thresh", 30.0)))
            res = run_circle_debug(
                frame, depth_params=dp, circle_params=cp,
                host_proxy=getattr(getattr(self.ctx, "stack", None),
                                   "host_proxy", None))
            bgr = self._frame_bgr(frame)
            if bgr is not None:
                import cv2
                for rect in res.rects:
                    cv2.rectangle(bgr, (int(rect["x1"]), int(rect["y1"])),
                                  (int(rect["x2"]), int(rect["y2"])),
                                  (0, 180, 0), 2)
                for h in res.hits:
                    cv2.circle(bgr, (int(h.cx), int(h.cy)), int(h.radius),
                               (0, 0, 255), 2)
                    if float(h.diameter_mm or 0.0) > 0:
                        t = f"dia {h.diameter_mm:.1f}mm"
                        cv2.putText(bgr, t, (int(h.cx) - int(h.radius),
                                             int(h.cy) - int(h.radius) - 8),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
                        cv2.putText(bgr, t, (int(h.cx) - int(h.radius),
                                             int(h.cy) - int(h.radius) - 8),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            self._show_bgr(bgr)
            if not res.ok:
                return f"未得到结果：{res.message}"
            hits = [h for h in res.hits if float(h.diameter_mm or 0.0) > 0]
            if not hits:
                return f"未检出带深度的圆（矩形 {len(res.rects)} 个）"
            f = self._f(frame)
            hits.sort(key=lambda h: -float(h.diameter_mm))
            self._pending_value = float(hits[0].diameter_mm)
            lines = []
            for h in hits[:6]:
                z = (float(h.diameter_mm) * f / (2.0 * float(h.radius))
                     if h.radius and f > 0 else 0.0)
                lines.append(f"直径 {h.diameter_mm:.1f} mm"
                             f"（半径 {h.radius:.1f}px，圆区深度≈{z:.0f}mm）")
            if len(hits) > 1:
                lines.insert(0, f"共 {len(hits)} 个圆，最大值在前：")
            return "\n".join(lines)

        @staticmethod
        def _f(frame) -> float:
            intr = _intrinsics_dict(frame)
            if not intr:
                return 0.0
            return (intr["fx"] * intr["fy"]) ** 0.5

    class MotionAdjustDialog(QDialog):
        """机械臂位置调整：内嵌「运动方案 · 手动调整」页签。"""

        def __init__(self, ctx, parent=None) -> None:
            super().__init__(parent)
            self.ctx = ctx
            self.setWindowTitle("机械臂位置调整（运动方案 · 手动调整）")
            self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
            self.resize(1180, 780)
            from application.presentation.pages.motion import MotionPage
            self.page = MotionPage(ctx)
            self.page.tabs.setCurrentIndex(2)          # 手动调整
            # DEC-149：弹窗只展示「手动调整」，路径规划/路径实施页签隐藏
            self.page.tabs.tabBar().setVisible(False)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self.page)

        def closeEvent(self, event) -> None:  # pragma: no cover - Qt
            try:
                timer = getattr(self.page, "_timer", None)
                if timer is not None:
                    timer.stop()
            except Exception:  # noqa: BLE001
                pass
            super().closeEvent(event)


__all__ = ["GUI_REQUIRED", "ManualCalcDialog", "MotionAdjustDialog"]
