"""Account equity repository cho PAPER account point-in-time."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trading_bot.adapters.database.models.entities import PaperAccountSeed
from trading_bot.adapters.database.models.market_entities import MarketBar, MarketInstrument
from trading_bot.adapters.database.models.paper_entities import (
    FillRecord,
    OrderIntentRecord,
    TradeRecord,
)
from trading_bot.adapters.database.models.risk_entities import TradePlanRecord
from trading_bot.domain.risk.equity import (
    AccountEquitySnapshot,
    EquityReasonCode,
    EquityValidationError,
    MarketMark,
    OpenPositionValuation,
    PaperEquityBasis,
    mark_to_market_equity,
)

if TYPE_CHECKING:
    pass


class AccountEquityRepository:
    """Nạp snapshot vốn point-in-time cho PAPER account và fail-closed khi thiếu dữ kiện."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        mark_loader: (
            Callable[[UUID, str, datetime, timedelta], Awaitable[MarketMark | None]] | None
        ) = None,
    ) -> None:
        self._session = session
        self._mark_loader = mark_loader

    async def load_paper_equity_snapshot(
        self,
        *,
        workspace_id: UUID,
        bot_id: UUID,
        account_id: UUID,
        as_of: datetime,
        max_equity_age: timedelta,
        max_mark_age: timedelta,
        captured_at: datetime | None = None,
        marks: Mapping[str, MarketMark] | None = None,
    ) -> AccountEquitySnapshot:
        """Nạp seed, realized PnL và open positions; tính mark-to-market hoặc trả về unknown."""
        _validate_aware(as_of, "as_of")
        effective_captured_at = captured_at or as_of
        _validate_aware(effective_captured_at, "captured_at")

        # 1. Nạp PaperAccountSeed
        seed = await self._session.scalar(
            select(PaperAccountSeed).where(
                PaperAccountSeed.workspace_id == workspace_id,
                PaperAccountSeed.bot_id == bot_id,
                PaperAccountSeed.trading_account_id == account_id,
                PaperAccountSeed.created_at <= as_of,
            )
        )
        if seed is None:
            empty_basis = PaperEquityBasis(
                workspace_id=str(workspace_id),
                bot_id=str(bot_id),
                account_id=str(account_id),
                asset="USDT",
                as_of=as_of,
                captured_at=effective_captured_at,
                available_at=effective_captured_at,
                source="paper_account_seed",
                source_version="v1",
                max_mark_age=max_mark_age,
                max_equity_age=max_equity_age,
                initial_capital_before_realized_pnl=Decimal("0"),
                cumulative_realized_pnl=Decimal("0"),
                known=False,
                unknown_reason=EquityReasonCode.BASIS_UNKNOWN.value,
            )
            return AccountEquitySnapshot(
                workspace_id=str(workspace_id),
                bot_id=str(bot_id),
                account_id=str(account_id),
                asset="USDT",
                equity_amount=Decimal("0"),
                equity_basis="PAPER",
                seed_capital=None,
                cumulative_realized_pnl=None,
                unrealized_pnl=Decimal("0"),
                captured_at=effective_captured_at,
                as_of=as_of,
                available_at=effective_captured_at,
                source="paper_account_seed",
                source_version="v1",
                source_hash="",
                mark_refs=(),
                basis=empty_basis,
                positions=(),
                known=False,
                unknown_reason=EquityReasonCode.BASIS_UNKNOWN.value,
            )

        starting_equity = Decimal(
            str(getattr(seed, "starting_equity", getattr(seed, "initial_equity", Decimal("0"))))
        )
        quote_asset = getattr(seed, "quote_asset", "USDT")

        # 2. Nạp Realized PnL: các trade CLOSED có closed_at <= as_of
        realized_scalar = await self._session.scalar(
            select(func.coalesce(func.sum(TradeRecord.net_pnl), Decimal("0"))).where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.account_id == account_id,
                TradeRecord.mode == "PAPER",
                TradeRecord.status == "CLOSED",
                TradeRecord.closed_at <= as_of,
            )
        )
        cumulative_realized_pnl = Decimal(str(realized_scalar or 0))

        # 3. Nạp Vị thế Mở: các trade OPEN có opened_at <= as_of
        trades_result = await self._session.scalars(
            select(TradeRecord)
            .where(
                TradeRecord.workspace_id == workspace_id,
                TradeRecord.bot_id == bot_id,
                TradeRecord.account_id == account_id,
                TradeRecord.mode == "PAPER",
                TradeRecord.status == "OPEN",
                TradeRecord.opened_at <= as_of,
            )
            .order_by(TradeRecord.opened_at.asc(), TradeRecord.id.asc())
        )
        open_trades = list(trades_result.all())

        # 4. Kiểm tra tuổi của snapshot vốn
        if as_of - effective_captured_at > max_equity_age:
            basis = PaperEquityBasis(
                workspace_id=str(workspace_id),
                bot_id=str(bot_id),
                account_id=str(account_id),
                asset=quote_asset,
                as_of=as_of,
                captured_at=effective_captured_at,
                available_at=effective_captured_at,
                source="paper_account_seed",
                source_version="v1",
                max_mark_age=max_mark_age,
                max_equity_age=max_equity_age,
                initial_capital_before_realized_pnl=starting_equity,
                cumulative_realized_pnl=cumulative_realized_pnl,
                known=False,
                unknown_reason=EquityReasonCode.EQUITY_STALE.value,
            )
            return _unknown_snapshot(basis, (), EquityReasonCode.EQUITY_STALE)

        # 5. Dựng OpenPositionValuation
        open_positions: list[OpenPositionValuation] = []
        for trade in open_trades:
            # Xác định intent nếu có
            intent: OrderIntentRecord | None = None
            if getattr(trade, "intent_id", None) is not None:
                intent = await self._session.scalar(
                    select(OrderIntentRecord).where(
                        OrderIntentRecord.workspace_id == workspace_id,
                        OrderIntentRecord.account_id == account_id,
                        OrderIntentRecord.id == trade.intent_id,
                    )
                )

            # Xác định quantity
            qty: Decimal | None = None
            if hasattr(trade, "quantity") and trade.quantity is not None:
                qty = Decimal(str(trade.quantity))
            elif intent is not None:
                qty = (
                    Decimal(str(intent.filled_quantity))
                    if intent.filled_quantity > 0
                    else Decimal(str(intent.quantity))
                )

            if qty is None or qty <= Decimal("0"):
                qty = Decimal("0")

            # Xác định entry_price
            entry_price: Decimal | None = None
            if hasattr(trade, "entry_price") and trade.entry_price is not None:
                entry_price = Decimal(str(trade.entry_price))
            elif getattr(trade, "intent_id", None) is not None:
                entry_fill = await self._session.scalar(
                    select(FillRecord)
                    .where(
                        FillRecord.workspace_id == workspace_id,
                        FillRecord.account_id == account_id,
                        FillRecord.intent_id == trade.intent_id,
                        FillRecord.fill_role == "ENTRY",
                    )
                    .order_by(FillRecord.occurred_at.asc())
                    .limit(1)
                )
                if entry_fill is not None:
                    entry_price = Decimal(str(entry_fill.price))
                elif intent is not None and intent.plan_hash:
                    plan = await self._session.scalar(
                        select(TradePlanRecord).where(
                            TradePlanRecord.workspace_id == workspace_id,
                            TradePlanRecord.account_id == account_id,
                            TradePlanRecord.plan_hash == intent.plan_hash,
                        )
                    )
                    if plan is not None and "entry_price" in plan.payload:
                        entry_price = Decimal(str(plan.payload["entry_price"]))

            if entry_price is None or entry_price <= Decimal("0"):
                entry_price = Decimal("0")

            multiplier = Decimal(str(getattr(trade, "contract_multiplier", Decimal("1"))))
            direction = str(trade.side).lower()
            open_positions.append(
                OpenPositionValuation(
                    workspace_id=str(workspace_id),
                    bot_id=str(bot_id),
                    account_id=str(account_id),
                    position_id=str(trade.id),
                    symbol=trade.symbol,
                    asset=getattr(trade, "quote_asset", quote_asset),
                    direction=direction,
                    quantity=qty,
                    entry_price=entry_price,
                    contract_multiplier=multiplier,
                    opened_at=trade.opened_at,
                )
            )

        basis = PaperEquityBasis(
            workspace_id=str(workspace_id),
            bot_id=str(bot_id),
            account_id=str(account_id),
            asset=quote_asset,
            as_of=as_of,
            captured_at=effective_captured_at,
            available_at=effective_captured_at,
            source="paper_account_seed",
            source_version="v1",
            max_mark_age=max_mark_age,
            max_equity_age=max_equity_age,
            initial_capital_before_realized_pnl=starting_equity,
            cumulative_realized_pnl=cumulative_realized_pnl,
        )

        # 6. Thu thập marks cho từng symbol vị thế mở
        used_marks: dict[str, MarketMark] = {}
        for pos in open_positions:
            if pos.symbol in used_marks:
                continue

            mark: MarketMark | None = None
            if marks is not None and pos.symbol in marks:
                mark = marks[pos.symbol]
            elif self._mark_loader is not None:
                mark = await self._mark_loader(workspace_id, pos.symbol, as_of, max_mark_age)
            else:
                bar = await self._session.scalar(
                    select(MarketBar)
                    .join(MarketInstrument, MarketBar.instrument_id == MarketInstrument.id)
                    .where(
                        MarketInstrument.workspace_id == workspace_id,
                        MarketInstrument.symbol == pos.symbol,
                        MarketBar.available_at <= as_of,
                    )
                    .order_by(MarketBar.close_time.desc(), MarketBar.revision.desc())
                    .limit(1)
                )
                if bar is not None:
                    mark = MarketMark(
                        workspace_id=str(workspace_id),
                        bot_id=str(bot_id),
                        account_id=str(account_id),
                        symbol=pos.symbol,
                        asset=pos.asset,
                        price=Decimal(str(bar.close)),
                        as_of=bar.close_time,
                        available_at=bar.available_at,
                        source=bar.source,
                        source_version=str(bar.revision),
                        source_hash=bar.checksum,
                    )

            if mark is None:
                return _unknown_snapshot(basis, open_positions, EquityReasonCode.MARK_MISSING)
            if mark.price <= Decimal("0"):
                return _unknown_snapshot(basis, open_positions, EquityReasonCode.AMOUNT_INVALID)
            if mark.available_at > as_of or mark.as_of > as_of:
                return _unknown_snapshot(basis, open_positions, EquityReasonCode.OBSERVATION_FUTURE)
            if mark.as_of > mark.available_at:
                return _unknown_snapshot(basis, open_positions, EquityReasonCode.TIMESTAMP_INVALID)
            if as_of - mark.as_of > max_mark_age:
                return _unknown_snapshot(basis, open_positions, EquityReasonCode.MARK_STALE)

            used_marks[pos.symbol] = mark

        # 7. Tính toán equity qua mark_to_market_equity thuần domain
        try:
            return mark_to_market_equity(basis, open_positions, used_marks, as_of)
        except EquityValidationError as exc:
            return _unknown_snapshot(basis, open_positions, exc.reason_code)


def _validate_aware(dt: datetime, name: str) -> None:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise ValueError(f"{name} phải là datetime nhận biết múi giờ UTC.")


def _unknown_snapshot(
    basis: PaperEquityBasis,
    positions: Sequence[OpenPositionValuation],
    reason: EquityReasonCode,
) -> AccountEquitySnapshot:
    return AccountEquitySnapshot(
        workspace_id=basis.workspace_id,
        bot_id=basis.bot_id,
        account_id=basis.account_id,
        asset=basis.asset,
        equity_amount=basis.initial_capital_before_realized_pnl + basis.cumulative_realized_pnl,
        equity_basis="PAPER",
        seed_capital=basis.initial_capital_before_realized_pnl,
        cumulative_realized_pnl=basis.cumulative_realized_pnl,
        unrealized_pnl=Decimal("0"),
        captured_at=basis.captured_at,
        as_of=basis.as_of,
        available_at=basis.available_at,
        source=basis.source,
        source_version=basis.source_version,
        source_hash="",
        mark_refs=(),
        basis=basis,
        positions=tuple(positions),
        known=False,
        unknown_reason=reason.value,
    )
