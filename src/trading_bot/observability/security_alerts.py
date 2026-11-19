"""Phát hiện bất thường bảo mật và phát cảnh báo có cấu trúc đã redact (SEC-34)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.events import SENSITIVE_PAYLOAD_KEYS, EventEnvelope
from trading_bot.observability.redaction import REDACTED_TOKEN, redact_value

if TYPE_CHECKING:
    from trading_bot.observability.audit_chain import AuditChainVerificationResult

logger = logging.getLogger("trading_bot.security.alerts")

_SENSITIVE_KEY_SUFFIXES = (
    "apikey",
    "apisecret",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)
_EXCHANGE_CRITICAL_ERROR_CODES = frozenset({-2015, -2014, -1022})
_EXCHANGE_ALERT_STATUS_CODES = frozenset({401, 403, 418, 429})
_CRITICAL_EMERGENCY_KEYWORDS = ("kill", "flatten", "circuit_breaker")


class SecurityAlertSeverity(StrEnum):
    """Mức độ nghiêm trọng của cảnh báo bảo mật (SEC-34)."""

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class SecurityAlertType(StrEnum):
    """Phân loại sự kiện cảnh báo bảo mật chuẩn hóa (SEC-34)."""

    AUTH_FAILURE_SPIKE = "security.alert.auth_failure_spike"
    STEP_UP_FAILURE_SPIKE = "security.alert.step_up_failure_spike"
    EMERGENCY_ACTION = "security.alert.emergency_action"
    PRIVILEGED_CHANGE = "security.alert.privileged_change"
    FOREIGN_ACTIVITY = "security.alert.foreign_activity"
    CLOCK_SKEW = "security.alert.clock_skew"
    EXCHANGE_AUTH_OR_BAN = "security.alert.exchange_auth_or_ban"
    AUDIT_CHAIN_BROKEN = "security.alert.audit_chain_broken"
    CSRF_OR_AUTHZ_PROBING = "security.alert.csrf_or_authz_probing"


@dataclass(frozen=True, slots=True)
class SecurityAlertPolicy:
    """Ngưỡng và cửa sổ thời gian phát hiện bất thường bảo mật."""

    auth_failure_threshold: int = 5
    auth_failure_window: timedelta = timedelta(minutes=5)
    step_up_failure_threshold: int = 3
    step_up_failure_window: timedelta = timedelta(minutes=10)
    probing_failure_threshold: int = 8
    probing_failure_window: timedelta = timedelta(minutes=5)
    clock_skew_warning_ms: float = 500.0
    clock_skew_critical_ms: float = 1500.0
    alert_cooldown: timedelta = timedelta(seconds=30)


def _sanitize_payload_key(key: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    if normalized in SENSITIVE_PAYLOAD_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES):
        return f"{key}_redacted_ref"
    return key


def _sanitize_alert_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, nested in value.items():
            safe_key = _sanitize_payload_key(str(raw_key))
            normalized = re.sub(r"[^a-z0-9]", "", str(raw_key).casefold())
            if normalized in SENSITIVE_PAYLOAD_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES):
                cleaned[safe_key] = REDACTED_TOKEN
            else:
                cleaned[safe_key] = _sanitize_alert_metadata(nested)
        return cleaned
    if isinstance(value, list | tuple):
        return [_sanitize_alert_metadata(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return redact_value(value)


def _actor_key_digest(actor_key: str) -> str:
    return hashlib.sha256(actor_key.strip().casefold().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class SecurityAlert:
    """Bản ghi cảnh báo bảo mật bất biến đã redact dữ liệu nhạy cảm."""

    alert_type: SecurityAlertType
    severity: SecurityAlertSeverity
    workspace_id: UUID | None
    actor_membership_id: UUID | None
    title: str
    summary: str
    detected_at: datetime
    request_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    alert_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", str(redact_value(self.title)))
        object.__setattr__(self, "summary", str(redact_value(self.summary)))
        object.__setattr__(self, "metadata", _sanitize_alert_metadata(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": str(self.alert_id),
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "workspace_id": str(self.workspace_id) if self.workspace_id is not None else None,
            "actor_membership_id": (
                str(self.actor_membership_id) if self.actor_membership_id is not None else None
            ),
            "title": self.title,
            "summary": self.summary,
            "detected_at": self.detected_at.isoformat(),
            "request_id": str(self.request_id) if self.request_id is not None else None,
            "metadata": dict(self.metadata),
        }

    def to_event_envelope(self) -> EventEnvelope:
        """Chuyển cảnh báo bảo mật thành EventEnvelope để đẩy qua transactional outbox."""
        if self.workspace_id is None:
            raise ValueError("Cần workspace_id để đóng gói EventEnvelope vào outbox")
        return EventEnvelope(
            event_type=self.alert_type.value,
            workspace_id=self.workspace_id,
            aggregate_type="security_alert",
            aggregate_id=self.alert_id,
            occurred_at=self.detected_at,
            correlation_id=self.request_id or self.alert_id,
            idempotency_key=f"security_alert:{self.alert_id}",
            schema_version=1,
            payload={
                "alert_id": str(self.alert_id),
                "severity": self.severity.value,
                "actor_membership_id": (
                    str(self.actor_membership_id) if self.actor_membership_id is not None else None
                ),
                "title": self.title,
                "summary": self.summary,
                "metadata": dict(self.metadata),
            },
        )


class SecurityAlertSink(Protocol):
    """Giao diện đầu ra nhận cảnh báo bảo mật (log, outbox, bộ nhớ kiểm thử, notifier)."""

    async def emit(self, alert: SecurityAlert, *, uow: UnitOfWork | None = None) -> None: ...


class InMemorySecurityAlertSink:
    """Sink lưu cảnh báo trong bộ nhớ phục vụ kiểm thử và truy vấn nhanh."""

    def __init__(self, max_items: int = 500) -> None:
        self._max_items = max(1, max_items)
        self.alerts: list[SecurityAlert] = []

    async def emit(self, alert: SecurityAlert, *, uow: UnitOfWork | None = None) -> None:
        del uow
        self.alerts.append(alert)
        if len(self.alerts) > self._max_items:
            del self.alerts[: len(self.alerts) - self._max_items]


class LoggingSecurityAlertSink:
    """Sink ghi cảnh báo bảo mật có cấu trúc (JSON) ra logger đã qua lớp redact."""

    def __init__(self, target_logger: logging.Logger | None = None) -> None:
        self._logger = target_logger or logger

    async def emit(self, alert: SecurityAlert, *, uow: UnitOfWork | None = None) -> None:
        del uow
        structured = json.dumps(alert.to_dict(), ensure_ascii=False, sort_keys=True)
        level = (
            logging.CRITICAL
            if alert.severity is SecurityAlertSeverity.CRITICAL
            else logging.WARNING
        )
        self._logger.log(level, "SECURITY_ALERT %s", structured)


class OutboxSecurityAlertSink:
    """Sink đẩy cảnh báo bảo mật vào bảng outbox_event để phát qua kênh vận hành."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._sessions = sessions

    async def emit(self, alert: SecurityAlert, *, uow: UnitOfWork | None = None) -> None:
        if alert.workspace_id is None:
            return
        envelope = alert.to_event_envelope()
        if uow is not None and uow.workspace_id == alert.workspace_id:
            uow.enqueue(envelope)
            return
        if not callable(self._sessions):
            return
        try:
            async with UnitOfWork(
                self._sessions,
                workspace_id=alert.workspace_id,
                actor_membership_id=alert.actor_membership_id,
                request_id=alert.request_id or alert.alert_id,
            ) as unit:
                unit.enqueue(envelope)
        except Exception:
            logger.warning(
                "Không thể ghi cảnh báo bảo mật vào outbox cho workspace %s",
                alert.workspace_id,
                exc_info=True,
            )


