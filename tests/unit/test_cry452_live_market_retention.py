"""Kiểm thử đơn vị cho chính sách retention 90 ngày của WP-07B (CRY-452)."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models.market_entities import (
    MarketFunding,
    MarketLiquidation,
    MarketTradeFlow,
)
from trading_bot.adapters.database.repositories.market_data import (
    DEFAULT_LIVE_MARKET_RETENTION,
    MarketDataRepository,
)
from trading_bot.domain.market.ingestion import (
    MarketRecordVersion,
    MarketStream,
)
from trading_bot.domain.market.stream_events import LiquidationEvent
from trading_bot.domain.market.trade_flow import TradeFlowBucket

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "migrations/versions/c9d0e1f2a3b4_cry452_live_market_retention.py"


def test_cry452_models_have_expires_at_and_index() -> None:
    for model, index_name in (
        (MarketFunding, "ix_market_funding_expires"),
        (MarketLiquidation, "ix_market_liquidation_expires"),
        (MarketTradeFlow, "ix_market_trade_flow_expires"),
    ):
        assert hasattr(model, "expires_at")
        column = model.__table__.c.expires_at
        assert column.nullable is False
        index_names = {idx.name for idx in model.__table__.indexes}
        assert index_name in index_names


def test_cry452_migration_shape_and_sole_head() -> None:
    assert MIGRATION_PATH.exists()
    spec = importlib.util.spec_from_file_location("cry452_migration", MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.down_revision) == {"ab26dde001df", "e7f8a9b0c1d2"}

    source = MIGRATION_PATH.read_text(encoding="utf-8")
    for table in ("market_funding", "market_liquidation", "market_trade_flow"):
        assert table in source
    assert "enforce_live_market_retention_immutability" in source
    assert "OLD.expires_at <= clock_timestamp()" in source
    assert "GRANT DELETE" in source
    assert "crypidian_ingestion_worker" in source


@pytest.mark.asyncio
async def test_cry452_writers_assign_90_day_expires_at() -> None:
    captured: list[object] = []
    executed: list[object] = []
    session = MagicMock()

    def _handle_scalar(stmt: object) -> object:
        if getattr(stmt, "is_insert", False):
            executed.append(stmt)
            return uuid4()
        return None

    session.scalar = AsyncMock(side_effect=_handle_scalar)
    session.add = lambda row: captured.append(row)
    session.flush = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = uuid4()
    session.execute = AsyncMock(side_effect=lambda stmt: executed.append(stmt) or exec_result)
    nested = AsyncMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)

    repo = MarketDataRepository(session)
    ws = uuid4()
    inst = uuid4()

    funding_time = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)
    version = MarketRecordVersion(
        record_id=uuid4(),
        stream=MarketStream(source="binance_usdm_rest", symbol="BTCUSDT", timeframe="8h"),
        logical_key="funding:BTCUSDT:2026-11-25T08:00:00Z",
        idempotency_key="funding-1",
        revision=1,
        content_checksum="a" * 64,
        event_at=funding_time,
        received_at=funding_time + timedelta(seconds=1),
        available_at=funding_time + timedelta(seconds=2),
    )
    await repo.append_funding_rate(
        workspace_id=ws,
        instrument_id=inst,
        version=version,
        funding_time=funding_time,
        funding_rate=Decimal("0.0001"),
    )

    trade_time = datetime(2026, 11, 25, 8, 5, tzinfo=UTC)
    liq = LiquidationEvent(
        symbol="BTCUSDT",
        event_at=trade_time,
        received_at=trade_time + timedelta(milliseconds=100),
        trade_time=trade_time,
        side="SELL",
        order_type="LIMIT",
        time_in_force="IOC",
        status="FILLED",
        price=Decimal("90000"),
        average_price=Decimal("90000"),
        quantity=Decimal("0.5"),
        filled_quantity=Decimal("0.5"),
        checksum="b" * 64,
    )
    await repo.append_liquidation(
        workspace_id=ws,
        instrument_id=inst,
        source="binance_usdm_ws",
        idempotency_key="liq-1",
        event=liq,
    )

    bucket_start = datetime(2026, 11, 25, 8, 6, tzinfo=UTC)
    bucket_end = bucket_start + timedelta(minutes=1)
    bucket = TradeFlowBucket(
        symbol="BTCUSDT",
        bucket_start=bucket_start,
        closed_at=bucket_end,
        bucket_seconds=60,
        buy_quantity=Decimal("1"),
        sell_quantity=Decimal("2"),
        buy_quote=Decimal("90000"),
        sell_quote=Decimal("180000"),
        trade_count=3,
        first_aggregate_id=10,
        last_aggregate_id=12,
        is_complete=True,
    )
    await repo.append_trade_flow(
        workspace_id=ws,
        instrument_id=inst,
        source="binance_usdm_ws",
        idempotency_key="flow-1",
        checksum="c" * 64,
        bucket=bucket,
    )

    assert len(captured) == 1
    funding_row = captured[0]
    assert isinstance(funding_row, MarketFunding)
    assert funding_row.expires_at == funding_time + DEFAULT_LIVE_MARKET_RETENTION

    assert len(executed) == 2
    liq_params = executed[0].compile().params
    flow_params = executed[1].compile().params
    assert liq_params["expires_at"] == trade_time + DEFAULT_LIVE_MARKET_RETENTION
    assert flow_params["expires_at"] == bucket_end + DEFAULT_LIVE_MARKET_RETENTION
