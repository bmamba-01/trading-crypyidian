"""Kiểm thử TDD (RED -> GREEN) cho SEC-14, SEC-15 và SEC-16 (CRY-136)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pyotp
import pytest
from fastapi.testclient import TestClient

from trading_bot.app import create_app
from trading_bot.auth.mfa_recovery import (
    BOOTSTRAP_ALREADY_COMPLETED_CODE,
    AccountSecurityError,
    AccountSecurityService,
    check_bootstrap_token_retired,
    generate_recovery_codes,
    hash_recovery_code,
    normalize_recovery_code,
    verify_recovery_code,
)
from trading_bot.auth.password_policy import (
    COMMON_BREACHED_PASSWORDS,
    MIN_PASSWORD_LENGTH,
    PasswordPolicyViolation,
    is_breached_or_common_password,
    validate_password_policy,
)
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.route_policy import assert_all_routes_have_policy
from trading_bot.auth.security import PasswordManager, TotpManager
from trading_bot.auth.service import AuthenticationFailure, AuthService
from trading_bot.config.settings import AppSettings
from trading_bot.ports.secrets import SecretAccessError, SecretLease, SecretProvider

WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
USER_ID = UUID("22222222-2222-4222-8222-222222222222")
MEMBERSHIP_ID = UUID("33333333-3333-4333-8333-333333333333")
OWNER_USER_ID = UUID("44444444-4444-4444-8444-444444444444")
OWNER_MEMBERSHIP_ID = UUID("55555555-5555-4555-8555-555555555555")
BASE_TIME = datetime.now(UTC)


class _Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class _ClockedTotpManager(TotpManager):
    """Dùng đồng hồ ảo trong unit test để không phải sleep 30 giây giữa các chu kỳ TOTP."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def matching_counter(
        self, secret: str, code: str, at: datetime, *, valid_window: int
    ) -> int | None:
        del at
        return super().matching_counter(secret, code, self._clock(), valid_window=valid_window)


class _MemorySecretProvider(SecretProvider):
    def __init__(self) -> None:
        self.secrets: dict[tuple[UUID, str], str] = {}

    def store_mfa_secret(self, workspace_id: UUID, value: str) -> str:
        ref = f"local-mfa:{uuid4()}"
        self.secrets[(workspace_id, ref)] = value
        return ref

    def resolve_mfa_secret(self, workspace_id: UUID, reference: str) -> str:
        key = (workspace_id, reference)
        if key not in self.secrets:
            raise SecretAccessError("Không tìm thấy secret MFA")
        return self.secrets[key]

    def delete_mfa_secret(self, workspace_id: UUID, reference: str) -> None:
        self.secrets.pop((workspace_id, reference), None)

    def resolve_for_gateway(self, workspace_id: UUID, reference: str) -> SecretLease:
        del workspace_id, reference
        raise SecretAccessError("Không hỗ trợ gateway")

    def resolve_telegram_bot_token(
        self, reference: str, *, workspace_id: UUID | None = None
    ) -> SecretLease:
        del reference, workspace_id
        raise SecretAccessError("Không hỗ trợ telegram")


class _MemoryAuthStore:
    """Kho dữ liệu giả lập PostgreSQL cho luồng Auth + MFA + Password + Bootstrap."""

    def __init__(self) -> None:
        self.workspaces: dict[UUID, Any] = {}
        self.users: dict[UUID, Any] = {}
        self.memberships: dict[UUID, Any] = {}
        self.role_bindings: list[Any] = []
        self.mfa_factors: dict[UUID, Any] = {}
        self.secret_references: dict[UUID, Any] = {}
        self.recovery_codes: dict[UUID, Any] = {}
        self.sessions: dict[UUID, Any] = {}
        self.audits: list[Any] = []
        self.lock = asyncio.Lock()


