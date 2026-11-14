from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from httpx2.websockets import ASGIWebSocketTransport

from trading_bot.adapters.binance_usdm.market_data import MarketDataContractError
from trading_bot.adapters.binance_usdm.market_stream import (
    BinanceUsdMEventStream,
    KlineStreamDisconnected,
    build_event_stream_names,
    parse_market_event_message,
)
from trading_bot.domain.market.stream_events import (
    AggTradeEvent,
    LiquidationEvent,
    MarkPriceEvent,
)

SAMPLES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "binance_usdm" / "stream_events.json").read_text()
)
NOW = datetime(2026, 11, 14, 15, 7, 10, tzinfo=UTC)


def raw(name: str) -> str:
    return json.dumps(SAMPLES[name])


def test_stream_names_cover_each_symbol_and_kind() -> None:
    names = build_event_stream_names(
        ("BTCUSDT", "ETHUSDT"), ("mark_price", "liquidation", "trade_flow")
    )

    assert names == (
        "btcusdt@markPrice@1s",
        "btcusdt@forceOrder",
        "btcusdt@aggTrade",
        "ethusdt@markPrice@1s",
        "ethusdt@forceOrder",
        "ethusdt@aggTrade",
    )


@pytest.mark.parametrize(
    ("symbols", "kinds"),
    [
        ((), ("mark_price",)),
        (("BTCUSDT",), ()),
        (("btcusdt",), ("mark_price",)),
        (("BTCUSDT",), ("depth",)),
        (("BTCUSDT", "BTCUSDT"), ("mark_price",)),
    ],
)
def test_stream_names_reject_invalid_input(symbols: Any, kinds: Any) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        build_event_stream_names(symbols, kinds)


def test_mark_price_message() -> None:
    event = parse_market_event_message(raw("mark_price"), received_at=NOW)

    assert isinstance(event, MarkPriceEvent)
    assert event.symbol == "BTCUSDT"
    assert event.mark_price == Decimal("65812.20000000")
    assert event.index_price == Decimal("65836.28782609")
    assert event.funding_rate == Decimal("0.00003251")
    assert event.next_funding_time == datetime(2026, 11, 14, 16, 0, tzinfo=UTC)
    assert event.event_at == datetime(2026, 11, 14, 15, 6, 48, tzinfo=UTC)
    assert event.received_at == NOW
    assert len(event.checksum) == 64


def test_liquidation_message() -> None:
    event = parse_market_event_message(raw("liquidation"), received_at=NOW + timedelta(minutes=1))

    assert isinstance(event, LiquidationEvent)
    assert event.symbol == "KAVAUSDT"
    assert event.side == "SELL"
    assert (event.order_type, event.time_in_force, event.status) == ("LIMIT", "IOC", "FILLED")
    assert event.price == Decimal("0.030830")
    assert event.average_price == Decimal("0.031261")
    assert event.quantity == Decimal("18163.6")
    assert event.filled_quantity == Decimal("18163.6")
    assert event.notional == Decimal("0.031261") * Decimal("18163.6")
    assert event.trade_time < event.received_at


def test_agg_trade_message() -> None:
    event = parse_market_event_message(raw("agg_trade"), received_at=NOW)

    assert isinstance(event, AggTradeEvent)
    assert event.aggregate_id == 3522214828
    assert event.taker_side == "BUY"
    assert event.quote_quantity == Decimal("65804.10") * Decimal("0.002")


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("mark_price", lambda d: d["data"].__setitem__("p", "0")),
        ("mark_price", lambda d: d["data"].__setitem__("s", "ETHUSDT")),
        ("mark_price", lambda d: d.__setitem__("stream", "btcusdt@markPrice")),
        ("liquidation", lambda d: d["data"]["o"].__setitem__("S", "HOLD")),
        ("liquidation", lambda d: d["data"]["o"].pop("z")),
        ("agg_trade", lambda d: d["data"].__setitem__("m", "false")),
        ("agg_trade", lambda d: d["data"].__setitem__("q", "-1")),
        ("agg_trade", lambda d: d["data"].__setitem__("e", "trade")),
    ],
)
def test_malformed_event_fails_closed(name: str, mutate: Any) -> None:
    payload = json.loads(raw(name))
    mutate(payload)

    with pytest.raises(MarketDataContractError):
        parse_market_event_message(json.dumps(payload), received_at=NOW + timedelta(minutes=1))


def test_event_far_in_the_future_is_rejected() -> None:
    with pytest.raises(MarketDataContractError):
        parse_market_event_message(raw("mark_price"), received_at=NOW - timedelta(minutes=5))


def fake_binance(messages: list[str], paths: list[str]) -> Any:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        paths.append(scope["path"] + "?" + scope["query_string"].decode("ascii"))
        await receive()
        await send({"type": "websocket.accept"})
        for message in messages:
            await send({"type": "websocket.send", "text": message})
        await send({"type": "websocket.close", "code": 1000, "reason": ""})

    return app


async def collect(stream: BinanceUsdMEventStream, into: list[Any]) -> None:
    async for event in stream.events():
        into.append(event)


@pytest.mark.asyncio
async def test_event_stream_yields_typed_events_on_market_route() -> None:
    paths: list[str] = []
    liquidation = json.loads(raw("liquidation"))
    liquidation["stream"] = "btcusdt@forceOrder"
    liquidation["data"]["o"]["s"] = "BTCUSDT"
    messages = [raw("mark_price"), raw("agg_trade"), json.dumps(liquidation)]
    transport = ASGIWebSocketTransport(fake_binance(messages, paths))
    events: list[Any] = []
    async with httpx2.AsyncClient(transport=transport) as client:
        stream = BinanceUsdMEventStream(
            symbols=("BTCUSDT",),
            kinds=("mark_price", "liquidation", "trade_flow"),
            client=client,
            clock=lambda: NOW + timedelta(minutes=1),
        )
        with pytest.raises(KlineStreamDisconnected):
            await collect(stream, events)

    assert [type(event) for event in events] == [MarkPriceEvent, AggTradeEvent, LiquidationEvent]
    assert paths == [
        "/market/stream?streams=btcusdt@markPrice@1s/btcusdt@forceOrder/btcusdt@aggTrade"
    ]


@pytest.mark.asyncio
async def test_event_stream_rejects_event_for_unsubscribed_symbol() -> None:
    paths: list[str] = []
    transport = ASGIWebSocketTransport(fake_binance([raw("liquidation")], paths))
    events: list[Any] = []
    async with httpx2.AsyncClient(transport=transport) as client:
        stream = BinanceUsdMEventStream(
            symbols=("BTCUSDT",),
            kinds=("liquidation",),
            client=client,
            clock=lambda: NOW + timedelta(minutes=1),
        )
        with pytest.raises(MarketDataContractError):
            await collect(stream, events)

    assert events == []
