"""Adapter chỉ đọc các endpoint market data công khai USDⓈ-M."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import httpx2

from trading_bot.domain.market.ingestion import InstrumentObservation
from trading_bot.domain.market.models import Instrument, OHLCVBar
from trading_bot.ports.market_data import (
    BookTicker,
    ExchangeInstrument,
    FundingInfo,
    FundingRateObservation,
    KlineFlow,
    LongShortRatioObservation,
    MarketDataPage,
    OpenInterestObservation,
    PremiumIndexObservation,
    PriceTicker,
    TakerFlowObservation,
    Ticker24h,
)

BINANCE_USDM_BASE_URL = "https://fapi.binance.com"
_KLINE_INTERVALS = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}
_OI_PERIODS = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
_FIXED_INTERVALS = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(days=7),
}
_RETRYABLE_STATUS = {418, 429}
_MAX_RETRY_AFTER_SECONDS = 30.0
_MAX_OI_MISSING_TIMES = 500


class MarketDataContractError(ValueError):
    """Nguồn đã đổi cấu trúc hoặc trả dữ liệu không hợp lệ."""


class BinanceHTTPError(RuntimeError):
    """Endpoint công khai trả mã HTTP không thành công."""

    def __init__(self, status_code: int, endpoint: str) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        super().__init__(f"Binance market-data trả HTTP {status_code} tại {endpoint}.")


class BinanceRateLimitError(BinanceHTTPError):
    """Sàn từ chối do giới hạn tần suất; caller cần hạ tải collector."""

    def __init__(self, status_code: int, endpoint: str, retry_after: float | None) -> None:
        self.retry_after = retry_after
        super().__init__(status_code, endpoint)


class PublicRequestLimiter:
    """Giữ headroom cho IP quota và các giới hạn đếm riêng của Binance."""

    def __init__(
        self,
        *,
        requests_per_minute: int = 2400,
        safety_ratio: float = 0.8,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if requests_per_minute < 1 or not 0 < safety_ratio <= 1:
            raise ValueError("Cấu hình limiter phải có quota và safety_ratio hợp lệ.")
        self._safety_ratio = safety_ratio
        self._capacity = int(requests_per_minute * self._safety_ratio)
        self._monotonic = monotonic
        self._sleep = sleep
        self._window_start = monotonic()
        self._used_weight = 0
        self._server_weight: int | None = None
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    def configure_requests_per_minute(self, limit: int) -> None:
        """Cập nhật quota theo exchangeInfo và giữ nguyên headroom đã cấu hình."""
        if limit < 1:
            raise ValueError("request-weight limit phải lớn hơn 0.")
        self._capacity = int(limit * self._safety_ratio)

    async def acquire(self, *, weight: int, category: str | None = None) -> None:
        """Chờ trong limiter trước khi dùng IP quota hoặc quota endpoint riêng."""
        if weight < 0:
            raise ValueError("weight không được âm.")
        async with self._lock:
            while True:
                now = self._monotonic()
                if now - self._window_start >= 60:
                    self._window_start = now
                    self._used_weight = 0
                    self._server_weight = None
                window_remaining = max(0.0, 60 - (now - self._window_start))
                wait_for = 0.0
                effective_used = max(self._used_weight, self._server_weight or 0)
                if effective_used + weight > self._capacity:
                    wait_for = window_remaining
                if category is not None:
                    calls = self._calls[category]
                    while calls and now - calls[0] >= 300:
                        calls.popleft()
                    count_limit = {"funding": 500, "oi_history": 1000, "futures_data": 1000}.get(
                        category
                    )
                    if count_limit is not None and len(calls) >= count_limit:
                        wait_for = max(wait_for, max(0.01, 300 - (now - calls[0])))
                if wait_for <= 0:
                    self._used_weight += weight
                    if category is not None:
                        self._calls[category].append(now)
                    return
                await self._sleep(wait_for)

    def observe(self, headers: Mapping[str, str]) -> None:
        """Cập nhật mức quota dùng từ header do API trả về."""
        raw_used = headers.get("x-mbx-used-weight-1m")
        if raw_used is not None:
            try:
                self._server_weight = max(self._server_weight or 0, int(raw_used))
            except ValueError:
                return


@dataclass(frozen=True, slots=True)
class _RawResponse:
    payload: Any
    body: bytes
    received_at: datetime
    checksum: str


class BinanceUsdMMarketDataAdapter:
    """Đọc market data public, parse Decimal và giữ provenance phản hồi gốc."""

    def __init__(
        self,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rate_limiter: Any | None = None,
        max_attempts: int = 3,
        max_retry_after_seconds: float = _MAX_RETRY_AFTER_SECONDS,
    ) -> None:
        if max_attempts < 1 or max_retry_after_seconds < 0:
            raise ValueError("Retry policy phải có số lần và độ trễ hợp lệ.")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._max_retry_after_seconds = max_retry_after_seconds
        self._limiter = rate_limiter or PublicRequestLimiter(monotonic=monotonic, sleep=sleep)
        self._client = httpx2.AsyncClient(
            base_url=BINANCE_USDM_BASE_URL,
            transport=transport,
            timeout=httpx2.Timeout(10.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
            auth=None,
            headers={"Accept": "application/json", "User-Agent": "Crypidian/0.1"},
        )

    async def __aenter__(self) -> BinanceUsdMMarketDataAdapter:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_exchange_info(self) -> MarketDataPage[ExchangeInstrument]:
        response = await self._request_json("/fapi/v1/exchangeInfo", weight=1)
        payload = _require_mapping(response.payload, "exchangeInfo")
        self._configure_weight_from_exchange_info(payload)
        symbols = _require_list(payload.get("symbols"), "exchangeInfo.symbols")
        records: list[ExchangeInstrument] = []
        for value in symbols:
            symbol_data = _require_mapping(value, "exchangeInfo.symbol")
            contract_type = _require_string(symbol_data.get("contractType"), "contractType")
            filters = _parse_filters(symbol_data.get("filters"))
            instrument = Instrument(
                symbol=_require_string(symbol_data.get("symbol"), "symbol"),
                base_asset=_require_string(symbol_data.get("baseAsset"), "baseAsset"),
                quote_asset=_require_string(symbol_data.get("quoteAsset"), "quoteAsset"),
                tick_size=_decimal(filters["PRICE_FILTER"].get("tickSize"), "tickSize"),
                quantity_step=_decimal(
                    filters.get("MARKET_LOT_SIZE", filters["LOT_SIZE"]).get("stepSize"),
                    "stepSize",
                ),
                min_notional=_minimum_notional(filters),
                status=_require_string(symbol_data.get("status"), "status"),
            )
            records.append(
                ExchangeInstrument(
                    observation=InstrumentObservation(
                        instrument=instrument,
                        source="BINANCE_USDM",
                        observed_at=response.received_at,
                    ),
                    contract_type=contract_type,
                    margin_asset=_require_string(symbol_data.get("marginAsset"), "marginAsset"),
                )
            )
        return _page(response, tuple(records))

    def _configure_weight_from_exchange_info(self, payload: Mapping[str, Any]) -> None:
        rate_limits = payload.get("rateLimits", [])
        if not isinstance(rate_limits, list):
            raise MarketDataContractError("exchangeInfo.rateLimits phải là array JSON.")
        for value in rate_limits:
            limit = _require_mapping(value, "exchangeInfo.rateLimit")
            if (
                limit.get("rateLimitType") == "REQUEST_WEIGHT"
                and limit.get("interval") == "MINUTE"
                and limit.get("intervalNum") == 1
            ):
                request_limit = limit.get("limit")
                if isinstance(request_limit, int) and not isinstance(request_limit, bool):
                    configure = getattr(self._limiter, "configure_requests_per_minute", None)
                    if callable(configure):
                        configure(request_limit)
                return

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> MarketDataPage[OHLCVBar]:
        _require_symbol(symbol)
        if interval not in _KLINE_INTERVALS:
            raise ValueError("interval không thuộc danh sách Kline USDⓈ-M được hỗ trợ.")
        _validate_limit(limit, maximum=1500)
        _validate_range(start_time, end_time)
        params: dict[str, str | int] = {"symbol": symbol, "interval": interval, "limit": limit}
        _add_time_params(params, start_time, end_time)
        response = await self._request_json(
            "/fapi/v1/klines", params=params, weight=_kline_weight(limit)
        )
        values = _require_list(response.payload, "klines")
        records = tuple(
            _parse_kline(
                value,
                symbol=symbol,
                interval=interval,
                received_at=response.received_at,
                checksum=response.checksum,
            )
            for value in values
        )
        in_range = tuple(
            bar
            for bar in records
            if (start_time is None or bar.open_time >= start_time)
            and (end_time is None or bar.open_time <= end_time)
        )
        complete = len(values) < limit
        reason = "Phản hồi đạt limit; cần lấy trang kế." if not complete else None
        return _page(response, in_range, is_complete=complete, incomplete_reason=reason)

    async def get_funding_rates(
        self,
        *,
        symbol: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> MarketDataPage[FundingRateObservation]:
        _require_symbol(symbol)
        _validate_limit(limit, maximum=1000)
        _validate_range(start_time, end_time)
        params: dict[str, str | int] = {"symbol": symbol, "limit": limit}
        _add_time_params(params, start_time, end_time)
        response = await self._request_json(
            "/fapi/v1/fundingRate", params=params, weight=0, category="funding"
        )
        values = _require_list(response.payload, "fundingRate")
        records = tuple(
            _parse_funding(value, symbol=symbol, received_at=response.received_at)
            for value in values
        )
        in_range = tuple(
            item
            for item in records
            if (start_time is None or item.funding_time >= start_time)
            and (end_time is None or item.funding_time <= end_time)
        )
        complete = len(values) < limit
        reason = "Phản hồi đạt limit; cần lấy trang kế." if not complete else None
        return _page(response, in_range, is_complete=complete, incomplete_reason=reason)

    async def get_open_interest(self, symbol: str) -> MarketDataPage[OpenInterestObservation]:
        _require_symbol(symbol)
        response = await self._request_json(
            "/fapi/v1/openInterest", params={"symbol": symbol}, weight=1
        )
        record = _parse_open_interest(
            response.payload,
            symbol=symbol,
            timestamp_field="time",
            quantity_field="openInterest",
            received_at=response.received_at,
        )
        return _page(response, (record,))

    async def get_open_interest_history(
        self,
        *,
        symbol: str,
        period: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 30,
    ) -> MarketDataPage[OpenInterestObservation]:
        _require_symbol(symbol)
        if period not in _OI_PERIODS:
            raise ValueError("period không thuộc danh sách open-interest history được hỗ trợ.")
        _validate_limit(limit, maximum=500)
        _validate_range(start_time, end_time)
        params: dict[str, str | int] = {"symbol": symbol, "period": period, "limit": limit}
        _add_time_params(params, start_time, end_time)
        response = await self._request_json(
            "/futures/data/openInterestHist", params=params, weight=0, category="oi_history"
        )
        values = _require_list(response.payload, "openInterestHist")
        records = tuple(
            _parse_open_interest(
                value,
                symbol=symbol,
                timestamp_field="timestamp",
                quantity_field="sumOpenInterest",
                value_field="sumOpenInterestValue",
                received_at=response.received_at,
            )
            for value in values
        )
        in_range = tuple(
            item
            for item in records
            if (start_time is None or item.observed_at >= start_time)
            and (end_time is None or item.observed_at <= end_time)
        )
        missing: tuple[datetime, ...] = ()
        incomplete_reasons: list[str] = []
        if start_time is not None and end_time is not None:
            interval = _OI_PERIOD_INTERVALS[period]
            expected = _expected_times(start_time, end_time, interval, limit)
            present = {item.observed_at for item in in_range}
            missing = tuple(value for value in expected if value not in present)
            if missing:
                incomplete_reasons.append("Endpoint đã lược bỏ mốc period trống.")
            if _range_count(start_time, end_time, interval, limit) > limit:
                incomplete_reasons.append("Range vượt limit response; cần chia trang.")
        cutoff = _one_month_before(self._clock())
        if (start_time is not None and start_time < cutoff) or (
            end_time is not None and end_time < cutoff
        ):
            incomplete_reasons.append("Range có phần trước mốc lưu dữ liệu một tháng.")
        complete = not incomplete_reasons and len(values) < limit
        if len(values) >= limit:
            incomplete_reasons.append("Phản hồi đạt limit; cần lấy trang kế.")
        return _page(
            response,
            in_range,
            is_complete=complete,
            missing_times=missing,
            incomplete_reason=" ".join(incomplete_reasons) or None,
        )

    async def get_premium_index(
        self, symbol: str | None = None
    ) -> MarketDataPage[PremiumIndexObservation]:
        """Mark/index price và funding hiện hành; bỏ symbol để lấy toàn thị trường."""
        response, values = await self._snapshot(
            "/fapi/v1/premiumIndex", symbol, single_weight=1, all_weight=10
        )
        return _page(
            response,
            tuple(_parse_premium_index(value, response.received_at) for value in values),
        )

    async def get_price_tickers(self, symbol: str | None = None) -> MarketDataPage[PriceTicker]:
        """Giá khớp gần nhất; dùng v2 vì v1 đã bị Binance đánh dấu deprecated."""
        response, values = await self._snapshot(
            "/fapi/v2/ticker/price", symbol, single_weight=1, all_weight=2
        )
        return _page(
            response,
            tuple(_parse_price_ticker(value, response.received_at) for value in values),
        )

    async def get_book_tickers(self, symbol: str | None = None) -> MarketDataPage[BookTicker]:
        """Best bid/ask; sổ chéo (bid >= ask) bị coi là dữ liệu sai."""
        response, values = await self._snapshot(
            "/fapi/v1/ticker/bookTicker", symbol, single_weight=2, all_weight=5
        )
        return _page(
            response,
            tuple(_parse_book_ticker(value, response.received_at) for value in values),
        )

    async def get_24h_tickers(self, symbol: str | None = None) -> MarketDataPage[Ticker24h]:
        """Thống kê 24 giờ trượt; lấy toàn thị trường tốn weight 40."""
        response, values = await self._snapshot(
            "/fapi/v1/ticker/24hr", symbol, single_weight=1, all_weight=40
        )
        return _page(
            response,
            tuple(_parse_ticker_24h(value, response.received_at) for value in values),
        )

    async def get_funding_info(self) -> MarketDataPage[FundingInfo]:
        """Chu kỳ/biên funding đã điều chỉnh; chung quota 500 lần/5 phút với fundingRate."""
        response = await self._request_json("/fapi/v1/fundingInfo", weight=0, category="funding")
        values = _require_list(response.payload, "fundingInfo")
        return _page(
            response,
            tuple(_parse_funding_info(value, response.received_at) for value in values),
        )

    async def get_long_short_ratio(
        self,
        *,
        symbol: str,
        kind: str,
        period: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 30,
    ) -> MarketDataPage[LongShortRatioObservation]:
        """Tỷ lệ long/short; sàn chỉ giữ 30 ngày nên phải thu thập định kỳ."""
        endpoint = _LONG_SHORT_ENDPOINTS.get(kind)
        if endpoint is None:
            raise ValueError("kind long/short ratio không được hỗ trợ.")
        response, values = await self._futures_data(
            endpoint, symbol=symbol, period=period, start=start_time, end=end_time, limit=limit
        )
        records = tuple(
            _parse_long_short_ratio(
                value, symbol=symbol, kind=kind, period=period, received_at=response.received_at
            )
            for value in values
        )
        return _page(response, tuple(sorted(records, key=lambda item: item.observed_at)))

    async def get_taker_buy_sell_volume(
        self,
        *,
        symbol: str,
        period: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 30,
    ) -> MarketDataPage[TakerFlowObservation]:
        """Khối lượng taker mua/bán theo period; sàn chỉ giữ 30 ngày."""
        response, values = await self._futures_data(
            "/futures/data/takerlongshortRatio",
            symbol=symbol,
            period=period,
            start=start_time,
            end=end_time,
            limit=limit,
        )
        records = tuple(
            _parse_taker_flow(value, symbol=symbol, period=period, received_at=response.received_at)
            for value in values
        )
        return _page(response, tuple(sorted(records, key=lambda item: item.observed_at)))

    async def get_kline_flows(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> MarketDataPage[KlineFlow]:
        """Số trade và taker buy volume của nến; cùng endpoint và weight với `get_klines`."""
        _require_symbol(symbol)
        if interval not in _KLINE_INTERVALS:
            raise ValueError("interval không thuộc danh sách Kline USDⓈ-M được hỗ trợ.")
        _validate_limit(limit, maximum=1500)
        _validate_range(start_time, end_time)
        params: dict[str, str | int] = {"symbol": symbol, "interval": interval, "limit": limit}
        _add_time_params(params, start_time, end_time)
        response = await self._request_json(
            "/fapi/v1/klines", params=params, weight=_kline_weight(limit)
        )
        values = _require_list(response.payload, "klines")
        records = tuple(
            _parse_kline_flow(
                value, symbol=symbol, interval=interval, received_at=response.received_at
            )
            for value in values
        )
        complete = len(values) < limit
        reason = "Phản hồi đạt limit; cần lấy trang kế." if not complete else None
        return _page(response, records, is_complete=complete, incomplete_reason=reason)

    async def _snapshot(
        self, endpoint: str, symbol: str | None, *, single_weight: int, all_weight: int
    ) -> tuple[_RawResponse, list[Any]]:
        """Gọi endpoint trả object khi có symbol và array khi lấy toàn thị trường."""
        if symbol is None:
            response = await self._request_json(endpoint, weight=all_weight)
            return response, _require_list(response.payload, endpoint)
        _require_symbol(symbol)
        response = await self._request_json(
            endpoint, params={"symbol": symbol}, weight=single_weight
        )
        item = _require_mapping(response.payload, endpoint)
        if item.get("symbol") != symbol:
            raise MarketDataContractError("Response symbol không khớp request.")
        return response, [item]

    async def _futures_data(
        self,
        endpoint: str,
        *,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> tuple[_RawResponse, list[Any]]:
        _require_symbol(symbol)
        if period not in _OI_PERIODS:
            raise ValueError("period không thuộc danh sách thống kê futures được hỗ trợ.")
        _validate_limit(limit, maximum=500)
        _validate_range(start, end)
        params: dict[str, str | int] = {"symbol": symbol, "period": period, "limit": limit}
        _add_time_params(params, start, end)
        response = await self._request_json(
            endpoint, params=params, weight=0, category="futures_data"
        )
        return response, _require_list(response.payload, endpoint)

    async def _request_json(
        self,
        endpoint: str,
        *,
        params: Mapping[str, str | int] | None = None,
        weight: int,
        category: str | None = None,
    ) -> _RawResponse:
        for attempt in range(self._max_attempts):
            await self._limiter.acquire(weight=weight, category=category)
            try:
                response = await self._client.get(endpoint, params=params)
            except httpx2.TimeoutException, httpx2.NetworkError:
                if attempt + 1 >= self._max_attempts:
                    raise
                await self._sleep(min(0.25 * (2**attempt), self._max_retry_after_seconds))
                continue
            self._limiter.observe(response.headers)
            if response.status_code in _RETRYABLE_STATUS:
                delay = _retry_after_seconds(response.headers.get("Retry-After"), self._clock())
                if delay is None:
                    delay = min(0.25 * (2**attempt), self._max_retry_after_seconds)
                if delay > self._max_retry_after_seconds or attempt + 1 >= self._max_attempts:
                    raise BinanceRateLimitError(response.status_code, endpoint, delay)
                await self._sleep(delay)
                continue
            if response.status_code >= 500:
                if attempt + 1 >= self._max_attempts:
                    raise BinanceHTTPError(response.status_code, endpoint)
                await self._sleep(min(0.25 * (2**attempt), self._max_retry_after_seconds))
                continue
            if not response.is_success:
                raise BinanceHTTPError(response.status_code, endpoint)
            try:
                payload = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise MarketDataContractError("Response không phải JSON hợp lệ.") from error
            received_at = self._clock()
            if received_at.tzinfo is None or received_at.utcoffset() is None:
                raise ValueError("clock phải trả datetime có múi giờ.")
            body = response.content
            return _RawResponse(
                payload=payload,
                body=body,
                received_at=received_at,
                checksum=hashlib.sha256(body).hexdigest(),
            )
        raise AssertionError("Retry loop kết thúc ngoài dự kiến.")


_OI_PERIOD_INTERVALS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}


def _parse_filters(value: Any) -> dict[str, Mapping[str, Any]]:
    filters = _require_list(value, "symbol.filters")
    parsed: dict[str, Mapping[str, Any]] = {}
    for item in filters:
        entry = _require_mapping(item, "symbol.filter")
        kind = _require_string(entry.get("filterType"), "filterType")
        if kind in {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "NOTIONAL"}:
            parsed[kind] = entry
    for required in ("PRICE_FILTER", "LOT_SIZE"):
        if required not in parsed:
            raise MarketDataContractError(f"Thiếu filter bắt buộc {required}.")
    if not ({"MIN_NOTIONAL", "NOTIONAL"} & parsed.keys()):
        raise MarketDataContractError("Thiếu filter bắt buộc MIN_NOTIONAL/NOTIONAL.")
    return parsed


def _minimum_notional(filters: Mapping[str, Mapping[str, Any]]) -> Decimal:
    for kind in ("MIN_NOTIONAL", "NOTIONAL"):
        if kind in filters:
            filter_data = filters[kind]
            value = filter_data.get("notional", filter_data.get("minNotional"))
            return _decimal(value, f"{kind}.notional")
    raise MarketDataContractError("Thiếu filter MIN_NOTIONAL/NOTIONAL.")


def _parse_kline(
    value: Any,
    *,
    symbol: str,
    interval: str,
    received_at: datetime,
    checksum: str,
) -> OHLCVBar:
    if not isinstance(value, list) or len(value) != 12:
        raise MarketDataContractError("Kline phải là tuple có đúng 12 trường.")
    try:
        open_time = _from_milliseconds(value[0], "open time")
        close_time = _from_milliseconds(value[6], "close time")
        event_at = min(close_time, received_at)
        return OHLCVBar(
            symbol=symbol,
            timeframe=interval,
            open_time=open_time,
            close_time=close_time,
            event_at=event_at,
            received_at=received_at,
            available_at=received_at,
            open=_decimal(value[1], "open"),
            high=_decimal(value[2], "high"),
            low=_decimal(value[3], "low"),
            close=_decimal(value[4], "close"),
            volume=_decimal(value[5], "volume"),
            quote_volume=_decimal(value[7], "quote asset volume"),
            is_closed=close_time < received_at,
            source="BINANCE_USDM",
            source_cursor=str(value[0]),
            checksum=checksum,
        )
    except (IndexError, TypeError, InvalidOperation, ValueError) as error:
        if isinstance(error, MarketDataContractError):
            raise
        raise MarketDataContractError("Kline có trường hoặc giá trị không hợp lệ.") from error


def _parse_funding(value: Any, *, symbol: str, received_at: datetime) -> FundingRateObservation:
    item = _require_mapping(value, "funding record")
    response_symbol = _require_string(item.get("symbol"), "funding.symbol")
    if response_symbol != symbol:
        raise MarketDataContractError("Funding response symbol không khớp request.")
    funding_time = _from_milliseconds(item.get("fundingTime"), "fundingTime")
    mark_price_value = item.get("markPrice")
    mark_price = None if mark_price_value in (None, "") else _decimal(mark_price_value, "markPrice")
    rate_type = item.get("rateType", "Regular")
    if rate_type not in {"Regular", "Special"}:
        raise MarketDataContractError("rateType funding không được hỗ trợ.")
    if funding_time > received_at:
        raise MarketDataContractError("Funding event nằm sau thời điểm nhận dữ liệu.")
    return FundingRateObservation(
        symbol=symbol,
        funding_time=funding_time,
        funding_rate=_decimal(item.get("fundingRate"), "fundingRate"),
        mark_price=mark_price,
        rate_type=rate_type,
        received_at=received_at,
        available_at=received_at,
    )


def _parse_open_interest(
    value: Any,
    *,
    symbol: str,
    timestamp_field: str,
    quantity_field: str,
    received_at: datetime,
    value_field: str | None = None,
) -> OpenInterestObservation:
    item = _require_mapping(value, "open interest record")
    response_symbol = _require_string(item.get("symbol"), "openInterest.symbol")
    if response_symbol != symbol:
        raise MarketDataContractError("Open-interest response symbol không khớp request.")
    observed_at = _from_milliseconds(item.get(timestamp_field), timestamp_field)
    if observed_at > received_at:
        raise MarketDataContractError("Open-interest event nằm sau thời điểm nhận dữ liệu.")
    raw_value = item.get(value_field) if value_field is not None else None
    return OpenInterestObservation(
        symbol=symbol,
        observed_at=observed_at,
        quantity=_decimal(item.get(quantity_field), quantity_field),
        value=None if raw_value in (None, "") else _decimal(raw_value, value_field or "value"),
        received_at=received_at,
        available_at=received_at,
    )


_LONG_SHORT_ENDPOINTS = {
    "global_account": "/futures/data/globalLongShortAccountRatio",
    "top_trader_account": "/futures/data/topLongShortAccountRatio",
    "top_trader_position": "/futures/data/topLongShortPositionRatio",
}


def _event_time(value: Any, name: str, received_at: datetime) -> datetime:
    observed_at = _from_milliseconds(value, name)
    if observed_at > received_at:
        raise MarketDataContractError(f"{name} nằm sau thời điểm nhận dữ liệu.")
    return observed_at


def _optional_decimal(value: Any, name: str) -> Decimal | None:
    return None if value in (None, "") else _decimal(value, name)


def _parse_premium_index(value: Any, received_at: datetime) -> PremiumIndexObservation:
    item = _require_mapping(value, "premiumIndex")
    next_funding = item.get("nextFundingTime")
    # Hợp đồng đang settle/chưa có lịch funding trả nextFundingTime = 0.
    next_funding_time = (
        None if next_funding in (None, 0) else _from_milliseconds(next_funding, "nextFundingTime")
    )
    return PremiumIndexObservation(
        symbol=_require_string(item.get("symbol"), "premiumIndex.symbol"),
        mark_price=_decimal(item.get("markPrice"), "markPrice"),
        index_price=_decimal(item.get("indexPrice"), "indexPrice"),
        last_funding_rate=_optional_decimal(item.get("lastFundingRate"), "lastFundingRate"),
        next_funding_time=next_funding_time,
        observed_at=_event_time(item.get("time"), "premiumIndex.time", received_at),
        received_at=received_at,
        available_at=received_at,
    )


def _parse_price_ticker(value: Any, received_at: datetime) -> PriceTicker:
    item = _require_mapping(value, "ticker price")
    return PriceTicker(
        symbol=_require_string(item.get("symbol"), "ticker.symbol"),
        price=_decimal(item.get("price"), "price"),
        observed_at=_event_time(item.get("time"), "ticker.time", received_at),
        received_at=received_at,
        available_at=received_at,
    )


def _parse_book_ticker(value: Any, received_at: datetime) -> BookTicker:
    item = _require_mapping(value, "bookTicker")
    ticker = BookTicker(
        symbol=_require_string(item.get("symbol"), "bookTicker.symbol"),
        bid_price=_decimal(item.get("bidPrice"), "bidPrice"),
        bid_quantity=_decimal(item.get("bidQty"), "bidQty"),
        ask_price=_decimal(item.get("askPrice"), "askPrice"),
        ask_quantity=_decimal(item.get("askQty"), "askQty"),
        observed_at=_event_time(item.get("time"), "bookTicker.time", received_at),
        received_at=received_at,
        available_at=received_at,
    )
    # Symbol ngừng giao dịch có thể trả giá 0 cả hai phía; chỉ từ chối sổ chéo thật.
    if ticker.bid_price > 0 and ticker.ask_price > 0 and ticker.bid_price >= ticker.ask_price:
        raise MarketDataContractError("bookTicker có bid không thấp hơn ask.")
    return ticker


def _parse_ticker_24h(value: Any, received_at: datetime) -> Ticker24h:
    item = _require_mapping(value, "ticker 24hr")
    trade_count = item.get("count")
    if type(trade_count) is not int or trade_count < 0:
        raise MarketDataContractError("ticker 24hr count phải là số nguyên không âm.")
    return Ticker24h(
        symbol=_require_string(item.get("symbol"), "ticker.symbol"),
        last_price=_decimal(item.get("lastPrice"), "lastPrice"),
        price_change_percent=_decimal(item.get("priceChangePercent"), "priceChangePercent"),
        high_price=_decimal(item.get("highPrice"), "highPrice"),
        low_price=_decimal(item.get("lowPrice"), "lowPrice"),
        volume=_decimal(item.get("volume"), "volume"),
        quote_volume=_decimal(item.get("quoteVolume"), "quoteVolume"),
        trade_count=trade_count,
        open_time=_from_milliseconds(item.get("openTime"), "openTime"),
        close_time=_from_milliseconds(item.get("closeTime"), "closeTime"),
        received_at=received_at,
        available_at=received_at,
    )


def _parse_funding_info(value: Any, received_at: datetime) -> FundingInfo:
    item = _require_mapping(value, "fundingInfo")
    hours = item.get("fundingIntervalHours")
    if type(hours) is not int or not 1 <= hours <= 24:
        raise MarketDataContractError("fundingIntervalHours phải là số nguyên 1..24.")
    return FundingInfo(
        symbol=_require_string(item.get("symbol"), "fundingInfo.symbol"),
        funding_interval_hours=hours,
        rate_cap=_decimal(item.get("adjustedFundingRateCap"), "adjustedFundingRateCap"),
        rate_floor=_decimal(item.get("adjustedFundingRateFloor"), "adjustedFundingRateFloor"),
        received_at=received_at,
        available_at=received_at,
    )


def _parse_long_short_ratio(
    value: Any, *, symbol: str, kind: str, period: str, received_at: datetime
) -> LongShortRatioObservation:
    item = _require_mapping(value, "long/short ratio")
    if item.get("symbol") != symbol:
        raise MarketDataContractError("Long/short ratio symbol không khớp request.")
    return LongShortRatioObservation(
        symbol=symbol,
        kind=kind,
        period=period,
        observed_at=_event_time(item.get("timestamp"), "ratio.timestamp", received_at),
        long_share=_decimal(item.get("longAccount"), "longAccount"),
        short_share=_decimal(item.get("shortAccount"), "shortAccount"),
        ratio=_decimal(item.get("longShortRatio"), "longShortRatio"),
        received_at=received_at,
        available_at=received_at,
    )


def _parse_taker_flow(
    value: Any, *, symbol: str, period: str, received_at: datetime
) -> TakerFlowObservation:
    item = _require_mapping(value, "taker buy/sell volume")
    return TakerFlowObservation(
        symbol=symbol,
        period=period,
        observed_at=_event_time(item.get("timestamp"), "taker.timestamp", received_at),
        buy_volume=_decimal(item.get("buyVol"), "buyVol"),
        sell_volume=_decimal(item.get("sellVol"), "sellVol"),
        ratio=_decimal(item.get("buySellRatio"), "buySellRatio"),
        received_at=received_at,
        available_at=received_at,
    )


def _parse_kline_flow(
    value: Any, *, symbol: str, interval: str, received_at: datetime
) -> KlineFlow:
    if not isinstance(value, list) or len(value) != 12:
        raise MarketDataContractError("Kline phải là tuple có đúng 12 trường.")
    trade_count = value[8]
    if type(trade_count) is not int or trade_count < 0:
        raise MarketDataContractError("Kline trade count phải là số nguyên không âm.")
    close_time = _from_milliseconds(value[6], "close time")
    flow = KlineFlow(
        symbol=symbol,
        timeframe=interval,
        open_time=_from_milliseconds(value[0], "open time"),
        close_time=close_time,
        volume=_decimal(value[5], "volume"),
        quote_volume=_decimal(value[7], "quote asset volume"),
        trade_count=trade_count,
        taker_buy_volume=_decimal(value[9], "taker buy base volume"),
        taker_buy_quote_volume=_decimal(value[10], "taker buy quote volume"),
        is_closed=close_time < received_at,
        received_at=received_at,
        available_at=received_at,
    )
    if not 0 <= flow.taker_buy_volume <= flow.volume:
        raise MarketDataContractError("Taker buy volume phải nằm trong tổng volume của nến.")
    return flow


def _page(
    response: _RawResponse,
    records: tuple[Any, ...],
    *,
    is_complete: bool = True,
    missing_times: tuple[datetime, ...] = (),
    incomplete_reason: str | None = None,
) -> MarketDataPage[Any]:
    return MarketDataPage(
        records=records,
        raw_payload=response.body,
        checksum=response.checksum,
        received_at=response.received_at,
        available_at=response.received_at,
        is_complete=is_complete,
        missing_times=missing_times,
        incomplete_reason=incomplete_reason,
    )


def _expected_times(
    start: datetime, end: datetime, interval: timedelta, limit: int
) -> tuple[datetime, ...]:
    first = _floor_time(start, interval)
    if first < start.astimezone(UTC):
        first += interval
    last = _floor_time(end, interval)
    count = _range_count(first, last, interval, limit)
    return tuple(first + interval * index for index in range(min(count, _MAX_OI_MISSING_TIMES)))


def _range_count(start: datetime, end: datetime, interval: timedelta, limit: int) -> int:
    return min(int((end - start) // interval) + 1, limit + 1)


def _one_month_before(value: datetime) -> datetime:
    month = value.month - 1
    year = value.year
    if month == 0:
        month = 12
        year -= 1
    day = min(value.day, _days_in_month(year, month))
    return value.replace(year=year, month=month, day=day)


def _floor_time(value: datetime, interval: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = value.astimezone(UTC) - epoch
    return epoch + interval * (elapsed // interval)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=UTC)
    return (next_month - timedelta(days=1)).day


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except TypeError, ValueError, OverflowError:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - now).total_seconds())


def _kline_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


def _add_time_params(
    params: dict[str, str | int], start_time: datetime | None, end_time: datetime | None
) -> None:
    if start_time is not None:
        params["startTime"] = _to_milliseconds(start_time)
    if end_time is not None:
        params["endTime"] = _to_milliseconds(end_time)


def _validate_range(start_time: datetime | None, end_time: datetime | None) -> None:
    for name, value in (("start_time", start_time), ("end_time", end_time)):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{name} phải có múi giờ.")
    if start_time is not None and end_time is not None and start_time > end_time:
        raise ValueError("start_time không được sau end_time; Binance dùng endTime inclusive.")


def _to_milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _from_milliseconds(value: Any, name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketDataContractError(f"{name} phải là số nguyên mili giây.")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise MarketDataContractError(f"{name} phải là chuỗi thập phân từ API.")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise MarketDataContractError(f"{name} không phải số thập phân hợp lệ.") from error
    if not parsed.is_finite():
        raise MarketDataContractError(f"{name} phải hữu hạn.")
    return parsed


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise MarketDataContractError(f"{name} phải là object JSON.")
    return value


def _require_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise MarketDataContractError(f"{name} phải là array JSON.")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketDataContractError(f"{name} phải là chuỗi không rỗng.")
    return value


def _require_symbol(symbol: str) -> None:
    if not symbol or not symbol.isascii() or not symbol.isalnum():
        raise ValueError("symbol chỉ được chứa ký tự ASCII chữ và số.")


def _validate_limit(limit: int, *, maximum: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise ValueError(f"limit phải nằm trong khoảng 1..{maximum}.")
