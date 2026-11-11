"""配方流程页 (E20, 03 §8；DEC-024 / DEC-030)。

两栏编辑：**步骤 / 属性**，加底部**校验结果（可点回定位）**。已**去工位**：
配方只是一条有序 ``sequence``，按顺序串行执行；相机需求写在**抓拍步骤**里，
不再有配方级「必选相机」。

- 所有改动都走 :class:`application.services.recipes.editor.RecipeEditor`（纯 Python，
  每次改完重新过 pydantic + 五层校验），页面只负责把控件值翻译成调用；
- ``robot.move_joint`` 步骤在属性栏有**「关节目标」下拉**（引用已登记示教点）
  与**「运动方案」下拉**（可选，按步骤绑定；绑了就按方案走，否则用示教点）；
- ``custom.call``（**自定义操作**）步骤在属性栏：选一个方法库里注册的**自定义方法**
  + 勾选要操作的**设备**（可多台；执行时同时下发、等全部完成）；方法的**安全边界**
  由方法库声明，属性栏如实展示。
- ``vision.run`` 步骤在属性栏有**「检测来源」下拉**：直接引流水线，或引「视觉调试」页
  「另存为检测」的**命名视觉检测**（二选一，选一个自动清另一个）；
- ``vision.guided_position``（**引导定位**）步骤在属性栏：相机可选**任意已启用相机通道**
  （默认 `ds86_01`；DEC-137 起 D405 USB 等也可用，DEC-034 的"只认 DS86"已放开）、
  段选择（粗→精 / 仅粗 / 仅精）、速度（0 = 跟随引导配置）、
  偏移上限 mm（0 = 不设上限）；执行期**每段人工确认**在作业台页弹出（DEC-033）；
- **落盘（DEC-035）**：「保存」直接覆盖当前配方对应的文件（新建的草稿存到
  ``configs/recipes/<配方ID>.yaml``）；「另存为」弹路径对话框。配方目录里的
  ``*.yaml`` 在启动时**自动载入**，所以存过的配方下次启动直接出现在下拉里；
- **发布闸**：离线示例草稿不允许真实发布；发布走 ``RecipeService.publish``
  （权限 + 审计），本页不绕过。按钮不可用时给出**原因**，不静默变灰。
  **发布即生效**：「发布」不弹操作员、不记录操作人员（DEC-040），成功后直接把
  ``state: published`` 写回来源文件（作业台启动校验据此放行，重启后仍有效），
  并**弹窗提示发布成功**（DEC-041）。

本页**不建定时器**（页面切换时由外壳调 ``refresh()``）。
"""
from __future__ import annotations

import json
import os

from application.contracts.enums import GUIDANCE_CAMERA_ID, NodeType
from application.presentation.components import Card, EmptyHint
from application.services.recipes.editor import (
    STEP_TYPE_CN,
    STEP_TYPE_LABELS,
    RecipeEditor,
    step_title,
)
from application.services.recipes.service import ReferenceCatalog, RecipeService

try:  # pragma: no cover
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QGroupBox,
        QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
        QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]

# 属性栏「检测来源」下拉的分组前缀
_PIPE_PREFIX = "流水线："
_DET_PREFIX = "视觉检测："


def _ask_text(parent, title: str, label: str, text: str = "") -> tuple[str, bool]:
    """取一段文本（唯一文本对话框入口，便于测试 monkeypatch，不阻塞自动化）。"""
    from PySide6.QtWidgets import QInputDialog
    return QInputDialog.getText(parent, title, label, text=text)


def _ask_item(parent, title: str, label: str, items: list[str]) -> tuple[str, bool]:
    """从列表中取一项（同上，便于 monkeypatch）。"""
    from PySide6.QtWidgets import QInputDialog
    return QInputDialog.getItem(parent, title, label, items, 0, False)


def _confirm(parent, title: str, text: str) -> bool:
    from PySide6.QtWidgets import QMessageBox
    return QMessageBox.question(
        parent, title, text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    ) == QMessageBox.StandardButton.Yes


def _warn(parent, title: str, text: str) -> None:  # pragma: no cover - Qt
    QMessageBox.warning(parent, title, text)


def _info(parent, title: str, text: str) -> None:  # pragma: no cover - Qt
    QMessageBox.information(parent, title, text)


def _editor_for(ctx) -> RecipeEditor:
    """取栈里的配方服务；没有就退化成内存上的空目录（页面仍可构造，不崩）。"""
    svc = getattr(getattr(ctx, "stack", None), "recipes", None)
    if isinstance(svc, RecipeService):
        return RecipeEditor(svc)
    return RecipeEditor(RecipeService(ReferenceCatalog()))