class _FakeDbSession:
    """Session SQLAlchemy tối giản hỗ trợ các truy vấn của AuthService & AccountSecurityService."""

    def __init__(self, store: _MemoryAuthStore, *, clock: _Clock) -> None:
        self._store = store
        self._clock = clock
        self.info: dict[str, Any] = {}
        self._pending_adds: list[Any] = []
        self._in_tx = False

    async def __aenter__(self) -> _FakeDbSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self._pending_adds.clear()

    def begin(self) -> _FakeTx:
        return _FakeTx(self)

    def add(self, item: object) -> None:
        if getattr(item, "id", None) is None:
            item.id = uuid4()
        if getattr(item, "created_at", None) is None:
            item.created_at = self._clock()
        self._pending_adds.append(item)

    def add_all(self, items: list[object]) -> None:
        for item in items:
            self.add(item)

    async def flush(self) -> None:
        self._commit_pending_adds()

    def _commit_pending_adds(self) -> None:
        from trading_bot.adapters.database.models import (
            AuditEvent,
            MfaFactor,
            MfaRecoveryCode,
            RoleBinding,
            SecretReference,
            Session,
            UserAccount,
            UserMembership,
            Workspace,
        )

        for item in self._pending_adds:
            if isinstance(item, Workspace):
                self._store.workspaces[item.id] = item
            elif isinstance(item, UserAccount):
                self._store.users[item.id] = item
            elif isinstance(item, UserMembership):
                self._store.memberships[item.id] = item
            elif isinstance(item, RoleBinding):
                if item not in self._store.role_bindings:
                    self._store.role_bindings.append(item)
            elif isinstance(item, MfaFactor):
                self._store.mfa_factors[item.id] = item
            elif isinstance(item, SecretReference):
                self._store.secret_references[item.id] = item
            elif isinstance(item, MfaRecoveryCode):
                self._store.recovery_codes[item.id] = item
            elif isinstance(item, Session):
                self._store.sessions[item.id] = item
            elif isinstance(item, AuditEvent):
                self._store.audits.append(item)
        self._pending_adds.clear()

    async def commit(self) -> None:
        self._commit_pending_adds()

    async def rollback(self) -> None:
        self._pending_adds.clear()

    async def close(self) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        del params
        text_sql = str(stmt)
        if "LOCK TABLE" in text_sql or "set_config" in text_sql:
            return _Result([])
        return await self._eval_select_or_update(stmt)

    async def scalar(self, stmt: Any) -> Any:
        result = await self._eval_select_or_update(stmt)
        return result.scalar()

    async def scalars(self, stmt: Any) -> Any:
        result = await self._eval_select_or_update(stmt)
        return _ScalarList(result.scalars_list())

    async def _eval_select_or_update(self, stmt: Any) -> _Result:
        from sqlalchemy.sql.dml import Update
        from sqlalchemy.sql.selectable import Select

        from trading_bot.adapters.database.models import (
            Session,
            UserAccount,
            UserMembership,
        )

        if isinstance(stmt, Update):
            table_name = stmt.table.name
            values = {
                k.name if hasattr(k, "name") else str(k): v.value for k, v in stmt._values.items()
            }  # type: ignore[attr-defined]
            collection = self._table_items(table_name)
            matched: list[Any] = []
            for row in list(collection):
                if self._matches_where(row, table_name, stmt._where_criteria):  # type: ignore[attr-defined]
                    for field, val in values.items():
                        setattr(row, field, val)
                    matched.append(row)
            if stmt._returning:  # type: ignore[attr-defined]
                col = stmt._returning[0]
                col_name = getattr(col, "name", "id")
                return _Result([(getattr(r, col_name),) for r in matched])
            return _Result([])

        if isinstance(stmt, Select):
            cols = stmt.column_descriptions
            entities = [c["entity"] for c in cols if c.get("entity") is not None]
            # Handle multi-table join Session + UserMembership + UserAccount
            if entities == [Session, UserMembership, UserAccount]:
                rows = []
                for sess in self._store.sessions.values():
                    mem = self._store.memberships.get(sess.membership_id)
                    if mem is None or mem.workspace_id != sess.workspace_id:
                        continue
                    user = self._store.users.get(mem.user_id)
                    if user is None:
                        continue
                    if self._matches_combined_where(
                        {"session": sess, "user_membership": mem, "user_account": user},
                        stmt._where_criteria,
                    ):
                        rows.append((sess, mem, user))
                return _Result(rows)

            if (
                len(cols) == 1
                and cols[0].get("expr") is not None
                and cols[0].get("expr") is not cols[0].get("entity")
            ):
                expr = cols[0]["expr"]
                table_name = expr.table.name
                attr = expr.name
                if table_name == "role_binding":
                    matched_roles = [
                        (getattr(rb, attr),)
                        for rb in self._store.role_bindings
                        if self._matches_where(rb, table_name, stmt._where_criteria)
                    ]
                    return _Result(matched_roles)
                rows = [
                    (getattr(item, attr),)
                    for item in self._table_items(table_name)
                    if self._matches_where(item, table_name, stmt._where_criteria)
                ]
                return _Result(rows)

            if len(entities) == 1:
                entity = entities[0]
                table_name = entity.__tablename__
                rows = [
                    (item,)
                    for item in self._table_items(table_name)
                    if self._matches_where(item, table_name, stmt._where_criteria)
                ]
                return _Result(rows)

            if (
                len(cols) == 1
                and hasattr(cols[0].get("expr"), "name")
                and cols[0]["expr"].name == "count"
            ):
                # func.count(...)
                return _Result([(0,)])

        raise AssertionError(f"Unsupported SQLAlchemy statement in fake session: {stmt!r}")

    def _table_items(self, table_name: str) -> list[Any]:
        return {
            "workspace": list(self._store.workspaces.values()),
            "user_account": list(self._store.users.values()),
            "user_membership": list(self._store.memberships.values()),
            "role_binding": list(self._store.role_bindings),
            "mfa_factor": list(self._store.mfa_factors.values()),
            "mfa_recovery_code": list(self._store.recovery_codes.values()),
            "session": list(self._store.sessions.values()),
        }[table_name]

    def _matches_combined_where(self, table_map: dict[str, Any], criteria: tuple[Any, ...]) -> bool:
        for clause in criteria:
            if not self._eval_clause(table_map, clause):
                return False
        return True

    def _matches_where(self, row: Any, table_name: str, criteria: tuple[Any, ...]) -> bool:
        return self._matches_combined_where({table_name: row}, criteria)

    def _eval_clause(self, table_map: dict[str, Any], clause: Any) -> bool:
        from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList, UnaryExpression

        if isinstance(clause, BooleanClauseList):
            return all(self._eval_clause(table_map, sub) for sub in clause.clauses)
        if isinstance(clause, BinaryExpression):
            left = clause.left
            right = clause.right
            t_name = left.table.name
            col_name = left.name
            row = table_map[t_name]
            val = getattr(row, col_name)
            op_name = getattr(clause.operator, "__name__", "")
            # Handle join condition like UserMembership.workspace_id == Session.workspace_id
            if hasattr(right, "table") and hasattr(right, "name"):
                other = getattr(table_map[right.table.name], right.name)
                return bool(val == other)
            rhs = getattr(right, "value", None)
            if op_name == "is_":
                return val is None
            if op_name == "is_not":
                return val is not None
            if op_name == "eq":
                return bool(val == rhs)
            if op_name == "ne":
                return bool(val != rhs)
            if op_name == "gt":
                return bool(val is not None and val > rhs)
            if op_name == "lt":
                return bool(val is not None and val < rhs)
            if op_name == "in_op":
                return val in rhs
            raise AssertionError(f"Unsupported binary op {op_name}: {clause}")
        if isinstance(clause, UnaryExpression):
            return True
        raise AssertionError(f"Unsupported clause {type(clause)}: {clause}")


