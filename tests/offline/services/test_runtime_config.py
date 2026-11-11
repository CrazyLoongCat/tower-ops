"""DEC-065 运行/日志参数存储：默认值、原子保存、重启后读回。"""
from __future__ import annotations

from application.services.runtime_config import RuntimeSettings, RuntimeSettingsStore


def test_runtime_settings_default_and_roundtrip(tmp_path):
    path = str(tmp_path / "settings.yaml")
    st = RuntimeSettingsStore(path)
    s = st.load()
    assert isinstance(s, RuntimeSettings)
    assert s.log_level == "DEBUG" and s.log_backup_days == 14
    st.save()
    assert (tmp_path / "settings.yaml").exists()

    st.settings.log_level = "INFO"
    st.settings.log_backup_days = 30
    st.settings.log_to_console = True
    st.save()
    again = RuntimeSettingsStore(path).load()
    assert again.log_level == "INFO"
    assert again.log_backup_days == 30
    assert again.log_to_console is True


# ---------------------------------------------------------------- DEC-115 快捷键
def test_shortcuts_default_normalize_and_roundtrip(tmp_path):
    from application.services.runtime_config import (
        DEFAULT_SHORTCUTS, duplicate_shortcuts, normalize_shortcuts,
    )

    s = RuntimeSettings()
    assert s.shortcuts["stop_all"] == "F12"
    assert s.shortcuts["start"] == "F5"

    # 缺省项补默认、空串=未绑定、未知键忽略
    got = normalize_shortcuts({"start": "", "page_console": "Ctrl+9", "nope": "F1"})
    assert got["start"] == "" and got["page_console"] == "Ctrl+9"
    assert "nope" not in got and got["stop_all"] == DEFAULT_SHORTCUTS["stop_all"]

    # 落盘再读回
    path = str(tmp_path / "settings.yaml")
    st = RuntimeSettingsStore(path)
    st.load()
    st.settings.shortcuts["start"] = "Ctrl+F5"
    st.settings.shortcuts["pause"] = ""
    st.save()
    again = RuntimeSettingsStore(path).load()
    assert again.shortcuts["start"] == "Ctrl+F5"
    assert again.shortcuts["pause"] == ""

    # 重复检测（空串不参与）
    dup = duplicate_shortcuts({"stop_all": "F12", "start": "F12", "pause": ""})
    assert dup == ["F12"]
    assert duplicate_shortcuts({"stop_all": "F12", "start": ""}) == []
