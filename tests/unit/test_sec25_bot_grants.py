"""SEC-25: thời hạn, lý do, audit grant/revoke và báo cáo rà soát quyền BotGrant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from trading_bot.adapters.database.models import AuditEvent, BotGrant, BotInstance, UserMembership
from trading_bot.adapters.database.repositories.bot_grants import (
    BotGrantRepository,
    BotGrantStatus,
    classify_bot_grant,
)
from trading_bot.admin.bot_grant_service import BotGrantAdminError, BotGrantAdminService
from trading_bot.app import create_app
from trading_bot.auth.bot_policy import BotPermission
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.router import current_identity, require_csrf
from trading_bot.auth.service import AuthIdentity
from trading_bot.config.settings import AppSettings


def _principal(role: Role, *, workspace_id: UUID | None = None) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=uuid4(),
        workspace_id=workspace_id or uuid4(),
        roles=frozenset({role}),
        mfa_verified_at=datetime.now(UTC),
    )


class MemoryGrantSession:
    """Session giả lưu BotGrant và AuditEvent để kiểm tra logic repository."""

    def __init__(self, *, bot_id: UUID, member_id: UUID, workspace_id: UUID) -> None:
        self.bot_id = bot_id
        self.member_id = member_id
        self.workspace_id = workspace_id
        self.grants: list[BotGrant] = []
        self.audits: list[AuditEvent] = []
        self.info: dict[str, Any] = {}
        self.committed = False

    async def begin(self) -> None:
        return None

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    def add(self, row: object) -> None:
        if isinstance(row, BotGrant):
            if row.created_at is None:
                row.created_at = datetime.now(UTC)
            self.grants.append(row)
        elif isinstance(row, AuditEvent):
            self.audits.append(row)
        else:
            raise TypeError(f"Không hỗ trợ entity: {type(row)!r}")

    async def scalar(self, query: Any) -> Any:
        entity = query.column_descriptions[0]["entity"]
        if entity is BotInstance:
            return SimpleNamespace(id=self.bot_id, workspace_id=self.workspace_id)
        if entity is UserMembership:
            return SimpleNamespace(
                id=self.member_id, workspace_id=self.workspace_id, status="ACTIVE"
            )
        if entity is BotGrant:
            return next((g for g in self.grants if g.revoked_at is None), None)
        raise AssertionError(f"Unexpected scalar query entity: {entity!r}")

    async def scalars(self, query: Any) -> Any:
        entity = query.column_descriptions[0]["entity"]
        assert entity is BotGrant
        rows = sorted(
            (g for g in self.grants if g.workspace_id == self.workspace_id),
            key=lambda g: (g.created_at or datetime.min.replace(tzinfo=UTC), g.id),
            reverse=True,
        )
        return SimpleNamespace(all=lambda: list(rows))


class MemorySessionFactory:
    def __init__(self, session: MemoryGrantSession) -> None:
        self._session = session

    def __call__(self) -> MemoryGrantSession:
        return self._session


def test_bot_grant_model_and_migration_have_expiry_reason_and_history_guard() -> None:
    table = BotGrant.__table__
    assert {"expires_at", "reason", "revoke_reason"} <= set(table.columns.keys())

    migration = Path("migrations/versions/e9f1a3b5c7d9_sec25_bot_grant_expiry_reason.py").read_text(
        encoding="utf-8"
    )
    assert 'down_revision: str | None = "d8e2f4a6b8c0"' in migration
    assert "expires_at" in migration
    assert "reason" in migration
    assert "revoke_reason" in migration
    assert "guard_bot_grant_history" in migration


@pytest.mark.asyncio
async def test_expired_grant_does_not_authorize_and_regrant_closes_stale_row() -> None:
    owner = _principal(Role.OWNER)
    operator = _principal(Role.OPERATOR, workspace_id=owner.workspace_id)
    bot_id = uuid4()
    session = MemoryGrantSession(
        bot_id=bot_id, member_id=operator.membership_id, workspace_id=owner.workspace_id
    )
    repo = BotGrantRepository(session)  # type: ignore[arg-type]
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="tương lai|lớn hơn"):
        await repo.grant(
            owner,
            bot_id=bot_id,
            membership_id=operator.membership_id,
            permission=BotPermission.RUNTIME_PAUSE,
            expires_at=t0 - timedelta(minutes=1),
            reason="Cấp thử hạn quá khứ",
            now=t0,
        )

    first = await repo.grant(
        owner,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.RUNTIME_PAUSE,
        expires_at=t0 + timedelta(hours=2),
        reason="Trực ca xử lý biến động BTC",
        now=t0,
    )
    assert first.expires_at == t0 + timedelta(hours=2)
    assert first.reason == "Trực ca xử lý biến động BTC"

    await repo.require(
        operator,
        bot_id=bot_id,
        permission=BotPermission.RUNTIME_PAUSE,
        now=t0 + timedelta(hours=1),
    )

    with pytest.raises(PermissionError, match="hết hạn|Thiếu grant"):
        await repo.require(
            operator,
            bot_id=bot_id,
            permission=BotPermission.RUNTIME_PAUSE,
            now=t0 + timedelta(hours=2),
        )

    # Cấp lại sau khi hết hạn phải đóng bản ghi cũ và phát sinh bản ghi mới.
    second = await repo.grant(
        owner,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.RUNTIME_PAUSE,
        expires_at=t0 + timedelta(hours=6),
        reason="Gia hạn ca trực mới",
        now=t0 + timedelta(hours=2, minutes=1),
    )
    assert second.id != first.id
    assert first.revoked_at == t0 + timedelta(hours=2, minutes=1)
    assert first.revoke_reason is not None
    await repo.require(
        operator,
        bot_id=bot_id,
        permission=BotPermission.RUNTIME_PAUSE,
        now=t0 + timedelta(hours=3),
    )


@pytest.mark.asyncio
async def test_grant_and_revoke_record_audit_events_with_reason_and_expiry() -> None:
    owner = _principal(Role.OWNER)
    operator = _principal(Role.OPERATOR, workspace_id=owner.workspace_id)
    bot_id = uuid4()
    session = MemoryGrantSession(
        bot_id=bot_id, member_id=operator.membership_id, workspace_id=owner.workspace_id
    )
    repo = BotGrantRepository(session)  # type: ignore[arg-type]
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    exp = t0 + timedelta(days=3)
    req_grant = uuid4()
    req_revoke = uuid4()

    grant_row = await repo.grant(
        owner,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.READ,
        expires_at=exp,
        reason="Kiểm tra báo cáo bot A",
        request_id=req_grant,
        now=t0,
    )
    assert len(session.audits) == 1
    grant_audit = session.audits[0]
    assert grant_audit.action == "bot.grant.granted"
    assert grant_audit.target_type == "bot_grant"
    assert grant_audit.target_id == grant_row.id
    assert grant_audit.actor_membership_id == owner.membership_id
    assert grant_audit.request_id == req_grant
    assert grant_audit.reason is not None
    assert "Kiểm tra báo cáo bot A" in grant_audit.reason
    assert exp.isoformat() in grant_audit.reason
    assert grant_audit.after_hash is not None

    revoked = await repo.revoke(
        owner,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.READ,
        reason="Kết thúc nhiệm vụ kiểm tra",
        request_id=req_revoke,
        now=t0 + timedelta(hours=1),
    )
    assert revoked is True
    assert grant_row.revoke_reason == "Kết thúc nhiệm vụ kiểm tra"
    assert len(session.audits) == 2
    revoke_audit = session.audits[1]
    assert revoke_audit.action == "bot.grant.revoked"
    assert revoke_audit.target_id == grant_row.id
    assert revoke_audit.request_id == req_revoke
    assert revoke_audit.reason is not None
    assert "Kết thúc nhiệm vụ kiểm tra" in revoke_audit.reason
    assert exp.isoformat() in revoke_audit.reason
    assert revoke_audit.before_hash is not None
    assert revoke_audit.after_hash is not None
    assert revoke_audit.before_hash != revoke_audit.after_hash


@pytest.mark.asyncio
async def test_periodic_access_review_classifies_active_expiring_soon_expired_and_revoked() -> None:
    owner = _principal(Role.OWNER)
    operator = _principal(Role.OPERATOR, workspace_id=owner.workspace_id)
    bot_id = uuid4()
    session = MemoryGrantSession(
        bot_id=bot_id, member_id=operator.membership_id, workspace_id=owner.workspace_id
    )
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

    g_active = BotGrant(
        id=uuid4(),
        workspace_id=owner.workspace_id,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.READ.value,
        granted_by=owner.membership_id,
        expires_at=t0 + timedelta(days=30),
        reason="Quyền đọc dài hạn",
        created_at=t0 - timedelta(days=1),
    )
    g_soon = BotGrant(
        id=uuid4(),
        workspace_id=owner.workspace_id,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.RUNTIME_PAUSE.value,
        granted_by=owner.membership_id,
        expires_at=t0 + timedelta(days=2),
        reason="Trực cuối tuần",
        created_at=t0 - timedelta(days=5),
    )
    g_expired = BotGrant(
        id=uuid4(),
        workspace_id=owner.workspace_id,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.RUNTIME_RESUME.value,
        granted_by=owner.membership_id,
        expires_at=t0 - timedelta(hours=1),
        reason="Quyền tạm đã quá hạn",
        created_at=t0 - timedelta(days=10),
    )
    g_revoked = BotGrant(
        id=uuid4(),
        workspace_id=owner.workspace_id,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.REPORT_READ.value,
        granted_by=owner.membership_id,
        expires_at=t0 + timedelta(days=10),
        reason="Cấp đọc báo cáo",
        revoked_at=t0 - timedelta(minutes=30),
        revoke_reason="Đã thu hồi sớm",
        created_at=t0 - timedelta(days=2),
    )
    session.grants.extend([g_active, g_soon, g_expired, g_revoked])

    assert classify_bot_grant(g_active, as_of=t0) is BotGrantStatus.ACTIVE
    assert classify_bot_grant(g_soon, as_of=t0) is BotGrantStatus.EXPIRING_SOON
    assert classify_bot_grant(g_expired, as_of=t0) is BotGrantStatus.EXPIRED
    assert classify_bot_grant(g_revoked, as_of=t0) is BotGrantStatus.REVOKED

    review = await BotGrantRepository(session).review_grants(  # type: ignore[arg-type]
        workspace_id=owner.workspace_id,
        as_of=t0,
        expiring_within=timedelta(days=7),
    )
    assert {item.id for item in review.active} == {g_active.id, g_soon.id}
    assert [item.id for item in review.expiring_soon] == [g_soon.id]
    assert [item.id for item in review.expired] == [g_expired.id]
    assert [item.id for item in review.revoked] == [g_revoked.id]
    assert review.summary == {"active": 2, "expiring_soon": 1, "expired": 1, "revoked": 1}


@pytest.mark.asyncio
async def test_bot_grant_admin_service_and_router_end_to_end() -> None:
    owner = _principal(Role.OWNER)
    operator = _principal(Role.OPERATOR, workspace_id=owner.workspace_id)
    bot_id = uuid4()
    session = MemoryGrantSession(
        bot_id=bot_id, member_id=operator.membership_id, workspace_id=owner.workspace_id
    )
    service = BotGrantAdminService(MemorySessionFactory(session))  # type: ignore[arg-type]

    # Non-owner bị từ chối ở service & API.
    with pytest.raises(BotGrantAdminError) as denied:
        await service.grant(
            operator,
            bot_id=bot_id,
            membership_id=operator.membership_id,
            permission=BotPermission.READ,
            reason="Tự cấp quyền",
        )
    assert denied.value.status_code == 403

    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    app.state.bot_grant_service = service
    app.dependency_overrides[current_identity] = lambda: AuthIdentity(
        principal=owner, email="owner@example.test"
    )
    app.dependency_overrides[require_csrf] = lambda: AuthIdentity(
        principal=owner, email="owner@example.test"
    )
    client = TestClient(app)

    create_resp = client.post(
        "/api/v1/bot-grants",
        json={
            "bot_id": str(bot_id),
            "membership_id": str(operator.membership_id),
            "permission": BotPermission.RUNTIME_PAUSE.value,
            "reason": "Cấp quyền pause trong ca trực",
            "expires_at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
    )
    assert create_resp.status_code == 201
    assert create_resp.headers["cache-control"] == "no-store"
    grant_payload = create_resp.json()["grant"]
    assert grant_payload["reason"] == "Cấp quyền pause trong ca trực"
    assert grant_payload["expires_at"] is not None

    review_resp = client.get(
        "/api/v1/bot-grants/review?expiring_within_days=7&include_revoked=true"
    )
    assert review_resp.status_code == 200
    assert review_resp.headers["cache-control"] == "no-store"
    report = review_resp.json()["review"]
    assert report["summary"] == {"active": 1, "expiring_soon": 1, "expired": 0, "revoked": 0}
    assert len(report["expiring_soon"]) == 1

    review_audit_resp = client.post(
        "/api/v1/bot-grants/review",
        json={"expiring_within_days": 7, "reason": "Rà soát quyền định kỳ hàng tuần"},
    )
    assert review_audit_resp.status_code == 200
    assert any(a.action == "bot.grant.access_reviewed" for a in session.audits)

    revoke_resp = client.post(
        "/api/v1/bot-grants/revoke",
        json={
            "bot_id": str(bot_id),
            "membership_id": str(operator.membership_id),
            "permission": BotPermission.RUNTIME_PAUSE.value,
            "reason": "Thu hồi sau ca trực",
        },
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["revoked"] is True
    assert any(a.action == "bot.grant.revoked" for a in session.audits)

    # Mặc định 90 ngày khi không truyền expires_at và permanent=False; không còn quyền nếu MFA cũ.
    default_grant = await service.grant(
        owner,
        bot_id=bot_id,
        membership_id=operator.membership_id,
        permission=BotPermission.READ,
        reason="Cấp quyền mặc định 90 ngày",
    )
    assert default_grant["expires_at"] is not None

    stale_owner = Principal(
        user_id=owner.user_id,
        membership_id=owner.membership_id,
        workspace_id=owner.workspace_id,
        roles=owner.roles,
        mfa_verified_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    with pytest.raises(BotGrantAdminError) as mfa_error:
        await service.grant(
            stale_owner,
            bot_id=bot_id,
            membership_id=operator.membership_id,
            permission=BotPermission.READ,
            reason="MFA hết hạn",
        )
    assert mfa_error.value.status_code == 403