class _FakeTx:
    def __init__(self, session: _FakeDbSession) -> None:
        self._session = session

    def __await__(self) -> Any:
        async def _start() -> _FakeTx:
            return self

        return _start().__await__()

    async def __aenter__(self) -> _FakeTx:
        await self._session._store.lock.acquire()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc, tb
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            self._session._store.lock.release()


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def first(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def scalar(self) -> Any:
        if not self._rows:
            return None
        return self._rows[0][0]

    def scalars_list(self) -> list[Any]:
        return [row[0] for row in self._rows]


class _ScalarList:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def all(self) -> list[Any]:
        return list(self._items)

    def first(self) -> Any:
        return self._items[0] if self._items else None


class _FakeSessionFactory:
    def __init__(self, store: _MemoryAuthStore, clock: _Clock) -> None:
        self._store = store
        self._clock = clock

    def __call__(self) -> _FakeDbSession:
        return _FakeDbSession(self._store, clock=self._clock)


def _setup_services(
    *,
    bootstrap_token_file: Path | None = None,
    clock: _Clock | None = None,
) -> tuple[
    _MemoryAuthStore,
    _Clock,
    _MemorySecretProvider,
    _FakeSessionFactory,
    AuthService,
    AccountSecurityService,
    TestClient,
]:
    active_clock = clock or _Clock()
    store = _MemoryAuthStore()
    sessions = _FakeSessionFactory(store, active_clock)
    secrets_provider = _MemorySecretProvider()
    totp_mgr = _ClockedTotpManager(active_clock)
    auth_service = AuthService(sessions, secrets_provider, totp=totp_mgr)  # type: ignore[arg-type]
    account_service = AccountSecurityService(
        sessions,  # type: ignore[arg-type]
        secrets_provider,
        totp=totp_mgr,
        clock=active_clock,
    )
    app = create_app(
        AppSettings(
            app_env="test",
            process_role="admin-web",
            bootstrap_token_file=bootstrap_token_file,
        ),
        session_factory=sessions,  # type: ignore[arg-type]
        secret_provider=secrets_provider,
    )
    app.state.auth_service = auth_service
    app.state.account_security_service = account_service
    client = TestClient(app)
    return store, active_clock, secrets_provider, sessions, auth_service, account_service, client


async def _bootstrap_and_login(
    auth_service: AuthService,
    *,
    clock: _Clock,
    email: str = "trader.owner@example.test",
    password: str = "quiet river mountain cedar falcon",
    slug: str = "alpha-desk",
) -> tuple[UUID, str, Any]:
    at = clock.now
    boot = await auth_service.bootstrap(
        workspace_name="Alpha Desk",
        workspace_slug=slug,
        email=email,
        password=password,
        mode="PAPER",
    )
    secret = parse_qs(urlparse(boot.mfa_setup_uri).query)["secret"][0]
    await auth_service.confirm_bootstrap(
        workspace_slug=slug,
        email=email,
        code=pyotp.TOTP(secret).at(at - timedelta(seconds=30)),
    )
    login_res = await auth_service.login(
        workspace_slug=slug,
        email=email,
        password=password,
        code=pyotp.TOTP(secret).at(at),
    )
    if login_res.principal.mfa_verified_at > clock.now:
        clock.now = login_res.principal.mfa_verified_at
    return boot.workspace_id, secret, login_res


# ---------------------------------------------------------------------------
# 1. SEC-15: Password Policy & Blocklist
# ---------------------------------------------------------------------------


def test_password_policy_enforces_length_blocklist_and_identifiers_without_composition_rules() -> (
    None
):
    assert MIN_PASSWORD_LENGTH == 14
    assert len(COMMON_BREACHED_PASSWORDS) >= 30

    # Passphrase chỉ gồm chữ thường và khoảng trắng >= 14 ký tự vẫn được chấp nhận.
    validate_password_policy(
        "silent river pine valley",
        email="alice.trader@example.test",
        username="alicetrader",
    )

    # Quá ngắn -> từ chối
    with pytest.raises(PasswordPolicyViolation) as short_err:
        validate_password_policy("shortpass123")
    assert short_err.value.code == "PASSWORD_TOO_SHORT"

    # Toàn khoảng trắng hoặc lặp 1 ký tự -> từ chối
    with pytest.raises(PasswordPolicyViolation):
        validate_password_policy("              ")
    with pytest.raises(PasswordPolicyViolation):
        validate_password_policy("aaaaaaaaaaaaaa")

    # Mật khẩu nằm trong blocklist (chuẩn hóa chữ hoa/thường và ký tự ngăn cách) -> từ chối
    assert is_breached_or_common_password("Password123456")
    assert is_breached_or_common_password("PASSWORD-123-456")
    with pytest.raises(PasswordPolicyViolation) as breached_err:
        validate_password_policy("Password123456")
    assert breached_err.value.code == "PASSWORD_BREACHED"

    # Trùng email / local-part / username -> từ chối
    for candidate in (
        "alice.trader@example.test",
        "Alice.Trader@Example.Test",
        "alice.trader1234",
        "alicetrader999",
    ):
        with pytest.raises(PasswordPolicyViolation) as id_err:
            validate_password_policy(
                candidate,
                email="alice.trader@example.test",
                username="alicetrader",
            )
        assert id_err.value.code == "PASSWORD_MATCHES_IDENTIFIER"

    # Không chặn cụm từ dài hợp lệ chỉ vì tình cờ chứa từ phổ thông như "owner"
    validate_password_policy("a long owner password for test", email="owner@example.test")


@pytest.mark.asyncio
async def test_change_password_updates_hash_revokes_other_sessions_and_audits(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "bootstrap_token"
    token_file.write_text("sec16-bootstrap-token-secret", encoding="utf-8")
    store, clock, _, _, auth_service, account_service, client = _setup_services(
        bootstrap_token_file=token_file
    )
    old_pw = "quiet river mountain cedar falcon"
    new_pw = "crimson cedar horizon autumn breeze"
    _, secret, sess_keep = await _bootstrap_and_login(auth_service, clock=clock, password=old_pw)

    # Tạo phiên thứ 2 ở chu kỳ TOTP kế tiếp để kiểm tra phiên khác bị thu hồi sau đổi mật khẩu.
    clock.advance(30)
    sess_other = await auth_service.login(
        workspace_slug="alpha-desk",
        email="trader.owner@example.test",
        password=old_pw,
        code=pyotp.TOTP(secret).at(clock.now),
    )
    # Làm mới mfa_verified_at cho sess_keep.
    clock.advance(30)
    await auth_service.reauthenticate(
        sess_keep.cookie_value, password=old_pw, code=pyotp.TOTP(secret).at(clock.now)
    )

    # Mật khẩu mới thuộc blocklist -> 422, giữ nguyên hash cũ & không thu hồi phiên khác.
    bad_resp = client.post(
        "/api/v1/auth/password/change",
        json={"current_password": old_pw, "new_password": "Password123456"},
        headers={
            "Cookie": f"crypidian_session={sess_keep.cookie_value}",
            "X-CSRF-Token": sess_keep.csrf_token,
        },
    )
    assert bad_resp.status_code == 422
    assert bad_resp.headers.get("Cache-Control") == "no-store"

    # Sai mật khẩu hiện tại -> 401
    wrong_cur = client.post(
        "/api/v1/auth/password/change",
        json={"current_password": "wrong current password value", "new_password": new_pw},
        headers={
            "Cookie": f"crypidian_session={sess_keep.cookie_value}",
            "X-CSRF-Token": sess_keep.csrf_token,
        },
    )
    assert wrong_cur.status_code == 401

    # MFA quá hạn (>15 phút) -> 403
    stale_principal = Principal(
        user_id=sess_keep.principal.user_id,
        membership_id=sess_keep.principal.membership_id,
        workspace_id=sess_keep.principal.workspace_id,
        roles=sess_keep.principal.roles,
        mfa_verified_at=clock.now - timedelta(minutes=20),
        mfa_method=MfaMethod.TOTP,
        session_id=sess_keep.principal.session_id,
    )
    with pytest.raises(AccountSecurityError) as stale_err:
        await account_service.change_password(
            stale_principal, current_password=old_pw, new_password=new_pw
        )
    assert stale_err.value.status_code == 403

    # Đổi mật khẩu thành công -> cập nhật hash, thu hồi sess_other, giữ sess_keep, ghi AuditEvent.
    ok_resp = client.post(
        "/api/v1/auth/password/change",
        json={"current_password": old_pw, "new_password": new_pw},
        headers={
            "Cookie": f"crypidian_session={sess_keep.cookie_value}",
            "X-CSRF-Token": sess_keep.csrf_token,
        },
    )
    assert ok_resp.status_code == 200
    assert ok_resp.headers.get("Cache-Control") == "no-store"
    assert ok_resp.json()["revoked_other_sessions"] == 1

    # Phiên hiện tại vẫn truy cập được, phiên khác trả 401.
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Cookie": f"crypidian_session={sess_keep.cookie_value}"}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Cookie": f"crypidian_session={sess_other.cookie_value}"}
        ).status_code
        == 401
    )

    # Đăng nhập bằng mật khẩu cũ bị từ chối; mật khẩu mới thành công.
    clock.advance(30)
    with pytest.raises(AuthenticationFailure):
        await auth_service.login(
            workspace_slug="alpha-desk",
            email="trader.owner@example.test",
            password=old_pw,
            code=pyotp.TOTP(secret).at(clock.now),
        )
    clock.advance(30)
    new_login = await auth_service.login(
        workspace_slug="alpha-desk",
        email="trader.owner@example.test",
        password=new_pw,
        code=pyotp.TOTP(secret).at(clock.now),
    )
    assert new_login.principal.user_id == sess_keep.principal.user_id
    assert any(a.action == "auth.password.changed" for a in store.audits)


