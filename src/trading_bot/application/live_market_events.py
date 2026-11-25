"""Ghi sự kiện thị trường live (mark price, thanh lý, dòng lệnh taker) vào kho market.

Mọi lần ghi đều qua rights gate `PAPER_DEMO` và idempotent theo khóa suy ra từ nội dung sự kiện.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from trading_bot.application.binance_rest_backfill import (
    BackfillRightsGate,
    BackfillRightsPurpose,
    RestBackfillRequest,
)
from trading_bot.application.live_kline_ingestion import LIVE_BAR_SOURCE, LiveIngestionError
from trading_bot.domain.market.stream_events import (
    BookTickerEvent,
    LiquidationEvent,
    MarkPriceEvent,
)
from trading_bot.domain.market.trade_flow import TradeFlowBucket

LiveMarketRecord = MarkPriceEvent | LiquidationEvent | TradeFlowBucket
_MINUTE = timedelta(minutes=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class LiveMarketEventRepository(Protocol):
    """Các thao tác append idempotent; trả True khi bản ghi mới được thêm."""

    async def get_instrument(
        self, *, workspace_id: UUID, source: str, symbol: str, as_of: datetime
    ) -> Any: ...

    async def append_mark_price(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        source: str,
        idempotency_key: str,
        event: MarkPriceEvent,
    ) -> bool: ...

    async def append_liquidation(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        source: str,
        idempotency_key: str,
        event: LiquidationEvent,
    ) -> bool: ...

    async def append_trade_flow(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        source: str,
        idempotency_key: str,
        checksum: str,
        bucket: TradeFlowBucket,
    ) -> bool: ...

    async def upsert_live_quote(
        self,
        *,
        workspace_id: UUID,
        instrument_id: UUID,
        source: str,
        event: BookTickerEvent,
    ) -> bool: ...


class LiveMarketEventTransaction(Protocol):
    @property
    def market(self) -> LiveMarketEventRepository: ...

    @property
    def rights(self) -> BackfillRightsGate: ...


EventTransactionFactory = Callable[[], AbstractAsyncContextManager[LiveMarketEventTransaction]]


class LiveMarketEventService:
    """Ghi một lô sự kiện trong một transaction; không gọi mạng."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        transactions: EventTransactionFactory,
        prune_interval: timedelta = timedelta(hours=1),
    ) -> None:
        if type(workspace_id) is not UUID:
            raise ValueError("workspace_id phải là UUID.")
        if prune_interval <= timedelta(0):
            raise ValueError("prune_interval phải dương.")
        self._workspace_id = workspace_id
        self._transactions = transactions
        self._prune_interval = prune_interval
        self._last_pruned_at: datetime | None = None

    async def prune_expired(self, *, now: datetime) -> dict[str, int]:
        """Dọn các bản ghi WP-07B đã quá hạn ``expires_at`` trong workspace hiện tại."""
        instant = _as_utc(now)
        async with self._transactions() as transaction:
            prune_fn = getattr(transaction.market, "prune_expired_live_market", None)
            if not callable(prune_fn):
                return {}
            deleted = await prune_fn(workspace_id=self._workspace_id, now=instant)
            self._last_pruned_at = instant
            return dict(deleted)

    async def ensure_ready(self, symbols: Sequence[str], *, now: datetime) -> None:
        """Kiểm tra quyền dữ liệu và instrument đã đăng ký trước khi mở stream."""
        instant = _as_utc(now)
        async with self._transactions() as transaction:
            for symbol in symbols:
                await self._authorize(transaction, symbol, instant)
                await self._instrument_id(transaction, symbol, instant)

    async def write(self, records: Sequence[LiveMarketRecord], *, now: datetime) -> int:
        """Append lô sự kiện; trả số bản ghi mới (bản trùng bị bỏ qua)."""
        if not records:
            return 0
        instant = _as_utc(now)
        written = 0
        async with self._transactions() as transaction:
            if (
                self._last_pruned_at is None
                or instant - self._last_pruned_at >= self._prune_interval
            ):
                prune_fn = getattr(transaction.market, "prune_expired_live_market", None)
                if callable(prune_fn):
                    await prune_fn(workspace_id=self._workspace_id, now=instant)
                self._last_pruned_at = instant
            instruments: dict[str, UUID] = {}
            for record in records:
                symbol = record.symbol
                if symbol not in instruments:
                    await self._authorize(transaction, symbol, instant)
                    instruments[symbol] = await self._instrument_id(transaction, symbol, instant)
                written += await self._append(transaction, instruments[symbol], record)
        return written

    async def upsert_quotes(self, events: Sequence[BookTickerEvent], *, now: datetime) -> int:
        """Ghi giá mua/bán mới nhất mỗi symbol (bảng giá trực tiếp, cập nhật tại chỗ).

        Chỉ giữ bản có ``update_id`` lớn nhất của mỗi symbol trong lô; repository bỏ qua bản cũ
        hơn bản đang lưu. Vẫn qua rights gate ``PAPER_DEMO`` như mọi dữ liệu live khác.
        """
        latest: dict[str, BookTickerEvent] = {}
        for event in events:
            current = latest.get(event.symbol)
            if current is None or event.update_id > current.update_id:
                latest[event.symbol] = event
        if not latest:
            return 0
        instant = _as_utc(now)
        written = 0
        async with self._transactions() as transaction:
            for symbol, event in sorted(latest.items()):
                await self._authorize(transaction, symbol, instant)
                instrument_id = await self._instrument_id(transaction, symbol, instant)
                written += await transaction.market.upsert_live_quote(
                    workspace_id=self._workspace_id,
                    instrument_id=instrument_id,
                    source=LIVE_BAR_SOURCE,
                    event=event,
                )
        return written

    async def _append(
        self, transaction: LiveMarketEventTransaction, instrument_id: UUID, record: LiveMarketRecord
    ) -> bool:
        prefix = f"{LIVE_BAR_SOURCE}:{record.symbol}"
        if isinstance(record, MarkPriceEvent):
            return await transaction.market.append_mark_price(
                workspace_id=self._workspace_id,
                instrument_id=instrument_id,
                source=LIVE_BAR_SOURCE,
                idempotency_key=f"{prefix}:mark:{_ms(record.event_at)}",
                event=record,
            )
        if isinstance(record, LiquidationEvent):
            return await transaction.market.append_liquidation(
                workspace_id=self._workspace_id,
                instrument_id=instrument_id,
                source=LIVE_BAR_SOURCE,
                # Sàn không cấp id cho lệnh thanh lý; checksum của message phân biệt các lệnh.
                idempotency_key=f"{prefix}:liq:{_ms(record.trade_time)}:{record.checksum[:32]}",
                event=record,
            )
        if isinstance(record, TradeFlowBucket):
            # Hai bucket một phần của cùng một phút (do rớt kết nối) có dải id khác nhau.
            return await transaction.market.append_trade_flow(
                workspace_id=self._workspace_id,
                instrument_id=instrument_id,
                source=LIVE_BAR_SOURCE,
                idempotency_key=(
                    f"{prefix}:flow:{record.bucket_seconds}:{_ms(record.bucket_start)}"
                    f":{record.first_aggregate_id}"
                ),
                checksum=trade_flow_checksum(record),
                bucket=record,
            )
        raise LiveIngestionError("Loại bản ghi live không được hỗ trợ.")

    async def _authorize(
        self, transaction: LiveMarketEventTransaction, symbol: str, now: datetime
    ) -> None:
        start = _EPOCH + _MINUTE * ((now - _EPOCH) // _MINUTE) - _MINUTE
        try:
            request = RestBackfillRequest(
                symbol=symbol,
                interval="1m",
                start=start,
                end=start + _MINUTE,
                purpose=BackfillRightsPurpose.PAPER_DEMO,
            )
        except ValueError as error:
            raise LiveIngestionError("Sự kiện live có symbol không hợp lệ.") from error
        await transaction.rights.authorize(request=request, now=now)

    async def _instrument_id(
        self, transaction: LiveMarketEventTransaction, symbol: str, now: datetime
    ) -> UUID:
        instrument = await transaction.market.get_instrument(
            workspace_id=self._workspace_id, source=LIVE_BAR_SOURCE, symbol=symbol, as_of=now
        )
        if instrument is None or type(instrument.id) is not UUID:
            # Instrument do luồng kline đăng ký từ exchangeInfo; thiếu thì không ghi mù.
            raise LiveIngestionError("Instrument live chưa được đăng ký cho symbol này.")
        return instrument.id


def trade_flow_checksum(bucket: TradeFlowBucket) -> str:
    """Checksum nội dung bucket để phát hiện ghi đè khác payload."""
    payload = {
        "symbol": bucket.symbol,
        "bucket_start": bucket.bucket_start.astimezone(UTC).isoformat(),
        "bucket_seconds": bucket.bucket_seconds,
        "buy_quantity": str(bucket.buy_quantity),
        "sell_quantity": str(bucket.sell_quantity),
        "buy_quote": str(bucket.buy_quote),
        "sell_quote": str(bucket.sell_quote),
        "trade_count": bucket.trade_count,
        "first_aggregate_id": bucket.first_aggregate_id,
        "last_aggregate_id": bucket.last_aggregate_id,
        "is_complete": bucket.is_complete,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now phải có múi giờ.")
    return value.astimezone(UTC)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)
