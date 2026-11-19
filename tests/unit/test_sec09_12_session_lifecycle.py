"""Kiểm thử TDD (SEC-09..12): vòng đời phiên, xoay token, thu hồi và timing-safe."""

from __future__ import annotations

import importlib.util
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pyotp
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from trading_bot.adapters.database.models import (
    AuditEvent,
    MfaFactor,
    RoleBinding,
    Session,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.app import create_app
from trading_bot.auth.cookies import session_cookie_name
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.route_policy import assert_all_routes_have_policy
from trading_bot.auth.router import current_identity, get_auth_service, require_csrf
from trading_bot.auth.security import PasswordManager, TotpManager
from trading_bot.auth.service import AuthenticationFailure, AuthIdentity, AuthService
from trading_bot.config.settings import AppSettings
from trading_bot.ports.secrets import SecretProvider

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "f0912a3b4c5d_sec09_11_session_lifecycle_and_membership_status.py"
)
BASE_TIME = datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC)


class _VirtualClock:
    def __init__(self, start: datetime = BASE_TIME) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


class _MemorySecrets(SecretProvider):
    def __init__(self) -> None:
        self.items: dict[tuple[UUID, str], str] = {}

    def store_mfa_secret(self, workspace_id: UUID, secret: str) -> str:
        ref = f"ref-{uuid4().hex}"
        self.items[(workspace_id, ref)] = secret
        return ref

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str:
        return self.items[(workspace_id, reference)]

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None:
        self.items.pop((workspace_id, reference), None)

    def resolve_telegram_bot_token(
        self, reference: str, *, workspace_id: UUID | None = None
    ) -> Any:
        from trading_bot.ports.secrets import SecretAccessError

        del workspace_id
        raise SecretAccessError(f"telegram token not configured: {reference}")


@dataclass
class _MemoryStore:
    workspaces: dict[UUID, Workspace] = field(default_factory=dict)
    users: dict[UUID, UserAccount] = field(default_factory=dict)
    memberships: dict[UUID, UserMembership] = field(default_factory=dict)
    roles: list[RoleBinding] = field(default_factory=list)
    factors: list[MfaFactor] = field(default_factory=list)
    sessions: dict[UUID, Session] = field(default_factory=dict)
    audits: list[AuditEvent] = field(default_factory=list)
    outbox: list[Any] = field(default_factory=list)


class _FakeBeginContext:
    def __init__(self, session: _MemoryAsyncSession) -> None:
        self._session = session

    def __await__(self) -> Any:
        async def _started() -> _FakeBeginContext:
            return self

        return _started().__await__()

    async def __aenter__(self) -> _FakeBeginContext:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc, tb
        if exc_type is None:
            await self._session.commit()
        else:
            await self._session.rollback()


class _FakeScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


def _extract_filter_predicates(stmt: Any) -> dict[tuple[str, str], tuple[str, Any]]:
    """Trích xuất điều kiện lọc SQLAlchemy đơn giản phục vụ kiểm thử bộ nhớ."""
    predicates: dict[tuple[str, str], tuple[str, Any]] = {}
    criteria = getattr(stmt, "_where_criteria", ())
    stack = list(criteria)
    while stack:
        node = stack.pop(0)
        clauses = getattr(node, "clauses", None)
        if clauses is not None:
            stack.extend(clauses)
            continue
        left = getattr(node, "left", None)
        right = getattr(node, "right", None)
        op = getattr(node, "operator", None)
        if left is None:
            continue
        table = getattr(getattr(left, "table", None), "name", None)
        col = getattr(left, "name", None)
        if not table or not col:
            continue
        op_name = getattr(op, "__name__", str(op))
        val = getattr(right, "value", None)
        if op_name == "is_" and val is None:
            predicates[(table, col)] = ("is_none", None)
        elif op_name == "is_not" and val is None:
            predicates[(table, col)] = ("is_not_none", None)
        elif op_name in {"eq", "gt", "lt", "ge", "le", "ne"}:
            predicates[(table, col)] = (op_name, val)
    return predicates


def _matches_predicates(
    row: Any, table: str, preds: dict[tuple[str, str], tuple[str, Any]]
) -> bool:
    for (pred_table, col), (op_name, val) in preds.items():
        if pred_table != table:
            continue
        actual = getattr(row, col)
        if op_name == "is_none" and actual is not None:
            return False
        if op_name == "is_not_none" and actual is None:
            return False
        if op_name == "eq" and actual != val:
            return False
        if op_name == "ne" and actual == val:
            return False
        if op_name == "gt" and not (actual > val):
            return False
        if op_name == "ge" and not (actual >= val):
            return False
        if op_name == "lt" and not (actual < val):
            return False
        if op_name == "le" and not (actual <= val):
            return False
    return True


