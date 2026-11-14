from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_bot.domain.market.stream_events import AggTradeEvent
from trading_bot.domain.market.trade_flow import TradeFlowAggregator

T0 = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)


def trade(
    aggregate_id: int,
    offset_seconds: float,
    *,
    quantity: str = "1",
    price: str = "100",
    taker_buys: bool = True,
    symbol: str = "BTCUSDT",
) -> AggTradeEvent:
    at = T0 + timedelta(seconds=offset_seconds)
    return AggTradeEvent(
        symbol=symbol,
        aggregate_id=aggregate_id,
        price=Decimal(price),
        quantity=Decimal(quantity),
        first_trade_id=aggregate_id * 10,
        last_trade_id=aggregate_id * 10,
        trade_time=at,
        buyer_is_maker=not taker_buys,
        received_at=at + timedelta(milliseconds=50),
    )


def test_bucket_closes_when_a_trade_of_the_next_minute_arrives() -> None:
    aggregator = TradeFlowAggregator()
    assert aggregator.add(trade(1, 59)) == ()
    # Bucket đầu tiên sau khi khởi động luôn là một phần nên bị bỏ qua ở đây.
    assert len(aggregator.add(trade(2, 61, quantity="2"))) == 1
    assert aggregator.add(trade(3, 70, quantity="3", taker_buys=False, price="101")) == ()

    (bucket,) = aggregator.add(trade(4, 121))

    assert bucket.symbol == "BTCUSDT"
    assert bucket.bucket_start == T0 + timedelta(minutes=1)
    assert bucket.bucket_seconds == 60
    assert bucket.buy_quantity == Decimal("2")
    assert bucket.sell_quantity == Decimal("3")
    assert bucket.buy_quote == Decimal("200")
    assert bucket.sell_quote == Decimal("303")
    assert bucket.trade_count == 2
    assert (bucket.first_aggregate_id, bucket.last_aggregate_id) == (2, 3)
    assert bucket.is_complete is True
    assert bucket.imbalance == Decimal("-103") / Decimal("503")
    assert bucket.closed_at == T0 + timedelta(seconds=121, milliseconds=50)


def test_first_bucket_after_start_is_incomplete() -> None:
    aggregator = TradeFlowAggregator()
    aggregator.add(trade(1, 30))

    (bucket,) = aggregator.add(trade(2, 65))

    assert bucket.bucket_start == T0
    assert bucket.is_complete is False


def test_missing_aggregate_id_marks_bucket_incomplete() -> None:
    aggregator = TradeFlowAggregator()
    aggregator.add(trade(1, 59))
    aggregator.add(trade(2, 61))
    aggregator.add(trade(4, 62))

    (bucket,) = aggregator.add(trade(5, 121))

    assert bucket.is_complete is False
    assert bucket.trade_count == 2


def test_gap_at_bucket_boundary_marks_the_closing_bucket_incomplete() -> None:
    aggregator = TradeFlowAggregator()
    aggregator.add(trade(1, 59))
    aggregator.add(trade(2, 61))

    (bucket,) = aggregator.add(trade(9, 121))

    assert bucket.is_complete is False


def test_duplicate_or_older_trade_is_ignored() -> None:
    aggregator = TradeFlowAggregator()
    aggregator.add(trade(1, 59))
    aggregator.add(trade(2, 61))
    assert aggregator.add(trade(2, 61)) == ()
    assert aggregator.add(trade(1, 59)) == ()

    (bucket,) = aggregator.add(trade(3, 121))

    assert bucket.trade_count == 1
    assert bucket.is_complete is True


def test_flush_closes_quiet_buckets_after_grace_and_keeps_symbols_separate() -> None:
    aggregator = TradeFlowAggregator()
    aggregator.add(trade(1, 59))
    aggregator.add(trade(2, 61))
    aggregator.add(trade(7, 62, symbol="ETHUSDT"))

    assert aggregator.flush(T0 + timedelta(seconds=121)) == ()
    buckets = aggregator.flush(T0 + timedelta(seconds=123))

    assert [(bucket.symbol, bucket.is_complete) for bucket in buckets] == [
        ("BTCUSDT", True),
        ("ETHUSDT", False),
    ]
    assert aggregator.flush(T0 + timedelta(seconds=200)) == ()
    # Bucket sau khoảng lặng vẫn đầy đủ nếu id nối tiếp.
    aggregator.add(trade(3, 190))
    (later,) = aggregator.add(trade(4, 245))
    assert later.bucket_start == T0 + timedelta(minutes=3)
    assert later.is_complete is True


def test_reset_emits_open_buckets_as_incomplete_and_forgets_continuity() -> None:
    aggregator = TradeFlowAggregator()
    aggregator.add(trade(1, 59))
    aggregator.add(trade(2, 61))

    (partial,) = aggregator.reset(T0 + timedelta(seconds=70))

    assert partial.is_complete is False
    assert partial.closed_at == T0 + timedelta(seconds=70)
    aggregator.add(trade(3, 80))
    (after,) = aggregator.add(trade(4, 125))
    assert after.is_complete is False


def test_bucket_seconds_must_divide_a_minute() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        TradeFlowAggregator(bucket_seconds=7)
