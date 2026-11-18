"""Truy vấn telemetry vận hành thật và trạng thái Circuit Breaker bền vững (OPS07A/B)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.bot_runtime_entities import (
    AccountActivityIncidentRecord,
)
from trading_bot.adapters.database.models.circuit_breaker_entities import (
    CircuitBreakerEventRecord,
)
from trading_bot.adapters.database.models.paper_entities import (
    OrderIntentRecord,
    TradeRecord,
)
from trading_bot.adapters.database.repositories.live_monitor import (
    CONSUMED_OUTBOX_EVENT_TYPES,
    FEED_SOURCES,
    FEED_TIMEFRAME,
)
from trading_bot.domain.monitoring.live_monitor import measure_clock_skew_ms
from trading_bot.domain.risk.circuit_breaker import (
    BreakerState,
    BreakerTriggerKind,
    CircuitBreakerSnapshot,
    OperationalTelemetry,
)
from trading_bot.domain.risk.drawdown_breaker import ClosedTradePnl, evaluate_drawdown_breaker
from trading_bot.domain.risk.risk_windows import (
    RISK_LOSS_WINDOW_POLICY,
    LossWindowSnapshot,
    RealizedTradePnlEvent,
    calculate_loss_windows,
)

_LOSS_LOOKBACK = timedelta(hours=48)
_FEED_BAR_INTERVAL_SECONDS = 60


def scope_key_for(bot_id: UUID | None = None) -> str:
    """Chuẩn hóa khóa phạm vi cho bảng ``circuit_breaker_event``."""
    return f"bot:{bot_id}" if bot_id is not None else "workspace"


class OperationalTelemetryRepository:
    """Đọc telemetry thật từ ledger/feed/outbox/incident và lưu lịch sử Circuit Breaker."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session = session
        self._clock = clock

    async def collect(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime,
        bot_id: UUID | None = None,
        consecutive_api_errors: int = 0,
        drawdown_pct_override: Decimal | None = None,
    ) -> tuple[OperationalTelemetry, LossWindowSnapshot]:
        """Thu thập ảnh chụp telemetry thật và loss windows dùng chung cho runtime & admin-web."""
        loss_snapshot = await self._load_loss_windows(
            workspace_id=workspace_id,
            bot_id=bot_id,
            as_of=as_of,
        )
        drawdown_pct = (
            drawdown_pct_override
            if drawdown_pct_override is not None
            else await self._load_drawdown_pct(
                workspace_id=workspace_id,
                bot_id=bot_id,
                as_of=as_of,
            )
        )
        feed_age_seconds = await self._load_feed_age_seconds(
            workspace_id=workspace_id,
            as_of=as_of,
        )
        outbox_lag_seconds = await self._load_outbox_lag_seconds(
            workspace_id=workspace_id,
            as_of=as_of,
        )
        clock_skew_ms = await self._measure_clock_skew_ms()
        unknown_order_count = await self._count_unknown_orders(
            workspace_id=workspace_id,
            bot_id=bot_id,
        )
        unprotected_exposure_count = await self._count_unprotected_exposures(
            workspace_id=workspace_id,
            bot_id=bot_id,
        )
        reconciliation_discrepancy_count = await self._count_open_incidents(
            workspace_id=workspace_id,
            bot_id=bot_id,
        )
        telemetry = OperationalTelemetry(
            evaluated_at=as_of,
            feed_age_seconds=feed_age_seconds,
            daily_loss=loss_snapshot.realized_loss_today,
            drawdown_pct=drawdown_pct,
            consecutive_api_errors=max(0, int(consecutive_api_errors)),
            outbox_lag_seconds=outbox_lag_seconds,
            clock_skew_ms=clock_skew_ms,
            unprotected_exposure_count=unprotected_exposure_count,
            reconciliation_discrepancy_count=reconciliation_discrepancy_count,
            unknown_order_count=unknown_order_count,
        )
        return telemetry, loss_snapshot

    async def _load_loss_windows(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None,
        as_of: datetime,
    ) -> LossWindowSnapshot:
        stmt = select(TradeRecord.id, TradeRecord.closed_at, TradeRecord.net_pnl).where(
            TradeRecord.workspace_id == workspace_id,
            TradeRecord.mode == "PAPER",
            TradeRecord.status == "CLOSED",
            TradeRecord.closed_at.is_not(None),
            TradeRecord.closed_at <= as_of,
            TradeRecord.closed_at > as_of - _LOSS_LOOKBACK,
        )
        if bot_id is not None:
            stmt = stmt.where(TradeRecord.bot_id == bot_id)
        rows = (await self._session.execute(stmt)).all()
        events = [
            RealizedTradePnlEvent(str(trade_id), closed_at, Decimal(net_pnl))
            for trade_id, closed_at, net_pnl in rows
            if closed_at is not None and net_pnl is not None
        ]
        return calculate_loss_windows(events, as_of=as_of, policy=RISK_LOSS_WINDOW_POLICY)

    async def _load_drawdown_pct(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None,
        as_of: datetime,
    ) -> Decimal | None:
        seed_sql = (
            "SELECT DISTINCT ON (s.bot_id) s.bot_id, s.initial_equity "
            "FROM paper_account_seed s WHERE s.workspace_id = :workspace_id "
        )
        params: dict[str, Any] = {"workspace_id": workspace_id, "as_of": as_of}
        if bot_id is not None:
            seed_sql += "AND s.bot_id = :bot_id "
            params["bot_id"] = bot_id
        seed_sql += "ORDER BY s.bot_id, s.created_at DESC"
        seed_rows = (await self._session.execute(text(seed_sql), params)).mappings().all()
        seed_total = sum(
            (
                Decimal(str(r["initial_equity"]))
                for r in seed_rows
                if r["initial_equity"] is not None
            ),
            Decimal(0),
        )
        if seed_total <= 0:
            return None

        baseline_sql = (
            "SELECT max(changed_at) FROM bot_runtime_control "
            "WHERE workspace_id = :workspace_id AND entry_state = 'ENABLED' "
            "AND changed_at <= :as_of"
        )
        if bot_id is not None:
            baseline_sql += " AND bot_id = :bot_id"
        baseline_at = await self._session.scalar(text(baseline_sql), params)

        trades_stmt = select(TradeRecord.closed_at, TradeRecord.net_pnl).where(
            TradeRecord.workspace_id == workspace_id,
            TradeRecord.mode == "PAPER",
            TradeRecord.status == "CLOSED",
            TradeRecord.closed_at.is_not(None),
            TradeRecord.closed_at <= as_of,
        )
        if bot_id is not None:
            trades_stmt = trades_stmt.where(TradeRecord.bot_id == bot_id)
        trade_rows = (await self._session.execute(trades_stmt)).all()
        closed_trades = [
            ClosedTradePnl(closed_at, Decimal(net_pnl))
            for closed_at, net_pnl in trade_rows
            if closed_at is not None and net_pnl is not None
        ]
        realized_total = sum((t.net_pnl for t in closed_trades), Decimal(0))
        current_equity = seed_total + realized_total
        state = evaluate_drawdown_breaker(
            seed_equity=seed_total,
            closed_trades=closed_trades,
            current_equity=current_equity,
            baseline_at=baseline_at,
        )
        return (state.drawdown_fraction * Decimal("100")).quantize(Decimal("0.01"))

    async def _load_feed_age_seconds(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime,
    ) -> int | None:
        latest_bar_close_at = await self._session.scalar(
            text(
                "SELECT max(mb.close_time) FROM market_bar mb "
                "WHERE mb.workspace_id = :workspace_id AND mb.timeframe = :timeframe "
                "AND mb.is_closed AND mb.source = ANY(:sources)"
            ),
            {
                "workspace_id": workspace_id,
                "timeframe": FEED_TIMEFRAME,
                "sources": list(FEED_SOURCES),
            },
        )
        if not isinstance(latest_bar_close_at, datetime):
            return None
        age = int((as_of - latest_bar_close_at).total_seconds())
        return max(0, age - _FEED_BAR_INTERVAL_SECONDS)

    async def _load_outbox_lag_seconds(
        self,
        *,
        workspace_id: UUID,
        as_of: datetime,
    ) -> int | None:
        oldest = await self._session.scalar(
            text(
                "SELECT min(available_at) FROM outbox_event "
                "WHERE workspace_id = :workspace_id AND status = 'PENDING' "
                "AND event_type = ANY(:consumed)"
            ),
            {
                "workspace_id": workspace_id,
                "consumed": list(CONSUMED_OUTBOX_EVENT_TYPES),
            },
        )
        if oldest is None:
            return 0
        if not isinstance(oldest, datetime):
            return None
        return max(0, int((as_of - oldest).total_seconds()))

    async def _measure_clock_skew_ms(self) -> int | None:
        sent = self._clock()
        db_now = await self._session.scalar(text("SELECT clock_timestamp()"))
        received = self._clock()
        if not isinstance(db_now, datetime):
            return None
        return abs(measure_clock_skew_ms(sent, db_now, received))

    async def _count_unknown_orders(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(OrderIntentRecord)
            .where(
                OrderIntentRecord.workspace_id == workspace_id,
                OrderIntentRecord.mode == "PAPER",
                OrderIntentRecord.status == "UNKNOWN",
            )
        )
        if bot_id is not None:
            stmt = stmt.where(OrderIntentRecord.bot_id == bot_id)
        return int((await self._session.scalar(stmt)) or 0)

    async def _count_unprotected_exposures(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None,
    ) -> int:
        sql = (
            "SELECT count(*) FROM trade t "
            "LEFT JOIN trade_plan pl ON pl.workspace_id = t.workspace_id "
            "AND pl.bot_id = t.bot_id AND pl.plan_hash = t.lineage->>'plan_hash' "
            "LEFT JOIN LATERAL ("
            "  SELECT p.payload->>'stop_price' AS stop_price "
            "  FROM position_event p "
            "  WHERE p.workspace_id = t.workspace_id AND p.bot_id = t.bot_id "
            "  AND p.intent_id = t.intent_id AND p.event_type = 'STOP_UPDATED' "
            "  ORDER BY p.occurred_at DESC, p.id DESC LIMIT 1"
            ") latest_stop ON true "
            "WHERE t.workspace_id = :workspace_id AND t.mode = 'PAPER' AND t.status = 'OPEN' "
            "AND coalesce(nullif(trim(latest_stop.stop_price), ''), "
            "nullif(trim(pl.payload->>'stop_price'), '')) IS NULL"
        )
        params: dict[str, Any] = {"workspace_id": workspace_id}
        if bot_id is not None:
            sql += " AND t.bot_id = :bot_id"
            params["bot_id"] = bot_id
        return int((await self._session.scalar(text(sql), params)) or 0)

    async def _count_open_incidents(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(AccountActivityIncidentRecord)
            .where(
                AccountActivityIncidentRecord.workspace_id == workspace_id,
                AccountActivityIncidentRecord.status == "OPEN",
            )
        )
        if bot_id is not None:
            stmt = stmt.where(AccountActivityIncidentRecord.bot_id == bot_id)
        return int((await self._session.scalar(stmt)) or 0)

    async def latest_breaker_snapshot(
        self,
        *,
        workspace_id: UUID,
        scope_key: str = "workspace",
    ) -> CircuitBreakerSnapshot | None:
        """Đọc bản ghi trạng thái Circuit Breaker mới nhất (revision lớn nhất)."""
        row = await self._session.scalar(
            select(CircuitBreakerEventRecord)
            .where(
                CircuitBreakerEventRecord.workspace_id == workspace_id,
                CircuitBreakerEventRecord.scope_key == scope_key,
            )
            .order_by(CircuitBreakerEventRecord.revision.desc())
            .limit(1)
        )
        if row is None:
            return None
        return _snapshot_from_record(row)

    async def has_open_breaker(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None = None,
    ) -> CircuitBreakerSnapshot | None:
        """Trả về snapshot nếu workspace hoặc bot đang ở trạng thái OPEN/COOLDOWN_PASSED."""
        scopes = ["workspace"]
        if bot_id is not None:
            scopes.insert(0, scope_key_for(bot_id))
        for scope in scopes:
            snap = await self.latest_breaker_snapshot(workspace_id=workspace_id, scope_key=scope)
            if snap is not None and not snap.entry_allowed:
                return snap
        return None

    async def append_breaker_event(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID | None,
        snapshot: CircuitBreakerSnapshot,
        policy_version: str = "ops07b-breaker-v1",
        resumed_by: UUID | None = None,
        resumed_at: datetime | None = None,
        resume_reason: str | None = None,
    ) -> CircuitBreakerEventRecord:
        """Ghi nhận append-only một chuyển đổi trạng thái Circuit Breaker."""
        scope_key = scope_key_for(bot_id)
        current_max = await self._session.scalar(
            select(func.max(CircuitBreakerEventRecord.revision)).where(
                CircuitBreakerEventRecord.workspace_id == workspace_id,
                CircuitBreakerEventRecord.scope_key == scope_key,
            )
        )
        next_revision = max(int(current_max or 0) + 1, snapshot.revision)
        record = CircuitBreakerEventRecord(
            workspace_id=workspace_id,
            bot_id=bot_id,
            scope_key=scope_key,
            revision=next_revision,
            state=snapshot.state.value,
            tripped_reasons=[r.value for r in snapshot.tripped_reasons],
            detail_vi=snapshot.detail_vi[:500],
            policy_version=policy_version,
            tripped_at=snapshot.tripped_at,
            cooldown_until=snapshot.cooldown_until,
            evidence=dict(snapshot.evidence),
            resumed_by=resumed_by,
            resumed_at=resumed_at,
            resume_reason=resume_reason[:500] if resume_reason else None,
        )
        self._session.add(record)
        await self._session.flush()
        return record


def _snapshot_from_record(row: CircuitBreakerEventRecord) -> CircuitBreakerSnapshot:
    state = BreakerState(row.state)
    reasons = tuple(BreakerTriggerKind(item) for item in (row.tripped_reasons or []))
    return CircuitBreakerSnapshot(
        state=state,
        entry_allowed=state is BreakerState.CLOSED,
        protective_exit_allowed=True,
        tripped_reasons=reasons,
        detail_vi=row.detail_vi,
        tripped_at=row.tripped_at,
        cooldown_until=row.cooldown_until,
        revision=int(row.revision),
        evidence=dict(row.evidence or {}),
    )


__all__ = [
    "OperationalTelemetryRepository",
    "scope_key_for",
]
