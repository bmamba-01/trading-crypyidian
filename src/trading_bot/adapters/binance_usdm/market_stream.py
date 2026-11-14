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
    return f"wss://{parts.netloc}/market/stream?streams={'/'.join(names)}"


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
            volume=_decimal(kline["v"], "volume"),
            quote_volume=_decimal(kline["q"], "quote asset volume"),
            is_closed=closed,
            source=_SOURCE,
            source_cursor=str(kline["t"]),
            checksum=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        )
    except (KeyError, IndexError, TypeError, InvalidOperation, ValueError) as error:
        if isinstance(error, MarketDataContractError):
            raise
        raise MarketDataContractError("Kline stream thiếu trường hoặc có giá trị sai.") from error


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

    async def bars(self) -> AsyncGenerator[OHLCVBar]:
        """Phát mọi cập nhật kline (đang chạy và đã đóng) cho tới khi kết nối kết thúc."""
        try:
            async with self._client.websocket(
                self._url, max_message_size_bytes=_MAX_MESSAGE_BYTES
            ) as session:
                while True:
                    message = await session.receive_text(timeout=self._idle_timeout_seconds)
                    bar = parse_kline_stream_message(message, received_at=self._clock())
                    if (bar.symbol, bar.timeframe) not in self._subscriptions:
                        raise MarketDataContractError("Stream trả kline ngoài subscription.")
                    yield bar
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
    "BinanceUsdMKlineStream",
    "KlineStreamDisconnected",
    "KlineSubscription",
    "build_kline_stream_url",
    "parse_kline_stream_message",
]