class _MemoryAsyncSession:
    """AsyncSession giả trong RAM mô phỏng transaction commit/rollback và truy vấn bảng auth."""

    def __init__(self, store: _MemoryStore, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._clock = clock
        self.info: dict[str, Any] = {}
        self._pending_add: list[Any] = []
        self._pending_delete: list[Any] = []
        self._snapshots: list[tuple[str, Any]] = []

    async def __aenter__(self) -> _MemoryAsyncSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        return None

    def begin(self) -> _FakeBeginContext:
        self._snapshot_existing()
        return _FakeBeginContext(self)

    def _snapshot_existing(self) -> None:
        self._snapshots = [
            (
                "sessions",
                {
                    sid: {
                        "revoked_at": s.revoked_at,
                        "last_seen_at": getattr(s, "last_seen_at", None),
                        "csrf_token_hash": s.csrf_token_hash,
                        "mfa_verified_at": s.mfa_verified_at,
                        "mfa_method": s.mfa_method,
                    }
                    for sid, s in self._store.sessions.items()
                },
            ),
            (
                "factors",
                {
                    f.id: {
                        "verified_at": f.verified_at,
                        "last_used_counter": f.last_used_counter,
                    }
                    for f in self._store.factors
                },
            ),
        ]

    def add(self, row: Any) -> None:
        if getattr(row, "id", None) is None:
            row.id = uuid4()
        if hasattr(row, "created_at") and getattr(row, "created_at", None) is None:
            row.created_at = self._clock()
        if isinstance(row, Session) and getattr(row, "last_seen_at", None) is None:
            row.last_seen_at = self._clock()
        self._pending_add.append(row)

    def add_all(self, rows: list[Any]) -> None:
        for row in rows:
            self.add(row)

    async def delete(self, row: Any) -> None:
        self._pending_delete.append(row)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        for row in self._pending_delete:
            if isinstance(row, RoleBinding) and row in self._store.roles:
                self._store.roles.remove(row)
            elif isinstance(row, MfaFactor) and row in self._store.factors:
                self._store.factors.remove(row)
        for row in self._pending_add:
            if isinstance(row, Workspace):
                self._store.workspaces[row.id] = row
            elif isinstance(row, UserAccount):
                self._store.users[row.id] = row
            elif isinstance(row, UserMembership):
                self._store.memberships[row.id] = row
            elif isinstance(row, RoleBinding):
                self._store.roles.append(row)
            elif isinstance(row, MfaFactor):
                self._store.factors.append(row)
            elif isinstance(row, Session):
                self._store.sessions[row.id] = row
            elif isinstance(row, AuditEvent):
                self._store.audits.append(row)
            else:
                self._store.outbox.append(row)
        self._pending_add.clear()
        self._pending_delete.clear()
        self._snapshots.clear()

    async def rollback(self) -> None:
        self._pending_add.clear()
        self._pending_delete.clear()
        for kind, snap in self._snapshots:
            if kind == "sessions":
                for sid, values in snap.items():
                    if sid in self._store.sessions:
                        for attr, val in values.items():
                            setattr(self._store.sessions[sid], attr, val)
            elif kind == "factors":
                for factor in self._store.factors:
                    if factor.id in snap:
                        for attr, val in snap[factor.id].items():
                            setattr(factor, attr, val)
        self._snapshots.clear()

    async def close(self) -> None:
        return None

    def _query_rows(self, stmt: Any) -> list[Any]:
        descs = getattr(stmt, "column_descriptions", [])
        preds = _extract_filter_predicates(stmt)
        if len(descs) == 3 and [d["entity"] for d in descs] == [
            Session,
            UserMembership,
            UserAccount,
        ]:
            joined: list[tuple[Session, UserMembership, UserAccount]] = []
            for sess in self._store.sessions.values():
                if not _matches_predicates(sess, "session", preds):
                    continue
                mem = self._store.memberships.get(sess.membership_id)
                if mem is None or mem.workspace_id != sess.workspace_id:
                    continue
                if not _matches_predicates(mem, "user_membership", preds):
                    continue
                user = self._store.users.get(mem.user_id)
                if user is None or not _matches_predicates(user, "user_account", preds):
                    continue
                joined.append((sess, mem, user))
            return joined

        if len(descs) == 2 and [d["entity"] for d in descs] == [UserMembership, UserAccount]:
            pairs = []
            for mem in self._store.memberships.values():
                if not _matches_predicates(mem, "user_membership", preds):
                    continue
                user = self._store.users.get(mem.user_id)
                if user is None or not _matches_predicates(user, "user_account", preds):
                    continue
                pairs.append((mem, user))
            return pairs

        entity = descs[0]["entity"] if descs else None
        if entity is Workspace:
            return [
                w
                for w in self._store.workspaces.values()
                if _matches_predicates(w, "workspace", preds)
            ]
        if entity is UserAccount:
            return [
                u
                for u in self._store.users.values()
                if _matches_predicates(u, "user_account", preds)
            ]
        if entity is UserMembership:
            return [
                m
                for m in self._store.memberships.values()
                if _matches_predicates(m, "user_membership", preds)
            ]
        if entity is RoleBinding or getattr(descs[0].get("expr"), "key", None) == "role":
            selects_role_col = getattr(descs[0].get("expr"), "key", None) == "role"
            return [
                r.role if selects_role_col else r
                for r in self._store.roles
                if _matches_predicates(r, "role_binding", preds)
            ]
        if entity is MfaFactor:
            return [f for f in self._store.factors if _matches_predicates(f, "mfa_factor", preds)]
        if entity is Session:
            rows = [
                s for s in self._store.sessions.values() if _matches_predicates(s, "session", preds)
            ]
            rows.sort(
                key=lambda s: (
                    getattr(s, "last_seen_at", None) or s.created_at,
                    s.created_at,
                ),
                reverse=True,
            )
            return rows
        return []

    async def execute(self, stmt: Any, params: Any = None) -> _FakeExecuteResult:
        del params
        if getattr(stmt, "is_select", False):
            return _FakeExecuteResult(self._query_rows(stmt))
        return _FakeExecuteResult([])

    async def scalar(self, stmt: Any) -> Any:
        rows = self._query_rows(stmt)
        return rows[0] if rows else None

    async def scalars(self, stmt: Any) -> _FakeScalarsResult:
        return _FakeScalarsResult(self._query_rows(stmt))


class _MemorySessionFactory:
    def __init__(self, store: _MemoryStore, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._clock = clock

    def __call__(self) -> _MemoryAsyncSession:
        return _MemoryAsyncSession(self._store, self._clock)


def _auth_headers(
    cookie_name: str, cookie_value: str, csrf_token: str | None = None
) -> dict[str, str]:
    headers = {"Cookie": f"{cookie_name}={cookie_value}"}
    if csrf_token is not None:
        headers["X-CSRF-Token"] = csrf_token
    return headers


def _seed_workspace_member(
    store: _MemoryStore,
    secrets_provider: _MemorySecrets,
    *,
    role: Role = Role.OWNER,
    slug: str = "paper-ws",
    email: str = "owner@example.test",
    password: str = "Correct Horse Battery Staple 2026!",
    membership_status: str = "ACTIVE",
    user_status: str = "ACTIVE",
    workspace_status: str = "ACTIVE",
    with_totp: bool = True,
    now: datetime = BASE_TIME,
) -> tuple[Workspace, UserAccount, UserMembership, str]:
    ws = Workspace(id=uuid4(), name="Paper WS", slug=slug, mode="PAPER", status=workspace_status)
    ws.created_at = now
    pw_hash = PasswordManager().hash(password)
    user = UserAccount(
        id=uuid4(), email_normalized=email, password_hash=pw_hash, status=user_status
    )
    user.created_at = now
    user.updated_at = now
    mem = UserMembership(id=uuid4(), workspace_id=ws.id, user_id=user.id, status=membership_status)
    mem.created_at = now
    store.workspaces[ws.id] = ws
    store.users[user.id] = user
    store.memberships[mem.id] = mem
    binding = RoleBinding(id=uuid4(), workspace_id=ws.id, membership_id=mem.id, role=role.value)
    binding.created_at = now
    store.roles.append(binding)
    totp_secret = TotpManager().generate_secret()
    if with_totp:
        ref = secrets_provider.store_mfa_secret(ws.id, totp_secret)
        factor = MfaFactor(
            id=uuid4(),
            workspace_id=ws.id,
            membership_id=mem.id,
            factor_type="TOTP",
            secret_reference=ref,
            verified_at=now - timedelta(days=1),
        )
        factor.created_at = now - timedelta(days=1)
        store.factors.append(factor)
    return ws, user, mem, totp_secret


def _load_migration() -> ModuleType:
    assert MIGRATION_PATH.exists(), f"Thiếu file migration {MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location("sec09_11_migration", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. Migration & Schema (SEC-09, SEC-11)
# ---------------------------------------------------------------------------


def test_sec09_11_migration_adds_last_seen_at_grant_and_membership_status_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_migration()
    assert module.revision == "f0912a3b4c5d"
    assert module.down_revision == "d9e1f3a5b7c8"

    added_columns: list[tuple[str, Any]] = []
    dropped_columns: list[tuple[str, str]] = []
    created_checks: list[tuple[str, str, str]] = []
    dropped_checks: list[tuple[str, str, str | None]] = []
    executed_sql: list[str] = []

    monkeypatch.setattr(
        module.op, "add_column", lambda table, col: added_columns.append((table, col))
    )
    monkeypatch.setattr(
        module.op, "drop_column", lambda table, col: dropped_columns.append((table, col))
    )
    monkeypatch.setattr(
        module.op,
        "create_check_constraint",
        lambda name, table, cond: created_checks.append((name, table, cond)),
    )
    monkeypatch.setattr(
        module.op,
        "drop_constraint",
        lambda name, table, type_=None: dropped_checks.append((name, table, type_)),
    )
    monkeypatch.setattr(
        module.op,
        "execute",
        lambda stmt: executed_sql.append(re.sub(r"\s+", " ", str(stmt)).strip()),
    )

    module.upgrade()
    assert len(added_columns) == 1
    table_name, column = added_columns[0]
    assert table_name == "session"
    assert column.name == "last_seen_at"
    assert column.type.timezone is True
    assert column.nullable is False
    assert any(
        'GRANT UPDATE (last_seen_at) ON TABLE "session" TO crypidian_admin_web' == sql
        for sql in executed_sql
    )
    assert any(
        table == "user_membership"
        and name == "user_membership_status_valid"
        and "ACTIVE" in cond
        and "SUSPENDED" in cond
        and "REVOKED" in cond
        for name, table, cond in created_checks
    )

    executed_sql.clear()
    module.downgrade()
    assert any(
        'REVOKE UPDATE (last_seen_at) ON TABLE "session" FROM crypidian_admin_web' == sql
        for sql in executed_sql
    )
    assert ("session", "last_seen_at") in dropped_columns
    assert ("user_membership_status_valid", "user_membership", "check") in dropped_checks


def test_session_and_membership_models_and_settings_defaults() -> None:
    assert "last_seen_at" in Session.__table__.columns
    last_seen_col = Session.__table__.c.last_seen_at
    assert last_seen_col.type.timezone is True
    assert last_seen_col.nullable is False

    check_names = {
        c.name for c in UserMembership.__table__.constraints if isinstance(c, CheckConstraint)
    }
    assert any("user_membership_status_valid" in (name or "") for name in check_names)

    settings = AppSettings(app_env="test")
    assert settings.session_idle_timeout_minutes == 30
    with pytest.raises(ValidationError):
        AppSettings(app_env="test", session_idle_timeout_minutes=0)


def test_create_app_wires_configured_session_idle_timeout_into_auth_service() -> None:
    store = _MemoryStore()
    clock = _VirtualClock(BASE_TIME)
    app = create_app(
        AppSettings(app_env="test", session_idle_timeout_minutes=15),
        session_factory=_MemorySessionFactory(store, clock),  # type: ignore[arg-type]
        secret_provider=_MemorySecrets(),
    )
    with TestClient(app):
        assert app.state.auth_service._idle_timeout == timedelta(minutes=15)


# ---------------------------------------------------------------------------
# 2. SEC-09: Idle timeout 30 phút + cập nhật last_seen_at tối đa mỗi 60 giây
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sec09_idle_timeout_and_last_seen_throttle_and_absolute_12h() -> None:
    store = _MemoryStore()
    secrets_provider = _MemorySecrets()
    clock = _VirtualClock(BASE_TIME)
    _, _, _, totp_secret = _seed_workspace_member(store, secrets_provider, now=BASE_TIME)
    service = AuthService(
        _MemorySessionFactory(store, clock),  # type: ignore[arg-type]
        secrets_provider,
        idle_timeout=timedelta(minutes=30),
        clock=clock,
    )

    login_res = await service.login(
        workspace_slug="paper-ws",
        email="owner@example.test",
        password="Correct Horse Battery Staple 2026!",
        code=pyotp.TOTP(totp_secret).at(clock()),
    )
    assert login_res.principal.session_id is not None
    session_row = store.sessions[login_res.principal.session_id]
    assert session_row.last_seen_at == BASE_TIME

    # Trong vòng < 60 giây: xác thực thành công nhưng KHÔNG cập nhật last_seen_at.
    clock.advance(seconds=30)
    await service.authenticate(login_res.cookie_value)
    assert session_row.last_seen_at == BASE_TIME

    # Sau >= 60 giây: xác thực thành công và CẬP NHẬT last_seen_at.
    clock.advance(seconds=35)  # +65s
    expected_refresh_time = clock()
    await service.authenticate(login_res.cookie_value)
    assert session_row.last_seen_at == expected_refresh_time

    # issue_csrf_token cũng cập nhật last_seen_at khi quá 60s.
    clock.advance(seconds=65)
    csrf_refresh_time = clock()
    new_csrf = await service.issue_csrf_token(login_res.cookie_value)
    assert session_row.last_seen_at == csrf_refresh_time
    await service.verify_csrf(login_res.cookie_value, new_csrf)

    # Hoạt động liên tục mỗi 20 phút vẫn hợp lệ,
    # nhưng chạm trần tuyệt đối 12 giờ từ lúc login -> 401 dù vừa hoạt động!
    for _ in range(36):
        clock.advance(minutes=20)
        if clock() < BASE_TIME + timedelta(hours=12):
            await service.authenticate(login_res.cookie_value)
        else:
            break
    clock.now = BASE_TIME + timedelta(hours=12)
    with pytest.raises(AuthenticationFailure) as abs_err:
        await service.authenticate(login_res.cookie_value)
    assert abs_err.value.status_code == 401

    # Phiên mới không hoạt động 31 phút -> idle timeout 401 và bị thu hồi revoked_at.
    clock.now = BASE_TIME + timedelta(days=1)
    fresh_login = await service.login(
        workspace_slug="paper-ws",
        email="owner@example.test",
        password="Correct Horse Battery Staple 2026!",
        code=pyotp.TOTP(totp_secret).at(clock()),
    )
    assert fresh_login.principal.session_id is not None
    idle_session = store.sessions[fresh_login.principal.session_id]
    clock.advance(minutes=31)
    with pytest.raises(AuthenticationFailure) as idle_err:
        await service.authenticate(fresh_login.cookie_value)
    assert idle_err.value.status_code == 401
    assert idle_session.revoked_at is not None

    with pytest.raises(AuthenticationFailure) as csrf_err:
        await service.verify_csrf(fresh_login.cookie_value, fresh_login.csrf_token)
    assert csrf_err.value.status_code == 403


# ---------------------------------------------------------------------------
# 3. SEC-10: Xoay token phiên khi reauthenticate và đổi role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sec10_reauthenticate_rotates_session_token_and_preserves_csrf() -> None:
    store = _MemoryStore()
    secrets_provider = _MemorySecrets()
    clock = _VirtualClock(BASE_TIME)
    _, _, _, totp_secret = _seed_workspace_member(store, secrets_provider, now=BASE_TIME)
    service = AuthService(
        _MemorySessionFactory(store, clock),  # type: ignore[arg-type]
        secrets_provider,
        clock=clock,
    )
    login_res = await service.login(
        workspace_slug="paper-ws",
        email="owner@example.test",
        password="Correct Horse Battery Staple 2026!",
        code=pyotp.TOTP(totp_secret).at(clock()),
    )
    old_session_id = login_res.principal.session_id
    assert old_session_id is not None

    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    cookie_name = session_cookie_name(app.state.settings)
    with TestClient(app) as client:
        app.state.auth_service = service
        app.state.session_factory = _MemorySessionFactory(store, clock)

        # Reauth sai mật khẩu KHÔNG thu hồi phiên cũ.
        failed_reauth = client.post(
            "/api/v1/auth/reauthenticate",
            json={"password": "wrong-password", "code": "000000"},
            headers=_auth_headers(cookie_name, login_res.cookie_value, login_res.csrf_token),
        )
        assert failed_reauth.status_code == 401
        assert store.sessions[old_session_id].revoked_at is None
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, login_res.cookie_value),
            ).status_code
            == 200
        )

        # Reauth thành công -> xoay token phiên mới, thu hồi phiên cũ, ghi audit.
        clock.advance(seconds=35)
        reauth_res = client.post(
            "/api/v1/auth/reauthenticate",
            json={
                "password": "Correct Horse Battery Staple 2026!",
                "code": pyotp.TOTP(totp_secret).at(clock()),
            },
            headers=_auth_headers(cookie_name, login_res.cookie_value, login_res.csrf_token),
        )
        assert reauth_res.status_code == 204
        assert reauth_res.headers["cache-control"] == "no-store"
        new_cookie = reauth_res.cookies.get(cookie_name)
        assert new_cookie is not None
        assert new_cookie != login_res.cookie_value

        # Phiên cũ bị thu hồi và không dùng được nữa.
        assert store.sessions[old_session_id].revoked_at is not None
        assert any(a.action == "auth.session.rotated" for a in store.audits)
        old_attempt = client.get(
            "/api/v1/auth/me",
            headers=_auth_headers(cookie_name, login_res.cookie_value),
        )
        assert old_attempt.status_code == 401

        # Cookie mới dùng được ngay với CSRF token hiện tại (tương thích SPA/HTML form).
        new_attempt = client.get(
            "/api/v1/auth/me",
            headers=_auth_headers(cookie_name, new_cookie),
        )
        assert new_attempt.status_code == 200
        await service.verify_csrf(new_cookie, login_res.csrf_token)


