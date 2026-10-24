"""Hợp đồng bất biến cho dữ liệu thị trường đã chuẩn hóa."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} phải có múi giờ.")


def _require_decimal(value: Decimal, name: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} phải là Decimal hữu hạn.")
    if positive and value <= 0:
        raise ValueError(f"{name} phải lớn hơn 0.")


@dataclass(frozen=True, slots=True)
class Instrument:
    """Quy cách giao dịch chuẩn hóa của một hợp đồng."""

    symbol: str
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    quantity_step: Decimal
    min_notional: Decimal
    status: str = "TRADING"

    def __post_init__(self) -> None:
        for name in ("symbol", "base_asset", "quote_asset", "status"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        _require_decimal(self.tick_size, "tick_size", positive=True)
        _require_decimal(self.quantity_step, "quantity_step", positive=True)
        _require_decimal(self.min_notional, "min_notional")
        if self.min_notional < 0:
            raise ValueError("min_notional không được âm.")


@dataclass(frozen=True, slots=True)
class OHLCVBar:
    """Một nến OHLCV kèm các mốc thời gian cần cho truy vấn point-in-time."""

    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    event_at: datetime
    received_at: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool
    source: str = ""
    source_cursor: str | None = None
    checksum: str | None = None
    raw_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("symbol", "timeframe"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} không được để trống.")
        for name in ("open_time", "close_time", "event_at", "received_at", "available_at"):
            _require_aware(getattr(self, name), name)
        if self.close_time < self.open_time or self.event_at < self.open_time:
            raise ValueError("Thời gian đóng và event_at không được trước open_time.")
        if self.event_at > self.received_at:
            raise ValueError("event_at không được sau received_at.")
        if self.received_at > self.available_at:
            raise ValueError("received_at không được sau available_at.")
        for name in ("open", "high", "low", "close"):
            _require_decimal(getattr(self, name), name, positive=True)
        _require_decimal(self.volume, "volume")
        if self.volume < 0:
            raise ValueError("volume không được âm.")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("Giá OHLC không thỏa quan hệ high/low.")
        if self.high < self.low:
            raise ValueError("high phải lớn hơn hoặc bằng low.")

    @property
    def identity(self) -> tuple[str, str, datetime]:
        """Khóa tự nhiên để nhận diện nến trùng từ cùng thị trường."""
        return (self.symbol, self.timeframe, self.open_time)


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """Tập nến đã có thể quan sát được tại mốc quyết định."""

    as_of: datetime
    bars: tuple[OHLCVBar, ...]

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        if any(bar.available_at > self.as_of for bar in self.bars):
            raise ValueError("Snapshot chứa dữ liệu chưa available tại as_of.")
