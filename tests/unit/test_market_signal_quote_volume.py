from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.application.market_signal_composition import _quote_volumes
from trading_bot.domain.market.models import OHLCVBar

NOW = datetime(2026, 11, 3, tzinfo=UTC)


def bar(index: int, quote_volume: str | None) -> OHLCVBar:
    open_time = NOW + timedelta(minutes=index)
    return OHLCVBar(
        symbol="BTCUSDT",
        timeframe="1m",
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        event_at=open_time + timedelta(minutes=1),
        received_at=open_time + timedelta(minutes=1),
        available_at=open_time + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
        quote_volume=None if quote_volume is None else Decimal(quote_volume),
        is_closed=True,
    )


def test_quote_volume_series_requires_complete_source_values() -> None:
    assert _quote_volumes((bar(0, "101.25"), bar(1, "204.5"))) == (101.25, 204.5)
    assert _quote_volumes((bar(0, "101.25"), bar(1, None))) is None
    assert _quote_volumes(()) is None
