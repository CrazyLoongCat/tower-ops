"""DEC-044 启动执行方案容错载入与旧版自愈。

守的边界：
- ``--recipe`` 指名文件坏了**绝不拖垮整栈启动**：返回通知、跳过，不抛异常；
- 旧版自愈只发生在"可写 configs 目录内 + 包内有同相对路径且自己能载入的新版"这一种情形，
  且现场旧文件**改名备份**（内容原样保留），绝不静默删除/覆盖；
- 目录外文件、无种子副本、种子本身也是坏的、源码运行（种子=现场）→ 一律"只跳过、不动盘"；
- 换新中途失败要回滚：把现场原文件放回去，不留"换了一半"的状态。
"""
from __future__ import annotations

import os

from application.bootstrap.startup_recipe import (
    ACTION_REPLACED,
    ACTION_SKIPPED,
    load_startup_recipe,
)
from application.services.recipes.service import RecipeService, ReferenceCatalog

_V2_SEED = """\
# 包内新版（schema v2）
schema_version: 2
recipe_id: ds86_two_station_example
version: 0.2-draft
state: draft
is_offline_draft: true
sequence:
  - {id: capture, type: camera.capture, camera_ref: ds86_01}
"""

_V1_OLD = """\
# 现场旧版（工位版, schema v1）
schema_version: 1
recipe_id: ds86_two_station_example
version: 0.1
state: draft
stations:
  - {station_id: station_01}
required_cameras: [ds86_01]
sequence:
  - {id: capture, type: camera.capture, target_from: {station_field: joint_target_ref}}
"""


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _svc():
    return RecipeService(ReferenceCatalog())


def _layout(tmp_path):
    """configs/recipes/<file>（现场） + seed/recipes/<file>（包内种子）。"""
    configs = tmp_path / "configs"
    seed = tmp_path / "seed"
    user = configs / "recipes" / "ds86_two_station_example.yaml"
    seed_file = seed / "recipes" / "ds86_two_station_example.yaml"
    return configs, seed, user, seed_file


def _load(svc, path, *, seed, configs, log=None):
    return load_startup_recipe(svc, str(path), log=log,
                               seed_dir=str(seed), configs_root=str(configs))


def _backups(user):
    return sorted(p for p in user.parent.iterdir()
                  if p.name.startswith(user.name + ".incompatible-") and p.suffix == ".bak")


# ---------------------------------------------------------------- 正常路径
def test_valid_recipe_loads_silently(tmp_path):
    configs, seed, user, seed_file = _layout(tmp_path)
    _write(user, _V2_SEED)
    _write(seed_file, _V2_SEED)
    svc = _svc()
    assert _load(svc, user, seed=seed, configs=configs) is None
    assert "ds86_two_station_example" in svc.drafts()
    assert svc.source_of("ds86_two_station_example") == os.path.abspath(user)
    assert _backups(user) == []                       # 好文件绝不触碰


def test_missing_file_is_skipped_with_notice(tmp_path):
    configs, seed, user, _ = _layout(tmp_path)
    logs = []
    notice = _load(_svc(), user, seed=seed, configs=configs, log=logs.append)
    assert notice is not None and notice.action == ACTION_SKIPPED
    assert "不存在" in notice.message
    assert logs and "不存在" in logs[0]


def test_empty_path_returns_none(tmp_path):
    assert load_startup_recipe(_svc(), "") is None


# ---------------------------------------------------------------- 旧版自愈
def test_old_recipe_is_backed_up_and_replaced_from_seed(tmp_path):
    configs, seed, user, seed_file = _layout(tmp_path)
    _write(user, _V1_OLD)
    _write(seed_file, _V2_SEED)
    svc = _svc()
    logs = []
    notice = _load(svc, user, seed=seed, configs=configs, log=logs.append)
    assert notice is not None and notice.action == ACTION_REPLACED
    assert "已自动换新" in notice.message
    # 现场旧文件改名备份，内容原样保留
    backups = _backups(user)
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == _V1_OLD
    # 原路径上是包内新版，并且已作为草稿载入（来源 = 原路径，供「保存」回写）
    assert user.read_text(encoding="utf-8") == _V2_SEED
    assert "ds86_two_station_example" in svc.drafts()
    assert svc.source_of("ds86_two_station_example") == os.path.abspath(user)
    assert logs and "已自动换新" in logs[0]


