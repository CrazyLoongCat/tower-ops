"""服务层角色权限与审计 (E17, 10 F3, 02 §11/§12)。

**权限在业务服务中执行**; UI 按钮灰化只是表现 —— 绕过 UI 直接调服务同样被拒绝。
敏感操作 (配方发布/配置与标定变更/手动运动/复位/角色变更) 必须留追加审计;
审计写失败时默认 **fail-closed**: 拒绝该操作 (可显式关闭)。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from application.contracts.enums import StrEnum
from application.infrastructure.audit import AuditEntry, AuditLedger, AuditWriteError


class Role(StrEnum):
    VIEWER = "VIEWER"
    OPERATOR = "OPERATOR"
    ENGINEER = "ENGINEER"
    ADMIN = "ADMIN"


class Permission(StrEnum):
    VIEW = "VIEW"
    VIEW_AUDIT = "VIEW_AUDIT"
    RUN_TASK = "RUN_TASK"
    STOP = "STOP"
    MANUAL_MOTION = "MANUAL_MOTION"
    PUBLISH_RECIPE = "PUBLISH_RECIPE"
    EDIT_DEVICE_CONFIG = "EDIT_DEVICE_CONFIG"
    EDIT_CALIBRATION = "EDIT_CALIBRATION"
    RESET = "RESET"
    MANAGE_ROLES = "MANAGE_ROLES"


_P = Permission
ROLE_PERMISSIONS: dict[str, set[Permission]] = {
    Role.VIEWER.value: {_P.VIEW},
    Role.OPERATOR.value: {_P.VIEW, _P.RUN_TASK, _P.STOP},
    Role.ENGINEER.value: {_P.VIEW, _P.VIEW_AUDIT, _P.RUN_TASK, _P.STOP,
                          _P.MANUAL_MOTION, _P.PUBLISH_RECIPE,
                          _P.EDIT_DEVICE_CONFIG, _P.EDIT_CALIBRATION, _P.RESET},
    Role.ADMIN.value: set(Permission),
}

# 需要留追加审计的敏感权限
SENSITIVE_PERMISSIONS = {
    _P.MANUAL_MOTION, _P.PUBLISH_RECIPE, _P.EDIT_DEVICE_CONFIG,
    _P.EDIT_CALIBRATION, _P.RESET, _P.MANAGE_ROLES,
}


class AuthorizationError(RuntimeError):
    """越权或审计失败导致的操作拒绝."""

    def __init__(self, message: str, *, permission: str | None = None) -> None:
        self.permission = permission
        super().__init__(message)


@dataclass(frozen=True)
class Principal:
    """操作主体: 稳定用户 ID + 角色. 角色变更应重新构造, 在途操作按原角色判定。"""

    user_id: str
    role: str = Role.VIEWER.value

    def permissions(self) -> set[Permission]:
        return set(ROLE_PERMISSIONS.get(self.role, set()))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuthorizationService:
    """权限判定 + 审计写入的唯一入口."""

    ledger: AuditLedger | None = None
    fail_closed: bool = True        # 审计写失败时是否拒绝操作
    _denied: list[dict] = field(default_factory=list)

    # ---- 判定 ----
    def allowed(self, principal: Principal, permission: Permission) -> bool:
        return permission in principal.permissions()

    def require(self, principal: Principal, permission: Permission, *,
                object_type: str | None = None, object_id: str | None = None,
                before_hash: str | None = None, after_hash: str | None = None,
                reason: str | None = None, detail: dict[str, Any] | None = None,
                ) -> None:
        """校验权限; 敏感操作留审计. 不满足/审计失败即抛出 (调用方操作被拒绝)."""
        if not self.allowed(principal, permission):
            self._record_denied(principal, permission, object_id, reason)
            raise AuthorizationError(
                f"角色 {principal.role!r} 无权限 {permission.value}"
                + (f" (对象 {object_id})" if object_id else ""),
                permission=permission.value)

        if permission not in SENSITIVE_PERMISSIONS or self.ledger is None:
            return
        entry = AuditEntry(
            utc=_utc_now(), user_id=principal.user_id, role=principal.role,
            action=permission.value, object_type=object_type, object_id=object_id,
            before_hash=before_hash, after_hash=after_hash, reason=reason,
            result="OK", detail=detail or {})
        try:
            self.ledger.append(entry)          # append 内部已脱敏秘密
        except AuditWriteError as exc:
            if self.fail_closed:
                raise AuthorizationError(
                    f"审计写入失败, 按 fail-closed 拒绝 {permission.value}: {exc}",
                    permission=permission.value) from exc

    # ---- 审计查询/导出 (需权限) ----
    def query_audit(self, principal: Principal, **filters) -> list[dict]:
        if not self.allowed(principal, Permission.VIEW_AUDIT):
            raise AuthorizationError("无权限查看审计", permission="VIEW_AUDIT")
        if self.ledger is None:
            return []
        return self.ledger.query(**filters)

    def export_audit(self, principal: Principal, path: str, **filters) -> str:
        if not self.allowed(principal, Permission.VIEW_AUDIT):
            raise AuthorizationError("无权限导出审计", permission="VIEW_AUDIT")
        if self.ledger is None:
            raise AuthorizationError("无审计账本")
        return self.ledger.export(path, **filters)

    # ---- 被拒记录 (尽力而为, 不因审计失败而改变拒绝结果) ----
    def _record_denied(self, principal: Principal, permission: Permission,
                       object_id: str | None, reason: str | None) -> None:
        rec = {"utc": _utc_now(), "user_id": principal.user_id,
               "role": principal.role, "permission": permission.value,
               "object_id": object_id, "reason": reason}
        self._denied.append(rec)
        if self.ledger is not None:
            try:
                self.ledger.append(AuditEntry(
                    utc=rec["utc"], user_id=principal.user_id, role=principal.role,
                    action=permission.value, object_id=object_id, reason=reason,
                    result="DENIED"))
            except AuditWriteError:
                pass                              # 拒绝结果不因审计失败而改变

    @property
    def denied_attempts(self) -> list[dict]:
        return list(self._denied)


__all__ = [
    "AuthorizationError", "AuthorizationService", "Permission", "Principal",
    "ROLE_PERMISSIONS", "Role", "SENSITIVE_PERMISSIONS",
]
