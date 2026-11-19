import asyncio
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    AuditEvent,
    MfaFactor,
    RoleBinding,
    SecretReference,
    Session,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.auth.policy import RECENT_MFA_LIFETIME as RECENT_MFA_LIFETIME
from trading_bot.auth.policy import MfaMethod, Principal, Role
from trading_bot.auth.security import DUMMY_PASSWORD_HASH as DUMMY_PASSWORD_HASH
from trading_bot.auth.security import PasswordManager, TotpManager
from trading_bot.auth.session_lifecycle import (
    DEFAULT_SESSION_IDLE_TIMEOUT,
    LAST_SEEN_REFRESH_INTERVAL,
    SESSION_ABSOLUTE_LIFETIME,
    effective_last_seen_at,
    hash_session_token,
    is_session_absolute_expired,
    is_session_idle_expired,
    issue_rotated_session,
    revoke_membership_sessions,
    should_refresh_last_seen,
)
from trading_bot.domain.events import state_hash
from trading_bot.ports.secrets import SecretProvider

SESSION_LIFETIME = SESSION_ABSOLUTE_LIFETIME


class AuthenticationFailure(Exception):
    """Lỗi xác thực không tiết lộ email, mật khẩu hay trạng thái tài khoản."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    workspace_id: UUID
    mfa_setup_uri: str


@dataclass(frozen=True, slots=True)
class LoginResult:
    cookie_value: str
    csrf_token: str
    principal: Principal
    email: str


@dataclass(frozen=True, slots=True)
class SessionRotationResult:
    """Kết quả xoay token phiên khi xác thực lại step-up hoặc đổi vai trò (SEC-10)."""

    cookie_value: str
    principal: Principal
    email: str
    expires_at: datetime
    csrf_token: str | None = None


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """Thông tin tóm tắt an toàn của một phiên đang hoạt động (không lộ hash token)."""

    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    idle_expires_at: datetime
    mfa_verified_at: datetime | None
    mfa_method: str | None
    is_current: bool


@dataclass(frozen=True, slots=True)
class SessionRevocationResult:
    """Kết quả thu hồi một phiên đăng nhập (SEC-11)."""

    session_id: UUID
    membership_id: UUID
    revoked_at: datetime


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    principal: Principal
    email: str


class AuthService:
    """Điều phối bootstrap, MFA, vòng đời phiên (idle/rotate/revoke) và phạm vi workspace."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        secret_provider: SecretProvider,
        *,
        passwords: PasswordManager | None = None,
        totp: TotpManager | None = None,
        idle_timeout: timedelta = DEFAULT_SESSION_IDLE_TIMEOUT,
        absolute_timeout: timedelta = SESSION_LIFETIME,
        last_seen_refresh_interval: timedelta = LAST_SEEN_REFRESH_INTERVAL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if idle_timeout.total_seconds() <= 0 or absolute_timeout.total_seconds() <= 0:
            raise ValueError("Ngưỡng thời hạn phiên phải lớn hơn 0")
        if last_seen_refresh_interval.total_seconds() < 0:
            raise ValueError("Khoảng làm mới last_seen_at không được âm")
        self._sessions = sessions
        self._secret_provider = secret_provider
        self._passwords = passwords or PasswordManager()
        self._totp = totp or TotpManager()
        self._idle_timeout = idle_timeout
        self._absolute_timeout = absolute_timeout
        self._last_seen_refresh_interval = last_seen_refresh_interval
        self._clock = clock or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        """Trả mốc thời gian UTC hiện hành theo đồng hồ của dịch vụ."""
        return self._clock()

    async def bootstrap(
        self, *, workspace_name: str, workspace_slug: str, email: str, password: str, mode: str
    ) -> BootstrapResult:
        normalized_email = normalize_email(email)
        validated_slug = normalize_slug(workspace_slug)
        password_hash = await asyncio.to_thread(self._passwords.hash, password)
        mfa_secret = self._totp.generate_secret()
        workspace_id = uuid4()
        reference: str | None = None
        try:
            async with self._sessions() as database, database.begin():
                await database.execute(text('LOCK TABLE "workspace" IN EXCLUSIVE MODE'))
                has_workspace = await database.scalar(select(Workspace.id).limit(1))
                has_user = await database.scalar(select(UserAccount.id).limit(1))
                if has_workspace is not None or has_user is not None:
                    raise AuthenticationFailure(409, "Thiết lập ban đầu đã hoàn tất")
                workspace = Workspace(
                    id=workspace_id,
                    name=workspace_name.strip(),
                    slug=validated_slug,
                    mode=mode,
                    status="SETUP",
                )
                user = UserAccount(
                    email_normalized=normalized_email,
                    password_hash=password_hash,
                    status="PENDING_MFA",
                )
                database.add_all([workspace, user])
                await database.flush()
                await self._set_workspace(database, workspace_id)
                membership = UserMembership(
                    workspace_id=workspace_id,
                    user_id=user.id,
                    status="ACTIVE",
                )
                database.add(membership)
                await database.flush()
                database.add(
                    RoleBinding(
                        workspace_id=workspace_id,
                        membership_id=membership.id,
                        role=Role.OWNER.value,
                    )
                )
                reference = self._secret_provider.store_mfa_secret(workspace_id, mfa_secret)
                database.add(
                    SecretReference(
                        workspace_id=workspace_id,
                        provider="local_mfa",
                        reference_key=reference,
                        status="ACTIVE",
                    )
                )
                database.add(
                    MfaFactor(
                        workspace_id=workspace_id,
                        membership_id=membership.id,
                        factor_type="TOTP",
                        secret_reference=reference,
                        verified_at=None,
                    )
                )
        except AuthenticationFailure:
            raise
        except Exception:
            if reference is not None:
                self._secret_provider.delete_mfa_secret(workspace_id, reference)
            raise
        return BootstrapResult(
            workspace_id=workspace_id,
            mfa_setup_uri=self._totp.provisioning_uri(mfa_secret, account=normalized_email),
        )

    async def confirm_bootstrap(self, *, workspace_slug: str, email: str, code: str) -> None:
        normalized_email = normalize_email(email)
        now = self._clock()
        async with self._sessions() as database, database.begin():
            user = await database.scalar(
                select(UserAccount).where(
                    UserAccount.email_normalized == normalized_email,
                    UserAccount.status == "PENDING_MFA",
                )
            )
            workspace = await database.scalar(
                select(Workspace).where(Workspace.slug == normalize_slug(workspace_slug))
            )
            if user is None or workspace is None or workspace.status != "SETUP":
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            await self._set_workspace(database, workspace.id)
            membership = await database.scalar(
                select(UserMembership).where(
                    UserMembership.workspace_id == workspace.id,
                    UserMembership.user_id == user.id,
                    UserMembership.status == "ACTIVE",
                )
            )
            if membership is None:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            factor = (
                await database.scalars(
                    select(MfaFactor)
                    .where(
                        MfaFactor.workspace_id == workspace.id,
                        MfaFactor.membership_id == membership.id,
                        MfaFactor.factor_type == "TOTP",
                    )
                    .with_for_update()
                )
            ).first()
            if factor is None:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            secret = self._secret_provider.resolve_mfa_secret(workspace.id, factor.secret_reference)
            counter = self._totp.matching_counter(secret, code, now, valid_window=1)
            if counter is None:
                raise AuthenticationFailure(401, "Mã xác thực không hợp lệ")
            factor.verified_at = now
            factor.last_used_counter = counter
            user.status = "ACTIVE"
            workspace.status = "ACTIVE"

    async def login(
        self, *, workspace_slug: str, email: str, password: str, code: str
    ) -> LoginResult:
        normalized_email = normalize_email(email)
        validated_slug = normalize_slug(workspace_slug)
        now = self._clock()
        async with self._sessions() as database, database.begin():
            user = await database.scalar(
                select(UserAccount).where(UserAccount.email_normalized == normalized_email)
            )
            workspace = await database.scalar(
                select(Workspace).where(Workspace.slug == validated_slug)
            )
            membership: UserMembership | None = None
            if workspace is not None and workspace.status == "ACTIVE":
                await self._set_workspace(database, workspace.id)
                if user is not None:
                    membership = await database.scalar(
                        select(UserMembership).where(
                            UserMembership.workspace_id == workspace.id,
                            UserMembership.user_id == user.id,
                        )
                    )
            account_active = (
                user is not None
                and user.status == "ACTIVE"
                and workspace is not None
                and workspace.status == "ACTIVE"
                and membership is not None
                and membership.status == "ACTIVE"
            )
            # SEC-12: luôn chạy Argon2 với hash giả khi user/workspace/membership không tồn tại
            # hoặc không ACTIVE để thời gian phản hồi và thông điệp lỗi luôn đồng nhất.
            candidate_hash = (
                user.password_hash if account_active and user is not None else DUMMY_PASSWORD_HASH
            )
            password_ok = await asyncio.to_thread(self._passwords.verify, candidate_hash, password)
            if not account_active or not password_ok:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            assert user is not None
            assert workspace is not None
            assert membership is not None

            factor = (
                await database.scalars(
                    select(MfaFactor)
                    .where(
                        MfaFactor.workspace_id == workspace.id,
                        MfaFactor.membership_id == membership.id,
                        MfaFactor.factor_type == "TOTP",
                        MfaFactor.verified_at.is_not(None),
                    )
                    .with_for_update()
                )
            ).first()
            if factor is None:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            secret = self._secret_provider.resolve_mfa_secret(workspace.id, factor.secret_reference)
            counter = self._totp.matching_counter(secret, code, now, valid_window=1)
            if counter is None or (
                factor.last_used_counter is not None and counter <= factor.last_used_counter
            ):
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            factor.last_used_counter = counter
            if self._passwords.needs_rehash(user.password_hash):
                user.password_hash = await asyncio.to_thread(self._passwords.hash, password)
            bindings = await database.scalars(
                select(RoleBinding.role).where(
                    RoleBinding.workspace_id == workspace.id,
                    RoleBinding.membership_id == membership.id,
                )
            )
            roles = frozenset(Role(role) for role in bindings.all())
            if not roles:
                raise AuthenticationFailure(403, "Tài khoản chưa được cấp vai trò")
            raw_token = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            session_id = uuid4()
            database.add(
                Session(
                    id=session_id,
                    workspace_id=workspace.id,
                    membership_id=membership.id,
                    token_hash=hash_token(raw_token),
                    csrf_token_hash=hash_token(csrf_token),
                    expires_at=now + self._absolute_timeout,
                    last_seen_at=now,
                    mfa_verified_at=now,
                    mfa_method=MfaMethod.TOTP.value,
                )
            )
            principal = Principal(
                user_id=user.id,
                membership_id=membership.id,
                workspace_id=workspace.id,
                roles=roles,
                mfa_verified_at=now,
                mfa_method=MfaMethod.TOTP,
                session_id=session_id,
            )
        return LoginResult(
            cookie_value=f"{principal.workspace_id}:{raw_token}",
            csrf_token=csrf_token,
            principal=principal,
            email=normalized_email,
        )

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        now = self._clock()
        failure: AuthenticationFailure | None = None
        identity: AuthIdentity | None = None
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, workspace_id)
            row = (
                await database.execute(
                    select(Session, UserMembership, UserAccount)
                    .join(
                        UserMembership,
                        (UserMembership.workspace_id == Session.workspace_id)
                        & (UserMembership.id == Session.membership_id),
                    )
                    .join(UserAccount, UserAccount.id == UserMembership.user_id)
                    .where(
                        Session.workspace_id == workspace_id,
                        Session.token_hash == hash_token(raw_token),
                        Session.revoked_at.is_(None),
                    )
                )
            ).first()
            if row is None:
                failure = AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")
            else:
                session, membership, user = row
                if membership.status != "ACTIVE" or user.status != "ACTIVE":
                    await self._revoke_inactive_member_sessions(
                        database,
                        workspace_id=workspace_id,
                        membership_id=membership.id,
                        now=now,
                    )
                    failure = AuthenticationFailure(
                        401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn"
                    )
                elif is_session_absolute_expired(session, now=now):
                    failure = AuthenticationFailure(
                        401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn"
                    )
                elif is_session_idle_expired(session, now=now, idle_timeout=self._idle_timeout):
                    session.revoked_at = now
                    failure = AuthenticationFailure(
                        401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn"
                    )
                elif session.mfa_verified_at is None or session.csrf_token_hash is None:
                    failure = AuthenticationFailure(401, "Phiên đăng nhập cần xác thực lại")
                else:
                    if should_refresh_last_seen(
                        session,
                        now=now,
                        refresh_interval=self._last_seen_refresh_interval,
                    ):
                        session.last_seen_at = now
                    roles = frozenset(
                        Role(role)
                        for role in (
                            await database.scalars(
                                select(RoleBinding.role).where(
                                    RoleBinding.workspace_id == workspace_id,
                                    RoleBinding.membership_id == membership.id,
                                )
                            )
                        ).all()
                    )
                    if not roles:
                        failure = AuthenticationFailure(403, "Tài khoản chưa được cấp vai trò")
                    else:
                        principal = Principal(
                            user_id=user.id,
                            membership_id=membership.id,
                            workspace_id=workspace_id,
                            roles=roles,
                            mfa_verified_at=session.mfa_verified_at,
                            mfa_method=(
                                MfaMethod(session.mfa_method)
                                if session.mfa_method is not None
                                else None
                            ),
                            session_id=session.id,
                        )
                        identity = AuthIdentity(principal=principal, email=user.email_normalized)
        if failure is not None:
            raise failure
        assert identity is not None
        return identity

    async def verify_csrf(self, cookie_value: str | None, csrf_token: str | None) -> None:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        if not csrf_token:
            raise AuthenticationFailure(403, "Thiếu mã CSRF")
        now = self._clock()
        failure: AuthenticationFailure | None = None
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, workspace_id)
            row = (
                await database.execute(
                    select(Session, UserMembership, UserAccount)
                    .join(
                        UserMembership,
                        (UserMembership.workspace_id == Session.workspace_id)
                        & (UserMembership.id == Session.membership_id),
                    )
                    .join(UserAccount, UserAccount.id == UserMembership.user_id)
                    .where(
                        Session.workspace_id == workspace_id,
                        Session.token_hash == hash_token(raw_token),
                        Session.revoked_at.is_(None),
                    )
                )
            ).first()
            if row is None:
                failure = AuthenticationFailure(403, "Mã CSRF không hợp lệ")
            else:
                session, membership, user = row
                if membership.status != "ACTIVE" or user.status != "ACTIVE":
                    await self._revoke_inactive_member_sessions(
                        database,
                        workspace_id=workspace_id,
                        membership_id=membership.id,
                        now=now,
                    )
                    failure = AuthenticationFailure(403, "Mã CSRF không hợp lệ")
                elif is_session_absolute_expired(session, now=now):
                    failure = AuthenticationFailure(403, "Mã CSRF không hợp lệ")
                elif is_session_idle_expired(session, now=now, idle_timeout=self._idle_timeout):
                    session.revoked_at = now
                    failure = AuthenticationFailure(403, "Mã CSRF không hợp lệ")
                elif session.csrf_token_hash is None or not secrets.compare_digest(
                    session.csrf_token_hash, hash_token(csrf_token)
                ):
                    failure = AuthenticationFailure(403, "Mã CSRF không hợp lệ")
        if failure is not None:
            raise failure

    async def logout(self, cookie_value: str | None) -> None:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        now = self._clock()
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, workspace_id)
            session = await database.scalar(
                select(Session).where(
                    Session.workspace_id == workspace_id,
                    Session.token_hash == hash_token(raw_token),
                    Session.revoked_at.is_(None),
                )
            )
            if session is not None:
                session.revoked_at = now

    async def issue_csrf_token(self, cookie_value: str | None) -> str:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        now = self._clock()
        token = secrets.token_urlsafe(32)
        failure: AuthenticationFailure | None = None
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, workspace_id)
            row = (
                await database.execute(
                    select(Session, UserMembership, UserAccount)
                    .join(
                        UserMembership,
                        (UserMembership.workspace_id == Session.workspace_id)
                        & (UserMembership.id == Session.membership_id),
                    )
                    .join(UserAccount, UserAccount.id == UserMembership.user_id)
                    .where(
                        Session.workspace_id == workspace_id,
                        Session.token_hash == hash_token(raw_token),
                        Session.revoked_at.is_(None),
                    )
                )
            ).first()
            if row is None:
                failure = AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")
            else:
                session, membership, user = row
                if membership.status != "ACTIVE" or user.status != "ACTIVE":
                    await self._revoke_inactive_member_sessions(
                        database,
                        workspace_id=workspace_id,
                        membership_id=membership.id,
                        now=now,
                    )
                    failure = AuthenticationFailure(
                        401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn"
                    )
                elif is_session_absolute_expired(session, now=now):
                    failure = AuthenticationFailure(
                        401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn"
                    )
                elif is_session_idle_expired(session, now=now, idle_timeout=self._idle_timeout):
                    session.revoked_at = now
                    failure = AuthenticationFailure(
                        401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn"
                    )
                else:
                    if should_refresh_last_seen(
                        session,
                        now=now,
                        refresh_interval=self._last_seen_refresh_interval,
                    ):
                        session.last_seen_at = now
                    session.csrf_token_hash = hash_token(token)
        if failure is not None:
            raise failure
        return token

    async def reauthenticate(
        self,
        cookie_value: str | None,
        *,
        password: str,
        code: str,
    ) -> SessionRotationResult:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        now = self._clock()
        failure: AuthenticationFailure | None = None
        rotated: SessionRotationResult | None = None
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, workspace_id)
            row = (
                await database.execute(
                    select(Session, UserMembership, UserAccount)
                    .join(
                        UserMembership,
                        (UserMembership.workspace_id == Session.workspace_id)
                        & (UserMembership.id == Session.membership_id),
                    )
                    .join(UserAccount, UserAccount.id == UserMembership.user_id)
                    .where(
                        Session.workspace_id == workspace_id,
                        Session.token_hash == hash_token(raw_token),
                        Session.revoked_at.is_(None),
                    )
                )
            ).first()
            if row is None:
                failure = AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            else:
                session, membership, user = row
                if membership.status != "ACTIVE" or user.status != "ACTIVE":
                    await self._revoke_inactive_member_sessions(
                        database,
                        workspace_id=workspace_id,
                        membership_id=membership.id,
                        now=now,
                    )
                    failure = AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
                elif is_session_absolute_expired(session, now=now):
                    failure = AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
                elif is_session_idle_expired(session, now=now, idle_timeout=self._idle_timeout):
                    session.revoked_at = now
                    failure = AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
                elif not await asyncio.to_thread(
                    self._passwords.verify, user.password_hash, password
                ):
                    failure = AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
                else:
                    factor = (
                        await database.scalars(
                            select(MfaFactor)
                            .where(
                                MfaFactor.workspace_id == workspace_id,
                                MfaFactor.membership_id == membership.id,
                                MfaFactor.factor_type == "TOTP",
                                MfaFactor.verified_at.is_not(None),
                            )
                            .with_for_update()
                        )
                    ).first()
                    if factor is None:
                        failure = AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
                    else:
                        secret = self._secret_provider.resolve_mfa_secret(
                            workspace_id, factor.secret_reference
                        )
                        counter = self._totp.matching_counter(secret, code, now, valid_window=1)
                        if counter is None or (
                            factor.last_used_counter is not None
                            and counter <= factor.last_used_counter
                        ):
                            failure = AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
                        else:
                            roles = frozenset(
                                Role(role)
                                for role in (
                                    await database.scalars(
                                        select(RoleBinding.role).where(
                                            RoleBinding.workspace_id == workspace_id,
                                            RoleBinding.membership_id == membership.id,
                                        )
                                    )
                                ).all()
                            )
                            if not roles:
                                failure = AuthenticationFailure(
                                    403, "Tài khoản chưa được cấp vai trò"
                                )
                            else:
                                factor.last_used_counter = counter
                                # SEC-10: xoay token phiên khi step-up, thu hồi phiên cũ,
                                # giữ nguyên CSRF hash để không làm gãy form/SPA đang mở.
                                new_session, new_raw_token, _ = issue_rotated_session(
                                    database,
                                    session,
                                    now=now,
                                    mfa_verified_at=now,
                                    mfa_method=MfaMethod.TOTP.value,
                                    preserve_csrf=True,
                                )
                                database.add(
                                    AuditEvent(
                                        workspace_id=workspace_id,
                                        actor_membership_id=membership.id,
                                        action="auth.session.rotated",
                                        target_type="session",
                                        target_id=new_session.id,
                                        reason="Xoay token phiên khi xác thực lại step-up",
                                        before_hash=state_hash({"old_session_id": str(session.id)}),
                                        after_hash=state_hash(
                                            {"new_session_id": str(new_session.id)}
                                        ),
                                        request_id=uuid4(),
                                        occurred_at=now,
                                    )
                                )
                                principal = Principal(
                                    user_id=user.id,
                                    membership_id=membership.id,
                                    workspace_id=workspace_id,
                                    roles=roles,
                                    mfa_verified_at=now,
                                    mfa_method=MfaMethod.TOTP,
                                    session_id=new_session.id,
                                )
                                rotated = SessionRotationResult(
                                    cookie_value=f"{workspace_id}:{new_raw_token}",
                                    principal=principal,
                                    email=user.email_normalized,
                                    expires_at=new_session.expires_at,
                                )
        if failure is not None:
            raise failure
        assert rotated is not None
        return rotated

    async def list_active_sessions(self, principal: Principal) -> list[SessionSummary]:
        """Liệt kê các phiên còn hiệu lực (chưa thu hồi, chưa idle/absolute timeout) của mình."""
        now = self._clock()
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, principal.workspace_id)
            rows = (
                await database.scalars(
                    select(Session)
                    .where(
                        Session.workspace_id == principal.workspace_id,
                        Session.membership_id == principal.membership_id,
                        Session.revoked_at.is_(None),
                        Session.expires_at > now,
                    )
                    .order_by(Session.last_seen_at.desc(), Session.created_at.desc())
                )
            ).all()
            summaries: list[SessionSummary] = []
            for row in rows:
                if is_session_idle_expired(row, now=now, idle_timeout=self._idle_timeout):
                    continue
                last_seen = effective_last_seen_at(row) or row.created_at
                summaries.append(
                    SessionSummary(
                        id=row.id,
                        created_at=row.created_at,
                        last_seen_at=last_seen,
                        expires_at=row.expires_at,
                        idle_expires_at=min(row.expires_at, last_seen + self._idle_timeout),
                        mfa_verified_at=row.mfa_verified_at,
                        mfa_method=row.mfa_method,
                        is_current=row.id == principal.session_id,
                    )
                )
            return summaries

    async def revoke_session(
        self,
        principal: Principal,
        *,
        session_id: UUID,
        reason: str | None = None,
        request_id: UUID | None = None,
    ) -> SessionRevocationResult:
        """Thu hồi một phiên của chính mình hoặc phiên của thành viên khác nếu là Owner."""
        now = self._clock()
        clean_reason = reason.strip() if reason and reason.strip() else "Thu hồi phiên đăng nhập"
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, principal.workspace_id)
            target = await database.scalar(
                select(Session)
                .where(
                    Session.workspace_id == principal.workspace_id,
                    Session.id == session_id,
                )
                .with_for_update()
            )
            if target is None:
                raise AuthenticationFailure(404, "Không tìm thấy phiên đăng nhập")
            if (
                target.membership_id != principal.membership_id
                and Role.OWNER not in principal.roles
            ):
                raise AuthenticationFailure(403, "Không đủ quyền thu hồi phiên của thành viên khác")
            was_revoked = target.revoked_at is not None
            if target.revoked_at is None:
                target.revoked_at = now
            revoked_at = target.revoked_at
            database.add(
                AuditEvent(
                    workspace_id=principal.workspace_id,
                    actor_membership_id=principal.membership_id,
                    action="auth.session.revoked",
                    target_type="session",
                    target_id=target.id,
                    reason=clean_reason,
                    before_hash=state_hash(
                        {
                            "session_id": str(target.id),
                            "membership_id": str(target.membership_id),
                            "was_revoked": was_revoked,
                        }
                    ),
                    after_hash=state_hash(
                        {
                            "session_id": str(target.id),
                            "revoked_at": revoked_at.isoformat(),
                        }
                    ),
                    request_id=request_id or uuid4(),
                    occurred_at=now,
                )
            )
            return SessionRevocationResult(
                session_id=target.id,
                membership_id=target.membership_id,
                revoked_at=revoked_at,
            )

    async def create_risk_profile(
        self,
        principal: Principal,
        *,
        name: str,
        max_risk_fraction: Decimal,
        settings: dict[str, object],
        max_open_risk_fraction: Decimal = Decimal("1.0"),
    ) -> UUID:
        from trading_bot.adapters.database.models import RiskProfileVersion

        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, principal.workspace_id)
            latest_version = await database.scalar(
                select(func.max(RiskProfileVersion.version)).where(
                    RiskProfileVersion.workspace_id == principal.workspace_id,
                    RiskProfileVersion.name == name,
                )
            )
            profile = RiskProfileVersion(
                workspace_id=principal.workspace_id,
                name=name.strip(),
                version=(latest_version or 0) + 1,
                max_risk_fraction=max_risk_fraction,
                max_open_risk_fraction=max_open_risk_fraction,
                settings=settings,
                is_active=False,
            )
            database.add(profile)
            await database.flush()
            return profile.id

    async def _revoke_inactive_member_sessions(
        self,
        database: AsyncSession,
        *,
        workspace_id: UUID,
        membership_id: UUID,
        now: datetime,
    ) -> None:
        revoked_ids = await revoke_membership_sessions(
            database,
            workspace_id=workspace_id,
            membership_id=membership_id,
            now=now,
        )
        database.add(
            AuditEvent(
                workspace_id=workspace_id,
                actor_membership_id=None,
                action="auth.sessions.revoked_inactive_member",
                target_type="user_membership",
                target_id=membership_id,
                reason=(
                    "Tự động thu hồi phiên do thành viên hoặc tài khoản không ở trạng thái ACTIVE"
                ),
                after_hash=state_hash({"revoked_count": len(revoked_ids)}),
                request_id=uuid4(),
                occurred_at=now,
            )
        )

    @staticmethod
    async def _set_workspace(database: AsyncSession, workspace_id: UUID) -> None:
        await database.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )


