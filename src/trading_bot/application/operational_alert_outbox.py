"""Consumer outbox thông báo vận hành qua Telegram và truy vấn cảnh báo cho Admin Web (CRY-274)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.models import (
    ConsumerReceipt,
    OutboxEvent,
    TelegramBinding,
    Workspace,
)
from trading_bot.adapters.telegram.topic_sender import bind_topic
from trading_bot.application.outbox import OutboxRepository, UnitOfWork
from trading_bot.domain.events import EventEnvelope
from trading_bot.ports.telegram import TelegramMessageSender

OPERATIONAL_ALERT_CONSUMER = "telegram_operational_alert_worker"

logger = logging.getLogger(__name__)

# Danh sách các sự kiện vận hành cần đẩy cảnh báo tới người đã liên kết Telegram
# và hiển thị fallback trên Admin Web khi Telegram lỗi/dead-letter.
OPERATIONAL_ALERT_EVENT_TYPES: tuple[str, ...] = (
    "bot.paused",
    "bot.resumed",
    "bot.killed",
    "bot.kill_released",
    "bot.disabled",
    "bot.enabled",
    "workspace.flatten.confirmed",
    "workspace.flatten.failed",
    "account.foreign_activity.detected",
    "incident.foreign_activity.detected",
    "learning.incident.entry_hold.confirmed",
    "learning.incident.entry_hold.released",
    "ops.circuit_breaker.tripped",
    "ops.circuit_breaker.resumed",
)

_EVENT_TITLES_VI: dict[str, str] = {
    "bot.paused": "Tạm dừng bot",
    "bot.resumed": "Tiếp tục chạy bot",
    "bot.killed": "Kích hoạt Kill Switch",
    "bot.kill_released": "Giải phóng Kill Switch",
    "bot.disabled": "Tắt bot",
    "bot.enabled": "Bật bot",
    "workspace.flatten.confirmed": "Xác nhận đóng toàn bộ vị thế (Flatten)",
    "workspace.flatten.failed": "Đóng toàn bộ vị thế (Flatten) thất bại",
    "account.foreign_activity.detected": "Phát hiện hoạt động lạ trên tài khoản",
    "incident.foreign_activity.detected": "Sự cố hoạt động lạ trên tài khoản",
    "learning.incident.entry_hold.confirmed": "Chặn mở vị thế do sự cố học máy",
    "learning.incident.entry_hold.released": "Gỡ chặn mở vị thế sau sự cố học máy",
    "ops.circuit_breaker.tripped": "Cầu dao rủi ro kích hoạt",
    "ops.circuit_breaker.resumed": "Cầu dao rủi ro đã khôi phục",
}

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-+/=]{8,}\b", re.IGNORECASE),
    re.compile(
        r"(?i)\b(api[_-]?key|api[_-]?secret|secret[_-]?key|password|passwd|bot[_-]?token|"
        r"access[_-]?token|refresh[_-]?token|private[_-]?key)\s*[:=]\s*\S+"
    ),
)
_REDACTED = "[ĐÃ ẨN BÍ MẬT]"


# Sender dùng chung cổng TelegramMessageSender (TelegramBotApiSender/ThrottledTelegramSender);
# consumer chỉ gọi send_message nhưng nhận đủ cổng để bind_topic hoạt động đúng kiểu.
TelegramAlertSender = TelegramMessageSender


@dataclass(frozen=True, slots=True)
class OperationalAlertMessage:
    """Thông điệp cảnh báo vận hành tiếng Việt đã làm sạch bí mật."""

    event_id: UUID
    event_type: str
    mode: str
    workspace_label: str
    reason: str
    admin_link: str
    text_vi: str


@dataclass(frozen=True, slots=True)
class OperationalAlertView:
    """Bản ghi cảnh báo vận hành đọc từ outbox để hiển thị trên Admin Web kể cả khi Telegram lỗi."""

    event_id: UUID
    event_type: str
    title_vi: str
    status: str
    attempts: int
    last_error_code: str | None
    mode: str
    reason: str
    admin_link: str
    created_at: datetime
    available_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class OperationalAlertDispatchSummary:
    """Kết quả một lượt poll/gửi của consumer outbox cảnh báo vận hành."""

    claimed: int
    delivered: int
    skipped_duplicate: int
    retried: int
    dead_lettered: int


def enqueue_operational_alert(
    uow: UnitOfWork,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: UUID,
    occurred_at: datetime,
    correlation_id: UUID,
    idempotency_key: str,
    reason: str,
    reason_code: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> None:
    """Ghi sự kiện cảnh báo vận hành vào outbox CÙNG transaction với thao tác (CRY-274).

    Chỉ gọi khi thao tác thực sự đổi trạng thái; idempotency_key theo revision/event nên
    update lặp (noop) không tạo thêm sự kiện. Payload không chứa secret; tin Telegram được
    che bí mật thêm lần nữa lúc format.
    """
    if event_type not in OPERATIONAL_ALERT_EVENT_TYPES:
        raise ValueError(f"Loại sự kiện vận hành không hỗ trợ: {event_type}")
    payload: dict[str, Any] = {
        "workspace_id": str(uow.workspace_id),
        "reason": reason,
    }
    if reason_code is not None:
        payload["reason_code"] = reason_code
    if extra_payload is not None:
        payload.update(extra_payload)
    uow.enqueue(
        EventEnvelope(
            event_type=event_type,
            workspace_id=uow.workspace_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            schema_version=1,
            payload=payload,
        )
    )


def sanitize_alert_text(value: str) -> str:
    """Che mọi chuỗi giống token/secret/mật khẩu trước khi đưa vào tin nhắn cảnh báo."""
    cleaned = value.strip()
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(_REDACTED, cleaned)
    return cleaned


def _admin_link_for_event(event: OutboxEvent, *, admin_base_url: str) -> str:
    base = admin_base_url.rstrip("/")
    payload = event.payload if isinstance(event.payload, dict) else {}
    bot_id = payload.get("bot_id")
    if event.event_type.startswith("bot.") and isinstance(bot_id, str) and bot_id.strip():
        return f"{base}/admin/bots/{bot_id.strip()}"
    if event.event_type.startswith("workspace.flatten."):
        return f"{base}/admin/flatten"
    if event.event_type.startswith("ops.circuit_breaker."):
        return f"{base}/admin/live"
    return f"{base}/admin/incidents"


def _extract_reason(payload: dict[str, Any]) -> str:
    for key in ("reason", "detail", "detail_vi", "resolution_reason", "summary", "error_code"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return sanitize_alert_text(raw)
    reasons = payload.get("reasons")
    if isinstance(reasons, (list, tuple)) and reasons:
        joined = ", ".join(str(item) for item in reasons if str(item).strip())
        if joined.strip():
            return sanitize_alert_text(joined)
    return "Không ghi lý do chi tiết."


def format_operational_alert_vi(
    event: OutboxEvent,
    *,
    mode: str = "PAPER",
    workspace_label: str,
    admin_base_url: str = "",
) -> OperationalAlertMessage:
    """Dựng tin cảnh báo vận hành tiếng Việt nêu rõ mode/workspace/lý do/link, không chứa secret."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    effective_mode = str(payload.get("mode") or mode or "PAPER").upper()
    title = _EVENT_TITLES_VI.get(event.event_type, f"Sự kiện vận hành ({event.event_type})")
    reason = _extract_reason(payload)
    link = _admin_link_for_event(event, admin_base_url=admin_base_url)
    safe_workspace = sanitize_alert_text(workspace_label)
    lines = [
        f"[CẢNH BÁO VẬN HÀNH · {effective_mode}] {title}",
        f"Workspace: {safe_workspace}",
        f"Lý do: {reason}",
        f"Xem trên Admin Web: {link}",
    ]
    return OperationalAlertMessage(
        event_id=event.id,
        event_type=event.event_type,
        mode=effective_mode,
        workspace_label=safe_workspace,
        reason=reason,
        admin_link=link,
        text_vi="\n".join(lines),
    )