@pytest.mark.asyncio
async def test_sec10_and_sec11_role_change_and_mfa_reset_revoke_or_rotate_sessions() -> None:
    store = _MemoryStore()
    secrets_provider = _MemorySecrets()
    clock = _VirtualClock(BASE_TIME)
    ws, _, _, owner1_totp = _seed_workspace_member(
        store, secrets_provider, role=Role.OWNER, email="owner1@example.test", now=BASE_TIME
    )
    owner2_pw = "Second Owner Password Long Enough 2026!"
    owner2_user = UserAccount(
        id=uuid4(),
        email_normalized="owner2@example.test",
        password_hash=PasswordManager().hash(owner2_pw),
        status="ACTIVE",
    )
    owner2_mem = UserMembership(
        id=uuid4(), workspace_id=ws.id, user_id=owner2_user.id, status="ACTIVE"
    )
    owner2_secret = TotpManager().generate_secret()
    owner2_ref = secrets_provider.store_mfa_secret(ws.id, owner2_secret)
    owner2_factor = MfaFactor(
        id=uuid4(),
        workspace_id=ws.id,
        membership_id=owner2_mem.id,
        factor_type="TOTP",
        secret_reference=owner2_ref,
        verified_at=BASE_TIME - timedelta(days=1),
    )
    op_pw = "Operator Member Password Long 2026!"
    op_user = UserAccount(
        id=uuid4(),
        email_normalized="op@example.test",
        password_hash=PasswordManager().hash(op_pw),
        status="ACTIVE",
    )
    op_mem = UserMembership(id=uuid4(), workspace_id=ws.id, user_id=op_user.id, status="ACTIVE")
    op_secret = TotpManager().generate_secret()
    op_ref = secrets_provider.store_mfa_secret(ws.id, op_secret)
    op_factor = MfaFactor(
        id=uuid4(),
        workspace_id=ws.id,
        membership_id=op_mem.id,
        factor_type="TOTP",
        secret_reference=op_ref,
        verified_at=BASE_TIME - timedelta(days=1),
    )
    store.users[owner2_user.id] = owner2_user
    store.users[op_user.id] = op_user
    store.memberships[owner2_mem.id] = owner2_mem
    store.memberships[op_mem.id] = op_mem
    store.roles.extend(
        [
            RoleBinding(
                id=uuid4(), workspace_id=ws.id, membership_id=owner2_mem.id, role=Role.OWNER.value
            ),
            RoleBinding(
                id=uuid4(), workspace_id=ws.id, membership_id=op_mem.id, role=Role.OPERATOR.value
            ),
        ]
    )
    store.factors.extend([owner2_factor, op_factor])

    service = AuthService(
        _MemorySessionFactory(store, clock),  # type: ignore[arg-type]
        secrets_provider,
        clock=clock,
    )
    owner1_login = await service.login(
        workspace_slug=ws.slug,
        email="owner1@example.test",
        password="Correct Horse Battery Staple 2026!",
        code=pyotp.TOTP(owner1_totp).at(clock()),
    )
    owner2_login = await service.login(
        workspace_slug=ws.slug,
        email="owner2@example.test",
        password=owner2_pw,
        code=pyotp.TOTP(owner2_secret).at(clock()),
    )
    op_login = await service.login(
        workspace_slug=ws.slug,
        email="op@example.test",
        password=op_pw,
        code=pyotp.TOTP(op_secret).at(clock()),
    )

    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    cookie_name = session_cookie_name(app.state.settings)
    with TestClient(app) as client:
        app.state.auth_service = service
        app.state.session_factory = _MemorySessionFactory(store, clock)

        # Owner1 đổi role của operator -> mọi phiên của operator bị thu hồi ngay.
        role_res = client.patch(
            f"/api/v1/members/{op_mem.id}/role",
            json={"role": Role.ANALYST_REVIEWER.value, "reason": "Chuyển sang vai trò reviewer"},
            headers=_auth_headers(cookie_name, owner1_login.cookie_value, owner1_login.csrf_token),
        )
        assert role_res.status_code == 200
        assert role_res.json()["revoked_sessions"] == 1
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, op_login.cookie_value),
            ).status_code
            == 401
        )

        # Owner2 tự đổi role của chính mình (còn owner1) -> xoay token phiên mới, cookie cũ -> 401.
        self_role_res = client.patch(
            f"/api/v1/members/{owner2_mem.id}/role",
            json={"role": Role.RISK_ADMIN.value, "reason": "Bàn giao quyền owner"},
            headers=_auth_headers(cookie_name, owner2_login.cookie_value, owner2_login.csrf_token),
        )
        assert self_role_res.status_code == 200
        rotated_owner2_cookie = self_role_res.cookies.get(cookie_name)
        assert rotated_owner2_cookie is not None
        assert rotated_owner2_cookie != owner2_login.cookie_value
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, owner2_login.cookie_value),
            ).status_code
            == 401
        )
        me_rotated = client.get(
            "/api/v1/auth/me",
            headers=_auth_headers(cookie_name, rotated_owner2_cookie),
        )
        assert me_rotated.status_code == 200
        assert me_rotated.json()["roles"] == [Role.RISK_ADMIN.value]

        # Owner1 reset MFA của owner2 -> factor bị vô hiệu hóa (verified_at=None, không delete)
        # và mọi phiên của owner2 bị thu hồi.
        mfa_reset_res = client.post(
            "/api/v1/members/recovery/mfa-reset",
            json={"membership_id": str(owner2_mem.id), "reason": "Mất thiết bị xác thực TOTP"},
            headers=_auth_headers(cookie_name, owner1_login.cookie_value, owner1_login.csrf_token),
        )
        assert mfa_reset_res.status_code == 200
        assert mfa_reset_res.json()["disabled_factors"] == 1
        assert mfa_reset_res.json()["revoked_sessions"] >= 1
        assert owner2_factor.verified_at is None
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, rotated_owner2_cookie),
            ).status_code
            == 401
        )


