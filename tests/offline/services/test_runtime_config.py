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