# ---------------------------------------------------------------------------
# 2. SEC-14: Recovery Codes & TOTP Rotation / Replay / Drift / Admin Reset
# ---------------------------------------------------------------------------


def test_recovery_code_generation_and_salted_hashing() -> None:
    codes = generate_recovery_codes(10)
    assert len(codes) == 10
    assert len(set(codes)) == 10
    sample = codes[0]
    normalized = normalize_recovery_code(sample.lower().replace("-", " "))
    digest_a = hash_recovery_code(
        normalized, salt="a" * 32, workspace_id=WORKSPACE_ID, membership_id=MEMBERSHIP_ID
    )
    digest_b = hash_recovery_code(
        normalized, salt="b" * 32, workspace_id=WORKSPACE_ID, membership_id=MEMBERSHIP_ID
    )
    assert len(digest_a) == 64
    assert digest_a != digest_b
    assert verify_recovery_code(
        sample.lower(),
        expected_hash=digest_a,
        salt="a" * 32,
        workspace_id=WORKSPACE_ID,
        membership_id=MEMBERSHIP_ID,
    )
    assert not verify_recovery_code(
        codes[1],
        expected_hash=digest_a,
        salt="a" * 32,
        workspace_id=WORKSPACE_ID,
        membership_id=MEMBERSHIP_ID,
    )


