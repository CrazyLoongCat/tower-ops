"""打包（PyInstaller 冻结）后的路径布局与种子释放（DEC-016）。

这些用例不依赖任何设备/SDK，也不构建 exe —— 只把 `sys.frozen` / `sys.executable` /
`sys._MEIPASS` 换成"冻结后应有的样子"，验证：

1. 源码运行布局**没有变化**（回归保护）；
2. 冻结后 `configs/`、`runtime/` 落在 **exe 同级**（可写、可备份），包内那份只当种子；
3. 种子释放**只补缺失、不覆盖**用户已改的配置（现场填的 devices.yaml 不会被盖）；
4. 厂家包与 cp38 原生件的默认目录在冻结后指向**包内** vendor/ 与 detect_circle/。

诚实边界：这里证明的是**路径解析逻辑**，不是"exe 能跑起来"—— 真机构建与运行
（Windows + PyInstaller）仍须在 ENV-MAIN 上做，见 `_evidence/打包_离线证据.txt`。
"""
from __future__ import annotations

import os
import sys

import pytest

from application.bootstrap import app_paths, vendor_paths
from application.hosts.circle38 import backends

_TOWER_OPS_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """把进程伪装成冻结运行：exe 在 <tmp>/dist/tower-ops/tower-ops.exe。

    返回 (exe 目录, 包内资源根) —— 包内资源根就是 PyInstaller 6 onedir 的 `_internal`。
    """
    exe_dir = tmp_path / "dist" / "tower-ops"
    internal = exe_dir / "_internal"
    internal.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tower-ops.exe"), raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    return str(exe_dir), str(internal)


# ---------------------------------------------------------------- 源码运行布局

def test_source_layout_unchanged():
    """源码运行：应用根=工程根，configs 就地读写（与打包前完全一致）。"""
    assert app_paths.is_frozen() is False
    assert app_paths.app_root() == _TOWER_OPS_ROOT
    assert app_paths.bundle_root() == _TOWER_OPS_ROOT
    assert app_paths.configs_dir() == os.path.join(_TOWER_OPS_ROOT, "configs")
    assert app_paths.runtime_dir() == os.path.join(_TOWER_OPS_ROOT, "runtime")
    assert os.path.isdir(os.path.join(app_paths.configs_dir(), "recipes"))


def test_source_seed_is_noop():
    """源码运行时"种子即现场"，不该有任何拷贝动作。"""
    assert app_paths.seed_user_files() == []


def test_vendor_and_native_defaults_match_functions_in_source_mode():
    """模块级常量与函数同源（源码模式）—— 换布局时不会只改一处。"""
    robot, camera = vendor_paths.default_sdk_dirs()
    assert vendor_paths.DEFAULT_ROBOT_SDK_DIR == robot
    assert vendor_paths.DEFAULT_DS86_SDK_PARENT == camera
    assert backends.DEFAULT_MODULE_DIR == backends._default_module_dir()


# ---------------------------------------------------------------- 冻结运行布局

def test_frozen_layout_puts_writable_dirs_beside_exe(frozen):
    exe_dir, internal = frozen
    assert app_paths.is_frozen() is True
    assert app_paths.app_root() == exe_dir
    assert app_paths.bundle_root() == internal
    # 关键：可写的 configs/runtime 在 exe 同级，不在 _internal 里
    assert app_paths.configs_dir() == os.path.join(exe_dir, "configs")
    assert app_paths.runtime_dir() == os.path.join(exe_dir, "runtime")
    assert not app_paths.configs_dir().startswith(internal)
    assert app_paths.seed_configs_dir() == os.path.join(internal, "configs")


def test_frozen_without_meipass_falls_back_to_exe_dir(tmp_path, monkeypatch):
    """异常情形（没有 _MEIPASS）也要能启动：退回 exe 目录，不抛异常。"""
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tower-ops.exe"), raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert app_paths.bundle_root() == str(exe_dir)


