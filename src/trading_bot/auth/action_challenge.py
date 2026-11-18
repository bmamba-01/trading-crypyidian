"""Dịch vụ step-up theo hành động (transaction authorization, SEC-20).

Mỗi thao tác rủi ro cao yêu cầu một challenge một lần (TTL ≤ 5 phút) gắn chặt với:
- `session_id` và `membership_id` / `workspace_id` của phiên đang hoạt động;
- tên hành động (`action`);
- SHA-256 JSON chuẩn hóa của payload hiển thị cho người dùng (WYSIWYS).

Tất cả bước xác minh TOTP, đếm số lần nhập sai (sai 5 lần tự động khóa/hủy challenge) và
tiêu thụ (`consumed_at`) đều khóa hàng nguyên tử trong transaction trước khi thực thi tác vụ.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.action_challenges import (
    ActionChallengeRepository,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import RECENT_MFA_LIFETIME, Principal
from trading_bot.auth.security import TotpManager
from trading_bot.config.workspace_settings import decimal_text
from trading_bot.ports.secrets import SecretProvider

MAX_ACTION_CHALLENGE_TTL_SECONDS = 300
DEFAULT_ACTION_CHALLENGE_TTL_SECONDS = 300
MAX_ACTION_CHALLENGE_ATTEMPTS = 5

STEP_UP_CHALLENGE_HEADER = "X-Step-Up-Challenge-Id"
STEP_UP_CODE_HEADER = "X-Step-Up-Code"
STEP_UP_TOKEN_HEADER = "X-Step-Up-Token"

ACTION_RISK_PROFILE_CREATE = "risk_profile.create"
ACTION_RISK_APPETITE_SAVE = "risk_appetite.save"
ACTION_SETTINGS_PUBLISH = "settings.publish"
ACTION_SETTINGS_ROLLBACK = "settings.rollback"
ACTION_BOT_CREATE = "bot.create"
ACTION_BOT_CONFIG_PUBLISH = "bot.config.publish"
ACTION_BOT_CONFIG_ROLLBACK = "bot.config.rollback"
ACTION_BOT_RUNTIME_RESUME = "bot.runtime.resume"
ACTION_BOT_ENABLE = "bot.enable"
ACTION_WORKSPACE_RUNTIME_RELEASE = "workspace.runtime.release"
ACTION_WORKSPACE_FLATTEN_CONFIRM = "workspace.flatten.confirm"
ACTION_TELEGRAM_LINK_CREATE = "telegram.link.create"
ACTION_TELEGRAM_BINDING_REVOKE = "telegram.binding.revoke"
ACTION_STRATEGY_RELEASE_PUBLISH = "strategy_release.publish"
ACTION_STRATEGY_RELEASE_RETIRE = "strategy_release.retire"
ACTION_BREAKER_RESUME = "breaker.resume"
ACTION_BOT_GRANT_WRITE = "bot.grant.write"
ACTION_BOT_GRANT_REVOKE = "bot.grant.revoke"
ACTION_MEMBER_ROLE_WRITE = "member.role.write"
ACTION_SECRET_WRITE = "secret.write"

HIGH_RISK_ACTIONS = frozenset(
    {
        ACTION_RISK_PROFILE_CREATE,
        ACTION_RISK_APPETITE_SAVE,
        ACTION_SETTINGS_PUBLISH,
        ACTION_SETTINGS_ROLLBACK,
        ACTION_BOT_CREATE,
        ACTION_BOT_CONFIG_PUBLISH,
        ACTION_BOT_CONFIG_ROLLBACK,
        ACTION_BOT_RUNTIME_RESUME,
        ACTION_BOT_ENABLE,
        ACTION_WORKSPACE_RUNTIME_RELEASE,
        ACTION_WORKSPACE_FLATTEN_CONFIRM,
        ACTION_TELEGRAM_LINK_CREATE,
        ACTION_TELEGRAM_BINDING_REVOKE,
        ACTION_STRATEGY_RELEASE_PUBLISH,
        ACTION_STRATEGY_RELEASE_RETIRE,
        ACTION_BREAKER_RESUME,
        ACTION_BOT_GRANT_WRITE,
        ACTION_BOT_GRANT_REVOKE,
        ACTION_MEMBER_ROLE_WRITE,
        ACTION_SECRET_WRITE,
    }
)

_ACTION_NAME = re.compile(r"^[a-z][a-z0-9._:-]{1,79}$")
_CURRENT_STEP_UP_PROOF: ContextVar[StepUpProof | None] = ContextVar(
    "crypidian_current_step_up_proof", default=None
)


class ActionChallengeError(Exception):
    """Lỗi xác thực theo hành động (mã HTTP 403/422/503 và code ổn định, không lộ bí mật)."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        remaining_attempts: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.remaining_attempts = remaining_attempts
        super().__init__(message)

    def as_detail(self) -> dict[str, object]:
        detail: dict[str, object] = {"code": self.code, "message": self.message}
        if self.remaining_attempts is not None:
            detail["remaining_attempts"] = self.remaining_attempts
        return detail


