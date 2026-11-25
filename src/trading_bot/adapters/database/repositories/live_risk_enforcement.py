"""Enforce hard limits Live nguyên tử trên PostgreSQL bằng khóa hàng `trading_account`.

Bảo đảm hai yêu cầu đồng thời không thể cùng vượt trần exposure; giữ nguyên reservation
khi có trạng thái UNKNOWN hoặc transaction rollback; áp dụng giảm cap/pause tức thời.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.entities import TradingAccount
from trading_bot.adapters.database.models.risk_entities import (
    RiskDecisionRecord,
    RiskReservationRecord,
    TradePlanRecord,
)
from trading_bot.domain.risk.live_risk_profile import (
    LiveAccountMode,
    LiveOrderRiskRequest,
    LiveRiskDecision,
    SignedLiveRiskProfile,
    VenueEquitySnapshot,
    evaluate_live_risk_limits,
)
from trading_bot.domain.risk.risk_windows import RealizedTradePnlEvent

ZERO = Decimal("0")


class AtomicLiveRiskEnforcementRepository:
    """Kiểm tra hard limits và ghi `risk_reservation` nguyên tử trong một transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def evaluate_and_reserve(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        profile: SignedLiveRiskProfile,
        wallet_balance: Decimal,
        unrealized_pnl: Decimal,
        available_balance: Decimal,
        initial_margin_used: Decimal,
        fee_and_funding_today: Decimal,
        fee_and_funding_rolling: Decimal,
        open_exposure_notional: Decimal,
        symbol_open_exposure_notional: Decimal,
        mark_price: Decimal | None,
        mark_observed_at: datetime | None,
        request: LiveOrderRiskRequest,
        realized_events: Iterable[RealizedTradePnlEvent],
        idempotency_key: str,
        reserved_risk: Decimal,
        reserved_initial_margin: Decimal,
        reservation_ttl: timedelta,
        as_of: datetime,
        signing_key: str,
    ) -> tuple[LiveRiskDecision, RiskReservationRecord | None]:
        """Khóa hàng `trading_account` (`FOR UPDATE`), cộng đủ RESERVED + UNKNOWN rồi cấp chỗ."""
        # 1. Khóa hàng tài khoản để tuần tự hóa các request đồng thời
        account = await self._session.scalar(
            select(TradingAccount)
            .where(
                TradingAccount.workspace_id == workspace_id,
                TradingAccount.id == account_id,
            )
            .with_for_update()
        )
        if account is None:
            raise LookupError(f"Không tìm thấy trading_account {account_id}.")

        # 2. Đọc tổng notional đang giữ từ cả RESERVED và UNKNOWN trong PostgreSQL
        active_reserved = (
            await self._session.scalar(
                select(
                    func.coalesce(func.sum(RiskReservationRecord.reserved_notional), ZERO)
                ).where(
                    RiskReservationRecord.workspace_id == workspace_id,
                    RiskReservationRecord.account_id == account_id,
                    RiskReservationRecord.status == "RESERVED",
                )
            )
            or ZERO
        )
        active_unknown = (
            await self._session.scalar(
                select(
                    func.coalesce(func.sum(RiskReservationRecord.reserved_notional), ZERO)
                ).where(
                    RiskReservationRecord.workspace_id == workspace_id,
                    RiskReservationRecord.account_id == account_id,
                    RiskReservationRecord.status == "UNKNOWN",
                )
            )
            or ZERO
        )
        symbol_reserved = (
            await self._session.scalar(
                select(
                    func.coalesce(func.sum(RiskReservationRecord.reserved_notional), ZERO)
                ).where(
                    RiskReservationRecord.workspace_id == workspace_id,
                    RiskReservationRecord.account_id == account_id,
                    RiskReservationRecord.symbol == request.symbol,
                    RiskReservationRecord.status == "RESERVED",
                )
            )
            or ZERO
        )
        symbol_unknown = (
            await self._session.scalar(
                select(
                    func.coalesce(func.sum(RiskReservationRecord.reserved_notional), ZERO)
                ).where(
                    RiskReservationRecord.workspace_id == workspace_id,
                    RiskReservationRecord.account_id == account_id,
                    RiskReservationRecord.symbol == request.symbol,
                    RiskReservationRecord.status == "UNKNOWN",
                )
            )
            or ZERO
        )

        equity = VenueEquitySnapshot(
            account_id=str(account_id),
            environment=profile.environment,
            account_mode=LiveAccountMode(profile.account_mode),
            wallet_balance=wallet_balance,
            unrealized_pnl=unrealized_pnl,
            available_balance=available_balance,
            initial_margin_used=initial_margin_used,
            fee_and_funding_today=fee_and_funding_today,
            fee_and_funding_rolling=fee_and_funding_rolling,
            open_exposure_notional=open_exposure_notional,
            symbol_open_exposure_notional=symbol_open_exposure_notional,
            active_reservations_notional=Decimal(active_reserved),
            symbol_reservations_notional=Decimal(symbol_reserved),
            unknown_order_exposure_notional=Decimal(active_unknown),
            symbol_unknown_exposure_notional=Decimal(symbol_unknown),
            mark_price=mark_price,
            mark_observed_at=mark_observed_at,
        )

        decision = evaluate_live_risk_limits(
            profile=profile,
            equity=equity,
            request=request,
            realized_events=realized_events,
            as_of=as_of,
            signing_key=signing_key,
        )

        plan_hash = decision.profile_hash
        existing_plan = await self._session.scalar(
            select(TradePlanRecord).where(
                TradePlanRecord.workspace_id == workspace_id,
                TradePlanRecord.account_id == account_id,
                TradePlanRecord.plan_hash == plan_hash,
            )
        )
        if existing_plan is None:
            self._session.add(
                TradePlanRecord(
                    workspace_id=workspace_id,
                    bot_id=bot_id,
                    account_id=account_id,
                    plan_hash=plan_hash,
                    symbol=request.symbol,
                    notional=request.order_notional,
                    payload={"profile_version": profile.profile_version},
                )
            )
            await self._session.flush()

        decision_record = RiskDecisionRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            decision_key=f"live-risk-{idempotency_key}",
            plan_hash=plan_hash,
            snapshot_id=f"live-{as_of.isoformat()}",
            approved=decision.approved,
            reason_code=decision.reason_code.value,
            detail_vi=decision.detail_vi,
            payload={
                "profile_version": decision.profile_version,
                "profile_hash": decision.profile_hash,
                "venue_equity": str(decision.venue_equity),
                "projected_account_exposure": str(decision.projected_account_exposure),
            },
        )
        self._session.add(decision_record)
        await self._session.flush()

        if not decision.approved:
            return decision, None

        reservation = RiskReservationRecord(
            id=uuid4(),
            workspace_id=workspace_id,
            bot_id=bot_id,
            account_id=account_id,
            risk_decision_id=decision_record.id,
            idempotency_key=idempotency_key,
            plan_hash=plan_hash,
            symbol=request.symbol,
            reserved_risk=reserved_risk,
            reserved_notional=request.order_notional,
            reserved_initial_margin=reserved_initial_margin,
            expires_at=as_of + reservation_ttl,
            status="RESERVED",
            resolution_reason=None,
        )
        self._session.add(reservation)
        await self._session.flush()
        return decision, reservation
