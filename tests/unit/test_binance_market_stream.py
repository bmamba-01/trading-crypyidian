from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from httpx2.websockets import ASGIWebSocketTransport

from trading_bot.adapters.binance_usdm.market_data import MarketDataContractError
from trading_bot.adapters.binance_usdm.market_stream import (
    BinanceUsdMKlineStream,
    KlineStreamDisconnected,
    build_kline_stream_url,
    parse_kline_stream_message,
)

OPEN = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
NOW = OPEN + timedelta(minutes=15, seconds=1)


def kline_message(
    *,
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    open_time: datetime = OPEN,
    closed: bool = True,
    stream: str | None = None,
) -> str:
    open_ms = int(open_time.timestamp() * 1000)
    close_ms = open_ms + 15 * 60 * 1000 - 1
    return json.dumps(
        {
            "stream": stream or f"{symbol.lower()}@kline_{interval}",
            "data": {
                "e": "kline",
                "E": close_ms + 5,
                "s": symbol,
                "k": {
                    "t": open_ms,
                    "T": close_ms,
                    "s": symbol,
                    "i": interval,
                    "f": 1,
                    "L": 9,
                    "o": "66764.30",
                    "c": "66773.80",
                    "h": "66774.00",
                    "l": "66751.50",
                    "v": "54.039",
                    "n": 1503,
                    "x": closed,
                    "q": "3608087.41430",
                    "V": "23.683",
                    "Q": "1581303.69040",
                    "B": "0",
                },
            },
        }
    )


def test_stream_url_uses_market_route_and_lowercase_combined_streams() -> None:
    url = build_kline_stream_url(
        "wss://fstream.binance.com",
        (("BTCUSDT", "15m"), ("ETHUSDT", "1h")),
    )

    assert url == (
        "wss://fstream.binance.com/market/stream?streams=btcusdt@kline_15m/ethusdt@kline_1h"
    )


@pytest.mark.parametrize(
    ("base_url", "subscriptions"),
    [
        ("ws://fstream.binance.com", (("BTCUSDT", "15m"),)),
        ("wss://fstream.binance.com/ws", (("BTCUSDT", "15m"),)),
        ("wss://fstream.binance.com", ()),
        ("wss://fstream.binance.com", (("btc/usdt", "15m"),)),
        ("wss://fstream.binance.com", (("BTCUSDT", "7m"),)),
        ("wss://fstream.binance.com", (("BTCUSDT", "15m"), ("BTCUSDT", "15m"))),
        ("wss://fstream.binance.com", tuple(("BTCUSDT", "15m") for _ in range(1025))),
    ],
)
def test_stream_url_rejects_unsafe_or_unsupported_input(
    base_url: str, subscriptions: tuple[tuple[str, str], ...]
) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        build_kline_stream_url(base_url, subscriptions)


def test_closed_kline_message_becomes_point_in_time_bar() -> None:
    raw = kline_message()

    bar = parse_kline_stream_message(raw, received_at=NOW)

    assert bar.symbol == "BTCUSDT"
    assert bar.timeframe == "15m"
    assert bar.open_time == OPEN
    assert bar.close_time == OPEN + timedelta(minutes=15) - timedelta(milliseconds=1)
    assert bar.is_closed is True
    assert bar.event_at == bar.close_time
    assert bar.received_at == NOW
    assert bar.available_at == NOW
    assert bar.close == Decimal("66773.80")
    assert bar.volume == Decimal("54.039")
    assert bar.quote_volume == Decimal("3608087.41430")
    assert bar.source == "BINANCE_USDM"
    assert bar.source_cursor == str(int(OPEN.timestamp() * 1000))
    assert bar.checksum == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_forming_kline_is_not_closed_and_never_claims_future_event_time() -> None:
    received = OPEN + timedelta(minutes=3)

    bar = parse_kline_stream_message(kline_message(closed=False), received_at=received)

    assert bar.is_closed is False
    assert bar.event_at <= received


