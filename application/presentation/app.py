"""应用外壳: MainWindow = 顶部状态栏 + 左侧导航 + 页面栈 (03 §3/§4.1)。

所有页面保留任务状态与停止入口 (顶部常驻); 页面切换不改变生产调度。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover
    from PySide6.QtCore import QDateTime, Qt, QTimer
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QPushButton,
        QStackedWidget, QVBoxLayout, QWidget,
    )
    GUI_REQUIRED = True
except Exception:  # noqa: BLE001
    GUI_REQUIRED = False
    QMainWindow = object  # type: ignore[assignment]

from application.presentation.components import StatusPill
from application.presentation.theme import THEME, qss, state_colors
from application.infrastructure import logging_setup

_log = logging_setup.get_logger("ui.main_window")

NAV_PAGES = (
    ("作业台", "workbench"),
    ("操作台", "console"),
    ("手动控制", "manual"),
    ("视觉调试", "vision"),
    ("执行方案", "recipes"),
    ("运动方案", "motion"),
    ("设备配置", "devices"),
    ("夹爪调试", "gripper"),
    ("参数配置", "settings"),
    ("日志诊断", "diagnostics"),
)

OFFLINE_BADGE = "V0.2 设计示例"
PRODUCTION_BADGE = "真实设备"

# 真实设备模式的两种使用者（DEC-098）：
# - 技术员：只展示「操作台」「日志诊断」两个导航项；
# - 程序员：展示全部页面（离线模式固定全部页面，不受角色影响）。
ROLE_TECHNICIAN = "technician"
ROLE_PROGRAMMER = "programmer"
ROLE_LABELS = {ROLE_TECHNICIAN: "技术员模式", ROLE_PROGRAMMER: "程序员模式"}
# 命令行简写：t = 技术员，g = 程序员（完整名仍可用）
ROLE_ALIASES = {
    "t": ROLE_TECHNICIAN, "technician": ROLE_TECHNICIAN,
    "g": ROLE_PROGRAMMER, "programmer": ROLE_PROGRAMMER,
}
TECHNICIAN_PAGES = ("console", "diagnostics")


def normalize_role(value: str | None) -> str:
    """角色简写/全名统一成 technician/programmer；未知值按程序员。"""
    return ROLE_ALIASES.get((value or "").strip().lower(), ROLE_PROGRAMMER)


def nav_pages_for_role(role: str | None) -> tuple[tuple[str, str], ...]:
    """按角色过滤左侧导航；技术员只保留操作台/日志诊断。"""
    if role == ROLE_TECHNICIAN:
        return tuple((label, key) for label, key in NAV_PAGES
                     if key in TECHNICIAN_PAGES)
    return NAV_PAGES


def initial_page_for_role(role: str | None) -> str:
    """技术员从操作台进入；程序员/离线从作业台进入。"""
    return "console" if role == ROLE_TECHNICIAN else "workbench"


def profile_badge(profile: str | None) -> str:
    """顶部徽标文案：**按 profile 明说**是离线示例还是真实设备，不混用。"""
    return PRODUCTION_BADGE if profile == "production" else OFFLINE_BADGE


@dataclass
class UiContext:
    """页面共享的上下文 (离线栈 + 作业台 ViewModel)。"""

    stack: Any
    vm: Any
    # 真实设备模式角色（DEC-098）；离线模式忽略，固定展示全部页面。
    role: str = ROLE_PROGRAMMER


if GUI_REQUIRED:

    class MainWindow(QMainWindow):
        def __init__(self, ctx: UiContext) -> None:
            super().__init__()
            self.ctx = ctx
            self.role = normalize_role(getattr(ctx, "role", ROLE_PROGRAMMER))
            # 角色只在真实设备模式生效；离线固定展示全部页面（DEC-098）。
            if getattr(getattr(ctx, "stack", None), "profile", "offline") != "production":
                self.role = ROLE_PROGRAMMER
            # 页面（如操作台快捷入口）读的是生效后的角色，不是请求角色。
            try:
                ctx.role = self.role
            except Exception:  # noqa: BLE001 - 上下文不可写时不影响主窗口
                pass
            title_suffix = "技术员" if self.role == ROLE_TECHNICIAN else "作业台"
            self.setWindowTitle(f"杆塔智能作业系统 - {title_suffix}")
            self.setStyleSheet(qss())
            font = QFont()
            font.setFamilies(list(THEME.font_stack))
            font.setPointSize(10)
            self.setFont(font)

            root = QWidget()
            outer = QVBoxLayout(root)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            outer.addWidget(self._build_topbar())

            body = QHBoxLayout()
            body.setContentsMargins(0, 0, 0, 0)
            body.setSpacing(0)
            body.addWidget(self._build_nav())

            self.stack_widget = QStackedWidget()
            self.pages = self._build_pages()
            for _key, page in self.pages.items():
                self.stack_widget.addWidget(page)
            body.addWidget(self.stack_widget, 1)
            outer.addLayout(body, 1)
            self.setCentralWidget(root)

            self._clock = QTimer(self)
            self._clock.setInterval(1000)
            self._clock.timeout.connect(self._refresh_status)
            self._clock.start()
            self._select(initial_page_for_role(self.role))
            self._refresh_status()
            _log.info("主窗口已打开 profile=%s role=%s",
                      getattr(getattr(self.ctx, "stack", None), "profile", "?"),
                      self.role)

        # ---------- 顶部状态栏 ----------
        def _build_topbar(self) -> QFrame:
            bar = QFrame()
            bar.setObjectName("TopBar")
            bar.setFixedHeight(56)
            lay = QHBoxLayout(bar)
            lay.setContentsMargins(16, 8, 16, 8)
            lay.setSpacing(12)

            title = QLabel("杆塔智能作业系统")
            title.setObjectName("TopTitle")
            lay.addWidget(title)
            profile = getattr(getattr(self.ctx, "stack", None), "profile", "offline")
            badge = QLabel(profile_badge(profile))
            badge.setObjectName("TopBadge")
            lay.addWidget(badge)
            lay.addStretch(1)

            # 硬件急停输入: 未验证显示"待确认", 不默认正常 (03 §4.1)
            self.estop = QLabel("!")
            self.estop.setFixedSize(22, 22)
            self.estop.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.estop.setStyleSheet(
                f"background:{THEME.danger}; color:#FFFFFF; border-radius:11px;"
                f" font-weight:700;")
            lay.addWidget(self.estop)
            self.estop_label = QLabel("急停反馈：待确认")
            self.estop_label.setObjectName("TopWarn")
            lay.addWidget(self.estop_label)

            self.ready_label = QLabel("所需设备检查通过后可启动")
            self.ready_label.setObjectName("TopOk")
            lay.addWidget(self.ready_label)

            self.clock_label = QLabel("")
            self.clock_label.setObjectName("TopOk")
            lay.addWidget(self.clock_label)

            lay.addSpacing(8)
            # 任务状态 + 全局停止入口: 所有页面常驻 (03 §3)
            self.task_pill = StatusPill("IDLE")
            lay.addWidget(self.task_pill)
            self.global_stop = QPushButton("停止全部")
            self.global_stop.setObjectName("Danger")
            self.global_stop.setMinimumHeight(THEME.button_min_height_px)
            self.global_stop.clicked.connect(self._global_stop)
            lay.addWidget(self.global_stop)
            return bar

        def _global_stop(self) -> None:  # pragma: no cover - Qt
            _log.warning("界面：触发全局停止")
            try:
                self.ctx.vm.stop_all()
            except Exception:  # noqa: BLE001
                pass
            self.refresh_all()

        # ---------- 导航 ----------
        def _build_nav(self) -> QFrame:
            nav = QFrame()
            nav.setObjectName("Nav")
            nav.setFixedWidth(168)
            lay = QVBoxLayout(nav)
            lay.setContentsMargins(0, 10, 0, 10)
            lay.setSpacing(2)
            self._group = QButtonGroup(self)
            self._group.setExclusive(True)
            for label, key in nav_pages_for_role(self.role):
                btn = QPushButton(f"  {label}")
                btn.setObjectName("NavItem")
                btn.setCheckable(True)
                btn.clicked.connect(lambda _=False, k=key: self._select(k))
                self._group.addButton(btn)
                lay.addWidget(btn)
                btn.setProperty("page_key", key)
            lay.addStretch(1)
            profile = getattr(getattr(self.ctx, "stack", None), "profile", "offline")
            if profile == "production":
                role_label = QLabel(ROLE_LABELS.get(self.role, ""))
                role_label.setObjectName("SubLabel")
                role_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lay.addWidget(role_label)
            return nav

        def _select(self, key: str) -> None:  # pragma: no cover - Qt
            # 页面切换**不记日志**（DEC-052）：纯界面导航，记下来只会把
            # 真正的故障/事件淹没；切换本身也不是需要追溯的操作。
            console = self.pages.get("console")
            monitor = getattr(console, "monitor", None)
            if key in ("manual", "motion", "vision", "devices") and monitor is not None:
                if not monitor.yield_camera():
                    self.ctx.vm._set_hint("正在结束监控采集，请稍后再次进入控制页", "warn")
                    for button in self._group.buttons():
                        button.setChecked(self.pages.get(button.property("page_key"))
                                          is self.stack_widget.currentWidget())
                    self.refresh_all()
                    return
            for btn in self._group.buttons():
                btn.setChecked(btn.property("page_key") == key)
            self.stack_widget.setCurrentWidget(self.pages[key])
            self.refresh_all()

        # ---------- 页面 ----------
        def _build_pages(self) -> dict:
            from application.presentation.pages.console import ConsolePage
            from application.presentation.pages.diagnostics import DiagnosticsPage
            from application.presentation.pages.devices import DevicesPage
            from application.presentation.pages.gripper import GripperPage
            from application.presentation.pages.manual import ManualPage
            from application.presentation.pages.motion import MotionPage
            from application.presentation.pages.recipes import RecipesPage
            from application.presentation.pages.settings import SettingsPage
            from application.presentation.pages.vision import VisionPage
            from application.presentation.pages.workbench import WorkbenchPage
            ctx = self.ctx
            workbench = WorkbenchPage(ctx)
            return {
                "workbench": workbench,
                "console": ConsolePage(ctx, workbench),
                "manual": ManualPage(ctx),
                "vision": VisionPage(ctx),
                "recipes": RecipesPage(ctx),
                "motion": MotionPage(ctx),
                "devices": DevicesPage(ctx),
                "gripper": GripperPage(ctx),
                "settings": SettingsPage(ctx),
                "diagnostics": DiagnosticsPage(ctx),
            }

        # ---------- 刷新 ----------
        def _refresh_status(self) -> None:  # pragma: no cover - Qt
            self.clock_label.setText(
                QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss"))
            state = self.ctx.vm.state().task_state
            fg, bg = state_colors(state)
            self.task_pill.set_text(state, tone=fg)
            active = state not in ("IDLE", "STOPPED", "COMPLETED", "FAULT")
            self.ready_label.setText(
                "任务执行中" if active else "所需设备检查通过后可启动")

        def refresh_all(self) -> None:  # pragma: no cover - Qt
            self._refresh_status()
            page = self.stack_widget.currentWidget()
            if page is not None and hasattr(page, "refresh"):
                try:
                    page.refresh()
                except Exception:  # noqa: BLE001
                    pass

        def shutdown(self) -> None:
            """退出前停止所有定时器, 再交给外层关服务/设备.

            顺序很重要: 先停定时器, 避免回调在对象销毁后仍被触发 (会导致原生崩溃)。
            """
            try:
                self._clock.stop()
            except Exception:  # noqa: BLE001
                pass
            for page in getattr(self, "pages", {}).values():
                fn = getattr(page, "shutdown", None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # noqa: BLE001
                        pass
            _log.info("主窗口关闭")

else:  # pragma: no cover
    class MainWindow:  # type: ignore[no-redef]
        def __init__(self, *a, **k) -> None:
            raise RuntimeError("未安装 PySide6, 无法创建界面窗口")
