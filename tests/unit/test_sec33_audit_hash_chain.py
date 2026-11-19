"""Unit tests cho SEC-33: chuỗi băm bất biến trên audit_event và kiểm tra toàn vẹn."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import FetchedValue, insert
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.models import AuditEvent
from trading_bot.admin.audit_router import _query_audit_events
from trading_bot.app import create_app
from trading_bot.auth.policy import Permission, Principal, Role
from trading_bot.auth.route_policy import CsrfMode, RouteAccess, audit_app_routes
from trading_bot.auth.service import AuthIdentity
from trading_bot.observability.audit_chain import (
    GENESIS_PREV_HASH,
    AuditChainVerificationResult,
    compute_audit_row_hash,
    compute_hash_for_audit_event,
    verify_audit_chain,
    verify_audit_rows,
)
from trading_bot.observability.security_alerts import (
    InMemorySecurityAlertSink,
    SecurityAlertSeverity,
    SecurityAlertType,
    SecurityAnomalyDetector,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "migrations" / "versions" / "c9d1e3f5a7b4_sec32_33_audit_hash_chain.py"


def _make_chain_row(
    *,
    workspace_id: UUID,
    seq: int,
    prev_hash: str,
    action: str = "auth.login.succeeded",
    target_type: str = "session",
    reason: str | None = "Đăng nhập thành công, có dấu phẩy: ok",
    db_role: str = "crypidian_admin_web",
    occurred_at: datetime | None = None,
    recorded_at: datetime | None = None,
    actor_membership_id: UUID | None = None,
    target_id: UUID | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    request_id: UUID | None = None,
    session_hash: str | None = None,
    client_ip: str | None = "203.0.113.0/24",
    user_agent: str | None = "pytest-client/1.0",
    outcome: str | None = "success",
    event_id: UUID | None = None,
) -> SimpleNamespace:
    base_time = occurred_at or datetime(2026, 11, 18, 12, 0, seq, 123456, tzinfo=UTC)
    rec_time = recorded_at or (base_time + timedelta(milliseconds=5))
    eid = event_id or uuid4()
    rid = request_id or uuid4()
    row_hash = compute_audit_row_hash(
        id=eid,
        workspace_id=workspace_id,
        actor_membership_id=actor_membership_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        before_hash=before_hash,
        after_hash=after_hash,
        request_id=rid,
        occurred_at=base_time,
        seq=seq,
        prev_hash=prev_hash,
        db_role=db_role,
        recorded_at=rec_time,
        session_hash=session_hash,
        client_ip=client_ip,
        user_agent=user_agent,
        outcome=outcome,
    )
    return SimpleNamespace(
        id=eid,
        workspace_id=workspace_id,
        actor_membership_id=actor_membership_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        before_hash=before_hash,
        after_hash=after_hash,
        request_id=rid,
        occurred_at=base_time,
        seq=seq,
        prev_hash=prev_hash,
        row_hash=row_hash,
        db_role=db_role,
        recorded_at=rec_time,
        session_hash=session_hash,
        client_ip=client_ip,
        user_agent=user_agent,
        outcome=outcome,
    )


def _fake_session_factory(rows: list[Any]) -> Any:
    """Tạo async_sessionmaker giả trả về danh sách bản ghi AuditEvent cho kiểm thử đơn vị."""
    scalar_result = MagicMock()
    scalar_result.all.return_value = list(rows)

    session = MagicMock()
    session.info = {}
    session.begin = AsyncMock()
    session.close = AsyncMock()
    session.execute = AsyncMock()
    session.scalars = AsyncMock(return_value=scalar_result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.add = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=session)
    return factory


def test_sec33_migration_chains_from_c8d28da699c6_and_contains_hash_chain_ddl() -> None:
    assert MIGRATION_PATH.exists(), "Phải có migration SEC-32/33 nối từ head hiện tại"
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision: str = "c9d1e3f5a7b4"' in source
    assert 'down_revision: str | Sequence[str] | None = "f2a4c6e8b0d1"' in source
    for token in (
        "seq",
        "prev_hash",
        "row_hash",
        "db_role",
        "recorded_at",
        "session_hash",
        "client_ip",
        "user_agent",
        "outcome",
        "BEFORE INSERT ON public.audit_event",
        "BEFORE TRUNCATE ON public.audit_event",
        "REVOKE TRUNCATE ON TABLE public.audit_event",
        "crypidian_audit_chain_guard",
        "GENESIS",
        "pg_catalog.sha256",
        "pg_advisory_xact_lock",
        "CURRENT_USER",
        "pg_catalog.clock_timestamp()",
    ):
        assert token in source, f"Thiếu DDL bắt buộc trong migration: {token}"

    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    script = ScriptDirectory.from_config(alembic_cfg)
    assert script.get_heads() == ["e8a2c4d6f0b3"]


def test_audit_event_orm_has_hash_chain_columns_and_disables_implicit_returning() -> None:
    table = AuditEvent.__table__
    assert table.implicit_returning is False

    for column_name in (
        "seq",
        "prev_hash",
        "row_hash",
        "db_role",
        "recorded_at",
        "session_hash",
        "client_ip",
        "user_agent",
        "outcome",
    ):
        assert column_name in table.c, f"AuditEvent thiếu cột {column_name}"

    for trigger_col in ("seq", "prev_hash", "row_hash", "db_role", "recorded_at"):
        col = table.c[trigger_col]
        assert col.nullable is False
        assert isinstance(col.server_default, FetchedValue)

    for ctx_col in ("session_hash", "client_ip", "user_agent", "outcome"):
        assert table.c[ctx_col].nullable is True

    unique_names = {constraint.name for constraint in table.constraints if constraint.name}
    assert "uq_audit_event_workspace_seq" in unique_names

    compiled_sql = str(
        insert(AuditEvent)
        .values(
            workspace_id=uuid4(),
            action="auth.login.succeeded",
            target_type="session",
            request_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )
        .compile(dialect=postgresql.dialect())
    )
    assert "RETURNING" not in compiled_sql.upper()


def test_compute_audit_row_hash_is_deterministic_timezone_normalized_and_tamper_sensitive() -> None:
    workspace_id = uuid4()
    event_id = uuid4()
    request_id = uuid4()
    utc_time = datetime(2026, 11, 18, 12, 30, 45, 654321, tzinfo=UTC)
    vn_time = utc_time.astimezone(timezone(timedelta(hours=7)))

    digest_utc = compute_audit_row_hash(
        id=event_id,
        workspace_id=workspace_id,
        actor_membership_id=None,
        action="auth.login.succeeded",
        target_type="session",
        target_id=None,
        reason='Có dấu phẩy ", " và hai chấm " : " kèm tiếng Việt',
        before_hash=None,
        after_hash="a" * 64,
        request_id=request_id,
        occurred_at=utc_time,
        seq=1,
        prev_hash=GENESIS_PREV_HASH,
        db_role="crypidian_admin_web",
        recorded_at=utc_time,
        session_hash="b" * 64,
        client_ip="203.0.113.0/24",
        user_agent="Mozilla/5.0 (test)",
        outcome="success",
    )
    digest_vn = compute_audit_row_hash(
        id=event_id,
        workspace_id=workspace_id,
        actor_membership_id=None,
        action="auth.login.succeeded",
        target_type="session",
        target_id=None,
        reason='Có dấu phẩy ", " và hai chấm " : " kèm tiếng Việt',
        before_hash=None,
        after_hash="a" * 64,
        request_id=request_id,
        occurred_at=vn_time,
        seq=1,
        prev_hash=GENESIS_PREV_HASH,
        db_role="crypidian_admin_web",
        recorded_at=vn_time,
        session_hash="b" * 64,
        client_ip="203.0.113.0/24",
        user_agent="Mozilla/5.0 (test)",
        outcome="success",
    )
    assert len(digest_utc) == 64
    assert digest_utc == digest_vn

    # Kiểm tra khớp đúng công thức sha256(prev_hash || canonical_json)
    expected_canonical = json.dumps(
        {
            "action": "auth.login.succeeded",
            "actor_membership_id": None,
            "after_hash": "a" * 64,
            "before_hash": None,
            "client_ip": "203.0.113.0/24",
            "db_role": "crypidian_admin_web",
            "id": str(event_id),
            "occurred_at": "2026-11-18T12:30:45.654321+00:00",
            "outcome": "success",
            "prev_hash": GENESIS_PREV_HASH,
            "reason": 'Có dấu phẩy ", " và hai chấm " : " kèm tiếng Việt',
            "recorded_at": "2026-11-18T12:30:45.654321+00:00",
            "request_id": str(request_id),
            "seq": 1,
            "session_hash": "b" * 64,
            "target_id": None,
            "target_type": "session",
            "user_agent": "Mozilla/5.0 (test)",
            "workspace_id": str(workspace_id),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_digest = hashlib.sha256(
        (GENESIS_PREV_HASH + expected_canonical).encode("utf-8")
    ).hexdigest()
    assert digest_utc == expected_digest

    # Thay đổi bất kỳ trường bảo vệ nào cũng làm đổi row_hash
    tampered = compute_audit_row_hash(
        id=event_id,
        workspace_id=workspace_id,
        actor_membership_id=None,
        action="auth.login.succeeded",
        target_type="session",
        target_id=None,
        reason="Đã bị sửa",
        before_hash=None,
        after_hash="a" * 64,
        request_id=request_id,
        occurred_at=utc_time,
        seq=1,
        prev_hash=GENESIS_PREV_HASH,
        db_role="crypidian_admin_web",
        recorded_at=utc_time,
        session_hash="b" * 64,
        client_ip="203.0.113.0/24",
        user_agent="Mozilla/5.0 (test)",
        outcome="success",
    )
    assert tampered != digest_utc


def test_verify_audit_rows_valid_and_empty() -> None:
    workspace_id = uuid4()

    empty = verify_audit_rows(workspace_id, [])
    assert empty.verified is True
    assert empty.checked_count == 0
    assert empty.first_seq is None
    assert empty.last_seq is None
    assert empty.head_hash is None
    assert empty.violations == ()

    row1 = _make_chain_row(workspace_id=workspace_id, seq=1, prev_hash=GENESIS_PREV_HASH)
    row2 = _make_chain_row(workspace_id=workspace_id, seq=2, prev_hash=row1.row_hash)
    row3 = _make_chain_row(workspace_id=workspace_id, seq=3, prev_hash=row2.row_hash)

    result = verify_audit_rows(workspace_id, [row1, row2, row3])
    assert result.verified is True
    assert result.checked_count == 3
    assert result.first_seq == 1
    assert result.last_seq == 3
    assert result.head_hash == row3.row_hash
    assert result.violations == ()
    assert compute_hash_for_audit_event(row2) == row2.row_hash


def test_verify_audit_rows_detects_tampering_gaps_duplicates_and_anchor_mismatch() -> None:
    workspace_id = uuid4()
    row1 = _make_chain_row(workspace_id=workspace_id, seq=1, prev_hash=GENESIS_PREV_HASH)
    row2 = _make_chain_row(workspace_id=workspace_id, seq=2, prev_hash=row1.row_hash)
    row3 = _make_chain_row(workspace_id=workspace_id, seq=3, prev_hash=row2.row_hash)

    # 1. Sửa nội dung dòng 2
    tampered_row2 = SimpleNamespace(**row2.__dict__)
    tampered_row2.reason = "Sửa lén bởi superuser"
    res_tamper = verify_audit_rows(workspace_id, [row1, tampered_row2, row3])
    assert res_tamper.verified is False
    assert any(v.code == "ROW_HASH_MISMATCH" and v.seq == 2 for v in res_tamper.violations)

    # 2. Xóa dòng giữa (seq nhảy 1 -> 3)
    res_gap = verify_audit_rows(workspace_id, [row1, row3])
    assert res_gap.verified is False
    codes_gap = {v.code for v in res_gap.violations}
    assert "SEQ_GAP" in codes_gap
    assert "PREV_HASH_MISMATCH" in codes_gap

    # 3. Xóa dòng đầu (bắt đầu từ seq=2)
    res_missing_first = verify_audit_rows(workspace_id, [row2, row3])
    assert res_missing_first.verified is False
    assert any(v.code == "SEQ_GAP" and v.expected_seq == 1 for v in res_missing_first.violations)

    # 4. Sai genesis ở seq=1
    bad_genesis = _make_chain_row(workspace_id=workspace_id, seq=1, prev_hash="0" * 64)
    res_genesis = verify_audit_rows(workspace_id, [bad_genesis])
    assert res_genesis.verified is False
    assert any(v.code == "PREV_HASH_MISMATCH" and v.seq == 1 for v in res_genesis.violations)

    # 5. Trùng seq
    dup_row2 = _make_chain_row(workspace_id=workspace_id, seq=2, prev_hash=row1.row_hash)
    res_dup = verify_audit_rows(workspace_id, [row1, row2, dup_row2])
    assert res_dup.verified is False
    assert any(v.code == "DUPLICATE_SEQ" and v.seq == 2 for v in res_dup.violations)

    # 6. Thiếu metadata chuỗi băm
    incomplete = SimpleNamespace(**row1.__dict__)
    incomplete.row_hash = None
    res_missing_meta = verify_audit_rows(workspace_id, [incomplete])
    assert res_missing_meta.verified is False
    assert any(v.code == "MISSING_CHAIN_METADATA" for v in res_missing_meta.violations)

    # 7. Lệch mốc neo đầu cuối (xóa dòng cuối)
    res_tail = verify_audit_rows(
        workspace_id,
        [row1, row2],
        expected_min_last_seq=3,
        expected_head_hash=row3.row_hash,
    )
    assert res_tail.verified is False
    assert any(v.code == "TAIL_ANCHOR_MISMATCH" for v in res_tail.violations)


@pytest.mark.asyncio
async def test_verify_audit_chain_emits_critical_alert_when_broken() -> None:
    workspace_id = uuid4()
    row1 = _make_chain_row(workspace_id=workspace_id, seq=1, prev_hash=GENESIS_PREV_HASH)
    row2 = _make_chain_row(workspace_id=workspace_id, seq=2, prev_hash=row1.row_hash)
    row2.action = "tampered.action"

    sink = InMemorySecurityAlertSink()
    detector = SecurityAnomalyDetector(sink=sink)
    sessions = _fake_session_factory([row1, row2])

    result = await verify_audit_chain(
        sessions,
        workspace_id=workspace_id,
        anomaly_detector=detector,
    )
    assert isinstance(result, AuditChainVerificationResult)
    assert result.verified is False
    assert len(sink.alerts) == 1
    alert = sink.alerts[0]
    assert alert.alert_type is SecurityAlertType.AUDIT_CHAIN_BROKEN
    assert alert.severity is SecurityAlertSeverity.CRITICAL
    assert alert.workspace_id == workspace_id
    assert alert.metadata["violation_count"] >= 1


@pytest.mark.asyncio
async def test_query_audit_events_exposes_hash_chain_and_sec32_fields() -> None:
    workspace_id = uuid4()
    row1 = _make_chain_row(
        workspace_id=workspace_id,
        seq=1,
        prev_hash=GENESIS_PREV_HASH,
        session_hash="c" * 64,
        client_ip="198.51.100.0/24",
        user_agent="CrypidianAdmin/1.0",
        outcome="success",
    )
    sessions = _fake_session_factory([row1])
    items = await _query_audit_events(sessions, workspace_id=workspace_id)
    assert len(items) == 1
    item = items[0]
    assert item["seq"] == 1
    assert item["prev_hash"] == GENESIS_PREV_HASH
    assert item["row_hash"] == row1.row_hash
    assert item["db_role"] == "crypidian_admin_web"
    assert item["recorded_at"] == row1.recorded_at.isoformat()
    assert item["session_hash"] == "c" * 64
    assert item["client_ip"] == "198.51.100.0/24"
    assert item["user_agent"] == "CrypidianAdmin/1.0"
    assert item["outcome"] == "success"


def test_audit_verify_http_endpoint_has_route_policy_and_returns_verification() -> None:
    workspace_id = uuid4()
    membership_id = uuid4()
    user_id = uuid4()
    row1 = _make_chain_row(workspace_id=workspace_id, seq=1, prev_hash=GENESIS_PREV_HASH)
    row2 = _make_chain_row(workspace_id=workspace_id, seq=2, prev_hash=row1.row_hash)

    identity = AuthIdentity(
        principal=Principal(
            user_id=user_id,
            membership_id=membership_id,
            workspace_id=workspace_id,
            roles=frozenset({Role.OWNER}),
            mfa_verified_at=datetime.now(UTC),
            session_id=uuid4(),
        ),
        email="owner@example.test",
    )

    class _StubAuthService:
        async def authenticate(self, cookie: str | None) -> AuthIdentity:
            assert cookie == "sid.token"
            return identity

        async def verify_csrf(self, cookie: str | None, csrf_token: str | None) -> None:
            return None

    app = create_app()
    app.state.auth_service = _StubAuthService()
    app.state.session_factory = _fake_session_factory([row1, row2])
    policies = {
        (record.path, record.methods): record.policy for record in audit_app_routes(app).audited
    }
    assert ("/api/v1/audit/verify", frozenset({"GET"})) in policies
    policy = policies[("/api/v1/audit/verify", frozenset({"GET"}))]
    assert policy is not None
    assert policy.access is RouteAccess.PERMISSION
    assert policy.csrf is CsrfMode.NONE
    assert Permission.WORKSPACE_READ in policy.permissions

    client = TestClient(app)
    response = client.get(
        "/api/v1/audit/verify",
        cookies={"crypidian_session": "sid.token"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is True
    assert body["checked_count"] == 2
    assert body["first_seq"] == 1
    assert body["last_seq"] == 2
    assert body["head_hash"] == row2.row_hash
    assert body["violations"] == []