async def list_operational_alerts(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    include_delivered: bool = True,
    limit: int = 50,
    admin_base_url: str = "",
) -> tuple[OperationalAlertView, ...]:
    """Liệt kê cảnh báo vận hành từ outbox (ưu tiên PENDING/PROCESSING/DEAD) cho Admin Web."""
    # Chỉ đọc cột được cấp cho signal-worker (GRANT SELECT (id, name, mode)).
    workspace_row = (
        await session.execute(
            select(Workspace.name, Workspace.mode).where(Workspace.id == workspace_id)
        )
    ).one_or_none()
    mode = workspace_row.mode if workspace_row is not None else "PAPER"
    workspace_label = workspace_row.name if workspace_row is not None else str(workspace_id)
    statuses = (
        ("PENDING", "PROCESSING", "DEAD", "DELIVERED")
        if include_delivered
        else ("PENDING", "PROCESSING", "DEAD")
    )
    rows = (
        await session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.workspace_id == workspace_id,
                OutboxEvent.event_type.in_(OPERATIONAL_ALERT_EVENT_TYPES),
                OutboxEvent.status.in_(statuses),
            )
            .order_by(OutboxEvent.created_at.desc())
            .limit(limit)
        )
    ).all()
    views: list[OperationalAlertView] = []
    for row in rows:
        formatted = format_operational_alert_vi(
            row,
            mode=mode,
            workspace_label=workspace_label,
            admin_base_url=admin_base_url,
        )
        views.append(
            OperationalAlertView(
                event_id=row.id,
                event_type=row.event_type,
                title_vi=_EVENT_TITLES_VI.get(row.event_type, row.event_type),
                status=row.status,
                attempts=row.attempts,
                last_error_code=row.last_error_code,
                mode=formatted.mode,
                reason=formatted.reason,
                admin_link=formatted.admin_link,
                created_at=row.created_at,
                available_at=row.available_at,
                completed_at=row.completed_at,
            )
        )
    return tuple(views)


