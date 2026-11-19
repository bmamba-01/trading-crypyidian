"""Ghi nhận sự kiện audit bảo mật chuẩn hóa, ẩn danh IP/session và xoay secret (SEC-32)."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import AuditEvent, SecretReference
from trading_bot.application.outbox import UnitOfWork
from trading_bot.auth.policy import Permission, Principal, Role, authorize
from trading_bot.domain.events import state_hash
from trading_bot.observability.redaction import redact_value

if TYPE_CHECKING:
    from trading_bot.observability.security_alerts import SecurityAnomalyDetector

logger = logging.getLogger("trading_bot.security.audit")

SECURITY_ACTION_LOGIN_SUCCEEDED = "auth.login.succeeded"
SECURITY_ACTION_LOGIN_FAILED = "auth.login.failed"
SECURITY_ACTION_REAUTH_SUCCEEDED = "auth.reauth.succeeded"
SECURITY_ACTION_REAUTH_FAILED = "auth.reauth.failed"
SECURITY_ACTION_STEP_UP_SUCCEEDED = "auth.step_up.succeeded"
SECURITY_ACTION_STEP_UP_FAILED = "auth.step_up.failed"
SECURITY_ACTION_STEP_UP_LOCKED = "auth.step_up.locked"
SECURITY_ACTION_CSRF_FAILED = "auth.csrf.failed"
SECURITY_ACTION_AUTHZ_DENIED = "auth.authorization.denied"
SECURITY_ACTION_RISK_PROFILE_CREATED = "risk_profile.created"
SECURITY_ACTION_SECRET_ROTATED = "secret.rotated"

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"
OUTCOME_DENIED = "denied"
OUTCOME_LOCKED = "locked"

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True, slots=True)
class SecurityAuditContext:
    """Ngữ cảnh request hiện hành phục vụ điền metadata bảo mật vào AuditEvent."""

    client_ip: str | None = None
    user_agent: str | None = None
    session_hash: str | None = None
    request_id: UUID | None = None


_CURRENT_SECURITY_AUDIT_CONTEXT: ContextVar[SecurityAuditContext] = ContextVar(
    "crypidian_security_audit_context",
    default=SecurityAuditContext(),  # noqa: B039
)


def current_security_audit_context() -> SecurityAuditContext:
    """Đọc ngữ cảnh audit bảo mật của request hiện tại."""
    return _CURRENT_SECURITY_AUDIT_CONTEXT.get()


def mask_client_ip(raw_ip: str | None) -> str | None:
    """Ẩn danh địa chỉ IP client về prefix mạng (/24 cho IPv4, /48 cho IPv6) theo SEC-32."""
    if raw_ip is None:
        return None
    candidate = raw_ip.strip()
    if not candidate:
        return None
    if "," in candidate:
        candidate = candidate.split(",", 1)[0].strip()
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        host_part, port_part = candidate.rsplit(":", 1)
        if port_part.isdigit():
            candidate = host_part

    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        try:
            net = ipaddress.ip_network(candidate, strict=False)
            addr = net.network_address
        except ValueError:
            return None

    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped

    if isinstance(addr, ipaddress.IPv4Address):
        network4 = ipaddress.IPv4Network(f"{addr}/24", strict=False)
        return str(network4)
    network6 = ipaddress.IPv6Network(f"{addr}/48", strict=False)
    return str(network6)


def sanitize_user_agent(raw_ua: str | None, *, max_length: int = 160) -> str | None:
    """Làm sạch ký tự điều khiển, redact bí mật và cắt ngắn User-Agent tối đa 160 ký tự."""
    if raw_ua is None:
        return None
    cleaned = _CONTROL_CHARS_RE.sub(" ", raw_ua).strip()
    if not cleaned:
        return None
    redacted = str(redact_value(cleaned)).strip()
    if not redacted:
        return None
    return redacted[:max_length]


def hash_session_identifier(cookie_or_token: str | None) -> str | None:
    """Băm SHA-256 một chiều giá trị cookie/session để truy vết mà không bao giờ lưu token thô."""
    if cookie_or_token is None:
        return None
    normalized = cookie_or_token.strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def sanitize_audit_reason(reason: str | None, *, max_length: int = 500) -> str | None:
    """Làm sạch lý do audit qua bộ lọc redaction để chặn rò rỉ token/password/canary."""
    if reason is None:
        return None
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(reason)).strip()
    if not cleaned:
        return None
    redacted = str(redact_value(cleaned)).strip()
    return redacted[:max_length]


def extract_client_ip_from_request(request: Request) -> str | None:
    """Trích xuất và mask IP client từ FastAPI Request."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and forwarded.strip():
        return mask_client_ip(forwarded)
    if request.client and request.client.host:
        return mask_client_ip(request.client.host)
    return None