def test_garbage_yaml_with_valid_seed_is_replaced(tmp_path):
    configs, seed, user, seed_file = _layout(tmp_path)
    _write(user, "sequence: [unclosed\n")
    _write(seed_file, _V2_SEED)
    notice = _load(_svc(), user, seed=seed, configs=configs)
    assert notice is not None and notice.action == ACTION_REPLACED


# ---------------------------------------------------------------- 只跳过、不动盘
def test_bad_recipe_without_seed_counterpart_only_skipped(tmp_path):
    configs, seed, user, _ = _layout(tmp_path)
    _write(user, _V1_OLD)
    (seed / "recipes").mkdir(parents=True)            # 种子目录在，但没有同名副本
    svc = _svc()
    notice = _load(svc, user, seed=seed, configs=configs)
    assert notice is not None and notice.action == ACTION_SKIPPED
    assert user.read_text(encoding="utf-8") == _V1_OLD
    assert _backups(user) == []
    assert svc.drafts() == []


def test_invalid_seed_is_not_used(tmp_path):
    configs, seed, user, seed_file = _layout(tmp_path)
    _write(user, _V1_OLD)
    _write(seed_file, _V1_OLD)                        # 包内副本也是坏的
    notice = _load(_svc(), user, seed=seed, configs=configs)
    assert notice is not None and notice.action == ACTION_SKIPPED
    assert user.read_text(encoding="utf-8") == _V1_OLD
    assert _backups(user) == []


def test_file_outside_configs_dir_is_never_touched(tmp_path):
    configs, seed, user, seed_file = _layout(tmp_path)
    outside = tmp_path / "elsewhere" / "ds86_two_station_example.yaml"
    _write(outside, _V1_OLD)
    _write(seed_file, _V2_SEED)                       # 即使种子有同名文件也不换
    notice = _load(_svc(), outside, seed=seed, configs=configs)
    assert notice is not None and notice.action == ACTION_SKIPPED
    assert outside.read_text(encoding="utf-8") == _V1_OLD
    assert _backups(outside) == []


def test_source_layout_seed_equals_configs_no_selfheal(tmp_path):
    """源码运行（未冻结）：种子目录就是现场目录，不存在"换新"，只跳过。"""
    configs, _, user, _ = _layout(tmp_path)
    _write(user, _V1_OLD)
    notice = _load(_svc(), user, seed=configs, configs=configs)
    assert notice is not None and notice.action == ACTION_SKIPPED
    assert user.read_text(encoding="utf-8") == _V1_OLD
    assert _backups(user) == []


# ---------------------------------------------------------------- 换新失败回滚
def test_copy_failure_rolls_back_original(tmp_path, monkeypatch):
    configs, seed, user, seed_file = _layout(tmp_path)
    _write(user, _V1_OLD)
    _write(seed_file, _V2_SEED)

    def _boom(*a, **k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr("application.bootstrap.startup_recipe.shutil.copy2", _boom)
    svc = _svc()
    notice = _load(svc, user, seed=seed, configs=configs)
    assert notice is not None and notice.action == ACTION_SKIPPED
    # 回滚：现场原文件放回原位（内容不变），不留 .bak 残件，也没有载入任何草稿
    assert user.read_text(encoding="utf-8") == _V1_OLD
    assert _backups(user) == []
    assert svc.drafts() == []


def test_selfheal_reports_replaced_when_recipe_id_differs_from_filename(tmp_path):
    """种子里的 recipe_id 与文件名无关：以文件内容为准（作业台按下拉 id 选择）。"""
    configs, seed, user, seed_file = _layout(tmp_path)
    _write(user, _V1_OLD)
    _write(seed_file, _V2_SEED.replace(
        "recipe_id: ds86_two_station_example", "recipe_id: some_other_id"))
    svc = _svc()
    notice = _load(svc, user, seed=seed, configs=configs)
    assert notice is not None and notice.action == ACTION_REPLACED
    assert "some_other_id" in svc.drafts()
