"""手动控制页 (E18, 03 §6)。

按机构分组, 不按4块板堆满; 未核对动作显示"未绑定"且不可点;
关节区显示 JointTarget 六关节 deg; 持续动作未验证不开放; 保留动作反馈与停止入口。
"""
from __future__ import annotations

from application.presentation.components import Card, EmptyHint, KV, StatusPill

try:  # pragma: no cover
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QWidget = object  # type: ignore[assignment]


if GUI_REQUIRED:

    class ManualPage(QWidget):
        def __init__(self, ctx) -> None:
            super().__init__()
            self.ctx = ctx
            self.vm = ctx.vm
            root = QVBoxLayout(self)
            root.setContentsMargins(14, 14, 14, 14)
            root.setSpacing(10)

            top = QHBoxLayout(); top.setSpacing(10)
            self.robot_card = Card("机械臂 · ECO65")
            self.kv_conn = KV("连接/状态", "待连接")
            self.kv_joint = KV("当前关节 (deg)", "-")
            self.robot_card.add(self.kv_conn); self.robot_card.add(self.kv_joint)
            self.joint_goal = QLabel("目标关节: 未提供示教点 (INPUT-PATH 待确认)")
            self.joint_goal.setObjectName("SubLabel")
            self.joint_goal.setWordWrap(True)
            self.robot_card.add(self.joint_goal)
            btn = QPushButton("移动至目标关节 (movej)")
            btn.setObjectName("Ghost"); btn.setEnabled(False)
            self.robot_card.add(btn)
            top.addWidget(self.robot_card, 1)

            self.ctrl_card = Card("控制权")
            self.kv_ctrl = KV("当前控制权", "自动任务/空闲")
            self.ctrl_card.add(self.kv_ctrl)
            self.ctrl_note = QLabel("自动任务运行中不能抢占运动资源；需显式交接控制权")
            self.ctrl_note.setObjectName("SubLabel"); self.ctrl_note.setWordWrap(True)
            self.ctrl_card.add(self.ctrl_note)
            self.transfer_btn = QPushButton("申请控制权 (需空闲/安全暂停)")
            self.transfer_btn.setObjectName("Ghost"); self.transfer_btn.setEnabled(False)
            self.ctrl_card.add(self.transfer_btn)
            self.ctrl_card.add(EmptyHint("手动真实动作需现场放行后才启用"))
            top.addWidget(self.ctrl_card, 1)
            root.addLayout(top)

            self.mech_card = Card("机构动作 · 控制板1 (按机构分组)")
            self.mech_rows = QVBoxLayout(); self.mech_rows.setSpacing(6)
            self.mech_card.body().addLayout(self.mech_rows)
            root.addWidget(self.mech_card, 1)
            self.refresh()

        def refresh(self) -> None:  # pragma: no cover - Qt
            robot = getattr(self.ctx.stack.executor, "robot", None)
            if robot is not None:
                s = robot.read_state()
                self.kv_conn.set_key("连接/状态",
                                     f"{s.get('connection', '-')} / {s.get('operational', '-')}")
                joint = robot.read_joint()
                self.kv_joint.set_key(
                    "当前关节 (deg)",
                    "  ".join(f"{j:.1f}" for j in joint) if joint else "-")

            while self.mech_rows.count():
                it = self.mech_rows.takeAt(0)
                if it.widget():
                    it.widget().setParent(None)
            actions = sorted(getattr(self.ctx.stack.actions, "ids", lambda: [])())
            if not actions:
                self.mech_rows.addWidget(EmptyHint("无已注册动作"))
            for a in actions:
                row = QWidget(); h = QHBoxLayout(row)
                h.setContentsMargins(0, 2, 0, 2); h.setSpacing(10)
                name = QLabel(a); name.setObjectName("StepTitle")
                # INPUT-MECHANISM 未确认 -> 未绑定, 不可点 (03 §6)
                pill = StatusPill("未绑定")
                b1 = QPushButton("执行"); b1.setObjectName("Ghost"); b1.setEnabled(False)
                h.addWidget(name, 1); h.addWidget(pill); h.addWidget(b1)
                self.mech_rows.addWidget(row)
            note = QLabel("动作映射与前后反馈待 INPUT-MECHANISM 确认前一律显示“未绑定”，不能点。")
            note.setObjectName("SubLabel"); note.setWordWrap(True)
            self.mech_rows.addWidget(note)
            self.mech_rows.addStretch(1)


__all__ = ["GUI_REQUIRED", "ManualPage"]
