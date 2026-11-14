"""Gộp aggregate trade thành bucket dòng lệnh taker theo thời gian.

Bucket chỉ được đánh dấu đầy đủ khi id aggregate trade nối tiếp liên tục từ bucket trước,
trong bucket và sang sự kiện đóng bucket; mọi trường hợp khác là dữ liệu một phần.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading_bot.domain.market.stream_events import AggTradeEvent

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# Chờ thêm sau biên bucket để các sự kiện đến trễ kịp vào trước khi đóng bucket yên lặng.
_FLUSH_GRACE = timedelta(seconds=2)


@dataclass(frozen=True, slots=True)
class TradeFlowBucket:
    """Khối lượng taker mua/bán của một symbol trong một bucket đã đóng."""

    symbol: str
    bucket_start: datetime
    bucket_seconds: int
    buy_quantity: Decimal
    sell_quantity: Decimal
    buy_quote: Decimal
    sell_quote: Decimal
    trade_count: int
    first_aggregate_id: int
    last_aggregate_id: int
    is_complete: bool
    closed_at: datetime

    @property
    def bucket_end(self) -> datetime:
        return self.bucket_start + timedelta(seconds=self.bucket_seconds)

    @property
    def imbalance(self) -> Decimal:
        """(mua − bán) / (mua + bán) theo quote asset; nằm trong [-1, 1]."""
        total = self.buy_quote + self.sell_quote
        return (self.buy_quote - self.sell_quote) / total


@dataclass(slots=True)
class _OpenBucket:
    start: datetime
    buy_quantity: Decimal
    sell_quantity: Decimal
    buy_quote: Decimal
    sell_quote: Decimal
    trade_count: int
    first_aggregate_id: int
    last_aggregate_id: int
    contiguous: bool


class TradeFlowAggregator:
    """Trạng thái gộp theo symbol; không tự đọc đồng hồ để dễ kiểm thử và replay."""

    def __init__(self, *, bucket_seconds: int = 60) -> None:
        if type(bucket_seconds) is not int or bucket_seconds < 1 or 60 % bucket_seconds != 0:
            raise ValueError("bucket_seconds phải là ước của 60.")
        self._bucket = timedelta(seconds=bucket_seconds)
        self._bucket_seconds = bucket_seconds
        self._open: dict[str, _OpenBucket] = {}
        self._last_id: dict[str, int] = {}

    def add(self, event: AggTradeEvent) -> tuple[TradeFlowBucket, ...]:
        """Nhận một aggregate trade; trả bucket vừa đóng (nếu có) của symbol đó."""
        symbol = event.symbol
        last_id = self._last_id.get(symbol)
        if last_id is not None and event.aggregate_id <= last_id:
            return ()
        follows = last_id is not None and event.aggregate_id == last_id + 1
        start = self._floor(event.trade_time)
        current = self._open.get(symbol)
        closed: tuple[TradeFlowBucket, ...] = ()
        if current is not None and start > current.start:
            # Sự kiện đóng bucket phải nối tiếp id; nếu không, có trade của bucket cũ bị mất.
            closed = (self._close(symbol, complete=follows, closed_at=event.received_at),)
            current = None
        elif current is not None and start < current.start:
            self._last_id[symbol] = event.aggregate_id
            current.contiguous = False
            return ()
        if current is None:
            current = _OpenBucket(
                start=start,
                buy_quantity=Decimal(0),
                sell_quantity=Decimal(0),
                buy_quote=Decimal(0),
                sell_quote=Decimal(0),
                trade_count=0,
                first_aggregate_id=event.aggregate_id,
                last_aggregate_id=event.aggregate_id,
                contiguous=follows,
            )
            self._open[symbol] = current
        elif not follows:
            current.contiguous = False
        if event.buyer_is_maker:
            current.sell_quantity += event.quantity
            current.sell_quote += event.quote_quantity
        else:
            current.buy_quantity += event.quantity
            current.buy_quote += event.quote_quantity
        current.trade_count += 1
        current.last_aggregate_id = event.aggregate_id
        self._last_id[symbol] = event.aggregate_id
        return closed

    def flush(self, now: datetime) -> tuple[TradeFlowBucket, ...]:
        """Đóng các bucket đã qua biên cộng thời gian chờ mà chưa có trade mới."""
        due = sorted(
            symbol
            for symbol, bucket in self._open.items()
            if now >= bucket.start + self._bucket + _FLUSH_GRACE
        )
        return tuple(self._close(symbol, complete=True, closed_at=now) for symbol in due)

    def reset(self, now: datetime) -> tuple[TradeFlowBucket, ...]:
        """Kết nối rớt: trả bucket đang mở dưới dạng một phần và quên chuỗi id."""
        buckets = tuple(
            self._close(symbol, complete=False, closed_at=now) for symbol in sorted(self._open)
        )
        self._last_id.clear()
        return buckets

    def _close(self, symbol: str, *, complete: bool, closed_at: datetime) -> TradeFlowBucket:
        bucket = self._open.pop(symbol)
        return TradeFlowBucket(
            symbol=symbol,
            bucket_start=bucket.start,
            bucket_seconds=self._bucket_seconds,
            buy_quantity=bucket.buy_quantity,
            sell_quantity=bucket.sell_quantity,
            buy_quote=bucket.buy_quote,
            sell_quote=bucket.sell_quote,
            trade_count=bucket.trade_count,
            first_aggregate_id=bucket.first_aggregate_id,
            last_aggregate_id=bucket.last_aggregate_id,
            is_complete=complete and bucket.contiguous,
            closed_at=closed_at,
        )

    def _floor(self, value: datetime) -> datetime:
        return _EPOCH + self._bucket * ((value.astimezone(UTC) - _EPOCH) // self._bucket)
