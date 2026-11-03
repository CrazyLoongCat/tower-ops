"""E17 服务层权限与追加审计 测试 (T20/T34 保护回归, T36; E17-附加 离线)。"""
from __future__ import annotations

import pytest

from application.contracts.targets import JointTarget
from application.domain.actions import ActionCatalog, ActionDefinition
from application.infrastructure.audit import AuditEntry, AuditLedger, AuditWriteError, redact
from application.services.authorization import (
    ROLE_PERMISSIONS,
    AuthorizationError,
    AuthorizationService,
    Permission,
    Principal,
    Role,
)
from application.services.recipes.service import (
    PublishError, RecipeService, ReferenceCatalog,
)

RECIPE = {
    "schema_version": 2, "recipe_id": "r", "version": "0.1-draft",
    "state": "draft", "is_offline_draft": True,
    "sequence": [{"id": "m", "type": "robot.move_joint",
                  "joint_target_ref": "jt"}],
}


def _catalog():
    return ReferenceCatalog(
        joint_targets={"jt": JointTarget(joint_deg=[0, 0, 0, 0, 0, 0])},
        pipelines={"p"},
        actions=ActionCatalog([ActionDefinition(action_id="a",
                                                resource_ids=("motion_global",))]),
        cameras={"ds86_01"}, devices={"eco65_01"})


def _svc(tmp_path):
    ledger = AuditLedger(str(tmp_path / "audit.db"))
    ledger.open()
    auth = AuthorizationService(ledger=ledger)
    svc = RecipeService(_catalog(), authorization=auth)
    svc.draft(RECIPE)
    return svc, auth, ledger


# ---------------- 角色/权限矩阵 ----------------
def test_role_permission_matrix():
    assert Permission.PUBLISH_RECIPE not in ROLE_PERMISSIONS[Role.VIEWER.value]
    assert Permission.RUN_TASK in ROLE_PERMISSIONS[Role.OPERATOR.value]
    assert Permission.PUBLISH_RECIPE not in ROLE_PERMISSIONS[Role.OPERATOR.value]
    assert Permission.PUBLISH_RECIPE in ROLE_PERMISSIONS[Role.ENGINEER.value]
    assert Permission.MANAGE_ROLES in ROLE_PERMISSIONS[Role.ADMIN.value]
    assert Permission.MANAGE_ROLES not in ROLE_PERMISSIONS[Role.ENGINEER.value]


def test_allowed_helper():
    auth = AuthorizationService()
    assert auth.allowed(Principal("u", Role.OPERATOR.value), Permission.STOP)
    assert not auth.allowed(Principal("u", Role.VIEWER.value), Permission.STOP)


# ---------------- 服务层强制 (绕过 UI) ----------------
def test_service_rejects_publish_without_permission(tmp_path):
    svc, auth, _ = _svc(tmp_path)
    viewer = Principal("u1", Role.VIEWER.value)
    with pytest.raises(AuthorizationError):
        svc.publish("r", allow_offline_draft=True, principal=viewer)
    op = Principal("u2", Role.OPERATOR.value)
    with pytest.raises(AuthorizationError):
        svc.publish("r", allow_offline_draft=True, principal=op)
    assert svc.history("r") == []                     # 拒绝: 未产生发布版本
    assert len(auth.denied_attempts) == 2             # 越权尝试被记录


def test_engineer_can_publish_and_audit_written(tmp_path):
    svc, auth, ledger = _svc(tmp_path)
    eng = Principal("alice", Role.ENGINEER.value)
    rev = svc.publish("r", allow_offline_draft=True, principal=eng)
    rows = ledger.query(action=Permission.PUBLISH_RECIPE.value)
    assert len(rows) == 1
    r = rows[0]
    assert r["user_id"] == "alice" and r["role"] == "ENGINEER"
    assert r["object_type"] == "recipe" and r["object_id"] == "r"
    assert r["after_hash"] and r["reason"].startswith("publish revision")
    assert rev in (r["detail"] or "")


