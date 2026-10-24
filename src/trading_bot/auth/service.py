import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    MfaFactor,
    RoleBinding,
    SecretReference,
    Session,
    UserAccount,
    UserMembership,
    Workspace,
)
from trading_bot.auth.policy import Principal, Role
from trading_bot.auth.security import PasswordManager, TotpManager
from trading_bot.ports.secrets import SecretProvider

SESSION_LIFETIME = timedelta(hours=12)
RECENT_MFA_LIFETIME = timedelta(minutes=15)


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
class AuthIdentity:
    principal: Principal
    email: str


class AuthService:
    """Điều phối bootstrap, MFA, session và phạm vi truy cập workspace."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        secret_provider: SecretProvider,
        *,
        passwords: PasswordManager | None = None,
        totp: TotpManager | None = None,
    ) -> None:
        self._sessions = sessions
        self._secret_provider = secret_provider
        self._passwords = passwords or PasswordManager()
        self._totp = totp or TotpManager()

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
            counter = self._totp.matching_counter(secret, code, datetime.now(UTC), valid_window=1)
            if counter is None:
                raise AuthenticationFailure(401, "Mã xác thực không hợp lệ")
            factor.verified_at = datetime.now(UTC)
            factor.last_used_counter = counter
            user.status = "ACTIVE"
            workspace.status = "ACTIVE"

    async def login(
        self, *, workspace_slug: str, email: str, password: str, code: str
    ) -> LoginResult:
        normalized_email = normalize_email(email)
        now = datetime.now(UTC)
        async with self._sessions() as database, database.begin():
            user = await database.scalar(
                select(UserAccount).where(
                    UserAccount.email_normalized == normalized_email,
                    UserAccount.status == "ACTIVE",
                )
            )
            workspace = await database.scalar(
                select(Workspace).where(
                    Workspace.slug == normalize_slug(workspace_slug),
                    Workspace.status == "ACTIVE",
                )
            )
            if (
                user is None
                or workspace is None
                or not await asyncio.to_thread(self._passwords.verify, user.password_hash, password)
            ):
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
            database.add(
                Session(
                    workspace_id=workspace.id,
                    membership_id=membership.id,
                    token_hash=hash_token(raw_token),
                    csrf_token_hash=hash_token(csrf_token),
                    expires_at=now + SESSION_LIFETIME,
                    mfa_verified_at=now,
                )
            )
            principal = Principal(
                user_id=user.id,
                membership_id=membership.id,
                workspace_id=workspace.id,
                roles=roles,
                mfa_verified_at=now,
            )
        return LoginResult(
            cookie_value=f"{principal.workspace_id}:{raw_token}",
            csrf_token=csrf_token,
            principal=principal,
            email=normalized_email,
        )

    async def authenticate(self, cookie_value: str | None) -> AuthIdentity:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        now = datetime.now(UTC)
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
                        Session.expires_at > now,
                        UserMembership.status == "ACTIVE",
                        UserAccount.status == "ACTIVE",
                    )
                )
            ).first()
            if row is None:
                raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")
            session, membership, user = row
            if session.mfa_verified_at is None or session.csrf_token_hash is None:
                raise AuthenticationFailure(401, "Phiên đăng nhập cần xác thực lại")
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
                raise AuthenticationFailure(403, "Tài khoản chưa được cấp vai trò")
            principal = Principal(
                user_id=user.id,
                membership_id=membership.id,
                workspace_id=workspace_id,
                roles=roles,
                mfa_verified_at=session.mfa_verified_at,
            )
            return AuthIdentity(principal=principal, email=user.email_normalized)

    async def verify_csrf(self, cookie_value: str | None, csrf_token: str | None) -> None:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        if not csrf_token:
            raise AuthenticationFailure(403, "Thiếu mã CSRF")
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, workspace_id)
            session = await database.scalar(
                select(Session).where(
                    Session.workspace_id == workspace_id,
                    Session.token_hash == hash_token(raw_token),
                    Session.revoked_at.is_(None),
                    Session.expires_at > datetime.now(UTC),
                )
            )
            if (
                session is None
                or session.csrf_token_hash is None
                or not secrets.compare_digest(session.csrf_token_hash, hash_token(csrf_token))
            ):
                raise AuthenticationFailure(403, "Mã CSRF không hợp lệ")

    async def logout(self, cookie_value: str | None) -> None:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
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
                session.revoked_at = datetime.now(UTC)

    async def issue_csrf_token(self, cookie_value: str | None) -> str:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        token = secrets.token_urlsafe(32)
        async with self._sessions() as database, database.begin():
            await self._set_workspace(database, workspace_id)
            session = await database.scalar(
                select(Session).where(
                    Session.workspace_id == workspace_id,
                    Session.token_hash == hash_token(raw_token),
                    Session.revoked_at.is_(None),
                    Session.expires_at > datetime.now(UTC),
                )
            )
            if session is None:
                raise AuthenticationFailure(401, "Phiên đăng nhập không hợp lệ hoặc đã hết hạn")
            session.csrf_token_hash = hash_token(token)
        return token

    async def reauthenticate(
        self,
        cookie_value: str | None,
        *,
        password: str,
        code: str,
    ) -> None:
        workspace_id, raw_token = parse_cookie_value(cookie_value)
        now = datetime.now(UTC)
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
                        Session.expires_at > now,
                        UserMembership.status == "ACTIVE",
                        UserAccount.status == "ACTIVE",
                    )
                )
            ).first()
            if row is None:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            session, membership, user = row
            if not await asyncio.to_thread(self._passwords.verify, user.password_hash, password):
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
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
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            secret = self._secret_provider.resolve_mfa_secret(workspace_id, factor.secret_reference)
            counter = self._totp.matching_counter(secret, code, now, valid_window=1)
            if counter is None or (
                factor.last_used_counter is not None and counter <= factor.last_used_counter
            ):
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")
            factor.last_used_counter = counter
            session.mfa_verified_at = now

    async def create_risk_profile(
        self,
        principal: Principal,
        *,
        name: str,
        max_risk_fraction: Decimal,
        settings: dict[str, object],
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
                settings=settings,
                is_active=False,
            )
            database.add(profile)
            await database.flush()
            return profile.id

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
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