# ---------------------------------------------------------------------------
# 4. SEC-11: Quản lý & thu hồi phiên (self + owner + member_suspended/disabled)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sec11_session_management_endpoints_and_suspended_member_auto_revoke() -> None:
    store = _MemoryStore()
    secrets_provider = _MemorySecrets()
    clock = _VirtualClock(BASE_TIME)
    ws, _, owner_mem, owner_totp = _seed_workspace_member(
        store, secrets_provider, role=Role.OWNER, email="owner@example.test", now=BASE_TIME
    )
    op_pw = "Operator Member Password Long 2026!"
    op_user = UserAccount(
        id=uuid4(),
        email_normalized="op@example.test",
        password_hash=PasswordManager().hash(op_pw),
        status="ACTIVE",
    )
    op_mem = UserMembership(id=uuid4(), workspace_id=ws.id, user_id=op_user.id, status="ACTIVE")
    op_secret = TotpManager().generate_secret()
    op_ref = secrets_provider.store_mfa_secret(ws.id, op_secret)
    store.users[op_user.id] = op_user
    store.memberships[op_mem.id] = op_mem
    store.roles.append(
        RoleBinding(
            id=uuid4(), workspace_id=ws.id, membership_id=op_mem.id, role=Role.OPERATOR.value
        )
    )
    store.factors.append(
        MfaFactor(
            id=uuid4(),
            workspace_id=ws.id,
            membership_id=op_mem.id,
            factor_type="TOTP",
            secret_reference=op_ref,
            verified_at=BASE_TIME - timedelta(days=1),
        )
    )

    service = AuthService(
        _MemorySessionFactory(store, clock),  # type: ignore[arg-type]
        secrets_provider,
        clock=clock,
    )
    owner_s1 = await service.login(
        workspace_slug=ws.slug,
        email="owner@example.test",
        password="Correct Horse Battery Staple 2026!",
        code=pyotp.TOTP(owner_totp).at(clock()),
    )
    clock.advance(seconds=35)
    owner_s2 = await service.login(
        workspace_slug=ws.slug,
        email="owner@example.test",
        password="Correct Horse Battery Staple 2026!",
        code=pyotp.TOTP(owner_totp).at(clock()),
    )
    op_s1 = await service.login(
        workspace_slug=ws.slug,
        email="op@example.test",
        password=op_pw,
        code=pyotp.TOTP(op_secret).at(clock()),
    )
    clock.advance(seconds=35)
    op_s2 = await service.login(
        workspace_slug=ws.slug,
        email="op@example.test",
        password=op_pw,
        code=pyotp.TOTP(op_secret).at(clock()),
    )

    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    assert_all_routes_have_policy(app)
    cookie_name = session_cookie_name(app.state.settings)
    with TestClient(app) as client:
        app.state.auth_service = service
        app.state.session_factory = _MemorySessionFactory(store, clock)

        # Liệt kê phiên đang hoạt động của mình (không lộ token_hash/csrf_token_hash).
        listed = client.get(
            "/api/v1/auth/sessions",
            headers=_auth_headers(cookie_name, owner_s2.cookie_value),
        )
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        items = listed.json()["sessions"]
        assert len(items) == 2
        assert {item["id"] for item in items} == {
            str(owner_s1.principal.session_id),
            str(owner_s2.principal.session_id),
        }
        for item in items:
            assert "token_hash" not in item
            assert "csrf_token_hash" not in item
        current_item = next(
            item for item in items if item["id"] == str(owner_s2.principal.session_id)
        )
        assert current_item["is_current"] is True

        # Operator không được thu hồi phiên của Owner hoặc gọi revoke-all.
        denied_revoke = client.post(
            f"/api/v1/auth/sessions/{owner_s1.principal.session_id}/revoke",
            headers=_auth_headers(cookie_name, op_s1.cookie_value, op_s1.csrf_token),
        )
        assert denied_revoke.status_code == 403
        denied_all = client.post(
            f"/api/v1/members/{owner_mem.id}/sessions/revoke-all",
            headers=_auth_headers(cookie_name, op_s1.cookie_value, op_s1.csrf_token),
        )
        assert denied_all.status_code == 403

        # Thu hồi session hoặc membership không tồn tại -> 404.
        assert (
            client.post(
                f"/api/v1/auth/sessions/{uuid4()}/revoke",
                headers=_auth_headers(cookie_name, owner_s2.cookie_value, owner_s2.csrf_token),
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/v1/members/{uuid4()}/sessions/revoke-all",
                headers=_auth_headers(cookie_name, owner_s2.cookie_value, owner_s2.csrf_token),
            ).status_code
            == 404
        )

        # Operator tự thu hồi phiên khác của chính mình.
        self_revoke = client.post(
            f"/api/v1/auth/sessions/{op_s2.principal.session_id}/revoke",
            headers=_auth_headers(cookie_name, op_s1.cookie_value, op_s1.csrf_token),
        )
        assert self_revoke.status_code == 200
        assert self_revoke.json()["revoked"] is True
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, op_s2.cookie_value),
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, op_s1.cookie_value),
            ).status_code
            == 200
        )

        # Owner tự thu hồi phiên hiện tại (owner_s1) -> xóa cookie (Max-Age=0).
        self_current_revoke = client.post(
            f"/api/v1/auth/sessions/{owner_s1.principal.session_id}/revoke",
            headers=_auth_headers(cookie_name, owner_s1.cookie_value, owner_s1.csrf_token),
        )
        assert self_current_revoke.status_code == 200
        assert any(
            "max-age=0" in header.lower()
            for header in self_current_revoke.headers.get_list("set-cookie")
        )
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, owner_s1.cookie_value),
            ).status_code
            == 401
        )

        # Owner thu hồi phiên cụ thể của operator -> 200 và audit.
        owner_revoke_op = client.post(
            f"/api/v1/auth/sessions/{op_s1.principal.session_id}/revoke",
            json={"reason": "Phát hiện truy cập bất thường"},
            headers=_auth_headers(cookie_name, owner_s2.cookie_value, owner_s2.csrf_token),
        )
        assert owner_revoke_op.status_code == 200
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, op_s1.cookie_value),
            ).status_code
            == 401
        )

        # Owner tạo lại phiên cho operator và gọi revoke-all.
        clock.advance(seconds=35)
        op_s3 = await service.login(
            workspace_slug=ws.slug,
            email="op@example.test",
            password=op_pw,
            code=pyotp.TOTP(op_secret).at(clock()),
        )
        clock.advance(seconds=35)
        op_s4 = await service.login(
            workspace_slug=ws.slug,
            email="op@example.test",
            password=op_pw,
            code=pyotp.TOTP(op_secret).at(clock()),
        )
        revoke_all_res = client.post(
            f"/api/v1/members/{op_mem.id}/sessions/revoke-all",
            json={"reason": "Thu hồi toàn bộ phiên cuối ca"},
            headers=_auth_headers(cookie_name, owner_s2.cookie_value, owner_s2.csrf_token),
        )
        assert revoke_all_res.status_code == 200
        assert revoke_all_res.json()["revoked_count"] == 2
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, op_s3.cookie_value),
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, op_s4.cookie_value),
            ).status_code
            == 401
        )

        # Khi member bị khóa (SUSPENDED), request đầu tự động revoke mọi phiên và ghi audit.
        clock.advance(seconds=35)
        op_s5 = await service.login(
            workspace_slug=ws.slug,
            email="op@example.test",
            password=op_pw,
            code=pyotp.TOTP(op_secret).at(clock()),
        )
        clock.advance(seconds=35)
        op_s6 = await service.login(
            workspace_slug=ws.slug,
            email="op@example.test",
            password=op_pw,
            code=pyotp.TOTP(op_secret).at(clock()),
        )
        op_mem.status = "SUSPENDED"
        assert (
            client.get(
                "/api/v1/auth/me",
                headers=_auth_headers(cookie_name, op_s5.cookie_value),
            ).status_code
            == 401
        )
        assert op_s5.principal.session_id is not None
        assert op_s6.principal.session_id is not None
        assert store.sessions[op_s5.principal.session_id].revoked_at is not None
        assert store.sessions[op_s6.principal.session_id].revoked_at is not None
        assert any(a.action == "auth.sessions.revoked_inactive_member" for a in store.audits)


