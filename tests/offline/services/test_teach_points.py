"""DEC-067 示教点登记：读当前关节 → 写入 joint_targets.yaml（confirmed=true）。

纯服务层用例：文件保留（注释/其它条目）、增/覆盖/删、共享字典即时同步、
非法关节值拒绝、与 real 启动的 ``load_confirmed_targets`` 同一格式。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from application.bootstrap.production import load_confirmed_targets
from application.services.teach_points import TeachPointStore

FIXED_NOW = datetime(2026, 10, 22, 9, 0, 0, tzinfo=timezone.utc)   # 北京时间 17:00:00
SEED_HEADER = """# 已确认示教点（E01 / INPUT-PATH）
#
# 本文件里的数值必须由现场用示教器实际取得并核对。
# targets:
#   - target_id: teach_01
"""


def _store(tmp_path, name="joint_targets.yaml", **kw):
    shared: dict = {}
    path = str(tmp_path / name)
    st = TeachPointStore(path, confirmed_targets=shared,
                         clock=lambda: FIXED_NOW, **kw)
    return st, path, shared


def test_add_writes_confirmed_entry_and_keeps_seed_header(tmp_path):
    (tmp_path / "joint_targets.yaml").write_text(SEED_HEADER, encoding="utf-8")
    st, path, shared = _store(tmp_path)

    entry = st.add("teach_01", [10.0, 20.0, 30.0, 40.0, 50.0, 60.0])

    assert entry.confirmed is True
    assert entry.waypoint_ref == "taught_20261022_170000"   # 北京时间生成
    assert entry.taught_at == "2026-10-22T09:00:00+00:00"   # UTC 存储
    text = (tmp_path / "joint_targets.yaml").read_text(encoding="utf-8")
    assert text.startswith("# 已确认示教点")                 # 前导注释保留
    assert "targets:" in text and "confirmed: true" in text
    # 与 real 启动同一读取口径：能载入为已确认示教点
    loaded = load_confirmed_targets(path)
    assert loaded["teach_01"].joint_deg == (10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
    assert loaded["teach_01"].waypoint_ref == "taught_20261022_170000"
    # 共享字典（catalog / TaskService 同一对象）已就地更新
    assert shared["teach_01"].joint_deg == (10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
    # 重新打开即读回
    again = TeachPointStore(path)
    assert [e.target_id for e in again.entries()] == ["teach_01"]


def test_duplicate_requires_overwrite_then_remove_syncs_shared(tmp_path):
    st, path, shared = _store(tmp_path)
    st.add("teach_01", [1, 2, 3, 4, 5, 6])
    with pytest.raises(ValueError):
        st.add("teach_01", [9, 9, 9, 9, 9, 9])
    st.add("teach_01", [9, 9, 9, 9, 9, 9], overwrite=True)
    assert len(st.entries()) == 1
    assert st.find("teach_01").joint_deg == (9.0, 9.0, 9.0, 9.0, 9.0, 9.0)
    assert shared["teach_01"].joint_deg == (9.0, 9.0, 9.0, 9.0, 9.0, 9.0)

    st.remove("teach_01")
    assert st.entries() == [] and "teach_01" not in shared
    assert load_confirmed_targets(path) == {}
    with pytest.raises(KeyError):
        st.remove("teach_01")


def test_existing_entries_and_comments_preserved(tmp_path):
    seed = (
        "# 现场备注：手写条目不要丢\n"
        "targets:\n"
        "  - target_id: field_a          # 现场已核对\n"
        "    joint_deg: [1, 2, 3, 4, 5, 6]\n"
        "    waypoint_ref: taught_20261001_090000\n"
        "    confirmed: true\n"
        "  - target_id: draft_b\n"
        "    joint_deg: [7, 8, 9, 10, 11, 12]\n"
        "    waypoint_ref: unconfirmed_tmp\n"
        "    confirmed: false\n"
    )
    (tmp_path / "joint_targets.yaml").write_text(seed, encoding="utf-8")
    st, path, shared = _store(tmp_path)

    assert {e.target_id for e in st.entries()} == {"field_a", "draft_b"}
    # 未确认条目不进共享字典（与 load_confirmed_targets 同口径）
    assert set(shared) == {"field_a"}

    st.add("teach_02", [0, 0, 0, 0, 0, 0])
    text = (tmp_path / "joint_targets.yaml").read_text(encoding="utf-8")
    assert "# 现场备注：手写条目不要丢" in text
    assert "# 现场已核对" in text                      # 行尾注释也保留
    assert {e.target_id for e in st.entries()} == {"field_a", "draft_b", "teach_02"}
    assert set(load_confirmed_targets(path)) == {"field_a", "teach_02"}


def test_invalid_joint_values_are_rejected(tmp_path):
    st, path, _shared = _store(tmp_path)
    with pytest.raises(ValueError):
        st.add("t5", [1, 2, 3, 4, 5])
    with pytest.raises(ValueError):
        st.add("t7", [1, 2, 3, 4, 5, 6, 7])
    with pytest.raises(ValueError):
        st.add("tnan", [1, 2, 3, float("nan"), 5, 6])
    with pytest.raises(ValueError):
        st.add("   ", [1, 2, 3, 4, 5, 6])
    assert st.entries() == []
    assert not (tmp_path / "joint_targets.yaml").exists()   # 失败不落盘


def test_missing_file_is_empty_and_add_creates_directory(tmp_path):
    path = str(tmp_path / "site" / "joint_targets.yaml")
    shared: dict = {}
    st = TeachPointStore(path, confirmed_targets=shared,
                         clock=lambda: FIXED_NOW)
    assert st.entries() == [] and shared == {}
    st.add("teach_01", [1, 2, 3, 4, 5, 6])
    written = (tmp_path / "site" / "joint_targets.yaml")
    assert written.exists()
    assert written.read_text(encoding="utf-8").startswith("# 示教点")  # 首次写入带说明头
    assert shared["teach_01"].waypoint_ref.startswith("taught_")
    assert load_confirmed_targets(str(written))["teach_01"].joint_deg \
        == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)


def test_no_path_means_memory_only_until_save(tmp_path):
    st = TeachPointStore(None, clock=lambda: FIXED_NOW)
    assert st.entries() == []
    with pytest.raises(RuntimeError):
        st.add("teach_01", [1, 2, 3, 4, 5, 6])