def extract_request_id(request: Request) -> UUID:
    """Đọc hoặc sinh mới request_id dạng UUID gắn trên request.state."""
    existing = getattr(request.state, "security_request_id", None)
    if isinstance(existing, UUID):
        return existing
    header_val = request.headers.get("x-request-id")
    if header_val:
        try:
            parsed = UUID(header_val.strip())
            request.state.security_request_id = parsed
            return parsed
        except ValueError:
            pass
    generated = uuid4()
    request.state.security_request_id = generated
    return generated


@contextmanager
def security_audit_scope(
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
    session_cookie: str | None = None,
    session_hash: str | None = None,
    request_id: UUID | None = None,
) -> Iterator[SecurityAuditContext]:
    """Thiết lập ngữ cảnh bảo mật tạm thời cho luồng xử lý hiện tại."""
    previous = _CURRENT_SECURITY_AUDIT_CONTEXT.get()
    resolved_hash = (
        session_hash
        if session_hash is not None
        else (
            hash_session_identifier(session_cookie)
            if session_cookie is not None
            else previous.session_hash
        )
    )
    ctx = SecurityAuditContext(
        client_ip=mask_client_ip(client_ip) if client_ip is not None else previous.client_ip,
        user_agent=(
            sanitize_user_agent(user_agent) if user_agent is not None else previous.user_agent
        ),
        session_hash=resolved_hash,
        request_id=request_id or previous.request_id,
    )
    token = _CURRENT_SECURITY_AUDIT_CONTEXT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT_SECURITY_AUDIT_CONTEXT.reset(token)


@contextmanager
def request_security_audit_scope(
    request: Request,
    *,
    session_cookie: str | None = None,
) -> Iterator[SecurityAuditContext]:
    """Thiết lập ngữ cảnh audit bảo mật trực tiếp từ FastAPI Request."""
    with security_audit_scope(
        client_ip=extract_client_ip_from_request(request),
        user_agent=request.headers.get("user-agent"),
        session_cookie=session_cookie,
        request_id=extract_request_id(request),
    ) as ctx:
        yield ctx


def build_security_audit_event(
    *,
    workspace_id: UUID,
    actor_membership_id: UUID | None,
    action: str,
    target_type: str,
    target_id: UUID | None = None,
    reason: str | None = None,
    outcome: str = OUTCOME_SUCCESS,
    before_state: object | None = None,
    after_state: object | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    request_id: UUID | None = None,
    session_hash: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    occurred_at: datetime | None = None,
) -> AuditEvent:
    """Khởi tạo thực thể AuditEvent chuẩn hóa với đầy đủ trường ngữ cảnh SEC-32 đã làm sạch."""
    ctx = current_security_audit_context()
    resolved_before = (
        before_hash
        if before_hash is not None
        else (state_hash(before_state) if before_state is not None else None)
    )
    resolved_after = (
        after_hash
        if after_hash is not None
        else (state_hash(after_state) if after_state is not None else None)
    )
    return AuditEvent(
        workspace_id=workspace_id,
        actor_membership_id=actor_membership_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=sanitize_audit_reason(reason),
        before_hash=resolved_before,
        after_hash=resolved_after,
        request_id=request_id or ctx.request_id or uuid4(),
        occurred_at=occurred_at or datetime.now(UTC),
        session_hash=session_hash if session_hash is not None else ctx.session_hash,
        client_ip=mask_client_ip(client_ip) if client_ip is not None else ctx.client_ip,
        user_agent=(sanitize_user_agent(user_agent) if user_agent is not None else ctx.user_agent),
        outcome=outcome,
    )


