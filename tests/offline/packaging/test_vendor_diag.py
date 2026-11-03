"""厂家 SDK 导入失败的**诊断报告**，以及"data 进包的 SDK 依赖必须登记"这条不变式。

起因（2026-10-15，真机）：打包好的 exe 里，
- 机器人报 `MotionArmError: 未注入 SDK 且无法导入厂家包 ... No module named 'ctypes.util'`；
- 相机报 `Ds86SdkError: 真实 Scepter 封装需在 A23/A24 登记后接线`。

两条都不是"设备不在"，而是可定位的软件缺陷：前者是厂家包按 **data** 进包、PyInstaller
不分析它，导致它 import 的 `ctypes.util` 没进包；后者是一条**陈旧的无条件 raise**。
这些用例把"再发生时会指哪打哪"钉住：

1. 失败报告要含**完整回溯**、期望包目录、实际解析位置、解释器自身缺件自检；
2. 强制导入失败（`sys.modules[name] = None`）时**任何平台**都能复现该报告；
3. `scripts/check_packaging.py` 的厂家包依赖不变式**真的会失败**（反向对照，防"假绿"）。

诚实边界：这里证明的是**报告内容与检查规则**，不是"真机已经能连上设备"——
真机构建/连机仍须在 Windows + ENV-MAIN 上做，`hardware` 保持 NOT_RUN。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from application.bootstrap import vendor_paths

_TOWER_OPS_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))


def _poison(monkeypatch, *names: str) -> None:
    """让 `import names` 必定失败（`None in sys.modules` 是标准报错），与平台无关。"""
    for n in names:
        monkeypatch.setitem(sys.modules, n, None)


# --------------------------------------------------------------- 诊断报告本身

def _caught(fn):
    """跑 fn 并把真正**抛出来过**的异常交给调用者（这样 `__traceback__` 是真实的）。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError("预期抛出异常，实际没有")


def test_report_carries_traceback_and_locations():
    exc = _caught(lambda: __import__("ctypes.util_missing_zzz_2026"))
    report = vendor_paths.describe_import_failure("Robotic_Arm", exc)

    assert "Robotic_Arm" in report                          # 哪个包
    assert "ctypes.util_missing_zzz_2026" in report         # 原始异常没被吞掉
    assert "完整回溯" in report
    # 真回溯（含 "Traceback (most recent call last)" 与出错位置），不是一句 str(exc)
    assert "Traceback (most recent call last)" in report
    assert "test_vendor_diag.py" in report
    assert "期望包目录" in report and "实际解析到" in report
    assert "sys.path" in report         # "目录在但没挂上"与"目录都没有"要能分开


def test_report_self_checks_module_that_froze_out():
    """`ctypes.util` 这类"解释器自身缺件"要被逐条点出来（本次真机的根因）。"""
    report = vendor_paths.describe_import_failure(
        "Robotic_Arm", ModuleNotFoundError("No module named 'ctypes.util'"))
    assert "解释器自检 ctypes.util" in report and "解释器自检 ctypes" in report


def test_report_survives_unresolvable_package(monkeypatch):
    """包名解析本身都失败时（None in sys.modules），报告仍要出得来、不能二次崩。"""
    _poison(monkeypatch, "Robotic_Arm")
    report = vendor_paths.describe_import_failure(
        "Robotic_Arm", ImportError("import of Robotic_Arm halted"))
    assert "Robotic_Arm" in report and "完整回溯" in report


def test_frozen_hint_only_when_frozen(monkeypatch):
    exc = ModuleNotFoundError("No module named 'ctypes.util'")
    plain = vendor_paths.describe_import_failure("Robotic_Arm", exc)
    assert "hiddenimports" not in plain                  # 源码运行不提打包

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen = vendor_paths.describe_import_failure("Robotic_Arm", exc)
    assert "hiddenimports" in frozen and "data 进包" in frozen


# ----------------------------------------------------- 三个真实入口都说真话

def test_motion_arm_reports_traceback(monkeypatch):
    """`RealmanMotionArm._ensure_sdk` 的失败要带完整回溯（曾只回显一句 str(exc)）。"""
    from application.adapters.realman.motion import MotionArmError, RealmanMotionArm
    _poison(monkeypatch, "Robotic_Arm")
    with pytest.raises(MotionArmError) as ei:
        RealmanMotionArm()._ensure_sdk()                 # noqa: SLF001
    msg = str(ei.value)
    assert "Robotic_Arm" in msg and "完整回溯" in msg and "Traceback" in msg


def test_rm_robot_adapter_reports_traceback(monkeypatch):
    from application.adapters.realman.real import RmRobotAdapter, RobotSdkError
    _poison(monkeypatch, "Robotic_Arm")
    with pytest.raises(RobotSdkError) as ei:
        RmRobotAdapter().connect()
    assert "完整回溯" in str(ei.value)


def test_ds86_scepter_reports_traceback(monkeypatch):
    from application.adapters.ds86 import scepter
    _poison(monkeypatch, "scepter_sdk")
    with pytest.raises(scepter.ScepterError) as ei:
        scepter.import_scepter_sdk()
    msg = str(ei.value)
    assert "scepter_sdk" in msg and "完整回溯" in msg and "Traceback" in msg


# -------------------------------------------- 打包检查规则有牙（反向对照）

def _load_checker():
    path = os.path.join(_TOWER_OPS_ROOT, "scripts", "check_packaging.py")
    spec = importlib.util.spec_from_file_location("_check_packaging_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # 脚本有 __main__ 守卫，不会跑 main()
    return mod


def test_vendor_dep_rule_fails_when_ctypes_util_missing():
    """缺 `ctypes.util`（父模块 ctypes 在也**不算数**）时必须报错。"""
    mod = _load_checker()
    before = len(mod._failures)                                   # noqa: SLF001
    mod.check_vendor_deps(["ctypes", "glob", "platform", "atexit"])
    assert len(mod._failures) > before, "漏了 ctypes.util 却没报错 = 检查形同虚设"
    assert "ctypes.util" in mod._failures[-1]
    assert "camera" in mod._failures[-1] or "robot" in mod._failures[-1]
    del mod._failures[before:]                                    # noqa: SLF001


def test_vendor_dep_rule_passes_on_the_shipped_set():
    """仓库现在给的那一组（含 ctypes.util）应当通过 —— 否则说明规则写歪了。"""
    mod = _load_checker()
    before = len(mod._failures)                                   # noqa: SLF001
    mod.check_vendor_deps(["ctypes", "ctypes.util", "atexit", "platform", "glob"])
    assert mod._failures[before:] == []
