"""Kiểm thử đơn vị cho CRY-453 (retention mark price/OI & prune ở FundingRateCollector)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from trading_bot.adapters.database.models.market_entities import (
    MarketMarkPrice,
    MarketOpenInterest,
)
from trading_bot.adapters.database.repositories.market_data import (
    DEFAULT_LIVE_MARKET_RETENTION,
    MarketDataRepository,
)
from trading_bot.application.funding_rate_collector import FundingRateCollector
from trading_bot.domain.market.ingestion import MarketRecordVersion, MarketStream
from trading_bot.domain.market.stream_events import MarkPriceEvent
from trading_bot.ports.market_data import MarketDataPage

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "migrations/versions/d0e1f2a3b4c5_cry453_mark_price_oi_retention.py"


def test_cry453_models_and_migration_shape() -> None:
    for model, index_name in (
        (MarketMarkPrice, "ix_market_mark_price_expires"),
        (MarketOpenInterest, "ix_market_open_interest_expires"),
    ):
        assert hasattr(model, "expires_at")
        assert model.__table__.c.expires_at.nullable is False
        assert index_name in {idx.name for idx in model.__table__.indexes}

    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "market_mark_price" in source
    assert "market_open_interest" in source
    assert "enforce_live_market_retention_immutability" in source
    assert "GRANT DELETE" in source
    assert "crypidian_ingestion_worker" in source


@pytest.mark.asyncio
async def test_cry453_writers_assign_90_day_expires_at() -> None:
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
    nested = AsyncMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=nested)

    repo = MarketDataRepository(session)
    ws = uuid4()
    inst = uuid4()

    observed_at = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)
    version = MarketRecordVersion(
        record_id=uuid4(),
        stream=MarketStream(source="binance_usdm_rest", symbol="BTCUSDT", timeframe="5m"),
        logical_key="oi:BTCUSDT:2026-11-25T09:00:00Z",
        idempotency_key="oi-1",
        revision=1,
        content_checksum="d" * 64,
        event_at=observed_at,
        received_at=observed_at + timedelta(seconds=1),
        available_at=observed_at + timedelta(seconds=2),
    )
    await repo.append_open_interest(
        workspace_id=ws,
        instrument_id=inst,
        version=version,
        observed_at=observed_at,
        quantity=Decimal("12345.6"),
    )

    event_at = datetime(2026, 11, 25, 9, 1, tzinfo=UTC)
    mark = MarkPriceEvent(
        symbol="BTCUSDT",
        event_at=event_at,
        received_at=event_at + timedelta(milliseconds=50),
        mark_price=Decimal("90000"),
        index_price=Decimal("89990"),
        funding_rate=Decimal("0.0001"),
        next_funding_time=event_at + timedelta(hours=4),
        checksum="e" * 64,
    )
    await repo.append_mark_price(
        workspace_id=ws,
        instrument_id=inst,
        source="binance_usdm_ws",
        idempotency_key="mark-1",
        event=mark,
    )

    assert len(captured) == 1
    oi_row = captured[0]
    assert isinstance(oi_row, MarketOpenInterest)
    assert oi_row.expires_at == observed_at + DEFAULT_LIVE_MARKET_RETENTION

    assert len(executed) == 1
    mark_params = executed[0].compile().params
    assert mark_params["expires_at"] == event_at + DEFAULT_LIVE_MARKET_RETENTION


@pytest.mark.asyncio
async def test_cry453_funding_collector_prunes_on_interval() -> None:
    ws = uuid4()
    prune_calls: list[datetime] = []

    market = MagicMock()
    market.get_cursor = AsyncMock(return_value=None)
    market.prune_expired_live_market = AsyncMock(
        side_effect=lambda *, workspace_id, now: (
            prune_calls.append(now) or {"market_funding": 1}
        )
    )
    rights_gate = MagicMock()
    rights_gate.authorize = AsyncMock()
    session_obj = MagicMock(market=market, rights_gate=rights_gate)

    @asynccontextmanager
    async def scope():
        yield session_obj

    t0 = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    market_data = MagicMock()
    market_data.get_funding_rates = AsyncMock(
        return_value=MarketDataPage(
            records=(),
            raw_payload=b"{}",
            checksum="f" * 64,
            received_at=t0,
            available_at=t0,
        )
    )

    collector = FundingRateCollector(
        workspace_id=ws,
        symbols=["BTCUSDT"],
        market_data=market_data,
        session_scope=scope,
        prune_interval=timedelta(hours=1),
    )

    await collector.collect_once(t0)
    await collector.collect_once(t0 + timedelta(minutes=15))
    await collector.collect_once(t0 + timedelta(hours=1, minutes=1))

    assert len(prune_calls) == 2
    assert prune_calls[0] == t0
    assert prune_calls[1] == t0 + timedelta(hours=1, minutes=1)
