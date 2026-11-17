"""FX-3 lát 1: thu mức funding đã chốt của Binance USDⓈ-M vào ``market_funding`` (CRY-198)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from trading_bot.application.binance_rest_backfill import BackfillRightsPurpose
from trading_bot.application.funding_rate_collector import (
    FUNDING_CURSOR_SOURCE,
    FUNDING_RECORD_SOURCE,
    FundingRateCollector,
    funding_record_version,
)
from trading_bot.ports.market_data import FundingRateObservation, MarketDataPage

WORKSPACE = UUID("9b2299f1-a58e-45c7-b0ad-83feb0862510")
NOW = datetime(2026, 11, 17, 9, 3, tzinfo=UTC)
SETTLED = [
    NOW.replace(hour=hour, minute=0) - timedelta(days=day) for day in (1, 0) for hour in (0, 8)
]


def _observation(
    symbol: str, at: datetime, *, mark: str | None = "100.5"
) -> FundingRateObservation:
    received = NOW
    return FundingRateObservation(
        symbol=symbol,
        funding_time=at,
        funding_rate=Decimal("0.0001"),
        mark_price=None if mark is None else Decimal(mark),
        rate_type="Regular",
        received_at=received,
        available_at=received,
    )


class FakeBinance:
    def __init__(self, published: list[datetime], *, fail: set[str] | None = None) -> None:
        self.published = published
        self.calls: list[tuple[str, datetime | None, datetime | None]] = []
        self.fail = fail or set()

    async def get_funding_rates(
        self,
        *,
        symbol: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> MarketDataPage[FundingRateObservation]:
        self.calls.append((symbol, start_time, end_time))
        if symbol in self.fail:
            raise RuntimeError("network")
        records = tuple(
            _observation(symbol, at)
            for at in self.published
            if (start_time is None or at >= start_time) and (end_time is None or at <= end_time)
        )
        return MarketDataPage(
            records=records,
            raw_payload=b"[]",
            checksum="0" * 64,
            received_at=NOW,
            available_at=NOW,
        )


@dataclass
class FakeMarket:
    cursors: dict[str, Any] = field(default_factory=dict)
    appended: list[dict[str, Any]] = field(default_factory=list)

    async def get_cursor(self, *, workspace_id: UUID, stream: Any, as_of: datetime) -> Any:
        return self.cursors.get(stream.symbol)

    async def save_cursor(self, *, workspace_id: UUID, cursor: Any) -> None:
        assert cursor.stream.source == FUNDING_CURSOR_SOURCE
        self.cursors[cursor.stream.symbol] = cursor

    async def get_instrument(self, **_: Any) -> Any:
        return SimpleNamespace(id=UUID("00000000-0000-4000-8000-000000000001"))

    async def append_funding_rate(self, **kwargs: Any) -> Any:
        self.appended.append(kwargs)
        return SimpleNamespace(record_id=kwargs["version"].record_id)


class FakeGate:
    def __init__(self, *, allow: bool = True) -> None:
        self.allow = allow
        self.requests: list[Any] = []

    async def authorize(self, *, request: Any, now: datetime) -> Any:
        self.requests.append(request)
        if not self.allow:
            raise PermissionError("thiếu quyền")
        return SimpleNamespace()


def _collector(binance: FakeBinance, market: FakeMarket, gate: FakeGate, symbols=("BTCUSDT",)):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield SimpleNamespace(market=market, rights_gate=gate)

    return FundingRateCollector(
        workspace_id=WORKSPACE,
        symbols=symbols,
        market_data=binance,  # type: ignore[arg-type]
        session_scope=scope,
    )


@pytest.mark.asyncio
async def test_first_tick_backfills_lookback_and_saves_cursor_after_last_settlement() -> None:
    binance, market, gate = FakeBinance(SETTLED), FakeMarket(), FakeGate()
    tick = await _collector(binance, market, gate).collect_once(NOW)
    (result,) = tick.results
    assert (result.symbol, result.status, result.records) == ("BTCUSDT", "collected", 4)
    assert [row["funding_time"] for row in market.appended] == SETTLED
    first = market.appended[0]
    assert first["mark_price"] == Decimal("100.5")
    assert first["rate_type"] == "Regular"
    assert first["version"].stream.source == FUNDING_RECORD_SOURCE
    assert first["version"].event_at == SETTLED[0]
    # Rights được kiểm trước khi gọi mạng, đúng mục đích PAPER nội bộ.
    assert gate.requests[0].purpose is BackfillRightsPurpose.PAPER_DEMO
    assert binance.calls[0][1] <= SETTLED[0]
    assert market.cursors["BTCUSDT"].covered_until == SETTLED[-1] + timedelta(milliseconds=1)
    assert tick.succeeded


@pytest.mark.asyncio
async def test_next_tick_reads_only_after_cursor_and_is_idempotent() -> None:
    binance, market, gate = FakeBinance(SETTLED), FakeMarket(), FakeGate()
    collector = _collector(binance, market, gate)
    await collector.collect_once(NOW)
    second = await collector.collect_once(NOW + timedelta(minutes=5))
    assert second.results[0].status == "up_to_date"
    assert len(market.appended) == 4
    assert binance.calls[-1][1] == SETTLED[-1] + timedelta(milliseconds=1)
    # Cùng kỳ funding luôn cho cùng record id/khóa: ghi lại là DUPLICATE ở repository.
    again, _ = funding_record_version(WORKSPACE, _observation("BTCUSDT", SETTLED[0]))
    assert again.idempotency_key == market.appended[0]["version"].idempotency_key
    assert again.record_id == market.appended[0]["version"].record_id


@pytest.mark.asyncio
async def test_missing_rights_skips_network_and_one_symbol_error_does_not_block_others() -> None:
    binance, market = FakeBinance(SETTLED), FakeMarket()
    denied = await _collector(binance, market, FakeGate(allow=False)).collect_once(NOW)
    assert denied.results[0].status == "rights_denied"
    assert binance.calls == []
    assert not denied.succeeded

    flaky = FakeBinance(SETTLED, fail={"ETHUSDT"})
    tick = await _collector(
        flaky, FakeMarket(), FakeGate(), symbols=("ETHUSDT", "BTCUSDT")
    ).collect_once(NOW)
    assert [(r.symbol, r.status) for r in tick.results] == [
        ("ETHUSDT", "error"),
        ("BTCUSDT", "collected"),
    ]


def test_funding_without_mark_price_keeps_null_and_checksum_differs() -> None:
    with_mark, _ = funding_record_version(WORKSPACE, _observation("BTCUSDT", SETTLED[0]))
    without, mark = funding_record_version(
        WORKSPACE, _observation("BTCUSDT", SETTLED[0], mark=None)
    )
    assert mark is None
    assert with_mark.idempotency_key == without.idempotency_key
    assert with_mark.content_checksum != without.content_checksum
