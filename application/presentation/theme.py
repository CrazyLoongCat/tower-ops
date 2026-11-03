"""主题令牌与样式 (12 §5, 03)。设计验收目标, 非已验证合规结论。

颜色取自 12 §5: 深青 #0A5F5F / 浅背景 #F4F7F8 / 主文字 #172B3A / 次文字 #526574。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    deep_teal: str = "#0A5F5F"
    teal_dark: str = "#084A4A"
    light_bg: str = "#F4F7F8"
    panel_bg: str = "#FFFFFF"
    border: str = "#DDE4E8"
    text_primary: str = "#172B3A"
    text_secondary: str = "#526574"
    text_disabled: str = "#9AA7B0"
    ok: str = "#0B6E3A"
    warn: str = "#8A5A00"
    danger: str = "#B3261E"
    font_family: str = "Microsoft YaHei"
    font_stack: tuple[str, ...] = ("Microsoft YaHei", "Noto Sans CJK SC", "sans-serif")
    font_size_px: int = 13
    spacing_px: int = 8
    button_min_height_px: int = 40
    button_key_height_px: int = 44
    target_screen: tuple[int, int] = (1440, 1024)
    min_contrast_ratio: float = 4.5


THEME = Theme()

# 任务状态 -> (前景色, 背景色), 用于状态徽标 (颜色+文字同时表达, 不只靠颜色)
STATE_COLORS: dict[str, tuple[str, str]] = {
    "IDLE": (THEME.text_secondary, "#E8EEF0"),
    "VALIDATING": (THEME.deep_teal, "#DCEDED"),
    "READY": (THEME.deep_teal, "#DCEDED"),
    "RUNNING": (THEME.ok, "#DDF2E6"),
    "PAUSE_REQUESTED": (THEME.warn, "#FBEFD6"),
    "PAUSED": (THEME.warn, "#FBEFD6"),
    "WAITING_OPERATOR": (THEME.warn, "#FBEFD6"),
    "STOPPING": (THEME.warn, "#FBEFD6"),
    "STOPPED": (THEME.text_secondary, "#E8EEF0"),
    "FAULT": (THEME.danger, "#FBE3E1"),
    "COMPLETED": (THEME.ok, "#DDF2E6"),
}

# 步骤状态 -> 文字颜色
STEP_COLORS: dict[str, str] = {
    "SUCCESS": THEME.ok,
    "FAILURE": THEME.danger,
    "RUNNING": THEME.deep_teal,
    "INVALID": THEME.warn,
    "STOPPED": THEME.warn,
    "PENDING": THEME.text_secondary,
}


def state_colors(state: str) -> tuple[str, str]:
    return STATE_COLORS.get(state, (THEME.text_secondary, "#E8EEF0"))


def qss() -> str:
    """应用级样式表。Qt QSS 只支持受控子集, 这里避免复杂选择器。"""
    t = THEME
    return f"""
    QWidget {{ background: {t.light_bg}; color: {t.text_primary}; }}
    #TopBar {{ background: {t.deep_teal}; }}
    #TopTitle {{ color: #FFFFFF; font-size: 18px; font-weight: 700;
                 background: transparent; }}
    #TopBadge {{ color: #FFFFFF; background: rgba(255,255,255,0.18);
                 border-radius: 10px; padding: 3px 10px; font-size: 12px; }}
    #TopSub {{ color: #D6E6E6; font-size: 12px; background: transparent; }}
    #TopWarn {{ color: #FFD9D6; font-size: 12px; font-weight: 600;
                background: transparent; }}
    #TopOk {{ color: #D6E6E6; font-size: 12px; background: transparent; }}
    QLabel#NavBrand {{ color: {t.deep_teal}; font-weight: 700; font-size: 13px;
                       background: transparent; padding: 10px 14px; }}

    #Nav {{ background: #FFFFFF; border-right: 1px solid {t.border}; }}
    #NavItem {{ background: transparent; color: {t.text_secondary}; border: none;
                text-align: left; padding: 10px 14px; font-size: 13px;
                min-height: 30px; }}
    #NavItem:checked {{ background: #E4EFEF; color: {t.deep_teal}; font-weight: 700;
                        border-left: 3px solid {t.deep_teal}; }}

    #Card {{ background: {t.panel_bg}; border: 1px solid {t.border};
             border-radius: 8px; }}
    #SectionTitle {{ color: {t.text_primary}; font-size: 14px; font-weight: 700;
                     background: transparent; }}
    #Pill {{ background: transparent; }}
    #EmptyHint {{ color: {t.text_secondary}; background: transparent; padding: 18px; }}
    #Stepper {{ background: #FFFFFF; border: 1px solid {t.border};
                border-radius: 8px; }}
    #DeviceStrip {{ background: #FFFFFF; border: 1px solid {t.border};
                    border-radius: 8px; }}
    #KV {{ background: transparent; }}
    #RailCaption {{ color: {t.text_secondary}; font-size: 12px;
                    background: transparent; }}
    #ActionRail {{ background: #FFFFFF; border: 1px solid {t.border};
                   border-radius: 8px; }}
    #BlockBox {{ background: #FDECEA; border: 1px solid #F3C6C0;
                 border-radius: 8px; }}
    #BlockTitle {{ color: {t.danger}; font-weight: 700; font-size: 14px;
                   background: transparent; }}
    #BlockSub {{ color: #8A4B45; font-size: 12px; background: transparent; }}
    #BlockBoxOk {{ background: #EAF4EC; border: 1px solid #BFDCC6;
                  border-radius: 8px; }}
    #BlockTitleOk {{ color: {t.ok}; font-weight: 700; font-size: 14px;
                     background: transparent; }}
    #BlockSubOk {{ color: #3F6B4E; font-size: 12px; background: transparent; }}
    #EventRow {{ color: {t.text_secondary}; background: transparent; font-size: 12px; }}
    #SubLabel {{ color: {t.text_secondary}; font-size: 12px; background: transparent; }}
    #StepTitle {{ color: {t.text_primary}; font-weight: 600; background: transparent; }}
    #StepSub {{ color: {t.text_secondary}; font-size: 12px; background: transparent; }}
    #StepNum {{ color: {t.text_secondary}; font-weight: 700; background: transparent; }}

    QPushButton#Primary {{ background: {t.deep_teal}; color: #FFFFFF; border: none;
                           border-radius: 6px; font-weight: 700; padding: 0 22px; }}
    QPushButton#Primary:hover:enabled {{ background: {t.teal_dark}; }}
    QPushButton#Primary:disabled {{ background: #B9CCCC; color: #EEF4F4; }}
    QPushButton#Ghost {{ background: {t.panel_bg}; color: {t.text_primary};
                         border: 1px solid {t.border}; border-radius: 6px;
                         padding: 0 18px; }}
    QPushButton#Ghost:hover:enabled {{ background: #EDF3F4; }}
    QPushButton#Ghost:disabled {{ color: {t.text_disabled}; background: #EDF1F3; }}
    QPushButton#Neutral {{ background: {t.panel_bg}; color: {t.text_primary};
                           border: 1px solid {t.border}; border-radius: 6px;
                           padding: 0 18px; }}
    QPushButton#Neutral:disabled {{ color: {t.text_disabled}; background: #EDF1F3; }}
    QPushButton#Danger {{ background: {t.danger}; color: #FFFFFF; border: none;
                          border-radius: 6px; font-weight: 700; padding: 0 22px; }}
    QPushButton#Danger:hover:enabled {{ background: #8E1D17; }}
    QPushButton#Danger:disabled {{ background: #D9A8A4; color: #F6EFEE; }}

    QTableWidget {{ background: {t.panel_bg}; border: none;
                    gridline-color: #EDF1F3; }}
    QHeaderView::section {{ background: #E8EEF0; color: {t.text_secondary};
                            padding: 6px; border: none;
                            border-right: 1px solid {t.border}; }}
    QListWidget {{ background: {t.panel_bg}; border: 1px solid {t.border};
                   border-radius: 6px; }}
    QComboBox {{ min-height: 30px; padding: 0 8px; border: 1px solid {t.border};
                 border-radius: 5px; background: {t.panel_bg}; }}
    QCheckBox {{ background: transparent; color: {t.text_secondary}; }}
    QRadioButton {{ background: transparent; color: {t.text_secondary};
                    spacing: 8px; }}
    QRadioButton:checked {{ color: {t.text_primary}; font-weight: 600; }}
    QRadioButton::indicator {{ width: 14px; height: 14px; border-radius: 7px;
                               border: 1px solid {t.border};
                               background: {t.panel_bg}; }}
    QRadioButton::indicator:hover {{ border: 1px solid {t.deep_teal}; }}
    QRadioButton::indicator:checked {{ background: #111111;
                                       border: 1px solid #111111; }}
    QRadioButton::indicator:checked:hover {{ background: #111111;
                                             border: 1px solid #111111; }}
    QRadioButton::indicator:disabled {{ background: #EDF1F3;
                                        border: 1px solid #C7D0D4; }}
    QRadioButton::indicator:checked:disabled {{ background: #B9CCCC;
                                                border: 1px solid #B9CCCC; }}
    QTabBar::tab {{ background: #E8EEF0; color: {t.text_secondary};
                    padding: 5px 12px; border-radius: 5px; margin-right: 6px; }}
    QTabBar::tab:selected {{ background: {t.deep_teal}; color: #FFFFFF;
                             font-weight: 600; }}
    QTabBar::tab:disabled {{ background: #F0F3F4; color: {t.text_disabled}; }}
    """