def test_closed_flag_is_rejected_before_the_bar_close_time() -> None:
    with pytest.raises(MarketDataContractError):
        parse_kline_stream_message(kline_message(), received_at=OPEN + timedelta(minutes=1))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("stream"),
        lambda p: p["data"].__setitem__("e", "aggTrade"),
        lambda p: p["data"]["k"].__setitem__("x", "true"),
        lambda p: p["data"]["k"].__setitem__("o", "NaN"),
        lambda p: p["data"]["k"].__setitem__("s", "ETHUSDT"),
        lambda p: p["data"]["k"].__setitem__("i", "1h"),
        lambda p: p["data"]["k"].pop("q"),
        lambda p: p["data"]["k"].__setitem__("T", p["data"]["k"]["t"] - 1),
    ],
)
def test_malformed_or_mismatched_kline_fails_closed(mutate: Any) -> None:
    payload = json.loads(kline_message())
    mutate(payload)

    with pytest.raises(MarketDataContractError):
        parse_kline_stream_message(json.dumps(payload), received_at=NOW)


def test_non_json_message_fails_closed() -> None:
    with pytest.raises(MarketDataContractError):
        parse_kline_stream_message("not-json", received_at=NOW)


async def collect(stream: BinanceUsdMKlineStream, into: list[Any]) -> None:
    async for bar in stream.bars():
        into.append(bar)


def fake_binance(messages: list[str], *, paths: list[str], hold_open: bool = False) -> Any:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        assert scope["type"] == "websocket"
        paths.append(scope["path"] + "?" + scope["query_string"].decode("ascii"))
        await receive()
        await send({"type": "websocket.accept"})
        for message in messages:
            await send({"type": "websocket.send", "text": message})
        if hold_open:
            # Chờ client rời đi thay vì ngủ, để transport ASGI kết thúc sạch.
            await receive()
            return
        await send({"type": "websocket.close", "code": 1000, "reason": ""})

    return app


@pytest.mark.asyncio
async def test_stream_yields_bars_then_reports_disconnect() -> None:
    paths: list[str] = []
    messages = [kline_message(closed=False), kline_message()]
    transport = ASGIWebSocketTransport(fake_binance(messages, paths=paths))
    async with httpx2.AsyncClient(transport=transport) as client:
        stream = BinanceUsdMKlineStream(
            subscriptions=(("BTCUSDT", "15m"),),
            client=client,
            clock=lambda: NOW,
        )
        bars: list[Any] = []
        with pytest.raises(KlineStreamDisconnected):
            await collect(stream, bars)

    assert [bar.is_closed for bar in bars] == [False, True]
    assert paths == ["/market/stream?streams=btcusdt@kline_15m"]


@pytest.mark.asyncio
async def test_stream_treats_silence_as_disconnect() -> None:
    paths: list[str] = []
    transport = ASGIWebSocketTransport(fake_binance([], paths=paths, hold_open=True))
    async with httpx2.AsyncClient(transport=transport) as client:
        stream = BinanceUsdMKlineStream(
            subscriptions=(("BTCUSDT", "15m"),),
            client=client,
            clock=lambda: NOW,
            idle_timeout_seconds=0.05,
        )
        bars: list[Any] = []
        with pytest.raises(KlineStreamDisconnected):
            await collect(stream, bars)

    assert bars == []


@pytest.mark.asyncio
async def test_stream_drops_bar_for_unsubscribed_stream() -> None:
    paths: list[str] = []
    messages = [kline_message(symbol="ETHUSDT")]
    transport = ASGIWebSocketTransport(fake_binance(messages, paths=paths))
    async with httpx2.AsyncClient(transport=transport) as client:
        stream = BinanceUsdMKlineStream(
            subscriptions=(("BTCUSDT", "15m"),),
            client=client,
            clock=lambda: NOW,
        )
        bars: list[Any] = []
        with pytest.raises(MarketDataContractError):
            await collect(stream, bars)

    assert bars == []


def test_closed_kline_tolerates_small_local_clock_lag_without_future_timestamps() -> None:
    close_time = OPEN + timedelta(minutes=15) - timedelta(milliseconds=1)
    lagging_clock = close_time - timedelta(milliseconds=400)

    bar = parse_kline_stream_message(kline_message(), received_at=lagging_clock)

    assert bar.is_closed is True
    assert bar.event_at == close_time
    # Mốc nhận được đẩy lên ngay sau thời điểm đóng để giữ event_at <= received_at.
    assert bar.received_at == close_time + timedelta(milliseconds=1)
    assert bar.available_at == bar.received_at


def test_closed_kline_beyond_clock_tolerance_is_rejected() -> None:
    close_time = OPEN + timedelta(minutes=15) - timedelta(milliseconds=1)

    with pytest.raises(MarketDataContractError):
        parse_kline_stream_message(kline_message(), received_at=close_time - timedelta(seconds=6))