# ---------------------------------------------------------------------------
# 5. SEC-12: Chống timing enumeration khi login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sec12_login_always_runs_argon2_verify_with_dummy_hash_and_uniform_error() -> None:
    from trading_bot.auth.service import DUMMY_PASSWORD_HASH

    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert PasswordHasher().check_needs_rehash(DUMMY_PASSWORD_HASH) is False
    assert PasswordManager().verify(DUMMY_PASSWORD_HASH, "any-password-value") is False

    store = _MemoryStore()
    secrets_provider = _MemorySecrets()
    clock = _VirtualClock(BASE_TIME)
    ws, user, mem, totp_secret = _seed_workspace_member(store, secrets_provider, now=BASE_TIME)

    class _SpyPasswords(PasswordManager):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, str]] = []

        def verify(self, encoded_hash: str, password: str) -> bool:
            self.calls.append((encoded_hash, password))
            return super().verify(encoded_hash, password)

    spy = _SpyPasswords()
    service = AuthService(
        _MemorySessionFactory(store, clock),  # type: ignore[arg-type]
        secrets_provider,
        passwords=spy,
        clock=clock,
    )

    cases = [
        ("paper-ws", "nobody@example.test", "wrong-password-14chars"),
        ("missing-ws", "owner@example.test", "Correct Horse Battery Staple 2026!"),
    ]
    for slug, email, pw in cases:
        spy.calls.clear()
        with pytest.raises(AuthenticationFailure) as err:
            await service.login(workspace_slug=slug, email=email, password=pw, code="000000")
        assert err.value.status_code == 401
        assert err.value.message == "Thông tin xác thực không hợp lệ"
        assert spy.calls == [(DUMMY_PASSWORD_HASH, pw)]

    # User tồn tại nhưng membership SUSPENDED -> vẫn chạy Argon2 giả và trả lỗi đồng nhất.
    mem.status = "SUSPENDED"
    spy.calls.clear()
    with pytest.raises(AuthenticationFailure) as suspended_err:
        await service.login(
            workspace_slug=ws.slug,
            email=user.email_normalized,
            password="Correct Horse Battery Staple 2026!",
            code=pyotp.TOTP(totp_secret).at(clock()),
        )
    assert suspended_err.value.status_code == 401
    assert suspended_err.value.message == "Thông tin xác thực không hợp lệ"
    assert spy.calls == [(DUMMY_PASSWORD_HASH, "Correct Horse Battery Staple 2026!")]

    # User tồn tại nhưng user.status != ACTIVE -> vẫn chạy Argon2 với DUMMY_PASSWORD_HASH.
    mem.status = "ACTIVE"
    user.status = "DISABLED"
    spy.calls.clear()
    with pytest.raises(AuthenticationFailure) as disabled_err:
        await service.login(
            workspace_slug=ws.slug,
            email=user.email_normalized,
            password="Correct Horse Battery Staple 2026!",
            code="000000",
        )
    assert disabled_err.value.status_code == 401
    assert disabled_err.value.message == "Thông tin xác thực không hợp lệ"
    assert spy.calls == [(DUMMY_PASSWORD_HASH, "Correct Horse Battery Staple 2026!")]