async def record_security_audit_event(
    *,
    workspace_id: UUID | None,
    actor_membership_id: UUID | None = None,
    action: str,
    target_type: str,
    target_id: UUID | None = None,
    reason: str | None = None,
    outcome: str = OUTCOME_SUCCESS,
    before_state: object | None = None,
    after_state: object | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    request_id: UUID | None = None,
    session_hash: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    uow: UnitOfWork | None = None,
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> AuditEvent | None:
    """Ghi sự kiện audit bảo mật chuẩn hóa vào DB (qua UoW hiện có hoặc giao dịch riêng)."""
    ctx = current_security_audit_context()
    effective_request_id = request_id or ctx.request_id or uuid4()

    if workspace_id is None:
        logger.warning(
            "SECURITY_AUDIT_NO_WORKSPACE action=%s target_type=%s outcome=%s ip=%s reason=%s",
            action,
            target_type,
            outcome,
            mask_client_ip(client_ip) if client_ip is not None else ctx.client_ip,
            sanitize_audit_reason(reason),
        )
        return None

    if uow is not None:
        return uow.record_audit(
            action=action,
            target_type=target_type,
            target_id=target_id,
            reason=reason,
            before_state=before_state,
            after_state=after_state,
            before_hash=before_hash,
            after_hash=after_hash,
            session_hash=session_hash,
            client_ip=client_ip,
            user_agent=user_agent,
            outcome=outcome,
        )

    if not callable(sessions):
        return None

    try:
        async with UnitOfWork(
            sessions,
            workspace_id=workspace_id,
            actor_membership_id=actor_membership_id,
            request_id=effective_request_id,
        ) as unit:
            event = unit.record_audit(
                action=action,
                target_type=target_type,
                target_id=target_id,
                reason=reason,
                before_state=before_state,
                after_state=after_state,
                before_hash=before_hash,
                after_hash=after_hash,
                session_hash=session_hash,
                client_ip=client_ip,
                user_agent=user_agent,
                outcome=outcome,
            )
            await unit.commit()
            return event
    except Exception:
        logger.warning(
            "Không thể ghi audit bảo mật độc lập cho action=%s workspace=%s",
            action,
            workspace_id,
            exc_info=True,
        )
        return None


@dataclass(frozen=True, slots=True)
class SecretRotationResult:
    """Kết quả xoay tham chiếu bí mật không chứa giá trị bí mật thô."""

    secret_reference_id: UUID
    workspace_id: UUID
    provider: str
    previous_reference_key: str
    new_reference_key: str
    status: str
    rotated_at: datetime


class SecretRotationService:
    """Dịch vụ xoay tham chiếu khóa bí mật có ghi audit bất biến và phát cảnh báo SEC-32/34."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        anomaly_detector: SecurityAnomalyDetector | None = None,
    ) -> None:
        self._sessions = sessions
        self._anomaly_detector = anomaly_detector

    async def rotate_secret(
        self,
        principal: Principal,
        *,
        secret_reference_id: UUID,
        new_reference_key: str,
        reason: str,
        new_provider: str | None = None,
        request_id: UUID | None = None,
    ) -> SecretRotationResult:
        """Cập nhật tham chiếu secret sang khóa mới, ghi audit `secret.rotated` và báo động."""
        if Role.OWNER not in principal.roles and not authorize(principal, Permission.SECRET_WRITE):
            raise PermissionError("Chỉ owner hoặc quyền secret:write mới được xoay secret")

        cleaned_key = new_reference_key.strip()
        if not cleaned_key or len(cleaned_key) > 240:
            raise ValueError("new_reference_key không hợp lệ")
        cleaned_reason = reason.strip()
        if len(cleaned_reason) < 3:
            raise ValueError("Lý do xoay secret phải từ 3 ký tự trở lên")

        effective_request_id = request_id or current_security_audit_context().request_id or uuid4()
        now = datetime.now(UTC)

        async with UnitOfWork(
            self._sessions,
            workspace_id=principal.workspace_id,
            actor_membership_id=principal.membership_id,
            request_id=effective_request_id,
        ) as uow:
            secret_ref = await uow.session.scalar(
                select(SecretReference).where(
                    SecretReference.workspace_id == principal.workspace_id,
                    SecretReference.id == secret_reference_id,
                )
            )
            if secret_ref is None:
                raise LookupError("Không tìm thấy tham chiếu secret trong workspace")

            previous_key = secret_ref.reference_key
            previous_provider = secret_ref.provider
            if previous_key == cleaned_key and (
                new_provider is None or new_provider.strip() == previous_provider
            ):
                raise ValueError("Tham chiếu secret mới phải khác tham chiếu hiện hành")

            before_snapshot = {
                "id": str(secret_ref.id),
                "provider": previous_provider,
                "reference_key": previous_key,
                "status": secret_ref.status,
            }
            if new_provider is not None and new_provider.strip():
                secret_ref.provider = new_provider.strip()
            secret_ref.reference_key = cleaned_key
            secret_ref.status = "ACTIVE"
            after_snapshot = {
                "id": str(secret_ref.id),
                "provider": secret_ref.provider,
                "reference_key": secret_ref.reference_key,
                "status": secret_ref.status,
            }

            await record_security_audit_event(
                uow=uow,
                workspace_id=principal.workspace_id,
                actor_membership_id=principal.membership_id,
                action=SECURITY_ACTION_SECRET_ROTATED,
                target_type="secret_reference",
                target_id=secret_ref.id,
                reason=cleaned_reason,
                outcome=OUTCOME_SUCCESS,
                before_state=before_snapshot,
                after_state=after_snapshot,
                request_id=effective_request_id,
            )
            if self._anomaly_detector is not None:
                await self._anomaly_detector.observe_privileged_change(
                    workspace_id=principal.workspace_id,
                    actor_membership_id=principal.membership_id,
                    action=SECURITY_ACTION_SECRET_ROTATED,
                    target_type="secret_reference",
                    target_id=secret_ref.id,
                    reason=cleaned_reason,
                    metadata={
                        "provider": secret_ref.provider,
                        "previous_ref_hash": state_hash(before_snapshot)[:16],
                        "new_ref_hash": state_hash(after_snapshot)[:16],
                    },
                    request_id=effective_request_id,
                    uow=uow,
                )
            await uow.session.commit()

        return SecretRotationResult(
            secret_reference_id=secret_reference_id,
            workspace_id=principal.workspace_id,
            provider=new_provider.strip()
            if new_provider and new_provider.strip()
            else previous_provider,
            previous_reference_key=previous_key,
            new_reference_key=cleaned_key,
            status="ACTIVE",
            rotated_at=now,
        )