def test_vendor_and_native_defaults_point_into_bundle_when_frozen(frozen, monkeypatch):
    """冻结后厂家包/原生件默认目录在包内（spec 把它们收进 vendor/ 与 detect_circle/）。"""
    _exe_dir, internal = frozen
    robot, camera = vendor_paths.default_sdk_dirs()
    assert robot == os.path.join(internal, "vendor", "robot")
    assert camera == os.path.join(internal, "vendor", "camera")
    assert backends._default_module_dir() == os.path.join(internal, "detect_circle")


# ---------------------------------------------------------------- 种子释放

def _make_seed(internal: str) -> None:
    seed = os.path.join(internal, "configs")
    os.makedirs(os.path.join(seed, "devices"), exist_ok=True)
    os.makedirs(os.path.join(seed, "recipes"), exist_ok=True)
    os.makedirs(os.path.join(seed, "__pycache__"), exist_ok=True)
    with open(os.path.join(seed, "devices", "devices.yaml"), "w", encoding="utf-8") as fh:
        fh.write("seed: 包内种子\n")
    with open(os.path.join(seed, "recipes", "ds86_two_station_example.yaml"),
              "w", encoding="utf-8") as fh:
        fh.write("recipe: 1\n")
    with open(os.path.join(seed, "devices", "devices.yaml.bak"), "w", encoding="utf-8") as fh:
        fh.write("备份不该被释放\n")
    with open(os.path.join(seed, "__pycache__", "x.pyc"), "w", encoding="utf-8") as fh:
        fh.write("cache")


def test_seed_copies_missing_files_and_never_overwrites_user_edits(frozen):
    """现场改过的 devices.yaml 必须原样保留；只补缺失的。"""
    exe_dir, internal = frozen
    _make_seed(internal)
    live = os.path.join(exe_dir, "configs", "devices")
    os.makedirs(live, exist_ok=True)
    user_file = os.path.join(live, "devices.yaml")
    with open(user_file, "w", encoding="utf-8") as fh:
        fh.write("user: 现场填的地址\n")

    copied = app_paths.seed_user_files()

    assert "recipes/ds86_two_station_example.yaml" in copied
    assert "devices/devices.yaml" not in copied
    with open(user_file, encoding="utf-8") as fh:
        assert fh.read() == "user: 现场填的地址\n"      # 未被覆盖
    with open(os.path.join(exe_dir, "configs", "recipes",
                           "ds86_two_station_example.yaml"), encoding="utf-8") as fh:
        assert fh.read() == "recipe: 1\n"


def test_seed_skips_backups_and_caches_and_is_idempotent(frozen):
    exe_dir, internal = frozen
    _make_seed(internal)
    first = app_paths.seed_user_files()
    assert not any(p.endswith(".bak") for p in first)
    assert not any("__pycache__" in p for p in first)
    assert app_paths.seed_user_files() == []          # 第二次无事可做
    assert not os.path.exists(os.path.join(exe_dir, "configs", "devices",
                                          "devices.yaml.bak"))


def test_seed_without_seed_dir_is_harmless(tmp_path, monkeypatch):
    """包内没有 configs（理论上不该发生）时不能挡启动。"""
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "tower-ops.exe"), raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "nope"), raising=False)
    assert app_paths.seed_user_files() == []


# ---------------------------------------------------------------- 与 main 接线

def test_main_derives_paths_from_app_paths(frozen, monkeypatch):
    """main.py 的 configs/runtime 默认值必须跟着 app_paths 走（别回去写死 _HERE）。"""
    import main as main_mod

    exe_dir, internal = frozen
    _make_seed(internal)

    captured: dict = {}

    def fake_build(args):
        captured.update(db=args.db, runtime=args.runtime, recipe=args.recipe)
        return object()

    monkeypatch.setattr(main_mod, "_build_stack", fake_build)
    monkeypatch.setattr(main_mod, "_open_gui", lambda stack, **_kw: 0)

    assert main_mod.main(["gui"]) == 0
    assert captured["runtime"] == os.path.join(exe_dir, "runtime")
    assert captured["db"] == os.path.join(exe_dir, "runtime", "records.db")
    assert captured["recipe"] == os.path.join(exe_dir, "configs", "recipes",
                                             "ds86_two_station_example.yaml")
    assert os.path.isfile(captured["recipe"])          # 种子已释放，配方真的在那儿
