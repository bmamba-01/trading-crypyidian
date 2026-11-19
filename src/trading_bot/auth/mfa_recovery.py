"""Dịch vụ vòng đời MFA, mã khôi phục, đổi mật khẩu và vô hiệu bootstrap token (SEC-14..16)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.password_policy import PasswordPolicyViolation, validate_password_policy
from trading_bot.auth.policy import RECENT_MFA_LIFETIME, Permission, Principal, Role, authorize
from trading_bot.auth.security import PasswordManager, TotpManager
from trading_bot.auth.service import (
    SESSION_LIFETIME,
    AuthenticationFailure,
    LoginResult,
    hash_token,
    normalize_email,
    normalize_slug,
)
from trading_bot.domain.events import state_hash
from trading_bot.ports.secrets import SecretAccessError, SecretProvider

logger = logging.getLogger(__name__)

MIN_RECOVERY_CODE_COUNT = 8
MAX_RECOVERY_CODE_COUNT = 10
DEFAULT_RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 16
# Bảng chữ cái Crockford không nhầm lẫn (32 ký tự -> 5 bit/ký tự × 16 = 80 bit entropy/mã).
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RECOVERY_ALPHABET_SET = frozenset(_RECOVERY_ALPHABET)

# Cửa sổ lệch đồng hồ TOTP được tài liệu hóa (SEC-14):
# Chu kỳ RFC 6238 = 30s; valid_window=1 chấp nhận tối đa ±1 bước (±30s) quanh thời điểm hiện hành
# và từ chối mọi mã ở chu kỳ ±2 (±60s) trở ra, kết hợp last_used_counter đơn điệu chống phát lại.
TOTP_PERIOD_SECONDS = 30
TOTP_DRIFT_WINDOW_STEPS = 1
WEBAUTHN_PASSKEY_ROADMAP_NOTE = (
    "TOTP không chống phishing thời gian thực theo NIST SP 800-63B-4; "
    "lộ trình sau MVP bổ sung yếu tố WebAuthn/FIDO2 passkey gắn domain."
)

BOOTSTRAP_ALREADY_COMPLETED_CODE = "BOOTSTRAP_ALREADY_COMPLETED"


class AccountSecurityError(Exception):
    """Lỗi nghiệp vụ vòng đời MFA/mật khẩu/bootstrap với mã HTTP và code ổn định."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)

    def as_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class BootstrapRetirementStatus:
    """Trạng thái vô hiệu hóa bootstrap token sau khi hoàn tất thiết lập ban đầu (SEC-16)."""

    bootstrapped: bool
    token_file_configured: bool
    token_file_present: bool
    retired: bool
    warning: str | None = None


def generate_recovery_codes(count: int = DEFAULT_RECOVERY_CODE_COUNT) -> list[str]:
    """Sinh `count` (8–10) mã khôi phục ngẫu nhiên dùng một lần với 80 bit entropy mỗi mã."""
    if not MIN_RECOVERY_CODE_COUNT <= count <= MAX_RECOVERY_CODE_COUNT:
        raise AccountSecurityError(
            422,
            "INVALID_RECOVERY_CODE_COUNT",
            (
                f"Số lượng mã khôi phục phải nằm trong khoảng "
                f"{MIN_RECOVERY_CODE_COUNT}–{MAX_RECOVERY_CODE_COUNT}."
            ),
        )
    codes: list[str] = []
    seen: set[str] = set()
    while len(codes) < count:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        if raw in seen:
            continue
        seen.add(raw)
        codes.append(f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}")
    return codes


def normalize_recovery_code(raw: str) -> str:
    """Chuẩn hóa mã khôi phục (bỏ khoảng trắng và dấu gạch nối, viết hoa)."""
    if not isinstance(raw, str):
        raise ValueError("Mã khôi phục không hợp lệ")
    cleaned = raw.strip().replace("-", "").replace(" ", "").upper()
    if len(cleaned) != RECOVERY_CODE_LENGTH or any(
        char not in _RECOVERY_ALPHABET_SET for char in cleaned
    ):
        raise ValueError("Định dạng mã khôi phục không hợp lệ")
    return cleaned


