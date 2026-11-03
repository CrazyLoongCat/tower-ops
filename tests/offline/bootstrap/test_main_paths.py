"""DEC-059：离线(offline/gui)与 real 入口必须装配**同一份**引导定位配置。

背景：`build_offline_stack` 的 ``guidance_config_path`` 缺省会退回
``<runtime>/guidance.yaml``（旧默认值：tcp_x=0、circle/ds86 段为空）。`main.py` 的离线入口
此前漏传该参数，导致离线界面的「粗定位」与「精定位」算出来完全一样（tcp_x=0），
找圆调试的深度/找圆参数也与现场 `configs/motion/guidance.yaml` 不一致。

本用例只守入口装配（不读也不断言现场配置里的具体数值）。
"""
from __future__ import annotations

import argparse
import os

import main as entry

from application.bootstrap import app_paths


def test_offline_stack_uses_same_guidance_config_as_real(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(entry, "build_offline_stack", fake_build)
    args = argparse.Namespace(
        db=str(tmp_path / "records.db"),
        runtime=str(tmp_path / "runtime"),
        recipe=str(tmp_path / "recipe.yaml"),
    )
    entry._build_stack(args)

    expected = os.path.join(app_paths.configs_dir(), "motion", "guidance.yaml")
    assert captured["guidance_config_path"] == expected
    # 其余三个配置路径保持原约定（回归保护）
    assert captured["motion_config_path"].endswith(
        os.path.join("configs", "motion", "motion.yaml"))
    assert captured["device_config_path"].endswith(
        os.path.join("configs", "devices", "devices.yaml"))
    assert captured["vision_detections_path"].endswith(
        os.path.join("configs", "vision", "detections.yaml"))
