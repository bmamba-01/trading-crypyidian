from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from trading_bot.adapters.database.models.entities import PaperAccountSeed, TradingAccount
from trading_bot.adapters.database.models.paper_entities import TradeRecord
from trading_bot.adapters.database.repositories.paper_account_state import (
    PaperAccountStateRepository,
)
from trading_bot.application.paper_account_state import (
    PaperAccountStateError,
    PaperAccountStateService,
)

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


class StateSession:
    def __init__(self, results: list[object | None]) -> None:
        self.results = iter(results)
        self.statements: list[str] = []

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return next(self.results)

    async def scalars(self, statement: object) -> SimpleNamespace:
        self.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(all=lambda: next(self.results))


def _account(*, workspace_id, account_id=None, mode="PAPER", status="ENABLED"):
    return TradingAccount(
        id=account_id or uuid4(),
        workspace_id=workspace_id,
        name="paper-default",
        venue="binance_usdm",
        mode=mode,
        environment="local",
        status=status,
        max_notional=Decimal("25000"),
    )


def _seed(*, workspace_id, account_id, quote_asset="USDT", initial_equity="10000"):
    return PaperAccountSeed(
        id=uuid4(),
        workspace_id=workspace_id,
        trading_account_id=account_id,
        quote_asset=quote_asset,
        initial_equity=Decimal(initial_equity),
        source_settings_version_id=uuid4(),
        source_settings_hash="a" * 64,
        idempotency_key="paper-seed:test",
        created_at=NOW - timedelta(days=2),
    )


def _trade(*, workspace_id, account_id, quote_asset="USDT", status="CLOSED", pnl="0"):
    return TradeRecord(
        id=uuid4(),
        workspace_id=workspace_id,
        account_id=account_id,
        mode="PAPER",
        intent_id=uuid4(),
        status=status,
        symbol="BTC/USDT:USDT",
        quote_asset=quote_asset,
        side="LONG",
        settings_version_id=uuid4(),
        strategy_release_id=uuid4(),
        strategy_release_hash="b" * 64,
        feature_snapshot_id="c" * 64,
        candidate_id="d" * 64,
        risk_decision_id=uuid4(),
        risk_reservation_id=uuid4(),
        gross_pnl=Decimal(pnl),
        fee_total=Decimal("0"),
        funding_total=Decimal("0"),
        slippage_total=Decimal("0"),
        total_cost=Decimal("0"),
        net_pnl=Decimal(pnl),
        opened_at=NOW - timedelta(hours=1),
        closed_at=NOW - timedelta(minutes=1) if status == "CLOSED" else None,
        lineage={},
    )


def _service(account, seed, trades, *, workspace_id):
    session = StateSession([account, seed, trades])
    return PaperAccountStateService(PaperAccountStateRepository(session)), session


@pytest.mark.asyncio
async def test_sums_only_closed_trade_net_pnl_and_returns_reproducible_lineage() -> None:
    workspace_id = uuid4()
    account = _account(workspace_id=workspace_id)
    seed = _seed(workspace_id=workspace_id, account_id=account.id)
    closed = _trade(workspace_id=workspace_id, account_id=account.id, pnl="12.50")
    open_trade = _trade(workspace_id=workspace_id, account_id=account.id, status="OPEN", pnl="99")
    service, session = _service(account, seed, [closed, open_trade], workspace_id=workspace_id)

    state = await service.get_state(workspace_id=workspace_id, account_id=account.id)

    assert state.quote_asset == "USDT"
    assert state.equity == Decimal("10012.50")
    assert state.closed_trade_count == 1
    assert state.closed_trade_ids == (closed.id,)
    assert state.as_of == closed.closed_at
    assert state.seed_id == seed.id
    assert len(state.state_hash) == 64
    assert all("workspace_id" in sql for sql in session.statements)
    assert all("account_id" in sql for sql in session.statements[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_mode", "account_status", "quote_asset", "trade_quote", "seed_present"),
    [
        ("LIVE", "ENABLED", "USDT", "USDT", True),
        ("PAPER", "DISABLED", "USDT", "USDT", True),
        ("PAPER", "ENABLED", "USDT", "USDC", True),
        ("PAPER", "ENABLED", "USDT", "USDT", False),
    ],
)
async def test_fails_closed_for_unsupported_or_inconsistent_account_state(
    account_mode, account_status, quote_asset, trade_quote, seed_present
) -> None:
    workspace_id = uuid4()
    account = _account(workspace_id=workspace_id, mode=account_mode, status=account_status)
    seed = (
        _seed(workspace_id=workspace_id, account_id=account.id, quote_asset=quote_asset)
        if seed_present
        else None
    )
    trades = [_trade(workspace_id=workspace_id, account_id=account.id, quote_asset=trade_quote)]
    service, _ = _service(account, seed, trades, workspace_id=workspace_id)

    with pytest.raises(PaperAccountStateError):
        await service.get_state(workspace_id=workspace_id, account_id=account.id)


@pytest.mark.asyncio
async def test_fails_closed_on_unknown_trade_state_or_nonfinite_ledger_value() -> None:
    workspace_id = uuid4()
    account = _account(workspace_id=workspace_id)
    seed = _seed(workspace_id=workspace_id, account_id=account.id)
    unknown = _trade(workspace_id=workspace_id, account_id=account.id, status="PENDING")
    service, _ = _service(account, seed, [unknown], workspace_id=workspace_id)

    with pytest.raises(PaperAccountStateError, match="trạng thái"):
        await service.get_state(workspace_id=workspace_id, account_id=account.id)

    malformed = _trade(workspace_id=workspace_id, account_id=account.id, pnl="NaN")
    service, _ = _service(account, seed, [malformed], workspace_id=workspace_id)
    with pytest.raises(PaperAccountStateError, match="hữu hạn"):
        await service.get_state(workspace_id=workspace_id, account_id=account.id)


@pytest.mark.asyncio
async def test_does_not_return_account_from_another_workspace() -> None:
    workspace_id = uuid4()
    other = _account(workspace_id=uuid4())
    service, _ = _service(other, None, [], workspace_id=workspace_id)

    with pytest.raises(PaperAccountStateError, match="không tồn tại"):
        await service.get_state(workspace_id=workspace_id, account_id=other.id)