class OperationalAlertOutboxConsumer:
    """Consumer outbox → Telegram cho sự kiện vận hành: backoff hữu hạn, idempotent, chịu lỗi."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        sender: TelegramAlertSender,
        workspace_id: UUID,
        worker_id: str = OPERATIONAL_ALERT_CONSUMER,
        consumer_name: str = OPERATIONAL_ALERT_CONSUMER,
        admin_base_url: str = "",
        lease_duration: timedelta = timedelta(seconds=60),
        max_attempts: int = 5,
        base_delay_seconds: int = 5,
        max_delay_seconds: int = 300,
        tick_seconds: float = 5.0,
        now_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 0.1 <= tick_seconds <= 300:
            raise ValueError("tick_seconds phải trong khoảng 0.1..300")
        self._sessions = sessions
        self._sender = sender
        self._workspace_id = workspace_id
        self._worker_id = worker_id
        self._consumer_name = consumer_name
        self._admin_base_url = admin_base_url
        self._lease_duration = lease_duration
        self._max_attempts = max_attempts
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._tick_seconds = tick_seconds
        self._now = now_provider
        self.last_tick_succeeded = False
        self.last_tick_at: datetime | None = None
        self.last_error_code: str | None = None

    async def run(self, stopping: asyncio.Event) -> None:
        """Chạy tick đến khi lifespan yêu cầu dừng; lỗi tick sẽ thử lại ở chu kỳ sau."""
        while not stopping.is_set():
            try:
                await self.tick()
                self.last_tick_succeeded = True
                self.last_tick_at = self._now()
                self.last_error_code = None
            except Exception as exc:  # noqa: BLE001 - worker không được chết vì 1 tick lỗi
                self.last_tick_succeeded = False
                self.last_error_code = type(exc).__name__
                logger.warning("Operational alert worker tick lỗi; code=%s", self.last_error_code)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=self._tick_seconds)
            except TimeoutError:
                continue

    async def tick(self) -> OperationalAlertDispatchSummary:
        """Một lượt poll/gửi cho workspace đã cấu hình của worker."""
        return await self.process_once(workspace_id=self._workspace_id)

    async def process_once(
        self,
        *,
        workspace_id: UUID,
        limit: int = 20,
        event_types: Sequence[str] = OPERATIONAL_ALERT_EVENT_TYPES,
    ) -> OperationalAlertDispatchSummary:
        """Claim event sẵn sàng trong transaction ngắn, gửi Telegram ngoài transaction, rồi chốt."""
        now = self._now()
        claimed_events: list[OutboxEvent] = []
        destinations: list[tuple[int, int | None]] = []
        mode = "PAPER"
        workspace_label = str(workspace_id)
        already_processed: set[UUID] = set()

        async with UnitOfWork(self._sessions, workspace_id=workspace_id) as unit:
            repo = OutboxRepository(unit.session)
            claimed_events = await repo.claim_ready(
                workspace_id=workspace_id,
                worker_id=self._worker_id,
                now=now,
                lease_duration=self._lease_duration,
                max_attempts=self._max_attempts,
                limit=limit,
                event_types=event_types,
            )
            if not claimed_events:
                return OperationalAlertDispatchSummary(0, 0, 0, 0, 0)
            workspace_row = (
                await unit.session.execute(
                    select(Workspace.name, Workspace.mode).where(Workspace.id == workspace_id)
                )
            ).one_or_none()
            if workspace_row is not None:
                mode = workspace_row.mode
                workspace_label = workspace_row.name
            # Chỉ đọc cột được cấp (telegram_user_id, report_thread_id) — least privilege.
            binding_rows = (
                await unit.session.execute(
                    select(
                        TelegramBinding.telegram_user_id,
                        TelegramBinding.report_thread_id,
                    ).where(
                        TelegramBinding.workspace_id == workspace_id,
                        TelegramBinding.verified_at.is_not(None),
                        TelegramBinding.revoked_at.is_(None),
                    )
                )
            ).all()
            seen_dest: set[tuple[int, int | None]] = set()
            for binding in binding_rows:
                # Private chat của người đã bind: chat_id chính là telegram_user_id
                # (quy ước MB-07b, xem TelegramSignalRepository.put_subscription).
                try:
                    chat_id = int(binding.telegram_user_id)
                except TypeError, ValueError:
                    continue
                if chat_id <= 0:
                    continue
                dest = (chat_id, binding.report_thread_id)
                if dest not in seen_dest:
                    seen_dest.add(dest)
                    destinations.append(dest)
            receipt_ids = (
                await unit.session.scalars(
                    select(ConsumerReceipt.outbox_event_id).where(
                        ConsumerReceipt.workspace_id == workspace_id,
                        ConsumerReceipt.consumer_name == self._consumer_name,
                        ConsumerReceipt.outbox_event_id.in_([ev.id for ev in claimed_events]),
                    )
                )
            ).all()
            already_processed = set(receipt_ids)

        delivered = 0
        skipped_duplicate = 0
        retried = 0
        dead_lettered = 0

        for event in claimed_events:
            if event.id in already_processed:
                async with UnitOfWork(self._sessions, workspace_id=workspace_id) as unit:
                    repo = OutboxRepository(unit.session)
                    await repo.acknowledge(
                        workspace_id=workspace_id,
                        event_id=event.id,
                        worker_id=self._worker_id,
                        consumer_name=self._consumer_name,
                        now=self._now(),
                    )
                skipped_duplicate += 1
                continue

            alert = format_operational_alert_vi(
                event,
                mode=mode,
                workspace_label=workspace_label,
                admin_base_url=self._admin_base_url,
            )
            send_error: BaseException | None = None
            for chat_id, thread_id in destinations:
                target = bind_topic(self._sender, thread_id)
                try:
                    await target.send_message(chat_id, alert.text_vi)
                except Exception as exc:  # noqa: BLE001
                    send_error = exc
                    break

            finish_time = self._now()
            if send_error is None:
                async with UnitOfWork(self._sessions, workspace_id=workspace_id) as unit:
                    repo = OutboxRepository(unit.session)
                    await repo.acknowledge(
                        workspace_id=workspace_id,
                        event_id=event.id,
                        worker_id=self._worker_id,
                        consumer_name=self._consumer_name,
                        now=finish_time,
                    )
                    unit.record_audit(
                        action="operational_alert.delivered",
                        target_type="outbox_event",
                        target_id=event.id,
                        reason=f"Đã gửi cảnh báo {event.event_type} tới {len(destinations)} chat.",
                        after_state={
                            "event_type": event.event_type,
                            "chat_count": len(destinations),
                        },
                    )
                delivered += 1
            else:
                async with UnitOfWork(self._sessions, workspace_id=workspace_id) as unit:
                    repo = OutboxRepository(unit.session)
                    new_status = await repo.record_failure(
                        workspace_id=workspace_id,
                        event_id=event.id,
                        worker_id=self._worker_id,
                        now=finish_time,
                        error=send_error,
                        max_attempts=self._max_attempts,
                        base_delay_seconds=self._base_delay_seconds,
                        max_delay_seconds=self._max_delay_seconds,
                    )
                    if new_status == "DEAD":
                        unit.record_audit(
                            action="operational_alert.dead_lettered",
                            target_type="outbox_event",
                            target_id=event.id,
                            reason=(
                                f"Cảnh báo {event.event_type} chuyển dead-letter sau "
                                f"{self._max_attempts} lần thử: {type(send_error).__name__}."
                            ),
                            after_state={
                                "event_type": event.event_type,
                                "error": type(send_error).__name__,
                                "status": "DEAD",
                            },
                        )
                        dead_lettered += 1
                    else:
                        retried += 1

        return OperationalAlertDispatchSummary(
            claimed=len(claimed_events),
            delivered=delivered,
            skipped_duplicate=skipped_duplicate,
            retried=retried,
            dead_lettered=dead_lettered,
        )


__all__ = [
    "OPERATIONAL_ALERT_CONSUMER",
    "enqueue_operational_alert",
    "OPERATIONAL_ALERT_EVENT_TYPES",
    "OperationalAlertDispatchSummary",
    "OperationalAlertMessage",
    "OperationalAlertOutboxConsumer",
    "OperationalAlertView",
    "TelegramAlertSender",
    "format_operational_alert_vi",
    "list_operational_alerts",
    "sanitize_alert_text",
]
