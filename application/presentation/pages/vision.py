"""视觉调试页 (E19, 03 §7)。

- **通道列表来自设备配置** (`stores/devices.yaml` 的 cameras)，与设备配置页联动，
  不再写死；每行显示协议与分级状态（未启用/待核对/未连接/探测可达/已连接）。
- 可**选中通道**、**连接/断开**（开关真实会话）、**单次抓拍**并显示**彩色图与深度图**，
  附帧元数据（来源/序号/dtype/shape/深度比例/有效深度比/内参）。
- 无适配器的通道抓拍会给出**明确错误**，不伪装成功；离线合成图**明确标注**。
- **图像算法（OpenCV）** 面板：几何/十字线/显示/降噪/二值化/边缘/形态学/深度伪彩/检测。
  参数表见 `application.algorithms.image_ops`（**唯一事实源**，界面由它生成）。
  **作用范围 = 显示预览**: 改了参数立刻重画预览，**原始帧一律不动**、也不喂给「运行判定」;
  「结构化结果」里记一份本次生效参数快照。「保存到本通道」把参数写进设备配置
  （按通道存, 只写与默认值不同的项）。

- **另存为视觉检测**：把当前通道 + 检测流水线 + 参数快照命名保存为一条「视觉检测」
  （`VisionDetectionStore`，见 `application.services.vision_detections`）。配方 ``vision.run``
  步骤通过 ``detection_ref`` 引用它。左列「已保存的视觉检测」列出已存项，可删除。
  **是调试配置的命名保存，不是发布**；离线快照来自进程内 stub 流水线，非真机现场值。

- **找圆调试（DS86）**：把旧工程 `Ganta_Crtl_System/vision_monitor` 的 ROBOT 引导
  调试逻辑并入本页 —— 当前帧上分步执行「深度检测（深度引导→正外接矩形）→ 找圆
  （cp38 宿主）→ 运行一次找圆调试（圆心 XYZ + 粗/精偏移）」，可逐阶段查看中间图
  （归一化灰度/二值图/形态学滤波/轮廓筛选图），参数面板可载入 guidance.yaml。
  算法判据全部复用既有 1:1 移植模块（`application.services.guidance_debug`）；
  **只调试、不驱动机械臂、不改配方**，宿主不可用时如实报错不给假圆。

**版面（三列并排）**：`相机通道 + 帧元数据`（左，收窄） | `主图`（中） |
`图像算法 / 视觉分割 / 找圆调试`（右，三页签）；
主图内的**彩色与深度是上下叠放**。与设计基线 03 §7.1「左侧相机/采集区，中间主图，
右侧当前算法参数，底部结构化结果」一致。

调试结果不直接驱动机器人；连续预览需显式开启，不能占用自动抓拍资源。
**本页不建任何定时器** —— 定时器回调可能晚于控件销毁（见"Qt 关闭顺序"那条坑）。
"""
from __future__ import annotations

import os
from dataclasses import asdict, replace, is_dataclass

from application.adapters.s100.legacy import PROTOCOL_NAME as LEGACY_S100_PROTOCOL
from application.bootstrap import app_paths
from application.presentation.components import Card, EmptyHint, KV, SectionTitle
from application.presentation.components.annot_image_view import AnnotImageView
from application.presentation.components.image_view import GUI_REQUIRED, ImageView
from application.presentation.pages import state_of, store_of
from application.services.vision_detections import VisionDetection

if GUI_REQUIRED:  # pragma: no cover - Qt
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
        QListWidget, QListWidgetItem, QPushButton, QScrollArea,
        QSizePolicy, QSpinBox,
        QTabWidget, QVBoxLayout, QWidget,
    )

    from application.algorithms.circle.params import CircleParams
    from application.algorithms.guidance.depth_guidance import (
        CENTER_MODES, MORPH_OPS, RECT_FITS, RECT_MODES, SEG_FILTER_MODES,
        THRESH_METHODS,
        DepthGuidanceParams,
    )
    from application.services.guidance_debug import (
        STAGE_CHOICES, STAGE_ORIG, CircleDebugResult, depth_guidance_stage,
        find_circles_in_rects, run_circle_debug,
    )
    from application.algorithms.image_ops import (
        PANEL_GROUPS, apply_chain, coerce_params, default_params,
        depth_display_range_mm, describe, format_detections,
    )
    from application.algorithms.segmentation import (
        OP_REGION_LABELS,
        SOURCE_LABELS,
        RunOnceScheme,
        default_segmenter,
        run_scheme,
        scheme_from_params,
    )