class CompositeSecurityAlertSink:
    """Sink kết hợp nhiều kênh phát cảnh báo bảo mật độc lập."""

    def __init__(self, sinks: Sequence[SecurityAlertSink]) -> None:
        self._sinks = tuple(sinks)

    async def emit(self, alert: SecurityAlert, *, uow: UnitOfWork | None = None) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(alert, uow=uow)
            except Exception:
                logger.warning("Lỗi khi phát cảnh báo qua sink %r", sink, exc_info=True)


class SecurityAnomalyDetector:
    """Bộ phát hiện bất thường bảo mật theo cửa sổ trượt và sự kiện trọng yếu (SEC-34)."""

    def __init__(
        self,
        *,
        policy: SecurityAlertPolicy | None = None,
        sink: SecurityAlertSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy or SecurityAlertPolicy()
        self._sink: SecurityAlertSink = sink or LoggingSecurityAlertSink()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._auth_failures: dict[str, deque[datetime]] = {}
        self._step_up_failures: dict[str, deque[datetime]] = {}
        self._probing_failures: dict[str, deque[datetime]] = {}
        self._last_emitted_at: dict[str, datetime] = {}

    @property
    def policy(self) -> SecurityAlertPolicy:
        return self._policy

    def _record_sliding_event(
        self,
        buckets: dict[str, deque[datetime]],
        bucket_key: str,
        window: timedelta,
        now: datetime,
    ) -> int:
        cutoff = now - window
        queue = buckets.get(bucket_key)
        if queue is None:
            queue = deque()
            buckets[bucket_key] = queue
        while queue and queue[0] <= cutoff:
            queue.popleft()
        queue.append(now)
        return len(queue)

    def _in_cooldown(self, dedup_key: str, now: datetime) -> bool:
        if self._policy.alert_cooldown <= timedelta(0):
            self._last_emitted_at[dedup_key] = now
            return False
        previous = self._last_emitted_at.get(dedup_key)
        if previous is not None and (now - previous) < self._policy.alert_cooldown:
            return True
        self._last_emitted_at[dedup_key] = now
        return False

    async def _emit(
        self,
        alert: SecurityAlert,
        *,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert:
        await self._sink.emit(alert, uow=uow)
        return alert

    async def observe_auth_failure(
        self,
        *,
        workspace_id: UUID | None,
        actor_key: str,
        reason: str,
        client_ip: str | None = None,
        action: str = "auth.login.failed",
        actor_membership_id: UUID | None = None,
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert | None:
        """Theo dõi chuỗi đăng nhập/xác thực lại thất bại vượt ngưỡng trong cửa sổ trượt."""
        from trading_bot.observability.security_audit import mask_client_ip

        now = self._clock()
        digest = _actor_key_digest(actor_key or (client_ip or "anonymous"))
        masked_ip = mask_client_ip(client_ip)
        ws_key = str(workspace_id) if workspace_id is not None else "global"
        bucket_key = f"{ws_key}:{digest}:{masked_ip or '-'}"
        count = self._record_sliding_event(
            self._auth_failures,
            bucket_key,
            self._policy.auth_failure_window,
            now,
        )
        if count < self._policy.auth_failure_threshold:
            return None
        if self._in_cooldown(f"auth_spike:{bucket_key}", now):
            return None

        alert = SecurityAlert(
            alert_type=SecurityAlertType.AUTH_FAILURE_SPIKE,
            severity=SecurityAlertSeverity.HIGH,
            workspace_id=workspace_id,
            actor_membership_id=actor_membership_id,
            title="Phát hiện chuỗi xác thực thất bại vượt ngưỡng",
            summary=(
                f"Ghi nhận {count} lần thất bại ({action}) trong cửa sổ "
                f"{int(self._policy.auth_failure_window.total_seconds())}s"
            ),
            detected_at=now,
            request_id=request_id,
            metadata={
                "action": action,
                "failure_count": count,
                "threshold": self._policy.auth_failure_threshold,
                "window_seconds": int(self._policy.auth_failure_window.total_seconds()),
                "actor_digest": digest,
                "client_ip": masked_ip,
                "reason": str(redact_value(reason)),
            },
        )
        return await self._emit(alert, uow=uow)

    async def observe_step_up_failure(
        self,
        *,
        workspace_id: UUID,
        actor_membership_id: UUID | None,
        action: str,
        failure_count: int | None = None,
        locked: bool = False,
        reason: str = "",
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert | None:
        """Phát cảnh báo khi xác thực bước 2 (step-up challenge) thất bại liên tiếp hoặc bị khóa."""
        now = self._clock()
        actor_part = str(actor_membership_id) if actor_membership_id is not None else "unknown"
        bucket_key = f"{workspace_id}:{actor_part}:{action}"
        sliding_count = self._record_sliding_event(
            self._step_up_failures,
            bucket_key,
            self._policy.step_up_failure_window,
            now,
        )
        effective_count = max(sliding_count, failure_count or 0)
        if not locked and effective_count < self._policy.step_up_failure_threshold:
            return None

        severity = SecurityAlertSeverity.CRITICAL if locked else SecurityAlertSeverity.HIGH
        alert = SecurityAlert(
            alert_type=SecurityAlertType.STEP_UP_FAILURE_SPIKE,
            severity=severity,
            workspace_id=workspace_id,
            actor_membership_id=actor_membership_id,
            title=(
                "Challenge step-up bị khóa do vượt số lần thử"
                if locked
                else "Chuỗi xác thực step-up thất bại vượt ngưỡng"
            ),
            summary=(
                f"Hành động {action} thất bại step-up {effective_count} lần (locked={locked})"
            ),
            detected_at=now,
            request_id=request_id,
            metadata={
                "action": action,
                "failure_count": effective_count,
                "locked": locked,
                "threshold": self._policy.step_up_failure_threshold,
                "reason": str(redact_value(reason)),
            },
        )
        return await self._emit(alert, uow=uow)

    async def observe_emergency_action(
        self,
        *,
        workspace_id: UUID,
        actor_membership_id: UUID | None,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        reason: str | None = None,
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert:
        """Phát cảnh báo tức thì cho các thao tác khẩn cấp: kill, flatten, resume, publish risk."""
        now = self._clock()
        lowered = action.casefold()
        is_critical = any(keyword in lowered for keyword in _CRITICAL_EMERGENCY_KEYWORDS)
        severity = SecurityAlertSeverity.CRITICAL if is_critical else SecurityAlertSeverity.HIGH
        alert = SecurityAlert(
            alert_type=SecurityAlertType.EMERGENCY_ACTION,
            severity=severity,
            workspace_id=workspace_id,
            actor_membership_id=actor_membership_id,
            title=f"Kích hoạt thao tác khẩn cấp/trọng yếu: {action}",
            summary=(
                f"Thực thi {action} trên {target_type}"
                + (f" ({target_id})" if target_id is not None else "")
            ),
            detected_at=now,
            request_id=request_id,
            metadata={
                "action": action,
                "target_type": target_type,
                "target_id": str(target_id) if target_id is not None else None,
                "reason": str(redact_value(reason)) if reason is not None else None,
            },
        )
        return await self._emit(alert, uow=uow)

    async def observe_privileged_change(
        self,
        *,
        workspace_id: UUID,
        actor_membership_id: UUID | None,
        action: str,
        target_type: str,
        target_id: UUID | None = None,
        reason: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert:
        """Phát cảnh báo khi xoay khóa bí mật, cấp quyền owner hoặc tạo hồ sơ rủi ro mới."""
        now = self._clock()
        extra = dict(metadata) if metadata is not None else {}
        extra.update(
            {
                "action": action,
                "target_type": target_type,
                "target_id": str(target_id) if target_id is not None else None,
                "reason": str(redact_value(reason)) if reason is not None else None,
            }
        )
        alert = SecurityAlert(
            alert_type=SecurityAlertType.PRIVILEGED_CHANGE,
            severity=SecurityAlertSeverity.HIGH,
            workspace_id=workspace_id,
            actor_membership_id=actor_membership_id,
            title=f"Thay đổi đặc quyền bảo mật: {action}",
            summary=f"Thao tác đặc quyền {action} trên {target_type}",
            detected_at=now,
            request_id=request_id,
            metadata=extra,
        )
        return await self._emit(alert, uow=uow)

    async def observe_foreign_activity(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None = None,
        symbol: str | None = None,
        detail: str,
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert:
        """Phát cảnh báo CRITICAL khi phát hiện giao dịch/vị thế lạ ngoài bot trên sàn."""
        now = self._clock()
        alert = SecurityAlert(
            alert_type=SecurityAlertType.FOREIGN_ACTIVITY,
            severity=SecurityAlertSeverity.CRITICAL,
            workspace_id=workspace_id,
            actor_membership_id=None,
            title="Phát hiện hoạt động giao dịch lạ (foreign activity) trên tài khoản sàn",
            summary=str(redact_value(detail)),
            detected_at=now,
            request_id=request_id,
            metadata={
                "bot_id": str(bot_id) if bot_id is not None else None,
                "symbol": symbol,
                "detail": str(redact_value(detail)),
            },
        )
        return await self._emit(alert, uow=uow)

    async def observe_clock_skew(
        self,
        *,
        workspace_id: UUID | None = None,
        offset_ms: float,
        source: str = "binance_server_time",
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert | None:
        """Kiểm tra độ lệch đồng hồ so với máy chủ sàn và cảnh báo khi vượt ngưỡng."""
        abs_offset = abs(float(offset_ms))
        if abs_offset < self._policy.clock_skew_warning_ms:
            return None
        now = self._clock()
        severity = (
            SecurityAlertSeverity.CRITICAL
            if abs_offset >= self._policy.clock_skew_critical_ms
            else SecurityAlertSeverity.WARNING
        )
        alert = SecurityAlert(
            alert_type=SecurityAlertType.CLOCK_SKEW,
            severity=severity,
            workspace_id=workspace_id,
            actor_membership_id=None,
            title="Độ lệch đồng hồ hệ thống vượt ngưỡng an toàn",
            summary=f"Lệch đồng hồ {offset_ms:.1f}ms so với {source}",
            detected_at=now,
            request_id=request_id,
            metadata={
                "offset_ms": round(float(offset_ms), 3),
                "abs_offset_ms": round(abs_offset, 3),
                "warning_threshold_ms": self._policy.clock_skew_warning_ms,
                "critical_threshold_ms": self._policy.clock_skew_critical_ms,
                "source": source,
            },
        )
        return await self._emit(alert, uow=uow)

    async def observe_exchange_auth_or_ban(
        self,
        *,
        workspace_id: UUID | None = None,
        status_code: int | None = None,
        error_code: int | None = None,
        detail: str = "",
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert | None:
        """Phát cảnh báo khi sàn trả lỗi xác thực khóa (-2015/-2014) hoặc khóa IP (418/429)."""
        is_critical_code = error_code in _EXCHANGE_CRITICAL_ERROR_CODES or status_code == 418
        is_alert_status = status_code in _EXCHANGE_ALERT_STATUS_CODES
        if not is_critical_code and not is_alert_status:
            return None
        now = self._clock()
        severity = (
            SecurityAlertSeverity.CRITICAL if is_critical_code else SecurityAlertSeverity.HIGH
        )
        alert = SecurityAlert(
            alert_type=SecurityAlertType.EXCHANGE_AUTH_OR_BAN,
            severity=severity,
            workspace_id=workspace_id,
            actor_membership_id=None,
            title="Lỗi xác thực hoặc giới hạn/chặn IP từ sàn giao dịch",
            summary=(
                f"Sàn trả status={status_code}, error_code={error_code}: {redact_value(detail)}"
            ),
            detected_at=now,
            request_id=request_id,
            metadata={
                "status_code": status_code,
                "error_code": error_code,
                "detail": str(redact_value(detail)),
            },
        )
        return await self._emit(alert, uow=uow)

    async def observe_probing_failure(
        self,
        *,
        workspace_id: UUID | None,
        actor_membership_id: UUID | None,
        action: str,
        client_ip: str | None = None,
        reason: str = "",
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert | None:
        """Theo dõi chuỗi lỗi CSRF hoặc từ chối phân quyền 403 lặp lại bất thường."""
        from trading_bot.observability.security_audit import mask_client_ip

        now = self._clock()
        masked_ip = mask_client_ip(client_ip)
        actor_key = (
            str(actor_membership_id) if actor_membership_id is not None else (masked_ip or "anon")
        )
        bucket_key = f"{workspace_id or 'global'}:{actor_key}:{action}"
        count = self._record_sliding_event(
            self._probing_failures,
            bucket_key,
            self._policy.probing_failure_window,
            now,
        )
        if count < self._policy.probing_failure_threshold:
            return None
        if self._in_cooldown(f"probing:{bucket_key}", now):
            return None

        alert = SecurityAlert(
            alert_type=SecurityAlertType.CSRF_OR_AUTHZ_PROBING,
            severity=SecurityAlertSeverity.WARNING,
            workspace_id=workspace_id,
            actor_membership_id=actor_membership_id,
            title="Phát hiện chuỗi lỗi CSRF/phân quyền 403 bất thường",
            summary=f"Ghi nhận {count} lần từ chối ({action}) trong cửa sổ giám sát",
            detected_at=now,
            request_id=request_id,
            metadata={
                "action": action,
                "failure_count": count,
                "client_ip": masked_ip,
                "reason": str(redact_value(reason)),
            },
        )
        return await self._emit(alert, uow=uow)

    async def report_audit_chain_broken(
        self,
        verification: AuditChainVerificationResult,
        *,
        actor_membership_id: UUID | None = None,
        request_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> SecurityAlert:
        """Phát cảnh báo CRITICAL khi phát hiện chuỗi băm audit_event bị giả mạo hoặc đứt quãng."""
        now = self._clock()
        first_violation = verification.violations[0] if verification.violations else None
        alert = SecurityAlert(
            alert_type=SecurityAlertType.AUDIT_CHAIN_BROKEN,
            severity=SecurityAlertSeverity.CRITICAL,
            workspace_id=verification.workspace_id,
            actor_membership_id=actor_membership_id,
            title="Chuỗi băm nhật ký audit bất biến bị đứt hoặc giả mạo (AUDIT_CHAIN_BROKEN)",
            summary=(
                f"Phát hiện {len(verification.violations)} vi phạm toàn vẹn trên "
                f"{verification.checked_count} dòng audit của workspace {verification.workspace_id}"
            ),
            detected_at=now,
            request_id=request_id,
            metadata={
                "checked_count": verification.checked_count,
                "violation_count": len(verification.violations),
                "first_violation_code": (
                    first_violation.code if first_violation is not None else None
                ),
                "first_violation_seq": (
                    first_violation.seq if first_violation is not None else None
                ),
                "first_violation_detail": (
                    first_violation.detail if first_violation is not None else None
                ),
                "first_seq": verification.first_seq,
                "last_seq": verification.last_seq,
            },
        )
        return await self._emit(alert, uow=uow)
