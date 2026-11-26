"""Đọc kline USDⓈ-M qua WebSocket route `/market`; chỉ dữ liệu công khai, không dùng API key."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncGenerator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import InvalidOperation
from urllib.parse import urlsplit

import httpx2
from httpx2.websockets import HTTPXWSException

from trading_bot.adapters.binance_usdm.market_data import (
    _FIXED_INTERVALS,
    MarketDataContractError,
    _decimal,
    _from_milliseconds,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.domain.market.stream_events import (
    AggTradeEvent,
    BookTickerEvent,
    LiquidationEvent,
    MarketStreamEvent,
    MarkPriceEvent,
)

BINANCE_USDM_MARKET_STREAM_URL = "wss://fstream.binance.com"
# Giới hạn do Binance công bố cho một kết nối; vượt mức phải tách kết nối.
MAX_STREAMS_PER_CONNECTION = 1024
_SYMBOL = re.compile(r"[A-Z0-9]{3,32}\Z")
_SOURCE = "BINANCE_USDM"
# Stream kline đẩy cập nhật mỗi 250 ms; im lặng lâu hơn mức này là kết nối đã chết.
_DEFAULT_IDLE_TIMEOUT_SECONDS = 60.0
_MAX_MESSAGE_BYTES = 64 * 1024
_MILLISECOND = timedelta(milliseconds=1)
# Mức lệch đồng hồ tối đa được chấp nhận cho sự kiện đóng nến; lớn hơn là dữ liệu sai.
_CLOCK_LAG_TOLERANCE = timedelta(seconds=5)

KlineSubscription = tuple[str, str]


class KlineStreamDisconnected(ConnectionError):
    """Kết nối stream đã đóng, lỗi mạng hoặc im lặng quá hạn; caller phải nối lại và lấp gap."""


def build_kline_stream_url(base_url: str, subscriptions: Sequence[KlineSubscription]) -> str:
    """Dựng URL combined stream trên route `/market`.

    Từ 2026-03-05 Binance chỉ đẩy kline qua `/market`; URL `/stream` cũ không còn nhận dữ liệu.
    """
    parts = urlsplit(base_url)
    if parts.scheme != "wss" or not parts.netloc or parts.path not in {"", "/"} or parts.query:
        raise ValueError("base_url stream phải là origin wss không kèm path hoặc query.")
    if not 1 <= len(subscriptions) <= MAX_STREAMS_PER_CONNECTION:
        raise ValueError("Số stream mỗi kết nối phải nằm trong 1..1024.")
    names: list[str] = []
    for symbol, interval in subscriptions:
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("symbol stream phải là mã USD-M viết hoa, không chứa separator.")
        if interval not in _FIXED_INTERVALS:
            raise ValueError("interval stream phải có độ dài cố định được hỗ trợ.")
        names.append(f"{symbol.lower()}@kline_{interval}")
    if len(set(names)) != len(names):
        raise ValueError("Subscription stream không được trùng lặp.")
    return _stream_url(base_url, names)


def parse_kline_stream_message(message: str, *, received_at: datetime) -> OHLCVBar:
    """Chuyển một message combined-stream kline thành bar; sai hợp đồng thì từ chối."""
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at phải có múi giờ.")
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise MarketDataContractError("Message stream không phải JSON hợp lệ.") from error
    try:
        stream_name = payload["stream"]
        data = payload["data"]
        kline = data["k"]
        if data["e"] != "kline" or not isinstance(stream_name, str):
            raise MarketDataContractError("Message stream không phải sự kiện kline.")
        symbol = kline["s"]
        interval = kline["i"]
        closed = kline["x"]
        if not isinstance(symbol, str) or not isinstance(interval, str) or type(closed) is not bool:
            raise MarketDataContractError("Kline stream có kiểu trường không hợp lệ.")
        if (
            _SYMBOL.fullmatch(symbol) is None
            or interval not in _FIXED_INTERVALS
            or data["s"] != symbol
            or stream_name != f"{symbol.lower()}@kline_{interval}"
        ):
            raise MarketDataContractError("Kline stream không khớp tên stream đã đăng ký.")
        open_time = _from_milliseconds(kline["t"], "open time")
        close_time = _from_milliseconds(kline["T"], "close time")
        if close_time - open_time != _FIXED_INTERVALS[interval] - _MILLISECOND:
            raise MarketDataContractError("Kline stream có khoảng thời gian không khớp interval.")
        instant = received_at.astimezone(UTC)
        if closed and close_time >= instant:
            # Sự kiện đóng tới sau ~100 ms; đồng hồ máy chậm nhẹ không được làm rớt phiên.
            if close_time - instant > _CLOCK_LAG_TOLERANCE:
                raise MarketDataContractError("Kline báo đã đóng trước thời điểm đóng nến.")
            instant = close_time + _MILLISECOND
        volume = _decimal(kline["v"], "volume")
        quote_volume = _decimal(kline["q"], "quote asset volume")
        trade_count: int | None = None
        if "n" in kline and kline["n"] is not None:
            raw_count = kline["n"]
            if type(raw_count) is not int or raw_count < 0:
                raise MarketDataContractError("Kline stream có trade_count không hợp lệ.")
            trade_count = raw_count
        taker_buy_base_volume = (
            _decimal(kline["V"], "taker buy base volume")
            if "V" in kline and kline["V"] is not None
            else None
        )
        taker_buy_quote_volume = (
            _decimal(kline["Q"], "taker buy quote volume")
            if "Q" in kline and kline["Q"] is not None
            else None
        )
        return OHLCVBar(
            symbol=symbol,
            timeframe=interval,
            open_time=open_time,
            close_time=close_time,
            event_at=min(close_time, instant),
            received_at=instant,
            available_at=instant,
            open=_decimal(kline["o"], "open"),
            high=_decimal(kline["h"], "high"),
            low=_decimal(kline["l"], "low"),
            close=_decimal(kline["c"], "close"),
            volume=volume,
            quote_volume=quote_volume,
            trade_count=trade_count,
            taker_buy_base_volume=taker_buy_base_volume,
            taker_buy_quote_volume=taker_buy_quote_volume,
            is_closed=closed,
            source=_SOURCE,
            source_cursor=str(kline["t"]),
            checksum=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        )
    except (KeyError, IndexError, TypeError, InvalidOperation, ValueError) as error:
        if isinstance(error, MarketDataContractError):
            raise
        raise MarketDataContractError("Kline stream thiếu trường hoặc có giá trị sai.") from error


_EVENT_STREAM_SUFFIXES = {
    "mark_price": "markPrice@1s",
    "liquidation": "forceOrder",
    "trade_flow": "aggTrade",
    "book_ticker": "bookTicker",
}
# Từ 2026-03-05 Binance tách route: bookTicker/depth ở `/public`, còn mark price, thanh lý,
# aggTrade, kline ở `/market` (Important WebSocket Change Notice, kiểm 15/11/2026).
_EVENT_ROUTES = {
    "mark_price": "market",
    "liquidation": "market",
    "trade_flow": "market",
    "book_ticker": "public",
}
_LIQUIDATION_SIDES = frozenset({"BUY", "SELL"})


def _stream_url(base_url: str, names: Sequence[str], route: str = "market") -> str:
    parts = urlsplit(base_url)
    if parts.scheme != "wss" or not parts.netloc or parts.path not in {"", "/"} or parts.query:
        raise ValueError("base_url stream phải là origin wss không kèm path hoặc query.")
    if not 1 <= len(names) <= MAX_STREAMS_PER_CONNECTION:
        raise ValueError("Số stream mỗi kết nối phải nằm trong 1..1024.")
    if route not in {"market", "public"}:
        raise ValueError("route stream không hợp lệ.")
    return f"wss://{parts.netloc}/{route}/stream?streams={'/'.join(names)}"


def build_event_stream_names(symbols: Sequence[str], kinds: Sequence[str]) -> tuple[str, ...]:
    """Tên stream theo từng symbol cho mark price (1 giây), thanh lý và aggregate trade."""
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("symbols event stream phải khác rỗng và không trùng lặp.")
    if not kinds or len(set(kinds)) != len(kinds) or not set(kinds) <= set(_EVENT_STREAM_SUFFIXES):
        raise ValueError("kinds event stream không hợp lệ.")
    names: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("symbol stream phải là mã USD-M viết hoa, không chứa separator.")
        names.extend(f"{symbol.lower()}@{_EVENT_STREAM_SUFFIXES[kind]}" for kind in kinds)
    return tuple(names)


def parse_market_event_message(message: str, *, received_at: datetime) -> MarketStreamEvent:
    """Chuyển message combined-stream thành sự kiện; sai hợp đồng thì từ chối."""
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at phải có múi giờ.")
    instant = received_at.astimezone(UTC)
    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise MarketDataContractError("Message stream không phải JSON hợp lệ.") from error
    try:
        stream_name = payload["stream"]
        data = payload["data"]
        event_type = data["e"]
        if not isinstance(stream_name, str):
            raise MarketDataContractError("Tên stream không hợp lệ.")
        checksum = hashlib.sha256(message.encode("utf-8")).hexdigest()
        if event_type == "markPriceUpdate":
            symbol = _event_symbol(data["s"], stream_name, "markPrice@1s")
            event_at, instant = _causal(_from_milliseconds(data["E"], "E"), instant)
            mark_price, index_price = _decimal(data["p"], "p"), _decimal(data["i"], "i")
            if mark_price <= 0 or index_price <= 0:
                raise MarketDataContractError("Mark/index price phải dương.")
            next_funding = data.get("T")
            return MarkPriceEvent(
                symbol=symbol,
                event_at=event_at,
                mark_price=mark_price,
                index_price=index_price,
                funding_rate=None if data.get("r") in (None, "") else _decimal(data["r"], "r"),
                next_funding_time=(
                    None if next_funding in (None, 0) else _from_milliseconds(next_funding, "T")
                ),
                received_at=instant,
                checksum=checksum,
            )
        if event_type == "forceOrder":
            order = data["o"]
            symbol = _event_symbol(order["s"], stream_name, "forceOrder")
            trade_time = _from_milliseconds(order["T"], "o.T")
            event_at, instant = _causal(_from_milliseconds(data["E"], "E"), instant)
            side = order["S"]
            texts = (order["o"], order["f"], order["X"])
            if side not in _LIQUIDATION_SIDES or not all(
                isinstance(text, str) and text for text in texts
            ):
                raise MarketDataContractError("Lệnh thanh lý có side hoặc trạng thái sai.")
            liquidation = LiquidationEvent(
                symbol=symbol,
                side=side,
                order_type=texts[0],
                time_in_force=texts[1],
                status=texts[2],
                price=_decimal(order["p"], "o.p"),
                average_price=_decimal(order["ap"], "o.ap"),
                quantity=_decimal(order["q"], "o.q"),
                filled_quantity=_decimal(order["z"], "o.z"),
                trade_time=min(trade_time, event_at),
                event_at=event_at,
                received_at=instant,
                checksum=checksum,
            )
            if (
                liquidation.quantity <= 0
                or liquidation.price < 0
                or liquidation.average_price < 0
                or not 0 <= liquidation.filled_quantity <= liquidation.quantity
            ):
                raise MarketDataContractError("Lệnh thanh lý có giá hoặc khối lượng sai.")
            return liquidation
        if event_type == "bookTicker":
            symbol = _event_symbol(data["s"], stream_name, "bookTicker")
            update_id = data["u"]
            if type(update_id) is not int or update_id < 0:
                raise MarketDataContractError("bookTicker có update id sai.")
            event_at, instant = _causal(_from_milliseconds(data["E"], "E"), instant)
            bid, ask = _decimal(data["b"], "b"), _decimal(data["a"], "a")
            bid_quantity, ask_quantity = _decimal(data["B"], "B"), _decimal(data["A"], "A")
            if bid <= 0 or ask <= 0 or bid > ask or bid_quantity < 0 or ask_quantity < 0:
                raise MarketDataContractError("bookTicker có giá hoặc khối lượng sai.")
            return BookTickerEvent(
                symbol=symbol,
                update_id=update_id,
                bid_price=bid,
                bid_quantity=bid_quantity,
                ask_price=ask,
                ask_quantity=ask_quantity,
                event_at=event_at,
                received_at=instant,
            )
        if event_type == "aggTrade":
            symbol = _event_symbol(data["s"], stream_name, "aggTrade")
            identifiers = (data["a"], data["f"], data["l"])
            buyer_is_maker = data["m"]
            if (
                any(type(identifier) is not int or identifier < 0 for identifier in identifiers)
                or identifiers[1] > identifiers[2]
                or type(buyer_is_maker) is not bool
            ):
                raise MarketDataContractError("aggTrade có id hoặc cờ maker sai.")
            trade_time, instant = _causal(_from_milliseconds(data["T"], "T"), instant)
            price, quantity = _decimal(data["p"], "p"), _decimal(data["q"], "q")
            if price <= 0 or quantity <= 0:
                raise MarketDataContractError("aggTrade có giá hoặc khối lượng sai.")
            return AggTradeEvent(
                symbol=symbol,
                aggregate_id=identifiers[0],
                price=price,
                quantity=quantity,
                first_trade_id=identifiers[1],
                last_trade_id=identifiers[2],
                trade_time=trade_time,
                buyer_is_maker=buyer_is_maker,
                received_at=instant,
            )
        raise MarketDataContractError("Loại sự kiện stream không được hỗ trợ.")
    except (KeyError, IndexError, TypeError, InvalidOperation, ValueError) as error:
        if isinstance(error, MarketDataContractError):
            raise
        raise MarketDataContractError("Sự kiện stream thiếu trường hoặc có giá trị sai.") from error


def _event_symbol(symbol: object, stream_name: str, suffix: str) -> str:
    if (
        not isinstance(symbol, str)
        or _SYMBOL.fullmatch(symbol) is None
        or stream_name != f"{symbol.lower()}@{suffix}"
    ):
        raise MarketDataContractError("Sự kiện không khớp tên stream đã đăng ký.")
    return symbol


def _causal(event_at: datetime, received_at: datetime) -> tuple[datetime, datetime]:
    """Giữ event_at <= received_at; chỉ dung sai đồng hồ máy chậm trong ngưỡng cho phép."""
    if event_at <= received_at:
        return event_at, received_at
    if event_at - received_at > _CLOCK_LAG_TOLERANCE:
        raise MarketDataContractError("Sự kiện stream nằm sau thời điểm nhận quá ngưỡng.")
    return event_at, event_at


class BinanceUsdMKlineStream:
    """Một phiên kết nối kline; không tự nối lại để caller chủ động lấp gap bằng REST."""

    def __init__(
        self,
        *,
        subscriptions: Sequence[KlineSubscription],
        client: httpx2.AsyncClient,
        base_url: str = BINANCE_USDM_MARKET_STREAM_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < idle_timeout_seconds <= 600:
            raise ValueError("idle_timeout_seconds phải nằm trong (0, 600].")
        self._url = build_kline_stream_url(base_url, subscriptions)
        self._subscriptions = frozenset(subscriptions)
        self._client = client
        self._clock = clock
        self._idle_timeout_seconds = idle_timeout_seconds

    def bars(self) -> AsyncGenerator[OHLCVBar]:
        """Phát mọi cập nhật kline (đang chạy và đã đóng) cho tới khi kết nối kết thúc."""

        def convert(message: str) -> OHLCVBar:
            bar = parse_kline_stream_message(message, received_at=self._clock())
            if (bar.symbol, bar.timeframe) not in self._subscriptions:
                raise MarketDataContractError("Stream trả kline ngoài subscription.")
            return bar

        return _converted(self._client, self._url, self._idle_timeout_seconds, convert)


class BinanceUsdMEventStream:
    """Một phiên stream sự kiện; mỗi kết nối chỉ một route (`/market` hoặc `/public`)."""

    def __init__(
        self,
        *,
        symbols: Sequence[str],
        kinds: Sequence[str],
        client: httpx2.AsyncClient,
        base_url: str = BINANCE_USDM_MARKET_STREAM_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < idle_timeout_seconds <= 600:
            raise ValueError("idle_timeout_seconds phải nằm trong (0, 600].")
        routes = {_EVENT_ROUTES.get(kind) for kind in kinds}
        if len(routes) != 1:
            raise ValueError(
                "Mỗi kết nối chỉ được dùng một route; tách bookTicker (/public) riêng."
            )
        self._url = _stream_url(
            base_url, build_event_stream_names(symbols, kinds), route=str(routes.pop())
        )
        self._symbols = frozenset(symbols)
        self._client = client
        self._clock = clock
        self._idle_timeout_seconds = idle_timeout_seconds

    @property
    def url(self) -> str:
        """URL combined stream đã dựng (không chứa bí mật; chỉ dữ liệu công khai)."""
        return self._url

    def events(self) -> AsyncGenerator[MarketStreamEvent]:
        """Phát sự kiện đã chuẩn hóa cho tới khi kết nối kết thúc."""

        def convert(message: str) -> MarketStreamEvent:
            event = parse_market_event_message(message, received_at=self._clock())
            if event.symbol not in self._symbols:
                raise MarketDataContractError("Stream trả sự kiện ngoài subscription.")
            return event

        return _converted(self._client, self._url, self._idle_timeout_seconds, convert)


async def _converted[RecordT](
    client: httpx2.AsyncClient,
    url: str,
    idle_timeout_seconds: float,
    convert: Callable[[str], RecordT],
) -> AsyncGenerator[RecordT]:
    """Đọc text frame và chuyển đổi ngay trong phiên; rớt kết nối quy về một exception."""
    try:
        async with client.websocket(url, max_message_size_bytes=_MAX_MESSAGE_BYTES) as session:
            while True:
                yield convert(await session.receive_text(timeout=idle_timeout_seconds))
    except _DISCONNECT_ERRORS as error:
        raise KlineStreamDisconnected(_disconnect_code(error)) from error
    except BaseExceptionGroup as group:
        # httpx2 chạy phiên WebSocket trong task group nên lỗi bên trong bị bọc lại.
        contract, rest = group.split(MarketDataContractError)
        if contract is not None:
            raise _first_leaf(contract) from group
        disconnects, rest = group.split(_DISCONNECT_ERRORS)
        if disconnects is not None and rest is None:
            leaf = _first_leaf(disconnects)
            raise KlineStreamDisconnected(_disconnect_code(leaf)) from group
        raise


_DISCONNECT_ERRORS = (HTTPXWSException, httpx2.HTTPError, TimeoutError, OSError)


def _first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    error: BaseException = group
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


def _disconnect_code(error: BaseException) -> str:
    """Chỉ giữ tên lớp lỗi; không đưa URL hoặc payload vào log."""
    return f"KLINE_STREAM_{type(error).__name__.upper()[:60]}"


__all__ = [
    "BINANCE_USDM_MARKET_STREAM_URL",
    "MAX_STREAMS_PER_CONNECTION",
    "BinanceUsdMEventStream",
    "BinanceUsdMKlineStream",
    "KlineStreamDisconnected",
    "KlineSubscription",
    "build_event_stream_names",
    "build_kline_stream_url",
    "parse_market_event_message",
    "parse_kline_stream_message",
]