def hash_recovery_code(
    normalized_code: str,
    *,
    salt: str,
    workspace_id: UUID,
    membership_id: UUID,
) -> str:
    """Băm HMAC-SHA256 kèm salt ngẫu nhiên và ràng buộc theo workspace/membership."""
    message = f"crypidian:mfa-recovery:{workspace_id}:{membership_id}:{normalized_code}".encode()
    return hmac.new(salt.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_recovery_code(
    candidate_raw: str,
    *,
    expected_hash: str,
    salt: str,
    workspace_id: UUID,
    membership_id: UUID,
) -> bool:
    """So khớp mã khôi phục theo hằng thời gian mà không làm rò rỉ dữ liệu."""
    try:
        normalized = normalize_recovery_code(candidate_raw)
    except ValueError:
        return False
    candidate_hash = hash_recovery_code(
        normalized,
        salt=salt,
        workspace_id=workspace_id,
        membership_id=membership_id,
    )
    return hmac.compare_digest(candidate_hash, expected_hash)


def check_bootstrap_token_retired(
    token_file: Path | None,
    *,
    bootstrapped: bool = True,
    log_warning: bool = True,
) -> BootstrapRetirementStatus:
    """Kiểm tra file bootstrap token đã xóa/gỡ mount sau khi bootstrap xong chưa (SEC-16)."""
    configured = token_file is not None
    present = bool(token_file is not None and token_file.exists())
    retired = (not bootstrapped) or (not present)
    warning: str | None = None
    if not retired:
        warning = (
            "BOOTSTRAP_TOKEN_NOT_RETIRED: File bootstrap token vẫn còn tồn tại sau khi "
            "đã hoàn tất bootstrap; hãy xóa file và gỡ mount secret bootstrap_token."
        )
        if log_warning:
            logger.warning(warning)
    return BootstrapRetirementStatus(
        bootstrapped=bootstrapped,
        token_file_configured=configured,
        token_file_present=present,
        retired=retired,
        warning=warning,
    )


async def inspect_bootstrap_state(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[bool, bool]:
    """Trả về `(has_any_identity, has_pending_bootstrap)` dựa trên bảng Workspace và UserAccount."""
    async with sessions() as database, database.begin():
        has_workspace = await database.scalar(select(Workspace.id).limit(1))
        has_user = await database.scalar(select(UserAccount.id).limit(1))
        has_any_identity = has_workspace is not None or has_user is not None
        if not has_any_identity:
            return False, False
        setup_workspace = await database.scalar(
            select(Workspace.id).where(Workspace.status == "SETUP").limit(1)
        )
        pending_user = await database.scalar(
            select(UserAccount.id).where(UserAccount.status == "PENDING_MFA").limit(1)
        )
        return True, (setup_workspace is not None and pending_user is not None)


async def check_database_bootstrap_retirement(
    sessions: async_sessionmaker[AsyncSession],
    token_file: Path | None,
    *,
    log_warning: bool = True,
) -> BootstrapRetirementStatus:
    """Truy vấn trạng thái bootstrap trong DB và cảnh báo nếu bootstrap token file vẫn tồn tại."""
    has_any_identity, has_pending_bootstrap = await inspect_bootstrap_state(sessions)
    bootstrapped = has_any_identity and not has_pending_bootstrap
    return check_bootstrap_token_retired(
        token_file,
        bootstrapped=bootstrapped,
        log_warning=log_warning,
    )


async def assert_bootstrap_allowed(
    sessions: async_sessionmaker[AsyncSession],
    *,
    phase: Literal["start", "verify"],
) -> None:
    """Từ chối vĩnh viễn mọi lời gọi `/api/v1/auth/bootstrap*` sau khi đã bootstrap (SEC-16)."""
    has_any_identity, has_pending_bootstrap = await inspect_bootstrap_state(sessions)
    if phase == "start" and has_any_identity:
        raise AccountSecurityError(
            409,
            BOOTSTRAP_ALREADY_COMPLETED_CODE,
            "Thiết lập ban đầu đã hoàn tất.",
        )
    if phase == "verify" and has_any_identity and not has_pending_bootstrap:
        raise AccountSecurityError(
            409,
            BOOTSTRAP_ALREADY_COMPLETED_CODE,
            "Thiết lập ban đầu đã hoàn tất.",
        )


def _require_recent_mfa(principal: Principal, now: datetime) -> None:
    if principal.mfa_verified_at > now or now - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise AccountSecurityError(
            403,
            "RECENT_MFA_REQUIRED",
            "Thao tác nhạy cảm yêu cầu xác thực MFA gần đây.",
        )


class AccountSecurityService:
    """Điều phối vòng đời MFA (recovery code, xoay TOTP, reset an toàn) và đổi mật khẩu."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        secret_provider: SecretProvider,
        *,
        passwords: PasswordManager | None = None,
        totp: TotpManager | None = None,
        clock: Callable[[], datetime] | None = None,
        extra_password_blocklist: Collection[str] | None = None,
    ) -> None:
        self._sessions = sessions
        self._secret_provider = secret_provider
        self._passwords = passwords or PasswordManager()
        self._totp = totp or TotpManager()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._extra_password_blocklist = extra_password_blocklist
        self._dummy_hash: str | None = None

    def _get_dummy_hash(self) -> str:
        if self._dummy_hash is None:
            self._dummy_hash = self._passwords.hash("crypidian-dummy-timing-guard-2026")
        return self._dummy_hash

    async def regenerate_recovery_codes(
        self,
        principal: Principal,
        *,
        current_password: str,
        count: int = DEFAULT_RECOVERY_CODE_COUNT,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Sinh bộ mã khôi phục mới (8–10 mã một lần), thu hồi bộ cũ và trả plaintext đúng 1 lần."""
        now = self._clock()
        _require_recent_mfa(principal, now)
        plaintext_codes = generate_recovery_codes(count)
        batch_id = uuid4()

        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        ) as unit:
            membership = await unit.session.scalar(
                select(UserMembership)
                .where(
                    UserMembership.workspace_id == principal.workspace_id,
                    UserMembership.id == principal.membership_id,
                    UserMembership.status == "ACTIVE",
                )
                .with_for_update()
            )
            user = await unit.session.scalar(
                select(UserAccount).where(
                    UserAccount.id == principal.user_id,
                    UserAccount.status == "ACTIVE",
                )
            )
            if membership is None or user is None:
                raise AccountSecurityError(401, "INVALID_ACCOUNT", "Tài khoản không còn hoạt động.")
            if not await asyncio.to_thread(
                self._passwords.verify, user.password_hash, current_password
            ):
                raise AccountSecurityError(
                    401, "INVALID_CREDENTIALS", "Mật khẩu hiện tại không chính xác."
                )

            revoked_rows = (
                await unit.session.execute(
                    update(MfaRecoveryCode)
                    .where(
                        MfaRecoveryCode.workspace_id == principal.workspace_id,
                        MfaRecoveryCode.membership_id == principal.membership_id,
                        MfaRecoveryCode.used_at.is_(None),
                        MfaRecoveryCode.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                    .returning(MfaRecoveryCode.id)
                )
            ).all()

            for raw_code in plaintext_codes:
                normalized = normalize_recovery_code(raw_code)
                salt = secrets.token_hex(16)
                digest = hash_recovery_code(
                    normalized,
                    salt=salt,
                    workspace_id=principal.workspace_id,
                    membership_id=principal.membership_id,
                )
                unit.session.add(
                    MfaRecoveryCode(
                        workspace_id=principal.workspace_id,
                        user_id=principal.user_id,
                        membership_id=principal.membership_id,
                        batch_id=batch_id,
                        code_salt=salt,
                        code_hash=digest,
                    )
                )

            unit.record_audit(
                action="auth.mfa_recovery_codes.regenerated",
                target_type="user_membership",
                target_id=principal.membership_id,
                reason="Tạo mới bộ mã khôi phục MFA dùng một lần",
                after_state={
                    "batch_id": str(batch_id),
                    "issued_count": len(plaintext_codes),
                    "revoked_count": len(revoked_rows),
                },
            )

        return {
            "recovery_codes": plaintext_codes,
            "count": len(plaintext_codes),
            "revoked_count": len(revoked_rows),
            "batch_id": str(batch_id),
            "warning": (
                "Lưu giữ các mã khôi phục ở nơi an toàn ngay bây giờ; "
                "mỗi mã chỉ hiển thị một lần và chỉ dùng được một lần."
            ),
        }

    async def recovery_code_status(self, principal: Principal) -> dict[str, Any]:
        """Trả về số lượng mã khôi phục còn hiệu lực/đã dùng (tuyệt đối không lộ mã hay hash)."""
        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        ) as unit:
            rows = (
                await unit.session.scalars(
                    select(MfaRecoveryCode).where(
                        MfaRecoveryCode.workspace_id == principal.workspace_id,
                        MfaRecoveryCode.membership_id == principal.membership_id,
                    )
                )
            ).all()
        active_count = sum(1 for r in rows if r.used_at is None and r.revoked_at is None)
        used_count = sum(1 for r in rows if r.used_at is not None)
        revoked_count = sum(1 for r in rows if r.revoked_at is not None)
        latest_created = max((r.created_at for r in rows if r.created_at is not None), default=None)
        return {
            "active_count": active_count,
            "used_count": used_count,
            "revoked_count": revoked_count,
            "last_generated_at": latest_created.isoformat() if latest_created is not None else None,
        }

    async def recovery_login(
        self,
        *,
        workspace_slug: str,
        email: str,
        password: str,
        recovery_code: str,
        request_id: UUID | None = None,
    ) -> LoginResult:
        """Đăng nhập/khôi phục một lần bằng mật khẩu + recovery code, tiêu thụ nguyên tử."""
        normalized_email = normalize_email(email)
        validated_slug = normalize_slug(workspace_slug)
        now = self._clock()
        try:
            normalized_code = normalize_recovery_code(recovery_code)
            code_format_ok = True
        except ValueError:
            normalized_code = ""
            code_format_ok = False

        async with self._sessions() as database, database.begin():
            user = await database.scalar(
                select(UserAccount).where(
                    UserAccount.email_normalized == normalized_email,
                    UserAccount.status == "ACTIVE",
                )
            )
            workspace = await database.scalar(
                select(Workspace).where(
                    Workspace.slug == validated_slug,
                    Workspace.status == "ACTIVE",
                )
            )
            target_hash = user.password_hash if user is not None else self._get_dummy_hash()
            password_ok = await asyncio.to_thread(self._passwords.verify, target_hash, password)
            if user is None or workspace is None or not password_ok or not code_format_ok:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

            await database.execute(
                text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
                {"workspace_id": str(workspace.id)},
            )
            membership = await database.scalar(
                select(UserMembership).where(
                    UserMembership.workspace_id == workspace.id,
                    UserMembership.user_id == user.id,
                    UserMembership.status == "ACTIVE",
                )
            )
            if membership is None:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

            active_codes = (
                await database.scalars(
                    select(MfaRecoveryCode)
                    .where(
                        MfaRecoveryCode.workspace_id == workspace.id,
                        MfaRecoveryCode.membership_id == membership.id,
                        MfaRecoveryCode.used_at.is_(None),
                        MfaRecoveryCode.revoked_at.is_(None),
                    )
                    .with_for_update()
                )
            ).all()

            matched_row: MfaRecoveryCode | None = None
            for candidate_row in active_codes:
                if verify_recovery_code(
                    normalized_code,
                    expected_hash=candidate_row.code_hash,
                    salt=candidate_row.code_salt,
                    workspace_id=workspace.id,
                    membership_id=membership.id,
                ):
                    matched_row = candidate_row

            if matched_row is None:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

            consumed_id = await database.scalar(
                update(MfaRecoveryCode)
                .where(
                    MfaRecoveryCode.id == matched_row.id,
                    MfaRecoveryCode.workspace_id == workspace.id,
                    MfaRecoveryCode.membership_id == membership.id,
                    MfaRecoveryCode.used_at.is_(None),
                    MfaRecoveryCode.revoked_at.is_(None),
                )
                .values(used_at=now)
                .returning(MfaRecoveryCode.id)
            )
            if consumed_id is None:
                raise AuthenticationFailure(401, "Thông tin xác thực không hợp lệ")

            bindings = await database.scalars(
                select(RoleBinding.role).where(
                    RoleBinding.workspace_id == workspace.id,
                    RoleBinding.membership_id == membership.id,
                )
            )
            roles = frozenset(Role(role) for role in bindings.all())
            if not roles:
                raise AuthenticationFailure(403, "Tài khoản chưa được cấp vai trò")

            if self._passwords.needs_rehash(user.password_hash):
                user.password_hash = await asyncio.to_thread(self._passwords.hash, password)

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
                    expires_at=now + SESSION_LIFETIME,
                    mfa_verified_at=now,
                    mfa_method=None,
                )
            )
            database.add(
                AuditEvent(
                    workspace_id=workspace.id,
                    actor_membership_id=membership.id,
                    action="auth.mfa_recovery.login_succeeded",
                    target_type="mfa_recovery_code",
                    target_id=matched_row.id,
                    reason="Đăng nhập khôi phục bằng mã dùng một lần",
                    after_hash=state_hash({"session_id": str(session_id)}),
                    request_id=request_id or uuid4(),
                    occurred_at=now,
                )
            )
            principal = Principal(
                user_id=user.id,
                membership_id=membership.id,
                workspace_id=workspace.id,
                roles=roles,
                mfa_verified_at=now,
                mfa_method=None,
                session_id=session_id,
            )

        return LoginResult(
            cookie_value=f"{principal.workspace_id}:{raw_token}",
            csrf_token=csrf_token,
            principal=principal,
            email=normalized_email,
        )

    async def initiate_totp_rotation(
        self,
        principal: Principal,
        *,
        current_password: str,
        current_code: str | None = None,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Khởi tạo TOTP factor mới (chưa kích hoạt); yêu cầu MFA gần đây + mật khẩu hiện tại."""
        now = self._clock()
        _require_recent_mfa(principal, now)
        new_secret = self._totp.generate_secret()
        reference: str | None = None

        try:
            async with UnitOfWork(
                self._sessions,
                workspace_id=principal.workspace_id,
                actor_membership_id=principal.membership_id,
                request_id=request_id,
            ) as unit:
                membership = await unit.session.scalar(
                    select(UserMembership)
                    .where(
                        UserMembership.workspace_id == principal.workspace_id,
                        UserMembership.id == principal.membership_id,
                        UserMembership.status == "ACTIVE",
                    )
                    .with_for_update()
                )
                user = await unit.session.scalar(
                    select(UserAccount).where(
                        UserAccount.id == principal.user_id,
                        UserAccount.status == "ACTIVE",
                    )
                )
                if membership is None or user is None:
                    raise AccountSecurityError(
                        401, "INVALID_ACCOUNT", "Tài khoản không còn hoạt động."
                    )
                if not await asyncio.to_thread(
                    self._passwords.verify, user.password_hash, current_password
                ):
                    raise AccountSecurityError(
                        401, "INVALID_CREDENTIALS", "Mật khẩu hiện tại không chính xác."
                    )

                if current_code is not None:
                    active_factor = await unit.session.scalar(
                        select(MfaFactor)
                        .where(
                            MfaFactor.workspace_id == principal.workspace_id,
                            MfaFactor.membership_id == principal.membership_id,
                            MfaFactor.factor_type == "TOTP",
                            MfaFactor.verified_at.is_not(None),
                        )
                        .with_for_update()
                    )
                    if active_factor is not None:
                        active_secret = self._secret_provider.resolve_mfa_secret(
                            principal.workspace_id, active_factor.secret_reference
                        )
                        counter = self._totp.matching_counter(
                            active_secret,
                            current_code,
                            now,
                            valid_window=TOTP_DRIFT_WINDOW_STEPS,
                        )
                        if counter is None or (
                            active_factor.last_used_counter is not None
                            and counter <= active_factor.last_used_counter
                        ):
                            raise AccountSecurityError(
                                401, "INVALID_TOTP_CODE", "Mã TOTP hiện tại không hợp lệ."
                            )
                        active_factor.last_used_counter = counter

                reference = self._secret_provider.store_mfa_secret(
                    principal.workspace_id, new_secret
                )
                unit.session.add(
                    SecretReference(
                        workspace_id=principal.workspace_id,
                        provider="local_mfa",
                        reference_key=reference,
                        status="ACTIVE",
                    )
                )
                factor = MfaFactor(
                    workspace_id=principal.workspace_id,
                    membership_id=principal.membership_id,
                    factor_type="TOTP",
                    secret_reference=reference,
                    verified_at=None,
                )
                unit.session.add(factor)
                await unit.session.flush()
                factor_id = factor.id
                email = user.email_normalized

                unit.record_audit(
                    action="auth.mfa_totp.rotation_initiated",
                    target_type="mfa_factor",
                    target_id=factor_id,
                    reason="Khởi tạo yếu tố TOTP mới chờ xác minh",
                    after_state={"factor_id": str(factor_id)},
                )
        except Exception:
            if reference is not None:
                self._secret_provider.delete_mfa_secret(principal.workspace_id, reference)
            raise

        return {
            "factor_id": str(factor_id),
            "mfa_setup_uri": self._totp.provisioning_uri(new_secret, account=email),
            "status": "pending_verification",
            "drift_window_steps": TOTP_DRIFT_WINDOW_STEPS,
            "period_seconds": TOTP_PERIOD_SECONDS,
        }

    async def confirm_totp_rotation(
        self,
        principal: Principal,
        *,
        factor_id: UUID,
        code: str,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Xác minh mã TOTP mới trước khi kích hoạt, vô hiệu factor cũ và thu hồi phiên khác."""
        now = self._clock()
        _require_recent_mfa(principal, now)

        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        ) as unit:
            pending_factor = await unit.session.scalar(
                select(MfaFactor)
                .where(
                    MfaFactor.workspace_id == principal.workspace_id,
                    MfaFactor.membership_id == principal.membership_id,
                    MfaFactor.id == factor_id,
                    MfaFactor.factor_type == "TOTP",
                    MfaFactor.verified_at.is_(None),
                )
                .with_for_update()
            )
            if pending_factor is None:
                raise AccountSecurityError(
                    404,
                    "MFA_FACTOR_NOT_FOUND",
                    "Không tìm thấy yếu tố TOTP đang chờ kích hoạt.",
                )

            secret = self._secret_provider.resolve_mfa_secret(
                principal.workspace_id, pending_factor.secret_reference
            )
            counter = self._totp.matching_counter(
                secret, code, now, valid_window=TOTP_DRIFT_WINDOW_STEPS
            )
            if counter is None:
                raise AccountSecurityError(401, "INVALID_TOTP_CODE", "Mã TOTP mới không hợp lệ.")

            old_factors = (
                await unit.session.scalars(
                    select(MfaFactor)
                    .where(
                        MfaFactor.workspace_id == principal.workspace_id,
                        MfaFactor.membership_id == principal.membership_id,
                        MfaFactor.factor_type == "TOTP",
                        MfaFactor.verified_at.is_not(None),
                        MfaFactor.id != pending_factor.id,
                    )
                    .with_for_update()
                )
            ).all()

            refs_to_delete: list[str] = []
            for old in old_factors:
                old.verified_at = None
                if old.secret_reference != pending_factor.secret_reference:
                    refs_to_delete.append(old.secret_reference)

            pending_factor.verified_at = now
            pending_factor.last_used_counter = counter

            if principal.session_id is not None:
                await unit.session.execute(
                    update(Session)
                    .where(
                        Session.workspace_id == principal.workspace_id,
                        Session.id == principal.session_id,
                        Session.revoked_at.is_(None),
                    )
                    .values(mfa_verified_at=now)
                )
                revoked_rows = (
                    await unit.session.execute(
                        update(Session)
                        .where(
                            Session.workspace_id == principal.workspace_id,
                            Session.membership_id == principal.membership_id,
                            Session.id != principal.session_id,
                            Session.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                        .returning(Session.id)
                    )
                ).all()
            else:
                revoked_rows = []

            def _cleanup_old_secrets(refs: tuple[str, ...] = tuple(refs_to_delete)) -> None:
                for ref in refs:
                    try:
                        self._secret_provider.delete_mfa_secret(principal.workspace_id, ref)
                    except SecretAccessError:
                        logger.warning("Không xóa được secret MFA cũ sau xoay vòng")

            if refs_to_delete:
                unit.after_commit(_cleanup_old_secrets)

            unit.record_audit(
                action="auth.mfa_totp.rotated",
                target_type="mfa_factor",
                target_id=pending_factor.id,
                reason="Kích hoạt yếu tố TOTP mới sau xác minh mã",
                after_state={
                    "factor_id": str(pending_factor.id),
                    "deactivated_factors": len(old_factors),
                    "revoked_other_sessions": len(revoked_rows),
                },
            )

        return {
            "factor_id": str(pending_factor.id),
            "status": "active",
            "deactivated_factors": len(old_factors),
            "revoked_other_sessions": len(revoked_rows),
        }

    async def admin_reset_member_mfa(
        self,
        principal: Principal,
        *,
        target_membership_id: UUID,
        current_password: str,
        reason: str,
        identity_verified: bool,
        issue_one_time_recovery_code: bool = True,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Owner đặt lại MFA sau xác minh danh tính; owner tuyệt đối không biết secret TOTP."""
        if not authorize(principal, Permission.MEMBER_WRITE):
            raise AccountSecurityError(
                403, "PERMISSION_DENIED", "Không đủ quyền đặt lại MFA của thành viên."
            )
        now = self._clock()
        _require_recent_mfa(principal, now)
        if not identity_verified or len(reason.strip()) < 5:
            raise AccountSecurityError(
                422,
                "IDENTITY_VERIFICATION_REQUIRED",
                "Phải xác nhận đã kiểm tra danh tính và ghi lý do tối thiểu 5 ký tự.",
            )

        one_time_code: str | None = None
        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        ) as unit:
            actor_user = await unit.session.scalar(
                select(UserAccount).where(
                    UserAccount.id == principal.user_id,
                    UserAccount.status == "ACTIVE",
                )
            )
            if actor_user is None or not await asyncio.to_thread(
                self._passwords.verify, actor_user.password_hash, current_password
            ):
                raise AccountSecurityError(
                    401, "INVALID_CREDENTIALS", "Mật khẩu xác nhận của quản trị viên không đúng."
                )

            target = await unit.session.scalar(
                select(UserMembership)
                .where(
                    UserMembership.workspace_id == principal.workspace_id,
                    UserMembership.id == target_membership_id,
                )
                .with_for_update()
            )
            if target is None:
                raise AccountSecurityError(
                    404, "MEMBER_NOT_FOUND", "Không tìm thấy thành viên trong workspace."
                )

            factors = (
                await unit.session.scalars(
                    select(MfaFactor)
                    .where(
                        MfaFactor.workspace_id == principal.workspace_id,
                        MfaFactor.membership_id == target_membership_id,
                        MfaFactor.verified_at.is_not(None),
                    )
                    .with_for_update()
                )
            ).all()

            refs_to_delete: list[str] = []
            for factor in factors:
                factor.verified_at = None
                refs_to_delete.append(factor.secret_reference)

            revoked_codes = (
                await unit.session.execute(
                    update(MfaRecoveryCode)
                    .where(
                        MfaRecoveryCode.workspace_id == principal.workspace_id,
                        MfaRecoveryCode.membership_id == target_membership_id,
                        MfaRecoveryCode.used_at.is_(None),
                        MfaRecoveryCode.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                    .returning(MfaRecoveryCode.id)
                )
            ).all()

            revoked_sessions = (
                await unit.session.execute(
                    update(Session)
                    .where(
                        Session.workspace_id == principal.workspace_id,
                        Session.membership_id == target_membership_id,
                        Session.revoked_at.is_(None),
                    )
                    .values(revoked_at=now)
                    .returning(Session.id)
                )
            ).all()

            if issue_one_time_recovery_code:
                one_time_code = generate_recovery_codes(MIN_RECOVERY_CODE_COUNT)[0]
                salt = secrets.token_hex(16)
                digest = hash_recovery_code(
                    normalize_recovery_code(one_time_code),
                    salt=salt,
                    workspace_id=principal.workspace_id,
                    membership_id=target_membership_id,
                )
                unit.session.add(
                    MfaRecoveryCode(
                        workspace_id=principal.workspace_id,
                        user_id=target.user_id,
                        membership_id=target_membership_id,
                        batch_id=uuid4(),
                        code_salt=salt,
                        code_hash=digest,
                    )
                )

            def _cleanup(refs: tuple[str, ...] = tuple(refs_to_delete)) -> None:
                for ref in refs:
                    try:
                        self._secret_provider.delete_mfa_secret(principal.workspace_id, ref)
                    except SecretAccessError:
                        logger.warning("Không xóa được secret MFA cũ khi admin reset")

            if refs_to_delete:
                unit.after_commit(_cleanup)

            unit.record_audit(
                action="auth.mfa.admin_reset",
                target_type="user_membership",
                target_id=target_membership_id,
                reason=reason.strip(),
                after_state={
                    "disabled_factors": len(factors),
                    "revoked_recovery_codes": len(revoked_codes),
                    "revoked_sessions": len(revoked_sessions),
                    "issued_one_time_recovery_code": issue_one_time_recovery_code,
                },
            )

        result: dict[str, Any] = {
            "membership_id": str(target_membership_id),
            "disabled_factors": len(factors),
            "revoked_recovery_codes": len(revoked_codes),
            "revoked_sessions": len(revoked_sessions),
            "status": "RESET",
        }
        if one_time_code is not None:
            result["one_time_recovery_code"] = one_time_code
        return result

    async def change_password(
        self,
        principal: Principal,
        *,
        current_password: str,
        new_password: str,
        request_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Đổi mật khẩu (SEC-15): kiểm mật khẩu cũ, MFA gần đây, blocklist, thu hồi phiên khác."""
        now = self._clock()
        _require_recent_mfa(principal, now)
        try:
            validate_password_policy(
                new_password,
                current_password=current_password,
                extra_blocklist=self._extra_password_blocklist,
            )
        except PasswordPolicyViolation as error:
            raise AccountSecurityError(422, error.code, error.message) from error

        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            request_id=request_id,
        ) as unit:
            user = await unit.session.scalar(
                select(UserAccount)
                .where(
                    UserAccount.id == principal.user_id,
                    UserAccount.status == "ACTIVE",
                )
                .with_for_update()
            )
            if user is None:
                raise AccountSecurityError(401, "INVALID_ACCOUNT", "Tài khoản không còn hoạt động.")
            if not await asyncio.to_thread(
                self._passwords.verify, user.password_hash, current_password
            ):
                raise AccountSecurityError(
                    401, "INVALID_CREDENTIALS", "Mật khẩu hiện tại không chính xác."
                )
            try:
                validate_password_policy(
                    new_password,
                    email=user.email_normalized,
                    current_password=current_password,
                    extra_blocklist=self._extra_password_blocklist,
                )
            except PasswordPolicyViolation as error:
                raise AccountSecurityError(422, error.code, error.message) from error

            new_hash = await asyncio.to_thread(self._passwords.hash, new_password)
            user.password_hash = new_hash

            membership_ids = list(
                (
                    await unit.session.scalars(
                        select(UserMembership.id).where(
                            UserMembership.workspace_id == principal.workspace_id,
                            UserMembership.user_id == principal.user_id,
                        )
                    )
                ).all()
            )
            if not membership_ids:
                membership_ids = [principal.membership_id]

            revoke_conditions: list[Any] = [
                Session.workspace_id == principal.workspace_id,
                Session.membership_id.in_(membership_ids),
                Session.revoked_at.is_(None),
            ]
            if principal.session_id is not None:
                revoke_conditions.append(Session.id != principal.session_id)

            revoked_rows = (
                await unit.session.execute(
                    update(Session)
                    .where(*revoke_conditions)
                    .values(revoked_at=now)
                    .returning(Session.id)
                )
            ).all()

            unit.record_audit(
                action="auth.password.changed",
                target_type="user_account",
                target_id=principal.user_id,
                reason="Người dùng đổi mật khẩu qua trang bảo mật tài khoản",
                after_state={"revoked_other_sessions": len(revoked_rows)},
            )

        return {
            "status": "password_changed",
            "revoked_other_sessions": len(revoked_rows),
        }


__all__ = [
    "BOOTSTRAP_ALREADY_COMPLETED_CODE",
    "DEFAULT_RECOVERY_CODE_COUNT",
    "MAX_RECOVERY_CODE_COUNT",
    "MIN_RECOVERY_CODE_COUNT",
    "TOTP_DRIFT_WINDOW_STEPS",
    "TOTP_PERIOD_SECONDS",
    "WEBAUTHN_PASSKEY_ROADMAP_NOTE",
    "AccountSecurityError",
    "AccountSecurityService",
    "BootstrapRetirementStatus",
    "assert_bootstrap_allowed",
    "check_bootstrap_token_retired",
    "check_database_bootstrap_retirement",
    "generate_recovery_codes",
    "hash_recovery_code",
    "inspect_bootstrap_state",
    "normalize_recovery_code",
    "verify_recovery_code",
]