@dataclass(frozen=True, slots=True)
class StepUpProof:
    """Bằng chứng step-up gắn challenge ID và mã TOTP hoặc token đã xác minh một lần."""

    challenge_id: UUID
    code: str | None = None
    step_up_token: str | None = None


@dataclass(frozen=True, slots=True)
class ActionChallengeCreated:
    challenge_id: UUID
    action: str
    payload_hash: str
    created_at: datetime
    expires_at: datetime
    max_attempts: int
    remaining_attempts: int
    status: str


@dataclass(frozen=True, slots=True)
class ActionChallengeVerified:
    challenge_id: UUID
    step_up_token: str
    action: str
    payload_hash: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ActionChallengeConsumed:
    challenge_id: UUID
    action: str
    payload_hash: str
    consumed_at: datetime


def _hash_digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize_json_value(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _normalize_json_value(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ActionChallengeError(
                422, "STEP_UP_PAYLOAD_INVALID", "Payload chứa số Decimal không hữu hạn."
            )
        return decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ActionChallengeError(
                422, "STEP_UP_PAYLOAD_INVALID", "Thời điểm trong payload phải có timezone."
            )
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ActionChallengeError(
                422, "STEP_UP_PAYLOAD_INVALID", "Payload chứa số thực không hữu hạn."
            )
        return value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    raise ActionChallengeError(
        422,
        "STEP_UP_PAYLOAD_INVALID",
        f"Kiểu dữ liệu không hỗ trợ trong payload step-up: {type(value).__name__}.",
    )


def canonical_action_payload(payload: Mapping[str, Any] | BaseModel) -> str:
    """Chuẩn hóa JSON để hai cấu trúc tương đương luôn sinh cùng một chuỗi hash WYSIWYS."""
    if not isinstance(payload, (Mapping, BaseModel)):
        raise ActionChallengeError(
            422, "STEP_UP_PAYLOAD_INVALID", "Payload challenge phải là đối tượng JSON."
        )
    normalized = _normalize_json_value(payload)
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_action_payload(payload: Mapping[str, Any] | BaseModel) -> str:
    """Tính SHA-256 chữ thường của canonical JSON payload (WYSIWYS)."""
    return _hash_digest(canonical_action_payload(payload))


# --- Canonical WYSIWYS payload builders cho các thao tác rủi ro cao -------------------------


def risk_profile_create_payload(
    *, name: str, max_risk_fraction: Decimal, settings: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "name": name.strip(),
        "max_risk_fraction": decimal_text(max_risk_fraction),
        "settings": dict(settings),
    }


def risk_appetite_save_payload(
    *,
    per_trade_risk_fraction: Decimal,
    account_max_open_risk_fraction: Decimal,
    max_positions: int,
    caps: Mapping[str, Decimal],
    workspace_max_open_risk_fraction: Decimal,
    max_drawdown_fraction: Decimal,
    reason: str,
) -> dict[str, Any]:
    return {
        "per_trade_risk_fraction": decimal_text(per_trade_risk_fraction),
        "account_max_open_risk_fraction": decimal_text(account_max_open_risk_fraction),
        "max_positions": max_positions,
        "caps": {key: decimal_text(value) for key, value in caps.items()},
        "workspace_max_open_risk_fraction": decimal_text(workspace_max_open_risk_fraction),
        "max_drawdown_fraction": decimal_text(max_drawdown_fraction),
        "reason": reason.strip(),
    }


def settings_publish_payload(
    *, draft_id: UUID, expected_revision: int, settings_hash: str
) -> dict[str, Any]:
    return {
        "draft_id": str(draft_id),
        "expected_revision": expected_revision,
        "settings_hash": settings_hash,
    }


def settings_rollback_payload(
    *, version: int, target_settings_hash: str, reason: str
) -> dict[str, Any]:
    return {
        "version": version,
        "target_settings_hash": target_settings_hash,
        "reason": reason.strip(),
    }


def bot_create_payload(*, name: str, max_notional: Decimal) -> dict[str, Any]:
    return {"name": name.strip(), "max_notional": decimal_text(max_notional)}


def bot_config_publish_payload(
    *, bot_id: UUID, draft_id: UUID, expected_revision: int, overlay_hash: str
) -> dict[str, Any]:
    return {
        "bot_id": str(bot_id),
        "draft_id": str(draft_id),
        "expected_revision": expected_revision,
        "overlay_hash": overlay_hash,
    }


def bot_config_rollback_payload(
    *,
    bot_id: UUID,
    version: int,
    expected_current_version_id: UUID | None,
    target_overlay_hash: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "bot_id": str(bot_id),
        "version": version,
        "expected_current_version_id": (
            str(expected_current_version_id) if expected_current_version_id is not None else None
        ),
        "target_overlay_hash": target_overlay_hash,
        "reason": reason.strip(),
    }


def bot_runtime_resume_payload(
    *, bot_id: UUID, expected_revision: int, reason: str
) -> dict[str, Any]:
    return {
        "bot_id": str(bot_id),
        "expected_revision": expected_revision,
        "reason": reason.strip(),
    }


def bot_enable_payload(*, bot_id: UUID, reason: str) -> dict[str, Any]:
    return {"bot_id": str(bot_id), "reason": reason.strip()}


def workspace_release_payload(*, expected_revision: int, reason: str) -> dict[str, Any]:
    return {"expected_revision": expected_revision, "reason": reason.strip()}


def flatten_confirm_payload(*, preview_id: UUID, preview_hash: str, reason: str) -> dict[str, Any]:
    return {
        "preview_id": str(preview_id),
        "preview_hash": preview_hash,
        "reason": reason.strip(),
    }


def telegram_link_payload(*, reason: str) -> dict[str, Any]:
    return {"reason": reason.strip()}


def telegram_revoke_payload(*, membership_id: UUID, reason: str) -> dict[str, Any]:
    return {"membership_id": str(membership_id), "reason": reason.strip()}


def strategy_release_payload(
    *, release_id: str, expected_content_hash: str | None, reason: str
) -> dict[str, Any]:
    return {
        "release_id": release_id.strip(),
        "expected_content_hash": expected_content_hash,
        "reason": reason.strip(),
    }


def breaker_resume_payload(*, bot_id: UUID | None, reason: str) -> dict[str, Any]:
    return {"bot_id": str(bot_id) if bot_id is not None else None, "reason": reason.strip()}


def bot_grant_payload(*, bot_id: UUID, membership_id: UUID, permission: str) -> dict[str, Any]:
    return {
        "bot_id": str(bot_id),
        "membership_id": str(membership_id),
        "permission": permission,
    }


def member_role_payload(
    *, membership_id: UUID, roles: list[str] | tuple[str, ...], status: str | None = None
) -> dict[str, Any]:
    return {
        "membership_id": str(membership_id),
        "roles": sorted(str(role) for role in roles),
        "status": status,
    }


def secret_write_payload(*, provider: str, reference_key: str, operation: str) -> dict[str, Any]:
    return {
        "provider": provider.strip(),
        "reference_key": reference_key.strip(),
        "operation": operation.strip(),
    }


# --- Trích xuất proof từ request / context ---------------------------------------------------


def extract_step_up_proof(
    request: Request, form_values: Mapping[str, str] | None = None
) -> StepUpProof | None:
    """Trích xuất proof step-up từ header/form; từ chối 403 nếu thiếu hoặc sai định dạng."""
    raw_id = request.headers.get(STEP_UP_CHALLENGE_HEADER)
    raw_code = request.headers.get(STEP_UP_CODE_HEADER)
    raw_token = request.headers.get(STEP_UP_TOKEN_HEADER)
    if form_values is not None:
        raw_id = raw_id or form_values.get("step_up_challenge_id")
        raw_code = raw_code or form_values.get("step_up_code")
        raw_token = raw_token or form_values.get("step_up_token")

    challenge_text = raw_id.strip() if isinstance(raw_id, str) else ""
    code_text = raw_code.strip() if isinstance(raw_code, str) else ""
    token_text = raw_token.strip() if isinstance(raw_token, str) else ""

    if not challenge_text and not code_text and not token_text:
        return None
    if not challenge_text:
        raise ActionChallengeError(403, "STEP_UP_INVALID_PROOF", "Thiếu mã challenge step-up.")
    try:
        challenge_id = UUID(challenge_text)
    except ValueError as error:
        raise ActionChallengeError(
            403, "STEP_UP_INVALID_PROOF", "Mã challenge step-up không đúng định dạng."
        ) from error
    if bool(code_text) == bool(token_text):
        raise ActionChallengeError(
            403,
            "STEP_UP_INVALID_PROOF",
            "Cần cung cấp đúng một trong hai: mã TOTP step-up hoặc step-up token.",
        )
    return StepUpProof(
        challenge_id=challenge_id,
        code=code_text or None,
        step_up_token=token_text or None,
    )


@contextmanager
def step_up_proof_scope(proof: StepUpProof | None) -> Iterator[None]:
    """Gắn proof tạm thời vào ContextVar cho một request mà không đổi chữ ký service."""
    token = _CURRENT_STEP_UP_PROOF.set(proof)
    try:
        yield
    finally:
        _CURRENT_STEP_UP_PROOF.reset(token)


@contextmanager
def http_step_up_scope(
    request: Request, form_values: Mapping[str, str] | None = None
) -> Iterator[None]:
    """Trích xuất proof từ HTTP request/form và gắn vào scope hiện tại."""
    proof = extract_step_up_proof(request, form_values=form_values)
    with step_up_proof_scope(proof):
        yield


async def verify_service_step_up(
    principal: Principal,
    *,
    action_challenge_service: ActionChallengeService | None,
    require_action_step_up: bool,
    action: str,
    payload: Mapping[str, Any] | BaseModel,
    step_up: StepUpProof | None = None,
    fallback_recent_mfa: Callable[[], None] | None = None,
) -> None:
    """Xác thực step-up dùng chung cho mọi service rủi ro cao (SEC-20).

    - Nếu request mang chứng từ step-up: luôn kiểm tra & tiêu thụ qua ActionChallengeService.
    - Nếu không mang chứng từ và `require_action_step_up=True`: từ chối 403 STEP_UP_REQUIRED.
    - Nếu `require_action_step_up=False`: giữ tương thích với cửa sổ MFA gần đây của phiên.
    """
    proof = resolve_step_up_proof(step_up)
    if proof is not None:
        if action_challenge_service is None:
            raise ActionChallengeError(
                503, "STEP_UP_SERVICE_UNAVAILABLE", "Dịch vụ xác thực step-up chưa sẵn sàng."
            )
        await action_challenge_service.consume_proof(
            principal, proof=proof, action=action, payload=payload
        )
        return
    if require_action_step_up:
        raise ActionChallengeError(
            403,
            "STEP_UP_REQUIRED",
            "Thao tác rủi ro cao yêu cầu xác thực step-up theo hành động.",
        )
    if fallback_recent_mfa is not None:
        fallback_recent_mfa()
        return
    now = datetime.now(UTC)
    if principal.mfa_verified_at > now:
        raise ActionChallengeError(
            403, "RECENT_MFA_REQUIRED", "Thời điểm xác thực MFA không hợp lệ."
        )
    if now - principal.mfa_verified_at > RECENT_MFA_LIFETIME:
        raise ActionChallengeError(403, "RECENT_MFA_REQUIRED", "Cần xác thực MFA gần đây.")


def current_step_up_proof() -> StepUpProof | None:
    return _CURRENT_STEP_UP_PROOF.get()


def resolve_step_up_proof(explicit: StepUpProof | None) -> StepUpProof | None:
    return explicit if explicit is not None else _CURRENT_STEP_UP_PROOF.get()


# --- Service chính --------------------------------------------------------------------------


class ActionChallengeService:
    """Quản lý vòng đời challenge step-up một lần gắn phiên, hành động và payload hash."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        secret_provider: SecretProvider,
        *,
        totp: TotpManager | None = None,
        clock: Callable[[], datetime] | None = None,
        unit_of_work_factory: Callable[..., Any] = UnitOfWork,
        repository_factory: Callable[[Any], ActionChallengeRepository] = (
            ActionChallengeRepository
        ),
    ) -> None:
        self._sessions = sessions
        self._secret_provider = secret_provider
        self._totp = totp or TotpManager()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._unit_of_work_factory = unit_of_work_factory
        self._repository_factory = repository_factory

    def _uow(self, principal: Principal) -> Any:
        return self._unit_of_work_factory(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
        )

    async def create_challenge(
        self,
        principal: Principal,
        *,
        action: str,
        payload: Mapping[str, Any] | BaseModel,
        ttl_seconds: int = DEFAULT_ACTION_CHALLENGE_TTL_SECONDS,
    ) -> ActionChallengeCreated:
        """Tạo challenge PENDING gắn với session, action và SHA-256 payload WYSIWYS."""
        session_id = _require_session_id(principal)
        clean_action = _validate_action_name(action)
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool):
            raise ActionChallengeError(422, "STEP_UP_TTL_INVALID", "TTL challenge không hợp lệ.")
        if ttl_seconds < 1 or ttl_seconds > MAX_ACTION_CHALLENGE_TTL_SECONDS:
            raise ActionChallengeError(
                422,
                "STEP_UP_TTL_INVALID",
                f"TTL challenge phải trong khoảng 1–{MAX_ACTION_CHALLENGE_TTL_SECONDS} giây.",
            )
        payload_hash = hash_action_payload(payload)
        now = _require_aware(self._clock())
        expires_at = now + timedelta(seconds=ttl_seconds)

        async with self._uow(principal) as unit:
            repo = self._repository_factory(unit)
            active_session = await repo.get_active_session(
                workspace_id=principal.workspace_id,
                membership_id=principal.membership_id,
                session_id=session_id,
                now=now,
            )
            if active_session is None:
                raise ActionChallengeError(
                    403, "STEP_UP_SESSION_INVALID", "Phiên đăng nhập không còn hiệu lực."
                )
            record = await repo.create(
                workspace_id=principal.workspace_id,
                membership_id=principal.membership_id,
                session_id=session_id,
                action=clean_action,
                payload_hash=payload_hash,
                created_at=now,
                expires_at=expires_at,
                max_attempts=MAX_ACTION_CHALLENGE_ATTEMPTS,
            )
            unit.record_audit(
                action="auth.action_challenge.created",
                target_type="action_challenge",
                target_id=record.id,
                reason=clean_action,
                after_state={
                    "action": clean_action,
                    "payload_hash": payload_hash,
                    "expires_at": expires_at.isoformat(),
                },
            )
        return ActionChallengeCreated(
            challenge_id=record.id,
            action=clean_action,
            payload_hash=payload_hash,
            created_at=now,
            expires_at=expires_at,
            max_attempts=MAX_ACTION_CHALLENGE_ATTEMPTS,
            remaining_attempts=MAX_ACTION_CHALLENGE_ATTEMPTS,
            status="PENDING",
        )

    async def verify_challenge(
        self,
        principal: Principal,
        *,
        challenge_id: UUID,
        code: str,
        action: str | None = None,
        payload: Mapping[str, Any] | BaseModel | None = None,
    ) -> ActionChallengeVerified:
        """Xác minh TOTP cho challenge PENDING và trả token một lần để thực thi hành động."""
        session_id = _require_session_id(principal)
        expected_action = _validate_action_name(action) if action is not None else None
        expected_hash = hash_action_payload(payload) if payload is not None else None
        now = _require_aware(self._clock())

        failure: ActionChallengeError | None = None
        verified_result: ActionChallengeVerified | None = None

        async with self._uow(principal) as unit:
            repo = self._repository_factory(unit)
            await _ensure_active_session(repo, principal, session_id, now)
            record = await repo.get_for_update(
                workspace_id=principal.workspace_id, challenge_id=challenge_id
            )
            _validate_record_binding(
                record,
                principal=principal,
                session_id=session_id,
                expected_action=expected_action,
                expected_payload_hash=expected_hash,
                now=now,
                allowed_statuses=("PENDING",),
            )
            assert record is not None
            factor = await repo.get_totp_factor_for_update(
                workspace_id=principal.workspace_id, membership_id=principal.membership_id
            )
            if factor is None:
                raise ActionChallengeError(
                    403, "STEP_UP_MFA_NOT_CONFIGURED", "Tài khoản chưa kích hoạt yếu tố TOTP."
                )
            counter = self._match_totp(principal.workspace_id, factor, code, now)
            if counter is None:
                failure = _record_failed_attempt(unit, record, now)
            else:
                factor.last_used_counter = counter
                step_up_token = secrets.token_urlsafe(32)
                record.status = "VERIFIED"
                record.verified_at = now
                record.verification_token_hash = _hash_digest(step_up_token)
                unit.record_audit(
                    action="auth.action_challenge.verified",
                    target_type="action_challenge",
                    target_id=record.id,
                    reason=record.action,
                    after_state={"action": record.action, "payload_hash": record.payload_hash},
                )
                verified_result = ActionChallengeVerified(
                    challenge_id=record.id,
                    step_up_token=step_up_token,
                    action=record.action,
                    payload_hash=record.payload_hash,
                    expires_at=record.expires_at,
                )

        if failure is not None:
            raise failure
        assert verified_result is not None
        return verified_result

    async def consume_challenge(
        self,
        principal: Principal,
        *,
        challenge_id: UUID,
        step_up_token: str,
        action: str,
        payload: Mapping[str, Any] | BaseModel,
    ) -> ActionChallengeConsumed:
        """Tiêu thụ nguyên tử challenge đã VERIFIED đúng 1 lần theo action + payload_hash."""
        session_id = _require_session_id(principal)
        clean_action = _validate_action_name(action)
        expected_hash = hash_action_payload(payload)
        now = _require_aware(self._clock())
        token_hash = _hash_digest(step_up_token) if step_up_token else ""

        failure: ActionChallengeError | None = None
        consumed_result: ActionChallengeConsumed | None = None

        async with self._uow(principal) as unit:
            repo = self._repository_factory(unit)
            await _ensure_active_session(repo, principal, session_id, now)
            record = await repo.get_for_update(
                workspace_id=principal.workspace_id, challenge_id=challenge_id
            )
            _validate_record_binding(
                record,
                principal=principal,
                session_id=session_id,
                expected_action=clean_action,
                expected_payload_hash=expected_hash,
                now=now,
                allowed_statuses=("VERIFIED",),
            )
            assert record is not None
            stored_hash = record.verification_token_hash
            if (
                not stored_hash
                or not token_hash
                or not secrets.compare_digest(stored_hash, token_hash)
            ):
                failure = _record_failed_attempt(
                    unit,
                    record,
                    now,
                    code="STEP_UP_INVALID_TOKEN",
                    message="Token xác thực step-up không hợp lệ.",
                )
            else:
                record.status = "CONSUMED"
                record.consumed_at = now
                record.verification_token_hash = None
                unit.record_audit(
                    action="auth.action_challenge.consumed",
                    target_type="action_challenge",
                    target_id=record.id,
                    reason=clean_action,
                    after_state={
                        "action": clean_action,
                        "payload_hash": expected_hash,
                        "consumed_at": now.isoformat(),
                    },
                )
                consumed_result = ActionChallengeConsumed(
                    challenge_id=record.id,
                    action=clean_action,
                    payload_hash=expected_hash,
                    consumed_at=now,
                )

        if failure is not None:
            raise failure
        assert consumed_result is not None
        return consumed_result

    async def verify_and_consume(
        self,
        principal: Principal,
        *,
        challenge_id: UUID,
        action: str,
        payload: Mapping[str, Any] | BaseModel,
        code: str,
    ) -> ActionChallengeConsumed:
        """Xác minh TOTP và tiêu thụ challenge nguyên tử trong một bước."""
        session_id = _require_session_id(principal)
        clean_action = _validate_action_name(action)
        expected_hash = hash_action_payload(payload)
        now = _require_aware(self._clock())

        failure: ActionChallengeError | None = None
        consumed_result: ActionChallengeConsumed | None = None

        async with self._uow(principal) as unit:
            repo = self._repository_factory(unit)
            await _ensure_active_session(repo, principal, session_id, now)
            record = await repo.get_for_update(
                workspace_id=principal.workspace_id, challenge_id=challenge_id
            )
            _validate_record_binding(
                record,
                principal=principal,
                session_id=session_id,
                expected_action=clean_action,
                expected_payload_hash=expected_hash,
                now=now,
                allowed_statuses=("PENDING",),
            )
            assert record is not None
            factor = await repo.get_totp_factor_for_update(
                workspace_id=principal.workspace_id, membership_id=principal.membership_id
            )
            if factor is None:
                raise ActionChallengeError(
                    403, "STEP_UP_MFA_NOT_CONFIGURED", "Tài khoản chưa kích hoạt yếu tố TOTP."
                )
            counter = self._match_totp(principal.workspace_id, factor, code, now)
            if counter is None:
                failure = _record_failed_attempt(unit, record, now)
            else:
                factor.last_used_counter = counter
                record.status = "CONSUMED"
                record.verified_at = now
                record.consumed_at = now
                record.verification_token_hash = None
                unit.record_audit(
                    action="auth.action_challenge.consumed",
                    target_type="action_challenge",
                    target_id=record.id,
                    reason=clean_action,
                    after_state={
                        "action": clean_action,
                        "payload_hash": expected_hash,
                        "consumed_at": now.isoformat(),
                    },
                )
                consumed_result = ActionChallengeConsumed(
                    challenge_id=record.id,
                    action=clean_action,
                    payload_hash=expected_hash,
                    consumed_at=now,
                )

        if failure is not None:
            raise failure
        assert consumed_result is not None
        return consumed_result

    async def consume_proof(
        self,
        principal: Principal,
        *,
        proof: StepUpProof,
        action: str,
        payload: Mapping[str, Any] | BaseModel,
    ) -> ActionChallengeConsumed:
        """Tiêu thụ proof từ HTTP request (bằng mã TOTP trực tiếp hoặc step_up_token)."""
        if proof.code is not None:
            return await self.verify_and_consume(
                principal,
                challenge_id=proof.challenge_id,
                action=action,
                payload=payload,
                code=proof.code,
            )
        if proof.step_up_token is not None:
            return await self.consume_challenge(
                principal,
                challenge_id=proof.challenge_id,
                step_up_token=proof.step_up_token,
                action=action,
                payload=payload,
            )
        raise ActionChallengeError(
            403, "STEP_UP_INVALID_PROOF", "Thiếu mã xác thực hoặc token step-up."
        )

    async def cancel_challenge(self, principal: Principal, *, challenge_id: UUID) -> None:
        """Hủy chủ động challenge đang chờ hoặc đã xác minh thuộc đúng phiên."""
        session_id = _require_session_id(principal)
        now = _require_aware(self._clock())
        async with self._uow(principal) as unit:
            repo = self._repository_factory(unit)
            await _ensure_active_session(repo, principal, session_id, now)
            record = await repo.get_for_update(
                workspace_id=principal.workspace_id, challenge_id=challenge_id
            )
            if record is None or record.membership_id != principal.membership_id:
                raise ActionChallengeError(
                    403, "STEP_UP_CHALLENGE_NOT_FOUND", "Không tìm thấy challenge step-up."
                )
            if record.session_id != session_id:
                raise ActionChallengeError(
                    403, "STEP_UP_SESSION_MISMATCH", "Challenge không thuộc phiên hiện tại."
                )
            if record.status in {"PENDING", "VERIFIED"}:
                record.status = "CANCELLED"
                record.cancelled_at = now
                record.verification_token_hash = None
                unit.record_audit(
                    action="auth.action_challenge.cancelled",
                    target_type="action_challenge",
                    target_id=record.id,
                    reason=record.action,
                    after_state={"action": record.action, "status": "CANCELLED"},
                )

    def _match_totp(self, workspace_id: UUID, factor: Any, code: str, now: datetime) -> int | None:
        cleaned = code.strip() if isinstance(code, str) else ""
        if len(cleaned) != 6 or not cleaned.isdigit():
            return None
        secret = self._secret_provider.resolve_mfa_secret(workspace_id, factor.secret_reference)
        counter = self._totp.matching_counter(secret, cleaned, now, valid_window=1)
        if counter is None:
            return None
        if factor.last_used_counter is not None and counter <= factor.last_used_counter:
            return None
        return counter


def _require_session_id(principal: Principal) -> UUID:
    if principal.session_id is None:
        raise ActionChallengeError(
            403,
            "STEP_UP_SESSION_REQUIRED",
            "Thao tác yêu cầu phiên web hợp lệ để gắn challenge step-up.",
        )
    return principal.session_id


def _validate_action_name(action: str) -> str:
    cleaned = action.strip() if isinstance(action, str) else ""
    if not _ACTION_NAME.fullmatch(cleaned):
        raise ActionChallengeError(422, "STEP_UP_ACTION_INVALID", "Tên hành động không hợp lệ.")
    return cleaned


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Đồng hồ challenge phải có timezone UTC.")
    return value


async def _ensure_active_session(
    repo: ActionChallengeRepository,
    principal: Principal,
    session_id: UUID,
    now: datetime,
) -> None:
    session = await repo.get_active_session(
        workspace_id=principal.workspace_id,
        membership_id=principal.membership_id,
        session_id=session_id,
        now=now,
    )
    if session is None:
        raise ActionChallengeError(
            403, "STEP_UP_SESSION_INVALID", "Phiên đăng nhập không còn hiệu lực."
        )


def _validate_record_binding(
    record: Any,
    *,
    principal: Principal,
    session_id: UUID,
    expected_action: str | None,
    expected_payload_hash: str | None,
    now: datetime,
    allowed_statuses: tuple[str, ...],
) -> None:
    if record is None or record.membership_id != principal.membership_id:
        raise ActionChallengeError(
            403, "STEP_UP_CHALLENGE_NOT_FOUND", "Challenge step-up không tồn tại."
        )
    if record.session_id != session_id:
        raise ActionChallengeError(
            403, "STEP_UP_SESSION_MISMATCH", "Challenge step-up không thuộc phiên hiện tại."
        )
    if expected_action is not None and record.action != expected_action:
        raise ActionChallengeError(
            403, "STEP_UP_ACTION_MISMATCH", "Challenge step-up không khớp hành động yêu cầu."
        )
    if expected_payload_hash is not None and not secrets.compare_digest(
        record.payload_hash, expected_payload_hash
    ):
        raise ActionChallengeError(
            403,
            "STEP_UP_PAYLOAD_MISMATCH",
            "Nội dung thao tác đã thay đổi so với lúc xác nhận step-up (WYSIWYS).",
        )
    if record.status == "CONSUMED" or record.consumed_at is not None:
        raise ActionChallengeError(
            403, "STEP_UP_CHALLENGE_ALREADY_USED", "Challenge step-up đã được sử dụng."
        )
    if (
        record.status == "CANCELLED"
        or record.cancelled_at is not None
        or record.failed_attempts >= record.max_attempts
    ):
        raise ActionChallengeError(
            403,
            "STEP_UP_CHALLENGE_LOCKED",
            "Challenge step-up đã bị hủy hoặc khóa; hãy tạo challenge mới.",
            remaining_attempts=0,
        )
    if record.expires_at <= now:
        raise ActionChallengeError(
            403, "STEP_UP_CHALLENGE_EXPIRED", "Challenge step-up đã hết hạn (TTL tối đa 5 phút)."
        )
    if record.status not in allowed_statuses:
        if record.status == "PENDING":
            raise ActionChallengeError(
                403,
                "STEP_UP_CHALLENGE_NOT_VERIFIED",
                "Challenge step-up chưa được xác minh mã OTP.",
            )
        raise ActionChallengeError(
            403, "STEP_UP_CHALLENGE_ALREADY_USED", "Challenge step-up đã được sử dụng."
        )


def _record_failed_attempt(
    unit: Any,
    record: Any,
    now: datetime,
    *,
    code: str = "STEP_UP_INVALID_CODE",
    message: str = "Mã xác thực step-up không hợp lệ.",
) -> ActionChallengeError:
    record.failed_attempts += 1
    remaining = max(0, record.max_attempts - record.failed_attempts)
    if record.failed_attempts >= record.max_attempts:
        record.status = "CANCELLED"
        record.cancelled_at = now
        record.verification_token_hash = None
        unit.record_audit(
            action="auth.action_challenge.locked",
            target_type="action_challenge",
            target_id=record.id,
            reason=record.action,
            after_state={
                "action": record.action,
                "failed_attempts": record.failed_attempts,
                "status": "CANCELLED",
            },
        )
        return ActionChallengeError(
            403,
            "STEP_UP_CHALLENGE_LOCKED",
            "Nhập sai quá 5 lần; challenge step-up đã bị hủy, hãy tạo challenge mới.",
            remaining_attempts=0,
        )
    unit.record_audit(
        action="auth.action_challenge.failed",
        target_type="action_challenge",
        target_id=record.id,
        reason=record.action,
        after_state={
            "action": record.action,
            "failed_attempts": record.failed_attempts,
        },
    )
    return ActionChallengeError(403, code, message, remaining_attempts=remaining)
