"""Kiểm ranh giới HTML policy nguồn và form CSRF."""

from uuid import uuid4

import pytest
from tests.unit.test_research_source_admin_api import client, identity

from trading_bot.auth.policy import Role
from trading_bot.auth.service import AuthenticationFailure


class AuthService:
    async def issue_csrf_token(self, cookie):
        return "html-csrf"

    async def verify_csrf(self, cookie, token):
        if token != "html-csrf":
            raise AuthenticationFailure(403, "CSRF không hợp lệ.")


def html_client(role=Role.OWNER):
    http, service, actor = client(actor=identity(role))
    http.app.state.auth_service = AuthService()
    return http, service


def test_html_safe_detail_and_permission_actions():
    http, service = html_client()
    page = http.get("/admin/research-sources?source_id=calendar")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Lịch sử policy" in page.text
    assert (
        "Evidence tham chiếu do người quản trị cung cấp — chưa được hệ thống xác minh pháp lý"
        in page.text
    )
    assert 'href="https://example.org/calendar"' not in page.text
    assert "Tạo nguồn ứng viên" in page.text
    http, _ = html_client(Role.ANALYST_REVIEWER)
    assert "Tạo nguồn ứng viên" not in http.get("/admin/research-sources").text
    http, _ = html_client(Role.OPERATOR)
    assert http.get("/admin/research-sources").status_code == 403


@pytest.mark.parametrize("action", ["create", "revision", "approve"])
def test_html_csrf_precedes_every_mutation(action):
    http, service = html_client()
    response = http.post("/admin/research-sources/" + action, data={"source_id": "calendar"})
    assert response.status_code == 403
    assert service.facade.calls == []


def test_html_mutation_schema_and_authorization():
    http, service = html_client()
    data = {
        "csrf_token": "html-csrf",
        "idempotency_key": str(uuid4()),
        "source_id": "calendar",
        "state": "blocked",
        "reason": "Chặn",
    }
    assert (
        http.post("/admin/research-sources/revision", data=data, follow_redirects=False).status_code
        == 303
    )
    assert service.facade.calls[0][0] == "disable"
    before = len(service.facade.calls)
    assert (
        http.post(
            "/admin/research-sources/revision", data=data | {"actor_id": str(uuid4())}
        ).status_code
        == 422
    )
    assert len(service.facade.calls) == before
    http, service = html_client(Role.ANALYST_REVIEWER)
    assert http.post("/admin/research-sources/revision", data=data).status_code == 403
    assert service.facade.calls == []


def test_html_pending_without_typed_scope_and_direct_verified_fail_closed():
    http, service = html_client()
    data = {
        "csrf_token": "html-csrf",
        "idempotency_key": str(uuid4()),
        "source_id": "calendar",
        "state": "rights_pending",
        "reason": "Xin duyệt",
        "evidence_ref": "terms",
        "purpose_metadata": "on",
        "window_seconds": "7200",
        "max_requests": "20",
        "max_bytes": "2000",
        "max_cost": "2.50",
        "cost_unit": "USD",
    }
    response = http.post("/admin/research-sources/revision", data=data, follow_redirects=False)
    assert response.status_code == 422
    assert service.facade.calls == []
    before = len(service.facade.calls)
    assert (
        http.post("/admin/research-sources/revision", data=data | {"state": "verified"}).status_code
        == 422
    )
    assert len(service.facade.calls) == before


def pending_client():
    from dataclasses import replace
    from datetime import UTC, datetime

    from tests.unit.test_research_source_admin_api import SOURCE

    from trading_bot.domain.research.source_policy import SourcePolicyState, SourceUsePurpose
    from trading_bot.domain.research.source_registry import SourceRegistryEntry

    http, service = html_client()
    expiry = datetime(2027, 1, 1, tzinfo=UTC)
    revision = replace(
        SOURCE.current,
        sequence=2,
        policy=replace(
            SOURCE.current.policy,
            policy_id="pending",
            state=SourcePolicyState.RIGHTS_PENDING,
            evidence_ref="terms",
            valid_until=expiry,
            allowed_purposes=frozenset(
                {SourceUsePurpose.DISPLAY, SourceUsePurpose.BACKTEST_RESEARCH}
            ),
        ),
    )
    entry = SourceRegistryEntry(SOURCE.source_id, (SOURCE.current, revision))

    async def get_source(source_id):
        return entry, None

    service.facade.get_source = get_source
    return http, service, revision, expiry


@pytest.mark.parametrize(
    ("mode", "value", "expected"),
    [
        ("preserve", "", "2027-01-01T00:00:00+00:00"),
        ("preserve", "2030-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00"),
        ("clear", "", None),
        ("change", "2028-01-01T00:00:00+00:00", "2028-01-01T00:00:00+00:00"),
    ],
)
def test_html_approval_preserves_server_expiry_unless_explicit_action(mode, value, expected):
    http, service, revision, expiry = pending_client()
    data = {
        "csrf_token": "html-csrf",
        "idempotency_key": str(uuid4()),
        "source_id": "calendar",
        "policy_id": revision.policy.policy_id,
        "reason": "Duyệt",
        "purpose_display": "on",
        "purpose_backtest_research": "on",
        "expiry_action": mode,
        "valid_until": value,
    }
    response = http.post("/admin/research-sources/approve", data=data, follow_redirects=False)
    assert response.status_code == 303
    command = service.facade.calls[-1][3]
    assert {str(p) for p in command.allowed_purposes} == {"display", "backtest_research"}
    assert (command.valid_until.isoformat() if command.valid_until else None) == expected


def test_html_approval_expiry_change_requires_finite_value_and_no_stale_snapshot():
    http, service, revision, expiry = pending_client()
    data = {
        "csrf_token": "html-csrf",
        "idempotency_key": str(uuid4()),
        "source_id": "calendar",
        "policy_id": revision.policy.policy_id,
        "reason": "Duyệt",
        "purpose_display": "on",
        "expiry_action": "change",
        "valid_until": "",
    }
    assert http.post("/admin/research-sources/approve", data=data).status_code == 422
    assert not any(call[0] == "approve" for call in service.facade.calls)
    assert (
        http.post(
            "/admin/research-sources/approve",
            data=data | {"expiry_action": "preserve", "policy_id": "stale"},
        ).status_code
        == 409
    )


def test_html_approval_retry_uses_referenced_pending_expiry_after_success():
    from dataclasses import replace

    from tests.unit.test_research_source_admin_api import SOURCE

    from trading_bot.domain.research.source_policy import SourcePolicyState
    from trading_bot.domain.research.source_registry import SourceRegistryEntry

    http, service, pending, expiry = pending_client()
    approved = replace(
        pending,
        sequence=3,
        approved_by=str(uuid4()),
        policy=replace(
            pending.policy,
            policy_id="approved",
            state=SourcePolicyState.VERIFIED,
            reviewed_at=pending.created_at,
        ),
    )
    entry = SourceRegistryEntry(SOURCE.source_id, (SOURCE.current, pending, approved))

    async def get_source(source_id):
        return entry, None

    service.facade.get_source = get_source
    response = http.post(
        "/admin/research-sources/approve",
        data={
            "csrf_token": "html-csrf",
            "idempotency_key": str(uuid4()),
            "source_id": "calendar",
            "policy_id": pending.policy.policy_id,
            "reason": "Duyệt",
            "purpose_display": "on",
            "expiry_action": "preserve",
            "valid_until": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert service.facade.calls[-1][3].valid_until == expiry