def test_sec11_route_policies_allow_auth_sessions_and_deny_unauthenticated() -> None:
    app = create_app(AppSettings(app_env="test", process_role="admin-web"))
    assert_all_routes_have_policy(app)
    client = TestClient(app)

    class _NoAuth:
        async def authenticate(self, _cookie: str | None) -> AuthIdentity:
            raise AuthenticationFailure(401, "Cần đăng nhập")

        async def verify_csrf(self, _cookie: str | None, _token: str | None) -> None:
            raise AuthenticationFailure(403, "Thiếu CSRF")

    app.state.auth_service = _NoAuth()
    assert client.get("/api/v1/auth/sessions").status_code == 401
    assert client.post(f"/api/v1/auth/sessions/{uuid4()}/revoke").status_code == 401
    assert client.post(f"/api/v1/members/{uuid4()}/sessions/revoke-all").status_code == 401

    op = AuthIdentity(
        principal=Principal(
            user_id=uuid4(),
            membership_id=uuid4(),
            workspace_id=uuid4(),
            roles=frozenset({Role.OPERATOR}),
            mfa_verified_at=BASE_TIME,
            mfa_method=MfaMethod.TOTP,
            session_id=uuid4(),
        ),
        email="op@example.test",
    )
    app.dependency_overrides[current_identity] = lambda: op
    app.dependency_overrides[require_csrf] = lambda: op
    app.dependency_overrides[get_auth_service] = lambda: _NoAuth()
    assert client.post(f"/api/v1/members/{uuid4()}/sessions/revoke-all").status_code == 403