def test_publish_without_authorization_still_works(tmp_path):
    """未注入权限服务时保持 A12 行为 (向后兼容)。"""
    svc = RecipeService(_catalog())
    svc.draft(RECIPE)
    assert svc.publish("r", allow_offline_draft=True)


# ---------------- 审计账本 ----------------
def test_audit_append_and_query_and_export(tmp_path):
    ledger = AuditLedger(str(tmp_path / "a.db")); ledger.open()
    ledger.append(AuditEntry(utc="", user_id="u", role="ENGINEER", action="X",
                             object_id="o1", reason="why"))
    ledger.append(AuditEntry(utc="", user_id="v", role="ADMIN", action="Y",
                             object_id="o2"))
    assert ledger.count() == 2
    assert len(ledger.query(object_id="o1")) == 1
    assert len(ledger.query(action="Y")) == 1
    out = ledger.export(str(tmp_path / "audit.json"))
    assert (tmp_path / "audit.json").exists() and out.endswith("audit.json")


def test_audit_redacts_secrets():
    masked = redact({"password": "p", "token": "t", "nested": {"api_key": "k"},
                     "ok": "visible"})
    assert masked["password"] == "<redacted>" and masked["token"] == "<redacted>"
    assert masked["nested"]["api_key"] == "<redacted>"
    assert masked["ok"] == "visible"


def test_audit_redaction_applied_on_append(tmp_path):
    ledger = AuditLedger(str(tmp_path / "a.db")); ledger.open()
    ledger.append(AuditEntry(utc="", user_id="u", role="ADMIN", action="LOGIN",
                             detail={"password": "hunter2", "user": "u"}))
    row = ledger.query()[0]
    assert "hunter2" not in row["detail"] and "<redacted>" in row["detail"]


def test_audit_write_failure_fail_closed(tmp_path):
    svc, auth, ledger = _svc(tmp_path)
    ledger.fail_writes = True
    eng = Principal("alice", Role.ENGINEER.value)
    with pytest.raises(AuthorizationError):        # fail-closed: 拒绝敏感操作
        svc.publish("r", allow_offline_draft=True, principal=eng)
    assert svc.history("r") == []


def test_audit_write_failure_can_be_non_blocking(tmp_path):
    svc, auth, ledger = _svc(tmp_path)
    auth.fail_closed = False
    ledger.fail_writes = True
    eng = Principal("alice", Role.ENGINEER.value)
    rev = svc.publish("r", allow_offline_draft=True, principal=eng)
    assert rev.startswith("0.1-draft")


def test_query_audit_requires_permission(tmp_path):
    _svc_, auth, _l = _svc(tmp_path)
    with pytest.raises(AuthorizationError):
        auth.query_audit(Principal("u", Role.VIEWER.value))
    with pytest.raises(AuthorizationError):
        auth.export_audit(Principal("u", Role.OPERATOR.value), str(tmp_path / "x.json"))
    assert auth.query_audit(Principal("u", Role.ENGINEER.value)) == []
    assert auth.query_audit(Principal("u", Role.ADMIN.value)) == []


def test_ledger_is_append_only_and_not_claimed_tamper_proof():
    assert AuditLedger.tamper_proof is False        # 诚实标注: 不宣称防篡改
    for name in ("update", "delete", "remove"):
        assert not hasattr(AuditLedger, name)       # 无修改/删除接口


def test_role_change_in_flight_is_explainable(tmp_path):
    """角色变更后按新角色判定 (在途操作可解释): 同一用户降级后发布被拒。"""
    svc, auth, _ = _svc(tmp_path)
    alice = Principal("alice", Role.ENGINEER.value)
    svc.publish("r", allow_offline_draft=True, principal=alice)
    downgraded = Principal("alice", Role.OPERATOR.value)   # 角色变更
    with pytest.raises(AuthorizationError):
        svc.publish("r", allow_offline_draft=True, principal=downgraded)