def normalize_email(email: str) -> str:
    normalized = email.strip().casefold()
    if len(normalized) > 320 or normalized.count("@") != 1:
        raise AuthenticationFailure(422, "Địa chỉ email không hợp lệ")
    local, domain = normalized.split("@", maxsplit=1)
    if not local or not domain or "." not in domain or any(char.isspace() for char in normalized):
        raise AuthenticationFailure(422, "Địa chỉ email không hợp lệ")
    return normalized


def normalize_slug(slug: str) -> str:
    normalized = slug.strip().casefold()
    if not 2 <= len(normalized) <= 80 or any(
        not (char.isascii() and (char.isalnum() or char == "-")) for char in normalized
    ):
        raise AuthenticationFailure(422, "Mã workspace chỉ gồm chữ thường, số và dấu gạch nối")
    return normalized


def hash_token(token: str) -> str:
    return hash_session_token(token)


def parse_cookie_value(cookie_value: str | None) -> tuple[UUID, str]:
    if not cookie_value or ":" not in cookie_value:
        raise AuthenticationFailure(401, "Cần đăng nhập")
    workspace_text, raw_token = cookie_value.split(":", maxsplit=1)
    try:
        workspace_id = UUID(workspace_text)
    except ValueError as error:
        raise AuthenticationFailure(401, "Cần đăng nhập") from error
    if len(raw_token) < 32 or len(raw_token) > 128:
        raise AuthenticationFailure(401, "Cần đăng nhập")
    return workspace_id, raw_token
