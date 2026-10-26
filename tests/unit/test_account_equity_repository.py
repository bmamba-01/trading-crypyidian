"""Unit tests cho AccountEquityRepository (MB04.4).

Kiểm tra:
- Seed capital và realized PnL không có open position
- Realized PnL chỉ tính trade CLOSED trước as_of
- Open positions với mark hợp lệ và tính unrealized PnL
- Fail-closed khi missing mark
- Fail-closed khi stale mark
- Fail-closed khi mark future
- Fail-closed khi missing seed
- Fail-closed khi stale equity
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from trading_bot.adapters.database.repositories.account_equity import AccountEquityRepository
from trading_bot.domain.risk.equity import (
    EquityReasonCode,
    MarketMark,
)

WORKSPACE_ID = UUID("11111111-1111-4111-8111-111111111111")
BOT_ID = UUID("22222222-2222-4222-8222-222222222222")
ACCOUNT_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
MAX_MARK_AGE = timedelta(seconds=60)
MAX_EQUITY_AGE = timedelta(seconds=120)


class FakeSession:
    """Fake SQLAlchemy AsyncSession cho unit test repository."""

    def __init__(
        self,
        *,
        seed: object | None = None,
        realized_pnl: Decimal = Decimal("0"),
        open_trades: list[object] | None = None,
    ) -> None:
        self.seed = seed
        self.realized_pnl = realized_pnl
        self.open_trades = list(open_trades or [])
        self.statements: list[str] = []

    async def scalar(self, statement: object) -> object | None:
        stmt_text = str(statement).lower()
        self.statements.append(stmt_text)
        if "paper_account_seed" in stmt_text:
            if self.seed is not None and getattr(self.seed, "created_at", NOW) > NOW:
                return None
            return self.seed
        if "sum" in stmt_text and "net_pnl" in stmt_text:
            return self.realized_pnl
        return None

    async def scalars(self, statement: object) -> object:
        stmt_text = str(statement).lower()
        if "trade" in stmt_text:
            trades = self.open_trades

            class _Result:
                def all(self) -> list[object]:
                    return list(trades)

            return _Result()
        return SimpleNamespace(all=lambda: [])


def make_seed(initial_equity: Decimal = Decimal("10000.0")) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        trading_account_id=ACCOUNT_ID,
        quote_asset="USDT",
        initial_equity=initial_equity,
        created_at=NOW - timedelta(minutes=10),
    )


def make_mark(
    symbol: str = "BTCUSDT",
    price: Decimal = Decimal("60000.0"),
    as_of: datetime = NOW,
    available_at: datetime = NOW,
) -> MarketMark:
    return MarketMark(
        workspace_id=str(WORKSPACE_ID),
        bot_id=str(BOT_ID),
        account_id=str(ACCOUNT_ID),
        symbol=symbol,
        asset="USDT",
        price=price,
        as_of=as_of,
        available_at=available_at,
        source="binance_mark",
        source_version="v1",
        source_hash="a" * 64,
    )


def make_open_trade(
    symbol: str = "BTCUSDT",
    side: str = "LONG",
    quantity: Decimal = Decimal("1.5"),
    entry_price: Decimal = Decimal("58000.0"),
    opened_at: datetime = NOW - timedelta(minutes=5),
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        mode="PAPER",
        status="OPEN",
        symbol=symbol,
        quote_asset="USDT",
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        contract_multiplier=Decimal("1"),
        opened_at=opened_at,
        intent_id=uuid4(),
        plan_hash="b" * 64,
    )


@pytest.mark.asyncio
async def test_load_paper_equity_seed_only_no_trades() -> None:
    session = FakeSession(seed=make_seed(Decimal("50000")), realized_pnl=Decimal("0"))
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
    )

    assert snapshot.known is True
    assert snapshot.unknown_reason is None
    assert snapshot.equity_amount == Decimal("50000")
    assert snapshot.seed_capital == Decimal("50000")
    assert snapshot.cumulative_realized_pnl == Decimal("0")
    assert snapshot.unrealized_pnl == Decimal("0")
    assert snapshot.asset == "USDT"
    assert len(snapshot.positions) == 0


@pytest.mark.asyncio
async def test_future_paper_seed_is_not_available_at_decision_as_of() -> None:
    future_seed = make_seed()
    future_seed.created_at = NOW + timedelta(seconds=1)
    session = FakeSession(seed=future_seed)
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
    )

    assert snapshot.known is False
    assert snapshot.unknown_reason == EquityReasonCode.BASIS_UNKNOWN.value
    assert any("paper_account_seed.created_at <=" in query for query in session.statements)


@pytest.mark.asyncio
async def test_load_paper_equity_with_realized_pnl() -> None:
    session = FakeSession(
        seed=make_seed(Decimal("50000")),
        realized_pnl=Decimal("2500.50"),
    )
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
    )

    assert snapshot.known is True
    assert snapshot.equity_amount == Decimal("52500.50")
    assert snapshot.cumulative_realized_pnl == Decimal("2500.50")
    assert snapshot.unrealized_pnl == Decimal("0")


@pytest.mark.asyncio
async def test_load_paper_equity_with_open_position_long() -> None:
    # Seed 50000, Realized 1000
    # Open position: Long 1.5 BTC at entry 58000, Mark is 60000
    # Unrealized = (60000 - 58000) * 1.5 = 3000
    # Total = 50000 + 1000 + 3000 = 54000
    trade = make_open_trade(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("1.5"),
        entry_price=Decimal("58000"),
    )
    mark = make_mark(symbol="BTCUSDT", price=Decimal("60000"))

    session = FakeSession(
        seed=make_seed(Decimal("50000")),
        realized_pnl=Decimal("1000"),
        open_trades=[trade],
    )
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
        marks={"BTCUSDT": mark},
    )

    assert snapshot.known is True
    assert snapshot.equity_amount == Decimal("54000")
    assert snapshot.unrealized_pnl == Decimal("3000")
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_load_paper_equity_with_open_position_short() -> None:
    # Seed 50000, Realized 0
    # Short 2 BTC at entry 62000, Mark is 60000 -> gain (62000 - 60000) * 2 = 4000
    trade = make_open_trade(
        symbol="BTCUSDT",
        side="SHORT",
        quantity=Decimal("2"),
        entry_price=Decimal("62000"),
    )
    mark = make_mark(symbol="BTCUSDT", price=Decimal("60000"))

    session = FakeSession(
        seed=make_seed(Decimal("50000")),
        realized_pnl=Decimal("0"),
        open_trades=[trade],
    )
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
        marks={"BTCUSDT": mark},
    )

    assert snapshot.known is True
    assert snapshot.equity_amount == Decimal("54000")
    assert snapshot.unrealized_pnl == Decimal("4000")


@pytest.mark.asyncio
async def test_load_paper_equity_missing_mark_fails_closed() -> None:
    trade = make_open_trade(symbol="BTCUSDT")
    session = FakeSession(
        seed=make_seed(Decimal("50000")),
        open_trades=[trade],
    )
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
        marks={},  # Không có mark cho BTCUSDT
    )

    assert snapshot.known is False
    assert snapshot.unknown_reason == EquityReasonCode.MARK_MISSING.value


@pytest.mark.asyncio
async def test_load_paper_equity_stale_mark_fails_closed() -> None:
    trade = make_open_trade(symbol="BTCUSDT")
    stale_time = NOW - MAX_MARK_AGE - timedelta(seconds=1)
    stale_mark = make_mark(symbol="BTCUSDT", as_of=stale_time, available_at=stale_time)

    session = FakeSession(
        seed=make_seed(Decimal("50000")),
        open_trades=[trade],
    )
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
        marks={"BTCUSDT": stale_mark},
    )

    assert snapshot.known is False
    assert snapshot.unknown_reason == EquityReasonCode.MARK_STALE.value


@pytest.mark.asyncio
async def test_load_paper_equity_future_mark_fails_closed() -> None:
    trade = make_open_trade(symbol="BTCUSDT")
    future_time = NOW + timedelta(seconds=5)
    future_mark = make_mark(symbol="BTCUSDT", as_of=future_time, available_at=future_time)

    session = FakeSession(
        seed=make_seed(Decimal("50000")),
        open_trades=[trade],
    )
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
        marks={"BTCUSDT": future_mark},
    )

    assert snapshot.known is False
    assert snapshot.unknown_reason == EquityReasonCode.OBSERVATION_FUTURE.value


@pytest.mark.asyncio
async def test_load_paper_equity_missing_seed_fails_closed() -> None:
    session = FakeSession(seed=None)
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
    )

    assert snapshot.known is False
    assert snapshot.unknown_reason == EquityReasonCode.BASIS_UNKNOWN.value


@pytest.mark.asyncio
async def test_load_paper_equity_stale_equity_fails_closed() -> None:
    session = FakeSession(seed=make_seed(Decimal("50000")))
    repo = AccountEquityRepository(session)  # type: ignore[arg-type]

    stale_captured = NOW - MAX_EQUITY_AGE - timedelta(seconds=1)
    snapshot = await repo.load_paper_equity_snapshot(
        workspace_id=WORKSPACE_ID,
        bot_id=BOT_ID,
        account_id=ACCOUNT_ID,
        as_of=NOW,
        max_equity_age=MAX_EQUITY_AGE,
        max_mark_age=MAX_MARK_AGE,
        captured_at=stale_captured,
    )

    assert snapshot.known is False
    assert snapshot.unknown_reason == EquityReasonCode.EQUITY_STALE.value
