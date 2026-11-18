"""Unit tests cho WP-04A (SEC-18, SEC-26, members/watchlist) và WP-04C (maker-checker, audit)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import APIRouter

from trading_bot.admin.settings_service import SettingsAdminError, SettingsAdminService
from trading_bot.app import create_app
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.route_policy import iter_app_routes, verify_app_routes_registered
from trading_bot.config.settings import AppSettings


def _principal(membership_id=None, roles=(Role.OWNER,)) -> Principal:
    return Principal(
        user_id=uuid4(),
        membership_id=membership_id or uuid4(),
        workspace_id=uuid4(),
        roles=frozenset(roles),
        mfa_verified_at=datetime.now(UTC),
        mfa_method=MfaMethod.TOTP,
    )


def test_sec18_all_app_routes_registered_deny_by_default() -> None:
    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    verify_app_routes_registered(app)

    # Thêm route chưa khai báo phải bị chặn fail-closed
    rogue = APIRouter()

    @rogue.get("/api/v1/rogue-unregistered")
    def _rogue() -> dict[str, str]:
        return {"ok": "no"}

    app.include_router(rogue)
    with pytest.raises(AssertionError, match="rogue-unregistered"):
        verify_app_routes_registered(app)


def test_sec26_openapi_disabled_in_production_and_admin_routers_isolated() -> None:
    prod_app = create_app(
        AppSettings(app_env="production", process_role="admin-web", allowed_hosts="testserver")
    )
    assert prod_app.openapi_url is None

    worker_app = create_app(
        AppSettings(
            app_env="test",
            process_role="ingestion-worker",
            ingestion_workspace_id=uuid4(),
        )
    )
    assert worker_app.openapi_url is None  # SEC-26: worker ngoài admin-web luôn tắt OpenAPI
    worker_paths = {path for _m, path, _r in iter_app_routes(worker_app)}
    assert "/api/v1/settings/current" not in worker_paths
    assert "/api/v1/members" not in worker_paths
    assert "/api/v1/audit" not in worker_paths
    assert "/health/live" in worker_paths


def test_new_admin_routes_have_vietnamese_openapi_summaries() -> None:
    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    schema = app.openapi()
    for path in (
        "/api/v1/members",
        "/api/v1/watchlists",
        "/api/v1/incidents",
        "/api/v1/experiments",
        "/api/v1/audit",
        "/api/v1/settings/drafts/{draft_id}/propose",
        "/api/v1/settings/drafts/{draft_id}/approve",
    ):
        assert path in schema["paths"], f"Thiếu path {path} trong OpenAPI"


@pytest.mark.asyncio
async def test_wp04c_maker_checker_enforced_on_propose_and_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maker = _principal(roles=(Role.STRATEGY_ADMIN,))
    checker = _principal(roles=(Role.OWNER,))
    draft_id = uuid4()
    now = datetime.now(UTC)
    draft_record = SimpleNamespace(
        id=draft_id,
        workspace_id=maker.workspace_id,
        version=2,
        parent_version_id=None,
        status="DRAFT",
        revision=1,
        settings_json={"schema_version": 1},
        settings_hash="abc123",
        created_by=maker.membership_id,
        proposed_by=None,
        approved_by=None,
        approval_reason=None,
        published_at=None,
        effective_at=None,
        created_at=now,
    )

    class _FakeRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def lock_workspace(self, _ws: object) -> None:
            return None

        async def list_versions(self, *, workspace_id: object) -> list[object]:
            return [draft_record]

        async def propose_draft(
            self,
            *,
            workspace_id: object,
            draft_id: object,
            expected_revision: int,
            proposed_by: object,
        ) -> object:
            draft_record.status = "PROPOSED"
            draft_record.proposed_by = proposed_by
            draft_record.revision = expected_revision + 1
            return draft_record

        async def approve_proposed(
            self,
            *,
            workspace_id: object,
            draft_id: object,
            expected_revision: int,
            approved_by: object,
            reason: str,
        ) -> object:
            draft_record.status = "APPROVED"
            draft_record.approved_by = approved_by
            draft_record.approval_reason = reason
            draft_record.revision = expected_revision + 1
            return draft_record

    class _FakeUow:
        def __init__(self, *_a: object, **_kw: object) -> None:
            self.session = object()
            self.workspace_id = maker.workspace_id
            self.request_id = uuid4()
            self.audits: list[dict[str, object]] = []

        async def __aenter__(self) -> _FakeUow:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        def record_audit(self, **kwargs: object) -> None:
            self.audits.append(kwargs)

        def enqueue(self, _env: object) -> None:
            return None

    monkeypatch.setattr("trading_bot.admin.settings_service.WorkspaceSettingsRepository", _FakeRepo)
    monkeypatch.setattr("trading_bot.admin.settings_service.UnitOfWork", _FakeUow)

    service = SettingsAdminService(AsyncMock())
    proposed = await service.propose_draft(
        maker, draft_id=draft_id, expected_revision=1, reason="Cập nhật chiến lược"
    )
    assert proposed["status"] == "PROPOSED"

    # Maker không thể tự duyệt đề xuất của chính mình (SEC-21)
    with pytest.raises(SettingsAdminError) as self_approve_err:
        await service.approve_proposed(
            maker, draft_id=draft_id, expected_revision=2, reason="Tự duyệt"
        )
    assert self_approve_err.value.status_code == 403

    # Bản PROPOSED chưa duyệt không thể publish
    with pytest.raises(SettingsAdminError) as publish_proposed_err:
        await service.publish_draft(checker, draft_id=draft_id, expected_revision=2)
    assert publish_proposed_err.value.status_code == 409

    # Checker khác duyệt thành công
    approved = await service.approve_proposed(
        checker, draft_id=draft_id, expected_revision=2, reason="Đã kiểm tra tham số"
    )
    assert approved["status"] == "APPROVED"
    assert approved["approved_by"] == str(checker.membership_id)
