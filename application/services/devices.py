"""设备/链路绑定校验与启动模式 (A24, 02 §10).

在装配前校验映射/地址/序列号/传输类型; 未确认的固件 profile / 缺失标定不能获得
自动能力. 离线只校验草稿, 不连接真实设备.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from application.contracts.enums import LinkTransportKind


@dataclass
class BindingIssue:
    code: str
    message: str
    blocking: bool = True


def validate_link_board_binding(links: list[dict], boards: list[dict]) -> list[BindingIssue]:
    """校验 Link/Board 绑定: 同 link 上地址唯一; 独立 link 允许地址重复; 传输类型合法."""
    issues: list[BindingIssue] = []
    link_ids = {l.get("link_id") for l in links}
    seen: set[tuple[str, int]] = set()
    for b in boards:
        lid = b.get("link_id")
        addr = b.get("address")
        if lid not in link_ids:
            issues.append(BindingIssue("board.link", f"板 {b.get('board_id')!r} 引用未知链路 {lid!r}"))
            continue
        key = (lid, addr)
        if key in seen:
            issues.append(BindingIssue(
                "board.addr", f"链路 {lid!r} 上地址 {addr} 重复绑定"))
        seen.add(key)
    for l in links:
        kind = l.get("transport_kind")
        if kind not in (LinkTransportKind.SERIAL.value, LinkTransportKind.RAW_RTU_TUNNEL.value):
            issues.append(BindingIssue("link.kind", f"链路 {l.get('link_id')!r} 传输类型非法: {kind!r}"))
    return issues


def validate_camera_binding(cameras: list[dict]) -> list[BindingIssue]:
    """四相机必须有唯一序列号; 重复序列号拒绝 (T08)."""
    issues: list[BindingIssue] = []
    serials: dict[str, str] = {}
    ids: set[str] = set()
    for cam in cameras:
        cid = cam.get("camera_id")
        if cid in ids:
            issues.append(BindingIssue("camera.id", f"相机 ID {cid!r} 重复"))
        ids.add(cid)
        serial = cam.get("serial")
        if serial:
            if serial in serials:
                issues.append(BindingIssue(
                    "camera.serial", f"序列号 {serial!r} 被多台相机共用"))
            serials[serial] = cid
    return issues


def validate_firmware_profile(profile: dict) -> list[BindingIssue]:
    """未确认的固件 profile 不能获得自动能力 (只作草稿)."""
    if not profile.get("confirmed", False):
        return [BindingIssue("fw.unconfirmed",
                             f"固件 profile {profile.get('profile_version')!r} 未确认 (INPUT-FW pending)",
                             blocking=True)]
    return []


def validate_startup_mode(mode: str, *, has_published_recipe: bool,
                          calibrations_ok: bool) -> list[BindingIssue]:
    """启动模式校验: 未发布配方/无效标定不能进入自动作业."""
    issues: list[BindingIssue] = []
    if mode == "auto" and not has_published_recipe:
        issues.append(BindingIssue("startup.recipe", "无已发布执行方案, 不能进入自动作业"))
    if mode in ("auto", "manual") and not calibrations_ok:
        issues.append(BindingIssue("startup.calib", "标定无效, 不能启用自动/手动运动"))
    return issues


@dataclass
class BindingReport:
    issues: list[BindingIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.blocking for i in self.issues)

    def messages(self) -> list[str]:
        return [i.message for i in self.issues]


def build_binding_report(*, links: list[dict] | None = None,
                         boards: list[dict] | None = None,
                         cameras: list[dict] | None = None,
                         firmware_profile: dict | None = None) -> BindingReport:
    issues: list[BindingIssue] = []
    if links is not None and boards is not None:
        issues += validate_link_board_binding(links, boards)
    if cameras:
        issues += validate_camera_binding(cameras)
    if firmware_profile is not None:
        issues += validate_firmware_profile(firmware_profile)
    return BindingReport(issues=issues)


__all__ = [
    "BindingIssue", "BindingReport", "build_binding_report",
    "validate_camera_binding", "validate_firmware_profile",
    "validate_link_board_binding", "validate_startup_mode",
]