def _plain(obj):
    """把 dataclass/对象转成可 JSON/YAML 落盘的普通结构（尽力而为）。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if is_dataclass(obj) and not isinstance(obj, type):
        try:
            return asdict(obj)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(obj, (list, tuple)):
        return [_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return str(obj)


def _ctx_stub(stack):  # pragma: no cover - 仅调试用
    from application.contracts.context import RunContext, VersionSnapshot
    return RunContext(run_id="vision-debug",
                      versions=VersionSnapshot("debug", "v0", "-", "-"))


def _ask_text(parent, title: str, label: str, text: str = "") -> tuple[str, bool]:  # pragma: no cover - Qt
    """取一段文本（唯一对话框入口，便于测试 monkeypatch，不阻塞自动化）。"""
    from PySide6.QtWidgets import QInputDialog
    return QInputDialog.getText(parent, title, label, text=text)


def _confirm(parent, title: str, text: str) -> bool:  # pragma: no cover - Qt
    from PySide6.QtWidgets import QMessageBox
    return QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    ) == QMessageBox.StandardButton.Yes


if GUI_REQUIRED:

    class VisionPage(QWidget):
        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.vm = ctx.vm
            self.store = store_of(ctx)
            self.state = state_of(ctx)
            self._frame = None
            self._frame_camera: str | None = None
            self._last_result = None
            self._loading = False            # 程序化填控件时不触发预览
            self._params_loaded_for: str | None = None
            self._widgets: dict = {}         # key -> (控件, 参数类型)
            # 视觉分割状态（E19 增强）：用户多边形/ROI、上次运行一次结果、分割参数控件
            self._polygon: list = []         # [[x, y], ...] 原图坐标
            self._roi = None                 # (x1, y1, x2, y2) 原图坐标
            self._seg_result = None          # 上次 RunOnceResult
            self._seg_widgets: dict = {}     # key -> 控件（分割参数）
            self._seg_camera: str | None = None   # 多边形/ROI 是按哪个通道画的
            # 找圆调试状态（旧工程 ROBOT 引导调试页并集）：参数控件、上次结果、矩形/圆缓存
            self._circle_widgets: dict = {}      # key -> 控件（深度引导 + 找圆参数）
            self._circle_result: "CircleDebugResult | None" = None
            self._circle_rects: list = []
            self._circle_hits: list = []
            self._circle_contours: list = []
            self._circle_stage_images: dict = {}   # 阶段图缓存（gray/binary/...）

            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            head = QHBoxLayout(); head.setSpacing(8)
            self.kv_sel = KV("当前通道", "未选择")
            head.addWidget(self.kv_sel)
            self.btn_conn = QPushButton("连接/断开"); self.btn_conn.setObjectName("Ghost")
            self.btn_conn.clicked.connect(self._toggle_conn)
            head.addWidget(self.btn_conn)
            self.btn_cap = QPushButton("单次抓拍"); self.btn_cap.setObjectName("Primary")
            self.btn_cap.clicked.connect(self._capture)
            head.addWidget(self.btn_cap)
            self.btn_open_file = QPushButton("载入图片"); self.btn_open_file.setObjectName("Ghost")
            self.btn_open_file.setToolTip(
                "载入本地抓帧（彩色图 + _depth.npy/_depth16.png + _depth.json）离线查看/调参；"
                "不连相机、不影响现场")
            self.btn_open_file.clicked.connect(self._load_local_images)
            head.addWidget(self.btn_open_file)
            self.btn_save_img = QPushButton("保存图片"); self.btn_save_img.setObjectName("Ghost")
            self.btn_save_img.setEnabled(False)
            self.btn_save_img.setToolTip(
                "保存本次抓拍帧：彩色 PNG + 原始深度（npy / 16位PNG / JSON 元数据）+ 伪彩预览；"
                "均不含显示旋转与图像算法预览")
            self.btn_save_img.clicked.connect(self._save_captured_image)
            head.addWidget(self.btn_save_img)
            self.btn_run = QPushButton("运行判定"); self.btn_run.setObjectName("Ghost")
            self.btn_run.clicked.connect(self._run_pipeline)
            head.addWidget(self.btn_run)
            # DEC-133：可选择覆盖哪条已保存检测；不选（新建）则弹名称新建
            self.det_overwrite = QComboBox()
            self.det_overwrite.setMinimumWidth(150)
            self.det_overwrite.setMaximumWidth(260)
            self.det_overwrite.setToolTip(
                "选择要覆盖的已保存视觉检测；选「（新建）」则保存为新检测")
            head.addWidget(self.det_overwrite)
            self.btn_save_det = QPushButton("另存为检测"); self.btn_save_det.setObjectName("Ghost")
            self.btn_save_det.setToolTip(
                "把当前通道 + 检测流水线 + 参数快照存成一条命名「视觉检测」"
                "（覆盖上面下拉选中的检测；选「（新建）」则新建），"
                "执行方案 vision.run 步骤即可引用它（不是发布）")
            self.btn_save_det.clicked.connect(self._save_as_detection)
            head.addWidget(self.btn_save_det)
            self.chk_raw = QCheckBox("原图对照（不套算法）")
            self.chk_raw.setToolTip("勾选后只显示原始帧，用来与算法预览对照；不改动原始帧")
            self.chk_raw.toggled.connect(lambda *_a: self._render_preview())
            head.addWidget(self.chk_raw)
            head.addStretch(1)
            self.kv_pipe = KV("流水线", "-")
            head.addWidget(self.kv_pipe)
            root.addLayout(head)

            body = QHBoxLayout(); body.setSpacing(10)

            # ---- 左列 (收窄): 相机通道 + 帧元数据 ----
            left_col = QVBoxLayout(); left_col.setSpacing(10)
            self.left = Card("相机通道（来自设备配置）")
            self.chan_list = QListWidget()
            self.chan_list.currentRowChanged.connect(lambda *_a: self._on_select())
            self.left.add(self.chan_list)
            self.chan_hint = EmptyHint("未启用/待核对的通道不可抓拍，不伪装 READY")
            self.left.add(self.chan_hint)
            left_col.addWidget(self.left, 3)

            # 帧元数据从右侧移到通道列表下方 (参数面板要占右侧原来的位置)
            self.meta = Card("帧元数据")
            self.kv_frame = KV("来源帧", "-")
            self.kv_seq = KV("序号", "-")
            self.kv_shape = KV("尺寸 / dtype", "-")
            self.kv_scale = KV("深度比例", "-")
            self.kv_valid = KV("有效深度比", "-")
            self.kv_intr = KV("内参", "-")
            for w in (self.kv_frame, self.kv_seq, self.kv_shape, self.kv_scale,
                      self.kv_valid, self.kv_intr):
                self.meta.add(w)
            self.meta.body().addStretch(1)   # 让 KV 靠上, 不被纵向摊开
            left_col.addWidget(self.meta, 2)
            left_wrap = QWidget(); left_wrap.setLayout(left_col)
            body.addWidget(left_wrap, 2)

            # ---- 主图 (中列): 彩色在上、深度在下 (**上下叠放**, 不再左右并排) ----
            self.mid = Card("主图（算法只作用于这里，原始帧不动）")
            mid_bar = QHBoxLayout(); mid_bar.addStretch(1)
            self.btn_image_popup = QPushButton("弹窗")
            self.btn_image_popup.setCheckable(True)
            self.btn_image_popup.setToolTip("把主图（彩色+深度）弹出为独立窗口；再点一次关闭")
            self.btn_image_popup.toggled.connect(self._toggle_image_popup)
            mid_bar.addWidget(self.btn_image_popup)
            self.mid.body().addLayout(mid_bar)
            # DEC-129：用 QGridLayout 两行等拉伸，保证彩色/深度两格高度（进而宽度）一致
            imgs = QGridLayout(); imgs.setSpacing(10)
            imgs.setRowStretch(0, 1)
            imgs.setRowStretch(1, 1)
            imgs.setColumnStretch(0, 1)
            for row, (caption, attr) in enumerate((("彩色", "view_color"),
                                                   ("深度（伪彩，0/65535=无效）",
                                                    "view_depth"))):
                cell = QWidget(); cv = QVBoxLayout(cell)
                cv.setContentsMargins(0, 0, 0, 0); cv.setSpacing(4)
                cap = QLabel(caption); cap.setObjectName("SubLabel")
                # 彩色用**可交互标注**视图（绘制/调整多边形、ROI、叠加），深度只读显示
                view = (AnnotImageView("尚未抓拍") if attr == "view_color"
                        else ImageView("尚未抓拍"))
                # DEC-129：彩色/深度两格**等宽等高**（同为 Expanding、同一最小高度）；
                # 此前彩色用了 AnnotImageView 的默认 Preferred 策略，纵向被深度的
                # Expanding 挤成一条窄带。
                view.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
                view.setMinimumHeight(200)
                cv.addWidget(cap); cv.addWidget(view, 1)
                setattr(self, "cap_color" if attr == "view_color" else "cap_depth", cap)
                setattr(self, attr, view)
                imgs.addWidget(cell, row, 0)
            self.mid.body().addLayout(imgs, 1)
            # 深度取点读数放在两格**下方**（不占深度格高度 -> 两行严格等高，DEC-129/130）
            self.depth_probe = QLabel(
                "点击深度图可查看该点深度（原始值 / mm / 相机 XYZ）")
            self.depth_probe.setObjectName("SubLabel")
            self.depth_probe.setWordWrap(True)
            self.mid.body().addWidget(self.depth_probe)
            self.view_depth.clicked.connect(self._on_depth_click)
            self.src_hint = QLabel(""); self.src_hint.setObjectName("SubLabel")
            self.src_hint.setWordWrap(True)
            self.preview_hint = QLabel(""); self.preview_hint.setObjectName("SubLabel")
            self.preview_hint.setWordWrap(True)
            self.mid.add(self.src_hint)
            self.mid.add(self.preview_hint)
            body.addWidget(self.mid, 4)
            self._image_slot = body

            # ---- 右列: 参数面板 (与主图**并排**)。用**页签**分「图像算法」「视觉分割」：
            # 两套参数各自成页，都不再占主图高度；页签本身只占一列宽度。 ----
            self.right_tabs = QTabWidget()
            self.right_tabs.setObjectName("RightTabs")

            self.algo = Card("图像算法（OpenCV）")
            self.algo_note = QLabel(
                "参考旧工程 视觉监控；只影响显示预览 —— 不改原始帧、不参与「运行判定」。"
                "几何变换对彩色与深度两路同时生效；长度/面积一律像素；"
                "深度切分上下限按毫米(mm)，应用时按本帧深度比例换算（本工程 1 原始值 = 1 mm）。")
            self.algo_note.setObjectName("SubLabel")
            self.algo_note.setWordWrap(True)
            self.algo.body().addWidget(self._build_algo_panel(), 1)
            self.algo.add(self.algo_note)
            algo_bar = QHBoxLayout(); algo_bar.addStretch(1)
            self.btn_algo_reset = QPushButton("恢复默认"); self.btn_algo_reset.setObjectName("Ghost")
            self.btn_algo_reset.setToolTip("把面板上的参数改回默认值（不会动已保存的配置）")
            self.btn_algo_reset.clicked.connect(self._reset_params)
            self.btn_algo_save = QPushButton("保存到本通道"); self.btn_algo_save.setObjectName("Ghost")
            self.btn_algo_save.setToolTip(
                "写进设备配置 (devices.yaml)，按通道保存；只写与默认值不同的项")
            self.btn_algo_save.clicked.connect(self._save_params)
            algo_bar.addWidget(self.btn_algo_reset)
            algo_bar.addWidget(self.btn_algo_save)
            self.algo.body().addLayout(algo_bar)
            self.right_tabs.addTab(self.algo, "图像算法")

            self.seg = Card("视觉分割（深度分割 / 智能分割 / 运行一次）")
            self.seg.body().addWidget(self._build_seg_panel(), 1)
            seg_help = QLabel(
                "流程同旧工程：画多边形 → 深度分割（轮廓内提示点）→ 智能分割"
                "（EdgeSAM 点提示）→ 运行一次（智能分割 ∩ 用户多边形）。"
                "画 ROI：在彩色主图上拖拽（左键或右键）。「清除」清掉多边形/提示点/分割框（ROI 保留）。"
                "「另存为检测」保存的就是运行一次这套逻辑（图源/区域/多边形/参数），"
                "执行方案 flow 的 vision.run 按检测名在真实帧上重跑同一算法。")
            seg_help.setObjectName("SubLabel"); seg_help.setWordWrap(True)
            self.seg.add(seg_help)
            self.seg_hint = QLabel(""); self.seg_hint.setObjectName("SubLabel")
            self.seg_hint.setWordWrap(True)
            self.seg_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.seg.add(self.seg_hint)
            seg_modes = QHBoxLayout()
            self.btn_draw_poly = QPushButton("绘制多边形"); self.btn_draw_poly.setObjectName("Ghost")
            self.btn_draw_poly.setCheckable(True)
            self.btn_draw_poly.setToolTip("左键依次加点，右键闭合（≥3 点）")
            self.btn_draw_poly.clicked.connect(lambda *_a: self._seg_mode("polygon"))
            self.btn_edit_poly = QPushButton("调整多边形"); self.btn_edit_poly.setObjectName("Ghost")
            self.btn_edit_poly.setCheckable(True)
            self.btn_edit_poly.setToolTip(
                "调整模式：拖动整体/顶点、双击边插点、右键顶点删除；选中后 Delete 删除；"
                "同时可拖动/缩放 ROI")
            self.btn_edit_poly.clicked.connect(lambda *_a: self._seg_mode("edit"))
            self.btn_draw_roi = QPushButton("画 ROI"); self.btn_draw_roi.setObjectName("Ghost")
            self.btn_draw_roi.setCheckable(True)
            self.btn_draw_roi.setToolTip("在彩色主图上拖拽（左键或右键）画运算区域；运算区域选「ROI裁剪图」时生效")
            self.btn_draw_roi.clicked.connect(lambda *_a: self._seg_mode("roi"))
            for b in (self.btn_draw_poly, self.btn_edit_poly, self.btn_draw_roi):
                seg_modes.addWidget(b)
            self.seg.body().addLayout(seg_modes)
            seg_btns = QHBoxLayout()
            self.btn_seg_clear = QPushButton("清除"); self.btn_seg_clear.setObjectName("Ghost")
            self.btn_seg_clear.setToolTip(
                "清除用户多边形、提示点与分割叠加框（轮廓/交集）；ROI 作为运算区域保留")
            self.btn_seg_clear.clicked.connect(self._seg_clear_all)
            self.btn_seg_depth = QPushButton("深度分割"); self.btn_seg_depth.setObjectName("Ghost")
            self.btn_seg_depth.setToolTip("只跑深度分割，标出最终轮廓与轮廓内提示点")
            self.btn_seg_depth.clicked.connect(self._run_depth_seg)
            self.btn_run_once = QPushButton("运行一次"); self.btn_run_once.setObjectName("Primary")
            self.btn_run_once.setToolTip("在当前帧上执行完整运行一次逻辑（真实视觉算法）")
            self.btn_run_once.clicked.connect(self._run_once)
            for b in (self.btn_seg_clear, self.btn_seg_depth, self.btn_run_once):
                seg_btns.addWidget(b)
            self.seg.body().addLayout(seg_btns)
            self.right_tabs.addTab(self.seg, "视觉分割")

            # ---- 「找圆调试」页签：旧工程 ROBOT 引导调试页并入本页（并集） ----
            # 与「图像算法」「视觉分割」共用同一帧：抓拍一次，三个页签都可反复调参重跑。
            self.circle = Card(
                "找圆调试（深度引导 → 正外接矩形 → cp38 找圆 → XYZ / 粗精偏移）")
            self.circle.body().addWidget(self._build_circle_panel(), 1)
            circle_help = QLabel(
                "流程（与旧工程 ROBOT 引导页一致）：当前帧 →「深度检测」（深度切分/归一化/"
                "二值化/形态学/轮廓过滤 → 正外接矩形）→「找圆」（每个矩形经 cp38 宿主找圆）"
                "→「运行一次找圆调试」（选第一个圆取均值深度换算相机系 XYZ 与粗/精偏移）。"
                "「调试画面」下拉可逐阶段查看中间图。找圆必须走 cp38 宿主；宿主不可用会"
                "如实报错，不给假圆。此处只调试、不驱动机械臂、不改配方。")
            circle_help.setObjectName("SubLabel"); circle_help.setWordWrap(True)
            self.circle.add(circle_help)
            self.circle_hint = QLabel(""); self.circle_hint.setObjectName("SubLabel")
            self.circle_hint.setWordWrap(True)
            self.circle_hint.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            self.circle.add(self.circle_hint)
            circle_bar = QHBoxLayout()
            self.btn_cd_depth = QPushButton("深度检测"); self.btn_cd_depth.setObjectName("Ghost")
            self.btn_cd_depth.setToolTip("只做深度引导（步骤1+2）：标出保留轮廓与正外接矩形")
            self.btn_cd_depth.clicked.connect(self._run_circle_depth)
            self.btn_cd_circles = QPushButton("找圆"); self.btn_cd_circles.setObjectName("Ghost")
            self.btn_cd_circles.setToolTip("对上次深度检测的矩形逐个找圆（没有矩形时先自动深度检测）")
            self.btn_cd_circles.clicked.connect(self._run_circle_hit)
            self.btn_cd_run = QPushButton("运行一次找圆调试")
            self.btn_cd_run.setObjectName("Primary")
            self.btn_cd_run.setToolTip("完整链路：深度引导 → 找圆 → 选中圆 XYZ → 粗/精偏移")
            self.btn_cd_run.clicked.connect(self._run_circle_full)
            self.btn_cd_clear = QPushButton("清除叠加"); self.btn_cd_clear.setObjectName("Ghost")
            self.btn_cd_clear.setToolTip("清除矩形/圆/轮廓叠加与阶段图缓存（ROI 多边形保留）")
            self.btn_cd_clear.clicked.connect(self._clear_circle_debug_all)
            for b in (self.btn_cd_depth, self.btn_cd_circles, self.btn_cd_run,
                      self.btn_cd_clear):
                circle_bar.addWidget(b)
            circle_bar.addStretch(1)
            self.btn_cd_load_cfg = QPushButton("载入引导配置")
            self.btn_cd_load_cfg.setObjectName("Ghost")
            self.btn_cd_load_cfg.setToolTip(
                "把 guidance.yaml（DS86 引导：深度/找圆/偏移参数）载入本面板，"
                "用来对照现场调参结果")
            self.btn_cd_load_cfg.clicked.connect(self._load_circle_from_config)
            circle_bar.addWidget(self.btn_cd_load_cfg)
            self.circle.body().addLayout(circle_bar)
            self.right_tabs.addTab(self.circle, "找圆调试")

            # ---- 「棱线测量」页签：平面两条棱线间距（DEC-143 脚本界面化，DEC-144） ----
            self.edge = Card("棱线测量（深度最大平面 → 两条棱线间距）")
            self.edge.body().addWidget(self._build_edge_panel(), 1)
            edge_help = QLabel(
                "流程：当前帧 → 「测量棱线间距」（有效深度 → 逆深度 RANSAC 最大平面 → "
                "逐行/逐列取轮廓两端点 → 两侧各拟合 3D 直线，两侧同为内点的行求同高度"
                "两端点三维距离）。主图叠加：红线=一侧棱边、绿线=另一侧棱边。参数全部"
                "可调；只调试、不驱动机械臂、不改配方。")
            edge_help.setObjectName("SubLabel"); edge_help.setWordWrap(True)
            self.edge.add(edge_help)
            edge_bar = QHBoxLayout()
            self.btn_edge_run = QPushButton("测量棱线间距")
            self.btn_edge_run.setObjectName("Primary")
            self.btn_edge_run.setToolTip(
                "用当前帧深度数据测「最大平面」两条轮廓棱线的三维间距（面宽）")
            self.btn_edge_run.clicked.connect(self._run_edge_measure)
            self.btn_edge_clear = QPushButton("清除叠加")
            self.btn_edge_clear.setObjectName("Ghost")
            self.btn_edge_clear.setToolTip("清除棱线测量的左右端点叠加")
            self.btn_edge_clear.clicked.connect(self._clear_edge_overlay)
            edge_bar.addWidget(self.btn_edge_run)
            edge_bar.addWidget(self.btn_edge_clear)
            edge_bar.addStretch(1)
            self.edge.body().addLayout(edge_bar)
            self.right_tabs.addTab(self.edge, "棱线测量")

            self.right_tabs.currentChanged.connect(self._on_right_tab_changed)

            body.addWidget(self.right_tabs, 2)

            root.addLayout(body, 1)

            self.result_card = Card("结构化结果")
            self.result = QLabel("-"); self.result.setObjectName("SubLabel")
            self.result.setWordWrap(True)
            # **可选中复制**: 结构化结果（状态/流水线/指标/原因/错误）常要贴进聊天或工单，
            # 不该"只能看不能拷" —— 鼠标拖选后 Ctrl+C 即可。
            self.result.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard)
            self.result.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            # 结果行数随内容变化(保存/判定会变长): **封顶 + 内部滚动**。
            # 否则结果卡越长、body 被压得越扁, 压缩到富文本 QLabel 的实际行高以下时
            # 第二行会被裁掉(帧元数据看起来像"糊在一起"), 还会把窗口顶高到超过设计尺寸。
            self.result_scroll = QScrollArea()
            self.result_scroll.setWidgetResizable(True)
            self.result_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            self.result_scroll.setMinimumHeight(90)
            self.result_scroll.setMaximumHeight(160)
            self.result_scroll.setWidget(self.result)   # 注意: 必须在 add 进 card 之前
            self.result_card.add(self.result_scroll)
            copy_bar = QHBoxLayout()
            copy_bar.addStretch(1)
            self.btn_copy = QPushButton("复制结果"); self.btn_copy.setObjectName("Ghost")
            self.btn_copy.setToolTip("把「结构化结果」整段复制到剪贴板")
            self.btn_copy.clicked.connect(self._copy_result)
            copy_bar.addWidget(self.btn_copy)
            self.result_card.body().addLayout(copy_bar)

            # 已保存的视觉检测（「另存为检测」落盘，供配方 vision.run 的 detection_ref 引用）。
            # 与「结构化结果」同处底部一行：**不另占纵向空间** —— 曾踩过把帧元数据压糊的坑。
            self.dets = Card("已保存的视觉检测")
            self.det_list = QListWidget()
            self.det_list.setToolTip("「另存为检测」保存的视觉检测；执行方案 vision.run 步骤"
                                     "可引用其名称；双击可把它的运行一次方案与找圆调试参数载回面板")
            self.det_list.itemDoubleClicked.connect(lambda *_a: self._load_detection_scheme())
            self.dets.add(self.det_list)
            self.det_hint = EmptyHint("还没有保存的检测；抓拍调好后点「另存为检测」。")
            self.dets.add(self.det_hint)
            det_bar = QHBoxLayout(); det_bar.addStretch(1)
            self.btn_det_del = QPushButton("删除所选"); self.btn_det_del.setObjectName("Ghost")
            self.btn_det_del.clicked.connect(self._delete_detection)
            det_bar.addWidget(self.btn_det_del)
            self.dets.body().addLayout(det_bar)

            bottom = QHBoxLayout(); bottom.setSpacing(10)
            bottom.addWidget(self.result_card, 3)
            bottom.addWidget(self.dets, 2)
            root.addLayout(bottom)
            self._wire_annot_view()
            self.refresh()
            self._refresh_detections()

        # =================================================================
        #  视觉分割（E19 增强）：绘制/调整多边形、深度分割、智能分割、运行一次
        # =================================================================
        def _wire_annot_view(self) -> None:  # pragma: no cover - Qt
            """把彩色主图的交互信号接到本页状态。"""
            v = self.view_color
            v.polygon_drawn.connect(self._on_polygon_drawn)
            v.polygon_edited.connect(self._on_polygon_edited)
            v.polygon_deleted.connect(lambda: self._set_polygon([]))
            v.roi_drawn.connect(self._on_roi_drawn)
            v.roi_edited.connect(self._on_roi_drawn)
            v.roi_deleted.connect(lambda: self._set_roi(None))

        def _build_seg_panel(self):  # pragma: no cover - Qt
            """分割参数面板（图源/区域/最终轮廓筛选/提示点 + 深度分割关键参数）。"""
            inner = QWidget()
            form = QVBoxLayout(inner)
            form.setContentsMargins(2, 2, 2, 2); form.setSpacing(6)

            def row(label, widget):
                h = QWidget(); hl = QHBoxLayout(h)
                hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(6)
                lab = QLabel(label); lab.setObjectName("SubLabel")
                hl.addWidget(lab); hl.addWidget(widget, 1)
                form.addWidget(h)

            def combo(key, choices, default, tip=""):
                w = QComboBox()
                for code, text in choices:
                    w.addItem(str(text), code)
                idx = w.findData(default)
                w.setCurrentIndex(idx if idx >= 0 else 0)
                w.setToolTip(tip)
                self._seg_widgets[key] = w
                return w

            def spin(key, lo, hi, val, step=1, tip=""):
                w = QSpinBox(); w.setRange(lo, hi); w.setSingleStep(step)
                w.setValue(val); w.setToolTip(tip)
                self._seg_widgets[key] = w
                return w

            def dspin(key, lo, hi, val, step=1.0, dec=1, tip=""):
                w = QDoubleSpinBox(); w.setRange(lo, hi); w.setSingleStep(step)
                w.setDecimals(dec); w.setValue(val); w.setToolTip(tip)
                self._seg_widgets[key] = w
                return w

            form.addWidget(SectionTitle("运行一次设置"))
            row("处理图源", combo(
                "source", SOURCE_LABELS, "color",
                "智能分割编码图来源：彩色图/深度伪彩/融合图(彩色×深度分割掩码)"))
            row("运算区域", combo(
                "op_region", OP_REGION_LABELS, "whole",
                "整张图，或先按 ROI 裁剪再运算（ROI 用「画 ROI」在彩色图上拖出）"))
            row("最终轮廓筛选", combo(
                "seg_filter_mode", tuple((m, m) for m in SEG_FILTER_MODES), "所有轮廓",
                "深度分割最终保留哪个/哪些轮廓"))
            row("保留轮廓数", spin("seg_filter_n", 1, 20, 1,
                                  tip="筛选保留的轮廓个数"))
            row("提示点数 k", spin("prompt_k", 1, 20, 3,
                                  tip="每个轮廓取几个提示点（智能分割的前景提示）"))
            row("提示点边距 px", dspin("prompt_margin", 0.0, 200.0, 5.0,
                                       tip="除首点外提示点到轮廓边缘的最小距离（像素）"))
            form.addWidget(SectionTitle("深度分割参数（步骤1）"))
            row("深度下限 mm", dspin("depth_min_mm", 0.0, 65535.0, 200.0, dec=0,
                                    tip="小于该深度(毫米)视为远/背景"))
            row("深度上限 mm", dspin("depth_max_mm", 0.0, 65535.0, 3000.0, dec=0,
                                    tip="大于该深度(毫米)视为近/无效"))
            # 二值化默认口径 = 旧现场配置（S100 检测 camera_config.json /
            # guidance_params_RDK S100（检测）.json 一律 depth_thresh_method="binary_inv"、
            # depth_morph_op="none"）。**不要**用算法 dataclass 的 otsu 默认：本页处理的
            # 典型场景是"目标比背景近"，otsu/binary 会把更亮(更远)的背景当白色前景 → 轮廓
            # 落在整幅背景上（正好是目标的互补，即用户反馈的"和旧程序相反"）；binary_inv 才把
            # 范围内更近(更暗)的目标当白色前景。（DEC-028）
            row("二值化", combo("thresh_method",
                               tuple((m, m) for m in THRESH_METHODS), "binary_inv",
                               tip="深度图二值化方法（决定目标/背景谁被当白色前景找轮廓）："
                                   "binary_inv 反转阈值——目标比背景近(更暗)时用；"
                                   "binary 固定阈值；otsu/triangle 自适应；adaptive_* 局部阈值。"
                                   "旧现场配置用 binary_inv"))
            row("形态学", combo("morph_op",
                               tuple((m, m) for m in MORPH_OPS), "none",
                               tip="二值化后的形态学处理：none 不处理（旧现场配置用）；"
                                   "close 先膨胀后腐蚀(补洞)；open 先腐蚀后膨胀(去毛刺)；"
                                   "erode/dilate 单步"))
            row("核大小", spin("morph_kernel_size", 1, 31, 3, step=2,
                              tip="形态学结构元边长（奇数）"))
            form.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setMinimumHeight(130)
            scroll.setWidget(inner)
            return scroll

        def _seg_value(self, key):
            w = self._seg_widgets[key]
            if isinstance(w, QComboBox):
                return w.currentData()
            return w.value()

        def _seg_depth_params(self):  # pragma: no cover - Qt
            """从面板收集深度分割参数（其余字段留给默认值）。"""
            base = DepthGuidanceParams()
            return DepthGuidanceParams(
                depth_min_mm=float(self._seg_value("depth_min_mm")),
                depth_max_mm=float(self._seg_value("depth_max_mm")),
                normalize_by_range=base.normalize_by_range,
                invalid_as_255=base.invalid_as_255,
                thresh_method=str(self._seg_value("thresh_method")),
                morph_op=str(self._seg_value("morph_op")),
                morph_kernel_size=int(self._seg_value("morph_kernel_size")),
            )

        def _build_scheme(self) -> "RunOnceScheme":  # pragma: no cover - Qt
            """把手上的设置 + 用户多边形/ROI 合成一份运行一次方案（= 要保存的实现逻辑）。"""
            return RunOnceScheme(
                source=str(self._seg_value("source")),
                op_region=str(self._seg_value("op_region")),
                roi=self._roi,
                polygon=[list(p) for p in self._polygon],
                depth_params=self._seg_depth_params().to_dict(),
                seg_filter_mode=str(self._seg_value("seg_filter_mode")),
                seg_filter_n=int(self._seg_value("seg_filter_n")),
                prompt_k=int(self._seg_value("prompt_k")),
                prompt_margin=float(self._seg_value("prompt_margin")),
            )

        def _load_scheme(self, scheme) -> None:  # pragma: no cover - Qt
            """把方案写回面板 + 多边形/ROI 叠加（供载入已保存检测时复原）。"""
            if scheme is None:
                return
            mapping = {
                "source": scheme.source, "op_region": scheme.op_region,
                "seg_filter_mode": scheme.seg_filter_mode,
                "seg_filter_n": scheme.seg_filter_n, "prompt_k": scheme.prompt_k,
                "prompt_margin": scheme.prompt_margin,
            }
            dp = scheme.depth_params or {}
            for key in ("depth_min_mm", "depth_max_mm", "thresh_method",
                        "morph_op", "morph_kernel_size"):
                if key in dp:
                    mapping[key] = dp[key]
            for key, val in mapping.items():
                w = self._seg_widgets.get(key)
                if w is None:
                    continue
                if isinstance(w, QComboBox):
                    idx = w.findData(val)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                else:
                    try:
                        w.setValue(val)
                    except (TypeError, ValueError):
                        pass
            self._set_roi(tuple(scheme.roi) if scheme.roi else None)
            self._set_polygon([list(p) for p in scheme.polygon])
            self._clear_seg_overlays()      # 上一轮的轮廓/交集叠加对新方案不再适用

        # ---------- 多边形 / ROI 状态 ----------
        def _set_polygon(self, pts) -> None:  # pragma: no cover - Qt
            """设置多边形（**原始帧坐标**）；画到预览上时按当前几何变换换算。"""
            self._polygon = [list(p) for p in (pts or [])]
            if len(self._polygon) >= 3:
                self.view_color.set_polygon_overlay(
                    [self._orig_to_preview_pt(p[0], p[1]) for p in self._polygon])
            else:
                self.view_color.clear_polygon_overlay()
            self._update_seg_hint()

        def _set_roi(self, roi) -> None:  # pragma: no cover - Qt
            """设置 ROI（**原始帧坐标**）；预览上画按当前几何变换换算。"""
            self._roi = tuple(roi) if roi else None
            disp = self._orig_to_preview_roi(self._roi)
            if disp:
                self.view_color.set_roi_overlay(*disp)
            else:
                self.view_color.clear_roi_overlay()
            self._update_seg_hint()

        def _on_polygon_drawn(self, pts) -> None:  # pragma: no cover - Qt
            self._set_polygon(
                [self._preview_to_orig_pt(p[0], p[1]) for p in (pts or [])])
            # 画完即退出绘制模式（与旧工程一致：画一次就停，避免误加多余点）
            self.btn_draw_poly.setChecked(False)
            self.view_color.set_annotation_mode(None)

        def _on_polygon_edited(self, pts) -> None:  # pragma: no cover - Qt
            self._set_polygon(
                [self._preview_to_orig_pt(p[0], p[1]) for p in (pts or [])])

        def _on_roi_drawn(self, x1, y1, x2, y2) -> None:  # pragma: no cover - Qt
            ox1, oy1 = self._preview_to_orig_pt(x1, y1)
            ox2, oy2 = self._preview_to_orig_pt(x2, y2)
            self._set_roi((min(ox1, ox2), min(oy1, oy2),
                           max(ox1, ox2), max(oy1, oy2)))
            self.btn_draw_roi.setChecked(False)
            self.view_color.set_annotation_mode(None)

        def _seg_mode(self, kind: str) -> None:  # pragma: no cover - Qt
            """在三选一（绘制/调整/画ROI）之间切换；按钮互斥。"""
            v = self.view_color
            if self.btn_draw_poly.isChecked() or self.btn_edit_poly.isChecked() \
                    or self.btn_draw_roi.isChecked():
                blocked = self._blocked_by_geometry()
                if blocked:
                    self.btn_draw_poly.setChecked(False)
                    self.btn_edit_poly.setChecked(False)
                    self.btn_draw_roi.setChecked(False)
                    v.set_annotation_mode(None)
                    v.set_edit_mode(False)
                    self.result.setText(blocked)
                    self._update_seg_hint()
                    return
            if kind == "polygon":
                on = self.btn_draw_poly.isChecked()
                self.btn_edit_poly.setChecked(False)
                self.btn_draw_roi.setChecked(False)
                v.set_edit_mode(False)
                v.set_annotation_mode("polygon" if on else None)
            elif kind == "edit":
                on = self.btn_edit_poly.isChecked()
                self.btn_draw_poly.setChecked(False)
                self.btn_draw_roi.setChecked(False)
                if not on:
                    v.set_annotation_mode(None)
                v.set_edit_mode(on)
            else:  # roi
                on = self.btn_draw_roi.isChecked()
                self.btn_draw_poly.setChecked(False)
                self.btn_edit_poly.setChecked(False)
                v.set_edit_mode(False)
                v.set_annotation_mode("roi" if on else None)
            self._update_seg_hint()

        def _seg_clear_all(self) -> None:  # pragma: no cover - Qt
            """清除：用户多边形 + 提示点 + 分割叠加框（轮廓/交集）。

            ROI 作为**运算区域输入**保留（要删 ROI：「调整多边形」选中后 Delete）。
            """
            self._set_polygon([])
            self._clear_seg_overlays()
            for b in (self.btn_draw_poly, self.btn_edit_poly, self.btn_draw_roi):
                b.setChecked(False)
            self.view_color.set_edit_mode(False)
            self.view_color.set_annotation_mode(None)
            self.result.setText(
                "已清除：用户多边形、提示点与分割叠加框（轮廓/交集）；ROI 作为运算区域保留")

        def _make_segmenter(self):  # pragma: no cover - Qt
            """按可用性给出智能分割后端：真机优先真实 EdgeSAM，离线栈用占位并如实标注。"""
            offline = getattr(self.ctx.stack, "profile", "") == "offline"
            return default_segmenter(offline=offline)

        def _clear_seg_overlays(self) -> None:  # pragma: no cover - Qt
            """清掉上次「深度分割/运行一次」的叠加（轮廓/交集/提示点）与缓存结果。

            用户多边形（品红）与 ROI（黄框）**不动** —— 那是输入，不是结果。
            """
            v = self.view_color
            v.clear_contours_overlay()
            v.clear_mark_overlay()
            v.clear_points_overlay()
            self._seg_result = None

        def _reset_seg_geometry(self) -> None:  # pragma: no cover - Qt
            """换通道时把多边形/ROI/叠加一并复位（不同通道分辨率不同，旧坐标会张冠李戴）。"""
            self._set_polygon([])
            self._set_roi(None)
            self._clear_seg_overlays()
            for b in (self.btn_draw_poly, self.btn_edit_poly, self.btn_draw_roi):
                b.setChecked(False)
            self.view_color.set_annotation_mode(None)
            self.view_color.set_edit_mode(False)
            self._update_seg_hint()

        def _preview_geometry(self) -> tuple[bool, bool, int]:
            """当前主图预览实际套用的几何变换 (水平镜像, 垂直镜像, 旋转角度)。

            「原图对照」勾选时预览就是原始帧，几何变换未生效 -> 返回恒等。
            """
            if getattr(self, "chk_raw", None) is not None and self.chk_raw.isChecked():
                return False, False, 0
            hflip = vflip = False
            entry = self._widgets.get("hflip")
            if entry:
                hflip = bool(entry[0].isChecked())
            entry = self._widgets.get("vflip")
            if entry:
                vflip = bool(entry[0].isChecked())
            rot = 0
            entry = self._widgets.get("rotate")
            if entry:
                try:
                    rot = int(entry[0].currentData() or 0) % 360
                except (TypeError, ValueError):
                    rot = 0
            return hflip, vflip, rot

        def _frame_size_wh(self):
            frame = self._frame
            data = getattr(getattr(frame, "color", None), "data", None)
            if data is None:
                data = getattr(getattr(frame, "depth", None), "data", None)
            if data is None or getattr(data, "ndim", 0) < 2:
                return None
            return int(data.shape[1]), int(data.shape[0])

        def _orig_to_preview_pt(self, x, y):
            """原始帧像素 -> 预览图像素（与 image_ops.apply_geometry 同序：先镜像后旋转）。"""
            size = self._frame_size_wh()
            hflip, vflip, rot = self._preview_geometry()
            if size is None or (not hflip and not vflip and not rot):
                return float(x), float(y)
            w, h = size
            x, y = float(x), float(y)
            if vflip:
                y = (h - 1) - y
            if hflip:
                x = (w - 1) - x
            if rot == 90:          # 顺时针 90
                x, y = (h - 1) - y, x
            elif rot == 180:
                x, y = (w - 1) - x, (h - 1) - y
            elif rot == 270:       # 逆时针 90
                x, y = y, (w - 1) - x
            return x, y

        def _preview_to_orig_pt(self, x, y):
            """预览图像素 -> 原始帧像素（_orig_to_preview_pt 的逆变换）。"""
            size = self._frame_size_wh()
            hflip, vflip, rot = self._preview_geometry()
            if size is None or (not hflip and not vflip and not rot):
                return float(x), float(y)
            w, h = size
            x, y = float(x), float(y)
            if rot == 90:
                x, y = y, (h - 1) - x
            elif rot == 180:
                x, y = (w - 1) - x, (h - 1) - y
            elif rot == 270:
                x, y = (w - 1) - y, x
            if hflip:
                x = (w - 1) - x
            if vflip:
                y = (h - 1) - y
            return x, y

        @staticmethod
        def _point_xy(p):
            """把 (x,y) / [[x,y]] / (2,1) 等形状统一取成 (x, y)；取不到返回 None。"""
            try:
                import numpy as _np
                flat = _np.asarray(p, dtype=float).reshape(-1)
            except Exception:  # noqa: BLE001
                return None
            if flat.size < 2:
                return None
            return float(flat[0]), float(flat[1])

        def _orig_to_preview_points(self, points) -> list:
            out = []
            if points is None:          # numpy 数组不能直接 `or`（真值不明确）
                return out
            for p in points:
                xy = self._point_xy(p)
                if xy is None:
                    continue
                out.append(self._orig_to_preview_pt(*xy))
            return out

        def _orig_to_preview_contours(self, contours) -> list:
            out = []
            if contours is None:
                return out
            for contour in contours:
                pts = self._orig_to_preview_points(contour)
                if pts:
                    out.append(pts)
            return out

        def _orig_to_preview_roi(self, roi):
            if not roi:
                return None
            x1, y1 = self._orig_to_preview_pt(roi[0], roi[1])
            x2, y2 = self._orig_to_preview_pt(roi[2], roi[3])
            return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))

        def _geo_transform_active(self) -> str:  # pragma: no cover - Qt
            """当前预览启用的**非恒等几何变换**说明（镜像/旋转），无则空串。"""
            hflip, vflip, rot = self._preview_geometry()
            bits = []
            if hflip:
                bits.append("水平镜像")
            if vflip:
                bits.append("垂直镜像")
            if rot:
                bits.append(f"旋转 {rot}°")
            return "、".join(bits)

        def _blocked_by_geometry(self) -> str | None:  # pragma: no cover - Qt
            """DEC-120：几何变换不再阻止绘制/分割。

            多边形/ROI 一律按**原始帧坐标**保存（分割与方案都在原始帧上跑），
            画在预览上的像素点进来时用逆变换换算回原始帧，叠加显示时再正变换回去。
            """
            return None

        def _update_seg_hint(self) -> None:  # pragma: no cover - Qt
            bits = []
            if self._roi:
                bits.append(f"ROI=({self._roi[0]},{self._roi[1]})-({self._roi[2]},{self._roi[3]})")
            else:
                bits.append("ROI=未设置")
            bits.append(f"多边形={len(self._polygon)} 点")
            mode = self.view_color.annotation_mode()
            if self.view_color.edit_mode():
                bits.append("模式=调整")
            elif mode:
                bits.append(f"模式={mode}")
            geo = self._geo_transform_active()
            if geo:
                bits.append(f"几何变换生效（{geo}）：坐标按原始帧记录、显示自动换算")
            self.seg_hint.setText("；".join(bits))

        # ---------- 深度分割 / 运行一次 ----------
        def _run_depth_seg(self) -> None:  # pragma: no cover - Qt
            """只跑深度分割：标出最终轮廓与轮廓内提示点（合成到运行一次的结果视图）。"""
            frame = self._frame
            if frame is None:
                self.result.setText("请先抓拍一帧，再运行深度分割")
                return
            depth = frame.depth.data if frame.depth is not None else None
            scale = getattr(frame, "depth_scale_m", None)
            if depth is None or not scale:
                self.result.setText("该帧缺深度图或 depth_scale，无法做深度分割")
                return
            blocked = self._blocked_by_geometry()
            if blocked:
                self.result.setText(blocked)
                return
            from application.algorithms.segmentation.depth_seg import run_depth_segmentation
            try:
                seg = run_depth_segmentation(
                    depth, float(scale), self._seg_depth_params(),
                    filter_mode=str(self._seg_value("seg_filter_mode")),
                    top_n=int(self._seg_value("seg_filter_n")),
                    k=int(self._seg_value("prompt_k")),
                    margin=float(self._seg_value("prompt_margin")),
                    roi=(self._roi if self._seg_value("op_region") == "roi" else None))
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"深度分割失败：{exc}")
                return
            from application.algorithms.segmentation.depth_seg import offset_points
            off = seg.get("roi_offset", (0, 0))
            contours = [self._shift_contour(c, off) for c in seg["contours"]]
            prompts = [offset_points(g, off) for g in seg["prompts"]]
            # 结果在原图坐标算；显示到预览上按当前几何变换换算（DEC-120）
            self.view_color.set_contours_overlay(
                self._orig_to_preview_contours(contours))
            self.view_color.clear_mark_overlay()
            self.view_color.set_points_overlay(
                self._orig_to_preview_points([p for g in prompts for p in g]))
            self.result.setText("\n".join([
                "深度分割完成（只标轮廓与提示点，未做智能分割）",
                f"  参数过滤后轮廓数={seg['n_contours']}；最终保留={len(contours)}",
                f"  提示点分组={len(prompts)}；点数={sum(len(g) for g in prompts)}",
                f"  运算区域={'ROI ' + str(self._roi) if off != (0, 0) else '整张图'}",
                "  叠加：轮廓=绿、提示点=黄十字；面积一律像素。",
            ]))

        def _run_once(self) -> None:  # pragma: no cover - Qt
            """运行一次：在**当前帧**上执行完整真实逻辑（深度分割→智能分割→交集）。"""
            frame = self._frame
            if frame is None:
                self.result.setText("请先抓拍一帧，再运行一次")
                return
            if len(self._polygon) < 3:
                self.result.setText("请先在彩色主图上「绘制多边形」（≥3 点），再运行一次")
                return
            blocked = self._blocked_by_geometry()
            if blocked:
                self.result.setText(blocked)
                return
            segmenter, note = self._make_segmenter()
            if segmenter is None:
                self.result.setText(
                    "智能分割不可用，无法运行一次：\n  " + note +
                    "\n（离线栈会用占位分割器；真机需安装 onnxruntime 并放好 EdgeSAM 模型）")
                return
            try:
                scheme = self._build_scheme()
                res = run_scheme(scheme, frame, segmenter)
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"运行一次失败：{exc}")
                return
            self._seg_result = res
            if res.error:
                self.result.setText(f"运行一次未完成：{res.error}")
                return
            # 叠加：轮廓=绿、交集=红、提示点=黄十字（与旧工程一致）
            # 结果在原图坐标算；显示时按当前几何变换换算（DEC-120）
            self.view_color.set_contours_overlay(
                self._orig_to_preview_contours(res.depth_contours))
            self.view_color.set_mark_overlay(
                self._orig_to_preview_contours(res.intersection_contours))
            self.view_color.set_points_overlay(
                self._orig_to_preview_points(
                    [p for g in res.prompts for p in g]))
            lines = [
                "运行一次完成（真实算法路径：深度分割 → 智能分割 → 交集）",
                f"  处理图源={res.source}（{res.source_note}）",
                f"  分割后端={res.segmenter_name}（{note}）",
                f"  深度分割：轮廓 {res.n_contours} → 最终 {len(res.depth_contours)}；"
                f"提示点 {sum(len(g) for g in res.prompts)}",
                f"  智能分割多边形={len(res.smart_polygons)}",
                f"  交集：{'命中' if res.hit else '未命中'}；面积={res.area_px} px²"
                f"（长度/面积一律像素）",
                "  叠加：轮廓=绿、交集=红、提示点=黄十字。",
            ]
            for n in res.notes:
                lines.append(f"  提示：{n}")
            self.result.setText("\n".join(lines))

        def _shift_contour(self, contour, offset):  # pragma: no cover - Qt
            import numpy as _np
            ox, oy = int(offset[0]), int(offset[1])
            arr = _np.asarray(contour, dtype=_np.int32).reshape(-1, 1, 2).copy()
            arr[:, 0, 0] += ox
            arr[:, 0, 1] += oy
            return arr

        # =================================================================
        #  找圆调试（旧工程 ROBOT 引导调试页并入；与上面两页签并集）
        # =================================================================
        # ---------- 棱线测量（DEC-144：DEC-143 脚本的界面化，参数全可调） ----------
        def _build_edge_panel(self):  # pragma: no cover - Qt
            """平面两棱线间距面板：平面 RANSAC / 扫描 / 深度限定 / 量程参数。"""
            inner = QWidget()
            form = QVBoxLayout(inner)
            form.setContentsMargins(2, 2, 2, 2); form.setSpacing(6)
            self._edge_widgets: dict = {}

            def row(label, widget):
                h = QWidget(); hl = QHBoxLayout(h)
                hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(6)
                lab = QLabel(label); lab.setObjectName("SubLabel")
                hl.addWidget(lab); hl.addWidget(widget, 1)
                form.addWidget(h)

            def combo(key, choices, default, tip=""):
                w = QComboBox()
                for code, text in choices:
                    w.addItem(str(text), code)
                idx = w.findData(default)
                w.setCurrentIndex(idx if idx >= 0 else 0)
                w.setToolTip(tip)
                self._edge_widgets[key] = w
                return w

            def spin(key, lo, hi, val, step=1, tip=""):
                w = QSpinBox(); w.setRange(lo, hi); w.setSingleStep(step)
                w.setValue(val); w.setToolTip(tip)
                self._edge_widgets[key] = w
                return w

            def dspin(key, lo, hi, val, step=1.0, dec=1, tip=""):
                w = QDoubleSpinBox(); w.setRange(lo, hi); w.setSingleStep(step)
                w.setDecimals(dec); w.setValue(val); w.setToolTip(tip)
                self._edge_widgets[key] = w
                return w

            form.addWidget(SectionTitle("平面与扫描参数"))
            row("平面容差 mm", dspin("tol_mm", 1.0, 100.0, 8.0,
                                   tip="逆深度 RANSAC 最大平面的内点深度容差"))
            row("开运算核 px", spin("open_kernel", 3, 51, 5, step=2,
                                  tip="掩膜去毛刺的椭圆核（奇数；越小越少削棱边）"))
            row("扫描方向", combo("scan",
                                 (("auto", "自动（按长轴）"),
                                  ("row", "按行扫左右"),
                                  ("col", "按列扫上下")), "auto",
                                 tip="画面整体旋转 90° 时可强制按列扫描"))
            row("最小跨度 px", spin("min_span", 10, 2000, 60,
                                  tip="扫描线两端跨度小于该值的行忽略"))
            row("直线内点 mm", dspin("line_thr_mm", 0.5, 50.0, 3.0,
                                   tip="两侧棱边 3D 直线拟合的内点容差"))
            form.addWidget(SectionTitle("深度限定与量程"))
            row("深度下限 mm", dspin("depth_min_mm", 0.0, 65535.0, 0.0, dec=0,
                                    tip="只用 ≥该深度(mm) 的有效点；0 = 不限"))
            row("深度上限 mm", dspin("depth_max_mm", 0.0, 65535.0, 0.0, dec=0,
                                    tip="只用 ≤该深度(mm) 的有效点；0 = 不限"))
            row("量程下限 mm", dspin("min_width_mm", 1.0, 5000.0, 5.0,
                                    tip="测得宽度低于该值视为无效"))
            row("量程上限 mm", dspin("max_width_mm", 10.0, 10000.0, 3000.0,
                                    tip="测得宽度高于该值视为无效"))
            form.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setMinimumHeight(130)
            scroll.setWidget(inner)
            return scroll

        def _edge_value(self, key):
            w = self._edge_widgets[key]
            if isinstance(w, QComboBox):
                return w.currentData()
            return w.value()

        def _clear_edge_overlay(self) -> None:  # pragma: no cover - Qt
            v = self.view_color
            v.clear_contours_overlay()
            v.clear_mark_overlay()
            v.clear_points_overlay()
            v.clear_circle_overlay()
            self.result.setText("已清除棱线测量叠加")

        def _run_edge_measure(self) -> None:  # pragma: no cover - Qt
            """用当前帧深度测「最大平面」两条棱线间距，并叠加左右棱边点。"""
            from application.algorithms.guidance.plane_width import (
                measure_plane_width,
            )
            frame = self._frame
            if frame is None:
                self.result.setText("请先「单次抓拍」或载入本地图片（四件套），"
                                    "再做棱线测量")
                return
            depth = getattr(getattr(frame, "depth", None), "data", None)
            scale = getattr(frame, "depth_scale_m", None)
            intr_obj = getattr(frame, "intrinsics", None)
            if depth is None or not scale:
                self.result.setText("当前帧缺原始深度或 depth_scale，不能做棱线测量")
                return
            intr = None
            if intr_obj is not None:
                try:
                    intr = {"fx": float(intr_obj.fx), "fy": float(intr_obj.fy),
                            "cx": float(intr_obj.cx), "cy": float(intr_obj.cy)}
                except Exception:  # noqa: BLE001
                    intr = None
            if intr is None:
                self.result.setText("当前帧缺内参（fx/fy/cx/cy），不能把像素换算成"
                                    "毫米；请载入带 _depth.json 的四件套或连相机抓拍")
                return
            info = measure_plane_width(
                depth, float(scale), intr,
                tol_mm=float(self._edge_value("tol_mm")),
                open_kernel=int(self._edge_value("open_kernel")),
                line_thr_mm=float(self._edge_value("line_thr_mm")),
                min_span=int(self._edge_value("min_span")),
                scan=str(self._edge_value("scan")),
                depth_min_mm=float(self._edge_value("depth_min_mm")),
                depth_max_mm=float(self._edge_value("depth_max_mm")),
                min_width_mm=float(self._edge_value("min_width_mm")),
                max_width_mm=float(self._edge_value("max_width_mm")))
            if not info.get("ok"):
                self.view_color.clear_points_overlay()
                tip = info.get("reason", "测量失败")
                if "width_mm" in info:
                    tip += f"（测得 {float(info['width_mm']):.1f}mm）"
                self.result.setText(f"棱线测量失败：{tip}")
                return
            # 主图叠加：左棱红、右棱绿（原图坐标 → 当前显示坐标）
            pts = []
            for pl, pr in info.get("pairs") or []:
                for p, col in ((pl, (235, 64, 52)), (pr, (46, 160, 67))):
                    z = max(float(p[2]), 1.0)
                    u = intr["fx"] * float(p[0]) / z + intr["cx"]
                    v = intr["fy"] * float(p[1]) / z + intr["cy"]
                    pu, pv = self._orig_to_preview_pt(u, v)
                    pts.append((pu, pv, col))
            v = self.view_color
            v.clear_contours_overlay()
            v.clear_mark_overlay()
            v.clear_circle_overlay()
            v.set_points_overlay(pts)
            lines = [
                "棱线测量完成",
                f"  两条棱线间距：{info['width_mm']:.1f} mm"
                f"（均值 {info['width_mean_mm']:.1f}，IQR {info['width_iqr_mm']:.1f}）",
                f"  扫描 {info['scan_lines']} 条（{info['scan_mode']}）；"
                f"两侧直线夹角 {info.get('line_angle_deg', 0.0):.2f}°",
                f"  平面倾角（法线 vs 光轴）{info['tilt_deg']:.1f}°；"
                f"面平均深度 {info['face_depth_mm']:.0f} mm",
                "  说明：单位 mm；红/绿点=两侧棱边；只调试，不驱动机械臂、不改配方。",
            ]
            self.result.setText("\n".join(lines))

        def _build_circle_panel(self):  # pragma: no cover - Qt
            """深度引导 + 找圆参数面板（样式与分割面板一致，装在滚动区）。

            **初始值 = 旧现场配置口径**（DEC-028/030：二值化 binary_inv、形态学 none、
            找圆最小圆 5–500px …）。要用现场 `guidance.yaml` 里实际调好的值，点
            「载入引导配置」。
            """
            inner = QWidget()
            form = QVBoxLayout(inner)
            form.setContentsMargins(2, 2, 2, 2); form.setSpacing(6)

            def row(label, widget):
                h = QWidget(); hl = QHBoxLayout(h)
                hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(6)
                lab = QLabel(label); lab.setObjectName("SubLabel")
                hl.addWidget(lab); hl.addWidget(widget, 1)
                form.addWidget(h)

            def combo(key, choices, default, tip=""):
                w = QComboBox()
                for code, text in choices:
                    w.addItem(str(text), code)
                idx = w.findData(default)
                w.setCurrentIndex(idx if idx >= 0 else 0)
                w.setToolTip(tip)
                self._circle_widgets[key] = w
                return w

            def spin(key, lo, hi, val, step=1, tip=""):
                w = QSpinBox(); w.setRange(lo, hi); w.setSingleStep(step)
                w.setValue(val); w.setToolTip(tip)
                self._circle_widgets[key] = w
                return w

            def dspin(key, lo, hi, val, step=1.0, dec=1, tip=""):
                w = QDoubleSpinBox(); w.setRange(lo, hi); w.setSingleStep(step)
                w.setDecimals(dec); w.setValue(val); w.setToolTip(tip)
                self._circle_widgets[key] = w
                return w

            def chk(key, checked, tip=""):
                w = QCheckBox(); w.setChecked(checked); w.setToolTip(tip)
                self._circle_widgets[key] = w
                return w

            form.addWidget(SectionTitle("调试画面"))
            stage_combo = combo("stage", STAGE_CHOICES, STAGE_ORIG,
                                "深度图窗口显示哪张中间图；原彩色+矩形时深度窗口显示原始深度伪彩")
            stage_combo.currentIndexChanged.connect(
                lambda *_a: self._render_circle_stage())
            row("阶段图", stage_combo)

            form.addWidget(SectionTitle("深度引导参数（步骤1+2）"))
            row("深度下限 mm", dspin("depth_min_mm", 0.0, 65535.0, 200.0, dec=0,
                                    tip="小于该深度(毫米)视为远/背景（自动模式下不生效）"))
            row("深度上限 mm", dspin("depth_max_mm", 0.0, 65535.0, 3000.0, dec=0,
                                    tip="大于该深度(毫米)视为近/无效（自动模式下不生效）"))
            row("深度范围", combo("depth_range_mode",
                                 (("auto", "自动（最小深度 / 最大平面）"),
                                  ("manual", "手动上下限")), "auto",
                                 tip="DEC-133：自动=下限取本帧最小有效深度−偏移、"
                                     "上限取最大平面平均深度−偏移；手动=用上面的上下限"))
            row("自动下限偏移", dspin("auto_min_offset_mm", 0.0, 500.0, 10.0,
                                    dec=1, tip="自动模式：下限 = 本帧最小有效深度 − 该值（mm）"))
            row("自动上限偏移", dspin("auto_plane_offset_mm", 0.0, 500.0, 20.0,
                                    dec=1, tip="自动模式：上限 = 最大平面平均深度 − 该值（mm）"))
            row("平面容差 mm", dspin("auto_plane_tol_mm", 0.5, 100.0, 8.0,
                                   dec=1, tip="自动模式：最大平面 RANSAC 内点的深度容差（mm）"))
            row("二值化", combo("thresh_method",
                               tuple((m, m) for m in THRESH_METHODS), "binary_inv",
                               tip="目标比背景近(更暗)时用 binary_inv（旧现场配置）；"
                                   "otsu/binary 会把更亮的背景当白色前景"))
            row("阈值", dspin("thresh_value", 0.0, 255.0, 127.0, dec=0,
                             tip="binary / binary_inv 的固定阈值"))
            row("形态学", combo("morph_op",
                               tuple((m, m) for m in MORPH_OPS), "none",
                               tip="二值化后的形态学处理（旧现场配置不处理）"))
            row("核大小", spin("morph_kernel_size", 1, 31, 3, step=2,
                              tip="形态学结构元边长（奇数）"))
            row("轮廓面积下限", dspin("min_contour_area", 0.0, 9999999.0, 0.0,
                                    dec=0, step=100.0, tip="像素面积；0 = 不限制"))
            row("轮廓面积上限", dspin("max_contour_area", 0.0, 9999999.0, 9999999.0,
                                    dec=0, step=1000.0, tip="像素面积；超大 = 不限制"))
            row("中心方式", combo("center_mode",
                                 tuple((m, m) for m in CENTER_MODES), "bbox",
                                 tip="正外接矩形中心取轮廓质心还是外接框中心"))
            row("矩形模式", combo("rect_mode",
                                 (("auto", "自动贴合"), ("fixed", "固定正方形")),
                                 "auto",
                                 tip="DEC-131：自动贴合=按轮廓外接框生成矩形（推荐）；"
                                     "固定正方形=旧算法（中心点+固定边长）"))
            row("贴合方式", combo("rect_fit",
                                 (("square", "正方形（取较大边）"),
                                  ("bbox", "按外接框（可长方形）")), "square",
                                 tip="自动贴合时：正方形=边长取外接框较大边；"
                                     "按外接框=宽高各自贴合（矩形可为长方形）"))
            row("尺寸系数", dspin("rect_scale", 0.1, 5.0, 1.0, step=0.05,
                                 dec=2,
                                 tip="自动贴合：外接框尺寸乘以该系数（>1 放大留余量）"))
            row("外扩边距 px", spin("rect_pad_px", 0, 2000, 20, step=5,
                                  tip="自动贴合：矩形四周额外外扩的像素"))
            row("最小边长 px", spin("rect_min_side_px", 0, 10000, 0, step=10,
                                  tip="自动贴合：边长下限；0 = 不限制"))
            row("最大边长 px", spin("rect_max_side_px", 0, 10000, 0, step=10,
                                  tip="自动贴合：边长上限；0 = 不限制"))
            row("合并间距 px", spin("rect_merge_gap_px", 0, 2000, 0, step=5,
                                  tip="同一物体的碎块：外接框间距 ≤ 该值即合并成一个"
                                      "矩形；0 = 不合并"))
            row("矩形边长 px", dspin("rect_side_length", 1.0, 5000.0, 100.0,
                                    dec=0, tip="仅「固定正方形」模式生效（旧算法）"))
            row("均值深度", chk("avg_depth_enable", False,
                              "开启后取圆心圆形区域内有效深度平均值（否则取圆心单点，"
                              "无效时螺旋搜索兜底）"))
            row("均值直径 px", dspin("avg_depth_diameter", 1.0, 2000.0, 20.0,
                                    dec=0, tip="圆心圆形区域直径"))
            row("深度有效下限 mm", dspin("depth_valid_min_mm", 0.0, 1000.0, 1.0,
                                         dec=1, tip="低于该值视为无效深度"))

            form.addWidget(SectionTitle("找圆参数（cp38 detect_circle_imp）"))
            row("找圆类型", combo("circle_type", (
                ("最大圆", "最大圆"), ("最小圆", "最小圆"), ("所有圆", "所有圆"),
                ("最大圆和最小圆", "最大圆和最小圆")), "最小圆",
                tip="每个矩形内保留哪个圆（旧现场配置：最小圆）"))
            row("半径下限 px", dspin("min_radius", 0.0, 20000.0, 5.0, dec=1,
                                   tip="找圆半径下限（旧现场配置 5px）"))
            row("半径上限 px", dspin("max_radius", 0.0, 20000.0, 500.0, dec=1,
                                   tip="找圆半径上限（旧现场配置 500px）"))
            row("覆盖率下限", dspin("min_cover_ratio", 0.0, 1.0, 0.3, step=0.05,
                                  dec=2, tip="圆弧覆盖比例下限"))
            row("覆盖率上限", dspin("max_cover_ratio", 0.0, 1.0, 1.0, step=0.05,
                                  dec=2, tip="圆弧覆盖比例上限"))
            row("梯度阈值", dspin("grad_thresh", 0.0, 255.0, 50.0, dec=1,
                                tip="边缘梯度阈值（旧现场配置 50）"))
            row("锚点阈值", dspin("anchor_thresh", 0.0, 255.0, 8.0, dec=1,
                                tip="弧段锚点阈值"))
            row("弧拟合误差", dspin("arc_fit_error", 0.0, 100.0, 1.0, dec=2,
                                  tip="弧段拟合误差上限（旧现场配置 1.0）"))
            row("圆拟合误差", dspin("circle_fit_error", 0.0, 100.0, 2.0, dec=2,
                                  tip="整圆拟合误差上限（旧现场配置 2.0）"))
            row("弧点数下限", spin("min_arc_points", 0, 100000, 0,
                                  tip="组成圆的最少弧点；0 = 不限制"))
            row("弧点数上限", spin("max_arc_points", 0, 1000000, 999999,
                                  tip="组成圆的最多弧点"))
            row("缩放系数", dspin("resize_factor", 0.05, 1.0, 1.0, step=0.05,
                                dec=2, tip="检测前缩放（<1 加速；坐标已换回全图）"))

            form.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setMinimumHeight(130)
            scroll.setWidget(inner)
            return scroll

        # ---------- 找圆调试：参数收集 ----------
        def _circle_value(self, key):
            w = self._circle_widgets[key]
            if isinstance(w, QCheckBox):
                return w.isChecked()
            if isinstance(w, QComboBox):
                return w.currentData()
            return w.value()

        def _circle_depth_params(self) -> "DepthGuidanceParams":  # pragma: no cover - Qt
            base = DepthGuidanceParams()
            return DepthGuidanceParams(
                depth_min_mm=float(self._circle_value("depth_min_mm")),
                depth_max_mm=float(self._circle_value("depth_max_mm")),
                depth_range_mode=str(self._circle_value("depth_range_mode")),
                auto_min_offset_mm=float(self._circle_value("auto_min_offset_mm")),
                auto_plane_offset_mm=float(self._circle_value("auto_plane_offset_mm")),
                auto_plane_tol_mm=float(self._circle_value("auto_plane_tol_mm")),
                normalize_by_range=base.normalize_by_range,
                invalid_as_255=base.invalid_as_255,
                invalid_raw_threshold=base.invalid_raw_threshold,
                thresh_method=str(self._circle_value("thresh_method")),
                thresh_value=float(self._circle_value("thresh_value")),
                adaptive_block_size=base.adaptive_block_size,
                adaptive_c=base.adaptive_c,
                morph_op=str(self._circle_value("morph_op")),
                morph_kernel_size=int(self._circle_value("morph_kernel_size")),
                min_contour_area=float(self._circle_value("min_contour_area")),
                max_contour_area=float(self._circle_value("max_contour_area")),
                center_mode=str(self._circle_value("center_mode")),
                rect_mode=str(self._circle_value("rect_mode")),
                rect_fit=str(self._circle_value("rect_fit")),
                rect_scale=float(self._circle_value("rect_scale")),
                rect_pad_px=float(self._circle_value("rect_pad_px")),
                rect_min_side_px=float(self._circle_value("rect_min_side_px")),
                rect_max_side_px=float(self._circle_value("rect_max_side_px")),
                rect_merge_gap_px=float(self._circle_value("rect_merge_gap_px")),
                rect_side_length=float(self._circle_value("rect_side_length")),
                avg_depth_enable=bool(self._circle_value("avg_depth_enable")),
                avg_depth_diameter=float(self._circle_value("avg_depth_diameter")),
                depth_valid_min_mm=float(self._circle_value("depth_valid_min_mm")))

        def _circle_params(self) -> "CircleParams":  # pragma: no cover - Qt
            return CircleParams(
                circle_type=str(self._circle_value("circle_type")),
                min_radius=float(self._circle_value("min_radius")),
                max_radius=float(self._circle_value("max_radius")),
                min_cover_ratio=float(self._circle_value("min_cover_ratio")),
                max_cover_ratio=float(self._circle_value("max_cover_ratio")),
                grad_thresh=float(self._circle_value("grad_thresh")),
                anchor_thresh=float(self._circle_value("anchor_thresh")),
                arc_fit_error=float(self._circle_value("arc_fit_error")),
                circle_fit_error=float(self._circle_value("circle_fit_error")),
                min_arc_points=int(self._circle_value("min_arc_points")),
                max_arc_points=int(self._circle_value("max_arc_points")),
                resize_factor=float(self._circle_value("resize_factor")))

        def _circle_offsets(self):
            """粗/精偏移映射与工具中心点：优先用引导配置（现场轴映射），否则默认。"""
            cfg = self._guidance_cfg()
            return getattr(cfg, "offsets", None)

        # ---------- 找圆调试：面板参数快照 / 复原（DEC-047） ----------
        def _active_right_tab_key(self) -> str:  # pragma: no cover - Qt
            w = self.right_tabs.currentWidget()
            if w is self.circle:
                return "circle"
            if w is self.seg:
                return "seg"
            return "algo"

        def _circle_debug_snapshot(self) -> dict:  # pragma: no cover - Qt
            """找圆调试面板的全部**参数**（写进视觉检测 ``params["circle_debug"]``）。

            只存参数，不存这一帧算出的矩形/圆/阶段图 —— 那些是逐帧运算结果，
            载回后必须在当前帧上重新「深度检测 / 运行一次找圆调试」。
            """
            return {
                "schema_version": 1,
                "depth": self._circle_depth_params().to_dict(),
                "circle": asdict(self._circle_params()),
                "stage": str(self._circle_value("stage")),
            }

        def _apply_circle_widgets(self, mapping: dict) -> None:  # pragma: no cover - Qt
            """把 key→值 写回找圆调试面板控件（认得的控件才写，坏值忽略）。"""
            for key, val in mapping.items():
                w = self._circle_widgets.get(key)
                if w is None:
                    continue
                if isinstance(w, QComboBox):
                    idx = w.findData(val)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                elif isinstance(w, QCheckBox):
                    w.setChecked(bool(val))
                else:
                    try:
                        w.setValue(val)
                    except (TypeError, ValueError):
                        pass

        def _apply_circle_debug_snapshot(self, snap) -> list[str]:  # pragma: no cover - Qt
            """把检测里保存的找圆调试参数复原到面板；返回恢复内容的说明行。

            宽容不等于猜：只写面板上真实存在的控件键；缺键/坏值一律跳过。
            参数变了，上一帧算出的叠加与阶段图缓存不再代表这组参数，一并清掉。
            """
            if not isinstance(snap, dict):
                return []
            lines: list[str] = []
            depth = snap.get("depth")
            if isinstance(depth, dict) and depth:
                dp = DepthGuidanceParams.from_dict(depth)
                self._apply_circle_widgets({
                    key: getattr(dp, key)
                    for key in self._circle_widgets
                    if key in DepthGuidanceParams.__dataclass_fields__
                })
                lines.append(
                    f"深度引导参数：{self._circle_value('depth_min_mm'):g}–"
                    f"{self._circle_value('depth_max_mm'):g}mm，"
                    f"二值化={self._circle_value('thresh_method')}，"
                    f"形态学={self._circle_value('morph_op')}")
            circle = snap.get("circle")
            if isinstance(circle, dict) and circle:
                known = set(CircleParams.__dataclass_fields__)
                self._apply_circle_widgets({
                    key: val for key, val in circle.items()
                    if key in known and key in self._circle_widgets
                })
                lines.append(
                    f"找圆参数：{self._circle_value('circle_type')}，半径 "
                    f"{self._circle_value('min_radius'):g}–"
                    f"{self._circle_value('max_radius'):g}px")
            stage = snap.get("stage")
            if isinstance(stage, str):
                self._apply_circle_widgets({"stage": stage})
            self._clear_circle_debug_overlays()
            self._update_circle_hint()
            return lines

        def _guidance_service(self):
            stack = getattr(self.ctx, "stack", None)
            return getattr(getattr(stack, "motion", None), "guidance", None)

        def _guidance_cfg(self):
            store = getattr(self._guidance_service(), "config_store", None)
            return getattr(store, "config", None)

        # ---------- 找圆调试：动作 ----------
        def _circle_frame_ready(self) -> bool:  # pragma: no cover - Qt
            if self._frame is None:
                self.result.setText("请先「单次抓拍」一帧，再做找圆调试（调试只用这一帧，"
                                    "不会自动抓图）")
                return False
            blocked = self._blocked_by_geometry()
            if blocked:
                self.result.setText(blocked)
                return False
            return True

        def _run_circle_depth(self) -> None:  # pragma: no cover - Qt
            """只跑深度引导（步骤1+2）：保留轮廓 + 正外接矩形。"""
            if not self._circle_frame_ready():
                return
            try:
                stage = depth_guidance_stage(self._frame, self._circle_depth_params())
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"深度引导失败：{exc}")
                return
            self._circle_rects = list(stage.rects)
            self._circle_hits = []
            self._circle_contours = list(stage.contours)
            self._circle_stage_images = dict(stage.stages)
            self._draw_circle_overlays()
            self._render_circle_stage()
            self._update_circle_hint()
            lines = [
                "深度引导完成（步骤1+2：切分→归一化→二值化→形态学→轮廓过滤→正外接矩形）",
                f"  过滤后轮廓={stage.n_contours}；正外接矩形={len(stage.rects)}",
            ]
            if stage.rects:
                lines.append("  下一步：点「找圆」，对每个矩形逐个找圆")
            else:
                lines.append("  没找到候选目标：调「深度下限/上限」「二值化」「形态学」"
                             "「轮廓面积」后重试；可切换「调试画面」逐阶段排查")
            lines.append("  叠加：轮廓=绿、矩形=红、矩形中心=黄十字")
            self.result.setText("\n".join(lines))

        def _run_circle_hit(self) -> None:  # pragma: no cover - Qt
            """对当前矩形逐个找圆（没有矩形时先自动深度检测）。"""
            if not self._circle_frame_ready():
                return
            if not self._circle_rects:
                self._run_circle_depth()
                if not self._circle_rects:
                    return          # 深度阶段已把原因写进结果
            frame = self._frame
            color = getattr(getattr(frame, "color", None), "data", None)
            image = color if color is not None else self._circle_stage_images.get(
                "contour_image")
            host = getattr(self.ctx.stack, "host_proxy", None)
            try:
                hits, errors = find_circles_in_rects(
                    image, self._circle_rects, self._circle_params(), host)
            except Exception as exc:  # noqa: BLE001 - 宿主缺失/无图: 如实报
                self.result.setText(f"找圆不可用：{exc}")
                return
            self._circle_hits = list(hits)
            self._draw_circle_overlays()
            self._update_circle_hint()
            lines = ["找圆完成（每个正外接矩形经 cp38 宿主 detect_circle_imp）",
                     f"  矩形={len(self._circle_rects)}；命中圆={len(hits)}"]
            for i, rect in enumerate(self._circle_rects):
                mine = [h for h in hits if h.rect_index == int(rect.get("idx", i))]
                if mine:
                    desc = "；".join(f"({h.cx:.0f},{h.cy:.0f}) r={h.radius:.1f}"
                                    for h in mine)
                    lines.append(f"  矩形#{i} ({rect['x1']},{rect['y1']}): {desc}")
                else:
                    lines.append(f"  矩形#{i} ({rect['x1']},{rect['y1']}): 未命中")
            for e in errors:
                lines.append(f"  异常：{e}")
            if hits:
                lines.append("  叠加：圆=红圈、圆心=黄十字、选中圆(第一个)=青圈")
                lines.append("  下一步：「运行一次找圆调试」换算 XYZ 与粗/精偏移")
            else:
                lines.append("  调「找圆参数」（半径范围/覆盖率/梯度阈值/拟合误差）后重试")
            self.result.setText("\n".join(lines))

        def _run_circle_full(self) -> None:  # pragma: no cover - Qt
            """完整链路：深度引导 → 找圆 → 选中圆 XYZ → 粗/精偏移。"""
            if not self._circle_frame_ready():
                return
            res = run_circle_debug(
                self._frame, depth_params=self._circle_depth_params(),
                circle_params=self._circle_params(), offsets=self._circle_offsets(),
                host_proxy=getattr(self.ctx.stack, "host_proxy", None))
            self._circle_result = res
            self._circle_rects = list(res.rects)
            self._circle_hits = list(res.hits)
            self._circle_contours = list(res.contours)
            self._circle_stage_images = dict(res.stages)
            self._draw_circle_overlays()
            self._render_circle_stage()
            self._update_circle_hint()
            self.result.setText(self._circle_result_text(res))

        def _circle_result_text(self, res: "CircleDebugResult") -> str:  # pragma: no cover - Qt
            lines = [f"找圆调试结果（阶段={res.stage}）"]
            if res.auto_range is not None:      # DEC-133：自动深度范围的来龙去脉
                ar = res.auto_range
                if ar.get("ok"):
                    note = "（上限已抬到下限之上）" if ar.get("clamped") else ""
                    lines.append(
                        f"  深度范围（自动）：{ar['min_mm']:g}–{ar['max_mm']:g} mm"
                        f"（最小深度 {ar['raw_min_mm']:g}−{ar['min_offset_mm']:g}；"
                        f"最大平面平均 {ar['plane_mean_mm']:g}−{ar['plane_offset_mm']:g}，"
                        f"平面占比 {ar['plane_ratio'] * 100:.1f}%）{note}")
                else:
                    lines.append(
                        f"  深度范围（自动）失败：{ar.get('reason', '未知原因')}；"
                        f"沿用手动上下限 {self._circle_value('depth_min_mm'):g}–"
                        f"{self._circle_value('depth_max_mm'):g} mm")
            lines.append(f"  深度引导：保留轮廓 {res.n_contours}；正外接矩形 {len(res.rects)}")
            if res.hits:
                lines.append(f"  找圆：命中 {len(res.hits)} 个圆")
                for h in res.hits:
                    _dia = float(getattr(h, "diameter_mm", 0.0) or 0.0)
                    lines.append(f"    矩形#{h.rect_index} 圆心 ({h.cx:.1f}, {h.cy:.1f}) "
                                 f"半径 {h.radius:.1f}px"
                                 + (f"，直径≈{_dia:.1f}mm" if _dia > 0 else ""))
            for e in res.rect_errors:
                lines.append(f"  异常：{e}")
            if res.selected is not None:
                s = res.selected
                _sdia = float(getattr(s, "diameter_mm", 0.0) or 0.0)
                lines.append(f"  选中圆：矩形#{s.rect_index} ({s.cx:.1f}, {s.cy:.1f}) "
                             f"r={s.radius:.1f}px"
                             + (f"，直径≈{_sdia:.1f}mm" if _sdia > 0 else ""))
            if res.cam_xyz is not None:
                x, y, z = res.cam_xyz
                lines.append(f"  相机系 XYZ=({x:.2f}, {y:.2f}, {z:.1f}) mm"
                             f"（深度={res.used_depth_mm:.1f}mm）")
            if res.coarse is not None and res.fine is not None:
                lines.append(f"  粗定位 (x,z)=({res.coarse[0]:.2f}, {res.coarse[1]:.2f}) mm；"
                             f"精定位 (x,z)=({res.fine[0]:.2f}, {res.fine[1]:.2f}) mm")
            if not res.ok:
                lines.append(f"  未完成：{res.message}")
            else:
                lines.append(f"  {res.message}")
            lines.append("  说明：单位 mm；像素指标不标 mm；只调试、不驱动机械臂、不改配方。")
            return "\n".join(lines)

        def _load_circle_from_config(self) -> None:  # pragma: no cover - Qt
            """把 guidance.yaml 的深度/找圆参数载入面板（轴映射随「运行」自动用配置）。"""
            cfg = self._guidance_cfg()
            if cfg is None:
                self.result.setText("当前栈未接引导配置（guidance.yaml），无法载入")
                return
            depth = cfg.depth
            mapping = {
                "depth_min_mm": depth.depth_min_mm, "depth_max_mm": depth.depth_max_mm,
                "depth_range_mode": depth.depth_range_mode,
                "auto_min_offset_mm": depth.auto_min_offset_mm,
                "auto_plane_offset_mm": depth.auto_plane_offset_mm,
                "auto_plane_tol_mm": depth.auto_plane_tol_mm,
                "thresh_method": depth.thresh_method, "thresh_value": depth.thresh_value,
                "morph_op": depth.morph_op, "morph_kernel_size": depth.morph_kernel_size,
                "min_contour_area": depth.min_contour_area,
                "max_contour_area": depth.max_contour_area,
                "center_mode": depth.center_mode,
                "rect_mode": depth.rect_mode,
                "rect_fit": depth.rect_fit,
                "rect_scale": depth.rect_scale,
                "rect_pad_px": depth.rect_pad_px,
                "rect_min_side_px": depth.rect_min_side_px,
                "rect_max_side_px": depth.rect_max_side_px,
                "rect_merge_gap_px": depth.rect_merge_gap_px,
                "rect_side_length": depth.rect_side_length,
                "avg_depth_enable": depth.avg_depth_enable,
                "avg_depth_diameter": depth.avg_depth_diameter,
                "depth_valid_min_mm": depth.depth_valid_min_mm,
            }
            known = set(CircleParams.__dataclass_fields__)
            for key, val in (cfg.circle or {}).items():
                if key in known and key in self._circle_widgets:
                    mapping[key] = val
            self._apply_circle_widgets(mapping)
            off = cfg.offsets
            self.result.setText("\n".join([
                "已载入引导配置（guidance.yaml）到找圆调试面板",
                f"  深度：{depth.depth_min_mm:g}–{depth.depth_max_mm:g}mm，"
                f"二值化={depth.thresh_method}，形态学={depth.morph_op}",
                f"  找圆：{cfg.circle.get('circle_type', '-')}，"
                f"半径 {cfg.circle.get('min_radius', '-')}–{cfg.circle.get('max_radius', '-')}px",
                f"  偏移轴映射：粗 {off.coarse.x_axis}{off.coarse.x_sign:+d}/"
                f"{off.coarse.y_axis}{off.coarse.y_sign:+d}，"
                f"精 {off.fine.x_axis}{off.fine.x_sign:+d}/{off.fine.y_axis}{off.fine.y_sign:+d}，"
                f"tcp=({off.tcp_x:g}, {off.tcp_y:g})mm",
                "  面板改动只影响本次调试，不回写 guidance.yaml。"]))
            self._update_circle_hint()

        # ---------- 找圆调试：叠加 / 阶段图 ----------
        @staticmethod
        def _circle_outline(cx, cy, r, n=48):
            import numpy as _np
            angles = _np.linspace(0.0, 2.0 * _np.pi, n, endpoint=False)
            pts = _np.stack([cx + r * _np.cos(angles), cy + r * _np.sin(angles)], axis=1)
            return pts.reshape(-1, 1, 2).astype("float32")

        def _draw_circle_overlays(self) -> None:  # pragma: no cover - Qt
            """彩色主图叠加：保留轮廓=绿、矩形=红、圆=红圈、圆心=黄十字、选中圆=青圈。"""
            v = self.view_color
            v.clear_contours_overlay()
            v.clear_mark_overlay()
            v.clear_points_overlay()
            v.clear_circle_overlay()
            # 找圆结果在原图坐标算；显示时按当前几何变换换算（DEC-120）
            if self._circle_contours:
                v.set_contours_overlay(
                    self._orig_to_preview_contours(self._circle_contours))
            marks = []
            for rect in self._circle_rects:
                x1, y1, x2, y2 = (float(rect["x1"]), float(rect["y1"]),
                                  float(rect["x2"]), float(rect["y2"]))
                marks.append([self._orig_to_preview_pt(x1, y1),
                              self._orig_to_preview_pt(x2, y1),
                              self._orig_to_preview_pt(x2, y2),
                              self._orig_to_preview_pt(x1, y2)])
            preview_hits = []
            for h in self._circle_hits:
                cx, cy = self._orig_to_preview_pt(h.cx, h.cy)
                preview_hits.append((cx, cy, h))
                # 镜像/90°整数倍旋转不改变半径，圆心换到预览坐标即可
                marks.append(self._circle_outline(cx, cy, max(h.radius, 1.0)))
            if marks:
                v.set_mark_overlay(marks)
            if preview_hits:
                v.set_points_overlay([(cx, cy) for cx, cy, _h in preview_hits])
                cx, cy, h0 = preview_hits[0]
                v.set_circle_overlay(cx, cy, max(h0.radius, 1.0))

        def _render_circle_stage(self) -> None:  # pragma: no cover - Qt
            """深度图窗口按「调试画面」显示阶段图；原彩色+矩形时显示原始深度伪彩。"""
            self._apply_display_rotation(getattr(self._frame, "camera_id", None))
            frame = self._frame
            if frame is None:
                self.view_depth.clear_image("尚未抓拍")
                return
            stage = str(self._circle_value("stage"))
            if stage == STAGE_ORIG or not self._circle_stage_images:
                depth = frame.depth.data if frame.depth is not None else None
                lo, hi, scale = self._display_range_for_frame(frame)
                if not self.view_depth.show_array(
                        depth, kind="depth", depth_lo_mm=lo, depth_hi_mm=hi,
                        depth_scale_m=scale):
                    self.view_depth.clear_image("该帧无深度流")
                return
            key = "contour_image" if stage == "contour" else stage
            arr = self._circle_stage_images.get(key)
            if arr is None:
                self.view_depth.clear_image(f"阶段图 {stage} 尚未生成（先点「深度检测」）")
                return
            if getattr(arr, "ndim", 0) == 2:
                import numpy as _np
                arr = _np.dstack([arr, arr, arr])
            self.view_depth.show_array(arr, kind="color")

        def _clear_circle_debug_overlays(self) -> None:  # pragma: no cover - Qt
            """清除找圆调试的叠加与缓存（不动用户多边形/ROI）。"""
            v = self.view_color
            v.clear_contours_overlay()
            v.clear_mark_overlay()
            v.clear_points_overlay()
            v.clear_circle_overlay()
            self._circle_result = None
            self._circle_rects = []
            self._circle_hits = []
            self._circle_contours = []
            self._circle_stage_images = {}
            self.circle_hint.setText("")

        def _clear_circle_debug_all(self) -> None:  # pragma: no cover - Qt
            self._clear_circle_debug_overlays()
            self._render_circle_stage()
            self.result.setText("已清除找圆调试的轮廓/矩形/圆叠加与阶段图缓存"
                                "（用户多边形与 ROI 保留）")

        def _update_circle_hint(self) -> None:  # pragma: no cover - Qt
            bits = [f"矩形={len(self._circle_rects)}", f"圆={len(self._circle_hits)}"]
            if self._circle_result is not None:
                bits.append(f"阶段={self._circle_result.stage}")
                if self._circle_result.message:
                    bits.append(self._circle_result.message)
            geo = self._geo_transform_active()
            if geo:
                bits.append(f"⚠几何变换生效（{geo}）：找圆调试已停用")
            self.circle_hint.setText("；".join(bits))

        def _on_right_tab_changed(self, _idx: int) -> None:  # pragma: no cover - Qt
            """页签切换：三页共用同一主图/叠加层，切回哪页就恢复哪页的叠加。"""
            current = self.right_tabs.currentWidget()
            if current is self.circle:
                self._clear_seg_overlays()
                self._draw_circle_overlays()
                self._render_circle_stage()
                self._update_circle_hint()
                return
            # 离开找圆调试：清掉它的叠加（缓存留着，切回来能恢复）
            v = self.view_color
            v.clear_contours_overlay()
            v.clear_mark_overlay()
            v.clear_points_overlay()
            v.clear_circle_overlay()
            if current is self.seg and self._seg_result is not None \
                    and not self._seg_result.error:
                res = self._seg_result
                v.set_contours_overlay(
                    self._orig_to_preview_contours(res.depth_contours))
                v.set_mark_overlay(
                    self._orig_to_preview_contours(res.intersection_contours))
                v.set_points_overlay(
                    self._orig_to_preview_points(
                        [p for g in res.prompts for p in g]))
            self._render_preview()

        # ---------- 图像算法面板 (由 PANEL_GROUPS 生成, 不手写控件) ----------
        def _build_algo_panel(self) -> QWidget:  # pragma: no cover - Qt
            """按参数表生成分组控件, 装在滚动区里 (面板可见组若干, 不滚动放不下)。

            只渲染 :data:`PANEL_GROUPS` —— 带 ``panel=False`` 的组 (十字线/显示增强)
            参数仍留在数据模型里, 但面板不给控件 (用户 2026-10-16「只从面板移除」)。

            构建期间置 ``_loading``: 初始化取值会触发 valueChanged, 那时预览控件虽已建好,
            但没必要跟着重算几十遍。
            """
            self._loading = True
            try:
                return self._build_algo_panel_inner()
            finally:
                self._loading = False

        def _build_algo_panel_inner(self) -> QWidget:  # pragma: no cover - Qt
            inner = QWidget()
            form = QVBoxLayout(inner)
            form.setContentsMargins(2, 2, 2, 2); form.setSpacing(6)
            for group in PANEL_GROUPS:
                form.addWidget(SectionTitle(group["label"]))
                note = group.get("note")
                if note:
                    lab = QLabel(note); lab.setObjectName("SubLabel")
                    lab.setWordWrap(True)
                    form.addWidget(lab)
                grid = QGridLayout()
                grid.setContentsMargins(0, 0, 0, 0)
                grid.setHorizontalSpacing(8); grid.setVerticalSpacing(4)
                # 逐参数排布: 带 ``same_row`` 的参数与上一个挤在同一行 (如 水平/垂直镜像)。
                row = 0
                col = 0
                for spec in group["params"]:
                    if col == 0 or not spec.get("same_row"):
                        row = row if col == 0 else row + 1
                        col = 0
                    name = QLabel(spec["label"]); name.setObjectName("SubLabel")
                    grid.addWidget(name, row, col * 2)
                    grid.addWidget(self._make_widget(spec, group["label"]), row, col * 2 + 1)
                    col += 1
                grid.setColumnStretch(1, 1)
                grid.setColumnStretch(3, 1)
                form.addLayout(grid)
            form.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            # 最小高别开太大: 它直接抬高整页的布局下限, 窗口就再也压不回设计尺寸。
            scroll.setMinimumHeight(130)
            scroll.setWidget(inner)
            return scroll

        def _make_widget(self, spec: dict, group_label: str = ""):  # pragma: no cover - Qt
            kind, key = spec["kind"], spec["key"]
            if kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(spec["default"]))
                w.toggled.connect(self._on_algo_changed)
            elif kind == "choice":
                w = QComboBox()
                for code, label in spec["choices"]:
                    w.addItem(str(label), code)
                idx = w.findData(spec["default"])
                w.setCurrentIndex(idx if idx >= 0 else 0)
                w.currentIndexChanged.connect(self._on_algo_changed)
            elif kind == "int":
                w = QSpinBox()
                w.setRange(int(spec["min"]), int(spec["max"]))
                w.setSingleStep(int(spec.get("step", 1)))
                w.setValue(int(spec["default"]))
                if spec.get("suffix"):
                    w.setSuffix(str(spec["suffix"]))
                w.valueChanged.connect(self._on_algo_changed)
            else:
                w = QDoubleSpinBox()
                w.setRange(float(spec["min"]), float(spec["max"]))
                w.setSingleStep(float(spec.get("step", 1.0)))
                w.setDecimals(int(spec.get("decimals", 2)))
                w.setValue(float(spec["default"]))
                if spec.get("suffix"):
                    w.setSuffix(str(spec["suffix"]))
                w.valueChanged.connect(self._on_algo_changed)
            w.setObjectName(f"algo_{key}")
            w.setToolTip(f"{spec['label']}（{group_label}）" if group_label
                         else str(spec["label"]))
            self._widgets[key] = (w, kind)
            return w

        def _params_from_widgets(self):  # pragma: no cover - Qt
            """收集面板取值 -> 合法 ImageOpsParams (越界/非法枚举由 coerce_params 兜底)。"""
            raw = {}
            for key, (w, kind) in self._widgets.items():
                if kind == "bool":
                    raw[key] = w.isChecked()
                elif kind == "choice":
                    raw[key] = w.currentData()
                else:
                    raw[key] = w.value()
            return coerce_params(raw)

        def _load_params(self, params) -> None:  # pragma: no cover - Qt
            """把参数写进控件; 期间屏蔽预览信号 (否则会触发几十次重算)。"""
            self._loading = True
            try:
                for key, (w, kind) in self._widgets.items():
                    v = getattr(params, key)
                    if kind == "bool":
                        w.setChecked(bool(v))
                    elif kind == "choice":
                        idx = w.findData(v)
                        w.setCurrentIndex(idx if idx >= 0 else 0)
                    else:
                        w.setValue(v)
            finally:
                self._loading = False

        def _ds86_guidance_cfg(self):  # pragma: no cover - Qt
            """引导配置里的 DS86 块 (随 guidance.yaml 导入; 含旧工程显示区间)。"""
            cfg = self._guidance_cfg()
            return getattr(cfg, "ds86", None) if cfg is not None else None

        def _channel_params(self, cam):
            """通道载入面板的初始参数 (DEC-046)。

            DS86 通道从未显式保存过深度区间时, 按现场显示区间 (guidance ds86
            ``*_depth_map_*`` = 100–300 mm) 播种 —— 面板显示的就是伪彩实际映射区间;
            通道已显式保存过值时以保存值为准 (深度区间若仍按 200–3000 映射, 现场
            100–300 mm 的像素会全被压到色标最低端, 看起来"深度图全黑")。
            """
            raw = dict(cam.image_ops) if isinstance(cam.image_ops, dict) else {}
            lo, hi = depth_display_range_mm(getattr(cam, "camera_id", None), raw,
                                            self._ds86_guidance_cfg())
            return coerce_params(raw).copy(depth_seg_min_mm=int(round(lo)),
                                           depth_seg_max_mm=int(round(hi)))

        def _display_range_for_frame(self, frame):  # pragma: no cover - Qt
            """该帧深度伪彩的显示区间与比例 (供原图/找圆调试的原深度显示)。"""
            cid = getattr(frame, "camera_id", None) or self.selected_camera()
            try:
                cam = self.store.find_camera(cid) if cid else None
            except Exception:  # noqa: BLE001
                cam = None
            lo, hi = depth_display_range_mm(
                cid, getattr(cam, "image_ops", None), self._ds86_guidance_cfg())
            return lo, hi, getattr(frame, "depth_scale_m", None)

        def _on_algo_changed(self, *_a) -> None:  # pragma: no cover - Qt
            if self._loading:
                return
            self._params_loaded_for = self.selected_camera()   # 已载入面板的通道
            # 显示预览变了（尤其几何变换）-> 上一轮的轮廓/交集叠加与当前画面不再对应
            self._clear_seg_overlays()
            self._update_seg_hint()
            self._render_preview()

        def _reset_params(self) -> None:  # pragma: no cover - Qt
            self._load_params(default_params())
            self._params_loaded_for = self.selected_camera()
            self._render_preview()
            self.result.setText("图像算法参数已恢复默认（尚未保存到设备配置，点「保存到本通道」才落盘）")

        def _save_params(self) -> None:  # pragma: no cover - Qt
            """按通道持久化: 只写**与默认值不同**的项, 其余回落默认 (默认值以后升级能跟得上)。"""
            cid = self.selected_camera()
            cam = self._selected_entry()
            if cid is None or cam is None:
                self.result.setText("请先选择一个通道")
                return
            params = self._params_from_widgets()
            changed = {k: getattr(params, k) for k in params.changed_keys()}
            try:
                self.store.update_camera(cid, replace(cam, image_ops=changed))
                self.store.save()
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"保存图像算法参数失败：{exc}")
                return
            self._params_loaded_for = cid
            self.result.setText("\n".join([
                f"已保存图像算法参数到通道 {cid}（设备配置 devices.yaml）",
                f"  只写入与默认值不同的 {len(changed)} 项；其余回落到默认值",
                *self._algo_snapshot_lines()]))

        def _algo_snapshot_lines(self) -> list[str]:  # pragma: no cover - Qt
            """本次生效参数的快照 (给「结构化结果」卡片)。"""
            params = self._params_from_widgets()
            lines = ["【图像算法（只影响显示预览，不参与判定）】"]
            lines.append("  原图对照：已开启（未套用算法）" if self.chk_raw.isChecked()
                         else "  原图对照：关闭")
            desc = describe(params)
            if desc:
                lines.extend("  " + d for d in desc)
                lines.append(f"  共改动 {len(params.changed_keys())} 项参数")
            else:
                lines.append("  全部为默认值（未做图像处理）")
            det = getattr(self._last_result, "detections", None)
            if det is not None and det.kind != "none":
                lines.extend("  " + t for t in format_detections(det))
            return lines

        # ---------- 列表 / 选择 ----------
        def refresh(self) -> None:  # pragma: no cover - Qt
            cfg = self.store.config
            keep = self.selected_camera()
            # 重建列表时**屏蔽信号**: 否则 clear()/逐条 addItem 会连着触发一串
            # currentRowChanged —— 中间态会把别的通道的参数载进面板, 也会被误当成
            # "用户换了通道"。(QListWidget 在空表里插入首条时还可能自动把它设为当前项。)
            # 下面在重建完成后显式调用一次 _on_select(), 状态一样同步。
            self.chan_list.blockSignals(True)
            try:
                self.chan_list.clear()
                for cam in cfg.cameras:
                    status = self.state.status_text(cam)
                    label = (f"{cam.name or cam.camera_id}  ·  {cam.camera_id}"
                             f"  ·  {cam.protocol}  ·  {status}")
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, cam.camera_id)
                    item.setToolTip(label)  # 左列收窄后可能省略号截断, 悬停看全
                    self.chan_list.addItem(item)
                if keep is not None:
                    for row in range(self.chan_list.count()):
                        if self.chan_list.item(row).data(
                                Qt.ItemDataRole.UserRole) == keep:
                            self.chan_list.setCurrentRow(row)
                            break
            finally:
                self.chan_list.blockSignals(False)
            self._on_select()

        def _apply_display_rotation(self, camera_id: str | None = None) -> None:
            """DEC-119：DS86 相机画面按显示旋转 270°（只影响显示，坐标仍是原图）。"""
            cid = str(camera_id or self.selected_camera() or "")
            deg = 270 if cid.lower().startswith("ds86") else 0
            for view in (self.view_color, self.view_depth):
                setter = getattr(view, "set_display_rotation", None)
                if callable(setter):
                    setter(deg)

        def selected_camera(self) -> str | None:  # pragma: no cover - Qt
            item = self.chan_list.currentItem()
            if item is None:
                return None
            return item.data(Qt.ItemDataRole.UserRole)

        def _selected_entry(self):  # pragma: no cover - Qt
            cid = self.selected_camera()
            return None if cid is None else self.store.find_camera(cid)

        def _on_select(self) -> None:  # pragma: no cover - Qt
            cam = self._selected_entry()
            if cam is None:
                self.kv_sel.set_key("当前通道", "未选择")
                self.btn_cap.setEnabled(False)
                self.btn_algo_save.setEnabled(False)
                self._set_conn_enabled(None)
                return
            status = self.state.status_text(cam)
            self.kv_sel.set_key("当前通道", f"{cam.name or cam.camera_id}"
                                            f"（{cam.camera_id} · {cam.protocol} · {status}）")
            self.btn_cap.setEnabled(bool(cam.enabled))
            self.btn_algo_save.setEnabled(True)
            self._set_conn_enabled(cam.camera_id)
            self.src_hint.setText(
                f"{self.state.detail_of(cam)}；抓拍仅取该通道，不触发任何运动。")
            # 通道第一次进入视野时载入它保存的参数；同一通道刷新 (如抓拍后) 不动面板,
            # 免得把操作员正在调的值冲掉。
            if self._params_loaded_for != cam.camera_id:
                self._load_params(self._channel_params(cam))
                self._params_loaded_for = cam.camera_id
            # 换通道 = 换画面：上一通道画的多边形/ROI 在别的分辨率上会张冠李戴 —— 复位。
            if self._seg_camera != cam.camera_id:
                self._seg_camera = cam.camera_id
                self._reset_seg_geometry()
            # 换个通道还挂着上一通道的帧会被误读成"当前通道的图" —— 清掉。
            if self._frame is not None and self._frame_camera != cam.camera_id:
                self._frame = None
                self._last_result = None
                self._frame_camera = None
                self.btn_save_img.setEnabled(False)
                self._clear_circle_debug_overlays()
                self._apply_display_rotation(cam.camera_id)
                self._render_preview()

        def _set_conn_enabled(self, cid: str | None) -> None:  # pragma: no cover - Qt
            """没有会话适配器的通道把「连接」**置灰**，并把原因挂在按钮上。

            理由：设备配置里没有对应会话（如没有可用的 zmq 通道）时，控制台不会报任何错，
            "点了才知道" 是这次踩坑的直接原因；能打开会话的通道才给按。
            """
            can = cid is not None and (self.state.has_adapter(cid)
                                      or self.state.is_open(cid))
            self.btn_conn.setEnabled(can)
            self.btn_conn.setToolTip(
                "" if (can or cid is None) else self.state.no_adapter_reason(cid))

        # ---------- 连接 ----------
        def _toggle_conn(self) -> None:  # pragma: no cover - Qt
            cid = self.selected_camera()
            if cid is None:
                self.result.setText("请先选择一个通道")
                return
            try:
                if self.state.is_open(cid):
                    self.state.close(cid)
                    self.result.setText(f"{cid} 会话已关闭")
                else:
                    self.state.open(cid)
                    self.result.setText(f"{cid} 会话已打开")
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"{cid} 连接失败：{exc}")
            self.refresh()

        # ---------- 抓拍 ----------
        # ---------- 主图弹窗（DEC-129） ----------
        def _ensure_image_popup(self):
            popup = getattr(self, "_image_popup", None)
            if popup is None:
                from application.presentation.components.capture_popup import (
                    CapturePopup,
                )
                popup = CapturePopup(self.mid, self._image_slot,
                                     parent=self.window(),
                                     title="视觉调试 · 主图（彩色 / 深度）")
                popup.finished.connect(self._on_image_popup_finished)
                self._image_popup = popup
            return popup

        def _on_image_popup_finished(self, *_a) -> None:  # pragma: no cover - Qt
            btn = getattr(self, "btn_image_popup", None)
            if btn is not None:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)

        def _toggle_image_popup(self, checked: bool) -> None:  # pragma: no cover - Qt
            popup = self._ensure_image_popup()
            if checked:
                popup.attach()
                popup.show()
                popup.raise_()
            else:
                popup.close()

        def shutdown(self) -> None:  # pragma: no cover - Qt
            popup = getattr(self, "_image_popup", None)
            if popup is not None:
                try:
                    popup.close()
                    popup.deleteLater()
                except Exception:  # noqa: BLE001
                    pass
                self._image_popup = None

        # ---------- 载入本地抓帧（DEC-126） ----------
        def _load_local_images(self) -> None:  # pragma: no cover - Qt
            """读本地保存的彩色/深度文件当一帧用（离线调参，不连相机）。"""
            from PySide6.QtWidgets import QFileDialog

            from application.services import frame_import

            start = os.path.join(app_paths.runtime_dir(), "captures")
            path, _flt = QFileDialog.getOpenFileName(
                self, "载入本地抓帧",
                start if os.path.isdir(start) else "",
                "抓帧文件 (*.npy *.png *.jpg *.jpeg *.json);;所有文件 (*)")
            if not path:
                return
            entries = frame_import.discover_frames([path])
            if not entries and path.lower().endswith("_depth.json"):
                entries = [e for e in frame_import.discover_frames(
                    [os.path.dirname(path)]) if e.meta_path == path]
            if not entries:
                self.result.setText(
                    "没有配对到可用的帧：请选彩色图、_depth.npy/_depth16.png"
                    "（或选四件套所在目录里任意一张）")
                return
            entry = frame_import.load_frame(entries[0])
            if entry.color is None and entry.depth is None:
                self.result.setText("该文件没有可读的彩色/深度数据："
                                    + "；".join(entry.notes))
                return
            frame = frame_import.to_bundle(entry)
            self._frame = frame
            self._frame_camera = frame.camera_id
            self._last_result = None
            self._clear_circle_debug_overlays()
            self._show_frame(frame, frame.camera_id)
            self.result.setText("\n".join([
                f"已载入本地帧：{os.path.basename(entry.name)}（相机 "
                f"{frame.camera_id}；非实时）",
                *[f"  {n}" for n in entry.notes],
                "  深度图可点击查看该点深度；切换通道或重新抓拍会清掉导入帧。",
            ]))

        def _on_depth_click(self, dx: float, dy: float) -> None:  # pragma: no cover - Qt
            """DEC-126：深度图取点读值（显示坐标 -> 原始帧坐标，含显示旋转）。"""
            frame = self._frame
            if frame is None or frame.depth is None:
                self.depth_probe.setText("当前没有深度帧")
                return
            depth = frame.depth.data
            h, w = depth.shape[:2]
            rot = int(self.view_depth.display_rotation() or 0) % 360
            src = self.view_depth.source_size()
            exp_w, exp_h = (h, w) if rot % 180 else (w, h)
            if src is not None and (src[0], src[1]) != (exp_w, exp_h):
                self.depth_probe.setText(
                    "当前深度窗口显示的不是整帧深度（阶段图/预览尺寸不同），"
                    "请切回原图再取点")
                return
            # 显示坐标（已含旋转） -> 原始帧坐标
            x, y = float(dx), float(dy)
            if rot == 90:
                x, y = y, (h - 1) - dx
            elif rot == 180:
                x, y = (w - 1) - dx, (h - 1) - dy
            elif rot == 270:
                x, y = (w - 1) - dy, dx
            xi, yi = int(round(x)), int(round(y))
            if not (0 <= xi < w and 0 <= yi < h):
                self.depth_probe.setText(f"点 ({xi},{yi}) 超出图像范围")
                return
            raw = int(depth[yi, xi])
            if raw in (0, 65535):
                self.depth_probe.setText(
                    f"像素 ({xi},{yi}) · 原始 {raw} · 无效深度（0/65535 视为无回波）")
                return
            scale = frame.depth_scale_m
            parts = [f"像素 ({xi},{yi}) · 原始 {raw}"]
            if scale:
                mm = float(raw) * float(scale) * 1000.0
                parts.append(f"{mm:.1f} mm")
            if frame.intrinsics is not None and scale:
                from application.algorithms.guidance.depth_guidance import (
                    compute_xyz_from_depth_mm,
                )
                from application.services.guidance_service import intrinsics_to_dict
                xyz = compute_xyz_from_depth_mm(
                    float(raw) * float(scale) * 1000.0, xi, yi,
                    intrinsics_to_dict(frame.intrinsics))
                if xyz is not None:
                    parts.append("相机 XYZ=(%.1f, %.1f, %.1f) mm" % tuple(xyz))
            else:
                parts.append("缺内参：不算 XYZ")
            self.depth_probe.setText(" · ".join(parts))

        def _capture(self) -> None:  # pragma: no cover - Qt
            cid = self.selected_camera()
            cam = self._selected_entry()
            if cid is None or cam is None:
                self.result.setText("请先选择一个通道")
                return
            if not cam.enabled:
                self.result.setText(f"{cid} 未启用：不抓拍（先到设备配置页启用并现场核对）")
                return
            from application.contracts.frames import CaptureRequest, FreshnessPolicy
            stack = self.ctx.stack
            executor = getattr(stack, "executor", None)
            if executor is None:
                self.result.setText("当前栈没有采集执行器")
                return
            ctx = getattr(getattr(stack, "task", None), "run_ctx", None) or _ctx_stub(stack)
            config_store = getattr(stack, "device_config", None)
            cam_cfg = config_store.find_camera(cid) if config_store is not None else None
            policy = (FreshnessPolicy.ALLOW_ARRIVAL_ONLY
                      if getattr(cam_cfg, "protocol", "") == "realsense_usb"
                      else FreshnessPolicy.REQUIRE_DEVICE_TIME)
            try:
                cs = executor.capture(ctx, CaptureRequest(
                    camera_ids=(cid,), request_id=f"vision-debug-{cid}",
                    step_id="vision_debug", allow_partial=True,
                    freshness_policy=policy))
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"抓拍失败：{exc}")
                return
            frame = cs.frames.get(cid)
            err = cs.errors.get(cid)
            if frame is None:
                self._frame = None
                self._last_result = None
                self._frame_camera = None
                self._clear_circle_debug_overlays()
                self._render_preview()
                self.result.setText(f"抓拍未取得帧：{err or '未知原因'}")
                return
            self._frame = frame
            self._frame_camera = cid
            self.refresh()            # 抓拍可能隐式建立会话 -> 状态列/提示同步刷新
            self._show_frame(frame, cid)

        def _save_captured_image(self) -> None:  # pragma: no cover - Qt
            """DEC-121：保存本次抓拍帧（原始数据，不套显示旋转/预览算法）。"""
            from application.presentation.components.frame_export import (
                save_frame_files,
            )
            if self._frame is None:
                self.result.setText("请先「单次抓拍」一帧，再保存图片")
                return
            saved = save_frame_files(self._frame, self,
                                     camera_id=self._frame_camera,
                                     title="保存抓拍图片")
            if saved:
                self.result.setText("已保存：" + "；".join(saved))

        def _show_frame(self, frame, cid: str) -> None:  # pragma: no cover - Qt
            self.btn_save_img.setEnabled(
                frame is not None and (frame.color is not None
                                       or frame.depth is not None))
            self._apply_display_rotation(cid)
            depth = frame.depth.data if frame.depth is not None else None
            color = frame.color.data if frame.color is not None else None

            self.kv_frame.set_key("来源帧", f"{frame.frame_id} · {cid}")
            self.kv_seq.set_key("序号", str(frame.sequence))
            if depth is not None:
                self.kv_shape.set_key("尺寸 / dtype",
                                      f"{depth.shape} {depth.dtype} (深度)")
            elif color is not None:
                self.kv_shape.set_key("尺寸 / dtype", f"{color.shape} {color.dtype}")
            if frame.depth_scale_m:
                scale = float(frame.depth_scale_m)
                self.kv_scale.set_key("深度比例", f"{scale} m/单位"
                                      f"（1 原始值 = {scale * 1000:g} mm）")
            else:
                self.kv_scale.set_key("深度比例",
                                      "未提供（切分上下限按 1 原始值 = 1 mm 换算）")
            self.kv_valid.set_key("有效深度比",
                                  f"{frame.depth_valid_ratio:.2f}"
                                  if frame.depth_valid_ratio is not None else "未提供")
            intr = frame.intrinsics
            self.kv_intr.set_key("内参", (f"fx={intr.fx:.1f} fy={intr.fy:.1f} "
                                          f"cx={intr.cx:.1f} cy={intr.cy:.1f} "
                                          f"@ {intr.intrinsics_space.value}"
                                          if intr is not None else "未提供"))
            src = self._source_label(frame)
            self.src_hint.setText(f"{src}；{self.state.detail_of(self._selected_entry())}")
            # 新帧 = 新坐标：上一帧的轮廓/交集/提示点叠加一律作废（多边形与 ROI 保留）
            self._clear_seg_overlays()
            self._clear_circle_debug_overlays()
            self._render_preview()
            self.result.setText("\n".join([
                f"抓拍成功：{frame.frame_id}（{cid}）"
                f"  序号={frame.sequence} 新鲜度={frame.freshness_quality.value}",
                *self._algo_snapshot_lines()]))

        # ---------- 预览 (算法只作用于这里) ----------
        def _render_preview(self) -> None:  # pragma: no cover - Qt
            """按当前面板参数重画**显示预览**。原始帧 (``self._frame``) 全程只读。"""
            self._apply_display_rotation(getattr(self._frame, "camera_id", None))
            # 几何变换可能刚改过：多边形/ROI 按新变换重新换算显示（DEC-120）
            if self._frame is not None:
                self._set_polygon(self._polygon)
                self._set_roi(self._roi)
            frame = self._frame
            if frame is None:
                self._last_result = None
                self.view_color.clear_image("尚未抓拍")
                self.view_depth.clear_image("尚未抓拍")
                self._clear_seg_overlays()
                self.preview_hint.setText("")
                return
            color = frame.color.data if frame.color is not None else None
            depth = frame.depth.data if frame.depth is not None else None

            if self.chk_raw.isChecked():
                self._last_result = None
                if not self.view_color.show_array(color, kind="color"):
                    self.view_color.clear_image("该帧无彩色流")
                lo, hi, scale = self._display_range_for_frame(frame)
                if not self.view_depth.show_array(
                        depth, kind="depth", depth_lo_mm=lo, depth_hi_mm=hi,
                        depth_scale_m=scale):
                    self.view_depth.clear_image("该帧无深度流")
                self.preview_hint.setText(
                    "原图对照：未套用任何图像算法（原始帧未被改动）；"
                    f"深度伪彩按 {lo:g}–{hi:g} mm 显示")
                return

            try:
                # 深度切分上下限按 mm 给: 把该帧的深度比例交给算法层换算 (见 DEC-025)
                result = apply_chain(color, depth, self._params_from_widgets(),
                                     depth_scale_m=getattr(frame, "depth_scale_m", None))
            except Exception as exc:  # noqa: BLE001
                self.preview_hint.setText(f"预览算法失败：{exc}（原始帧未被改动）")
                return
            self._last_result = result
            # 处理后的图已是 3 通道 RGB, 两路都按 color 显示 (按 depth 会再套一次伪彩)
            if not self.view_color.show_array(result.color, kind="color"):
                self.view_color.clear_image("该帧无彩色流")
            if not self.view_depth.show_array(result.depth, kind="color"):
                self.view_depth.clear_image("该帧无深度流")

            n = len(result.params.changed_keys())
            bits = [f"预览已套用 {n} 项参数" if n else "参数全为默认值，预览＝原图"]
            if result.detections.kind != "none":
                bits.append(f"检测[{result.detections.kind}] 命中"
                            f" {result.detections.count}")
            bits.extend(result.notes)
            self.preview_hint.setText(
                "；".join(bits) + "（仅影响显示，原始帧未改动）")

        def _source_label(self, frame) -> str:
            """来源标注要**按适配器/服务/协议**说清楚, 不能一律写成 FakeCamera 或 V2。"""
            offline = getattr(self.ctx.stack, "profile", "") == "offline"
            service = str(getattr(frame, "service_id", "") or "")
            if service == "file-import" or str(
                    getattr(frame, "capture_id", "") or "").startswith("import-"):
                return ("本地文件导入（非实时；深度比例/内参来自 _depth.json 或手工指定，"
                        "不冒充现场采集）")
            if "s100" in service:
                adapter = self.state.adapter(str(frame.camera_id))
                protocol = str(getattr(adapter, "protocol", "") or "")
                if protocol == LEGACY_S100_PROTOCOL:
                    # 现场部署的是旧协议: 别标成 V2 —— 排查协议问题时会被带偏
                    return ("离线合成（进程内 Fake S100，非真实相机图像）" if offline
                            else f"真实 S100 采集；协议=现场旧协议 pickle/v1（{service}）")
                base = ("离线合成（进程内 Fake S100 V2 服务，非真实相机图像）" if offline
                        else "真实 S100 采集")
                icd = getattr(adapter, "icd_status", None)
                return f"{base}；V2 编码状态：{icd or 'draft（待 E05 ICD 冻结）'}"
            if offline:
                return "离线合成（FakeCamera，非真实相机图像）"
            return "真实采集"

        def _copy_result(self) -> None:  # pragma: no cover - Qt
            """把结构化结果整段放进剪贴板（也可用鼠标选中 + Ctrl+C）。

            反馈只放在按钮提示上：不弹对话框、不覆盖结果、**不起定时器**
            （定时器回调可能晚于控件销毁，见"Qt 关闭顺序"那条坑）。
            """
            from PySide6.QtWidgets import QApplication
            text = self.result.text() or ""
            QApplication.clipboard().setText(text)
            self.btn_copy.setToolTip(f"已复制 {len(text)} 个字符到剪贴板")

        # ---------- 命名视觉检测（另存 / 删除，供配方 vision.run 引用） ----------
        def _detection_store(self):  # pragma: no cover - Qt
            return getattr(self.ctx.stack, "vision_detections", None)

        def _refresh_detections(self) -> None:  # pragma: no cover - Qt
            """把已保存的检测刷新到左列卡片；没有则显示引导提示。"""
            store = self._detection_store()
            items = list(getattr(store, "detections", []) or []) if store is not None else []
            self.det_list.blockSignals(True)
            try:
                self.det_list.clear()
                for det in items:
                    label = (f"{det.detection_id}  ·  {det.camera_ref or '未记通道'}"
                             f"  ·  {det.pipeline_ref}")
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, det.detection_id)
                    item.setToolTip(f"{label}\n来源：{det.source or '未记'}"
                                    f"\n参数哈希：{det.params_hash[:12]}")
                    self.det_list.addItem(item)
            finally:
                self.det_list.blockSignals(False)
            has = bool(items)
            self.det_list.setVisible(has)
            self.det_hint.setVisible(not has)
            self.btn_det_del.setEnabled(has)
            combo = getattr(self, "det_overwrite", None)
            if combo is not None:
                keep = combo.currentData()
                combo.blockSignals(True)
                try:
                    combo.clear()
                    combo.addItem("（新建）", None)
                    for det in items:
                        combo.addItem(det.detection_id, det.detection_id)
                    idx = combo.findData(keep)
                    combo.setCurrentIndex(idx if idx >= 0 else 0)
                finally:
                    combo.blockSignals(False)

        def _detection_snapshot(self, vision) -> dict:  # pragma: no cover - Qt
            """保存那一刻的配置快照：**判定**用的流水线参数 + **显示**用的图像算法参数
            + **找圆调试面板参数**（深度引导/找圆参数，DEC-047）。

            两类分开列出并写清用途，别让人以为显示参数参与了判定（DEC-021）。
            """
            snap: dict = {}
            spec = getattr(vision, "spec", None)
            if spec is not None:
                snap["pipeline"] = {
                    "pipeline_id": str(getattr(spec, "pipeline_id", "")),
                    "version": str(getattr(spec, "version", "")),
                    "host": str(getattr(spec, "host", "")),
                    "params": _plain(getattr(spec, "params", None)),
                    "decision": _plain(getattr(spec, "decision", None)),
                }
            try:
                params = self._params_from_widgets()
                snap["display_image_ops"] = {k: getattr(params, k)
                                             for k in params.changed_keys()}
            except Exception:  # noqa: BLE001
                snap["display_image_ops"] = {}
            # 关键：把**运行一次的实现逻辑**（图源/区域/多边形/深度分割参数/智能分割参数）
            # 一并存进检测。配方 vision.run 引用这条检测时，在真实帧上重跑的就是这套逻辑，
            # 而不是只记一个流水线名（见 DEC-026）。
            scheme = self._build_scheme()
            snap["scheme"] = scheme.to_dict()
            # 「找圆调试」面板的参数也一并保存（DEC-047）：载回检测时按原样复原，
            # 免得调好的深度引导/找圆参数只能靠「载入引导配置」重新找回来。
            snap["circle_debug"] = self._circle_debug_snapshot()
            tab = self._active_right_tab_key()
            # 「图像算法」页不算调试目标：不在那儿保存时就不记页签偏好
            snap["active_tab"] = tab if tab in ("seg", "circle") else ""
            return snap

        def _save_as_detection(self) -> None:  # pragma: no cover - Qt
            cid = self.selected_camera()
            if cid is None:
                self.result.setText("请先选择一个通道，再把当前检测另存")
                return
            store = self._detection_store()
            if store is None:
                self.result.setText("当前栈未接入视觉检测存储（vision_detections），无法另存")
                return
            vision = getattr(self.ctx.stack, "vision", None)
            pipeline_ref = str(getattr(vision, "pipeline_id", "") or "")
            if not pipeline_ref:
                self.result.setText("当前栈没有可用的检测流水线，无法另存检测")
                return
            target = None
            combo = getattr(self, "det_overwrite", None)
            if combo is not None:
                target = combo.currentData()
            if target:
                # 下拉里明确选了要覆盖的检测：不再要求输入同名
                det_id = str(target).strip()
                if store.find(det_id) is None:
                    self.result.setText(
                        f"选择的检测 {det_id!r} 已不存在，请刷新后重试或选「（新建）」")
                    return
                if not _confirm(self, "覆盖检测",
                                f"用当前设置覆盖检测 {det_id!r}？"):
                    return
            else:
                default_id = f"检测-{cid}"
                name, ok = _ask_text(
                    self, "另存为视觉检测",
                    "检测名称（执行方案 vision.run 步骤将引用它）:", default_id)
                if not ok or not name.strip():
                    return
                det_id = name.strip()
                if store.find(det_id) is not None and not _confirm(
                        self, "覆盖检测",
                        f"检测 {det_id!r} 已存在，用当前设置覆盖它？"):
                    return
            det = VisionDetection(
                detection_id=det_id, label=det_id, camera_ref=cid,
                pipeline_ref=pipeline_ref, params=self._detection_snapshot(vision),
                source="视觉调试页另存")
            has_poly = len(self._polygon) >= 3
            try:
                store.upsert(det)
                store.save()
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"另存视觉检测失败：{exc}")
                return
            self._refresh_detections()
            lines = [
                f"已另存视觉检测 {det_id!r}",
                f"  通道 {cid}；流水线 {pipeline_ref}",
                f"  参数哈希 {det.params_hash[:12]}…（含判定参数 + 显示参数 + 找圆调试参数三类快照）",
                f"  运行一次方案：{'已保存（图源/区域/多边形/参数）' if has_poly else '未保存用户多边形——执行方案运行时会因缺多边形失败'}",
                f"  找圆调试参数：已保存（深度 {self._circle_value('depth_min_mm'):g}–"
                f"{self._circle_value('depth_max_mm'):g}mm，二值化 "
                f"{self._circle_value('thresh_method')}；找圆 "
                f"{self._circle_value('circle_type')} 半径 "
                f"{self._circle_value('min_radius'):g}–"
                f"{self._circle_value('max_radius'):g}px）；双击载回时一并复原",
                f"  存于 {getattr(store, 'path', '') or '（内存）'}；执行方案 vision.run 可引用此名",
                "说明：这是调试配置的命名保存，不是发布；离线档位下绑定的流水线快照来自"
                "进程内 stub 圆宿主（运行一次方案本身是真实算法：深度分割 + EdgeSAM + 交集）。",
                *self._algo_snapshot_lines()]
            if not has_poly:
                lines.append("提示：先在彩色主图上「绘制多边形」（≥3 点）再另存，"
                             "执行方案 vision.run 引用的运行一次方案才完整。")
            self.result.setText("\n".join(lines))

        def _load_detection_scheme(self) -> None:  # pragma: no cover - Qt
            """双击已保存检测：载回运行一次方案与找圆调试参数，切到保存时的页签。

            DEC-047：找圆调试面板的深度引导/找圆参数也是「检测」的一部分，
            以前只载回运行一次方案（而且总跳到「视觉分割」页），调好的找圆参数丢了。
            这里按检测里实际保存了什么如实复原；旧的只绑流水线检测明确拒绝。
            """
            store = self._detection_store()
            item = self.det_list.currentItem()
            if store is None or item is None:
                self.result.setText("请先选择一条视觉检测")
                return
            det_id = item.data(Qt.ItemDataRole.UserRole)
            det = store.find(det_id)
            params = getattr(det, "params", None) if det is not None else None
            scheme = scheme_from_params(params)
            circle_snap = params.get("circle_debug") if isinstance(params, dict) else None
            if scheme is None and not isinstance(circle_snap, dict):
                self.result.setText(
                    f"检测 {det_id!r} 没有保存运行一次方案或找圆调试参数"
                    "（旧的只绑流水线检测），无法载回。")
                return
            lines = [f"已载回检测 {det_id!r}"]
            if scheme is not None:
                self._load_scheme(scheme)
                lines.append(
                    f"  运行一次方案：图源={scheme.source}；区域={scheme.op_region}；"
                    f"多边形={len(scheme.polygon)} 点；ROI={'有' if scheme.roi else '无'}")
            if isinstance(circle_snap, dict):
                lines.extend("  " + t for t in self._apply_circle_debug_snapshot(circle_snap))
                lines.append("  找圆调试的矩形/圆/阶段图是逐帧运算结果，载回参数后需重新"
                             "「深度检测」或「运行一次找圆调试」生成")
            tab = params.get("active_tab") if isinstance(params, dict) else None
            target = {"circle": self.circle, "seg": self.seg}.get(
                tab) if isinstance(tab, str) else None
            if target is None:      # 旧检测没有页签记录：有方案回视觉分割，只有找圆参数回找圆调试
                target = self.seg if scheme is not None else self.circle
            self.right_tabs.setCurrentWidget(target)
            lines.append("  面板可继续调整；改完再「另存为检测」覆盖它即可。")
            blocked = self._blocked_by_geometry()
            if blocked:
                lines.append(blocked)
            self.result.setText("\n".join(lines))

        def _delete_detection(self) -> None:  # pragma: no cover - Qt
            store = self._detection_store()
            item = self.det_list.currentItem()
            if store is None or item is None:
                self.result.setText("请先选择一条要删除的视觉检测")
                return
            det_id = item.data(Qt.ItemDataRole.UserRole)
            if not _confirm(self, "删除检测", f"确认删除视觉检测 {det_id!r} ?"):
                return
            store.remove(det_id)
            store.save()
            self._refresh_detections()
            self.result.setText(f"已删除视觉检测 {det_id!r}")

        # ---------- 判定 ----------
        def _run_pipeline(self) -> None:  # pragma: no cover - Qt
            frame = self._frame
            if frame is None:
                self.result.setText("请先抓拍一帧，再运行判定")
                return
            vision = getattr(self.ctx.stack, "vision", None)
            if vision is None:
                self.result.setText("当前栈没有视觉流水线")
                return
            try:
                det = vision.run(frame, result_id=f"vision-debug-{frame.sequence}")
            except Exception as exc:  # noqa: BLE001
                self.result.setText(f"判定失败：{exc}")
                return
            lines = [f"状态：{det.status.value}",
                     f"流水线：{det.pipeline_id}@{det.pipeline_version}（宿主 {det.host}）",
                     f"来源帧/相机：{det.frame_id} / {frame.camera_id}"]
            if det.measurements:
                for m in det.measurements[:5]:
                    unit = getattr(m, "unit", "")
                    lines.append(f"指标 {getattr(m, 'name', '?')}="
                                 f"{getattr(m, 'value', '?')} {unit}（单位={unit or '未标注'}）")
            if det.reason:
                lines.append(f"判定原因：{det.reason}")
            if det.errors:
                lines.append("错误：" + "；".join(det.errors))
            lines.append("像素指标不标 mm；单位以帧 depth_scale 与内参为准。")
            # 判定用的**始终是原始帧**，图像算法只影响显示 —— 照实写清楚，别让人以为
            # 面板上的参数参与了判定。
            lines.extend(self._algo_snapshot_lines())
            lines.append("说明：以上图像算法参数只用于显示预览，判定输入是未处理的原始帧。")
            self.result.setText("\n".join(lines))

        # 兼容旧接口名（其它页面/测试若调用）
        def _capture_and_judge(self) -> None:  # pragma: no cover - Qt
            self._capture()
            if self._frame is not None:
                self._run_pipeline()


__all__ = ["GUI_REQUIRED", "VisionPage"]
