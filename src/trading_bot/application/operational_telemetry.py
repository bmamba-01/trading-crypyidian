"""Dịch vụ dùng chung cho telemetry thật, đánh giá và mở lại Circuit Breaker (OPS07A/B)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trading_bot.adapters.database.repositories.operational_telemetry import (
    OperationalTelemetryRepository,
    scope_key_for,
)
from trading_bot.application.outbox import UnitOfWork
from trading_bot.domain.events import EventEnvelope
from trading_bot.domain.risk.circuit_breaker import (
    BreakerState,
    CircuitBreakerEngine,
    CircuitBreakerPolicy,
    CircuitBreakerSnapshot,
    OperationalTelemetry,
)
from trading_bot.domain.risk.risk_windows import LossWindowSnapshot
from trading_bot.observability.health import build_system_health_snapshot


async def evaluate_and_persist_breaker(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    bot_id: UUID | None,
    as_of: datetime,
    policy: CircuitBreakerPolicy | None = None,
    consecutive_api_errors: int = 0,
    drawdown_pct_override: Decimal | None = None,
    outbox: Any | None = None,
) -> tuple[OperationalTelemetry, LossWindowSnapshot, CircuitBreakerSnapshot]:
    """Đọc telemetry thật, đánh giá Circuit Breaker và lưu append-only khi chuyển trạng thái."""
    repo = OperationalTelemetryRepository(session)
    telemetry, loss_snapshot = await repo.collect(
        workspace_id=workspace_id,
        as_of=as_of,
        bot_id=bot_id,
        consecutive_api_errors=consecutive_api_errors,
        drawdown_pct_override=drawdown_pct_override,
    )
    scope_key = scope_key_for(bot_id)
    persisted = await repo.latest_breaker_snapshot(workspace_id=workspace_id, scope_key=scope_key)
    engine = CircuitBreakerEngine.from_snapshot(persisted, policy=policy)
    previous_state = persisted.state if persisted is not None else BreakerState.CLOSED
    previous_reasons = persisted.tripped_reasons if persisted is not None else ()
    snapshot = engine.evaluate(telemetry)
    if (
        snapshot.state is not previous_state
        or snapshot.tripped_reasons != previous_reasons
        or (persisted is None and snapshot.state is not BreakerState.CLOSED)
    ):
        await repo.append_breaker_event(
            workspace_id=workspace_id,
            bot_id=bot_id,
            snapshot=snapshot,
        )
        if outbox is not None and snapshot.state is BreakerState.OPEN:
            outbox.enqueue(
                EventEnvelope(
                    event_id=uuid4(),
                    workspace_id=workspace_id,
                    event_type="ops.circuit_breaker.tripped",
                    aggregate_type="circuit_breaker",
                    aggregate_id=bot_id or workspace_id,
                    occurred_at=as_of,
                    correlation_id=uuid4(),
                    idempotency_key=f"breaker-tripped:{scope_key}:{snapshot.revision}",
                    schema_version=1,
                    payload={
                        "workspace_id": str(workspace_id),
                        "bot_id": str(bot_id) if bot_id else None,
                        "state": snapshot.state.value,
                        "reasons": [r.value for r in snapshot.tripped_reasons],
                        "detail_vi": snapshot.detail_vi,
                        "revision": snapshot.revision,
                    },
                )
            )
    return telemetry, loss_snapshot, snapshot


class OperationalHealthService:
    """Đọc sức khỏe vận hành thật (admin-web READ ONLY) và xử lý mở lại breaker có MFA."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    async def load_health_snapshot(
        self,
        *,
        workspace_id: UUID,
        actor_membership_id: UUID | None,
        runtime: list[dict[str, Any]],
        workspace: dict[str, Any],
        correlation_id: str,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Đọc telemetry thật trong transaction READ ONLY và trả snapshot đã che bí mật."""
        evaluated_at = now_utc or self._clock()
        total_api_errors = sum(int(item.get("error_count", 0) or 0) for item in runtime)
        async with UnitOfWork(
            self._sessions,
            workspace_id=workspace_id,
            actor_membership_id=actor_membership_id,
            snapshot_read_only=True,
        ) as unit:
            repo = OperationalTelemetryRepository(unit.session, clock=self._clock)
            telemetry, loss_snapshot = await repo.collect(
                workspace_id=workspace_id,
                as_of=evaluated_at,
                consecutive_api_errors=total_api_errors,
            )
            persisted = await repo.latest_breaker_snapshot(
                workspace_id=workspace_id,
                scope_key="workspace",
            )
        engine = CircuitBreakerEngine.from_snapshot(persisted)
        breaker_snapshot = engine.evaluate(telemetry)
        health_snapshot = build_system_health_snapshot(
            service_name="crypidian-admin",
            environment="PAPER",
            correlation_id=correlation_id,
            breaker=breaker_snapshot,
            loss_windows=loss_snapshot,
            metadata={
                "workspace_id": str(workspace_id),
                "kill_state": str(workspace.get("kill_state", "ACTIVE")),
                "runtime_bot_count": len(runtime),
                "telemetry": telemetry.to_evidence(),
                "telemetry_available": True,
                "telemetry_source": "postgres_ledger",
            },
            evaluated_at=evaluated_at,
        )
        return health_snapshot.to_dict()

    async def resume_breaker(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None,
        operator_membership_id: UUID,
        operator_id: str,
        mfa_verified: bool,
        approval_reason: str,
    ) -> CircuitBreakerSnapshot:
        """Mở lại Circuit Breaker sau cooldown + telemetry sạch + MFA + lý do."""
        now_utc = self._clock()
        scope_key = scope_key_for(bot_id)
        async with UnitOfWork(
            self._sessions,
            workspace_id=workspace_id,
            actor_membership_id=operator_membership_id,
        ) as unit:
            repo = OperationalTelemetryRepository(unit.session, clock=self._clock)
            persisted = await repo.latest_breaker_snapshot(
                workspace_id=workspace_id,
                scope_key=scope_key,
            )
            telemetry, _ = await repo.collect(
                workspace_id=workspace_id,
                as_of=now_utc,
                bot_id=bot_id,
            )
            engine = CircuitBreakerEngine.from_snapshot(persisted)
            # Cập nhật cooldown nếu đã trôi qua trước khi gọi request_resume
            engine.evaluate(telemetry)
            resumed = engine.request_resume(
                now=now_utc,
                telemetry=telemetry,
                operator_id=operator_id,
                mfa_verified=mfa_verified,
                approval_reason=approval_reason,
            )
            await repo.append_breaker_event(
                workspace_id=workspace_id,
                bot_id=bot_id,
                snapshot=resumed,
                resumed_by=operator_membership_id,
                resumed_at=now_utc,
                resume_reason=approval_reason.strip(),
            )
            unit.enqueue(
                EventEnvelope(
                    event_id=uuid4(),
                    workspace_id=workspace_id,
                    event_type="ops.circuit_breaker.resumed",
                    aggregate_type="circuit_breaker",
                    aggregate_id=bot_id or workspace_id,
                    occurred_at=now_utc,
                    correlation_id=uuid4(),
                    idempotency_key=f"breaker-resumed:{scope_key}:{resumed.revision}",
                    schema_version=1,
                    payload={
                        "workspace_id": str(workspace_id),
                        "bot_id": str(bot_id) if bot_id else None,
                        "operator_membership_id": str(operator_membership_id),
                        "revision": resumed.revision,
                    },
                )
            )
            return resumed


__all__ = [
    "OperationalHealthService",
    "evaluate_and_persist_breaker",
]