if GUI_REQUIRED:

    def _fit_combo(combo: QComboBox, *, chars: int = 18) -> QComboBox:
        """属性栏下拉统一限宽（DEC-036）。

        长条目（如方案名『任意位置->拍照位（动作前请确保套筒已离开螺栓）』）默认会把
        下拉撑得比内容宽很多、右侧一片空白；而且选中长条目后**整格还会再变宽**
        （QComboBox 的 ``minimumSizeHint`` 跟当前项文本走）。这里按 ``chars`` 个字符
        定宽度上限（``maximumWidth`` 会把布局最小值一并夹住），完整文字放到 tooltip，
        弹层仍按内容展开、不影响选择。
        """
        fm = combo.fontMetrics()
        combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(chars)
        combo.setMaximumWidth(int(fm.horizontalAdvance("M") * chars + 48))
        combo.setToolTip(combo.currentText())
        combo.currentTextChanged.connect(combo.setToolTip)
        return combo

    def _wrap_note(text: str) -> QLabel:
        """属性栏说明文字（DEC-066）：宽度固定，放不下就折行，不横向撑宽、不被裁切。"""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setObjectName("SubLabel")
        return lbl

    class RecipesPage(QWidget):
        # 属性栏固定宽度（px，DEC-066）：按调整前的实际宽度固化，右侧步骤列表吃掉剩余空间
        _PROP_CARD_WIDTH = 520

        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.editor = _editor_for(ctx)
            self._sel_step: str | None = None
            self._building = False

            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            head = QHBoxLayout(); head.setSpacing(8)
            head.addWidget(QLabel("执行方案:"))
            self.recipe_combo = QComboBox(); self.recipe_combo.setMinimumWidth(240)
            self.recipe_combo.currentIndexChanged.connect(self._on_recipe_changed)
            head.addWidget(self.recipe_combo)
            self.btn_new = QPushButton("新建"); self.btn_new.setObjectName("Ghost")
            self.btn_new.clicked.connect(self._new_recipe)
            self.btn_copy = QPushButton("复制"); self.btn_copy.setObjectName("Ghost")
            self.btn_copy.clicked.connect(self._copy_recipe)
            # DEC-108：删除整条执行方案（草稿 + 来源文件改名备份）；运行中的方案不许删。
            self.btn_delete = QPushButton("删除"); self.btn_delete.setObjectName("Ghost")
            self.btn_delete.setToolTip("删除当前执行方案（来源文件改名为 .deleted-<时间>.bak，可手工恢复）")
            self.btn_delete.clicked.connect(self._delete_recipe)
            # 「保存」= 直接覆盖当前配方对应的文件（新建后没落过盘 → 存到配方目录）；
            # 「另存为」= 弹路径对话框（DEC-035）。存过的配方**下次启动自动进下拉**。
            self.btn_save = QPushButton("保存"); self.btn_save.setObjectName("Ghost")
            self.btn_save.setToolTip("直接覆盖当前执行方案对应的文件"
                                     "（新建的执行方案存到 configs/recipes/<ID>.yaml）")
            self.btn_save.clicked.connect(self._save_now)
            self.btn_save_as = QPushButton("另存为"); self.btn_save_as.setObjectName("Ghost")
            self.btn_save_as.setToolTip("选择路径另存一份（之后「保存」就写这个文件）")
            self.btn_save_as.clicked.connect(self._save_as)
            for b in (self.btn_new, self.btn_copy, self.btn_delete,
                      self.btn_save, self.btn_save_as):
                head.addWidget(b)
            head.addStretch(1)
            self.btn_validate = QPushButton("校验"); self.btn_validate.setObjectName("Ghost")
            self.btn_validate.clicked.connect(self._validate_and_show)
            self.btn_compare = QPushButton("版本比较"); self.btn_compare.setObjectName("Ghost")
            self.btn_compare.clicked.connect(self._compare)
            self.btn_publish = QPushButton("发布"); self.btn_publish.setObjectName("Primary")
            self.btn_publish.setEnabled(False)
            self.btn_publish.clicked.connect(self._publish)
            for b in (self.btn_validate, self.btn_compare, self.btn_publish):
                head.addWidget(b)
            root.addLayout(head)

            panes = QHBoxLayout(); panes.setSpacing(10)

            # ---------- 步骤 ----------
            self.step_card = Card("步骤（按顺序串行执行）")
            self.step_list = QListWidget()
            self.step_list.currentItemChanged.connect(lambda *_a: self._on_step_selected())
            self.step_card.add(self.step_list)
            self.step_hint = EmptyHint("还没有步骤；点「添加步骤」选类型。")
            self.step_card.add(self.step_hint)
            kbar = QHBoxLayout()
            for text, slot in (("添加步骤", self._add_step),
                               ("删除", self._remove_step),
                               ("上移", lambda: self._move_step(-1)),
                               ("下移", lambda: self._move_step(+1))):
                b = QPushButton(text); b.setObjectName("Ghost"); b.clicked.connect(slot)
                kbar.addWidget(b)
            kbar.addStretch(1)
            self.step_card.body().addLayout(kbar)
            panes.addWidget(self.step_card, 3)

            # ---------- 属性 ----------
            self.prop_card = Card("属性")
            # DEC-066：属性栏**固定宽度**（不随内容/窗口被撑宽），内容放不下时靠
            # 表单行换行（WrapLongRows）与说明文字折行，而不是横向滚动。
            self.prop_card.setFixedWidth(self._PROP_CARD_WIDTH)
            self.prop_scroll = QScrollArea()
            self.prop_scroll.setWidgetResizable(True)
            self.prop_scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.prop_scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.prop_body = QWidget()
            self.prop_lay = QVBoxLayout(self.prop_body)
            self.prop_lay.setContentsMargins(0, 0, 0, 0)
            self.prop_scroll.setWidget(self.prop_body)
            self.prop_card.add(self.prop_scroll)
            panes.addWidget(self.prop_card)
            root.addLayout(panes, 1)

            # ---------- 校验结果 ----------
            self.result_card = Card("校验结果（点一条可定位到步骤/字段）")
            self.result_label = QLabel("-"); self.result_label.setObjectName("SubLabel")
            self.result_label.setWordWrap(True)
            self.result_card.add(self.result_label)
            self.err_list = QListWidget()
            self.err_list.itemActivated.connect(self._goto_error)
            self.err_list.itemClicked.connect(self._goto_error)
            self.result_card.add(self.err_list)
            root.addWidget(self.result_card)

            self.refresh()

        # ------------------------------------------------------------- 目录同步
        def _sync_catalog(self) -> None:
            """把「运动方案」页的方案与「视觉调试」页另存的检测**实时**同步进引用目录。

            目录是校验/编译的引用来源；不同步的话，刚另存的检测在下拉里看不到，
            发布出去还会被判「引用未知检测」。
            """
            cat = self.editor.catalog
            store = getattr(getattr(self.ctx, "stack", None), "vision_detections", None)
            dets = getattr(store, "detections", None)
            if dets is not None:
                cat.detections = {d.detection_id: d.pipeline_ref for d in dets}
            mc = getattr(getattr(self.ctx, "stack", None), "motion_config", None)
            cfg = getattr(mc, "config", None)
            if cfg is not None:
                cat.motion_plans = {p.plan_id for p in cfg.plans}
            gs = getattr(getattr(getattr(self.ctx, "stack", None), "motion", None),
                         "guidance", None)
            gcfg = getattr(getattr(gs, "config_store", None), "config", None)
            if gcfg is not None:
                cat.fine_offset_profiles = set(gcfg.fine_profile_ids())

        # ------------------------------------------------------------- 刷新
        def refresh(self) -> None:  # pragma: no cover - Qt
            self._sync_catalog()
            self._reload()

        def _reload(self) -> None:  # pragma: no cover - Qt
            """重建配方下拉/两个列表/属性栏（保留原选中项）；期间屏蔽控件信号。"""
            self._building = True
            try:
                self._rebuild_recipe_combo()
                self._rebuild_step_list()
                self._rebuild_props()
            finally:
                self._building = False
            self._validate_and_show()

        def _rebuild_recipe_combo(self) -> None:
            keep = self.editor.recipe_id
            ids = self.editor.recipes()
            vm = getattr(self.ctx, "vm", None)
            if keep is None or keep not in ids:
                # 优先跟作业台当前选中的方案对齐（DEC-041 两页可选同一份）；
                # 只是刷新时**不回头覆盖**作业台的选择（用户可能在作业台换过）。
                pref = getattr(vm, "selected_recipe", None) if vm is not None else None
                keep = pref if pref in ids else (ids[0] if ids else None)
            self.recipe_combo.clear()
            for rid in ids:
                self.recipe_combo.addItem(rid, rid)
            idx = self.recipe_combo.findData(keep)
            if idx >= 0:
                self.recipe_combo.setCurrentIndex(idx)
            self.editor.select(keep)

        def _rebuild_step_list(self) -> None:
            model = self._model()
            steps = list(model.sequence) if model else []
            self.step_list.clear()
            for i, step in enumerate(steps, 1):
                extra = ""
                if step.type is NodeType.ROBOT_MOVE_BY_BOLT_SIZE:
                    extra = f"   ｜方案 {step.motion_plan_ref or '（未选）'}（按螺栓直径）"
                elif step.type is NodeType.ROBOT_MOVE_JOINT:
                    if step.motion_plan_ref:
                        extra = f"   ｜方案 {step.motion_plan_ref}"
                    elif step.joint_target_ref:
                        extra = f"   ｜目标 {step.joint_target_ref}"
                    else:
                        extra = "   ｜（未设运动目标）"
                elif step.type is NodeType.VISION_RUN:
                    src = step.detection_ref or step.pipeline_ref or "（未选检测来源）"
                    extra = f"   ｜检测 {src}"
                elif step.type is NodeType.GUIDED_POSITION:
                    cam = step.camera_ref or "（未选相机）"
                    stages = "+".join(step.guide_stages or ()) or "（未选段）"
                    extra = f"   ｜相机 {cam} ｜段 {stages}"
                elif step.type is NodeType.CUSTOM_CALL:
                    n = len(step.device_refs)
                    devs = "、".join(step.device_refs) if step.device_refs else "（未选设备）"
                    extra = f"   ｜{step.method_ref or '（未选方法）'} → {devs}"
                    if n:
                        extra += f" · {n} 台"
                elif step.type is NodeType.PARALLEL_GROUP:
                    robot = next((b for b in step.branches
                                  if b.type is NodeType.ROBOT_MOVE_JOINT), None)
                    mech = next((b for b in step.branches
                                 if b.type is NodeType.MECHANISM_ACTION), None)
                    robot_txt = "（未设运动目标）"
                    if robot is not None:
                        robot_txt = (robot.motion_plan_ref or robot.joint_target_ref
                                     or "（未设运动目标）")
                    mech_txt = ((mech.action_ref if mech is not None else None)
                                or "（未选动作）")
                    extra = f"   ｜机器人 {robot_txt} ∥ 机构 {mech_txt}"
                # 列表首列用**中文显示名**（DEC-037）：设了步骤名称用它，否则用中文类型名；
                # 只有设了自定义名才在后面补 [类型]，免得「机器人运动 [机器人运动]」重复。
                if getattr(step, "post_checks", ()):
                    extra += f"   ｜判断 {len(step.post_checks)}"
                text = f"{i:02d}  {step_title(step)}"
                if step.title:
                    text += f"   [{STEP_TYPE_CN.get(step.type, step.type.value)}]"
                text += extra
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, step.id)
                # 方案/检测名可能很长，列表里会被裁掉；悬停看全名（不改数据，只改显示）
                item.setToolTip(text.strip())
                self.step_list.addItem(item)
            self.step_hint.setVisible(not steps)
            self.step_list.setVisible(bool(steps))
            self._select_row(self.step_list, self._sel_step)
            if self.step_list.currentItem() is None and steps:
                self.step_list.setCurrentRow(0)
            cur = self.step_list.currentItem()
            self._sel_step = cur.data(Qt.ItemDataRole.UserRole) if cur else None

        @staticmethod
        def _select_row(lst, ident) -> None:
            if ident is None:
                return
            for row in range(lst.count()):
                if lst.item(row).data(Qt.ItemDataRole.UserRole) == ident:
                    lst.setCurrentRow(row)
                    return

        # ------------------------------------------------------------- 属性栏
        def _clear_layout(self, layout) -> None:
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
                elif item.layout() is not None:
                    self._clear_layout(item.layout())

        def _rebuild_props(self) -> None:  # pragma: no cover - Qt
            self._clear_layout(self.prop_lay)
            model = self._model()
            if model is None:
                self.prop_lay.addWidget(EmptyHint("没有可用执行方案；点「新建」建一条草稿。"))
                self.prop_lay.addStretch(1)
                return
            self.prop_lay.addWidget(self._recipe_group(model))
            step = self._current_step()
            self.prop_lay.addWidget(self._step_group(step))
            if step is not None:
                # DEC-066：判断属性是独立整块分组，放在「步骤属性」整体之后
                # （此前挂在步骤属性的 QFormLayout 里，只占字段列、被挤窄）。
                self.prop_lay.addWidget(self._post_checks_group(step))
            self.prop_lay.addStretch(1)

        def _recipe_group(self, model) -> QWidget:
            g = QGroupBox("执行方案属性")
            form = QFormLayout(g)
            # DEC-066：属性栏宽度固定，行放不下时标签换行到字段上方，不横向撑宽
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.addRow("执行方案 ID", QLabel(model.recipe_id))
            ver = QLineEdit(model.version)
            ver.editingFinished.connect(lambda: self._edit(
                lambda: self.editor.set_version(ver.text())))
            form.addRow("版本", ver)
            offline = QCheckBox("离线示例（不得用于真实生产发布）")
            offline.setChecked(model.is_offline_draft)
            offline.toggled.connect(lambda v: self._edit(
                lambda: self.editor.set_offline_draft(v)))
            form.addRow("", offline)
            partial = QCheckBox("允许部分完成（allow_partial）")
            partial.setChecked(model.allow_partial)
            partial.toggled.connect(lambda v: self._edit(
                lambda: self.editor.set_allow_partial(v)))
            form.addRow("", partial)
            revs = self.editor.history()
            form.addRow("已发布版本", QLabel(", ".join(revs) if revs else "（无）"))
            return g

        def _step_group(self, step) -> QWidget:
            if step is None:
                g = QGroupBox("步骤属性")
                lay = QVBoxLayout(g)
                lay.addWidget(EmptyHint("在上面的步骤列表里选一步来编辑它的属性。"))
                return g
            cn = STEP_TYPE_CN.get(step.type, step.type.value)
            heading = (f"步骤属性 — {step.title}（{cn} · {step.id}）"
                       if step.title else f"步骤属性 — {cn}（{step.id}）")
            g = QGroupBox(heading)
            form = QFormLayout(g)
            # DEC-066：固定宽度下放不下的行，标签换行到字段上方
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

            # 中文显示名（DEC-037）：只影响界面展示（步骤列表/作业台执行步骤），可留空。
            name = QLineEdit(step.title or "")
            name.setPlaceholderText(f"（留空：显示「{cn}」）")
            name.setToolTip("步骤名称只用于界面展示（执行方案页列表与作业台执行步骤），"
                            "不参与引用与执行；留空时显示中文类型名。")
            name.editingFinished.connect(lambda: self._edit(
                lambda: self.editor.set_step_title(step.id, name.text())))
            form.addRow("步骤名称", name)

            if step.type is NodeType.ROBOT_MOVE_JOINT:
                tgt = _fit_combo(QComboBox())
                tgt.addItem("（未设目标）", None)
                for jt in self.editor.joint_targets():
                    tgt.addItem(jt, jt)
                idx = tgt.findData(step.joint_target_ref)
                tgt.setCurrentIndex(idx if idx >= 0 else 0)
                tgt.currentIndexChanged.connect(lambda _i, c=tgt: self._edit(
                    lambda: self.editor.set_step_joint_target(step.id, c.currentData())))
                form.addRow("关节目标", tgt)
                plan = _fit_combo(QComboBox())
                plan.addItem("（不绑定：直接用上面的关节目标）", None)
                for pid in self.editor.motion_plans():
                    plan.addItem(pid, pid)
                idx = plan.findData(step.motion_plan_ref)
                plan.setCurrentIndex(idx if idx >= 0 else 0)
                plan.currentIndexChanged.connect(lambda _i, c=plan: self._edit(
                    lambda: self.editor.set_motion_plan(step.id, c.currentData())))
                form.addRow("运动方案（可选）", plan)
                form.addRow("", _wrap_note(
                    "绑了运动方案就按方案回放；不绑则直接用上面的关节目标"
                    "（引用已登记的示教点）。方案里可含套筒偏移路点"
                    "（带符号 mm：+ 进入/前进、− 脱离/后退）——按方案顺序混排执行。"))

            elif step.type is NodeType.ROBOT_MOVE_BY_BOLT_SIZE:
                # DEC-155：按全局参数「螺栓直径」选运动方案路点（1 起）
                plan = _fit_combo(QComboBox())
                plan.addItem("（请选择运动方案）", None)
                for pid in self.editor.motion_plans():
                    plan.addItem(pid, pid)
                idx = plan.findData(step.motion_plan_ref)
                plan.setCurrentIndex(idx if idx >= 0 else 0)
                plan.currentIndexChanged.connect(lambda _i, c=plan: self._edit(
                    lambda: self.editor.set_motion_plan(step.id, c.currentData())))
                form.addRow("运动方案", plan)

                mc = getattr(getattr(self.ctx, "stack", None), "motion_config", None)
                plan_obj = (mc.config.find_plan(step.motion_plan_ref)
                            if mc is not None and step.motion_plan_ref else None)
                waypoints = list(getattr(plan_obj, "waypoints", None) or [])

                def _wp_label(i: int, wp) -> str:
                    pos = "，".join(f"{float(v):g}" for v in (wp.pos or [])[:3])
                    name = str(getattr(wp, "note", "") or "").strip()
                    head = f"第 {i} 点" + (f"「{name}」" if name else "")
                    return f"{head}（{wp.mode}，{pos}）"

                def _wp_combo(current, *, allow_none: bool, none_text: str,
                              on_change):
                    c = _fit_combo(QComboBox(), chars=26)
                    if allow_none:
                        c.addItem(none_text, None)
                    for i, wp in enumerate(waypoints, 1):
                        c.addItem(_wp_label(i, wp), i)
                    idx = c.findData(int(current) if current else None)
                    c.setCurrentIndex(idx if idx >= 0 else 0)
                    c.currentIndexChanged.connect(
                        lambda _i, cc=c: on_change(cc.currentData()))
                    return c

                def _set_via(value):
                    self._edit(lambda: self.editor.set_step_field(
                        step.id, "bolt_size_via_waypoint",
                        int(value) if value else None))

                def _set_waypoint(size_key, value):
                    cur = next((st for st in self.editor.model().sequence
                                if st.id == step.id), None)
                    mapping = dict(getattr(cur, "bolt_size_waypoints", None) or {})
                    if value:
                        mapping[str(size_key)] = int(value)
                    else:
                        mapping.pop(str(size_key), None)
                    self._edit(lambda: self.editor.set_step_field(
                        step.id, "bolt_size_waypoints", mapping))

                if not waypoints:
                    form.addRow("", _wrap_note(
                        "选中的运动方案没有路点（或尚未选择方案）——先到「运动方案」页"
                        "把螺栓一/二/三的位置登记成路点。"))
                form.addRow("先到路点（可选）", _wp_combo(
                    step.bolt_size_via_waypoint, allow_none=True,
                    none_text="不设置（直接去目标路点）", on_change=_set_via))
                mapping = dict(step.bolt_size_waypoints or {})
                for size_key in ("16", "20", "24"):
                    form.addRow(f"直径 {size_key}mm → 路点", _wp_combo(
                        mapping.get(size_key), allow_none=True,
                        none_text="（未配置）",
                        on_change=lambda v, k=size_key: _set_waypoint(k, v)))
                form.addRow("", _wrap_note(
                    "下拉里就是所选运动方案的路点：第 1 点 = 「运动方案」页该方案名单里"
                    "的第 1 个点。一般把螺栓一/二/三的位置登记为前三个路点，这里就分别选"
                    "「16→螺栓一」「20→螺栓二」「24→螺栓三」；「先到路点」选先固定去的"
                    "那个（如螺栓二）。运行时读全局参数的螺栓直径（就近取档），"
                    "先走先到路点，再只走所选目标路点。"))

            elif step.type is NodeType.CAMERA_CAPTURE:
                combo = self._combo(self.editor.cameras(), step.camera_ref)
                combo.currentIndexChanged.connect(lambda _i, c=combo: self._edit(
                    lambda: self.editor.set_step_field(step.id, "camera_ref", c.currentData())))
                form.addRow("相机", combo)

            elif step.type is NodeType.VISION_RUN:
                form.addRow("输入帧来源", self._input_step_combo(step))
                form.addRow("检测来源", self._detection_combo(step))

            elif step.type is NodeType.GUIDED_POSITION:
                form.addRow("相机（引导定位）", self._guide_camera_combo(step))
                live_note = self._guide_camera_live_note(step)
                if live_note:
                    note_lbl = QLabel(live_note)
                    note_lbl.setWordWrap(True)
                    note_lbl.setObjectName("SubLabel")
                    form.addRow("", note_lbl)
                form.addRow("段选择", self._guide_stages_combo(step))
                form.addRow("速度(%)", self._guide_speed_spin(step))
                form.addRow("偏移上限(mm)", self._guide_limit_spin(step))
                form.addRow("精定位参数", self._fine_profile_combo(step))
                hint = QLabel(
                    "按段依次执行：每段检测出偏差后，运行页弹出「人工确认」，"
                    "确认才按 Bolt 工具系 [X, 0, Z] 偏移；不确认 / 检测失败 / 偏移超上限 "
                    "→ 步骤失败，绝不自动继续。速度 0 = 跟随引导配置；上限 0 = 不设上限"
                    "（与手动「引导定位」按钮一致）。")
                hint.setWordWrap(True)
                hint.setObjectName("SubLabel")
                form.addRow("", hint)

            elif step.type is NodeType.MECHANISM_ACTION:
                combo = self._combo(self.editor.actions(), step.action_ref)
                combo.currentIndexChanged.connect(lambda _i, c=combo: self._edit(
                    lambda: self.editor.set_step_field(step.id, "action_ref", c.currentData())))
                form.addRow("动作", combo)

            elif step.type is NodeType.CUSTOM_CALL:
                form.addRow("自定义方法", self._method_combo(step))
                form.addRow("设备（可多选）", self._devices_multi(step))
                form.addRow("安全边界", self._method_bounds_label(step))

            elif step.type is NodeType.WAIT_STATE:
                dev = self._combo(self.editor.devices(), step.device_ref)
                dev.currentIndexChanged.connect(lambda _i, c=dev: self._edit(
                    lambda: self.editor.set_step_field(step.id, "device_ref", c.currentData())))
                form.addRow("设备", dev)
                field = QLineEdit(step.field_name or "")
                field.editingFinished.connect(lambda: self._edit(
                    lambda: self.editor.set_step_field(step.id, "field_name", field.text())))
                form.addRow("字段", field)
                expected = QLineEdit(str(step.expected if step.expected is not None else ""))
                expected.editingFinished.connect(lambda: self._edit(
                    lambda: self.editor.set_step_field(step.id, "expected", expected.text())))
                form.addRow("期望值", expected)
                form.addRow("持续窗(次)", self._spin(step.stable_ticks, 1, 999, step.id,
                                                  "stable_ticks"))
                form.addRow("超时(ms)", self._spin(step.deadline_ms or 5000, 1, 600000,
                                                 step.id, "deadline_ms"))

            elif step.type is NodeType.CONDITION:
                form.addRow("判定步骤", self._predicate_step_combo(step))
                form.addRow("说明", _wrap_note("条件字段/比较值请在下方的 JSON 里改；"
                                              "校验会检查引用的步骤是否存在。"))
                form.addRow("on_false 策略", QLabel(str((step.on_false or {}).get("policy", "-"))))

            elif step.type is NodeType.OPERATOR_CONFIRM:
                opts = QLineEdit(", ".join(step.operator_options))
                opts.editingFinished.connect(lambda: self._edit(
                    lambda: self.editor.set_step_field(
                        step.id, "operator_options",
                        [o.strip() for o in opts.text().split(",") if o.strip()])))
                form.addRow("允许选项", opts)

            elif step.type is NodeType.RECORD_EVIDENCE:
                refs = QLineEdit(", ".join(step.inputs))
                refs.editingFinished.connect(lambda: self._edit(
                    lambda: self.editor.set_step_field(
                        step.id, "inputs",
                        [r.strip() for r in refs.text().split(",") if r.strip()])))
                form.addRow("证据步骤", refs)

            elif step.type in (NodeType.SUBFLOW, NodeType.REPEAT):
                combo = self._combo(self.editor.subflows(), step.subflow_ref)
                combo.currentIndexChanged.connect(lambda _i, c=combo: self._edit(
                    lambda: self.editor.set_step_field(step.id, "subflow_ref", c.currentData())))
                form.addRow("子流程模板", combo)
                if step.type is NodeType.REPEAT:
                    form.addRow("重复次数", self._spin(step.max_iterations or 3, 1, 1000,
                                                    step.id, "max_iterations"))

            elif step.type is NodeType.PARALLEL_GROUP:
                # DEC-096：并行执行步骤组 = 机器人运动 ∥ 机构动作，两个分支同时开始。
                robot = next((b for b in step.branches
                              if b.type is NodeType.ROBOT_MOVE_JOINT), None)
                mech = next((b for b in step.branches
                             if b.type is NodeType.MECHANISM_ACTION), None)
                if robot is not None:
                    tgt = _fit_combo(QComboBox())
                    tgt.addItem("（未设目标）", None)
                    for jt in self.editor.joint_targets():
                        tgt.addItem(jt, jt)
                    idx = tgt.findData(robot.joint_target_ref)
                    tgt.setCurrentIndex(idx if idx >= 0 else 0)
                    tgt.currentIndexChanged.connect(lambda _i, c=tgt: self._edit(
                        lambda: self.editor.set_parallel_branch_field(
                            step.id, NodeType.ROBOT_MOVE_JOINT,
                            "joint_target_ref", c.currentData())))
                    form.addRow("机器人分支：关节目标", tgt)
                    plan = _fit_combo(QComboBox())
                    plan.addItem("（不绑定：直接用上面的关节目标）", None)
                    for pid in self.editor.motion_plans():
                        plan.addItem(pid, pid)
                    idx = plan.findData(robot.motion_plan_ref)
                    plan.setCurrentIndex(idx if idx >= 0 else 0)
                    plan.currentIndexChanged.connect(lambda _i, c=plan: self._edit(
                        lambda: self.editor.set_parallel_branch_field(
                            step.id, NodeType.ROBOT_MOVE_JOINT,
                            "motion_plan_ref", c.currentData())))
                    form.addRow("机器人分支：运动方案", plan)
                if mech is not None:
                    combo = self._combo(self.editor.actions(), mech.action_ref)
                    combo.currentIndexChanged.connect(lambda _i, c=combo: self._edit(
                        lambda: self.editor.set_parallel_branch_field(
                            step.id, NodeType.MECHANISM_ACTION,
                            "action_ref", c.currentData())))
                    form.addRow("机构分支：动作", combo)
                form.addRow("", _wrap_note(
                    "两个分支同时开始：机器人按示教点/运动方案运动，机构（控制板）"
                    "同时执行所选动作。两个分支都成功，本步骤才算完成（整组只弹一次"
                    "步骤确认）；任一分失败 → 整组失败，并停止另一分支。首版只支持"
                    "「机器人运动 + 机构动作」两类分支。"))

            return g

        # ---------------------------------------------------- 步骤后判断属性
        _CHECK_KIND_LABELS = {
            "robot_pose": "机械臂末端位姿",
            "device_field": "设备遥测字段",
            "step_output": "前序步骤输出",
        }
        _CHECK_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "in")

        def _post_checks_group(self, step) -> QWidget:
            g = QGroupBox("判断属性（步骤完成后，可选）")
            lay = QVBoxLayout(g)
            lay.setSpacing(4)
            checks = [c.model_dump(mode="json")
                      for c in (getattr(step, "post_checks", ()) or ())]
            if not checks:
                lay.addWidget(QLabel("未设置：步骤完成即进入下一步。"))
            for idx, chk in enumerate(checks):
                lay.addWidget(self._check_row(step, idx, chk))
            add = QHBoxLayout()
            kind = _fit_combo(QComboBox(), chars=14)
            for key, label in self._CHECK_KIND_LABELS.items():
                kind.addItem(label, key)
            add.addWidget(kind, 1)
            btn = QPushButton("添加判断")
            btn.setObjectName("Ghost")

            def _add() -> None:
                new = checks + [self._default_check(kind.currentData())]
                self._save_checks(step, new)
            btn.clicked.connect(_add)
            add.addWidget(btn)
            lay.addLayout(add)
            note = QLabel("全部判断通过才进入下一步；不通过停在安全边界（暂停），"
                          "人工处理后可继续，会重新判断。末端位姿的期望值取本次运行"
                          "运动到该示教点时的实测位姿。")
            note.setWordWrap(True)
            note.setObjectName("SubLabel")
            lay.addWidget(note)
            return g

        @staticmethod
        def _default_check(kind: str) -> dict:
            if kind == "robot_pose":
                return {"kind": "robot_pose", "joint_target_ref": None,
                        "pos_tol_mm": 2.0, "check_rotation": False,
                        "rot_tol_deg": 2.0}
            if kind == "device_field":
                return {"kind": "device_field", "device_ref": None,
                        "field_name": "", "op": "eq", "value": ""}
            return {"kind": "step_output", "source_step": None,
                    "field_name": "", "op": "eq", "value": ""}

        @staticmethod
        def _value_text(value) -> str:
            if isinstance(value, str):
                return value
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                return str(value)

        @staticmethod
        def _parse_value(text: str):
            t = (text or "").strip()
            if t == "":
                return ""
            try:
                if t[0] in '[{"' or t in ("true", "false", "null") or \
                        (t.lstrip("-").replace(".", "", 1).isdigit()):
                    return json.loads(t)
            except Exception:  # noqa: BLE001
                pass
            return t

        def _save_checks(self, step, checks: list[dict]) -> None:
            self._edit(lambda: self.editor.set_step_checks(step.id, checks))

        def _update_check(self, step, idx: int, checks: list[dict],
                          **changes) -> None:
            checks = [dict(c) for c in checks]
            checks[idx].update(changes)
            self._save_checks(step, checks)

        def _check_row(self, step, idx: int, chk: dict) -> QWidget:
            checks = [c.model_dump(mode="json")
                      for c in (getattr(step, "post_checks", ()) or ())]
            box = QFrame()
            box.setObjectName("BlockBox")
            lay = QVBoxLayout(box)
            lay.setContentsMargins(8, 6, 8, 6)
            lay.setSpacing(4)
            head = QHBoxLayout()
            kind = _fit_combo(QComboBox(), chars=14)
            for key, label in self._CHECK_KIND_LABELS.items():
                kind.addItem(label, key)
            kind.setCurrentIndex(max(0, kind.findData(chk.get("kind"))))
            kind.currentIndexChanged.connect(
                lambda _i, k=kind: self._change_check_kind(step, idx, k.currentData()))
            head.addWidget(kind, 1)
            delete = QPushButton("删除")
            delete.setObjectName("Ghost")
            delete.clicked.connect(lambda: self._save_checks(
                step, [c for j, c in enumerate(checks) if j != idx]))
            head.addWidget(delete)
            lay.addLayout(head)

            kind_key = str(chk.get("kind") or "")
            form = QFormLayout()
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            if kind_key == "robot_pose":
                ref = _fit_combo(QComboBox())
                ref.addItem("（未选示教点）", None)
                for t in self.editor.joint_targets():
                    ref.addItem(t, t)
                ref.setCurrentIndex(max(0, ref.findData(chk.get("joint_target_ref"))))
                ref.currentIndexChanged.connect(lambda _i, c=ref: self._update_check(
                    step, idx, checks, joint_target_ref=c.currentData()))
                form.addRow("示教点", ref)
                pos = QDoubleSpinBox()
                pos.setRange(0.0, 10000.0); pos.setDecimals(2); pos.setSuffix(" mm")
                pos.setValue(float(chk.get("pos_tol_mm") or 2.0))
                pos.editingFinished.connect(lambda p=pos: self._update_check(
                    step, idx, checks, pos_tol_mm=float(p.value())))
                form.addRow("位置容差", pos)
                rot_on = QCheckBox("同时判断姿态")
                rot_on.setChecked(bool(chk.get("check_rotation")))
                rot_on.toggled.connect(lambda v: self._update_check(
                    step, idx, checks, check_rotation=bool(v)))
                form.addRow("", rot_on)
                rot = QDoubleSpinBox()
                rot.setRange(0.0, 180.0); rot.setDecimals(2); rot.setSuffix(" °")
                rot.setValue(float(chk.get("rot_tol_deg") or 2.0))
                rot.setEnabled(bool(chk.get("check_rotation")))
                rot.editingFinished.connect(lambda r=rot: self._update_check(
                    step, idx, checks, rot_tol_deg=float(r.value())))
                form.addRow("姿态容差", rot)
            elif kind_key == "device_field":
                dev = _fit_combo(QComboBox())
                dev.addItem("（未选设备）", None)
                for d in self.editor.devices():
                    dev.addItem(d, d)
                dev.setCurrentIndex(max(0, dev.findData(chk.get("device_ref"))))
                dev.currentIndexChanged.connect(lambda _i, c=dev: self._update_check(
                    step, idx, checks, device_ref=c.currentData()))
                form.addRow("设备", dev)
                form.addRow("字段", self._check_text_field(
                    step, idx, checks, chk, "field_name"))
                form.addRow("比较", self._check_op_combo(step, idx, checks, chk))
                form.addRow("期望值", self._check_value_field(step, idx, checks, chk))
            elif kind_key == "step_output":
                src = _fit_combo(QComboBox())
                src.addItem("（未选步骤）", None)
                prior = self._prior_step_ids(step)
                for sid in prior:
                    src.addItem(sid, sid)
                src.setCurrentIndex(max(0, src.findData(chk.get("source_step"))))
                src.currentIndexChanged.connect(lambda _i, c=src: self._update_check(
                    step, idx, checks, source_step=c.currentData()))
                form.addRow("前序步骤", src)
                form.addRow("字段", self._check_text_field(
                    step, idx, checks, chk, "field_name"))
                form.addRow("比较", self._check_op_combo(step, idx, checks, chk))
                form.addRow("期望值", self._check_value_field(step, idx, checks, chk))
            lay.addLayout(form)
            return box

        def _change_check_kind(self, step, idx: int, kind: str) -> None:
            checks = [c.model_dump(mode="json")
                      for c in (getattr(step, "post_checks", ()) or ())]
            checks[idx] = self._default_check(kind)
            self._save_checks(step, checks)

        def _check_text_field(self, step, idx, checks, chk, field: str) -> QLineEdit:
            edit = QLineEdit(str(chk.get(field) or ""))
            edit.setPlaceholderText("字段名")
            edit.editingFinished.connect(lambda e=edit: self._update_check(
                step, idx, checks, **{field: e.text().strip()}))
            return edit

        def _check_op_combo(self, step, idx, checks, chk) -> QComboBox:
            combo = _fit_combo(QComboBox(), chars=8)
            for op in self._CHECK_OPS:
                combo.addItem(op, op)
            combo.setCurrentIndex(max(0, combo.findData(chk.get("op") or "eq")))
            combo.currentIndexChanged.connect(lambda _i, c=combo: self._update_check(
                step, idx, checks, op=c.currentData()))
            return combo

        def _check_value_field(self, step, idx, checks, chk) -> QLineEdit:
            edit = QLineEdit(self._value_text(chk.get("value")))
            edit.setPlaceholderText("值（数字/true/false/文本/[列表]）")
            edit.editingFinished.connect(lambda e=edit: self._update_check(
                step, idx, checks, value=self._parse_value(e.text())))
            return edit

        def _prior_step_ids(self, step) -> list[str]:
            model = self._model()
            if model is None:
                return []
            out = []
            for s in model.sequence:
                if s.id == step.id:
                    break
                out.append(s.id)
            return out

        def _method_combo(self, step) -> QComboBox:
            """自定义方法下拉：显示 ``id — 标题``，把 id 写回 method_ref。"""
            combo = _fit_combo(QComboBox())
            combo.addItem("（未选）", None)
            for mid in self.editor.methods():
                info = self.editor.method_info(mid)
                title = getattr(info, "title", "") if info is not None else ""
                combo.addItem(f"{mid} — {title}" if title else mid, mid)
            idx = combo.findData(step.method_ref)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.currentIndexChanged.connect(lambda _i, c=combo: self._edit(
                lambda: self.editor.set_step_method(step.id, c.currentData())))
            return combo

        def _devices_multi(self, step) -> QWidget:
            """多设备勾选：执行时对勾选的设备**同时下发、等全部完成**。"""
            box = QWidget()
            lay = QVBoxLayout(box)
            lay.setContentsMargins(0, 0, 0, 0); lay.setSpacing(2)
            devices = self.editor.devices()
            if not devices:
                lay.addWidget(QLabel("引用目录里没有设备"))
                return box
            chosen = set(step.device_refs)
            boxes: list[QCheckBox] = []

            def _apply(_checked: bool) -> None:
                refs = [b.text() for b in boxes if b.isChecked()]
                self._edit(lambda: self.editor.set_step_devices(step.id, refs))

            for dev in devices:
                cb = QCheckBox(dev); cb.setChecked(dev in chosen)
                cb.toggled.connect(_apply)
                boxes.append(cb); lay.addWidget(cb)
            return box

        def _method_bounds_label(self, step) -> QLabel:
            """如实展示所选方法的安全边界（与方法库声明一致，不美化）。"""
            info = self.editor.method_info(step.method_ref) if step.method_ref else None
            if info is None:
                lack = "（未选方法）" if not step.method_ref else "（未知方法）"
                lbl = QLabel(f"{lack} 安全边界由方法库声明。")
                lbl.setWordWrap(True)
                return lbl
            bits = [info.device_hint(),
                    "需运动" if info.needs_motion else "不运动",
                    "需控制板" if info.needs_board else "不需要控制板",
                    "可离线" if info.offline_ok else "仅真实设备"]
            if info.requires_confirm:
                bits.append("需人工确认")
            text = "；".join(bits)
            if info.description:
                text += f"\n{info.description}"
            lbl = QLabel(text); lbl.setWordWrap(True)
            return lbl

        def _combo(self, options: list[str], current: str | None) -> QComboBox:
            combo = _fit_combo(QComboBox())
            combo.addItem("（未选）", None)
            for opt in options:
                combo.addItem(opt, opt)
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            return combo

        def _input_step_combo(self, step) -> QComboBox:
            """输入帧来源：只能引**本配方里**的步骤（在它之前的更合理，但不强制）。"""
            combo = _fit_combo(QComboBox())
            combo.addItem("（未选）", None)
            prev = (step.input_from or {}).get("step")
            for other in self.editor.model.sequence:
                if other.id == step.id:
                    continue
                combo.addItem(other.id, other.id)
            idx = combo.findData(prev)
            combo.setCurrentIndex(idx if idx >= 0 else 0)

            def _apply(_i: int) -> None:
                sid = combo.currentData()
                self._edit(lambda: self.editor.set_step_field(
                    step.id, "input_from",
                    {"step": sid, "output": "frame"} if sid else None))
            combo.currentIndexChanged.connect(_apply)
            return combo

        def _predicate_step_combo(self, step) -> QComboBox:
            combo = _fit_combo(QComboBox())
            combo.addItem("（未选）", None)
            cur = getattr(step.predicate, "source", None)
            for other in self.editor.model.sequence:
                if other.id == step.id:
                    continue
                combo.addItem(other.id, other.id)
            idx = combo.findData(getattr(cur, "step", None))
            combo.setCurrentIndex(idx if idx >= 0 else 0)

            def _apply(_i: int) -> None:
                sid = combo.currentData()
                if not sid:
                    return
                field = getattr(cur, "field", "status") or "status"
                value = step.predicate.value if step.predicate else "PASS"
                self._edit(lambda: self.editor.set_step_field(step.id, "predicate", {
                    "source": {"step": sid, "field": field},
                    "op": (step.predicate.op.value if step.predicate and step.predicate.op
                           else "eq"),
                    "value": value}))
            combo.currentIndexChanged.connect(_apply)
            return combo

        def _detection_combo(self, step) -> QComboBox:
            """检测来源：流水线 或 命名视觉检测（二选一）。"""
            combo = _fit_combo(QComboBox())
            combo.addItem("（未选）", ("none", None))
            for pid in self.editor.pipelines():
                combo.addItem(_PIPE_PREFIX + pid, ("pipeline", pid))
            for det, pipe in self.editor.detections():
                combo.addItem(f"{_DET_PREFIX}{det}（← {pipe}）", ("detection", det))
            want = (("detection", step.detection_ref) if step.detection_ref
                    else ("pipeline", step.pipeline_ref) if step.pipeline_ref
                    else ("none", None))
            idx = next((i for i in range(combo.count())
                        if combo.itemData(i) == want), 0)
            combo.setCurrentIndex(idx)

            def _apply(_i: int) -> None:
                kind, value = combo.currentData()
                if kind == "pipeline":
                    self._edit(lambda: self.editor.set_pipeline(step.id, value))
                elif kind == "detection":
                    self._edit(lambda: self.editor.set_detection(step.id, value))
                else:
                    self._edit(lambda: self.editor.set_pipeline(step.id, None))
            combo.currentIndexChanged.connect(_apply)
            return combo

        def _spin(self, value: int, lo: int, hi: int, step_id: str, field_name: str) -> QSpinBox:
            spin = QSpinBox(); spin.setRange(lo, hi); spin.setValue(int(value))
            spin.valueChanged.connect(lambda v: self._edit(
                lambda: self.editor.set_step_field(step_id, field_name, v)))
            return spin

        # ---------------- 引导定位 (vision.guided_position) ----------------
        _GUIDE_STAGE_CHOICES = (
            ("粗 → 精（默认）", ["coarse", "fine"]),
            ("仅粗", ["coarse"]),
            ("仅精", ["fine"]),
        )

        def _guide_camera_combo(self, step) -> QComboBox:
            """DEC-137：引导定位可选**任意已启用相机**（默认 ds86_01，可用 D405 等）。

            目录里没有的引用也列出来并标注「未启用」，让用户看得见、改得掉；
            排序把 DS86 放最前（现场默认）；目录为空时保留 ds86_01 占位项。
            """
            combo = _fit_combo(QComboBox())
            cams = sorted(set(self.editor.cameras()),
                          key=lambda c: (c != GUIDANCE_CAMERA_ID, c))
            for cid in cams:
                combo.addItem(cid, cid)
            if not cams:
                combo.addItem(f"{GUIDANCE_CAMERA_ID}（设备配置里未启用）",
                              GUIDANCE_CAMERA_ID)
            if step.camera_ref and step.camera_ref not in cams:
                combo.addItem(f"{step.camera_ref}（设备配置里未启用）", step.camera_ref)
            idx = combo.findData(step.camera_ref)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.currentIndexChanged.connect(lambda _i, c=combo: self._edit(
                lambda: self.editor.set_step_field(step.id, "camera_ref", c.currentData())))
            return combo

        def _guide_camera_live_note(self, step) -> str:
            """本次运行没打开所选相机时提示可编辑但运行会失败（运行时如实报，不凑数）。"""
            live = getattr(getattr(self.ctx, "stack", None), "cameras", None)
            cam = (getattr(step, "camera_ref", "") or GUIDANCE_CAMERA_ID)
            if isinstance(live, dict) and cam not in live:
                return (f"本次运行未打开相机 {cam}（原因见「设备配置」页的设备状态）；"
                        "执行方案可编辑/保存，但运行时该步骤会如实失败，"
                        "不会拿其它相机的帧凑数。")
            return ""

        def _guide_stages_combo(self, step) -> QComboBox:
            """段选择固定三种：粗→精 / 仅粗 / 仅精（顺序永远粗在前，执行器再兜底排序）。"""
            combo = _fit_combo(QComboBox())
            for label, stages in self._GUIDE_STAGE_CHOICES:
                combo.addItem(label, stages)
            want = list(step.guide_stages or ())
            idx = next((i for i in range(combo.count())
                        if list(combo.itemData(i)) == want), 0)
            combo.setCurrentIndex(idx)
            combo.currentIndexChanged.connect(lambda _i, c=combo: self._edit(
                lambda: self.editor.set_step_field(
                    step.id, "guide_stages", list(c.currentData()))))
            return combo

        def _guide_speed_spin(self, step) -> QSpinBox:
            """速度 1~100%；0 表示跟随引导配置（guide_move_speed，默认 10%）。"""
            spin = QSpinBox(); spin.setRange(0, 100)
            spin.setSuffix(" %"); spin.setSpecialValueText("跟随配置")
            spin.setValue(int(step.guide_speed_pct or 0))
            spin.valueChanged.connect(lambda v: self._edit(
                lambda: self.editor.set_step_field(
                    step.id, "guide_speed_pct", int(v) if v > 0 else None)))
            return spin

        def _guide_limit_spin(self, step) -> QDoubleSpinBox:
            """偏移上限 mm（|X| 与 |Z| 各自判）；0 = 不设上限。确认**之前**就已判超限。"""
            spin = QDoubleSpinBox(); spin.setRange(0.0, 200.0)
            spin.setDecimals(1); spin.setSingleStep(0.5)
            spin.setSuffix(" mm"); spin.setSpecialValueText("不设上限")
            spin.setValue(float(step.guide_max_offset_mm or 0.0))
            spin.valueChanged.connect(lambda v: self._edit(
                lambda: self.editor.set_step_field(
                    step.id, "guide_max_offset_mm", float(v))))
            return spin

        def _fine_profile_combo(self, step) -> QComboBox:
            """精定位参数套（DEC-065）：空=默认；其余来自「参数配置」页。"""
            combo = _fit_combo(QComboBox())
            combo.addItem("（默认：沿用精定位偏移参数）", None)
            for pid in self.editor.fine_profiles():
                combo.addItem(pid, pid)
            cur = getattr(step, "fine_profile_ref", None)
            idx = combo.findData(cur)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.currentIndexChanged.connect(lambda _i, c=combo: self._edit(
                lambda: self.editor.set_step_field(
                    step.id, "fine_profile_ref", c.currentData())))
            combo.setToolTip("该引导定位步骤的精定位偏移用哪一套参数（在「参数配置」页维护）")
            return combo

        # ------------------------------------------------------------- 选中项
        def _model(self):  # pragma: no cover - Qt
            try:
                return self.editor.model
            except Exception:  # noqa: BLE001
                return None

        def _current_step(self):  # pragma: no cover - Qt
            model = self._model()
            if model is None or not self._sel_step:
                return None
            return next((s for s in model.sequence if s.id == self._sel_step), None)

        def _on_recipe_changed(self, _index: int) -> None:  # pragma: no cover - Qt
            if self._building:
                return
            rid = self.recipe_combo.currentData()
            self.editor.select(rid)
            self._sel_step = None
            vm = getattr(self.ctx, "vm", None)
            if rid and vm is not None and hasattr(vm, "select_recipe"):
                vm.select_recipe(rid)
            self._reload()

        def _on_step_selected(self) -> None:  # pragma: no cover - Qt
            if self._building:
                return
            cur = self.step_list.currentItem()
            self._sel_step = cur.data(Qt.ItemDataRole.UserRole) if cur else None
            self._building = True
            try:
                self._rebuild_props()
            finally:
                self._building = False

        # ------------------------------------------------------------- 编辑动作
        def _edit(self, fn) -> None:  # pragma: no cover - Qt
            """执行一次改动 -> 重建界面 -> 立刻重跑校验。改动失败要说出来。"""
            if self._building:
                return
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - pydantic 校验错也要让人看见
                _warn(self, "改动未保存", str(exc))
            self._reload()

        def _new_recipe(self) -> None:  # pragma: no cover - Qt
            rid, ok = _ask_text(self, "新建执行方案", "执行方案 ID：", text="recipe_new")
            if not ok or not rid.strip():
                return
            try:
                self.editor.new_recipe(rid.strip())
            except Exception as exc:  # noqa: BLE001
                _warn(self, "新建失败", str(exc))
            self._sel_step = None
            self._reload()

        def _copy_recipe(self) -> None:  # pragma: no cover - Qt
            src = self.editor.recipe_id
            if not src:
                _info(self, "复制", "先选一条执行方案")
                return
            rid, ok = _ask_text(self, "复制执行方案", "新执行方案 ID：", text=f"{src}_copy")
            if not ok or not rid.strip():
                return
            try:
                self.editor.duplicate(src, rid.strip())
            except Exception as exc:  # noqa: BLE001
                _warn(self, "复制失败", str(exc))
            self._sel_step = None
            self._reload()

        def _delete_recipe(self) -> None:  # pragma: no cover - Qt
            """删除整条执行方案（DEC-108）：先确认；运行中的方案拒绝删除。"""
            rid = self.editor.recipe_id
            if not rid:
                _info(self, "删除执行方案", "先选一条执行方案")
                return
            vm = getattr(self.ctx, "vm", None)
            if vm is not None and vm.recipe_in_use(rid):
                _warn(self, "不能删除",
                      f"执行方案 {rid!r} 正在执行中，请先终止任务再删除。")
                return
            src = self.editor.source_path()
            extra = (f"\n来源文件：{src}\n将改名为 .deleted-<时间>.bak（可手工恢复）。"
                     if src else "\n（该执行方案尚未落盘，仅从列表移除。）")
            if not _confirm(self, "删除执行方案",
                            f"确认删除执行方案 {rid!r} ?{extra}"):
                return
            try:
                info = self.editor.delete_recipe(rid)
            except Exception as exc:  # noqa: BLE001
                _warn(self, "删除失败", str(exc))
                return
            if vm is not None:
                vm.forget_recipe(rid)
            self._sel_step = None
            self._reload()
            back = info.get("backup")
            self.result_label.setText(
                f"已删除执行方案 {rid}"
                + (f"（文件已备份为 {back}）" if back else "（未落盘的草稿）"))

        def _save_now(self) -> None:  # pragma: no cover - Qt
            """「保存」：不弹框，直接覆盖**当前配方对应的文件**（DEC-035）。

            来源 = 载入时的 `--recipe` 文件 / 之前「另存为」的位置；新建且没落过盘的
            草稿存到配方目录 ``configs/recipes/<配方ID>.yaml``。下次启动自动进下拉。
            """
            rid = self.editor.recipe_id
            if not rid:
                _info(self, "保存", "先选一条执行方案")
                return
            path = self.editor.source_path() or os.path.join(_recipes_dir(), f"{rid}.yaml")
            try:
                self.editor.save(path)
            except Exception as exc:  # noqa: BLE001
                _warn(self, "保存失败", str(exc))
                return
            self.result_label.setText(f"已保存到 {path}（原子写，保留 .bak）")

        def _save_as(self) -> None:  # pragma: no cover - Qt
            """「另存为」：弹路径对话框，存到哪就以哪为新的来源文件。"""
            rid = self.editor.recipe_id
            if not rid:
                _info(self, "另存为", "先选一条执行方案")
                return
            default = (self.editor.source_path()
                       or os.path.join(_recipes_dir(), f"{rid}.yaml"))
            path, ok = _ask_text(self, "另存为", "保存到文件：", text=default)
            if not ok or not path.strip():
                return
            try:
                self.editor.save(path.strip())
            except Exception as exc:  # noqa: BLE001
                _warn(self, "保存失败", str(exc))
                return
            self.result_label.setText(f"已保存到 {path.strip()}（原子写，保留 .bak）")

        def _add_step(self) -> None:  # pragma: no cover - Qt
            labels = [STEP_TYPE_LABELS[t] for t in STEP_TYPE_LABELS]
            label, ok = _ask_item(self, "添加步骤", "步骤类型：", labels)
            if not ok or not label:
                return
            ntype = next(t for t, text in STEP_TYPE_LABELS.items() if text == label)
            try:
                sid = self.editor.add_step(ntype)
            except Exception as exc:  # noqa: BLE001
                _warn(self, "添加步骤失败", str(exc))
                return
            self._sel_step = sid
            self._reload()

        def _remove_step(self) -> None:  # pragma: no cover - Qt
            sid = self._sel_step
            if not sid:
                return
            if not _confirm(self, "删除步骤", f"确认删除步骤 {sid!r} ?"):
                return
            self.editor.remove_step(sid)
            self._sel_step = None
            self._reload()

        def _move_step(self, delta: int) -> None:  # pragma: no cover - Qt
            if self._sel_step:
                self.editor.move_step(self._sel_step, delta)
                self._reload()

        # ------------------------------------------------------------- 校验/发布
        def _validate_and_show(self) -> None:  # pragma: no cover - Qt
            self.err_list.clear()
            if not self.editor.recipe_id:
                self.result_label.setText("未选择执行方案。点「新建」建一条草稿。")
                self.btn_publish.setEnabled(False)
                self.btn_publish.setToolTip("未选择执行方案")
                return
            try:
                report = self.editor.validate()
            except Exception as exc:  # noqa: BLE001
                self.result_label.setText(f"校验失败：{exc}")
                self.btn_publish.setEnabled(False)
                return
            for entry in report.entries:
                tag = f"步骤 {entry['step_id']}" if entry.get("step_id") else "执行方案级"
                if entry.get("field_name"):
                    tag += f" · {entry['field_name']}"
                item = QListWidgetItem(f"[{entry['layer']}] {tag} — {entry['message']}")
                item.setData(Qt.ItemDataRole.UserRole, entry.get("step_id"))
                self.err_list.addItem(item)
            if report.ok:
                self.err_list.setVisible(False)
                self.result_label.setText("校验通过：结构/值/引用/能力/业务五层均无问题。")
            else:
                self.err_list.setVisible(True)
                self.result_label.setText(
                    f"校验未通过（{len(report.entries)} 条）：点下面任一条定位到步骤。")
            can, why = self.editor.can_publish()
            self.btn_publish.setEnabled(can)
            self.btn_publish.setToolTip(why)
            if not report.ok:
                return
            self.result_label.setText(f"{self.result_label.text()}  {why}")

        def _goto_error(self, item) -> None:  # pragma: no cover - Qt
            sid = item.data(Qt.ItemDataRole.UserRole)
            if not sid:
                return
            self._sel_step = sid
            self._building = True
            try:
                self._select_row(self.step_list, sid)
                self._rebuild_props()
            finally:
                self._building = False

        def _publish(self) -> None:  # pragma: no cover - Qt
            can, why = self.editor.can_publish()
            if not can:
                _warn(self, "不能发布", why)
                return
            try:
                rev = self.editor.publish()      # DEC-040: 不弹操作员, 直接发布
            except Exception as exc:  # noqa: BLE001
                _warn(self, "发布失败", str(exc))
                return
            self._reload()          # 先刷新（已发布版本行要更新），再写发布结果
            note = ""
            src = self.editor.source_path()
            if src:                 # DEC-040: 把 state: published 写回来源文件, 重启后仍放行
                try:
                    self.editor.save(src)
                except Exception as exc:  # noqa: BLE001
                    note = f"（发布已生效，但写回文件失败：{exc}）"
            self.result_label.setText(
                f"发布成功：revision {rev}。发布即生效，可直接在作业台执行。{note}")
            # DEC-041: 发布成功弹窗（用户要求"提示一个发布成功的窗口"），内容与结果栏一致
            _info(self, "发布成功",
                  f"发布成功：revision {rev}\n发布即生效，可直接在作业台执行。{note}")

        def _compare(self) -> None:  # pragma: no cover - Qt
            revs = self.editor.history()
            if len(revs) < 2:
                _info(self, "版本比较", "已发布版本不足两个，无法比较。")
                return
            a, ok = _ask_item(self, "版本比较", "版本 A：", revs)
            if not ok or not a:
                return
            b, ok = _ask_item(self, "版本比较", "版本 B：", revs)
            if not ok or not b:
                return
            try:
                diff = self.editor.compare(a, b)
            except Exception as exc:  # noqa: BLE001
                _warn(self, "版本比较失败", str(exc))
                return
            changed = diff.get("changed") or {}
            self.err_list.clear()
            self.err_list.setVisible(True)
            if not changed:
                self.result_label.setText(f"{a} 与 {b} 无字段差异。")
                return
            self.result_label.setText(f"{a} → {b} 有 {len(changed)} 处字段差异：")
            for key, pair in changed.items():
                self.err_list.addItem(QListWidgetItem(
                    f"{key}: {pair.get('a')!r} → {pair.get('b')!r}"))

        def shutdown(self) -> None:
            """本页不建定时器，无需停表；保留方法以便外壳统一调用。"""

else:  # pragma: no cover
    class RecipesPage:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6")


def _recipes_dir() -> str:
    """配方目录：`<应用根>/configs/recipes`（源码运行 = 工程根；DEC-035）。"""
    try:
        from application.bootstrap import app_paths
        return app_paths.recipes_dir()
    except Exception:  # noqa: BLE001
        return os.path.join(os.getcwd(), "configs", "recipes")


__all__ = ["GUI_REQUIRED", "RecipesPage"]
