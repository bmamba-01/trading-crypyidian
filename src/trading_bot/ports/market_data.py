"""Cổng dữ liệu thị trường công khai, tách hợp đồng khỏi HTTP của Binance."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from trading_bot.domain.market.ingestion import InstrumentObservation
from trading_bot.domain.market.models import OHLCVBar


@dataclass(frozen=True, slots=True)
class MarketDataPage[RecordT]:
    """Một phản hồi bất biến, giữ bytes nguồn và checksum để truy vết."""

    records: tuple[RecordT, ...]
    raw_payload: bytes
    checksum: str
    received_at: datetime
    available_at: datetime
    is_complete: bool = True
    missing_times: tuple[datetime, ...] = ()
    incomplete_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExchangeInstrument:
    """Quy cách instrument và metadata USDⓈ-M bổ sung từ exchangeInfo."""

    observation: InstrumentObservation
    contract_type: str
    margin_asset: str


@dataclass(frozen=True, slots=True)
class FundingRateObservation:
    """Một mức funding được sàn công bố cùng thời điểm nhận phản hồi."""

    symbol: str
    funding_time: datetime
    funding_rate: Decimal
    mark_price: Decimal | None
    rate_type: str
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class OpenInterestObservation:
    """Một quan sát open interest theo đơn vị hợp đồng và giá trị nếu có."""

    symbol: str
    observed_at: datetime
    quantity: Decimal
    value: Decimal | None
    received_at: datetime
    available_at: datetime


class MarketDataPort(Protocol):
    """Hợp đồng bất đồng bộ để đọc market data công khai."""

    async def get_exchange_info(self) -> MarketDataPage[ExchangeInstrument]: ...

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> MarketDataPage[OHLCVBar]: ...

    async def get_funding_rates(
        self,
        *,
        symbol: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> MarketDataPage[FundingRateObservation]: ...

    async def get_open_interest(self, symbol: str) -> MarketDataPage[OpenInterestObservation]: ...

    async def get_open_interest_history(
        self,
        *,
        symbol: str,
        period: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 30,
    ) -> MarketDataPage[OpenInterestObservation]: ...


@dataclass(frozen=True, slots=True)
class PremiumIndexObservation:
    """Mark price, index price và funding hiện hành của một hợp đồng."""

    symbol: str
    mark_price: Decimal
    index_price: Decimal
    last_funding_rate: Decimal | None
    next_funding_time: datetime | None
    observed_at: datetime
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class PriceTicker:
    """Giá khớp gần nhất do sàn công bố."""

    symbol: str
    price: Decimal
    observed_at: datetime
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class BookTicker:
    """Giá và khối lượng tốt nhất hai phía sổ lệnh; không gồm lệnh RPI."""

    symbol: str
    bid_price: Decimal
    bid_quantity: Decimal
    ask_price: Decimal
    ask_quantity: Decimal
    observed_at: datetime
    received_at: datetime
    available_at: datetime

    @property
    def spread(self) -> Decimal:
        return self.ask_price - self.bid_price


@dataclass(frozen=True, slots=True)
class Ticker24h:
    """Thống kê cửa sổ trượt 24 giờ; dùng để xếp hạng thanh khoản, không thay cho nến."""

    symbol: str
    last_price: Decimal
    price_change_percent: Decimal
    high_price: Decimal
    low_price: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    open_time: datetime
    close_time: datetime
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class FundingInfo:
    """Chu kỳ và biên funding đã điều chỉnh của một symbol."""

    symbol: str
    funding_interval_hours: int
    rate_cap: Decimal
    rate_floor: Decimal
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class LongShortRatioObservation:
    """Tỷ lệ long/short theo tài khoản hoặc vị thế; là thống kê sàn, không phải dòng lệnh."""

    symbol: str
    kind: str
    period: str
    observed_at: datetime
    long_share: Decimal
    short_share: Decimal
    ratio: Decimal
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class TakerFlowObservation:
    """Khối lượng taker mua/bán trong một period."""

    symbol: str
    period: str
    observed_at: datetime
    buy_volume: Decimal
    sell_volume: Decimal
    ratio: Decimal
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True, slots=True)
class KlineFlow:
    """Số trade và khối lượng taker mua của một nến (cột 8–10 của kline)."""

    symbol: str
    timeframe: str
    open_time: datetime
    close_time: datetime
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_volume: Decimal
    taker_buy_quote_volume: Decimal
    is_closed: bool
    received_at: datetime
    available_at: datetime

    @property
    def taker_sell_volume(self) -> Decimal:
        return self.volume - self.taker_buy_volume


class MarketSnapshotPort(Protocol):
    """Hợp đồng đọc giá hiện tại, funding và thống kê vị thế/dòng lệnh công khai."""

    async def get_premium_index(
        self, symbol: str | None = None
    ) -> MarketDataPage[PremiumIndexObservation]: ...

    async def get_price_tickers(self, symbol: str | None = None) -> MarketDataPage[PriceTicker]: ...

    async def get_book_tickers(self, symbol: str | None = None) -> MarketDataPage[BookTicker]: ...

    async def get_24h_tickers(self, symbol: str | None = None) -> MarketDataPage[Ticker24h]: ...

    async def get_funding_info(self) -> MarketDataPage[FundingInfo]: ...

    async def get_long_short_ratio(
        self,
        *,
        symbol: str,
        kind: str,
        period: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 30,
    ) -> MarketDataPage[LongShortRatioObservation]: ...

    async def get_taker_buy_sell_volume(
        self,
        *,
        symbol: str,
        period: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 30,
    ) -> MarketDataPage[TakerFlowObservation]: ...

    async def get_kline_flows(
        self,
        *,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 500,
    ) -> MarketDataPage[KlineFlow]: ...
