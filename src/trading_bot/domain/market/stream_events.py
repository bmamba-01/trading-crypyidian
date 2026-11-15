"""Sự kiện thị trường thời gian thực đã chuẩn hóa, độc lập với định dạng của sàn."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MarkPriceEvent:
    """Mark price, index price và funding dự kiến tại một thời điểm."""

    symbol: str
    event_at: datetime
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal | None
    next_funding_time: datetime | None
    received_at: datetime
    checksum: str


@dataclass(frozen=True, slots=True)
class LiquidationEvent:
    """Lệnh thanh lý do sàn công bố; mỗi giây sàn chỉ đẩy lệnh lớn nhất của symbol."""

    symbol: str
    side: str
    order_type: str
    time_in_force: str
    status: str
    price: Decimal
    average_price: Decimal
    quantity: Decimal
    filled_quantity: Decimal
    trade_time: datetime
    event_at: datetime
    received_at: datetime
    checksum: str

    @property
    def notional(self) -> Decimal:
        """Giá trị đã khớp theo quote asset."""
        return self.average_price * self.filled_quantity


@dataclass(frozen=True, slots=True)
class AggTradeEvent:
    """Một aggregate trade từ stream; không gồm giao dịch RPI."""

    symbol: str
    aggregate_id: int
    price: Decimal
    quantity: Decimal
    first_trade_id: int
    last_trade_id: int
    trade_time: datetime
    buyer_is_maker: bool
    received_at: datetime

    @property
    def taker_side(self) -> str:
        return "SELL" if self.buyer_is_maker else "BUY"

    @property
    def quote_quantity(self) -> Decimal:
        return self.price * self.quantity


@dataclass(frozen=True, slots=True)
class BookTickerEvent:
    """Giá mua/bán tốt nhất (không gồm lệnh RPI) tại một thời điểm; ``bid <= ask``."""

    symbol: str
    update_id: int
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    event_at: datetime
    received_at: datetime

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price

    @property
    def mid(self) -> Decimal:
        return (self.bid_price + self.ask_price) / 2


MarketStreamEvent = MarkPriceEvent | LiquidationEvent | AggTradeEvent | BookTickerEvent
