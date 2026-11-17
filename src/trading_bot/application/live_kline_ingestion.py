"""Thu nến đóng thời gian thực vào kho market: stream ghi nối tiếp, REST lấp khoảng trống.

Cursor `covered_until` luôn là open time của nến kế tiếp cần ghi, nên mỗi nến chỉ được append
một lần theo đúng thứ tự. Mọi lần ghi đều phải qua rights gate với mục đích PAPER_DEMO.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from trading_bot.application.binance_rest_backfill import (
    _FIXED_INTERVALS,
    BackfillRightsGate,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.binance_rest_backfill_ingestion import _make_version
from trading_bot.domain.market.ingestion import (
    IngestionCursor,
    IngestionGap,
    InstrumentObservation,
    MarketRecordVersion,
    MarketStream,
)
from trading_bot.domain.market.models import OHLCVBar
from trading_bot.ports.market_data import ExchangeInstrument, MarketDataPage

LIVE_BAR_SOURCE = "BINANCE_USDM"
_SYMBOL = re.compile(r"[A-Z0-9]{3,32}\Z")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MILLISECOND = timedelta(milliseconds=1)


class LiveIngestionError(RuntimeError):
    """Dữ liệu live không khớp hợp đồng ingest hoặc trạng thái cursor đã đổi."""


class LiveBarDisposition(StrEnum):
    """Kết quả xử lý một nến đóng nhận từ stream."""

    APPENDED = "APPENDED"
    DUPLICATE = "DUPLICATE"
    NEEDS_CATCH_UP = "NEEDS_CATCH_UP"


@dataclass(frozen=True, slots=True)
class LiveKlineSubscription:
    """Một chuỗi nến cần thu: symbol USDⓈ-M và interval có độ dài cố định."""

    symbol: str
    interval: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or _SYMBOL.fullmatch(self.symbol) is None:
            raise ValueError("symbol phải là mã USD-M viết hoa, không chứa separator.")
        if self.interval not in _FIXED_INTERVALS:
            raise ValueError("interval phải có độ dài cố định được hỗ trợ.")

    @property
    def duration(self) -> timedelta:
        return _FIXED_INTERVALS[self.interval]

    @property
    def stream(self) -> MarketStream:
        return MarketStream(LIVE_BAR_SOURCE, self.symbol, self.interval)


@dataclass(frozen=True, slots=True)
class CatchUpResult:
    """Tiến độ sau một lần lấp bằng REST; `complete` là đã tới nến đang chạy."""

    subscription: LiveKlineSubscription
    bars_appended: int
    covered_until: datetime
    complete: bool


class LiveKlineRepository(Protocol):
    """Các thao tác kho market dùng chung một transaction của caller."""

    async def get_cursor(
        self, *, workspace_id: UUID, stream: MarketStream, as_of: datetime
    ) -> IngestionCursor | None: ...

    async def save_cursor(self, *, workspace_id: UUID, cursor: IngestionCursor) -> None: ...

    async def get_instrument(
        self, *, workspace_id: UUID, source: str, symbol: str, as_of: datetime
    ) -> Any: ...

    async def append_instrument(
        self,
        *,
        workspace_id: UUID,
        observation: InstrumentObservation,
        idempotency_key: str,
        received_at: datetime | None = None,
        available_at: datetime | None = None,
    ) -> Any: ...

    async def append_bar(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        bar: OHLCVBar,
        version: MarketRecordVersion,
    ) -> object: ...

    async def append_gap(self, *, workspace_id: UUID, gap: IngestionGap) -> UUID: ...


class LiveKlineTransaction(Protocol):
    """Kho market và rights gate gắn cùng một transaction/workspace."""

    @property
    def market(self) -> LiveKlineRepository: ...

    @property
    def rights(self) -> BackfillRightsGate: ...


class LiveKlineMarketData(Protocol):
    """Phần REST công khai cần cho lấp gap và đăng ký instrument."""

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> MarketDataPage[OHLCVBar]: ...

    async def get_exchange_info(self) -> MarketDataPage[ExchangeInstrument]: ...


TransactionFactory = Callable[[], AbstractAsyncContextManager[LiveKlineTransaction]]


class LiveKlineIngestionService:
    """Ghi nến đóng theo thứ tự; request mạng luôn nằm ngoài transaction DB."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        market_data: LiveKlineMarketData,
        transactions: TransactionFactory,
        warmup_bars: int = 300,
        page_limit: int = 1500,
        max_pages: int = 10,
    ) -> None:
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if type(warmup_bars) is not int or not 1 <= warmup_bars <= 1500:
            raise ValueError("warmup_bars phải nằm trong 1..1500.")
        if type(page_limit) is not int or not 1 <= page_limit <= 1500:
            raise ValueError("page_limit phải nằm trong 1..1500.")
        if type(max_pages) is not int or not 1 <= max_pages <= 50:
            raise ValueError("max_pages phải nằm trong 1..50.")
        self._workspace_id = workspace_id
        self._market_data = market_data
        self._transactions = transactions
        self._warmup_bars = warmup_bars
        self._page_limit = page_limit
        self._max_pages = max_pages

    async def catch_up(
        self, subscription: LiveKlineSubscription, *, now: datetime
    ) -> CatchUpResult:
        """Lấp các nến đã đóng từ cursor (hoặc cửa sổ warm-up) tới nến đang chạy bằng REST."""
        instant = _as_utc(now)
        interval = subscription.duration
        end = _floor(instant, interval)
        async with self._transactions() as transaction:
            await self._authorize(transaction, subscription, start=end - interval, now=instant)
            cursor = await transaction.market.get_cursor(
                workspace_id=self._workspace_id, stream=subscription.stream, as_of=instant
            )
            instrument = await transaction.market.get_instrument(
                workspace_id=self._workspace_id,
                source=LIVE_BAR_SOURCE,
                symbol=subscription.symbol,
                as_of=instant,
            )
        planned_from = None if cursor is None else cursor.covered_until
        start = end - interval * self._warmup_bars if planned_from is None else planned_from
        if start >= end:
            return CatchUpResult(subscription, 0, start, True)

        observation: ExchangeInstrument | None = None
        if instrument is None:
            observation = await self._lookup_instrument(subscription.symbol)
        bars: list[OHLCVBar] = []
        position = start
        for _ in range(self._max_pages):
            page = await self._market_data.get_klines(
                symbol=subscription.symbol,
                interval=subscription.interval,
                start_time=position,
                end_time=end - _MILLISECOND,
                limit=self._page_limit,
            )
            closed = sorted(
                (bar for bar in page.records if bar.is_closed and position <= bar.open_time < end),
                key=lambda bar: bar.open_time,
            )
            if not closed:
                break
            for bar in closed:
                self._validate_bar(bar, subscription)
            bars.extend(closed)
            position = closed[-1].open_time + interval
            if position >= end:
                break
        if not bars:
            return CatchUpResult(subscription, 0, start, False)

        async with self._transactions() as transaction:
            await self._authorize(transaction, subscription, start=end - interval, now=instant)
            current = await transaction.market.get_cursor(
                workspace_id=self._workspace_id, stream=subscription.stream, as_of=instant
            )
            if (None if current is None else current.covered_until) != planned_from:
                raise LiveIngestionError("Cursor live đã đổi trong lúc lấp gap; cần chạy lại.")
            instrument_id = await self._instrument_id(
                transaction, subscription, observation=observation, as_of=instant
            )
            expected = start
            for bar in bars:
                if bar.open_time > expected:
                    await transaction.market.append_gap(
                        workspace_id=self._workspace_id,
                        gap=IngestionGap(
                            stream=subscription.stream,
                            gap_start=expected,
                            gap_end=bar.open_time,
                            detected_at=instant,
                        ),
                    )
                await self._append(transaction, instrument_id, bar)
                expected = bar.open_time + interval
            await self._save_cursor(
                transaction, subscription, covered_until=expected, now=instant, previous=current
            )
        return CatchUpResult(subscription, len(bars), expected, expected >= end)

    async def ingest_closed_bar(self, bar: OHLCVBar, *, now: datetime) -> LiveBarDisposition:
        """Ghi một nến đóng từ stream nếu nó đúng là nến kế tiếp của cursor."""
        instant = _as_utc(now)
        if not bar.is_closed:
            raise LiveIngestionError("Chỉ ghi nến đã đóng vào kho market.")
        try:
            subscription = LiveKlineSubscription(bar.symbol, bar.timeframe)
        except ValueError as error:
            raise LiveIngestionError("Nến live có symbol hoặc interval không hợp lệ.") from error
        self._validate_bar(bar, subscription)
        async with self._transactions() as transaction:
            await self._authorize(transaction, subscription, start=bar.open_time, now=instant)
            cursor = await transaction.market.get_cursor(
                workspace_id=self._workspace_id, stream=subscription.stream, as_of=instant
            )
            if cursor is None or bar.open_time > cursor.covered_until:
                return LiveBarDisposition.NEEDS_CATCH_UP
            if bar.open_time < cursor.covered_until:
                return LiveBarDisposition.DUPLICATE
            instrument = await transaction.market.get_instrument(
                workspace_id=self._workspace_id,
                source=LIVE_BAR_SOURCE,
                symbol=subscription.symbol,
                as_of=instant,
            )
            if instrument is None:
                return LiveBarDisposition.NEEDS_CATCH_UP
            await self._append(transaction, instrument.id, bar)
            await self._save_cursor(
                transaction,
                subscription,
                covered_until=bar.open_time + subscription.duration,
                now=instant,
                previous=cursor,
            )
        return LiveBarDisposition.APPENDED

    async def _authorize(
        self,
        transaction: LiveKlineTransaction,
        subscription: LiveKlineSubscription,
        *,
        start: datetime,
        now: datetime,
    ) -> None:
        """Rights gate không có mặc định cho phép; PermissionError dừng mọi thao tác ghi."""
        await transaction.rights.authorize(
            request=RestBackfillRequest(
                symbol=subscription.symbol,
                interval=subscription.interval,
                start=start,
                end=start + subscription.duration,
                purpose=BackfillRightsPurpose.PAPER_DEMO,
            ),
            now=now,
        )

    async def _lookup_instrument(self, symbol: str) -> ExchangeInstrument:
        page = await self._market_data.get_exchange_info()
        for record in page.records:
            if record.observation.instrument.symbol == symbol:
                if record.observation.source != LIVE_BAR_SOURCE:
                    raise LiveIngestionError("exchangeInfo trả nguồn instrument không khớp.")
                return record
        raise LiveIngestionError("Symbol live không có trong exchangeInfo USDⓈ-M.")

    async def _instrument_id(
        self,
        transaction: LiveKlineTransaction,
        subscription: LiveKlineSubscription,
        *,
        observation: ExchangeInstrument | None,
        as_of: datetime,
    ) -> UUID:
        existing = await transaction.market.get_instrument(
            workspace_id=self._workspace_id,
            source=LIVE_BAR_SOURCE,
            symbol=subscription.symbol,
            as_of=as_of,
        )
        if existing is not None:
            return _require_uuid(existing.id)
        if observation is None:
            raise LiveIngestionError("Instrument live biến mất trong lúc lấp gap; cần chạy lại.")
        observed_at = observation.observation.observed_at
        result = await transaction.market.append_instrument(
            workspace_id=self._workspace_id,
            observation=observation.observation,
            idempotency_key=f"{LIVE_BAR_SOURCE}:{subscription.symbol}:{observed_at.isoformat()}",
            received_at=observed_at,
            available_at=observed_at,
        )
        return _require_uuid(result.record_id)

    async def _append(
        self, transaction: LiveKlineTransaction, instrument_id: UUID, bar: OHLCVBar
    ) -> None:
        versioned, version = _make_version(workspace_id=self._workspace_id, bar=bar)
        await transaction.market.append_bar(
            workspace_id=self._workspace_id,
            instrument_id=instrument_id,
            bar=versioned,
            version=version,
        )

    async def _save_cursor(
        self,
        transaction: LiveKlineTransaction,
        subscription: LiveKlineSubscription,
        *,
        covered_until: datetime,
        now: datetime,
        previous: IngestionCursor | None,
    ) -> None:
        updated_at = now if previous is None else max(now, previous.updated_at)
        await transaction.market.save_cursor(
            workspace_id=self._workspace_id,
            cursor=IngestionCursor(
                stream=subscription.stream,
                token=json.dumps(
                    {"next_open_time_ms": int(covered_until.timestamp() * 1000), "version": 1},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                covered_until=covered_until,
                updated_at=updated_at,
            ),
        )

    @staticmethod
    def _validate_bar(bar: OHLCVBar, subscription: LiveKlineSubscription) -> None:
        interval = subscription.duration
        if (
            bar.source != LIVE_BAR_SOURCE
            or bar.symbol != subscription.symbol
            or bar.timeframe != subscription.interval
            or not bar.is_closed
            or _floor(bar.open_time, interval) != bar.open_time
            or bar.close_time != bar.open_time + interval - _MILLISECOND
        ):
            raise LiveIngestionError("Nến live không khớp nguồn, chuỗi hoặc biên interval.")


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now phải có múi giờ.")
    return value.astimezone(UTC)


def _floor(value: datetime, interval: timedelta) -> datetime:
    # Tuần của Binance bắt đầu thứ Hai; 1970-01-05 là thứ Hai đầu tiên sau epoch.
    anchor = _EPOCH + timedelta(days=4) if interval == timedelta(days=7) else _EPOCH
    return anchor + interval * ((value.astimezone(UTC) - anchor) // interval)


def _require_uuid(value: object) -> UUID:
    if type(value) is not UUID:
        raise LiveIngestionError("Instrument live thiếu định danh UUID.")
    return value