@pytest.mark.asyncio
async def test_recovery_codes_regenerate_single_use_and_atomic_race_protection(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "bootstrap_token"
    token_file.write_text("sec14-bootstrap-token", encoding="utf-8")
    store, clock, _, _, auth_service, account_service, client = _setup_services(
        bootstrap_token_file=token_file
    )
    pw = "quiet river mountain cedar falcon"
    _, _, login_res = await _bootstrap_and_login(auth_service, clock=clock, password=pw)

    # Tạo bộ mã lần 1 (10 mã).
    regen_1 = client.post(
        "/api/v1/auth/mfa/recovery-codes/regenerate",
        json={"current_password": pw, "count": 10},
        headers={
            "Cookie": f"crypidian_session={login_res.cookie_value}",
            "X-CSRF-Token": login_res.csrf_token,
        },
    )
    assert regen_1.status_code == 200
    assert regen_1.headers.get("Cache-Control") == "no-store"
    codes_1 = regen_1.json()["recovery_codes"]
    assert len(codes_1) == 10
    # DB chỉ lưu hash, không lưu plaintext.
    for row in store.recovery_codes.values():
        assert row.code_hash not in codes_1
        assert len(row.code_hash) == 64
        assert len(row.code_salt) >= 16

    # Regenerate lần 2 thu hồi toàn bộ bộ mã cũ.
    regen_2 = client.post(
        "/api/v1/auth/mfa/recovery-codes/regenerate",
        json={"current_password": pw, "count": 8},
        headers={
            "Cookie": f"crypidian_session={login_res.cookie_value}",
            "X-CSRF-Token": login_res.csrf_token,
        },
    )
    assert regen_2.status_code == 200
    codes_2 = regen_2.json()["recovery_codes"]
    assert len(codes_2) == 8
    assert regen_2.json()["revoked_count"] == 10

    # Mã từ bộ cũ bị thu hồi không dùng được.
    old_use = client.post(
        "/api/v1/auth/mfa/recovery-login",
        json={
            "workspace_slug": "alpha-desk",
            "email": "trader.owner@example.test",
            "password": pw,
            "recovery_code": codes_1[0],
        },
    )
    assert old_use.status_code == 401

    # Mã từ bộ mới dùng được đúng 1 lần; lần 2 bị từ chối (SECT-14).
    first_use = client.post(
        "/api/v1/auth/mfa/recovery-login",
        json={
            "workspace_slug": "alpha-desk",
            "email": "trader.owner@example.test",
            "password": pw,
            "recovery_code": codes_2[0].lower().replace("-", " "),
        },
    )
    assert first_use.status_code == 200
    assert first_use.headers.get("Cache-Control") == "no-store"
    assert "crypidian_session" in first_use.cookies

    second_use = client.post(
        "/api/v1/auth/mfa/recovery-login",
        json={
            "workspace_slug": "alpha-desk",
            "email": "trader.owner@example.test",
            "password": pw,
            "recovery_code": codes_2[0],
        },
    )
    assert second_use.status_code == 401

    # Đua song song với cùng 1 mã recovery thứ 2: chỉ đúng 1 lần thành công.
    results = await asyncio.gather(
        account_service.recovery_login(
            workspace_slug="alpha-desk",
            email="trader.owner@example.test",
            password=pw,
            recovery_code=codes_2[1],
        ),
        account_service.recovery_login(
            workspace_slug="alpha-desk",
            email="trader.owner@example.test",
            password=pw,
            recovery_code=codes_2[1],
        ),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, AuthenticationFailure)]
    assert len(successes) == 1
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_totp_rotation_drift_replay_and_admin_reset_without_owner_knowing_secret(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "bootstrap_token"
    token_file.write_text("sec14-totp-token", encoding="utf-8")
    store, clock, secrets_provider, _, auth_service, account_service, client = _setup_services(
        bootstrap_token_file=token_file
    )
    pw = "quiet river mountain cedar falcon"
    _, old_secret, login_res = await _bootstrap_and_login(auth_service, clock=clock, password=pw)

    # SECT-13: mã TOTP chu kỳ ±2 (±60s) bị từ chối; mã đã dùng bị từ chối.
    assert (
        TotpManager().matching_counter(
            old_secret,
            pyotp.TOTP(old_secret).at(clock.now - timedelta(seconds=60)),
            clock.now,
            valid_window=1,
        )
        is None
    )
    assert (
        TotpManager().matching_counter(
            old_secret,
            pyotp.TOTP(old_secret).at(clock.now + timedelta(seconds=60)),
            clock.now,
            valid_window=1,
        )
        is None
    )
    with pytest.raises(AuthenticationFailure):
        await auth_service.login(
            workspace_slug="alpha-desk",
            email="trader.owner@example.test",
            password=pw,
            code=pyotp.TOTP(old_secret).at(clock.now),
        )

    # Mở thêm phiên thứ 2 để kiểm chứng rotate thu hồi phiên khác.
    clock.advance(30)
    other_sess = await auth_service.login(
        workspace_slug="alpha-desk",
        email="trader.owner@example.test",
        password=pw,
        code=pyotp.TOTP(old_secret).at(clock.now),
    )

    # Bắt đầu xoay TOTP factor mới -> tạo pending factor (chưa kích hoạt).
    clock.advance(30)
    await auth_service.reauthenticate(
        login_res.cookie_value, password=pw, code=pyotp.TOTP(old_secret).at(clock.now)
    )
    init_resp = client.post(
        "/api/v1/auth/mfa/totp/rotate",
        json={"current_password": pw},
        headers={
            "Cookie": f"crypidian_session={login_res.cookie_value}",
            "X-CSRF-Token": login_res.csrf_token,
        },
    )
    assert init_resp.status_code == 201
    assert init_resp.headers.get("Cache-Control") == "no-store"
    factor_id = init_resp.json()["factor_id"]
    new_secret = parse_qs(urlparse(init_resp.json()["mfa_setup_uri"]).query)["secret"][0]
    assert new_secret != old_secret

    # Xác nhận mã TOTP sai -> thất bại 401, factor cũ vẫn còn hiệu lực.
    bad_confirm = client.post(
        "/api/v1/auth/mfa/totp/rotate/confirm",
        json={"factor_id": factor_id, "code": "000000"},
        headers={
            "Cookie": f"crypidian_session={login_res.cookie_value}",
            "X-CSRF-Token": login_res.csrf_token,
        },
    )
    assert bad_confirm.status_code == 401

    # Xác nhận đúng mã TOTP mới -> kích hoạt factor mới, vô hiệu cũ, xóa secret cũ, revoke khác.
    clock.advance(30)
    ok_confirm = client.post(
        "/api/v1/auth/mfa/totp/rotate/confirm",
        json={"factor_id": factor_id, "code": pyotp.TOTP(new_secret).at(clock.now)},
        headers={
            "Cookie": f"crypidian_session={login_res.cookie_value}",
            "X-CSRF-Token": login_res.csrf_token,
        },
    )
    assert ok_confirm.status_code == 200
    assert ok_confirm.json()["status"] == "active"
    assert ok_confirm.json()["revoked_other_sessions"] == 1
    assert (
        client.get(
            "/api/v1/auth/me", headers={"Cookie": f"crypidian_session={other_sess.cookie_value}"}
        ).status_code
        == 401
    )

    # Mã TOTP cũ sau khi rotate bị từ chối; mã TOTP mới đăng nhập được.
    clock.advance(30)
    with pytest.raises(AuthenticationFailure):
        await auth_service.login(
            workspace_slug="alpha-desk",
            email="trader.owner@example.test",
            password=pw,
            code=pyotp.TOTP(old_secret).at(clock.now),
        )
    clock.advance(30)
    logged_new = await auth_service.login(
        workspace_slug="alpha-desk",
        email="trader.owner@example.test",
        password=pw,
        code=pyotp.TOTP(new_secret).at(clock.now),
    )
    assert logged_new.principal.membership_id == login_res.principal.membership_id

    # Owner reset MFA cho member khác: không bao giờ biết secret TOTP mới của member!
    from trading_bot.adapters.database.models import RoleBinding, UserAccount, UserMembership

    member_user_id = uuid4()
    member_id = uuid4()
    member_pw = "member strong passphrase alpine forest"
    store.users[member_user_id] = UserAccount(
        id=member_user_id,
        email_normalized="operator.one@example.test",
        password_hash=PasswordManager().hash(member_pw),
        status="ACTIVE",
        created_at=clock.now,
        updated_at=clock.now,
    )
    store.memberships[member_id] = UserMembership(
        id=member_id,
        workspace_id=login_res.principal.workspace_id,
        user_id=member_user_id,
        status="ACTIVE",
        created_at=clock.now,
    )
    store.role_bindings.append(
        RoleBinding(
            id=uuid4(),
            workspace_id=login_res.principal.workspace_id,
            membership_id=member_id,
            role=Role.OPERATOR.value,
            created_at=clock.now,
        )
    )
    reset_resp = client.post(
        "/api/v1/auth/mfa/admin-reset",
        json={
            "membership_id": str(member_id),
            "current_password": pw,
            "reason": "Thành viên xác minh mất thiết bị qua cuộc gọi video",
            "identity_verified": True,
            "issue_one_time_recovery_code": True,
        },
        headers={
            "Cookie": f"crypidian_session={login_res.cookie_value}",
            "X-CSRF-Token": login_res.csrf_token,
        },
    )
    assert reset_resp.status_code == 200
    reset_body = reset_resp.json()
    assert "mfa_setup_uri" not in reset_body
    assert "totp_secret" not in reset_body
    assert reset_body["one_time_recovery_code"]
    # Member tự đăng nhập bằng mật khẩu của mình + recovery code và tự xoay TOTP mới.
    rec_login = await account_service.recovery_login(
        workspace_slug="alpha-desk",
        email="operator.one@example.test",
        password=member_pw,
        recovery_code=reset_body["one_time_recovery_code"],
    )
    member_init = await account_service.initiate_totp_rotation(
        rec_login.principal, current_password=member_pw
    )
    assert "otpauth://totp/" in member_init["mfa_setup_uri"]
    member_secret = parse_qs(urlparse(member_init["mfa_setup_uri"]).query)["secret"][0]
    clock.advance(30)
    await account_service.confirm_totp_rotation(
        rec_login.principal,
        factor_id=UUID(member_init["factor_id"]),
        code=pyotp.TOTP(member_secret).at(clock.now),
    )
    assert any(a.action == "auth.mfa.admin_reset" for a in store.audits)


# ---------------------------------------------------------------------------
# 3. SEC-16: Bootstrap Token Retirement
# ---------------------------------------------------------------------------


def test_check_bootstrap_token_retired_warns_when_file_remains_after_bootstrap(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    token_file = tmp_path / "bootstrap_token"
    token_file.write_text("still-present-token", encoding="utf-8")

    # Trước bootstrap: còn file là bình thường, không cảnh báo.
    pre = check_bootstrap_token_retired(token_file, bootstrapped=False)
    assert pre.bootstrapped is False
    assert pre.retired is True
    assert pre.warning is None

    # Sau bootstrap nhưng vẫn còn file -> retired=False + phát log cảnh báo.
    with caplog.at_level(logging.WARNING):
        still_there = check_bootstrap_token_retired(token_file, bootstrapped=True)
    assert still_there.bootstrapped is True
    assert still_there.token_file_present is True
    assert still_there.retired is False
    assert still_there.warning is not None
    assert "BOOTSTRAP_TOKEN_NOT_RETIRED" in caplog.text

    # Xóa file -> retired=True.
    token_file.unlink()
    after_unlink = check_bootstrap_token_retired(token_file, bootstrapped=True)
    assert after_unlink.retired is True
    assert after_unlink.token_file_present is False
    assert after_unlink.warning is None


def test_bootstrap_endpoints_permanently_blocked_after_completion(tmp_path: Path) -> None:
    token_file = tmp_path / "bootstrap_token"
    token = "one-time-bootstrap-token-sec16"
    token_file.write_text(token, encoding="utf-8")
    _, clock, _, _, _, _, client = _setup_services(bootstrap_token_file=token_file)

    payload = {
        "workspace_name": "Alpha Desk",
        "workspace_slug": "alpha-desk",
        "email": "owner@example.test",
        "password": "quiet river mountain cedar falcon",
        "mode": "PAPER",
    }
    # Bootstrap bước 1
    boot_resp = client.post(
        "/api/v1/auth/bootstrap",
        json=payload,
        headers={"X-Bootstrap-Token": token},
    )
    assert boot_resp.status_code == 201
    secret = parse_qs(urlparse(boot_resp.json()["mfa_setup_uri"]).query)["secret"][0]

    # Trong lúc SETUP vẫn không được gọi /bootstrap lần 2
    second_boot_during_setup = client.post(
        "/api/v1/auth/bootstrap",
        json=payload,
        headers={"X-Bootstrap-Token": token},
    )
    assert second_boot_during_setup.status_code == 409
    assert second_boot_during_setup.json()["detail"]["code"] == BOOTSTRAP_ALREADY_COMPLETED_CODE

    # Xác minh bước 2 -> hoàn tất bootstrap
    verify_resp = client.post(
        "/api/v1/auth/bootstrap/verify",
        json={
            "workspace_slug": "alpha-desk",
            "email": "owner@example.test",
            "code": pyotp.TOTP(secret).at(clock.now),
        },
    )
    assert verify_resp.status_code == 200

    # Sau bootstrap: mọi nỗ lực gọi /bootstrap* đều bị 409 BOOTSTRAP_ALREADY_COMPLETED.
    for headers in ({"X-Bootstrap-Token": token}, {"X-Bootstrap-Token": "wrong"}, {}):
        blocked_boot = client.post("/api/v1/auth/bootstrap", json=payload, headers=headers)
        assert blocked_boot.status_code == 409
        assert blocked_boot.json()["detail"]["code"] == BOOTSTRAP_ALREADY_COMPLETED_CODE

    blocked_verify = client.post(
        "/api/v1/auth/bootstrap/verify",
        json={
            "workspace_slug": "alpha-desk",
            "email": "owner@example.test",
            "code": "123456",
        },
    )
    assert blocked_verify.status_code == 409
    assert blocked_verify.json()["detail"]["code"] == BOOTSTRAP_ALREADY_COMPLETED_CODE

    # Kiểm tra qua endpoint status sau khi owner đăng nhập (still mounted on admin-web, policy ok)
    clock.advance(30)
    login_resp = client.post(
        "/api/v1/auth/login",
        json={
            "workspace_slug": "alpha-desk",
            "email": "owner@example.test",
            "password": "quiet river mountain cedar falcon",
            "code": pyotp.TOTP(secret).at(clock.now),
        },
    )
    assert login_resp.status_code == 200
    cookie_val = login_resp.cookies["crypidian_session"]
    status_before = client.get(
        "/api/v1/auth/bootstrap/status", headers={"Cookie": f"crypidian_session={cookie_val}"}
    )
    assert status_before.status_code == 200
    assert status_before.json()["bootstrapped"] is True
    assert status_before.json()["retired"] is False

    token_file.unlink()
    status_after = client.get(
        "/api/v1/auth/bootstrap/status", headers={"Cookie": f"crypidian_session={cookie_val}"}
    )
    assert status_after.status_code == 200
    assert status_after.json()["retired"] is True

    # Xác nhận mọi route mới đều đạt RoutePolicy SEC-18
    assert_all_routes_have_policy(client.app)
